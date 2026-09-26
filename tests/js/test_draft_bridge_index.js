/**
 * Offline integration tests for plugins/nullone-draft-bridge/index.js (#142).
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
const { spawn } = require("node:child_process");

const MID = "2026-09-16-ai-access-equity-gates-pledge-2026-09-16";

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
      data: `texbrif:draft:${MID}`,
      namespace: "texbrif",
      payload: `draft:${MID}`,
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
  const runner = async (manifestId) => {
    calls.push(manifestId);
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
  plugin = require("../../plugins/nullone-draft-bridge/index.js");
});

function registeredHandler(runner) {
  const api = makeApi();
  // P0 texbrif namespace collision fix: this plugin no longer registers its
  // own interactive handler (nullone-final-publish is the sole registrant
  // and delegates to plugin.buildHandler(runner) directly). register() must
  // still be a safe no-op call, but claims zero registrations.
  plugin.register(api);
  assert.equal(registrations.length, 0);
  return plugin.buildHandler(runner);
}

test("NO_REGISTRATION_COLLISION: register() never calls registerInteractiveHandler", () => {
  const api = makeApi();
  plugin.register(api);
  assert.deepEqual(registrations, []);
});

test("OpenClaw loader sees top-level plugin entry", () => {
  assert.equal(plugin.id, "nullone-draft-bridge");
  assert.equal(typeof plugin.register, "function");
  assert.equal(typeof plugin.buildHandler, "function");
  assert.equal(typeof plugin.resolveControllerPath, "function");
});

test("draft callback invokes the runner with exactly the manifest id", async () => {
  const runner = makeRunner();
  const handler = registeredHandler(runner);
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.equal(result.handled, true);
  assert.deepEqual(runner.calls, [MID]);
  assert.equal(ctx._replies.length, 1);
});

test("non-draft callbacks fall through to existing flows", async () => {
  const runner = makeRunner();
  const handler = registeredHandler(runner);
  for (const data of [
    "texbrif:approve:0123456789abcdef01234567",
    "texbrif:publish:0123456789abcdef01234567",
  ]) {
    const ctx = makeHandlerCtx({ callback: { data, messageId: 1, chatId: "c" } });
    const result = await handler(ctx);
    assert.equal(result.handled, false, data);
  }
  assert.deepEqual(runner.calls, []);
});

test("malformed draft callback is consumed with zero runner calls", async () => {
  const runner = makeRunner();
  const handler = registeredHandler(runner);
  const ctx = makeHandlerCtx({ callback: { data: "texbrif:draft:../x", messageId: 1, chatId: "c" } });
  const result = await handler(ctx);
  assert.equal(result.handled, true);
  assert.deepEqual(runner.calls, []);
  assert.equal(ctx._replies.length, 0);
});

test("null runner fails closed with zero side effects", async () => {
  const handler = plugin.buildHandler(null);
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.equal(result.handled, true);
  assert.equal(ctx._replies.length, 1);
});

test("unauthorized sender is silent with zero runner calls", async () => {
  const runner = makeRunner();
  const handler = registeredHandler(runner);
  const ctx = makeHandlerCtx({ auth: { isAuthorizedSender: false } });
  const result = await handler(ctx);
  assert.equal(result.handled, true);
  assert.deepEqual(runner.calls, []);
  assert.equal(ctx._replies.length, 0);
});

test("runner failure replies unavailable", async () => {
  const runner = makeRunner({ throw: new Error("spawn failed") });
  const handler = registeredHandler(runner);
  const ctx = makeHandlerCtx();
  const result = await handler(ctx);
  assert.equal(result.handled, true);
  assert.match(ctx._replies[0], /hazır deyil/);
});

test("outcome text mapping is fixed and input-free", () => {
  assert.match(plugin.outcomeText("ACTION_STATUS=COMPLETED\nREASON_CODE=OK\n"), /yaradıldı/);
  assert.match(plugin.outcomeText("ACTION_STATUS=BLOCKED\nREASON_CODE=ACTION_BUSY\n"), /icradadır/);
  assert.match(plugin.outcomeText("ACTION_STATUS=BLOCKED\nREASON_CODE=X\n"), /bloklandı/);
  assert.match(plugin.outcomeText("garbage"), /hazır deyil/);
  assert.match(plugin.outcomeText(null), /hazır deyil/);
});

test("spawn runner uses fixed argv without shell or env additions", async () => {
  const seen = [];
  const fakeSpawn = (bin, argv, opts) => {
    const handlers = {};
    const child = {
      stdout: { on: () => {} },
      on: (event, fn) => {
        handlers[event] = fn;
      },
      kill: () => {},
    };
    seen.push({ bin, argv, opts, child, handlers });
    return child;
  };
  const Module2 = require("node:module");
  const originalLoad = Module2._load;
  Module2._load = function (request, parent, isMain) {
    if (request === "node:child_process") {
      return { spawn: fakeSpawn };
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  let runner;
  try {
    delete require.cache[require.resolve("../../plugins/nullone-draft-bridge/index.js")];
    const fresh = require("../../plugins/nullone-draft-bridge/index.js");
    runner = fresh.spawnRunner("python3", "/ws/social/ops/scripts/nullone_draft_bridge_action.py", "/ws");
    var pending = runner(MID);
  } finally {
    Module2._load = originalLoad;
  }
  assert.equal(seen.length, 1);
  assert.equal(seen[0].bin, "python3");
  assert.deepEqual(seen[0].argv, [
    "/ws/social/ops/scripts/nullone_draft_bridge_action.py",
    "handle",
    "--manifest-id",
    MID,
  ]);
  assert.equal(seen[0].opts.cwd, "/ws");
  assert.equal(seen[0].opts.shell, undefined);
  assert.deepEqual(Object.keys(seen[0].opts.stdio), ["0", "1", "2"]);
  // Complete the child lifecycle deterministically (no timer wait).
  seen[0].handlers.exit(0);
  const stdout = await pending;
  assert.equal(stdout, "");
});

test("controller path refuses escape and wrong basename", () => {
  assert.throws(() => plugin.resolveControllerPath("", undefined), /workspace unavailable/);
  assert.throws(
    () => plugin.resolveControllerPath("/ws", "/etc/evil.py"),
    /basename mismatch/
  );
  const ok = plugin.resolveControllerPath(
    "/ws",
    "/other/social/ops/scripts/nullone_draft_bridge_action.py"
  );
  assert.equal(ok, "/other/social/ops/scripts/nullone_draft_bridge_action.py");
});
