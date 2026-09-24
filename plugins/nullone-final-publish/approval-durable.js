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
const DEFAULT_TIMEOUT_MS = 10000;
const MAX_OUTPUT_LEN = 65536;

/**
 * Deterministic runner path construction (mirrors resolveControllerPath in
 * index.js for the same reason: the daemon/subprocess child runs with
 * cwd = workspace, so a relative path would resolve wrong).
 */
function resolveApprovalRunnerPath(workspace, override) {
  if (typeof workspace !== "string" || workspace.length === 0) {
    throw new Error("workspace unavailable");
  }
  if (override !== undefined && override !== null && override !== "") {
    if (typeof override !== "string" || !path.isAbsolute(override)) {
      throw new Error("approval runner override must be absolute");
    }
    if (path.basename(override) !== RUNNER_BASENAME) {
      throw new Error("approval runner override basename mismatch");
    }
    return override;
  }
  const root = path.resolve(workspace);
  const absolute = path.join(root, ...RUNNER_RELATIVE);
  const relative = path.relative(root, absolute);
  if (
    relative === "" ||
    relative.startsWith("..") ||
    path.isAbsolute(relative)
  ) {
    throw new Error("approval runner path escapes workspace");
  }
  return absolute;
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

module.exports = {
  resolveApprovalRunnerPath,
  runApprovalCallback,
  RUNNER_RELATIVE,
  RUNNER_BASENAME,
  DEFAULT_TIMEOUT_MS,
};
