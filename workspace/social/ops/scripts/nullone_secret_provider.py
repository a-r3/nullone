#!/usr/bin/env python3
"""Reviewed secret boundary for NullOne infrastructure (#61, #81, #90).

This is the M0 minimum port required by issue #65: a small, reviewable
runtime boundary through which infrastructure secrets reach adapter
construction. Application and domain layers never import this module;
only the production provider factories
(`nullone_analytics_provider_factory.py`, `nullone_draft_provider_factory.py`,
`nullone_publish_provider_factory.py`) and deployment-edge readback
tooling do.

Design constraints:

- The logical secret identity used by application-facing infrastructure
  construction is `zernio.analytics.bearer`
  (`SECRET_ID_ZERNIO_ANALYTICS_BEARER`).
- The environment-variable binding is owned here and nowhere else:
  `ZERNIO_ANALYTICS_API_TOKEN` is the only mapping
  (`ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN`). The legacy key alias is
  intentionally absent from this module and from all new code/docs.
- `SecretValue` is the only envelope. `repr()` / `str()` / `format()`
  and other accidental-render paths never output the value: they return
  the fixed token `SECRET_REDACTED_RENDER` (`<redacted>`).
- Absent / empty / whitespace-only values are a typed *missing-secret*
  condition; an unreadable secret source is a typed *unavailable*
  condition. Neither error carries the value, its length, a prefix,
  suffix, hash, or raw OS exception text.
- The runtime source is the inherited process environment only: no
  secret file is ever read by this module, nothing is written to disk,
  and there is no network access. Filesystem control checks are
  deployment-edge validation only and are based on `lstat` metadata.
"""

from __future__ import annotations

import argparse
import enum
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

# Logical secret identity used by application-facing infrastructure
# construction. Adapters map it to a concrete runtime source.
SECRET_ID_ZERNIO_ANALYTICS_BEARER = "zernio.analytics.bearer"

# Sole environment-variable binding for the analytics bearer secret.
# Nothing outside this module needs (or may) know it.
ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN = "ZERNIO_ANALYTICS_API_TOKEN"

# DISTINCT draft/write credential identity (#81).
#
# Deliberately separate from `zernio.analytics.bearer`. The analytics
# credential is read-only and must never be reused for draft creation,
# media presign, or any write surface. The draft credential is the only
# identity the direct Zernio DraftProvider may use.
SECRET_ID_ZERNIO_DRAFTS_BEARER = "zernio.drafts.bearer"

# Sole environment-variable binding for the drafts bearer secret.
ENV_VAR_ZERNIO_DRAFT_API_TOKEN = "ZERNIO_DRAFT_API_TOKEN"

# DISTINCT publication credential identity (#90).
#
# Deliberately separate from both `zernio.analytics.bearer` (read-only)
# and `zernio.drafts.bearer` (draft creation). Neither credential may ever
# be reused for the consequential publication write.
#
# The publish credential is intentionally NOT bound to any environment
# variable below: a plain inherited environment variable alone must never
# satisfy publication auth. In production the value arrives only from the
# OpenClaw protected store (SecretRef id `ZERNIO_PUBLISH_API_TOKEN`,
# declared as a plugin SecretInput), is handed to the controller daemon
# over the authenticated private spawn pipe at startup, and is held in
# controller-process memory as a SecretValue inside InMemorySecretProvider
# (this module). The publisher adapter and bridge never read the
# environment.
SECRET_ID_ZERNIO_PUBLISH_BEARER = "zernio.publish.bearer"

# Protected-store entry name for the publish bearer secret.
# This is a store id, never an environment-variable binding: nothing in
# this module (or anywhere else) reads it from the process environment.
PUBLISH_SECRET_STORE_ID = "ZERNIO_PUBLISH_API_TOKEN"

# Fixed, value-free rendering produced by every non-revealing
# representation of a SecretValue.
SECRET_REDACTED_RENDER = "<redacted>"

# Expected secret file layout for deployment-edge readback below the
# user's home: <home>/.config/nullone/secrets/zernio-analytics.env, directory
# 0700, file 0600, owned by the systemd user-unit owner. Only the
# deployment doc and offline validation refer to it; the runtime secret
# path is purely the inherited environment variable.
SECRETS_DIR_RELATIVE = Path(".config").joinpath("nullone").joinpath("secrets")
SECRET_FILE_NAME = "zernio-analytics.env"


class SecretProviderError(RuntimeError):
    """Base error for the reviewed secret boundary (#61)."""


class SecretNotConfiguredError(SecretProviderError):
    """Typed missing-secret condition: absent, empty, or whitespace-only.

    Never carries the value or anything derived from it.
    """


class SecretUnavailableError(SecretProviderError):
    """The secret runtime source itself could not be read at all.

    Never carries the raw OS exception text or the value.
    """


class SecretPresence(str, enum.Enum):
    """Redacted classification of a secret's runtime state (readback)."""

    PRESENT_READABLE = "PRESENT_READABLE"
    MISSING = "MISSING"
    BLANK = "BLANK"
    UNAVAILABLE = "UNAVAILABLE"


class SecretValue:
    """Deliberately non-loggable secret wrapper (#61).

    The underlying string is private. The only escape hatch is the narrow
    `reveal()`, used by the infrastructure factory when it constructs the
    authenticated HTTP transport. Every accidental-render path returns the
    fixed `SECRET_REDACTED_RENDER`.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("SecretValue requires a string value")
        self._value = value

    def reveal(self) -> str:
        """Narrow, explicit access. Callers must never log or persist it."""
        return self._value

    def __repr__(self) -> str:
        return f"SecretValue({SECRET_REDACTED_RENDER!r})"

    def __str__(self) -> str:
        return SECRET_REDACTED_RENDER

    def __format__(self, format_spec: str) -> str:
        return SECRET_REDACTED_RENDER

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SecretValue):
            return self._value == other._value
        return NotImplemented

    __hash__ = None

    def __bool__(self) -> bool:
        return bool(self._value)


class SecretProvider(Protocol):
    """Replaceable runtime secret source (Protocol; implementation-free)."""

    def get_required(self, secret_id: str) -> SecretValue:
        """Return the secret identified by `secret_id`.

        Raises `SecretNotConfiguredError` / `SecretUnavailableError` (both
        `SecretProviderError` subclasses) on a typed absence / unreadable
        source. Must never log or persist the value.
        """
        ...


class EnvironmentSecretProvider:
    """Binds the logical analytics secret id to the inherited process
    environment variable.

    This is the runtime source proven for the OpenClaw Gateway's child
    commands (issue #61, branch A: systemd user service EnvironmentFile
    drop-in -> Gateway `process.env` -> spawned command processes). The
    variable is read at call time from an injected mapping (default: the
    current process environment).

    Absent / empty / whitespace-only -> `SecretNotConfiguredError`.
    Any exception while reading the source -> `SecretUnavailableError`.
    Unknown secret id -> `SecretNotConfiguredError`.
    """

    ENV_VAR_BY_SECRET_ID: Mapping[str, str] = {
        SECRET_ID_ZERNIO_ANALYTICS_BEARER: ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN,
        SECRET_ID_ZERNIO_DRAFTS_BEARER: ENV_VAR_ZERNIO_DRAFT_API_TOKEN,
    }

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def get_required(self, secret_id: str) -> SecretValue:
        var_name = self.ENV_VAR_BY_SECRET_ID.get(secret_id)

        if var_name is None:
            raise SecretNotConfiguredError(
                "requested secret is not bound to a runtime source"
            )

        try:
            raw = self._environ.get(var_name)
        except Exception:
            raise SecretUnavailableError(
                "secret runtime source could not be read"
            ) from None

        if raw is None or raw == "" or raw.isspace():
            raise SecretNotConfiguredError(
                "secret runtime source is absent or blank"
            )

        return SecretValue(raw)

    def probe(self, secret_id: str) -> SecretPresence:
        """Classify the secret's runtime state without revealing anything."""
        var_name = self.ENV_VAR_BY_SECRET_ID.get(secret_id)

        if var_name is None:
            return SecretPresence.UNAVAILABLE

        try:
            raw = self._environ.get(var_name)
        except Exception:
            return SecretPresence.UNAVAILABLE

        if raw is None:
            return SecretPresence.MISSING
        if raw == "" or raw.isspace():
            return SecretPresence.BLANK
        return SecretPresence.PRESENT_READABLE

    @classmethod
    def bound_env_var(cls, secret_id: str) -> str | None:
        """Redacted metadata: the env-var name bound to `secret_id` (or None)."""
        return cls.ENV_VAR_BY_SECRET_ID.get(secret_id)


class InMemorySecretProvider:
    """Single-source memory-only secret provider (#90 publication path).

    Holds exactly one SecretValue, installed once from the credential the
    OpenClaw plugin delivered over the authenticated private spawn pipe
    at controller startup. There is deliberately no environment fallback,
    no file fallback, and no second source: `get_required` serves the
    held value for the single bound logical id
    (`zernio.publish.bearer`) and raises typed missing-secret for
    anything else.

    Blank/whitespace-only values are rejected at construction as a typed
    missing-secret condition so a malformed pipe delivery fails closed
    before any publication attempt. Nothing here ever logs or persists
    the value.
    """

    BOUND_SECRET_ID = SECRET_ID_ZERNIO_PUBLISH_BEARER

    def __init__(self, token: SecretValue) -> None:
        if not isinstance(token, SecretValue):
            raise TypeError(
                "InMemorySecretProvider requires a SecretValue token"
            )
        revealed = token.reveal()
        if revealed == "" or revealed.isspace():
            raise SecretNotConfiguredError(
                "in-memory publication credential is absent or blank"
            )
        self._token = token

    def get_required(self, secret_id: str) -> SecretValue:
        if secret_id != self.BOUND_SECRET_ID:
            raise SecretNotConfiguredError(
                "requested secret is not bound to a runtime source"
            )
        return self._token

    def probe(self, secret_id: str) -> SecretPresence:
        if secret_id != self.BOUND_SECRET_ID:
            return SecretPresence.UNAVAILABLE
        return SecretPresence.PRESENT_READABLE


def default_secret_file_path() -> Path:
    """The expected secrets file location for deployment-edge validation."""
    return Path.home() / SECRETS_DIR_RELATIVE / SECRET_FILE_NAME


@dataclass(frozen=True)
class SecretFileControlReport:
    """Metadata-only readback of the expected secret file layout (#24).

    Built exclusively from `lstat` metadata; the file value is
    never opened, read, or echoed.
    """

    exists: bool
    regular_file: bool
    symlink_absent: bool
    owner_matches: bool
    owner_uid: int
    file_mode_secure: bool
    parent_dir_secure: bool
    secure: bool
    reason_codes: tuple[str, ...]


def secret_file_control_report(
    secret_file: Path,
    *,
    expected_uid: int | None = None,
) -> SecretFileControlReport:
    """Deployment-edge validation of the secret file layout.

    Rejects a missing file, a symlink (file or parent), a non-regular
    file, a non-directory parent, an unexpected owner, and any mode
    other than file 0600 / directory 0700. Never reads the value.
    Uses lstat() for both file and parent so symlinks are rejected,
    not followed.
    """
    if expected_uid is None:
        expected_uid = os.getuid()

    reasons: list[str] = []

    try:
        st = secret_file.lstat()
    except FileNotFoundError:
        return SecretFileControlReport(
            exists=False,
            regular_file=False,
            symlink_absent=False,
            owner_matches=False,
            owner_uid=-1,
            file_mode_secure=False,
            parent_dir_secure=False,
            secure=False,
            reason_codes=("FILE_MISSING",),
        )
    except NotADirectoryError:
        return SecretFileControlReport(
            exists=False,
            regular_file=False,
            symlink_absent=False,
            owner_matches=False,
            owner_uid=-1,
            file_mode_secure=False,
            parent_dir_secure=False,
            secure=False,
            reason_codes=("PARENT_DIR_NOT_DIRECTORY",),
        )

    if stat.S_ISLNK(st.st_mode):
        reasons.append("SECRET_FILE_SYMLINK")
    if not stat.S_ISREG(st.st_mode):
        reasons.append("SECRET_FILE_NOT_REGULAR_FILE")

    owner_matches = st.st_uid == expected_uid
    if not owner_matches:
        reasons.append("OWNER_MISMATCH")

    file_mode_exact = stat.S_IMODE(st.st_mode) == 0o600
    if not file_mode_exact:
        reasons.append("FILE_MODE_NOT_0600")

    parent_dir_secure = False
    try:
        pst = secret_file.parent.lstat()
    except FileNotFoundError:
        reasons.append("PARENT_DIR_MISSING")
        pst = None
    except NotADirectoryError:
        reasons.append("PARENT_DIR_NOT_DIRECTORY")
        pst = None

    if pst is not None:
        if stat.S_ISLNK(pst.st_mode):
            reasons.append("PARENT_DIR_SYMLINK")
        if not stat.S_ISDIR(pst.st_mode):
            reasons.append("PARENT_DIR_NOT_DIRECTORY")

        parent_owner_matches = pst.st_uid == expected_uid
        if not parent_owner_matches:
            reasons.append("OWNER_MISMATCH")

        parent_mode_exact = stat.S_IMODE(pst.st_mode) == 0o700
        if not parent_mode_exact:
            reasons.append("PARENT_DIR_MODE_NOT_0700")

        parent_dir_secure = (
            stat.S_ISDIR(pst.st_mode)
            and not stat.S_ISLNK(pst.st_mode)
            and parent_owner_matches
            and parent_mode_exact
        )

    secure = (
        owner_matches
        and file_mode_exact
        and parent_dir_secure
        and not stat.S_ISLNK(st.st_mode)
        and stat.S_ISREG(st.st_mode)
    )

    return SecretFileControlReport(
        exists=True,
        regular_file=stat.S_ISREG(st.st_mode),
        symlink_absent=not stat.S_ISLNK(st.st_mode),
        owner_matches=owner_matches,
        owner_uid=st.st_uid,
        file_mode_secure=file_mode_exact,
        parent_dir_secure=parent_dir_secure,
        secure=secure,
        reason_codes=tuple(reasons) if not secure else (),
    )


def _format_presence_row(secret_id: str, presence: SecretPresence) -> str:
    var_name = EnvironmentSecretProvider.bound_env_var(secret_id)
    bound = var_name if var_name is not None else "UNBOUND"
    return f"{secret_id}\tsource=environment\tenv_var={bound}\tstatus={presence.value}"


def _cmd_list() -> int:
    for secret_id in sorted(EnvironmentSecretProvider.ENV_VAR_BY_SECRET_ID):
        presence = EnvironmentSecretProvider().probe(secret_id)
        print(_format_presence_row(secret_id, presence))
    return 0


def _cmd_readback(secret_id: str) -> int:
    provider = EnvironmentSecretProvider()
    var_name = provider.bound_env_var(secret_id)
    presence = provider.probe(secret_id)

    print(f"SECRET_ID={secret_id}")
    print(f"ENV_VAR={var_name if var_name is not None else 'UNBOUND'}")
    print(f"STATUS={presence.value}")
    print("VALUE_REDACTED=TRUE")

    if presence is SecretPresence.PRESENT_READABLE:
        return 0
    if presence in (SecretPresence.MISSING, SecretPresence.BLANK):
        return 1
    return 2


def _cmd_file_check(secret_file: str) -> int:
    report = secret_file_control_report(Path(secret_file))
    print(f"FILE={secret_file}")
    print(f"EXISTS={report.exists}")
    print(f"REGULAR_FILE={report.regular_file}")
    print(f"SYMLINK_ABSENT={report.symlink_absent}")
    print(f"OWNER_UID={report.owner_uid}")
    print(f"FILE_MODE_0600={report.file_mode_secure}")
    print(f"PARENT_DIR_0700={report.parent_dir_secure}")
    print(f"SECURE={report.secure}")
    print(f"REASONS={','.join(report.reason_codes) if report.reason_codes else 'NONE'}")
    if report.exists:
        print("VALUE_READ=FALSE")
    return 0 if report.secure else 1


def self_test() -> int:
    marker = "FAKE_ZERNIO_SECRET_DO_NOT_LOG_123"
    secret = SecretValue(marker)

    assert repr(secret) == f"SecretValue({SECRET_REDACTED_RENDER!r})"
    assert str(secret) == SECRET_REDACTED_RENDER
    assert f"{secret}" == SECRET_REDACTED_RENDER
    assert f"{secret!s}" == SECRET_REDACTED_RENDER
    assert f"{secret:s}" == SECRET_REDACTED_RENDER
    assert marker not in repr(secret) and marker not in str(secret)
    assert secret == SecretValue(marker)
    assert secret != SecretValue("other")
    assert secret.reveal() == marker

    try:
        SecretValue(123)  # type: ignore[arg-type]
        raise AssertionError("SecretValue accepted a non-string")
    except TypeError:
        pass

    env = EnvironmentSecretProvider(
        environ={
            ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN: marker,
        }
    )
    assert env.probe(SECRET_ID_ZERNIO_ANALYTICS_BEARER) is SecretPresence.PRESENT_READABLE
    assert env.get_required(SECRET_ID_ZERNIO_ANALYTICS_BEARER) == SecretValue(marker)

    for absent in (None, "", "   ", "\t\n"):
        snap = dict(env._environ)
        if absent is None:
            snap.pop(ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN, None)
        else:
            snap[ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN] = absent
        try:
            EnvironmentSecretProvider(environ=snap).get_required(
                SECRET_ID_ZERNIO_ANALYTICS_BEARER
            )
            raise AssertionError("missing/blank secret did not fail closed")
        except SecretNotConfiguredError:
            pass

    class ExplodingSource:
        def get(self, key):
            raise OSError("disk gone")

    class UnavailableEnv(EnvironmentSecretProvider):
        def __init__(self):
            self._environ = ExplodingSource()  # type: ignore[assignment]

    assert (
        UnavailableEnv().probe(SECRET_ID_ZERNIO_ANALYTICS_BEARER)
        is SecretPresence.UNAVAILABLE
    )
    try:
        UnavailableEnv().get_required(SECRET_ID_ZERNIO_ANALYTICS_BEARER)
        raise AssertionError("unavailable source did not fail closed")
    except SecretUnavailableError:
        pass

    # Publication identity is intentionally NOT env-bound (#90): a plain
    # inherited variable alone must never satisfy publication auth.
    assert (
        EnvironmentSecretProvider.bound_env_var(
            SECRET_ID_ZERNIO_PUBLISH_BEARER
        )
        is None
    )
    assert (
        EnvironmentSecretProvider().probe(SECRET_ID_ZERNIO_PUBLISH_BEARER)
        is SecretPresence.UNAVAILABLE
    )
    try:
        EnvironmentSecretProvider(
            environ={PUBLISH_SECRET_STORE_ID: marker}
        ).get_required(SECRET_ID_ZERNIO_PUBLISH_BEARER)
        raise AssertionError("unbound publish secret resolved from env")
    except SecretNotConfiguredError:
        pass

    # In-memory provider: single source, no env/file fallback.
    held = InMemorySecretProvider(SecretValue(marker))
    assert held.get_required(SECRET_ID_ZERNIO_PUBLISH_BEARER) == SecretValue(
        marker
    )
    assert held.probe(SECRET_ID_ZERNIO_PUBLISH_BEARER) is (
        SecretPresence.PRESENT_READABLE
    )
    assert (
        held.probe(SECRET_ID_ZERNIO_ANALYTICS_BEARER)
        is SecretPresence.UNAVAILABLE
    )
    try:
        held.get_required(SECRET_ID_ZERNIO_ANALYTICS_BEARER)
        raise AssertionError("in-memory provider served an unbound id")
    except SecretNotConfiguredError:
        pass
    try:
        InMemorySecretProvider(SecretValue("   "))
        raise AssertionError("blank in-memory token was accepted")
    except SecretNotConfiguredError:
        pass
    try:
        InMemorySecretProvider(marker)  # type: ignore[arg-type]
        raise AssertionError("non-SecretValue token was accepted")
    except TypeError:
        pass
    assert marker not in repr(held)

    print("SECRET_PROVIDER_SELF_TEST=PASS")
    print("SECRET_REDACTED=TRUE")
    print("NO_SECRET_READ=TRUE")
    print("NO_NETWORK=TRUE")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="NullOne reviewed secret boundary (#61) readback tooling"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("self-test")
    sub.add_parser("list")

    p_readback = sub.add_parser("readback")
    p_readback.add_argument(
        "--secret-id",
        default=SECRET_ID_ZERNIO_ANALYTICS_BEARER,
        help=f"logical secret id (default: {SECRET_ID_ZERNIO_ANALYTICS_BEARER})",
    )

    p_file = sub.add_parser("file-check")
    p_file.add_argument(
        "--secret-file",
        default=str(default_secret_file_path()),
        help="expected Zernio secrets env file path (metadata-only check)",
    )

    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()
    if args.command == "list":
        return _cmd_list()
    if args.command == "readback":
        return _cmd_readback(args.secret_id)
    if args.command == "file-check":
        return _cmd_file_check(args.secret_file)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())