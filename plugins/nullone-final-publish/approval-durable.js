/**
 * Durable first-stage Telegram approval callback runner (P0: durable
 * approval state).
 *
 * The first-stage (approve/reject/revise/back) decision previously lived
 * only in a per-process in-memory Map (`approval-route.js`'s
 * `createApprovalStore()`), which a Gateway restart or process stall
 * silently forgets -- a real REJECT for POST_ID 6ab46980b85359fa0c9305c8
 * (WeatherNext 3) was received but never left DRAFT_CREATED anywhere
 * durable (traced 2026-09-24).
 *
 * This module spawns the existing NullOne Python bridge
 * (`nullone-approval-callback-run.py`, which wraps
 * `nullone_approval_durable.handle_durable_approval_callback`) fresh per
 * callback: first-stage callbacks are human button clicks, not a hot path,
 * so a short-lived subprocess keeps this layer simple and avoids a second
 * long-lived daemon/protocol next to the existing second-stage publish
 * DaemonLink (which exists specifically to hold a publication secret in
 * memory -- nothing on this path needs that).
 *
 * Dispatch-boundary error classification mirrors the second-stage
 * DaemonLink philosophy: `dispatched:false` means the request never
 * reached the subprocess (nothing happened, safe to say so);
 * `dispatched:true` means the outcome is unknown and MUST NOT be reported
 * as "nothing changed". The Python side guarantees a non-zero exit only
 * ever occurs before any durable write (persistence failures inside
 * `handle_durable_approval_callback` are caught there and returned as a
 * normal exit-0 JSON result), so every failure this module raises after a
 * clean spawn+write+close is safely `dispatched:false` -- only a timeout
 * or an unparseable/interrupted response is `dispatched:true`.
 */

"use strict";

const path = require("node:path");
const { spawn } = require("node:child_process");

const RUNNER_RELATIVE = [
  "social",
  "ops",
  "scripts",
  "nullone-approval-callback-run.py",
];
const RUNNER_BASENAME = "nullone-approval-callback-run.py";
const RECOVERY_MODULE_RELATIVE = [
  "social",
  "ops",
  "scripts",
  "nullone_approval_durable.py",
];
const RECOVERY_MODULE_BASENAME = "nullone_approval_durable.py";
const DEFAULT_TIMEOUT_MS = 10000;
// Recovery is a background sweep over every manifest in the workspace, not
// one bounded human click: generous but still bounded, so a stuck sweep
// can never hang around the Gateway process forever.
const DEFAULT_RECOVERY_TIMEOUT_MS = 30000;
const MAX_OUTPUT_LEN = 65536;

/**
 * Deterministic path construction shared by every script this plugin
 * spawns (mirrors resolveControllerPath in index.js for the same reason:
 * the child runs with cwd = workspace, so a relative path would resolve
 * wrong).
 */
function resolveWorkspaceScriptPath(workspace, override, relativeParts, basename, label) {
  if (typeof workspace !== "string" || workspace.length === 0) {
    throw new Error("workspace unavailable");
  }
  if (override !== undefined && override !== null && override !== "") {
    if (typeof override !== "string" || !path.isAbsolute(override)) {
      throw new Error(`${label} override must be absolute`);
    }
    if (path.basename(override) !== basename) {
      throw new Error(`${label} override basename mismatch`);
    }
    return override;
  }
  const root = path.resolve(workspace);
  const absolute = path.join(root, ...relativeParts);
  const relative = path.relative(root, absolute);
  if (
    relative === "" ||
    relative.startsWith("..") ||
    path.isAbsolute(relative)
  ) {
    throw new Error(`${label} path escapes workspace`);
  }
  return absolute;
}

function resolveApprovalRunnerPath(workspace, override) {
  return resolveWorkspaceScriptPath(
    workspace,
    override,
    RUNNER_RELATIVE,
    RUNNER_BASENAME,
    "approval runner"
  );
}

function resolveRecoveryModulePath(workspace, override) {
  return resolveWorkspaceScriptPath(
    workspace,
    override,
    RECOVERY_MODULE_RELATIVE,
    RECOVERY_MODULE_BASENAME,
    "pending-ledger recovery module"
  );
}

/**
 * Run ONE durable first-stage approval callback via a fresh subprocess.
 * @returns {Promise<object>} the parsed JSON result from
 *   nullone_approval_durable.handle_durable_approval_callback.
 */
function runApprovalCallback(
  { pythonBin, runnerPath, workspace, spawnFn, timeoutMs },
  envelope
) {
  const spawnImpl = spawnFn || spawn;
  const timeout = timeoutMs || DEFAULT_TIMEOUT_MS;
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawnImpl(pythonBin, [runnerPath], {
        cwd: workspace,
        env: { ...process.env, NULLONE_WORKSPACE: workspace },
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (error) {
      error.dispatched = false;
      reject(error);
      return;
    }

    let stdout = "";
    let stderr = "";
    let settled = false;

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try {
        child.kill("SIGKILL");
      } catch {
        // Best-effort only.
      }
      const error = new Error("approval callback timeout");
      // May have reached (and even completed) the subprocess before the
      // timeout fired: truth is UNKNOWN, never "nothing changed".
      error.dispatched = true;
      reject(error);
    }, timeout);

    child.stdout.on("data", (chunk) => {
      if (stdout.length < MAX_OUTPUT_LEN) {
        stdout += chunk.toString("utf8");
      }
    });
    child.stderr.on("data", (chunk) => {
      if (stderr.length < MAX_OUTPUT_LEN) {
        stderr += chunk.toString("utf8");
      }
    });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      error.dispatched = false;
      reject(error);
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code !== 0) {
        // nullone-approval-callback-run.py exits non-zero ONLY before any
        // durable write is attempted (malformed request, or an exception
        // raised while locating/validating the manifest, both of which
        // precede the guarded persist step). Safe to report dispatched:false.
        const error = new Error(
          `approval callback exited ${code}: ${stderr.slice(0, 500)}`
        );
        error.dispatched = false;
        reject(error);
        return;
      }
      let parsed;
      try {
        parsed = JSON.parse(stdout);
      } catch (error) {
        error.dispatched = true;
        reject(error);
        return;
      }
      resolve(parsed);
    });

    try {
      child.stdin.write(JSON.stringify(envelope));
      child.stdin.end();
    } catch (error) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      error.dispatched = false;
      reject(error);
    }
  });
}

/**
 * Automatic recovery sweep (PR #162 review, blocker: "recovery mechanism
 * exists but is never invoked automatically"). Runs
 * `nullone_approval_durable.py recover-pending`, which flushes every
 * manifest's pending audit-ledger outbox marker -- catch-up for a
 * transition that committed durably but whose ledger row lagged behind
 * (see runApprovalCallback's docstring). Never touches approval.stage;
 * see nullone_approval_durable.py's own broad-except guarantee that a
 * failed flush leaves the manifest exactly as it was.
 *
 * Bounded by `timeoutMs` (SIGKILL on expiry) so a stuck sweep can never
 * hang around the Gateway process. Every outcome -- success, failure, or
 * timeout -- resolves rather than rejects: this is a best-effort
 * background catch-up, not a human-facing action, so the caller logs
 * whatever happened and moves on instead of treating a failure as fatal.
 */
function runPendingLedgerRecoverySweep({
  pythonBin,
  modulePath,
  workspace,
  spawnFn,
  timeoutMs,
}) {
  const spawnImpl = spawnFn || spawn;
  const timeout = timeoutMs || DEFAULT_RECOVERY_TIMEOUT_MS;
  return new Promise((resolve) => {
    let child;
    try {
      child = spawnImpl(pythonBin, [modulePath, "recover-pending"], {
        cwd: workspace,
        env: { ...process.env, NULLONE_WORKSPACE: workspace },
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      resolve({ ok: false, error: error && error.message, recoveredCount: 0 });
      return;
    }

    let stdout = "";
    let stderr = "";
    let settled = false;

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try {
        child.kill("SIGKILL");
      } catch {
        // Best-effort only.
      }
      resolve({ ok: false, error: "pending-ledger recovery timeout", recoveredCount: 0 });
    }, timeout);

    child.stdout.on("data", (chunk) => {
      if (stdout.length < MAX_OUTPUT_LEN) stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk) => {
      if (stderr.length < MAX_OUTPUT_LEN) stderr += chunk.toString("utf8");
    });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ ok: false, error: error && error.message, recoveredCount: 0 });
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code !== 0) {
        resolve({
          ok: false,
          error: `recover-pending exited ${code}: ${stderr.slice(0, 500)}`,
          recoveredCount: 0,
        });
        return;
      }
      const match = stdout.match(/RECOVERED_COUNT=(\d+)/);
      resolve({
        ok: true,
        recoveredCount: match ? Number(match[1]) : 0,
      });
    });
  });
}

module.exports = {
  resolveApprovalRunnerPath,
  resolveRecoveryModulePath,
  runApprovalCallback,
  runPendingLedgerRecoverySweep,
  RUNNER_RELATIVE,
  RUNNER_BASENAME,
  RECOVERY_MODULE_RELATIVE,
  RECOVERY_MODULE_BASENAME,
  DEFAULT_TIMEOUT_MS,
  DEFAULT_RECOVERY_TIMEOUT_MS,
};
