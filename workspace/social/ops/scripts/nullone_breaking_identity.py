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

Identity precedence: an exact announcement/event identifier is the strongest
anchor. Absent that, event equivalence is derived only from sufficient
deterministic structured occurrence metadata (product/version/region); a
source URL alone is evidence, never a license to mint a new event, and a
different URL is never by itself proof of a distinct event. When neither an
exact identifier nor sufficient structured metadata is available, an exact
shared canonical source URL may still establish identity for matching
purposes (e.g. a literal reused/repeated URL), but that basis alone can
never produce `DISTINCT_EVENT` - only a positive match or fail-closed
ambiguity. Required state (manifests, publish ledger, topic ledger, queue)
that is missing, unreadable or malformed blocks as ambiguous unless a
definite positive match/suppression is already established from a higher-
precedence source; a proven initialized-empty store counts as empty, a
missing one never does.
"""
from __future__ import annotations

import hashlib
import json
import os
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

# Manifest publication states that are themselves unsafe/ambiguous regardless
# of attempt count - UNKNOWN never means empty, and an inconsistent
# zero-attempt occurrence of these is not permission to regenerate.
_CONSEQUENTIAL_AFTER_ATTEMPT_STATES = frozenset(
    {"UNKNOWN", "READBACK_FAILED", "CHECK_REQUIRED", "FAILED"}
)

# Auditable marker for a consumed publication attempt whose resulting state
# is not itself one of the named unsafe states above (e.g. NOT_REQUESTED
# with attempts >= 1, or any other unexplained state) - still suppresses.
_CONSUMED_PUBLICATION_ATTEMPT = "CONSUMED_PUBLICATION_ATTEMPT"

# Review states that reserve a draft outright (definite, non-ambiguous).
_RESERVED_DEFINITE_STATES = frozenset({"DRAFT_CREATED"})

# Review states that reserve a draft but whose outcome is itself unresolved.
_RESERVED_UNRESOLVED_STATES = frozenset({"CREATE_IN_FLIGHT", "REVIEW_UNKNOWN"})

# Auditable marker for a consumed review-create attempt whose resulting
# state is not itself one of the named states above (including NOT_CREATED
# with create_attempts >= 1) - still suppresses.
_CONSUMED_REVIEW_CREATE_ATTEMPT = "CONSUMED_REVIEW_CREATE_ATTEMPT"

# Queue-status markers for historical/excluded candidates (not active load,
# but never eligible for automatic resurrection).
_EXCLUDED_QUEUE_STATUSES = frozenset(
    {"REJECTED", "LEGACY_DRAFT", "SUPERSEDED_DRAFT"}
)

# Read-state classification for a required or optional state source. A
# proven initialized-empty store counts as empty; a missing one never does.
STATE_MISSING = "MISSING"
STATE_INITIALIZED_EMPTY = "INITIALIZED_EMPTY"
STATE_PRESENT_WITH_DATA = "PRESENT_WITH_DATA"
STATE_UNREADABLE = "UNREADABLE"
STATE_MALFORMED = "MALFORMED"
STATE_NOT_PROVIDED = "NOT_PROVIDED"

# A required source proves it can stand in for "no equivalent history" only
# in these two states; MISSING/UNREADABLE/MALFORMED never do.
_PROVEN_STATUSES = frozenset({STATE_INITIALIZED_EMPTY, STATE_PRESENT_WITH_DATA})


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
        different URLs are never silently merged. This normalization never
        establishes event identity by itself - see `compute_identity`.
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
    """Build a NORMALIZED_CLAIM basis key from structured occurrence fields.

    Requires at minimum a product to safely anchor identity; without one,
    the caller must fail ambiguous (or fall back to exact-URL matching)
    rather than guess semantics. Deliberately excludes the source URL: a
    different URL must never by itself change this key (accepted #34
    policy - "a different article URL is not a new event").
    """
    if not item.product or not item.product.strip():
        return None
    parts = [item.product, item.version or "", item.region or ""]
    return "|".join(_normalize_key(part) for part in parts)


def compute_identity(candidate: CandidateInput) -> IdentityComputation:
    """Deterministically derive event/development/topic identity.

    Precedence (accepted #34 policy - "a different article URL is not a new
    event"; deterministic structured evidence over source URL):
      1. EXACT_IDENTIFIER - a consistent `announcement_id` across evidence.
         Strongest anchor.
      2. NORMALIZED_CLAIM - normalized product/version/region occurrence
         key. Computed from structured fields only; never from the URL, so
         a different source URL can never change this basis or event_id.
      3. CANONICAL_SOURCE - an exact normalized canonical source URL, used
         only when no exact identifier and no sufficient structured
         occurrence metadata exists. This basis proves same-source
         equivalence (a literal reused/repeated URL) for matching, but -
         per `evaluate` - can never by itself license `DISTINCT_EVENT`: an
         absent/differing URL is evidence of nothing on its own.
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
        claim_key = _normalized_claim_key(primary)

        if claim_key is not None:
            basis = "NORMALIZED_CLAIM"
            event_key = f"claim:{claim_key}"
            identity_refs = (primary.ref,)
        else:
            urls = [
                normalize_url(item.source_url)
                for item in candidate.evidence
                if item.source_url
            ]
            if not urls:
                return IdentityComputation(None, None, topic_id, "UNRESOLVED", ())

            basis = "CANONICAL_SOURCE"
            canonical_url = (
                normalize_url(primary.source_url) if primary.source_url else urls[0]
            )
            event_key = f"url:{canonical_url}"
            identity_refs = tuple(sorted(set(urls)))

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
    populate it.

    This source is genuinely optional: its absence is never treated as
    "proven empty" and is never itself grounds for `DISTINCT_EVENT` (it
    simply contributes no matches), but if a caller does attempt to supply
    it (`prior_developments`/`prior_developments_path`), a malformed or
    unreadable supplied index still fails closed to ambiguous - it is never
    silently ignored once an attempt to read it was made.
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


def _read_jsonl_store(path: Path) -> tuple[tuple[dict, ...], str]:
    """Read an append-only JSONL ledger with an explicit read-state.

    A missing file is MISSING (not proven empty). An existing, valid file
    with zero records is a proven INITIALIZED_EMPTY. Any unreadable file or
    malformed line is UNREADABLE/MALFORMED - stricter than the existing
    lenient `nullone_state.read_jsonl`, which silently skips bad lines
    (intentional for this higher-assurance identity domain; it does not
    change that existing helper's behavior).
    """
    if not path.exists():
        return (), STATE_MISSING

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return (), STATE_UNREADABLE

    rows: list[dict] = []

    for line in raw.splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            obj = json.loads(line)
        except Exception:
            return (), STATE_MALFORMED

        if not isinstance(obj, dict):
            return (), STATE_MALFORMED

        rows.append(obj)

    status = STATE_PRESENT_WITH_DATA if rows else STATE_INITIALIZED_EMPTY
    return tuple(rows), status


def _read_manifest_store(manifest_dir: Path) -> tuple[dict[str, dict], str]:
    """Read production manifests keyed by candidate_id, with a read-state.

    A missing directory is MISSING (not proven empty). An existing
    directory containing zero manifests is a proven INITIALIZED_EMPTY. Any
    manifest that fails to parse, lacks a candidate_id, or contradicts an
    already-seen manifest for the same candidate_id is MALFORMED - fail
    closed rather than silently pick one.
    """
    if not manifest_dir.exists():
        return {}, STATE_MISSING

    try:
        # os.listdir (unlike Path.glob on some Python versions) reliably
        # raises OSError on a permission-denied directory rather than
        # silently yielding an empty listing.
        names = sorted(os.listdir(manifest_dir))
    except OSError:
        return {}, STATE_UNREADABLE

    paths = [manifest_dir / name for name in names if name.endswith(".json")]
    out: dict[str, dict] = {}

    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}, STATE_MALFORMED

        if not isinstance(data, dict):
            return {}, STATE_MALFORMED

        candidate_id = data.get("candidate_id")

        if not isinstance(candidate_id, str) or not candidate_id.strip():
            return {}, STATE_MALFORMED

        if candidate_id in out and out[candidate_id] != data:
            return {}, STATE_MALFORMED

        out[candidate_id] = data

    status = STATE_PRESENT_WITH_DATA if out else STATE_INITIALIZED_EMPTY
    return out, status


def _read_queue_store(path: Path) -> tuple[tuple[QueueBlock, ...], str]:
    if not path.exists():
        return (), STATE_MISSING

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return (), STATE_UNREADABLE

    blocks = parse_queue_markdown(text)
    status = STATE_PRESENT_WITH_DATA if blocks else STATE_INITIALIZED_EMPTY
    return blocks, status


@dataclass(frozen=True)
class RepositoryState:
    manifests_by_candidate_id: Mapping[str, dict]
    manifests_status: str
    publish_ledger_rows: tuple[dict, ...]
    publish_ledger_status: str
    topic_ledger_rows: tuple[dict, ...]
    topic_ledger_status: str
    queue_blocks: tuple[QueueBlock, ...]
    queue_status: str
    prior_developments: tuple[PriorDevelopment, ...]
    prior_developments_status: str

    @property
    def required_sources_proven(self) -> bool:
        """True only if every required source is readable-and-classified.

        A required source that is MISSING, UNREADABLE or MALFORMED means
        "no equivalent history" cannot be safely proven from it - it does
        NOT mean the source is safe to treat as empty.
        """
        return (
            self.manifests_status in _PROVEN_STATUSES
            and self.publish_ledger_status in _PROVEN_STATUSES
            and self.topic_ledger_status in _PROVEN_STATUSES
            and self.queue_status in _PROVEN_STATUSES
        )

    @property
    def state_gate_failed(self) -> bool:
        """True when required state (or an attempted optional read) fails closed.

        The optional prior-development index's absence never fails the
        gate (`STATE_NOT_PROVIDED` is not in this check) but a malformed or
        unreadable attempted read of it does - it was not silently ignored.
        """
        return not self.required_sources_proven or self.prior_developments_status in (
            STATE_MALFORMED,
            STATE_UNREADABLE,
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

    manifests, manifests_status = _read_manifest_store(manifest_dir)
    publish_rows, publish_status = _read_jsonl_store(publish_ledger_path)
    topic_rows, topic_status = _read_jsonl_store(topic_ledger_path)
    queue_blocks, queue_status = _read_queue_store(queue_path)

    prior_status = STATE_NOT_PROVIDED
    prior_raw: list[Mapping[str, Any]] = []
    prior_provided = False

    if prior_developments is not None:
        prior_provided = True
        prior_raw.extend(prior_developments)

    if prior_developments_path is not None:
        if not prior_developments_path.exists():
            if not prior_provided:
                prior_status = STATE_NOT_PROVIDED
        else:
            prior_provided = True
            try:
                loaded = json.loads(prior_developments_path.read_text(encoding="utf-8"))
            except OSError:
                prior_status = STATE_UNREADABLE
                loaded = None
            except Exception:
                prior_status = STATE_MALFORMED
                loaded = None

            if loaded is not None:
                if not isinstance(loaded, list):
                    prior_status = STATE_MALFORMED
                else:
                    prior_raw.extend(loaded)

    priors: tuple[PriorDevelopment, ...] = ()

    if prior_provided and prior_status not in (STATE_UNREADABLE, STATE_MALFORMED):
        try:
            parsed = tuple(_parse_prior_development(item) for item in prior_raw)
        except (IdentityInputError, KeyError, TypeError):
            prior_status = STATE_MALFORMED
        else:
            seen_refs: dict[str, PriorDevelopment] = {}
            conflict = False

            for prior in parsed:
                existing = seen_refs.get(prior.ref)
                if existing is not None and existing != prior:
                    conflict = True
                    break
                seen_refs[prior.ref] = prior

            if conflict:
                prior_status = STATE_MALFORMED
            else:
                priors = parsed
                prior_status = STATE_PRESENT_WITH_DATA if priors else STATE_INITIALIZED_EMPTY

    return RepositoryState(
        manifests_by_candidate_id=manifests,
        manifests_status=manifests_status,
        publish_ledger_rows=publish_rows,
        publish_ledger_status=publish_status,
        topic_ledger_rows=topic_rows,
        topic_ledger_status=topic_status,
        queue_blocks=queue_blocks,
        queue_status=queue_status,
        prior_developments=priors,
        prior_developments_status=prior_status,
    )


# --------------------------------------------------------------------------
# Matching (state precedence: manifest+ledger+queue > topic ledger > prior
# development history; positive matches are collected from every source,
# never truncated after the first, but a higher-precedence tier's governing
# interpretation is never overridden by a lower one)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResult:
    source: str
    ref: str
    development_id: str | None
    identity_basis: str
    same_source: bool
    claim_unchanged: bool
    consequential_kind: str | None
    reserved_kind: str | None
    excluded_kind: str | None
    unresolved: bool


def _manifest_kind(manifest: Mapping[str, Any]) -> tuple[str | None, str | None, bool]:
    """Return (consequential_kind, reserved_kind, unresolved) for a manifest.

    Implements the accepted #34 OR semantics exactly: a live/in-flight
    publication state, an unsafe publication state (UNKNOWN/READBACK_FAILED/
    CHECK_REQUIRED/FAILED), or ANY consumed publication attempt
    (`attempts >= 1`, regardless of the resulting state) is consequential.
    Likewise a reserved/unsafe review state, or ANY consumed review-create
    attempt (`create_attempts >= 1`, regardless of the resulting state,
    including NOT_CREATED), is a reserved/consumed request. Zero-attempt
    unsafe states are still consequential - an inconsistent zero-attempt
    unsafe state is never permission to regenerate.
    """
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
    elif pub_state in _CONSEQUENTIAL_AFTER_ATTEMPT_STATES:
        # Unsafe state itself is consequential regardless of attempt count.
        consequential_kind = pub_state
        unresolved = True
    elif pub_attempts >= 1:
        # Any consumed attempt suppresses, even with an unexplained/other
        # resulting state (e.g. NOT_REQUESTED with attempts >= 1).
        consequential_kind = _CONSUMED_PUBLICATION_ATTEMPT
        unresolved = True
    elif approval.get("first_stage") is True:
        consequential_kind = "APPROVAL_FIRST_STAGE"

    reserved_kind: str | None = None

    if review_state in _RESERVED_DEFINITE_STATES:
        reserved_kind = review_state
    elif review_state in _RESERVED_UNRESOLVED_STATES:
        reserved_kind = review_state
        unresolved = True
    elif review_attempts >= 1:
        # Any consumed create attempt suppresses, even with NOT_CREATED or
        # another unexplained resulting state.
        reserved_kind = _CONSUMED_REVIEW_CREATE_ATTEMPT
        unresolved = True

    return consequential_kind, reserved_kind, unresolved


def _classify_ledger_status(status: str) -> tuple[str | None, str | None, bool]:
    """Return (consequential_kind, excluded_kind, unresolved) for one status."""
    if status in _CONSEQUENTIAL_LIVE_STATES:
        return status, None, False
    if status in _CONSEQUENTIAL_AFTER_ATTEMPT_STATES:
        return status, None, True
    if status in _EXCLUDED_QUEUE_STATUSES:
        return None, status, False
    return None, None, False


def _resurface_match(
    candidate: CandidateInput, state: RepositoryState
) -> tuple[MatchResult | None, tuple[str, ...]]:
    """Resolve exact resurfacing of the same candidate_id/topic slot.

    Merges manifest, publish-ledger and queue signals for this exact
    candidate/topic with precedence manifest > publish ledger > queue (a
    stale READY queue row never overrides an unsafe manifest/ledger fact),
    while every individual matched row/manifest/block is still recorded as
    its own audit ref.
    """
    refs: list[str] = []
    found = False
    consequential_kind: str | None = None
    reserved_kind: str | None = None
    excluded_kind: str | None = None
    unresolved = False

    manifest = state.manifests_by_candidate_id.get(candidate.candidate_id)

    if manifest is not None:
        found = True
        manifest_id = manifest.get("manifest_id") or candidate.candidate_id
        refs.append(f"manifest:{manifest_id}")
        consequential_kind, reserved_kind, unresolved = _manifest_kind(manifest)

    for idx, row in enumerate(state.publish_ledger_rows):
        if row.get("candidate_id") != candidate.candidate_id:
            continue

        found = True
        result = str(row.get("result") or row.get("event") or "").upper()
        refs.append(f"publish-ledger:{idx}:{result or 'UNKNOWN'}")

        if consequential_kind is None:
            if result in _CONSEQUENTIAL_LIVE_STATES:
                consequential_kind = result
            elif result in _CONSEQUENTIAL_AFTER_ATTEMPT_STATES:
                consequential_kind = result
                unresolved = True

    if candidate.topic_title:
        for idx, block in enumerate(state.queue_blocks):
            if block.topic != candidate.topic_title:
                continue

            found = True
            status = (block.fields.get("status") or "").upper()
            refs.append(f"queue:{idx}:{status or 'UNKNOWN'}")

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
        return None, ()

    match = MatchResult(
        source="resurface",
        ref=refs[0],
        development_id=None,
        identity_basis="EXACT_IDENTIFIER",
        same_source=True,
        claim_unchanged=True,
        consequential_kind=consequential_kind,
        reserved_kind=reserved_kind,
        excluded_kind=excluded_kind,
        unresolved=unresolved,
    )
    return match, tuple(refs)


def _topic_ledger_matches(
    candidate: CandidateInput, identity: IdentityComputation, state: RepositoryState
) -> tuple[MatchResult, ...]:
    """Collect topic-ledger rows deterministically linked to this candidate.

    Linkage requires an exact persisted reference - the candidate's own
    manifest_id/review-post-id/live-post-id, or (if present) an explicit
    event/development identity match. A shared `topic_cluster` or topic
    title alone is never sufficient to call two developments identical, so
    it is never used as a linkage key here.
    """
    manifest = state.manifests_by_candidate_id.get(candidate.candidate_id)
    manifest_id = manifest.get("manifest_id") if manifest else None
    review_post_id = ((manifest or {}).get("review") or {}).get("zernio_draft_id")
    live_post_id = ((manifest or {}).get("publication") or {}).get("live_zernio_post_id")

    matches: list[MatchResult] = []

    for idx, row in enumerate(state.topic_ledger_rows):
        linked = (
            (row.get("candidate_id") is not None and row.get("candidate_id") == candidate.candidate_id)
            or (manifest_id is not None and row.get("manifest_id") == manifest_id)
            or (live_post_id is not None and row.get("live_zernio_post_id") == live_post_id)
            or (review_post_id is not None and row.get("review_post_id") == review_post_id)
            or (identity.event_id is not None and row.get("event_id") == identity.event_id)
            or (
                identity.development_id is not None
                and row.get("development_id") == identity.development_id
            )
        )

        if not linked:
            continue

        status = str(row.get("status") or "").upper()
        consequential_kind, excluded_kind, unresolved = _classify_ledger_status(status)
        row_development_id = row.get("development_id")

        matches.append(
            MatchResult(
                source="topic_ledger",
                ref=f"topic-ledger:{idx}:{status or 'UNKNOWN'}",
                development_id=row_development_id,
                identity_basis="EXACT_IDENTIFIER",
                same_source=False,
                claim_unchanged=(
                    row_development_id == identity.development_id
                    if row_development_id
                    else True
                ),
                consequential_kind=consequential_kind,
                reserved_kind=None,
                excluded_kind=excluded_kind,
                unresolved=unresolved,
            )
        )

    return tuple(matches)


def _prior_development_matches(
    identity: IdentityComputation, state: RepositoryState
) -> tuple[MatchResult, ...]:
    """Collect ALL prior-development records sharing this event identity.

    Never stops at the first match: multiple prior records for the same
    event are all recorded, and the most consequential one governs.
    """
    if identity.event_id is None:
        return ()

    matches: list[MatchResult] = []

    for prior in state.prior_developments:
        if prior.event_id != identity.event_id:
            continue

        matches.append(
            MatchResult(
                source="prior_development",
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
        )

    return tuple(matches)


def _severity_rank(match: MatchResult) -> int:
    """Lower is more severe/authoritative; used to pick a tier's governing match."""
    if match.consequential_kind is not None and not match.unresolved:
        return 0
    if match.consequential_kind is not None:
        return 1
    if match.reserved_kind is not None and not match.unresolved:
        return 2
    if match.reserved_kind is not None:
        return 3
    if match.excluded_kind is not None:
        return 4
    return 5


def _tier_winner(matches: tuple[MatchResult, ...]) -> MatchResult | None:
    if not matches:
        return None
    return min(matches, key=_severity_rank)


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
    state_reads: Mapping[str, str]

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


def _state_reads(state: RepositoryState) -> dict[str, str]:
    return {
        "manifests_status": state.manifests_status,
        "publish_ledger_status": state.publish_ledger_status,
        "topic_ledger_status": state.topic_ledger_status,
        "queue_status": state.queue_status,
        "prior_developments_status": state.prior_developments_status,
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
            "No matching development found in fully proven authoritative "
            "state; deterministic structured evidence supports a distinct "
            "occurrence."
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
    matched_refs: tuple[str, ...],
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
                matched_refs=matched_refs,
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
        "matched_refs": list(matched_refs),
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

    Order of operations (accepted #34 precedence and safety rules):
      1. Resolve deterministic identity; conflicting/insufficient evidence
         fails ambiguous immediately.
      2. Collect every deterministic positive match across every source
         (manifest, publish ledger, queue, topic ledger, prior development)
         - never stopping at the first. A higher-precedence source's
         governing interpretation is never overridden by a lower one, but
         every matched reference from every source is still recorded.
      3. Any governing match resolves the decision directly - a definite
         positive match/suppression is never downgraded to ambiguous just
         because a lower-precedence source is missing/unreadable/malformed.
      4. Absent any match, required state must be fully proven (readable
         and either genuinely empty or populated, never missing/unreadable/
         malformed) before anything can be declared novel.
      5. Even with fully proven empty state, `DISTINCT_EVENT` requires the
         identity itself to rest on an exact identifier or sufficient
         deterministic structured occurrence metadata - a bare canonical
         source URL (no exact id, no structured fields) can never alone
         prove a distinct event, only fail ambiguous.
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

    resurface, resurface_refs = _resurface_match(candidate, state)
    topic_ledger_matches = _topic_ledger_matches(candidate, identity, state)
    prior_matches = _prior_development_matches(identity, state)

    matched_refs_all: list[str] = []
    for ref in resurface_refs:
        if ref not in matched_refs_all:
            matched_refs_all.append(ref)
    for m in topic_ledger_matches:
        if m.ref not in matched_refs_all:
            matched_refs_all.append(m.ref)
    for m in prior_matches:
        if m.ref not in matched_refs_all:
            matched_refs_all.append(m.ref)

    governing: MatchResult | None = None
    precedence_rule = ""

    if resurface is not None:
        governing = resurface
        precedence_rule = "CANDIDATE_ID_EXACT_MATCH"
    else:
        topic_winner = _tier_winner(topic_ledger_matches)
        if topic_winner is not None:
            governing = topic_winner
            precedence_rule = "TOPIC_LEDGER_LINKED_MATCH"
        else:
            prior_winner = _tier_winner(prior_matches)
            if prior_winner is not None:
                governing = prior_winner
                precedence_rule = "PRIOR_DEVELOPMENT_MATCH"

    if governing is not None:
        return _resolve_from_match(
            candidate, identity, state, governing, tuple(matched_refs_all), precedence_rule
        )

    if state.state_gate_failed:
        return _ambiguous(
            candidate,
            identity,
            state,
            reason_code="STATE_UNAVAILABLE_OR_CONFLICTING",
            reason_text=(
                "Required state could not be proven fully readable (missing, "
                "unreadable or malformed) and no positive equivalent record "
                "was found; a missing store is never treated as empty."
            ),
            matched_refs=tuple(matched_refs_all),
            precedence_rule="STATE_UNAVAILABLE",
        )

    if identity.identity_basis not in ("EXACT_IDENTIFIER", "NORMALIZED_CLAIM"):
        return _ambiguous(
            candidate,
            identity,
            state,
            reason_code="IDENTITY_UNRESOLVED",
            reason_text=(
                "No exact identifier or sufficient deterministic structured "
                "occurrence metadata distinguishes this candidate from prior "
                "coverage; a differing (or absent) source URL alone can "
                "never establish a distinct event."
            ),
            matched_refs=tuple(matched_refs_all),
            precedence_rule="INSUFFICIENT_STRUCTURED_EVIDENCE",
        )

    return _distinct(candidate, identity, state)


# --------------------------------------------------------------------------
# Self-test (offline, no fixtures, no network) - registered in run_offline.py
# --------------------------------------------------------------------------


def self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)

        # Completely missing state substrate must fail closed to ambiguous,
        # never silently "no history" (accepted #34: a missing store is
        # never proven empty).
        missing_state = load_repository_state(workspace)
        assert missing_state.manifests_status == STATE_MISSING
        assert missing_state.publish_ledger_status == STATE_MISSING
        assert missing_state.topic_ledger_status == STATE_MISSING
        assert missing_state.queue_status == STATE_MISSING
        assert not missing_state.required_sources_proven
        assert missing_state.state_gate_failed

        candidate = CandidateInput(
            candidate_id="self-test-candidate",
            assessment_ref="self-test:assessment",
            state_snapshot_ref="self-test:state",
            topic_cluster="self-test-topic",
            evidence=(
                EvidenceItem(
                    ref="self-test:evidence-1",
                    supported_claim="Self-test synthetic release.",
                    source_url="https://example.invalid/releases/self-test?utm_source=x&id=1",
                    product="self-test-widget",
                    version="1.0",
                ),
            ),
        )

        missing_result = evaluate(candidate, missing_state)
        assert missing_result.dedup["decision"] == "AMBIGUOUS_IDENTITY"
        assert missing_result.reconciliation_required

        # Explicitly initialized-empty stores (proven empty, not merely
        # absent) - created without any write-capable helper, matching this
        # module's own no-write-capability guarantee.
        (workspace / "social" / "ops" / "manifests").mkdir(parents=True)
        state_dir = workspace / "social" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "publish-ledger.jsonl").touch()
        (state_dir / "topic-ledger.jsonl").touch()
        (state_dir / "candidate-queue.md").touch()

        empty_state = load_repository_state(workspace, prior_developments=[])
        assert empty_state.manifests_status == STATE_INITIALIZED_EMPTY
        assert empty_state.publish_ledger_status == STATE_INITIALIZED_EMPTY
        assert empty_state.topic_ledger_status == STATE_INITIALIZED_EMPTY
        assert empty_state.queue_status == STATE_INITIALIZED_EMPTY
        assert empty_state.prior_developments_status == STATE_INITIALIZED_EMPTY
        assert empty_state.required_sources_proven
        assert not empty_state.state_gate_failed

        result_a = evaluate(candidate, empty_state)
        result_b = evaluate(candidate, empty_state)

        assert result_a.to_dict() == result_b.to_dict(), "non-deterministic result"
        assert result_a.dedup["decision"] == "DISTINCT_EVENT"
        assert result_a.event["identity_basis"] == "NORMALIZED_CLAIM"
        assert not result_a.reconciliation_required

        # A bare canonical-source URL alone (no exact id, no structured
        # fields) must never mint DISTINCT_EVENT on its own - only a
        # positive match or fail-closed ambiguity.
        url_only_candidate = CandidateInput(
            candidate_id="self-test-url-only",
            assessment_ref="self-test:assessment-2",
            state_snapshot_ref="self-test:state-2",
            topic_cluster="self-test-topic-2",
            evidence=(
                EvidenceItem(
                    ref="self-test:evidence-2",
                    supported_claim="Self-test synthetic release with only a URL.",
                    source_url="https://example.invalid/releases/self-test-2",
                ),
            ),
        )
        url_only_result = evaluate(url_only_candidate, empty_state)
        assert url_only_result.event["identity_basis"] == "CANONICAL_SOURCE"
        assert url_only_result.dedup["decision"] == "AMBIGUOUS_IDENTITY"
        assert url_only_result.reconciliation_required

        # URL normalization strips tracking params but preserves identity id.
        assert normalize_url(
            "https://Example.invalid/releases/self-test?utm_source=x&id=1"
        ) == normalize_url("https://example.invalid/releases/self-test?id=1")

        # No mutation beyond the self-test's own explicit setup above.
        before = sorted(str(p) for p in workspace.rglob("*"))
        evaluate(candidate, empty_state)
        after = sorted(str(p) for p in workspace.rglob("*"))
        assert before == after

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
