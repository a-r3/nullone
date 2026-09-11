"""Deterministic plan / update / bootstrap / rollback / status / preflight."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
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
    mapping_for_prod,
    mapping_restart_required,
    parse_policy_blob,
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
POLICY_BLOB_PATH = "ops/release-policy.json"


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


# ---------- policy authority ----------

def load_policy_at(repo_root: Path, sha: str) -> tuple[dict, str]:
    """Load deployment policy from the exact target commit blob.

    Deployment authorization comes from reviewed Git history, never from the
    local working tree. Returns (policy, policy_sha256).
    """
    blob = gitops.blob_bytes(repo_root, sha, POLICY_BLOB_PATH)
    if blob is None:
        raise ReleaseError(
            f"POLICY_NOT_FOUND_AT_TARGET: {POLICY_BLOB_PATH} absent at {sha[:12]}; "
            "deployment authority requires a reviewed policy at the target commit"
        )
    try:
        policy = parse_policy_blob(blob, source=f"{sha[:12]}:{POLICY_BLOB_PATH}")
    except PolicyError as e:
        raise ReleaseError(str(e)) from e
    return policy, sha256_bytes(blob)


def resolve_policy(repo_root: Path, target_sha: str,
                   policy_override: dict | None = None,
                   override_sha256: str | None = None) -> tuple[dict, str, str]:
    """Return (policy, policy_sha256, source). Override is developer-only and
    must be explicitly passed; production flows always use the commit blob."""
    if policy_override is not None:
        return policy_override, override_sha256 or "working-tree-override", "working-tree-override"
    policy, digest = load_policy_at(repo_root, target_sha)
    return policy, digest, f"{target_sha[:12]}:{POLICY_BLOB_PATH}"


# ---------- plan ----------

def build_plan(repo_root: Path, policy: dict, target_sha: str, prod_root: Path,
               policy_sha256: str | None = None) -> dict:
    """Deterministic plan. No mutation. Returns dict with TARGET_SHA, FILES_* etc."""
    repo_files = gitops.list_files_at(repo_root, target_sha)
    try:
        git_modes = gitops.ls_tree_modes(repo_root, target_sha)
    except gitops.GitError as e:
        raise ReleaseError(str(e)) from e
    managed_target: dict[str, str] = {}  # prod_rel -> expected sha256
    managed_modes: dict[str, str] = {}  # prod_rel -> "0644"/"0755"
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
        mode = git_modes.get(rp, 0o644)
        managed_modes[prod_rel] = "0755" if mode == 0o755 else "0644"

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
            files_add.append(prod_rel)
        elif prod_rel not in prev_managed and cur is None:
            # No deploy state: reported as ADD for visibility, but ordinary
            # update refuses to execute without a bootstrap (BOOTSTRAP_REQUIRED).
            files_add.append(prod_rel)
        elif prod_rel in prev_managed:
            if prev_managed[prod_rel] != exp:
                files_update.append(prod_rel)
            else:
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

    # Per-mapping restart metadata: YES iff any changed file belongs to a
    # restart-required mapping. Mappings without explicit metadata default
    # conservative (restart required) in mapping_restart_required().
    restart_files = [f for f in sorted(set(files_add) | set(files_update) | set(files_remove))
                     if mapping_restart_required(mapping_for_prod(f, policy))]
    restart = "YES" if restart_files else "NO"
    plan = {
        "TARGET_SHA": target_sha,
        "POLICY_VERSION": policy.get("policy_version"),
        "POLICY_SHA256": policy_sha256,
        "RELEASE_TOOL_VERSION": RELEASE_TOOL_VERSION,
        "FILES_ADD": sorted(files_add),
        "FILES_UPDATE": sorted(files_update),
        "FILES_REMOVE": sorted(files_remove),
        "UNCHANGED": sorted(unchanged),
        "FORBIDDEN": sorted(forbidden),
        "RESTART_REQUIRED": restart,
        "RESTART_FILES": sorted(restart_files),
        "VALIDATION_HOOKS": list(policy.get("validation_hooks", [])),
        "MANAGED_TARGET": managed_target,
        "MANAGED_MODES": managed_modes,
    }
    return plan


def check_no_forbidden(plan: dict) -> None:
    if plan.get("FORBIDDEN"):
        raise ReleaseError(f"FORBIDDEN_PLAN: {plan['FORBIDDEN'][:10]}")


# ---------- drift ----------

def detect_drift(prod_root: Path, policy: dict) -> dict:
    """Compare production files vs expected hashes from recorded deployed SHA.

    Path-safety is verified BEFORE any stat/read/hash of a managed path, so
    a symlinked ancestor can never cause external content to be read.
    Returns {status, drifted, symlink_blocked, deployed_sha}. No mutation.
    """
    cur = read_current(prod_root)
    if cur is None:
        return {"status": "DEPLOY_STATE_MISSING", "drifted": [],
                "symlink_blocked": [], "deployed_sha": None}
    deployed_sha = cur.get("deployed_sha")
    if not deployed_sha:
        return {"status": "DEPLOY_STATE_MISSING", "drifted": [],
                "symlink_blocked": [], "deployed_sha": None}
    managed = dict(cur.get("managed_files", {}))
    drifted: list[str] = []
    blocked: list[str] = []
    for prod_rel, exp_hash in sorted(managed.items()):
        try:
            abs_p = assert_prod_path_safe(prod_root, prod_rel, policy)
        except PolicyError:
            blocked.append(prod_rel)
            continue
        if abs_p.is_symlink() or not abs_p.is_file():
            drifted.append(prod_rel)
            continue
        try:
            actual = sha256_file(abs_p)
        except OSError:
            drifted.append(prod_rel)
            continue
        if actual != exp_hash:
            drifted.append(prod_rel)
    status = "CLEAN" if not drifted and not blocked else "DRIFT"
    return {"status": status, "drifted": drifted,
            "symlink_blocked": blocked, "deployed_sha": deployed_sha}


def require_clean_drift(prod_root: Path, policy: dict, operation: str) -> None:
    drift = detect_drift(prod_root, policy)
    if drift["status"] == "DEPLOY_STATE_MISSING":
        raise ReleaseError(f"BOOTSTRAP_REQUIRED: no deploy state; ordinary {operation} refused")
    if drift["symlink_blocked"]:
        raise ReleaseError(f"SYMLINK_BLOCKED_{operation}: {drift['symlink_blocked'][:20]}")
    if drift["status"] == "DRIFT":
        raise ReleaseError(f"DRIFT_BLOCKED: {drift['drifted'][:20]}")


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
        return {"STATUS": "DEPLOY_STATE_MISSING", "DEPLOYED_SHA": None, "AVAILABLE_SHA": avail,
                "BOOTSTRAP_REQUIRED": True}
    deployed = cur.get("deployed_sha")
    avail = available_sha
    if avail is None:
        avail = _safe_origin_sha(repo_root)
    drift = detect_drift(prod_root, policy)
    if drift["symlink_blocked"]:
        return {"STATUS": "DRIFT_DETECTED", "REASON": "SYMLINK_UNSAFE",
                "DEPLOYED_SHA": deployed, "AVAILABLE_SHA": avail,
                "DRIFTED_FILES": drift["drifted"], "BLOCKED_FILES": drift["symlink_blocked"]}
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
        # if node missing: skip, do not fail closed on missing optional toolchain
    # .md and others: no syntax gate


# ---------- backup ----------

def create_backup(prod_root: Path, policy: dict, plan: dict,
                  prev_sha: str | None, target_sha: str) -> tuple[str, Path]:
    """Backup only files the plan will change/remove. Returns (backup_id, backup_dir).

    Path-safety is verified BEFORE any stat/read/hash/copy, so symlinked
    ancestors can never cause external content to enter deploy-state backups.
    Raises ReleaseError if backup cannot be proven complete.
    """
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
        "policy_sha256": plan.get("POLICY_SHA256"),
        "release_tool_version": RELEASE_TOOL_VERSION,
        "files": {},
    }
    todo = sorted(set(plan["FILES_ADD"]) | set(plan["FILES_UPDATE"]) | set(plan["FILES_REMOVE"]))
    for prod_rel in todo:
        try:
            abs_p = assert_prod_path_safe(prod_root, prod_rel, policy)
        except PolicyError as e:
            raise ReleaseError(f"BACKUP_BLOCKED_UNSAFE_PATH {prod_rel}: {e}") from e
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
    for req in ("target_sha", "files", "created_at"):
        if req not in manifest:
            raise ReleaseError(f"BACKUP_INVALID: {backup_id} missing field {req}")
    if "previous_deployed_sha" not in manifest:
        raise ReleaseError(f"BACKUP_INVALID: {backup_id} missing field previous_deployed_sha")
    if not manifest.get("target_sha"):
        raise ReleaseError(f"BACKUP_INVALID: {backup_id} missing target_sha")
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

    New/updated files receive the reviewed Git mode (0644/0755) from the
    target tree. On any failure, restores changed files' prior bytes AND
    modes, then raises."""
    managed_target: dict[str, str] = plan["MANAGED_TARGET"]
    managed_modes: dict[str, str] = plan.get("MANAGED_MODES", {})
    changed: list[str] = []
    originals: dict[str, tuple[bytes | None, int | None]] = {}
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
        # snapshot originals (bytes + mode) for rollback-on-failure
        for prod_rel in sorted(set(plan["FILES_ADD"]) | set(plan["FILES_UPDATE"]) | set(plan["FILES_REMOVE"])):
            try:
                abs_p = assert_prod_path_safe(prod_root, prod_rel, policy)
            except PolicyError as e:
                raise ReleaseError(str(e)) from e
            if abs_p.is_symlink():
                raise ReleaseError(f"INSTALL_BLOCKED_SYMLINK: {prod_rel}")
            if abs_p.is_file():
                originals[prod_rel] = (abs_p.read_bytes(), abs_p.stat().st_mode & 0o777)
            else:
                originals[prod_rel] = (None, None)
        # removals first (only managed removals)
        for prod_rel in sorted(plan["FILES_REMOVE"]):
            abs_p = prod_root / prod_rel
            if abs_p.is_file() and not abs_p.is_symlink():
                abs_p.unlink()
                changed.append(prod_rel)
            # absent -> nothing
        # installs with reviewed Git modes
        for prod_rel in sorted(set(plan["FILES_ADD"]) | set(plan["FILES_UPDATE"])):
            sp = staged / prod_rel
            abs_p = prod_root / prod_rel
            abs_p.parent.mkdir(parents=True, exist_ok=True)
            mode = 0o755 if managed_modes.get(prod_rel) == "0755" else 0o644
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
        # rollback changed files to original bytes AND modes (best effort)
        for prod_rel, (content, mode) in originals.items():
            try:
                abs_p = prod_root / prod_rel
                if content is None:
                    if abs_p.is_file() and not abs_p.is_symlink():
                        abs_p.unlink()
                else:
                    abs_p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = abs_p.with_name(abs_p.name + f".rollback-{os.getpid()}")
                    tmp.write_bytes(content)
                    if mode is not None:
                        os.chmod(tmp, mode)
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
    prod_root: Path,
    target_arg: str | None = None,
    assume_yes: bool = False,
    do_fetch: bool = True,
    confirm_fn=None,
    policy_override: dict | None = None,
    policy_override_sha256: str | None = None,
) -> dict:
    with deploy_lock(prod_root):
        target_sha = gitops.resolve_target(repo_root, target_arg, do_fetch=do_fetch)
        policy, policy_sha256, _ = resolve_policy(
            repo_root, target_sha, policy_override, policy_override_sha256)
        # CI gate BEFORE any mutation
        try:
            _proven, detail = ci_gate.check_ci_success(str(repo_root), target_sha)
        except ci_gate.CIError as e:
            raise ReleaseError(f"CI_GATE_BLOCKED: {e}") from e
        plan = build_plan(repo_root, policy, target_sha, prod_root, policy_sha256)
        check_no_forbidden(plan)
        cur = read_current(prod_root)
        if cur is None:
            # Never silently adopt an existing production tree as a first
            # deployment. One-time adoption requires explicit `bootstrap`.
            raise ReleaseError(
                "BOOTSTRAP_REQUIRED: no deploy state; ordinary update refused "
                "(use explicit `nullone bootstrap --baseline <full-sha>` for one-time adoption)")
        require_clean_drift(prod_root, policy, "UPDATE")
        prev_sha = cur.get("deployed_sha")
        if prev_sha == target_sha:
            return {"RESULT": "ALREADY_UP_TO_DATE", "TARGET_SHA": target_sha,
                    "PLAN": plan, "CI": detail, "POLICY_SHA256": policy_sha256}
        # show plan + require explicit confirmation
        if not assume_yes:
            if confirm_fn is None:
                raise ReleaseError("CONFIRMATION_REQUIRED: rerun with --yes after reviewing plan")
            if not confirm_fn(plan):
                raise ReleaseError("UPDATE_DECLINED")
        write_txn(prod_root, {"status": "IN_PROGRESS", "phase": "backup",
                              "target_sha": target_sha, "started_at": time.time()})
        try:
            backup_id, _bdir = create_backup(prod_root, policy, plan, prev_sha, target_sha)
            write_txn(prod_root, {"status": "IN_PROGRESS", "phase": "install",
                                  "target_sha": target_sha, "backup_id": backup_id,
                                  "started_at": time.time()})
            changed = atomic_install(repo_root, policy, prod_root, plan, target_sha)
            managed_target: dict[str, str] = plan["MANAGED_TARGET"]
            managed_modes: dict[str, str] = plan.get("MANAGED_MODES", {})
            write_current(prod_root, {
                "deployed_sha": target_sha,
                "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "release_tool_version": RELEASE_TOOL_VERSION,
                "policy_version": policy.get("policy_version"),
                "policy_sha256": policy_sha256,
                "managed_files": managed_target,
                "managed_modes": managed_modes,
            })
            append_history(prod_root, {"event": "update", "previous_sha": prev_sha,
                                       "target_sha": target_sha, "backup_id": backup_id,
                                       "changed": changed, "ci": detail,
                                       "policy_sha256": policy_sha256,
                                       "release_tool_version": RELEASE_TOOL_VERSION})
            clear_txn(prod_root)
            return {"RESULT": "UPDATED", "TARGET_SHA": target_sha, "BACKUP_ID": backup_id,
                    "CHANGED": changed, "PLAN": plan, "CI": detail,
                    "POLICY_SHA256": policy_sha256}
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


# ---------- bootstrap (one-time adoption) ----------

def resolve_baseline(repo_root: Path, baseline_arg: str | None, do_fetch: bool = True) -> str:
    """Explicit baseline SHA for one-time adoption. Fails closed."""
    from .gitops import FULL_SHA_RE
    gitops.check_remote_identity(repo_root)
    if do_fetch:
        gitops.fetch_origin_main(repo_root)
    if baseline_arg is None:
        raise ReleaseError("BOOTSTRAP_BASELINE_REQUIRED: pass explicit --baseline <full-sha>")
    sha = baseline_arg.strip().lower()
    if not FULL_SHA_RE.match(sha):
        raise ReleaseError(f"BOOTSTRAP_BASELINE_MUST_BE_FULL_SHA: {baseline_arg!r}")
    if not gitops.commit_exists(repo_root, sha):
        raise ReleaseError(f"BOOTSTRAP_BASELINE_NOT_FOUND: {sha}")
    if not gitops.is_reachable_from_main(repo_root, sha):
        raise ReleaseError(f"BOOTSTRAP_BASELINE_NOT_ON_MAIN: {sha} is not reachable from origin/main")
    return sha


def run_bootstrap(
    repo_root: Path,
    prod_root: Path,
    baseline_arg: str | None = None,
    do_fetch: bool = True,
    policy_override: dict | None = None,
    policy_override_sha256: str | None = None,
) -> dict:
    """One-time adoption of an already-existing production tree.

    Writes deploy-state metadata ONLY when every managed file at the
    baseline matches production byte-for-byte. Zero production file writes
    in all cases; any mismatch blocks with BOOTSTRAP_BLOCKED.
    """
    with deploy_lock(prod_root):
        if read_current(prod_root) is not None:
            raise ReleaseError(
                "BOOTSTRAP_REFUSED_ALREADY_ADOPTED: deploy state exists; use ordinary update")
        baseline_sha = resolve_baseline(repo_root, baseline_arg, do_fetch=do_fetch)
        policy, policy_sha256, _ = resolve_policy(
            repo_root, baseline_sha, policy_override, policy_override_sha256)
        try:
            _proven, detail = ci_gate.check_ci_success(str(repo_root), baseline_sha)
        except ci_gate.CIError as e:
            raise ReleaseError(f"CI_GATE_BLOCKED: {e}") from e
        plan = build_plan(repo_root, policy, baseline_sha, prod_root, policy_sha256)
        check_no_forbidden(plan)
        managed_target: dict[str, str] = plan["MANAGED_TARGET"]
        write_txn(prod_root, {"status": "IN_PROGRESS", "phase": "bootstrap",
                              "target_sha": baseline_sha, "started_at": time.time()})
        try:
            mismatched: list[str] = []
            blocked: list[str] = []
            for prod_rel in sorted(managed_target):
                try:
                    abs_p = assert_prod_path_safe(prod_root, prod_rel, policy)
                except PolicyError:
                    blocked.append(prod_rel)
                    continue
                if abs_p.is_symlink() or not abs_p.is_file():
                    mismatched.append(f"{prod_rel} (missing-or-symlink)")
                    continue
                try:
                    actual = sha256_file(abs_p)
                except OSError:
                    mismatched.append(f"{prod_rel} (unreadable)")
                    continue
                if actual != managed_target[prod_rel]:
                    mismatched.append(prod_rel)
            if blocked:
                raise ReleaseError(f"BOOTSTRAP_BLOCKED_SYMLINK: {blocked[:20]}")
            if mismatched:
                raise ReleaseError(f"BOOTSTRAP_BLOCKED: {len(mismatched)} managed file(s) differ "
                                   f"from baseline {baseline_sha[:12]}: {mismatched[:20]}")
            # Exact match: metadata only, no production file writes.
            write_current(prod_root, {
                "deployed_sha": baseline_sha,
                "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "release_tool_version": RELEASE_TOOL_VERSION,
                "policy_version": policy.get("policy_version"),
                "policy_sha256": policy_sha256,
                "managed_files": managed_target,
                "managed_modes": plan.get("MANAGED_MODES", {}),
            })
            append_history(prod_root, {"event": "bootstrap", "baseline_sha": baseline_sha,
                                       "target_sha": baseline_sha,
                                       "managed_count": len(managed_target),
                                       "ci": detail, "policy_sha256": policy_sha256,
                                       "release_tool_version": RELEASE_TOOL_VERSION})
            clear_txn(prod_root)
            return {"RESULT": "BOOTSTRAPPED", "BASELINE_SHA": baseline_sha,
                    "MANAGED_COUNT": len(managed_target), "CI": detail,
                    "POLICY_SHA256": policy_sha256}
        except Exception as e:
            try:
                txn = read_txn(prod_root)
            except Exception:
                txn = None
            if txn is not None and txn.get("status") == "IN_PROGRESS":
                write_txn(prod_root, {**txn, "status": "INTERRUPTED",
                                      "error": str(e)[:500]})
            raise


# ---------- rollback (one-step, current-release bound) ----------

def list_backups(prod_root: Path) -> list[str]:
    bd = backups_dir(prod_root)
    if not bd.is_dir():
        return []
    return sorted([p.name for p in bd.iterdir() if p.is_dir()])


def run_rollback(repo_root: Path, prod_root: Path,
                 backup_arg: str | None = None,
                 policy_override: dict | None = None,
                 policy_override_sha256: str | None = None) -> dict:
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
        cur = read_current(prod_root)
        if cur is None:
            raise ReleaseError("ROLLBACK_NO_DEPLOY_STATE: cannot bind rollback without current release")
        current_sha = cur.get("deployed_sha")
        # One-step bound: the backup must belong to the currently deployed release.
        if manifest.get("target_sha") != current_sha:
            raise ReleaseError(
                f"ROLLBACK_NOT_CURRENT_RELEASE: backup {backup_id} targets "
                f"{manifest.get('target_sha')[:12]} "
                f"but current release is {(current_sha[:12] if current_sha else 'None')}; zero mutations")
        prev_sha = manifest.get("previous_deployed_sha")
        if prev_sha is not None and not gitops.commit_exists(repo_root, prev_sha):
            raise ReleaseError(f"ROLLBACK_SHA_UNKNOWN: {prev_sha} not found in repo")
        # Policy authority: path safety under the currently deployed (target)
        # release policy; rebuilt truth under the previous release policy.
        if policy_override is not None:
            target_policy, target_digest = policy_override, policy_override_sha256 or "override"
            prev_policy, prev_digest = policy_override, policy_override_sha256 or "override"
        else:
            target_policy, target_digest = load_policy_at(repo_root, manifest["target_sha"])
            if prev_sha is not None:
                prev_policy, prev_digest = load_policy_at(repo_root, prev_sha)
            else:
                prev_policy, prev_digest = target_policy, target_digest
        # Drift check before ordinary rollback (uses deployed-release policy).
        require_clean_drift(prod_root, target_policy, "ROLLBACK")
        write_txn(prod_root, {"status": "IN_PROGRESS", "phase": "rollback",
                              "backup_id": backup_id, "started_at": time.time()})
        try:
            files_dir = backups_dir(prod_root) / backup_id / "files"
            # restore each recorded file; remove files that did not exist before
            for prod_rel in sorted(manifest["files"]):
                entry = manifest["files"][prod_rel]
                abs_p = assert_prod_path_safe(prod_root, prod_rel, target_policy)
                if entry.get("existed"):
                    bp = files_dir / (prod_rel + ".bak")
                    abs_p.parent.mkdir(parents=True, exist_ok=True)
                    mode = 0o644
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
            # rebuild managed truth under the previous release policy.
            if prev_sha:
                prev_plan = build_plan(repo_root, prev_policy, prev_sha, prod_root, prev_digest)
                managed = prev_plan["MANAGED_TARGET"]
                managed_modes = prev_plan.get("MANAGED_MODES", {})
                # verify restored files match prev target where they exist
                for prod_rel, exp in managed.items():
                    abs_p = prod_root / prod_rel
                    if abs_p.is_file() and not abs_p.is_symlink():
                        if sha256_file(abs_p) != exp:
                            raise ReleaseError(f"ROLLBACK_VERIFY_FAILED: {prod_rel}")
                write_current(prod_root, {
                    "deployed_sha": prev_sha,
                    "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "release_tool_version": RELEASE_TOOL_VERSION,
                    "policy_version": prev_policy.get("policy_version"),
                    "policy_sha256": prev_digest,
                    "managed_files": managed,
                    "managed_modes": managed_modes,
                    "rolled_back_from_backup": backup_id,
                })
            else:
                # Rolling back the first deployment un-adopts: no fabricated SHA.
                write_current(prod_root, {
                    "deployed_sha": None,
                    "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "release_tool_version": RELEASE_TOOL_VERSION,
                    "policy_version": target_policy.get("policy_version"),
                    "policy_sha256": target_digest,
                    "managed_files": {},
                    "managed_modes": {},
                    "rolled_back_from_backup": backup_id,
                })
            append_history(prod_root, {"event": "rollback", "backup_id": backup_id,
                                       "restored_sha": prev_sha, "target_sha": current_sha,
                                       "policy_sha256": prev_digest if prev_sha else target_digest,
                                       "release_tool_version": RELEASE_TOOL_VERSION})
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

def run_preflight(repo_root: Path, prod_root: Path,
                  target_arg=None, do_fetch=True,
                  policy_override: dict | None = None,
                  policy_override_sha256: str | None = None) -> dict:
    target_sha = gitops.resolve_target(repo_root, target_arg, do_fetch=do_fetch)
    policy, policy_sha256, _ = resolve_policy(
        repo_root, target_sha, policy_override, policy_override_sha256)
    ci_detail = "UNCHECKED"
    ci_ok = False
    try:
        _ok, ci_detail = ci_gate.check_ci_success(str(repo_root), target_sha)
        ci_ok = True
    except ci_gate.CIError as e:
        ci_detail = str(e)
    plan = build_plan(repo_root, policy, target_sha, prod_root, policy_sha256)
    drift = detect_drift(prod_root, policy)
    status = get_status(repo_root, policy, prod_root, available_sha=target_sha)
    bootstrap_required = drift["status"] == "DEPLOY_STATE_MISSING"
    drift_clean = drift["status"] == "CLEAN"
    ok = ci_ok and drift_clean and not plan.get("FORBIDDEN")
    return {
        "TARGET_SHA": target_sha,
        "POLICY_SHA256": policy_sha256,
        "POLICY_VERSION": policy.get("policy_version"),
        "CI_PROVEN": ci_ok,
        "CI_DETAIL": ci_detail,
        "DRIFT": drift,
        "BOOTSTRAP_REQUIRED": bootstrap_required,
        "PLAN_SUMMARY": {k: (len(v) if isinstance(v, list) else v)
                         for k, v in plan.items()
                         if k not in ("MANAGED_TARGET", "MANAGED_MODES")},
        "STATUS": status.get("STATUS"),
        "RESTART_REQUIRED": plan.get("RESTART_REQUIRED"),
        "READY_FOR_UPDATE": ok,
    }
