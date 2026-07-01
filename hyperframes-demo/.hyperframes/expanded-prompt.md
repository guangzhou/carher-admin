# CarHer Admin Pulse Demo

## Style Block

- Visual identity: dark premium operations room, precise but not sterile.
- Palette: background `#071512`, panel `#10221d`, foreground `#f3eadc`, muted `#a8b8aa`, accent `#d8863b`, signal `#72d2a3`, danger `#f06449`.
- Typography: Georgia for large editorial statements, "Space Mono" for labels, counters, and operational readouts.
- Mood: executive control room meets infrastructure map. Calm confidence, real operational density, no generic neon/purple SaaS look.

## Rhythm Declaration

Pattern: `hook-HOLD -> directional-shift -> BUILD -> iris-reveal -> resolve`.

Three scenes across 9 seconds:

1. `0.0-3.0s`: Scale hook. Introduce CarHer Admin and the 500+ instance surface.
2. `3.0-6.2s`: Control path. Show API, CRD, operator, pods, Cloudflare as one running system.
3. `6.2-9.0s`: Outcome. Zero interruption, visible rollout status, operational CTA.

## Global Rules

- Same background color across all scenes.
- Every scene has 2-5 ambient decoratives: grid, glow, ghost labels, orbit rings, tick marks.
- Transitions are CSS-only for portability: diagonal wipe into scene 2, circle iris into scene 3.
- No element exits before transitions. Outgoing scenes remain visible until the transition handles the handoff.
- Final scene may fade to black after the CTA has landed.

## Scene Beats

### Scene 1: Fleet Hook

Concept: The viewer sees the platform as a live fleet surface, not a dashboard screenshot. A large 500+ counter anchors the frame while tiny instance pulses imply scale.

Mood direction: cinematic infrastructure title card, measured control-room energy.

Depth layers:
- BG: dark green-black field, accent radial glow, ghost "HERINSTANCE" wordmark, subtle grid.
- MG: large `500+` figure, "CarHer Admin" title, short platform line.
- FG: metadata strip, three live counters, thin copper rules, instance pulse dots.

Animation choreography:
- Ghost wordmark drifts slowly.
- Accent glow breathes with a finite pulse.
- `500+` drops in with weight, title slides from left, subtitle resolves upward.
- Counters step in from alternating directions.
- Dots pulse in staggered rows.

Transition out: diagonal wipe, 0.55s, directional/purposeful, copper sweep.

### Scene 2: Control Path

Concept: A request becomes infrastructure. The system path is rendered as connected blocks from Admin API through HerInstance CRD to Operator, Pod, and Cloudflare ingress.

Mood direction: technical diagram turned into a title sequence.

Depth layers:
- BG: persistent grid, moving scanner line, low accent glow.
- MG: five connected nodes with labels and short details.
- FG: route line, status pills, command-strip labels, small active indicators.

Animation choreography:
- Heading snaps in, route line draws across.
- Nodes assemble one by one with different entrances.
- Active indicators pulse with finite repeats.
- Scanner line sweeps downward to imply reconciliation.

Transition out: circle iris centered near the operator node, 0.7s, resolve into outcome.

### Scene 3: Outcome

Concept: The operations chain resolves into a clear deployment promise: zero interruption and observable rollout health.

Mood direction: restrained launch card, the moment after the system locks in.

Depth layers:
- BG: calm glow, oversized "ROLLING" ghost text, corner registration marks.
- MG: headline, three status capsules, rollout bar.
- FG: final CTA, signal ticks, small version labels.

Animation choreography:
- Headline expands from slight scale with confidence.
- Capsules lock in with separate directions.
- Rollout bar fills from left.
- CTA types in as a final operations note.
- Final fade to black closes the piece.

## Recurring Motifs

- Copper structural rules.
- Green signal dots.
- Monospace metadata and tabular counters.
- Large editorial serif numbers paired with technical labels.

## Negative Prompt

- No purple-to-blue gradients.
- No Roboto, Inter, default system UI fonts.
- No flat solid background.
- No generic card grid.
- No jump cuts.
