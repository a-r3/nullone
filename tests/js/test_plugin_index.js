/**
 * Offline integration tests for plugins/nullone-final-publish/index.js (#89).
 *
 * Stubs the OpenClaw SDK import (definePluginEntry passthrough) and drives
 * the REAL registered handler with exact 2026.8.2-shaped handlerCtx objects:
 * callback.{data,messageId,chatId}, accountId/conversationId/senderId,
 * auth.isAuthorizedSender, respond.reply. A fake daemon link stands in for
 * the Python controller. No OpenClaw runtime, no Telegram, no network.
 */
"use strict";

const assert = require("node:assert/strict");
const { test, before } = require("node:test");
const Module = require("node:module");

const POST = "0123456789abcdef01234567";

let plugin;
let registrations;

function makeApi(opts) {
  registrations = [];
  return {
    registerInteractiveHandler: (reg) => {
      registrations.push(reg);
    },
    pluginConfig:
      (opts && opts.pluginConfig) || {},
  };
}

function makeCtx(overrides) {
  return {
    workspace: "/tmp/nullone-test-workspace",
    ...(overrides || {}),
  };
}

function makeHandlerCtx(overrides) {
  const replies = [];
  const ctx = {
    callback: {
      data: `texbrif:publish:${POST}`,
      namespace: "texbrif",
      payload: `publish:${POST}`,
      messageId: 424242,
      chatId: "770011",
      messageText: "approval card",
    },
    accountId: "test-bot-account",
    conversationId: "770011",
    senderId: "990022",
    senderUsername: "operator",
    auth: { isAuthorizedSender: true },
    respond: {
      reply: async ({ text }) => {
        replies.push(text);
        return undefined;
      },
    },
    ...(overrides || {}),
  };
  ctx._replies = replies;
  return ctx;
}

function makeLink(impl) {
  return {
    calls: [],
    request: async function (envelope) {
      this.calls.push(envelope);
      if (impl && impl.throw) {
        throw impl.throw;
      }
      if (impl && Object.prototype.hasOwnProperty.call(impl, "reply")) {
        return impl.reply;
      }
      return { t: "result", outcome: "SETTLED", code: 0 };
    },
  };
}

before(() => {
  const originalLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "openclaw/plugin-sdk/plugin-entry") {
      // Faithful-enough stand-in for the real SDK: passes id/name/
      // description/register through unchanged and defaults configSchema
      // (the real helper always exposes one, even when the caller omits
      // it), without pulling in the full host SDK offline.
      return {
        definePluginEntry: ({ configSchema, ...rest }) => ({
          configSchema: configSchema || {},
          ...rest,
        }),
      };
    }
    if (request === "openclaw/plugin-sdk/secret-input-runtime") {
      return {
        resolveRequiredConfiguredSecretRefInputString: async ({ value }) => {
          if (
            value &&
            value.source === "store" &&
            value.id === "ZERNIO_PUBLISH_API_TOKEN"
          ) {
            return { value: "resolved-store-token" };
          }
          return { unresolvedRefReason: "not configured" };
        },
      };
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  plugin = require("../../plugins/nullone-final-publish/index.js");
});

function registeredHandler(link) {
  const api = makeApi();
  plugin.register(api, makeCtx());
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].channel, "telegram");
  assert.equal(registrations[0].namespace, "texbrif");
  // Swap the real link for the fake by rebuilding through buildHandler.
  return plugin.buildHandler(link);
}

test("OpenClaw loader sees top-level plugin entry (regression)", () => {
  // The production loader reads the plugin entry directly off the module
  // export (require(...) / import().default) -- it never unwraps a nested
  // `.default`. This is the exact shape the Sep-11 live callback fallthrough
  // proved missing: FAILS against pre-fix main, where the entry lived only
  // at module.exports.default and module.exports itself had no `register`.
  assert.equal(typeof plugin.register, "function");
  assert.equal(plugin.id, "nullone-final-publish");
  assert.equal(plugin.name, "NullOne Final Publish Handoff");
  assert.equal(typeof plugin.description, "string");
  assert.ok(plugin.description.length > 0);
  assert.ok("configSchema" in plugin);

  const api = makeApi();
  plugin.register(api, makeCtx());
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].channel, "telegram");
  assert.equal(registrations[0].namespace, "texbrif");
});

test("registration claims telegram/texbrif exactly once", () => {
  const api = makeApi();
  plugin.register(api, makeCtx());
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].channel, "telegram");
  assert.equal(registrations[0].namespace, "texbrif");
  assert.equal(typeof registrations[0].handler, "function");
});

test("exact valid handlerCtx produces the expected envelope", async () => {
  const link = makeLink();
  const handler = registeredHandler(link);
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal("submitText" in result, false);
  assert.equal(link.calls.length, 1);
  const envelope = link.calls[0];
  assert.equal(envelope.schema, "nullone.publish-callback.v1");
  assert.equal(envelope.post_id, POST);
  assert.equal(envelope.account_id, "test-bot-account");
  assert.equal(envelope.chat_id, "770011");
  assert.equal(envelope.message_id, "424242");
  assert.equal(envelope.sender_id, "990022");
  assert.match(envelope.nonce, /^[0-9a-f]{32}$/);
  assert.match(envelope.request_id, /^[0-9a-f]{32}$/);
  assert.notEqual(envelope.nonce, envelope.request_id);
  assert.equal(ctx._replies.length, 1);
});

test("callbackMessage.messageId drives identity: new id, new instance fields", async () => {
  const link = makeLink();
  const handler = registeredHandler(link);
  await handler(makeHandlerCtx({ callback: {
    data: `texbrif:publish:${POST}`,
    messageId: 111,
    chatId: "770011",
  }}));
  await handler(makeHandlerCtx({ callback: {
    data: `texbrif:publish:${POST}`,
    messageId: 222,
    chatId: "770011",
  }}));
  assert.equal(link.calls.length, 2);
  assert.equal(link.calls[0].message_id, "111");
  assert.equal(link.calls[1].message_id, "222");
});

test("missing callbackMessage identity fails closed with zero daemon request", async () => {
  const link = makeLink();
  const handler = registeredHandler(link);
  for (const callback of [
    { data: `texbrif:publish:${POST}` },
    { data: `texbrif:publish:${POST}`, messageId: 0, chatId: "770011" },
    { data: `texbrif:publish:${POST}`, messageId: 424242, chatId: "" },
    { data: `texbrif:publish:${POST}`, messageId: "not-a-number", chatId: "1" },
  ]) {
    const result = await handler(makeHandlerCtx({ callback }));
    assert.deepEqual(result, { handled: true });
  }
  assert.equal(link.calls.length, 0);
});

test("unauthorized sender fails closed with zero daemon request", async () => {
  const link = makeLink();
  const handler = registeredHandler(link);
  const ctx = makeHandlerCtx({ auth: { isAuthorizedSender: false } });
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(link.calls.length, 0);
  assert.equal(ctx._replies.length, 0);
});

test("approve/reject/revise/back fall through to agent flow", async () => {
  const link = makeLink();
  const handler = registeredHandler(link);
  for (const action of ["approve", "reject", "revise", "back"]) {
    const result = await handler(
      makeHandlerCtx({ callback: { data: `texbrif:${action}:${POST}` } })
    );
    assert.deepEqual(result, { handled: false }, action);
  }
  assert.equal(link.calls.length, 0);
});

test("malformed publish callbacks are consumed safely", async () => {
  const link = makeLink();
  const handler = registeredHandler(link);
  const result = await handler(
    makeHandlerCtx({ callback: { data: "texbrif:publish:ZZZ" } })
  );
  assert.deepEqual(result, { handled: true });
  assert.equal(link.calls.length, 0);
});

test("daemon loss is consumed fail-closed with safe text, no LLM fallback", async () => {
  const link = makeLink({ throw: new Error("daemon exited") });
  const handler = registeredHandler(link);
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(ctx._replies.length, 1);
  assert.match(ctx._replies[0], /hazır deyil|unavailable|blok/i);
});

test("outcome codes map to safe bounded texts", async () => {
  const cases = [
    [{ t: "result", outcome: "SETTLED", code: 0, publication_state: "PUBLISHED" }, /tamamlandı/],
    [{ t: "result", outcome: "SETTLED", code: 0, publication_state: "PUBLISHING" }, /emaldadır/],
    [{ t: "result", outcome: "SETTLED", code: 0, publication_state: "FAILED" }, /uğursuz/],
    [{ t: "result", outcome: "SETTLED", code: 0, publication_state: "CHECK_REQUIRED" }, /yoxlama tələb/],
    [{ t: "result", outcome: "SETTLED", code: 0, publication_state: "READBACK_FAILED" }, /oxunuşu uğursuz|qeyri-müəyyən/],
    [{ t: "result", outcome: "SETTLED", code: 3, publication_state: "UNKNOWN" }, /qeyri-müəyyən/],
    [{ t: "result", outcome: "SETTLED", code: 2, publication_state: "BLOCKED", hint: "fresh_confirmation_required" }, /təsdiq/],
    [{ t: "result", outcome: "SETTLED", code: 2, publication_state: "BLOCKED" }, /blok/],
    [{ t: "result", outcome: "REJECTED", code: 2, publication_state: "REJECTED" }, /rədd edildi/],
    [{ t: "result", outcome: "SETTLED", code: 0 }, /qeyri-müəyyən/],
    [{ t: "result", outcome: "SETTLED", code: 0, publication_state: "SOMETHING_ELSE" }, /qeyri-müəyyən/],
    [null, /qeyri-müəyyən/],
  ];
  for (const [reply, pattern] of cases) {
    const link = makeLink({ reply });
    const handler = registeredHandler(link);
    const ctx = makeHandlerCtx();
    await handler(ctx);
    assert.match(ctx._replies[0], pattern, JSON.stringify(reply));
  }
});

test("only PUBLISHED may use the success text (false-positive sweep)", async () => {
  const states = [
    "PUBLISHING",
    "FAILED",
    "CHECK_REQUIRED",
    "READBACK_FAILED",
    "UNKNOWN",
    "BLOCKED",
    "REJECTED",
    null,
    "SOMETHING_ELSE",
  ];
  for (const publication_state of states) {
    const reply = { t: "result", outcome: "SETTLED", code: 0 };
    if (publication_state !== null) {
      reply.publication_state = publication_state;
    }
    const link = makeLink({ reply });
    const handler = registeredHandler(link);
    const ctx = makeHandlerCtx();
    await handler(ctx);
    assert.ok(
      !ctx._replies[0].includes("Nəşr tamamlandı"),
      `state ${publication_state} must not claim completion`
    );
  }
  const published = makeLink({
    reply: { t: "result", outcome: "SETTLED", code: 0, publication_state: "PUBLISHED" },
  });
  const handler = registeredHandler(published);
  const ctx = makeHandlerCtx();
  await handler(ctx);
  assert.ok(ctx._replies[0].includes("Nəşr tamamlandı"));
});

test("A: workspace maps to the exact in-workspace controller path", () => {
  const resolved = plugin.resolveControllerPath("/tmp/nullone-workspace");
  assert.equal(
    resolved,
    "/tmp/nullone-workspace/social/ops/scripts/nullone_final_publish_controller.py"
  );
});

test("B: no duplicated workspace/workspace component", () => {
  const resolved = plugin.resolveControllerPath("/tmp/nullone-workspace");
  assert.ok(!resolved.includes("workspace/workspace"), resolved);
  const deployed = plugin.resolveControllerPath("/home/oem/.openclaw/workspace");
  assert.equal(
    deployed,
    "/home/oem/.openclaw/workspace/social/ops/scripts/nullone_final_publish_controller.py"
  );
  assert.ok(!deployed.includes("workspace/workspace"), deployed);
});

test("C: default registration spawns the exact production-relative path", async () => {
  const crypto = require("node:crypto");
  const { EventEmitter } = require("node:events");
  const seen = { argv: null, key: null };
  const { canonicalStringify } = plugin;
  const STARTUP_TOKEN = "test-e2e-token";
  function replyFrame(payload, key) {
    const body = Buffer.from(canonicalStringify(payload), "utf8");
    const header = Buffer.alloc(8);
    Buffer.from("NP1", "utf8").copy(header, 0);
    header.writeUInt8(1, 3);
    header.writeUInt32BE(body.length, 4);
    const mac = crypto.createHmac("sha256", key).update(header).update(body).digest();
    return Buffer.concat([header, body, mac]);
  }
  const fake = new EventEmitter();
  fake.stdout = new EventEmitter();
  fake.stdin = {
    write: (chunk) => {
      const data = Buffer.from(chunk);
      if (!seen.key) {
        assert.equal(data.length, 32);
        seen.key = data;
        setImmediate(() =>
          fake.stdout.emit(
            "data",
            replyFrame(
              { schema: "nullone.publish-reply.v1", t: "ready", reconciled: "0/0/0" },
              seen.key
            )
          )
        );
        return true;
      }
      if (!seen.startup) {
        // Second write is the bounded startup credential frame (#90).
        seen.startup = data;
        return true;
      }
      if (!seen.envelope) {
        seen.envelope = data;
      }
      // Echo the REAL request_id from the framed envelope (parsed, not guessed).
      const bodyLen = data.readUInt32BE(4);
      const envelope = JSON.parse(data.subarray(8, 8 + bodyLen).toString("utf8"));
      const requestId = envelope.request_id;
      setImmediate(() =>
        fake.stdout.emit(
          "data",
          replyFrame(
            {
              schema: "nullone.publish-reply.v1",
              t: "result",
              outcome: "SETTLED",
              code: "0",
              publication_state: "PUBLISHED",
              request_id: requestId,
            },
            seen.key
          )
        )
      );
      return true;
    },
  };
  const spawnFn = (bin, argv, opts) => {
    seen.argv = argv;
    seen.opts = opts;
    return fake;
  };
  const api = makeApi({ pluginConfig: { publishToken: STARTUP_TOKEN } });
  plugin.register(
    api,
    makeCtx({ workspace: "/tmp/nullone-workspace", spawnFn })
  );
  assert.equal(registrations.length, 1);
  const handler = registrations[0].handler;
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.ok(seen.argv, "spawn must have been invoked");
  assert.equal(
    seen.argv[0],
    "/tmp/nullone-workspace/social/ops/scripts/nullone_final_publish_controller.py"
  );
  assert.deepEqual(seen.argv.slice(1), ["daemon"]);
  assert.ok(!seen.argv[0].includes("workspace/workspace"), seen.argv[0]);
  assert.ok(ctx._replies[0].includes("Nəşr tamamlandı"));
  // Startup credential frame (#90): HMAC'd, exact schema, carries the
  // resolved token, sent before the first envelope.
  assert.ok(seen.startup, "startup frame must be the second write");
  const startupLen = seen.startup.readUInt32BE(4);
  const startupBody = seen.startup.subarray(8, 8 + startupLen);
  const startupMac = seen.startup.subarray(8 + startupLen, 8 + startupLen + 32);
  const startupExpected = crypto
    .createHmac("sha256", seen.key)
    .update(seen.startup.subarray(0, 8))
    .update(startupBody)
    .digest();
  assert.ok(crypto.timingSafeEqual(startupMac, startupExpected));
  const startupParsed = JSON.parse(startupBody.toString("utf8"));
  assert.equal(startupParsed.schema, "nullone.publish-startup.v1");
  assert.equal(startupParsed.publish_token, STARTUP_TOKEN);
  // Token never enters spawn argv or environment.
  assert.ok(!JSON.stringify(seen.argv).includes(STARTUP_TOKEN));
  assert.ok(!JSON.stringify(seen.opts.env).includes(STARTUP_TOKEN));
});

test("D: missing workspace fails closed with zero spawn", async () => {
  let spawned = false;
  const spawnFn = () => {
    spawned = true;
    throw new Error("must not spawn");
  };
  const api = makeApi();
  plugin.register(api, makeCtx({ workspace: "", spawnFn }));
  assert.equal(registrations.length, 1);
  const ctx = makeHandlerCtx();
  const result = await registrations[0].handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(spawned, false);
  assert.equal(ctx._replies.length, 1);
  assert.match(ctx._replies[0], /hazır deyil/);
});

test("E: spawn failure consumes safely and never routes to LLM", async () => {
  const spawnFn = () => {
    throw new Error("spawn exploded");
  };
  const api = makeApi({ pluginConfig: { publishToken: "test-token" } });
  plugin.register(
    api,
    makeCtx({ workspace: "/tmp/nullone-workspace", spawnFn })
  );
  const ctx = makeHandlerCtx();
  const result = await registrations[0].handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal("submitText" in result, false);
  assert.equal(ctx._replies.length, 1);
  assert.match(ctx._replies[0], /hazır deyil/);
});

test("pre-dispatch spawn failure says request was not sent (exact)", async () => {
  const spawnFn = () => {
    throw new Error("spawn exploded");
  };
  const api = makeApi({ pluginConfig: { publishToken: "test-token" } });
  plugin.register(
    api,
    makeCtx({ workspace: "/tmp/nullone-workspace", spawnFn })
  );
  const ctx = makeHandlerCtx();
  const result = await registrations[0].handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(ctx._replies.length, 1);
  assert.ok(ctx._replies[0].includes("Heç nə yayımlanmadı"));
  assert.ok(!ctx._replies[0].includes("qeyri-müəyyən"));
});

test("post-dispatch timeout uses the exact UNKNOWN wording", async () => {
  const error = new Error("request timeout");
  error.dispatched = true;
  const link = {
    request: async () => {
      throw error;
    },
  };
  const handler = plugin.buildHandler(link);
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(ctx._replies.length, 1);
  assert.equal(
    ctx._replies[0],
    "❓ Nəşr sorğusunun nəticəsi qeyri-müəyyəndir. Avtomatik təkrar cəhd edilməyəcək."
  );
  assert.ok(!ctx._replies[0].includes("Heç nə yayımlanmadı"));
});

test("post-dispatch daemon death uses the exact UNKNOWN wording", async () => {
  const error = new Error("daemon exited");
  error.dispatched = true;
  const link = {
    request: async () => {
      throw error;
    },
  };
  const handler = plugin.buildHandler(link);
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(
    ctx._replies[0],
    "❓ Nəşr sorğusunun nəticəsi qeyri-müəyyəndir. Avtomatik təkrar cəhd edilməyəcək."
  );
});

test("pre-dispatch rejection keeps the not-sent wording", async () => {
  for (const error of [new Error("spawn exploded"), new Error("handshake timeout")]) {
    const link = {
      request: async () => {
        throw error;
      },
    };
    const handler = plugin.buildHandler(link);
    const ctx = makeHandlerCtx();
    const result = await handler(ctx);
    assert.deepEqual(result, { handled: true });
    assert.ok(ctx._replies[0].includes("Heç nə yayımlanmadı"));
    assert.ok(!ctx._replies[0].includes("qeyri-müəyyən"));
  }
});

test("manifest declares the publishToken SecretInput path", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const manifest = JSON.parse(
    fs.readFileSync(
      path.join(__dirname, "../../plugins/nullone-final-publish/openclaw.plugin.json"),
      "utf8"
    )
  );
  const paths =
    manifest.configContracts &&
    manifest.configContracts.secretInputs &&
    manifest.configContracts.secretInputs.paths;
  assert.ok(Array.isArray(paths), "secretInputs.paths must exist");
  const entry = paths.find(
    (p) =>
      p.path ===
      "plugins.entries.nullone-final-publish.config.publishToken"
  );
  assert.ok(entry, "publishToken secret input must be declared");
  assert.equal(entry.expected, "string");
});

test("manifest declares Gateway-startup activation (regression)", () => {
  // The installed OpenClaw 2026.8.2 startup planner only imports a plugin
  // ahead of any live callback if its manifest sets activation.onStartup
  // (installed-plugin-index-scope-lookup: shouldConsiderForGatewayStartup).
  // Without it, the plugin is silently skipped every boot -- no error, no
  // diagnostic -- and its telegram/texbrif interactive handler never
  // reaches the active registry before a human clicks publish.
  const fs = require("node:fs");
  const path = require("node:path");
  const manifest = JSON.parse(
    fs.readFileSync(
      path.join(__dirname, "../../plugins/nullone-final-publish/openclaw.plugin.json"),
      "utf8"
    )
  );
  assert.equal(manifest.activation && manifest.activation.onStartup, true);
});

test("missing publishToken fails closed with zero spawn", async () => {
  let spawned = false;
  const api = makeApi({ pluginConfig: {} });
  plugin.register(
    api,
    makeCtx({
      workspace: "/tmp/nullone-workspace",
      spawnFn: () => {
        spawned = true;
        throw new Error("must not spawn");
      },
    })
  );
  assert.equal(registrations.length, 1);
  const ctx = makeHandlerCtx();
  const result = await registrations[0].handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(spawned, false);
  assert.equal(ctx._replies.length, 1);
  assert.match(ctx._replies[0], /hazır deyil/);
});

test("blank publishToken fails closed with zero spawn", async () => {
  let spawned = false;
  const api = makeApi({ pluginConfig: { publishToken: "   " } });
  plugin.register(
    api,
    makeCtx({
      workspace: "/tmp/nullone-workspace",
      spawnFn: () => {
        spawned = true;
        throw new Error("must not spawn");
      },
    })
  );
  const ctx = makeHandlerCtx();
  const result = await registrations[0].handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(spawned, false);
  assert.match(ctx._replies[0], /hazır deyil/);
});

test("non-store SecretRef fails closed with zero spawn", async () => {
  let spawned = false;
  const api = makeApi({
    pluginConfig: {
      publishToken: {
        source: "env",
        provider: "default",
        id: "ZERNIO_PUBLISH_API_TOKEN",
      },
    },
  });
  plugin.register(
    api,
    makeCtx({
      workspace: "/tmp/nullone-workspace",
      spawnFn: () => {
        spawned = true;
        throw new Error("must not spawn");
      },
    })
  );
  const ctx = makeHandlerCtx();
  const result = await registrations[0].handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(spawned, false);
  assert.match(ctx._replies[0], /hazır deyil/);
});

test("store SecretRef resolves through the SDK and reaches the pipe", async () => {
  const crypto = require("node:crypto");
  const { EventEmitter } = require("node:events");
  const seen = {};
  const { canonicalStringify } = plugin;
  function replyFrame(payload, key) {
    const body = Buffer.from(canonicalStringify(payload), "utf8");
    const header = Buffer.alloc(8);
    Buffer.from("NP1", "utf8").copy(header, 0);
    header.writeUInt8(1, 3);
    header.writeUInt32BE(body.length, 4);
    const mac = crypto.createHmac("sha256", key).update(header).update(body).digest();
    return Buffer.concat([header, body, mac]);
  }
  const fake = new EventEmitter();
  fake.stdout = new EventEmitter();
  fake.stdin = {
    write: (chunk) => {
      const data = Buffer.from(chunk);
      if (!seen.key) {
        seen.key = data;
        setImmediate(() =>
          fake.stdout.emit(
            "data",
            replyFrame(
              { schema: "nullone.publish-reply.v1", t: "ready", reconciled: "0/0/0" },
              seen.key
            )
          )
        );
        return true;
      }
      if (!seen.startup) {
        seen.startup = data;
        return true;
      }
      const bodyLen = data.readUInt32BE(4);
      const envelope = JSON.parse(data.subarray(8, 8 + bodyLen).toString("utf8"));
      setImmediate(() =>
        fake.stdout.emit(
          "data",
          replyFrame(
            {
              schema: "nullone.publish-reply.v1",
              t: "result",
              outcome: "SETTLED",
              code: "0",
              publication_state: "PUBLISHED",
              request_id: envelope.request_id,
            },
            seen.key
          )
        )
      );
      return true;
    },
  };
  const api = makeApi({
    pluginConfig: {
      publishToken: {
        source: "store",
        provider: "default",
        id: "ZERNIO_PUBLISH_API_TOKEN",
      },
    },
    config: { secrets: {} },
  });
  // makeApi only models pluginConfig; attach host config for resolution.
  api.config = { secrets: {} };
  plugin.register(
    api,
    makeCtx({ workspace: "/tmp/nullone-workspace", spawnFn: () => fake })
  );
  const ctx = makeHandlerCtx();
  const result = await registrations[0].handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.ok(seen.startup, "startup frame must be sent");
  const startupLen = seen.startup.readUInt32BE(4);
  const startupParsed = JSON.parse(
    seen.startup.subarray(8, 8 + startupLen).toString("utf8")
  );
  assert.equal(startupParsed.publish_token, "resolved-store-token");
  assert.ok(ctx._replies[0].includes("Nəşr tamamlandı"));
});
