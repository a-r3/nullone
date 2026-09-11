#!/usr/bin/env python3
"""Offline tests for the deterministic NullOne release CLI.

All tests use temp directories and synthetic git repositories only.
Never touches ~/.openclaw or real production.
"""
from __future__ import annotations

import json
import os
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
    detect_drift,
    get_status,
    run_rollback,
    run_update,
)
from ops.lib.state import (  # noqa: E402
    StateError,
    append_history,
    deploy_lock,
    read_current,
    read_txn,
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


def make_repo(files: dict[str, str]) -> Path:
    td = Path(tempfile.mkdtemp(prefix="nullone-repo-"))
    git("init", "-b", "main", cwd=td)
    git("remote", "add", "origin", "https://github.com/a-r3/nullone.git", cwd=td)
    for rel, content in files.items():
        p = td / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git("add", "-A", cwd=td)
    git("commit", "-m", "init", "--no-gpg-sign", cwd=td)
    sha = git("rev-parse", "HEAD", cwd=td)
    git("update-ref", "refs/remotes/origin/main", sha, cwd=td)
    return td


def commit_all(repo: Path, files: dict[str, str] | None = None, remove: list[str] | None = None, msg="x") -> str:
    for rel, content in (files or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    for rel in (remove or []):
        p = repo / rel
        if p.exists():
            p.unlink()
    git("add", "-A", cwd=repo)
    git("commit", "-m", msg, "--no-gpg-sign", cwd=repo)
    sha = git("rev-parse", "HEAD", cwd=repo)
    git("update-ref", "refs/remotes/origin/main", sha, cwd=repo)
    return sha


BASE_FILES = {
    "workspace/social/ops/scripts/a.py": "print('a1')\n",
    "workspace/social/ops/prompts/morning-editorial.md": "# prompt\n",
}


class ReleaseCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullone-t-")
        self.addCleanup(self.tmp.cleanup)
        self.old_ci = os.environ.get("NULONE_CI_MOCK")
        os.environ["NULONE_CI_MOCK"] = "success"
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

    # -- version --
    def test_version_reports_tool(self):
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "version"],
                            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(cp.returncode, 0)
        self.assertIn("nullone-release", cp.stdout)

    # -- status: missing / up-to-date / available --
    def test_status_missing_then_up_to_date_then_available(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "DEPLOY_STATE_MISSING")
        sha1 = gitops.origin_main_sha(repo)
        res = run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        self.assertEqual(res["RESULT"], "UPDATED")
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "UP_TO_DATE")
        self.assertEqual(st["DEPLOYED_SHA"], sha1)
        sha2 = commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        self.assertNotEqual(sha1, sha2)
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "UPDATE_AVAILABLE")
        self.assertEqual(st["AVAILABLE_SHA"], sha2)

    def test_exact_target_sha_recorded(self):
        repo = make_repo(dict(BASE_FILES))
        sha1 = gitops.origin_main_sha(repo)
        prod = self._prod()
        res = run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        cur = read_current(prod)
        self.assertEqual(cur["deployed_sha"], sha1)
        self.assertRegex(cur["deployed_sha"], r"^[0-9a-f]{40}$")

    def test_non_main_target_rejected(self):
        repo = make_repo(dict(BASE_FILES))
        # side branch not on main
        git("checkout", "-b", "side", cwd=repo)
        git("commit", "--allow-empty", "-m", "side", "--no-gpg-sign", cwd=repo)
        side_sha = git("rev-parse", "HEAD", cwd=repo)
        git("checkout", "main", cwd=repo)
        prod = self._prod()
        with self.assertRaises(Exception) as cm:
            run_update(repo, self.policy, POLICY_PATH, prod, target_arg=side_sha,
                       assume_yes=True, do_fetch=False)
        self.assertIn("TARGET_NOT_ON_MAIN", str(cm.exception))

    def test_short_sha_rejected(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        with self.assertRaises(Exception) as cm:
            run_update(repo, self.policy, POLICY_PATH, prod, target_arg="abc123",
                       assume_yes=True, do_fetch=False)
        self.assertIn("TARGET_MUST_BE_FULL_SHA", str(cm.exception))

    def test_ci_unproven_rejected(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        for mode, needle in [("failure", "CI"), ("pending", "CI"), ("missing", "CI"), ("error", "CI")]:
            os.environ["NULONE_CI_MOCK"] = mode
            with self.assertRaises(Exception) as cm:
                run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
            self.assertIn("CI_GATE_BLOCKED", str(cm.exception))
        os.environ["NULONE_CI_MOCK"] = "success"

    # -- clean update add/update/remove + unmanaged preserved --
    def test_clean_update_add_update_remove_and_unmanaged_preserved(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        # unmanaged production file
        (prod / "social" / "ops" / "run-outcomes").mkdir(parents=True, exist_ok=True)
        unmanaged = prod / "social" / "ops" / "run-outcomes" / "custom.json"
        unmanaged.write_text('{"keep": true}')
        (prod / "notes.txt").write_text("operator notes")
        # v2: update a.py, add b.py, remove prompt file
        commit_all(repo,
                   {"workspace/social/ops/scripts/a.py": "print('a2')\n",
                    "workspace/social/ops/scripts/b.py": "print('b')\n"},
                   remove=["workspace/social/ops/prompts/morning-editorial.md"],
                   msg="v2")
        res = run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        self.assertEqual(res["RESULT"], "UPDATED")
        self.assertEqual((prod / "social" / "ops" / "scripts" / "a.py").read_text(), "print('a2')\n")
        self.assertEqual((prod / "social" / "ops" / "scripts" / "b.py").read_text(), "print('b')\n")
        self.assertFalse((prod / "social" / "ops" / "prompts" / "morning-editorial.md").exists())
        # unmanaged preserved
        self.assertTrue(unmanaged.is_file())
        self.assertTrue((prod / "notes.txt").is_file())
        # unknown prod file never deleted: create stray managed-looking but unmanaged file
        stray = prod / "social" / "ops" / "scripts" / "operator-local.py"
        stray.write_text("local\n")
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a3')\n"}, msg="v3")
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        self.assertTrue(stray.is_file())

    def test_forbidden_path_rejected(self):
        # policy mapping a non-forbidden repo prefix into a forbidden prod
        # destination must surface in FORBIDDEN and refuse install.
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
        # create symlink social -> outside
        if not link.exists():
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PolicyError):
            assert_prod_path_safe(prod, "social/ops/scripts/a.py", self.policy)

    # -- drift blocks update --
    def test_production_drift_blocks_update(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        # tamper managed file
        (prod / "social" / "ops" / "scripts" / "a.py").write_text("tampered\n")
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "DRIFT_DETECTED")
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        with self.assertRaises(Exception) as cm:
            run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        self.assertIn("DRIFT_BLOCKED", str(cm.exception))

    def test_backup_created_before_mutation(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        res = run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
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
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        before = (prod / "social" / "ops" / "scripts" / "a.py").read_text()
        # monkeypatch blob to corrupt install mid-way: patch atomic_install verification by
        # writing a bad managed target? simpler: corrupt gitops.blob_bytes temporarily
        import ops.lib.gitops as g

        orig = g.blob_bytes
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")

        def bad_blob(root, sha, rp):
            b = orig(root, sha, rp)
            if rp.endswith("scripts/a.py"):
                return b + b"# CORRUPT\n"
            return b

        # corrupt the plan's expected hash path: build plan with good bytes, then install with bad bytes
        # easiest: patch blob_bytes only during atomic_install's staging by making installed file differ:
        # we patch sha verify by letting install write then tampering before verify — instead directly
        # test atomic_install rollback by pre-creating a directory where a file should go.
        g.blob_bytes = bad_blob
        try:
            plan = build_plan(repo, self.policy, gitops.origin_main_sha(repo), prod)
            # plan built from corrupted blob; force mismatch by restoring orig for install verify:
            g.blob_bytes = orig
            from ops.lib import release as R

            # tamper plan to have wrong expected hash -> install must fail and restore
            plan["MANAGED_TARGET"]["social/ops/scripts/a.py"] = "0" * 64
            with self.assertRaises(Exception):
                R.atomic_install(repo, self.policy, prod, plan, gitops.origin_main_sha(repo))
            self.assertEqual((prod / "social" / "ops" / "scripts" / "a.py").read_text(), before)
        finally:
            g.blob_bytes = orig

    def test_interrupted_update_reports_check_required(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        write_txn(prod, {"status": "IN_PROGRESS", "phase": "install",
                         "target_sha": "0" * 40, "started_at": 0})
        st = get_status(repo, self.policy, prod)
        self.assertEqual(st["STATUS"], "CHECK_REQUIRED")
        self.assertEqual(st["REASON"], "INCOMPLETE_TRANSACTION")

    def test_rollback_success(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        sha1 = gitops.origin_main_sha(repo)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        self.assertEqual((prod / "social" / "ops" / "scripts" / "a.py").read_text(), "print('a2')\n")
        from ops.lib.release import list_backups

        backups = list_backups(prod)
        self.assertEqual(len(backups), 2)
        # roll back the update that deployed sha2 (previous == sha1)
        target_backup = None
        for bid in backups:
            meta = json.loads((prod / "deploy-state" / "backups" / bid / "backup.json").read_text())
            if meta.get("previous_deployed_sha") == sha1:
                target_backup = bid
        self.assertIsNotNone(target_backup)
        res = run_rollback(repo, self.policy, prod, backup_arg=target_backup)
        self.assertEqual(res["RESULT"], "ROLLED_BACK")
        self.assertEqual(res["RESTORED_SHA"], sha1)
        self.assertEqual((prod / "social" / "ops" / "scripts" / "a.py").read_text(), "print('a1')\n")
        # history preserved (append-only, has both events)
        hist = (prod / "deploy-state" / "history.jsonl").read_text()
        self.assertIn("update", hist)
        self.assertIn("rollback", hist)

    def test_invalid_backup_blocks_rollback(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        res = run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        bid = res["BACKUP_ID"]
        # corrupt backup
        (prod / "deploy-state" / "backups" / bid / "backup.json").write_text("{bad json")
        with self.assertRaises(Exception) as cm:
            run_rollback(repo, self.policy, prod, backup_arg=bid)
        self.assertIn("BACKUP_INVALID", str(cm.exception))

    def test_ambiguous_rollback_requires_selection(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        import time as _t

        _t.sleep(1.1)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a3')\n"}, msg="v3")
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        with self.assertRaises(Exception) as cm:
            run_rollback(repo, self.policy, prod)
        self.assertIn("ROLLBACK_AMBIGUOUS", str(cm.exception))

    def test_concurrent_lock(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        with deploy_lock(prod):
            with self.assertRaises(StateError) as cm:
                with deploy_lock(prod):
                    pass
            self.assertIn("DEPLOY_LOCK_HELD", str(cm.exception))

    def test_deploy_state_contains_no_secrets(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        run_update(repo, self.policy, POLICY_PATH, prod, assume_yes=True, do_fetch=False)
        cur = read_current(prod)
        blob = json.dumps(cur).lower()
        for needle in ["bearer ", "api_key", "zernio_draft_api_token", "ghp_", "presigned"]:
            self.assertNotIn(needle, blob)

    def test_cli_commands_against_fixture(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        env = dict(os.environ, NULONE_CI_MOCK="success", NULONE_NO_FETCH="1")
        for cmd in (["version"],):
            cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), *cmd],
                                capture_output=True, text=True, cwd=str(ROOT), env=env)
            self.assertEqual(cp.returncode, 0, cp.stderr)
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "status",
                             "--production-root", str(prod), "--repo-root", str(repo)],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "update",
                             "--production-root", str(prod), "--repo-root", str(repo), "--yes"],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("TARGET_SHA=", cp.stdout)
        cp = subprocess.run([sys.executable, str(ROOT / "ops" / "nullone"), "preflight",
                             "--production-root", str(prod), "--repo-root", str(repo)],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        # preflight exit 0 when clean+CI proven
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("TARGET_SHA=", cp.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
