/**
 * Pure callback-routing decisions for the NullOne final-publish plugin.
 *
 * Dependency-free: importable by the OpenClaw plugin entry AND by the
 * offline Node test suite. No OpenClaw imports here.
 *
 * Telegram legacy `value` callbacks arrive as plain `data` strings with the
 * shape `texbrif:<action>:<POST_ID>`. Only `publish:<24-hex>` enters the
 * deterministic route; every other `texbrif:*` falls through to the existing
 * agent flow (handled:false); malformed publish-shaped callbacks are
 * consumed safely (handled:true, zero subprocess, zero receipt).
 */

const NAMESPACE = "texbrif";
const POST_ID_RE = /^[0-9a-fA-F]{24}$/;
const FALLTHROUGH_ACTIONS = new Set(["approve", "reject", "revise", "back"]);

/**
 * @param {unknown} data raw callback data
 * @returns {{decision: "publish", postId: string} | {decision: "fallthrough"} | {decision: "consume", reason: string}}
 */
function routeCallback(data) {
  if (typeof data !== "string") {
    return { decision: "fallthrough" };
  }
  const trimmed = data.trim();
  if (trimmed.length === 0 || trimmed.length > 256) {
    return { decision: "fallthrough" };
  }
  const separator = trimmed.indexOf(":");
  if (separator < 0) {
    return { decision: "fallthrough" };
  }
  const namespace = trimmed.slice(0, separator);
  const payload = trimmed.slice(separator + 1);
  if (namespace !== NAMESPACE) {
    return { decision: "fallthrough" };
  }
  const actionEnd = payload.indexOf(":");
  const action = actionEnd < 0 ? payload : payload.slice(0, actionEnd);
  if (FALLTHROUGH_ACTIONS.has(action)) {
    return { decision: "fallthrough" };
  }
  if (action !== "publish") {
    // Unknown texbrif:* subcommand: preserve existing agent behavior.
    return { decision: "fallthrough" };
  }
  const postId = actionEnd < 0 ? "" : payload.slice(actionEnd + 1);
  if (!POST_ID_RE.test(postId)) {
    return { decision: "consume", reason: "malformed-post-id" };
  }
  return { decision: "publish", postId: postId.toLowerCase() };
}

/**
 * Canonical JSON shared with the Python controller: UTF-8, recursively
 * sorted keys, no whitespace. Must byte-match Python
 * json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False).
 * @param {unknown} value
 * @returns {string}
 */
function canonicalStringify(value) {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalStringify).join(",")}]`;
  }
  const keys = Object.keys(value).sort();
  const parts = keys.map((k) => `${JSON.stringify(k)}:${canonicalStringify(value[k])}`);
  return `{${parts.join(",")}}`;
}

module.exports = { routeCallback, canonicalStringify, NAMESPACE };
