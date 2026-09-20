#!/usr/bin/env python3
"""Reachability wiring tests for #142 amendment (PR #143 audit).

Proves the REAL Draft Factory flow reaches the credentialed action
automatically after authoritative manifest creation -- no Telegram
callback, no fabricated input, no agent turn, no model call:

  wrapper execute() (agent cycle stubbed, zero model surface)
  -> ensure_pending_bridge()
  -> handle_request() -> fake bridge execute()
  -> exactly one fake Zernio creation

Plus: replay creates zero additional drafts; STORY/prior-day/consumed
manifests untouched; no Telegram/publication keys anywhere; wrapper
return codes preserved on both cycle outcomes.

Deterministic offline (no network, no Zernio, no Telegram, no model).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
import nullone_draft_bridge_action as action  # noqa: E402

BAKU = ZoneInfo("Asia/Baku")
ACCOUNT = "6a982bbf77555aae01c28f21"
MID = "2026-09-16-wiring-pledge-2026-09-16"


def load_wrapper():
    spec = importlib.util.spec_from_file_location(
        "draft_factory_run_wiring_test", SCRIPTS / "nullone-draft-factory-run.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_png(path: Path) -> None:
    from PIL import Image

    Image.new("RGB", (1080, 1350), (21, 22, 23)).save(path, "PNG")


def write_manifest(root: Path, manifest_id: str, *, created_at: str,
                   fmt: str = "FEED", **review_overrides) -> None:
    caption = root / f"caption-{manifest_id}.txt"
    caption.write_text("NullOne wiring caption #NullOne", encoding="utf-8")
    caption_sha = hashlib.sha256(caption.read_bytes()).hexdigest()
    png = root / f"feed-{manifest_id}.png"
    make_png(png)
    png_sha = hashlib.sha256(png.read_bytes()).hexdigest()
    review = {
        "create_attempts": 0,
        "state": "NOT_CREATED",
        "zernio_draft_id": None,
        "created_at": None,
    }
    review.update(review_overrides)
    manifest = {
        "schema": "nullone.production.v1",
        "manifest_id": manifest_id,
        "created_at": created_at,
        "candidate_id": "wiring-candidate-2026-09-16",
        "topic": "Wiring topic",
        "topic_cluster": "wiring-cluster",
        "content_type": "NEWS",
        "format": fmt,
        "verification": "PASS",
        "account_id": ACCOUNT,
        "caption": {"file": caption.name, "sha256": caption_sha},
        "media": [
            {
                "local_path": png.name,
                "sha256": png_sha,
                "content_type": "image/png",
                "width": 1080,
                "height": 1350,
            }
        ],
        "review": review,
        "approval": {"first_stage": False, "final_publish": False},
        "publication": {"state": "NOT_REQUESTED", "attempts": 0},
    }
    manifest_dir = root / "social" / "ops" / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / f"{manifest_id}.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


class FakeBridge:
    def __init__(self, mode: str = "create"):
        self.mode = mode
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def execute(self, manifest_arg: str) -> int:
        with self.lock:
            self.calls.append(manifest_arg)
        if self.mode != "create":
            return 2
        root = Path(bridge_common.WORKSPACE)
        path = root / manifest_arg
        data = json.loads(path.read_text(encoding="utf-8"))
        data["review"]["create_attempts"] = 1
        data["review"]["state"] = "DRAFT_CREATED"
        data["review"]["zernio_draft_id"] = "6aa965709ec9a893c0ffee99"
        path.write_text(json.dumps(data), encoding="utf-8")
        return 0


def today_iso() -> str:
    return datetime.now(BAKU).isoformat()


class EnsurePendingBridgeTest(unittest.TestCase):
    def test_completes_same_day_pending_exactly_once(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = action._BRIDGE_OVERRIDE
            action._BRIDGE_OVERRIDE = fake
            with mock.patch.object(bridge_common, "WORKSPACE", root):
                try:
                    write_manifest(root, MID, created_at=today_iso())
                    since = datetime.now(BAKU) - timedelta(hours=1)
                    first = action.ensure_pending_bridge(
                        workspace_root=root, since=since
                    )
                    self.assertEqual(first["status"], "COMPLETED")
                    self.assertEqual(
                        first["created"]["draft_id"], "6aa965709ec9a893c0ffee99"
                    )
                    self.assertEqual(len(fake.calls), 1)
                    self.assertNotIn("telegram", json.dumps(first).lower())
                    second = action.ensure_pending_bridge(
                        workspace_root=root, since=since
                    )
                    self.assertEqual(second["status"], "NOOP")
                    self.assertEqual(len(fake.calls), 1)
                finally:
                    action._BRIDGE_OVERRIDE = old

    def test_missing_since_is_contract_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception):
                action.ensure_pending_bridge(workspace_root=Path(tmp))

    def test_prior_day_story_consumed_untouched(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = action._BRIDGE_OVERRIDE
            action._BRIDGE_OVERRIDE = fake
            with mock.patch.object(bridge_common, "WORKSPACE", root):
                try:
                    write_manifest(
                        root, "2026-09-15-old-feed-2026-09-15",
                        created_at="2026-09-15T09:30:54+00:00",
                    )
                    write_manifest(
                        root, "story-today-2026-09-16",
                        created_at=today_iso(), fmt="STORY",
                    )
                    write_manifest(
                        root, "2026-09-16-used-2026-09-16",
                        created_at=today_iso(), create_attempts=1,
                    )
                    since = datetime.now(BAKU) - timedelta(hours=1)
                    summary = action.ensure_pending_bridge(
                        workspace_root=root, since=since
                    )
                    self.assertEqual(summary["status"], "NOOP")
                    self.assertEqual(fake.calls, [])
                    self.assertEqual(summary["attempted"], [])
                finally:
                    action._BRIDGE_OVERRIDE = old

    def test_pre_cycle_pending_excluded_only_cycle_bound_completed(self):
        """Issue B/E: alphabetically-first older manifest never consumed."""
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = action._BRIDGE_OVERRIDE
            action._BRIDGE_OVERRIDE = fake
            with mock.patch.object(bridge_common, "WORKSPACE", root):
                try:
                    since = datetime.now(BAKU) - timedelta(minutes=30)
                    old_created = (since - timedelta(hours=2)).isoformat()
                    new_created = since.isoformat()
                    write_manifest(
                        root, "2026-09-16-aaa-older-2026-09-16",
                        created_at=old_created,
                    )
                    write_manifest(
                        root, "2026-09-16-zzz-current-2026-09-16",
                        created_at=new_created,
                    )
                    summary = action.ensure_pending_bridge(
                        workspace_root=root, since=since
                    )
                    self.assertEqual(summary["status"], "COMPLETED")
                    self.assertEqual(len(fake.calls), 1)
                    self.assertIn("zzz-current", fake.calls[0])
                    self.assertNotIn("aaa-older", fake.calls[0])
                    self.assertEqual(
                        summary["created"]["manifest_id"],
                        "2026-09-16-zzz-current-2026-09-16",
                    )
                    older = json.loads(
                        (
                            root / "social" / "ops" / "manifests"
                            / "2026-09-16-aaa-older-2026-09-16.json"
                        ).read_text(encoding="utf-8")
                    )
                    self.assertEqual(older["review"]["create_attempts"], 0)
                    self.assertEqual(older["review"]["state"], "NOT_CREATED")
                finally:
                    action._BRIDGE_OVERRIDE = old

    def test_missing_manifests_dir_is_clean_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            since = datetime.now(BAKU) - timedelta(hours=1)
            summary = action.ensure_pending_bridge(
                workspace_root=Path(tmp), since=since
            )
            self.assertEqual(summary["status"], "NOOP")
            self.assertEqual(summary["attempted"], [])


class WrapperWiringIntegrationTest(unittest.TestCase):
    """The real wrapper entrypoint reaches the action with zero model calls."""

    @staticmethod
    def fixed_start() -> datetime:
        """Pinned cycle start on the real current Baku date."""
        return datetime.now(BAKU).replace(
            hour=10, minute=40, second=0, microsecond=0
        )

    def run_wrapper(self, tmp: str, fake=None, cycle_effect=None,
                    fixture_created_at: str | None = None,
                    extra_fixtures: list | None = None):
        import nullone_provider_adapter as provider_adapter

        root = Path(tmp)
        wrapper = load_wrapper()
        if fake is None:
            fake = FakeBridge()
        old = action._BRIDGE_OVERRIDE
        action._BRIDGE_OVERRIDE = fake
        start = self.fixed_start()
        cycle_calls: list[str] = []
        if cycle_effect is None:
            def cycle_effect(profile, prompt, workspace):
                cycle_calls.append("cycle")
                return provider_adapter.AdapterOutcome(
                    role=profile.role,
                    transport=profile.transport,
                    model=profile.model,
                    outcome="COMPLETED",
                )
        with (
            mock.patch.object(bridge_common, "WORKSPACE", root),
            mock.patch.object(
                provider_adapter, "invoke_role_cycle", side_effect=cycle_effect
            ),
            mock.patch.object(wrapper, "_utcnow", return_value=start),
        ):
            try:
                write_manifest(
                    root, MID,
                    created_at=fixture_created_at or start.isoformat(),
                )
                for spec in extra_fixtures or []:
                    write_manifest(root, **spec)
                code = wrapper.execute()
            finally:
                action._BRIDGE_OVERRIDE = old
        return code, fake, cycle_calls

    def test_wrapper_execute_creates_exactly_once_no_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, fake, cycle_calls = self.run_wrapper(tmp)
            self.assertEqual(code, 0)
            self.assertEqual(cycle_calls, ["cycle"])
            self.assertEqual(len(fake.calls), 1)
            data = json.loads(
                (
                    Path(tmp) / "social" / "ops" / "manifests" / f"{MID}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(data["review"]["state"], "DRAFT_CREATED")
            self.assertEqual(
                data["review"]["zernio_draft_id"], "6aa965709ec9a893c0ffee99"
            )

    def test_wrapper_second_run_replays_zero_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            import nullone_provider_adapter as provider_adapter

            def completed_cycle(profile, prompt, workspace):
                return provider_adapter.AdapterOutcome(
                    role=profile.role,
                    transport=profile.transport,
                    model=profile.model,
                    outcome="COMPLETED",
                )

            root = Path(tmp)
            wrapper = load_wrapper()
            fake = FakeBridge()
            old = action._BRIDGE_OVERRIDE
            action._BRIDGE_OVERRIDE = fake
            start = self.fixed_start()
            with (
                mock.patch.object(bridge_common, "WORKSPACE", root),
                mock.patch.object(
                    provider_adapter, "invoke_role_cycle", side_effect=completed_cycle
                ),
                mock.patch.object(wrapper, "_utcnow", return_value=start),
            ):
                try:
                    write_manifest(root, MID, created_at=start.isoformat())
                    self.assertEqual(wrapper.execute(), 0)
                    self.assertEqual(len(fake.calls), 1)
                    self.assertEqual(wrapper.execute(), 0)
                    self.assertEqual(len(fake.calls), 1)
                finally:
                    action._BRIDGE_OVERRIDE = old

    def test_wrapper_blocked_cycle_still_backstops_return_code_kept(self):
        from nullone_bridge_common import BridgeError

        with tempfile.TemporaryDirectory() as tmp:
            import nullone_provider_adapter as provider_adapter

            root = Path(tmp)
            wrapper = load_wrapper()
            fake = FakeBridge()
            old = action._BRIDGE_OVERRIDE
            action._BRIDGE_OVERRIDE = fake
            start = self.fixed_start()

            def boom(profile, prompt, workspace):
                raise BridgeError("transport down")

            with (
                mock.patch.object(bridge_common, "WORKSPACE", root),
                mock.patch.object(
                    provider_adapter, "invoke_role_cycle", side_effect=boom
                ),
                mock.patch.object(wrapper, "_utcnow", return_value=start),
            ):
                try:
                    write_manifest(root, MID, created_at=start.isoformat())
                    self.assertEqual(wrapper.execute(), 1)
                    self.assertEqual(len(fake.calls), 1)
                finally:
                    action._BRIDGE_OVERRIDE = old

    def test_wrapper_true_noop_stays_exit_zero(self):
        """Issue A/B: no eligible manifest is a successful exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            code, fake, _ = self.run_wrapper(
                tmp,
                fixture_created_at="2026-09-15T09:30:54+00:00",
            )
            self.assertEqual(code, 0)
            self.assertEqual(fake.calls, [])

    def test_wrapper_blocked_bridge_fails_nonzero_retry_safe(self):
        """Issue A/C: eligible + BLOCKED bridge => exit 1, no duplicate."""
        with tempfile.TemporaryDirectory() as tmp:
            code, fake, _ = self.run_wrapper(tmp, fake=FakeBridge(mode="blocked"))
            self.assertEqual(code, 1)
            self.assertEqual(len(fake.calls), 1)
            data = json.loads(
                (
                    Path(tmp) / "social" / "ops" / "manifests" / f"{MID}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(data["review"]["create_attempts"], 0)
            self.assertEqual(data["review"]["state"], "NOT_CREATED")
            self.assertIsNone(data["review"]["zernio_draft_id"])

    def test_wrapper_unexpected_bridge_error_fails_nonzero(self):
        """Issue A/D: unexpected internal error => exit 1, fail closed."""
        with tempfile.TemporaryDirectory() as tmp:
            import nullone_provider_adapter as provider_adapter

            def completed_cycle(profile, prompt, workspace):
                return provider_adapter.AdapterOutcome(
                    role=profile.role,
                    transport=profile.transport,
                    model=profile.model,
                    outcome="COMPLETED",
                )

            root = Path(tmp)
            wrapper = load_wrapper()
            fake = FakeBridge()
            old = action._BRIDGE_OVERRIDE
            action._BRIDGE_OVERRIDE = fake
            start = self.fixed_start()
            with (
                mock.patch.object(bridge_common, "WORKSPACE", root),
                mock.patch.object(
                    provider_adapter, "invoke_role_cycle", side_effect=completed_cycle
                ),
                mock.patch.object(wrapper, "_utcnow", return_value=start),
                mock.patch.object(
                    action, "handle_request",
                    side_effect=RuntimeError("simulated internal failure"),
                ),
            ):
                try:
                    write_manifest(root, MID, created_at=start.isoformat())
                    self.assertEqual(wrapper.execute(), 1)
                    self.assertEqual(fake.calls, [])
                finally:
                    action._BRIDGE_OVERRIDE = old

    def test_wrapper_binds_current_cycle_not_alphabetical(self):
        """Issue B/E: pre-cycle pending manifest never consumed."""
        with tempfile.TemporaryDirectory() as tmp:
            start = self.fixed_start()
            old_created = (start - timedelta(hours=2)).isoformat()
            code, fake, _ = self.run_wrapper(
                tmp,
                fixture_created_at=start.isoformat(),
                extra_fixtures=[
                    {
                        "manifest_id": "2026-09-16-aaa-older-2026-09-16",
                        "created_at": old_created,
                    }
                ],
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(fake.calls), 1)
            self.assertIn(MID, fake.calls[0])
            older = json.loads(
                (
                    Path(tmp) / "social" / "ops" / "manifests"
                    / "2026-09-16-aaa-older-2026-09-16.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(older["review"]["create_attempts"], 0)

    def test_summary_has_no_telegram_or_publication_keys(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = action._BRIDGE_OVERRIDE
            action._BRIDGE_OVERRIDE = fake
            with mock.patch.object(bridge_common, "WORKSPACE", root):
                try:
                    since = datetime.now(BAKU) - timedelta(hours=1)
                    write_manifest(root, MID, created_at=today_iso())
                    summary = action.ensure_pending_bridge(
                        workspace_root=root, since=since
                    )
                finally:
                    action._BRIDGE_OVERRIDE = old
        blob = json.dumps(summary).lower()
        self.assertNotIn("telegram", blob)
        self.assertNotIn("publish", blob)
        self.assertNotIn("approv", blob)


if __name__ == "__main__":
    unittest.main()
