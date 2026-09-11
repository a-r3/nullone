#!/usr/bin/env python3
"""Offline tests for the deterministic NullOne release CLI.

All tests use temp directories and synthetic git repositories only.
Never touches ~/.openclaw or real production.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.lib import gitops  # noqa: E402
from ops.lib.policy import (  # noqa: E402
    PolicyError,
    assert_prod_path_safe,
    is_forbidden_prod_path,
    is_forbidden_repo_path,
    load_policy,
    map_repo_to_prod,
)
from ops.lib.release import (  # noqa: E402
    build_plan,
    create_backup,
    detect_drift,
    get_status,
    load_policy_at,
    run_bootstrap,
    run_rollback,
    run_update,
)
from ops.lib.state import (  # noqa: E402
    StateError,
    append_history,
    deploy_lock,
    read_current,
    read_txn,
    validate_safe_metadata,
    write_current,
    write_txn,
)

POLICY_PATH = ROOT / "ops" / "release-policy.json"


def git(*args, cwd, env=None):
    e = dict(os.environ)
    e["GIT_CONFIG_NOSYSTEM"] = "1"
    e["GIT_AUTHOR_NAME"] = "t"
    e["GIT_AUTHOR_EMAIL"] = "t@t"
    e["GIT_COMMITTER_NAME"] = "t"
    e["GIT_COMMITTER_EMAIL"] = "t@t"
    if env:
        e.update(env)
    cp = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=e)
    assert cp.returncode == 0, f"git {' '.join(args)} failed: {cp.stderr[:400]}"
    return cp.stdout.strip()


def make_repo(files: dict[str, str], executable: list[str] | None = None) -> Path:
    """Synthetic repo with origin identity a-r3/nullone, origin/main set,
    and a committed copy of the working-tree release policy (deployment
    authority under test comes from this committed blob)."""
    td = Path(tempfile.mkdtemp(prefix="nullone-repo-"))
    git("init", "-b", "main", cwd=td)
    git("remote", "add", "origin", "https://github.com/a-r3/nullone.git", cwd=td)
    pol = td / "ops" / "release-policy.json"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text(POLICY_PATH.read_text())
    for rel, content in files.items():
        p = td / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git("add", "-A", cwd=td)
    for rel in executable or []:
        git("update-index", "--chmod=+x", "--", rel, cwd=td)
    git("commit", "-m", "init", "--no-gpg-sign", cwd=td)
    sha = git("rev-parse", "HEAD", cwd=td)
    git("update-ref", "refs/remotes/origin/main", sha, cwd=td)
    return td


def commit_all(repo: Path, files: dict[str, str] | None = None,
               remove: list[str] | None = None, msg="x",
               executable: list[str] | None = None) -> str:
    for rel, content in (files or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    for rel in (remove or []):
        p = repo / rel
        if p.exists():
            p.unlink()
    git("add", "-A", cwd=repo)
    for rel in executable or []:
        git("update-index", "--chmod=+x", "--", rel, cwd=repo)
    git("commit", "-m", msg, "--no-gpg-sign", cwd=repo)
    sha = git("rev-parse", "HEAD", cwd=repo)
    git("update-ref", "refs/remotes/origin/main", sha, cwd=repo)
    return sha


def materialize(repo: Path, sha: str, prod: Path) -> int:
    """Copy every managed file at sha from git into prod (simulates a
    pre-existing production tree for adoption tests). Returns file count."""
    policy, _ = load_policy_at(repo, sha)
    n = 0
    for rp in gitops.list_files_at(repo, sha):
        if is_forbidden_repo_path(rp, policy):
            continue
        pr = map_repo_to_prod(rp, policy)
        if pr is None or is_forbidden_prod_path(pr, policy):
            continue
        blob = gitops.blob_bytes(repo, sha, rp)
        if blob is None:
            continue
        p = prod / pr
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
        n += 1
    return n


def snapshot_tree(prod: Path) -> dict[str, str]:
    """Map relative path -> sha256 for everything outside deploy-state."""
    out = {}
    for p in sorted(prod.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = str(p.relative_to(prod))
        if rel == "deploy-state" or rel.startswith("deploy-state/"):
            continue
        h = hashlib.sha256()
        h.update(p.read_bytes())
        out[rel] = h.hexdigest()
    return out


BASE_FILES = {
    "workspace/social/ops/scripts/a.py": "print('a1')\n",
    "workspace/social/ops/prompts/morning-editorial.md": "# prompt\n",
}


class ReleaseCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullone-t-")
        self.addCleanup(self.tmp.cleanup)
        self.old_ci = os.environ.get("NULONE_CI_MOCK")
        os.environ["NULONE_CI_MOCK"] = "nullone-success"
        self.addCleanup(self._restore_ci)
        self.policy = load_policy(POLICY_PATH)

    def _restore_ci(self):
        if self.old_ci is None:
            os.environ.pop("NULONE_CI_MOCK", None)
        else:
            os.environ["NULONE_CI_MOCK"] = self.old_ci

    def _prod(self, name="prod") -> Path:
        p = Path(self.tmp.name) / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _adopt(self, repo: Path, prod: Path, sha: str | None = None) -> dict:
        sha = sha or gitops.origin_main_sha(repo)
        materialize(repo, sha, prod)
        return run_bootstrap(repo, prod, baseline_arg=sha, do_fetch=False)

    # -- version --
    def test_version_reports_tool(self):
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "version"],
                            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(cp.returncode, 0)
        self.assertIn("nullone-release", cp.stdout)

    # -- update without state is blocked --
    def test_update_without_state_blocked(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        materialize(repo, gitops.origin_main_sha(repo), prod)
        before = snapshot_tree(prod)
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertIn("BOOTSTRAP_REQUIRED", str(cm.exception))
        # zero production mutations
        self.assertEqual(snapshot_tree(prod), before)
        self.assertIsNone(read_current(prod))
        # preflight agrees: not ready for ordinary update
        from ops.lib.release import run_preflight

        pf = run_preflight(repo, prod, do_fetch=False)
        self.assertTrue(pf["BOOTSTRAP_REQUIRED"])
        self.assertFalse(pf["READY_FOR_UPDATE"])
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "DEPLOY_STATE_MISSING")

    # -- bootstrap exact match --
    def test_bootstrap_exact_match_zero_runtime_writes(self):
        repo = make_repo(dict(BASE_FILES))
        sha = gitops.origin_main_sha(repo)
        prod = self._prod()
        n = materialize(repo, sha, prod)
        self.assertGreater(n, 0)
        before = snapshot_tree(prod)
        res = run_bootstrap(repo, prod, baseline_arg=sha, do_fetch=False)
        self.assertEqual(res["RESULT"], "BOOTSTRAPPED")
        self.assertEqual(res["BASELINE_SHA"], sha)
        # zero runtime file writes
        self.assertEqual(snapshot_tree(prod), before)
        cur = read_current(prod)
        self.assertEqual(cur["deployed_sha"], sha)
        self.assertIn("policy_sha256", cur)
        hist = (prod / "deploy-state" / "history.jsonl").read_text()
        self.assertIn("bootstrap", hist)

    def test_bootstrap_mismatch_blocked_zero_mutations(self):
        repo = make_repo(dict(BASE_FILES))
        sha = gitops.origin_main_sha(repo)
        prod = self._prod()
        materialize(repo, sha, prod)
        (prod / "social" / "ops" / "scripts" / "a.py").write_text("tampered\n")
        before = snapshot_tree(prod)
        with self.assertRaises(Exception) as cm:
            run_bootstrap(repo, prod, baseline_arg=sha, do_fetch=False)
        self.assertIn("BOOTSTRAP_BLOCKED", str(cm.exception))
        self.assertEqual(snapshot_tree(prod), before)
        self.assertIsNone(read_current(prod))

    def test_bootstrap_missing_managed_file_blocked(self):
        repo = make_repo(dict(BASE_FILES))
        sha = gitops.origin_main_sha(repo)
        prod = self._prod()
        materialize(repo, sha, prod)
        (prod / "social" / "ops" / "scripts" / "a.py").unlink()
        with self.assertRaises(Exception) as cm:
            run_bootstrap(repo, prod, baseline_arg=sha, do_fetch=False)
        self.assertIn("BOOTSTRAP_BLOCKED", str(cm.exception))
        self.assertIsNone(read_current(prod))

    def test_bootstrap_unknown_files_preserved(self):
        repo = make_repo(dict(BASE_FILES))
        sha = gitops.origin_main_sha(repo)
        prod = self._prod()
        materialize(repo, sha, prod)
        (prod / "operator-notes.txt").write_text("keep me\n")
        (prod / "social" / "ops" / "run-outcomes").mkdir(parents=True, exist_ok=True)
        (prod / "social" / "ops" / "run-outcomes" / "custom.json").write_text("{}")
        res = run_bootstrap(repo, prod, baseline_arg=sha, do_fetch=False)
        self.assertEqual(res["RESULT"], "BOOTSTRAPPED")
        self.assertEqual((prod / "operator-notes.txt").read_text(), "keep me\n")
        self.assertTrue((prod / "social" / "ops" / "run-outcomes" / "custom.json").is_file())

    def test_bootstrap_symlink_ancestor_blocked(self):
        repo = make_repo(dict(BASE_FILES))
        sha = gitops.origin_main_sha(repo)
        prod = self._prod()
        outside = Path(self.tmp.name) / "outside"
        (outside / "social" / "ops" / "scripts").mkdir(parents=True, exist_ok=True)
        (prod / "social").symlink_to(outside / "social", target_is_directory=True)
        with self.assertRaises(Exception) as cm:
            run_bootstrap(repo, prod, baseline_arg=sha, do_fetch=False)
        self.assertIn("BOOTSTRAP_BLOCKED", str(cm.exception))
        self.assertIsNone(read_current(prod))

    def test_bootstrap_bad_baseline_rejected(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        with self.assertRaises(Exception) as cm:
            run_bootstrap(repo, prod, baseline_arg="abc123", do_fetch=False)
        self.assertIn("FULL_SHA", str(cm.exception))
        git("checkout", "-b", "side", cwd=repo)
        git("commit", "--allow-empty", "-m", "side", "--no-gpg-sign", cwd=repo)
        side_sha = git("rev-parse", "HEAD", cwd=repo)
        git("checkout", "main", cwd=repo)
        with self.assertRaises(Exception) as cm:
            run_bootstrap(repo, prod, baseline_arg=side_sha, do_fetch=False)
        self.assertIn("NOT_ON_MAIN", str(cm.exception))

    # -- status / update flow after adoption --
    def test_status_up_to_date_then_available(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        sha1 = gitops.origin_main_sha(repo)
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "UP_TO_DATE")
        self.assertEqual(st["DEPLOYED_SHA"], sha1)
        sha2 = commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "UPDATE_AVAILABLE")
        self.assertEqual(st["AVAILABLE_SHA"], sha2)

    def test_exact_target_sha_recorded(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        sha1 = gitops.origin_main_sha(repo)
        cur = read_current(prod)
        self.assertEqual(cur["deployed_sha"], sha1)
        self.assertRegex(cur["deployed_sha"], r"^[0-9a-f]{40}$")
        self.assertRegex(cur["policy_sha256"], r"^[0-9a-f]{64}$")

    def test_non_main_target_rejected(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        git("checkout", "-b", "side", cwd=repo)
        git("commit", "--allow-empty", "-m", "side", "--no-gpg-sign", cwd=repo)
        side_sha = git("rev-parse", "HEAD", cwd=repo)
        git("checkout", "main", cwd=repo)
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, target_arg=side_sha, assume_yes=True, do_fetch=False)
        self.assertIn("TARGET_NOT_ON_MAIN", str(cm.exception))

    def test_short_sha_rejected(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, target_arg="abc123", assume_yes=True, do_fetch=False)
        self.assertIn("TARGET_MUST_BE_FULL_SHA", str(cm.exception))

    # -- CI gate: NullOne CI specifically --
    def test_ci_unproven_rejected(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        for mode, needle in [
            ("nullone-failure", "CI_FAILED"),
            ("nullone-cancelled", "CI_FAILED"),
            ("nullone-pending", "CI_PENDING"),
            ("nullone-queued", "CI_PENDING"),
            ("nullone-missing", "CI_MISSING"),
            ("unrelated-only", "CI_MISSING"),
            ("unrelated-only-plus-pending", "CI_PENDING"),
            ("ci-error", "CI_STATUS_UNKNOWN"),
            ("failure", "CI_FAILED"),
            ("pending", "CI_PENDING"),
            ("missing", "CI_MISSING"),
            ("error", "CI_STATUS_UNKNOWN"),
        ]:
            os.environ["NULONE_CI_MOCK"] = mode
            with self.assertRaises(Exception) as cm:
                run_update(repo, prod, assume_yes=True, do_fetch=False)
            self.assertIn("CI_GATE_BLOCKED", str(cm.exception), mode)
            self.assertIn(needle, str(cm.exception), mode)
        os.environ["NULONE_CI_MOCK"] = "nullone-success"

    def test_ci_nullone_success_passes(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        os.environ["NULONE_CI_MOCK"] = "nullone-success"
        res = self._adopt(repo, prod)
        self.assertEqual(res["RESULT"], "BOOTSTRAPPED")

    # -- target-commit policy authority --
    def test_target_commit_policy_authority(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        committed_policy, committed_digest = load_policy_at(repo, gitops.origin_main_sha(repo))
        # mutate the working-tree policy WITHOUT committing: redirect scripts
        # elsewhere and bump nothing. Deployment plan must be unaffected.
        real_text = POLICY_PATH.read_text()
        try:
            import json as _json

            mutated = _json.loads(real_text)
            for m in mutated["mappings"]:
                if m["repo_prefix"] == "workspace/social/ops/scripts/":
                    m["prod_prefix"] = "social/EVIL/"
            POLICY_PATH.write_text(_json.dumps(mutated, indent=2))
            # working-tree loader sees the mutation ...
            wt = load_policy(POLICY_PATH)
            self.assertEqual(
                [m["prod_prefix"] for m in wt["mappings"]
                 if m["repo_prefix"] == "workspace/social/ops/scripts/"],
                ["social/EVIL/"])
            # ... but deployment authority still comes from the commit blob.
            policy2, digest2 = load_policy_at(repo, gitops.origin_main_sha(repo))
            self.assertEqual(digest2, committed_digest)
            commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
            # NOTE: commit_all does not touch ops/release-policy.json, so the
            # committed policy at the new target is unchanged.
            res = run_update(repo, prod, assume_yes=True, do_fetch=False)
            self.assertEqual(res["RESULT"], "UPDATED")
            self.assertEqual(res["POLICY_SHA256"], committed_digest)
            self.assertEqual(
                (prod / "social" / "ops" / "scripts" / "a.py").read_text(), "print('a2')\n")
            self.assertFalse((prod / "social" / "EVIL").exists())
        finally:
            POLICY_PATH.write_text(real_text)

    # -- clean update add/update/remove + unmanaged preserved --
    def test_clean_update_add_update_remove_and_unmanaged_preserved(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        (prod / "social" / "ops" / "run-outcomes").mkdir(parents=True, exist_ok=True)
        unmanaged = prod / "social" / "ops" / "run-outcomes" / "custom.json"
        unmanaged.write_text('{"keep": true}')
        (prod / "notes.txt").write_text("operator notes")
        commit_all(repo,
                   {"workspace/social/ops/scripts/a.py": "print('a2')\n",
                    "workspace/social/ops/scripts/b.py": "print('b')\n"},
                   remove=["workspace/social/ops/prompts/morning-editorial.md"],
                   msg="v2")
        res = run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertEqual(res["RESULT"], "UPDATED")
        self.assertEqual((prod / "social" / "ops" / "scripts" / "a.py").read_text(), "print('a2')\n")
        self.assertEqual((prod / "social" / "ops" / "scripts" / "b.py").read_text(), "print('b')\n")
        self.assertFalse((prod / "social" / "ops" / "prompts" / "morning-editorial.md").exists())
        self.assertTrue(unmanaged.is_file())
        self.assertTrue((prod / "notes.txt").is_file())
        stray = prod / "social" / "ops" / "scripts" / "operator-local.py"
        stray.write_text("local\n")
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a3')\n"}, msg="v3")
        run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertTrue(stray.is_file())

    def test_forbidden_path_rejected(self):
        bad_policy = json.loads(POLICY_PATH.read_text())
        for m in bad_policy["mappings"]:
            if m.get("repo_prefix") == "workspace/social/tools/":
                m["prod_prefix"] = "social/state/tools/"
        repo = make_repo({
            "workspace/social/ops/scripts/a.py": "print('a1')\n",
            "workspace/social/tools/render_x.py": "print('x')\n",
        })
        prod = self._prod()
        plan = build_plan(repo, bad_policy, gitops.origin_main_sha(repo), prod)
        self.assertTrue(plan["FORBIDDEN"])
        with self.assertRaises(Exception):
            from ops.lib.release import check_no_forbidden

            check_no_forbidden(plan)

    def test_mutable_state_paths_cannot_be_managed(self):
        for forbidden in ["social/state/x.json", "social/ops/manifests/m.json",
                          "social/ops/run-outcomes/r.json", "deploy-state/current.json",
                          ".openclaw/config.json", "secrets/token.env"]:
            self.assertTrue(is_forbidden_prod_path(forbidden, self.policy), forbidden)

    def test_docs_tests_cannot_be_deployed(self):
        for rp in ["docs/ARCHITECTURE.md", "tests/test_x.py",
                   "NULLONE_PROJECT_CONTEXT.md", ".github/workflows/ci.yml"]:
            self.assertTrue(is_forbidden_repo_path(rp, self.policy), rp)
            self.assertIsNone(map_repo_to_prod(rp, self.policy))

    def test_path_traversal_rejected(self):
        prod = self._prod()
        with self.assertRaises(PolicyError):
            assert_prod_path_safe(prod, "../escape.txt", self.policy)
        with self.assertRaises(PolicyError):
            assert_prod_path_safe(prod, "/abs.txt", self.policy)
        with self.assertRaises(PolicyError):
            assert_prod_path_safe(prod, "social/ops/scripts/../../etc/passwd", self.policy)

    def test_symlink_escape_rejected(self):
        prod = self._prod()
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir(exist_ok=True)
        link = prod / "social"
        prod.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PolicyError):
            assert_prod_path_safe(prod, "social/ops/scripts/a.py", self.policy)

    # -- drift blocks update; symlink-safe reads --
    def test_production_drift_blocks_update(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        (prod / "social" / "ops" / "scripts" / "a.py").write_text("tampered\n")
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "DRIFT_DETECTED")
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertIn("DRIFT_BLOCKED", str(cm.exception))

    def test_drift_symlink_ancestor_blocked_before_reads(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        outside = Path(self.tmp.name) / "outside2"
        (outside / "social" / "ops" / "scripts").mkdir(parents=True, exist_ok=True)
        (outside / "social" / "ops" / "scripts" / "a.py").write_text("external\n")
        import shutil as _sh

        _sh.rmtree(prod / "social")
        (prod / "social").symlink_to(outside / "social", target_is_directory=True)
        drift = detect_drift(prod, self.policy)
        self.assertIn("social/ops/scripts/a.py", drift["symlink_blocked"])
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "DRIFT_DETECTED")

    def test_backup_symlink_ancestor_blocked_no_external_copy(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        policy, digest = load_policy_at(repo, gitops.origin_main_sha(repo))
        plan = build_plan(repo, policy, gitops.origin_main_sha(repo), prod, digest)
        outside = Path(self.tmp.name) / "outside3"
        (outside / "social" / "ops" / "scripts").mkdir(parents=True, exist_ok=True)
        (outside / "social" / "ops" / "scripts" / "a.py").write_text("EXTERNAL_SECRET_XYZ\n")
        import shutil as _sh

        _sh.rmtree(prod / "social")
        (prod / "social").symlink_to(outside / "social", target_is_directory=True)
        with self.assertRaises(Exception) as cm:
            create_backup(prod, policy, plan, "0" * 40, gitops.origin_main_sha(repo))
        self.assertIn("BACKUP_BLOCKED", str(cm.exception))
        # no external content may enter deploy-state backups
        found = []
        bdir = prod / "deploy-state" / "backups"
        if bdir.is_dir():
            for p in bdir.rglob("*"):
                if p.is_file() and not p.is_symlink() and p.suffix != ".json":
                    if "EXTERNAL_SECRET_XYZ" in p.read_bytes().decode("utf-8", "replace"):
                        found.append(str(p))
        self.assertEqual(found, [])

    def test_backup_created_before_mutation(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        res = run_update(repo, prod, assume_yes=True, do_fetch=False)
        bid = res["BACKUP_ID"]
        bdir = prod / "deploy-state" / "backups" / bid
        self.assertTrue((bdir / "backup.json").is_file())
        meta = json.loads((bdir / "backup.json").read_text())
        self.assertIn("previous_deployed_sha", meta)
        self.assertIn("target_sha", meta)
        self.assertIn("files", meta)
        self.assertIn("created_at", meta)

    def test_hash_mismatch_causes_rollback(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        before = (prod / "social" / "ops" / "scripts" / "a.py").read_text()
        import ops.lib.gitops as g

        orig = g.blob_bytes
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")

        def bad_blob(root, sha, rp):
            b = orig(root, sha, rp)
            if rp.endswith("scripts/a.py"):
                return b + b"# CORRUPT\n"
            return b

        g.blob_bytes = bad_blob
        try:
            plan = build_plan(repo, self.policy, gitops.origin_main_sha(repo), prod)
            g.blob_bytes = orig
            from ops.lib import release as R

            plan["MANAGED_TARGET"]["social/ops/scripts/a.py"] = "0" * 64
            with self.assertRaises(Exception):
                R.atomic_install(repo, self.policy, prod, plan, gitops.origin_main_sha(repo))
            self.assertEqual((prod / "social" / "ops" / "scripts" / "a.py").read_text(), before)
        finally:
            g.blob_bytes = orig

    def test_interrupted_update_reports_check_required(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        write_txn(prod, {"status": "IN_PROGRESS", "phase": "install",
                         "target_sha": "0" * 40, "started_at": 0})
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "CHECK_REQUIRED")
        self.assertEqual(st["REASON"], "INCOMPLETE_TRANSACTION")

    # -- rollback: one-step bound + drift check --
    def test_rollback_one_step_bound(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        sha_a = gitops.origin_main_sha(repo)
        self._adopt(repo, prod, sha_a)
        sha_b = commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('b')\n"}, msg="B")
        res_b = run_update(repo, prod, assume_yes=True, do_fetch=False)
        bid_ab = res_b["BACKUP_ID"]
        sha_c = commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('c')\n"}, msg="C")
        self.assertEqual(sha_c, gitops.origin_main_sha(repo))
        res_c = run_update(repo, prod, assume_yes=True, do_fetch=False)
        bid_bc = res_c["BACKUP_ID"]
        # while deployed at C: the A->B backup must be rejected ...
        with self.assertRaises(Exception) as cm:
            run_rollback(repo, prod, backup_arg=bid_ab)
        self.assertIn("ROLLBACK_NOT_CURRENT_RELEASE", str(cm.exception))
        self.assertEqual((prod / "social" / "ops" / "scripts" / "a.py").read_text(), "print('c')\n")
        # ... while the B->C backup rolls C -> B exactly one step.
        res = run_rollback(repo, prod, backup_arg=bid_bc)
        self.assertEqual(res["RESULT"], "ROLLED_BACK")
        self.assertEqual(res["RESTORED_SHA"], sha_b)
        self.assertEqual((prod / "social" / "ops" / "scripts" / "a.py").read_text(), "print('b')\n")
        # now current is B: the A->B backup is separately evaluable.
        res2 = run_rollback(repo, prod, backup_arg=bid_ab)
        self.assertEqual(res2["RESTORED_SHA"], sha_a)
        hist = (prod / "deploy-state" / "history.jsonl").read_text()
        self.assertIn("update", hist)
        self.assertIn("rollback", hist)

    def test_rollback_drift_blocked(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('b')\n"}, msg="B")
        res = run_update(repo, prod, assume_yes=True, do_fetch=False)
        (prod / "social" / "ops" / "scripts" / "a.py").write_text("tampered\n")
        with self.assertRaises(Exception) as cm:
            run_rollback(repo, prod, backup_arg=res["BACKUP_ID"])
        self.assertIn("DRIFT_BLOCKED", str(cm.exception))

    def test_invalid_backup_blocks_rollback(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        res = run_update(repo, prod, assume_yes=True, do_fetch=False)
        bid = res["BACKUP_ID"]
        (prod / "deploy-state" / "backups" / bid / "backup.json").write_text("{bad json")
        with self.assertRaises(Exception) as cm:
            run_rollback(repo, prod, backup_arg=bid)
        self.assertIn("BACKUP_INVALID", str(cm.exception))

    def test_ambiguous_rollback_requires_selection(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        run_update(repo, prod, assume_yes=True, do_fetch=False)
        import time as _t

        _t.sleep(1.1)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a3')\n"}, msg="v3")
        run_update(repo, prod, assume_yes=True, do_fetch=False)
        with self.assertRaises(Exception) as cm:
            run_rollback(repo, prod)
        self.assertIn("ROLLBACK_AMBIGUOUS", str(cm.exception))

    def test_concurrent_lock(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        with deploy_lock(prod):
            with self.assertRaises(StateError) as cm:
                with deploy_lock(prod):
                    pass
            self.assertIn("DEPLOY_LOCK_HELD", str(cm.exception))

    # -- restart metadata per mapping --
    def test_restart_metadata_prompt_vs_plugin(self):
        files = {
            "workspace/social/ops/prompts/morning-editorial.md": "# p1\n",
            "plugins/nullone-final-publish/index.js": "module.exports = 1;\n",
            "workspace/social/ops/scripts/a.py": "print('a1')\n",
        }
        repo = make_repo(files)
        prod = self._prod()
        sha1 = gitops.origin_main_sha(repo)
        self._adopt(repo, prod, sha1)
        # prompt-only change => no restart
        commit_all(repo, {"workspace/social/ops/prompts/morning-editorial.md": "# p2\n"}, msg="prompt")
        res = run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertEqual(res["RESULT"], "UPDATED")
        self.assertEqual(res["PLAN"]["RESTART_REQUIRED"], "NO")
        # plugin change => restart required, plugin file listed
        commit_all(repo, {"plugins/nullone-final-publish/index.js": "module.exports = 2;\n"},
                   msg="plugin")
        res = run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertEqual(res["RESULT"], "UPDATED")
        self.assertEqual(res["PLAN"]["RESTART_REQUIRED"], "YES")
        self.assertIn("plugins/nullone-final-publish/index.js", res["PLAN"]["RESTART_FILES"])
        # no plugin change in plan => no false plugin restart requirement
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="scripts")
        policy, digest = load_policy_at(repo, gitops.origin_main_sha(repo))
        plan = build_plan(repo, policy, gitops.origin_main_sha(repo), prod, digest)
        self.assertEqual(plan["RESTART_REQUIRED"], "NO")
        self.assertNotIn("plugins/nullone-final-publish/index.js", plan["RESTART_FILES"])

    # -- file modes from git --
    def test_file_mode_preservation(self):
        repo = make_repo(
            {"workspace/social/ops/scripts/run.sh": "#!/bin/sh\necho hi\n",
             "workspace/social/ops/scripts/a.py": "print('a1')\n"},
            executable=["workspace/social/ops/scripts/run.sh"])
        prod = self._prod()
        sha1 = gitops.origin_main_sha(repo)
        self._adopt(repo, prod, sha1)
        # v2 changes the executable file: install must apply the reviewed git mode
        commit_all(repo, {"workspace/social/ops/scripts/run.sh": "#!/bin/sh\necho hi2\n"},
                   msg="v2", executable=["workspace/social/ops/scripts/run.sh"])
        run_update(repo, prod, assume_yes=True, do_fetch=False)
        st = (prod / "social" / "ops" / "scripts" / "run.sh").stat()
        self.assertTrue(bool(st.st_mode & stat.S_IXUSR), "executable bit preserved from git")
        self.assertEqual(oct(st.st_mode & 0o777), "0o755")
        st_py = (prod / "social" / "ops" / "scripts" / "a.py").stat()
        self.assertFalse(bool(st_py.st_mode & 0o111), "non-executable stays non-executable")
        cur = read_current(prod)
        self.assertEqual(cur["managed_modes"]["social/ops/scripts/run.sh"], "0755")

    def test_install_failure_restores_bytes_and_mode(self):
        repo = make_repo({"workspace/social/ops/scripts/a.py": "print('a1')\n"})
        prod = self._prod()
        self._adopt(repo, prod)
        target = prod / "social" / "ops" / "scripts" / "a.py"
        os.chmod(target, 0o755)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        policy, digest = load_policy_at(repo, gitops.origin_main_sha(repo))
        plan = build_plan(repo, policy, gitops.origin_main_sha(repo), prod, digest)
        plan["MANAGED_TARGET"]["social/ops/scripts/a.py"] = "0" * 64
        from ops.lib import release as R

        with self.assertRaises(Exception):
            R.atomic_install(repo, policy, prod, plan, gitops.origin_main_sha(repo))
        self.assertEqual(target.read_text(), "print('a1')\n")
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o755")

    # -- deploy-state structural safe metadata --
    def test_deploy_state_rejects_content_bearing_payloads(self):
        prod = self._prod()
        bad_current = {
            "deployed_sha": "a" * 40,
            "deployed_at": "2026-01-01T00:00:00Z",
            "release_tool_version": "0.1.0",
            "policy_version": "1.1.0",
            "policy_sha256": "b" * 64,
            "managed_files": {"social/ops/scripts/a.py": "print('evil content, not a hash')\n" * 100},
        }
        with self.assertRaises(StateError):
            write_current(prod, bad_current)
        with self.assertRaises(StateError):
            validate_safe_metadata({**bad_current, "managed_files": {}, "extra_key": 1}, "current")
        with self.assertRaises(StateError):
            validate_safe_metadata({"event": "pwn", "at": "x"}, "history")
        with self.assertRaises(StateError):
            append_history(prod, {"event": "update", "previous_sha": "not-a-sha"})
        with self.assertRaises(StateError):
            write_txn(prod, {"status": "HACKED", "phase": "install"})
        # legitimate writes pass
        write_txn(prod, {"status": "IN_PROGRESS", "phase": "install",
                         "target_sha": "0" * 40, "started_at": 0})
        self.assertIsNotNone(read_txn(prod))

    def test_deploy_state_contains_no_secret_values(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        sha = gitops.origin_main_sha(repo)
        materialize(repo, sha, prod)
        run_bootstrap(repo, prod, baseline_arg=sha, do_fetch=False)
        cur = read_current(prod)
        blob = json.dumps(cur).lower()
        for needle in ["bearer ", "api_key", "zernio_draft_api_token", "ghp_", "presigned"]:
            self.assertNotIn(needle, blob)

    def test_cli_commands_against_fixture(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        env = dict(os.environ, NULONE_CI_MOCK="nullone-success", NULONE_NO_FETCH="1")
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "version"],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "status",
                             "--production-root", str(prod), "--repo-root", str(repo)],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("DEPLOY_STATE_MISSING", cp.stdout)
        # ordinary update refused without bootstrap
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "update",
                             "--production-root", str(prod), "--repo-root", str(repo), "--yes"],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("BOOTSTRAP_REQUIRED", cp.stderr + cp.stdout)
        # bootstrap then update then preflight
        sha = gitops.origin_main_sha(repo)
        materialize(repo, sha, prod)
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "bootstrap",
                             "--production-root", str(prod), "--repo-root", str(repo),
                             "--baseline", sha],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("BOOTSTRAPPED", cp.stdout)
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "update",
                             "--production-root", str(prod), "--repo-root", str(repo), "--yes"],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("TARGET_SHA=", cp.stdout)
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "preflight",
                             "--production-root", str(prod), "--repo-root", str(repo)],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("TARGET_SHA=", cp.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
