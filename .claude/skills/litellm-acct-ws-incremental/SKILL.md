---
name: litellm-acct-ws-incremental
description: |
  acct 多账户链路 WS 增量传输（responses-over-WebSocket，服务端合成增量）的完整运维/迭代 SOP。
  当需要：迭代 ws_transport 补丁、烘镜像上 canary/全池、诊断命中率/断流/1009、读 ws_incr 观测日志、
  Phase 2 波次推广或回滚、或复用"帧级归因日志+mock 演练器"方法论时使用。
  全部判据来自 2026-08-23 生产实测（acct-82 canary 12 版迭代 + Phase-1 PASS 80.5% 命中），不是推理。
---

# acct 多账户 WS 增量传输：架构·SOP·判据库

> 一句话：acct pod 内给 LiteLLM 打补丁，同一条持久 `wss://chatgpt.com/backend-api/codex/responses`
> 上只发「新增 input 项 + previous_response_id」，服务端拼回历史。增量轮 8-40KB vs HTTP 全量 ~400KB，
> **实测削减 90.3%**（预言 90%+ 精确命中）；计价不变（上游重建全上下文照旧计费）；缓存命中 +12.8pp。

## 架构与安全属性（承重墙，改动前必读）

```
客户端 --HTTP全量--> 外层litellm-proxy --WA(pck)亲和--> acct-N pod --WS增量--> chatgpt.com
                     (完全不改)                          (唯一改动点)
```

- 注入点：`custom_httpx/llm_http_handler.py :: async_response_api_handler`，
  `if stream and not fake_stream and custom_llm_provider=="chatgpt":` → `try_ws_incremental(...)`。
- **结构性安全（硬约束1）**：`try_ws_incremental` 对一切非成功返 None → 字节级落原生 HTTP POST
  → 429/换号经今天的 HTTP 路径原样冒泡。地板=请求自带全量历史，pod 内状态永不承重。
- **提交点纪律**：只认 `response.created`/`in_progress` 为提交信号。上游首帧序恒为
  `codex.rate_limits`(pos0)→`codex.response.metadata`(pos1)→`created`(pos2)；created 前任何
  error/桶满/close/超时 → None。把 rate_limits 当首帧成功=历史上删加密的祸根类。
- **锁纪律（三处，缺一现役流被掐）**：GC 驱逐豁免 lock 持有者；need_full 遇 in-flight →
  `lock_busy_full` 走 HTTP（演练器抓出的真 bug）；增量并发抢锁失败 → `lock_busy` 走 HTTP。
- **preflight**：复用前毫秒级吸本地已到队 CLOSE，死连接→重建全量**留在 WS**（官方闲断纪律）。

## 文件与脚本（全在本仓）

| 文件 | 作用 |
|---|---|
| `scripts/litellm-patch-chatgpt-ws-incremental/ws_transport.py` | 核心模块（7 闸门/账本/canonical/echo 预测/shim） |
| `.../install_ws_incremental.py` | 幂等装配器（锚函数名/形状，烘镜像时 RUN） |
| `.../test_ws_transport.py` | 离线单测 43/43（FakeWS 剧本回放） |
| `.../drill_ws_fallbacks.py` | **异常演练器**：pod 内 mock 上游打 12 条异常剧本于已安装真实模块 |
| `.../probe_ws_incr.py` | pod 内两轮探针（T1 全量→T2 增量+计价单增断言） |
| `scripts/litellm-ws-incr-phase2-rollout.py` | 全池波次编排（dry-run 默认/备份/秒级回滚） |

## 迭代 SOP（一轮 ≈ 5 分钟）

```bash
# 1. 本地改 + 测
python3 test_ws_transport.py                     # 必须全过
# 2. ship + 烘 + 部署（198 内网 10.68.13.198 cltx；KUBECONFIG=/home/cltx/.kube/config 不 sudo）
sshpass -p '...' scp ws_transport.py install_ws_incremental.py cltx@10.68.13.198:/tmp/
# 198 上: cp 到 /tmp/ws-incr-build/ && docker build -t 127.0.0.1:5000/litellm-carher:<新tag> . && push
kubectl -n litellm-product set image deploy/chatgpt-acct-82 litellm=<img> && rollout status
# 3. 验证三层: probe(base64 进 pod python3 -) + drill(同法, 期望 12/12) + 看 ws_incr 日志
```
- acct pod = **单进程 num_workers=1**：不能 hot-patch（改盘不重载；起第二进程 2Gi 秒 OOM）→ 只能烘镜像。
- 镜像 FROM `vanilla-v1.90.2.cache-session-fix-v2-20260817-103630`（=acct-stable digest，全部旧补丁在内）。
- 秒级回滚：`set env ... CHATGPT_WS_INCREMENTAL=0`（gate off=字节级 stock）；或 set image 回 base。

## 观测日志语法（grep 即诊断）

```
ws_incr mode={full_ws|incremental} pck=<sha8> src=<pck来源字段> full_reason=<触发闸> delta_items= total_input_items= frame_bytes= ledger_len= prev_resp= elapsed_ms=
ws_incr_fallback reason={frame_oversize|first_frame_closed|first_frame_error|rate_limit_blocked|first_frame_timeout|lock_busy|lock_busy_full|send_exc|connect_exc|no_commit_signal|prewarm_*} + close_code/exc/帧特征
ws_incr_midstream_break type= close_code= exc= age_s= mode= pck=
ws_incr_reconnect reason=preflight_dead / ws_incr_gc evict={ttl|lru} / ws_incr_prefix_diff idx= region= div= expected[..] got[..]
```
- **full_reason 分类学**：no_session(首轮必然)/ws_closed(上游闲断,官方式重建)/no_receipt/props_change/
  shorter_input/prefix_break@i/N:{echo|input}(echo=我们预测错,可修;input=客户端改写历史,正确全量)/preflight_dead。
- 命中率 = incremental/(incremental+full_ws)；frame_oversize 轮按设计走 HTTP 不进分母。

## 硬判据库（全部生产实测，勿凭想象推翻）

1. **上游 WS 单消息上限 16MiB**（二分：15MB ACCEPT/16MB REJECT 1009）。闸门 `CHATGPT_WS_MAX_FRAME_B` 默认 14MiB。
2. **分块引导（prewarm 链）是协议死路**：`generate:false`+prev 链能累积上下文，但**绕过 HTTP 路径的服务端自动截断**——9MB 请求 HTTP 只计 ~10K tokens（截断救活），链式累积 209 万 tokens 后 `context_length_exceeded` 必炸；且 prewarm 的 input_tokens 全额计入（耗桶）。超限帧唯一正解=本地闸门走 HTTP；治本在客户端压缩（另立项）。
3. **回显预测必须镜像 normalize/客户端真实变换**（prefix_break:echo 的三层修复）：
   reasoning 项=剥 `encrypted_content` 字段保留带 summary 的项（剥后空壳才丢）；
   canonical 剥空值字段（None/[]/{}）+ 缺省 `type:"message"` + `internal_*` 前缀（客户端注入的每轮易变元数据）；list 元素不删（位置有语义）。
4. **pck 来源**：只认稳定键 prompt_cache_key/litellm_session_id/session_id（ctx/ctx.lp/meta/data 四源扫描）；
   绝不用 call_id/trace_id（每请求唯一=纯 churn）。src= 字段暴露来源。
5. **中流断残余 ~0.2-0.5%=协议常态**：上游按 policy 掐长连接（官方 issue #13041/#13039 佐证 1008；实测 1006 RST/257/258），官方客户端同样只能轮级重试（我们的下游 codex CLI 自带该循环）。close_code 归因：1009=超限(有闸)/1006=异常断/1000=正常关。
6. **v2 就是 2026-02-06 头**（codex client.rs:158 RESPONSES_WEBSOCKETS_V2_BETA_HEADER_VALUE）；官方 main 无尺寸预判门（我们领先）；官方中流断后整会话钉死 SSE（我们按轮重试更优）。
7. **计价**：上游从 prev 链重建全上下文照旧计费（in_tok 跨轮单增）——只省出网不省钱，桶消耗不变。
8. **性能实测**：增量轮均值 38.9KB vs HTTP 等价 401KB（-90.3%）；命中率成熟窗 75-80%；缓存命中 70.1% vs stock 57.3%。

## Phase 2 波次推广（脚本用法）

```bash
python3 /tmp/litellm-ws-incr-phase2-rollout.py --stage inventory        # 只读盘点
... --stage a --wave N [--apply]   # 铺镜像(env off=行为中性), 自动备份到 ~/ws-incr-rollout/
... --stage b --wave N [--apply]   # 开闸
... --rollback-env --wave N --apply    # 秒级回滚该波
... --rollback-image --backup <json> --apply
```
- 波次 3/8/21；每波 ≥2h 干净再推下一波；闸门=命中率≥60% + midstream≤0.5% + 零客户端错误增量 + 0 重启。
- ⚠⚠ **registry 每节点独立**：standby(225) 节点的 pod（如 acct-237）换镜像前必须预载：
  `k3s ctr images pull --plain-http 10.68.13.198:5000/litellm-carher:<tag>` + `ctr images tag ... 127.0.0.1:5000/...`。
- ⚠ 不用"重指 acct-stable 别名"：pullPolicy=IfNotPresent，retag 静默不生效。

## 旧补丁共存审计法（换镜像前）

以在役 stock pod（如 acct-83）为参考机：site-packages 全树 `find -name '*.py' | xargs md5sum` 双侧 diff——
差异必须恰为本补丁两处。运行时补丁（callbacks CM 挂 `/app/sitecustomize.py`+`responses_aclose.py`+
PYTHONPATH=/app）只在 81/82/83 老 deployment，`set image` 不动 mounts/env 天然保留；acct pod 无
DATABASE_URL，sitecustomize 在 acct 侧惰性。

## 踩坑清单

- ❌ SpendLogs `proxy_server_request` 是 TOAST 大字段：octet_length 只能 ≤30min 窗，全表/24h 必超时。
- ❌ psql 经 ssh 三层引号必碎：SQL base64 进 `psql -f -`。
- ❌ 先写盘后 ast.parse：批量替换脚本必须先校验后写。
- ❌ 相同 pattern 两个缩进变体：replace 后必须 count 断言。
- ⚠ pod 日志按大小滚动：跨窗绝对计数不可比，只有同窗比率有效。
- ⚠ 每轮 set image/env 都重启 pod（Recreate，秒级断该号，外层换号兜住）；诊断迭代别忘最后留静默 soak 窗。
- ⚠ 探针纪律：大 payload 探针（prewarm）真耗账号桶配额，量入为出；`generate:false` 不生成但计 input。
- ❌ **会话 LRU 默认 32 在高流量 pod 上被打穿=命中率归零（2026-08-25 复验实锤）**：80 系 7 个池主力
  pod（81-84/89-91）5h 活跃 distinct pck 48–109 > 32 → `evict=lru` 与 full 发送几乎 1:1（84 号
  2234 full / 2229 evict）→ 会话在用户两轮之间被挤出 → **持续 0 命中**；低流量 pod（pck≤8）94-97%
  正常。**流量越大的 pod 收益越该大，却恰好全灭——soak 只在低流量 canary 上测过，规模效应看不到。**
  修法=`CHATGPT_WS_MAX_SESSIONS=512`（kubectl set env，Recreate 秒断由外层兜住）；内存无虞（会话只存
  SHA-1 账本，512 个 ≈ 几十 MB vs 2Gi），日常回收靠 idle TTL 600s，LRU 上限应设到正常运行永远碰不到。
  修后 10min 实测：7 pod 命中 84-88%、evict_lru=0、增量样例 delta 1/129 items frame 8KB。
  **现状（08-25）**：全部 acct deploy 已推平 env=512（含 scale0 号，重新上线自动带上）；
  仓库 `ws_transport.py` 源码默认也已 32→512（以后烘镜像不再带旧默认），单测 43/43。
  ⚠ env 只在集群态，acct deploy 无 repo manifest——**重建 deploy 的脚本要带上这个 env**。
  **诊断范式**：命中率异常先按 pod 分桶看二态分布，再对 `evict_lru:full` 比率——1:1 即 thrash 指纹。

## 关联

- 全程日志：memory `project_198_acct_encrypted_incremental_survey_2026_08_22`（含加密放行前史）
- 调研：memory `project_codex_harness_ws_v2_research_2026_08_23`（harness/官方对照/两个新立项）
- 协议判据：skill `codex-multiturn-transport-mechanism`（codex 多轮机制 file:line）
- 后续立项：WS ingress（客户端→网关增量）·外层 callbacks 压缩（治超巨会话，省桶容量）
