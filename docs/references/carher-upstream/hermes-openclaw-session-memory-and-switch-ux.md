# Snapshot Note

Copied into `carher-admin` from `../CarHer/artifacts/2026-05-11-hermes-openclaw-session-memory-and-switch-ux.md` during the her-266 H75/Dify session artifact cleanup. Treat this as a read-only reference snapshot; refresh intentionally from upstream instead of editing `../CarHer` for rollout/runbook work.

# Hermes/OpenClaw Cross-Session Memory And Hot-Switch UX Plan

Date: 2026-05-11
Owner: aligned with @卜弋天
Status: Workstream B delivered; Workstream A peer-session skill corrected after design review and awaiting fresh bot-level validation

This file captures two related but separate workstreams:

1. Hermes/OpenClaw cross-session visibility plus official OpenClaw-to-Hermes memory migration.
2. CarHer engine branding and hot-switch UX, to start only after the current Feishu Context Dedup Fix lands and passes E2E.

## Update: Workstream A Peer-Session Skill Corrected

Updated on 2026-05-12 after design review found that the first peer-session skill incorrectly wrapped lookup behavior in a custom helper script. That helper-centric design has been replaced with a pure storage-map skill.

Corrected scope:

- Added shared skill `carher-peer-sessions`.
  - Local audit copy: `artifacts/carher-peer-sessions-skill/carher-peer-sessions/`.
  - S1 OpenClaw skill root: `/home/cltx/.openclaw/skills/carher-peer-sessions/`.
  - S1 Hermes skill root: `/Data/hermestest/skills/carher-peer-sessions/`.
  - Runtime-visible OpenClaw path: `/data/.openclaw/skills/carher-peer-sessions/`.
  - Runtime-visible Hermes path: `/opt/hermestest/skills/carher-peer-sessions/`.
- The skill is read-only and intentionally provides no helper program.
- It teaches the bot the OpenClaw and Hermes session/memory layouts, how to identify the peer engine from `/data/.engine/active`, what files are authoritative, and what evidence to cite.
- The bot should use its normal file-inspection tools directly against the live filesystem instead of trusting a bespoke script result.
- The old `scripts/peer-session-search.sh` helper was removed from the audit copy and must not be shipped as the bot-facing interface.

Previous helper-based validation is now treated as invalid evidence for the product goal. Fresh validation must prove that a new bot session can use the structure-only skill and inspect peer files directly:

- In Hermes mode, ask about a fact that exists only in OpenClaw session files.
- In OpenClaw mode, ask about a fact that exists only in Hermes session files.
- The answer must cite the peer engine storage path and record/line evidence.
- No answer may rely on `peer-session-search.sh` or any other helper shipped by this skill.

Official Hermes OpenClaw migration:

- Verified official CLI path from Hermes docs: `hermes claw migrate --source /data/.openclaw --preset user-data`.
- Dry-run artifact: `/Data/carher-runtime/deploy/carher-200/artifacts/memory-migration-20260512T073239/dry-run-yes.txt`.
  - Preview: 20 items would migrate, 1 conflict (`soul`, because Hermes already had an empty `SOUL.md`), 32 skipped.
- Apply command used:
  - `/opt/hermes/venv/bin/hermes claw migrate --source /data/.openclaw --preset user-data --overwrite --skill-conflict rename --yes`
  - `user-data` was used deliberately; secrets/API keys were not migrated.
- Backup created by official migrator:
  - `/opt/data/backups/pre-migration-2026-05-11-233313.zip`.
- Migration report:
  - `/opt/data/migration/openclaw/20260511T233317/summary.md`
  - `/opt/data/migration/openclaw/20260511T233317/report.json`
- Apply result: 19 migrated, 31 skipped, 0 conflicts, 0 errors.

Before vs after memory verification:

- Before:
  - OpenClaw `SOUL.md`: 72 lines, sha256 `4bab4962f5a85bd4e419054fa32e0a4e6b5c98857d254105b6b9ef8b83d7d945`.
  - OpenClaw `USER.md`: 33 lines, sha256 `3870fbfbf7150d035f35aa1e915d02f39be4e628aa41535d17252f779bfa1dbe`.
  - OpenClaw `MEMORY.md`: 19 lines, sha256 `e758b5fb3b6c0077e539372340a734924f199035e13c5f443f9359f426859dbd`.
  - Hermes `SOUL.md`, `memories/USER.md`, and `memories/MEMORY.md`: all 0 lines.
- After:
  - Hermes `SOUL.md`: 72 lines, same sha256 as OpenClaw `SOUL.md`; byte-exact match.
  - Hermes `memories/USER.md`: 45 lines, official transformed Hermes memory format.
  - Hermes `memories/MEMORY.md`: 39 lines, official transformed Hermes memory format.
  - Key phrase verification passed for `卜弋天`, `Open ID`, `Asia/Shanghai`, `Nova / 研究3`, `Glory Liao`, `闭环 > 宣告闭环`, and `影 (Shadow)`.
- New Hermes session memory-load validation passed:
  - Test marker: `MEMMIG_20260512T073543_HERMES_MEMORY`.
  - After `/new`, Hermes answered from loaded long-term memory with `卜弋天`, `Nova / 研究3`, and `Glory Liao`.

Skipped or intentionally not migrated:

- Secrets/API keys: not selected; official output says re-run with `--migrate-secrets` only if desired.
- Workspace `AGENTS.md`: skipped because no `--workspace-target` was provided.
- Messaging/provider/model/deep channel config: skipped where no Hermes-compatible source values were found.
- Sensitive binary/runtime OpenClaw state: archived/skipped by the official migrator.

## Update: Workstream B Delivered

Updated on 2026-05-12 after the final S1 live E2E run.

Delivered scope:

- `carher-openclaw` dev: `30b10ad5a15` (`CarHer: add engine footer branding`).
- `carher-hermes` dev: `c551a4b` (`CarHer: add Hermes footer branding`).
- `carher-runtime` dev: `0708b27f0e74` through the swap-card animation and E2E-window fixes.
- S1 deploy targets: `carher-12`, `carher-198`, `hermestest-199`, and `hermestest-200`.

Final live E2E:

- Run id: `s1-branding-swap-20260511T165913`.
- Artifact root: `/Data/carher-runtime/deploy/carher-200/artifacts/s1-branding-swap-20260511T165913`.
- Result: `PASS s1-branding-swap-20260511T165913`.

Verified online:

- `carher-12` and `carher-198` OpenClaw reply footers show the OpenClaw engine marker.
- `hermestest-199` Hermes reply footer shows the Hermes engine marker.
- `hermestest-200` shows the OpenClaw marker before `/hermes` and the Hermes marker after `/hermes`.
- `/hermes` edits the same Feishu card to 100%, sends the independent Hermes welcome card, and deletes `/data/.engine/swap-card.json`.
- `/openclaw` edits the same Feishu card to 100%, sends the independent OpenClaw welcome card, and deletes `/data/.engine/swap-card.json`.
- Stale `swap-card.json` degrades to welcome-card-only and cleanup.
- A fresh state with an invalid `message_id` logs the Feishu edit error, still sends a welcome card, and cleans up.
- Pure Hermes `hermestest-199` rejects `/openclaw` and does not start a swap animation.

Historical note for the first Workstream B pass:

- At the time of the initial branding/swap delivery, Workstream A had not been implemented and was not claimed as part of that production pass. It is now delivered in the Workstream A update above.

## Update: Extended Production Validation

Updated again on 2026-05-12 after the user requested a broader end-to-end sweep
covering the Feishu context-dedup work from the parallel session, lark-cli/KQA,
session storage cleanliness, and hot-switch failure boundaries.

Validated from the same S1 dev deployment:

- `carher-openclaw` dev: `73b1a2761ef`.
- `carher-hermes` dev: `c551a4b`.
- `carher-runtime` dev: `0708b27f0e74`.
- Targets: `carher-12`, `carher-198`, `hermestest-199`, `hermestest-200`.

Additional live E2E runs:

- `s1-context-dedup-20260511T172735`: PASS.
  - Multi-bot group context worked for 198/199/200.
  - `hermestest-199` group self-check saw exactly one `[Recent group history]` block.
  - `hermestest-199` DM persisted user rows without group-history preamble.
  - `hermestest-200` Hermes group self-check saw exactly one group-history block.
  - `hermestest-200` Hermes DM persisted clean user rows.
  - `hermestest-200` OpenClaw DM jsonl had no `Chat history since last reply` or `openclaw.runtime-context` pollution for the tested marker.
  - Pure Hermes `hermestest-199` still did not write runtime engine markers for `/openclaw`.
- `s1-feature-20260511T174247`: PASS.
  - 199 native table card raw content contained a `table` element.
  - 199 KQA through lark-cli user token returned the `Base 逐版本升级实验` wiki result.
  - 199 A2A to 200 returned `A2A_FROM_199_TO_200_OK`.
  - 199 `/gpt` and `/opus` model switches both worked.
  - 200 Hermes KQA through lark-cli user token returned the same wiki result.
- `s1-command-matrix-20260511T175428`: PASS.
  - Ordinary multi-bot mention, multi-bot `/status`, multi-bot `/new`, suffix `/status`, and suffix `/new` all worked.
- `s1-branding-swap-20260511T175812`: PASS.
  - Revalidated footer branding, `/hermes`, `/openclaw`, welcome cards, and swap-card cleanup after the broader traffic.
- `s1-dual-200-20260511T180518`: PASS.
  - 200 Hermes DM returned `HERMES200_DM_OK`.
  - After DM `/openclaw`, 200 OpenClaw DM recovered the previous Hermes DM marker and returned `OPENCLAW200_DM_OK`.
- `lark-cli-smoke-20260511T181147`: PASS.
  - 199 lark-cli auth status OK.
  - 200 OpenClaw HOME `/data` lark-cli auth status OK.
  - 200 Hermes HOME `/opt/data` lark-cli auth status OK.
  - Real `im +chat-messages-list` and raw `api GET /im/v1/messages/{id}` calls succeeded.
- `swap-fault-injection-20260511T181323`: PASS for docker-restart interruption cases.
  - Restart during OpenClaw -> Hermes still ended with `✅ ☤ Hermes 已就位`, `active=hermes`, and no `swap-card.json`.
  - Restart during Hermes -> OpenClaw still ended with `✅ 🦞 OpenClaw 已就位`, `active=openclaw`, and no `swap-card.json`.
- `swap-fault-injection-retry-20260511T181938`: PASS.
  - Corrupted Feishu `message_id` caused a real Feishu 400 edit failure; helper logged `frame 12 failed`, sent the independent OpenClaw welcome card, and cleaned `swap-card.json`.
  - Stale `swap-card.json` degraded directly to welcome-card-only and cleanup.
- `swap-during-message-20260511T182126`: PASS.
  - A message sent during OpenClaw -> Hermes was recoverable by Hermes from Feishu history after readiness.
  - A message sent during Hermes -> OpenClaw was recoverable by OpenClaw from Feishu history after readiness.
- `storage-audit-20260511T182954`: PASS.
  - Hermes SQLite audit found no persisted group-history preamble rows for the tested markers.
  - OpenClaw jsonl audit found no structural `openclaw.runtime-context` records and no user-message history preamble pollution for the tested markers.

Operational note:

- The first invalid-edit fault script run had a test harness issue: `docker exec python3 -` was missing `-i`, so the intended state corruption was not applied. The corrected retry run above is the authoritative invalid-edit evidence.

## Update: Conflict-Avoidance Test Lane

Updated on 2026-05-11 after the user clarified that another Codex session is pressure-testing the 198/199/200 lane on `dev`.

For tonight's implementation validation, avoid using `carher-198`, `hermestest-199`, and `hermestest-200` unless the user explicitly reopens that lane. Use docker12/docker13 instead:

- `docker12` / `carher-12`: pure OpenClaw footer and baseline behavior.
- `docker13` / `carher-13` / current dual equivalent: hot-switch and cross-engine behavior when available.

Final product expectations still require changes to come from the relevant `dev` branches and be deployable through the normal pipeline. The live E2E target lane for this session is docker12/docker13 to avoid collisions with the other agent.

## Update: Primary E2E Lane Reopened

Updated again on 2026-05-11 after the other Codex session confirmed its work was complete and pushed to `dev`.

The final live E2E lane for this delivery is now:

- `carher-12`: pure OpenClaw footer/baseline behavior.
- `carher-198`: pure OpenClaw control.
- `hermestest-199`: pure Hermes control.
- `hermestest-200`: carher-runtime hot-switch canary.

Before implementation, deployment, or E2E, re-fetch the three `dev` branches and build from the newest dev state so this session includes the other session's delivered work.

## Workstream A: Cross-Session Memory And Official Migration

### Goal

OpenClaw and Hermes must be able to see each other's session history after a hot switch:

- After switching from OpenClaw to Hermes, Hermes can inspect OpenClaw's session/history files.
- After switching from Hermes to OpenClaw, OpenClaw can inspect Hermes's session/history files.
- A global CarHer skill teaches the bot the other engine's session and memory structure so it can inspect the inactive engine's persisted files directly.
- After `/new`, the bot should naturally use that skill when the user asks about cross-engine memory.

This is not just a one-time copy. The runtime needs a stable peer-home or shared-mount design so either active engine can inspect the inactive engine's persisted conversation history.

### Proposed Architecture

- Keep each engine's own home/session store as source of truth.
- Add stable, read-oriented peer paths in the dual runtime:
  - OpenClaw active state can read Hermes history.
  - Hermes active state can read OpenClaw history.
- Make peer session visibility explicit in a global skill under the CarHer shared skill layer:
  - Host path: `~/.openclaw/skills/`
  - Container path: `/data/.openclaw/skills/`
  - The skill is read-only by default and should avoid mutating either engine's memory/session store.
- Publish the skill through the existing shared-skill push flow, then verify with `/new`.

### Official Hermes Migration Path

Hermes's official OpenClaw migration path is:

```bash
hermes skills install official/migration/openclaw-migration
hermes claw migrate --dry-run
hermes claw migrate
```

Useful supported options:

```bash
hermes claw migrate --preset user-data
hermes claw migrate --overwrite
hermes claw migrate --source /custom/path/.openclaw
```

Relevant official references:

- https://hermes-agent.nousresearch.com/docs/user-guide/skills/optional/migration/migration-openclaw-migration
- https://hermes-agent.nousresearch.com/docs/zh-Hans/guides/migrate-from-openclaw
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md

The official migration imports or transforms:

- `SOUL.md` into Hermes home as `SOUL.md`.
- OpenClaw `MEMORY.md` and `USER.md` into Hermes memory entries.
- Command allowlist entries.
- Hermes-compatible messaging settings.
- OpenClaw skills into `~/.hermes/skills/openclaw-imports/`.
- Selected compatible workspace assets.
- A structured report of migrated, skipped, archived, and conflicting items.

Hermes persistent memory lives in:

- `~/.hermes/memories/MEMORY.md`
- `~/.hermes/memories/USER.md`

Memory is loaded as a frozen snapshot at new-session start, so migration verification must include a new Hermes session.

### Acceptance Criteria

1. OpenClaw can read Hermes session/history storage through a stable path after hot switch.
2. Hermes can read OpenClaw session/history storage through a stable path after hot switch.
3. The shared global skill is installed, pushed, and visible to new sessions after `/new`.
4. The bot can answer cross-engine memory questions by using the skill's storage map and inspecting peer files directly, not by guessing from current context or trusting a custom helper.
5. Official Hermes migration is run first as dry-run, then applied with an explicit conflict policy.
6. Before/after evidence confirms `SOUL.md`, `USER.md`, and `MEMORY.md` content moved accurately from OpenClaw into Hermes.
7. Any skipped, archived, or conflicting migration item is reported with its reason.
8. Canary dual container validation passes before broader rollout.
9. Rollback to the OpenClaw-safe state remains available.

### Test Cases

#### A1. Peer Mount Visibility

1. Start with active engine `openclaw`.
2. From the OpenClaw runtime, list and read the Hermes session/history path.
3. Switch to active engine `hermes`.
4. From the Hermes runtime, list and read the OpenClaw session/history path.
5. Restart the container and repeat both reads.

Expected result: both directions remain readable after switch and restart.

#### A2. Cross-Engine History Lookup

1. In OpenClaw, create a session containing a unique marker.
2. Switch to Hermes.
3. Ask the bot to find whether OpenClaw ever discussed that marker.
4. In Hermes, create a session containing a different unique marker.
5. Switch to OpenClaw.
6. Ask the bot to find whether Hermes ever discussed that marker.

Expected result: the bot uses the peer-session skill and returns concrete evidence from the other engine.

#### A3. Skill Trigger After `/new`

1. Publish the global skill.
2. Start a fresh bot session with `/new`.
3. Ask: "去查一下 Hermes 之前有没有聊过 `<unique-marker>`."
4. Repeat in the reverse direction for OpenClaw.

Expected result: the skill is discoverable in the fresh session and the bot directly inspects the peer engine's persisted files.

#### A4. Migration Dry Run

1. Run official Hermes migration in dry-run mode against the intended OpenClaw source.
2. Capture migrated, skipped, archived, and conflict summaries.
3. Decide conflict behavior before applying.

Expected result: no write occurs during dry-run, and the apply plan is clear.

#### A5. Migration Before/After

Before applying migration, capture key excerpts and checksums or line counts from:

- OpenClaw `SOUL.md`
- OpenClaw `USER.md`
- OpenClaw `MEMORY.md`

After applying migration, verify the corresponding Hermes destinations:

- Hermes `SOUL.md`
- Hermes `~/.hermes/memories/USER.md`
- Hermes `~/.hermes/memories/MEMORY.md`

Expected result: key memory/persona facts are present, correctly categorized, not silently truncated, and not duplicated in harmful ways.

#### A6. Real User Bot Test

1. Use the user's real identity in Feishu or the target bot channel.
2. Ask questions that depend on migrated `SOUL.md`, `USER.md`, and `MEMORY.md`.
3. Start `/new` and repeat a smaller probe.

Expected result: the new session reflects migrated Hermes memory rather than stale current-context residue.

## Workstream B: Engine Branding And Hot-Switch UX

### Source Spec Summary

Spec: CarHer Engine Branding & Hot-Switch UX

Date: 2026-05-11
Author: aligned with @卜弋天
For: Codex to implement after current Feishu Context Dedup Fix lands
Status: design locked, awaiting implementation

The UX goal is that users can tell, in Feishu at any time and for any bot:

1. Whether the current underlying engine is OpenClaw or Hermes, using the official engine marker/logo.
2. Whether a hot switch is currently happening, using an animated progress card.
3. When the new engine is ready to talk, using an explicit ready/welcome card.

Scope:

- Pure OpenClaw fleet bots such as `carher-12`: add the OpenClaw marker.
- Hermes-capable or dual bot in the docker13 lane: add the Hermes marker when Hermes is active.
- Runtime hot-switch bot in the docker13 lane: add switch animation and post-switch welcome card.

### Locked Footer Design

Every bot reply card footer has exactly six fields in this order:

1. Engine marker, bolded.
2. Model version.
3. Context usage.
4. Compact ratio.
5. Owner/group marker.
6. Elapsed seconds.

Examples:

```text
🦞 **OpenClaw** · opus4.7 · 29k/1.0m · 3% · 🔒主人@ · 17.5s
**☤ Hermes** · opus4.7 · 35k/1.0m · 4% · 👥群@ · 13.0s
```

Locked symbols:

- OpenClaw: `🦞 **OpenClaw**`
- Hermes: `**☤ Hermes**`
- Do not replace `☤` with non-official alternatives such as feathers, wings, or airplanes.

### Locked Switch Animation

Use a five-frame Feishu card edit sequence, editing the same card about every 2 seconds for roughly 8-10 seconds total.

Hermes to OpenClaw:

```text
T=0s   ☤ → 🦞 切换中
       ░░░░░░░░░░░░░░░░  0%   💓 写 marker

T=2s   ☤ → 🦞 切换中
       ████░░░░░░░░░░░░  25%  💓 容器重启

T=4s   ☤ → 🦞 切换中
       ████████░░░░░░░░  50%  💓 新引擎启动

T=6s   ☤ → 🦞 切换中
       ████████████░░░░  75%  💓 patches 加载

T=8s   ✅ 🦞 OpenClaw 已就位
       ████████████████ 100%  💚 可对话
```

OpenClaw to Hermes uses the same animation, replacing direction and final state:

- `🦞 → ☤`
- `**☤ Hermes** 已就位`

### Locked Welcome Card

Immediately after the final animation frame, send a second independent welcome card.

Example OpenClaw target:

```text
✅ 🦞 OpenClaw 已就位
────────────────────────────────────────────
• 模型: opus4.7
• 上下文: 已从飞书拉回最近 49 条续话
• 命令: /new  /status  /hermes 切回 Hermes

你的下一句话我接着答 👇
────────────────────────────────────────────
🦞 **OpenClaw** · opus4.7 · 0/1.0m · 0% · 🔒主人@ · 0.5s
```

The footer must use the same six-field format as normal replies. Initial values are acceptable for the welcome card.

### Implementation Ownership

OpenClaw footer:

- Repo: `carher-openclaw`
- File: `scripts/carher-patches/apply-footer-status.sh`
- Change: add `🦞 **OpenClaw**` as the first footer field.

Hermes footer:

- Repo: `carher-hermes`
- File: `patches/apply-footer-runtime-metadata.sh` or `patches/apply-card-output.sh`
- Change: add `**☤ Hermes**` as the first footer field.

OpenClaw to Hermes animation:

- Repo: `carher-runtime`
- File: `runtime/plugins/carher-engine-swap/index.ts`
- Change: when old engine is OpenClaw, send initial card, write swap-card protocol state, and hand off to Hermes.

Hermes to OpenClaw animation:

- Repo: `carher-runtime`
- File: `runtime/patches/hermes-engine-swap.sh`
- Change: when old engine is Hermes, send initial card, write swap-card protocol state, and hand off to OpenClaw.

Swap-card protocol:

- Repo: `carher-runtime`
- File: `runtime/spec/swap-animation.md`
- Change: document new cross-engine animation handoff protocol.

Welcome card:

- Repo: `carher-runtime`
- Files:
  - `runtime/plugins/carher-engine-swap/welcome-card.ts`
  - `runtime/patches/hermes-welcome-card.sh`
- Change: send engine-specific ready card after resumed animation finishes or stale handoff is detected.

Lifecycle spec update:

- Repo: `carher-runtime`
- File: `spec/swap-lifecycle.md`
- Change: add animation and welcome-card sections.

### Swap-Card Protocol

Path:

```text
/data/.engine/swap-card.json
```

This reuses the shared dual-engine bind mount that already stores the active engine marker.

Old engine writes before `process.exit(0)`:

```json
{
  "schema_version": 1,
  "from_engine": "hermes",
  "to_engine": "openclaw",
  "chat_id": "oc_24f93dcf5e05d025b6cf12a204b1bd8f",
  "message_id": "om_x100b6f2a4d3380acb3f575240742c5b",
  "card_owner_app_id": "cli_a94a51d8873bdbb6",
  "started_at_ms": 1778500537528,
  "frames_already_sent": 1,
  "current_frame": 0
}
```

New engine behavior after patches load and Feishu websocket is ready:

1. Read `/data/.engine/swap-card.json`.
2. If it exists, `to_engine` matches current engine, and `now - started_at_ms < 60_000`:
   - Resume from the 25% frame.
   - Edit the same card about every 2 seconds until 100%.
   - Persist frame progress after each edit.
   - Send the independent welcome card after the final frame.
   - Delete `swap-card.json`.
3. If it is stale by at least 60 seconds:
   - Do not resume animation.
   - Send the welcome card.
   - Delete `swap-card.json`.
4. If it does not exist:
   - Normal startup, no UX side effect.

Old engine sequence:

1. Receive `/openclaw` or `/hermes`.
2. Send the 0% frame via Lark API and capture `chat_id` and `message_id`.
3. Write `swap-card.json`.
4. Write `/data/.engine/active` to the target engine.
5. Optionally edit to the 25% frame for transition feel.
6. `process.exit(0)`.
7. Docker restart policy brings the container back.
8. New engine resumes animation and sends welcome card.

### Evaluation

The spec is feasible and well-scoped, but it should be implemented in strict phases because it crosses three repos and touches hot-switch lifecycle code.

Recommended sequence:

1. Wait for Feishu Context Dedup Fix F1-F6 to land, deploy, and pass E2E.
2. Implement footer engine branding first because it is independent and benefits pure single-engine bots immediately.
3. Add and test `swap-card.json` as a small versioned protocol with validation and stale cleanup.
4. Add animation resume in `carher-runtime` only after the protocol is documented and covered by unit tests.
5. Add welcome card delivery and fallback behavior.
6. Run docker13-lane E2E for both switch directions.
7. Only after docker13 passes, roll footer-only changes to the selected pure OpenClaw/Hermes fleet lane.

Important engineering notes:

- The card edit handoff only works if the new engine can authenticate as the app that owns the original card. `card_owner_app_id` is useful metadata, but implementation must verify whether Feishu edit permissions require the same bot app credentials in practice.
- Write `swap-card.json` atomically, for example by writing a temp file in `/data/.engine/` and renaming it, so the restarted engine never reads partial JSON.
- Validate protocol shape and engine enum values before use. Invalid JSON should degrade to welcome-card-only and cleanup, not block startup.
- Make frame progression idempotent. A restart during frame 50% should not duplicate or regress the card.
- Treat Lark edit failure as non-fatal. The locked fallback is to send the welcome card and keep inbound handling healthy.
- Keep the animation state file free of secrets. It should only contain routing/card identifiers and timestamps.
- Keep the frame interval at roughly 2 seconds and never below 1 second.
- Avoid doing switch animation for pure single-engine bots. Pure Hermes bots must continue rejecting `/openclaw` and `/hermes`.
- The spec references both `runtime/spec/swap-animation.md` and `spec/swap-lifecycle.md`; when implementing in `carher-runtime`, first confirm the repo's actual spec directory layout and keep the docs in one consistent place.

### Feishu Card Edit Live Probe

Result: passed on 2026-05-11.

Probe target:

- Bot app: `hermestest-200` / 研究3的her.
- Chat: S1 runtime test group.
- Marker: `HER_CARD_PROBE_20260511T230930`.
- Message id: `om_x100b6f2b5c7de0acb4aa5cc73a40467`.

Observed API sequence:

1. Created a Feishu interactive shared card with `config.update_multi=true`.
2. Patched the same `message_id` to the 25% frame with the same bot app tenant token.
3. Patched the same `message_id` to the 100% frame with the same bot app tenant token.
4. Read back raw card content and confirmed it contained both the marker and `100%`.

Observed API results:

```json
{"step":"create","code":0,"msg":"success"}
{"step":"patch_25","code":0,"msg":"success"}
{"step":"patch_100","code":0,"msg":"success"}
{"step":"get_verify","code":0,"msg":"success","contains_marker":true,"contains_100":true}
```

Conclusion: the largest Feishu uncertainty is resolved for the important permission model. A separate process using the same canary bot app credentials can edit a card it previously created by `message_id`. The remaining work is lifecycle engineering: capture the old engine's `message_id`, write `swap-card.json` atomically, wait until the new engine's Feishu path is ready, then resume the same edit sequence.

### Acceptance Criteria

#### B1. Footer Single-Bot Verification

- `carher-12` footer starts with `🦞 **OpenClaw**`.
- docker13 in OpenClaw mode starts with `🦞 **OpenClaw**`.
- docker13 in Hermes mode starts with `**☤ Hermes**`.
- All footers preserve the six-field order: engine, model, context, compact, owner/group marker, elapsed time.

#### B2. Hermes To OpenClaw Switch Animation

- Set docker13 marker to Hermes.
- Send `/openclaw` in the target Feishu group.
- Receive the 0% card immediately.
- See the same card edited through 25%, 50%, 75%, and 100% within about 8-10 seconds.
- Receive an independent welcome card: `✅ 🦞 OpenClaw 已就位`.
- The next owner message receives a normal reply.
- Footer shows `🦞 **OpenClaw**`.
- `/data/.engine/swap-card.json` is deleted after welcome-card delivery.

#### B3. OpenClaw To Hermes Switch Animation

- Set docker13 marker to OpenClaw.
- Send `/hermes`.
- Expect the same sequence, ending with `**☤ Hermes** 已就位`.
- The next owner message receives a normal reply.
- Footer shows `**☤ Hermes**`.
- `/data/.engine/swap-card.json` is deleted after welcome-card delivery.

#### B4. Fault Tolerance

- If `swap-card.json` is older than 60 seconds, do not resume animation. Send welcome card and delete it.
- If one frame edit fails, do not retry indefinitely. Send welcome card and keep inbound handling live.
- During `_engineSwapPending=true`, additional user messages are absorbed with a "切换中, ~10s 后再发" response.
- After the new engine starts, normal Feishu history injection can still recover recent messages.

#### B5. Single-Engine Isolation

- Pure Hermes bots still reject `/openclaw` and `/hermes`; no animation starts.
- `carher-12` keeps normal reply behavior, with only the footer engine field added.

### Out Of Scope

- Do not fix the `openclaw-lark` `contracts.tools` warning here.
- Do not modify frozen `hermestest-13`, `hermestest-14`, or `hermestest-75` legacy dual baselines.
- Do not replace the Hermes `☤` symbol.
- Do not build GIF animation or a true realtime progress bar; Feishu card edits are the intended mechanism.

### Estimated Effort

- Footer changes plus focused tests: about 0.5 day.
- `swap-card.json` protocol, five-frame animation, and welcome card: about 1.5 days.
- E2E validation V1-V5: about 0.5 day.

Total: about 2.5-3 days after Feishu Context Dedup Fix is complete.

### Implementation Gate

This work must be serialized after Feishu Context Dedup Fix:

1. Finish, deploy, and E2E the dedup work first.
2. Start this branding/hot-switch UX work only after dedup is green.
3. Avoid parallel edits to `runtime/plugins/carher-engine-swap/index.ts`, since both workstreams touch that file.
