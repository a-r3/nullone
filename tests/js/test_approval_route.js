/**
 * Offline tests for the deterministic first-stage approval route (P0, #132).
 *
 * Dependency-free Node asserts against approval-route.js only. No OpenClaw
 * runtime, no Telegram, no network, no model.
 * Run with: node tests/js/test_approval_route.js
 */
"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  routeApprovalCallback,
  approvalReply,
  createApprovalStore,
  STAGE_DRAFT_READY,
  STAGE_AWAITING,
  STAGE_REJECTED,
  STAGE_REVISION,
} = require("../../plugins/nullone-final-publish/approval-route");

const POST = "0123456789abcdef01234567";
const OTHER = "aaaaaaaaaaaaaaaaaaaaaaaa";

function ids(overrides) {
  return {
    authorized: true,
    messageId: 424242,
    chatId: "770011",
    accountId: "test-bot-account",
    senderId: "990022",
    ...(overrides || {}),
  };
}

test("approval callbacks are claimed with lower-cased post id", () => {
  for (const action of ["approve", "reject", "revise", "back"]) {
    const routed = routeApprovalCallback(`texbrif:${action}:${POST}`);
    assert.equal(routed.decision, action);
    assert.equal(routed.postId, POST);
  }
  const upper = routeApprovalCallback("texbrif:approve:0123456789ABCDEF01234567");
  assert.equal(upper.decision, "approve");
  assert.equal(upper.postId, POST);
});

test("publish and unknown subcommands are not owned here", () => {
  assert.equal(routeApprovalCallback(`texbrif:publish:${POST}`).decision, "fallthrough");
  assert.equal(routeApprovalCallback("texbrif:weird:thing").decision, "fallthrough");
  assert.equal(routeApprovalCallback("other:approve:abc").decision, "fallthrough");
  assert.equal(routeApprovalCallback("").decision, "fallthrough");
  assert.equal(routeApprovalCallback(null).decision, "fallthrough");
});

test("malformed approval-shaped callbacks are consumed safely", () => {
  for (const bad of [
    "texbrif:approve:",
    "texbrif:reject:ZZZ",
    "texbrif:revise:short",
    `texbrif:back:${POST}:extra`,
  ]) {
    const routed = routeApprovalCallback(bad);
    assert.equal(routed.decision, "consume", bad);
    assert.equal(routed.reason, "malformed-post-id", bad);
  }
});

test("valid approve transitions to second confirmation with button values", () => {
  const store = createApprovalStore();
  const result = store.handle({ action: "approve", postId: POST, ...ids() });
  assert.equal(result.outcome, "TRANSITIONED");
  assert.equal(result.fromStage, STAGE_DRAFT_READY);
  assert.equal(result.toStage, STAGE_AWAITING);
  assert.equal(result.publishAuthorized, false);
  assert.equal(result.zernioCalls, 0);
  assert.match(result.reply.text, /son təsdiqdən sonra/);
  const values = result.reply.buttons.map((b) => b.value).sort();
  assert.deepEqual(values, [`texbrif:back:${POST}`, `texbrif:publish:${POST}`]);
  assert.equal(store.stageFor(POST), STAGE_AWAITING);
});

test("duplicate approve is idempotent", () => {
  const store = createApprovalStore();
  const first = store.handle({ action: "approve", postId: POST, ...ids() });
  const second = store.handle({ action: "approve", postId: POST, ...ids() });
  assert.equal(second.outcome, "DUPLICATE");
  assert.deepEqual(second.reply, first.reply);
  assert.equal(store.stageFor(POST), STAGE_AWAITING);
});

test("reject and revise transition from draft ready", () => {
  const store = createApprovalStore();
  const rej = store.handle({ action: "reject", postId: POST, ...ids() });
  assert.equal(rej.outcome, "TRANSITIONED");
  assert.equal(rej.toStage, STAGE_REJECTED);
  assert.match(rej.reply.text, /İmtina edildi/);
  assert.equal(rej.publishAuthorized, false);
  assert.equal(rej.zernioCalls, 0);

  const store2 = createApprovalStore();
  const rev = store2.handle({ action: "revise", postId: POST, ...ids() });
  assert.equal(rev.outcome, "TRANSITIONED");
  assert.equal(rev.toStage, STAGE_REVISION);
  assert.match(rev.reply.text, /hansı dəyişikliyi/);
});

test("duplicate reject converges without state change", () => {
  const store = createApprovalStore();
  store.handle({ action: "reject", postId: POST, ...ids() });
  const dup = store.handle({ action: "reject", postId: POST, ...ids() });
  assert.equal(dup.outcome, "DUPLICATE");
  assert.equal(store.stageFor(POST), STAGE_REJECTED);
});

test("back returns an awaiting post to the safe view", () => {
  const store = createApprovalStore();
  store.handle({ action: "approve", postId: POST, ...ids() });
  const back = store.handle({
    action: "back",
    postId: POST,
    ...ids({ messageId: 424243 }),
  });
  assert.equal(back.outcome, "TRANSITIONED");
  assert.equal(back.toStage, STAGE_DRAFT_READY);
  assert.match(back.reply.text, /ləğv edildi/);
});

test("approve after terminal reject is a wrong-state rejection", () => {
  const store = createApprovalStore();
  store.handle({ action: "reject", postId: POST, ...ids() });
  const wrong = store.handle({
    action: "approve",
    postId: POST,
    ...ids({ messageId: 424243 }),
  });
  assert.equal(wrong.outcome, "REJECTED_WRONG_STATE");
  assert.equal(wrong.toStage, STAGE_REJECTED);
  assert.equal(wrong.publishAuthorized, false);
  assert.equal(wrong.zernioCalls, 0);
});

test("stale approve on an awaiting post converges to the same card", () => {
  const store = createApprovalStore();
  const first = store.handle({ action: "approve", postId: POST, ...ids() });
  const stale = store.handle({
    action: "approve",
    postId: POST,
    ...ids({ messageId: 111111 }),
  });
  assert.equal(stale.outcome, "CONVERGED");
  assert.deepEqual(stale.reply, first.reply);
  assert.equal(store.stageFor(POST), STAGE_AWAITING);
});

test("posts are tracked independently", () => {
  const store = createApprovalStore();
  store.handle({ action: "reject", postId: POST, ...ids() });
  const other = store.handle({ action: "approve", postId: OTHER, ...ids() });
  assert.equal(other.outcome, "TRANSITIONED");
  assert.equal(other.toStage, STAGE_AWAITING);
  assert.equal(store.stageFor(POST), STAGE_REJECTED);
});

test("unauthorized callbacks never reply and never transition", () => {
  const store = createApprovalStore();
  const result = store.handle({
    action: "approve",
    postId: POST,
    ...ids({ authorized: false }),
  });
  assert.equal(result.outcome, "REJECTED_UNAUTHORIZED");
  assert.equal(result.reply, null);
  assert.equal(store.stageFor(POST), STAGE_DRAFT_READY);
});

test("malformed identity never transitions", () => {
  const store = createApprovalStore();
  for (const bad of [
    ids({ messageId: 0 }),
    ids({ chatId: "" }),
    ids({ accountId: "" }),
    ids({ senderId: "" }),
  ]) {
    const result = store.handle({ action: "approve", postId: POST, ...bad });
    assert.equal(result.outcome, "REJECTED_MALFORMED");
  }
  assert.equal(store.stageFor(POST), STAGE_DRAFT_READY);
});

test("full transition table never authorizes publication or Zernio", () => {
  const stages = [STAGE_DRAFT_READY, STAGE_AWAITING, STAGE_REJECTED, STAGE_REVISION];
  for (const stage of stages) {
    for (const action of ["approve", "reject", "revise", "back"]) {
      const store = createApprovalStore();
      store._stages.set(POST, stage);
      const result = store.handle({ action, postId: POST, ...ids() });
      assert.equal(result.publishAuthorized, false, `${stage}/${action}`);
      assert.equal(result.zernioCalls, 0, `${stage}/${action}`);
      assert.ok(
        ["TRANSITIONED", "CONVERGED", "DUPLICATE", "REJECTED_WRONG_STATE"].includes(result.outcome),
        `${stage}/${action}: ${result.outcome}`
      );
    }
  }
});

test("approvalReply cards match the reviewed first-stage vocabulary", () => {
  const card = approvalReply("approve", POST);
  assert.match(card.text, /son təsdiqdən sonra/);
  assert.equal(card.buttons[0].value, `texbrif:publish:${POST}`);
  assert.equal(card.buttons[1].value, `texbrif:back:${POST}`);
  assert.equal(approvalReply("reject", POST).text, "❌ İmtina edildi. Heç nə yayımlanmadı.");
  assert.match(approvalReply("revise", POST).text, /hansı dəyişikliyi/);
  assert.match(approvalReply("back", POST).text, /ləğv edildi/);
});
