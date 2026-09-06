#!/usr/bin/env python3
"""Deterministic breaking-event identity, dedup and follow-up suppression (#35).

Implements the identity/dedup domain layer required by the accepted #34
policy (docs/contracts/breaking-routing-policy-v1.md). Given one candidate's
structured evidence/claim metadata and a read-only snapshot of authoritative
NullOne state, this module derives a versioned, deterministic decision
(`nullone.breaking-identity.v1`) that a future #36 routing layer can consume
without redefining identity, dedup or suppression rules.

Scope boundaries (see issue #35):

* No network access, no AI/model calls, no vector database or embeddings.
* No Story/Feed draft dispatch, no publication, scheduling or approval
  mutation. This module only reads state; it never writes production state.
* No severity classification and no routing decision. Those remain #36.

Identity precedence (exact identifiers > authoritative manifests/ledgers >
queue/review history > normalized deterministic claim comparison > fail
ambiguous) follows the accepted policy's "Precedence and state authority"
section verbatim.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "nullone.breaking-identity.v1"

IDENTITY_BASES = frozenset(
    {"EXACT_IDENTIFIER", "CANONICAL_SOURCE", "NORMALIZED_CLAIM", "UNRESOLVED"}
)

DEDUP_DECISIONS = frozenset(
    {
        "EXACT_DUPLICATE",
        "SAME_EVENT",
        "MATERIAL_FOLLOW_UP",
        "DISTINCT_EVENT",
        "AMBIGUOUS_IDENTITY",
    }
)

# Stable reason set accepted by #34; #35 must not invent alternatives.
FOLLOW_UP_REASONS = frozenset(
    {
        "AVAILABILITY_CHANGED",
        "OFFICIAL_NUMBER_CHANGED",
        "AFFECTED_REGION_CHANGED",
        "MATERIAL_CORRECTION",
        "PRODUCT_VERSION_CHANGED",
        "USER_CONSEQUENCE_CHANGED",
    }
)

# Reason codes shared verbatim with the accepted #34 routing vocabulary.
# #36 may pass these through directly as its own `reason_code` on the
# SUPPRESS_DUPLICATE / BLOCKED_AMBIGUOUS_IDENTITY routes; #35 never emits
# a routing_decision or severity itself.
SHARED_POLICY_REASON_CODES = frozenset(
    {
        "EXACT_EVENT_DUPLICATE",
        "SAME_EVENT_DIFFERENT_SOURCE",
        "EXISTING_CONSEQUENTIAL_STATE",
        "EXISTING_DRAFT_REQUEST",
        "CANDIDATE_EXCLUDED",
        "UNRESOLVED_EVENT_HISTORY",
        "IDENTITY_UNRESOLVED",
        "STATE_UNAVAILABLE_OR_CONFLICTING",
    }
)

# Additional #35-only audit reason codes for non-suppressed outcomes. #36
# computes its own routing-specific reason code for these paths, so no
# collision with the shared vocabulary above is required.
DISTINCT_REASON_CODE = "DISTINCT_DEVELOPMENT"
FOLLOW_UP_LINKED_REASON_CODE = "MATERIAL_FOLLOW_UP_LINKED"

ALL_REASON_CODES = SHARED_POLICY_REASON_CODES | {
    DISTINCT_REASON_CODE,
    FOLLOW_UP_LINKED_REASON_CODE,
}

# Manifest publication states that are consequential on their own (a live or
# actively-in-flight publication), independent of attempt count.
_CONSEQUENTIAL_LIVE_STATES = frozenset(
    {"PUBLISH_ACCEPTED", "PUBLISHING", "PUBLISHED", "PUBLISH_IN_FLIGHT"}
)

# Manifest publication states that are only consequential after a consumed
# attempt, and whose outcome is unsafe/ambiguous (UNKNOWN never means empty).
_CONSEQUENTIAL_AFTER_ATTEMPT_STATES = frozenset(
    {"UNKNOWN", "READBACK_FAILED", "CHECK_REQUIRED", "FAILED"}
)

# Review states that reserve a draft outright (definite, non-ambiguous).
_RESERVED_DEFINITE_STATES = frozenset({"DRAFT_CREATED"})

# Review states that reserve a draft but whose outcome is itself unresolved.
_RESERVED_UNRESOLVED_STATES = frozenset({"CREATE_IN_FLIGHT", "REVIEW_UNKNOWN"})

# Queue-status markers for historical/excluded candidates (not active load,
# but never eligible for automatic resurrection).
_EXCLUDED_QUEUE_STATUSES = frozenset(
    {"REJECTED", "LEGACY_DRAFT", "SUPERSEDED_DRAFT"}
)


class IdentityInputError(ValueError):
    """Raised for caller-provided (in-process) structured input mistakes.

    Distinct from state-read failures: this is a programming error in how
    a CandidateInput/PriorDevelopment was constructed, not an ambiguous or
    unreadable repository state.
    """


# --------------------------------------------------------------------------
# URL normalization (conservative; no network, no redirects, no aliasing)
# --------------------------------------------------------------------------

# Known tracking-only query parameters. Conservative allowlist-of-removal:
# anything not explicitly listed here is preserved because it may be
# identity-bearing (release/version identifier, resource ID, region, ...).
_TRACKING_QUERY_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "utm_reader",
        "gclid",
        "dclid",
        "fbclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "yclid",
        "msclkid",
        "ref_src",
        "ref_url",
        "spm",
        "cmpid",
        "icid",
    }
)


def normalize_url(url: str) -> str:
    """Conservatively canonicalize a URL for identity comparison.

    Rules (documented per issue #35 section 8):
      * scheme and host are lowercased; nothing else is case-folded, since
        paths/query values may be case-sensitive identity-bearing content.
      * a single trailing slash on a non-root path is removed (cosmetic).
      * only known tracking-only query parameters are removed; every other
        query parameter is preserved verbatim because it may carry
        identity-bearing information (release id, version, region, ...).
      * remaining query parameters are sorted by (key, value) for stable
        comparison; this does not change their values.
      * the fragment is preserved unchanged (never assumed to be tracking
        noise).
      * no network access, no redirect following, no alias guessing: two
        different URLs are never silently merged.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    raw = url.strip()
    parts = urlsplit(raw)

    scheme = (parts.scheme or "").lower()
    netloc = parts.netloc.lower()

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_PARAMS
    ]
    query_pairs.sort(key=lambda pair: (pair[0], pair[1]))
    query = urlencode(query_pairs)

    return urlunsplit((scheme, netloc, path, query, parts.fragment))


# --------------------------------------------------------------------------
# Stable deterministic hashing primitives
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalize_key(value: str) -> str:
    """Lowercase, whitespace/punctuation-collapsing key normalization."""
    return "-".join(_WORD_RE.findall(value.lower()))


def _stable_id(prefix: str, parts: Sequence[str]) -> str:
    """A deterministic ID derived only from the given canonical parts.

    Uses SHA-256 (stdlib) over a canonical JSON encoding. Never depends on
    scan time, source popularity, target format, model confidence or
    process/random state - only on the caller-supplied canonical parts.
    """
    canonical = json.dumps(list(parts), ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


# --------------------------------------------------------------------------
# Structured candidate/evidence input (pure domain objects; no I/O)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceItem:
    """One structured evidence record for a candidate development.

    `ref` is a durable, caller-assigned audit reference (e.g. an evidence
    store key); it is never itself used as identity, only as an audit
    pointer recorded in output `matched_refs`/`identity_refs` provenance.
    """

    ref: str
    supported_claim: str
    source_url: str | None = None
    announcement_id: str | None = None
    product: str | None = None
    version: str | None = None
    region: str | None = None
    availability_stage: str | None = None
    number_value: str | None = None
    number_unit: str | None = None
    number_population: str | None = None
    number_period: str | None = None

    def __post_init__(self) -> None:
        if not self.ref or not self.ref.strip():
            raise IdentityInputError("EvidenceItem.ref is required")
        if not self.supported_claim or not self.supported_claim.strip():
            raise IdentityInputError("EvidenceItem.supported_claim is required")


@dataclass(frozen=True)
class FollowUpDelta:
    """A caller-asserted, evidence-linked scoped delta over a parent claim.

    This is deterministic structured input, not an AI inference: the
    upstream evidence pipeline (Radar) is expected to tag `delta_kind` from
    structured fields (e.g. an official number changed), not from free
    prose. The comparator only validates and applies this input; it never
    invents a delta_kind from unstructured text.
    """

    delta_kind: str
    parent_claim: str
    new_claim: str
    evidence_ref: str

    def __post_init__(self) -> None:
        if self.delta_kind not in FOLLOW_UP_REASONS:
            raise IdentityInputError(
                f"delta_kind must be one of {sorted(FOLLOW_UP_REASONS)}"
            )
        if not self.parent_claim or not self.parent_claim.strip():
            raise IdentityInputError("FollowUpDelta.parent_claim is required")
        if not self.new_claim or not self.new_claim.strip():
            raise IdentityInputError("FollowUpDelta.new_claim is required")
        if not self.evidence_ref or not self.evidence_ref.strip():
            raise IdentityInputError("FollowUpDelta.evidence_ref is required")


@dataclass(frozen=True)
class CandidateInput:
    """The one candidate/development under evaluation.

    `evidence[0]` is treated as the canonical/primary evidence item; callers
    (the evidence pipeline) are responsible for deterministic ordering -
    this is a caller-side construction convention, never scan time or
    source popularity, so it does not break determinism across runs.
    """

    candidate_id: str
    assessment_ref: str
    state_snapshot_ref: str
    topic_cluster: str
    evidence: tuple[EvidenceItem, ...]
    delta: FollowUpDelta | None = None
    topic_title: str | None = None

    def __post_init__(self) -> None:
        for name in ("candidate_id", "assessment_ref", "state_snapshot_ref", "topic_cluster"):
            if not getattr(self, name) or not str(getattr(self, name)).strip():
                raise IdentityInputError(f"CandidateInput.{name} is required")
        if self.delta is not None:
            refs = {item.ref for item in self.evidence}
            if self.delta.evidence_ref not in refs:
                raise IdentityInputError(
                    "FollowUpDelta.evidence_ref must reference a candidate evidence item"
                )


# --------------------------------------------------------------------------
# Identity computation (pure function of CandidateInput; no I/O, no state)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityComputation:
    event_id: str | None
    development_id: str | None
    topic_id: str
    identity_basis: str
    identity_refs: tuple[str, ...]


def _claim_fingerprint(item: EvidenceItem) -> str:
    fields = [
        item.product or "",
        item.version or "",
        item.region or "",
        item.availability_stage or "",
        item.number_value or "",
        item.number_unit or "",
        item.number_population or "",
        item.number_period or "",
        item.supported_claim or "",
    ]
    return "|".join(_normalize_key(value) for value in fields)


def _normalized_claim_key(item: EvidenceItem) -> str | None:
    """Build a NORMALIZED_CLAIM basis key from structured claim fields.

    Requires at minimum a product to safely anchor identity; without one,
    the caller must fail ambiguous rather than guess semantics.
    """
    if not item.product or not item.product.strip():
        return None
    parts = [item.product, item.version or "", item.region or ""]
    return "|".join(_normalize_key(part) for part in parts)


def compute_identity(candidate: CandidateInput) -> IdentityComputation:
    """Deterministically derive event/development/topic identity.

    Precedence (accepted #34 policy, "deterministic evidence first"):
      1. EXACT_IDENTIFIER - a consistent `announcement_id` across evidence.
      2. CANONICAL_SOURCE - a normalized canonical source URL.
      3. NORMALIZED_CLAIM - normalized product/version/region claim key.
      4. UNRESOLVED - insufficient or conflicting deterministic evidence;
         never guessed.

    `topic_id` always resolves from `topic_cluster` (existing convention);
    it groups related developments and is never itself proof of event
    equivalence.
    """
    topic_id = _stable_id("topic", [_normalize_key(candidate.topic_cluster)])

    if not candidate.evidence:
        return IdentityComputation(None, None, topic_id, "UNRESOLVED", ())

    announcement_ids = {
        item.announcement_id for item in candidate.evidence if item.announcement_id
    }
    if len(announcement_ids) > 1:
        # Conflicting exact identifiers: ambiguous, never a license to pick one.
        return IdentityComputation(None, None, topic_id, "UNRESOLVED", ())

    primary = candidate.evidence[0]

    if len(announcement_ids) == 1:
        (announcement_id,) = announcement_ids
        basis = "EXACT_IDENTIFIER"
        event_key = f"announcement:{_normalize_key(announcement_id)}"
        identity_refs: tuple[str, ...] = (announcement_id,)
    else:
        urls = [
            normalize_url(item.source_url)
            for item in candidate.evidence
            if item.source_url
        ]
        if urls:
            basis = "CANONICAL_SOURCE"
            canonical_url = (
                normalize_url(primary.source_url) if primary.source_url else urls[0]
            )
            event_key = f"url:{canonical_url}"
            identity_refs = tuple(sorted(set(urls)))
        else:
            claim_key = _normalized_claim_key(primary)
            if claim_key is None:
                return IdentityComputation(None, None, topic_id, "UNRESOLVED", ())
            basis = "NORMALIZED_CLAIM"
            event_key = f"claim:{claim_key}"
            identity_refs = (primary.ref,)

    development_key = f"{event_key}|claim:{_claim_fingerprint(primary)}"

    event_id = _stable_id("event", [event_key])
    development_id = _stable_id("development", [development_key])

    return IdentityComputation(event_id, development_id, topic_id, basis, identity_refs)


# --------------------------------------------------------------------------
# Read-only repository-state adapter
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueBlock:
    """One parsed `- **topic:** ...` block from candidate-queue.md.

    Mirrors the exact block-detection convention already used by
    `nullone_state.mark_queue_published_exact` (topic line, followed by
    `- **key:** value` lines, terminated by the next topic/heading/rule).
    """

    topic: str
    fields: Mapping[str, str]


_QUEUE_FIELD_RE = re.compile(r"^-\s*\*\*([^*]+):\*\*\s*(.*)$")


def parse_queue_markdown(text: str) -> tuple[QueueBlock, ...]:
    lines = text.splitlines()
    blocks: list[QueueBlock] = []
    i = 0

    while i < len(lines):
        match = _QUEUE_FIELD_RE.match(lines[i].strip())

        if not (match and match.group(1).strip().lower() == "topic"):
            i += 1
            continue

        topic = match.group(2).strip()
        fields: dict[str, str] = {}
        j = i + 1

        while j < len(lines):
            stripped = lines[j].strip()

            if (
                stripped.startswith("- **topic:**")
                or stripped.startswith("### ")
                or stripped.startswith("## ")
                or stripped == "---"
            ):
                break

            field_match = _QUEUE_FIELD_RE.match(stripped)

            if field_match:
                key = field_match.group(1).strip().lower().replace(" ", "_")
                fields[key] = field_match.group(2).strip()

            j += 1

        blocks.append(QueueBlock(topic=topic, fields=fields))
        i = j

    return tuple(blocks)


@dataclass(frozen=True)
class PriorDevelopment:
    """One previously-resolved development, for cross-source identity match.

    This is #35's own narrow, versioned, read-only supplementary contract:
    the real repository today stores no per-candidate evidence/identity
    (manifests carry no source URL; see docs/contracts/breaking-routing-
    policy-v1.md's inspected-sources table). Wiring a production writer for
    this index is explicitly out of scope for #35 (no live state writes);
    only its read shape is defined here so #36/a future integration can
    populate it. Absence of this optional source is empty history, not an
    error - identical to how the existing publish/topic ledgers already
    treat a missing file as legitimately empty.
    """

    ref: str
    event_id: str
    development_id: str
    identity_basis: str
    candidate_id: str | None = None
    consequential_kind: str | None = None
    reserved_kind: str | None = None
    excluded_kind: str | None = None
    unresolved_outcome: bool = False

    def __post_init__(self) -> None:
        for name in ("ref", "event_id", "development_id", "identity_basis"):
            if not getattr(self, name) or not str(getattr(self, name)).strip():
                raise IdentityInputError(f"PriorDevelopment.{name} is required")
        if self.identity_basis not in IDENTITY_BASES:
            raise IdentityInputError("PriorDevelopment.identity_basis is invalid")


def _parse_prior_development(raw: Mapping[str, Any]) -> PriorDevelopment:
    return PriorDevelopment(
        ref=raw["ref"],
        event_id=raw["event_id"],
        development_id=raw["development_id"],
        identity_basis=raw["identity_basis"],
        candidate_id=raw.get("candidate_id"),
        consequential_kind=raw.get("consequential_kind"),
        reserved_kind=raw.get("reserved_kind"),
        excluded_kind=raw.get("excluded_kind"),
        unresolved_outcome=bool(raw.get("unresolved_outcome", False)),
    )


def _read_jsonl_strict(path: Path) -> tuple[tuple[dict, ...], bool]:
    """Read an append-only JSONL ledger; missing file is empty, not an error.

    Any unreadable file or any malformed line marks the source as
    unreadable (stricter than the existing lenient `nullone_state.read_jsonl`,
    which silently skips bad lines - intentional for this higher-assurance
    identity domain, and does not change that existing helper's behavior).
    """
    if not path.exists():
        return (), True

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return (), False

    rows: list[dict] = []

    for line in raw.splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            obj = json.loads(line)
        except Exception:
            return (), False

        if not isinstance(obj, dict):
            return (), False

        rows.append(obj)

    return tuple(rows), True


def _read_manifests_strict(manifest_dir: Path) -> tuple[dict[str, dict], bool]:
    """Read production manifests keyed by candidate_id.

    Missing directory is empty, not an error. Any manifest that fails to
    parse, lacks a candidate_id, or contradicts an already-seen manifest for
    the same candidate_id marks manifest state as unreadable/malformed -
    fail closed rather than silently pick one.
    """
    if not manifest_dir.exists():
        return {}, True

    try:
        paths = sorted(manifest_dir.glob("*.json"))
    except OSError:
        return {}, False

    out: dict[str, dict] = {}

    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}, False

        if not isinstance(data, dict):
            return {}, False

        candidate_id = data.get("candidate_id")

        if not isinstance(candidate_id, str) or not candidate_id.strip():
            return {}, False

        if candidate_id in out and out[candidate_id] != data:
            return {}, False

        out[candidate_id] = data

    return out, True


def _read_queue_strict(path: Path) -> tuple[tuple[QueueBlock, ...], bool]:
    if not path.exists():
        return (), True

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return (), False

    return parse_queue_markdown(text), True


@dataclass(frozen=True)
class RepositoryState:
    manifests_by_candidate_id: Mapping[str, dict]
    manifests_readable: bool
    publish_ledger_rows: tuple[dict, ...]
    publish_ledger_readable: bool
    topic_ledger_rows: tuple[dict, ...]
    topic_ledger_readable: bool
    queue_blocks: tuple[QueueBlock, ...]
    queue_readable: bool
    prior_developments: tuple[PriorDevelopment, ...]
    prior_developments_readable: bool

    @property
    def fully_readable(self) -> bool:
        return (
            self.manifests_readable
            and self.publish_ledger_readable
            and self.topic_ledger_readable
            and self.queue_readable
            and self.prior_developments_readable
        )


def load_repository_state(
    workspace: Path,
    *,
    prior_developments: Sequence[Mapping[str, Any]] | None = None,
    prior_developments_path: Path | None = None,
) -> RepositoryState:
    """Read-only load of the authoritative NullOne state sources.

    `workspace` must be passed explicitly (never defaulted to a live
    production path) so this adapter never accidentally touches production
    state; production wiring is left to a future integration boundary.
    Mirrors the exact directory/file layout already used by
    `nullone_bridge_common`/`nullone_state` (manifests under
    social/ops/manifests, ledgers under social/state/*.jsonl, queue at
    social/state/candidate-queue.md).
    """
    manifest_dir = workspace / "social" / "ops" / "manifests"
    publish_ledger_path = workspace / "social" / "state" / "publish-ledger.jsonl"
    topic_ledger_path = workspace / "social" / "state" / "topic-ledger.jsonl"
    queue_path = workspace / "social" / "state" / "candidate-queue.md"

    manifests, manifests_ok = _read_manifests_strict(manifest_dir)
    publish_rows, publish_ok = _read_jsonl_strict(publish_ledger_path)
    topic_rows, topic_ok = _read_jsonl_strict(topic_ledger_path)
    queue_blocks, queue_ok = _read_queue_strict(queue_path)

    prior_ok = True
    prior_raw: list[Mapping[str, Any]] = list(prior_developments or ())

    if prior_developments_path is not None and prior_developments_path.exists():
        try:
            loaded = json.loads(prior_developments_path.read_text(encoding="utf-8"))
        except Exception:
            loaded = None
            prior_ok = False

        if prior_ok:
            if not isinstance(loaded, list):
                prior_ok = False
            else:
                prior_raw.extend(loaded)

    priors = tuple(_parse_prior_development(item) for item in prior_raw) if prior_ok else ()

    return RepositoryState(
        manifests_by_candidate_id=manifests,
        manifests_readable=manifests_ok,
        publish_ledger_rows=publish_rows,
        publish_ledger_readable=publish_ok,
        topic_ledger_rows=topic_rows,
        topic_ledger_readable=topic_ok,
        queue_blocks=queue_blocks,
        queue_readable=queue_ok,
        prior_developments=priors,
        prior_developments_readable=prior_ok,
    )


# --------------------------------------------------------------------------
# Matching (state precedence: manifest > publish ledger > queue > board)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResult:
    ref: str
    development_id: str | None
    identity_basis: str
    same_source: bool
    claim_unchanged: bool
    consequential_kind: str | None
    reserved_kind: str | None
    excluded_kind: str | None
    unresolved: bool


def _manifest_state_flags(
    manifest: Mapping[str, Any]
) -> tuple[str | None, str | None, bool]:
    """Return (consequential_kind, reserved_kind, unresolved) for a manifest."""
    review = manifest.get("review") or {}
    publication = manifest.get("publication") or {}
    approval = manifest.get("approval") or {}

    pub_state = publication.get("state")
    pub_attempts = publication.get("attempts", 0) or 0
    review_state = review.get("state")
    review_attempts = review.get("create_attempts", 0) or 0

    consequential_kind: str | None = None
    unresolved = False

    if pub_state in _CONSEQUENTIAL_LIVE_STATES:
        consequential_kind = pub_state
    elif pub_attempts >= 1 and pub_state in _CONSEQUENTIAL_AFTER_ATTEMPT_STATES:
        consequential_kind = pub_state
        unresolved = True
    elif approval.get("first_stage") is True:
        consequential_kind = "APPROVAL_FIRST_STAGE"

    reserved_kind: str | None = None

    if review_state in _RESERVED_DEFINITE_STATES:
        reserved_kind = review_state
    elif review_state in _RESERVED_UNRESOLVED_STATES:
        reserved_kind = review_state
        unresolved = True
    elif review_attempts >= 1 and review_state not in {"NOT_CREATED"}:
        reserved_kind = review_state or "UNEXPLAINED_CONSUMED_ATTEMPT"
        unresolved = True

    return consequential_kind, reserved_kind, unresolved


def _match_by_candidate_id(
    candidate: CandidateInput, state: RepositoryState
) -> MatchResult | None:
    """Resolve exact resurfacing of the same candidate (candidate_id match).

    Applies manifest > publish ledger > queue precedence: once a source
    higher in precedence has set a kind, a lower one never overrides it
    (a stale READY queue row cannot override a PUBLISHED manifest).
    """
    found = False
    consequential_kind: str | None = None
    reserved_kind: str | None = None
    excluded_kind: str | None = None
    unresolved = False

    manifest = state.manifests_by_candidate_id.get(candidate.candidate_id)

    if manifest is not None:
        found = True
        consequential_kind, reserved_kind, unresolved = _manifest_state_flags(manifest)

    for row in state.publish_ledger_rows:
        if row.get("candidate_id") != candidate.candidate_id:
            continue

        found = True
        result = str(row.get("result") or row.get("event") or "").upper()

        if result in _CONSEQUENTIAL_LIVE_STATES and consequential_kind is None:
            consequential_kind = result
        elif result in _CONSEQUENTIAL_AFTER_ATTEMPT_STATES and consequential_kind is None:
            consequential_kind = result
            unresolved = True

    if candidate.topic_title:
        for block in state.queue_blocks:
            if block.topic != candidate.topic_title:
                continue

            found = True
            status = (block.fields.get("status") or "").upper()

            if status in _EXCLUDED_QUEUE_STATUSES and excluded_kind is None:
                excluded_kind = status
            elif status == "DRAFTED" and manifest is None and excluded_kind is None:
                excluded_kind = "DRAFTED_NO_MANIFEST"
            elif status == "SCHEDULED" and consequential_kind is None:
                consequential_kind = "SCHEDULED"
            elif status == "PUBLISHED" and consequential_kind is None:
                consequential_kind = "PUBLISHED"

            break

    if not found:
        return None

    return MatchResult(
        ref=f"candidate:{candidate.candidate_id}",
        development_id=None,
        identity_basis="EXACT_IDENTIFIER",
        same_source=True,
        claim_unchanged=True,
        consequential_kind=consequential_kind,
        reserved_kind=reserved_kind,
        excluded_kind=excluded_kind,
        unresolved=unresolved,
    )


def _match_prior_development(
    identity: IdentityComputation, state: RepositoryState
) -> MatchResult | None:
    if identity.event_id is None:
        return None

    for prior in state.prior_developments:
        if prior.event_id != identity.event_id:
            continue

        return MatchResult(
            ref=prior.ref,
            development_id=prior.development_id,
            identity_basis=prior.identity_basis,
            same_source=False,
            claim_unchanged=prior.development_id == identity.development_id,
            consequential_kind=prior.consequential_kind,
            reserved_kind=prior.reserved_kind,
            excluded_kind=prior.excluded_kind,
            unresolved=prior.unresolved_outcome,
        )

    return None


# --------------------------------------------------------------------------
# Result object
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakingIdentityResult:
    schema: str
    candidate_id: str
    assessment_ref: str
    state_snapshot_ref: str
    event: Mapping[str, Any]
    dedup: Mapping[str, Any]
    reason_code: str
    reason_text: str
    reconciliation_required: bool
    precedence_rule: str
    state_reads: Mapping[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "assessment_ref": self.assessment_ref,
            "state_snapshot_ref": self.state_snapshot_ref,
            "event": dict(self.event),
            "dedup": dict(self.dedup),
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "reconciliation_required": self.reconciliation_required,
            "audit": {
                "precedence_rule": self.precedence_rule,
                "state_reads": dict(self.state_reads),
            },
        }


def _event_dict(identity: IdentityComputation) -> dict[str, Any]:
    unresolved = identity.identity_basis == "UNRESOLVED"
    return {
        "event_id": None if unresolved else identity.event_id,
        "development_id": None if unresolved else identity.development_id,
        "topic_id": identity.topic_id,
        "identity_basis": identity.identity_basis,
        "identity_refs": list(identity.identity_refs),
    }


def _state_reads(state: RepositoryState) -> dict[str, bool]:
    return {
        "manifests_readable": state.manifests_readable,
        "publish_ledger_readable": state.publish_ledger_readable,
        "topic_ledger_readable": state.topic_ledger_readable,
        "queue_readable": state.queue_readable,
        "prior_developments_readable": state.prior_developments_readable,
    }


def _ambiguous(
    candidate: CandidateInput,
    identity: IdentityComputation,
    state: RepositoryState,
    *,
    reason_code: str,
    reason_text: str,
    matched_refs: tuple[str, ...],
    precedence_rule: str,
) -> BreakingIdentityResult:
    dedup = {
        "decision": "AMBIGUOUS_IDENTITY",
        "matched_refs": list(matched_refs),
        "parent_development_id": None,
        "follow_up_reason": None,
    }
    return BreakingIdentityResult(
        schema=SCHEMA,
        candidate_id=candidate.candidate_id,
        assessment_ref=candidate.assessment_ref,
        state_snapshot_ref=candidate.state_snapshot_ref,
        event=_event_dict(identity),
        dedup=dedup,
        reason_code=reason_code,
        reason_text=reason_text,
        reconciliation_required=True,
        precedence_rule=precedence_rule,
        state_reads=_state_reads(state),
    )


def _distinct(
    candidate: CandidateInput, identity: IdentityComputation, state: RepositoryState
) -> BreakingIdentityResult:
    dedup = {
        "decision": "DISTINCT_EVENT",
        "matched_refs": [],
        "parent_development_id": None,
        "follow_up_reason": None,
    }
    return BreakingIdentityResult(
        schema=SCHEMA,
        candidate_id=candidate.candidate_id,
        assessment_ref=candidate.assessment_ref,
        state_snapshot_ref=candidate.state_snapshot_ref,
        event=_event_dict(identity),
        dedup=dedup,
        reason_code=DISTINCT_REASON_CODE,
        reason_text=(
            "No matching development found in authoritative state; "
            "evidence supports a distinct occurrence."
        ),
        reconciliation_required=False,
        precedence_rule="NO_MATCH_DISTINCT",
        state_reads=_state_reads(state),
    )


def _reason_for_duplicate_like(
    decision: str, match: MatchResult
) -> tuple[str, str, bool]:
    if match.consequential_kind:
        return (
            "EXISTING_CONSEQUENTIAL_STATE",
            f"Matched development already has consequential state ({match.consequential_kind}).",
            bool(match.unresolved),
        )
    if match.reserved_kind:
        return (
            "EXISTING_DRAFT_REQUEST",
            f"Matched development already has a reserved or consumed review request ({match.reserved_kind}).",
            bool(match.unresolved),
        )
    if match.excluded_kind:
        return (
            "CANDIDATE_EXCLUDED",
            f"Matched development is historical/excluded ({match.excluded_kind}) and must not be resurrected.",
            False,
        )
    if decision == "EXACT_DUPLICATE":
        return (
            "EXACT_EVENT_DUPLICATE",
            "Same stable development identity and unchanged scoped claims already considered.",
            False,
        )
    return (
        "SAME_EVENT_DIFFERENT_SOURCE",
        "Different source reports the same underlying development without an evidenced material delta.",
        False,
    )


def _resolve_from_match(
    candidate: CandidateInput,
    identity: IdentityComputation,
    state: RepositoryState,
    match: MatchResult,
    precedence_rule: str,
) -> BreakingIdentityResult:
    if match.same_source:
        decision = "EXACT_DUPLICATE"
    elif match.claim_unchanged:
        decision = "SAME_EVENT"
    else:
        delta = candidate.delta

        if delta is None or delta.parent_claim == delta.new_claim:
            return _ambiguous(
                candidate,
                identity,
                state,
                reason_code="IDENTITY_UNRESOLVED",
                reason_text=(
                    "Matched development has a differing scoped claim without a "
                    "verified, evidenced delta; equivalence cannot be resolved safely."
                ),
                matched_refs=(match.ref,),
                precedence_rule=precedence_rule,
            )

        decision = "MATERIAL_FOLLOW_UP"

    parent_development_id = None
    follow_up_reason = None

    if decision == "MATERIAL_FOLLOW_UP":
        assert candidate.delta is not None
        parent_development_id = match.development_id or match.ref
        follow_up_reason = candidate.delta.delta_kind

        if match.consequential_kind and match.unresolved:
            reason_code = "UNRESOLVED_EVENT_HISTORY"
            reason_text = (
                f"Parent development has unresolved {match.consequential_kind} "
                "state; automatic follow-up regeneration is blocked pending "
                "manual reconciliation."
            )
            reconciliation_required = True
        else:
            reason_code = FOLLOW_UP_LINKED_REASON_CODE
            reason_text = (
                f"Verified substantive delta ({follow_up_reason}) over parent "
                f"development {parent_development_id}."
            )
            reconciliation_required = False
    else:
        reason_code, reason_text, reconciliation_required = _reason_for_duplicate_like(
            decision, match
        )

    dedup = {
        "decision": decision,
        "matched_refs": [match.ref],
        "parent_development_id": parent_development_id,
        "follow_up_reason": follow_up_reason,
    }

    return BreakingIdentityResult(
        schema=SCHEMA,
        candidate_id=candidate.candidate_id,
        assessment_ref=candidate.assessment_ref,
        state_snapshot_ref=candidate.state_snapshot_ref,
        event=_event_dict(identity),
        dedup=dedup,
        reason_code=reason_code,
        reason_text=reason_text,
        reconciliation_required=reconciliation_required,
        precedence_rule=precedence_rule,
        state_reads=_state_reads(state),
    )


def evaluate(candidate: CandidateInput, state: RepositoryState) -> BreakingIdentityResult:
    """Evaluate identity and dedup relation for one candidate.

    Pure with respect to `state` (read-only snapshot passed in); performs no
    I/O, no network access, no AI calls and no state mutation. Calling this
    twice with identical arguments returns an identical result.
    """
    identity = compute_identity(candidate)

    if identity.identity_basis == "UNRESOLVED":
        return _ambiguous(
            candidate,
            identity,
            state,
            reason_code="IDENTITY_UNRESOLVED",
            reason_text=(
                "Evidence does not deterministically resolve a stable "
                "development identity (missing or conflicting identifiers)."
            ),
            matched_refs=(),
            precedence_rule="IDENTITY_UNRESOLVED",
        )

    candidate_match = _match_by_candidate_id(candidate, state)

    if candidate_match is not None:
        return _resolve_from_match(
            candidate, identity, state, candidate_match, "CANDIDATE_ID_EXACT_MATCH"
        )

    prior_match = _match_prior_development(identity, state)

    if prior_match is not None:
        return _resolve_from_match(
            candidate, identity, state, prior_match, "PRIOR_DEVELOPMENT_MATCH"
        )

    if not state.fully_readable:
        return _ambiguous(
            candidate,
            identity,
            state,
            reason_code="STATE_UNAVAILABLE_OR_CONFLICTING",
            reason_text=(
                "Required state could not be read completely and no positive "
                "equivalent record was found."
            ),
            matched_refs=(),
            precedence_rule="STATE_UNAVAILABLE",
        )

    return _distinct(candidate, identity, state)


# --------------------------------------------------------------------------
# Self-test (offline, no fixtures, no network) - registered in run_offline.py
# --------------------------------------------------------------------------


def self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        state = load_repository_state(workspace)

        assert state.manifests_readable
        assert state.publish_ledger_readable
        assert state.fully_readable

        evidence = (
            EvidenceItem(
                ref="self-test:evidence-1",
                supported_claim="Self-test synthetic release.",
                source_url="https://example.invalid/releases/self-test?utm_source=x&id=1",
            ),
        )

        candidate = CandidateInput(
            candidate_id="self-test-candidate",
            assessment_ref="self-test:assessment",
            state_snapshot_ref="self-test:state",
            topic_cluster="self-test-topic",
            evidence=evidence,
        )

        result_a = evaluate(candidate, state)
        result_b = evaluate(candidate, state)

        assert result_a.to_dict() == result_b.to_dict(), "non-deterministic result"
        assert result_a.dedup["decision"] == "DISTINCT_EVENT"
        assert result_a.event["identity_basis"] == "CANONICAL_SOURCE"
        assert not result_a.reconciliation_required

        # URL normalization strips tracking params but preserves identity id.
        assert normalize_url(
            "https://Example.invalid/releases/self-test?utm_source=x&id=1"
        ) == normalize_url("https://example.invalid/releases/self-test?id=1")

        # No mutation: workspace stays empty after evaluation.
        assert not any(workspace.rglob("*"))

    print("BREAKING_IDENTITY_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_STATE_MUTATION=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Breaking Identity/Dedup V1")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
