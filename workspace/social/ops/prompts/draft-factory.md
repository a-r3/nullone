Run one NullOne PRODUCTION cycle.

Read:
- AGENTS.md
- social/CONTENT_STRATEGY.md
- social/STATE_RULES.md
- social/OPERATING_SYSTEM.md
- social/SCORING.md
- social/CONTENT_RULES.md
- social/ZERNIO.md
- social/references/visual-rules.md
- social/state/candidate-queue.md
- social/state/topic-ledger.jsonl
- social/state/publish-ledger.jsonl

MODE:
DRAFT_FIRST.

ABSOLUTELY DO NOT PUBLISH OR SCHEDULE.

TASK:

1. Inspect:
- READY candidates
- today's editorial board if present:
  social/research/daily/YYYY-MM-DD-editorial-board.md
- today's existing main Feed/Carousel drafts
- today's published main posts

2. Determine today's MAIN production count.

Normal target:
2 main Feed/Carousel pieces.

Existing active drafts awaiting approval count toward today's production load
so NullOne does not flood Rauf with unnecessary main drafts.

If today's main count is below 2:
select AT MOST ONE strongest READY candidate by EDITORIAL VALUE.

If today's main count is already 2:
continue ONLY for a genuinely BREAKING, materially new, high-confidence
development.

Otherwise:
return NO_REPLY.

The 20:45 Factory run is primarily:
- a recovery opportunity when fewer than 2 strong main pieces exist
- OR an exceptional breaking-news opportunity

It is NOT an automatic third-post slot.

Selection must consider:

- score
- audience usefulness
- consequence
- source confidence
- timeliness appropriate to CONTENT_TYPE
- distinctiveness
- TOPIC_CLUSTER saturation
- rolling content-type balance
- format suitability

Freshness alone must NOT determine the winner.

A strong EXPLAINER, PRACTICAL, COMPARISON, AZ_CONTEXT or EVERGREEN candidate
may outrank mediocre NEWS.

Do not lower standards to hit cadence.

Default:
maximum 2 main pieces from the same TOPIC_CLUSTER within 7 days unless a
material new development exists.

If no candidate is strong enough:
do nothing and return NO_REPLY.

3. Re-open and verify the PRIMARY source.
Verify the exact final Azerbaijani wording after drafting.

4. Apply:
VERIFICATION: PASS
or
VERIFICATION: BLOCKED

BLOCKED content cannot continue.

5. Determine best format.

CURRENT AUTOMATED PRODUCTION SUPPORT:

A) SINGLE-IMAGE FEED:
Fully supported.

- prepare final plain-text Azerbaijani caption
- exact feed asset 1080×1350
- use official/source media
- render using NullOne renderer
- validate exact dimensions locally
- upload via Zernio presigned media flow
- validate media
- validate complete post payload
- create exactly ONE Zernio draft
- isDraft: true
- NO publishNow
- NO scheduledFor
- GET it again and confirm status=draft and publishAttempts=0

B) CAROUSEL:
Do NOT create a Zernio post yet.
Prepare full slide copy and production specification under
social/drafts/production/.
Mark:
NEEDS_CAROUSEL_RENDERER

C) STORY:
Prepare the exact 1080×1920 asset/spec locally,
but do NOT create a Zernio Story yet until the Story publishing flow
has passed its first controlled manual integration test.

D) REEL:
Prepare concept/source-footage plan only.
Do not create/publish.

6. For a successful single-feed Zernio draft:
- update candidate status to DRAFTED
- append a safe record to topic-ledger.jsonl
- never include API keys
- store production report under:

social/publisher/YYYY-MM-DD-<short-topic>-draft.md

Include:
- score
- sources
- final verification
- headline
- caption
- local asset
- exact dimensions
- Zernio media URL
- Zernio post ID
- status
- publishAttempts

Never create a second draft for the same candidate.

## Production efficiency rules

Production is NOT a discovery cycle.

When a READY candidate has already been selected:

- Do NOT run broad web searches.
- Do NOT inspect reference Instagram accounts.
- Do NOT browse news sites for alternative stories.
- Do NOT rescan the ecosystem.
- Do NOT reread unrelated analytics/history.

Use only:
1. the selected READY candidate;
2. its primary source;
3. the minimum current NullOne content/visual rules required for production.

FACT CHECK:
- Open/fetch the primary source once where practical.
- Verify only the claims actually used in the final post.
- Do not copy or retain long source-page text.
- Keep verification notes compact.

MEDIA:
- Prefer an official/source asset already exposed by the primary source.
- Do not search through many alternative images unless the first usable official
  asset fails the visual requirements.

ZERNI0:
Perform only the required sequence:
- local dimension validation
- presign/upload
- media validation
- post validation
- create one draft
- one read-back confirmation

Do not repeatedly GET the same resource unless necessary.

REPORTING:
Keep production reports concise.
Do not reproduce full research histories.
Record only:
- candidate
- score
- source
- verified claims
- headline
- caption
- asset
- dimensions
- validation
- Zernio ID/status

The target is a small, deterministic production run.
Editorial discovery belongs to Morning Editorial and Breaking Radar.

## Feed renderer — current production standard

For SINGLE-IMAGE FEED posts:

DEFAULT renderer:
social/tools/render_texbrif_v2.py

The old:
social/tools/render_texbrif.py

is LEGACY and must not be used for new production unless V2 technically fails.

Source visual quality matters:
- prefer clean official product/person/screenshot imagery
- avoid text-heavy English source cards when a cleaner official visual exists
- a technically valid source asset is not automatically editorially suitable

Final asset must:
- be exactly 1080x1350
- use the V2 editorial hierarchy
- pass local dimension validation
- pass Zernio media validation

## Carousel production — current standard

CAROUSEL production is now fully supported.

DEFAULT renderer:
social/tools/render_carousel_v2.py

LEGACY:
social/tools/render_carousel.py

Do not use the legacy carousel renderer for new production unless V2
technically fails.

Supported V2 slide roles:
- cover
- stat
- explainer
- comparison
- limitation
- final

Carousel rules:
- 2–10 slides
- normally 5–8 slides
- exactly 1080x1350 every slide
- one primary idea per slide
- use visual rhythm; do not repeat one identical composition
- strong numbers become STAT slides
- comparisons become COMPARISON slides
- restrictions/safeguards become LIMITATION slides
- final slide must answer WHY IT MATTERS
- Signal Orange is the current NullOne primary accent
- body copy must remain comfortably readable on a phone

For a verified READY carousel candidate the factory may autonomously:
1. create the slide spec
2. render V2 slides
3. validate exact dimensions
4. upload each slide via Zernio presigned flow
5. validate each media item
6. validate the complete carousel payload
7. create exactly ONE Zernio draft
8. read back and verify draft status

It MUST NOT publish or schedule.

Use a unique x-request-id for carousel draft creation.

## Zernio integration — mandatory MCP path

Use the official Zernio MCP server through OAuth.

DO NOT use:
- ZERNIO_API_KEY
- authenticated curl to Zernio
- legacy secret-egress REST calls

Use Zernio MCP tools for all authenticated Zernio operations.

If a required API capability is not among the visible core tools:
1. use zernio search_tools to discover the exact API tool;
2. inspect its schema;
3. use zernio call_tool only for that required operation.

Never use dynamic tool discovery to publish, delete, message, comment,
run ads, or perform unrelated writes.

Current mode remains DRAFT_FIRST.

## Mandatory Zernio path

Use official Zernio MCP OAuth only.

Never use:
- ZERNIO_API_KEY
- authenticated curl to Zernio
- secret-egress REST integration

For local assets:
MCP presign -> direct PUT to presigned URL -> MCP validation -> MCP draft creation.

If an MCP capability is unavailable, STOP as BLOCKED instead of falling back
to legacy authentication.

DRAFT_FIRST remains mandatory.

## Story production V2

For Instagram Stories use:
social/tools/render_story_v2.py

Select layout deliberately:

1. comparison
   when two comparable verified metrics form the story.

2. big-stat
   when one verified number is clearly the strongest hook.

3. breaking
   only for genuinely time-sensitive news; source imagery strongly preferred.

4. explainer
   when context/understanding is more important than a number.

Do not default every Story to the same layout.

Before rendering:
- identify the story's visual anchor;
- identify exact primary-source wording;
- identify whether suitable official/source imagery exists.

If a strong official/source visual exists, use it unless the statistical or
comparison visualization is objectively clearer.

All Story assets:
1080x1920 exactly.

The final wording inside the image requires the same VERIFICATION: PASS
standard as feed/carousel captions.

Do not broaden benchmark, availability, rollout, capability, price or
performance claims to make the Story more dramatic.

# FINAL NULLONE PRODUCTION CONTRACT

This section overrides older conflicting production instructions.

## Integration

Use official Zernio MCP OAuth only.

Never use:
- ZERNIO_API_KEY
- raw authenticated Zernio REST
- protected-secret egress for Zernio

Dynamic zernio call_tool targets are restricted to:
- media_get_media_presigned_url
- posts_create_post

Any other dynamic target = BLOCKED.

## Production is not discovery

Start from the strongest READY candidate already present in the candidate queue.

Do not perform a broad news scan inside Draft Factory.

Before rendering:
1. read existing candidate evidence;
2. recheck only the necessary primary source;
3. perform final exact-wording verification;
4. require VERIFICATION: PASS.

Create at most ONE new Zernio draft per factory run.

Avoid duplicates by consulting:
- candidate queue
- topic ledger
- publish ledger
- existing Zernio drafts

## Format selection

FEED
- one primary message
- renderer: social/tools/render_texbrif_v2.py
- exactly 1080x1350

CAROUSEL
- only when multiple slides materially improve understanding
- renderer: social/tools/render_carousel_v2.py
- every slide exactly 1080x1350
- normally 5-8 slides
- never article-screenshot carousels

STORY
- renderer: social/tools/render_story_v2.py
- exactly 1080x1920
- comparison: two genuinely comparable verified metrics
- big-stat: one dominant verified number
- breaking: genuinely time-sensitive only
- explainer: context/understanding is primary

Story creation:
platformSpecificData.contentType = "story"
is_draft = true

## Visual hierarchy

Preferred order:
1. official/source visual
2. useful screenshot or screen recording
3. NullOne editorial/data visualization
4. reusable owned asset
5. generated imagery only when materially useful

Never generate decorative AI imagery merely to fill space.

Never stretch source media or destructively crop the subject.

## Fact safety

Compare every final factual phrase appearing in:
- image
- carousel slides
- Story
- caption

against the source.

Do not broaden:
- benchmarks
- capabilities
- price
- rollout
- availability
- geography
- subscription tier
- partnership
- dates
- performance

Preserve distinctions:
can / will / plans / testing / preview / available.

Numbers require exact source support.

Primary source wins when evidence conflicts.

Final verification:
VERIFICATION: PASS
or
VERIFICATION: BLOCKED

Only PASS may become a draft.

## Production media flow

local asset
-> MCP presign
-> direct unauthenticated PUT
-> MCP validate_media
-> MCP validate_post where applicable
-> create exactly ONE draft
-> posts_get read-back verification

Never persist presigned uploadUrl values in reports.

## Absolute prohibition

Draft Factory MUST NEVER:
- publish
- schedule
- delete
- unpublish
- retry publication
- answer comments
- send messages
- run ads

If safe draft creation cannot be completed:
write BLOCKED and stop.

## TELEGRAM APPROVAL DELIVERY — MANDATORY

After a new Zernio draft is successfully created and read-back verification
passes:

1. Read the Telegram owner ID from:
   social/ops/private/telegram-owner-id

2. Send the completed media preview to that Telegram user using account:
   texbrif

3. Use the existing PUBLIC media URLs from the successful Zernio media flow.
   Never send or persist presigned uploadUrl values.

### Feed

Send the final image preview.

### Story

Send the final 1080x1920 Story preview.

### Carousel

Send every final slide in exact order.

Label slides:
1/N
2/N
...
N/N

Do not substitute the contact sheet for the actual publication slides.

### Approval card

After media preview, send one final Telegram message:

"📰 Yeni NullOne draft

Mövzu: <TOPIC>
Format: <FEED/CAROUSEL/STORY>
Verification: PASS
Post ID: <EXACT_POST_ID>

Yayımlama qərarını seç:"

Attach inline callback buttons:

✅ Təsdiq et
callback value:
texbrif:approve:<POST_ID>

❌ İmtina et
callback value:
texbrif:reject:<POST_ID>

📝 Dəyişiklik istə
callback value:
texbrif:revise:<POST_ID>

Use Telegram account:
texbrif

Use the numeric owner ID from:
social/ops/private/telegram-owner-id

Never expose the approval card for VERIFICATION: BLOCKED content.

If Telegram delivery fails:
- do NOT publish
- keep the Zernio draft
- record NOTIFY_FAILED
- retry notification at most once
- do not create a duplicate draft merely because notification failed

Telegram delivery failure is not publication failure.

## APPROVAL BUTTON TRANSPORT — MANDATORY

When sending the Telegram approval card:

Do NOT use ask_user or native/transient approval actions.

Use the Telegram message tool with ordinary callback buttons:

{
  "type": "buttons",
  "buttons": [
    {
      "label": "✅ Təsdiq et",
      "action": {
        "type": "callback",
        "value": "texbrif:approve:<POST_ID>"
      }
    },
    {
      "label": "❌ İmtina et",
      "action": {
        "type": "callback",
        "value": "texbrif:reject:<POST_ID>"
      }
    },
    {
      "label": "📝 Dəyişiklik istə",
      "action": {
        "type": "callback",
        "value": "texbrif:revise:<POST_ID>"
      }
    }
  ]
}

Never create an ephemeral approval/action ID for NullOne.

## TELEGRAM CALLBACK COMPATIBILITY — FINAL

For NullOne Telegram approval cards use legacy button `value`, not typed
`action.type=callback`.

Required first-stage presentation:

{
  "blocks": [
    {
      "type": "buttons",
      "buttons": [
        {
          "label": "✅ Təsdiq et",
          "value": "texbrif:approve:<POST_ID>",
          "style": "success"
        },
        {
          "label": "❌ İmtina et",
          "value": "texbrif:reject:<POST_ID>",
          "style": "danger"
        },
        {
          "label": "📝 Dəyişiklik istə",
          "value": "texbrif:revise:<POST_ID>"
        }
      ]
    }
  ]
}

Do not use typed callback action objects for this workflow on the current
OpenClaw runtime.

# PRODUCTION BRIDGE V1 — HIGHEST PRIORITY OVERRIDE

This section overrides every older conflicting Zernio production instruction
in this prompt.

## Architecture

Draft Factory owns:

- candidate selection
- primary-source verification
- final Azerbaijani copy
- format decision
- rendering
- local dimension validation
- immutable production manifest creation
- local Draft Bridge invocation
- Telegram preview delivery

Draft Factory does NOT directly call Zernio MCP.

Do NOT use:
- zernio__*
- Zernio MCP tools
- Zernio REST
- authenticated curl
- posts_create
- posts_get
- validate_media
- validate_post
- presign through the OpenClaw tool catalog

The local Production Bridge owns Zernio transport.

## Required output files

For every production candidate that reaches VERIFICATION: PASS:

1. Write the exact final approved caption to a dedicated UTF-8 text file:

social/drafts/production/YYYY-MM-DD-<slug>-caption.txt

This file is immutable after manifest creation.

2. Render the final media asset(s).

3. Validate dimensions locally.

4. Build a production manifest using:

python3 social/ops/scripts/nullone-manifest.py build \
  --candidate-id "<CANDIDATE_ID>" \
  --topic "<TOPIC>" \
  --topic-cluster "<TOPIC_CLUSTER>" \
  --content-type "<CONTENT_TYPE>" \
  --format "<FEED|CAROUSEL|STORY>" \
  --caption-file "<CAPTION_FILE>" \
  --media "<MEDIA_FILE>"

For CAROUSEL:
repeat --media in exact slide order.

Do not use --force.

Manifest verification must return PASS.

## Draft Bridge

After manifest creation, invoke exactly once:

python3 social/ops/scripts/nullone-draft-bridge.py execute <MANIFEST_PATH>

Do not invoke it twice.

If the bridge returns:

DRAFT_BRIDGE=PASS

record:
- manifest path
- review_post_id
- review state

If bridge returns BLOCKED before a create attempt:
record BLOCKED and stop.

If manifest review.create_attempts == 1 and state is:
- REVIEW_UNKNOWN
- CREATE_IN_FLIGHT
- DRAFT_CREATED

never create another review draft automatically.

## Telegram delivery

Only after Draft Bridge reports DRAFT_CREATED:

1. Read Telegram owner ID from:

social/ops/private/telegram-owner-id

2. Send the final media preview using Telegram account:

texbrif

3. Send exactly one approval card:

"📰 Yeni NullOne draft

Mövzu: <TOPIC>
Format: <FORMAT>
Verification: PASS
Post ID: <REVIEW_POST_ID>

Yayımlama qərarını seç:"

Use legacy callback values only:

✅ Təsdiq et
value:
texbrif:approve:<REVIEW_POST_ID>

❌ İmtina et
value:
texbrif:reject:<REVIEW_POST_ID>

📝 Dəyişiklik istə
value:
texbrif:revise:<REVIEW_POST_ID>

Do not use typed callback action objects.

Do not create duplicate approval cards.

## Production report

Write:

social/publisher/YYYY-MM-DD-<slug>-draft.md

Include:

- candidate
- content type
- score
- sources
- verification
- format
- caption file
- media file(s)
- dimensions
- production manifest path
- review Zernio draft ID
- review state
- Telegram notification state

Do NOT persist:
- presigned upload URLs
- OAuth tokens
- secrets

Public media URL may remain only inside the private production manifest.

## State update

Only after:
- manifest creation PASS
- Draft Bridge DRAFT_CREATED
- Telegram preview successfully delivered

mark candidate:

DRAFTED

If Telegram notification fails:
- keep review draft
- keep manifest
- record NOTIFY_FAILED
- do not create another Zernio draft
- retry Telegram notification at most once

## Absolute publication boundary

Draft Factory must NEVER:

- call nullone-publish-bridge.py
- call nullone-publisher-run.py
- mark final_publish=true
- publish
- schedule
- retry publication

Only the two-stage Telegram approval route may reach Publish Bridge.


# LEGACY STATE ACCOUNTING — HIGHEST PRIORITY

For daily main-post load and candidate selection:

- PUBLISHED -> exclude
- LEGACY_DRAFT -> exclude and do NOT count as active
- SUPERSEDED_DRAFT -> exclude and do NOT count as active
- historical queue-only DRAFTED without a current Production Bridge manifest
  -> exclude from selection and do NOT count as active

Only current Production Bridge manifests with:
review.state = DRAFT_CREATED

count as active approval drafts.

Do not resurrect an old Zernio draft merely because its old scheduled date
has passed.

Do not create a replacement draft for a LEGACY_DRAFT automatically.

# PUBLIC BRAND OUTPUT GUARD — HIGHEST PRIORITY

User-visible NullOne content must NEVER contain:

Texbrif
#Texbrif
#texbrif

This restriction applies to:
- caption
- hashtags
- headline
- preview copy
- source attribution visible to the audience

Legacy infrastructure identifiers such as:
texbrif-approval
texbrif-publisher
texbrif:
accountId texbrif

are internal only and must remain unchanged.

If a brand hashtag is useful, use:

#NullOne

Before manifest creation, explicitly scan the final caption.
If the word Texbrif appears in any case, BLOCK and correct the caption first.

The deterministic manifest validator is authoritative and must reject
pre-publication captions containing the legacy public brand.
