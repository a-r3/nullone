/**
 * NullOne acceptance plugin entry (OpenClaw 2026.8.2).
 *
 * Registers ONE interactive handler (channel telegram, namespace texbrif).
 * `texbrif:accept:<ACCEPTANCE_ID>` callbacks take the deterministic route:
 * sender check via ingress `ctx.auth.isAuthorizedSender`, then a one-shot
 * spawn of the reviewed Python action core
 * (`nullone_acceptance_action.py handle --acceptance-id <id>`) with an argv
 * array (no shell) and the inherited Gateway process environment -- the
 * only runtime that carries the Zernio + Telegram credentials. Every other
 * `texbrif:*` callback returns handled:false so existing approval/reject/
 * revise/back/publish flows are byte-for-byte unchanged.
 *
 * Why a one-shot child and not the final-publish daemon + HMAC pipe: the
 * HMAC pipe exists to deliver the publish SECRET without touching
 * argv/env. The acceptance action carries NO secret -- credentials stay in
 * the inherited Gateway environment and are never placed in argv, files,
 * or logs. The Python action core enforces the exact
 * `{"acceptance_id": ...}` schema, derives the manifest path internally,
 * holds the single-flight lock, and returns only non-secret fields.
 *
 * No model routing: the handler never sets submitText, so no agent turn
 * is spawned on this path.
 *
 * EXACT 2026.8.2 context contract (same host contract as
 * plugins/nullone-final-publish/index.js, verified from installed
 * dist/plugin-runtime dispatch + telegram ingress factory):
 * - handlerCtx.callback.data = raw callback data string
 * - handlerCtx.callback.messageId / .chatId / .accountId / .senderId
 * - handlerCtx.auth.isAuthorizedSender (ingress-computed)
 * - handlerCtx.respond.reply({text})
 * - return {handled:false} falls through to agent routing; any other
 *   handled result consumes the callback; with no submitText no agent turn
 *   is spawned.
 * - register(api) is the REAL host contract -- exactly one argument.
 *   A second parameter below exists ONLY as a test/dev override hook.
 * - register(...) is SYNCHRONOUS by host contract: the child is spawned
 *   lazily per callback, never in register().
 *
 * Authorization boundary: invokers are exactly the already-authorized
 * sender(s) of the existing Telegram channel binding, as computed by
 * Gateway ingress. No new auth layer is invented; the action is
 * unreachable from any other channel, namespace, or remote surface.
 *
 * NOT deployed by Git merge. Install/enable/restart happens only at the
 * controlled #37 deployment. See README.md in this directory.
 */

const path = require("node:path");
const { definePluginEntry } = require("openclaw/plugin-sdk/plugin-entry");
const { routeCallback, NAMESPACE } = require("./route");

const CONTROLLER_RELATIVE = [
  "social",
  "ops",
  "scripts",
  "nullone_acceptance_action.py",
];
const CONTROLLER_BASENAME = "nullone_acceptance_action.py";

// One-shot child bound: acceptance is minutes-long at most (Zernio draft
// + Telegram preview); the Python core additionally enforces its own
// single-flight lock with deterministic stale recovery.
const CHILD_TIMEOUT_MS = 600000;
const CHILD_MAX_BUFFER = 64 * 1024;

const SAFE_TEXT = {
  completed: "✅ Qəbul tsikli tamamlandı. İnsan təsdiqi gözlənilir.",
  blocked: "⛔ Qəbul sorğusu bloklandı. Heç bir xarici çağırış edilmədi.",
  busy: "⏳ Bu qəbul sorğusu artıq icradadır. Təkrar cəhd etməyin.",
  unavailable: "⛔ Qəbul xidməti hazır deyil. Heç nə icra edilmədi.",
};

/**
 * Deterministic controller path construction (same hardening as #89).
 *
 * The child runs with cwd = workspace, so the absolute path is
 * constructed deterministically with node:path and contained in the
 * served workspace.
 *
 * @param {string} workspace served workspace root (required, non-empty)
 * @param {string|undefined} override explicit test/dev override only;
 *   must be an absolute path to the action file.
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
 * A null runner (failed registration) consumes every accept callback
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
      // Malformed accept-shaped callback: swallow safely, zero side effects.
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
    // Deterministic acceptance route. Authorization was established by
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
      reply = await runner(routed.acceptanceId);
    } catch {
      try {
        await handlerCtx.respond.reply({ text: SAFE_TEXT.unavailable });
      } catch {
        // Respond path is best-effort; the callback stays consumed.
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
 * --acceptance-id <id>` where `<id>` already matched the strict
 * `nullone-acceptance-YYYYMMDD-HHMMSS` edge regex and the Python core
 * re-validates everything. Credentials flow via the inherited Gateway
 * process environment only -- never argv, never files, never logs.
 */
function spawnRunner(pythonBin, controllerPath, workspace) {
  return (acceptanceId) =>
    new Promise((resolve, reject) => {
      let child;
      try {
        const { spawn } = require("node:child_process");
        child = spawn(
          pythonBin,
          [controllerPath, "handle", "--acceptance-id", acceptanceId],
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
        const error = new Error("acceptance action timeout");
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
  id: "nullone-acceptance",
  name: "NullOne Acceptance Action",
  description:
    "Claims texbrif:accept callbacks on Telegram and routes them to the deterministic NullOne acceptance action core (#129). All other texbrif:* callbacks fall through to existing flows.",
  register(api, ctx) {
    // ctx is a TEST/DEV-ONLY override hook. The real Gateway calls
    // register(api) with exactly one argument.
    const pythonBin = (ctx && ctx.pythonBin) || "python3";
    const override =
      ctx && ctx.controllerPath ? ctx.controllerPath : undefined;
    const spawnOverride = (ctx && ctx.spawnRunner) || undefined;
    let runner = null;
    try {
      // Supported installed 2026.8.2 API for the served production
      // workspace (same resolution as nullone-final-publish).
      const workspace = api.runtime.agent.resolveAgentWorkspaceDir(
        api.config,
        "main"
      );
      const controllerPath = resolveControllerPath(workspace, override);
      runner =
        spawnOverride || spawnRunner(pythonBin, controllerPath, workspace);
    } catch {
      // Fail closed at registration: the handler below stays installed but
      // every accept callback is consumed safely with zero side effects
      // and zero LLM fallback.
      runner = null;
    }

    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: NAMESPACE,
      handler: buildHandler(runner),
    });
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
