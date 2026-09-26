/**
 * NullOne draft-bridge plugin entry (OpenClaw 2026.8.2, issue #142).
 *
 * `draft:<MANIFEST_ID>` callbacks take the deterministic route: fixed-argv
 * one-shot spawn of the reviewed Python action core
 * (`nullone_draft_bridge_action.py handle --manifest-id <id>`), zero LLM
 * involvement. Any other `texbrif:*` callback returns handled:false so
 * existing flows (approval first-stage, publish, acceptance) are
 * byte-for-byte unchanged.
 *
 * REGISTRATION (P0, texbrif namespace collision fix): this plugin does NOT
 * call `api.registerInteractiveHandler` itself. The installed host allows
 * exactly one registered handler per (channel, namespace) pair; this
 * plugin and nullone-final-publish both used to independently register
 * {channel:"telegram", namespace:"texbrif"}, and this plugin always lost
 * that race in production (every retained Gateway boot logged the
 * collision warning), silently stranding every `texbrif:draft:*` callback
 * in generic agent flow. nullone-final-publish is now the sole registrant
 * and delegates `texbrif:draft:*` here via `buildHandler` directly (see
 * its `loadDraftBridge` doc comment), so `register()` below is now an
 * intentional no-op: this module is loaded purely as a library.
 * `routeCallback`, `buildHandler`, `spawnRunner`, `resolveControllerPath`,
 * `outcomeText`, and `SAFE_TEXT` remain the reviewed, independently tested
 * contract that nullone-final-publish (and this file's own offline tests)
 * call directly.
 *
 * CREDENTIALS: the Zernio drafts bearer flows via the inherited Gateway
 * process environment only (issue #61 branch A, the runtime source the
 * reviewed bridge CLI already reads). It never enters argv/env additions,
 * files, or logs: the spawn env is `{...process.env}` unmodified, and the
 * action core returns only stable statuses plus the non-secret remote
 * draft id. No secret is read, resolved, or returned anywhere in this
 * plugin.
 *
 * SCOPE: exactly one remote Zernio draft creation maximum per manifest
 * (single-flight in the action core + bridge at-most-once). No Telegram
 * send, no approval, no second confirmation, no scheduling, no
 * publication. Human approval and second Confirm Publish remain mandatory
 * downstream and are untouched.
 *
 * NOT deployed by Git merge. Install/enable/restart happens only at the
 * controlled #37 deployment. See README.md in this directory.
 */

"use strict";

const path = require("node:path");
const { definePluginEntry } = require("openclaw/plugin-sdk/plugin-entry");
const { routeCallback } = require("./route");

// One-shot child bound: bridge execute is presign + upload + create +
// readback, minutes-long at most; the Python core additionally enforces
// its own single-flight lock with deterministic stale recovery.
const CHILD_TIMEOUT_MS = 600000;
const CHILD_MAX_BUFFER = 64 * 1024;

const CONTROLLER_RELATIVE = [
  "social",
  "ops",
  "scripts",
  "nullone_draft_bridge_action.py",
];
const CONTROLLER_BASENAME = "nullone_draft_bridge_action.py";

const SAFE_TEXT = {
  completed: "✅ Qaralama yaradıldı. İnsan təsdiqi gözlənilir.",
  blocked: "⛔ Qaralama sorğusu bloklandı. Heç bir xarici çağırış edilmədi.",
  busy: "⏳ Bu qaralama sorğusu artıq icradadır. Təkrar cəhd etməyin.",
  unavailable: "⛔ Qaralama xidməti hazır deyil. Heç nə icra edilmədi.",
};

/**
 * Deterministic controller path construction (same hardening as #89).
 */
function resolveControllerPath(workspace, override) {
  if (typeof workspace !== "string" || workspace.length === 0) {
    throw new Error("workspace unavailable");
  }
  if (override !== undefined && override !== null && override !== "") {
    if (typeof override !== "string" || !path.isAbsolute(override)) {
      throw new Error("controller override must be absolute");
    }
    if (path.basename(override) !== CONTROLLER_BASENAME) {
      throw new Error("controller override basename mismatch");
    }
    return override;
  }
  const root = path.resolve(workspace);
  const absolute = path.join(root, ...CONTROLLER_RELATIVE);
  const relative = path.relative(root, absolute);
  if (
    relative === "" ||
    relative.startsWith("..") ||
    path.isAbsolute(relative)
  ) {
    throw new Error("controller path escapes workspace");
  }
  return absolute;
}

function isPositiveMessageId(value) {
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value > 0;
  }
  if (typeof value === "string" && /^[0-9]+$/.test(value)) {
    try {
      return BigInt(value) > 0n;
    } catch {
      return false;
    }
  }
  return false;
}

function isNonEmptyId(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 128;
}

/**
 * Map the action-core stdout (`ACTION_STATUS=` / `REASON_CODE=` lines) to
 * a fixed user reply. Fully fixed texts: caller input is never echoed.
 */
function outcomeText(stdout) {
  const text = typeof stdout === "string" ? stdout : "";
  const statusLine = text.split("\n").find((line) => line.startsWith("ACTION_STATUS="));
  const status = statusLine ? statusLine.slice("ACTION_STATUS=".length).trim() : "";
  const reasonLine = text.split("\n").find((line) => line.startsWith("REASON_CODE="));
  const reason = reasonLine ? reasonLine.slice("REASON_CODE=".length).trim() : "";
  if (status === "COMPLETED") {
    return SAFE_TEXT.completed;
  }
  if (reason === "ACTION_BUSY") {
    return SAFE_TEXT.busy;
  }
  if (status === "BLOCKED") {
    return SAFE_TEXT.blocked;
  }
  return SAFE_TEXT.unavailable;
}

/**
 * Build the interactive handler with an injected runner (production
 * spawns the reviewed Python action core; offline tests inject a fake).
 * A null runner (failed registration) consumes every draft callback
 * safely with zero side effects and zero LLM fallback.
 */
function buildHandler(runner) {
  return async (handlerCtx) => {
    const callback =
      handlerCtx && typeof handlerCtx.callback === "object" && handlerCtx.callback !== null
        ? handlerCtx.callback
        : {};
    const data = typeof callback.data === "string" ? callback.data : "";
    const routed = routeCallback(data);
    if (routed.decision === "fallthrough") {
      return { handled: false };
    }
    if (routed.decision === "consume") {
      // Malformed draft-shaped callback: swallow safely, zero side effects.
      return { handled: true };
    }
    if (!runner) {
      try {
        await handlerCtx.respond.reply({ text: SAFE_TEXT.unavailable });
      } catch {
        // Best-effort only.
      }
      return { handled: true };
    }
    // Deterministic draft route. Authorization was established by
    // ingress (auth.isAuthorizedSender); re-check it here before anything
    // else. This is the existing local control-plane boundary -- no new
    // auth layer is invented.
    const authed =
      handlerCtx && handlerCtx.auth && handlerCtx.auth.isAuthorizedSender === true;
    if (!authed) {
      return { handled: true };
    }
    const messageId = callback.messageId;
    const chatId = callback.chatId;
    const accountId = handlerCtx.accountId;
    const senderId = handlerCtx.senderId;
    if (
      !isPositiveMessageId(messageId) ||
      !isNonEmptyId(typeof chatId === "number" ? String(chatId) : chatId) ||
      !isNonEmptyId(typeof accountId === "number" ? String(accountId) : accountId) ||
      !isNonEmptyId(typeof senderId === "number" ? String(senderId) : senderId)
    ) {
      return { handled: true };
    }
    let reply;
    try {
      reply = await runner(routed.manifestId);
    } catch {
      try {
        await handlerCtx.respond.reply({ text: SAFE_TEXT.unavailable });
      } catch {
        // Best-effort only.
      }
      return { handled: true };
    }
    try {
      await handlerCtx.respond.reply({ text: outcomeText(reply) });
    } catch {
      // Best-effort user feedback; outcome truth lives in receipts/audit.
    }
    // No submitText: zero LLM involvement after the human click.
    return { handled: true };
  };
}

/**
 * Production runner: one-shot spawn of the reviewed action core.
 *
 * Fixed argv array (no shell): `python3 <controller> handle
 * --manifest-id <id>` where `<id>` already matched the strict edge regex
 * and the Python core re-validates everything. Credentials flow via the
 * inherited Gateway process environment only -- never argv, never files,
 * never logs. No `shell: true`, no interpolation, no caller-controlled
 * executable/cwd/env.
 */
function spawnRunner(pythonBin, controllerPath, workspace) {
  return (manifestId) =>
    new Promise((resolve, reject) => {
      let child;
      try {
        const { spawn } = require("node:child_process");
        child = spawn(
          pythonBin,
          [controllerPath, "handle", "--manifest-id", manifestId],
          {
            cwd: workspace,
            env: { ...process.env },
            stdio: ["ignore", "pipe", "ignore"],
          }
        );
      } catch (error) {
        reject(error);
        return;
      }
      let stdout = "";
      const timer = setTimeout(() => {
        try {
          child.kill("SIGKILL");
        } catch {
          // Already exited.
        }
        const error = new Error("draft-bridge action timeout");
        reject(error);
      }, CHILD_TIMEOUT_MS);
      child.stdout.on("data", (chunk) => {
        if (stdout.length < CHILD_MAX_BUFFER) {
          stdout += chunk.toString("utf8").slice(0, CHILD_MAX_BUFFER - stdout.length);
        }
      });
      child.on("error", (error) => {
        clearTimeout(timer);
        reject(error);
      });
      child.on("exit", () => {
        clearTimeout(timer);
        resolve(stdout);
      });
    });
}

// Canonical CommonJS export: the plugin entry object ITSELF is the
// top-level module export (same loader contract as nullone-final-publish:
// the production loader never unwraps a nested `.default.register`).
// Test/helper exports are attached as properties on the SAME entry object.
const entry = definePluginEntry({
  id: "nullone-draft-bridge",
  name: "NullOne Draft Bridge Action",
  description:
    "Claims texbrif:draft callbacks on Telegram and routes them to the deterministic NullOne draft-bridge action core (#142). All other texbrif:* callbacks fall through to existing flows.",
  register() {
    // P0 texbrif namespace collision fix: this plugin no longer calls
    // api.registerInteractiveHandler itself. The installed host allows
    // exactly one registered handler per (channel, namespace) pair;
    // nullone-final-publish is now the sole registrant of "texbrif" and
    // delegates texbrif:draft:* to this module's buildHandler(...) and
    // spawnRunner(...) directly (see its loadDraftBridge doc comment).
    // Registering here too would just recreate the exact collision this
    // fix removes, with whichever plugin loads second silently losing
    // again -- so register() is now intentionally a no-op, and this
    // module is used purely as an imported library of pure functions
    // (routeCallback, buildHandler, spawnRunner, resolveControllerPath,
    // outcomeText, SAFE_TEXT), all still exported below unchanged.
  },
});

Object.assign(entry, {
  routeCallback,
  buildHandler,
  spawnRunner,
  resolveControllerPath,
  outcomeText,
  SAFE_TEXT,
  CONTROLLER_RELATIVE,
});

module.exports = entry;
