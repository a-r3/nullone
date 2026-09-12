#!/usr/bin/env python3
"""OpenCode CLI Story writer adapter (issue #112).

Side-by-side with `HaikuStoryWriter` in `nullone_story_pipeline.py`,
which is preserved unchanged as the fallback/rollback writer.
Selection between the two happens only in
`nullone_story_provider_factory.py` (`NULLONE_STORY_PROVIDER`); the
Story workflow modules never import this adapter directly.

Domain parity with the Claude writer is exact:

- same prompt: the shared `_writer_prompt()` builder from
  `nullone_story_pipeline` is reused verbatim, plus a short
  transport framing line requiring a bare JSON object reply (the
  OpenCode `run` edge has no schema-enforcement flag, so the
  JSON-only instruction is the transport equivalent of the Claude
  `--json-schema` flag; the pipeline's own shape validation is
  unchanged and still owns the contract);
- same capabilities: none -- the checked-in `nullone-story-writer`
  agent (`workspace/.opencode/agents/nullone-story-writer.md`) denies
  every tool, mirroring the Claude writer's empty `allowed_tools`.
  Editorial context arrives inline; all reads, validation, and
  persistence stay in the deterministic pipeline;
- same output post-processing: empty-string values are stripped,
  exactly like `HaikuStoryWriter`;
- same failure taxonomy: transport timeout/startup/exit failures
  raise `BridgeError` subclasses (the pipeline maps any writer
  exception to `WRITER_FAILED`, identical to the Claude writer's
  `BridgeError` path); unparseable stdout raises `BridgeError`
  (identical to the Claude "non-JSON output" path); a parsed dict
  with the wrong shape is RETURNED so the pipeline's own validation
  still reports `WRITER_OUTPUT_INVALID` (identical to the Claude
  parsed-but-invalid path).

Invocation contract, verified against installed OpenCode 1.18.30:

    opencode run <prompt> --agent nullone-story-writer \\
        --model <provider/model> --format json --dir <workspace>

- One-shot isolated session: no `--continue`/`--session` is ever
  passed.
- Explicit model in `provider/model` syntax.
- Machine-readable output requested via `--format json`; the adapter
  parses the response text out of the JSON event stream.
- No `--auto` is ever passed; the agent denies all tools.
- Timeout is enforced externally via the subprocess deadline
  (`STORY_WRITER_TIMEOUT_SECONDS`, matching the previous
  `run_structured` 300s default for this small structured call).
- `cwd` and `--dir` are the exact same workspace path.
- stdout/stderr are captured; failure messages are fixed strings
  plus the exit code only, so no credential material can leak.

Model mapping (transport requires an explicit value):

- logical role: Story writer reasoning model;
- transport: OpenCode;
- provider/model: `NULLONE_OPENCODE_MODEL` when set, else
  `DEFAULT_STORY_MODEL` below. The shared override knob is reused
  deliberately so a model change still needs no workflow rewrite;
  per-role routing belongs to issue #111.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_editorial_runtime import REACHABILITY_PATTERN
from nullone_story_pipeline import _writer_prompt

OPENCODE_STORY_AGENT_NAME = "nullone-story-writer"

OPENCODE_MODEL_ENV_VAR = "NULLONE_OPENCODE_MODEL"

DEFAULT_STORY_MODEL = "opencode/muse-spark-1.3-contributor-free"

# Matches the previous `run_structured` default timeout for this small
# structured writer call (not the 600s Morning research budget).
STORY_WRITER_TIMEOUT_SECONDS = 300

_TRANSPORT_SUFFIX = """

Transport instruction: reply with ONLY the JSON object, no prose,
no fences, no commentary.
"""


class StoryWriterTimeoutError(BridgeError):
    """The whole OpenCode Story writer process exceeded its deadline."""


class StoryWriterUnreachableError(BridgeError):
    """The OpenCode Story writer run failed with a reachability signature."""


def resolve_story_model(raw: str | None = None) -> str:
    """Return the explicit `provider/model` value for the Story writer."""

    candidate = raw if raw is not None else os.environ.get(OPENCODE_MODEL_ENV_VAR, "")
    cleaned = (candidate or "").strip()
    return cleaned if cleaned else DEFAULT_STORY_MODEL


def build_writer_prompt(editorial_context: dict[str, Any]) -> str:
    """Shared writer prompt plus the JSON-only transport framing."""

    return _writer_prompt(editorial_context) + _TRANSPORT_SUFFIX


def build_opencode_command(
    *,
    prompt: str,
    workspace: Path | str,
    model: str | None = None,
) -> list[str]:
    """Build the deterministic `opencode run` argv for one writer call.

    Pure function (no I/O, no subprocess) so offline tests can pin the
    exact argv shape.
    """

    resolved_model = model if model is not None else resolve_story_model()
    workspace_text = str(workspace)
    return [
        "opencode",
        "run",
        prompt,
        "--agent",
        OPENCODE_STORY_AGENT_NAME,
        "--model",
        resolved_model,
        "--format",
        "json",
        "--dir",
        workspace_text,
    ]


def _response_text(stdout: str) -> str:
    """Extract the model's response text from `--format json` output.

    Each line is one raw JSON event; `text` parts carry the response.
    Falls back to the whole stdout so a plain-text response still
    parses instead of failing on framing.
    """

    chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part", {})
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text", "")
            if isinstance(text, str):
                chunks.append(text)
    if chunks:
        return "".join(chunks)
    return stdout


def _parse_spec(response_text: str) -> dict[str, Any]:
    """Parse the writer spec dict, failing closed on malformed output."""

    stripped = response_text.strip()
    try:
        parsed = json.loads(stripped)
    except ValueError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise BridgeError(
                "OpenCode Story writer returned non-JSON output"
            )
        try:
            parsed = json.loads(stripped[start : end + 1])
        except ValueError as e:
            raise BridgeError(
                "OpenCode Story writer returned non-JSON output"
            ) from e
    if not isinstance(parsed, dict):
        raise BridgeError(
            "OpenCode Story writer JSON output is not an object"
        )
    return parsed


class OpenCodeStoryWriter:
    """Real Story writer adapter over the OpenCode CLI.

    Implements the `StoryWriter` protocol
    (`(editorial_context) -> spec dict`); the pipeline validates,
    verifies, and persists -- this adapter only produces the raw spec.
    """

    model = DEFAULT_STORY_MODEL

    def __init__(
        self,
        *,
        workspace: Path | str | None = None,
        timeout: float | int = STORY_WRITER_TIMEOUT_SECONDS,
    ) -> None:
        self._workspace = Path(workspace) if workspace is not None else WORKSPACE
        self._timeout = timeout

    def build_command(self, editorial_context: dict[str, Any]) -> list[str]:
        return build_opencode_command(
            prompt=build_writer_prompt(editorial_context),
            workspace=self._workspace,
        )

    def __call__(self, editorial_context: dict[str, Any]) -> dict[str, Any]:
        prompt = build_writer_prompt(editorial_context)
        cmd: Sequence[str] = build_opencode_command(
            prompt=prompt,
            workspace=self._workspace,
        )

        try:
            cp = subprocess.run(
                cmd,
                cwd=self._workspace,
                text=True,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise StoryWriterTimeoutError(
                "OpenCode Story writer exceeded its execution deadline"
            ) from e
        except FileNotFoundError as e:
            raise BridgeError("opencode binary not found") from e

        if cp.returncode != 0:
            combined = f"{cp.stdout}\n{cp.stderr}"
            if REACHABILITY_PATTERN.search(combined):
                raise StoryWriterUnreachableError(
                    "OpenCode Story writer failed: provider unreachable"
                )
            raise BridgeError(
                f"OpenCode Story writer failed (exit={cp.returncode})"
            )

        parsed = _parse_spec(_response_text(cp.stdout))
        return {k: v for k, v in parsed.items() if v != ""}


def self_test() -> int:
    argv = build_opencode_command(
        prompt="probe",
        workspace=Path("/tmp/nullone-story-self-test"),
        model="opencode/muse-spark-1.3-contributor-free",
    )
    assert argv == [
        "opencode",
        "run",
        "probe",
        "--agent",
        OPENCODE_STORY_AGENT_NAME,
        "--model",
        "opencode/muse-spark-1.3-contributor-free",
        "--format",
        "json",
        "--dir",
        "/tmp/nullone-story-self-test",
    ]
    assert "--auto" not in argv
    assert "--continue" not in argv
    assert "--session" not in argv
    assert resolve_story_model("  ") == DEFAULT_STORY_MODEL

    events = (
        '{"type":"text","part":{"type":"text","text":'
        + json.dumps(json.dumps({"layout": "breaking"}))
        + "}}\n"
        + '{"type":"text","part":{"type":"text","text":'
        + json.dumps(json.dumps({"headline": "Hi"}))
        + "}}\n"
    )
    assert _response_text(events) == '{"layout": "breaking"}{"headline": "Hi"}'
    assert _parse_spec('{"layout": "breaking", "headline": "x"}')["layout"] == "breaking"
    try:
        _parse_spec("not json at all")
    except BridgeError:
        pass
    else:
        raise AssertionError("malformed output did not fail closed")

    print("OPENCODE_STORY_PROVIDER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="NullOne OpenCode Story writer adapter"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
