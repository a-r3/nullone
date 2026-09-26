/**
 * NullOne final-publish plugin entry (OpenClaw 2026.8.2).
 *
 * Registers ONE interactive handler (channel telegram, namespace texbrif) --
 * the SOLE registrant of that namespace (P0, texbrif registration collision
 * fix: the installed host allows exactly one handler per channel+namespace,
 * and this plugin already won that race in every retained production boot).
 * `publish:<POST_ID>` callbacks take the deterministic route: envelope +
 * HMAC over the private daemon pipe, zero LLM involvement.
 * `approve|reject|revise|back:<POST_ID>` callbacks take the deterministic
 * first-stage control route (issue #132): authenticated validation +
 * per-post state transition + bounded reply, zero LLM involvement.
 * `draft:<MANIFEST_ID>` callbacks are delegated verbatim to the
 * nullone-draft-bridge sibling plugin's own handler (see `loadDraftBridge`)
 * so its deterministic action core stays reachable now that it no longer
 * attempts its own (losing) registration.
 * Any other `texbrif:*` callback returns handled:false so existing agent
 * flow is byte-for-byte unchanged.
 *
 * PUBLICATION SECRET (issue #90 fix): the publish credential NEVER comes
 * from inherited environment state. The manifest declares a managed
 * plugin SecretInput
 * (`plugins.entries.nullone-final-publish.config.publishToken`, expected
 * string) whose production value is the protected store SecretRef
 * `{source:"store", provider:"default", id:"ZERNIO_PUBLISH_API_TOKEN"}`.
 * At activation the plugin reads the configured input from
 * `api.pluginConfig.publishToken`: a plain string (host-materialized) is
 * used directly; a `{source:"store",...}` ref object is resolved through
 * the supported SDK resolver (`resolveRequiredConfiguredSecretRefInputString`
 * from `openclaw/plugin-sdk/secret-input-runtime`) -- never by reading a
 * secret store manually, never from the environment. Any other shape
 * (missing, blank, env-sourced) fails closed: no daemon spawn, no
 * publication. The resolved token travels to the controller daemon ONLY
 * inside one bounded HMAC-authenticated startup frame on the private
 * spawn pipe, stays memory-only on both sides, and never enters
 * argv/env/files/logs/receipts.
 *
 * EXACT 2026.8.2 context contract (verified from installed
 * dist/plugin-runtime-k6Y5nYdM.js dispatch + telegram ingress factory):
 * - handlerCtx.callback.data = raw callback data string
 * - handlerCtx.callback.namespace / .payload = first-colon split
 * - handlerCtx.callback.messageId = callbackMessage.message_id (number)
 * - handlerCtx.callback.chatId = String(callbackMessage.chat.id)
 * - handlerCtx.accountId / .conversationId / .senderId / .senderUsername
 * - handlerCtx.auth.isAuthorizedSender (ingress-computed)
 * - handlerCtx.respond.reply({text, buttons?})
 * - return {handled:false} falls through to agent routing; any other
 *   handled result consumes the callback; with no submitText no agent turn
 *   is spawned.
 * - register(api) is the REAL host contract -- exactly one argument.
 *   Installed 2026.8.2 declares `register?: (api: OpenClawPluginApi) =>
 *   void` and the Gateway's native-plugin loader calls it as
 *   `register(guarded.api)` (dist/loader-*.js, runPluginRegisterSync) --
 *   there is no second `ctx` argument on the real host path. A second
 *   parameter below exists ONLY as a test/dev override hook (spawnFn,
 *   controllerPath); production correctness never reads workspace from it.
 * - register(...) is SYNCHRONOUS by host contract ("plugin register must
 *   be synchronous"): async store resolution happens lazily inside
 *   DaemonLink.ensure() before the daemon spawns, never in register().
 * - api.pluginConfig = validated `plugins.entries.<id>.config` (raw
 *   SecretInput: resolved string or SecretRef object).
 *
 * NOT deployed by Git merge. Install/enable/restart happens only at the
 * controlled #37 deployment. See README.md in this directory.
 */

const crypto = require("node:crypto");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { definePluginEntry } = require("openclaw/plugin-sdk/plugin-entry");
const { routeCallback, canonicalStringify } = require("./route");
const {
  routeApprovalCallback,
  approvalReply,
  createApprovalStore,
} = require("./approval-route");
const {
  resolveApprovalRunnerPath,
  resolveRecoveryModulePath,
  runApprovalCallback,
  runPendingLedgerRecoverySweep,
} = require("./approval-durable");

const FRAME_MAGIC = Buffer.from("NP1", "utf8");
const FRAME_VERSION = 1;
const MAX_BODY_LEN = 8192;
const MAC_LEN = 32;
const KEY_LEN = 32;
const HANDSHAKE_TIMEOUT_MS = 15000;
const REQUEST_TIMEOUT_MS = 120000;

const CONTROLLER_RELATIVE = [
  "social",
  "ops",
  "scripts",
  "nullone_final_publish_controller.py",
];
const CONTROLLER_BASENAME = "nullone_final_publish_controller.py";

// Managed SecretInput path for the publication credential (mirrors the
// manifest `configContracts.secretInputs.paths` entry).
const SECRET_CONFIG_PATH =
  "plugins.entries.nullone-final-publish.config.publishToken";
// Publication credentials are short bearer strings; anything larger is
// hostile/malformed. Mirrors the Python pipe bound (MAX_TOKEN_LEN).
const MAX_TOKEN_LEN = 2048;
const STARTUP_SCHEMA = "nullone.publish-startup.v1";

/**
 * Deterministic controller path construction (issue #89 hardening).
 *
 * The daemon child runs with cwd = workspace, so a *relative* controller
 * path would resolve to <workspace>/workspace/... — wrong. The reviewed
 * controller lives INSIDE the served workspace; the absolute path is
 * constructed deterministically with node:path.
 *
 * @param {string} workspace served workspace root (required, non-empty)
 * @param {string|undefined} override explicit test/dev override only;
 *   must be an absolute path to the controller file. Production never sets
 *   it (no ctx.controllerPath injection is documented or supported).
 * @throws on missing workspace or on a resolved path escaping the workspace.
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

const SAFE_TEXT = {
  published: "✅ Nəşr tamamlandı.",
  publishing: "⏳ Nəşr emaldadır.",
  blocked: "⛔ Təhlükəsiz blok. Yeni ikinci təsdiq tələb olunur.",
  abandoned: "⛔ Köhnə təsdiq qüvvədən düşüb. Yeni ikinci təsdiq tələb olunur.",
  failed: "❌ Nəşr uğursuz oldu. Təkrar cəhd edilməyəcək.",
  checkRequired: "❓ Nəşr nəticəsi yoxlama tələb edir. Təkrar cəhd edilməyəcək.",
  readbackFailed:
    "❓ Nəşr oxunuşu uğursuz oldu. Status qeyri-müəyyəndir. Təkrar cəhd edilməyəcək.",
  unknown: "❓ Nəşr statusu qeyri-müəyyəndir. Təkrar cəhd edilməyəcək.",
  rejected: "⛔ Sorğu rədd edildi.",
  unavailable: "⛔ Nəşr xidməti hazır deyil. Heç nə yayımlanmadı.",
  // Post-dispatch truth: once the frame may have reached the daemon,
  // timeout/death/silence proves NOTHING about publication. Never claim
  // "nothing was published" here; never retry; never mint a new
  // authorization. Attempts/receipt authority stays with the controller.
  dispatchUnknown:
    "❓ Nəşr sorğusunun nəticəsi qeyri-müəyyəndir. Avtomatik təkrar cəhd edilməyəcək.",
};

// First-stage (approve/reject/revise/back) safe fallback text, distinct
// from SAFE_TEXT above: this path never touches publication/Zernio, so its
// wording must never borrow "Nəşr" (publication) framing.
const FIRST_STAGE_SAFE_TEXT = {
  unavailable: "⛔ Təsdiq xidməti hazır deyil. Heç nə dəyişmədi.",
  // Dispatch boundary: the request may have reached the durable subprocess
  // (and even completed a manifest write) before failure; never claim
  // "nothing changed" here.
  dispatchUnknown:
    "❓ Sorğunun nəticəsi qeyri-müəyyəndir. Bir daha basma, admin yoxlayacaq.",
};

/**
 * Structured, redacted first-stage callback observability (issue: durable
 * approval state, observability requirement). Never includes tokens,
 * messageId/chatId/senderId, or any other raw Telegram identity -- only
 * business-outcome fields (action, stage, outcome), which is what a P0
 * incident trace actually needs and what the 2026-09-24 investigation
 * found was completely absent from the live path.
 */
function logApprovalEvent(fields) {
  try {
    console.log(
      JSON.stringify({
        subsystem: "approval/callback",
        ts: new Date().toISOString(),
        ...fields,
      })
    );
  } catch {
    // Logging must never break the callback path.
  }
}

// Authoritative publication states the daemon may report (closed enum).
// Anything else (missing, malformed, unknown) maps to unknown — never to
// the success text.
const PUBLICATION_STATES = new Set([
  "PUBLISHED",
  "PUBLISHING",
  "FAILED",
  "CHECK_REQUIRED",
  "READBACK_FAILED",
  "UNKNOWN",
  "BLOCKED",
  "REJECTED",
]);

function buildFrame(envelope, key) {
  const body = Buffer.from(canonicalStringify(envelope), "utf8");
  if (body.length > MAX_BODY_LEN) {
    throw new Error("envelope exceeds frame bound");
  }
  const header = Buffer.alloc(3 + 1 + 4);
  FRAME_MAGIC.copy(header, 0);
  header.writeUInt8(FRAME_VERSION, 3);
  header.writeUInt32BE(body.length, 4);
  const mac = crypto.createHmac("sha256", key).update(header).update(body).digest();
  return Buffer.concat([header, body, mac]);
}

/**
 * Build the ONE authenticated startup credential frame.
 *
 * Same wire security as callback frames (HMAC under the per-boot key K
 * established over the private spawn pipe). The controller verifies the
 * MAC before parsing and refuses READY until a well-formed non-blank
 * credential arrives. The token travels memory-only and is never placed
 * in argv/env/files/logs.
 */
function buildStartupFrame(publishToken, key) {
  if (
    typeof publishToken !== "string" ||
    publishToken.length === 0 ||
    /^\s*$/.test(publishToken)
  ) {
    throw new Error("publish credential unavailable");
  }
  if (publishToken.length > MAX_TOKEN_LEN) {
    throw new Error("publish credential oversize");
  }
  if (!Buffer.isBuffer(key) || key.length !== KEY_LEN) {
    throw new Error("channel key length invalid");
  }
  return buildFrame(
    { schema: STARTUP_SCHEMA, publish_token: publishToken },
    key
  );
}

function isStoreSecretRef(value) {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    value.source === "store" &&
    typeof value.provider === "string" &&
    value.provider.length > 0 &&
    typeof value.id === "string" &&
    value.id.length > 0
  );
}

function loadSecretResolver() {
  // Supported SDK surface only; never read a secret store manually.
  // Lazy so offline/test environments without the SDK subtree still load.
  try {
    const runtime = require("openclaw/plugin-sdk/secret-input-runtime");
    if (
      runtime &&
      typeof runtime.resolveRequiredConfiguredSecretRefInputString ===
        "function"
    ) {
      return runtime.resolveRequiredConfiguredSecretRefInputString;
    }
  } catch {
    // Unavailable outside the Gateway host.
  }
  return null;
}

/**
 * Resolve the configured publish SecretInput to a memory-only string.
 *
 * Accepted (in order):
 * 1. a non-blank string within the length bound (host-materialized);
 * 2. a `{source:"store", provider, id}` ref object, resolved through the
 *    supported SDK resolver against the protected store.
 * Everything else -- missing, blank, oversize, env-sourced or otherwise
 * malformed input -- rejects: publication auth can never be satisfied by
 * a plain inherited environment variable.
 */
async function resolvePublishToken(rawInput, hostConfig) {
  if (typeof rawInput === "string") {
    if (
      rawInput.length > 0 &&
      rawInput.length <= MAX_TOKEN_LEN &&
      !/^\s*$/.test(rawInput)
    ) {
      return rawInput;
    }
    throw new Error("publish credential unavailable");
  }
  if (isStoreSecretRef(rawInput)) {
    const resolveRef = loadSecretResolver();
    if (!resolveRef) {
      throw new Error("publish secret resolver unavailable");
    }
    const resolved = await resolveRef({
      config: hostConfig,
      value: rawInput,
      path: SECRET_CONFIG_PATH,
      env: process.env,
    });
    // Installed OpenClaw 2026.8.2 contract:
    // resolveRequiredConfiguredSecretRefInputString(...) -> Promise<string | undefined>
    const value = typeof resolved === "string" ? resolved : "";
    if (
      value.length > 0 &&
      value.length <= MAX_TOKEN_LEN &&
      !/^\s*$/.test(value)
    ) {
      return value;
    }
    throw new Error("publish credential unavailable");
  }
  throw new Error("publish credential unavailable");
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

class DaemonLink {
  constructor(pythonBin, controllerPath, workspace, spawnFn, opts) {
    this.pythonBin = pythonBin;
    this.controllerPath = controllerPath;
    this.workspace = workspace;
    this.spawnFn = spawnFn || spawn;
    this.requestTimeoutMs =
      (opts && opts.requestTimeoutMs) || REQUEST_TIMEOUT_MS;
    this.handshakeTimeoutMs =
      (opts && opts.handshakeTimeoutMs) || HANDSHAKE_TIMEOUT_MS;
    // Async SecretInput resolver: () => Promise<string>. Injected by
    // register() (production: SDK store resolution of the configured
    // publishToken); offline tests inject fakes. Never the environment.
    this.resolvePublishToken =
      (opts && opts.resolvePublishToken) || null;
    this.publishToken = null;
    this.tokenPromise = null;
    this.child = null;
    this.key = null;
    this.ready = false;
    this.buffer = Buffer.alloc(0);
    this.pending = new Map();
    this.handshake = null;
    this.writeChain = Promise.resolve();
  }

  async ensurePublishToken() {
    if (this.publishToken !== null) {
      return this.publishToken;
    }
    if (!this.tokenPromise) {
      this.tokenPromise = (async () => {
        if (typeof this.resolvePublishToken !== "function") {
          throw new Error("publish credential unavailable");
        }
        const token = await this.resolvePublishToken();
        if (
          typeof token !== "string" ||
          token.length === 0 ||
          token.length > MAX_TOKEN_LEN ||
          /^\s*$/.test(token)
        ) {
          throw new Error("publish credential unavailable");
        }
        this.publishToken = token;
        return token;
      })();
      // A rejection must not poison later callbacks forever: a fresh
      // second-stage message retries resolution (never a retry of a
      // consumed publication attempt -- nothing was sent yet).
      this.tokenPromise.catch(() => {
        this.tokenPromise = null;
      });
    }
    return this.tokenPromise;
  }

  async ensure() {
    if (this.child && this.ready) {
      return;
    }
    const token = await this.ensurePublishToken();
    await this._spawn(token);
  }

  async _spawn(publishToken) {
    this.key = crypto.randomBytes(KEY_LEN);
    this.pending.clear();
    const child = this.spawnFn(
      this.pythonBin,
      [this.controllerPath, "daemon"],
      {
        cwd: this.workspace,
        env: { ...process.env, NULLONE_WORKSPACE: this.workspace },
        stdio: ["pipe", "pipe", "ignore"],
      }
    );
    this.child = child;
    this.buffer = Buffer.alloc(0);
    child.on("exit", () => this._onDeath(new Error("daemon exited")));
    child.on("error", (error) => this._onDeath(error));
    child.stdout.on("data", (chunk) => this._onData(chunk));
    // Private spawn pipe: the per-boot key goes first, exactly KEY_LEN
    // bytes, then ONE bounded authenticated startup credential frame.
    // argv and env carry nothing sensitive: the resolved token travels
    // memory-only inside the HMAC'd frame.
    child.stdin.write(this.key);
    child.stdin.write(buildStartupFrame(publishToken, this.key));
    await this._waitReady();
    this.ready = true;
  }

  _onDeath(error) {
    this.ready = false;
    this.child = null;
    // Fail closed: every outstanding requester gets a terminal rejection.
    // No legacy LLM fallback exists anywhere on this path.
    // Outstanding request entries were all dispatched (entries are created
    // only around the wire write), so death after dispatch reports
    // dispatched:true: the daemon may have executed, truth is UNKNOWN.
    if (this.handshake) {
      const reject = this.handshake.reject;
      this.handshake = null;
      reject(error);
    }
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer);
      if (error && error.dispatched === undefined) {
        error.dispatched = true;
      }
      entry.reject(error);
    }
    this.pending.clear();
    this.buffer = Buffer.alloc(0);
  }

  _onData(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    this._pump();
  }

  _frameLength() {
    if (this.buffer.length < 8) {
      return null;
    }
    if (!this.buffer.subarray(0, 3).equals(FRAME_MAGIC)) {
      throw new Error("bad magic");
    }
    if (this.buffer[3] !== FRAME_VERSION) {
      throw new Error("bad version");
    }
    const len = this.buffer.readUInt32BE(4);
    if (len > MAX_BODY_LEN) {
      throw new Error("oversize");
    }
    return 8 + len + MAC_LEN;
  }

  _deliver(payload) {
    // Handshake reply carries no request_id by design.
    if (payload && payload.t === "ready" && this.handshake) {
      const resolve = this.handshake.resolve;
      const reject = this.handshake.reject;
      this.handshake = null;
      if (payload && payload.schema === "nullone.publish-reply.v1") {
        resolve(payload);
      } else {
        reject(new Error("bad handshake"));
      }
      return;
    }
    const id =
      payload && typeof payload.request_id === "string" ? payload.request_id : null;
    const entry = id !== null ? this.pending.get(id) : undefined;
    if (!entry) {
      // Unknown/stale reply (e.g. a late reply for an expired request):
      // ignored, never mapped to any current callback. Fail-closed by design.
      return;
    }
    this.pending.delete(id);
    clearTimeout(entry.timer);
    entry.resolve(payload);
  }

  _pump() {
    for (;;) {
      let total;
      try {
        total = this._frameLength();
      } catch {
        // Framing violation: drop the connection state fail-closed.
        this._onDeath(new Error("framing violation"));
        return;
      }
      if (total === null || this.buffer.length < total) {
        return;
      }
      const frame = this.buffer.subarray(0, total);
      this.buffer = this.buffer.subarray(total);
      const header = frame.subarray(0, 8);
      const body = frame.subarray(8, total - MAC_LEN);
      const mac = frame.subarray(total - MAC_LEN);
      const expected = crypto.createHmac("sha256", this.key).update(header).update(body).digest();
      if (!crypto.timingSafeEqual(mac, expected)) {
        continue;
      }
      let payload;
      try {
        payload = JSON.parse(body.toString("utf8"));
      } catch {
        continue;
      }
      this._deliver(payload);
    }
  }

  _waitReady() {
    const timeoutMs = this.handshakeTimeoutMs;
    return new Promise((resolvePromise, rejectPromise) => {
      const timer = setTimeout(() => {
        this.handshake = null;
        rejectPromise(new Error("handshake timeout"));
      }, timeoutMs);
      this.handshake = {
        resolve: (payload) => {
          clearTimeout(timer);
          resolvePromise(payload);
        },
        reject: (error) => {
          clearTimeout(timer);
          rejectPromise(error);
        },
      };
    });
  }

  request(envelope) {
    const requestId = envelope.request_id;
    const timeoutMs = this.requestTimeoutMs;
    if (typeof requestId !== "string" || requestId.length === 0) {
      return Promise.reject(new Error("request_id required"));
    }
    // Serialized writes (one frame at a time on the wire) + correlated
    // replies (per-request map). A late reply for an expired request can
    // never resolve another request: expired entries are deleted, and
    // unknown ids are ignored.
    // Dispatch boundary: failures BEFORE the wire write carry
    // dispatched=false (nothing was sent); failures AT/AFTER the write
    // carry dispatched=true (the daemon may have executed — truth UNKNOWN).
    const run = async () => {
      await this.ensure();
      const frame = buildFrame(envelope, this.key);
      const reply = await new Promise((resolvePromise, rejectPromise) => {
        const timer = setTimeout(() => {
          this.pending.delete(requestId);
          const error = new Error("request timeout");
          error.dispatched = true;
          rejectPromise(error);
        }, timeoutMs);
        this.pending.set(requestId, {
          resolve: (payload) => {
            clearTimeout(timer);
            resolvePromise(payload);
          },
          reject: (error) => {
            clearTimeout(timer);
            rejectPromise(error);
          },
          timer,
        });
        try {
          this.child.stdin.write(frame);
        } catch (error) {
          this.pending.delete(requestId);
          clearTimeout(timer);
          // The write was attempted: a partial frame may have reached the
          // daemon, so this is post-dispatch by the safe classification.
          error.dispatched = true;
          rejectPromise(error);
        }
      });
      return reply;
    };
    const queued = this.writeChain.then(run);
    // Keep the write chain alive across failures; callers see their error.
    // A caller-side dispatch flag defaults to false (pre-dispatch) when the
    // failure predates any write attempt.
    const guarded = queued.catch((error) => {
      if (error && error.dispatched === undefined) {
        error.dispatched = false;
      }
      throw error;
    });
    this.writeChain = guarded.catch(() => {});
    return guarded;
  }
}

function outcomeText(reply) {
  // Domain truth FIRST: the user-facing text is determined by the
  // authoritative publication_state re-read from durable manifest truth
  // after execution. The numeric return code is process/control information
  // only and MUST NOT determine "published" by itself.
  const state =
    reply && typeof reply.publication_state === "string"
      ? reply.publication_state
      : null;
  if (state !== null && PUBLICATION_STATES.has(state)) {
    switch (state) {
      case "PUBLISHED":
        return SAFE_TEXT.published;
      case "PUBLISHING":
        return SAFE_TEXT.publishing;
      case "FAILED":
        return SAFE_TEXT.failed;
      case "CHECK_REQUIRED":
        return SAFE_TEXT.checkRequired;
      case "READBACK_FAILED":
        return SAFE_TEXT.readbackFailed;
      case "REJECTED":
        return SAFE_TEXT.rejected;
      case "BLOCKED": {
        const hint = reply && typeof reply.hint === "string" ? reply.hint : "";
        if (hint === "fresh_confirmation_required") {
          return SAFE_TEXT.abandoned;
        }
        return SAFE_TEXT.blocked;
      }
      case "UNKNOWN":
      default:
        return SAFE_TEXT.unknown;
    }
  }
  // Missing/malformed/unknown state: never claim completion.
  return SAFE_TEXT.unknown;
}

/**
 * Adapt a NullOne-internal button payload to the installed Telegram
 * channel's inline-keyboard contract (P0, row.map incident).
 *
 * `approvalReply()` (approval-route.js) and the durable Python
 * controller's `reply_for()` both return `buttons` as a FLAT array of
 * button objects: `[publishButton, backButton]`. The installed host's
 * keyboard builder (`buildInlineKeyboard`, dist
 * telegram-ingress-drain-factory) requires rows-of-rows instead --
 * `rows.map((row) => row.map(...))` -- so a flat array makes it call
 * `.map` on a plain button object and throw "row.map is not a function".
 * Confirmed 3/3 times in production on every `approve` action (the only
 * first-stage reply that ever carries real buttons; reject/revise/back
 * always pass `buttons: null` and never hit this path).
 *
 * This is the one adaptation point both reply sources flow through
 * (`sendReply` below): it changes nothing about callback values, labels,
 * or the approval reply contract -- only the array nesting the host
 * requires. Already-nested input (rows-of-rows) passes through
 * unchanged, so this stays correct if a future reply source starts
 * emitting proper rows itself.
 *
 * @param {unknown} buttons
 * @returns {Array<Array<object>>|null} rows-of-rows, or null for "no buttons"
 */
function toButtonRows(buttons) {
  if (!Array.isArray(buttons) || buttons.length === 0) {
    return null;
  }
  if (buttons.every((row) => Array.isArray(row))) {
    return buttons;
  }
  return [buttons];
}

/**
 * Deterministic first-stage approval handler (P0, issue #132).
 *
 * Consumes `texbrif:approve|reject|revise|back:<POST_ID>` with zero LLM
 * involvement: the same auth + identity gating as the publish route,
 * then a pure per-post state transition from the approval store, then one
 * bounded deterministic reply. Unknown `texbrif:*` still falls through to
 * existing agent behavior; malformed approval-shaped callbacks are
 * swallowed safely. Never touches the daemon link, never authorizes
 * publication, never calls Zernio.
 *
 * @returns {{handled: boolean}} always handled:true except fallthrough.
 */
async function handleApprovalFirstStage(handlerCtx, callback, data, approvalRunner) {
  logApprovalEvent({ event: "callback_received" });
  const approval = routeApprovalCallback(data);
  logApprovalEvent({ event: "callback_parsed", decision: approval.decision });
  if (approval.decision === "fallthrough") {
    return { handled: false };
  }
  if (approval.decision === "consume") {
    return { handled: true };
  }
  const authed =
    handlerCtx && handlerCtx.auth && handlerCtx.auth.isAuthorizedSender === true;
  logApprovalEvent({ event: "auth_result", authorized: authed === true });
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

  async function sendReply(text, buttons) {
    try {
      const rows = toButtonRows(buttons);
      if (rows) {
        await handlerCtx.respond.reply({ text, buttons: rows });
      } else {
        await handlerCtx.respond.reply({ text });
      }
      logApprovalEvent({ event: "callback_ack_result", ok: true });
      logApprovalEvent({ event: "reply_send_result", ok: true });
    } catch (error) {
      // Observable now (previously silently swallowed): a failed send must
      // leave a trace, even though it stays best-effort/non-throwing --
      // whatever the durable subprocess already did or didn't do stands
      // either way, and this function must never throw out of a Telegram
      // interactive handler.
      logApprovalEvent({
        event: "reply_send_result",
        ok: false,
        error: error && error.message,
      });
    }
  }

  if (!approvalRunner) {
    logApprovalEvent({ event: "persistence_result", result: "runner_unavailable" });
    await sendReply(FIRST_STAGE_SAFE_TEXT.unavailable);
    return { handled: true };
  }

  let result;
  try {
    result = await runApprovalCallback(approvalRunner, {
      action: approval.decision,
      review_post_id: approval.postId,
      authorized: true,
      message_id: messageId,
      chat_id: String(chatId),
      account_id: String(accountId),
      sender_id: String(senderId),
    });
  } catch (error) {
    const dispatched = error && error.dispatched === true;
    logApprovalEvent({ event: "persistence_result", result: "error", dispatched });
    // Dispatch boundary (mirrors the second-stage #89 rule): never claim
    // "nothing changed" once the request may have reached the subprocess.
    await sendReply(
      dispatched
        ? FIRST_STAGE_SAFE_TEXT.dispatchUnknown
        : FIRST_STAGE_SAFE_TEXT.unavailable
    );
    return { handled: true };
  }

  logApprovalEvent({
    event: "persistence_result",
    postId: approval.postId,
    action: approval.decision,
    outcome: result && result.outcome,
    from_stage: result && result.from_stage,
    to_stage: result && result.to_stage,
    // "pending" | "flushed" | null (no manifest examined). Distinguishes
    // "decision persisted" from "audit ledger row still catching up" --
    // the human-facing reply below is unaffected either way.
    ledger_sync: result && result.ledger_sync,
  });

  if (result && result.outcome === "REJECTED_UNAUTHORIZED") {
    return { handled: true };
  }
  const reply = (result && result.reply) || approvalReply(approval.decision, approval.postId);
  await sendReply(reply.text, reply.buttons);
  // No submitText: zero LLM involvement after the human click.
  return { handled: true };
}

const DRAFT_BRIDGE_SIBLING_RELATIVE = ["..", "nullone-draft-bridge"];

/**
 * Load nullone-draft-bridge's pure routing/runner exports as a sibling
 * plugin package (P0, texbrif namespace registration collision).
 *
 * The installed host allows exactly ONE registered interactive handler per
 * (channel, namespace) pair. Both plugins used to independently call
 * `api.registerInteractiveHandler({channel:"telegram", namespace:"texbrif"})`,
 * and nullone-draft-bridge always lost that race in production (confirmed:
 * every retained Gateway boot logs `"texbrif" namespace already registered
 * by plugin "nullone-final-publish"` for `plugin=nullone-draft-bridge`),
 * silently stranding `texbrif:draft:<manifestId>` callbacks in generic
 * agent flow instead of the deterministic draft-bridge action core.
 *
 * Resolution: nullone-final-publish is the SOLE registrant of "texbrif"
 * (it already wins the race today, so this is zero behavior change for
 * existing approve/reject/revise/back/publish callbacks) and delegates
 * `texbrif:draft:*` to nullone-draft-bridge's own reviewed, independently
 * tested module, loaded from its sibling plugin directory (both plugins
 * are installed side by side under the same plugins root -- confirmed in
 * production and mirrored by this repo's layout). This never changes the
 * `"texbrif:"` callback_data wire format and never duplicates
 * draft-bridge's routing/spawn logic: it reuses draft-bridge's own
 * `routeCallback` and `buildHandler` verbatim. If the sibling package is
 * ever missing, this returns null and `buildHandler`'s fallthrough branch
 * preserves today's existing (pre-fix) behavior for `texbrif:draft:*` --
 * generic agent flow -- rather than inventing new behavior for a
 * deployment shape that has never existed in production.
 *
 * @returns {object|null} the required draft-bridge module, or null
 */
function loadDraftBridge() {
  try {
    return require(path.join(__dirname, ...DRAFT_BRIDGE_SIBLING_RELATIVE));
  } catch {
    return null;
  }
}

/**
 * Build the interactive handler with an injected daemon link (production
 * constructs the real link; offline tests inject a fake). A null link
 * (failed registration) consumes every publish callback safely with zero
 * daemon contact and zero LLM fallback.
 *
 * `approvalRunner` (production: {pythonBin, runnerPath, workspace, spawnFn}
 * bound to the resolved workspace; offline tests inject a fake spawnFn) is
 * the durable first-stage authority. A null approvalRunner (failed
 * registration) consumes every approve/reject/revise/back callback safely
 * with a fail-closed reply -- never falls back to an in-memory decision.
 *
 * `draftBridge` (production: the required nullone-draft-bridge module;
 * offline tests inject a fake with the same shape) plus `draftRunner`
 * (production: nullone-draft-bridge's own `spawnRunner(...)` output; offline
 * tests inject a fake) delegate `texbrif:draft:*` to draft-bridge's own
 * handler (P0, namespace collision fix). A null draftBridge or draftRunner
 * fails closed exactly as draft-bridge's own registration failure would.
 */
function buildHandler(link, approvalRunner, draftBridge, draftRunner) {
  return async (handlerCtx) => {
    const callback =
      handlerCtx && typeof handlerCtx.callback === "object" && handlerCtx.callback !== null
        ? handlerCtx.callback
        : {};
    const data = typeof callback.data === "string" ? callback.data : "";
    const routed = routeCallback(data);
    if (routed.decision === "fallthrough") {
      if (draftBridge && typeof draftBridge.routeCallback === "function") {
        const draftRouted = draftBridge.routeCallback(data);
        if (draftRouted.decision !== "fallthrough") {
          // texbrif:draft:* (claimed or malformed-shaped): hand off to
          // draft-bridge's own handler verbatim -- byte-identical to what
          // draft-bridge's own registration would have produced had it won
          // the (now-removed) registration race. A null draftRunner (e.g.
          // draft-bridge's own workspace resolution failed) fails closed
          // via draft-bridge's OWN SAFE_TEXT.unavailable -- unchanged from
          // its pre-collision-fix behavior.
          return draftBridge.buildHandler(draftRunner)(handlerCtx);
        }
      }
      // draftBridge itself unavailable (sibling package missing entirely,
      // not merely a resolution failure) preserves today's existing
      // behavior for texbrif:draft:*: falls through to generic agent flow,
      // same as before this fix for any deployment that installs
      // nullone-final-publish without its nullone-draft-bridge sibling.
      // P0 deterministic first-stage: approve/reject/revise/back are
      // consumed here with zero LLM involvement. Unknown texbrif:*
      // subcommands still fall through to existing agent behavior.
      return handleApprovalFirstStage(handlerCtx, callback, data, approvalRunner);
    }
    if (routed.decision === "consume") {
      // Malformed publish-shaped callback: swallow safely, zero side effects.
      return { handled: true };
    }
    if (!link) {
      // Registration failed (workspace/path unavailable): fail closed.
      try {
        await handlerCtx.respond.reply({ text: SAFE_TEXT.unavailable });
      } catch {
        // Best-effort only.
      }
      return { handled: true };
    }
    // Deterministic publish route. Authorization was established by ingress
    // (auth.isAuthorizedSender); re-check it here before anything else.
    const authed =
      handlerCtx && handlerCtx.auth && handlerCtx.auth.isAuthorizedSender === true;
    if (!authed) {
      return { handled: true };
    }
    // EXACT second-stage message identity (installed 2026.8.2 fields).
    // Missing identity fails closed: never fall back to an empty message_id
    // (an empty id would alias unrelated chats into one instance).
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
    const envelope = {
      schema: "nullone.publish-callback.v1",
      post_id: routed.postId,
      account_id: String(accountId),
      chat_id: String(chatId),
      message_id: String(messageId),
      sender_id: String(senderId),
      nonce: crypto.randomBytes(16).toString("hex"),
      // Reply correlation only: random per callback, never authorization,
      // never a raw Telegram identifier.
      request_id: crypto.randomBytes(16).toString("hex"),
    };
    let reply;
    try {
      reply = await link.request(envelope);
    } catch (error) {
      // Dispatch boundary wording (exact #89 pre-merge rule):
      // - pre-dispatch failure (spawn/handshake/frame build): the request
      //   was never sent, so the unavailable wording is truthful;
      // - post-dispatch failure (timeout, daemon death, missing reply,
      //   protocol ambiguity): publication MAY have happened — truthful
      //   UNKNOWN wording, never "nothing was published".
      // Never cancel, never retry, never mint a new authorization here.
      const text =
        error && error.dispatched === true
          ? SAFE_TEXT.dispatchUnknown
          : SAFE_TEXT.unavailable;
      try {
        await handlerCtx.respond.reply({ text });
      } catch {
        // Respond path is best-effort; the callback stays consumed.
      }
      return { handled: true };
    }
    try {
      await handlerCtx.respond.reply({ text: outcomeText(reply) });
    } catch {
      // Best-effort user feedback; outcome truth lives in receipts.
    }
    // No submitText: zero LLM involvement after the human click.
    return { handled: true };
  };
}

// Canonical CommonJS export: the plugin entry object ITSELF (id, name,
// description, configSchema, register) is the top-level module export.
// OpenClaw's loader reads the entry directly off `require(...)` / the
// resolved `import().default` — it never reaches into a nested
// `.default.register`. A prior revision exported the helpers at
// module.exports and buried the real entry at module.exports.default,
// which the production loader never unwraps: register() was never called,
// so api.registerInteractiveHandler({channel:"telegram", namespace:"texbrif"})
// never ran and the live interactive registry stayed empty (Sep 11 live
// callback fallthrough). Test/helper exports are attached as properties on
// the SAME entry object so existing offline tests keep working via
// `require(...).routeCallback` etc.
const entry = definePluginEntry({
  id: "nullone-final-publish",
  name: "NullOne Final Publish Handoff",
  description:
    "Claims texbrif:publish Telegram callbacks and routes them to the deterministic NullOne publication controller.",
  register(api, ctx) {
    // ctx is a TEST/DEV-ONLY override hook (spawnFn, controllerPath). The
    // real Gateway calls register(api) with exactly one argument -- ctx is
    // always undefined on the real host path, so production correctness
    // must never depend on it. In particular, workspace resolution below
    // NEVER reads ctx.workspace or NULLONE_WORKSPACE: the real host never
    // supplies either (confirmed: installed register contract is one
    // argument, and the live Gateway process has no NULLONE_WORKSPACE env),
    // which previously produced workspace="" -> resolveControllerPath("")
    // throwing -> link=null on every production boot (Sep 11 incident).
    const pythonBin = (ctx && ctx.pythonBin) || "python3";
    // Test/dev override only (see resolveControllerPath); production derives
    // the absolute path deterministically from the served workspace.
    const override =
      ctx && ctx.controllerPath ? ctx.controllerPath : undefined;
    // Publication SecretInput as configured on the managed path
    // (api.pluginConfig). Raw shape: host-materialized string or
    // `{source:"store",...}` ref object. Resolution is async and lazy
    // (register must stay synchronous): DaemonLink resolves it before
    // the first spawn and fails closed without it.
    const rawPublishToken =
      api && api.pluginConfig
        ? api.pluginConfig.publishToken
        : undefined;
    const hostConfig = api && api.config ? api.config : undefined;
    let link = null;
    let linkError = null;
    let approvalRunner = null;
    let approvalRunnerError = null;
    let workspace = null;
    try {
      // Supported installed 2026.8.2 API for the served production
      // workspace: api.runtime.agent.resolveAgentWorkspaceDir(cfg, agentId).
      // Kept inside this same fail-closed boundary so an unavailable/invalid
      // runtime resolver still registers the interactive handler with
      // link=null/approvalRunner=null instead of throwing out of register()
      // and crashing Gateway startup.
      workspace = api.runtime.agent.resolveAgentWorkspaceDir(api.config, "main");
    } catch (error) {
      linkError = error;
      approvalRunnerError = error;
    }
    if (workspace !== null) {
      const spawnFn = (ctx && ctx.spawnFn) || undefined;
      try {
        const controllerPath = resolveControllerPath(workspace, override);
        link = new DaemonLink(pythonBin, controllerPath, workspace, spawnFn, {
          resolvePublishToken: () =>
            resolvePublishToken(rawPublishToken, hostConfig),
        });
      } catch (error) {
        // Fail closed at registration: the handler below stays installed but
        // every publish callback is consumed safely with zero daemon contact
        // and zero LLM fallback.
        linkError = error;
      }
      try {
        // Independent from the publish DaemonLink above: a failure
        // resolving one path must never take down the other (they used to
        // share one try/catch only for the publish path; the first-stage
        // durable runner is a separate concern with its own fail-closed
        // boundary).
        const approvalOverride =
          ctx && ctx.approvalRunnerPath ? ctx.approvalRunnerPath : undefined;
        const runnerPath = resolveApprovalRunnerPath(workspace, approvalOverride);
        approvalRunner = {
          pythonBin,
          runnerPath,
          workspace,
          spawnFn,
          timeoutMs: (ctx && ctx.approvalTimeoutMs) || undefined,
        };
      } catch (error) {
        approvalRunnerError = error;
      }
    }

    // texbrif namespace collision fix: nullone-final-publish is the sole
    // registrant below and delegates texbrif:draft:* to nullone-draft-bridge's
    // own module (see loadDraftBridge doc comment). Independent fail-closed
    // boundary, same pattern as link/approvalRunner above -- a problem here
    // never affects publish or approve/reject/revise/back.
    const draftBridge = (ctx && ctx.draftBridge) || loadDraftBridge();
    let draftRunner = null;
    if (draftBridge && workspace !== null) {
      try {
        const draftOverride =
          ctx && ctx.draftControllerPath ? ctx.draftControllerPath : undefined;
        const draftControllerPath = draftBridge.resolveControllerPath(
          workspace,
          draftOverride
        );
        const draftSpawnFn = (ctx && ctx.draftSpawnFn) || undefined;
        draftRunner =
          draftSpawnFn ||
          draftBridge.spawnRunner(pythonBin, draftControllerPath, workspace);
      } catch {
        // Fail closed: draftBridge.buildHandler(null) replies with
        // draft-bridge's own SAFE_TEXT.unavailable and zero side effects.
        draftRunner = null;
      }
    }

    if (approvalRunnerError) {
      // Observable registration failure: previously nothing recorded why
      // the first-stage durable path came up unavailable.
      logApprovalEvent({
        event: "persistence_result",
        result: "runner_registration_failed",
        error: approvalRunnerError.message,
      });
    }

    if (approvalRunner) {
      // Automatic pending-ledger recovery (PR #162 review, blocker: the
      // recovery function existed but nothing ever called it, so a
      // terminal REJECT/REVISE nobody clicks again could leave its audit
      // row pending forever). Fires once per register() call -- i.e. once
      // per Gateway boot AND once per plugin reload, satisfying both
      // "after host reboot" and "after plugin reload" without a new
      // daemon or scheduled job: register() already runs on exactly those
      // two lifecycle events. Deliberately NOT awaited: register() must
      // stay synchronous and startup must never block on this. Every
      // outcome (success, failure, timeout) only ever logs -- a failed
      // sweep leaves pending markers exactly as they were (proven safe in
      // nullone_approval_durable.py) and is retried on the NEXT boot/
      // reload or the next opportunistic per-callback flush; it can never
      // corrupt approval.stage, since the sweep only ever touches
      // approval.pending_ledger_event.
      try {
        const recoveryOverride =
          ctx && ctx.recoveryModulePath ? ctx.recoveryModulePath : undefined;
        const recoveryModulePath = resolveRecoveryModulePath(workspace, recoveryOverride);
        runPendingLedgerRecoverySweep({
          pythonBin,
          modulePath: recoveryModulePath,
          workspace,
          // Deliberately its OWN override hook, never ctx.spawnFn: the
          // recovery sweep is a fundamentally different spawn (different
          // argv, different stdio contract -- pipes stderr, where the
          // publish DaemonLink ignores it) that fires unconditionally and
          // unawaited on every register() call. Sharing ctx.spawnFn would
          // silently feed this call into every existing publish-path test
          // fake (many assert "spawn was never called" via one shared
          // flag/mock built only for the daemon's HMAC handshake shape).
          spawnFn: (ctx && ctx.recoverySpawnFn) || undefined,
          timeoutMs: (ctx && ctx.recoveryTimeoutMs) || undefined,
        }).then((outcome) => {
          logApprovalEvent({
            event: "pending_ledger_recovery",
            ok: outcome.ok,
            recovered_count: outcome.recoveredCount,
            error: outcome.ok ? undefined : outcome.error,
          });
        });
        // No .catch() needed: runPendingLedgerRecoverySweep's promise
        // always resolves (see its own docstring) -- it never rejects.
      } catch (error) {
        // Path resolution itself failed (e.g. workspace escape): log and
        // move on, never throw out of register().
        logApprovalEvent({
          event: "pending_ledger_recovery",
          ok: false,
          error: error && error.message,
          result: "path_resolution_failed",
        });
      }
    }

    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "texbrif",
      handler: buildHandler(link, approvalRunner, draftBridge, draftRunner),
    });
  },
});

Object.assign(entry, {
  routeCallback,
  routeApprovalCallback,
  approvalReply,
  createApprovalStore,
  handleApprovalFirstStage,
  toButtonRows,
  loadDraftBridge,
  DRAFT_BRIDGE_SIBLING_RELATIVE,
  canonicalStringify,
  buildFrame,
  buildStartupFrame,
  buildHandler,
  DaemonLink,
  outcomeText,
  SAFE_TEXT,
  FIRST_STAGE_SAFE_TEXT,
  resolveControllerPath,
  resolveApprovalRunnerPath,
  resolveRecoveryModulePath,
  runApprovalCallback,
  runPendingLedgerRecoverySweep,
  resolvePublishToken,
  SECRET_CONFIG_PATH,
  MAX_TOKEN_LEN,
  STARTUP_SCHEMA,
  CONTROLLER_RELATIVE,
});

module.exports = entry;
