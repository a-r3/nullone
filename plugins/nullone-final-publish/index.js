/**
 * NullOne final-publish plugin entry (OpenClaw 2026.8.2).
 *
 * Registers ONE interactive handler (channel telegram, namespace texbrif).
 * `publish:<POST_ID>` callbacks take the deterministic route: envelope +
 * HMAC over the private daemon pipe, zero LLM involvement. Every other
 * `texbrif:*` callback returns handled:false so existing approval/reject/
 * revise/back agent flow is byte-for-byte unchanged.
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
 *
 * NOT deployed by Git merge. Install/enable/restart happens only at the
 * controlled #37 deployment. See README.md in this directory.
 */

const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const { definePluginEntry } = require("openclaw/plugin-sdk/plugin-entry");
const { routeCallback, canonicalStringify } = require("./route");

const FRAME_MAGIC = Buffer.from("NP1", "utf8");
const FRAME_VERSION = 1;
const MAX_BODY_LEN = 8192;
const MAC_LEN = 32;
const KEY_LEN = 32;
const HANDSHAKE_TIMEOUT_MS = 15000;
const REQUEST_TIMEOUT_MS = 120000;

const SAFE_TEXT = {
  published: "✅ Nəşr tamamlandı.",
  publishing: "⏳ Nəşr emaldadır.",
  blocked: "⛔ Təhlükəsiz blok. Yeni ikinci təsdiq tələb olunur.",
  abandoned: "⛔ Köhnə təsdiq qüvvədən düşüb. Yeni ikinci təsdiq tələb olunur.",
  failed: "❌ Nəşr uğursuz oldu. Təkrar cəhd edilməyəcək.",
  unknown: "❓ Nəşr statusu qeyri-müəyyəndir. Təkrar cəhd edilməyəcək.",
  rejected: "⛔ Sorğu rədd edildi.",
  unavailable: "⛔ Nəşr xidməti hazır deyil. Heç nə yayımlanmadı.",
};

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
    this.child = null;
    this.key = null;
    this.ready = false;
    this.buffer = Buffer.alloc(0);
    this.pending = new Map();
    this.handshake = null;
    this.writeChain = Promise.resolve();
  }

  async ensure() {
    if (this.child && this.ready) {
      return;
    }
    await this._spawn();
  }

  async _spawn() {
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
    // Private spawn pipe: the per-boot key goes first, exactly KEY_LEN bytes.
    // argv carries nothing sensitive.
    child.stdin.write(this.key);
    await this._waitReady();
    this.ready = true;
  }

  _onDeath(error) {
    this.ready = false;
    this.child = null;
    // Fail closed: every outstanding requester gets a terminal rejection.
    // No legacy LLM fallback exists anywhere on this path.
    if (this.handshake) {
      const reject = this.handshake.reject;
      this.handshake = null;
      reject(error);
    }
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer);
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
    const run = async () => {
      await this.ensure();
      const frame = buildFrame(envelope, this.key);
      const reply = await new Promise((resolvePromise, rejectPromise) => {
        const timer = setTimeout(() => {
          this.pending.delete(requestId);
          rejectPromise(new Error("request timeout"));
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
        this.child.stdin.write(frame);
      });
      return reply;
    };
    const queued = this.writeChain.then(run);
    // Keep the write chain alive across failures; callers see their error.
    this.writeChain = queued.catch(() => {});
    return queued;
  }
}

function outcomeText(reply) {
  if (!reply || reply.t !== "result") {
    return SAFE_TEXT.unknown;
  }
  const code = Number(reply.code);
  if (code === 0) {
    return SAFE_TEXT.published;
  }
  if (code === 2) {
    const hint = String(reply.hint || "");
    if (hint === "fresh_confirmation_required") {
      return SAFE_TEXT.abandoned;
    }
    return SAFE_TEXT.blocked;
  }
  return SAFE_TEXT.unknown;
}

/**
 * Build the interactive handler with an injected daemon link (production
 * constructs the real link; offline tests inject a fake).
 */
function buildHandler(link) {
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
      // Malformed publish-shaped callback: swallow safely, zero side effects.
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
      // Best-effort user feedback; outcome truth lives in receipts.
    }
    // No submitText: zero LLM involvement after the human click.
    return { handled: true };
  };
}

module.exports = {
  routeCallback,
  canonicalStringify,
  buildFrame,
  buildHandler,
  DaemonLink,
  outcomeText,
  SAFE_TEXT,
};

module.exports.default = definePluginEntry({
  register(api, ctx) {
    const workspace =
      (ctx && ctx.workspace) || process.env.NULLONE_WORKSPACE || "";
    const pythonBin = (ctx && ctx.pythonBin) || "python3";
    const controllerPath =
      (ctx && ctx.controllerPath) ||
      "workspace/social/ops/scripts/nullone_final_publish_controller.py";
    const link = new DaemonLink(pythonBin, controllerPath, workspace);

    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "texbrif",
      handler: buildHandler(link),
    });
  },
});
