# LiteLLM 198 Gray Rollout - Frozen Execution Record

Use one copy of this file for one rollout run. The design and safety rules live
in `litellm-gray-rollout/docs/litellm-198-gray-upgrade-plan.md`; this file records concrete frozen
inputs, approvals, evidence, phase transitions, and recovery decisions.

Never paste credentials

- Never record a virtual key, master key, password, cookie, token, kubeconfig,
  DSN, Secret data, or unredacted request body in this document or its evidence.
- Secret resourceVersion/checksum only
- Evidence exporters must replace account/user identifiers with stable opaque
  IDs. Store root-only raw evidence outside Git and link only to its redacted
  derivative and checksum here.

Run identity

| Field | Frozen value |
|---|---|
| Run ID | `litellm-198-v195-20260914` |
| Change ticket | `FILL-TICKET` |
| Environment | `198 / litellm-product` |
| Window start/end (UTC) | `FILL-START / FILL-END` |
| Target release | `v1.95.0` |
| Stable immutable digest | `127.0.0.1:5000/litellm-carher@sha256:7286aa2de7ca8c047c7141a60cba04a58a4a621bead0d7a75ebf3ebfbefacef7` |
| Target immutable digest | `127.0.0.1:5000/litellm-carher@sha256:50e647bd5ee32010317378335d5830dbbcd793b4dd1a9a4460bd34a9272cda95` |
| Guarded-old immutable digest | same as stable — `sha256:7286aa2de7ca…`; must be re-read from `imageID`, not inherited |
| Evidence root | `/root/litellm-gray-run/litellm-198-v195-20260914` (root-only, on 198 `/Data`) |
| Runbook SHA-256 | `FILL-CHECKSUM` |
| Approval status/time | `FILL-APPROVAL` |

> **The serving Deployment is pinned by tag, not by digest.** Measured
> 2026-09-14: `deploy/litellm-proxy` carries
> `127.0.0.1:5000/litellm-carher:vanilla-v1.90.2.capacity.sse-fix-bare-20260711-122004`,
> and all four Pods resolve it to `sha256:7286aa2de7ca…`. The stable and
> guarded-old digests above come from the Pods' `imageID`, which is the only
> place the *running* bytes are named. Do not re-derive them from the tag during
> the window: a tag can be repushed and the resolution would silently change.


Stop before `preflight` if any `FILL-` marker remains, an image is not an
immutable digest from `127.0.0.1:5000`, or evidence storage is group/world
readable.

Owner matrix

| Responsibility | Primary owner | Backup owner | Acknowledged at |
|---|---|---|---|
| Change commander / phase authority | `FILL` | `FILL` | `FILL` |
| nginx routing | `FILL` | `FILL` | `FILL` |
| Helm / K3s workloads | `FILL` | `FILL` | `FILL` |
| PostgreSQL / migration ledger | `FILL` | `FILL` | `FILL` |
| Runtime callbacks / acct pool | `FILL` | `FILL` | `FILL` |
| Metrics / automatic dispatch | `FILL` | `FILL` | `FILL` |
| Independent reviewer | `FILL` | `FILL` | `FILL` |

Frozen artifacts and checksums

| Artifact | Exact identity/path | SHA-256 or immutable revision | Reviewer |
|---|---|---|---|
| chart package SHA-256 | `FILL-.tgz` | `FILL-CHECKSUM` | `FILL` |
| prod values | `FILL-PATH` | `FILL-CHECKSUM` | `FILL` |
| gray values | `FILL-PATH` | `FILL-CHECKSUM` | `FILL` |
| guarded-old values | `FILL-PATH` | `FILL-CHECKSUM` | `FILL` |
| migration Job render | `FILL-PATH` | `FILL-CHECKSUM` | `FILL` |
| nginx candidate generation | `FILL-PATH` | `FILL-CHECKSUM` | `FILL` |
| reviewed full nginx base template | `FILL-PATH` | `FILL-CHECKSUM` | `FILL` |
| nginx debug-token file | root-only path only | `FILL-CHECKSUM` | `FILL` |
| callback/config snapshot | `FILL-PATH` | `FILL-CHECKSUM` | `FILL` |
| Secret metadata snapshot | `FILL-PATH` | `FILL-RV-AND-CHECKSUM` | `FILL` |
| prod Helm revision | `FILL-REVISION` | `FILL-MANIFEST-CHECKSUM` | `FILL` |
| guarded-old Helm revision | `FILL-REVISION` | `FILL-MANIFEST-CHECKSUM` | `FILL` |
| historical gray archive | `FILL-PATH` | `FILL-CHECKSUM` | `FILL` |

Record the output of `helm lint`, frozen-package `helm template`, nginx 1.18.0
fixture tests, pytest, and shellcheck in the evidence index. Rebuilding a chart
package after approval creates a new run; do not silently replace the checksum.

**The rows above are prose until they are pinned.** Until 2026-09-21 nothing in the
scripts hashed the chart package, the values or the image digest — `config_checksum`
covered exactly the seven nginx route files — so patching a live release rotated no
generation, `verify_generation` kept passing, and every gate approved against the
pre-patch workload stayed valid against the post-patch one. Copy the three checksums
per release from this table into `gray-workload-pin.sh` in `preflight`, and run it
again after **every** mid-run patch:

| Release | chart `.tgz` SHA-256 | values SHA-256 | image digest | `workload_checksum` after pin | Reviewer |
|---|---|---|---|---|---|
| `litellm-product-gray` | `FILL` | `FILL` | `sha256:FILL` | `FILL` | `FILL` |
| `litellm-product-guarded-old` | `N/A/FILL` | `N/A/FILL` | `N/A/sha256:FILL` | — | `FILL` |
| `litellm-product-proxy` (post-convergence) | `N/A/FILL` | `N/A/FILL` | `N/A/sha256:FILL` | — | `FILL` |

Each pin is recorded with a `--reason`, a `check-pod-spec-shape.py` verdict (or an
explicit `--no-require-shape-evidence` waiver, which is written into the hashed
artifact as `shape_evidence=waived`), and rotates the generation — which is the
point: it invalidates every gate evidence file in one step, so the ramp re-earns
`split_sample`, `split_monitor_continuity` and, at ≥50%, `split_capacity`, and the
sustain streak restarts from zero. Log each pin as its own phase-ledger-style row:

| Sequence | Reason | `previous_workload_checksum` | `workload_checksum` | Generation after | Time UTC | Actor |
|---:|---|---|---|---|---|---|
| p0 | preflight freeze | `unbound` | `FILL` | `FILL` | `FILL` | `FILL` |
| p1 | `N/A` mid-run patch | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` |

⚠️ **The chart package checksum is not reproducible on 198 — the `.tgz` is the
artifact, not the source tree.** Measured 2026-09-13:

| 打包环境 | 同一份 chart 源，两次连续打包 |
|---|---|
| 198 (`helm v3.17.3`) | **checksum 不同**（`0beba529…` vs `eaaca078…`） |
| 本机 (`helm v4.2.3`) | checksum 相同 |

gzip 头的 mtime 两边都被归零（`1f 8b 08 14 00 00 00 00`），差异在**内层 tar**：
helm 3 把打包时刻写进每个成员的 tar header ModTime（两次相隔 2 s，`tar -tv`
分钟级显示一致而字节不同）。成员的 size/mode/owner 全部相同。

因此：

- ⛔ 不许在本机预算出 checksum 再拿到 198 上比对 —— **helm 3 与 helm 4 打出的包
  本来就不同**（`ab5e125f…` vs `a3de2bd5…`），这个"不匹配"读起来像被篡改，其实是量具错。
- ⛔ 不许用"重新打一次包、看 checksum 对不对"来验证冻结产物的完整性；在 198 上
  这个动作**必然**报假红。
- ✅ 执行当天在 198 上 `helm package` **一次**，立刻 `sha256sum` 写进上表，
  之后每一步都引用**那个文件**。校验完整性用 `sha256sum -c` 比对该文件本身。

Phase ledger

Every transition must be performed by the rollout scripts, then copied from the
root-only state record. Never hand-edit mode, split, override maps, generation,
or phase. Terminal runs are never reused.

| Sequence | Phase | Generation/checksum | Time UTC | Actor | Gate/evidence | Result |
|---:|---|---|---|---|---|---|
| 0 | `preflight` | `FILL` | `FILL` | `FILL` | artifact readiness | `FILL` |
| 1a | `bridge_preparing` | `FILL` | `FILL` | `FILL` | only if stable lacks fuse | `N/A/FILL` |
| 1b | `bridge_verified` | `FILL` | `FILL` | `FILL` | bridge runtime gate | `N/A/FILL` |
| 1c | `preflight` | `FILL` | `FILL` | `FILL` | stable fuse complete | `N/A/FILL` |
| 2 | `normal_gray` | `FILL` | `FILL` | `FILL` | gray entry gates | `FILL` |
| 3 | `convergence_ready` | `FILL` | `FILL` | `FILL` | pre-mode bypass disposition + mode=1 commit | `FILL` |
| 4 | `prod_offline_upgrading` | `FILL` | `FILL` | `FILL` | post-mode `prod_zero` evidence bound to row 3 | `FILL` |
| 5 | `prod_verified` | `FILL` | `FILL` | `FILL` | new prod direct smoke | `FILL` |
| 6 | `committed` | `FILL` | `FILL` | `FILL` | atomic convergence commit | `FILL` |

Failure terminals are `rolled_back` or `aborting_to_bridge` followed by
`aborted`. Delete unused phase rows from the frozen copy; do not rewrite history.

Migration gate

| Check | Evidence/checksum | Owner | Result |
|---|---|---|---|
| Metadata dump excludes historical `LiteLLM_SpendLogs`/`LiteLLM_SpendLogToolIndex` rows; both restored tables are empty | `FILL` | `FILL` | `FILL` |
| Full schema + every non-log public table count share one MVCC snapshot | `FILL` | `FILL` | `FILL` |
| Restore counts match exactly; dump <=2GiB and source non-log data <=1GiB | `FILL` | `FILL` | `FILL` |
| Clone namespace, Secret, NetworkPolicy isolation | `FILL` | `FILL` | `FILL` |
| Schema before/after normalized dumps | `FILL` | `FILL` | `FILL` |
| Exact DDL ledger, duration, lock evidence | `FILL` | `FILL` | `FILL` |
| `collect-migration-evidence.py` bound input | `FILL` | `FILL` | `PASS/FAIL` |
| Partial DDL state is `none` or reconciled | `FILL` | `FILL` | `FILL` |
| Clone A target-version read/write | `FILL` | `FILL` | `FILL` |
| Clone B stable-version read/write | `FILL` | `FILL` | `FILL` |
| Clone C concurrent stable/target writes | `FILL` | `FILL` | `FILL` |
| Clone C scheduler-observation ruler: positive control first (a known write from a `role=version-test` client must appear in clone C's log with its `%h`), then >=25 min (>=2 `reset_budget` cycles) of `log_statement='mod'` + `%h` attribution, then `ALTER SYSTEM RESET` both knobs | `docs/scheduler-suppression-evidence-2026-09-14.md` §6.3 | `FILL` | `FILL` |
| `check-migration.py` structured result | `FILL` | `FILL` | `PASS/FAIL` |
| Index runner `inspect` preflight snapshot (oldest transaction, prepared xacts, vacuum in progress, dead-tuple %, SpendLogs vacuum age, index bytes) | `FILL` | `FILL` | `FILL` |
| Approved index headroom thresholds (`GRAY_INDEX_MAX_XACT_AGE_SECONDS`, `GRAY_INDEX_MAX_DEAD_TUP_PERCENT`, `GRAY_INDEX_MAX_VACUUM_AGE_SECONDS`) — no placeholder may remain | `FILL` | `FILL` | `FILL` |
| `GRAY_INDEX_REQUIRED_BYTES` measured on the clone rehearsal and `GRAY_INDEX_FREE_BYTES` measured with `df` on the DB data volume; free >= 2x required | `FILL` | `FILL` | `PASS/FAIL` |
| Index runner result `schema_version: 2`, `preflight` populated, `cleanup` is `null` on success; any invalid index left by a failed create was dropped and re-inspected | `FILL` | `FILL` | `PASS/FAIL` |
| Job/Pod/imageID/runner/DB attestations (5) | `FILL` | `FILL` | `PASS/FAIL` |
| Qualification run/generation/freshness and live production before-schema | `FILL` | `FILL` | `PASS/FAIL` |
| Selected path and justification | `Path FILL` | `FILL` | `FILL` |

现场容量与隔离记录（2026-08-05）

| Check | Evidence | Result |
|---|---|---|
| 198 production DB | `pg_database_size=166 GB`; non-log public relations `454 MB` | 精简 clone 可行，但不得复制历史日志行 |
| 198 `/Data` | `492 GB total / 185 GB free`；生产 DB 位于同一块盘 | 生产盘不承载 dump/clone |
| 188 `/Data` | `100% used / 0 free` | 禁止作为备份目标 |
| Clone placement | `aiyjy-litellm-standby`，每个 clone 与 dump 均使用 `local-path` 4Gi PVC；provisioner 根目录 `/Data/rancher/storage` | 模板已固定；禁止使用系统盘 `emptyDir` |
| Live prod placement | 4 个 prod LiteLLM Pod：198×2、225×2；DB 在 198 | 225 不是空闲 standby；clone/gray 必须做共置容量评估 |
| 225 host disk | `/` 19G、已用 11G、可用约 7.2G；`/Data` 492G、已用 23G、可用约 445G | clone 数据只能落 `/Data`；禁止落系统盘 |
| Legacy full-basebackup | `litellm-v195-clone/full-basebackup` 使用 `pg_basebackup`，PV 指向不存在的 `/var/lib/litellm-v195-clone`；Job 仍为 `suspend: true`、无 Pod | **禁止解除 suspend**：它既会复制约 166GB（违反排除历史日志的精简方案），又会落到仅余约 7.2G 的系统盘。必须以仓库内受限 `pg_dump` 流程替代，目标落 225 `/Data` 的小容量受控卷 |
| NetworkPolicy enforcement | 225 上临时 server/client：策略前 TCP 连通，空 ingress 策略后连接超时 | `PASS`；临时 namespace 已销毁 |
| v1.95.0 image on standby | 225 成功拉取并启动 `127.0.0.1:5000/litellm-carher@sha256:50e647...` | `PASS` |
| Postgres clone image on standby | 将生产当前 `postgres:16` amd64 内容镜像到 198 registry；225 成功运行 `127.0.0.1:5000/postgres@sha256:adaa8b...`，输出 PostgreSQL 16.13 | `PASS`；不使用 198 无法解析的 ACR VPC 域名 |

For Path A, also record the serving stable fuse proof, fresh production backup,
migration role identity, Job digest, `lock_timeout`, `statement_timeout`,
`backoffLimit: 0`, per-DDL completion state, final schema checksum, and stable
post-migration smoke. A dump is disaster evidence, not an online rollback plan.

> **Path A precondition, measured 2026-09-14 and currently NOT met.** The fuse is
> `DISABLE_SCHEMA_UPDATE=True` on every serving old Pod *before* the migration
> Job runs. Read inside all four live replicas
> (`litellm-proxy-677b5474-{68jjb,bjm2g,m9f66,zbcjb}`, `-n litellm-product`), the
> variable is **unset on all four** — not `False`, absent. Do not read the
> chart's `DISABLE_SCHEMA_UPDATE: "True"` as proof: that is what the *gray*
> render emits, and the serving prod Deployment is a different object. Set and
> re-read it per Pod before the Job, and record the four readings here.


Runtime gate

| Check | Evidence/checksum | Owner | Result/freeze decision |
|---|---|---|---|
| Live callback/patch/env inventory | `FILL` | `FILL` | `FILL` |
| Frozen expected callback/API/acct/mutation inventory | `FILL` | `FILL` | `FILL` |
| Callback import and behavior matrix | `FILL` | `FILL` | `FILL` |
| Enabled/recent API surface discovery | `FILL` | `FILL` | `FILL` |
| All surface smoke including images | `FILL` | `FILL` | `FILL` |
| Redis format/prefix/TTL compatibility | `FILL` | `FILL` | `FILL` |
| Measured p100 `upstream_response_time` per `uri_class` (window + access-log path) | window = `zkreq.log` + `.1` (2 days, N=1,243,452 `/pro`); path `/var/log/nginx/zkreq.log` (format `zkreq`, **not** `access.log`) | `docs/drain-budget-evidence-2026-09-13.md` | p100 = **81,665 s** (`responses`); p99 = 1,442 s. **Re-measure on the day** |
| Drain budget holds: `terminationGracePeriodSeconds >= drain.preStopSeconds + drain.streamDrainSeconds` and nginx `proxy_read_timeout == drain.streamDrainSeconds` | `600 >= 30 + 570` ✅ / live nginx is **600 s**, must be lowered to **570 s** (a real behavior change, not a template copy) | `docs/drain-budget-evidence-2026-09-13.md` §4 | `PASS` on arithmetic; nginx change **pending** |
| ⚠️ `proxy_read_timeout` is an **inter-read** timeout, not a total-duration cap — it does not bound stream length and cannot prevent truncation of an actively streaming SSE. Its real job is to make nginx give up **before** the Pod is SIGKILLed (570 < 600) so a dying upstream yields a clean 504 instead of a silent cut | measured: 600 s timeout coexists with an 81,665 s request | `docs/drain-budget-evidence-2026-09-13.md` §4 | `NOTED` |
| If measured p100 exceeds `drain.streamDrainSeconds`: grace and nginx timeout raised together, or truncation explicitly accepted with the affected request share and consumers named | **Truncation explicitly accepted.** Share = **0.0940%** (1,169 / 1,243,452). Consumers = **Codex Desktop / codex-tui on `/pro/v1/responses` + Cursor 3.18.25** — the day-of re-measure put 27 Cursor requests over 570 s, so the 09-13 "single consumer" reading is stale and the truncation notice must include the Cursor lane. Grace NOT raised: covering p100 would need ~22.7 h | `docs/drain-budget-evidence-2026-09-13.md` §3.1–3.2, §7.2 | `ACCEPTED` — needs sign-off |
| Declared source CIDRs (`prepare-values.py --ingress-cidr`, `/24` or narrower) — **two legs: (a) NodePort forwarding paths, one per path not per node; (b) every in-cluster Pod→Pod consumer, by podCIDR per node** | (a) `10.42.0.0/32` (198 `flannel.1`, cross-node) + `10.42.0.1/32` (198 `cni0`, same-node); (b) `10.42.0.0/24` (198) + `10.42.1.0/24` (standby) + `10.42.2.0/24` (242) + `10.68.13.0/24` (node LAN) | `docs/nodeport-source-cidr-evidence.md` §1, §1.1 | (a) 2026-09-13 measured, **re-measure on the day**; (b) 2026-09-19 measured after leg (b) was found missing and cut in-cluster traffic for 3.5 h while the public entry stayed green |
| NodePort reachability positive leg: `curl 127.0.0.1:<nodePort>/health/liveliness` from the host-nginx node returns 200 after the policy is applied | `FILL` | `FILL` | `PASS/FAIL` |
| NodePort reachability falsification leg: same request from an undeclared source times out / is refused. ⚠️ 188 is **not** an undeclared source — it SNATs to `10.42.0.0` like everything else; use a pod-network client hitting `10.42.x.y:4000` directly | `FILL` | `FILL` | `PASS/FAIL` |
| Observed source address at the Pod matches the declared CIDR list line by line | `FILL` | `FILL` | `PASS/FAIL` |
| Scheduler/background task safety | `FILL` | `FILL` | `FILL` |
| Stable-to-gray control mutation visibility | `FILL` | `FILL` | `PASS/FREEZE` |
| Gray-to-stable control mutation visibility | `FILL` | `FILL` | `PASS/FREEZE` |
| Same-batch acct deployment/service/quota/model/request snapshot | `FILL` | `FILL` | `FILL` |
| 30402 bypass inventory and classification | `scripts/collect-bypass-inventory.sh --output <run-dir>/bypass-raw.txt` (read-only) | `docs/bypass-consumer-disposition.md` | 2026-09-13 rehearsed: 3 live consumers (2×A-write, 1×B-read); **re-scan on the day and diff against §1 line by line** |

Each callback record must contain import/behavior PASS, probe digest, image
digest, config digest, and timestamp. Each API smoke record must contain a
request ID, timestamp, and response digest. Mutation records must name the
operation, writer/reader endpoints, request ID, timestamps, measured latency,
SLA, and observed-result digest. Bare names and bare `PASS` strings are invalid.
| `audit-runtime.py` structured result | `FILL` | `FILL` | `PASS/FAIL` |

If cross-process mutation visibility is not proven, record the approved mutation
freeze start, affected automations, queue/compensation evidence, and recovery
owner. A freeze decision is required before gray receives traffic.

Gray traffic gates

| Gate | Required evidence | Result/time |
|---|---|---|
| Frozen baseline produced by `collect-metrics.py --emit-baseline` at `--rollout-percent 0` (hand-written files are rejected) | path + `payload_sha256` | `FILL` |
| Frozen monitor cycle interval (seconds), `--max-missed-cycles`, and `--min-cycles` used by `check-monitor-continuity.py` (`--min-cycles` must equal `metrics.py` `ABS_SUSTAIN_WINDOWS`, default 4, or the deepest 5xx stop-loss leg cannot fire in that step) | `FILL` | `FILL` |
| Heartbeat ledger path + `check-monitor-continuity.py` PASS for the window before each ramp step (gaps must be repaired and re-observed, never widened away; a step that dwelled for fewer than `--min-cycles` cycles must dwell longer, never have the floor lowered) | `FILL` | `PASS/FAIL` |
| Each ramp step's dwell ≥ `--min-cycles × interval` (default 4 × 300s = 20min), proved from the heartbeat ledger via `gray-progress.py --ledger` — the 2026-09-14 run ran 5%/10%/50% at 2 cycles each and the stop-loss was unarmed in all three | `FILL` | `PASS/FAIL` |
| Baseline `run_id` equals this run and `captured_at` is inside the 24h change window | `FILL` | `PASS/FAIL` |
| Baseline per-`uri_class` sample counts and the `--baseline-min-samples` used (default 100; never lowered to make a thin window pass) | `FILL` | `FILL` |
| Gray direct smoke on 30405 | readiness, all enabled surfaces, DB writes, callbacks | `FILL` |
| Schema unchanged after gray start | normalized before/after checksum | `FILL` |
| Rollback rehearsal | target-state transaction and failed-reload recovery | `FILL` |
| Specified key route | force-gray, force-prod, force-gray; pool/sid proof | `FILL` |
| 1% | metrics JSON, request-ID spend reconciliation, capacity | `FILL` |
| 5% | metrics JSON, request-ID spend reconciliation, capacity | `FILL` |
| 10% | metrics JSON, one-hour observation | `FILL` |
| Frozen per-container upstream-concurrency ceiling (upstream-seconds per second one ready container sustains) and headroom fraction for `check-split-capacity.py` — there is no default, and a hard-coded fleet constant is the `llm-stab-scrape-down` literal `5` all over again | `FILL` | `FILL` |
| 50% | `check-split-capacity.py` PASS (measured, NOT hand-written; ruler is upstream-seconds per ready container, never request count — the two differed 21.4% vs 44.9% on 2026-09-18) plus guarded-old capacity | `FILL` |
| 100% | frozen same-uri baseline; no heterogeneous prod comparison | `FILL` |

Before run initialization, record the exact values or separately reviewed
checksums for `GRAY_NGINX_TEST_CMD`, `GRAY_RELOAD_CMD`,
`GRAY_POST_RELOAD_CMD`, `GRAY_INITIAL_ROLLBACK_CMD`, and
`GRAY_ABORT_VERIFY_CMD`, and `GRAY_GATE_MAX_AGE_SECONDS`. The generation input
manifest binds their SHA-256 digests for the run. Production routing
transactions reject changed test/reload/post-reload hooks before staging or
rendering; abort rejects a changed guarded-old verification hook before bridge
activation; evidence checks reject a changed freshness window. To change any
of these contracts, terminate the run and initialize a new reviewed run; do not
edit the active manifest.

Only `gray-auto-dispatch.sh` may consume a `metrics.py`
`dispatcher_recommendation`. Paste the structured result checksum, selected
action, dispatcher evidence, and final phase; do not paste raw access logs.
Each cycle must first invoke `collect-metrics.py` with the current run/generation/
config checksum, the real timestamped access log, and fresh checksum-bearing
hard-error/backend-health/SpendLogs snapshots. Then invoke
`gray-monitor-cycle.sh`, never the dispatcher or rollback scripts directly.
Freeze the exact collector command, input path, cycle interval, timeout,
service/cron identity, alert recipient, and a tested missed-cycle alarm here;
repository code provides one safe cycle, not the host scheduler configuration.
If a hard trigger occurs while prod health is not explicitly true, the expected
action is `alert_only` with no route mutation.

Convergence gates

| Check | Evidence/checksum | Owner | Result |
|---|---|---|---|
| 100% gray stable for approved duration | `FILL` | `FILL` | `FILL` |
| Active routing state is exactly `split=100` before convergence prepare | `FILL` | `FILL` | `FILL` |
| Incident force-prod is empty | `FILL` | `FILL` | `FILL` |
| Guarded-old is full-capacity Ready and directly verified | `FILL` | `FILL` | `FILL` |
| Control/acct mutation freeze or approved gray bridge | `FILL` | `FILL` | `FILL` |
| Pre-mode `convergence_bypass_disposition` evidence: every 30402 caller classified and disposed | `FILL` | `FILL` | `FILL` |
| `gray-convergence-prepare.sh` atomically committed mode/phase | `FILL` | `FILL` | `FILL` |
| Post-mode observation covers longest task interval with access-log silence | `FILL` | `FILL` | `FILL` |
| Post-mode backend Pod IP conntrack/ss shows no business connection | `FILL` | `FILL` | `FILL` |
| `prod_zero` evidence binds current `convergence_ready` generation/checksum | `FILL` | `FILL` | `FILL` |
| Prod rendered manifest and all target digests frozen | `FILL` | `FILL` | `FILL` |
| `check-release-deletion-set.py` PASS for `litellm-product-proxy` (live manifest vs frozen target render) | `FILL` | `FILL` | `PASS/FAIL` |
| Every object in the deletion set has a named consumer and disposition; approval file checksum | `FILL` | `FILL` | `FILL` |
| `check-pod-spec-shape.py` PASS for `litellm-proxy` (live `kubectl get deploy -o yaml` vs the same frozen target render — **not** `helm get manifest`) | `FILL` | `FILL` | `PASS/FAIL` |
| Live ConfigMap capture (`--live-configmaps`, 0600, shredded after the run) supplied, so a content-addressed rename is provably inert instead of demanding 40 identical approvals | `FILL` | `FILL` | `YES/NO` |
| `inert_by_content` entries spot-checked: each one is a rename of the **same bytes**, and no removed mount appears among them (the tool errors `INTERNAL_MOUNT_REMOVAL_MARKED_INERT` if it ever does) | `FILL` | `FILL` | `PASS/FAIL` |
| Every removed/swapped volumeMount, volume, lifecycle hook and env name has a named consumer; approval file checksum | `FILL` | `FILL` | `FILL` |
| `kubectl apply --dry-run=server` and `kubectl diff` run against the prod chart swap, not only the gray render | `FILL` | `FILL` | `PASS/FAIL` |
| Prod 4/4 target digest and full direct smoke | `FILL` | `FILL` | `FILL` |
| `gray-convergence-commit.sh` atomically returned all traffic | `FILL` | `FILL` | `FILL` |
| Bypass and acct automations restored and reconciled | `FILL` | `FILL` | `FILL` |

Evidence index

Each referenced JSON must be deterministic structured output. Raw evidence stays
root-only and out of Git. Checksum the command/script version, redacted input,
and output separately.

| ID | Stage | Redacted path | SHA-256 | Captured UTC | Owner | Reviewer | Retention |
|---|---|---|---|---|---|---|---|
| `EV-001` | artifact readiness | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` |
| `EV-002` | migration | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` |
| `EV-003` | runtime | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` |
| `EV-004` | routing/metrics | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` |
| `EV-005` | convergence | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` |
| `EV-006` | terminal observation | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` | `FILL` |

Artifact readiness results

`PASS` requires a real execution on the named runtime. `SKIP`, missing binary,
model-only routing, or a local test double is `NOT RUN`, never `PASS`.

| Check | Runtime/version | Command/evidence | Result |
|---|---|---|---|
| Python gray suite | frozen source tree | `python3 -m pytest litellm-gray-rollout/tests/test_litellm_gray_{routing,audit,chart,nginx,rollout,migration}.py -v` | `FILL` |
| shellcheck | 198 change host | `shellcheck -x -P litellm-gray-rollout/scripts litellm-gray-rollout/scripts/*.sh` | `FILL` |
| Helm lint/template | exact Helm version on change host | frozen chart package + all three frozen values | `FILL` |
| nginx routing fixture | stock nginx 1.18.0 | real process + HTTP upstream proof for every case | `FILL` |
| metrics monitor cycle | 198 change host scheduler | frozen collector + `gray-monitor-cycle.sh`; invalid-output and missed-cycle drill | `FILL` |
| server-side manifest validation | target 198 API server | dry-run, diff rc, selectors, images, NodePorts | `FILL` |
| clone NetworkPolicy | target CNI | allowed and denied probe logs/checksums | `FILL` |

Rollback and abort

Record the exact trigger, current phase, backend health, recommendation JSON,
approver, script invocation checksum, transition evidence, and post-action
validation. Never improvise a phase action.

| Current phase | Safe action | Required serving target |
|---|---|---|
| `normal_gray` | global rollback transaction | complete healthy old prod |
| `convergence_ready` | exit convergence and global rollback | complete healthy old prod |
| `prod_offline_upgrading` | hold healthy gray; abort if gray fails | verified full-capacity guarded-old |
| `prod_verified` | hold healthy gray; abort if gray fails | verified full-capacity guarded-old |
| `committed` | bridge-first offline prod rollback sequence | verified guarded-old revision |

Budget reset during `prod_offline_upgrading`

Gray pins the background-task suppressors and overlays
`general_settings.disable_reset_budget: true`, so while prod is offline **no
release is resetting budgets**. This is a measured, bounded gap, not an unknown
— see `docs/scheduler-suppression-evidence-2026-09-14.md` §5.

Measured 2026-09-14 02:40 Beijing on `litellm-db-0` (`-n litellm-product`): 1526
keys and 1 user carry `budget_duration`, and all but one stale key share a single
next `budget_reset_at` of `2026-09-14 16:00:00 UTC` (2026-09-15 00:00 Beijing).
`reset_budget_job` reschedules every ~597-605s.

- Finish the window before 00:00 Beijing and the gap touches **0 rows**.
- If the window does cross that boundary, accept a reset delay bounded by
  (window remainder + ~10 min). No manual action is needed: prod picks the job
  back up on its next cycle.
- Either way, 15 minutes after prod is verified, re-run the overdue count and
  record it. Convergence criterion: `budget_reset_at < now()` is back to <= 1.

```sql
SELECT count(*) FROM "LiteLLM_VerificationToken"
 WHERE budget_duration IS NOT NULL AND budget_reset_at < now();
```

🔴 After convergence, the **idle lane also needs the suppressor** (2026-09-19)

`disable_reset_budget: true` was only ever pinned on the *gray* lane's config. Once
the run committed and `litellm-proxy` became the idle lane, that lane kept running at
`replicas=1`, kept `envFrom: litellm-secrets` — i.e. **the same production database** —
and therefore kept scheduling `reset_budget_job` against real user budgets. Measured on
`litellm-proxy-7c8b7d56-z2lgt`: `Scheduled job stagger applied (… reset_budget_job=+270s …)`,
two workers, 11h uptime. A lane serving no traffic is still a *writer*.

Fix is one appended line in the idle lane's config CM (`general_settings.disable_reset_budget: true`),
a new content-hashed CM, and a `patch` of the `config` volume — **never `apply` on
`litellm-proxy`**:

```bash
# guarded patch: assert the old CM name before replacing it
kubectl -n litellm-product patch deploy litellm-proxy --type=json -p '[
  {"op":"test","path":"/spec/template/spec/volumes/1/configMap/name","value":"<OLD-CM>"},
  {"op":"replace","path":"/spec/template/spec/volumes/1/configMap/name","value":"<NEW-CM>"}]'
```

⛔ **Judging it by re-grepping the old pod is a bad ruler** — the pod is gone after the
rollout, so `grep -c reset_budget_job` returns 0 for both the broken and the fixed build.
The real criterion is the **contents of the new pod's stagger line**: the other 9 jobs
are still listed and `reset_budget_job` is absent from that list.

For an abort, record `aborting_to_bridge`, bridge direct smoke, atomic route
activation, stable traffic proof, `aborted`, and compensation/recovery backlog.
If bridge verification fails after route activation, keep the active
`aborting_to_bridge` generation and mutation freeze. Repair the bridge and
resume with the same frozen `GRAY_ABORT_VERIFY_CMD`; do not route back to the
failed gray/prod target and do not edit the verification hook mid-run.
For post-commit rollback, freeze mutation, scale and verify guarded-old, route all
new requests to it, prove new prod is idle, run only the frozen guarded-old Helm
revision, verify old prod, route back, reconcile automations, then drain bridge.

Observation and cleanup

| Check | 24-hour evidence/checksum | Owner | Approved result |
|---|---|---|---|
| Peak/off-peak hard errors and per-uri metrics | `FILL` | `FILL` | `FILL` |
| Spend request-ID reconciliation | `FILL` | `FILL` | `FILL` |
| Acct/quota/registry/sticky reconciliation | `FILL` | `FILL` | `FILL` |
| Compensation queues empty | `FILL` | `FILL` | `FILL` |
| Bridge/old digest retention satisfied | `FILL` | `FILL` | `FILL` |
| Clone, Secret and namespace destruction approved | `FILL` | `FILL` | `FILL` |
| Final evidence bundle checksum | `FILL` | `FILL` | `FILL` |

Do not delete the guarded-old release, old digest, frozen config/callback
snapshots, migration ledger, clone evidence, or rollback revision before the
observation owner and independent reviewer approve this section.

The workload pin rows are part of that evidence bundle: they are the only record of
which build served users during each stretch of the run. If a pin row reads
`unbound`, the run cannot say which build was serving during the windows its gates
approved — record that in the closeout as residual risk rather than leaving the row
blank.
