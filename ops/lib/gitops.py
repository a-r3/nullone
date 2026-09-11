"""Git operations: target resolution from exact commits, never working tree."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_REMOTE = "a-r3/nullone"


class GitError(Exception):
    pass


def _run_git(repo_root: Path, *args: str, env_extra: dict | None = None) -> str:
    import os

    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    cp = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if cp.returncode != 0:
        raise GitError(f"GIT_FAILED: git {' '.join(args)}: {cp.stderr.strip()[:500]}")
    return cp.stdout.strip()


def repo_toplevel(start: Path) -> Path:
    cp = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        raise GitError("NOT_A_GIT_REPO")
    return Path(cp.stdout.strip())


def check_remote_identity(repo_root: Path) -> None:
    try:
        url = _run_git(repo_root, "remote", "get-url", "origin")
    except GitError as e:
        raise GitError(f"REMOTE_IDENTITY_UNKNOWN: {e}") from e
    low = url.lower()
    if "a-r3/nullone" not in low:
        raise GitError(f"REMOTE_IDENTITY_MISMATCH: {url!r} does not match {EXPECTED_REMOTE}")


def fetch_origin_main(repo_root: Path) -> None:
    # best-effort fetch; offline tests may have no network — caller may skip via env
    _run_git(repo_root, "fetch", "origin", "main")


def origin_main_sha(repo_root: Path) -> str:
    out = _run_git(repo_root, "rev-parse", "origin/main")
    if not FULL_SHA_RE.match(out):
        raise GitError(f"MAIN_SHA_INVALID: {out!r}")
    return out


def commit_exists(repo_root: Path, sha: str) -> bool:
    if not FULL_SHA_RE.match(sha):
        return False
    cp = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-t", sha],
        capture_output=True,
        text=True,
    )
    return cp.returncode == 0 and cp.stdout.strip() == "commit"


def is_reachable_from_main(repo_root: Path, sha: str) -> bool:
    """True iff sha is an ancestor of (or equal to) origin/main."""
    cp = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", sha, "origin/main"],
        capture_output=True,
        text=True,
    )
    return cp.returncode == 0


def resolve_target(repo_root: Path, target_arg: str | None, do_fetch: bool = True) -> str:
    """Deterministically resolve target SHA. Fails closed."""
    check_remote_identity(repo_root)
    if do_fetch:
        fetch_origin_main(repo_root)
    main_sha = origin_main_sha(repo_root)
    if target_arg is None:
        return main_sha
    sha = target_arg.strip().lower()
    if not FULL_SHA_RE.match(sha):
        raise GitError(f"TARGET_MUST_BE_FULL_SHA: {target_arg!r}")
    if not commit_exists(repo_root, sha):
        raise GitError(f"TARGET_NOT_FOUND: {sha}")
    if not is_reachable_from_main(repo_root, sha):
        raise GitError(f"TARGET_NOT_ON_MAIN: {sha} is not reachable from origin/main")
    return sha


def blob_bytes(repo_root: Path, sha: str, repo_path: str) -> bytes | None:
    """Return exact blob bytes of repo_path at commit sha, or None if absent."""
    cp = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{sha}:{repo_path}"],
        capture_output=True,
    )
    if cp.returncode != 0:
        return None
    return cp.stdout if isinstance(cp.stdout, bytes) else bytes(cp.stdout)


def list_files_at(repo_root: Path, sha: str) -> list[str]:
    out = _run_git(repo_root, "ls-tree", "-r", "--name-only", sha)
    if not out:
        return []
    return [line for line in out.splitlines() if line]


def ls_tree_modes(repo_root: Path, sha: str) -> dict[str, int]:
    """Map repo-relative path -> file mode (0o644 or 0o755) at commit sha.

    Only regular files/blobs are returned; symlinks, submodules, and other
    object types raise GitError so callers never silently deploy them.
    """
    out = _run_git(repo_root, "ls-tree", "-r", sha)
    modes: dict[str, int] = {}
    for line in out.splitlines():
        # format: "<mode> <type> <hash>\t<path>"
        try:
            meta, path = line.split("\t", 1)
            mode_s, type_s, _ = meta.split(" ")
        except ValueError as e:
            raise GitError(f"LS_TREE_PARSE_FAILED: {line!r}") from e
        if type_s == "commit":
            raise GitError(f"LS_TREE_SUBMODULE_REJECTED: {path!r}")
        if type_s == "tree":
            continue
        if type_s != "blob":
            raise GitError(f"LS_TREE_UNEXPECTED_TYPE: {line!r}")
        if mode_s == "100644":
            modes[path] = 0o644
        elif mode_s == "100755":
            modes[path] = 0o755
        elif mode_s == "120000":
            raise GitError(f"LS_TREE_SYMLINK_REJECTED: {path!r}")
        else:
            raise GitError(f"LS_TREE_UNEXPECTED_MODE: {line!r}")
    return modes


def blob_hash_hex(repo_root: Path, sha: str, repo_path: str) -> str | None:
    import hashlib

    b = blob_bytes(repo_root, sha, repo_path)
    if b is None:
        return None
    return hashlib.sha256(b).hexdigest()
