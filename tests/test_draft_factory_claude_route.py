#!/usr/bin/env python3
"""Offline regression coverage for the Draft Factory Claude route (issue #194).

Mirrors tests/test_breaking_radar_claude_route.py: real router/adapter
dispatch, mocked Claude boundary, real deterministic helpers where
they prove mediation (packaging evaluator + fallback ledger), fake
subprocesses nowhere near production. No network, no production, no
external calls.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_claude as claude  # noqa: E402
import nullone_claude_draft_provider as draft  # noqa: E402
import nullone_draft_candidate_queue as draft_queue  # noqa: E402
import nullone_provider_adapter as adapter  # noqa: E402
import nullone_provider_router as router  # noqa: E402

MUSE_SPARK = "opencode/muse-spark-1.3-contributor-free"
DRAFT_PROMPT = (
    ROOT / "workspace/social/ops/prompts/draft-factory.md"
).read_text(encoding="utf-8")


def select_result(ranked):
    return {"decision": "SELECT", "ranked": ranked, "notes": "probe"}


def ranked_item(candidate_id="probe-candidate-one", **overrides):
    candidate = {
        "content_type": "NEWS",
        "content_shape": "SINGLE_FACT",
        "timeliness": "TODAY",
        "verification_status": "PASS",
        "source_grounding": "STRONG_PRIMARY",
        "audience_value": "HIGH",
        "distinct_beat_count": 1,
        "depicts_real_world_subject": False,
        "still_developing": False,
        "visual_requirement": "NONE",
    }
    assets = {
        "has_official_or_source_image": False,
        "has_usable_screenshot": False,
        "image_on_topic": False,
        "image_quality_ok": False,
        "data_visualization_possible": False,
    }
    item = {
        "candidate_id": candidate_id,
        "topic": "Probe topic",
        "topic_cluster": "probe",
        "content_type": "NEWS",
        "packaging_request": {"candidate": candidate, "assets": assets},
        "asset": {"asset_kind": "NONE"},
    }
    item.update(overrides)
    return item


def queue_entry(candidate_id="post-probe-two", *, topic="Probe topic",
                cluster="probe", content_type="NEWS", status="READY"):
    return (
        f"- **candidate_id:** {candidate_id}\n"
        f"- **topic:** {topic}\n"
        f"- **topic_cluster:** {cluster}\n"
        f"- **content_type:** {content_type}\n"
        f"- **status:** {status}\n"
        "- **verification_status:** PASS\n"
    )


def write_queue(root, content):
    path = root / draft_queue.QUEUE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def load_hyphenated(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evaluator_cli = load_hyphenated("draft_test_evaluator_cli", "nullone-packaging-evaluator.py")


def load_draft_wrapper():
    spec = importlib.util.spec_from_file_location(
        "nullone_draft_factory_run_under_test",
        SCRIPTS / "nullone-draft-factory-run.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def emulated_run_helper(root, recorded):
    """Deterministic helper stand-in for offline mediation tests.

    Executes NO subprocess. For the evaluator it runs the REAL
    request validation + policy evaluation and writes a REAL
    receipt through the REAL canonical path (hash-verified on
    read); every other helper is out of scope for the calling test
    and raises. Exact argv is recorded for boundary assertions.
    """
    from nullone_bridge_common import atomic_write_json
    from nullone_packaging_receipt import (
        canonical_receipt_path,
        evaluate_request,
    )

    def fake(argv, *, workspace_root, timeout, marker):
        recorded.append(list(argv))
        assert workspace_root == root
        if argv[1].endswith("nullone-packaging-evaluator.py"):
            cid = argv[argv.index("--candidate-id") + 1]
            request = evaluator_cli.load_validated_request(
                Path(argv[argv.index("--request-file") + 1]), root=root
            )
            receipt = evaluate_request(cid, request)
            out = canonical_receipt_path(cid, root=root)
            out.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(out, receipt)
            assert marker in f"RECEIPT_PATH={out}"
            return f"RECEIPT_PATH={out}\nFORMAT_DECISION={receipt['FORMAT_DECISION']}\n"
        raise AssertionError(f"unexpected helper in this test: {argv[1]}")

    return fake
    spec = importlib.util.spec_from_file_location(
        "nullone_draft_factory_run_under_test",
        SCRIPTS / "nullone-draft-factory-run.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DraftClaudeRouteTests(unittest.TestCase):
    def test_checked_in_profile_and_other_routes_unchanged(self):
        expected = {
            "breaking_radar": ("claude", "haiku", 600),
            "draft_factory": ("claude", "sonnet", 900),
            "morning_editorial": ("claude", "sonnet", 600),
            "story_writer": ("claude", "haiku", 300),
            "weekly_strategy": ("claude", "sonnet", 600),
        }
        for role, wanted in expected.items():
            profile = router.resolve_provider_profile(role, env={})
            self.assertEqual(
                (profile.transport, profile.model, profile.timeout_seconds), wanted, role
            )
            self.assertEqual(profile.fallback_policy, "none", role)
            self.assertIn(role, router.CLAUDE_SUPPORTED_ROLES, role)

    def test_legacy_opencode_model_inert_for_draft_factory(self):
        with mock.patch.dict(
            __import__("os").environ,
            {"NULLONE_OPENCODE_MODEL": MUSE_SPARK},
        ):
            profile = router.resolve_provider_profile("draft_factory", env={})
        self.assertEqual((profile.transport, profile.model), ("claude", "sonnet"))
        self.assertEqual(profile.timeout_seconds, 900)

    def test_adapter_dispatches_draft_to_dedicated_provider(self):
        profile = router.resolve_provider_profile("draft_factory", env={})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch(
                "nullone_claude_draft_provider.invoke_draft",
                return_value={"status": "NO_ACTION"},
            ) as invoke_draft, mock.patch.object(
                adapter.opencode_role, "run_opencode_cycle"
            ) as opencode, mock.patch(
                "nullone_claude_editorial_provider.default_invoke_provider"
            ) as morning, mock.patch(
                "nullone_claude_weekly_provider.invoke_weekly"
            ) as weekly, mock.patch(
                "nullone_claude_radar_provider.invoke_radar"
            ) as radar_provider:
                outcome = adapter.invoke_adapter(
                    profile,
                    adapter.AdapterCall(
                        role="draft_factory", prompt="EXACT DRAFT PROMPT", workspace=root
                    ),
                )
            self.assertEqual(invoke_draft.call_count, 1)
            call = invoke_draft.call_args
            self.assertEqual(call.kwargs["prompt"], "EXACT DRAFT PROMPT")
            self.assertEqual(call.kwargs["workspace"], root)
            self.assertEqual(call.kwargs["model"], "sonnet")
            self.assertEqual(call.kwargs["timeout"], 900)
            self.assertEqual(
                (outcome.role, outcome.transport, outcome.model),
                ("draft_factory", "claude", "sonnet"),
            )
            opencode.assert_not_called()
            morning.assert_not_called()
            weekly.assert_not_called()
            radar_provider.assert_not_called()

    def test_no_silent_fallback_after_draft_claude_failure(self):
        profile = router.resolve_provider_profile("draft_factory", env={})
        with tempfile.TemporaryDirectory() as td:
            with mock.patch(
                "nullone_claude_draft_provider.invoke_draft",
                side_effect=BridgeError("boom"),
            ), mock.patch.object(
                adapter.opencode_role, "run_opencode_cycle"
            ) as opencode, mock.patch(
                "nullone_claude_editorial_provider.default_invoke_provider"
            ) as morning:
                with self.assertRaises(BridgeError):
                    adapter.invoke_adapter(
                        profile,
                        adapter.AdapterCall(
                            role="draft_factory", prompt="p",
                            workspace=Path(td),
                        ),
                    )
            opencode.assert_not_called()
            morning.assert_not_called()

    def test_structured_call_carries_exact_model_timeout_and_tools(self):
        profile = router.resolve_provider_profile("draft_factory", env={})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_queue(root, queue_entry())
            captured = {}

            def fake_structured(**kwargs):
                captured.update(kwargs)
                return {"decision": "NO_ACTION", "ranked": [], "notes": "n"}

            with mock.patch.object(
                draft, "run_structured", side_effect=fake_structured
            ):
                draft.invoke_draft(
                    prompt=DRAFT_PROMPT, workspace=root, model="sonnet", timeout=900
                )
            self.assertIn("DRAFT_FIRST", captured["prompt"])
            self.assertTrue(
                captured["prompt"].startswith(DRAFT_PROMPT),
                "provider must execute the Draft Factory prompt, not substitute another",
            )
            self.assertEqual(captured["workspace"], root)
            self.assertEqual(captured["model"], "sonnet")
            # Round timeout derives from the profile's 900s cycle
            # budget via deadline tracking, so it is at most 900.
            self.assertLessEqual(captured["timeout"], 900)
            self.assertGreaterEqual(captured["timeout"], 800)
            self.assertEqual(
                captured["allowed_tools"], ["Read", "WebSearch", "WebFetch", "Glob"]
            )
            self.assertEqual(
                captured["weekly_security_settings"],
                draft.draft_security_settings(root),
            )

    def test_dedicated_provider_never_substitutes_morning_prompt(self):
        source = (SCRIPTS / "nullone_claude_draft_provider.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("morning-editorial.md", source)
        self.assertNotIn("default_invoke_provider", source)
        self.assertNotIn("nullone_claude_editorial_provider", source)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_queue(root, queue_entry())
            seen = {}

            def fake_structured(**kwargs):
                seen.update(kwargs)
                return {"decision": "NO_ACTION", "ranked": [], "notes": "n"}

            with mock.patch.object(draft, "run_structured", side_effect=fake_structured):
                draft.invoke_draft(
                    prompt="SENTINEL DRAFT PROMPT", workspace=root,
                    model="sonnet", timeout=900,
                )
            self.assertTrue(
                seen["prompt"].startswith("SENTINEL DRAFT PROMPT"),
                "provider must pass the caller prompt through verbatim",
            )


class DraftSecurityBoundaryTests(unittest.TestCase):
    def test_ready_queue_authority_before_side_effects(self):
        selected = select_result([ranked_item("post-probe-two")])
        cases = {
            "missing-file": None,
            "unknown": queue_entry("other"),
            "legacy": "- **topic:** Probe topic\n- **status:** READY\n",
            "not-ready": queue_entry(status="DRAFTED"),
            "story-only": queue_entry(status="READY (STORY only)"),
            "story-threshold": queue_entry(status="READY (STORY threshold only)"),
            "duplicate": queue_entry() + "\n" + queue_entry(),
            "two-statuses": queue_entry() + "- **status:** DRAFTED\n",
            "missing-topic": "- **candidate_id:** post-probe-two\n- **status:** READY\n",
            "checklist": "- [ ] post-probe-two | Probe topic | status=READY\n",
        }
        for case, queue in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                if queue is not None:
                    write_queue(root, queue)
                with mock.patch.object(draft, "run_structured", return_value=selected), \
                        mock.patch.object(draft, "_run_helper") as helper:
                    with self.assertRaises(BridgeError):
                        draft.invoke_draft(prompt="p", workspace=root,
                                           model="sonnet", timeout=900)
                self.assertEqual(helper.call_count, 0)
                files = [p for p in root.rglob("*") if p.is_file()]
                self.assertEqual(files, [] if queue is None else [root / draft.QUEUE_PATH])

    def test_model_identity_mismatch_blocks_before_side_effects(self):
        for field, value in (("topic", "Wrong topic"),
                             ("topic_cluster", "wrong"),
                             ("content_type", "EVERGREEN")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                queue = write_queue(root, queue_entry())
                selected = select_result([ranked_item("post-probe-two", **{field: value})])
                with mock.patch.object(draft, "run_structured", return_value=selected), \
                        mock.patch.object(draft, "_run_helper") as helper:
                    with self.assertRaisesRegex(BridgeError, "identity disagrees"):
                        draft.invoke_draft(prompt="p", workspace=root,
                                           model="sonnet", timeout=900)
                self.assertEqual(helper.call_count, 0)
                self.assertEqual([p for p in root.rglob("*") if p.is_file()], [queue])

    def test_production_field_block_mapping_and_legacy_ineligibility(self):
        data = (
            "# NullOne Candidate Queue\n\n"
            "- **topic:** Legacy Topic\n- **status:** READY\n\n"
            "- **discovered_at:** 2026-10-01\n"
            + queue_entry("candidate-a", topic="Topic A", cluster="cluster-a")
            + "\n"
            + "- **discovered_at:** 2026-10-01\n"
            + queue_entry("candidate-b", topic="Topic B", cluster="cluster-b")
        ).encode("utf-8")
        snapshot = draft_queue.parse_queue(data)
        self.assertEqual([(entry.candidate_id, entry.topic) for entry in snapshot.entries],
                         [(None, "Legacy Topic"), ("candidate-a", "Topic A"),
                          ("candidate-b", "Topic B")])
        self.assertEqual(set(snapshot.eligible()), {"candidate-a", "candidate-b"})
        self.assertEqual(snapshot.ready_entry("candidate-a").fields["topic_cluster"],
                         "cluster-a")
        self.assertEqual(snapshot.ready_entry("candidate-b").topic, "Topic B")
        self.assertEqual(snapshot.entries[0].start_line, 2)
        self.assertIsNotNone(snapshot.entries[1].status_line)

    def test_exact_ready_status_and_malformed_blocks(self):
        for status, eligible in (
            ("READY", True), ("READY (STORY only)", False),
            ("READY (STORY threshold only)", False), ("DRAFTED", False),
        ):
            with self.subTest(status=status):
                snapshot = draft_queue.parse_queue(queue_entry(status=status).encode())
                self.assertEqual("post-probe-two" in snapshot.eligible(), eligible)
        for content in (
            queue_entry() + queue_entry(),
            queue_entry() + "- **status:** READY\n",
            "- **candidate_id:** candidate-a\n- **status:** READY\n",
            "- **candidate_id:** candidate-a\n- **status:** READY\n"
            "- **topic:** Topic A\n",
            queue_entry() + "- **topic:** Ambiguous second topic\n",
        ):
            with self.subTest(content=content), self.assertRaises(BridgeError):
                draft_queue.parse_queue(content.encode())
        self.assertEqual(draft_queue.parse_queue(
            b"- [ ] candidate-a | Topic A | status=READY\n"
        ).eligible(), {})

    def test_exact_status_flip_preserves_other_bytes(self):
        original = (
            b"# Queue\r\n\r\n"
            + queue_entry("candidate-a", topic="Topic A").replace("\n", "\r\n").encode()
            + b"\r\n- **topic:** Legacy Topic\r\n- **status:** READY\r\n"
            + queue_entry("candidate-b", topic="Topic B").encode()
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / draft_queue.QUEUE_PATH
            path.parent.mkdir(parents=True)
            path.write_bytes(original)
            baseline = draft_queue.load_queue(root).ready_entry("candidate-a")
            draft_queue.flip_ready_to_drafted(root, "candidate-a", baseline)
            expected = original.replace(b"- **status:** READY\r\n",
                                        b"- **status:** DRAFTED\r\n", 1)
            self.assertEqual(path.read_bytes(), expected)
            self.assertIn(b"- **topic:** Legacy Topic\r\n- **status:** READY",
                          path.read_bytes())

    def test_status_flip_rejects_changed_entry_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_queue(root, queue_entry())
            baseline = draft_queue.load_queue(root).ready_entry("post-probe-two")
            path.write_text(queue_entry(status="DRAFTED"), encoding="utf-8")
            with self.assertRaises(BridgeError):
                draft_queue.flip_ready_to_drafted(root, "post-probe-two", baseline)
            self.assertEqual(path.read_text(encoding="utf-8"),
                             queue_entry(status="DRAFTED"))
            path.unlink()
            outside = root / "outside.md"
            outside.write_text(queue_entry(), encoding="utf-8")
            path.symlink_to(outside)
            with self.assertRaisesRegex(BridgeError, "symlink"):
                draft_queue.load_queue(root)
            with self.assertRaisesRegex(BridgeError, "symlink"):
                draft_queue.flip_ready_to_drafted(root, "post-probe-two", baseline)
            self.assertEqual(outside.read_text(encoding="utf-8"), queue_entry())

    def test_whole_cycle_timeout_bounds(self):
        with mock.patch.object(draft.time, "monotonic", return_value=100.0):
            deadline = 1000.0
            self.assertEqual(draft.remaining_budget(deadline), 870)
            self.assertEqual(draft.bounded_timeout(deadline, minimum=60), 870)
            for name, cap in draft.HELPER_TIMEOUTS.items():
                self.assertEqual(draft.bounded_timeout(deadline, helper_cap=cap), cap,
                                 name)
            self.assertLess(draft.bounded_timeout(deadline), 900)
        with mock.patch.object(draft.time, "monotonic", return_value=960.0):
            with self.assertRaisesRegex(BridgeError, "budget exhausted"):
                draft.bounded_timeout(deadline, minimum=draft.MIN_ROUND_SECONDS)
        with mock.patch.object(draft.time, "monotonic", return_value=975.0):
            with self.assertRaisesRegex(BridgeError, "budget exhausted"):
                draft.bounded_timeout(deadline, helper_cap=120, minimum=1)
        with mock.patch.object(draft.time, "monotonic", return_value=850.0):
            self.assertEqual(draft.bounded_timeout(deadline, helper_cap=120), 120)
        with mock.patch.object(draft.time, "monotonic", return_value=900.0):
            self.assertEqual(draft.bounded_timeout(deadline, helper_cap=120), 70)

    def test_near_deadline_evaluator_is_not_started(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_queue(root, queue_entry())
            ticks = iter((100.0, 100.0, 975.0))
            with mock.patch.object(draft.time, "monotonic", side_effect=ticks), \
                    mock.patch.object(draft, "run_structured",
                                      return_value=select_result([ranked_item("post-probe-two")])), \
                    mock.patch.object(draft, "_run_helper") as helper:
                with self.assertRaisesRegex(BridgeError, "budget exhausted"):
                    draft.invoke_draft(prompt="p", workspace=root,
                                       model="sonnet", timeout=900)
            self.assertEqual(helper.call_count, 0)

    def test_model_tools_are_read_only_exact(self):
        self.assertEqual(
            draft.ALLOWED_TOOLS, ["Read", "WebSearch", "WebFetch", "Glob"]
        )
        self.assertNotIn("queue_content", draft.COMPLETE_FIELDS)
        self.assertNotIn("queue_content", draft.COMPLETE_SCHEMA["properties"])
        for forbidden in ("Bash", "Edit", "Write", "Agent", "mcp__*"):
            self.assertNotIn(forbidden, draft.ALLOWED_TOOLS)

    def test_secret_denies_cover_reviewed_patterns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            denied = draft.draft_security_settings(root)["permissions"]["deny"]
            anchor = "//" + root.resolve().as_posix().lstrip("/")
            for name in (
                ".env",
                ".env.*",
                "**/.env",
                "**/.env.*",
                "*.key",
                "**/*.key",
                "*.pem",
                "**/*.pem",
            ):
                self.assertIn(f"Read({anchor}/{name})", denied)
            self.assertIn(f"Read({anchor}/social/ops/private/**)", denied)
            # Grep is content-bearing with unenforceable denies, so it
            # is dropped from the toolset instead of allowlisted.
            for entry in denied:
                self.assertNotIn("Grep(", entry)

    def test_cli_boundary_exact_and_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            seen = {}

            def fake_run(cmd, **kwargs):
                seen.update(cmd=cmd, kwargs=kwargs)
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=json.dumps({"structured_output": {
                        "decision": "NO_ACTION", "ranked": [], "notes": "n"}}),
                    stderr="",
                )

            with mock.patch.object(claude.subprocess, "run", side_effect=fake_run):
                claude.run_structured(
                    prompt="draft only", allowed_tools=draft.ALLOWED_TOOLS,
                    schema=draft.SELECT_SCHEMA, model="sonnet", timeout=900,
                    workspace=Path(td),
                    weekly_security_settings=draft.draft_security_settings(Path(td)),
                )
            cmd = seen["cmd"]
            self.assertEqual(seen["kwargs"]["cwd"], Path(td))
            self.assertEqual(seen["kwargs"]["timeout"], 900)
            self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
            self.assertEqual(
                cmd[cmd.index("--tools") + 1], "Read,WebSearch,WebFetch,Glob"
            )
            self.assertEqual(
                cmd[cmd.index("--allowedTools") + 1:cmd.index("--restricted")],
                ["Read", "WebSearch", "WebFetch", "Glob"],
            )
            self.assertIn("--no-session-persistence", cmd)
            self.assertIn("--disable-slash-commands", cmd)
            self.assertIn("--permission-mode", cmd)
            self.assertIn("dontAsk", cmd)
            self.assertIn("--restricted", cmd)
            self.assertIn("--safe-mode", cmd)
            self.assertIn("--strict-mcp-config", cmd)
            denied_tools = cmd[cmd.index("--disallowedTools") + 1].split(",")
            for denied in ("mcp__*", "Agent", "Bash", "Edit", "Write"):
                self.assertIn(denied, denied_tools)
            settings = json.loads(cmd[cmd.index("--settings") + 1])
            anchor = "//" + Path(td).resolve().as_posix().lstrip("/")
            denied_reads = settings["permissions"]["deny"]
            self.assertIn(f"Read({anchor}/social/ops/private/**)", denied_reads)
            self.assertIn(f"Read({anchor}/.env)", denied_reads)

    def test_no_publish_or_shell_tokens_in_provider(self):
        import re

        source = (SCRIPTS / "nullone_claude_draft_provider.py").read_text(
            encoding="utf-8"
        )
        code = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        code = re.sub(r"#.*", "", code)
        for token in (
            "nullone-publish-bridge",
            "nullone-publisher-run",
            "final_publish",
            "publishNow",
            "scheduledFor",
            "ZERNIO_API_KEY",
            "mcp__zernio",
            "openclaw message send",
            "curl ",
        ):
            self.assertNotIn(token, code, msg=f"provider must not contain {token!r}")
        # No model tool grant may name a mutating/transport tool
        # anywhere in executable code (docstring rationale excluded).
        for tool in ("Bash", "Edit", "Write", "Agent"):
            self.assertNotIn(f'"{tool}"', code, msg=f"code must not grant {tool}")

    def test_only_five_reviewed_helpers_can_execute(self):
        import re

        source = (SCRIPTS / "nullone_claude_draft_provider.py").read_text(
            encoding="utf-8"
        )
        code = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        invoked = re.findall(r"SCRIPTS_DIR / \"([^\"]+)\"", code)
        self.assertEqual(
            sorted(set(invoked)),
            sorted(
                [
                    "nullone-packaging-evaluator.py",
                    "nullone-packaging-render.py",
                    "nullone-manifest.py",
                    "nullone-draft-bridge.py",
                    "nullone_telegram_review_delivery_adapter.py",
                ]
            ),
        )

    def test_select_mediation_writes_and_exact_evaluator_argv(self):
        skip = ranked_item("skip-probe-one")
        skip["packaging_request"]["candidate"]["verification_status"] = "BLOCKED"
        post = ranked_item("post-probe-two")
        result = select_result([skip, post])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "social/drafts/production").mkdir(parents=True)
            (root / "social/state").mkdir(parents=True)
            write_queue(root, queue_entry("skip-probe-one") + "\n" + queue_entry())
            rounds = [result]

            def fake_structured(**kwargs):
                if not rounds:
                    raise BridgeError("stop-after-select")
                return rounds.pop(0)

            argv_seen: list = []
            with mock.patch.object(
                draft, "run_structured", side_effect=fake_structured
            ), mock.patch.object(
                draft, "_run_helper", side_effect=emulated_run_helper(root, argv_seen)
            ):
                with self.assertRaises(BridgeError, msg="stop-after-select"):
                    draft.invoke_draft(
                        prompt="p", workspace=root, model="sonnet", timeout=900
                    )
            # Only the evaluator ran, with the exact reviewed argv, once
            # per ranked candidate in ranked order.
            self.assertEqual(len(argv_seen), 2)
            for argv, cid in zip(argv_seen, ("skip-probe-one", "post-probe-two")):
                self.assertEqual(argv[0], draft.sys.executable)
                self.assertEqual(
                    argv[1], str(draft.SCRIPTS_DIR / "nullone-packaging-evaluator.py")
                )
                self.assertEqual(argv[2:4], ["evaluate", "--candidate-id"])
                self.assertEqual(argv[4], cid)
                self.assertEqual(argv[5], "--request-file")
                self.assertTrue(argv[6].endswith(f"{cid}-packaging-request.json"))
            # The SKIP then POST path was exercised through the REAL
            # evaluator + fallback ledger: both request files exist and
            # the ledger records the skip plus the single acceptance.
            today = draft._today(root)
            for cid in ("skip-probe-one", "post-probe-two"):
                request = root / f"social/drafts/production/{today}-{cid}-packaging-request.json"
                asset = root / f"social/drafts/production/{today}-{cid}-packaging-asset.json"
                self.assertTrue(request.is_file(), request)
                self.assertTrue(asset.is_file(), asset)
            ledger_files = list(
                (root / "social/drafts/production").glob("*-draft-fallback-ledger.json")
            )
            self.assertEqual(len(ledger_files), 1)
            ledger = json.loads(ledger_files[0].read_text(encoding="utf-8"))
            self.assertEqual(ledger["accepted_candidate_id"], "post-probe-two")
            skips = [a for a in ledger["attempts"] if a["candidate_id"] == "skip-probe-one"]
            self.assertEqual(len(skips), 1)

    def _exercise_full_cycle(self, *, delivery_failure=False, exhaust_before=None,
                             change_before_bridge=False, bridge_failure=False,
                             change_after_bridge=False, complete_failure=False):
        """SELECT→PRODUCE→COMPLETE with deterministic Python mediation.

        The evaluator runs for real (request validation + policy +
        hash-verified receipt); render output is a real PNG with a
        real render record; manifest/bridge/deliver are emulated at
        the helper seam (their internals are covered by their own
        suites; the credentialed bridge can never run offline). The
        test proves exact argv for all five helpers, deterministic
        destinations, real preview-payload validation, the
        constrained queue flip, ledger append, and report write.
        """
        from nullone_bridge_common import atomic_write_json
        from nullone_packaging_receipt import (
            build_render_record,
            canonical_receipt_path,
            canonical_render_record_path,
        )

        post = ranked_item("post-probe-two")
        rounds = [
            select_result([post]),
            {
                "caption": "Probe caption #NullOne",
                "render_text": {"headline": "Probe headline"},
                "slides": None,
            },
        ]
        queue_text = (
            "# Probe queue\n\n"
            + queue_entry()
            + "\n"
            + queue_entry("other-candidate", topic="Other", cluster="other")
        )
        rounds.append(
            {
                "ledger_record": {
                    "candidate_id": "post-probe-two",
                    "review_post_id": "review-probe-1",
                    "state": "DRAFT_CREATED",
                },
                "report_markdown": "# Probe draft report\n\n- candidate post-probe-two\n",
            }
        )
        argv_seen: list = []
        now = [100.0]

        def fake_run_structured(**kwargs):
            if len(rounds) == 1 and complete_failure:
                raise BridgeError("Claude COMPLETE failed")
            if len(rounds) == 1:
                self.assertEqual(
                    (root / draft.QUEUE_PATH).read_text(encoding="utf-8"),
                    queue_text.replace("- **status:** READY", "- **status:** DRAFTED", 1),
                )
            return rounds.pop(0)

        def fake_helper(argv, *, workspace_root, timeout, marker):
            recorded = list(argv)
            argv_seen.append(recorded)
            name = argv[1]
            helper_names = {
                "nullone-packaging-evaluator.py": "evaluate",
                "nullone-packaging-render.py": "render",
                "nullone-manifest.py": "manifest",
                "nullone-draft-bridge.py": "bridge",
                "nullone_telegram_review_delivery_adapter.py": "deliver",
            }
            helper_name = helper_names[Path(name).name]
            self.assertLessEqual(timeout, draft.HELPER_TIMEOUTS[helper_name])
            self.assertLessEqual(timeout, 900 - draft.CYCLE_SAFETY_MARGIN_SECONDS)
            self.assertEqual(timeout, draft.HELPER_TIMEOUTS[helper_name])
            if name.endswith("nullone-packaging-evaluator.py"):
                cid = argv[argv.index("--candidate-id") + 1]
                request = evaluator_cli.load_validated_request(
                    Path(argv[argv.index("--request-file") + 1]), root=root
                )
                from nullone_packaging_receipt import evaluate_request

                receipt = evaluate_request(cid, request)
                out = canonical_receipt_path(cid, root=root)
                out.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(out, receipt)
                assert marker in f"RECEIPT_PATH={out}"
                return f"RECEIPT_PATH={out}\n"
            if name.endswith("nullone-packaging-render.py"):
                from PIL import Image

                out = Path(argv[argv.index("--output") + 1])
                Image.new("RGB", (1080, 1350), (21, 22, 23)).save(out, "PNG")
                cid = "post-probe-two"
                receipt = json.loads(
                    canonical_receipt_path(cid, root=root).read_text(
                        encoding="utf-8"
                    )
                )
                asset = json.loads(
                    (root / "social/drafts/production" /
                     f"{draft._today(root)}-{cid}-packaging-asset.json").read_text(
                        encoding="utf-8"
                    )
                )
                record = build_render_record(
                    candidate_id=cid,
                    receipt_hash=receipt["receipt_hash"],
                    format_decision=receipt["FORMAT_DECISION"],
                    asset_kind=asset.get("asset_kind", "NONE"),
                    outputs=[
                        {
                            "path": str(out.relative_to(root)),
                            "sha256": __import__("hashlib").sha256(
                                out.read_bytes()
                            ).hexdigest(),
                        }
                    ],
                )
                record_path = canonical_render_record_path(cid, root=root)
                atomic_write_json(record_path, record)
                assert marker in "RENDER_FORMAT=SINGLE_POST"
                return "RENDER_FORMAT=SINGLE_POST\n"
            if name.endswith("nullone-manifest.py"):
                manifest_id = argv[argv.index("--manifest-id") + 1]
                manifest_path = (
                    root / "social/ops/manifests" / f"{manifest_id}.json"
                )
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(
                    manifest_path, {"manifest_id": manifest_id, "emulated": True}
                )
                if exhaust_before == "bridge":
                    now[0] = 960.0
                if change_before_bridge:
                    queue_path = root / draft.QUEUE_PATH
                    queue_path.write_text(
                        queue_path.read_text(encoding="utf-8").replace(
                            "- **status:** READY", "- **status:** DRAFTED", 1
                        ), encoding="utf-8"
                    )
                assert marker in "MANIFEST_CREATED=x"
                return "MANIFEST_CREATED=x\n"
            if name.endswith("nullone-draft-bridge.py"):
                if bridge_failure:
                    raise BridgeError("Draft bridge failed")
                if exhaust_before == "deliver":
                    now[0] = 960.0
                if change_after_bridge:
                    queue_path = root / draft.QUEUE_PATH
                    queue_path.write_text(
                        queue_path.read_text(encoding="utf-8").replace(
                            "- **status:** READY", "- **status:** DRAFTED", 1
                        ), encoding="utf-8"
                    )
                assert marker in "DRAFT_BRIDGE=PASS"
                return (
                    "DRAFT_BRIDGE=PASS\nMANIFEST=m\n"
                    "REVIEW_POST_ID=review-probe-1\nREVIEW_STATE=DRAFT_CREATED\n"
                )
            if name.endswith("nullone_telegram_review_delivery_adapter.py"):
                self.assertEqual(
                    (root / draft.QUEUE_PATH).read_text(encoding="utf-8"),
                    queue_text.replace("- **status:** READY", "- **status:** DRAFTED", 1),
                )
                if delivery_failure:
                    raise BridgeError("ambiguous Telegram timeout")
                assert marker in "DELIVERY_STATUS=SENT"
                return "DELIVERY_STATUS=SENT\n"
            raise AssertionError(f"unexpected helper: {name}")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "social/drafts/production").mkdir(parents=True)
            (root / "social/state").mkdir(parents=True)
            (root / "social/state/candidate-queue.md").write_text(
                queue_text, encoding="utf-8"
            )
            with mock.patch.object(draft.time, "monotonic", side_effect=lambda: now[0]), mock.patch.object(
                draft, "run_structured", side_effect=fake_run_structured
            ), mock.patch.object(
                draft, "_run_helper", side_effect=fake_helper
            ):
                if exhaust_before or change_before_bridge or bridge_failure or change_after_bridge or complete_failure:
                    with self.assertRaises(BridgeError):
                        draft.invoke_draft(
                            prompt="p", workspace=root, model="sonnet", timeout=900
                        )
                    scripts = [Path(a[1]).name for a in argv_seen]
                    if exhaust_before == "bridge" or change_before_bridge:
                        self.assertNotIn("nullone-draft-bridge.py", scripts)
                    if bridge_failure or change_after_bridge or exhaust_before == "deliver":
                        self.assertNotIn("nullone_telegram_review_delivery_adapter.py", scripts)
                    if bridge_failure:
                        self.assertEqual((root / draft.QUEUE_PATH).read_text(encoding="utf-8"), queue_text)
                    if change_after_bridge:
                        self.assertEqual(scripts.count("nullone-draft-bridge.py"), 1)
                    if complete_failure:
                        self.assertEqual(scripts.count("nullone_telegram_review_delivery_adapter.py"), 1)
                    if complete_failure or exhaust_before == "deliver":
                        self.assertEqual((root / draft.QUEUE_PATH).read_text(encoding="utf-8"),
                                         queue_text.replace("- **status:** READY", "- **status:** DRAFTED", 1))
                    return
                summary = draft.invoke_draft(
                    prompt="p", workspace=root, model="sonnet", timeout=900
                )
            self.assertEqual(summary["status"], "DRAFT_CREATED")
            self.assertEqual(summary["candidate_id"], "post-probe-two")
            self.assertEqual(summary["review_post_id"], "review-probe-1")
            self.assertEqual(summary["notify_state"],
                             "NOTIFY_FAILED" if delivery_failure else "SENT")
            # All five reviewed helpers ran with exact script argv.
            scripts = [Path(a[1]).name for a in argv_seen]
            self.assertEqual(
                scripts,
                [
                    "nullone-packaging-evaluator.py",
                    "nullone-packaging-render.py",
                    "nullone-manifest.py",
                    "nullone-draft-bridge.py",
                    "nullone_telegram_review_delivery_adapter.py",
                ],
            )
            self.assertEqual(scripts.count("nullone_telegram_review_delivery_adapter.py"), 1)
            self.assertEqual(scripts.count("nullone-draft-bridge.py"), 1)
            render_argv = argv_seen[1]
            self.assertIn("--receipt", render_argv)
            self.assertIn("--asset-file", render_argv)
            self.assertIn("--output", render_argv)
            manifest_argv = argv_seen[2]
            for flag in (
                "--candidate-id", "--topic", "--topic-cluster",
                "--content-type", "--format", "--caption-file",
                "--manifest-id", "--media", "--packaging-receipt",
                "--render-record",
            ):
                self.assertIn(flag, manifest_argv)
            self.assertNotIn("--force", manifest_argv)
            # Deterministic destinations exist; preview payload passes
            # the REAL preview validator.
            today = draft._today(root)
            base = root / "social/drafts/production"
            for name in (
                f"{today}-post-probe-two-packaging-request.json",
                f"{today}-post-probe-two-packaging-asset.json",
                f"{today}-post-probe-two-caption.txt",
                f"{today}-post-probe-two-feed.png",
                f"{today}-post-probe-two-preview-payload.json",
            ):
                self.assertTrue((base / name).is_file(), name)
            from nullone_review_delivery import validate_preview_payload

            payload = json.loads(
                (base / f"{today}-post-probe-two-preview-payload.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                validate_preview_payload(payload)["review_post_id"], "review-probe-1"
            )
            # Constrained queue flip, ledger append, report write.
            queue = (root / "social/state/candidate-queue.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(queue, queue_text.replace(
                "- **status:** READY", "- **status:** DRAFTED", 1
            ))
            self.assertIn(queue_entry("other-candidate", topic="Other", cluster="other"), queue)
            ledger_lines = (
                (root / "social/state/topic-ledger.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            )
            self.assertEqual(len(ledger_lines), 1)
            self.assertEqual(
                json.loads(ledger_lines[0])["candidate_id"], "post-probe-two"
            )
            report = root / f"social/publisher/{today}-post-probe-two-draft.md"
            self.assertTrue(report.is_file())

    def test_full_cycle_mediation_through_complete(self):
        self._exercise_full_cycle()

    def test_telegram_failure_invokes_delivery_once_and_persists(self):
        self._exercise_full_cycle(delivery_failure=True)

    def test_bridge_not_started_without_safe_budget(self):
        self._exercise_full_cycle(exhaust_before="bridge")

    def test_delivery_not_started_without_safe_budget(self):
        self._exercise_full_cycle(exhaust_before="deliver")

    def test_concurrent_status_change_blocks_bridge(self):
        self._exercise_full_cycle(change_before_bridge=True)

    def test_bridge_failure_leaves_candidate_ready(self):
        self._exercise_full_cycle(bridge_failure=True)

    def test_queue_flip_failure_blocks_telegram(self):
        self._exercise_full_cycle(change_after_bridge=True)

    def test_complete_failure_keeps_candidate_drafted(self):
        self._exercise_full_cycle(complete_failure=True)

    def test_no_action_writes_nothing_and_spawns_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            queue = write_queue(root, queue_entry())
            with mock.patch.object(
                draft, "run_structured",
                return_value={"decision": "NO_ACTION", "ranked": [], "notes": "n"},
            ), mock.patch.object(
                draft.subprocess, "run", side_effect=AssertionError("no subprocess")
            ):
                summary = draft.invoke_draft(
                    prompt="p", workspace=root, model="sonnet", timeout=900
                )
            self.assertEqual(summary["status"], "NO_ACTION")
            leftovers = [p for p in root.rglob("*") if p.is_file()]
            self.assertEqual(leftovers, [queue])

    def test_malformed_rounds_fail_closed(self):
        bad_selects = [
            None,
            {},
            {"decision": "SELECT", "ranked": [], "notes": "n"},
            {"decision": "NOPE", "ranked": [], "notes": "n"},
            {"decision": "SELECT", "ranked": ["x"], "notes": "n"},
        ]
        for bad in bad_selects:
            with self.subTest(bad=bad):
                with self.assertRaises(BridgeError):
                    draft._validated_select(bad)
        with self.assertRaises(BridgeError):
            draft._validated_produce(
                {"caption": " ", "render_text": {}, "slides": None}, carousel=False
            )
        with self.assertRaises(BridgeError):
            draft._validated_produce(
                {"caption": "c", "render_text": {}, "slides": {"slides": []}},
                carousel=False,
            )
        with self.assertRaises(BridgeError):
            draft._validated_complete(
                {"ledger_record": {}, "report_markdown": " "}
            )


class DraftDomainTests(unittest.TestCase):
    def test_wrapper_stays_provider_neutral_with_backstop(self):
        wrapper = load_draft_wrapper()
        self.assertEqual(wrapper.DRAFT_FACTORY_TIMEOUT_SECONDS, 900)
        self.assertEqual(wrapper.PROMPT_PATH.name, "draft-factory.md")
        self.assertIn("DRAFT_FIRST", wrapper.PROMPT_PATH.read_text(encoding="utf-8"))
        with mock.patch.object(
            wrapper.provider_adapter, "invoke_role_cycle"
        ) as cycle, mock.patch.object(
            wrapper, "ensure_pending_bridge", return_value={"status": "NOOP", "attempted": [], "created": {}}
        ):
            cycle.return_value = adapter.AdapterOutcome(
                role="draft_factory", transport="claude", model="sonnet",
                outcome="COMPLETED",
            )
            self.assertEqual(wrapper.execute(), 0)
            cycle.assert_called_once()
        with mock.patch.object(
            wrapper.provider_adapter, "invoke_role_cycle",
            side_effect=BridgeError("boom"),
        ), mock.patch.object(
            wrapper, "ensure_pending_bridge", return_value={"status": "NOOP", "attempted": [], "created": {}}
        ):
            self.assertEqual(wrapper.execute(), 1)

    def test_wrapper_backstop_blocked_still_fails_closed(self):
        wrapper = load_draft_wrapper()
        with mock.patch.object(
            wrapper.provider_adapter, "invoke_role_cycle"
        ) as cycle, mock.patch.object(
            wrapper, "ensure_pending_bridge", return_value={"status": "BLOCKED", "attempted": ["m"], "created": {}}
        ):
            cycle.return_value = adapter.AdapterOutcome(
                role="draft_factory", transport="claude", model="sonnet",
                outcome="COMPLETED",
            )
            self.assertEqual(wrapper.execute(), 1)

    def test_other_routes_do_not_regress(self):
        expected = {
            "breaking_radar": ("claude", "haiku", 600),
            "morning_editorial": ("claude", "sonnet", 600),
            "story_writer": ("claude", "haiku", 300),
            "weekly_strategy": ("claude", "sonnet", 600),
        }
        for role, wanted in expected.items():
            profile = router.resolve_provider_profile(role, env={})
            self.assertEqual(
                (profile.transport, profile.model, profile.timeout_seconds), wanted, role
            )
            self.assertEqual(profile.fallback_policy, "none", role)


if __name__ == "__main__":
    unittest.main(verbosity=2)
