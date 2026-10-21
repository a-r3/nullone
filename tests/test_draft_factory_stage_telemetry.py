#!/usr/bin/env python3
"""Safe failure-stage telemetry for Draft Factory (offline, no external calls).

Observability only. Proves that each reviewed Draft stage failure
becomes a stable, fixed ``DraftStageError`` reason code at the adapter
boundary and is printed by the runner as
``ROLE_OUTCOME=BLOCKED reason=DraftStageError code=<CODE>``, that
arbitrary exception text (URLs, signed URLs, secret-looking values,
model/caption text) is NEVER emitted, and that Draft behavior (original
exception types/messages inside the provider, side effects, summary,
fail-closed exit status) is unchanged.

Every cycle runs in a temp workspace with mocked Claude transport and
mocked helper seams; no subprocess, network, model, or production state.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

from nullone_bridge_common import BridgeError, atomic_write_json  # noqa: E402
import nullone_claude_draft_provider as draft  # noqa: E402
import nullone_draft_candidate_fallback as fallback  # noqa: E402
import nullone_draft_stage_error as stage  # noqa: E402
import nullone_packaging_receipt as receipt_mod  # noqa: E402
import nullone_provider_adapter as adapter  # noqa: E402
import nullone_provider_router as router  # noqa: E402
from test_draft_factory_claude_route import (  # noqa: E402
    evaluator_cli,
    load_draft_wrapper,
    queue_entry,
    ranked_item,
    select_result,
    write_queue,
)

# Hostile text that must never appear in any telemetry output.
SIGNED_URL = "https://cdn.example.invalid/a.png?X-Amz-Signature=deadbeef&token=zzz"
SECRET_TEXT = "api_key=sk-live-SECRET123 bearer abc.def.ghi password: hunter2"
HOSTILE = f"{SIGNED_URL} {SECRET_TEXT} /home/oem/private/caption.txt CAPTION BODY"
HOSTILE_MARKERS = (
    "X-Amz-Signature", "deadbeef", "token=zzz", "sk-live", "SECRET123",
    "hunter2", "abc.def.ghi", "cdn.example.invalid", "CAPTION BODY",
    "/home/oem", "caption.txt",
)

CID = "post-probe-two"
OTHER = "other-candidate"

QUEUE_TEXT = (
    "# Probe queue\n\n"
    + queue_entry(CID)
    + "\n"
    + queue_entry(OTHER, topic="Other", cluster="other")
)

PRODUCE_RESULT = {
    "caption": "Probe caption #NullOne",
    "render_text": {"headline": "Probe headline"},
    "slides": None,
}
COMPLETE_RESULT = {
    "ledger_record": {
        "candidate_id": CID,
        "review_post_id": "review-probe-1",
        "state": "DRAFT_CREATED",
    },
    "report_markdown": "# Probe draft report\n\n- candidate post-probe-two\n",
}


class Case:
    """One deterministic Draft cycle with an injectable failure point."""

    def __init__(self, test, *, select=None, produce=None, complete=None,
                 structured_error=None, structured_error_phase="SELECT",
                 helper_error=None, helper_skip=None, queue_text=QUEUE_TEXT,
                 pre_files=None, patches=None, change_after_bridge=False,
                 exhaust_before_deliver=False):
        self.test = test
        self.select = select if select is not None else select_result([ranked_item(CID)])
        self.produce = produce if produce is not None else copy.deepcopy(PRODUCE_RESULT)
        self.complete = complete if complete is not None else copy.deepcopy(COMPLETE_RESULT)
        self.structured_error = structured_error
        self.structured_error_phase = structured_error_phase
        self.helper_error = helper_error  # helper short name -> exception
        self.helper_skip = helper_skip or {}  # helper short name -> stdout override
        self.queue_text = queue_text
        self.pre_files = pre_files or {}
        self.patches = patches or []
        self.change_after_bridge = change_after_bridge
        self.exhaust_before_deliver = exhaust_before_deliver
        self.helpers: list[str] = []
        self.rounds: list[str] = []
        self.now = [100.0]
        self._td = tempfile.TemporaryDirectory()
        test.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        (self.root / "social/drafts/production").mkdir(parents=True)
        (self.root / "social/state").mkdir(parents=True)
        if queue_text is not None:
            write_queue(self.root, queue_text)
        today = draft._today(self.root)
        for rel, content in self.pre_files.items():
            path = self.root / rel.format(today=today)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    # -- seams ---------------------------------------------------------
    def fake_structured(self, **kwargs):
        phase = ("SELECT", "PRODUCE", "COMPLETE")[len(self.rounds)]
        self.rounds.append(phase)
        if self.structured_error is not None and phase == self.structured_error_phase:
            raise self.structured_error
        return {"SELECT": self.select, "PRODUCE": self.produce,
                "COMPLETE": self.complete}[phase]

    def fake_helper(self, argv, *, workspace_root, timeout, marker,
                    grounding_unmet_ok=False):
        from PIL import Image

        short = {
            "nullone-packaging-evaluator.py": "evaluate",
            "nullone-packaging-render.py": "render",
            "nullone-manifest.py": "manifest",
            "nullone-draft-bridge.py": "bridge",
            "nullone_telegram_review_delivery_adapter.py": "deliver",
        }[Path(argv[1]).name]
        self.helpers.append(short)
        root = self.root
        if self.helper_error and short in self.helper_error:
            raise self.helper_error[short]
        if short in self.helper_skip:
            return self.helper_skip[short]
        if short == "evaluate":
            cid = argv[argv.index("--candidate-id") + 1]
            request = evaluator_cli.load_validated_request(
                Path(argv[argv.index("--request-file") + 1]), root=root
            )
            receipt = receipt_mod.evaluate_request(cid, request)
            out = receipt_mod.canonical_receipt_path(cid, root=root)
            out.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(out, receipt)
            return f"RECEIPT_PATH={out}\n"
        if short == "render":
            out = Path(argv[argv.index("--output") + 1])
            Image.new("RGB", (1080, 1350), (21, 22, 23)).save(out, "PNG")
            receipt = json.loads(
                receipt_mod.canonical_receipt_path(CID, root=root).read_text(encoding="utf-8")
            )
            record = receipt_mod.build_render_record(
                candidate_id=CID,
                receipt_hash=receipt["receipt_hash"],
                format_decision=receipt["FORMAT_DECISION"],
                asset_kind="NONE",
                outputs=[{"path": str(out.relative_to(root)),
                          "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}],
            )
            atomic_write_json(receipt_mod.canonical_render_record_path(CID, root=root), record)
            return "RENDER_FORMAT=SINGLE_POST\n"
        if short == "manifest":
            manifest_id = argv[argv.index("--manifest-id") + 1]
            path = root / "social/ops/manifests" / f"{manifest_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, {"manifest_id": manifest_id, "emulated": True})
            return "MANIFEST_CREATED=x\n"
        if short == "bridge":
            if self.exhaust_before_deliver:
                self.now[0] = 960.0
            if self.change_after_bridge:
                queue_path = root / draft.QUEUE_PATH
                queue_path.write_text(
                    queue_path.read_text(encoding="utf-8").replace(
                        "- **status:** READY", "- **status:** DRAFTED", 1),
                    encoding="utf-8",
                )
            return ("DRAFT_BRIDGE=PASS\nMANIFEST=m\n"
                    "REVIEW_POST_ID=review-probe-1\nREVIEW_STATE=DRAFT_CREATED\n")
        if short == "deliver":
            return "DELIVERY_STATUS=SENT\nMEDIA_MESSAGE_IDS=1\nAPPROVAL_MESSAGE_ID=1\n"
        raise AssertionError(short)

    # -- run -----------------------------------------------------------
    def _contexts(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(draft.time, "monotonic", side_effect=lambda: self.now[0]))
        stack.enter_context(mock.patch.object(draft, "run_structured", side_effect=self.fake_structured))
        stack.enter_context(mock.patch.object(draft, "_run_helper", side_effect=self.fake_helper))
        stack.enter_context(mock.patch.object(
            draft.subprocess, "run", side_effect=AssertionError("no subprocess")))
        for patcher in self.patches:
            stack.enter_context(patcher())
        return stack

    def run_raw(self):
        """Provider-level run (original exceptions, no conversion)."""
        with self._contexts():
            return draft.invoke_draft(prompt="p", workspace=self.root,
                                      model="sonnet", timeout=900)

    def run_adapter(self):
        """Adapter-level run (the production path: conversion applies)."""
        profile = router.resolve_provider_profile("draft_factory", env={})
        with self._contexts():
            return adapter.invoke_adapter(
                profile,
                adapter.AdapterCall(role="draft_factory", prompt="p", workspace=self.root),
            )

    def files(self):
        return {
            p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(self.root.rglob("*")) if p.is_file()
        }


def runner_line(exc) -> str:
    return stage.format_draft_stage_blocked(exc)


class StageCodeTests(unittest.TestCase):
    """1 + 6-10: each reviewed stage emits only its fixed code."""

    def expect(self, code, case, *, run="run_adapter"):
        with self.assertRaises(stage.DraftStageError) as ctx:
            getattr(case, run)()
        err = ctx.exception
        self.assertEqual(err.reason_code, code)
        self.assertEqual(
            runner_line(err),
            f"ROLE_OUTCOME=BLOCKED reason=DraftStageError code={code}",
        )
        self.assertNotIn("exit=", runner_line(err))
        self.assertEqual(str(err), f"Draft stage failure: {code}")
        return err

    def test_queue_load(self):
        def patch():
            return mock.patch.object(draft, "load_queue", side_effect=BridgeError(HOSTILE))
        self.expect("QUEUE_LOAD", Case(self, patches=[patch]))

    def test_select_validation_distinct(self):
        # Outer decision enum is a result-shape failure (substage code).
        bad = select_result([ranked_item(CID)])
        bad["decision"] = "BOGUS"
        self.expect("SELECT_RESULT_SHAPE", Case(self, select=bad))

    def test_select_validation_generic_fallback_fails_closed(self):
        # Any untagged failure inside SELECT validation keeps the
        # generic SELECT_VALIDATION code (fail closed, no text).
        def patch():
            return mock.patch.object(
                draft, "_validated_select", side_effect=BridgeError(HOSTILE))
        err = self.expect("SELECT_VALIDATION", Case(self, patches=[patch]))
        for marker in HOSTILE_MARKERS:
            self.assertNotIn(marker, runner_line(err))
            self.assertNotIn(marker, str(err))

    def test_select_queue_identity_distinct(self):
        item = ranked_item(CID)
        item["topic"] = "Different topic"
        self.expect("SELECT_QUEUE_IDENTITY_TOPIC",
                    Case(self, select=select_result([item])))

    def test_select_unknown_candidate_is_queue_identity(self):
        self.expect("SELECT_QUEUE_IDENTITY_ELIGIBILITY",
                    Case(self, select=select_result([ranked_item("not-in-queue")])))

    def test_fallback_ledger_init(self):
        def patch():
            return mock.patch.object(
                fallback, "new_ledger", side_effect=fallback.DraftFallbackError(HOSTILE))
        self.expect("FALLBACK_LEDGER_INIT", Case(self, patches=[patch]))

    def test_fallback_next(self):
        def patch():
            return mock.patch.object(
                fallback, "next_candidate", side_effect=fallback.DraftFallbackError(HOSTILE))
        self.expect("FALLBACK_NEXT", Case(self, patches=[patch]))

    def test_existing_receipt_validation(self):
        case = Case(self)
        receipt_file = receipt_mod.canonical_receipt_path(CID, root=case.root)
        receipt_file.parent.mkdir(parents=True, exist_ok=True)
        receipt_file.write_text("{not json " + HOSTILE, encoding="utf-8")
        self.expect("EXISTING_RECEIPT_VALIDATION", case)

    def test_interrupted_state_partial(self):
        case = Case(self, pre_files={
            "social/drafts/production/{today}-" + CID + "-packaging-request.json": "{}",
        })
        self.expect("INTERRUPTED_STATE_VALIDATION", case)

    def test_interrupted_state_malformed_bytes(self):
        base = "social/drafts/production/{today}-" + CID
        case = Case(self, pre_files={
            base + "-packaging-request.json": "not json " + HOSTILE,
            base + "-packaging-asset.json": "{}",
        })
        self.expect("INTERRUPTED_STATE_VALIDATION", case)

    def test_packaging_input_write(self):
        # Fresh-candidate WRITE path: make the request/asset write fail.
        def patch():
            return mock.patch.object(
                draft, "_write_new_file", side_effect=BridgeError(HOSTILE))
        self.expect("PACKAGING_INPUT_WRITE", Case(self, patches=[patch]))

    def test_evaluator_failure_distinguishable(self):
        case = Case(self, helper_error={"evaluate": BridgeError(HOSTILE)})
        self.expect("PACKAGING_EVALUATOR", case)

    def test_packaging_receipt_validation(self):
        # Evaluator "succeeds" but no canonical receipt exists.
        case = Case(self, helper_skip={"evaluate": "RECEIPT_PATH=/nowhere\n"})
        self.expect("PACKAGING_RECEIPT_VALIDATION", case)

    def test_packaging_classification(self):
        def patch():
            return mock.patch.object(
                fallback, "classify_packaging_receipt", side_effect=RuntimeError(HOSTILE))
        self.expect("PACKAGING_CLASSIFICATION", Case(self, patches=[patch]))

    def test_fallback_record(self):
        def patch():
            return mock.patch.object(
                fallback, "record_acceptance", side_effect=fallback.DraftFallbackError(HOSTILE))
        self.expect("FALLBACK_RECORD", Case(self, patches=[patch]))

    def test_produce_validation(self):
        self.expect("PRODUCE_VALIDATION", Case(self, produce={"caption": " "}))

    def test_caption_write(self):
        base = "social/drafts/production/{today}-" + CID
        case = Case(self, pre_files={base + "-caption.txt": "different existing caption\n"})
        self.expect("CAPTION_WRITE", case)

    def test_render_manifest_bridge_distinguishable(self):
        self.expect("RENDER", Case(self, helper_error={"render": BridgeError(HOSTILE)}))
        self.expect("MANIFEST", Case(self, helper_error={"manifest": BridgeError(HOSTILE)}))
        self.expect("DRAFT_BRIDGE", Case(self, helper_error={"bridge": BridgeError(HOSTILE)}))

    def test_render_record_failure_is_render(self):
        # Render helper "succeeds" but writes no render record.
        self.expect("RENDER", Case(self, helper_skip={"render": "RENDER_FORMAT=SINGLE_POST\n"}))

    def test_manifest_missing_after_build_is_manifest(self):
        self.expect("MANIFEST", Case(self, helper_skip={"manifest": "MANIFEST_CREATED=x\n"}))

    def test_bridge_proof_missing_is_bridge(self):
        self.expect("DRAFT_BRIDGE", Case(self, helper_skip={"bridge": "DRAFT_BRIDGE=PASS\n"}))

    def test_queue_status_flip(self):
        self.expect("QUEUE_STATUS_FLIP", Case(self, change_after_bridge=True))

    def test_preview_payload(self):
        import nullone_review_delivery as delivery

        def patch():
            return mock.patch.object(
                delivery, "validate_preview_payload", side_effect=ValueError(HOSTILE))
        self.expect("PREVIEW_PAYLOAD", Case(self, patches=[patch]))

    def test_telegram_delivery_budget_stage(self):
        self.expect("TELEGRAM_DELIVERY", Case(self, exhaust_before_deliver=True))

    def test_claude_complete(self):
        self.expect("CLAUDE_COMPLETE", Case(self, complete={"ledger_record": {}, "report_markdown": " "}))

    def test_ledger_record_validation(self):
        bad = copy.deepcopy(COMPLETE_RESULT)
        bad["ledger_record"]["candidate_id"] = "someone-else"
        self.expect("LEDGER_RECORD_VALIDATION", Case(self, complete=bad))

    def test_ledger_secret_scan(self):
        bad = copy.deepcopy(COMPLETE_RESULT)
        bad["ledger_record"]["note"] = "token: abcdef123456"
        self.expect("LEDGER_SECRET_SCAN", Case(self, complete=bad))

    def test_ledger_write(self):
        case = Case(self)
        (case.root / "social/state/topic-ledger.jsonl").mkdir()
        self.expect("LEDGER_WRITE", case)

    def test_report_write(self):
        bad = copy.deepcopy(COMPLETE_RESULT)
        bad["report_markdown"] = "# r\n\ntoken: abcdef123456\n"
        self.expect("REPORT_WRITE", Case(self, complete=bad))

    def test_phase_codes_for_unknown_transport_failures(self):
        for phase, code in (("SELECT", "CLAUDE_SELECT"), ("PRODUCE", "CLAUDE_PRODUCE"),
                            ("COMPLETE", "CLAUDE_COMPLETE")):
            case = Case(self, structured_error=BridgeError(HOSTILE),
                        structured_error_phase=phase)
            self.expect(code, case)

    def test_untagged_failure_is_unknown(self):
        # Failure outside any reviewed stage (blank prompt guard).
        profile = router.resolve_provider_profile("draft_factory", env={})
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(stage.DraftStageError) as ctx:
                adapter._invoke_claude_cycle(
                    profile,
                    adapter.AdapterCall(role="draft_factory", prompt=" ", workspace=Path(td)),
                )
        self.assertEqual(ctx.exception.reason_code, "UNKNOWN_DRAFT_FAILURE")
        self.assertEqual(
            runner_line(ctx.exception),
            "ROLE_OUTCOME=BLOCKED reason=DraftStageError code=UNKNOWN_DRAFT_FAILURE",
        )

    def test_all_stage_codes_covered_by_allowed_set(self):
        self.assertEqual(len(stage.ALLOWED_DRAFT_REASON_CODES), 51)
        required = {
            "CLAUDE_TIMEOUT", "CLAUDE_BINARY_MISSING", "CLAUDE_EXIT_NONZERO",
            "CLAUDE_OUTPUT_INVALID", "QUEUE_LOAD", "CLAUDE_SELECT",
            "SELECT_VALIDATION", "SELECT_RESULT_SHAPE",
            "SELECT_PACKAGING_REQUEST", "SELECT_ASSET_DESCRIPTOR",
            "SELECT_ASSET_DESCRIPTOR_SHAPE",
            "SELECT_ASSET_DESCRIPTOR_SCHEMA",
            "SELECT_ASSET_DESCRIPTOR_IDENTITY",
            "SELECT_ASSET_DESCRIPTOR_KIND",
            "SELECT_ASSET_DESCRIPTOR_FILE_PATH",
            "SELECT_ASSET_DESCRIPTOR_FILE_PATH_REQUIRED",
            "SELECT_ASSET_DESCRIPTOR_FILE_PATH_FORBIDDEN",
            "SELECT_ASSET_DESCRIPTOR_PROVENANCE",
            "SELECT_ASSET_DESCRIPTOR_OPTIONAL",
            "SELECT_QUEUE_IDENTITY", "SELECT_QUEUE_IDENTITY_ID",
            "SELECT_QUEUE_IDENTITY_ELIGIBILITY",
            "SELECT_QUEUE_IDENTITY_TOPIC",
            "SELECT_QUEUE_IDENTITY_TOPIC_CLUSTER",
            "SELECT_QUEUE_IDENTITY_CONTENT_TYPE",
            "SELECT_QUEUE_IDENTITY_REVIEWABILITY",
            "SELECT_QUEUE_IDENTITY_DUPLICATE",
            "FALLBACK_LEDGER_INIT",
            "FALLBACK_NEXT", "EXISTING_RECEIPT_VALIDATION",
            "INTERRUPTED_STATE_VALIDATION", "PACKAGING_EVALUATOR",
            "PACKAGING_RECEIPT_VALIDATION", "PACKAGING_CLASSIFICATION",
            "FALLBACK_RECORD", "CLAUDE_PRODUCE", "PRODUCE_VALIDATION",
            "CAPTION_WRITE", "RENDER", "MANIFEST", "DRAFT_BRIDGE",
            "QUEUE_STATUS_FLIP", "PREVIEW_PAYLOAD", "TELEGRAM_DELIVERY",
            "CLAUDE_COMPLETE", "LEDGER_RECORD_VALIDATION",
            "LEDGER_SECRET_SCAN", "LEDGER_WRITE", "REPORT_WRITE",
            "UNKNOWN_DRAFT_FAILURE",
        }
        self.assertTrue(required <= stage.ALLOWED_DRAFT_REASON_CODES)
        self.assertEqual(stage.ALLOWED_DRAFT_REASON_CODES - required,
                         {"PACKAGING_INPUT_WRITE"})


class QueueIdentitySubstageTests(unittest.TestCase):
    """SELECT_QUEUE_IDENTITY invariants map to distinct safe codes.

    Natural production failure 2026-10-19 09:45 Asia/Baku reported the
    coarse parent code SELECT_QUEUE_IDENTITY. These tests prove each
    distinct invariant inside that block now names itself in telemetry
    while the original exception type/message, check order, and
    fail-closed domain behavior stay exactly unchanged, and no hostile
    model value can reach any emitted line.
    """

    def expect_code(self, code, case, *, forbidden=()):
        with self.assertRaises(stage.DraftStageError) as ctx:
            case.run_adapter()
        err = ctx.exception
        self.assertEqual(err.reason_code, code)
        self.assertEqual(
            runner_line(err),
            f"ROLE_OUTCOME=BLOCKED reason=DraftStageError code={code}",
        )
        self.assertNotIn("exit=", runner_line(err))
        self.assertEqual(str(err), f"Draft stage failure: {code}")
        for marker in HOSTILE_MARKERS:
            self.assertNotIn(marker, runner_line(err))
            self.assertNotIn(marker, str(err))
        for text in forbidden:
            self.assertNotIn(text, runner_line(err))
            self.assertNotIn(text, str(err))
        return err

    def raw_error(self, **case_kwargs):
        """Provider-level run: original BridgeError, type/message unchanged."""
        case = Case(self, **case_kwargs)
        with self.assertRaises(BridgeError) as ctx:
            case.run_raw()
        self.assertNotIsInstance(ctx.exception, stage.DraftStageError)
        return ctx.exception

    def test_invalid_candidate_id_syntax_is_id_code(self):
        # A: path-hostile id is rejected by the structural id check
        # before any eligibility lookup.
        bad = ranked_item("../etc/passwd")
        self.expect_code(
            "SELECT_QUEUE_IDENTITY_ID", Case(self, select=select_result([bad])),
            forbidden=("passwd",),
        )
        raw = self.raw_error(select=select_result([ranked_item("../etc/passwd")]))
        self.assertEqual(
            str(raw),
            "PACKAGING_INPUT_INVALID: candidate_id must be a path-safe non-empty string",
        )

    def test_non_eligible_id_is_eligibility_code(self):
        # B: structurally valid but absent from the frozen queue map.
        self.expect_code(
            "SELECT_QUEUE_IDENTITY_ELIGIBILITY",
            Case(self, select=select_result([ranked_item("not-in-queue")])),
            forbidden=("not-in-queue",),
        )
        raw = self.raw_error(select=select_result([ranked_item("not-in-queue")]))
        self.assertEqual(str(raw), "Draft SELECT candidate is not eligible")

    def test_topic_mismatch_is_topic_code(self):
        # C: only the topic field deviates from the queue echo.
        item = ranked_item(CID, topic=HOSTILE)
        self.expect_code(
            "SELECT_QUEUE_IDENTITY_TOPIC", Case(self, select=select_result([item]))
        )
        raw = self.raw_error(select=select_result([ranked_item(CID, topic=HOSTILE)]))
        self.assertEqual(str(raw), "Draft SELECT identity disagrees with queue")

    def test_topic_cluster_mismatch_is_topic_cluster_code(self):
        # D: only the topic_cluster field deviates.
        item = ranked_item(CID, topic_cluster=HOSTILE)
        self.expect_code(
            "SELECT_QUEUE_IDENTITY_TOPIC_CLUSTER",
            Case(self, select=select_result([item])),
        )
        raw = self.raw_error(
            select=select_result([ranked_item(CID, topic_cluster=HOSTILE)])
        )
        self.assertEqual(str(raw), "Draft SELECT identity disagrees with queue")

    def test_content_type_mismatch_is_content_type_code(self):
        # E: item (and its nested packaging candidate, single authority
        # chain) consistently claim a reviewable type the queue entry
        # does not have.
        item = ranked_item(CID)
        item["content_type"] = "PRACTICAL"
        item["packaging_request"]["candidate"]["content_type"] = "PRACTICAL"
        self.expect_code(
            "SELECT_QUEUE_IDENTITY_CONTENT_TYPE",
            Case(self, select=select_result([item])),
            forbidden=("PRACTICAL",),
        )
        raw = self.raw_error(select=select_result([item]))
        self.assertEqual(str(raw), "Draft SELECT identity disagrees with queue")

    def test_queue_unreviewable_content_type_is_reviewability_code(self):
        # F: queue-side content_type outside the reviewable CONTENT_TYPES
        # vocabulary (identity fields still agree). Conforming echo of a
        # NEWS queue entry under a vocabulary that excludes NEWS.
        def patch():
            return mock.patch.object(draft, "CONTENT_TYPES", ("AZ_CONTEXT",))
        self.expect_code(
            "SELECT_QUEUE_IDENTITY_REVIEWABILITY", Case(self, patches=[patch])
        )
        raw = self.raw_error(patches=[patch])
        self.assertEqual(str(raw), "Draft candidate content_type not reviewable")

    def test_duplicate_ranked_id_is_duplicate_code(self):
        # G: two conforming items naming the same ranked candidate.
        item = ranked_item(CID)
        dupes = select_result([item, copy.deepcopy(item)])
        self.expect_code(
            "SELECT_QUEUE_IDENTITY_DUPLICATE", Case(self, select=dupes)
        )
        raw = self.raw_error(select=dupes)
        self.assertEqual(str(raw), "Draft ranked candidates contain duplicates")

    def test_conforming_ranked_list_passes_exactly_as_before(self):
        # H: a fully conforming ranked list completes the full cycle.
        case = Case(self)
        summary = case.run_raw()
        self.assertEqual(summary["status"], "DRAFT_CREATED")
        self.assertEqual(summary["candidate_id"], CID)
        self.assertEqual(
            case.helpers, ["evaluate", "render", "manifest", "bridge", "deliver"]
        )

    def test_parent_code_kept_when_no_inner_tag_applies(self):
        # Backward compatibility: the parent code stays registered and
        # an already-tagged (outer) failure is never re-tagged by an
        # inner stage.
        exc = BridgeError("Draft SELECT identity disagrees with queue")
        stage.mark_stage(exc, "SELECT_QUEUE_IDENTITY")
        stage.mark_stage(exc, "SELECT_QUEUE_IDENTITY_TOPIC")  # no override
        self.assertEqual(exc._draft_stage_code, "SELECT_QUEUE_IDENTITY")
        self.assertEqual(
            stage.stage_error_from(exc).reason_code, "SELECT_QUEUE_IDENTITY"
        )
        self.assertIn(
            "SELECT_QUEUE_IDENTITY", stage.ALLOWED_DRAFT_REASON_CODES
        )


class TransportMappingTests(unittest.TestCase):
    """2-5: known local nullone_claude messages map; nothing else does."""

    def run_transport(self, message, phase="SELECT"):
        case = Case(self, structured_error=BridgeError(message),
                    structured_error_phase=phase)
        with self.assertRaises(stage.DraftStageError) as ctx:
            case.run_adapter()
        return ctx.exception

    def test_timeout(self):
        err = self.run_transport("Claude invocation timed out")
        self.assertEqual(err.reason_code, "CLAUDE_TIMEOUT")
        self.assertEqual(runner_line(err),
                         "ROLE_OUTCOME=BLOCKED reason=DraftStageError code=CLAUDE_TIMEOUT")

    def test_binary_missing(self):
        err = self.run_transport("claude binary not found")
        self.assertEqual(err.reason_code, "CLAUDE_BINARY_MISSING")
        self.assertNotIn("exit=", runner_line(err))

    def test_nonzero_exit_preserves_numeric_exit_only(self):
        for exit_code in (1, 2, 137, -9):
            err = self.run_transport(f"Claude invocation failed (exit={exit_code})")
            self.assertEqual(err.reason_code, "CLAUDE_EXIT_NONZERO")
            self.assertEqual(err.exit_code, exit_code)
            self.assertEqual(
                runner_line(err),
                f"ROLE_OUTCOME=BLOCKED reason=DraftStageError code=CLAUDE_EXIT_NONZERO exit={exit_code}",
            )

    def test_nonzero_exit_in_any_phase(self):
        for phase in ("SELECT", "PRODUCE", "COMPLETE"):
            err = self.run_transport("Claude invocation failed (exit=1)", phase)
            self.assertEqual(err.reason_code, "CLAUDE_EXIT_NONZERO")
            self.assertEqual(err.exit_code, 1)

    def test_malformed_output(self):
        for message in ("Claude returned non-JSON output",
                        "Claude JSON output is not an object"):
            err = self.run_transport(message)
            self.assertEqual(err.reason_code, "CLAUDE_OUTPUT_INVALID")
            self.assertNotIn("exit=", runner_line(err))

    def test_lookalike_messages_do_not_map(self):
        for message in (
            "Claude invocation failed (exit=1) " + HOSTILE,
            "Claude invocation failed (exit=abc)",
            "Claude invocation failed (exit=1\n)",
            "xx Claude invocation timed out",
            "Claude invocation timed out " + SIGNED_URL,
            "claude binary not found: " + HOSTILE,
        ):
            err = self.run_transport(message)
            self.assertEqual(err.reason_code, "CLAUDE_SELECT", message)
            self.assertIsNone(err.exit_code)
            line = runner_line(err)
            self.assertEqual(
                line, "ROLE_OUTCOME=BLOCKED reason=DraftStageError code=CLAUDE_SELECT")

    def test_exit_code_only_printed_for_nonzero_code(self):
        err = stage.DraftStageError("RENDER", exit_code=7)
        self.assertEqual(runner_line(err),
                         "ROLE_OUTCOME=BLOCKED reason=DraftStageError code=RENDER")
        self.assertEqual(stage.DraftStageError("CLAUDE_EXIT_NONZERO", exit_code=True).exit_code, None)
        self.assertEqual(stage.DraftStageError("CLAUDE_EXIT_NONZERO", exit_code="9").exit_code, None)


class NoLeakTests(unittest.TestCase):
    """11-13: arbitrary text is never emitted."""

    def test_arbitrary_bridge_error_text_never_emitted(self):
        messages = (HOSTILE, SIGNED_URL, SECRET_TEXT, "Draft helper rejected: x (exit=3)")
        helpers = ("evaluate", "render", "manifest", "bridge")
        for message in messages:
            for helper in helpers:
                case = Case(self, helper_error={helper: BridgeError(message)})
                with self.assertRaises(stage.DraftStageError) as ctx:
                    case.run_adapter()
                err = ctx.exception
                blob = runner_line(err) + str(err) + repr(err) + repr(err.args)
                for marker in HOSTILE_MARKERS + (message,):
                    self.assertNotIn(marker, blob)
                self.assertIsNone(err.__cause__)
                self.assertTrue(err.__suppress_context__)

    def test_hostile_text_through_runner_stdout(self):
        wrapper = load_draft_wrapper()
        for exc in (
            BridgeError(HOSTILE),
            stage.stage_error_from(_tagged(BridgeError(HOSTILE), "RENDER")),
            stage.DraftStageError("MANIFEST"),
        ):
            with mock.patch.object(
                wrapper.provider_adapter, "invoke_role_cycle", side_effect=exc
            ), mock.patch.object(
                wrapper, "ensure_pending_bridge",
                return_value={"status": "NOOP", "attempted": [], "created": {}},
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = wrapper.execute()
            out = buf.getvalue()
            self.assertEqual(rc, 1)
            for marker in HOSTILE_MARKERS:
                self.assertNotIn(marker, out)

    def test_secret_in_every_phase_failure_never_emitted(self):
        for phase in ("SELECT", "PRODUCE", "COMPLETE"):
            case = Case(self, structured_error=BridgeError(SECRET_TEXT + SIGNED_URL),
                        structured_error_phase=phase)
            with self.assertRaises(stage.DraftStageError) as ctx:
                case.run_adapter()
            for marker in HOSTILE_MARKERS:
                self.assertNotIn(marker, runner_line(ctx.exception))

    def test_unknown_or_forged_code_cannot_inject_text(self):
        for forged in (HOSTILE, SIGNED_URL, "render", "", None, 5):
            err = stage.DraftStageError(forged)
            self.assertEqual(err.reason_code, "UNKNOWN_DRAFT_FAILURE")
        fake = BridgeError("x")
        fake.reason_code = HOSTILE
        fake.exit_code = 3
        self.assertEqual(
            stage.format_draft_stage_blocked(fake),
            "ROLE_OUTCOME=BLOCKED reason=DraftStageError code=UNKNOWN_DRAFT_FAILURE",
        )
        tagged = _tagged(BridgeError(HOSTILE), HOSTILE)
        self.assertEqual(stage.stage_error_from(tagged).reason_code, "UNKNOWN_DRAFT_FAILURE")


def _tagged(exc, code):
    stage.mark_stage(exc, code)
    return exc


class BehaviorUnchangedTests(unittest.TestCase):
    """14: behavior and side effects are unchanged."""

    def test_provider_keeps_original_exception_type_and_message(self):
        item = ranked_item(CID)
        item["topic"] = "Different topic"
        case = Case(self, select=select_result([item]))
        with self.assertRaisesRegex(BridgeError, "identity disagrees") as ctx:
            case.run_raw()
        self.assertNotIsInstance(ctx.exception, stage.DraftStageError)
        self.assertEqual(str(ctx.exception), "Draft SELECT identity disagrees with queue")
        # Tag is attribute-only metadata on the same exception object.
        self.assertEqual(ctx.exception._draft_stage_code, "SELECT_QUEUE_IDENTITY_TOPIC")

    def test_innermost_stage_wins_and_transport_beats_phase(self):
        case = Case(self, structured_error=BridgeError("Claude invocation timed out"))
        with self.assertRaises(BridgeError) as ctx:
            case.run_raw()
        self.assertEqual(str(ctx.exception), "Claude invocation timed out")
        self.assertEqual(ctx.exception._draft_stage_code, "CLAUDE_TIMEOUT")

    def test_full_cycle_summary_and_side_effects_identical(self):
        raw = Case(self)
        summary_raw = raw.run_raw()
        via_adapter = Case(self)
        via_adapter.run_adapter()
        self.assertEqual(summary_raw["status"], "DRAFT_CREATED")
        self.assertEqual(summary_raw["candidate_id"], CID)
        self.assertEqual(summary_raw["review_post_id"], "review-probe-1")
        self.assertEqual(summary_raw["notify_state"], "SENT")
        self.assertEqual(raw.helpers, ["evaluate", "render", "manifest", "bridge", "deliver"])
        self.assertEqual(via_adapter.helpers, raw.helpers)
        self.assertEqual(raw.rounds, ["SELECT", "PRODUCE", "COMPLETE"])
        self.assertEqual(set(raw.files()), set(via_adapter.files()))
        queue = (raw.root / draft.QUEUE_PATH).read_text(encoding="utf-8")
        self.assertEqual(queue, QUEUE_TEXT.replace(
            "- **status:** READY", "- **status:** DRAFTED", 1))
        today = draft._today(raw.root)
        for name in (f"{today}-{CID}-caption.txt", f"{today}-{CID}-feed.png",
                     f"{today}-{CID}-preview-payload.json"):
            self.assertTrue((raw.root / "social/drafts/production" / name).is_file(), name)
        self.assertTrue(
            (raw.root / f"social/publisher/{today}-{CID}-draft.md").is_file())

    def test_telegram_failure_still_swallowed_not_a_stage_failure(self):
        case = Case(self, helper_error={"deliver": BridgeError(HOSTILE)})
        outcome = case.run_adapter()
        self.assertEqual(outcome.outcome, "COMPLETED")  # no raise
        raw = Case(self, helper_error={"deliver": BridgeError(HOSTILE)})
        self.assertEqual(raw.run_raw()["notify_state"], "NOTIFY_FAILED")

    def test_failure_side_effects_match_between_raw_and_adapter(self):
        a = Case(self, helper_error={"bridge": BridgeError(HOSTILE)})
        b = Case(self, helper_error={"bridge": BridgeError(HOSTILE)})
        with self.assertRaises(BridgeError):
            a.run_raw()
        with self.assertRaises(stage.DraftStageError):
            b.run_adapter()
        self.assertEqual(a.helpers, b.helpers)
        self.assertEqual(a.files(), b.files())
        # Bridge failure leaves the candidate READY (unchanged queue).
        self.assertEqual((a.root / draft.QUEUE_PATH).read_text(encoding="utf-8"), QUEUE_TEXT)

    def test_non_bridge_exceptions_still_propagate_unchanged(self):
        def patch():
            return mock.patch.object(draft, "load_queue", side_effect=KeyError("boom"))
        case = Case(self, patches=[patch])
        with self.assertRaises(KeyError):
            case.run_adapter()

    def test_grounding_unmet_control_flow_not_a_failure(self):
        item = ranked_item(CID)
        item["packaging_request"]["candidate"].update(
            content_shape="ANNOUNCEMENT", depicts_real_world_subject=True,
            visual_requirement="SOURCE_GROUNDED")
        second = ranked_item(OTHER)
        queue = "# Probe queue\n\n" + queue_entry(CID) + "\n" + queue_entry(OTHER)

        def helper(argv, *, workspace_root, timeout, marker, grounding_unmet_ok=False):
            cid = argv[argv.index("--candidate-id") + 1]
            if cid == CID:
                assert grounding_unmet_ok
                raise draft._PackagingGroundingUnmet()
            raise BridgeError("stop at second candidate " + HOSTILE)

        case = Case(self, select=select_result([item, second]), queue_text=queue)
        with mock.patch.object(case, "fake_helper", side_effect=helper):
            with self.assertRaises(stage.DraftStageError) as ctx:
                case.run_adapter()
        # Candidate one was recorded and skipped; failure belongs to the
        # evaluator of candidate two.
        self.assertEqual(ctx.exception.reason_code, "PACKAGING_EVALUATOR")
        ledger = json.loads(next(
            (case.root / "social/drafts/production").glob("*-draft-fallback-ledger.json")
        ).read_text(encoding="utf-8"))
        self.assertEqual(ledger["attempts"][0]["candidate_id"], CID)


class RunnerBoundaryTests(unittest.TestCase):
    """Runner output + fail-closed exit; generic BridgeError unchanged."""

    def run_wrapper(self, effect):
        wrapper = load_draft_wrapper()
        with mock.patch.object(
            wrapper.provider_adapter, "invoke_role_cycle", side_effect=effect
        ), mock.patch.object(
            wrapper, "ensure_pending_bridge",
            return_value={"status": "NOOP", "attempted": [], "created": {}},
        ) as backstop:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = wrapper.execute()
        return rc, buf.getvalue(), backstop

    def test_typed_stage_error_line_exit_and_backstop(self):
        rc, out, backstop = self.run_wrapper(stage.DraftStageError("PACKAGING_EVALUATOR"))
        self.assertEqual(rc, 1)
        self.assertIn("ROLE_OUTCOME=BLOCKED reason=DraftStageError code=PACKAGING_EVALUATOR\n", out)
        self.assertNotIn("reason=BridgeError", out)
        self.assertIn("OUTCOME=BLOCKED", out)
        self.assertEqual(backstop.call_count, 1)  # backstop still runs

    def test_nonzero_exit_line(self):
        rc, out, _ = self.run_wrapper(
            stage.DraftStageError("CLAUDE_EXIT_NONZERO", exit_code=1))
        self.assertEqual(rc, 1)
        self.assertIn(
            "ROLE_OUTCOME=BLOCKED reason=DraftStageError code=CLAUDE_EXIT_NONZERO exit=1\n", out)

    def test_generic_bridge_error_still_fails_closed_unchanged(self):
        rc, out, backstop = self.run_wrapper(BridgeError(HOSTILE))
        self.assertEqual(rc, 1)
        self.assertIn("ROLE_OUTCOME=BLOCKED reason=BridgeError\n", out)
        self.assertEqual(backstop.call_count, 1)
        for marker in HOSTILE_MARKERS:
            self.assertNotIn(marker, out)

    def test_completed_path_unchanged(self):
        rc, out, _ = self.run_wrapper(
            lambda *a, **k: adapter.AdapterOutcome(
                role="draft_factory", transport="claude", model="sonnet",
                outcome="COMPLETED"))
        self.assertEqual(rc, 0)
        self.assertIn("ROLE_OUTCOME=COMPLETED", out)

    def test_wrapper_imports_neutral_contract_not_vendor_provider(self):
        source = (SCRIPTS / "nullone-draft-factory-run.py").read_text(encoding="utf-8")
        self.assertIn("nullone_draft_stage_error", source)
        self.assertNotIn("nullone_claude_draft_provider", source)
        self.assertNotIn("nullone_claude import", source)
        neutral = (SCRIPTS / "nullone_draft_stage_error.py").read_text(encoding="utf-8")
        self.assertNotIn("import nullone_claude", neutral)
        self.assertNotIn("from nullone_claude", neutral)
        self.assertNotIn("Claude invocation timed out", neutral)
        provider = (SCRIPTS / "nullone_claude_draft_provider.py").read_text(encoding="utf-8")
        self.assertIn("_claude_transport_stage", provider)

    def test_neutral_self_test_and_provider_self_test(self):
        self.assertEqual(stage.self_test(), 0)
        self.assertEqual(draft.self_test(), 0)

    def test_runner_self_test_unchanged(self):
        wrapper = load_draft_wrapper()
        self.assertEqual(wrapper.DRAFT_FACTORY_TIMEOUT_SECONDS, 900)
        self.assertEqual(wrapper.self_test(), 0)


def _mutated(mutate):
    item = ranked_item(CID)
    result = select_result([item])
    mutate(result, item)
    return result


class SelectSubstageTests(unittest.TestCase):
    """SELECT validation substages map to fixed, secret-safe codes."""

    def expect_code(self, code, mutate):
        case = Case(self, select=_mutated(mutate))
        with self.assertRaises(stage.DraftStageError) as ctx:
            case.run_adapter()
        err = ctx.exception
        self.assertEqual(err.reason_code, code)
        line = runner_line(err)
        self.assertEqual(
            line, f"ROLE_OUTCOME=BLOCKED reason=DraftStageError code={code}")
        self.assertEqual(str(err), f"Draft stage failure: {code}")
        for marker in HOSTILE_MARKERS:
            self.assertNotIn(marker, line)
            self.assertNotIn(marker, str(err))
        self.assertNotIn("Malformed", line)
        # Fail closed before any helper side effect; only SELECT ran.
        self.assertEqual(case.helpers, [])
        self.assertEqual(case.rounds, ["SELECT"])
        # Provider-internal exception is unchanged (type + message).
        raw = Case(self, select=_mutated(mutate))
        with self.assertRaises(BridgeError) as raw_ctx:
            raw.run_raw()
        self.assertNotIsInstance(raw_ctx.exception, stage.DraftStageError)
        self.assertEqual(str(raw_ctx.exception), "Malformed Draft Factory SELECT result")
        self.assertEqual(raw_ctx.exception._draft_stage_code, code)

    # -- SELECT_RESULT_SHAPE ------------------------------------------
    def test_outer_result_extra_key(self):
        self.expect_code("SELECT_RESULT_SHAPE",
                         lambda r, i: r.update(extra=HOSTILE))

    def test_outer_ranked_not_list(self):
        self.expect_code("SELECT_RESULT_SHAPE",
                         lambda r, i: r.update(ranked=HOSTILE))

    def test_outer_notes_type(self):
        self.expect_code("SELECT_RESULT_SHAPE", lambda r, i: r.update(notes=7))

    def test_ranked_item_missing_key(self):
        self.expect_code("SELECT_RESULT_SHAPE", lambda r, i: i.pop("topic_cluster"))

    def test_ranked_item_blank_string(self):
        self.expect_code("SELECT_RESULT_SHAPE", lambda r, i: i.update(topic="  "))

    def test_ranked_item_not_dict(self):
        self.expect_code("SELECT_RESULT_SHAPE",
                         lambda r, i: r.update(ranked=[HOSTILE]))

    # -- SELECT_PACKAGING_REQUEST -------------------------------------
    def test_packaging_candidate_field_set(self):
        self.expect_code(
            "SELECT_PACKAGING_REQUEST",
            lambda r, i: i["packaging_request"]["candidate"].update(extra=HOSTILE))

    def test_packaging_forbidden_candidate_field(self):
        self.expect_code(
            "SELECT_PACKAGING_REQUEST",
            lambda r, i: i["packaging_request"]["candidate"].update(
                FORMAT_DECISION=HOSTILE))

    def test_packaging_enum(self):
        self.expect_code(
            "SELECT_PACKAGING_REQUEST",
            lambda r, i: i["packaging_request"]["candidate"].update(timeliness=HOSTILE))

    def test_packaging_int_type(self):
        self.expect_code(
            "SELECT_PACKAGING_REQUEST",
            lambda r, i: i["packaging_request"]["candidate"].update(
                distinct_beat_count=True))

    def test_packaging_nested_content_type_mismatch(self):
        self.expect_code(
            "SELECT_PACKAGING_REQUEST",
            lambda r, i: i["packaging_request"]["candidate"].update(
                content_type="EXPLAINER"))

    def test_packaging_assets_bool_field(self):
        self.expect_code(
            "SELECT_PACKAGING_REQUEST",
            lambda r, i: i["packaging_request"]["assets"].update(image_on_topic="yes"))

    def test_packaging_request_not_dict(self):
        self.expect_code("SELECT_PACKAGING_REQUEST",
                         lambda r, i: i.update(packaging_request=HOSTILE))

    # -- SELECT_ASSET_DESCRIPTOR --------------------------------------
    def test_asset_schema(self):
        self.expect_code("SELECT_ASSET_DESCRIPTOR_SCHEMA",
                         lambda r, i: i["asset"].update(schema=HOSTILE))

    def test_asset_candidate_id_mismatch(self):
        self.expect_code("SELECT_ASSET_DESCRIPTOR_IDENTITY",
                         lambda r, i: i["asset"].update(candidate_id=OTHER))

    def test_asset_kind(self):
        self.expect_code("SELECT_ASSET_DESCRIPTOR_KIND",
                         lambda r, i: i["asset"].update(asset_kind="GENERATED_IMAGE"))

    def test_asset_file_backed_path_provenance_shape(self):
        self.expect_code(
            "SELECT_ASSET_DESCRIPTOR_PROVENANCE",
            lambda r, i: i["asset"].update(
                asset_kind="REAL_PHOTO", local_path="/home/oem/private/caption.txt",
                provenance=None))
        self.expect_code(
            "SELECT_ASSET_DESCRIPTOR_FILE_PATH_REQUIRED",
            lambda r, i: i["asset"].update(asset_kind="REAL_PHOTO", local_path=None))
        self.expect_code(
            "SELECT_ASSET_DESCRIPTOR_FILE_PATH_FORBIDDEN",
            lambda r, i: i["asset"].update(local_path="/home/oem/private/caption.txt"))

    def test_asset_optional_field_type(self):
        self.expect_code("SELECT_ASSET_DESCRIPTOR_OPTIONAL",
                         lambda r, i: i["asset"].update(source_url=["x"]))
        self.expect_code("SELECT_ASSET_DESCRIPTOR_OPTIONAL",
                         lambda r, i: i["asset"].update(sha256=123))

    def test_asset_not_dict(self):
        self.expect_code("SELECT_ASSET_DESCRIPTOR",
                         lambda r, i: i.update(asset=SIGNED_URL))

    def test_asset_extra_key(self):
        self.expect_code("SELECT_ASSET_DESCRIPTOR_SHAPE",
                         lambda r, i: i["asset"].update(extra=HOSTILE))

    def test_asset_missing_key(self):
        self.expect_code("SELECT_ASSET_DESCRIPTOR_SHAPE",
                         lambda r, i: i["asset"].pop("schema"))

    # -- precedence / unchanged ---------------------------------------
    def test_queue_identity_unchanged(self):
        # A content_type echo mismatch still fails at the queue-identity
        # boundary (never earlier at packaging-request validation); it
        # now names its exact invariant substage instead of the parent.
        item = ranked_item(CID)
        item["content_type"] = "EXPLAINER"
        item["packaging_request"]["candidate"]["content_type"] = "EXPLAINER"
        case = Case(self, select=select_result([item]))
        with self.assertRaises(stage.DraftStageError) as ctx:
            case.run_adapter()
        self.assertEqual(
            ctx.exception.reason_code, "SELECT_QUEUE_IDENTITY_CONTENT_TYPE"
        )

    def test_valid_select_unchanged(self):
        case = Case(self)
        summary = case.run_raw()
        self.assertEqual(summary["status"], "DRAFT_CREATED")
        self.assertEqual(case.rounds, ["SELECT", "PRODUCE", "COMPLETE"])
        result = select_result([ranked_item(CID)])
        self.assertIs(draft._validated_select(result), result)

    def test_recovery_helpers_not_select_tagged(self):
        # Shared contract helpers stay untagged so interrupted-state
        # recovery keeps its own INTERRUPTED_STATE_VALIDATION stage.
        with self.assertRaises(BridgeError) as ctx:
            draft._check_packaging_request_contract(
                HOSTILE, expected_content_type="NEWS")
        self.assertIsNone(getattr(ctx.exception, "_draft_stage_code", None))
        with self.assertRaises(BridgeError) as ctx:
            draft._check_asset_descriptor_contract(HOSTILE, candidate_id=CID)
        self.assertIsNone(getattr(ctx.exception, "_draft_stage_code", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
