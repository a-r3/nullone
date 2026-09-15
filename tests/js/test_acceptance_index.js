/**
 * Offline integration tests for plugins/nullone-acceptance/index.js (#129).
 *
 * Stubs the OpenClaw SDK import (definePluginEntry passthrough) and drives
 * the REAL registered handler with exact 2026.8.2-shaped handlerCtx objects:
 * callback.{data,messageId,chatId}, accountId/senderId,
 * auth.isAuthorizedSender, respond.reply. A fake runner stands in for the
 * Python action-core spawn. No OpenClaw runtime, no Telegram, no Gateway,
 * no network.
 */
"use strict";

const assert = require("node:assert/strict");
const { test, before } = require("node:test");
const Module = require("node:module");

const AID = "nullone-acceptance-20260101-000000";

let plugin;
let registrations;

function makeApi(opts) {
  registrations = [];
  const config = (opts && opts.config) || {};
  const api = {
    registerInteractiveHandler: (reg) => {
      registrations.push(reg);
    },
    pluginConfig: {},
    config,
    runtime: {
      agent: {
        resolveAgentWorkspaceDir: (_cfg, _agentId) =>
          opts && Object.prototype.hasOwnProperty.call(opts, "workspace")
            ? opts.workspace
            : "/tmp/nullone-test-workspace",
      },
    },
  };
  return api;
}

function makeHandlerCtx(overrides) {
  const replies = [];
  const ctx = {
    callback: {
      data: `texbrif:accept:${AID}`,
      namespace: "texbrif",
      payload: `accept:${AID}`,
      messageId: 424242,
      chatId: "770011",
    },
    accountId: "test-bot-account",
    senderId: "990022",
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

function makeRunner(impl) {
  const calls = [];
  const runner = async (acceptanceId) => {
    calls.push(acceptanceId);
    if (impl && impl.throw) {
      throw impl.throw;
    }
    return impl && impl.stdout !== undefined
      ? impl.stdout
      : "ACTION_STATUS=COMPLETED\nREASON_CODE=OK\n";
  };
  runner.calls = calls;
  return runner;
}

before(() => {
  const originalLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "openclaw/plugin-sdk/plugin-entry") {
      return {
        definePluginEntry: ({ configSchema, ...rest }) => ({
          configSchema: configSchema || {},
          ...rest,
        }),
      };
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  plugin = require("../../plugins/nullone-acceptance/index.js");
});

function registeredHandler(runner) {
  const api = makeApi();
  plugin.register(api);
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].channel, "telegram");
  assert.equal(registrations[0].namespace, "texbrif");
  return plugin.buildHandler(runner);
}

test("OpenClaw loader sees top-level plugin entry", () => {
  assert.equal(plugin.id, "nullone-acceptance");
  assert.equal(typeof plugin.register, "function");
  assert.equal(typeof plugin.buildHandler, "function");
  assert.equal(typeof plugin.resolveControllerPath, "function");
});

test("accept callback invokes the runner with exactly the acceptance id", async () => {
  const runner = makeRunner();
  const handler = registeredHandler(runner);
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.equal(result.handled, true);
  assert.deepEqual(runner.calls, [AID]);
  assert.equal(ctx._replies.length, 1);
  assert.equal(ctx._replies[0], plugin.SAFE_TEXT.completed);
});

test("unauthorized sender never reaches the runner", async () => {
  const runner = makeRunner();
  const handler = registeredHandler(runner);
  const ctx = makeHandlerCtx({ auth: { isAuthorizedSender: false } });
  const result = await handler(ctx);
  assert.equal(result.handled, true);
  assert.deepEqual(runner.calls, []);
  assert.deepEqual(ctx._replies, []);
});

test("approval-family and publish callbacks fall through untouched", async () => {
  const runner = makeRunner();
  const handler = registeredHandler(runner);
  for (const data of [
    "texbrif:approve:x",
    "texbrif:reject:x",
    "texbrif:revise:x",
    "texbrif:back:x",
    "texbrif:publish:0123456789abcdef01234567",
    "ordinary operator text",
  ]) {
    const ctx = makeHandlerCtx({ callback: { data, messageId: 1, chatId: "770011" } });
    const result = await handler(ctx);
    assert.equal(result.handled, false, data);
  }
  assert.deepEqual(runner.calls, []);
});

test("malformed accept-shaped callback is consumed with zero side effects", async () => {
  const runner = makeRunner();
  const handler = registeredHandler(runner);
  const ctx = makeHandlerCtx({
    callback: { data: "texbrif:accept:not-an-id", messageId: 1, chatId: "770011" },
  });
  const result = await handler(ctx);
  assert.equal(result.handled, true);
  assert.deepEqual(runner.calls, []);
});

test("missing message identity fails closed before the runner", async () => {
  const runner = makeRunner();
  const handler = registeredHandler(runner);
  for (const callback of [
    { data: `texbrif:accept:${AID}`, messageId: 0, chatId: "770011" },
    { data: `texbrif:accept:${AID}`, messageId: 5, chatId: "" },
  ]) {
    const ctx = makeHandlerCtx({ callback });
    const result = await handler(ctx);
    assert.equal(result.handled, true);
  }
  assert.deepEqual(runner.calls, []);
});

test("runner failure replies unavailable and stays consumed", async () => {
  const runner = makeRunner({ throw: new Error("spawn failed") });
  const handler = registeredHandler(runner);
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.equal(result.handled, true);
  assert.deepEqual(ctx._replies, [plugin.SAFE_TEXT.unavailable]);
});

test("null runner (failed registration) is safe with zero side effects", async () => {
  const handler = plugin.buildHandler(null);
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.equal(result.handled, true);
  assert.deepEqual(ctx._replies, [plugin.SAFE_TEXT.unavailable]);
});

test("outcomeText maps core output to fixed replies only", () => {
  assert.equal(
    plugin.outcomeText("ACTION_STATUS=COMPLETED\nREASON_CODE=OK\n"),
    plugin.SAFE_TEXT.completed
  );
  assert.equal(
    plugin.outcomeText("ACTION_STATUS=BLOCKED\nREASON_CODE=ACTION_BUSY\n"),
    plugin.SAFE_TEXT.busy
  );
  assert.equal(
    plugin.outcomeText("ACTION_STATUS=BLOCKED\nREASON_CODE=ACTION_INVALID_ACCEPTANCE_ID\n"),
    plugin.SAFE_TEXT.blocked
  );
  assert.equal(plugin.outcomeText("garbage"), plugin.SAFE_TEXT.unavailable);
  assert.equal(plugin.outcomeText(null), plugin.SAFE_TEXT.unavailable);
});

test("controller path is deterministic, contained, and override-guarded", () => {
  const path = require("node:path");
  const built = plugin.resolveControllerPath("/srv/ws", undefined);
  assert.equal(
    built,
    path.join("/srv/ws", "social", "ops", "scripts", "nullone_acceptance_action.py")
  );
  const override = "/tmp/x/nullone_acceptance_action.py";
  assert.equal(plugin.resolveControllerPath("/srv/ws", override), override);
  assert.throws(() => plugin.resolveControllerPath("", undefined));
  assert.throws(() => plugin.resolveControllerPath("/srv/ws", "relative/path.py"));
  assert.throws(() => plugin.resolveControllerPath("/srv/ws", "/tmp/x/other.py"));
});
