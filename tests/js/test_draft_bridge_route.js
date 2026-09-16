/**
 * Offline tests for the NullOne draft-bridge route (#142).
 *
 * Dependency-free Node asserts against plugins/nullone-draft-bridge/route.js
 * only. No OpenClaw runtime, no Telegram, no network, no model.
 * Run with: node tests/js/test_draft_bridge_route.js
 */
"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  routeCallback,
  NAMESPACE,
  DRAFT_ACTION,
} = require("../../plugins/nullone-draft-bridge/route");

const MID = "2026-09-16-ai-access-equity-gates-pledge-2026-09-16";

test("valid draft callback routes with the manifest id", () => {
  assert.deepEqual(routeCallback(`texbrif:draft:${MID}`), {
    decision: "draft",
    manifestId: MID,
  });
});

test("approval/publish/accept callbacks fall through unchanged", () => {
  for (const data of [
    "texbrif:approve:0123456789abcdef01234567",
    "texbrif:reject:0123456789abcdef01234567",
    "texbrif:revise:0123456789abcdef01234567",
    "texbrif:back:0123456789abcdef01234567",
    "texbrif:publish:0123456789abcdef01234567",
    "texbrif:accept:nullone-acceptance-20260101-000000",
  ]) {
    assert.deepEqual(routeCallback(data), { decision: "fallthrough" }, data);
  }
});

test("malformed draft callbacks are consumed safely", () => {
  for (const data of [
    "texbrif:draft:",
    "texbrif:draft:../escape",
    "texbrif:draft:has space",
    `texbrif:draft:${"x".repeat(200)}`,
  ]) {
    const routed = routeCallback(data);
    assert.equal(routed.decision, "consume", data);
  }
});

test("non-string and foreign-namespace input falls through", () => {
  assert.deepEqual(routeCallback(null), { decision: "fallthrough" });
  assert.deepEqual(routeCallback(42), { decision: "fallthrough" });
  assert.deepEqual(routeCallback("other:draft:abc"), { decision: "fallthrough" });
  assert.deepEqual(routeCallback("no-colons-here"), { decision: "fallthrough" });
});

test("unknown texbrif subcommand falls through", () => {
  assert.deepEqual(routeCallback("texbrif:launch:x"), { decision: "fallthrough" });
});

test("route vocabulary constants", () => {
  assert.equal(NAMESPACE, "texbrif");
  assert.equal(DRAFT_ACTION, "draft");
});

test("manifest id with dots/underscores routes", () => {
  const routed = routeCallback("texbrif:draft:story-frontier-pace.2026-09-15_abc123");
  assert.deepEqual(routed, {
    decision: "draft",
    manifestId: "story-frontier-pace.2026-09-15_abc123",
  });
});
