# her-266 H75 / S1-S3 White-Box Test Plan

> Scope: current ACK H75 image for `her-266` / `her-268`, the corresponding
> OpenClaw overlay code, the H75 runtime glue, and the internal S1-S3 Docker
> line. This document is intentionally operational and evidence-driven. Do not
> add secrets, full chat IDs, app secrets, API keys, cookies, or temporary login
> URLs here.

## 1. Evidence Baseline

### 1.1 Target Image And Code Provenance

Current ACK runtime image:

```text
cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-litellmchat-20260530
```

Live provenance from `/etc/carher-release.json` in the ACK pod:

| Layer | Ref / Image |
| --- | --- |
| OpenClaw overlay | `4f7012297075ce4c969a6f5c13eb98172250d657` |
| OpenClaw base image | `ghcr.io/openclaw/openclaw@sha256:17a04e767f3097d08b0f31ecd753c5743f0e9c7e3ee613820f1e1d57d84efa4d` |
| Hermes ref | `f81ed4deb95752040c95de0b30204f8a8c14118c` |
| Hermes base image | `docker.io/nousresearch/hermes-agent@sha256:02475b70b176b92f046120301de05afe63434971426c9feeb7a91ac4e73fd54a` |
| Runtime ref | `68c11a88d8dd` |
| Contract | `official-images-plus-locked-patches` |
| Platform | `linux/amd64` |

Current local upstream code checkout:

| Repo | Branch | HEAD |
| --- | --- | --- |
| `../CarHer` | `dev_new` | `4f7012297075ce4c969a6f5c13eb98172250d657` |
| `carher-admin` | `main` | `83599bbb207282d822635fd49bb92fb402485b90` |

Source-of-truth rollout artifacts:

- `scripts/her266-h75/README.md`
- `docs/her266-h75-dify-retrospective.md`
- `docs/her266-h75-session-artifacts.md`
- `scripts/her266-h75/*.sh`
- Upstream reference snapshots under `docs/references/carher-upstream/`

### 1.2 Current ACK Scope

| Her | Image | Runtime profile | Deploy group | Feishu WS | Notes |
| --- | --- | --- | --- | --- | --- |
| `her-266` | H75 `litellmchat` | `h75-openclaw` | `beta-her-266` | Connected | Has current group-mention REST poller workaround in deployment `postStart` |
| `her-268` | H75 `litellmchat` | `h75-openclaw` | `beta-h75-268` | Connected | Used as peer for rollback / re-gray / A2A probes |

Isolation expectation for current state: exactly `266,268` are allowed to match
the H75 image/profile set. Any other Her matching this image or profile is a
test failure unless explicitly added to the expected target set.

### 1.3 S1-S3 Internal Baseline

Read-only Docker inspection on 2026-05-30 showed:

| Host | Asset | Observed functions |
| --- | --- | --- |
| S1 | `JSZX-AI-01` / `jszx-ai-186` | `hermestest-13`, `hermestest-199-dual`, `hermestest-200`, legacy `carher-12`, local registry, Redis, fallback nginx |
| S2 | `JSZX-AI-02` / `jszx-ai-187` | Dify 1.4.2 raw stack, `dify-bootstrap`, `carher-221`, fallback nginx |
| S3 | `JSZX-AI-03` / `jszx-ai-188` | `hermestest-75` H75 reference, `hermestest-14/265/267/1001`, ChatGPT LiteLLM shards, Anthropic LiteLLM shards, Claude Max proxy, OpenRouter Opus proxy, Cloudflare tunnel, fallback nginx |

S3 `hermestest-75` is the behavioral reference for H75. Its current runtime
ref differs from ACK runtime ref (`43deaffc39dc` vs ACK `68c11a88d8dd`), but
it shares the same OpenClaw overlay ref and Hermes ref. Treat ACK image labels
as ACK source-of-truth, and S3 H75 as behavior source-of-truth.

## 2. Function Inventory

This section lists the user-visible and ops-visible functions that should be
covered by the white-box plan. It groups the functionality by implementation
surface rather than by UI menu.

### 2.1 Base OpenClaw Runtime

Evidence: `../CarHer/src`, `../CarHer/package.json`, `../CarHer/docs/her/*`,
and the H75 image release metadata.

| Function area | Included behavior |
| --- | --- |
| Agent run loop | Session dispatch, model calls, tool calls, streaming/non-streaming responses, retries, turn timeout, failover policy, command delivery |
| Model provider runtime | OpenAI-compatible chat/responses, Anthropic-style messages, Gemini/Google transport, model aliases, model status, auth profiles, fallbacks |
| CLI and daemon | `openclaw agent`, `gateway`, `channels`, `models`, `plugins`, `cron`, `config`, `doctor`, `daemon`, `node/nodes`, `mcp`, `proxy`, `hooks`, `security`, `logs`, `docs`, `tui` |
| Gateway control plane | WebSocket gateway, local gateway API, auth token, status/probe/discovery, usage/cost, protocol schema |
| Channels | Core/plugin channel registry, Telegram/Discord/Slack/Signal/iMessage/Web, plus bundled channel docs; for this image the Feishu channel is the relevant production channel |
| Sessions and memory | Session store, compaction, context-window guard, memory flush, memory search, SQLite/vector/FTS backends, prompt-cache stability |
| Tools | Bash/PTY/process tools, file edit/apply patch, media/image/file handling, channel tools, MCP tools, web fetch/search, sandbox policy |
| Cron/automation | Cron store, legacy payload migration, isolated cron agent runs, channel delivery |
| Plugin platform | Manifest validation, plugin install/enable/disable/update, runtime services, public SDK, package boundary |
| Media and realtime | Image/audio/video processing, TTS, realtime transcription/voice, WebRTC-related surfaces |
| Pairing/nodes | QR pairing, paired node camera/screen/canvas/location/notify/invoke APIs |
| Safety/diagnostics | Doctor, security audit, auth health, state integrity, sandbox warnings, import-cycle checks |

### 2.2 CarHer Bundled Plugins In The Overlay

Evidence: `../CarHer/docker/plugins/*`.

| Plugin | Functions |
| --- | --- |
| `feishu-her` | Feishu WS gateway, DM/group routing, owner gating, mention parsing/rendering, rich text/card streaming, image/file/audio/video upload/download, reactions, reply/thread metadata, sent-message archive, group archive, group modes, discussion mode, Feishu tools for chat/docs/sheets/wiki/tasks/calendar/mail/minutes/bitable/board/drive/search/directory, OAuth/device flow, memory bridge |
| `a2a-gateway` | A2A v0.3 JSON-RPC/REST/gRPC fallback, Agent Card, SSE status, FilePart/DataPart/TextPart, peer registry, DNS-SD/mDNS discovery, peer health/circuit breaker, push notifications, bearer token rotation, SSRF/file guardrails, JSONL audit, telemetry, durable task store |
| `shadow-daemon` | Document-to-markdown mirror for PDF/Word/Excel/PPT into `memory/_shadow`, markitdown bootstrap, periodic reconcile, extraction timeout/budget, semantic recall integration |
| `her-antitalker-poc` | Stop-hook / anti-talker proof-of-concept rules pipeline; not a default production gate unless explicitly enabled |

### 2.3 H75 Runtime Glue In The Image

Evidence: `/etc/carher-release.json`, `/entrypoint.sh`, `/runtime-patches`,
`scripts/her266-h75/*.sh`, and live pod checks.

| Function area | Included behavior |
| --- | --- |
| Dual engine | `/data/.engine/active` selects `openclaw` or `hermes`; entrypoint execs the active engine |
| Engine switch | Feishu `/hermes` and `/openclaw` are intercepted by runtime glue instead of entering normal LLM chat; active marker changes; process restarts into target engine |
| Hermes gateway | Official Hermes gateway with CarHer patches, Feishu adapter, context anchor, card output, footer metadata, pending catch-up, quick model commands |
| OpenClaw runtime | OpenClaw gateway with H75 base config and runtime plugins copied/refreshed into the container volume |
| A2A bridge | Port `18800` exposes A2A JSON-RPC; `hermestest/scripts/hermes-a2a-server.py` bridges Hermes to A2A |
| ACP/acpx | ACP enabled with `acpx`, Claude agent ACP, Codex ACP adapter, canonical state roots under `/data`, guarded env passthrough |
| Dify workflow tools | `dify-bootstrap-init`, `her-workflow-dify-creator`, `her-workflow-dify-mcp`; per-bot `workflow/dify-config.json` |
| Model routing | ACK H75 Hermes uses `chatgpt-pro` through LiteLLM `chat_completions`; additional Wangsu/OpenRouter/LiteLLM-chat providers and quick commands exist |
| Feishu home channel fallback | ACK annotation/env gives Hermes a home channel target for switch cards and lifecycle notices; values must stay redacted |
| Runtime patch idempotency | `apply-*.sh` and runtime glue patches are re-run at container start and must remain idempotent |
| Group-mention fallback | Current her-266 deployment has a `postStart` script that starts a scoped REST poller for `hers回归测试` while native Feishu group WS delivery is blocked |

### 2.4 ACK Operator/Admin Functions For H75

Evidence: `carher-admin/AGENTS.md`, `operator-go`, `backend/config_gen.py`,
`scripts/her266-h75/operator-h75-profile.patch`, live CRDs and deployments.

| Function area | Included behavior |
| --- | --- |
| HerInstance CRD | Spec image/deploy group/app config; status phase/Feishu WS; runtime profile annotations |
| H75 profile | Opt-in annotation `carher.io/runtime-profile=h75-openclaw`; injects H75 base config, Dify env, ACP env, runtime plugin refresh, A2A/ACP ports |
| Image/config reconcile | Operator writes per-user config, base config, PVC, Service, Deployment/Pod; config hash drives rollouts |
| Admin API | Instance update, deploy group/image changes, UID guard, audit/deploy records |
| Dify HA in ACK | Dify API/Web/Worker/bootstrap/Nginx stateless layer at 2 replicas with PDBs and ACR VPC images |
| Isolation guard | H75 image/profile must only hit the target Her set |
| Rollback | Per-Her restore of prior image/deploy group/runtime profile; no broad stable/normal deploy path |

### 2.5 S1-S3 Internal Functions

| Host | Functions to cover |
| --- | --- |
| S1 | Legacy and H75-like Compose Her runtime; OpenClaw/Hermes dual images; ACP/acpx and A2A on newer `hermestest-*`; local Redis / local registry; fallback nginx; old `carher-*` naming and newer `hermestest-*` naming coexist |
| S2 | Dify raw stack (`api`, `web`, `worker`, `nginx`, `db`, `redis`, `weaviate`, `sandbox`, `ssrf_proxy`, `plugin_daemon`); `dify-bootstrap` endpoints `/healthz`, `/v1/bootstrap/carher-bot`, `/v1/lifecycle/*`, `/v1/user-login/*`, `/v1/exchange`, `/auto`; `carher-221` as a non-H75/older dual-runtime reference |
| S3 | H75 behavioral reference (`hermestest-75`); other H75-like canaries (`265`, `267`); LiteLLM ChatGPT shards; LiteLLM Anthropic shards; Claude Max proxy; OpenRouter Opus proxy; Cloudflare tunnel; fallback nginx; Dify tools installed in H75 containers |

## 3. White-Box Test Strategy

### 3.1 Principles

1. Test the real user path for every e2e claim: Feishu message in, actual
   bot reply out, actual target system inspected.
2. Separate static/unit checks from integration and live e2e. Do not call a
   simulated harness "e2e".
3. Every rollout gate must have a rollback proof: snapshot original image,
   deploy group, profile annotation, and observed health before mutation.
4. Treat ACK and S1-S3 as two tracks. The same behavior can be validated by
   different commands, but the expected evidence must be equivalent.
5. Secrets are never evidence. Evidence should be hashes, status, marker text,
   redacted configs, or scoped health responses.

### 3.2 Test Levels

| Level | Purpose | Example command / evidence |
| --- | --- | --- |
| Static code inventory | Prove target code and plugin surfaces exist | `git rev-parse HEAD`, plugin manifest parse, `rg` over source modules |
| Unit | Prove local contracts and regressions | `pnpm test <file>` in `../CarHer`; `go test ./internal/controller/...` in `operator-go`; `pytest backend/tests/...` |
| Integration | Prove container/config wiring | `30-post-rollout-audit.sh`, pod file checks, config hash checks, Dify health |
| Live functional e2e | Prove real user/system behavior | Feishu DM/group marker messages, `/hermes`/`/openclaw`, A2A marker, Dify workflow run |
| Resilience/rollback | Prove recovery path | `40-fast-gray-rollout.sh rollback`, deployment rollout status, Feishu reconnect |
| Soak/negative | Prove absence of leaks/storms | isolation audit, discussion anti-loop tests, memory/CPU/log scan, duplicate reply scan |

## 4. Test Matrix

### 4.1 Provenance And Build Reproducibility

| ID | Target | White-box assertions | Evidence |
| --- | --- | --- | --- |
| P1 | ACK image labels | Release metadata contains expected OpenClaw/Hermes/runtime refs and platform | `kubectl exec ... cat /etc/carher-release.json` |
| P2 | Local code match | `../CarHer` HEAD equals OpenClaw overlay ref | `git -C ../CarHer rev-parse HEAD` |
| P3 | H75 build scripts | Build chain records source image, base image, entrypoint patches, Feishu patches, dualswitch, chatid fallback, LiteLLM chat transport | `scripts/her266-h75/{10,16,17,18,19,20,21}-*.sh` review |
| P4 | Image pull path | ACK pod image uses ACR VPC registry, not GHCR/Docker Hub | Deployment image inspection |
| P5 | Runtime patches | `/runtime-patches/*.sh` and `/patches/apply-*.sh` apply idempotently | Container startup logs and repeated restart |

### 4.2 ACK H75 Deployment And Isolation

| ID | Target | White-box assertions | Evidence |
| --- | --- | --- | --- |
| K1 | HerInstance | `her-266` and `her-268` are Running and Feishu WS Connected | `kubectl get herinstance her-266 her-268 -o json` |
| K2 | Deployment | pods are `2/2 Running`, image tag matches target, node placement is valid | `kubectl get pods -l user-id in (...) -o wide` |
| K3 | Profile isolation | only expected target IDs have H75 image/profile/chatidfix | `EXPECTED_H75_IDS=266,268 ./scripts/her266-h75/30-post-rollout-audit.sh` |
| K4 | Config generation | H75 base config and per-user config contain required plugin paths/env and no secret literals | ConfigMap and pod redacted grep |
| K5 | Lifecycle hook | her-266 `postStart` preserves `preStop`, starts group poller only when active engine is Hermes | Deployment lifecycle JSON and poller log |
| K6 | Rollout readiness vs functional readiness | Deployment Ready is not accepted until A2A/Feishu/Dify functional checks pass | compare rollout status time and functional marker time |

### 4.3 Feishu DM / Group / Discussion

| ID | Target | White-box assertions | Evidence |
| --- | --- | --- | --- |
| F1 | DM native WS | User DM reaches Hermes/OpenClaw via WS and returns marker | Feishu send + gateway log `Received raw` + message history reply |
| F2 | Group native WS | Group `@阿里云beta的her` should reach native WS; current known issue is no group raw event | Feishu history has user message; gateway log lacks group raw event |
| F3 | Group REST poller workaround | her-266 poller sees new target mentions, calls local A2A, replies to original group message | `her266-group-poller.log` handling/replied lines + Feishu group history |
| F4 | Mention parsing | Feishu mentions strip correctly; prompt becomes user text, not raw mention key | `mention-text.test.ts`, live group `hi`/Chinese prompt |
| F5 | Rich card output | Responses render card/footer correctly; no title-generation error card leaks | message history and log scan for `Auxiliary title generation failed` |
| F6 | Attachments | image/file/audio/video in Feishu download/upload paths work or fail with precise user-facing diagnostics | plugin tests + live small file/image smoke |
| F7 | Group modes | default, auto-reply owner, at-reply, group, discussion route correctly without bot loops | `group-mode`, `discussion-state`, `gateway.discussion-*` tests + 3-bot group smoke |
| F8 | Feishu tools | docs/sheets/wiki/task/calendar/mail/minutes/bitable/board/drive/search/directory tools call with scoped OAuth and proper permission errors | targeted tool unit tests + one live safe read/write per domain |

### 4.4 Engine Switching

| ID | Target | White-box assertions | Evidence |
| --- | --- | --- | --- |
| E1 | `/hermes` from OpenClaw | command intercepted, switch card shown, marker changes to Hermes, Hermes connects Feishu | Feishu command, `/data/.engine/active`, Hermes logs |
| E2 | `/openclaw` from Hermes | command intercepted, marker changes to OpenClaw, OpenClaw starts Feishu gateway | Feishu command, process list, logs |
| E3 | Command bypass | slash command is not sent as normal prompt to LLM | absence of model call for command text; switch log lines |
| E4 | Cold start | extension/plugin copy finishes; A2A opens; functional readiness eventually succeeds | process/listening port checks + A2A marker |
| E5 | Shutdown/drain | preStop/drain sends interruption notice only once and does not corrupt sessions | rollout logs + no duplicate pending deliveries |

### 4.5 Hermes Runtime

| ID | Target | White-box assertions | Evidence |
| --- | --- | --- | --- |
| H1 | Hermes config | `chatgpt-pro` uses LiteLLM `chat_completions` in ACK H75 | `/opt/data/.hermes/config.yaml` redacted grep |
| H2 | Quick commands | `gpt`, `gpt-5.4`, `codex`, `opus`, `opus4.7`, `sonnet`, `gemini`, `glm`, `minimax`, `ds-*`, `gemini35`, `glm51` map to expected providers | config quick_commands + live `/model` smoke |
| H3 | Direct Hermes response | `hermes -z` or A2A local returns exact marker | in-pod direct/A2A probe |
| H4 | Auxiliary errors | title-generation or background review failures stay in logs unless intentionally surfaced | log scan + no Feishu error card |
| H5 | Session expiry | idle expiry finalizes sessions without losing new turns | gateway session expiry logs + subsequent DM marker |
| H6 | Card output patch | Hermes card renderer supports Feishu card output and footer metadata | live DM card + unit/read-only source anchor check |

### 4.6 OpenClaw Runtime

| ID | Target | White-box assertions | Evidence |
| --- | --- | --- | --- |
| O1 | Gateway boot | OpenClaw gateway starts with Feishu plugin and H75 runtime plugins | logs and `openclaw plugins list` in pod |
| O2 | Agent tools | bash/file/apply patch/channel tools are available and policy-bound | `openclaw agent` smoke in disposable session |
| O3 | Model routing | default and aliases route to expected LiteLLM/OpenRouter/Wangsu providers | model status/list and LiteLLM spend logs by key alias |
| O4 | Compaction/context | long prompt triggers compact/context guard without corrupting recent bytes | targeted compaction tests + synthetic long-turn live smoke |
| O5 | Memory search | memory load/search/flush works; group MEMORY leakage does not occur | memory tests + controlled memory question |
| O6 | Cron | cron delivery reaches active channel and does not use stale legacy payloads | `cron` command tests + one disabled/safe cron dry run |
| O7 | MCP | MCP config/list/serve and channel bridge surfaces remain loadable | `pnpm test src/cli/mcp-cli.test.ts` + config smoke |

### 4.7 A2A / ACP

| ID | Target | White-box assertions | Evidence |
| --- | --- | --- | --- |
| A1 | A2A port | port `18800` accepts JSON-RPC and agent card | service/pod port + curl/A2A script |
| A2 | 268 -> 266 | peer sends exact marker and receives exact marker | `50-a2a-functional-probe.sh` |
| A3 | 266 -> 268 | reverse direction exact marker | `50-a2a-functional-probe.sh` |
| A4 | A2A security | bearer token, SSRF guard, file size/MIME guard, audit log, task TTL | plugin tests and redacted audit event |
| A5 | Routing rules | agentId targeting and peer skill routing select expected peer | plugin routing tests + one configured route smoke |
| A6 | ACP bootstrap | `acpx`, Claude ACP, Codex ACP binaries exist; state roots are under `/data`; process cleanup works | in-pod file checks, `acpx --version`, process scan |
| A7 | ACP negative | missing token or missing binary fails before half-configured agent use | bootstrap script negative unit/integration |

### 4.8 Dify

| ID | Target | White-box assertions | Evidence |
| --- | --- | --- | --- |
| D1 | Public health | `https://dify-k8s.carher.net/healthz` returns 200 | `30-post-rollout-audit.sh` |
| D2 | In-cluster bootstrap | Her pod reaches `dify-bootstrap.dify.svc.cluster.local:5688/healthz` | in-pod curl |
| D3 | Per-bot config | `/data/.openclaw/workflow/dify-config.json` exists and `bot_id=carher-266` | in-pod JSON check |
| D4 | Tools installed | `dify-bootstrap-init`, `her-workflow-dify-creator`, `her-workflow-dify-mcp` executable | in-pod file checks |
| D5 | Lifecycle proxy | lifecycle health returns 200 through configured scoped token | redacted tool/API result |
| D6 | Workflow closed loop | create/import/publish/run/query a deterministic workflow and verify exact output | dedicated live workflow smoke, no LLM node required |
| D7 | Stateless HA | API/Web/Worker/bootstrap/Nginx are desired=2 available=2; services have 2 endpoints; PDBs minAvailable=1 | `62-dify-stateless-ha.sh verify` |
| D8 | Stateful risk | DB/Redis/Weaviate/plugin-daemon/sandbox/ssrf remain single replica and are not claimed as strict HA | `60-dify-ha-audit.sh` |
| D9 | S2 compatibility | S3 H75 bootstraps against S2 Dify; ACK H75 bootstraps against ACK Dify; configs stay scoped per bot | S2/S3 Docker checks + ACK checks |

### 4.9 Model / LiteLLM / Spend

| ID | Target | White-box assertions | Evidence |
| --- | --- | --- | --- |
| M1 | ACK key alias | her-266 calls use per-Her LiteLLM key alias | SpendLogs query by `carher-266`, redacted |
| M2 | Model aliases | 13-model alias block works for OpenClaw; Hermes quick commands work for H75 config | config review + marker per class |
| M3 | Transport compatibility | ACK Hermes `chat_completions` avoids prior `codex_responses` failure | direct Hermes and A2A marker |
| M4 | Fallbacks | proxy-level fallbacks fire for retryable upstream errors; no silent `NO_REPLY` | controlled failure or LiteLLM staging test |
| M5 | Budget | budget exceeded returns a clear user-facing reason and does not masquerade as infra failure | LiteLLM budget test/key with safe small limit |
| M6 | S3 shards | S3 ChatGPT LiteLLM shards, Anthropic LiteLLM shards, Claude Max proxy, OpenRouter proxy are healthy | Docker ps + health endpoint/model smoke |

### 4.10 Memory, Files, Skills, Shadow Daemon

| ID | Target | White-box assertions | Evidence |
| --- | --- | --- | --- |
| R1 | Shared skills | image-baked, shared, department, and personal skill precedence matches documented order | skill path listing + controlled same-name skill test |
| R2 | Runtime skill sync | H75 entrypoint syncs skills to OpenClaw and Hermes dogfood roots | startup logs + file counts |
| R3 | Shadow daemon | supported docs mirror into markdown and memory search can recall content | shadow-daemon e2e + one disposable doc |
| R4 | Feishu memory bridge | Feishu docs/minutes/group archives become memory files | plugin memory-bridge tests + controlled archive |
| R5 | Memory corruption rescue | FTS integrity checks detect corruption before GC/VACUUM | S1-S3 rescue script test in non-prod copy |

### 4.11 S1-S3 Internal Track

| ID | Host | White-box assertions | Evidence |
| --- | --- | --- | --- |
| S1-1 | S1 | running `hermestest-*` and legacy `carher-*` containers are healthy and expose expected ports | `scripts/jms ssh JSZX-AI-01 docker ps` |
| S1-2 | S1 | H75-like containers have ACP/A2A/engine-swap config; legacy containers do not claim H75 features | redacted config grep |
| S1-3 | S1 | local Redis/registry/fallback nginx are up and reachable from local containers | container health and local curl |
| S2-1 | S2 | Dify stack services are up; bootstrap image is running; raw stack version is 1.4.2 | `docker ps`, bootstrap health |
| S2-2 | S2 | bootstrap endpoints return expected scoped data without leaking tokens | safe `/healthz` and redacted lifecycle smoke |
| S2-3 | S2 | `carher-221` remains older/non-H75 reference with its own routing/fallback profile | redacted config grep |
| S3-1 | S3 | `hermestest-75` remains behavior reference and has Dify tools, ACP/A2A/engine-swap | redacted config grep and tool existence |
| S3-2 | S3 | `hermestest-265/267` are comparable H75 canaries with Hermes active and Dify tools installed | redacted config grep |
| S3-3 | S3 | LiteLLM/Claude/OpenRouter proxies are running and health endpoints respond | `docker ps`, endpoint smoke |
| S3-4 | S3 | Cloudflare tunnel/fallback nginx are up; external routes do not point to wrong old tunnel after migration | cloudflared status + DNS route audit |
| Sx-rollback | S1-S3 | Compose rollback removes override, preserves original compose file, restarts only target service | `40-fast-gray-rollout.sh rollback` with compose target manifest |

## 5. Recommended Execution Plan

### Phase 0: Read-Only Inventory

Run before any mutation:

```bash
git -C ../CarHer rev-parse HEAD
git -C . rev-parse HEAD
kubectl get herinstance her-266 her-268 -n carher -o json
kubectl get deploy carher-266 carher-268 -n carher -o json
./scripts/her266-h75/30-post-rollout-audit.sh
./scripts/her266-h75/62-dify-stateless-ha.sh verify
scripts/jms ssh JSZX-AI-01 'docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"'
scripts/jms ssh JSZX-AI-02 'docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"'
scripts/jms ssh JSZX-AI-03 'docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"'
```

Pass criteria:

- image refs and code refs match Section 1;
- exactly expected H75 IDs are present;
- S1-S3 inventory can be collected without printing secrets.

### Phase 1: Static And Unit Gate

Run in `../CarHer`:

```bash
pnpm test docker/plugins/feishu-her/src/mention-text.test.ts
pnpm test docker/plugins/feishu-her/src/model-shortcuts.test.ts
pnpm test docker/plugins/feishu-her/src/gateway.reply-delivery.test.ts
pnpm test docker/plugins/feishu-her/src/gateway.discussion-routing.test.ts
pnpm test docker/plugins/a2a-gateway/tests/p0-runtime.test.ts
pnpm test docker/plugins/a2a-gateway/tests/transport-fallback.test.ts
pnpm test docker/plugins/a2a-gateway/tests/file-security.test.ts
pnpm test src/agents/compaction.test.ts src/agents/context-window-guard.test.ts
pnpm test src/plugins/contracts src/channels/plugins/contracts
```

Run in `carher-admin`:

```bash
python -m pytest backend/tests/test_config_gen.py -v
cd operator-go && go test ./internal/controller/ -v
```

Pass criteria:

- no failures in tests directly covering changed/targeted surfaces;
- if broader gates fail, classify as related/unrelated before proceeding.

### Phase 2: ACK Integration Gate

```bash
EXPECTED_IMAGE_TAG=h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-litellmchat-20260530 \
EXPECTED_H75_IDS=266,268 \
EXPECTED_HERMES_LITELLM_TRANSPORT=chat_completions \
./scripts/her266-h75/30-post-rollout-audit.sh

./scripts/her266-h75/62-dify-stateless-ha.sh verify
```

Additional white-box checks:

```bash
kubectl exec -n carher deploy/carher-266 -c carher -- \
  sh -lc 'cat /etc/carher-release.json; cat /data/.engine/active; ps -ef | grep -E "hermes gateway|openclaw|her266-group-poller" | grep -v grep'

kubectl exec -n carher deploy/carher-266 -c carher -- \
  sh -lc 'grep -n "chat_completions" /opt/data/.hermes/config.yaml; test -x /data/.openclaw/local/bin/her-workflow-dify-creator'
```

Pass criteria:

- audit result `OK`;
- Dify stateless result `OK`;
- release metadata and transport match;
- poller is running only for her-266 and only while active engine is Hermes.

### Phase 3: Live Functional Gate

Use real Feishu messages and exact marker verification.

| Gate | Procedure | Pass criteria |
| --- | --- | --- |
| DM Hermes | Send DM `DM_<ts> 请只回复 DM_OK` to `阿里云beta的her` | Feishu history contains `DM_OK`; Hermes/OpenClaw log contains inbound and response ready |
| Group mention | Send group `@阿里云beta的her GROUP_<ts> 请只回复 GROUP_OK` | group history contains bot reply to original message; poller log handles/replies marker until native WS is fixed |
| `/openclaw` | Send `/openclaw` DM | active engine becomes `openclaw`; OpenClaw process starts; test DM replies |
| `/hermes` | Send `/hermes` DM | active engine becomes `hermes`; Hermes connects WS; test DM replies |
| A2A 268->266 | `FROM_HER_ID=268 PEER_URL=http://carher-266-svc.carher.svc.cluster.local:18800 EXPECT_TEXT=A2A_OK_266 ./scripts/her266-h75/50-a2a-functional-probe.sh` | exact marker |
| A2A 266->268 | reverse probe | exact marker |
| Dify workflow | run deterministic workflow closed loop | run status succeeded; exact output |

Pass criteria:

- every live gate has both send-side and target-side evidence;
- no visible auxiliary error cards;
- no duplicate replies for one message.

### Phase 4: S1-S3 Internal Gate

Run read-only inventory first, then targeted live smokes on representative
containers.

| Host | Representative tests |
| --- | --- |
| S1 | `hermestest-13` direct DM or `hermes -z` marker; A2A port check; local Redis/registry health; legacy `carher-12` basic health |
| S2 | Dify raw `/healthz`; bootstrap `/healthz`; lifecycle health for a test bot; `carher-221` OpenClaw basic marker |
| S3 | `hermestest-75` marker; `hermestest-265/267` Hermes marker; A2A between two S3 containers; ChatGPT LiteLLM shard health; Anthropic shard health; Claude Max proxy health; Cloudflare tunnel status |

Pass criteria:

- H75 reference still works on S3;
- S2 Dify/bootstrap is healthy;
- no S1-S3 proxy/tunnel/fallback regression;
- any mismatch against ACK is documented as intentional or a defect.

### Phase 5: Resilience, Negative, And Rollback

| Scenario | Test |
| --- | --- |
| ACK rollback | snapshot `her-268`, roll back to prior image/group/profile, verify Ready and Feishu, then re-gray to H75 and verify |
| Compose rollback | use a disposable compose target or staging target with `40-fast-gray-rollout.sh` compose manifest; apply override then rollback |
| Feishu group WS native failure | keep a known failing native group event case documented; poller must bridge only new messages and not replay old messages |
| Dify stateless pod loss | restart one stateless pod or scale one deployment down/up in a controlled window; service must retain endpoint >= 1 |
| A2A peer down | stop/blackhole one peer in staging; caller surfaces clear error and circuit breaker/health state changes |
| Model upstream failure | force retryable LiteLLM upstream failure in staging; fallback path or user-facing error matches policy |
| Memory corruption | use copied SQLite DB to trigger FTS integrity check/rescue path; no production DB mutation |

Pass criteria:

- rollback time and user-visible downtime are recorded;
- recovery does not leak H75 profile outside expected targets;
- failure mode is explicit and non-silent.

## 6. Acceptance Criteria

The H75 image and S1-S3 internal track can be accepted only when:

1. Provenance is pinned and inspected from the live runtime.
2. ACK H75 isolation is green for the declared target set.
3. Feishu DM, Feishu group mention, `/hermes`, `/openclaw`, A2A both ways,
   and Dify workflow closed loop all pass on live systems.
4. S1/S2/S3 inventories are current and representative smokes pass.
5. Dify is described accurately: stateless HA is green; DB/Redis/Weaviate are
   still not strict HA.
6. Rollback has been demonstrated for at least one ACK H75 target and one
   Compose-style target before broad rollout.
7. All test artifacts are scrubbed of secrets and full chat IDs.

## 7. Known Gaps To Track

| Gap | Impact | Required follow-up |
| --- | --- | --- |
| Feishu native group WS event is not delivered to her-266 | Group replies currently depend on REST poller workaround | Fix app permission/event publication in Feishu Developer Console, then remove or disable poller |
| Dify stateful layer is single replica | Cannot claim strict end-to-end HA | Move DB/Redis/Weaviate to managed HA or stateful HA |
| H75 cold start copies extensions before full functional readiness | Deployment Ready can precede A2A readiness by minutes | Add functional readiness gate or optimize extension copy |
| S1/S2/S3 are heterogeneous | Same test command does not apply uniformly | Keep host-specific target manifest and representative fixtures |
| Hermes title-generation failure was patched live for her-266 | Needs upstream/runtime patch consolidation | Add image-level patch or config knob; add regression test |
| Group poller is deployment-local | It is not part of base image | Either fix native WS and remove it, or productize a scoped poller with tests |

## 8. Minimal Nightly Regression Set

If the full matrix is too expensive, run this nightly for H75 targets:

```bash
EXPECTED_IMAGE_TAG=h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-litellmchat-20260530 \
EXPECTED_H75_IDS=266,268 \
EXPECTED_HERMES_LITELLM_TRANSPORT=chat_completions \
./scripts/her266-h75/30-post-rollout-audit.sh

./scripts/her266-h75/62-dify-stateless-ha.sh verify

FROM_HER_ID=268 PEER_URL=http://carher-266-svc.carher.svc.cluster.local:18800 \
EXPECT_TEXT=NIGHTLY_A2A_266 MESSAGE='请只回复：NIGHTLY_A2A_266' \
./scripts/her266-h75/50-a2a-functional-probe.sh

FROM_HER_ID=266 PEER_URL=http://carher-268-svc.carher.svc.cluster.local:18800 \
EXPECT_TEXT=NIGHTLY_A2A_268 MESSAGE='请只回复：NIGHTLY_A2A_268' \
./scripts/her266-h75/50-a2a-functional-probe.sh
```

Manual/live Feishu checks should still run after every image/profile change:

- one DM marker;
- one group mention marker;
- one `/openclaw` switch and one `/hermes` switch;
- one deterministic Dify workflow run.
