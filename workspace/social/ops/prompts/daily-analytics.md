Run the NullOne DAILY ANALYTICS collection.

READ ONLY.

Read:
- social/OPERATING_SYSTEM.md
- social/state/publish-ledger.jsonl
- social/state/experiments.jsonl

ACCOUNT:
@nullone.az
Zernio accountId: 6a982bbf77555aae01c28f21

DO NOT:
- publish
- edit posts
- create drafts
- schedule
- modify Instagram state

Collect using Zernio:

1. current connected-account metadata
2. follower-history
3. Instagram account insights:
   reach
   views
   accounts_engaged
   total_interactions
   comments
   likes
   saves
   shares
   profile_links_taps
4. post analytics for published NullOne content

Remember:
- account insights may lag up to 48h
- follower history may lag up to 24h
- do not judge same-day posts prematurely
- total_interactions=-1 may mean no data

Save RAW/safe snapshot information under:
social/analytics/raw/YYYY-MM-DD.md

Save interpreted summary under:
social/analytics/reports/YYYY-MM-DD.md

For mature-enough published posts, compute when available:
- shares / reach
- saves / reach
- likes / reach
- comments / reach

Record follower delta from reliable snapshots.

Do not claim causation.
Do not change strategy based on one post.
Do not expose ZERNIO_API_KEY.

If there is insufficient new data, record that and stop.
