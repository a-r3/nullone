/**
 * Deterministic first-stage approval/control route (P0, issue #132).
 *
 * Dependency-free: importable by the OpenClaw plugin entry AND by the
 * offline Node test suite. No OpenClaw imports, no network, no model.
 *
 * Handles `texbrif:approve|reject|revise|back:<POST_ID>` deterministically:
 * authenticated validation -> current-stage validation -> deterministic
 * state transition -> deterministic reply card -> STOP. `publish` is NEVER
 * handled here (owned exclusively by the deterministic final-publish
 * controller path, #89).
 *
 * Authorization comes ONLY from the caller's `authorized` boolean, which
 * the plugin entry sets from trusted runtime data
 * (`handlerCtx.auth.isAuthorizedSender`). A wrapper-written
 * `source="approval"` string is never consulted.
 *
 * Button vocabulary mirrors the reviewed legacy contract
 * (agents/approval/AGENTS.md: `label`/`value`/`style`, legacy `value`
 * transport). Whether the live host renders `respond.reply` buttons is
 * verified at controlled deployment; the text alone stays truthful.
 */

"use strict";

const POST_ID_RE = /^[0-9a-fA-F]{24}$/;
const MAX_ID_LEN = 128;
const MAX_SEEN_KEYS = 2000;

const STAGE_DRAFT_READY = "DRAFT_READY";
const STAGE_AWAITING = "AWAITING_PUBLISH_CONFIRMATION";
const STAGE_REJECTED = "REJECTED";
const STAGE_REVISION = "REVISION_REQUESTED";

const STAGES = new Set([
  STAGE_DRAFT_READY,
  STAGE_AWAITING,
  STAGE_REJECTED,
  STAGE_REVISION,
]);

const ACTION_APPROVE = "approve";
const ACTION_REJECT = "reject";
const ACTION_REVISE = "revise";
const ACTION_BACK = "back";

const APPROVAL_ACTIONS = new Set([
  ACTION_APPROVE,
  ACTION_REJECT,
  ACTION_REVISE,
  ACTION_BACK,
]);

const TEXT_APPROVE_CARD =
  "Rauf, bu NullOne draftı son təsdiqdən sonra Instagram-da yayımlanacaq.";
const TEXT_REJECTED = "❌ İmtina edildi. Heç nə yayımlanmadı.";
const TEXT_REVISE = "Rauf, hansı dəyişikliyi istəyirsən?";
const TEXT_BACK_SAFE = "Yayım ləğv edildi. Draft dəyişmədən saxlanıldı.";
const TEXT_WRONG_STATE =
  "⛔ Bu sorğu cari mərhələ üçün keçərli deyil. Heç nə dəyişmədi.";
const TEXT_MALFORMED = "⛔ Sorğu qəbul edilmədi. Heç nə dəyişmədi.";
const TEXT_EXPIRED =
  "⏳ Bu sorğunun redaksiya günü keçib. Heç nə yayımlanmadı.";

// (stage, action) -> [toStage, converged]. Missing entries are
// wrong-state rejections. `converged=true` affirms without moving state.
const TRANSITIONS = new Map([
  [`${STAGE_DRAFT_READY}|${ACTION_APPROVE}`, [STAGE_AWAITING, false]],
  [`${STAGE_DRAFT_READY}|${ACTION_REJECT}`, [STAGE_REJECTED, false]],
  [`${STAGE_DRAFT_READY}|${ACTION_REVISE}`, [STAGE_REVISION, false]],
  [`${STAGE_DRAFT_READY}|${ACTION_BACK}`, [STAGE_DRAFT_READY, true]],
  [`${STAGE_AWAITING}|${ACTION_APPROVE}`, [STAGE_AWAITING, true]],
  [`${STAGE_AWAITING}|${ACTION_REJECT}`, [STAGE_REJECTED, false]],
  [`${STAGE_AWAITING}|${ACTION_REVISE}`, [STAGE_REVISION, false]],
  [`${STAGE_AWAITING}|${ACTION_BACK}`, [STAGE_DRAFT_READY, false]],
  [`${STAGE_REJECTED}|${ACTION_REJECT}`, [STAGE_REJECTED, true]],
  [`${STAGE_REJECTED}|${ACTION_BACK}`, [STAGE_REJECTED, true]],
  [`${STAGE_REVISION}|${ACTION_REVISE}`, [STAGE_REVISION, true]],
  [`${STAGE_REVISION}|${ACTION_BACK}`, [STAGE_REVISION, true]],
]);

/**
 * Route one raw callback data string.
 * @param {unknown} data
 * @returns {{decision: string, postId?: string, reason?: string}}
 *   decision is one of approve|reject|revise|back (claimed),
 *   fallthrough (not an approval callback; publish + unknown keep their
 *   existing routing), or consume (malformed approval-shaped).
 */
function routeApprovalCallback(data) {
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
  if (trimmed.slice(0, separator) !== "texbrif") {
    return { decision: "fallthrough" };
  }
  const payload = trimmed.slice(separator + 1);
  const actionEnd = payload.indexOf(":");
  const action = actionEnd < 0 ? payload : payload.slice(0, actionEnd);
  if (!APPROVAL_ACTIONS.has(action)) {
    return { decision: "fallthrough" };
  }
  const postId = actionEnd < 0 ? "" : payload.slice(actionEnd + 1);
  if (!POST_ID_RE.test(postId)) {
    return { decision: "consume", reason: "malformed-post-id" };
  }
  return { decision: action, postId: postId.toLowerCase() };
}

function isNonEmptyId(value) {
  return (
    typeof value === "string" && value.length > 0 && value.length <= MAX_ID_LEN
  );
}

function isPositiveMessageId(value) {
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value > 0;
  }
  if (typeof value === "string" && /^[0-9]+$/.test(value)) {
    try {
      return BigInt(value) > 0n;
    } catch {
      return false;
    }
  }
  return false;
}

/**
 * Stable replay/duplicate identity for one callback delivery.
 */
function approvalKey({ action, postId, messageId, chatId, senderId }) {
  return [action, postId, String(messageId), String(chatId), String(senderId)].join("|");
}

/**
 * Deterministic reply card for an accepted control callback.
 */
function approvalReply(action, postId) {
  if (action === ACTION_APPROVE) {
    return {
      text: TEXT_APPROVE_CARD,
      buttons: [
        {
          label: "🚀 Paylaş",
          value: `texbrif:publish:${postId}`,
          style: "success",
        },
        { label: "↩️ Geri", value: `texbrif:back:${postId}` },
      ],
    };
  }
  if (action === ACTION_REJECT) {
    return { text: TEXT_REJECTED, buttons: null };
  }
  if (action === ACTION_REVISE) {
    return { text: TEXT_REVISE, buttons: null };
  }
  return { text: TEXT_BACK_SAFE, buttons: null };
}

/**
 * Per-process deterministic approval store (stage per post + replay keys).
 * Bounded: oldest replay keys are evicted past MAX_SEEN_KEYS.
 */
function createApprovalStore() {
  const stages = new Map();
  const seen = new Set();
  return {
    stageFor(postId) {
      return stages.get(postId) || STAGE_DRAFT_READY;
    },
    _stages: stages,
    _seen: seen,
    handle({ action, postId, authorized, messageId, chatId, accountId, senderId, reviewExpired }) {
      if (authorized !== true) {
        return {
          outcome: "REJECTED_UNAUTHORIZED",
          fromStage: this.stageFor(postId),
          toStage: this.stageFor(postId),
          reply: null, // unauthorized: silent, mirroring the publish route
          publishAuthorized: false,
          zernioCalls: 0,
        };
      }
      if (reviewExpired === true) {
        // P0 #140: stale card from a prior editorial date. Fail closed
        // before duplicate tracking: no transition, no seen mutation,
        // no second confirmation. Mirrors the Python controller.
        const stage = this.stageFor(typeof postId === "string" ? postId.toLowerCase() : postId);
        return {
          outcome: "REJECTED_EXPIRED",
          fromStage: stage,
          toStage: stage,
          reply: { text: TEXT_EXPIRED, buttons: null },
          publishAuthorized: false,
          zernioCalls: 0,
        };
      }
      if (
        !APPROVAL_ACTIONS.has(action) ||
        typeof postId !== "string" ||
        !POST_ID_RE.test(postId) ||
        !isPositiveMessageId(messageId) ||
        !isNonEmptyId(typeof chatId === "number" ? String(chatId) : chatId) ||
        !isNonEmptyId(typeof accountId === "number" ? String(accountId) : accountId) ||
        !isNonEmptyId(typeof senderId === "number" ? String(senderId) : senderId)
      ) {
        return {
          outcome: "REJECTED_MALFORMED",
          fromStage: STAGE_DRAFT_READY,
          toStage: STAGE_DRAFT_READY,
          reply: { text: TEXT_MALFORMED, buttons: null },
          publishAuthorized: false,
          zernioCalls: 0,
        };
      }
      const post = postId.toLowerCase();
      const fromStage = this.stageFor(post);
      const key = approvalKey({
        action,
        postId: post,
        messageId,
        chatId,
        senderId,
      });
      if (seen.has(key)) {
        return {
          outcome: "DUPLICATE",
          fromStage,
          toStage: fromStage,
          reply: approvalReply(action, post),
          publishAuthorized: false,
          zernioCalls: 0,
        };
      }
      const transition = TRANSITIONS.get(`${fromStage}|${action}`);
      if (!transition) {
        return {
          outcome: "REJECTED_WRONG_STATE",
          fromStage,
          toStage: fromStage,
          reply: { text: TEXT_WRONG_STATE, buttons: null },
          publishAuthorized: false,
          zernioCalls: 0,
        };
      }
      const [toStage, converged] = transition;
      stages.set(post, toStage);
      seen.add(key);
      if (seen.size > MAX_SEEN_KEYS) {
        const oldest = seen.values().next().value;
        seen.delete(oldest);
      }
      return {
        outcome: converged ? "CONVERGED" : "TRANSITIONED",
        fromStage,
        toStage,
        reply: approvalReply(action, post),
        publishAuthorized: false,
        zernioCalls: 0,
      };
    },
  };
}

module.exports = {
  routeApprovalCallback,
  approvalReply,
  approvalKey,
  createApprovalStore,
  isNonEmptyId,
  isPositiveMessageId,
  STAGE_DRAFT_READY,
  STAGE_AWAITING,
  STAGE_REJECTED,
  STAGE_REVISION,
  ACTION_APPROVE,
  ACTION_REJECT,
  ACTION_REVISE,
  ACTION_BACK,
  TEXT_APPROVE_CARD,
  TEXT_REJECTED,
  TEXT_REVISE,
  TEXT_BACK_SAFE,
  TEXT_WRONG_STATE,
  TEXT_MALFORMED,
  TEXT_EXPIRED,
};
