/**
 * Offline routing tests for the NullOne final-publish plugin (issue #89).
 *
 * Dependency-free Node asserts against route.js only. No OpenClaw runtime,
 * no Telegram, no network. Run with: node tests/js/test_plugin_route.js
 */
"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  routeCallback,
  canonicalStringify,
} = require("../../plugins/nullone-final-publish/route");

const POST = "0123456789abcdef01234567";

test("valid publish callback is claimed with lower-cased post id", () => {
  const routed = routeCallback(`texbrif:publish:${POST}`);
  assert.equal(routed.decision, "publish");
  assert.equal(routed.postId, POST);
  const upper = routeCallback("texbrif:publish:0123456789ABCDEF01234567");
  assert.equal(upper.decision, "publish");
  assert.equal(upper.postId, POST);
});

test("first-stage and control callbacks fall through to agent flow", () => {
  for (const action of ["approve", "reject", "revise", "back"]) {
    const routed = routeCallback(`texbrif:${action}:${POST}`);
    assert.equal(routed.decision, "fallthrough", action);
  }
});

test("unknown texbrif subcommand falls through (never swallowed)", () => {
  assert.equal(routeCallback("texbrif:weird:thing").decision, "fallthrough");
  assert.equal(routeCallback("texbrif:").decision, "fallthrough");
});

test("wrong namespace is not owned", () => {
  assert.equal(routeCallback("other:publish:abc").decision, "fallthrough");
  assert.equal(routeCallback("no-colon-here").decision, "fallthrough");
  assert.equal(routeCallback("").decision, "fallthrough");
  assert.equal(routeCallback(null).decision, "fallthrough");
  assert.equal(routeCallback(42).decision, "fallthrough");
});

test("ordinary text cannot publish", () => {
  for (const text of ["publish", "hə", "okay", "PAYLAŞ", "texbrif publish"]) {
    assert.equal(routeCallback(text).decision, "fallthrough", text);
  }
});

test("malformed publish-shaped callback is consumed safely", () => {
  for (const bad of [
    "texbrif:publish:",
    "texbrif:publish:ZZZ",
    "texbrif:publish:short",
    `texbrif:publish:${POST}:extra`,
    "texbrif:publish:0123456789abcdef0123456",
  ]) {
    const routed = routeCallback(bad);
    assert.equal(routed.decision, "consume", bad);
    assert.equal(routed.reason, "malformed-post-id");
  }
});

test("oversize input falls through safely", () => {
  assert.equal(routeCallback(`texbrif:publish:${"a".repeat(300)}`).decision, "fallthrough");
});

test("canonical JSON is deterministic and whitespace-free", () => {
  const first = canonicalStringify({ b: 1, a: { z: [3, 2], y: "x" } });
  const second = canonicalStringify({ a: { y: "x", z: [3, 2] }, b: 1 });
  assert.equal(first, second);
  assert.ok(!first.includes(" "));
  assert.equal(first, '{"a":{"y":"x","z":[3,2]},"b":1}');
});

test("canonical JSON preserves non-ascii without escaping", () => {
  const out = canonicalStringify({ t: "Paylaş ✅" });
  assert.ok(out.includes("Paylaş ✅"), out);
  assert.ok(!out.includes("\\u"), out);
});
