/**
 * Pure callback-routing decisions for the NullOne draft-bridge plugin.
 *
 * Dependency-free: importable by the OpenClaw plugin entry AND by the
 * offline Node test suite. No OpenClaw imports here.
 *
 * Telegram legacy `value` callbacks arrive as plain `data` strings.
 * Only `texbrif:draft:<MANIFEST_ID>` enters the deterministic route;
 * every other `texbrif:*` callback (including approve/reject/revise/back,
 * publish, and accept) returns handled:false so existing flows are
 * byte-for-byte unchanged. Malformed draft-shaped callbacks are consumed
 * safely (handled:true, zero side effects) so they never reach an agent
 * turn.
 *
 * The manifest-ID rule mirrors the Python action core
 * (`nullone_draft_bridge_action.py`, `MANIFEST_ID_RE`):
 * alphanumerics plus `.`/`_`/`-`, 1..128 chars. The Python core
 * re-validates everything (stem binding, canonical dir, preconditions);
 * this edge check only decides routing.
 */

const NAMESPACE = "texbrif";
const DRAFT_ACTION = "draft";
const MANIFEST_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

/**
 * @param {unknown} data raw callback data
 * @returns {{decision: "draft", manifestId: string} | {decision: "fallthrough"} | {decision: "consume", reason: string}}
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
  if (action !== DRAFT_ACTION) {
    // Unknown or unrelated texbrif:* subcommand (approve, reject, revise,
    // back, publish, accept, ...): preserve existing behavior, never swallow.
    return { decision: "fallthrough" };
  }
  const manifestId = actionEnd < 0 ? "" : payload.slice(actionEnd + 1);
  if (!MANIFEST_ID_RE.test(manifestId)) {
    return { decision: "consume", reason: "malformed-manifest-id" };
  }
  return { decision: "draft", manifestId };
}

module.exports = { routeCallback, NAMESPACE, DRAFT_ACTION, MANIFEST_ID_RE };
