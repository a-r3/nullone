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

function makeApi() {
  registrations = [];
  return {
    registerInteractiveHandler: (reg) => {
      registrations.push(reg);
    },
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
      return { definePluginEntry: (entry) => entry };
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  plugin = require("../../plugins/nullone-final-publish/index.js");
});

function registeredHandler(link) {
  const api = makeApi();
  plugin.default.register(api, makeCtx());
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].channel, "telegram");
  assert.equal(registrations[0].namespace, "texbrif");
  // Swap the real link for the fake by rebuilding through buildHandler.
  return plugin.buildHandler(link);
}

test("registration claims telegram/texbrif exactly once", () => {
  const api = makeApi();
  plugin.default.register(api, makeCtx());
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
    [{ t: "result", outcome: "SETTLED", code: 0 }, /tamamlandı/],
    [{ t: "result", outcome: "SETTLED", code: 2, hint: "fresh_confirmation_required" }, /təsdiq/],
    [{ t: "result", outcome: "SETTLED", code: 2 }, /blok/],
    [{ t: "result", outcome: "SETTLED", code: 3 }, /qeyri-müəyyən|uncertain/i],
    [{ t: "result", outcome: "BLOCKED", code: 2 }, /blok/],
    [null, /qeyri-müəyyən|uncertain/i],
  ];
  for (const [reply, pattern] of cases) {
    const link = makeLink({ reply });
    const handler = registeredHandler(link);
    const ctx = makeHandlerCtx();
    await handler(ctx);
    assert.match(ctx._replies[0], pattern, JSON.stringify(reply));
  }
});
