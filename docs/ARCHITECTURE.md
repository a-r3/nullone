# NullOne Architecture

## Editorial path

Morning Editorial
→ candidate board
→ Breaking Radar delta monitoring
→ Draft Factory
→ verification
→ render
→ deterministic Production Bridge
→ Zernio review draft
→ Telegram preview
→ first human approval
→ second publish confirmation
→ publisher
→ deterministic notifier

## Safety invariants

- Publication-ready output requires `VERIFICATION: PASS`.
- Approval agent must not publish.
- Approval agent must not call Zernio.
- Publisher execution must be human-authorized.
- Publication attempts must never exceed one for the same authorization.
- Notification failure must never trigger publication retry.
- Review draft ID and live post ID are distinct concepts.
- Public copy must use NullOne branding.
