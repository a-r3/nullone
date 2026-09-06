#!/usr/bin/env python3
"""Behavioral tests for #35 breaking event identity/dedup/follow-up suppression.

These tests exercise `nullone_breaking_identity` against scenarios mirroring
`tests/fixtures/breaking_routing_policy_v1.json` rows (per issue #35 section
22, that fixture is #34's policy fixture, not #35's runtime implementation;
here we prove the identity/dedup inputs those routing rows depend on using
this module's own deterministic engine - we do not reproduce the fixture's
JSON verbatim).

No network, no AI/model calls, no live state writes: all repository state is
built under temporary directories and read back read-only.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "workspace/social/ops/scripts/nullone_breaking_identity.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("nullone_breaking_identity", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bi = _load_module()


def make_candidate(
    candidate_id="candidate-1",
    *,
    evidence=None,
    delta=None,
    topic_cluster="topic-cluster-1",
    topic_title=None,
):
    if evidence is None:
        evidence = (
            bi.EvidenceItem(
                ref="evidence-1",
                supported_claim="Synthetic first-party release claim.",
                source_url="https://example.invalid/releases/1",
            ),
        )
    return bi.CandidateInput(
        candidate_id=candidate_id,
        assessment_ref=f"assessment:{candidate_id}",
        state_snapshot_ref=f"state:{candidate_id}",
        topic_cluster=topic_cluster,
        evidence=evidence,
        delta=delta,
        topic_title=topic_title,
    )


def write_manifest(workspace: Path, candidate_id: str, **overrides) -> Path:
    manifest_dir = workspace / "social" / "ops" / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema": "nullone.production.v1",
        "manifest_id": f"manifest-{candidate_id}",
        "created_at": "2026-01-01T00:00:00+00:00",
        "candidate_id": candidate_id,
        "topic": f"Topic for {candidate_id}",
        "topic_cluster": "topic-cluster-1",
        "content_type": "NEWS",
        "format": "STORY",
        "verification": "PASS",
        "account_id": "6a982bbf77555aae01c28f21",
        "caption": {"file": "unused", "sha256": "0" * 64},
        "media": [],
        "review": {
            "create_attempts": 0,
            "state": "NOT_CREATED",
            "zernio_draft_id": None,
            "created_at": None,
        },
        "approval": {
            "first_stage": False,
            "first_stage_at": None,
            "final_publish": False,
            "final_publish_at": None,
            "source": None,
            "operator": None,
            "human_confirmation": None,
        },
        "publication": {
            "attempts": 0,
            "state": "NOT_REQUESTED",
            "live_zernio_post_id": None,
            "platform_post_id": None,
            "permalink": None,
            "last_checked_at": None,
            "error": None,
        },
    }

    for section, patch in overrides.items():
        manifest[section].update(patch) if isinstance(manifest.get(section), dict) else manifest.update({section: patch})

    path = manifest_dir / f"{candidate_id}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def write_publish_ledger_row(workspace: Path, candidate_id: str, result: str) -> None:
    path = workspace / "social" / "state" / "publish-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"candidate_id": candidate_id, "result": result, "event": result}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def write_topic_ledger_row(workspace: Path, **fields) -> None:
    path = workspace / "social" / "state" / "topic-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + json.dumps(fields) + "\n", encoding="utf-8")


def write_queue_block(workspace: Path, topic: str, status: str, **linkage_fields) -> None:
    """Write one `candidate-queue.md` block.

    `linkage_fields` (e.g. `candidate_id=`, `manifest_id=`, `review_post_id=`,
    `live_zernio_post_id=`, `event_id=`, `development_id=`) are the only
    fields the module treats as deterministic persisted linkage - topic/
    title alone is never sufficient (Correction 1).
    """
    path = workspace / "social" / "state" / "candidate-queue.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [f"- **topic:** {topic}", f"- **status:** {status}"]
    for key, value in linkage_fields.items():
        lines.append(f"- **{key}:** {value}")
    block = "\n".join(lines) + "\n---\n"
    path.write_text(existing + block, encoding="utf-8")


class URLNormalizationTests(unittest.TestCase):
    def test_strips_known_tracking_params(self):
        a = bi.normalize_url("https://example.invalid/a/b?utm_source=x&id=42")
        b = bi.normalize_url("https://example.invalid/a/b?id=42")
        self.assertEqual(a, b)

    def test_preserves_identity_bearing_query_param(self):
        a = bi.normalize_url("https://example.invalid/a?version=2.1")
        b = bi.normalize_url("https://example.invalid/a?version=2.2")
        self.assertNotEqual(a, b)

    def test_host_lowercased_path_case_preserved(self):
        normalized = bi.normalize_url("https://Example.INVALID/A/B")
        self.assertIn("example.invalid", normalized)
        self.assertIn("/A/B", normalized)

    def test_trailing_slash_removed_on_nonroot_path(self):
        a = bi.normalize_url("https://example.invalid/a/b/")
        b = bi.normalize_url("https://example.invalid/a/b")
        self.assertEqual(a, b)

    def test_different_paths_never_merged(self):
        a = bi.normalize_url("https://example.invalid/releases/1")
        b = bi.normalize_url("https://example.invalid/releases/2")
        self.assertNotEqual(a, b)


class IdentityDeterminismTests(unittest.TestCase):
    def test_same_input_repeated_same_ids(self):
        c1 = make_candidate()
        c2 = make_candidate()
        i1 = bi.compute_identity(c1)
        i2 = bi.compute_identity(c2)
        self.assertEqual(i1.event_id, i2.event_id)
        self.assertEqual(i1.development_id, i2.development_id)
        self.assertEqual(i1.topic_id, i2.topic_id)

    def test_identity_has_no_scan_time_or_process_dependence(self):
        # Identity is a pure function of evidence; nothing in CandidateInput
        # or compute_identity accepts a timestamp, run id or random seed.
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "compute_identity":
                for call in ast.walk(node):
                    if isinstance(call, ast.Call):
                        name = getattr(call.func, "id", "") or getattr(call.func, "attr", "")
                        self.assertNotIn(name, {"time", "now", "uuid4", "random", "randint"})

    def test_output_format_does_not_alter_identity(self):
        # Whether the caller renders the result as dict or dataclass, the
        # underlying event/development ids are identical.
        candidate = make_candidate()
        state = bi.load_repository_state(Path(tempfile.mkdtemp()))
        result = bi.evaluate(candidate, state)
        as_dict = result.to_dict()
        self.assertEqual(as_dict["event"]["event_id"], result.event["event_id"])
        self.assertEqual(as_dict["event"]["development_id"], result.event["development_id"])

    def test_tracking_only_url_variant_same_identity(self):
        c1 = make_candidate(evidence=(
            bi.EvidenceItem(ref="e1", supported_claim="claim", source_url="https://example.invalid/releases/9"),
        ))
        c2 = make_candidate(evidence=(
            bi.EvidenceItem(ref="e1", supported_claim="claim", source_url="https://example.invalid/releases/9?utm_source=newsletter"),
        ))
        i1, i2 = bi.compute_identity(c1), bi.compute_identity(c2)
        self.assertEqual(i1.event_id, i2.event_id)
        self.assertEqual(i1.development_id, i2.development_id)

    def test_identity_bearing_url_param_preserved(self):
        c1 = make_candidate(evidence=(
            bi.EvidenceItem(ref="e1", supported_claim="claim", source_url="https://example.invalid/releases?version=1"),
        ))
        c2 = make_candidate(evidence=(
            bi.EvidenceItem(ref="e1", supported_claim="claim", source_url="https://example.invalid/releases?version=2"),
        ))
        i1, i2 = bi.compute_identity(c1), bi.compute_identity(c2)
        self.assertNotEqual(i1.event_id, i2.event_id)


def init_empty_required_stores(workspace: Path) -> None:
    """Explicitly prove all four required stores initialized-empty.

    Uses only directory/file creation (mkdir/touch), never a write helper,
    so this fixture itself never exercises the module's own no-write
    capability guarantee.
    """
    (workspace / "social" / "ops" / "manifests").mkdir(parents=True, exist_ok=True)
    state_dir = workspace / "social" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "publish-ledger.jsonl").touch()
    (state_dir / "topic-ledger.jsonl").touch()
    (state_dir / "candidate-queue.md").touch()


class ExactDuplicateTests(unittest.TestCase):
    def test_completely_missing_state_is_ambiguous_not_distinct(self):
        # Nothing initialized at all: a missing store must never silently
        # mean "no history" - this is a first sighting only in appearance.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            state = bi.load_repository_state(workspace)
            self.assertEqual(state.manifests_status, bi.STATE_MISSING)
            self.assertEqual(state.publish_ledger_status, bi.STATE_MISSING)
            self.assertEqual(state.topic_ledger_status, bi.STATE_MISSING)
            self.assertEqual(state.queue_status, bi.STATE_MISSING)
            self.assertTrue(state.state_gate_failed)

            candidate = make_candidate()
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertEqual(result.reason_code, "STATE_UNAVAILABLE_OR_CONFLICTING")
            self.assertTrue(result.reconciliation_required)

    def test_all_required_stores_initialized_empty_allows_distinct(self):
        # Proven initialized-empty (not merely absent) plus sufficient
        # structured occurrence evidence (product) can reach DISTINCT_EVENT.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            state = bi.load_repository_state(workspace, prior_developments=[])
            self.assertTrue(state.required_sources_proven)
            self.assertFalse(state.state_gate_failed)

            candidate = make_candidate(evidence=(
                bi.EvidenceItem(
                    ref="e1",
                    supported_claim="claim",
                    product="widget",
                    version="1.0",
                ),
            ))
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.dedup["decision"], "DISTINCT_EVENT")
            self.assertEqual(result.event["identity_basis"], "NORMALIZED_CLAIM")
            self.assertFalse(result.reconciliation_required)

    def test_bare_url_only_evidence_never_reaches_distinct_even_when_state_proven(self):
        # A canonical-source-only basis (no exact id, no structured fields)
        # must never mint DISTINCT_EVENT by itself, even with fully proven
        # empty state - only a positive match or ambiguity.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            state = bi.load_repository_state(workspace, prior_developments=[])
            candidate = make_candidate()  # default evidence: URL only, no product
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.event["identity_basis"], "CANONICAL_SOURCE")
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertTrue(result.reconciliation_required)

    def test_repeat_candidate_id_is_exact_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_manifest(workspace, "candidate-1")
            state = bi.load_repository_state(workspace)
            candidate = make_candidate("candidate-1")
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
            self.assertEqual(result.reason_code, "EXACT_EVENT_DUPLICATE")
            self.assertFalse(result.reconciliation_required)

    def test_same_announcement_id_is_exact_identifier_basis(self):
        candidate = make_candidate(evidence=(
            bi.EvidenceItem(
                ref="e1",
                supported_claim="claim",
                announcement_id="ANNOUNCE-42",
                source_url="https://example.invalid/releases/42",
            ),
        ))
        identity = bi.compute_identity(candidate)
        self.assertEqual(identity.identity_basis, "EXACT_IDENTIFIER")


class SameEventDifferentSourceTests(unittest.TestCase):
    def test_different_source_same_development_suppressed(self):
        # Real different-source matching cannot rely on URL equality alone
        # (issue #35 section 16): both outlets reference the same
        # structured announcement identifier even though their article URLs
        # differ, which is what makes deterministic cross-source matching
        # possible without guessing that two URLs are aliases.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            original = make_candidate("candidate-original", evidence=(
                bi.EvidenceItem(
                    ref="e1",
                    supported_claim="Same claim text",
                    announcement_id="official-release-1234",
                    source_url="https://example.invalid/releases/6",
                ),
            ))
            identity = bi.compute_identity(original)

            prior = {
                "ref": "prior:candidate-original",
                "event_id": identity.event_id,
                "development_id": identity.development_id,
                "identity_basis": identity.identity_basis,
                "candidate_id": "candidate-original",
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            different_source = make_candidate("candidate-reprint", evidence=(
                bi.EvidenceItem(
                    ref="e2",
                    supported_claim="Same claim text",
                    announcement_id="official-release-1234",
                    source_url="https://example.invalid/reporting/other-outlet",
                ),
            ))
            result = bi.evaluate(different_source, state)

            self.assertEqual(result.dedup["decision"], "SAME_EVENT")
            self.assertEqual(result.reason_code, "SAME_EVENT_DIFFERENT_SOURCE")
            self.assertFalse(result.reconciliation_required)


class MaterialFollowUpTests(unittest.TestCase):
    def _prior_for(self, candidate, **overrides):
        identity = bi.compute_identity(candidate)
        prior = {
            "ref": "prior:parent",
            "event_id": identity.event_id,
            "development_id": identity.development_id,
            "identity_basis": identity.identity_basis,
        }
        prior.update(overrides)
        return prior, identity

    def _evidence(self, **claim_fields):
        return (
            bi.EvidenceItem(
                ref="e1",
                supported_claim=claim_fields.pop("supported_claim", "claim"),
                source_url="https://example.invalid/releases/99",
                **claim_fields,
            ),
        )

    def _run(self, delta_kind, parent_fields, new_fields, workspace):
        parent_candidate = make_candidate("candidate-parent", evidence=self._evidence(**parent_fields))
        prior, _ = self._prior_for(parent_candidate, consequential_kind="PUBLISHED")
        state = bi.load_repository_state(workspace, prior_developments=[prior])

        new_candidate = make_candidate(
            "candidate-followup",
            evidence=self._evidence(**new_fields),
            delta=bi.FollowUpDelta(
                delta_kind=delta_kind,
                parent_claim="parent claim",
                new_claim="new claim",
                evidence_ref="e1",
            ),
        )
        return bi.evaluate(new_candidate, state)

    def test_availability_changed(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                "AVAILABILITY_CHANGED",
                {"region": "US", "supported_claim": "claim"},
                {"region": "EU", "supported_claim": "claim"},
                Path(td),
            )
            self.assertEqual(result.dedup["decision"], "MATERIAL_FOLLOW_UP")
            self.assertEqual(result.dedup["follow_up_reason"], "AVAILABILITY_CHANGED")
            self.assertIsNotNone(result.dedup["parent_development_id"])

    def test_official_number_changed(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                "OFFICIAL_NUMBER_CHANGED",
                {"number_value": "100", "supported_claim": "claim"},
                {"number_value": "10000", "supported_claim": "claim"},
                Path(td),
            )
            self.assertEqual(result.dedup["follow_up_reason"], "OFFICIAL_NUMBER_CHANGED")

    def test_affected_region_changed(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                "AFFECTED_REGION_CHANGED",
                {"region": "US-only", "supported_claim": "claim"},
                {"region": "Global", "supported_claim": "claim"},
                Path(td),
            )
            self.assertEqual(result.dedup["decision"], "MATERIAL_FOLLOW_UP")
            self.assertEqual(result.dedup["follow_up_reason"], "AFFECTED_REGION_CHANGED")

    def test_material_correction(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                "MATERIAL_CORRECTION",
                {"supported_claim": "original claim"},
                {"supported_claim": "corrected claim"},
                Path(td),
            )
            self.assertEqual(result.dedup["follow_up_reason"], "MATERIAL_CORRECTION")

    def test_product_version_changed(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                "PRODUCT_VERSION_CHANGED",
                {"version": "1.0", "supported_claim": "claim"},
                {"version": "2.0", "supported_claim": "claim"},
                Path(td),
            )
            self.assertEqual(result.dedup["decision"], "MATERIAL_FOLLOW_UP")
            self.assertEqual(result.dedup["follow_up_reason"], "PRODUCT_VERSION_CHANGED")

    def test_user_consequence_changed(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(
                "USER_CONSEQUENCE_CHANGED",
                {"supported_claim": "minor consequence"},
                {"supported_claim": "major consequence"},
                Path(td),
            )
            self.assertEqual(result.dedup["follow_up_reason"], "USER_CONSEQUENCE_CHANGED")

    def test_follow_up_requires_distinct_parent(self):
        with tempfile.TemporaryDirectory() as td:
            parent_candidate = make_candidate("candidate-parent")
            prior, identity = self._prior_for(parent_candidate)
            state = bi.load_repository_state(Path(td), prior_developments=[prior])
            result = bi.evaluate(parent_candidate, state)
            # Same candidate_id / same claim: not a follow-up, it's exact/duplicate territory.
            self.assertNotEqual(result.dedup["decision"], "MATERIAL_FOLLOW_UP")

    def test_new_headline_alone_is_not_a_follow_up(self):
        # No FollowUpDelta supplied and claim unchanged -> SAME_EVENT, not a follow-up.
        with tempfile.TemporaryDirectory() as td:
            parent_candidate = make_candidate("candidate-parent", evidence=(
                bi.EvidenceItem(
                    ref="e1",
                    supported_claim="unchanged claim",
                    announcement_id="official-release-55",
                    source_url="https://example.invalid/releases/55",
                ),
            ))
            identity = bi.compute_identity(parent_candidate)
            prior = {
                "ref": "prior:parent",
                "event_id": identity.event_id,
                "development_id": identity.development_id,
                "identity_basis": identity.identity_basis,
            }
            state = bi.load_repository_state(Path(td), prior_developments=[prior])

            new_headline_candidate = make_candidate("candidate-new-headline", evidence=(
                bi.EvidenceItem(
                    ref="e2",
                    supported_claim="unchanged claim",
                    announcement_id="official-release-55",
                    source_url="https://example.invalid/reporting/rewrite",
                ),
            ))
            result = bi.evaluate(new_headline_candidate, state)
            self.assertEqual(result.dedup["decision"], "SAME_EVENT")


class AmbiguousFollowUpWithoutParentTests(unittest.TestCase):
    """An explicit `candidate.delta` asserts a claimed material follow-up
    relationship; absent a deterministic parent development/event match it
    must fail closed to AMBIGUOUS_IDENTITY, never silently DISTINCT_EVENT
    (Correction 2)."""

    def _evidence(self, **claim_fields):
        # No `product` field by default: a shared canonical source URL
        # (CANONICAL_SOURCE basis) is what keeps the parent/follow-up
        # identity linked here, mirroring MaterialFollowUpTests/
        # ReusedOfficialUrlTests - this class is about the parent-linkage
        # gate itself, not re-testing identity-basis precedence.
        return (
            bi.EvidenceItem(
                ref="e1",
                supported_claim=claim_fields.pop("supported_claim", "claim"),
                source_url="https://example.invalid/releases/correction-2",
                **claim_fields,
            ),
        )

    def test_product_version_changed_with_proven_parent_is_material_follow_up(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            parent = make_candidate(
                "candidate-parent",
                evidence=self._evidence(version="1.0"),
            )
            identity = bi.compute_identity(parent)
            prior = {
                "ref": "prior:parent",
                "event_id": identity.event_id,
                "development_id": identity.development_id,
                "identity_basis": identity.identity_basis,
                "consequential_kind": "PUBLISHED",
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            followup = make_candidate(
                "candidate-followup",
                evidence=self._evidence(version="2.0"),
                delta=bi.FollowUpDelta(
                    delta_kind="PRODUCT_VERSION_CHANGED",
                    parent_claim="1.0",
                    new_claim="2.0",
                    evidence_ref="e1",
                ),
            )
            result = bi.evaluate(followup, state)
            self.assertEqual(result.dedup["decision"], "MATERIAL_FOLLOW_UP")
            self.assertEqual(result.dedup["follow_up_reason"], "PRODUCT_VERSION_CHANGED")

    def test_product_version_changed_without_parent_match_is_ambiguous_not_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            state = bi.load_repository_state(workspace, prior_developments=[])

            followup = make_candidate(
                "candidate-followup",
                evidence=self._evidence(product="widget", version="2.0"),
                delta=bi.FollowUpDelta(
                    delta_kind="PRODUCT_VERSION_CHANGED",
                    parent_claim="1.0",
                    new_claim="2.0",
                    evidence_ref="e1",
                ),
            )
            result = bi.evaluate(followup, state)
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertNotEqual(result.dedup["decision"], "DISTINCT_EVENT")
            self.assertEqual(result.reason_code, "IDENTITY_UNRESOLVED")
            self.assertTrue(result.reconciliation_required)

    def test_affected_region_changed_with_proven_parent_is_material_follow_up(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            parent = make_candidate(
                "candidate-parent",
                evidence=self._evidence(region="US-only"),
            )
            identity = bi.compute_identity(parent)
            prior = {
                "ref": "prior:parent",
                "event_id": identity.event_id,
                "development_id": identity.development_id,
                "identity_basis": identity.identity_basis,
                "consequential_kind": "PUBLISHED",
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            followup = make_candidate(
                "candidate-followup",
                evidence=self._evidence(region="Global"),
                delta=bi.FollowUpDelta(
                    delta_kind="AFFECTED_REGION_CHANGED",
                    parent_claim="US-only",
                    new_claim="Global",
                    evidence_ref="e1",
                ),
            )
            result = bi.evaluate(followup, state)
            self.assertEqual(result.dedup["decision"], "MATERIAL_FOLLOW_UP")
            self.assertEqual(result.dedup["follow_up_reason"], "AFFECTED_REGION_CHANGED")

    def test_affected_region_changed_without_parent_match_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            state = bi.load_repository_state(workspace, prior_developments=[])

            followup = make_candidate(
                "candidate-followup",
                evidence=self._evidence(product="widget", region="Global"),
                delta=bi.FollowUpDelta(
                    delta_kind="AFFECTED_REGION_CHANGED",
                    parent_claim="US-only",
                    new_claim="Global",
                    evidence_ref="e1",
                ),
            )
            result = bi.evaluate(followup, state)
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertEqual(result.reason_code, "IDENTITY_UNRESOLVED")
            self.assertTrue(result.reconciliation_required)

    def test_same_new_version_delta_none_positively_evidenced_may_remain_distinct(self):
        # This is the important negative case for Correction 2: when no
        # delta is asserted at all, a separately, positively evidenced new
        # product/version is still allowed to be DISTINCT_EVENT under the
        # existing positive-evidence rule - the fail-closed behavior above
        # applies only when the caller explicitly asserts a follow-up delta.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            first = make_candidate(
                "candidate-a", evidence=self._evidence(product="widget", version="1.0")
            )
            identity_a = bi.compute_identity(first)
            prior = {
                "ref": "prior:a",
                "event_id": identity_a.event_id,
                "development_id": identity_a.development_id,
                "identity_basis": identity_a.identity_basis,
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            second = make_candidate(
                "candidate-b",
                evidence=self._evidence(product="widget", version="2.0"),
                delta=None,
            )
            result = bi.evaluate(second, state)
            self.assertEqual(result.dedup["decision"], "DISTINCT_EVENT")
            self.assertFalse(result.reconciliation_required)

    def test_explicit_delta_with_missing_required_state_is_ambiguous_not_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            # No required stores initialized at all - state_gate_failed.
            state = bi.load_repository_state(workspace)
            self.assertTrue(state.state_gate_failed)

            followup = make_candidate(
                "candidate-followup",
                evidence=self._evidence(product="widget", version="2.0"),
                delta=bi.FollowUpDelta(
                    delta_kind="PRODUCT_VERSION_CHANGED",
                    parent_claim="1.0",
                    new_claim="2.0",
                    evidence_ref="e1",
                ),
            )
            result = bi.evaluate(followup, state)
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertNotEqual(result.dedup["decision"], "DISTINCT_EVENT")
            self.assertTrue(result.reconciliation_required)


class CrossSourceNoAnnouncementIdTests(unittest.TestCase):
    """No announcement ID: a differing source URL alone must never decide
    identity, in either direction (never merges, never distinguishes)."""

    def test_different_urls_same_structured_occurrence_is_same_event(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            first = make_candidate("candidate-a", evidence=(
                bi.EvidenceItem(
                    ref="e1", supported_claim="claim", product="widget", version="1.0",
                    source_url="https://example.invalid/outlet-a/story",
                ),
            ))
            identity_a = bi.compute_identity(first)
            prior = {
                "ref": "prior:a",
                "event_id": identity_a.event_id,
                "development_id": identity_a.development_id,
                "identity_basis": identity_a.identity_basis,
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            second = make_candidate("candidate-b", evidence=(
                bi.EvidenceItem(
                    ref="e2", supported_claim="claim", product="widget", version="1.0",
                    source_url="https://example.invalid/outlet-b/completely-different-url",
                ),
            ))
            result = bi.evaluate(second, state)
            self.assertEqual(result.dedup["decision"], "SAME_EVENT")
            self.assertEqual(result.event["event_id"], identity_a.event_id)

    def test_different_urls_insufficient_structure_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            # State is fully proven empty; the ONLY reason this cannot
            # resolve is insufficient structured evidence, never the URLs
            # differing.
            state = bi.load_repository_state(workspace, prior_developments=[])
            candidate = make_candidate(evidence=(
                bi.EvidenceItem(
                    ref="e1", supported_claim="claim",
                    source_url="https://example.invalid/outlet-a/story-x",
                ),
            ))
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.event["identity_basis"], "CANONICAL_SOURCE")
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertEqual(result.reason_code, "IDENTITY_UNRESOLVED")


class DistinctEventTests(unittest.TestCase):
    def test_same_topic_cluster_distinct_product_version(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            first = make_candidate("candidate-a", topic_cluster="shared-topic", evidence=(
                bi.EvidenceItem(ref="e1", supported_claim="claim", product="widget", version="1.0"),
            ))
            identity_a = bi.compute_identity(first)
            prior = {
                "ref": "prior:a",
                "event_id": identity_a.event_id,
                "development_id": identity_a.development_id,
                "identity_basis": identity_a.identity_basis,
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            second = make_candidate("candidate-b", topic_cluster="shared-topic", evidence=(
                bi.EvidenceItem(ref="e2", supported_claim="claim", product="widget", version="2.0"),
            ))
            result = bi.evaluate(second, state)

            self.assertEqual(result.dedup["decision"], "DISTINCT_EVENT")
            self.assertEqual(result.event["topic_id"], identity_a.topic_id)
            self.assertNotEqual(result.event["event_id"], identity_a.event_id)

    def test_different_urls_positively_evidenced_distinct_occurrence(self):
        # Different source URLs, but genuinely different structured
        # occurrence metadata (product): a URL difference is never itself
        # the evidence of distinctness, but distinct structured evidence is.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            first = make_candidate("candidate-a", evidence=(
                bi.EvidenceItem(
                    ref="e1", supported_claim="claim", product="widget-x",
                    source_url="https://example.invalid/outlet-a/story",
                ),
            ))
            identity_a = bi.compute_identity(first)
            prior = {
                "ref": "prior:a",
                "event_id": identity_a.event_id,
                "development_id": identity_a.development_id,
                "identity_basis": identity_a.identity_basis,
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            second = make_candidate("candidate-b", evidence=(
                bi.EvidenceItem(
                    ref="e2", supported_claim="claim", product="widget-y",
                    source_url="https://example.invalid/outlet-b/other-story",
                ),
            ))
            result = bi.evaluate(second, state)
            self.assertEqual(result.dedup["decision"], "DISTINCT_EVENT")
            self.assertEqual(result.event["identity_basis"], "NORMALIZED_CLAIM")


class ConsequentialSuppressionTests(unittest.TestCase):
    def _evaluate_with_manifest(self, **manifest_overrides):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_manifest(workspace, "candidate-1", **manifest_overrides)
            state = bi.load_repository_state(workspace)
            candidate = make_candidate("candidate-1")
            return bi.evaluate(candidate, state)

    def test_published(self):
        result = self._evaluate_with_manifest(publication={"state": "PUBLISHED"})
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertFalse(result.reconciliation_required)

    def test_unknown_after_attempt_requires_reconciliation(self):
        result = self._evaluate_with_manifest(publication={"state": "UNKNOWN", "attempts": 1})
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertTrue(result.reconciliation_required)

    def test_publish_in_flight_or_accepted_or_publishing(self):
        for state_value in ("PUBLISH_IN_FLIGHT", "PUBLISH_ACCEPTED", "PUBLISHING"):
            with self.subTest(state=state_value):
                result = self._evaluate_with_manifest(publication={"state": state_value})
                self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
                self.assertFalse(result.reconciliation_required)

    def test_first_stage_approval(self):
        result = self._evaluate_with_manifest(approval={"first_stage": True})
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertFalse(result.reconciliation_required)

    def test_draft_created_is_definite_reserved_state(self):
        result = self._evaluate_with_manifest(
            review={"state": "DRAFT_CREATED", "create_attempts": 1, "zernio_draft_id": "z-1"}
        )
        self.assertEqual(result.reason_code, "EXISTING_DRAFT_REQUEST")
        self.assertFalse(result.reconciliation_required)

    def test_consumed_publication_attempt_readback_failed(self):
        result = self._evaluate_with_manifest(publication={"state": "READBACK_FAILED", "attempts": 1})
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertTrue(result.reconciliation_required)

    def test_consumed_review_create_attempt_review_unknown(self):
        result = self._evaluate_with_manifest(review={"state": "REVIEW_UNKNOWN", "create_attempts": 1})
        self.assertEqual(result.reason_code, "EXISTING_DRAFT_REQUEST")
        self.assertTrue(result.reconciliation_required)

    def test_unknown_with_zero_attempts_still_suppresses(self):
        # UNKNOWN never means empty, even with an inconsistent zero-attempt
        # counter - it is not permission to regenerate.
        result = self._evaluate_with_manifest(publication={"state": "UNKNOWN", "attempts": 0})
        self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertTrue(result.reconciliation_required)

    def test_check_required_with_zero_attempts_still_suppresses(self):
        result = self._evaluate_with_manifest(publication={"state": "CHECK_REQUIRED", "attempts": 0})
        self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertTrue(result.reconciliation_required)

    def test_readback_failed_with_zero_attempts_still_suppresses(self):
        result = self._evaluate_with_manifest(publication={"state": "READBACK_FAILED", "attempts": 0})
        self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertTrue(result.reconciliation_required)

    def test_failed_with_consumed_attempt_suppresses(self):
        result = self._evaluate_with_manifest(publication={"state": "FAILED", "attempts": 1})
        self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertTrue(result.reconciliation_required)

    def test_not_requested_with_consumed_attempt_suppresses_and_reconciles(self):
        # Any consumed publication attempt suppresses regardless of the
        # later/unexplained resulting state - a consumed attempt with
        # NOT_REQUESTED must never become fresh capacity.
        result = self._evaluate_with_manifest(publication={"state": "NOT_REQUESTED", "attempts": 1})
        self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertTrue(result.reconciliation_required)

    def test_draft_created_with_zero_attempts_still_suppresses(self):
        # DRAFT_CREATED is consequential as a state in its own right,
        # independent of the attempt counter.
        result = self._evaluate_with_manifest(
            review={"state": "DRAFT_CREATED", "create_attempts": 0, "zernio_draft_id": "z-1"}
        )
        self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
        self.assertEqual(result.reason_code, "EXISTING_DRAFT_REQUEST")
        self.assertFalse(result.reconciliation_required)

    def test_review_unknown_with_zero_attempts_still_suppresses(self):
        result = self._evaluate_with_manifest(review={"state": "REVIEW_UNKNOWN", "create_attempts": 0})
        self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
        self.assertEqual(result.reason_code, "EXISTING_DRAFT_REQUEST")
        self.assertTrue(result.reconciliation_required)

    def test_not_created_with_consumed_create_attempt_suppresses_and_reconciles(self):
        # Regression for the exact Blocker C gap: a consumed review-create
        # attempt must suppress even when the resulting state is literally
        # NOT_CREATED (previously excluded from the consumed-attempt check).
        result = self._evaluate_with_manifest(review={"state": "NOT_CREATED", "create_attempts": 1})
        self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
        self.assertEqual(result.reason_code, "EXISTING_DRAFT_REQUEST")
        self.assertTrue(result.reconciliation_required)

    def test_all_suppress_equivalent_regeneration(self):
        for kwargs in (
            {"publication": {"state": "PUBLISHED"}},
            {"publication": {"state": "UNKNOWN", "attempts": 1}},
            {"publication": {"state": "PUBLISH_IN_FLIGHT"}},
            {"approval": {"first_stage": True}},
            {"review": {"state": "DRAFT_CREATED", "create_attempts": 1}},
            {"publication": {"state": "CHECK_REQUIRED", "attempts": 1}},
            {"review": {"state": "CREATE_IN_FLIGHT", "create_attempts": 1}},
        ):
            with self.subTest(kwargs=kwargs):
                result = self._evaluate_with_manifest(**kwargs)
                self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")


class ExcludedHistoricalCandidateTests(unittest.TestCase):
    def _evaluate_with_queue(self, status):
        # Linked via candidate_id (deterministic persisted linkage), not via
        # topic/title alone - Correction 1 requires an exact persisted
        # reference before a queue row can establish a match.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_queue_block(
                workspace, "Shared topic title", status, candidate_id="candidate-1"
            )
            state = bi.load_repository_state(workspace)
            candidate = make_candidate("candidate-1", topic_title="Shared topic title")
            return bi.evaluate(candidate, state)

    def test_rejected_legacy_superseded_are_excluded_not_missing(self):
        for status in ("REJECTED", "LEGACY_DRAFT", "SUPERSEDED_DRAFT"):
            with self.subTest(status=status):
                result = self._evaluate_with_queue(status)
                self.assertEqual(result.reason_code, "CANDIDATE_EXCLUDED")
                self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
                self.assertFalse(result.reconciliation_required)

    def test_drafted_without_manifest_is_excluded(self):
        result = self._evaluate_with_queue("DRAFTED")
        self.assertEqual(result.reason_code, "CANDIDATE_EXCLUDED")

    def test_scheduled_is_consequential(self):
        result = self._evaluate_with_queue("SCHEDULED")
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertFalse(result.reconciliation_required)


class QueueIdentityLinkageTests(unittest.TestCase):
    """Queue history is inspected via deterministic linkage only, never via
    a shared topic/title alone (Correction 1)."""

    def test_topic_title_only_queue_match_is_not_identity_match(self):
        # Same topic/title as an excluded queue row, but no deterministic
        # linkage (no candidate_id/manifest_id/post-id/event/development
        # reference) - the row must never be treated as a match, so this
        # reaches DISTINCT_EVENT on the candidate's own sufficient evidence.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            write_queue_block(workspace, "Shared topic title", "REJECTED")
            state = bi.load_repository_state(workspace, prior_developments=[])
            candidate = make_candidate(
                "candidate-unlinked",
                topic_title="Shared topic title",
                evidence=(
                    bi.EvidenceItem(ref="e1", supported_claim="claim", product="widget"),
                ),
            )
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.dedup["decision"], "DISTINCT_EVENT")
            self.assertEqual(result.dedup["matched_refs"], [])

    def test_topic_title_plus_persisted_linkage_may_match(self):
        # Same topic/title AND an exact persisted candidate_id reference -
        # now a legitimate deterministic match per precedence.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_queue_block(
                workspace,
                "Shared topic title",
                "REJECTED",
                candidate_id="candidate-linked",
            )
            state = bi.load_repository_state(workspace)
            candidate = make_candidate(
                "candidate-linked", topic_title="Shared topic title"
            )
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.reason_code, "CANDIDATE_EXCLUDED")
            self.assertIn("queue:0:REJECTED", result.dedup["matched_refs"])

    def test_two_linked_queue_records_both_refs_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_manifest(workspace, "candidate-two")
            write_queue_block(
                workspace, "First topic", "READY", candidate_id="candidate-two"
            )
            write_queue_block(
                workspace,
                "Second topic",
                "READY",
                manifest_id="manifest-candidate-two",
            )
            state = bi.load_repository_state(workspace)
            candidate = make_candidate("candidate-two")
            result = bi.evaluate(candidate, state)
            refs = result.dedup["matched_refs"]
            queue_refs = [r for r in refs if r.startswith("queue:")]
            self.assertEqual(len(queue_refs), 2)

    def test_one_linked_unsafe_queue_record_and_one_stale_ready_wins_unsafe(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_queue_block(
                workspace, "First topic", "SCHEDULED", candidate_id="candidate-mix"
            )
            write_queue_block(
                workspace, "Second topic", "READY", candidate_id="candidate-mix"
            )
            state = bi.load_repository_state(workspace)
            candidate = make_candidate("candidate-mix")
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
            self.assertFalse(result.reconciliation_required)
            refs = result.dedup["matched_refs"]
            self.assertIn("queue:0:SCHEDULED", refs)
            self.assertIn("queue:1:READY", refs)


class TopicLedgerLinkageTests(unittest.TestCase):
    """Topic ledger is inspected via deterministic linkage only, never via
    a shared topic_cluster/title alone (Blocker D)."""

    def test_candidate_id_linked_topic_ledger_row_is_used(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_topic_ledger_row(
                workspace, candidate_id="candidate-1", status="LEGACY_DRAFT"
            )
            state = bi.load_repository_state(workspace)
            candidate = make_candidate("candidate-1")
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.reason_code, "CANDIDATE_EXCLUDED")
            self.assertIn("topic-ledger:0:LEGACY_DRAFT", result.dedup["matched_refs"])

    def test_manifest_id_linked_topic_ledger_row_is_used(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_manifest(workspace, "candidate-1")
            write_topic_ledger_row(
                workspace, manifest_id="manifest-candidate-1", status="LEGACY_DRAFT"
            )
            state = bi.load_repository_state(workspace)
            candidate = make_candidate("candidate-1")
            result = bi.evaluate(candidate, state)
            # The manifest itself (benign) is resurface's highest-precedence
            # signal, so it still governs as EXACT_DUPLICATE - but the
            # linked topic-ledger row is still recorded as matched history.
            self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
            self.assertTrue(
                any(ref.startswith("topic-ledger:") for ref in result.dedup["matched_refs"])
            )

    def test_shared_topic_cluster_alone_is_never_sufficient_linkage(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            write_topic_ledger_row(
                workspace, topic_cluster="topic-cluster-1", status="LEGACY_DRAFT"
            )
            state = bi.load_repository_state(workspace, prior_developments=[])
            candidate = make_candidate(evidence=(
                bi.EvidenceItem(ref="e1", supported_claim="claim", product="widget"),
            ))
            result = bi.evaluate(candidate, state)
            # No exact linkage (no candidate_id/manifest_id/post-id/event
            # match) - the topic-cluster-only row must never be treated as
            # a match, so this reaches DISTINCT_EVENT on its own evidence.
            self.assertEqual(result.dedup["decision"], "DISTINCT_EVENT")
            self.assertEqual(result.dedup["matched_refs"], [])


class AllMatchingHistoryTests(unittest.TestCase):
    """Record all matching history across every source; the highest-
    authority positive record decides, stale/lower records cannot
    downgrade it (Blocker E)."""

    def test_all_sources_recorded_highest_authority_wins(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            # Manifest itself is benign (no unsafe state).
            write_manifest(workspace, "candidate-x")
            # Publish ledger independently proves PUBLISHED - higher
            # authority than the benign manifest state and any lower tier.
            write_publish_ledger_row(workspace, "candidate-x", "PUBLISHED")
            # Queue shows a stale READY row for the same slot, linked via
            # candidate_id (not topic/title alone) - must not downgrade the
            # ledger's PUBLISHED fact.
            write_queue_block(
                workspace, "Some queue title", "READY", candidate_id="candidate-x"
            )
            # Topic ledger, linked via manifest_id, shows a lower-precedence
            # excluded status - must not override PUBLISHED either.
            write_topic_ledger_row(
                workspace, manifest_id="manifest-candidate-x", status="LEGACY_DRAFT"
            )
            state = bi.load_repository_state(workspace)

            candidate = make_candidate("candidate-x", topic_title="Some queue title")
            identity = bi.compute_identity(candidate)
            prior = {
                "ref": "prior:candidate-x-lower",
                "event_id": identity.event_id,
                "development_id": "development-irrelevant",
                "identity_basis": identity.identity_basis,
                "consequential_kind": "FAILED",
                "unresolved_outcome": True,
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            result = bi.evaluate(candidate, state)

            self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
            self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
            self.assertFalse(result.reconciliation_required)

            refs = result.dedup["matched_refs"]
            self.assertTrue(any(r.startswith("manifest:") for r in refs))
            self.assertTrue(any(r.startswith("publish-ledger:") for r in refs))
            self.assertTrue(any(r.startswith("queue:") for r in refs))
            self.assertTrue(any(r.startswith("topic-ledger:") for r in refs))
            self.assertIn("prior:candidate-x-lower", refs)

    def test_multiple_prior_development_records_all_collected(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            candidate = make_candidate("candidate-y")
            identity = bi.compute_identity(candidate)
            priors = [
                {
                    "ref": "prior:y-1",
                    "event_id": identity.event_id,
                    "development_id": identity.development_id,
                    "identity_basis": identity.identity_basis,
                    "reserved_kind": "DRAFT_CREATED",
                },
                {
                    "ref": "prior:y-2",
                    "event_id": identity.event_id,
                    "development_id": identity.development_id,
                    "identity_basis": identity.identity_basis,
                    "consequential_kind": "PUBLISHED",
                },
            ]
            state = bi.load_repository_state(workspace, prior_developments=priors)
            result = bi.evaluate(candidate, state)

            # The most severe (PUBLISHED/consequential) prior record must
            # govern even though a second, less severe record also matched.
            self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
            self.assertIn("prior:y-1", result.dedup["matched_refs"])
            self.assertIn("prior:y-2", result.dedup["matched_refs"])


class PriorDevelopmentIndexSafetyTests(unittest.TestCase):
    def test_malformed_prior_developments_path_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            index_path = workspace / "prior-developments.json"
            index_path.write_text("{not valid json", encoding="utf-8")
            state = bi.load_repository_state(workspace, prior_developments_path=index_path)
            self.assertEqual(state.prior_developments_status, bi.STATE_MALFORMED)
            self.assertTrue(state.state_gate_failed)
            candidate = make_candidate(evidence=(
                bi.EvidenceItem(ref="e1", supported_claim="claim", product="widget"),
            ))
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertEqual(result.reason_code, "STATE_UNAVAILABLE_OR_CONFLICTING")

    def test_conflicting_duplicate_prior_records_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            init_empty_required_stores(workspace)
            priors = [
                {
                    "ref": "prior:conflict",
                    "event_id": "event-a",
                    "development_id": "development-a",
                    "identity_basis": "EXACT_IDENTIFIER",
                },
                {
                    "ref": "prior:conflict",
                    "event_id": "event-b",
                    "development_id": "development-b",
                    "identity_basis": "EXACT_IDENTIFIER",
                },
            ]
            state = bi.load_repository_state(workspace, prior_developments=priors)
            self.assertEqual(state.prior_developments_status, bi.STATE_MALFORMED)
            self.assertTrue(state.state_gate_failed)

    def test_absent_optional_index_does_not_block_a_positive_manifest_match(self):
        # The optional index's absence never gates a definite match.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_manifest(workspace, "candidate-1", publication={"state": "PUBLISHED"})
            state = bi.load_repository_state(workspace)
            self.assertEqual(state.prior_developments_status, bi.STATE_NOT_PROVIDED)
            candidate = make_candidate("candidate-1")
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")

    def test_absent_optional_index_cannot_compensate_for_insufficient_evidence(self):
        # The optional index's absence must not itself enable DISTINCT_EVENT
        # when required state is otherwise incomplete.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            state = bi.load_repository_state(workspace)
            self.assertEqual(state.prior_developments_status, bi.STATE_NOT_PROVIDED)
            candidate = make_candidate(evidence=(
                bi.EvidenceItem(ref="e1", supported_claim="claim", product="widget"),
            ))
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertEqual(result.reason_code, "STATE_UNAVAILABLE_OR_CONFLICTING")


class AmbiguityTests(unittest.TestCase):
    def test_conflicting_exact_identifiers(self):
        candidate = make_candidate(evidence=(
            bi.EvidenceItem(ref="e1", supported_claim="claim", announcement_id="A-1"),
            bi.EvidenceItem(ref="e2", supported_claim="claim", announcement_id="A-2"),
        ))
        state = bi.load_repository_state(Path(tempfile.mkdtemp()))
        result = bi.evaluate(candidate, state)
        self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
        self.assertEqual(result.reason_code, "IDENTITY_UNRESOLVED")
        self.assertTrue(result.reconciliation_required)
        self.assertIsNone(result.event["event_id"])

    def test_one_required_store_missing_is_ambiguous(self):
        # Three required stores explicitly initialized empty, but the
        # publish ledger left entirely missing: the missing store alone
        # must block as ambiguous, never silently count as empty.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "social" / "ops" / "manifests").mkdir(parents=True)
            state_dir = workspace / "social" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "topic-ledger.jsonl").touch()
            (state_dir / "candidate-queue.md").touch()
            state = bi.load_repository_state(workspace)
            self.assertEqual(state.publish_ledger_status, bi.STATE_MISSING)
            self.assertFalse(state.required_sources_proven)
            candidate = make_candidate(evidence=(
                bi.EvidenceItem(ref="e1", supported_claim="claim", product="widget"),
            ))
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertEqual(result.reason_code, "STATE_UNAVAILABLE_OR_CONFLICTING")

    def test_malformed_publish_ledger_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            ledger = workspace / "social" / "state" / "publish-ledger.jsonl"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text("{not valid json\n", encoding="utf-8")
            state = bi.load_repository_state(workspace)
            self.assertEqual(state.publish_ledger_status, bi.STATE_MALFORMED)
            candidate = make_candidate()
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertEqual(result.reason_code, "STATE_UNAVAILABLE_OR_CONFLICTING")

    def test_unreadable_state_directory_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            manifest_dir = workspace / "social" / "ops" / "manifests"
            manifest_dir.mkdir(parents=True)
            bad = manifest_dir / "broken.json"
            bad.write_text("{not json", encoding="utf-8")
            state = bi.load_repository_state(workspace)
            self.assertEqual(state.manifests_status, bi.STATE_MALFORMED)
            candidate = make_candidate()
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")

    def test_truly_unreadable_manifest_directory_is_ambiguous(self):
        # A genuine OSError on listing (permission denied), distinct from a
        # parse failure, still fails closed to ambiguous.
        import os

        if os.geteuid() == 0:
            self.skipTest("permission bits are not enforced for root")

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            manifest_dir = workspace / "social" / "ops" / "manifests"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "candidate-1.json").write_text("{}", encoding="utf-8")
            original_mode = manifest_dir.stat().st_mode
            manifest_dir.chmod(0o000)
            try:
                state = bi.load_repository_state(workspace)
                self.assertEqual(state.manifests_status, bi.STATE_UNREADABLE)
                candidate = make_candidate()
                result = bi.evaluate(candidate, state)
                self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            finally:
                manifest_dir.chmod(original_mode)

    def test_positive_published_match_wins_even_with_lower_store_missing(self):
        # An UNKNOWN publication match still suppresses even when the
        # topic ledger/queue are entirely missing - a definite positive
        # record is never downgraded by a missing lower-precedence source.
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_manifest(workspace, "candidate-1", publication={"state": "UNKNOWN", "attempts": 1})
            state = bi.load_repository_state(workspace)
            self.assertEqual(state.publish_ledger_status, bi.STATE_MISSING)
            self.assertEqual(state.topic_ledger_status, bi.STATE_MISSING)
            self.assertEqual(state.queue_status, bi.STATE_MISSING)
            candidate = make_candidate("candidate-1")
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.dedup["decision"], "EXACT_DUPLICATE")
            self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
            self.assertTrue(result.reconciliation_required)

    def test_same_url_unclear_changed_claims_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            parent = make_candidate("candidate-parent", evidence=(
                bi.EvidenceItem(ref="e1", supported_claim="original claim", source_url="https://example.invalid/releases/77"),
            ))
            identity = bi.compute_identity(parent)
            prior = {
                "ref": "prior:parent",
                "event_id": identity.event_id,
                "development_id": identity.development_id,
                "identity_basis": identity.identity_basis,
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            changed_claim = make_candidate("candidate-changed", evidence=(
                bi.EvidenceItem(ref="e2", supported_claim="different claim", source_url="https://example.invalid/releases/77"),
            ))
            # No FollowUpDelta supplied to explain/evidence the change.
            result = bi.evaluate(changed_claim, state)
            self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")
            self.assertEqual(result.reason_code, "IDENTITY_UNRESOLVED")

    def test_insufficient_claim_fields_unresolved(self):
        candidate = make_candidate(evidence=(
            bi.EvidenceItem(ref="e1", supported_claim="claim with no identifiers"),
        ))
        state = bi.load_repository_state(Path(tempfile.mkdtemp()))
        result = bi.evaluate(candidate, state)
        self.assertEqual(result.dedup["decision"], "AMBIGUOUS_IDENTITY")


class PrecedenceTests(unittest.TestCase):
    def test_stale_ready_queue_does_not_override_published_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_manifest(workspace, "candidate-1", publication={"state": "PUBLISHED"})
            # Linked via candidate_id so it is still collected as matched
            # history, but must not downgrade the manifest's PUBLISHED fact.
            write_queue_block(
                workspace, "Some topic", "READY", candidate_id="candidate-1"
            )
            state = bi.load_repository_state(workspace)
            candidate = make_candidate("candidate-1", topic_title="Some topic")
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
            self.assertTrue(
                any(r.startswith("queue:") for r in result.dedup["matched_refs"])
            )

    def test_absent_lower_precedence_source_does_not_block_positive_manifest_match(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_manifest(workspace, "candidate-1", publication={"state": "PUBLISHED"})
            # No publish ledger, no queue file at all.
            state = bi.load_repository_state(workspace)
            self.assertEqual(state.publish_ledger_status, bi.STATE_MISSING)
            self.assertEqual(state.queue_status, bi.STATE_MISSING)
            candidate = make_candidate("candidate-1")
            result = bi.evaluate(candidate, state)
            self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")


class ReusedOfficialUrlTests(unittest.TestCase):
    def test_same_url_same_claims_is_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            parent = make_candidate("candidate-parent", evidence=(
                bi.EvidenceItem(ref="e1", supported_claim="available in US", source_url="https://example.invalid/releases/official"),
            ))
            identity = bi.compute_identity(parent)
            prior = {
                "ref": "prior:parent",
                "event_id": identity.event_id,
                "development_id": identity.development_id,
                "identity_basis": identity.identity_basis,
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            repeat = make_candidate("candidate-repeat", evidence=(
                bi.EvidenceItem(ref="e2", supported_claim="available in US", source_url="https://example.invalid/releases/official"),
            ))
            result = bi.evaluate(repeat, state)
            self.assertEqual(result.dedup["decision"], "SAME_EVENT")

    def test_same_url_materially_updated_claim_is_follow_up(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            parent = make_candidate("candidate-parent", evidence=(
                bi.EvidenceItem(ref="e1", supported_claim="claim", source_url="https://example.invalid/releases/official", region="US-only"),
            ))
            identity = bi.compute_identity(parent)
            prior = {
                "ref": "prior:parent",
                "event_id": identity.event_id,
                "development_id": identity.development_id,
                "identity_basis": identity.identity_basis,
                "consequential_kind": "PUBLISHED",
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            updated = make_candidate(
                "candidate-updated",
                evidence=(
                    bi.EvidenceItem(ref="e2", supported_claim="claim", source_url="https://example.invalid/releases/official", region="Global"),
                ),
                delta=bi.FollowUpDelta(
                    delta_kind="AVAILABILITY_CHANGED",
                    parent_claim="US-only availability",
                    new_claim="Global availability",
                    evidence_ref="e2",
                ),
            )
            result = bi.evaluate(updated, state)
            self.assertEqual(result.dedup["decision"], "MATERIAL_FOLLOW_UP")
            self.assertEqual(result.dedup["follow_up_reason"], "AVAILABILITY_CHANGED")


class IdempotenceReplayTests(unittest.TestCase):
    def test_repeated_evaluation_identical_result_no_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            write_manifest(workspace, "candidate-1", publication={"state": "PUBLISHED"})
            state = bi.load_repository_state(workspace)
            candidate = make_candidate("candidate-1")

            manifest_path = workspace / "social" / "ops" / "manifests" / "candidate-1.json"
            before = manifest_path.read_text(encoding="utf-8")
            before_mtime = manifest_path.stat().st_mtime_ns

            result1 = bi.evaluate(candidate, state).to_dict()
            result2 = bi.evaluate(candidate, state).to_dict()

            after = manifest_path.read_text(encoding="utf-8")
            after_mtime = manifest_path.stat().st_mtime_ns

            self.assertEqual(result1, result2)
            self.assertEqual(before, after)
            self.assertEqual(before_mtime, after_mtime)


class CapabilityNegativeTests(unittest.TestCase):
    """Statically assert #35 has no publish/draft/schedule/LLM/network capability."""

    FORBIDDEN_IMPORT_MODULES = {
        "requests",
        "urllib.request",
        "http.client",
        "socket",
        "smtplib",
        "ftplib",
        "telegram",
        "anthropic",
        "openai",
    }

    FORBIDDEN_NAME_FRAGMENTS = (
        "publish",
        "schedule",
        "zernio",
        "telegram",
        "draft_bridge",
        "approve",
        "approval_grant",
    )

    def setUp(self):
        self.source = MODULE_PATH.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_forbidden_imports(self):
        imported_names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)
        self.assertFalse(imported_names & self.FORBIDDEN_IMPORT_MODULES, imported_names)

    def test_no_network_sockets_opened(self):
        self.assertNotIn("socket.", self.source)
        self.assertNotIn("urlopen", self.source)
        self.assertNotIn("http.client", self.source)

    def test_no_publish_draft_or_schedule_capable_functions(self):
        defined_names = {
            node.name.lower()
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for fragment in self.FORBIDDEN_NAME_FRAGMENTS:
            for name in defined_names:
                self.assertNotIn(fragment, name, f"{name} suggests dispatch capability")

    def test_module_never_writes_manifest_or_ledger_files(self):
        # The only file writes in this module are read()s; grep for common
        # write patterns used elsewhere in the repo for manifests/ledgers.
        self.assertNotIn("atomic_write_json", self.source)
        self.assertNotIn('open(path, "a"', self.source)
        self.assertNotIn(".write_text(", self.source)

    def test_routing_and_publish_fields_never_produced(self):
        # A descriptive comment mentioning the future #36 field name is fine;
        # the module must never actually construct/emit these output keys.
        self.assertNotIn('"PUBLISH"', self.source)
        self.assertNotIn('"routing_decision":', self.source)
        self.assertNotIn('"draft_targets":', self.source)
        self.assertNotIn('zernio_draft_id"] =', self.source)

        result = bi.evaluate(make_candidate(), bi.load_repository_state(Path(tempfile.mkdtemp()))).to_dict()
        self.assertNotIn("routing_decision", result)
        self.assertNotIn("draft_targets", result)
        self.assertNotIn("severity", result)


class SchemaAlignmentTests(unittest.TestCase):
    """Confirm the vocabulary matches the accepted #34 policy exactly."""

    def test_follow_up_reasons_match_accepted_policy(self):
        self.assertEqual(
            bi.FOLLOW_UP_REASONS,
            {
                "AVAILABILITY_CHANGED",
                "OFFICIAL_NUMBER_CHANGED",
                "AFFECTED_REGION_CHANGED",
                "MATERIAL_CORRECTION",
                "PRODUCT_VERSION_CHANGED",
                "USER_CONSEQUENCE_CHANGED",
            },
        )

    def test_dedup_decisions_match_accepted_policy(self):
        self.assertEqual(
            bi.DEDUP_DECISIONS,
            {
                "EXACT_DUPLICATE",
                "SAME_EVENT",
                "MATERIAL_FOLLOW_UP",
                "DISTINCT_EVENT",
                "AMBIGUOUS_IDENTITY",
            },
        )

    def test_identity_basis_matches_accepted_policy(self):
        self.assertEqual(
            bi.IDENTITY_BASES,
            {"EXACT_IDENTIFIER", "CANONICAL_SOURCE", "NORMALIZED_CLAIM", "UNRESOLVED"},
        )

    def test_result_never_emits_null_output_field(self):
        candidate = make_candidate()
        state = bi.load_repository_state(Path(tempfile.mkdtemp()))
        result = bi.evaluate(candidate, state).to_dict()
        self.assertEqual(result["schema"], "nullone.breaking-identity.v1")
        for key in ("candidate_id", "assessment_ref", "state_snapshot_ref", "reason_code", "reason_text"):
            self.assertTrue(result[key])

    def test_material_follow_up_output_maps_losslessly_into_policy_dedup_fields(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            parent = make_candidate("candidate-parent", evidence=(
                bi.EvidenceItem(ref="e1", supported_claim="claim", product="widget", number_value="100"),
            ))
            identity = bi.compute_identity(parent)
            prior = {
                "ref": "prior:parent",
                "event_id": identity.event_id,
                "development_id": identity.development_id,
                "identity_basis": identity.identity_basis,
                "consequential_kind": "PUBLISHED",
            }
            state = bi.load_repository_state(workspace, prior_developments=[prior])

            followup = make_candidate(
                "candidate-followup",
                evidence=(bi.EvidenceItem(ref="e2", supported_claim="claim", product="widget", number_value="10000"),),
                delta=bi.FollowUpDelta(
                    delta_kind="OFFICIAL_NUMBER_CHANGED",
                    parent_claim="100",
                    new_claim="10000",
                    evidence_ref="e2",
                ),
            )
            result = bi.evaluate(followup, state).to_dict()
            dedup = result["dedup"]
            self.assertEqual(set(dedup), {"decision", "matched_refs", "parent_development_id", "follow_up_reason"})
            self.assertEqual(dedup["decision"], "MATERIAL_FOLLOW_UP")
            self.assertNotEqual(dedup["parent_development_id"], result["event"]["development_id"])
            self.assertIn(dedup["follow_up_reason"], bi.FOLLOW_UP_REASONS)
            event = result["event"]
            self.assertEqual(set(event), {"event_id", "development_id", "topic_id", "identity_basis", "identity_refs"})


if __name__ == "__main__":
    unittest.main()
