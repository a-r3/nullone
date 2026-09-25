# NullOne Architecture

## Editorial path

Morning Editorial
→ candidate board
→ Breaking Radar delta monitoring
→ Draft Factory
→ verification
→ Visual Director (docs/contracts/visual-director-contract-v1.md;
  decides visual_style only, never format/carousel eligibility)
→ deterministic packaging evaluator (format + final visual_style)
→ source asset acquisition/validation where the decision requires it
→ render
→ template-aware brand gate
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
