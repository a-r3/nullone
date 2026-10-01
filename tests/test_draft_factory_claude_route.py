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
    def test_model_tools_are_read_only_exact(self):
        self.assertEqual(
            draft.ALLOWED_TOOLS, ["Read", "WebSearch", "WebFetch", "Glob"]
        )
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

    def test_full_cycle_mediation_through_complete(self):
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
            "- [ ] post-probe-two | Probe topic | status=READY | score=90\n"
            "- [ ] other-candidate | Other | status=READY | score=10\n"
        )
        rounds.append(
            {
                "ledger_record": {
                    "candidate_id": "post-probe-two",
                    "review_post_id": "review-probe-1",
                    "state": "DRAFT_CREATED",
                },
                "queue_content": queue_text.replace(
                    "post-probe-two | Probe topic | status=READY",
                    "post-probe-two | Probe topic | status=DRAFTED",
                ),
                "report_markdown": "# Probe draft report\n\n- candidate post-probe-two\n",
            }
        )
        argv_seen: list = []

        def fake_run_structured(**kwargs):
            return rounds.pop(0)

        def fake_helper(argv, *, workspace_root, timeout, marker):
            recorded = list(argv)
            argv_seen.append(recorded)
            name = argv[1]
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
                assert marker in "MANIFEST_CREATED=x"
                return "MANIFEST_CREATED=x\n"
            if name.endswith("nullone-draft-bridge.py"):
                assert marker in "DRAFT_BRIDGE=PASS"
                return (
                    "DRAFT_BRIDGE=PASS\nMANIFEST=m\n"
                    "REVIEW_POST_ID=review-probe-1\nREVIEW_STATE=DRAFT_CREATED\n"
                )
            if name.endswith("nullone_telegram_review_delivery_adapter.py"):
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
            with mock.patch.object(
                draft, "run_structured", side_effect=fake_run_structured
            ), mock.patch.object(
                draft, "_run_helper", side_effect=fake_helper
            ):
                summary = draft.invoke_draft(
                    prompt="p", workspace=root, model="sonnet", timeout=900
                )
            self.assertEqual(summary["status"], "DRAFT_CREATED")
            self.assertEqual(summary["candidate_id"], "post-probe-two")
            self.assertEqual(summary["review_post_id"], "review-probe-1")
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
            self.assertIn("post-probe-two | Probe topic | status=DRAFTED", queue)
            self.assertIn("other-candidate | Other | status=READY", queue)
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

    def test_no_action_writes_nothing_and_spawns_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
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
            self.assertEqual(leftovers, [])

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
                {"ledger_record": {}, "queue_content": " ", "report_markdown": "r"}
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
