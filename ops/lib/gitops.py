"""Git operations: target resolution from exact commits, never working tree."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_OWNER = "a-r3"
EXPECTED_REPO = "nullone"
EXPECTED_HOST = "github.com"

# scp-like syntax: [user@]host:path  (no "://" present)
_SCP_RE = re.compile(r"^(?:(?P<user>[^@/:]+)@)?(?P<host>[^/:]+):(?P<path>.+)$")


def sanitize_remote_url(url: str) -> str:
    """Strip embedded credentials (userinfo) so error output never leaks secrets."""
    try:
        return re.sub(r"://[^@/\s]+@", "://", url)
    except re.error:
        return "<unprintable-remote>"


def parse_github_remote(url: str) -> tuple[str, str, str] | None:
    """Normalize a git remote URL to (host, owner, repo).

    Accepts only exact canonical GitHub forms for a-r3/nullone enforcement:
    https://github.com/a-r3/nullone[.git], scp-style
    [git@]github.com:a-r3/nullone[.git], and ssh://git@github.com/... forms.
    Returns None for anything else (different host/owner/repo/path, ports,
    extra segments, malformed input). Comparison is case-insensitive per
    GitHub semantics; credentials are ignored for identity.
    """
    if not isinstance(url, str):
        return None
    s = url.strip()
    if not s or len(s) > 512 or any(c in s for c in (" ", "\t", "\n", "\x00")):
        return None
    host: str | None = None
    path: str | None = None
    if "://" in s:
        m = re.match(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<rest>.*)$", s)
        if not m:
            return None
        if m.group("scheme").lower() not in ("https", "ssh"):
            return None
        rest = m.group("rest")
        # strip userinfo (credentials — ignored for identity, never echoed)
        if "@" in rest:
            _userinfo, _, rest = rest.rpartition("@")
        if "/" not in rest:
            return None
        host, _, path = rest.partition("/")
        # reject ports and empty hosts
        if not host or ":" in host:
            return None
    else:
        m = _SCP_RE.match(s)
        if not m:
            return None
        host = m.group("host")
        path = m.group("path")
        if not host or not path or path.startswith("/"):
            return None
    if host.lower() != EXPECTED_HOST:
        return None
    segs = [seg for seg in path.strip("/").split("/") if seg not in ("", ".")]
    if len(segs) != 2:
        return None
    owner, repo = segs
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    return (host.lower(), owner.lower(), repo.lower())


def remote_identity_ok(url: str) -> bool:
    parsed = parse_github_remote(url)
    return parsed == (EXPECTED_HOST, EXPECTED_OWNER, EXPECTED_REPO)


def check_remote_identity(repo_root: Path) -> None:
    try:
        url = _run_git(repo_root, "remote", "get-url", "origin")
    except GitError as e:
        raise GitError(f"REMOTE_IDENTITY_UNKNOWN: {e}") from e
    if not remote_identity_ok(url):
        raise GitError(
            f"REMOTE_IDENTITY_MISMATCH: {sanitize_remote_url(url)!r} "
            f"is not exactly {EXPECTED_HOST}/{EXPECTED_OWNER}/{EXPECTED_REPO}")


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
