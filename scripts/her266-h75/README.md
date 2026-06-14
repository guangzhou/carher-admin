# her-266 H75/Dify single-instance rollout runbook

Goal: align only `carher-266` in ACK with the live `hermestest-75` OpenClaw/Hermes/Dify behavior while keeping the ACK Operator/Admin deployment model. Do not use `normal`, `stable`, `fast`, or fleet deploy paths for this rollout.

## Source of truth

Live S3 `hermestest-75` image:

```text
ghcr.io/buyitsydney/carher-runtime@sha256:b600887e7602dfdfd74128b80ea84e5f416107c4c7789a2bda53b41a18fc769b
```

Live image labels:

```text
carher.image.runtime_ref=68c11a88d8dd
carher.image.openclaw_overlay_ref=4f7012297075ce4c969a6f5c13eb98172250d657
carher.image.hermes_ref=f81ed4deb95752040c95de0b30204f8a8c14118c
carher.image.openclaw_base_image=ghcr.io/openclaw/openclaw@sha256:17a04e767f3097d08b0f31ecd753c5743f0e9c7e3ee613820f1e1d57d84efa4d
```

ACK fork branch `sync-from-buyitsydney/dev-latest` was at `119223bc988d94dadd8e8cdc8d11227d0a0b4554` during the rollout, but the H75 live image label points at upstream overlay commit `4f7012297075ce4c969a6f5c13eb98172250d657`. Use the live H75 labels as runtime source-of-truth.

## Final ACK targets

- Runtime image: `cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-litellmchat-20260530`
- Operator image: `cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-operator:her266-h75-profile-20260530-r16`
- HerInstance: `her-266`
- Kubernetes UID guard: `92629155-7299-4e5e-acd0-566e28a4234e`
- Deploy group: `beta-her-266`
- Runtime profile annotation: `carher.io/runtime-profile=h75-openclaw`
- Rollback image/group: `fix-compact-eb348941` / `stable`

## Build and image preparation

Run from the `carher-admin` repo root.

```bash
# Copy small pinned H75 config from S3 to local and k8s-work-227.
./scripts/her266-h75/05-fetch-h75-config.sh

# Upload scripts/operator source and start long builds on k8s-work-227 tmux.
./scripts/her266-h75/00-upload-and-start-build.sh

# Local packaging self-check; does not contact JumpServer.
PACK_ONLY=1 ./scripts/her266-h75/00-upload-and-start-build.sh

# Rerun remote tmux sessions only when intentionally replacing prior jobs.
FORCE_RESTART=1 ./scripts/her266-h75/00-upload-and-start-build.sh

# Poll remote tmux/logs/state.
./scripts/her266-h75/01-remote-status.sh
```

On `k8s-work-227`, uploaded scripts live under `/root/her266-h75-rollout/`:

```bash
./10-build-h75-runtime-from-source.sh
./11-verify-h75-image.sh
./12-build-operator-image.sh
./16-build-h75-runtime-entrypoint-patch.sh
./17-build-h75-runtime-hermes-feishu.sh
./18-build-h75-runtime-engine-command-bypass.sh
./19-build-h75-runtime-dual-engine-command-bypass.sh
./20-build-h75-runtime-chatid-fallback.sh
./21-build-h75-runtime-litellm-chat-completions.sh
```

If pinned source repos are unavailable on `k8s-work-227`, push the exact live H75 image from S3 to ACR and verify from the ACK build node:

```bash
# Run on JSZX-AI-03 with a temporary DOCKER_CONFIG containing ACR auth.
./15-push-h75-from-s3-local-image.sh

# Run on k8s-work-227; pulls through the ACR VPC endpoint.
./11-verify-h75-image.sh
```

## ACK rollout

The rollout script is guarded by the HerInstance UID and only updates instance `266`.

```bash
./scripts/her266-h75/20-ack-her266-ops.sh preflight
./scripts/her266-h75/20-ack-her266-ops.sh snapshot
./scripts/her266-h75/20-ack-her266-ops.sh apply-profile
./scripts/her266-h75/20-ack-her266-ops.sh upgrade
./scripts/her266-h75/20-ack-her266-ops.sh watch-verify
```

`apply-profile` creates or updates:

- `carher-base-config-h75`, derived from H75 `base.json5` and `docker.json5` with ACK-safe include paths.
- H75 runtime plugin paths for `acpx` and `carher-engine-swap`.
- H75 runtime, ACP, gateway, and Dify bootstrap Secret references. Secret values must not be printed or copied into docs.
- Operator image with opt-in H75 profile support.

If local tunnel/network is unstable, run the same ACK steps inside the cluster:

```bash
DIFY_BOOTSTRAP_TOKEN_FROM_SECRET=carher-dify-bootstrap-source \
  ./scripts/her266-h75/21-apply-ack-ops-job.sh
```

## Dify linkage checks

H75 behavior depends on this chain:

```text
CARHER_DIFY_* env
  -> dify-bootstrap-init
  -> /v1/bootstrap/carher-bot
  -> /data/.openclaw/workflow/dify-config.json
  -> her-workflow-dify-creator / her-workflow-dify-mcp
  -> /v1/lifecycle/<bot_id>
```

Required checks:

- `https://dify-k8s.carher.net/healthz` returns HTTP 200.
- `http://dify-bootstrap.dify.svc.cluster.local:5688/healthz` works from the `her-266` pod.
- `workflow/dify-config.json` exists and contains `bot_id=carher-266`.
- `her-workflow-dify-creator` and `her-workflow-dify-mcp` are executable.
- Lifecycle health returns HTTP 200 through public and in-cluster paths.

Current ACK Dify has stateless HA staged and verified: API, Web, Worker, bootstrap, and Nginx run as `2/2` replicas, pull mirrored ACR VPC images, and have PDBs with `minAvailable=1`. This is still not strict end-to-end HA because DB, Redis, Weaviate, plugin-daemon, sandbox, and ssrf-proxy remain single replica; DB/Redis/Weaviate also keep RWO PVCs.

Use the Dify stateless HA scripts when rechecking or rolling back this layer:

```bash
./scripts/her266-h75/61-mirror-dify-stateless-images.sh
./scripts/her266-h75/62-dify-stateless-ha.sh verify
./scripts/her266-h75/62-dify-stateless-ha.sh rollback .dify-ha-state/<rollback.tsv>
```

## Engine switch checks

For `/hermes` or `/openclaw` failures, check the command hook before the LLM path:

- `carher-engine-swap` is enabled in H75 config.
- `CARHER_RUNTIME_PLUGINS_REFRESH=1` is present for H75 profile pods.
- Feishu home channel fallback annotation/env is present, but redact its value in output.
- Logs show command interception and engine swap start/completion.
- Active engine changes after the switch; the reply should not be a normal chat response to `/hermes`.

## Post-rollout audit

Run the read-only audit after rollout and before declaring success:

```bash
./scripts/her266-h75/30-post-rollout-audit.sh
```

Expected high-level result:

```text
result=OK failures=0
isolation holds: HER_DEPLOYS=<count> H75_HER=1
```

The audit must not call `kubectl patch/apply/delete`, restart pods, or call Admin update APIs. It only reads K8s/Admin state, probes Dify health, and verifies target-pod files/env without printing secrets.

Run a real A2A message probe after the audit. This catches failures where the card/port exists but the agent returns no content:

```bash
FROM_HER_ID=268 \
PEER_URL=http://carher-266-svc.carher.svc.cluster.local:18800 \
EXPECT_TEXT=A2A_OK_266 \
./scripts/her266-h75/50-a2a-functional-probe.sh
```

If another H75 canary is already active, pass the full expected isolation set to the audit, for example `EXPECTED_H75_IDS=266,268`.

## Multi-user gray rollout

Use this only after `her-266` is healthy on the final H75 runtime image and ACK Dify/bootstrap is green. Keep the rollout opt-in per HerInstance; do not deploy to `stable`, `normal`, or a broad group.

Estimated time:

- Optimized path: 3-5 users in 20-45 minutes, 6-10 users in 45-90 minutes. Use this when image/operator/Dify are already green and targets are known.
- Conservative path: 3-5 users in 60-90 minutes, 6-10 users in 2-3 hours. Use this when a fixed human observation window is required.
- The speedup comes from prebuilt target/rollback manifests, parallel ACK watches, in-cluster Jobs, and remote tmux on Compose hosts.

Recommended wave shape:

```text
wave 0: collect target ids, UIDs, current image/group/profile
wave 1: 1 user, verify hard gates
wave 2: remaining users or 2-user step, depending on risk
wave 3: final audit and rollback-manifest retention
```

Before changing any target user, write a rollback manifest with:

```text
her_id
herinstance_name
k8s_metadata_uid
current_image
current_deploy_group
current_runtime_profile_annotation
target_image
target_deploy_group
```

The existing rollout script can be reused for a target user by passing explicit environment variables, but only after confirming the UID and rollback values:

```bash
HER_ID=<id> \
HER_K8S_UID_EXPECTED=<metadata.uid> \
NEW_IMAGE_TAG=h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-20260530 \
NEW_DEPLOY_GROUP=beta-h75-<id> \
ROLLBACK_IMAGE_TAG=<current_image> \
ROLLBACK_DEPLOY_GROUP=<current_deploy_group> \
./scripts/her266-h75/20-ack-her266-ops.sh preflight
```

For several users, prefer an ACK in-cluster Job or a small wrapper that loops over a checked target manifest. Local networks should not own long `watch` loops.

Fast gray script:

```bash
./scripts/her266-h75/40-fast-gray-rollout.sh plan targets.tsv
./scripts/her266-h75/40-fast-gray-rollout.sh snapshot targets.tsv rollback.tsv
./scripts/her266-h75/40-fast-gray-rollout.sh apply rollback.tsv
./scripts/her266-h75/40-fast-gray-rollout.sh verify rollback.tsv
./scripts/her266-h75/40-fast-gray-rollout.sh rollback rollback.tsv
```

`40-fast-gray-rollout.sh verify` only proves the Deployment is on the target image and available. Always follow it with `30-post-rollout-audit.sh` and `50-a2a-functional-probe.sh`; in the 2026-05-30 run, Deployment Ready happened within seconds but functional A2A/Dify readiness lagged by several minutes while OpenClaw extensions were copied.

Target file format:

```text
ack     <her_id> <k8s_uid> <target_image_tag> <target_deploy_group>
compose <host>   <project> <compose_files_csv> <service> <target_image_ref>
```

ACK targets are changed by runtime-profile annotation plus Admin API. Compose targets are changed by adding a temporary override file, so the original compose file remains the rollback source.

Acceptance per wave:

- Every target Pod is Ready and on the target image.
- Feishu WS is connected for each target.
- `/hermes` and `/openclaw` switch engines for at least one representative target per wave, and any user reporting issue gets individual verification.
- Dify health/bootstrap/config/lifecycle checks pass for each target.
- Real A2A probes return the expected marker text in both directions for at least one representative pair.
- Full Deployment audit shows H75/profile only on the target set.

## 2026-05-30 her-266/her-268 validation notes

- `her-266` and `her-268` both rolled to `h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-litellmchat-20260530` with H75 profile isolation.
- Root cause fixed during validation: ACK Hermes `chatgpt-pro` failed with LiteLLM `codex_responses`; switching the H75 Hermes template to `chat_completions` made direct Hermes and A2A return content.
- `her-268` rollback to `stable / fix-compact-eb348941 / no runtime-profile` completed in 46 seconds at Deployment level in the repeat validation.
- Re-gray of `her-268` back to H75 completed in 41 seconds at Deployment level. Functional readiness still lagged until OpenClaw extension dependencies finished copying and A2A opened, about five minutes after Pod creation in the repeat validation.
- Verified markers after the repeat validation: direct H75 audit on both `her-266` and `her-268`, A2A `her-268 -> her-266`, and A2A `her-266 -> her-268`.
- Dify stateless HA verified after rollout: `dify-api`, `dify-web`, `dify-worker`, `dify-bootstrap`, and `dify-nginx` are `2/2` with ACR VPC images and PDBs; stateful strict HA remains a separate phase.
- Remaining optimization: avoid full extension copy on every H75 cold start, and move DB/Redis/vector store to managed HA or stateful HA before claiming strict Dify high availability.

## Rollback

Rollback restores the old deploy group/image and removes the H75 profile annotation:

```bash
./scripts/her266-h75/20-ack-her266-ops.sh rollback
```

Rollback is mandatory if any of these persist longer than the agreed triage window:

- `carher-266` rollout does not become Ready.
- Feishu WS is disconnected.
- `/hermes` or `/openclaw` cannot switch engines.
- Dify bootstrap/lifecycle fails and blocks workflow behavior.
- H75 image/profile appears outside `carher-266`.

H75 ConfigMap/Secret and ACR tags may remain in place; without the opt-in annotation, old HerInstances do not consume them.

For multi-user rollback, restore each user's recorded original image, deploy group, and runtime profile state. The rollback guarantee is operational: the old values are snapshotted and can be restored per user. It does not mean zero interruption; each rollback still recreates that user's Pod and requires Feishu WS reconnect.

Targets:

- ACK single user rollback: 3-8 minutes.
- Compose single container rollback: 2-6 minutes.
- 3-5 user wave rollback across ACK/Compose: 10-20 minutes, submitted in parallel and watched individually.

If the confirmed issue is common to both environments, such as Dify, Cloudflare, H75 runtime, or engine-swap, freeze expansion and roll back every changed target from the same rollback manifest. If the issue is isolated to one user, roll back only that user and keep the rest under observation.

## Documentation

- Artifact index: `docs/her266-h75-session-artifacts.md`
- Retrospective source: `docs/her266-h75-dify-retrospective.md`
- Reusable project skill: `.codex/skills/carher-h75-dify-single-her-rollout/SKILL.md`
- Upstream reference snapshots: `docs/references/carher-upstream/`
- Dify HA read-only audit: `scripts/her266-h75/60-dify-ha-audit.sh`
- Dify stateless HA mirror/apply/verify: `scripts/her266-h75/61-mirror-dify-stateless-images.sh` and `scripts/her266-h75/62-dify-stateless-ha.sh`

Keep this runbook, the H75/Dify skill, Feishu board exports, and future session notes in `carher-admin`. Do not add rollout-specific docs or skills to the upstream `../CarHer` repo; copy upstream material here as a dated reference snapshot when needed.
