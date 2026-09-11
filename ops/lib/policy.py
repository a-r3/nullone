"""Deployment policy loading and path authorization."""
from __future__ import annotations

import json
from pathlib import Path

POLICY_SCHEMA = "nullone.release-policy/v1"


class PolicyError(Exception):
    pass


def load_policy(policy_path: str | Path) -> dict:
    p = Path(policy_path)
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise PolicyError(f"POLICY_NOT_FOUND: {p}") from e
    except json.JSONDecodeError as e:
        raise PolicyError(f"POLICY_INVALID_JSON: {e}") from e
    if data.get("schema") != POLICY_SCHEMA:
        raise PolicyError(f"POLICY_SCHEMA_MISMATCH: {data.get('schema')!r}")
    if not data.get("policy_version"):
        raise PolicyError("POLICY_VERSION_MISSING")
    if not isinstance(data.get("mappings"), list) or not data["mappings"]:
        raise PolicyError("POLICY_MAPPINGS_MISSING")
    return data


def _norm(p: str) -> str:
    # posix-style, no leading slash
    return p.replace("\\", "/").lstrip("/")


def map_repo_to_prod(repo_path: str, policy: dict) -> str | None:
    """Map a repo-relative path to a prod-relative path, or None if not managed."""
    rp = _norm(repo_path)
    for m in policy["mappings"]:
        prefix = _norm(m["repo_prefix"])
        dest = _norm(m["prod_prefix"])
        if m.get("single_file"):
            if rp == prefix:
                return dest
        else:
            if not prefix.endswith("/"):
                prefix += "/"
            if rp.startswith(prefix):
                rest = rp[len(prefix):]
                if not rest:
                    return None
                return dest.rstrip("/") + "/" + rest if dest else rest
    return None


def is_forbidden_repo_path(repo_path: str, policy: dict) -> bool:
    rp = _norm(repo_path)
    for prefix in policy.get("forbidden_repo_prefixes", []):
        fp = _norm(prefix)
        if fp.endswith("/"):
            if rp.startswith(fp):
                return True
        else:
            if rp == fp or rp.startswith(fp + "/"):
                return True
    return False


def is_forbidden_prod_path(prod_path: str, policy: dict) -> bool:
    pp = _norm(prod_path)
    # absolute or traversal rejected upstream, but double-guard here
    if pp.startswith("/") or ".." in pp.split("/"):
        return True
    for prefix in policy.get("forbidden_prod_prefixes", []):
        fp = _norm(prefix).rstrip("/") + "/"
        if pp == _norm(prefix).rstrip("/") or pp.startswith(fp):
            return True
    # NOTE: no generic substring matching here. Allowlisted code filenames
    # legitimately contain substrings like "auth" (authority), "secret"
    # (secret_provider boundary module), or "session" (supersession).
    # Secret-bearing *destinations* are excluded via the explicit
    # forbidden_prod_prefixes above (private/, secrets/, manifests/,
    # run-outcomes/, state/, deploy-state/, ...), and deploy-state content
    # is independently guarded against secret values.
    return False


def assert_prod_path_safe(prod_root: Path, prod_rel: str, policy: dict) -> Path:
    """Resolve prod_rel under prod_root, rejecting traversal and symlinks. Returns resolved absolute path."""
    raw = str(prod_rel)
    if raw.startswith("/") or raw.startswith("\\"):
        raise PolicyError(f"PATH_TRAVERSAL_REJECTED: {prod_rel!r}")
    rel = _norm(prod_rel)
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        raise PolicyError(f"PATH_TRAVERSAL_REJECTED: {prod_rel!r}")
    if is_forbidden_prod_path(rel, policy):
        raise PolicyError(f"FORBIDDEN_DESTINATION: {rel!r}")
    abs_path = (prod_root / rel)
    # symlink escape: resolve parent chain; if any component is a symlink escaping root, reject
    try:
        # lstat each ancestor
        cur = prod_root.resolve()
        parts = rel.split("/")
        node = prod_root
        for part in parts[:-1]:
            node = node / part
            if node.is_symlink():
                raise PolicyError(f"SYMLINK_ESCAPE_REJECTED: {prod_rel!r}")
        # final component itself must not be a symlink pointing outside (we handle at install time;
        # here reject if existing symlink resolves outside root)
        if abs_path.is_symlink():
            target = abs_path.resolve()
            if not str(target).startswith(str(cur) + "/") and target != cur:
                raise PolicyError(f"SYMLINK_ESCAPE_REJECTED: {prod_rel!r}")
    except PolicyError:
        raise
    except OSError as e:
        raise PolicyError(f"PATH_CHECK_FAILED: {e}") from e
    return abs_path
