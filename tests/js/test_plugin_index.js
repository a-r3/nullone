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
const { EventEmitter } = require("node:events");
const { createApprovalStore } = require("../../plugins/nullone-final-publish/approval-route");

const POST = "0123456789abcdef01234567";

let plugin;
let registrations;

const DEFAULT_TEST_WORKSPACE = "/tmp/nullone-test-workspace";

function makeApi(opts) {
  registrations = [];
  const config = (opts && opts.config) || { secrets: {} };
  // Faithful to the installed 2026.8.2 host contract: the ONLY supported
  // way for a plugin to learn the served workspace is
  // api.runtime.agent.resolveAgentWorkspaceDir(api.config, agentId) -- there
  // is no ctx.workspace and no NULLONE_WORKSPACE on the real host path.
  const resolveAgentWorkspaceDir =
    opts && opts.resolveAgentWorkspaceDir
      ? opts.resolveAgentWorkspaceDir
      : (_cfg, _agentId) =>
          opts && Object.prototype.hasOwnProperty.call(opts, "workspace")
            ? opts.workspace
            : DEFAULT_TEST_WORKSPACE;
  const api = {
    registerInteractiveHandler: (reg) => {
      registrations.push(reg);
    },
    pluginConfig: (opts && opts.pluginConfig) || {},
    config,
    runtime: {
      agent: { resolveAgentWorkspaceDir },
    },
  };
  return api;
}

function makeCtx(overrides) {
  // Test/dev-only override hook (spawnFn, controllerPath, pythonBin). The
  // real Gateway never supplies this second register() argument at all, and
  // it must never carry workspace (see makeApi's resolveAgentWorkspaceDir).
  return { ...(overrides || {}) };
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
        // Faithful to the installed 2026.8.2 contract:
        // resolveRequiredConfiguredSecretRefInputString(...) -> Promise<string | undefined>
        resolveRequiredConfiguredSecretRefInputString: async ({ value }) => {
          if (
            value &&
            value.source === "store" &&
            value.id === "ZERNIO_PUBLISH_API_TOKEN"
          ) {
            return "resolved-store-token";
          }
          return undefined;
        },
      };
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  plugin = require("../../plugins/nullone-final-publish/index.js");
});

function registeredHandler(link, approvalRunner) {
  const api = makeApi();
  plugin.register(api);
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].channel, "telegram");
  assert.equal(registrations[0].namespace, "texbrif");
  // Swap the real link/runner for fakes by rebuilding through buildHandler.
  return plugin.buildHandler(link, approvalRunner);
}

/**
 * Fake child process for the durable first-stage subprocess
 * (nullone-approval-callback-run.py). Mirrors FakeChild's shape below
 * (stdin/stdout/stderr, 'error'/'close' events) for the same reason: the
 * real Node.ChildProcess contract, not a mock library.
 */
class FakeApprovalChild extends EventEmitter {
  constructor(handleRequest) {
    super();
    this._chunks = [];
    this.stdin = {
      write: (chunk) => {
        this._chunks.push(Buffer.from(chunk));
        return true;
      },
      end: () => {
        setImmediate(() => {
          let request;
          try {
            request = JSON.parse(Buffer.concat(this._chunks).toString("utf8"));
          } catch {
            this.stderr.emit("data", "bad request");
            this.emit("close", 2);
            return;
          }
          let outcome;
          try {
            outcome = handleRequest(request);
          } catch (error) {
            this.stderr.emit("data", String(error));
            this.emit("close", 2);
            return;
          }
          if (outcome && outcome.__spawnError) {
            this.emit("error", outcome.__spawnError);
            return;
          }
          if (outcome && outcome.__hang) {
            return;
          }
          this.stdout.emit("data", JSON.stringify(outcome));
          this.emit("close", 0);
        });
      },
    };
    this.stdout = new EventEmitter();
    this.stderr = new EventEmitter();
  }
  kill() {
    this.killed = true;
  }
}

/**
 * A working fake durable-approval subprocess backed by the SAME pure
 * transition table as the real Python controller (approval-route.js is
 * kept byte-identical to nullone_approval_controller.py -- see
 * test_deterministic_approval.py's parity test), so these offline tests
 * exercise real stage-machine behavior without spawning Python.
 */
function makeApprovalRunner(handleRequestOverride) {
  const store = createApprovalStore();
  const calls = [];
  const handleRequest =
    handleRequestOverride ||
    ((request) => {
      const result = store.handle({
        action: request.action,
        postId: request.review_post_id,
        authorized: request.authorized,
        messageId: request.message_id,
        chatId: request.chat_id,
        accountId: request.account_id,
        senderId: request.sender_id,
      });
      return {
        outcome: result.outcome,
        from_stage: result.fromStage,
        to_stage: result.toStage,
        reply: result.reply,
        publish_authorized: result.publishAuthorized,
        zernio_calls: result.zernioCalls,
      };
    });
  const spawnFn = () =>
    new FakeApprovalChild((request) => {
      calls.push(request);
      return handleRequest(request);
    });
  return {
    runner: {
      pythonBin: "python3",
      runnerPath: "/tmp/nullone-test-workspace/social/ops/scripts/nullone-approval-callback-run.py",
      workspace: "/tmp/nullone-test-workspace",
      spawnFn,
      timeoutMs: 500,
    },
    calls,
    store,
  };
}

/**
 * Fake child for the pending-ledger recovery sweep
 * (nullone_approval_durable.py recover-pending) -- simpler protocol than
 * FakeApprovalChild: no stdin envelope, just stdout + close.
 */
class FakeRecoveryChild extends EventEmitter {
  constructor(behavior) {
    super();
    this.stdin = { write: () => true, end: () => {} };
    this.stdout = new EventEmitter();
    this.stderr = new EventEmitter();
    setImmediate(() => {
      if (behavior && behavior.hang) return;
      if (behavior && behavior.spawnError) {
        this.emit("error", behavior.spawnError);
        return;
      }
      if (behavior && behavior.stderr) this.stderr.emit("data", behavior.stderr);
      this.stdout.emit(
        "data",
        `RECOVERED_COUNT=${(behavior && behavior.recoveredCount) || 0}\n`
      );
      this.emit("close", behavior && behavior.exitCode !== undefined ? behavior.exitCode : 0);
    });
  }
  kill() {
    this.killed = true;
  }
}

function makeRecoverySpawnFn(behavior) {
  const calls = [];
  const spawnFn = (bin, args, opts) => {
    calls.push({ bin, args, opts });
    return new FakeRecoveryChild(behavior);
  };
  return { spawnFn, calls };
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
  plugin.register(api);
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].channel, "telegram");
  assert.equal(registrations[0].namespace, "texbrif");
});

test("registration claims telegram/texbrif exactly once", () => {
  const api = makeApi();
  plugin.register(api);
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].channel, "telegram");
  assert.equal(registrations[0].namespace, "texbrif");
  assert.equal(typeof registrations[0].handler, "function");
});

test("NO_REGISTRATION_COLLISION: loading the real nullone-draft-bridge sibling never produces a second registration", () => {
  // No draftBridge override: register() resolves the REAL sibling package
  // at plugins/nullone-draft-bridge (loadDraftBridge's sibling-path
  // resolution), exactly as production does. Before the fix this ran
  // alongside draft-bridge's OWN register() independently calling
  // registerInteractiveHandler a second time; now draft-bridge never calls
  // it at all (see test_draft_bridge_index.js), so there is only ever one
  // registration to begin with -- this asserts nullone-final-publish's own
  // side of that contract.
  const api = makeApi();
  plugin.register(api);
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].namespace, "texbrif");
});

test("DRAFT_BRIDGE_ACTION_REACHABLE: texbrif:draft:* is delegated to nullone-draft-bridge's own handler", async () => {
  const link = makeLink();
  const { runner } = makeApprovalRunner();
  const draftCalls = [];
  const fakeDraftBridge = {
    routeCallback: (data) => {
      const m = /^texbrif:draft:(.+)$/.exec(data || "");
      return m ? { decision: "draft", manifestId: m[1] } : { decision: "fallthrough" };
    },
    resolveControllerPath: (workspace) => `${workspace}/social/ops/scripts/nullone_draft_bridge_action.py`,
    spawnRunner: () => {
      throw new Error("spawnRunner must not be called when draftSpawnFn override is supplied");
    },
    buildHandler: (draftRunner) => async (handlerCtx) => {
      draftCalls.push({ draftRunner, data: handlerCtx.callback.data });
      await handlerCtx.respond.reply({ text: "✅ Qaralama yaradıldı. İnsan təsdiqi gözlənilir." });
      return { handled: true };
    },
  };
  const fakeDraftRunner = async () => "ACTION_STATUS=COMPLETED\n";
  const api = makeApi();
  plugin.register(api, makeCtx({ draftBridge: fakeDraftBridge, draftSpawnFn: fakeDraftRunner }));
  assert.equal(registrations.length, 1, "still exactly one registration with draftBridge present");
  const handler = registrations[0].handler;
  const ctx = makeHandlerCtx({ callback: {
    data: "texbrif:draft:2026-09-16-example-manifest",
    namespace: "texbrif",
    payload: "draft:2026-09-16-example-manifest",
    messageId: 1,
    chatId: "770011",
  }});
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(draftCalls.length, 1);
  assert.equal(draftCalls[0].data, "texbrif:draft:2026-09-16-example-manifest");
  assert.equal(draftCalls[0].draftRunner, fakeDraftRunner);
  assert.equal(ctx._replies.length, 1);
  assert.match(ctx._replies[0], /Qaralama yaradıldı/);
  // Delegating to draft never touches the publish daemon or the approval runner.
  assert.equal(link.calls.length, 0);
});

test("DRAFT_BRIDGE_ACTION_REACHABLE: a missing draft-bridge runner fails closed via draft-bridge's own unavailable text (not agent flow)", async () => {
  const realDraftBridge = require("../../plugins/nullone-draft-bridge/index.js");
  const api = makeApi();
  // draftControllerPath override that resolveControllerPath rejects (wrong
  // basename) forces draftRunner resolution to fail, exercising the
  // draftBridge.buildHandler(null) fail-closed path with the REAL module.
  plugin.register(api, makeCtx({
    draftBridge: realDraftBridge,
    draftControllerPath: "/tmp/not-the-right-basename.py",
  }));
  const handler = registrations[0].handler;
  const ctx = makeHandlerCtx({ callback: {
    data: "texbrif:draft:2026-09-16-example-manifest",
    namespace: "texbrif",
    payload: "draft:2026-09-16-example-manifest",
    messageId: 1,
    chatId: "770011",
  }});
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(ctx._replies.length, 1);
  assert.match(ctx._replies[0], /Qaralama xidməti hazır deyil/);
});

test("ALL_EXISTING_CALLBACKS_ROUTE_CORRECTLY / FINAL_PUBLISH_ROUTE_REACHABLE: approve/reject/revise/back/publish are unaffected by draft delegation", async () => {
  const realDraftBridge = require("../../plugins/nullone-draft-bridge/index.js");
  const link = makeLink({ reply: { t: "result", outcome: "SETTLED", code: 0, publication_state: "PUBLISHED" } });
  const { runner } = makeApprovalRunner();
  const api = makeApi();
  plugin.register(api, makeCtx({ draftBridge: realDraftBridge }));
  assert.equal(registrations.length, 1);
  // Rebuild with test fakes for link/approvalRunner exactly like
  // registeredHandler() does, but keep the real draftBridge module wired in
  // through the same buildHandler signature registration used.
  const handler = plugin.buildHandler(link, runner, realDraftBridge, async () => "ACTION_STATUS=COMPLETED\n");
  for (const action of ["approve", "reject", "revise", "back"]) {
    const post = "0123456789abcdef01234567";
    const ctx = makeHandlerCtx({ callback: {
      data: `texbrif:${action}:${post}`,
      namespace: "texbrif",
      payload: `${action}:${post}`,
      messageId: 424242,
      chatId: "770011",
    }});
    const result = await handler(ctx);
    assert.deepEqual(result, { handled: true }, action);
    assert.equal(ctx._replies.length, 1, action);
  }
  assert.equal(link.calls.length, 0, "first-stage actions never touch the publish daemon");
  // FINAL_PUBLISH_ROUTE_REACHABLE: publish still reaches the daemon link.
  const publishCtx = makeHandlerCtx();
  const publishResult = await handler(publishCtx);
  assert.deepEqual(publishResult, { handled: true });
  assert.equal(link.calls.length, 1);
  assert.equal(link.calls[0].post_id, POST);
  assert.match(publishCtx._replies[0], /Nəşr tamamlandı/);
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

test("approve/reject/revise/back are consumed deterministically (no agent flow)", async () => {
  const link = makeLink();
  const { runner } = makeApprovalRunner();
  const handler = registeredHandler(link, runner);
  const expectations = {
    approve: /son təsdiqdən sonra/,
    reject: /İmtina edildi/,
    revise: /hansı dəyişikliyi/,
    back: /ləğv edildi/,
  };
  const posts = {
    approve: POST,
    reject: "aaaaaaaaaaaaaaaaaaaaaaaa",
    revise: "bbbbbbbbbbbbbbbbbbbbbbbb",
    back: "cccccccccccccccccccccccc",
  };
  for (const action of ["approve", "reject", "revise", "back"]) {
    const post = posts[action];
    const ctx = makeHandlerCtx({ callback: {
      data: `texbrif:${action}:${post}`,
      namespace: "texbrif",
      payload: `${action}:${post}`,
      messageId: 424242,
      chatId: "770011",
    }});
    const result = await handler(ctx);
    assert.deepEqual(result, { handled: true }, action);
    assert.equal("submitText" in result, false, action);
    assert.equal(ctx._replies.length, 1, action);
    assert.match(ctx._replies[0], expectations[action], action);
  }
  // First-stage control never touches the publish daemon link.
  assert.equal(link.calls.length, 0);
});

test("approve reply carries the second-confirmation button values", async () => {
  const link = makeLink();
  const { runner } = makeApprovalRunner();
  const handler = registeredHandler(link, runner);
  const sent = [];
  const ctx = makeHandlerCtx({ callback: {
    data: `texbrif:approve:${POST}`,
    namespace: "texbrif",
    payload: `approve:${POST}`,
    messageId: 424242,
    chatId: "770011",
  }});
  ctx.respond.reply = async (payload) => {
    sent.push(payload);
    return undefined;
  };
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(sent.length, 1);
  assert.match(sent[0].text, /son təsdiqdən sonra/);
  // Rows-of-rows (P0 row.map fix): the host's buildInlineKeyboard requires
  // an array of rows, each row itself an array of button objects -- a flat
  // array crashes with "row.map is not a function" (confirmed 3x in prod).
  assert.ok(Array.isArray(sent[0].buttons), "approve card must carry buttons");
  assert.ok(
    sent[0].buttons.every((row) => Array.isArray(row)),
    "approve card buttons must be rows-of-rows, not a flat array"
  );
  const values = sent[0].buttons.flat().map((b) => b.value).sort();
  assert.deepEqual(values, [`texbrif:back:${POST}`, `texbrif:publish:${POST}`]);
  assert.equal(link.calls.length, 0);
});

test("APPROVE_REPLY_BUTTONS_ROWS_OF_ROWS: toButtonRows wraps a flat array into one row", () => {
  const flat = [
    { label: "🚀 Paylaş", value: `texbrif:publish:${POST}`, style: "success" },
    { label: "↩️ Geri", value: `texbrif:back:${POST}` },
  ];
  const rows = plugin.toButtonRows(flat);
  assert.deepEqual(rows, [flat]);
  // Already-nested input passes through unchanged (idempotent).
  assert.deepEqual(plugin.toButtonRows(rows), rows);
  // No buttons stays no buttons.
  assert.equal(plugin.toButtonRows(null), null);
  assert.equal(plugin.toButtonRows([]), null);
});

test("APPROVE_REPLY_NO_ROW_MAP_FAILURE: a host-faithful buildInlineKeyboard never throws on the approve reply", async () => {
  const link = makeLink();
  const { runner } = makeApprovalRunner();
  const handler = registeredHandler(link, runner);
  const sent = [];
  // Mirrors the installed host's real bug shape: buildInlineKeyboard expects
  // rows-of-rows and calls row.map on each element it's given.
  const buildInlineKeyboardLikeHost = (buttons) => {
    if (!buttons) return undefined;
    return {
      inline_keyboard: buttons.map((row) =>
        row.map((button) => ({ text: button.label, callback_data: button.value }))
      ),
    };
  };
  const ctx = makeHandlerCtx({ callback: {
    data: `texbrif:approve:${POST}`,
    namespace: "texbrif",
    payload: `approve:${POST}`,
    messageId: 424242,
    chatId: "770011",
  }});
  ctx.respond.reply = async ({ text, buttons }) => {
    const reply_markup = buildInlineKeyboardLikeHost(buttons);
    sent.push({ text, reply_markup });
    return undefined;
  };
  // Must not throw/reject -- this is exactly the call that crashed with
  // "row.map is not a function" in production before the fix.
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].reply_markup.inline_keyboard.length, 1);
  assert.deepEqual(
    sent[0].reply_markup.inline_keyboard[0].map((b) => b.callback_data).sort(),
    [`texbrif:back:${POST}`, `texbrif:publish:${POST}`]
  );
});

test("REJECT_REPLY_UNCHANGED / REVISE_REPLY_UNCHANGED / BACK_REPLY_UNCHANGED: no-button replies are untouched", async () => {
  const link = makeLink();
  const { runner } = makeApprovalRunner();
  const handler = registeredHandler(link, runner);
  const cases = [
    { action: "reject", post: "aaaaaaaaaaaaaaaaaaaaaaaa", expect: /İmtina edildi/ },
    { action: "revise", post: "bbbbbbbbbbbbbbbbbbbbbbbb", expect: /hansı dəyişikliyi/ },
    { action: "back", post: "cccccccccccccccccccccccc", expect: /ləğv edildi/ },
  ];
  for (const { action, post, expect } of cases) {
    const sent = [];
    const ctx = makeHandlerCtx({ callback: {
      data: `texbrif:${action}:${post}`,
      namespace: "texbrif",
      payload: `${action}:${post}`,
      messageId: 424242,
      chatId: "770011",
    }});
    ctx.respond.reply = async (payload) => {
      sent.push(payload);
      return undefined;
    };
    const result = await handler(ctx);
    assert.deepEqual(result, { handled: true }, action);
    assert.equal(sent.length, 1, action);
    assert.match(sent[0].text, expect, action);
    assert.equal("buttons" in sent[0], false, `${action}: no buttons key when there are no buttons`);
  }
});

test("PUBLISH_CALLBACK_UNCHANGED: the second-stage publish reply never carries buttons", async () => {
  const link = makeLink({ reply: { t: "result", outcome: "SETTLED", code: 0, publication_state: "PUBLISHED" } });
  const handler = registeredHandler(link);
  const sent = [];
  const ctx = makeHandlerCtx();
  ctx.respond.reply = async (payload) => {
    sent.push(payload);
    return undefined;
  };
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(sent.length, 1);
  assert.match(sent[0].text, /Nəşr tamamlandı/);
  assert.equal("buttons" in sent[0], false);
});

test("duplicate approve is idempotent with zero daemon contact", async () => {
  const link = makeLink();
  const { runner } = makeApprovalRunner();
  const handler = registeredHandler(link, runner);
  const base = { callback: {
    data: `texbrif:approve:${POST}`,
    namespace: "texbrif",
    payload: `approve:${POST}`,
    messageId: 424242,
    chatId: "770011",
  }};
  const first = makeHandlerCtx(base);
  const second = makeHandlerCtx(base);
  assert.deepEqual(await handler(first), { handled: true });
  assert.deepEqual(await handler(second), { handled: true });
  assert.equal(first._replies.length, 1);
  assert.equal(second._replies.length, 1);
  assert.equal(first._replies[0], second._replies[0]);
  assert.equal(link.calls.length, 0);
});

test("unauthorized first-stage callback is consumed silently", async () => {
  const link = makeLink();
  const handler = registeredHandler(link);
  const ctx = makeHandlerCtx({
    callback: {
      data: `texbrif:approve:${POST}`,
      namespace: "texbrif",
      payload: `approve:${POST}`,
      messageId: 424242,
      chatId: "770011",
    },
    auth: { isAuthorizedSender: false },
  });
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(ctx._replies.length, 0);
  assert.equal(link.calls.length, 0);
});

test("missing/unavailable approval runner fails closed with safe text, no throw", async () => {
  const link = makeLink();
  // approvalRunner intentionally omitted: mirrors a failed registration
  // (workspace/runner path unavailable) -- must never fall back to an
  // in-memory decision.
  const handler = registeredHandler(link);
  const ctx = makeHandlerCtx({
    callback: {
      data: `texbrif:reject:${POST}`,
      namespace: "texbrif",
      payload: `reject:${POST}`,
      messageId: 424242,
      chatId: "770011",
    },
  });
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(ctx._replies.length, 1);
  assert.match(ctx._replies[0], /Təsdiq xidməti hazır deyil/);
});

test("subprocess failure (non-zero exit) fails closed, never claims a transition", async () => {
  const link = makeLink();
  const { runner } = makeApprovalRunner((_request) => {
    throw new Error("simulated internal failure");
  });
  const handler = registeredHandler(link, runner);
  const ctx = makeHandlerCtx({
    callback: {
      data: `texbrif:reject:${POST}`,
      namespace: "texbrif",
      payload: `reject:${POST}`,
      messageId: 424242,
      chatId: "770011",
    },
  });
  const result = await handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(ctx._replies.length, 1);
  assert.match(ctx._replies[0], /nəticəsi qeyri-müəyyəndir|hazır deyil/);
});

test("reply-send failure is observable (logged) and never throws", async () => {
  const link = makeLink();
  const { runner } = makeApprovalRunner();
  const handler = registeredHandler(link, runner);
  const ctx = makeHandlerCtx({
    callback: {
      data: `texbrif:reject:${POST}`,
      namespace: "texbrif",
      payload: `reject:${POST}`,
      messageId: 424242,
      chatId: "770011",
    },
  });
  ctx.respond.reply = async () => {
    throw new Error("Telegram API unreachable");
  };
  const logs = [];
  const originalLog = console.log;
  console.log = (line) => logs.push(line);
  let result;
  try {
    result = await handler(ctx);
  } finally {
    console.log = originalLog;
  }
  // Previously silently swallowed (bare catch {}): now must leave a trace,
  // and must never throw out of the interactive handler either way.
  assert.deepEqual(result, { handled: true });
  const parsed = logs.map((line) => JSON.parse(line));
  const replyFailure = parsed.find(
    (entry) => entry.event === "reply_send_result" && entry.ok === false
  );
  assert.ok(replyFailure, "expected an observable reply_send_result failure log");
  assert.match(replyFailure.error, /Telegram API unreachable/);
});

test("malformed approval-shaped callbacks are consumed safely", async () => {
  const link = makeLink();
  const handler = registeredHandler(link);
  for (const data of [
    "texbrif:approve:",
    "texbrif:reject:ZZZ",
    "texbrif:revise:short",
    `texbrif:back:${POST}:extra`,
  ]) {
    const result = await handler(makeHandlerCtx({ callback: { data } }));
    assert.deepEqual(result, { handled: true }, data);
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
  const api = makeApi({
    pluginConfig: { publishToken: STARTUP_TOKEN },
    workspace: "/tmp/nullone-workspace",
  });
  plugin.register(api, makeCtx({ spawnFn }));
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
  const api = makeApi({ workspace: "" });
  plugin.register(api, makeCtx({ spawnFn }));
  assert.equal(registrations.length, 1);
  const ctx = makeHandlerCtx();
  const result = await registrations[0].handler(ctx);
  assert.deepEqual(result, { handled: true });
  assert.equal(spawned, false);
  assert.equal(ctx._replies.length, 1);
  assert.match(ctx._replies[0], /hazır deyil/);
});

test("D2: resolveAgentWorkspaceDir throwing fails closed with zero spawn", async () => {
  // Negative coverage for the supported workspace API itself: if the
  // installed runtime resolver throws (unavailable agent, config error,
  // etc.) registration must still succeed with link=null, never crash
  // Gateway startup, and never spawn.
  let spawned = false;
  const spawnFn = () => {
    spawned = true;
    throw new Error("must not spawn");
  };
  const api = makeApi({
    resolveAgentWorkspaceDir: () => {
      throw new Error("agent workspace resolver unavailable");
    },
  });
  plugin.register(api, makeCtx({ spawnFn }));
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
  const api = makeApi({
    pluginConfig: { publishToken: "test-token" },
    workspace: "/tmp/nullone-workspace",
  });
  plugin.register(api, makeCtx({ spawnFn }));
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
  const api = makeApi({
    pluginConfig: { publishToken: "test-token" },
    workspace: "/tmp/nullone-workspace",
  });
  plugin.register(api, makeCtx({ spawnFn }));
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
  const api = makeApi({
    pluginConfig: {},
    workspace: "/tmp/nullone-workspace",
  });
  plugin.register(
    api,
    makeCtx({
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
  const api = makeApi({
    pluginConfig: { publishToken: "   " },
    workspace: "/tmp/nullone-workspace",
  });
  plugin.register(
    api,
    makeCtx({
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
    workspace: "/tmp/nullone-workspace",
  });
  plugin.register(
    api,
    makeCtx({
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
    workspace: "/tmp/nullone-workspace",
  });
  plugin.register(api, makeCtx({ spawnFn: () => fake }));
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

test("required SecretRef resolver bare-string result is accepted", async () => {
  // Installed OpenClaw 2026.8.2 contract: resolveRequiredConfiguredSecretRefInputString(...)
  // -> Promise<string | undefined>. Calls the REAL resolvePublishToken() directly
  // (not through the daemon pipe) so this fails against the old `.value` extraction
  // even when the stub already returns the correct bare-string shape.
  const ref = {
    source: "store",
    provider: "default",
    id: "ZERNIO_PUBLISH_API_TOKEN",
  };
  const value = await plugin.resolvePublishToken(ref, { secrets: {} });
  assert.equal(value, "resolved-store-token");
});

test("required SecretRef resolver undefined result fails closed", async () => {
  // The real resolver returns bare `undefined` (never `{unresolvedRefReason}`)
  // when the ref is not configured/resolvable -- resolvePublishToken must
  // still fail closed with zero token exposure.
  const ref = {
    source: "store",
    provider: "default",
    id: "SOME_OTHER_UNCONFIGURED_ID",
  };
  await assert.rejects(
    () => plugin.resolvePublishToken(ref, { secrets: {} }),
    /publish credential unavailable/
  );
});

test("OpenClaw one-argument register resolves workspace from runtime agent API", () => {
  // Installed 2026.8.2 contract: register?: (api: OpenClawPluginApi) => void,
  // invoked by the Gateway loader as register(guarded.api) -- exactly one
  // argument, no ctx (dist/loader-*.js runPluginRegisterSync). Calling
  // plugin.register(api) here with NO second argument at all proves the
  // production path never needs ctx.workspace / NULLONE_WORKSPACE.
  // FAILS against pre-fix main: resolveAgentWorkspaceDir is never called
  // there (workspace instead came from ctx/env, both absent -> link=null).
  const calls = [];
  const testConfig = { secrets: {}, marker: "resolve-workspace-test-config" };
  const api = makeApi({
    config: testConfig,
    resolveAgentWorkspaceDir: (config, agentId) => {
      calls.push({ config, agentId });
      return "/tmp/nullone-test-workspace";
    },
  });

  plugin.register(api);

  assert.equal(calls.length, 1, "resolveAgentWorkspaceDir must be called exactly once");
  assert.equal(calls[0].agentId, "main");
  assert.equal(calls[0].config, testConfig);

  const controllerPath = plugin.resolveControllerPath(
    "/tmp/nullone-test-workspace"
  );
  assert.ok(
    controllerPath.startsWith("/tmp/nullone-test-workspace/"),
    controllerPath
  );
  assert.ok(!controllerPath.includes("workspace/workspace"), controllerPath);

  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].channel, "telegram");
  assert.equal(registrations[0].namespace, "texbrif");
});

test("register() fires the automatic pending-ledger recovery sweep on every call", async () => {
  const api = makeApi();
  const { spawnFn, calls } = makeRecoverySpawnFn({ recoveredCount: 2 });
  const logs = [];
  const originalLog = console.log;
  console.log = (line) => logs.push(line);
  try {
    plugin.register(api, makeCtx({ recoverySpawnFn: spawnFn }));
    // Fire-and-forget: give the microtask/timer queue a turn to settle.
    await new Promise((resolve) => setTimeout(resolve, 20));
  } finally {
    console.log = originalLog;
  }
  assert.equal(calls.length, 1, "recovery sweep must spawn exactly once per register()");
  assert.match(calls[0].args[0], /nullone_approval_durable\.py$/);
  assert.equal(calls[0].args[1], "recover-pending");
  const parsed = logs.map((l) => JSON.parse(l));
  // Matched on the exact recovered_count this test's fake reports, not just
  // event name: other tests' own (unawaited, real-spawn-against-a-fake-path)
  // recovery sweeps can still be settling concurrently and would otherwise
  // be picked up here too.
  const recoveryLog = parsed.find(
    (e) => e.event === "pending_ledger_recovery" && e.recovered_count === 2
  );
  assert.ok(recoveryLog, "expected an observable pending_ledger_recovery log");
  assert.equal(recoveryLog.ok, true);
});

test("register() fires recovery again on plugin reload (a second register() call)", async () => {
  const api = makeApi();
  const { spawnFn, calls } = makeRecoverySpawnFn({ recoveredCount: 0 });
  plugin.register(api, makeCtx({ recoverySpawnFn: spawnFn }));
  plugin.register(api, makeCtx({ recoverySpawnFn: spawnFn }));
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(calls.length, 2, "each register() call (boot, and every reload) sweeps again");
});

test("recovery sweep failure is observable but never throws out of register()", async () => {
  const api = makeApi();
  const { spawnFn } = makeRecoverySpawnFn({ exitCode: 1, stderr: "disk full" });
  const logs = [];
  const originalLog = console.log;
  console.log = (line) => logs.push(line);
  let threw = false;
  try {
    plugin.register(api, makeCtx({ recoverySpawnFn: spawnFn }));
    await new Promise((resolve) => setTimeout(resolve, 20));
  } catch {
    threw = true;
  } finally {
    console.log = originalLog;
  }
  assert.equal(threw, false, "register() must never throw on a recovery sweep failure");
  const parsed = logs.map((l) => JSON.parse(l));
  const recoveryLog = parsed.find(
    (e) =>
      e.event === "pending_ledger_recovery" &&
      typeof e.error === "string" &&
      e.error.includes("disk full")
  );
  assert.ok(recoveryLog, "expected the specific disk-full failure to be logged");
  assert.equal(recoveryLog.ok, false);
  // Approval state safety: a failed sweep must never touch the interactive
  // registration itself -- the handler is still installed normally.
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].channel, "telegram");
});

test("recovery sweep is bounded: a hung subprocess is killed and logged, register() unaffected", async () => {
  const api = makeApi();
  const { spawnFn } = makeRecoverySpawnFn({ hang: true });
  const logs = [];
  const originalLog = console.log;
  console.log = (line) => logs.push(line);
  const start = Date.now();
  try {
    plugin.register(
      api,
      makeCtx({ recoverySpawnFn: spawnFn, recoveryTimeoutMs: 30 })
    );
    // register() itself returns immediately regardless of the sweep timeout.
    assert.ok(Date.now() - start < 25, "register() must not block on the sweep");
    await new Promise((resolve) => setTimeout(resolve, 80));
  } finally {
    console.log = originalLog;
  }
  const parsed = logs.map((l) => JSON.parse(l));
  const recoveryLog = parsed.find(
    (e) =>
      e.event === "pending_ledger_recovery" &&
      typeof e.error === "string" &&
      /timeout/.test(e.error)
  );
  assert.ok(recoveryLog, "expected the specific timeout failure to be logged");
  assert.equal(recoveryLog.ok, false);
});

test("recovery sweep does not use the publish daemon's spawn hook (no interference)", async () => {
  // Regression for the exact bug this test suite caught: sharing ctx.spawnFn
  // between the publish DaemonLink and the recovery sweep broke every
  // existing "spawn must never happen" test built only for the daemon's
  // HMAC handshake shape.
  const api = makeApi();
  let daemonSpawnCalls = 0;
  plugin.register(
    api,
    makeCtx({
      spawnFn: () => {
        daemonSpawnCalls += 1;
        throw new Error("daemon must not spawn in this test");
      },
    })
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(
    daemonSpawnCalls,
    0,
    "the recovery sweep must never invoke ctx.spawnFn (the daemon-only hook)"
  );
});

test("ledger_sync is surfaced in persistence_result logs: flushed", async () => {
  const link = makeLink();
  const { runner } = makeApprovalRunner((request) => {
    return {
      outcome: "TRANSITIONED",
      from_stage: "DRAFT_READY",
      to_stage: "REJECTED",
      reply: { text: "❌ İmtina edildi. Heç nə yayımlanmadı.", buttons: null },
      publish_authorized: false,
      zernio_calls: 0,
      ledger_sync: "flushed",
    };
  });
  const handler = registeredHandler(link, runner);
  const logs = [];
  const originalLog = console.log;
  console.log = (line) => logs.push(line);
  let result;
  try {
    result = await handler(
      makeHandlerCtx({
        callback: { data: `texbrif:reject:${POST}`, messageId: 1, chatId: "770011" },
      })
    );
  } finally {
    console.log = originalLog;
  }
  assert.deepEqual(result, { handled: true });
  const parsed = logs.map((l) => JSON.parse(l));
  const persistLog = parsed.find(
    (e) => e.event === "persistence_result" && e.outcome === "TRANSITIONED"
  );
  assert.ok(persistLog);
  assert.equal(persistLog.ledger_sync, "flushed");
});

test("ledger_sync is surfaced in persistence_result logs: pending", async () => {
  const link = makeLink();
  const { runner } = makeApprovalRunner((request) => {
    return {
      outcome: "TRANSITIONED",
      from_stage: "DRAFT_READY",
      to_stage: "REJECTED",
      reply: { text: "❌ İmtina edildi. Heç nə yayımlanmadı.", buttons: null },
      publish_authorized: false,
      zernio_calls: 0,
      ledger_sync: "pending",
    };
  });
  const handler = registeredHandler(link, runner);
  const logs = [];
  const originalLog = console.log;
  console.log = (line) => logs.push(line);
  const ctx = makeHandlerCtx({
    callback: { data: `texbrif:reject:${POST}`, messageId: 1, chatId: "770011" },
  });
  let result;
  try {
    result = await handler(ctx);
  } finally {
    console.log = originalLog;
  }
  // The human-facing outcome must be identical to the "flushed" case above
  // -- only observability differs. This is the exact requirement: pending
  // audit sync must never change what the human sees.
  assert.deepEqual(result, { handled: true });
  assert.equal(ctx._replies.length, 1);
  assert.equal(ctx._replies[0], "❌ İmtina edildi. Heç nə yayımlanmadı.");
  const parsed = logs.map((l) => JSON.parse(l));
  const persistLog = parsed.find(
    (e) => e.event === "persistence_result" && e.outcome === "TRANSITIONED"
  );
  assert.ok(persistLog);
  assert.equal(persistLog.ledger_sync, "pending");
});
