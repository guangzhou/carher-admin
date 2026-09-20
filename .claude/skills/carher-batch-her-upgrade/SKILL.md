---
name: carher-batch-her-upgrade
version: 1.0.7
description: "Use when upgrading many CarHer Her instances, planning a wave rollout, migrating a batch to H75/OpenClaw/Hermes/Dify, or when the user says many her need upgrade/regression."
metadata:
  requires:
    bins: ["kubectl", "jq", "lark-cli", "node"]
---

# CarHer Batch Her Upgrade

> Front door for many-Her upgrades. Use this to build the manifest, run waves, freeze on unknown failures, and delegate detailed checks to the existing CarHer upgrade skills.

## Required H75 Entrypoint

For H75/OpenClaw/Hermes/Dify batches, the default operational entrypoint is:

```bash
cd ~/codes/carher-admin
python3 scripts/h75-upgrade-repair-suite.py --mode canary
python3 scripts/h75-upgrade-repair-suite.py --mode batch --targets <ids...> --wave-size <n>
```

The suite runs the recurring repair/verification chain in order: Dify login-entry infrastructure repair, Dify API/bootstrap failure-log scan, 266/268 issue-login + auto-exchange canary smoke, K8s-side H75 runtime repair, generated `workflow/dify-config.json` audit, Hermes LiteLLM config audit, title failure card suppression audit, and current Ready pod selection. Future batch upgrades should not manually stitch together `dify-login-entry-repair.py`, `h75-runtime-repair.py`, or `h75-runtime-repair-runner.sh` except for targeted debugging after the suite identifies a failing gate.

## Load Order

1. `carher-upgrade-flow` for global gates and repeat-failure checklist.
2. `carher-upgrade-compare` for image/profile/source-vs-deploy comparison.
3. `carher-deploy` or `hot-grayscale` for rollout mechanics.
4. `carher-feishu-bench-regression` for Feishu smoke, switch, long-message, and long-task bench.
5. Project-local `carher-h75-dify-single-her-rollout` when H75/OpenClaw/Hermes/Dify is involved.

## Manifest First

Do not patch from a raw list of IDs. Create a TSV/JSON manifest with:

```text
her_id  metadata_uid  owner_name  app_id  bot_open_id  deployment_home_chat_id  tester_smoke_chat_id  current_image  target_image  current_group  target_group  runtime_profile  rollback_image  rollback_group  rollback_profile
```

Required checks:

- Every HerInstance UID is captured before mutation.
- `deployment_home_chat_id` is the exact target chat/latest failing chat seen by the target bot, not a guessed group name.
- `tester_smoke_chat_id` is optional and only means the current operator can send a Feishu smoke there; do not confuse it with the target bot's home channel.
- Target bot membership is verified for any chat used as `deployment_home_chat_id` or `tester_smoke_chat_id`.
- Current pod, Deployment env, active engine, Feishu WS, and Dify health are recorded.
- Rollback values are present for every target.
- User-visible acceptance requires an exact bot-visible home chat. Targets without home channel may be deployed only when the user explicitly accepts `not_self_tested`; do not call group `@` fixed for those targets.
- A canary that already has a valid home chat does not prove the batch manifest is complete. After canary and before any batch wave, run a full manifest audit and fail the wave if any target that is expected to support Feishu switch/reply has `deployment_home_chat_id` empty.
- When a target has multiple historical chats, prefer the latest failing switch card or latest command/reply log conversation over old `feishu-groups/index.json` or `feishu-sent-messages.json`. Historical local files are fallback evidence only; they are not enough when newer pod logs show a different active conversation.

If no bot-visible chat exists for a target and the user asks to deploy anyway, leave the home channel empty, deploy the image/profile, and report "not self-tested/no home channel" for that target. Do not invent a channel from a group name and do not block deployment if the user explicitly relaxed per-target self-test.

## Decompose Before Executing

Complex Her upgrades must be decomposed into small, automatable gates before the first mutation. Do not rely on memory or ad hoc manual sequencing.

Required gate table:

```text
gate_id  her_id  input  command_or_probe  expected  actual  status  evidence_path  fix_action
```

Use these simple gates, in this order:

- `manifest`: UID, image, group, profile, app id, home chat, rollback values.
- `deploy`: Admin/API apply result and rollout status.
- `runtime_env`: Deployment env and pod env for profile, Dify URLs, home channel, plugin refresh.
- `home_channel_fleet_audit`: every target has the intended `FEISHU_HOME_CHANNEL`, Redis `group:mode:<chat>:<app>` is `group-at`, `group:tracked:<app>` contains the chat, and the Redis value is ASCII-only.
- `active_engine_fleet_audit`: every target finishes in `openclaw` with `/healthz` live unless the user explicitly requested final Hermes state.
- `generated_config`: in-pod `workflow/dify-config.json`, engine marker, Hermes deps.
- `openclaw_health`: healthz, gateway ready, Feishu WS ready.
- `hermes_preflight`: `/hermes` real ready, Feishu WS connected, Redis group mode `group-at`.
- `smoke`: one marker send and reply search by target app id.
- `status_footer`: for card replies, verify the OpenClaw footer parser sees `group-at` and the expected footer is `👥群@`.
- `dify`: health plus create/confirm/publish/new-key/run when in scope.
- `a2a`: representative marker probe when in scope.
- `rollback`: recorded command and minimal readiness expectation.

Every gate must emit one of: `pass`, `fail`, `skipped_by_user`, `not_applicable`, `not_tested`. A report that says "all good" without gate rows is not acceptable for batch upgrades.

## Quality Bar For Fast Batch Upgrades

"Fast and high quality" means fewer manual branches, not fewer checks. Use this compression strategy:

- Run the complete gate matrix on one canary first, including generated config, OpenClaw reply, Dify, A2A when exposed, footer/group-at, and optional Hermes switch when dual-engine is in scope.
- Turn every canary fix into an automated batch gate before touching the rest of the list.
- For non-canary targets, keep the check set narrow but mandatory: manifest, rollout, env/config hardening, Hermes deps import, OpenClaw health, Redis group mode, recent-log signature scan, and one Feishu smoke only when the current operator is in the exact home chat.
- Do not run long-message, long-task, or switch-pressure during smoke-only batches. Mark those gates `skipped_by_user`.
- If any target has no exact home chat or the operator cannot send to that chat, deploy it only as `not_self_tested`; do not substitute a different group.
- A "fixed" batch requires a fleet scan for all failure classes found during the run. Scan results are part of the report, not optional commentary.
- `chat_id_unregistered` is a mandatory pre-batch scan even if only one canary was fully self-tested. Do not wait for `/hermes` to fail on a later user; missing home chat is discoverable from K8s state before any user-visible failure.
- `active_engine_left_hermes` is a mandatory post-batch scan. A target left in Hermes can have Feishu WS connected and still drop group messages before a turn starts; restore OpenClaw first, then debug Hermes.

Failure classes that must always have a scan after one hit:

```text
chat_id_unregistered
owner_gate_blocks_group_at
footer_parser_reads_empty_mode
missing_hermes_feishu_deps
dify_public_url_in_runtime
plugin_refresh_enabled_after_h75
active_engine_left_hermes
malformed_feishu_send_command
operator_reverted_runtime_env
initcontainer_shell_expanded_variables
bad_initcontainer_residual
emptydir_dependency_cache_missing
rollout_hidden_by_old_ready_pod
interrupted_feishu_write_may_have_succeeded
batch_maxsurge_capacity_deadlock
hermes_s3_litellm_endpoint_mismatch
home_channel_from_stale_history
latest_sent_mismatch_not_authoritative
dify_auto_login_invalid_nonce
sensitive_override_not_propagated
dify_issue_only_false_positive
dify_lifecycle_bot_id_drift
hermes_litellm_config_drift
title_failure_card_patch_missing
stale_failed_pod_pollutes_anomaly_scan
manual_probe_wrong_path_or_port
```

Recent ACK/S3 H75 lessons:

- The default H75 repair path is script-first: run `h75-upgrade-repair-suite.py`, then inspect the run directory. Do not manually patch one Pod and later try to remember what to automate; every new failure class must become a suite gate before rollout continues.
- Canary is not optional for target mutations. Keep 266/268 as runtime canaries for Dify login-entry, auto-exchange, Hermes LiteLLM config, title patch, and generated config before touching a new wave or a sensitive target.
- Do not treat "no new log errors" as a pass. For Dify/login/Hermes/footer issues, the pass condition is an active config read plus a reproduction action: issue-login + `/v1/exchange`, Hermes config parse, Redis group-mode read, or real Feishu `@` smoke when the exact chat is available.
- Do not infer the S3 reference image from an ACK tag name. Query the actual S3 container first, for example `docker inspect hermestest-75 --format 'image={{.Config.Image}} image_id={{.Image}}'`, then mirror that digest to ACR before patching HerInstance images.
- Local zsh does not split scalar variables on spaces. For batch target lists, feed explicit newline-separated ids or JSON arrays; do not use `printf '%s\n' $TARGETS` from zsh and assume it becomes many ids.
- Local macOS/bash/zsh is only a transport for triggering K8s-side runners. It is not a validation environment for shell semantics, runtime config, Feishu readiness, or Dify/Hermes behavior.
- When patching an initContainer command that contains `$SRC`, `$DST`, quotes, or Python `-c`, generate the JSON patch with Python or another structured encoder. Do not embed the patch in a shell string where local shell expansion can turn `$SRC` into an empty string or strip Python quotes.
- After an operator-triggered image reconcile, re-audit Deployment env and generated runtime config. The operator/profile can revert `CARHER_DIFY_BASE_URL` to public Cloudflare, set `CARHER_RUNTIME_PLUGINS_REFRESH=1`, and drop `PYTHONPATH`.
- S3 images can preserve `/opt/data/.hermes/config.yaml` model `base_url` values for the S3 endpoint, such as `https://cc.auto-link.com.cn/pro/v1`. ACK per-Her keys may belong to `https://litellm.carher.net/v1`; if Hermes logs HTTP 401 `token_not_found_in_db`, back up and rewrite the Hermes runtime config endpoint before judging Hermes generation failed.
- If many Deployments all use `maxSurge=1,maxUnavailable=0`, a batch template change can deadlock on insufficient cluster CPU because every target tries to schedule an extra pod. For a controlled maintenance wave, temporarily switch the batch to `maxSurge=0,maxUnavailable=1`, wait for rollout, then restore the original strategy.
- If the target image lacks Hermes Feishu deps, preinstall them into a PVC-backed cache and use an initContainer to copy that cache into the pod-local `/data/.openclaw/local/hermes-python-packages`; verify import after pod recreation, not before.
- If `/data/.openclaw/local` is an `emptyDir`, a one-time hot install is not durable. Add a rollout-time initContainer that mounts `h75-openclaw-local` at `/data/.openclaw/local` and installs or verifies `lark-oapi` plus `aiohttp-socks` into `/data/.openclaw/local/hermes-python-packages`.
- Do not keep a broken deps-copy initContainer such as one that writes `/data/.openclaw/local/...` without mounting `h75-openclaw-local`. It can leave an old healthy Pod serving traffic while the new ReplicaSet is stuck in `Init:CrashLoopBackOff`.
- The first successful canary can hide manifest completeness bugs. `carher-266` passed because it already had a home channel; later `carher-169` and `carher-78` failed the same class because they had no home channel. Treat canary success as image/runtime proof only, not fleet Feishu-registration proof.
- A `latest_sent` chat that differs from the home channel is only a stale-home candidate, not an automatic fix signal. Many Her instances legitimately reply in multiple groups or P2P chats. Only change home channel when the latest failing card, latest slash command, or latest pod log `received message ... in <chat>` proves the current user-visible failure is in that chat.
- If Hermes `feishu_seen_message_ids.json` records recent message ids but `gateway.log` has no received raw message / inbound turn / response, treat it as Hermes admission/drop risk. If the target is not intentionally under Hermes test, restore OpenClaw and rerun health/smoke before deeper Hermes analysis.
- If a Feishu group-member add command is interrupted by the user or transport, treat the write as unknown, not failed. Re-read the group member list before retrying or selecting replacements; the invite may already have succeeded.
- For high-sensitivity Her instances, such as `carher-2`, keep executors forbidden by default, but support an explicit, logged override only after the user names that Her again. Do not permanently delete the guard because future random batches must still avoid accidental inclusion. When using `h75-upgrade-repair-suite.py --include-sensitive`, verify the flag is propagated to every lower runner (`h75-runtime-repair-runner.sh` and `h75-runtime-repair.py`), otherwise the top-level run can appear selected while the actual runtime repair silently filters the sensitive target.
- Dify login smoke is not complete if it only verifies `/v1/user-login/<bot_id>/issue` returns a `login_url`. It must generate a test login URL and immediately call public `/v1/exchange?t=<token>` with a cookie jar; require `auto_exchange_ok=True` and do not print the token. Never consume a user's actual Feishu link in diagnostics.
- If `/auto?t=...` shows `链接已失效` while issue-login succeeds, check `dify-bootstrap` replicas and nonce storage. Two bootstrap replicas with process-local `_NONCES` can issue on replica A and consume on replica B, producing `invalid_nonce`. Fix via shared nonce storage under `/Data/dify-bootstrap/login-nonces` or an equivalent shared store; do not merely declare issue-login smoke green.
- If `collection-pod-anomalies.tsv` is large, do not conclude the fleet is broken until `collection-current-pod-anomalies.tsv` is inspected. Old Failed/Succeeded pods are stale cleanup evidence when a newer Ready pod exists; current-pod gates must use the Deployment selector, not guessed labels such as `her-id`.
- For `/dify` failures, read config before logs: Deployment env, `/data/.openclaw/workflow/dify-config.json`, `bot_id`, lifecycle URL, workspace/api/lifecycle token, and bootstrap shared nonce markers. Logs are incident evidence, not the primary detector.
- For Hermes "Unknown provider 'litellm'", do not switch to Claude or another fallback. Repair `/opt/data/.hermes/config.yaml` to `provider: litellm`, K8s internal LiteLLM endpoint, and `chat_completions`, then verify in the current Ready pod.
- For title/footer/card regressions, verify the actual generated card/footer path or title patch marker. A normal text reply does not prove card footer parsing is correct.

2026-06-04 ACK full-fleet H75 lessons:

- `Unrecognized key: "llm"` after H75 rollout is usually old base config, not source code. Check the Deployment `base-config` volume first; H75 targets must mount `carher-base-config-h75`, not `carher-base-config`.
- H75 runtime secret hardening is mandatory. The `carher` container must have `CARHER_GATEWAY_TOKEN` from `carher-h75-runtime-secrets`, `ANTHROPIC_AUTH_TOKEN` from `carher-h75-acp-secrets`, `CARHER_DIFY_BOOTSTRAP_TOKEN` from `carher-dify-bootstrap-token`, `CARHER_REQUIRED_SECRET_ENVS=CARHER_PROD_KEY`, and `CARHER_PROD_KEY` equal to the instance `LITELLM_API_KEY`.
- H75 writable mounts must be explicitly writable. If `h75-agent-skills`, `h75-openclaw-skills`, `h75-openclaw-local`, `h75-runtime-plugins`, `h75-openclaw-extensions`, `h75-hermes-skills`, or `h75-hermes-opt-skills` has `readOnly:true`, startup can fail with `Read-only file system`.
- Do not rely on strategic merge to remove old `readOnly:true` from `volumeMounts`. For array items where a field must be deleted or overwritten, use JSON Patch to replace the target container's `volumeMounts` array or verify the rendered Deployment has the intended value.
- Long batch executors must run as Kubernetes Jobs using `carher-admin` serviceAccount and ACR pull secrets. Do not run long Python executors inside the live `carher-admin` pod; it can OOM and affect admin service.
- Default batch mode must not include already-target CRDs. Treat flags such as `--include-target-crd` as special repair mode only after printing the impact count; otherwise a repair job can unnecessarily re-roll already healthy users.
- A successful `rollout status` is not enough. Final closure requires a fleet scan for non-`2/2 Running` pods, common failure states, base-config, H75 writable mounts, and final manifest count.

## Execution Contract

Treat the upgrade as three separate phases. Do not collapse them, and do not run dependent phases in parallel:

1. `manifest`: build and persist the exact JSON/TSV input first.
2. `apply`: run the deployment executor from K8s using only that manifest.
3. `verify`: wait for runtime readiness, then run probes and Feishu smoke.

Rules:

- The manifest must exist in both the run directory and the in-cluster executor location before any Feishu smoke or rollout probe starts. Never start smoke in parallel with manifest copy/export.
- Use a single run directory as the source of evidence. Copy in-cluster result files back only after the in-cluster command exits.
- K8s-side scripts must be validated and executed in the K8s-side environment. Local macOS/zsh is not a validation environment for shell semantics.
- Prefer Python with the Kubernetes client for in-cluster CRD/Deployment/pod operations. If shell is used, avoid `mapfile`, shell-specific arrays, and implicit word splitting; drive loops from JSON lines.
- Probes must use exact command arrays or small POSIX shell snippets that have already run in the target container family. A probe syntax error is a probe failure, not a service failure.
- A dependent probe may retry during startup, but it must record the startup-window state separately from final failure.
- If a previously excluded/high-sensitivity Her is later explicitly included by the user, record that override in the report and require the same UID guard, owner-group evidence, and verify-only evidence as ordinary targets.

## Automate The Repeated Checks

Before moving from canary to a batch, create or reuse a script that reads the manifest and writes TSV/JSON results. The script should:

- Run deployment executors and runtime probes from a Kubernetes-side environment, preferably the `carher-admin` pod or an approved in-cluster toolbox. The local laptop may be used only as a transport to copy/trigger scripts; never validate shell behavior or runtime readiness against macOS/zsh/bash.
- Use UID guards before applying changes.
- Patch only deployment/runtime/config surfaces that are allowed by the user.
- Re-read live K8s state after rollout instead of trusting the apply response.
- Poll Feishu replies by marker and target app id.
- Query Redis group mode for every home chat.
- Fail the summary when any upgraded target has `NO_HOME`, `mode!=group-at`, missing tracked membership, non-ASCII group-mode JSON, `phase!=Running`, or `feishuWS!=Connected`, unless the report explicitly marks that target `not_self_tested/user_accepted`.
- Fail the summary when any upgraded target has `active!=openclaw` or OpenClaw health is not live, unless the user explicitly requested final Hermes state for that target.
- Fail the summary when a Deployment has `status.replicas > spec.replicas`, an unavailable updated ReplicaSet, `ProgressDeadlineExceeded`, or any target Pod in `Init:CrashLoopBackOff`, even if one old Pod is still `2/2 Running`.
- For Hermes deps, verify after a fresh Pod recreation. A current `deps_ok` inside a reused Pod is insufficient if the deps live on an `emptyDir` and no correct initContainer exists in the Deployment template.
- Treat local/kubectl transport errors separately from service failures. If a batch audit row says `ERR` because of JMS EOF, client rate limiting, or local shell syntax, rerun that target with a longer timeout before marking the Her failed.
- Save raw evidence per target under a run directory.
- Produce a final summary table from evidence, not from notes typed by hand.
- After fixing any new failure on one Her, immediately scan every already-upgraded target for the same log signature/config defect. Do not wait for users to report the next instance.

If a new manual fix is needed, stop expanding, add that fix as an automated gate or documented runtime repair, rerun canary, then continue.

## Readiness Model

Use a layered readiness model so a target is not failed or passed too early:

- `k8s_rollout_ready`: Deployment observed generation, updated replicas, ready replicas, and target image all match.
- `runtime_files_ready`: expected tools and generated files exist, such as `/data/.openclaw/local/bin/her-workflow-dify-creator` and `workflow/dify-config.json`.
- `deployment_hardening_ready`: H75 `base-config`, required secret envs, internal URLs, fastbin/deps initContainers, and writable mount flags are correct in the rendered Deployment.
- `openclaw_ready`: active engine is `openclaw`, `/healthz` returns 200, gateway is ready, and Feishu WS is connected.
- `user_visible_ready`: a real Feishu `@` marker gets a reply from the target app id.
- `footer_ready`: the current card/footer path reads the same group mode as Redis/config; for `group-at`, new card replies should show `👥群@`, not `🔒主人@`.
- `hermes_ready`: active engine is `hermes`, Hermes Feishu WS is connected, Redis group mode is `group-at`, and a real marker gets a reply.

Startup windows:

- After a new H75 pod reports K8s Ready, allow a bounded startup window for OpenClaw gateway and Dify tools/config to finish bootstrapping. Poll every 5-10 seconds for up to 3 minutes before marking a runtime probe failed.
- If a probe fails because a tool is missing during startup, rerun after the startup window. Do not report the first transient result as the final state.
- If the container restarts during verification, pause user-visible tests, wait for `openclaw_ready`, then rerun the affected probe.

## Wave Policy

| Wave | Size | Purpose | Must Pass |
|---|---:|---|---|
| Canary | 1 | Prove image/profile and one real Feishu chat | Full matrix green |
| Paired canary | 2 | Prove A2A and per-user isolation | Both targets green, A2A both directions |
| Small batch | 3-5 | Prove operator/reconcile repeatability | No undocumented manual fix |
| Production wave | 10-20 | Scale only known-good path | Automated result table and rollback-ready |

Freeze the wave on any new failure class. Restore affected targets to OpenClaw or rollback image before deeper analysis.

## Preflight For H75/Hermes/Dify

Before the first wave:

```bash
kubectl exec -n carher "$POD" -c carher -- sh -lc '
  /opt/hermes/.venv/bin/python3 -c "import lark_oapi, aiohttp_socks"
  /data/.openclaw/local/bin/her-workflow-dify-creator health
'
```

Reference comparisons:

- S3 `hermestest-75`: switch latency, entrypoint hash, env, mounts, Hermes Feishu Python deps.
- S3 `hermestest-14`: internal Dify URLs and lifecycle behavior.

Rules:

- `lark-oapi` missing on ACK means the image/profile is incomplete for fleet rollout.
- A current-pod hot install under `/data/.openclaw/local/hermes-python-packages` can prove the diagnosis, but it is not durable across new Pods.
- Public `https://dify-k8s.carher.net` is not a Her runtime URL; ACK pods use `dify-nginx` and `dify-bootstrap` services.

## Per-Target Hard Gates

Each target must pass:

- HerInstance, Deployment, and pod agree on image/group/profile/home channel.
- Deployment mounts `carher-base-config-h75`, not the old `carher-base-config`.
- Required H75 env is present: `CARHER_GATEWAY_TOKEN`, `ANTHROPIC_AUTH_TOKEN`, `CARHER_DIFY_BOOTSTRAP_TOKEN`, `CARHER_REQUIRED_SECRET_ENVS=CARHER_PROD_KEY`, and `CARHER_PROD_KEY == LITELLM_API_KEY`.
- H75 writable mounts are not read-only: `h75-agent-skills`, `h75-openclaw-skills`, `h75-openclaw-local`, `h75-runtime-plugins`, `h75-openclaw-extensions`, `h75-hermes-skills`, `h75-hermes-opt-skills`.
- OpenClaw health 200, gateway ready, Feishu WS ready.
- Real Feishu `@` smoke reply from the target `app_id`.
- Dify `health` and one `run` when enabled.
- A2A functional probe for paired canary and representative later waves.
- Long-message and long-task pressure after smoke only when the user has not scoped the run to smoke-only.
- `/hermes` and `/openclaw` only when dual-engine support is in scope; measure to real ready.

Hermes pass requires both Feishu WS connected and a real marker reply. If Hermes logs LiteLLM/provider `NoneType object is not iterable`, Feishu ingress is fixed but Hermes generation is not; restore OpenClaw and mark Hermes partial.

## Hermes Switch Gate

Do not use Hermes switch testing as casual smoke for every target unless the user asks for dual-engine verification. When it is in scope:

- Measure `/hermes` and `/openclaw` to real readiness, not just `/data/.engine/active`.
- Do not send the Hermes marker during the handoff window. Wait for active engine, Hermes Feishu WS connected, and a short stability delay.
- If the Hermes process is reused and no new "connected" log appears, use current process health plus an existing connected state, then require a real marker reply.
- Always restore OpenClaw after Hermes testing. The final state must be `openclaw_ready` plus a final OpenClaw marker reply if the operator can send to the chat.
- If Hermes switch testing causes a container restart, record it, wait for the pod to return ready, and rerun OpenClaw health/smoke before reporting completion.

## Deployment-Only Or Smoke-Only Runs

When the user explicitly says not every target needs self-test, or asks for smoke-only:

- Deploy every target from the manifest using UID guards and rollback snapshots.
- Run one full canary plus representative Feishu/Dify/A2A smoke probes, unless the user narrows further.
- Do not run long-message, long-task, or switch-pressure tests; mark them `skipped_by_user`.
- Do not mark untested targets as Feishu passed. Use `deployed_health_ok` for K8s/OpenClaw/Dify/dependency checks and `not_self_tested` for Feishu.
- Keep targets with no home channel deployed only if the user approved it, and list them as requiring a real group registration before user-facing acceptance.

Feishu visibility handling:

- If `lark-cli im +messages-send` returns `Bot/User can NOT be out of the chat`, mark `feishu_smoke=not_self_tested/current_operator_not_in_chat`.
- Do not convert that error into a runtime failure and do not create a new test group as a substitute for the real home channel.
- If the send request itself is invalid, such as an empty `--chat-id`, treat it as an automation failure and rerun only after the manifest artifact is confirmed present.
- If an invite or message send was interrupted, do not assume rollback semantics. Fetch the authoritative chat/member/message state first, then continue idempotently.

## ACK H75 Post-Deploy Hardening

The H75 Admin/profile path can still render public Dify URLs or runtime plugin refresh defaults. After each rollout, audit and harden the live Deployment plus generated config:

- `CARHER_DIFY_BASE_URL=http://dify-nginx.dify.svc.cluster.local`
- `CARHER_DIFY_BOOTSTRAP_URL=http://dify-bootstrap.dify.svc.cluster.local:5688/v1/bootstrap/carher-bot`
- `CARHER_RUNTIME_PLUGINS_REFRESH=0` only after runtime plugins are already present in the image/profile.
- `FEISHU_HOME_CHANNEL` matches `deployment_home_chat_id` when one exists.
- `/data/.openclaw/workflow/dify-config.json` uses internal `dify-nginx` and `dify-bootstrap`, not `https://dify-k8s.carher.net`.
- Redis group mode for every home chat is `group-at` unless the owner explicitly wants owner-only Hermes group replies. Missing key, `owner-at`, and legacy `discussion` can make Hermes silently drop group `@` messages after `/hermes`.
- Redis group-mode values must be parseable by both gate code and footer/card code. Keep runtime workaround JSON ASCII-only; non-ASCII `context` strings can trigger older footer parsers to read the value as empty and display `🔒主人@` even when mode is functionally `group-at`.
- Remove stale or bad initContainers left from earlier hot fixes before judging the new image/profile. A healthy old Pod plus a failing new ReplicaSet is not a passed rollout; fix the template and wait for a single current `2/2 Running` Pod.
- Correct H75 Hermes deps initContainer pattern:

```sh
set -eu
DST=/data/.openclaw/local/hermes-python-packages
mkdir -p "$DST"
if PYTHONPATH="$DST" /opt/hermes/.venv/bin/python3 -c 'import lark_oapi, aiohttp_socks' 2>/dev/null; then
  echo hermes_feishu_deps_already_ok
  exit 0
fi
uv pip install --target "$DST" lark-oapi aiohttp-socks==0.11.0
PYTHONPATH="$DST" /opt/hermes/.venv/bin/python3 -c 'import lark_oapi, aiohttp_socks; print("hermes_feishu_deps_ok")'
```

Mount `h75-openclaw-local` at `/data/.openclaw/local` for that initContainer. Do not use a copy-only initContainer without the target volume mount.

Repeat this audit after Pod recreation. If the operator later reconciles these values away, fix the profile/source in an approved source-change window instead of silently relying on live patches.

### H75 b600887 group-at owner-gate hardening

Known affected image: `h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-litellmchat-20260530`.

This image contains a runtime OpenClaw Lark group-mode gate that can fail to read Redis group mode and fall back to `owner-at`. The symptom is that Redis says `group-at`, the UI/body may claim group-at, but logs contain:

```text
message in group oc_... mentioned bot but sender ou_... is not owner
```

When source/image changes are not allowed, use this deployment workaround before claiming group-at works:

- Run the probe from K8s/the target pod family, not macOS. Local laptop may only trigger `kubectl`.
- For each target with an exact home chat, set `group:mode:<chat_id>:<app_id>` to JSON `{"mode":"group-at",...}` and add the chat to `group:tracked:<app_id>`.
- Use the target pod's own Feishu app credentials to list current members of the exact home chat; mirror those member open_ids into Deployment `FEISHU_OWNER_OPEN_IDS`.
- Scan the current pod logs for observed owner-block lines. For every observed blocked chat/sender, set that chat to `group-at` and add at least the blocked sender to `FEISHU_OWNER_OPEN_IDS`; if Feishu member listing succeeds, mirror that observed chat's current members too.
- Roll out the Deployment and verify the new pod is `2/2 Running`, `FEISHU_GROUP_POLICY=open`, `FEISHU_ALLOW_ALL_USERS=true`, Redis mode is `group-at`, and the latest 5-minute logs have zero owner-block and zero `无法识别飞书 chat_id`.
- Verify the card/footer path separately. The `feishu-her` text footer and `@larksuite/openclaw-lark` streaming card footer may use different caches/parsers than the inbound gate. A functional `group-at` reply is not enough if new OpenClaw cards still show `🔒主人@`.
- If the footer parser sees an empty mode while `redis-cli GET group:mode:<chat_id>:<app_id>` shows `group-at`, rewrite that Redis JSON to ASCII-only, for example `{"mode":"group-at","context":"group-at runtime state for footer/gate parsers; ascii-only","set_by":"codex-upgrade-flow"}`.
- After fixing one target, normalize all upgraded targets' `group:mode:*:<app_id>` values that contain `group-at` to ASCII-only JSON, then run the same footer parser probe on at least the failing target and a representative batch target.

Important limits:

- This is a runtime bridge, not the durable fix. New group members or previously unseen groups may require the owner-id mirror to be refreshed until a corrected image/source patch is shipped.
- Do not set `FEISHU_OWNER_OPEN_IDS` from guessed users. Use exact Feishu member lists or exact blocked sender ids from logs.
- If home channel is empty, do not invent one. Deploy-only is allowed only as `not_self_tested/no_home_channel`.
- Old Feishu cards keep their old footer. Verify with a new reply/card after Redis normalization.

## Single-Her Fast Path

For a single target, keep the same gates but run them in a narrow sequence:

1. Build manifest in-cluster: UID, app id, bot open id, exact home chat, rollback values.
2. Copy manifest to the local run directory and in-cluster executor path.
3. Apply target image/profile/group/home with UID guard from the in-cluster executor.
4. Wait for rollout and H75 startup window.
5. Audit env, generated Dify config, OpenClaw health, Hermes deps, Dify health.
6. Run one Feishu marker only if the current operator is in the exact home chat.
7. Report `not_self_tested` separately from `deployed_health_ok`.

## Message Bench Contract

- Use short idempotency keys.
- Require `lark-cli im +messages-send` JSON `.ok == true` before polling.
- Search by marker plus target sender `app_id`.
- After every Hermes test, restore OpenClaw and run a final OpenClaw smoke.

## Completion Report

Report per wave:

- Target IDs, image/profile, S3 references used.
- Deployment/runtime fixes applied.
- Pass/fail table for Feishu smoke, long-message, long-task, `/hermes`, `/openclaw`, Dify, A2A.
- Switch timings measured to real ready.
- Residual risks and skipped gates.
- Skill/runbook updates made before expanding.

## Common Mistakes

- Upgrading from a raw ID list without UID/home-channel manifest.
- Calling Kubernetes Ready or active marker a pass.
- Continuing a wave after a current-pod manual fix.
- Treating missing group membership as model failure.
- Treating Dify `202 pending` as an async worker instead of a confirm flow.
- Leaving a bot in Hermes after a failed test.
- Claiming a target passed Feishu when only K8s/OpenClaw health was checked.
- Treating current operator chat visibility as equivalent to target bot home-channel registration.
- Claiming `/hermes` switch meets 40s unless measured to real Hermes Feishu ready; `CARHER_RUNTIME_PLUGINS_REFRESH=0` alone may not fix 100s-class switching on ACK.
- Missing Hermes Redis group mode: Feishu event can be seen/deduped but rejected before `Received raw message`, so logs look empty and the user sees no reply.
- Fixing one H75 b600887 group-at owner-gate failure but not scanning the upgraded fleet for the same `mentioned bot but sender ... is not owner` signature.
- Only mirroring the home group for a bot that is actively used in multiple groups; observed blocked groups need the same Redis/group-member or sender workaround.
- Treating `已开启 group-at` or a successful reply as proof the footer is fixed. Card footer rendering must be verified separately because older footer parsers can fail on non-ASCII Redis values and default to `🔒主人@`.
- Validating batch scripts on macOS/zsh and then discovering Linux/K8s shell differences during rollout. Validate in-cluster first; use deterministic iteration (`while read`, JSON-driven loops) instead of shell word-splitting.
- Running Feishu smoke before the manifest has been copied into the run directory, causing empty `--chat-id` and a false send failure.
- Treating the first post-rollout Dify/OpenClaw probe as final while the H75 entrypoint is still installing tools and writing generated config.
- Leaving evidence from an earlier failed probe as the final conclusion after a later retry passes. Final reports must cite the latest successful probe or clearly state both transient and final results.
