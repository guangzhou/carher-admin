---
name: carher-upgrade-flow
description: Use when upgrading CarHer, changing a Her image/profile, moving ACK/S3 H75 or Dify behavior, or when post-upgrade Feishu replies, /hermes switching, Dify Creator, or long-message regression fails.
---

# CarHer Upgrade Flow

## Purpose

This is the front door for CarHer upgrades. Load this first, then load the narrower skill for the surface you are touching. The goal is to prevent repeat failures from the carher-1000 H75/Dify/Feishu upgrade.

## Unified H75 Repair Entrypoint

For H75/OpenClaw/Hermes/Dify upgrade repair and post-upgrade regression, use the unified entrypoint first:

```bash
cd ~/codes/carher-admin
python3 scripts/h75-upgrade-repair-suite.py --mode canary
```

Batch repair must go through the same entrypoint after canary passes:

```bash
python3 scripts/h75-upgrade-repair-suite.py --mode batch --targets 10 100 101 --wave-size 3
```

This entrypoint serializes the repeated fixes and gates from this upgrade series: Dify login-entry repair, Dify API/bootstrap config checks, 266/268 canary issue-login + `/v1/exchange` smoke, in-cluster H75 runtime repair, generated Dify config audit, Hermes LiteLLM config audit, title-failure visibility patch, and explicit current-pod selection. Do not start future H75 upgrade repair by manually running the sub-scripts unless this suite fails and you are doing targeted diagnosis.

If the user explicitly includes a sensitive Her such as `carher-2`, run the same suite with `--include-sensitive` and confirm the run summary contains `sensitive_override` plus a current-pod gate for that target:

```bash
python3 scripts/h75-upgrade-repair-suite.py --mode batch --targets 2 --wave-size 1 --include-sensitive
```

## Skill Order

1. K8s access and status: `carher-k8s-ops`, `check-instance-status`.
2. Version/risk comparison: `carher-upgrade-compare`.
3. Runtime deployment: `carher-deploy`; operator/admin changes: `carher-admin-deploy`.
4. H75/OpenClaw/Hermes/Dify moves: project-local `carher-h75-dify-single-her-rollout`.
5. Feishu verification: `carher-feishu-bench-regression`; run pressure only when the user keeps it in scope.
6. No-reply or slash-command failures: `carher-her-reply-failure-triage`, `carher-slash-command-noreply-debug`.

## Non-Negotiable Gates

- Compare before changing: image digest, entrypoint hash, env, mounts/profile, runtime tools, and S3 reference behavior.
- Deployment fixes only unless the user explicitly permits source changes.
- Run deployment executors and runtime verification from the Kubernetes side. The local laptop is only a trigger/evidence collector; do not validate upgrade scripts or runtime readiness against macOS/zsh/bash.
- Decompose complex upgrade work into simple gates before acting. Each gate needs an input, command/probe, expected value, status, and evidence; do not rely on memory from earlier upgrades.
- Do not call Kubernetes Ready a pass. Require gateway/WS readiness, Feishu reply, A2A or workflow output, and logs.
- Do not call "no matching logs" a pass. Dify, Hermes, group-at/footer, and title-card failures need config reads plus an active reproduction probe: issue-login + `/v1/exchange`, Hermes config parse, Redis group-mode parser, or a target-app-id Feishu marker reply.
- Select current pods through Deployment selectors. Do not guess labels such as `her-id`; stale Failed pods must be separated from current Ready service state.
- Patch persistent desired state and live state together: CRD annotation, Deployment env, and generated runtime config when all are involved.
- Back up files under `/data/.openclaw` before runtime repair.
- Report exact skipped gates; never hide partial verification. If the user narrows the run to smoke-only, mark long-message/long-task pressure as skipped by user, not failed or passed.
- When a single Her exposes a new post-upgrade failure class, immediately scan the already-upgraded fleet for the same signature and fix all matching deployment/runtime/config cases before reporting done.
- When a new failure class needs any manual diagnosis, update `h75-upgrade-repair-suite.py` or a lower repair script before expanding the wave. Notes in chat are not a durable regression gate.
- After any emergency runtime fix, rerun the same probe after rollout or pod recreation before calling it durable. A fix that only works before restart is a temporary bridge, not an upgrade result.
- A canary only proves the gates it actually exercises. If the canary target already had a valid Feishu home channel, it does not prove other targets have one. Add a separate fleet manifest gate for any property that can vary per Her, especially `FEISHU_HOME_CHANNEL`, Redis group mode, owner gate, and active engine.
- Final fleet state defaults to OpenClaw. Any target left in Hermes after upgrade/regression is a failure unless the user explicitly requested final Hermes state. Hermes may have Feishu WS connected but still drop group messages before a turn starts.
- For H75, old base config and partial runtime env are deployment failures, not source failures. Gate on `base-config=carher-base-config-h75`, `CARHER_PROD_KEY == LITELLM_API_KEY`, gateway/ACP/Dify tokens, and writable H75 mounts before user-visible smoke.
- Do not trust strategic merge to remove stale fields from Deployment arrays. If a previous template has `volumeMounts[].readOnly=true`, use JSON Patch or re-read the rendered Deployment to prove it became writable.

## Upgrade Execution Discipline

Use this order for both single-Her and batch work:

```text
manifest -> in-cluster apply -> rollout -> startup window -> runtime audit -> user-visible smoke -> final state
```

Rules:

- Never patch directly from a raw id. Build a manifest with UID, app id, bot open id, exact home chat, current image/group/profile, pod, active engine, and rollback values.
- Persist the manifest before launching dependent probes. Do not run Feishu smoke or local evidence collection in parallel with manifest export/copy.
- In-cluster scripts should use the Kubernetes API or known POSIX-compatible commands. If a probe command has a syntax error, fix the probe and rerun; do not classify the service as failed.
- After H75 rollout, allow a bounded startup window before final runtime probes. Dify tools/config and OpenClaw gateway may appear after K8s reports the pod ready.
- Final conclusions must be based on the latest post-startup probe, not an earlier transient startup failure.

## Batch Her Upgrade Operating Model

When many Her instances will be upgraded, treat the work as a controlled wave rollout, not repeated ad hoc single-instance fixes.

### Principle: split complex upgrades into small automated gates

Before changing anything, convert the upgrade into a machine-readable checklist. Each item must have a command/probe, expected value, status, and evidence path. Avoid "I checked it" notes unless the raw evidence exists.

The minimum gate sequence is:

```text
manifest -> deploy -> deployment_hardening -> runtime_env -> generated_config -> openclaw_health -> hermes_preflight -> smoke -> status_footer -> dify -> a2a -> rollback
```

Rules:

- Batch work must be driven from the manifest and gate results, not from chat history or memory.
- New failure classes become new gates before the next wave.
- Manual runtime fixes are allowed only as an emergency bridge; if repeated once, automate or document the repair before expanding.
- The final report must distinguish `pass`, `fail`, `skipped_by_user`, `not_applicable`, and `not_tested`.
- Long batch executors run as Kubernetes Jobs, not inside the live `carher-admin` pod. The Job must use the proper serviceAccount and ACR imagePullSecrets, and must write machine-readable partial results.

### 0. Build the target manifest first

Create a TSV/JSON manifest before patching anything. Minimum columns:

```text
her_id  her_uid  owner_name  app_id  bot_open_id  deployment_home_chat_id  tester_smoke_chat_id  current_image  target_image  current_group  target_group  runtime_profile  active_engine  phase  feishu_ws
```

Rules:

- Resolve `deployment_home_chat_id` from the exact target chat, latest failing message, or known working bot-visible group. Do not infer it from a similarly named regression group.
- Prefer newest evidence over oldest evidence when resolving home chat. Latest failing switch card or latest pod log `received message ... in <chat>` beats historical `feishu-groups/index.json`; historical files are fallback only when no newer conversation exists.
- Do not use `latest_sent != home` by itself as a patch trigger. It only means the bot has replied elsewhere. Change home only when the mismatch is tied to the current user-visible failure by a failing card, slash-command message, or pod log conversation.
- Keep `tester_smoke_chat_id` separate; it only proves the current operator can send a smoke message there.
- Verify the target bot is a member of any chat before group `@` tests or before registering it as the home channel.
- If no bot-visible chat exists and the user explicitly says deployment is enough, leave home empty, deploy, and report the target as not Feishu self-tested.
- Snapshot each target HerInstance and Deployment YAML into a run directory.
- Record the original image, deploy group, runtime profile annotation, home channel annotation, and pod name for rollback.
- Use UID guards when patching CRDs; refuse to mutate if the live HerInstance UID differs from the manifest.

Manifest source rules:

- Resolve `bot_open_id` with the target app's own app id and secret; do not reuse another app's open id.
- Resolve `deployment_home_chat_id` from the target bot's exact recent chat history, latest failing message, or an already verified channel annotation.
- If the Feishu groups file is Python-style rather than strict JSON, parse it as structured data in the in-cluster tool, not by brittle text grep.

### 1. Wave sizing

Use small waves until the whole chain is proven:

| Wave | Size | Purpose | Required pass before widening |
|---|---:|---|---|
| Canary | 1 | Prove image/profile/Dify/Feishu on a single real user | Full functional matrix green |
| Paired canary | 2 | Prove A2A and per-user isolation | Both users green, A2A both directions |
| Small batch | 3-5 | Prove operational repeatability | No manual per-pod fix except known documented workaround |
| Production wave | 10-20 | Scale after repeatability | Automated manifest/report, no new failure class |

Freeze the wave on the first unknown failure. Restore affected users to OpenClaw or previous image before continuing analysis.

### 2. Preflight comparison for every new image/profile

Before the first wave:

- Compare ACK target against S3 `hermestest-75`: image digest, `/entrypoint.sh` hash, env, mounts, runtime tools, Python deps, and hot switch timings.
- Compare Dify against S3 `hermestest-14`: Her runtime workflow API and lifecycle must be internal URLs, not public Cloudflare.
- Check Hermes Feishu deps before any `/hermes` test:

  ```bash
  kubectl exec -n carher "$POD" -c carher -- \
    /opt/hermes/.venv/bin/python3 -c 'import lark_oapi, aiohttp_socks'
  ```

- If ACK lacks `lark_oapi`, do not scale the rollout on a current-pod hot fix. Build a new runtime image/profile with the Hermes Feishu deps baked in, or explicitly mark the workaround as temporary. When hot-fixing, install `lark-oapi` at the version already present under `/data/.openclaw/.hermes/python-deps` when possible, plus `aiohttp-socks==0.11.0`, set `PYTHONPATH`, and recheck imports after the final rollout/restart.

### 3. Per-wave hard gates

For each target in a wave, require:

- HerInstance/Deployment/pod env agree on image, deploy group, runtime profile, and `FEISHU_HOME_CHANNEL`.
- Active engine is the intended start engine, usually `openclaw`.
- OpenClaw `/healthz` returns 200 and logs show gateway ready plus Feishu WS ready.
- Real Feishu group `@` smoke reply from the target bot, not just the user's own message.
- Dify `health` and one `run` through `/data/.openclaw/local/bin/her-workflow-dify-creator` when enabled.
- A2A functional probe for at least paired canaries and representative later waves.
- Long-message and long-task pressure after basic smoke passes, unless the user explicitly requests smoke-only.
- `/hermes` and `/openclaw` only if the release claims dual-engine support; measure to real readiness, not marker change.

Use layered readiness:

| Layer | Meaning |
|---|---|
| `k8s_rollout_ready` | Deployment observed generation, target image, ready/available replicas all match |
| `deployment_hardening_ready` | Rendered Deployment has target base config, required H75 envs, internal URLs, initContainers, and writable H75 mounts |
| `runtime_files_ready` | H75 tools and generated configs exist in the pod |
| `openclaw_ready` | `active=openclaw`, `/healthz=200`, gateway ready, Feishu WS connected |
| `user_visible_ready` | Real Feishu marker reply from target app id |
| `footer_ready` | New card/footer reads the intended group mode; `group-at` shows `👥群@`, not `🔒主人@` |
| `hermes_ready` | `active=hermes`, Hermes Feishu WS connected, Redis `group-at`, real marker reply |

If a layer fails, do not jump to later layers until it is fixed or explicitly skipped.

### 3b. Smoke-only or deployment-only mode

Use this mode only when the user explicitly says not every target needs self-test, or asks to skip pressure tests.

- Still deploy through the manifest with UID guards and rollback snapshots.
- Run a full canary and representative Feishu/Dify/A2A smokes when possible.
- Do not run long-message, long-task, or repeated switch pressure; record `skipped_by_user`.
- Separate `deployed_health_ok` from `feishu_smoke_pass`. K8s Ready, OpenClaw health, Hermes dependency import, and Dify health do not prove a group `@` reply.
- For targets whose bot is invisible to the current operator, do not create fake regression groups. Report them as deployed but not self-tested.

Feishu smoke classification:

- `pass`: send succeeds and a target app-id reply containing the marker is found.
- `not_self_tested/current_operator_not_in_chat`: Feishu returns `Bot/User can NOT be out of the chat`.
- `automation_failed`: the send command is malformed, for example empty `--chat-id`; fix manifest/evidence ordering and rerun.
- `fail`: send succeeds but no target app-id reply arrives after the polling window.

### 4. Batch failure policy

- If a target is stuck in Hermes or stops replying, restore service first: set `/data/.engine/active` to `openclaw` and restart/roll only that target.
- If a current-pod hot fix is used, retest after the exact restart path that users will hit. Do not claim it survives a new Pod unless verified after rollout.
- If `lark-cli im +messages-send` returns JSON `ok:false`, stop and fix the send request; do not poll for a reply.
- If one wave needs manual runtime repair, update the skill/runbook before expanding.
- On ACK H75, check the rendered Deployment after Admin/operator apply. Current profiles can still need post-rollout hardening for internal Dify env, `FEISHU_HOME_CHANNEL`, and runtime plugin refresh.

H75 group-at/runtime-display failure policy:

- Treat group reply eligibility and footer display as separate surfaces. `group-at` is not fully accepted until the target can reply to a non-owner group `@` and new OpenClaw cards show `👥群@`.
- For image `h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-litellmchat-20260530`, if logs contain `message in group ... mentioned bot but sender ... is not owner`, the inbound gate is effectively falling back to owner-at. With no source-change authorization, mirror exact home-chat members or exact blocked senders into `FEISHU_OWNER_OPEN_IDS`, set Redis `group:mode:<chat>:<app>` to `group-at`, roll out, then scan all upgraded targets for the same signature.
- If the footer still shows `🔒主人@` while Redis says `group-at`, run the footer parser probe from the target pod. Older footer parsers can misread Redis bulk responses when the JSON contains non-ASCII `context`; rewrite `group:mode:*:<app>` values to ASCII-only JSON and retest with a new card.
- Do not claim old Feishu cards changed after a runtime fix. Only new replies/cards prove footer state.

Hermes switch failure policy:

- `/data/.engine/active=hermes` is not enough. Require Hermes Feishu WS and a marker reply before calling Hermes passed.
- The Hermes active marker can flip before Feishu WS is connected. After `/hermes`, wait for a real `connected to wss` log before sending the Hermes marker; a marker sent during that gap can be silently missed and must be rerun, not counted as a Hermes failure.
- If Hermes switch testing triggers container restart, wait for the pod to return ready, restore OpenClaw, and rerun final OpenClaw health/smoke.
- Do not run repeated switch-pressure tests when the user asked for smoke-only.

## Repeat-Failure Checklist

| Symptom | First check | Correct action |
|---|---|---|
| `无法识别飞书 chat_id` | Search the exact failing message and sender app | Register that exact chat as `carher.io/feishu-home-channel`, verify `FEISHU_HOME_CHANNEL` reached Deployment and pod, retest in the same chat |
| Canary passes but later users hit `无法识别飞书 chat_id` | Whether the canary already had home channel while other batch targets did not | Classify as `canary_scope_gap`. Add a pre-batch fleet gate that fails on `NO_HOME`, `mode!=group-at`, missing tracked set, non-ASCII Redis value, non-Running phase, or disconnected Feishu WS |
| Group message reaches Hermes but no reply | `feishu_seen_message_ids.json` has recent ids, but Hermes `gateway.log` has no raw message / inbound turn / response | Classify as Hermes admission/drop risk. If Hermes was not the intended final state, restore OpenClaw, roll out, then verify health and real Feishu smoke |
| Batch audit shows `latest_sent` and home differ | Whether there is a current failure in that latest_sent chat | Do not auto-patch. Treat as `latest_sent_mismatch_not_authoritative`; only act when latest failure/log evidence proves stale home |
| No usable self-test chat | Bot visibility to the current operator and bot-visible recent chats | Do not guess from group names. If the user approved deploy-only, leave home empty or keep the last bot-visible exact chat and report `not_self_tested` |
| Bot stops replying after `/hermes` | `/data/.engine/active`, Hermes logs, Feishu WS | Marker is not enough; require Hermes Feishu WS connected. If service is down, restore OpenClaw marker and roll before continuing |
| Hermes logs `lark-oapi not installed` / `No adapter available for feishu` | In pod: `/opt/hermes/.venv/bin/python3 -c 'import lark_oapi'`; compare S3 `hermestest-75` | ACK H75 image/profile is missing Hermes Feishu Python deps. Emergency workaround: install `lark-oapi` matching the existing Hermes deps version plus `aiohttp-socks==0.11.0` into `/data/.openclaw/local/hermes-python-packages`, set Deployment `PYTHONPATH` to that path, then retest after the final rollout/restart. This is current-pod/runtime repair and may be lost on new Pod; durable fleet fix is rebuilding the image/profile with the deps |
| Hermes Feishu WS connects but replies fail | Hermes logs around the test marker and provider/model config | If logs show LiteLLM/provider `TypeError: 'NoneType' object is not iterable`, channel is fixed but Hermes model path is not. Restore OpenClaw and report Hermes as partial, not pass |
| Hermes Feishu WS connects, marker eventually fails or is very slow, logs show HTTP 401 `token_not_found_in_db` | `/opt/data/.hermes/config.yaml` base URLs and ACK `OPENAI_BASE_URL`/`CARHER_PROD_KEY` | S3 images can carry S3 LiteLLM endpoint `https://cc.auto-link.com.cn/pro/v1` while ACK pods use `https://litellm.carher.net/v1` keys. Back up Hermes config, replace only the Hermes model `base_url` values with the ACK endpoint, then retest Hermes marker and restore OpenClaw |
| `/openclaw` from Hermes does not work | Hermes command-detect logs and active marker | Do not leave the bot in Hermes. Restore OpenClaw via marker/restart for that target, then debug Hermes command bypass before expanding |
| `/hermes` or `/openclaw` slow | Compare S3 `hermestest-75` image/entrypoint/env/mount/profile | Fix deployment/profile/prewarm/cache first. Same code can switch fast on S3 and slow on ACK. User accepted 40s target, but do not claim it unless measured to real ready; `CARHER_RUNTIME_PLUGINS_REFRESH=0` alone may not solve 100s-class ACK switching |
| Post-rollout OpenClaw/Dify probe fails immediately | H75 startup logs and tool/config existence | Treat as startup-window until the bounded retry period expires. Rerun health after tools/config appear; final report must use the latest probe |
| OpenClaw config fails with `Unrecognized key: "llm"` | Deployment `base-config` volume and included `carher-config.json` | Patch the Deployment to mount `carher-base-config-h75`. Do not start by editing source or PVC unless the old base config has already been ruled out |
| Gateway fails with `CARHER_GATEWAY_TOKEN is missing or empty` | Rendered Deployment env and SecretRef names | Add `CARHER_GATEWAY_TOKEN` from `carher-h75-runtime-secrets`, plus `ANTHROPIC_AUTH_TOKEN` and `CARHER_DIFY_BOOTSTRAP_TOKEN`; roll and re-read pod env |
| Container exits with `required secret env CARHER_PROD_KEY is missing` | Whether `CARHER_REQUIRED_SECRET_ENVS=CARHER_PROD_KEY` is set but `CARHER_PROD_KEY` is absent | Set `CARHER_PROD_KEY` to the same value as the instance `LITELLM_API_KEY`; verify equality in the rendered Deployment |
| Startup fails with `Read-only file system` under `/data/.agents/skills` or `/data/.openclaw/skills` | H75 writable volumeMounts still have `readOnly:true` | Use JSON Patch to replace the target container `volumeMounts`; strategic merge may not remove stale `readOnly:true` |
| Batch repair Job unexpectedly targets many already-upgraded Her | Executor flags and manifest count before apply | Stop the Job. Rerun without already-target inclusion flags unless this is an explicit repair wave with an approved impact count |
| Feishu smoke says `specify at least one of --chat-id or --user-id` | Manifest/evidence copy order | This is an automation failure from empty chat id, not a bot failure. Persist manifest first, then rerun smoke |
| Feishu send script fails with `unknown flag: --format` | `lark-cli im +messages-send --help` on the executor | Some installed lark-cli versions emit JSON by default but do not support `--format` on send. Remove `--format` or use `-q`; do not keep polling after a malformed send |
| Feishu smoke says `Bot/User can NOT be out of the chat` | Current operator membership in the exact home chat | Mark `not_self_tested/current_operator_not_in_chat`; do not create a fake group or call runtime failed |
| Hermes sees Feishu message id but no reply/logged turn | `feishu_seen_message_ids.json`, `gateway.log`, Redis `group:mode:<chat>:<app>` | If mode is missing/`owner-at`/legacy `discussion`, set the target home chat to `group-at` in Redis and retest a real group `@` marker. This is a runtime config fix, not a model failure |
| Group says `已开启 group-at` but non-owner `@` gets no reply | Current pod logs for `mentioned bot but sender ... is not owner`; Deployment `FEISHU_OWNER_OPEN_IDS`; Redis `group:mode:<chat>:<app>` | For H75 b600887, mirror exact home-chat members or exact blocked senders into `FEISHU_OWNER_OPEN_IDS`, set Redis mode to `group-at`, roll out, and scan every upgraded target for the same signature |
| Footer still shows `🔒主人@` after group-at is enabled | Run the same footer/card Redis parser from the target pod; compare with `redis-cli GET group:mode:<chat>:<app>` | If Redis mode is `group-at` but the parser reads empty, rewrite group-mode JSON to ASCII-only and retest with a new card. Old cards do not update |
| S3 reference tag assumption is wrong | `docker inspect hermestest-75` on S3 | Use the live S3 image digest as source of truth, mirror that digest to ACR, then patch ACK. Do not infer from ACK tag names like `hermestest75` |
| Batch patch creates many Pending pods | Deployment strategy and scheduler events `Insufficient cpu` | Many `maxSurge=1` rollouts need extra cluster CPU. For a controlled maintenance wave, temporarily use `maxSurge=0,maxUnavailable=1`, roll out, then restore strategy |
| initContainer crashes after JSON patch | Init command in Deployment and init logs | Local shell may have expanded `$SRC/$DST` or stripped Python quotes. Generate JSON patches with a structured encoder and verify the rendered command before rollout |
| Runtime env is fixed, then reverts after image patch | Deployment env after operator reconcile | Re-audit after every HerInstance image reconcile; patch internal Dify URL, `CARHER_RUNTIME_PLUGINS_REFRESH=0`, `PYTHONPATH`, and generated `dify-config.json` again if the operator/profile re-rendered old values |
| Dify `health` or `run` returns Cloudflare `403/1010` | `workflow/dify-config.json.dify_base_url` and `lifecycle_base_url` | ACK Her pods use `http://dify-nginx.dify.svc.cluster.local` for workflow API and `http://dify-bootstrap.dify.svc.cluster.local:5688` for lifecycle |
| Dify `run` returns HTTP 500 | Dify API logs, especially SQLAlchemy/psycopg2 DB connection errors | If Her config uses internal URLs and the Dify API log says the DB connection was closed while updating `api_tokens.last_used_at`, retry once before changing Her runtime config. If it persists, repair/restart Dify API/DB, not the Her |
| Dify Creator import returns `202 pending` and `app_id=null` | DSL version | `version: 0.3.1` needs `POST /apps/imports/<import_id>/confirm` within 10 minutes; use `version: 0.3.0` for direct `200 completed` regression |
| Dify helper not in PATH | `command -v` and known local bin | Use `/data/.openclaw/local/bin/her-workflow-dify-creator` |
| New app can publish/new-key but not run | `dify_base_url` | Lifecycle may be fixed while workflow API still points to public Cloudflare; fix both live config and Deployment env |
| Group `@` no reply | Bot membership and registered channel | Missing group membership/channel registration is not a model failure |
| Long-message or long-task failures | Bench with real Feishu messages and logs | Include long content and long-running task pressure after basic smoke passes |

## Required Upgrade Report

End every upgrade with:

- Target, image/profile, active engine, and S3 reference used.
- Fixes applied: CRD annotation, Deployment env, generated config, rollout.
- Evidence table: Feishu smoke, `/hermes`, `/openclaw`, Dify health/create/publish/run, A2A, long-message/long-task bench.
- Known residual risks and skipped checks.
- Skill updates made for new lessons learned.

## Upgrade Retrospective Loop

Use this loop whenever the user asks why repeated upgrades still produce repeated failures, or asks to "沉淀/优化 skill":

1. Convert every user-visible failure into a named failure class, for example `chat_id_unregistered`, `canary_scope_gap`, `home_channel_from_stale_history`, `latest_sent_mismatch_not_authoritative`, `active_engine_left_hermes`, `hermes_ws_not_ready`, `group_at_owner_gate`, `footer_parser_ascii`, `dify_public_url`, `missing_hermes_deps`, or `send_automation_malformed`.
2. Add or update exactly one gate for that class. The gate must be runnable from K8s or the target pod family, and must produce `pass`, `fail`, `skipped_by_user`, `not_applicable`, or `not_tested`.
3. Add a fleet scan for the same class before expanding waves. If the issue was found on one upgraded Her, assume it may exist on all upgraded Her until the scan says otherwise.
4. Add a rollback or service-restoration action for the class. For reply failures, restore OpenClaw first, then debug Hermes/Dify/model behavior.
5. Update the final report template so the class cannot disappear into prose. The report row must show the command/evidence used for the fix.

Anti-regression reminders:

- K8s/server-side validation is mandatory. macOS local shell success does not prove a K8s script, runtime env, or pod readiness.
- User-facing Feishu success requires a reply from the target `app_id`, not the user's own marker and not a different Her in the same group.
- `/hermes` success requires active marker, Hermes Feishu WS readiness, and a real Hermes reply. `/data/.engine/active=hermes` alone is only a transition signal.
- Footer/group-at must be verified as two paths: inbound reply eligibility and new card/footer display.
- Smoke-only means no long pressure and no switch-pressure. It does not mean skipping env, generated config, OpenClaw health, Dify health, or dependency gates.
