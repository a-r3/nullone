"""Deterministic plan / update / rollback / status / preflight."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import ci_gate, gitops
from .policy import (
    PolicyError,
    assert_prod_path_safe,
    is_forbidden_prod_path,
    is_forbidden_repo_path,
    map_repo_to_prod,
)
from .state import (
    append_history,
    backups_dir,
    deploy_lock,
    read_current,
    read_txn,
    write_current,
    write_txn,
    clear_txn,
    ensure_state_dir,
)

RELEASE_TOOL_VERSION = "0.1.0"


class ReleaseError(Exception):
    pass


# ---------- hashing ----------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------- plan ----------

def build_plan(repo_root: Path, policy: dict, target_sha: str, prod_root: Path) -> dict:
    """Deterministic plan. No mutation. Returns dict with TARGET_SHA, FILES_* etc."""
    repo_files = gitops.list_files_at(repo_root, target_sha)
    managed_target: dict[str, str] = {}  # prod_rel -> expected sha256
    forbidden: list[str] = []
    for rp in sorted(repo_files):
        if is_forbidden_repo_path(rp, policy):
            continue
        prod_rel = map_repo_to_prod(rp, policy)
        if prod_rel is None:
            continue
        if is_forbidden_prod_path(prod_rel, policy):
            forbidden.append(f"{rp} -> {prod_rel}")
            continue
        blob = gitops.blob_bytes(repo_root, target_sha, rp)
        if blob is None:
            continue
        managed_target[prod_rel] = sha256_bytes(blob)

    cur = None
    try:
        cur = read_current(prod_root)
    except Exception as e:
        raise ReleaseError(f"DEPLOY_STATE_UNREADABLE: {e}") from e
    prev_managed: dict[str, str] = {}
    if cur is not None:
        prev_managed = dict(cur.get("managed_files", {}))

    files_add: list[str] = []
    files_update: list[str] = []
    unchanged: list[str] = []
    for prod_rel in sorted(managed_target):
        exp = managed_target[prod_rel]
        prod_abs = prod_root / prod_rel
        if prod_rel not in prev_managed and not prod_abs.exists() and cur is not None:
            # new file in target not previously managed -> ADD (also covers first deploy)
            files_add.append(prod_rel)
        elif prod_rel not in prev_managed and cur is None:
            files_add.append(prod_rel)
        elif prod_rel in prev_managed:
            if prev_managed[prod_rel] != exp:
                # content changed between releases -> UPDATE (drift handled separately pre-mutation)
                files_update.append(prod_rel)
            else:
                # same expected hash; check working file lazily? plan reports UNCHANGED;
                # drift detection runs separately before mutation.
                unchanged.append(prod_rel)
        else:
            files_update.append(prod_rel) if prod_abs.exists() else files_add.append(prod_rel)

    # Removals: previously managed but absent from new target
    files_remove: list[str] = []
    target_set = set(managed_target)
    for prod_rel in sorted(prev_managed):
        if prod_rel not in target_set:
            files_remove.append(prod_rel)
    # Never delete unknown production files: only managed removals listed. OK.

    restart = "YES" if policy.get("restart_required") else "NO"
    plan = {
        "TARGET_SHA": target_sha,
        "POLICY_VERSION": policy.get("policy_version"),
        "RELEASE_TOOL_VERSION": RELEASE_TOOL_VERSION,
        "FILES_ADD": sorted(files_add),
        "FILES_UPDATE": sorted(files_update),
        "FILES_REMOVE": sorted(files_remove),
        "UNCHANGED": sorted(unchanged),
        "FORBIDDEN": sorted(forbidden),
        "RESTART_REQUIRED": restart,
        "VALIDATION_HOOKS": list(policy.get("validation_hooks", [])),
        "MANAGED_TARGET": managed_target,
    }
    return plan


def check_no_forbidden(plan: dict) -> None:
    if plan.get("FORBIDDEN"):
        raise ReleaseError(f"FORBIDDEN_PLAN: {plan['FORBIDDEN'][:10]}")


# ---------- drift ----------

def detect_drift(repo_root: Path, prod_root: Path) -> dict:
    """Compare production files vs expected hashes from recorded deployed SHA.
    Returns {status, drifted: [...], deployed_sha}. No mutation."""
    cur = read_current(prod_root)
    if cur is None:
        return {"status": "DEPLOY_STATE_MISSING", "drifted": [], "deployed_sha": None}
    deployed_sha = cur.get("deployed_sha")
    if not deployed_sha:
        return {"status": "DEPLOY_STATE_MISSING", "drifted": [], "deployed_sha": None}
    managed = dict(cur.get("managed_files", {}))
    drifted: list[str] = []
    for prod_rel, exp_hash in sorted(managed.items()):
        abs_p = prod_root / prod_rel
        if not abs_p.is_file() or abs_p.is_symlink():
            # symlink at managed location or missing -> drift (symlink escape risk)
            drifted.append(prod_rel)
            continue
        try:
            actual = sha256_file(abs_p)
        except OSError:
            drifted.append(prod_rel)
            continue
        if actual != exp_hash:
            drifted.append(prod_rel)
    return {"status": "DRIFT" if drifted else "CLEAN", "drifted": drifted, "deployed_sha": deployed_sha}


# ---------- status ----------

def get_status(repo_root: Path, policy: dict, prod_root: Path, available_sha: str | None = None) -> dict:
    txn = None
    try:
        txn = read_txn(prod_root)
    except Exception:
        txn = {"status": "CORRUPT"}
    if txn is not None and txn.get("status") in ("IN_PROGRESS", "CORRUPT", "INTERRUPTED"):
        return {
            "STATUS": "CHECK_REQUIRED",
            "REASON": "INCOMPLETE_TRANSACTION",
            "DETAIL": txn,
            "DEPLOYED_SHA": None,
            "AVAILABLE_SHA": available_sha,
        }
    cur = None
    try:
        cur = read_current(prod_root)
    except Exception as e:
        return {"STATUS": "UNKNOWN", "REASON": f"DEPLOY_STATE_UNREADABLE: {e}",
                "DEPLOYED_SHA": None, "AVAILABLE_SHA": available_sha}
    if cur is None:
        avail = available_sha or _safe_origin_sha(repo_root)
        return {"STATUS": "DEPLOY_STATE_MISSING", "DEPLOYED_SHA": None, "AVAILABLE_SHA": avail}
    deployed = cur.get("deployed_sha")
    avail = available_sha
    if avail is None:
        avail = _safe_origin_sha(repo_root)
    drift = detect_drift(repo_root, prod_root)
    if drift["status"] == "DRIFT":
        return {"STATUS": "DRIFT_DETECTED", "DEPLOYED_SHA": deployed, "AVAILABLE_SHA": avail,
                "DRIFTED_FILES": drift["drifted"]}
    if avail is None:
        return {"STATUS": "UNKNOWN", "DEPLOYED_SHA": deployed, "AVAILABLE_SHA": None}
    if deployed == avail:
        return {"STATUS": "UP_TO_DATE", "DEPLOYED_SHA": deployed, "AVAILABLE_SHA": avail}
    return {"STATUS": "UPDATE_AVAILABLE", "DEPLOYED_SHA": deployed, "AVAILABLE_SHA": avail}


def _safe_origin_sha(repo_root: Path) -> str | None:
    try:
        return gitops.origin_main_sha(repo_root)
    except Exception:
        return None


# ---------- validation hooks ----------

def validate_staged_file(staged_path: Path, prod_rel: str) -> None:
    suffix = staged_path.suffix.lower()
    name = staged_path.name
    if suffix == ".py" or name.endswith(".py"):
        import py_compile

        try:
            py_compile.compile(str(staged_path), doraise=True)
        except py_compile.PyCompileError as e:
            raise ReleaseError(f"VALIDATION_FAILED py_compile {prod_rel}: {e}") from e
    elif suffix == ".json":
        try:
            json.loads(staged_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            raise ReleaseError(f"VALIDATION_FAILED json-parse {prod_rel}: {e}") from e
    elif suffix in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")
        if node is not None:
            cp = subprocess.run([node, "--check", str(staged_path)], capture_output=True, text=True)
            if cp.returncode != 0:
                raise ReleaseError(f"VALIDATION_FAILED node --check {prod_rel}: {cp.stderr.strip()[:400]}")
        # if node missing: skip (recorded by caller), do not fail closed on missing optional toolchain
    # .md and others: no syntax gate


# ---------- backup ----------

def create_backup(prod_root: Path, plan: dict, prev_sha: str | None, target_sha: str) -> tuple[str, Path]:
    """Backup only files the plan will change/remove. Returns (backup_id, backup_dir).
    Raises ReleaseError if backup cannot be proven complete."""
    ensure_state_dir(prod_root)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_id = f"{ts}_{target_sha[:12]}"
    bdir = backups_dir(prod_root) / backup_id
    bdir.mkdir(parents=True, exist_ok=False)
    files_dir = bdir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "backup_id": backup_id,
        "created_at": ts,
        "previous_deployed_sha": prev_sha,
        "target_sha": target_sha,
        "policy_version": plan.get("POLICY_VERSION"),
        "release_tool_version": RELEASE_TOOL_VERSION,
        "files": {},
    }
    todo = sorted(set(plan["FILES_ADD"]) | set(plan["FILES_UPDATE"]) | set(plan["FILES_REMOVE"]))
    for prod_rel in todo:
        abs_p = prod_root / prod_rel
        entry: dict = {"existed": False, "sha256": None, "mode": None}
        if abs_p.is_symlink():
            raise ReleaseError(f"BACKUP_BLOCKED_SYMLINK: {prod_rel}")
        if abs_p.is_file():
            entry["existed"] = True
            try:
                entry["sha256"] = sha256_file(abs_p)
                entry["mode"] = oct(abs_p.stat().st_mode & 0o777)
            except OSError as e:
                raise ReleaseError(f"BACKUP_READ_FAILED {prod_rel}: {e}") from e
            dest = files_dir / (prod_rel + ".bak")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(abs_p), str(dest))
            # prove copy
            if sha256_file(dest) != entry["sha256"]:
                raise ReleaseError(f"BACKUP_INCOMPLETE {prod_rel}: hash mismatch after copy")
        manifest["files"][prod_rel] = entry
    # prove completeness: every todo path has a manifest entry
    if set(manifest["files"]) != set(todo):
        raise ReleaseError("BACKUP_INCOMPLETE: manifest file set mismatch")
    (bdir / "backup.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return backup_id, bdir


def verify_backup(prod_root: Path, backup_id: str) -> dict:
    bdir = backups_dir(prod_root) / backup_id
    meta_p = bdir / "backup.json"
    if not bdir.is_dir() or not meta_p.is_file():
        raise ReleaseError(f"BACKUP_INVALID: {backup_id} missing metadata")
    try:
        manifest = json.loads(meta_p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise ReleaseError(f"BACKUP_INVALID: {backup_id} corrupt metadata: {e}") from e
    for req in ("previous_deployed_sha", "target_sha", "files", "created_at"):
        if req not in manifest:
            raise ReleaseError(f"BACKUP_INVALID: {backup_id} missing field {req}")
    if not isinstance(manifest["files"], dict):
        raise ReleaseError(f"BACKUP_INVALID: {backup_id} files not a mapping")
    files_dir = bdir / "files"
    for prod_rel, entry in manifest["files"].items():
        if ".." in prod_rel.split("/") or prod_rel.startswith("/"):
            raise ReleaseError(f"BACKUP_INVALID: path traversal in {prod_rel!r}")
        if entry.get("existed"):
            bp = files_dir / (prod_rel + ".bak")
            if not bp.is_file():
                raise ReleaseError(f"BACKUP_INCOMPLETE: {backup_id} missing file {prod_rel}")
            if sha256_file(bp) != entry.get("sha256"):
                raise ReleaseError(f"BACKUP_INVALID: {backup_id} hash mismatch {prod_rel}")
    return manifest


# ---------- install ----------

def atomic_install(repo_root: Path, policy: dict, prod_root: Path, plan: dict, target_sha: str) -> list[str]:
    """Stage, validate, atomically install. Verifies hashes. Returns changed files.
    On any failure, restores changed files from staging/originals and raises."""
    managed_target: dict[str, str] = plan["MANAGED_TARGET"]
    changed: list[str] = []
    originals: dict[str, bytes | None] = {}
    staged = Path(tempfile.mkdtemp(prefix="nullone-stage-"))
    try:
        # stage exact blob bytes
        for prod_rel in sorted(set(plan["FILES_ADD"]) | set(plan["FILES_UPDATE"])):
            repo_path = _prod_to_repo(prod_rel, policy)
            if repo_path is None:
                raise ReleaseError(f"INSTALL_UNMAPPED: {prod_rel}")
            blob = gitops.blob_bytes(repo_root, target_sha, repo_path)
            if blob is None:
                raise ReleaseError(f"INSTALL_BLOB_MISSING: {repo_path}@{target_sha[:12]}")
            if sha256_bytes(blob) != managed_target[prod_rel]:
                raise ReleaseError(f"INSTALL_HASH_PLAN_MISMATCH: {prod_rel}")
            sp = staged / prod_rel
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_bytes(blob)
            validate_staged_file(sp, prod_rel)
        # snapshot originals for rollback-on-failure
        for prod_rel in sorted(set(plan["FILES_ADD"]) | set(plan["FILES_UPDATE"]) | set(plan["FILES_REMOVE"])):
            abs_p = prod_root / prod_rel
            try:
                _ = assert_prod_path_safe(prod_root, prod_rel, policy)
            except PolicyError as e:
                raise ReleaseError(str(e)) from e
            if abs_p.is_symlink():
                raise ReleaseError(f"INSTALL_BLOCKED_SYMLINK: {prod_rel}")
            if abs_p.is_file():
                originals[prod_rel] = abs_p.read_bytes()
            else:
                originals[prod_rel] = None
        # removals first (only managed removals)
        for prod_rel in sorted(plan["FILES_REMOVE"]):
            abs_p = prod_root / prod_rel
            if abs_p.is_file() and not abs_p.is_symlink():
                abs_p.unlink()
                changed.append(prod_rel)
            # absent -> nothing
        # installs
        for prod_rel in sorted(set(plan["FILES_ADD"]) | set(plan["FILES_UPDATE"])):
            sp = staged / prod_rel
            abs_p = prod_root / prod_rel
            abs_p.parent.mkdir(parents=True, exist_ok=True)
            # preserve expected permissions
            if abs_p.exists():
                mode = abs_p.stat().st_mode & 0o777
            else:
                mode = 0o644
            tmp = abs_p.with_name(abs_p.name + f".tmp-{os.getpid()}")
            shutil.copy2(str(sp), str(tmp))
            os.chmod(tmp, mode)
            os.replace(tmp, abs_p)
            changed.append(prod_rel)
        # verify every installed hash against exact target content
        for prod_rel in sorted(set(plan["FILES_ADD"]) | set(plan["FILES_UPDATE"])):
            abs_p = prod_root / prod_rel
            actual = sha256_file(abs_p)
            if actual != managed_target[prod_rel]:
                raise ReleaseError(f"HASH_MISMATCH_AFTER_INSTALL: {prod_rel}")
        return sorted(set(changed))
    except Exception:
        # rollback changed files to originals (best effort, fail-closed signal preserved)
        for prod_rel, content in originals.items():
            try:
                abs_p = prod_root / prod_rel
                if content is None:
                    if abs_p.is_file() and not abs_p.is_symlink():
                        abs_p.unlink()
                else:
                    abs_p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = abs_p.with_name(abs_p.name + f".rollback-{os.getpid()}")
                    tmp.write_bytes(content)
                    os.replace(tmp, abs_p)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _prod_to_repo(prod_rel: str, policy: dict) -> str | None:
    for m in policy["mappings"]:
        dest = m["prod_prefix"].rstrip("/") if not m.get("single_file") else m["prod_prefix"]
        src = m["repo_prefix"]
        if m.get("single_file"):
            if prod_rel == dest:
                return src
        else:
            dp = dest.rstrip("/") + "/"
            if prod_rel.startswith(dp):
                return src.rstrip("/") + "/" + prod_rel[len(dp):]
    return None


# ---------- update ----------

def run_update(
    repo_root: Path,
    policy: dict,
    policy_path: Path,
    prod_root: Path,
    target_arg: str | None = None,
    assume_yes: bool = False,
    do_fetch: bool = True,
    confirm_fn=None,
) -> dict:
    with deploy_lock(prod_root):
        # crash-safety: mark transaction early (after lock)
        target_sha = gitops.resolve_target(repo_root, target_arg, do_fetch=do_fetch)
        # CI gate BEFORE any mutation
        try:
            proven, detail = ci_gate.check_ci_success(str(repo_root), target_sha)
        except ci_gate.CIError as e:
            raise ReleaseError(f"CI_GATE_BLOCKED: {e}") from e
        plan = build_plan(repo_root, policy, target_sha, prod_root)
        check_no_forbidden(plan)
        # drift check blocks update
        drift = detect_drift(repo_root, prod_root)
        if drift["status"] == "DRIFT":
            raise ReleaseError(f"DRIFT_BLOCKED: {drift['drifted'][:20]}")
        cur = read_current(prod_root)
        prev_sha = cur.get("deployed_sha") if cur else None
        if prev_sha == target_sha and drift["status"] == "CLEAN":
            return {"RESULT": "ALREADY_UP_TO_DATE", "TARGET_SHA": target_sha, "PLAN": plan, "CI": detail}
        # show plan + require explicit confirmation
        if not assume_yes:
            if confirm_fn is None:
                raise ReleaseError("CONFIRMATION_REQUIRED: rerun with --yes after reviewing plan")
            if not confirm_fn(plan):
                raise ReleaseError("UPDATE_DECLINED")
        write_txn(prod_root, {"status": "IN_PROGRESS", "phase": "backup",
                              "target_sha": target_sha, "started_at": time.time()})
        try:
            backup_id, _bdir = create_backup(prod_root, plan, prev_sha, target_sha)
            write_txn(prod_root, {"status": "IN_PROGRESS", "phase": "install",
                                  "target_sha": target_sha, "backup_id": backup_id,
                                  "started_at": time.time()})
            changed = atomic_install(repo_root, policy, prod_root, plan, target_sha)
            # rebuild managed_files truthfully from target
            managed_target: dict[str, str] = plan["MANAGED_TARGET"]
            write_current(prod_root, {
                "deployed_sha": target_sha,
                "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "release_tool_version": RELEASE_TOOL_VERSION,
                "policy_version": policy.get("policy_version"),
                "policy_path": str(policy_path),
                "managed_files": managed_target,
            })
            append_history(prod_root, {"event": "update", "previous_sha": prev_sha,
                                       "target_sha": target_sha, "backup_id": backup_id,
                                       "changed": changed, "ci": detail})
            clear_txn(prod_root)
            return {"RESULT": "UPDATED", "TARGET_SHA": target_sha, "BACKUP_ID": backup_id,
                    "CHANGED": changed, "PLAN": plan, "CI": detail}
        except Exception as e:
            # leave transaction as INTERRUPTED if we cannot prove outcome
            try:
                txn = read_txn(prod_root)
            except Exception:
                txn = None
            if txn is not None and txn.get("status") == "IN_PROGRESS":
                write_txn(prod_root, {**txn, "status": "INTERRUPTED",
                                      "error": str(e)[:500]})
            raise


# ---------- rollback ----------

def list_backups(prod_root: Path) -> list[str]:
    bd = backups_dir(prod_root)
    if not bd.is_dir():
        return []
    return sorted([p.name for p in bd.iterdir() if p.is_dir()])


def run_rollback(repo_root: Path, policy: dict, prod_root: Path, backup_arg: str | None = None) -> dict:
    with deploy_lock(prod_root):
        backups = list_backups(prod_root)
        if not backups:
            raise ReleaseError("ROLLBACK_NO_BACKUPS")
        if backup_arg is None:
            if len(backups) > 1:
                raise ReleaseError(f"ROLLBACK_AMBIGUOUS: specify --backup one of {backups}")
            backup_id = backups[0]
        else:
            backup_id = backup_arg
            if backup_id not in backups:
                raise ReleaseError(f"ROLLBACK_BACKUP_NOT_FOUND: {backup_id}")
        manifest = verify_backup(prod_root, backup_id)
        prev_sha = manifest.get("previous_deployed_sha")
        if not prev_sha:
            # first-deploy backup has no previous SHA: rollback means removing target files?
            # Refuse fabricated SHA; only allow restore of recorded file state.
            pass
        else:
            if not gitops.commit_exists(repo_root, prev_sha):
                raise ReleaseError(f"ROLLBACK_SHA_UNKNOWN: {prev_sha} not found in repo")
        write_txn(prod_root, {"status": "IN_PROGRESS", "phase": "rollback",
                              "backup_id": backup_id, "started_at": time.time()})
        try:
            files_dir = backups_dir(prod_root) / backup_id / "files"
            # restore each recorded file; remove files that did not exist before
            for prod_rel in sorted(manifest["files"]):
                entry = manifest["files"][prod_rel]
                abs_p = assert_prod_path_safe(prod_root, prod_rel, policy)
                if entry.get("existed"):
                    bp = files_dir / (prod_rel + ".bak")
                    abs_p.parent.mkdir(parents=True, exist_ok=True)
                    mode = abs_p.stat().st_mode & 0o777 if abs_p.exists() else 0o644
                    try:
                        entry_mode = int(entry.get("mode", "0o644"), 8)
                        mode = entry_mode
                    except (ValueError, TypeError):
                        pass
                    tmp = abs_p.with_name(abs_p.name + f".rbtmp-{os.getpid()}")
                    shutil.copy2(str(bp), str(tmp))
                    os.chmod(tmp, mode)
                    os.replace(tmp, abs_p)
                    if sha256_file(abs_p) != entry["sha256"]:
                        raise ReleaseError(f"ROLLBACK_HASH_MISMATCH: {prod_rel}")
                else:
                    if abs_p.is_symlink():
                        raise ReleaseError(f"ROLLBACK_BLOCKED_SYMLINK: {prod_rel}")
                    if abs_p.is_file():
                        abs_p.unlink()
            # rebuild managed truth: if prev_sha known, recompute from git + policy at prev_sha;
            # else (first deploy) managed set is empty.
            if prev_sha:
                prev_plan = build_plan(repo_root, policy, prev_sha, prod_root)
                managed = prev_plan["MANAGED_TARGET"]
                # verify restored files match prev target where they exist
                for prod_rel, exp in managed.items():
                    abs_p = prod_root / prod_rel
                    if abs_p.is_file():
                        if sha256_file(abs_p) != exp:
                            raise ReleaseError(f"ROLLBACK_VERIFY_FAILED: {prod_rel}")
                write_current(prod_root, {
                    "deployed_sha": prev_sha,
                    "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "release_tool_version": RELEASE_TOOL_VERSION,
                    "policy_version": policy.get("policy_version"),
                    "managed_files": managed,
                    "rolled_back_from_backup": backup_id,
                })
            else:
                write_current(prod_root, {
                    "deployed_sha": prev_sha,
                    "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "release_tool_version": RELEASE_TOOL_VERSION,
                    "policy_version": policy.get("policy_version"),
                    "managed_files": {},
                    "rolled_back_from_backup": backup_id,
                })
            append_history(prod_root, {"event": "rollback", "backup_id": backup_id,
                                       "restored_sha": prev_sha})
            clear_txn(prod_root)
            return {"RESULT": "ROLLED_BACK", "BACKUP_ID": backup_id, "RESTORED_SHA": prev_sha}
        except Exception as e:
            try:
                txn = read_txn(prod_root)
            except Exception:
                txn = None
            if txn is not None and txn.get("status") == "IN_PROGRESS":
                write_txn(prod_root, {**txn, "status": "INTERRUPTED", "error": str(e)[:500]})
            raise


# ---------- preflight ----------

def run_preflight(repo_root: Path, policy: dict, prod_root: Path, target_arg=None, do_fetch=True) -> dict:
    target_sha = gitops.resolve_target(repo_root, target_arg, do_fetch=do_fetch)
    ci_detail = "UNCHECKED"
    ci_ok = False
    try:
        _ok, ci_detail = ci_gate.check_ci_success(str(repo_root), target_sha)
        ci_ok = True
    except ci_gate.CIError as e:
        ci_detail = str(e)
    plan = build_plan(repo_root, policy, target_sha, prod_root)
    drift = detect_drift(repo_root, prod_root)
    status = get_status(repo_root, policy, prod_root, available_sha=target_sha)
    return {
        "TARGET_SHA": target_sha,
        "CI_PROVEN": ci_ok,
        "CI_DETAIL": ci_detail,
        "DRIFT": drift,
        "PLAN_SUMMARY": {k: (len(v) if isinstance(v, list) else v)
                         for k, v in plan.items() if k != "MANAGED_TARGET"},
        "STATUS": status.get("STATUS"),
        "RESTART_REQUIRED": plan.get("RESTART_REQUIRED"),
    }
