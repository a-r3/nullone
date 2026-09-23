#!/usr/bin/env python3
"""Deterministic Morning queue/ledger persistence regressions (issue #153).

Proven live evidence (2026-09-23 Claude/Sonnet production-parity
benchmark): the model finished genuine editorial work (valid board +
valid handoff) but stayed in the tool loop until the 600s deadline
because prompt steps told it to mutate queue/ledger state itself and it
reached for denied Bash/sed. The fix moves persistence OUT of the
model: `nullone_morning_persistence.persist_morning_state` derives
queue/ledger updates from the validated handoff, wired into
`run_morning_editorial` after artifact validation.

NO real models. NO network. Temp dirs only.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
PROMPT_PATH = ROOT / "workspace/social/ops/prompts/morning-editorial.md"
AGENT_PATH = ROOT / "workspace/.opencode/agents/nullone-editorial.md"
sys.path.insert(0, str(SCRIPTS))

from nullone_editorial_runtime import run_morning_editorial  # noqa: E402
import nullone_morning_persistence as persistence  # noqa: E402
from nullone_morning_persistence import MorningPersistenceError  # noqa: E402

DATE = "2026-09-08"
BOARD_REL = f"social/research/daily/{DATE}-editorial-board.md"
HANDOFF_REL = f"social/research/daily/{DATE}-editorial-candidates.json"


def _candidate(**overrides):
    base = {
        "candidate_id": "cand-1",
        "rank": 1,
        "topic": "Topic",
        "topic_cluster": "cluster",
        "content_type": "NEWS",
        "angle": "Angle",
        "verification": "PASS",
        "evidence_refs": ["evidence"],
        "source_attribution": "Source",
        "source_urls": ["https://example.com/a"],
        "editorial_status": "READY",
        "story_eligible": True,
    }
    base.update(overrides)
    return base


def _handoff(*candidates):
    return {
        "schema": "nullone.editorial-candidate-handoff.v1",
        "contract_version": "1.0.0",
        "editorial_date": DATE,
        "board_path": BOARD_REL,
        "candidates": list(candidates),
    }


def _write_cycle(artifact_root: Path, doc: dict, board: str = "# Board\n") -> None:
    (artifact_root / BOARD_REL).parent.mkdir(parents=True, exist_ok=True)
    (artifact_root / BOARD_REL).write_text(board, encoding="utf-8")
    (artifact_root / HANDOFF_REL).write_text(json.dumps(doc), encoding="utf-8")


def _run(artifact_root: Path, output_root: Path, cycle) -> dict:
    def invoke_provider() -> None:
        cycle(artifact_root)

    return run_morning_editorial(
        occurrence_id="occ_" + "1" * 24,
        board_date=DATE,
        invoke_provider=invoke_provider,
        artifact_root=artifact_root,
        output_root=output_root,
        sleep=lambda _s: None,
    )


class ValidOutputPersistsTests(unittest.TestCase):
    """A: valid board/handoff -> deterministic queue + ledger persistence,
    terminal success."""

    def test_success_persists_queue_and_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifacts, out = root / "artifacts", root / "run-outcomes"
            result = _run(
                artifacts, out,
                lambda ar: _write_cycle(ar, _handoff(_candidate())),
            )
            self.assertEqual(result["domain_outcome"], "SUCCEEDED", result)
            queue = (artifacts / "social/state/candidate-queue.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(f"## {DATE} morning-editorial scan", queue)
            self.assertIn("- **candidate_id:** cand-1", queue)
            rows = (artifacts / "social/state/topic-ledger.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(rows), 1)
            row = json.loads(rows[0])
            self.assertEqual(row["event"], "MORNING_EDITORIAL_SCAN")
            self.assertEqual(row["candidate_id"], "cand-1")

    def test_empty_handoff_succeeds_without_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifacts, out = root / "artifacts", root / "run-outcomes"
            result = _run(
                artifacts, out, lambda ar: _write_cycle(ar, _handoff())
            )
            self.assertEqual(result["domain_outcome"], "SUCCEEDED", result)
            self.assertFalse((artifacts / "social/state/candidate-queue.md").exists())
            self.assertFalse(
                (artifacts / "social/state/topic-ledger.jsonl").exists()
            )


class InvalidOutputMutatesNothingTests(unittest.TestCase):
    """B: invalid handoff -> no queue/ledger mutation, deterministic failure."""

    def test_malformed_handoff_fails_closed_without_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifacts, out = root / "artifacts", root / "run-outcomes"

            def cycle(ar: Path) -> None:
                doc = _handoff(_candidate())
                doc["candidates"][0]["verification"] = "MAYBE"
                _write_cycle(ar, doc)

            result = _run(artifacts, out, cycle)
            self.assertEqual(result["domain_outcome"], "FAILED", result)
            self.assertEqual(result["reason_code"], "HANDOFF_INVALID", result)
            self.assertFalse((artifacts / "social/state/candidate-queue.md").exists())
            self.assertFalse(
                (artifacts / "social/state/topic-ledger.jsonl").exists()
            )

    def test_persist_raises_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            doc = _handoff(_candidate())
            doc["candidates"][0]["editorial_status"] = "NOPE"
            (root / BOARD_REL).parent.mkdir(parents=True, exist_ok=True)
            (root / BOARD_REL).write_text("# Board\n", encoding="utf-8")
            (root / HANDOFF_REL).write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(MorningPersistenceError):
                persistence.persist_morning_state(
                    workspace_root=root, editorial_date=DATE
                )
            self.assertFalse((root / "social/state/candidate-queue.md").exists())
            self.assertFalse((root / "social/state/topic-ledger.jsonl").exists())


class IdempotencyTests(unittest.TestCase):
    """C: same occurrence/result processed twice -> no duplicate entries."""

    def test_replay_adds_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifacts, out = root / "artifacts", root / "run-outcomes"
            doc = _handoff(
                _candidate(),
                _candidate(
                    candidate_id="cand-2", rank=2,
                    editorial_status="REJECTED", story_eligible=False,
                    verification="PARTIAL",
                ),
            )
            first = _run(artifacts, out, lambda ar: _write_cycle(ar, doc))
            self.assertEqual(first["domain_outcome"], "SUCCEEDED", first)
            second = _run(artifacts, out, lambda ar: _write_cycle(ar, doc))
            self.assertEqual(second["domain_outcome"], "SUCCEEDED", second)
            queue = (artifacts / "social/state/candidate-queue.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(queue.count(f"## {DATE} morning-editorial scan"), 1)
            self.assertEqual(queue.count("- **candidate_id:** cand-1"), 1)
            self.assertNotIn("cand-2", queue)
            rows = (artifacts / "social/state/topic-ledger.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(rows), 2)
            events = sorted(json.loads(r)["event"] for r in rows)
            self.assertEqual(
                events,
                ["MORNING_EDITORIAL_SCAN", "MORNING_EDITORIAL_SCAN_NOT_QUEUED"],
            )


class ModelToolContractTests(unittest.TestCase):
    """D: Morning prompt/agent never requires Bash/sed/shell for state."""

    def test_prompt_forbids_model_queue_ledger_edits(self):
        text = PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn("Do NOT edit social/state/candidate-queue.md", text)
        self.assertIn("Do NOT edit social/state/topic-ledger.jsonl", text)
        self.assertIn("STOP", text)

    def test_prompt_never_instructs_shell_state_edits(self):
        text = PROMPT_PATH.read_text(encoding="utf-8")
        for needle in ("sed -i", "| sed", "$(sed", "`sed",
                       "cp social/state", ">> social/state"):
            self.assertNotIn(
                needle, text, f"prompt must not instruct shell state edits ({needle})"
            )

    def test_agent_denies_bash_and_state_writes(self):
        lines = AGENT_PATH.read_text(encoding="utf-8").splitlines()
        self.assertIn("  bash: deny", lines)
        # Collect the `write:` / `edit:` permission blocks (two-space keys).
        blocks: dict[str, list[str]] = {}
        current: str | None = None
        for line in lines:
            if line in ("  write:", "  edit:"):
                current = line.strip(": ")
                blocks[current] = []
            elif line.startswith("  ") and not line.startswith("    "):
                current = None
            elif current is not None:
                blocks[current].append(line)
        for section in ("write", "edit"):
            body = "\n".join(blocks[section])
            self.assertIn("deny", body)
            self.assertNotIn("candidate-queue", body)
            self.assertNotIn("topic-ledger", body)
            self.assertIn("editorial-board.md", body)
            self.assertIn("editorial-candidates.json", body)

    def test_claude_transport_allowlist_has_no_shell(self):
        from nullone_claude_editorial_provider import build_claude_command

        argv = build_claude_command(prompt="p", model="sonnet")
        self.assertIn("--allowedTools", argv)
        tools = argv[argv.index("--allowedTools") + 1]
        self.assertEqual(tools, "Read,Write,WebSearch,WebFetch")
        self.assertNotIn("Bash", tools)


class PersistenceFailureTests(unittest.TestCase):
    """E: deterministic writer failure -> domain failure, no false success."""

    def test_writer_failure_is_domain_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifacts, out = root / "artifacts", root / "run-outcomes"
            real = persistence.persist_morning_state

            def exploding(**kwargs):
                raise MorningPersistenceError("disk is on fire")

            persistence.persist_morning_state = exploding  # type: ignore[assignment]
            try:
                result = _run(
                    artifacts, out,
                    lambda ar: _write_cycle(ar, _handoff(_candidate())),
                )
            finally:
                persistence.persist_morning_state = real  # type: ignore[assignment]
            self.assertEqual(result["domain_outcome"], "FAILED", result)
            self.assertEqual(
                result["reason_code"], "MORNING_PERSISTENCE_ERROR", result
            )


class NoPublicationSurfaceTests(unittest.TestCase):
    """F: this change creates no Telegram/Zernio/publication behavior."""

    def test_persistence_module_has_no_messaging_surface(self):
        import re

        src = (
            ROOT / "workspace/social/ops/scripts/nullone_morning_persistence.py"
        ).read_text(encoding="utf-8")
        # No messaging/publication call surface: prose mentions in the
        # docstring ("no ... behavior changes") carry no parens, so only
        # call-like references fail this guard.
        calls = re.findall(
            r"(?i)(telegram|zernio|publish|approve|cron)[\w]*\s*\(", src
        )
        self.assertEqual(calls, [], f"persistence module must not touch {calls}")

    def test_prompt_still_forbids_publication(self):
        text = PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn("No publication or Zernio creation.", text)


class NeighborPathsUnchangedTests(unittest.TestCase):
    """G: provider routing/adapter behavior relevant to Morning is unchanged."""

    def test_morning_role_profile_defaults(self):
        import nullone_provider_adapter as adapter
        import nullone_provider_router as router

        profile = router.resolve_provider_profile(
            router.ROLE_MORNING_EDITORIAL, env={}
        )
        self.assertEqual(profile.timeout_seconds, 600)
        self.assertEqual(profile.fallback_policy, "none")
        self.assertEqual(router.role_agent(router.ROLE_MORNING_EDITORIAL),
                         "nullone-editorial")
        call = adapter.AdapterCall(
            role=profile.role, prompt="p", workspace=Path("/tmp")
        )
        self.assertEqual(call.role, "morning_editorial")


if __name__ == "__main__":
    unittest.main(verbosity=2)
