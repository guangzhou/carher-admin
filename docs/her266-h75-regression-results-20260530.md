# her-266/her-268 Regression and Benchmark - 2026-05-30

## Scope

- ACK beta target: her-266, "阿里云beta的her".
- ACK release baseline: her-268, "阿里云release的her".
- Feishu group: `hers回归测试` (chat id redacted in this report).
- her-266 target image: `h75-runtime-43deaffc-hermestest75-20260530`.
- her-266 source-of-truth reference: S3 `hermestest-75` local image digest `sha256:582ae504df741dd11dc9ca4f180dd2c1d9edaa9f2d6ab247438e171b6f89a029`.
- her-268 baseline image: `fix-compact-eb348941`.
- S3 `hermestest-75/265/267` model routing was left unchanged because the current `cc.auto-link.com.cn` routing is intentional.

## Executive Summary

Result: pass for the requested beta-vs-release live regression, with two follow-up risks.

- PASS: her-266 is running the S3 `hermestest-75` H75 image under `beta-her-266`; her-268 is running old `fix-compact-eb348941` under `stable`.
- PASS: her-266 no longer has the group-poller `postStart` workaround; the live process list has only OpenClaw gateway, and group messages arrive through native Feishu WebSocket.
- PASS: `hers回归测试` group mentions reply on both bots: beta 3/3 and release 3/3 with exact `reply_to` matching.
- PASS: isolation holds across 259 her deployments: exactly her-266 has the H75 image/profile.
- PASS: Dify stateless HA and her-266 in-pod Dify wiring pass.
- PASS: LiteLLM spend logs show successful completions for both her-266 and her-268 during the benchmark.
- RISK: old her-268 exposes A2A on pod-local port 18800, but its Kubernetes Service does not expose 18800 and the old image lacks the H75 A2A sender script. Stable-service A2A into her-268 is therefore not available on the old baseline.
- RISK: live `/hermes` engine switching was not exercised after the fix. S3 `hermestest-75` is active on OpenClaw, and an inherited Hermes active marker caused Feishu adapter startup failure before the marker was reset to OpenClaw.

## Deployment State

Current K8s state:

| Instance | Role | Image | Deploy group | Runtime profile | Phase | Feishu WS |
| --- | --- | --- | --- | --- | --- | --- |
| her-266 | beta/new | `h75-runtime-43deaffc-hermestest75-20260530` | `beta-her-266` | `h75-openclaw` | Running | Connected |
| her-268 | release/old | `fix-compact-eb348941` | `stable` | none | Running | Connected |

Deployment template checks:

- `carher-266`: expected H75 image, `postStart=none`, 1 updated and 1 available replica.
- `carher-268`: expected old image, `postStart=none`, 1 updated and 1 available replica.
- her-266 process check: active engine `openclaw`; OpenClaw gateway is running; no `group-poller` process is present.
- her-268 process check: OpenClaw gateway is running; healthz is live.

## Feishu Group Regression

Native her-266 group-path evidence:

- Smoke message at 23:34 BJT replied `BETA_SMOKE_OK`.
- Recent group history still contains older no-reply beta messages before the current fix window; post-fix messages from 23:34 BJT onward all have replies in the sampled set.
- her-266 log shows the exact group message as native Feishu inbound:
  - `received message ... (group)`
  - `dispatching to agent`
  - `dispatch complete (queuedFinal=true, replies=1)`
- her-266 has no group-poller process and no Deployment lifecycle hook, so this reply did not come from the old poller workaround.
- her-268 saw the raw inbound event for beta-targeted messages but did not dispatch them, which confirms mention isolation.

Group benchmark with exact `reply_to` matching:

| Target | Samples | Pass | User-visible latency ms | Average |
| --- | ---: | ---: | --- | ---: |
| her-266 beta/new | 3 | 3 | 16153, 15717, 15971 | 15947 |
| her-268 release/old | 3 | 3 | 12397, 15847, 12444 | 13563 |

Interpretation:

- Both bots are functionally healthy in the group.
- Release was about 2.4s faster on average in this small sample, but both are dominated by end-to-end Feishu/channel/agent overhead rather than raw model time.
- Benchmark polling granularity is 2s, so treat sub-second differences as noise.

P2P/DM note:

- The test harness could not reliably benchmark bot P2P by bot open_id. Feishu returned `operation to bots is currently not supported` for this route, so P2P was not used as a pass/fail gate in this run.

## LiteLLM Benchmark

Spend logs during the exact group benchmark:

| Target | Model | Samples | Model duration s | Average duration s | Prompt tokens | Completion tokens |
| --- | --- | ---: | --- | ---: | --- | --- |
| her-266 beta/new | `openai/chatgpt-gpt-5.5` | 3 | 3.71, 2.07, 2.37 | 2.72 | 27838, 27978, 28116 | 22, 23, 24 |
| her-268 release/old | `openai/chatgpt-gpt-5.5` | 3 | 2.69, 3.93, 2.59 | 3.07 | 42377, 44474, 46567 | 22, 12, 12 |

All sampled rows had `status=success`. No budget exceeded, unauthorized key, model idle timeout, `NO_REPLY`, or missing LiteLLM spend record was observed for the benchmark window.

## Dify Regression

`scripts/her266-h75/30-post-rollout-audit.sh` passed for her-266 with:

- Admin API instance endpoint reachable.
- Public Dify healthz reachable.
- In-pod Dify env/tools/config/bootstrap checks passing.
- Engine marker and A2A endpoint checks passing.
- H75 isolation passing: 259 her deployments, exactly 1 H75 deployment expected and found (`266`).

`TARGET_REPLICAS=2 scripts/her266-h75/62-dify-stateless-ha.sh verify` passed:

- `dify-api`: 2/2
- `dify-web`: 2/2
- `dify-worker`: 2/2
- `dify-bootstrap`: 2/2
- `dify-nginx`: 2/2
- service endpoints for api/web/bootstrap/nginx: 2 each
- public `https://dify-k8s.carher.net/healthz`: OK

Stateful components remain intentionally unchanged and single-replica:

- `dify-db`: 1/1
- `dify-redis`: 1/1
- `dify-weaviate`: 1/1

This proves stateless Dify HA and her-266 Dify wiring, not strict end-to-end Dify HA.

## A2A Regression

Results:

- her-268 -> her-266 through stable service `carher-266-svc:18800`: OK, returned `A2A_268_TO_266_OK`.
- her-266 -> her-268 through direct her-268 pod IP `:18800`: OK, returned `A2A_266_TO_268_POD_OK`.
- her-266 -> her-268 through `carher-268-svc:18800`: FAIL, connection refused because the old release Service does not expose port 18800.
- her-268 as script-based source: not available because old `fix-compact-eb348941` lacks `/opt/hermestest/scripts/a2a-send-hermes.py`.

Interpretation:

- New her-266 A2A is service-accessible and functional.
- Old her-268 A2A can answer on pod-local 18800, but it is not exposed as a stable service contract in the release baseline.

## Plugin and Runtime Inventory

her-266 OpenClaw config:

- Providers: `litellm`
- Channels: `feishu`
- Configured plugins: `realtime`
- Runtime tools present: `claude`, `claude-agent-acp`, `codex-acp`, `dify-bootstrap-init`, `her-workflow-dify-creator`, `her-workflow-dify-mcp`
- Active engine: `openclaw`

Hermes config note:

- `chatgpt-pro` base URL points at `https://litellm.carher.net/v1`.
- Hermes transport entries still include `codex_responses`; do not switch live traffic to Hermes until that path is retested and explicitly approved.

## Log Scans

her-266/her-268 logs during the regression window:

- No `reload still deferred`.
- No `Budget has been exceeded`.
- No LiteLLM unauthorized/key-not-found symptoms.
- No model idle timeout.
- No `NO_REPLY`.
- No traceback related to benchmark dispatch.

Known non-fatal startup warnings:

- her-266: plugin auto-enable persistence failed because config is `$include` owned; runtime still loaded and served traffic.
- her-268: one `EBUSY` config-write rename warning at startup; runtime stayed healthy.

## Local Artifacts

- Snapshot directory: `.her266-h75-state/regression-20260530T152256Z/`
- Benchmark runner: `.her266-h75-state/feishu-regression-benchmark.mjs`
- Exact group benchmark JSON: `.her266-h75-state/regression-20260530T152256Z/feishu-group-benchmark-20260530T1547.json`

These state artifacts are local operational evidence and may include live message ids. Keep them out of commits unless scrubbed.

## Follow-Up Work

1. Do not re-enable the her-266 group poller; native Feishu WebSocket group dispatch is now working.
2. If old release A2A needs stable service access, add 18800 to the release Service contract or keep that as an H75-only feature.
3. Retest and, if needed, patch Hermes engine switching before exposing `/hermes` as a safe user-facing path on the S3 H75 image.
4. Optimize H75 cold start: the first OpenClaw boot spent several minutes copying/chmodding runtime plugin trees on NAS.
5. Update `lark-cli` separately; the current CLI reported `1.0.44` available while local is `1.0.39`.
