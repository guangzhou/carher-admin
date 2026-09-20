---
name: litellm-198-latency-triage
description: |
  「同事说 GPT / codex / cursor 变慢了」的分层归因 runbook（198 LiteLLM，ns litellm-product）。
  用途：在 30 分钟内判定慢在**上游 ChatGPT 后端** 还是 **我们的 router/亲和/缓存层**，
  并给出各占多少秒。含 6 条现成的证伪腿（机器 / proxy / 模型 / 路由 / 量 / 补丁）、
  两层自报量具（叶子 elapsed_ms vs 母 router SpendLogs）的同窗差分、绕开一切的叶子直打探针、
  会话甩号率与丢缓存代价的配平 A/B，以及一串会骗人的量具（含"SpendLogs 算不出 TTFT"这条）。判据全部来自 2026-09-09 生产实测，不是推理。
  Use when 用户说「gpt 变慢了」「codex 好慢」「cursor 卡」「198 上看看为什么慢」
  「延迟涨了帮我找根因」。
---

# 198 GPT 变慢：分层归因 runbook

> **先记住结论的形状**：2026-09-09 这次，22 秒里 **~15 秒是上游**、**~7 秒是我们的重试放大**，
> 而**丢缓存只占 3~4 秒，不是主因**。历史上（09-08）也是同一个形状。
> 所以默认假设应当是「上游」，你的活儿是**去证伪它**，而不是先去翻我们的配置。

## 0. 三十秒定形状

```bash
scripts/gpt-slow-diagnose/latency-triage.sh hourly 20
```

看两列就够：

| 现象 | 含义 |
|---|---|
| `total_s` 涨 | 有事。→ 继续往下走本 runbook |
| `in_ktok` 同步暴涨 | 是客户端把上下文喂大了，**不是上游劣化**。去查 cursor/codex 侧 |
| `in_ktok` 没涨甚至降了，`total_s` 还在涨 | 单位输入的代价变贵了 ⇒ 上游嫌疑最大 |

2026-09-09 实测：p50 从 09-08 20:00 的 **5.5s** 涨到 09-09 09:00 的 **22.1s**，
而 `in_ktok` 从 126k **降到** 101k、`out_tok` 平（299→416）。**喂得更少、等得更久。**

> ⛔ **别用 `ttft_s` / `gen_s` 那两列下结论**。LiteLLM 的 `completionStartTime`
> 不是首 token 时刻，绝大多数行它约等于 `endTime`（09-09 实测 45% 的行完全相等、
> gen p90 仅 0.24s；07-26 独立测过 gpt-5.6-sol 99.0% 同形状）。
> 所以 `gen_s≈0` 是**记账假象**，不能读成"上游不开口、开口后吐得飞快"。
> 脚本仍打印这两列，只是为了让你**认出这个假象**，不是让你引用它。
> 真要区分"等待 vs 吐字"，唯一的办法是第 4 腿（叶子直打探针，`max_output_tokens=16`
> ——输出被钉死成 16 token 还要十几秒，那就与吐字速度无关）。
> → [[feedback_litellm_completionstarttime_not_ttft]]

## 1. 六条证伪腿（按成本从低到高，一条都别跳）

| # | 假设 | 命令 | 09-09 的证伪数据 |
|---|---|---|---|
| 1 | 我们机器扛不住 | `latency-triage.sh infra` | load 4.86/8 核、盘 42%、proxy 4 pod 各 0.8~1.9/4 核、mem 5.5~6.2/12Gi、17h 零重启 |
| 2 | 我们的 proxy 慢 | `latency-triage.sh controls 90` | 同窗同 proxy：`ag-gemini-3.8-flash` **4.4s@135k**、`deepseek-v4-flash` **6.4s@159k** |
| 3 | 只是某个模型 | 同上（按 `model_id` 分列） | 同账号上 luna 23.7 / **5.5 22.3** / sol 22.0 / terra 20.2 —— 四个一起烂 |
| 4 | 路由/重试/缓存造成 | `leaf-direct-probe.py --n-leaf 12 --n-mother 0` | 绕开一切、20 token prompt：med **5.7s** max **17.4s**（早 1 小时同样测法 med 2.1s） |
| 5 | 我们请求量涨了 | `hourly` 里的 `n` 列 | 05:00 **8061 req/h→9.6s** vs 08:00 **6588 req/h→21.2s**，不相关 |
| 6 | 是某个补丁 | 比补丁上线时刻 vs 症状起点 | WS 增量补丁 09-08 04:39 全开，而 09-08 18:00~22:00 p50 只有 5.5~7.1s ⇒ **改动早于症状** |

**第 2 腿是最强的对照组**：同一台 proxy、同一分钟、更大的输入，别的上游 4.4 秒。
它一次性干掉「proxy 慢」「网络慢」「大上下文本来就慢」三个假设。

### 上游自己的原话

```bash
scripts/gpt-slow-diagnose/latency-triage.sh census 2
```

`MidStreamFallbackError: (transient capacity, stream) - Our servers are currently overloaded.`
⚠️ 母 router 的 failure hook 会把**绝大多数**报错打成 `***`，
2h 里 5733 条 MidStreamFallbackError 只有 101 条是可读的。别因为看不到文本就以为没有。

## 2. 量化我们自己的放大（22s 里的 ~7s）

两层自报量具**同窗口**对比：

```
叶子自报 elapsed_ms  p50 13~16s   ← ws_incr 日志（collect + ws-log-analyze.py）
母 router p50        21~22s        ← SpendLogs（hourly）
差值 ≈ 6~8s = 我们的放大链
```

放大链（每一环都有计数）：

```
上游 mid-stream 吐 overloaded ── 2h 5733 次 MidStreamFallbackError
  ↓ 母 router 盖 60s cooldown + WA 再 fail-mark 180s（604 次）
  ↓ pin 指向的腿不在健康集 → 重挑号（2h 1005 次 re-picking）
  ↓ 换了号 → 新叶子没有这个会话 → 全量重发上下文
```

WA 决策分布（2h）：**HIT ~100 / pin 在但腿不健康 1005 / pin 缺失 943** ⇒ 命中率 ~5%。
后果：**96.4% 的请求落在被多个账号轮番服务过的会话上**，单会话最多 **22 个号**。

## 3. ⛔ 别把「丢缓存」当根因 —— 这是本 runbook 最容易犯的错

直觉上「换号→丢缓存→重发 110k 上下文→当然慢」，**实测是错的**：

```bash
latency-triage.sh collect 2          # 远端抓日志
scp ...:/tmp/wspck-XXXX.txt /tmp/
python3 scripts/gpt-slow-diagnose/ws-log-analyze.py /tmp/wspck-XXXX.txt
```

按 `total_input_items` 配平后（**不配平就是把「深会话本来就慢」记到丢缓存头上**）：

| items | incremental(有缓存, 帧~10KB) | full_ws(丢缓存, 帧~500KB) | 差 |
|---|---|---|---|
| 61-120 | 13.3s | 17.0s | +3.7s (1.28x) |
| 121-240 | 11.9s | 16.3s | +4.5s (1.38x) |
| 601+ | 12.2s | 15.8s | +3.6s (1.29x) |

而且 full_ws 内部**帧从 24KB 涨到 10MB，p50 只从 14.0 走到 16.2s** ⇒ p50 对字节数近乎不敏感。
**有缓存的 incremental 自己 p50 也是 12~13s，那才是上游给的地板。**

丢缓存真正贵在两处，别忽略但也别错记成延迟根因：
- **p90**：24~34s → 35~49s
- **出网**：2h **7.1GB** vs 0.09GB

## 4. 顺手会挖出来的三个独立问题

1. **死号还在被派活**：`latency-triage.sh deadlegs`。
   判据是**成功数 = 0**，不是失败数大 —— SpendLogs 严重漏记失败（2h 只记 79 条，
   而母日志里 5733 次 MidStreamFallback）。
   09-09 实测 acct-137/135/115/121/120/119 六个号刷 401、2h 被挑中 80 次、**0 次成功**。
2. **`Unknown parameter: 'input[N].status'`**（2h 708 次硬 400）：客户端传的 input item 带
   `status`，`ws_transport._strip_volatile` **只在算 canonical hash 时剥**，外发帧是
   `data["input"]` 的原样切片 ⇒ 原封不动进 WS 被上游拒 → 整个上下文改走 HTTP。
   ⚠️ 已证伪它不是 cooldown 的触发源（400 数 55~97 的那几个号，429 数是 0）。
3. **DB 体积**：`LiteLLM_SpendLogs` 34G + `LiteLLM_SpendLogToolIndex` 25G，
   `STORE_PROMPTS_IN_SPEND_LOGS=True` 还开着 —— 就是 09-08 盘满 502 的源头，
   见 skill `litellm-198-diskpressure-502`。

## 5. 会骗人的量具（每一条我都实际被骗过）

| 量具 | 骗法 | 正解 |
|---|---|---|
| `kubectl exec litellm-db-0 -- psql` | 不给凭据会 `FATAL: role "root" does not exist`，但被吞掉后**长得就像空结果** | 每次先跑阳性对照（脚本已内置，失败直接 exit 2） |
| `model` / `model_group` 列 | `model_id` 才是部署（`chatgpt-acct-N-gpt-5.6-sol`）；`model_id LIKE 'chatgpt-acct-%'` 才选得中，`model LIKE` 返 0 行 | `model_group`=请求名·`model`=落点·`model_id`=部署 |
| `completionStartTime` | **它不是首 token 时刻**，几乎等于 `endTime`（09-09：45% 完全相等、gen p90 0.24s；07-26：sol 99.0%）。据此说"慢在首 token 前"是**用一个坏尺子印证自己的假设** | 只用 `request_duration_ms` 报总耗时；要拆"等待 vs 吐字"只能用叶子直打探针把 `max_output_tokens` 钉成 16 |
| SpendLogs 的失败数 | 严重漏记（79 vs 5733） | 失败率只能从母 router 日志数；SpendLogs 只用来判「成功了多久」 |
| 叶子容器日志 | 高负载 pod 只留 **~25 分钟**，`--since=2h` 是保留边界不是真 2h | 说「2 小时内 N 次」前先看首行时间戳 |
| `ws_incr` 的 mode 分母 | `lock_busy_full`/`first_frame_error`/`handshake_401` 走 `return None`，**不 emit mode= 行** | 「命中率 40%」是上了 WS 的里面的比率，真实复用率更差 |
| 叶子直打探针 n=3 | 上游是双峰的，同一秒能给 1.2s 和 17.4s，n=3 完全被方差盖住 | **n≥12** 才下结论 |
| `metadata->'usage_object'` | 在 34GB 表上 jsonb 解引用直接超时（我跑挂过两次） | 别在大窗口查询里碰 metadata |
| `grep 'POST /v1/responses'` 数叶子入站量 | 日志格式不是这个，会得到全 0 —— 长得跟「没流量」一模一样 | 入站量只从 SpendLogs 数 |

## 6. 止血手段（都不治本，动之前先说清楚这一点）

上游慢不会因为下面任何一条变快，它们只是让 pin 少被踢掉、少赔几次重试：

- 摘掉刷 401 的死号（skill `chatgpt-acct-audit-and-retire`）
- `cooldown_time: 60 → ~15s`（母 router `current_values`；见 `topic_litellm_ops_index`）
- 剥掉外发帧里的 `status` 字段（改 `ws_transport.py`，见 skill `litellm-acct-ws-incremental`）

## 相关

- `project_198_gpt_slow_upstream_degradation_2026_09_09` —— 本次的完整档案
- `project_198_upstream_5_6_slowdown_and_cooldown_stickiness_2026_09_08` —— 前一天同形状
- `feedback_cache_loss_is_not_the_latency_root_cause` —— §3 的独立记忆
- `feedback_litellm_completionstarttime_not_ttft` —— §0 那个坏尺子的来龙去脉
- `feedback_zero_rows_vanish_from_groupby_use_set_difference` —— `deadlegs` 为什么这么写
- skill `litellm-acct-ws-incremental`（WS 增量补丁本体）
- skill `litellm-198-diskpressure-502`（DB 体积那条线）
- skill `litellm-wa-flush`（要动亲和 pin 的时候）
