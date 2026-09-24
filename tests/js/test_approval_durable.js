/**
 * Offline tests for the durable first-stage approval subprocess runner
 * (plugins/nullone-final-publish/approval-durable.js).
 *
 * Covers path resolution/containment and the dispatch-boundary error
 * classification: a caller must never be told "nothing changed" once a
 * request may have reached the subprocess. No real Python, no network.
 */
"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { EventEmitter } = require("node:events");

const {
  resolveApprovalRunnerPath,
  runApprovalCallback,
  RUNNER_BASENAME,
} = require("../../plugins/nullone-final-publish/approval-durable");

test("resolveApprovalRunnerPath: exact in-workspace path", () => {
  const p = resolveApprovalRunnerPath("/tmp/ws");
  assert.equal(
    p,
    require("node:path").join(
      "/tmp/ws",
      "social",
      "ops",
      "scripts",
      "nullone-approval-callback-run.py"
    )
  );
});

test("resolveApprovalRunnerPath: empty workspace throws", () => {
  assert.throws(() => resolveApprovalRunnerPath(""), /workspace unavailable/);
});

test("resolveApprovalRunnerPath: relative override rejected", () => {
  assert.throws(
    () => resolveApprovalRunnerPath("/tmp/ws", "relative/path.py"),
    /must be absolute/
  );
});

test("resolveApprovalRunnerPath: wrong basename override rejected", () => {
  assert.throws(
    () => resolveApprovalRunnerPath("/tmp/ws", "/abs/wrong-name.py"),
    /basename mismatch/
  );
});

test("resolveApprovalRunnerPath: valid absolute override accepted", () => {
  const override = `/abs/${RUNNER_BASENAME}`;
  assert.equal(resolveApprovalRunnerPath("/tmp/ws", override), override);
});

class FakeChild extends EventEmitter {
  constructor(behavior) {
    super();
    this.stdin = {
      write: () => true,
      end: () => {
        if (behavior.throwOnEnd) {
          throw behavior.throwOnEnd;
        }
        setImmediate(() => {
          if (behavior.hang) {
            return;
          }
          if (behavior.spawnError) {
            this.emit("error", behavior.spawnError);
            return;
          }
          if (behavior.stderr) {
            this.stderr.emit("data", behavior.stderr);
          }
          if (typeof behavior.stdout === "string") {
            this.stdout.emit("data", behavior.stdout);
          }
          this.emit("close", behavior.exitCode === undefined ? 0 : behavior.exitCode);
        });
      },
    };
    this.stdout = new EventEmitter();
    this.stderr = new EventEmitter();
    this.killed = false;
  }
  kill() {
    this.killed = true;
  }
}

function runnerFor(behavior, opts) {
  return {
    pythonBin: "python3",
    runnerPath: "/tmp/ws/social/ops/scripts/nullone-approval-callback-run.py",
    workspace: "/tmp/ws",
    spawnFn: () => new FakeChild(behavior),
    timeoutMs: (opts && opts.timeoutMs) || 200,
  };
}

test("success: parses the subprocess JSON result", async () => {
  const result = await runApprovalCallback(
    runnerFor({ stdout: JSON.stringify({ outcome: "TRANSITIONED", to_stage: "REJECTED" }) }),
    { action: "reject", review_post_id: "0".repeat(24) }
  );
  assert.equal(result.outcome, "TRANSITIONED");
  assert.equal(result.to_stage, "REJECTED");
});

test("non-zero exit: dispatched=false (never claims durable work happened)", async () => {
  await assert.rejects(
    runApprovalCallback(
      runnerFor({ exitCode: 2, stderr: "malformed" }),
      { action: "reject" }
    ),
    (error) => {
      assert.equal(error.dispatched, false);
      return true;
    }
  );
});

test("spawn error: dispatched=false", async () => {
  await assert.rejects(
    runApprovalCallback(runnerFor({ spawnError: new Error("ENOENT") }), {
      action: "reject",
    }),
    (error) => {
      assert.equal(error.dispatched, false);
      return true;
    }
  );
});

test("malformed JSON on stdout: dispatched=true (UNKNOWN, never nothing-changed)", async () => {
  await assert.rejects(
    runApprovalCallback(runnerFor({ stdout: "not json" }), { action: "reject" }),
    (error) => {
      assert.equal(error.dispatched, true);
      return true;
    }
  );
});

test("timeout: dispatched=true and the child is killed", async () => {
  const behavior = { hang: true };
  const runner = runnerFor(behavior, { timeoutMs: 30 });
  await assert.rejects(
    runApprovalCallback(runner, { action: "reject" }),
    (error) => {
      assert.match(error.message, /timeout/);
      assert.equal(error.dispatched, true);
      return true;
    }
  );
});

test("spawnFn throwing synchronously: dispatched=false", async () => {
  const runner = {
    pythonBin: "python3",
    runnerPath: "/tmp/ws/social/ops/scripts/nullone-approval-callback-run.py",
    workspace: "/tmp/ws",
    spawnFn: () => {
      throw new Error("spawn EMFILE");
    },
    timeoutMs: 200,
  };
  await assert.rejects(runApprovalCallback(runner, { action: "reject" }), (error) => {
    assert.equal(error.dispatched, false);
    return true;
  });
});
