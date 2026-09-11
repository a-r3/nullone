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
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.lib import ci_gate, gitops  # noqa: E402

# Original live fetch (setUp patches ci_gate._fetch_runs_via_gh; tests needing
# the real function use this reference to bypass the patch explicitly).
_REAL_FETCH_RUNS = ci_gate._fetch_runs_via_gh
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
    """Map relative path -> sha256 for runtime files (outside deploy-state)."""
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


def _cli_main():
    """Import the extensionless ops/nullone entrypoint for in-process tests."""
    import importlib.util
    from importlib.machinery import SourceFileLoader

    mod = sys.modules.get("nullone_cli_entry")
    if mod is None:
        loader = SourceFileLoader("nullone_cli_entry", str(ROOT / "ops" / "nullone"))
        spec = importlib.util.spec_from_loader("nullone_cli_entry", loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["nullone_cli_entry"] = mod
        loader.exec_module(mod)
    return mod.main


def snapshot_full(prod: Path) -> dict[str, str]:
    """Full-tree snapshot including deploy-state. Symlinks recorded, never followed."""
    import os as _os

    out: dict[str, str] = {}
    if not prod.exists():
        return {"<missing>": "1"}
    for root, dirs, files in _os.walk(prod, followlinks=False):
        for d in sorted(dirs):
            p = Path(root) / d
            rel = str(p.relative_to(prod))
            if p.is_symlink():
                out[rel] = "symlink->" + _os.readlink(p)
            else:
                out[rel + "/"] = "dir"
        for f in sorted(files):
            p = Path(root) / f
            rel = str(p.relative_to(prod))
            if p.is_symlink():
                out[rel] = "symlink->" + _os.readlink(p)
            else:
                h = hashlib.sha256()
                h.update(p.read_bytes())
                out[rel] = "sha256:" + h.hexdigest()
    return out


BASE_FILES = {
    "workspace/social/ops/scripts/a.py": "print('a1')\n",
    "workspace/social/ops/prompts/morning-editorial.md": "# prompt\n",
}


def _runs_for(sha: str, name: str = "NullOne CI", status: str = "completed",
              conclusion: str | None = "success") -> list[dict]:
    return [{"name": name, "head_sha": sha, "status": status, "conclusion": conclusion}]


@contextmanager
def mock_ci(mode: str = "nullone-success"):
    """Explicit Python-only CI adapter injection for offline tests.

    Patches ci_gate._fetch_runs_via_gh; the live check_ci_success path
    (including the real evaluator) is otherwise untouched. No environment
    variable influences the gate.
    """
    other = {"name": "Some Other Check", "status": "completed", "conclusion": "success"}

    def _fetch(sha: str) -> list[dict]:
        nul = {"name": "NullOne CI", "head_sha": sha}
        if mode in ("nullone-success", "success"):
            return [{**nul, "status": "completed", "conclusion": "success"}]
        if mode in ("nullone-failure", "failure"):
            return [{**nul, "status": "completed", "conclusion": "failure"}]
        if mode == "nullone-cancelled":
            return [{**nul, "status": "completed", "conclusion": "cancelled"}]
        if mode in ("nullone-pending", "pending"):
            return [{**nul, "status": "in_progress", "conclusion": None}]
        if mode == "nullone-queued":
            return [{**nul, "status": "queued", "conclusion": None}]
        if mode in ("nullone-missing", "missing"):
            return []
        if mode == "unrelated-only":
            return [{**other, "head_sha": sha}]
        if mode == "unrelated-only-plus-pending":
            return [{**other, "head_sha": sha},
                    {**nul, "status": "in_progress", "conclusion": None}]
        if mode in ("error", "ci-error"):
            raise ci_gate.CIError("CI_STATUS_UNKNOWN (mock transport error)")
        raise AssertionError(f"unknown mock_ci mode: {mode!r}")

    with mock.patch.object(ci_gate, "_fetch_runs_via_gh", side_effect=_fetch):
        yield


class ReleaseCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullone-t-")
        self.addCleanup(self.tmp.cleanup)
        # Default: explicit success adapter for all offline flows.
        patcher = mock.patch.object(
            ci_gate, "_fetch_runs_via_gh",
            side_effect=lambda sha: _runs_for(sha))
        patcher.start()
        self.addCleanup(patcher.stop)
        # The (removed) env bypass must not exist: fail loudly if leaked in.
        self._saved_ci_env = os.environ.pop("NULONE_CI_MOCK", None)
        self.addCleanup(self._restore_ci_env)
        self.policy = load_policy(POLICY_PATH)

    def _restore_ci_env(self):
        if self._saved_ci_env is not None:
            os.environ["NULONE_CI_MOCK"] = self._saved_ci_env
        else:
            os.environ.pop("NULONE_CI_MOCK", None)

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

    # -- update without state is blocked, zero mutation anywhere --
    def test_update_without_state_blocked(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        materialize(repo, gitops.origin_main_sha(repo), prod)
        before = snapshot_full(prod)
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertIn("BOOTSTRAP_REQUIRED", str(cm.exception))
        # zero mutations across the ENTIRE tree, including deploy-state
        after = snapshot_full(prod)
        self.assertEqual(after, before)
        self.assertIsNone(read_current(prod))
        self.assertFalse((prod / "deploy-state").exists(),
                         "UPDATE_WITHOUT_STATE_CREATED_PATHS must be 0")
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
        before = snapshot_full(prod)
        with self.assertRaises(Exception) as cm:
            run_bootstrap(repo, prod, baseline_arg=sha, do_fetch=False)
        self.assertIn("BOOTSTRAP_BLOCKED", str(cm.exception))
        self.assertEqual(snapshot_full(prod), before,
                         "BOOTSTRAP_MISMATCH_CREATED_PATHS must be 0")
        self.assertIsNone(read_current(prod))

    def test_bootstrap_missing_managed_file_blocked(self):
        repo = make_repo(dict(BASE_FILES))
        sha = gitops.origin_main_sha(repo)
        prod = self._prod()
        materialize(repo, sha, prod)
        (prod / "social" / "ops" / "scripts" / "a.py").unlink()
        before = snapshot_full(prod)
        with self.assertRaises(Exception) as cm:
            run_bootstrap(repo, prod, baseline_arg=sha, do_fetch=False)
        self.assertIn("BOOTSTRAP_BLOCKED", str(cm.exception))
        self.assertEqual(snapshot_full(prod), before,
                         "BOOTSTRAP_MISSING_CREATED_PATHS must be 0")
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
        before = snapshot_full(prod)
        with self.assertRaises(Exception) as cm:
            run_bootstrap(repo, prod, baseline_arg=sha, do_fetch=False)
        self.assertIn("BOOTSTRAP_BLOCKED", str(cm.exception))
        self.assertEqual(snapshot_full(prod), before,
                         "BOOTSTRAP_SYMLINK_CREATED_PATHS must be 0")
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

    # -- CI gate: NullOne CI specifically (explicit adapter injection) --
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
            with mock_ci(mode):
                with self.assertRaises(Exception) as cm:
                    run_update(repo, prod, assume_yes=True, do_fetch=False)
            self.assertIn("CI_GATE_BLOCKED", str(cm.exception), mode)
            self.assertIn(needle, str(cm.exception), mode)

    def test_ci_nullone_success_passes(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        with mock_ci("nullone-success"):
            res = self._adopt(repo, prod)
        self.assertEqual(res["RESULT"], "BOOTSTRAPPED")

    def test_ci_env_bypass_has_zero_effect(self):
        # NULONE_CI_MOCK must not influence the live gate: with the env var
        # set and NO adapter patch, the gate still uses the real fetch path.
        from ops.lib import ci_gate as _ci

        seen: dict = {}

        class _Boom(Exception):
            pass

        def _fake_run(cmd, **kw):
            seen["cmd"] = cmd
            raise FileNotFoundError("no gh here")

        os.environ["NULONE_CI_MOCK"] = "nullone-success"
        try:
            with mock.patch.object(ci_gate, "_fetch_runs_via_gh", new=_REAL_FETCH_RUNS):
                with mock.patch.object(_ci.subprocess, "run", side_effect=_fake_run):
                    with self.assertRaises(_ci.CIError) as cm:
                        _ci.check_ci_success(".", "a" * 40)
        finally:
            os.environ.pop("NULONE_CI_MOCK", None)
        self.assertIn("CI_STATUS_UNKNOWN", str(cm.exception))
        self.assertIn("gh", seen.get("cmd", []),
                      "live gate must consult the real adapter despite the env var")

    def test_gh_api_uses_explicit_get(self):
        from unittest import mock

        from ops.lib import ci_gate

        sha = "a" * 40
        cmd = ci_gate.build_runs_command(sha)
        self.assertIn("--method", cmd)
        self.assertEqual(cmd[cmd.index("--method") + 1], "GET")
        # fields must come after the explicit method (shape regression)
        self.assertLess(cmd.index("--method"), cmd.index("-f"))
        self.assertIn(f"head_sha={sha}", cmd)
        # prove the real fetch path issues exactly this shape
        seen: dict = {}

        class _FakeCP:
            returncode = 0
            stdout = '{"total_count": 0, "workflow_runs": []}'
            stderr = ""

        def _fake_run(cmd2, **kw):
            seen["cmd"] = cmd2
            return _FakeCP()

        with mock.patch.object(ci_gate, "_fetch_runs_via_gh", new=_REAL_FETCH_RUNS):
            with mock.patch.object(ci_gate.subprocess, "run", side_effect=_fake_run):
                runs = ci_gate._fetch_runs_via_gh(sha)
        self.assertEqual(runs, [])
        self.assertIn("--method", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("--method") + 1], "GET")

    def test_remote_identity_exact(self):
        from ops.lib.gitops import remote_identity_ok, sanitize_remote_url

        for good in [
            "https://github.com/a-r3/nullone.git",
            "https://github.com/a-r3/nullone",
            "https://github.com/a-r3/nullone/",
            "git@github.com:a-r3/nullone.git",
            "git@github.com:a-r3/nullone",
            "ssh://git@github.com/a-r3/nullone.git",
            "ssh://git@github.com/a-r3/nullone",
            "HTTPS://GITHUB.COM/A-R3/NULLONE.GIT",
        ]:
            self.assertTrue(remote_identity_ok(good), good)
        for evil in [
            "https://github.com/a-r3/nullone-evil",
            "https://github.com/a-r3/nullone-evil.git",
            "https://github.com/evil/a-r3/nullone",
            "https://github.com/a-r3/nullone/extra",
            "https://github.example.com/a-r3/nullone",
            "https://github.com.evil.com/a-r3/nullone",
            "https://notgithub.com/a-r3/nullone",
            "git@github.com:a-r3/nullone-evil.git",
            "git@evil.com:a-r3/nullone.git",
            "other@github.com:a-r3/nullone.git",
            "github.com:a-r3/nullone.git",
            "ssh://other@github.com/a-r3/nullone.git",
            "ssh://git:secret@github.com/a-r3/nullone.git",
            "ssh://github.com/a-r3/nullone.git",
            "https://u:sekret123@github.com/a-r3/nullone.git",
            "https://token123@github.com/a-r3/nullone.git",
            "https://github.com:443/a-r3/nullone",
            "",
            "not a url",
            "https://github.com/a-r3",
            "a-r3/nullone",
        ]:
            self.assertFalse(remote_identity_ok(evil), evil)
        # rejected credential-bearing remotes never leak values in errors
        clean = sanitize_remote_url("https://u:sekret123@github.com/evil/x.git")
        self.assertNotIn("sekret123", clean)
        self.assertNotIn("u:", clean)

    def test_remote_identity_rejected_end_to_end_no_leak(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        git("remote", "set-url", "origin",
            "https://tok:sekret-leak-check@github.com/evil/fork.git", cwd=repo)
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertIn("REMOTE_IDENTITY_MISMATCH", str(cm.exception))
        self.assertNotIn("sekret-leak-check", str(cm.exception))
        self.assertEqual(snapshot_full(prod), {})

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

    # -- restart metadata: workspace changes need no restart --
    def test_restart_metadata_workspace_only(self):
        files = {
            "workspace/social/ops/prompts/morning-editorial.md": "# p1\n",
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
        self.assertEqual(res["PLAN"]["EXTERNAL_COMPONENT_CHANGES"], [])
        # scripts change => still no restart, no external components
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="scripts")
        policy, digest = load_policy_at(repo, gitops.origin_main_sha(repo))
        plan = build_plan(repo, policy, gitops.origin_main_sha(repo), prod, digest)
        self.assertEqual(plan["RESTART_REQUIRED"], "NO")
        self.assertEqual(plan["EXTERNAL_COMPONENT_CHANGES"], [])

    # -- V1 scope: workspace only; external components gate --
    def _repo_with_plugin(self):
        return make_repo({
            "workspace/social/ops/scripts/a.py": "print('a1')\n",
            "workspace/social/ops/prompts/morning-editorial.md": "# p1\n",
            "plugins/nullone-final-publish/index.js": "module.exports = 1;\n",
            "agents/approval/AGENTS.md": "# agent\n",
        })

    def test_external_files_never_mapped_under_prod_root(self):
        self.assertIsNone(map_repo_to_prod(
            "plugins/nullone-final-publish/index.js", self.policy))
        self.assertIsNone(map_repo_to_prod(
            "plugins/nullone-final-publish/route.js", self.policy))
        self.assertIsNone(map_repo_to_prod("agents/approval/AGENTS.md", self.policy))
        from ops.lib.policy import external_component_for_repo

        self.assertIsNotNone(external_component_for_repo(
            "plugins/nullone-final-publish/index.js", self.policy))
        self.assertIsNotNone(external_component_for_repo(
            "agents/approval/AGENTS.md", self.policy))
        self.assertIsNone(external_component_for_repo(
            "workspace/social/ops/scripts/a.py", self.policy))

    def test_workspace_only_change_updates_normally(self):
        repo = self._repo_with_plugin()
        prod = self._prod()
        sha1 = gitops.origin_main_sha(repo)
        self._adopt(repo, prod, sha1)
        self.assertFalse((prod / "plugins").exists())
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="ws")
        before = snapshot_full(prod)
        res = run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertEqual(res["RESULT"], "UPDATED")
        self.assertEqual(res["PLAN"]["EXTERNAL_COMPONENT_CHANGES"], [])
        self.assertEqual((prod / "social" / "ops" / "scripts" / "a.py").read_text(), "print('a2')\n")
        self.assertFalse((prod / "plugins").exists())
        self.assertFalse((prod / "agents").exists())

    def test_plugin_only_change_blocks_update_zero_mutation(self):
        repo = self._repo_with_plugin()
        prod = self._prod()
        sha1 = gitops.origin_main_sha(repo)
        self._adopt(repo, prod, sha1)
        commit_all(repo, {"plugins/nullone-final-publish/index.js": "module.exports = 2;\n"},
                   msg="plugin")
        target = gitops.origin_main_sha(repo)
        policy, digest = load_policy_at(repo, target)
        plan = build_plan(repo, policy, target, prod, digest)
        self.assertIn("plugins/nullone-final-publish/index.js",
                      plan["EXTERNAL_COMPONENT_CHANGES"])
        self.assertEqual(plan["RESTART_REQUIRED"], "YES")
        before = snapshot_full(prod)
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertIn("CONTROLLED_COMPONENT_DEPLOY_REQUIRED", str(cm.exception))
        self.assertEqual(snapshot_full(prod), before)
        self.assertFalse((prod / "plugins").exists())
        cur = read_current(prod)
        self.assertEqual(cur["deployed_sha"], sha1)

    def test_mixed_workspace_plugin_change_atomic_block(self):
        repo = self._repo_with_plugin()
        prod = self._prod()
        sha1 = gitops.origin_main_sha(repo)
        self._adopt(repo, prod, sha1)
        commit_all(repo, {
            "workspace/social/ops/scripts/a.py": "print('a2')\n",
            "plugins/nullone-final-publish/index.js": "module.exports = 2;\n",
        }, msg="mixed")
        before = snapshot_full(prod)
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertIn("CONTROLLED_COMPONENT_DEPLOY_REQUIRED", str(cm.exception))
        # no partial workspace deploy
        self.assertEqual(snapshot_full(prod), before)
        self.assertEqual((prod / "social" / "ops" / "scripts" / "a.py").read_text(), "print('a1')\n")
        cur = read_current(prod)
        self.assertEqual(cur["deployed_sha"], sha1)

    def test_agents_change_blocks_update(self):
        repo = self._repo_with_plugin()
        prod = self._prod()
        sha1 = gitops.origin_main_sha(repo)
        self._adopt(repo, prod, sha1)
        commit_all(repo, {"agents/approval/AGENTS.md": "# agent v2\n"}, msg="agents")
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertIn("CONTROLLED_COMPONENT_DEPLOY_REQUIRED", str(cm.exception))

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
        # In-process CLI run with explicit Python adapter injection (no env).
        import io
        from contextlib import redirect_stderr, redirect_stdout

        cli_main = _cli_main()

        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()

        def run_cli(argv):
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                rc = cli_main(argv)
            return rc, buf_out.getvalue(), buf_err.getvalue()

        rc, out, _ = run_cli(["version"])
        self.assertEqual(rc, 0)
        self.assertIn("nullone-release", out)
        rc, out, _ = run_cli(["status", "--production-root", str(prod),
                              "--repo-root", str(repo), "--no-fetch"])
        self.assertEqual(rc, 0)
        self.assertIn("DEPLOY_STATE_MISSING", out)
        # ordinary update refused without bootstrap (before any CI proof)
        rc, out, err = run_cli(["update", "--production-root", str(prod),
                                "--repo-root", str(repo), "--yes", "--no-fetch"])
        self.assertNotEqual(rc, 0)
        self.assertIn("BOOTSTRAP_REQUIRED", out + err)
        # bootstrap then update then preflight then rollback
        sha = gitops.origin_main_sha(repo)
        materialize(repo, sha, prod)
        rc, out, err = run_cli(["bootstrap", "--production-root", str(prod),
                                "--repo-root", str(repo), "--baseline", sha,
                                "--no-fetch"])
        self.assertEqual(rc, 0, err)
        self.assertIn("BOOTSTRAPPED", out)
        rc, out, err = run_cli(["update", "--production-root", str(prod),
                                "--repo-root", str(repo), "--yes", "--no-fetch"])
        self.assertEqual(rc, 0, err)
        self.assertIn("TARGET_SHA=", out)
        rc, out, err = run_cli(["preflight", "--production-root", str(prod),
                                "--repo-root", str(repo), "--no-fetch"])
        self.assertEqual(rc, 0, err)
        self.assertIn("TARGET_SHA=", out)
        self.assertIn("EXTERNAL_COMPONENT_CHANGES=0", out)
        # new workspace-only change -> update creates a backup -> rollback works
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('a2')\n"}, msg="v2")
        rc, out, err = run_cli(["update", "--production-root", str(prod),
                                "--repo-root", str(repo), "--yes", "--no-fetch"])
        self.assertEqual(rc, 0, err)
        self.assertIn("BACKUP_ID=", out)
        rc, out, err = run_cli(["rollback", "--production-root", str(prod),
                                "--repo-root", str(repo)])
        self.assertEqual(rc, 0, err)
        self.assertIn("ROLLED_BACK", out)

    # -- real-production mode: dev overrides forbidden --
    def _fake_home(self):
        home = Path(self.tmp.name) / "fakehome"
        (home / ".openclaw").mkdir(parents=True, exist_ok=True)
        return home

    def _run_cli_real_prod(self, argv, extra_env=None):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        cli_main = _cli_main()

        home = self._fake_home()
        env = {"HOME": str(home), "NULONE_ALLOW_REAL_PROD": "1"}
        env.update(extra_env or {})
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            # HOME must resolve through Path.home(); patch it explicitly.
            with mock.patch.object(Path, "home", return_value=home):
                try:
                    with redirect_stdout(buf_out), redirect_stderr(buf_err):
                        rc = cli_main(argv)
                    return rc, buf_out.getvalue(), buf_err.getvalue(), None
                except SystemExit as e:
                    return None, buf_out.getvalue(), buf_err.getvalue(), e.code

    def test_real_prod_rejects_policy_override(self):
        repo = make_repo(dict(BASE_FILES))
        home = self._fake_home()
        prod = home / ".openclaw" / "prod"
        _, _, err, code = self._run_cli_real_prod(
            ["update", "--production-root", str(prod), "--repo-root", str(repo),
             "--yes", "--policy", str(POLICY_PATH)])
        self.assertEqual(code, 2)
        self.assertIn("DEV_OVERRIDE_FORBIDDEN_IN_PRODUCTION", err)

    def test_real_prod_rejects_no_fetch_flag(self):
        repo = make_repo(dict(BASE_FILES))
        home = self._fake_home()
        prod = home / ".openclaw" / "prod"
        _, _, err, code = self._run_cli_real_prod(
            ["update", "--production-root", str(prod), "--repo-root", str(repo),
             "--yes", "--no-fetch"])
        self.assertEqual(code, 2)
        self.assertIn("DEV_OVERRIDE_FORBIDDEN_IN_PRODUCTION", err)

    def test_real_prod_rejects_no_fetch_env(self):
        repo = make_repo(dict(BASE_FILES))
        home = self._fake_home()
        prod = home / ".openclaw" / "prod"
        _, _, err, code = self._run_cli_real_prod(
            ["status", "--production-root", str(prod), "--repo-root", str(repo)],
            extra_env={"NULONE_NO_FETCH": "1"})
        self.assertEqual(code, 2)
        self.assertIn("DEV_OVERRIDE_FORBIDDEN_IN_PRODUCTION", err)

    def test_real_prod_rejects_ci_mock_env(self):
        repo = make_repo(dict(BASE_FILES))
        home = self._fake_home()
        prod = home / ".openclaw" / "prod"
        _, _, err, code = self._run_cli_real_prod(
            ["preflight", "--production-root", str(prod), "--repo-root", str(repo)],
            extra_env={"NULONE_CI_MOCK": "nullone-success"})
        self.assertEqual(code, 2)
        self.assertIn("DEV_OVERRIDE_FORBIDDEN_IN_PRODUCTION", err)

    def test_real_prod_without_overrides_passes_guard(self):
        # Guard passes (no DEV_OVERRIDE refusal); the command then fails only
        # on real fetch/CI authority, proving the guard itself did not block.
        repo = make_repo(dict(BASE_FILES))
        home = self._fake_home()
        prod = home / ".openclaw" / "prod"
        prod.mkdir(parents=True, exist_ok=True)
        rc, out, err, code = self._run_cli_real_prod(
            ["status", "--production-root", str(prod), "--repo-root", str(repo)])
        combined = out + err
        self.assertNotIn("DEV_OVERRIDE_FORBIDDEN_IN_PRODUCTION", combined)

    # -- forward-only update --
    def test_forward_update_allowed_and_same_is_uptodate(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        sha_a = gitops.origin_main_sha(repo)
        self._adopt(repo, prod, sha_a)
        sha_b = commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('b')\n"}, msg="B")
        res = run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertEqual(res["RESULT"], "UPDATED")
        self.assertEqual(res["TARGET_SHA"], sha_b)
        res = run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertEqual(res["RESULT"], "ALREADY_UP_TO_DATE")

    def test_backward_update_rejected_zero_mutation(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('b')\n"}, msg="B")
        sha_c = commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('c')\n"}, msg="C")
        self._adopt(repo, prod, sha_c)
        before = snapshot_full(prod)
        sha_b = self._sha_before(repo, sha_c)
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, target_arg=sha_b, assume_yes=True, do_fetch=False)
        self.assertIn("NON_FORWARD_UPDATE_REJECTED", str(cm.exception))
        self.assertEqual(snapshot_full(prod), before)
        self.assertEqual(read_current(prod)["deployed_sha"], sha_c)

    def _sha_before(self, repo: Path, sha: str) -> str:
        return git("rev-parse", f"{sha}~1", cwd=repo)

    def test_divergent_deployed_sha_rejected(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        sha_b = commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('b')\n"}, msg="B")
        self._adopt(repo, prod, sha_b)
        # side commit diverging from A (exists, but not an ancestor of B)
        git("checkout", "-b", "side", f"{sha_b}~1", cwd=repo)
        git("commit", "--allow-empty", "-m", "side", "--no-gpg-sign", cwd=repo)
        side_sha = git("rev-parse", "HEAD", cwd=repo)
        git("checkout", "main", cwd=repo)
        cur = read_current(prod)
        cur["deployed_sha"] = side_sha
        write_current(prod, cur)
        before = snapshot_full(prod)
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertIn("NON_FORWARD_UPDATE_REJECTED", str(cm.exception))
        self.assertEqual(snapshot_full(prod), before)

    def test_unknown_deployed_sha_rejected(self):
        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        cur = read_current(prod)
        cur["deployed_sha"] = "f" * 40
        write_current(prod, cur)
        before = snapshot_full(prod)
        with self.assertRaises(Exception) as cm:
            run_update(repo, prod, assume_yes=True, do_fetch=False)
        self.assertIn("DEPLOYED_SHA_UNKNOWN", str(cm.exception))
        self.assertEqual(snapshot_full(prod), before)

    def test_fetch_failure_blocks_update_zero_mutation(self):
        from ops.lib import gitops as _g

        repo = make_repo(dict(BASE_FILES))
        prod = self._prod()
        self._adopt(repo, prod)
        commit_all(repo, {"workspace/social/ops/scripts/a.py": "print('b')\n"}, msg="B")
        before = snapshot_full(prod)
        with mock.patch.object(_g, "fetch_origin_main",
                               side_effect=_g.GitError("GIT_FAILED: simulated fetch outage")):
            with self.assertRaises(Exception):
                run_update(repo, prod, assume_yes=True, do_fetch=True)
        self.assertEqual(snapshot_full(prod), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
