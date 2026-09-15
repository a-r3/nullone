/**
 * Offline routing tests for the NullOne acceptance plugin (issue #129).
 *
 * Dependency-free Node asserts against route.js only. No OpenClaw runtime,
 * no Telegram, no Gateway, no network. Run with: node tests/js/test_acceptance_route.js
 */
"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  routeCallback,
  NAMESPACE,
  ACCEPT_ACTION,
} = require("../../plugins/nullone-acceptance/route");

const AID = "nullone-acceptance-20260101-000000";

test("valid accept callback is claimed with the acceptance id", () => {
  const routed = routeCallback(`texbrif:accept:${AID}`);
  assert.equal(routed.decision, "accept");
  assert.equal(routed.acceptanceId, AID);
});

test("approval-family and publish callbacks fall through untouched", () => {
  for (const action of ["approve", "reject", "revise", "back", "publish", "weird"]) {
    const routed = routeCallback(`texbrif:${action}:${AID}`);
    assert.equal(routed.decision, "fallthrough", action);
  }
  for (const action of ["approve", "reject", "revise", "back", "publish"]) {
    const routed = routeCallback(`texbrif:${action}:0123456789abcdef01234567`);
    assert.equal(routed.decision, "fallthrough", action);
  }
});

test("wrong namespace or shapeless input is not owned", () => {
  assert.equal(routeCallback("other:accept:abc").decision, "fallthrough");
  assert.equal(routeCallback("no-colon-here").decision, "fallthrough");
  assert.equal(routeCallback("").decision, "fallthrough");
  assert.equal(routeCallback(null).decision, "fallthrough");
  assert.equal(routeCallback(42).decision, "fallthrough");
});

test("ordinary text cannot trigger acceptance", () => {
  for (const text of ["accept", "hə", "okay", "QƏBUL ET", "texbrif accept"]) {
    assert.equal(routeCallback(text).decision, "fallthrough", text);
  }
});

test("malformed accept-shaped callback is consumed safely", () => {
  for (const bad of [
    "texbrif:accept:",
    "texbrif:accept:ZZZ",
    "texbrif:accept:short",
    `texbrif:accept:${AID}:extra`,
    "texbrif:accept:0123456789abcdef01234567",
    "texbrif:accept:nullone-acceptance-2026-1-1",
  ]) {
    const routed = routeCallback(bad);
    assert.equal(routed.decision, "consume", bad);
    assert.equal(routed.reason, "malformed-acceptance-id");
  }
});

test("oversize input falls through safely", () => {
  assert.equal(routeCallback(`texbrif:accept:${"a".repeat(300)}`).decision, "fallthrough");
});

test("namespace and action constants match the publish-plugin convention", () => {
  assert.equal(NAMESPACE, "texbrif");
  assert.equal(ACCEPT_ACTION, "accept");
});
