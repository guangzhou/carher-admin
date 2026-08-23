# Handoff — acct 多账户链路 WS 增量传输（2026-08-23，session 8a0efd4e 深挖）

> 定位：**接手文档**。目标/范围见 `docs/acct-multiaccount-incremental-transport-goal.md`；
> 实施长 log 见记忆 `project_198_acct_encrypted_incremental_survey_2026_08_22`。
> 本文所有"当前态"数字都是 2026-08-23 ~12:00 北京**线上实测**（acct-82 pod + git），
> 不是凭记忆——记忆那份 log 停在 prod6/命中率 27%，**已过时**，本文纠正。

---

## ⚠ 复核更新（2026-08-23 15:46 北京）——本文已被 live 工作追平

> **最终态（2026-08-23 ~17:00，权威）**：SOP+判据已沉淀 **skill `litellm-acct-ws-incremental`**（读它，别读本文正文）。
> - **Phase-1 PASS（14:55 判定）**：命中率 **80.5%**(327/406) / 断流 0.2%≤0.5% / 0 重启 / 1009 已根治（16MiB 闸门）。
> - **Phase 2 推广中**：wave1(101/102/103) 15:44 开闸；每小时 cron 自动巡检+推 wave2(8台)/wave3(21台)，越界自动回滚。
> - 实测收益：增量轮削减 **90.3%**（38.9KB vs 401KB）；缓存命中 70.1% vs stock 57.3%。
> - 旧补丁审计（以 acct-83 为参考）：site-packages 2019/2019 一致；81/82/83 的 CM 运行时补丁不受 set image 影响；acct pod 无 DATABASE_URL。
> - 225 节点已预载 prod12（registry 每节点独立，acct-237/wave3 的 ImagePullBackOff 雷已拆）。


写完本文（~12:00）后有**并行会话继续推进**，§1–§9 是 12:00 快照，以下修正优先：

- **镜像 prod11 → prod12**（commit `732e3a9`，"echo 匹配收官"：canonical 再剥客户端每轮注入的
  `internal_chat_message_metadata_passthrough` 等 `internal_*` 元数据。commit 自述 prod11 命中率
  **71%(198/278) 已过 60% 闸**，剩余 echo 断是"客户端把工具调用改写成空 assistant 消息=历史真变"
  的合法全量，不修）。另有 2 个 Phase2 脚本修复：`d3302cb`(tag prod2→prod12)、`f50ea55`(backup 目录→`~`)。
  > ⚠ §0 我写的"命中率 78.4%"是 prod11 一个 18min 子窗（192/53）；settled 值约 71%。两者都过闸，别把 78.4% 当权威。
- **canary 已从单号扩到 4 号（实测）**：`chatgpt-acct-82/101/102/103` **全部** prod12 +
  `CHATGPT_WS_INCREMENTAL=1`，均在 **15:40–15:46 北京 (re)启动**，紧接 Phase2 脚本 15:42 修复之后。
  - **【推测，非亲验】** 这是 **Phase 2 wave 1（101/102/103，波次 3/8/21）+ 原 canary 82** 被并行会话
    执行。**soak 是否满 5h、用户是否显式放行，我无法从现场证实**——需用户确认这是不是你授权的动作。
    §0/§4 里"单号 canary""Phase 2 唯一卡点=等用户放行"的表述**据此作废**。
- **soak 时钟又重置**：4 号均 15:40+ 重启 → prod12 安静窗从此重计（commit 自述"canary 自此静默，
  让 soak 跑完整安静窗"）。当前 6min 窗命中率 **58.8%(20/34) 不可判**（刚 mass restart，首轮
  `no_session` 占比高必压低），**非回归**；中流断 0，fallback=lock_busy_full4/frame_oversize2/terminate1。
- **cron `4d5d3ba2` 已不在 CronList**（可能被并行会话删/替）→ §6"cron 每 2h 已在判"存疑。
- **doc 2（cursor-ide-chatgpt-web）复核 = 无问题**：bpi pod 仍 Running 30h/0 重启；`cursor-web-fc-terra`
  最后流量仍 08-22 04:31（近 2 天零新增），`-high`/`-max` 仍各 1 探针。

---

## 0. 一句话现状

WS 增量在 **acct-82 单号 canary** 上跑 **prod11 镜像**，**命中率 78.4%**（192 incremental /
53 full_ws，18min 窗）、**中流断 0**、frame_oversize 闸门正常（16 个怪物帧→HTTP）。
命中率主拖累（首个 reasoning 项每轮必断）**已在 prod9 根治**。**Phase 2（全池灰度）唯一卡点
= prod11 需静置 soak ≥5h 干净 + 用户显式放行**（触及 32 个生产账号）。

⚠ **关键接手事实：每次重烘镜像/重启 pod 都重置 soak 时钟。** prod7→prod11 全在今天迭代，
最后一次部署 11:43 北京，所以 5h 干净窗**从 11:43 重新计**，当前只跑了不到 20min。
**接手第一动作 = 停止迭代，让 prod11 静置**，除非发现新 bug。

---

## 1. 这是什么工作

**主目标**：让 acct 多账户链路（客户端 → 外层 litellm-proxy → acct-N pod → 上游 ChatGPT）
用上 codex 官方多轮机制，最终做**增量传输**（省 pod→上游出网 ~90%+，降延迟）。加密放行
是前置地基不是目的。

**硬约束**（goal 文档原文，违反即回滚）：
1. **换号/账户异常必须扎实**——桶满切别的账户是系统行为；这层历史上没做好导致把加密整个删过。
2. 改线上代码遵循**可回滚/可验证/可灰度**三原则。
3. **验证看实际发送内容不看响应码**——200 ≠ 密文/增量真到了上游，查 SpendLogs `proxy_server_request` 值形态。

**落点**（已定论，别再纠结）：不做薄代理，直接在 **acct pod 内 LiteLLM 打补丁**。注入点
`custom_httpx/llm_http_handler.py :: async_response_api_handler`，`if stream and not
fake_stream and custom_llm_provider=="chatgpt":` → `try_ws_incremental(...)`，返 None 即
字节级落原生 HTTP POST（硬约束1 的结构性安全地板）。acct pod `num_workers=1` 单进程，改盘
上文件不重载 → canary 只能**烘镜像**，不能 hot-patch。

---

## 2. 当前热路径：WS 增量 canary（acct-82）线上实测

| 项 | 值 | 来源 |
|---|---|---|
| 部署镜像 | `127.0.0.1:5000/litellm-carher:vanilla-v1.90.2.cache-session-fix-v2.ws-incr-prod11-20260823` | `kubectl get deploy` |
| pod | `chatgpt-acct-82-58cff6f765-crvkn` restarts=0 started 2026-08-23T03:43:08Z(=11:43 北京) | `kubectl get pod` |
| 网关闸门 | `CHATGPT_WS_INCREMENTAL=1` `CHATGPT_WS_INCREMENTAL_LOG=1` | deploy env |
| acct-82 身份 | **真实 pool 成员**（33d svc，外层 affinity 路由真流量，非隔离 canary） | 记忆 |

**当前窗口（~18min）日志实测**：
- mode=incremental **192** / mode=full_ws **53** → **命中率 78.4%**（>60% 闸）
- full_reason 分解（53 个全量）：`no_session`16（首轮，固有不可省）+ `ws_closed`8（连接死，已恢复）
  + `prefix_break@*`~27（分散在 @2…@2714）+ `shorter_input`1 + `preflight_dead`1
- fallback：`frame_oversize`16（怪物 Cursor 会话 >14MiB→HTTP，正确）+ `terminate_response`1
- **中流断 0**、5xx 0

**计价/语义**：上游从 previous_response_id 重建全上下文照旧计费，只省 pod→上游出网、不改语义
（记忆实测 in_tok 跨轮单增）。真实大会话曾测得 T1 2.2MB full_ws → 后续 8KB incremental =
**≈99.6% 出网削减**。

---

## 3. 记忆之后发生了什么（prod7→prod11，全是命中率根治）

记忆 log 停在 **prod6（命中率 27%）**。之后 5 个镜像今天全部迭代完成，**把命中率从 27% 拉到 78%**：

| 镜像 | commit | 内容 |
|---|---|---|
| prod7 | da6c95e | `full_reason=` 闸门归因：全量 WS 行自报被哪道闸拦（no_session/ws_closed/no_receipt/props_change/shorter_input/prefix_break@断点/preflight_dead）——让"反复全量"的原因躺在日志里 |
| prod8 | ad96e83 | prefix_break 差异快照：断点落回显区时并排打预测项 vs 实收项 300 字符快照 |
| **prod9** | **eca26d4** | **命中率主拖累根治**：见下 |
| prod10 | 237c097 | canonical 比对对空值/缺省字段不敏感（客户端回显省略 `annotations:[]`/`logprobs:[]`/`content:[]` 与缺省 `type:"message"`）→ 只差空字段的 item 判等价 |
| prod11 | 70d569f | 差异快照定位分歧字节 ±150（剩余 echo 断的分歧点藏在 300 字符外的长 arguments 深处） |

### prod9 根因（本次最重要的工程发现）

**命中率主拖累 = 我们的 `_expected_echo` 没有镜像 normalize 对历史的真实变换。** 两类 prefix_break：

- **① reasoning(summary) 项**：外层 normalize 实际是**剥 `encrypted_content` 字段、保留带
  summary 的项**（剥后成空壳才丢）；我们却预测**整项丢弃** → 每个带思考摘要的会话在**首个
  reasoning 位置必断** → 每轮全量。**修**：`_expected_echo` 镜像同一变换。这是 27%→78% 的主因。
- **② 客户端原地改写历史**（background task 状态文本每轮变）→ 历史**真的变了**，全量重放是
  **正确行为，不修**。当前窗口剩余的 ~27 个 prefix_break 主要是这类 + 长 payload 深处序列化差异，
  prod11 的 ±150 快照就是用来继续定位这条尾巴的。

**接手判断**：命中率已过闸；剩余 prefix_break 尾巴大部分是"历史真变了"的合法全量，不是 bug。
prod11 快照日志（`prefix_break@div` + 分歧点±150）留着继续观察，但**不构成继续迭代镜像的理由**
——除非快照证明还有一类"历史没变我们却判断错"的假断（那才是 bug）。

---

## 4. Phase 2（全池灰度）门槛与放行

Phase 2 = 把 WS 增量推到全池（经 per-deploy `set image`，env 默认 OFF 按波开）。波次 **3/8/21**。
编排脚本 `scripts/litellm-ws-incr-phase2-rollout.py`（dry-run 默认，两段式：Stage A 铺镜像中性 →
Stage B 按波开闸；`--rollback-env` 秒级 / `--rollback-image` deploy 粒度）。

**放行门槛（三选一未过则不进）**：
1. ✅ **1009 谜题闭环**：帧 >16MiB 上游 WS 上限 → 本地 `frame_oversize` 闸门直接走 HTTP
   （二分钉死 16MiB；分块引导 prewarm 链实测退役——WS prev 链绕过服务端截断累积
   2M tokens 必炸 context_length_exceeded）。prod5 起已在线，本窗 16 次实测 HTTP 200。
2. ✅ **客户端 429 无增量**（阻塞项 b）：SpendLogs 26h 窗 canary 2609 成功 / **0 条可归因
   客户端失败**；上游 155+ 次 429 全被外层换号吸收。
3. ❌ **soak ≥5h 干净**：⚠ **未过**——prod11 部署于 11:43，soak 窗从 0 重计。命中率/中流断/
   计价当前全绿但窗太短。**需 prod11 静置 5h+ 不再重烘。**
4. ❌ **用户显式放行**：触及 32 生产账号，记忆明确"等用户确认"。

**适格性已核对**：`acct-stable` digest == `cache-session-fix-v2-20260817` digest → 32 活跃 stock
pod 同 base 适格，0 台需人工决策。⚠ **别名陷阱**：`pullPolicy=IfNotPresent` + registry retag
后节点用缓存旧镜像**静默不生效** → Phase 2 必须**显式 per-deploy set image**，不能重指别名。

**接手 Go/No-Go 流程**：让 prod11 soak 到 08-23 17:00 北京后 → 拉当窗 hit-rate（≥60%）+
midstream_break（≤0.5%）+ 外层 SpendLogs 客户端失败（0）→ 全绿则**问用户是否放行 Phase 2 Stage A**。

---

## 5. 回滚与访问

**秒级回滚**（零 schema/PVC/CM 改动）：
```
# gate off = 字节级 stock
kubectl -n litellm-product set env deploy/chatgpt-acct-82 CHATGPT_WS_INCREMENTAL=0
# 或整镜像回退
kubectl -n litellm-product set image deploy/chatgpt-acct-82 \
  <container>=127.0.0.1:5000/litellm-carher:vanilla-v1.90.2.cache-session-fix-v2-20260817-103630
```

**198 访问**（记忆 `reference_198_direct_ssh`）：
```
sshpass -p 'Hn8#mKLp3QxZ' ssh -o StrictHostKeyChecking=no cltx@10.68.13.198
export KUBECONFIG=/home/cltx/.kube/config    # 不要 sudo，root 读不到 k3s.yaml
```
（公网 47.83.211.198 已 stale；jms 间歇 Permission denied，用直连。）

**SpendLogs 查询**（防引号剥离）：SQL base64 送入 `kubectl -n litellm-product exec -i
litellm-db-0 -- sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB -f -'`。⚠ `proxy_server_request`
是 TOAST 大字段，全表 octet_length 必超时；只能小窗(≤30min) 或 `pg_column_size`。

---

## 6. 资产清单

**代码（本仓 `scripts/litellm-patch-chatgpt-ws-incremental/`）**：
- `ws_transport.py`（45KB 核心模块，含 `try_ws_incremental`/`_expected_echo`/GC/preflight/frame 闸门）
- `install_ws_incremental.py`（幂等装配器，注入 `async_response_api_handler`）
- `test_ws_transport.py`（42/42，含 reasoning 回显往返、canonical 空值不敏感用例）
- `probe_ws_incr.py`（两轮增量 E2E，55→110 算术）
- `drill_ws_fallbacks.py`（pod 内 mock WS 上游打 12 条异常剧本；base64 进 pod `cd /tmp && python3 -`）

**Phase 2 编排**：`scripts/litellm-ws-incr-phase2-rollout.py`（198:/tmp/ 已 ship）。

**烘镜像**（198 `/tmp/ws-incr-build/`）：Dockerfile FROM vanilla-v1.90.2.cache-session-fix-v2
+ COPY 两文件 + RUN installer；`sudo docker build -t 127.0.0.1:5000/litellm-carher:<tag> . &&
docker push`。clean prod 基线镜像无 DIAG。

**cron**：`4d5d3ba2`（每 2h :37 Phase-1 soak check acct-82）——⚠ 描述串写 "prod5" 是**旧标签**，
它 grep 日志所以功能不受影响，但接手时心里知道它盯的是当前 pod 不是 prod5。

**198 探针留档** `/root/enc-keep-rollout/`：ws_probe.py（握手）、ws_incr_probe.py（两轮增量）、
ws_slug.py（slug 枚举）、probe_enc4（密文边界）、enc_ab_test/enc_ab_repeat。

**镜像版本谱**：prod2(cdbc9a6 中流断三刀)→prod5(acf4088 frame闸门)→prod6(f9d16db drill+并发need_full修)
→prod7(full_reason)→prod8(diff快照)→prod9(echo根治)→prod10(canonical)→**prod11(±150定位)=当前**。

---

## 7. 相邻线程（同 session 沉淀，别丢）

这个 session 有**多条并行线**，接手时别混：

### A. normalize v2 / enc-keep（加密放行地基，P0 已上线）
- litellm-callbacks CM `chatgpt_responses_normalize.py`：`_KEEP_ENCRYPTED_ALIASES={"enc-canary-01"}`
  灰度名单不剥密文；带密文 reasoning 不算空壳不删；fallback guard 对 encrypted 错误放行；日志 info→warning。
- 上游**容忍跨号裸密文**（实测多块/混块全 200）；客户端回放的密文带 LiteLLM 包裹
  `litellm_enc:{b64(model_id)};gAAAA`，发上游前官方 `_restore_encrypted_content_item_ids_in_input`
  无条件解包。
- **待办**：canary(enc-canary-01, budget $10) 挂真实实例观察数天（encrypted_keep 频率/spend 对比，
  密文回放放大 input tokens 要看真账）；渠道闸门挂 pre_deployment/pre_routing 是**死代码**
  （v1.90.2 从不调这两 hook），下次 CM 变更时清理。回滚备份 `/root/enc-keep-rollout/*-20260822-211940.yaml`。

### B. cursor-web-fc-terra（Cursor 接入 GPT 网页，独立线，r18 稳定）
- 与 acct 链路**完全不同的路**：Cursor `/v1/responses` → 198 LiteLLM(hook `cursor_web_fc_sys_rewrite`)
  → pod `zero-cursor-bpi:8201`（CM `zk-cursor-bpi-patch` key responses.js）→ 网页 ChatGPT 有状态会话。
  走**网页订阅额度**不烧 codex 份额。模型 `cursor-web-fc-terra{,-high,-max}`，key=cursor-liuguoxian03。
- 配置线上核对一致（`mode:chat` 是对的——决定转发的是 `use_chat_completions_api=false`，mode 只管
  /v1/models 广告）。**无流量自 08-22 04:31**（重心转到 acct 线）。SOP=skill `zk-cursor-web-fc-iterate`。
- ⚠ skill 尾部引用 `.claude/plans/indexed-riding-mountain.md`（cursor-fc-* plan）**本地不存在**；
  该机制细节在记忆 `feedback_cursor_hits_responses_endpoint_not_chat_shim_dormant`。

### C. 两个新立项方向（WS v2 调研产出，未启动）
1. **WS ingress（客户端→网关端到端增量）**：codex 0.147 已就绪；省掉 client→gateway→pod 入网腿
   （现在只省了 pod→上游腿）。要点：prewarm 帧网关**本地合成** response.created+done 不转发上游。
2. **shim 侧 auto-compaction（治 18.9MB 怪物 Cursor 会话的正道）**：参照 harness `compact_remote_v2`
   + 上游 `/responses/compact` 端点，在 cursor-agents-shim 对超阈值历史压缩——上游本来就把 99% 截断
   扔掉，压缩后入网出网双降。传输层增量之后收益最大的单点。
   （怪物会话现在走 frame_oversize→HTTP+服务端截断，行为同 stock，但白白传 18.9MB 入网。）

---

## 8. 接手动作清单（按优先级）

1. **[立即] 停止迭代 prod 镜像**，让 prod11 静置。soak 窗从 11:43 北京重计，跑满 5h（到 ~17:00）。
2. **[17:00 后] 拉 soak 判据**：当窗 hit-rate（≥60%）、midstream_break（≤0.5%）、外层 SpendLogs
   客户端失败（0）。cron 4d5d3ba2 每 2h 已在判。
3. **[全绿后] 问用户放行 Phase 2 Stage A**（铺镜像 env 中性，不开闸，无客户端影响）。触及 32 账号需显式确认。
4. **[观察] prefix_break 尾巴**：看 prod11 ±150 快照，确认剩余全量都是"历史真变了"（类②）而非假断；
   若发现假断=新 bug，才继续迭代。
5. **[并行] enc-keep canary** 挂真实实例观察数天，看 encrypted_keep 频率与 spend 真账。
6. **[未启动] 两个新立项** 待 Phase 2 落定后按收益排期（shim auto-compaction 收益最大）。

---

## 9. 方法论红线（本 session 踩过/用户抓过，接手务必守）

- **200 ≠ 密文/增量真到上游**：验证放行必须查 `proxy_server_request` 实际发送**值形态**，不能只看响应码。
- **acct pod 容器日志按大小滚动**（k3s log rotation），`logs --tail=-1` 是滑动窗非累计 →
  **跨 cycle 绝对计数不可比，只有同窗内比率有效**。
- **每次重烘镜像/重启 pod 重置 soak 时钟**——迭代和 soak 是互斥的，想过闸就得停手静置。
- **归因三段式**（CLAUDE.md）：假设/证伪条件/数据缺一不可；"代码存在 ≠ 该路径被执行"，要日志/计数器/复现。
  本 session 正例：命中率根因是靠 full_reason+差异快照日志**实锤**才敢改，不是猜。
- **别名陷阱**：pullPolicy=IfNotPresent + retag 静默不生效，Phase 2 必须显式 per-deploy set image。
