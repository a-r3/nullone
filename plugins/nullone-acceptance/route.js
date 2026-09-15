/**
 * Pure callback-routing decisions for the NullOne acceptance plugin.
 *
 * Dependency-free: importable by the OpenClaw plugin entry AND by the
 * offline Node test suite. No OpenClaw imports here.
 *
 * Telegram legacy `value` callbacks arrive as plain `data` strings.
 * Only `texbrif:accept:<ACCEPTANCE_ID>` enters the deterministic route;
 * every other `texbrif:*` callback (including approve/reject/revise/back
 * and publish) returns handled:false so existing flows are byte-for-byte
 * unchanged. Malformed accept-shaped callbacks are consumed safely
 * (handled:true, zero side effects) so they never reach an agent turn.
 *
 * The acceptance-ID rule mirrors the Python acceptance entrypoint
 * (`nullone_acceptance_run.py`, `ACCEPTANCE_ID_RE`):
 * `nullone-acceptance-YYYYMMDD-HHMMSS`. The Python action core
 * re-validates everything; this edge check only decides routing.
 */

const NAMESPACE = "texbrif";
const ACCEPT_ACTION = "accept";
const ACCEPTANCE_ID_RE = /^nullone-acceptance-\d{8}-\d{6}$/;

/**
 * @param {unknown} data raw callback data
 * @returns {{decision: "accept", acceptanceId: string} | {decision: "fallthrough"} | {decision: "consume", reason: string}}
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
  if (action !== ACCEPT_ACTION) {
    // Unknown or unrelated texbrif:* subcommand (approve, reject, revise,
    // back, publish, ...): preserve existing behavior, never swallow.
    return { decision: "fallthrough" };
  }
  const acceptanceId = actionEnd < 0 ? "" : payload.slice(actionEnd + 1);
  if (!ACCEPTANCE_ID_RE.test(acceptanceId)) {
    return { decision: "consume", reason: "malformed-acceptance-id" };
  }
  return { decision: "accept", acceptanceId };
}

module.exports = { routeCallback, NAMESPACE, ACCEPT_ACTION, ACCEPTANCE_ID_RE };
