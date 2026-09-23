---
name: codex-remote-compaction-triage
description: >-
  诊断并修复 Codex 客户端的远程压缩失败 —— `Error running remote compact task:
  stream disconnected before completion: Incomplete response returned,
  reason: max_output_tokens`，以及 `remote compaction v2 expected exactly one
  compaction output item, got 0`。含「只变一个变量」的预算扫描尺子、
  两层抬预算补丁各自的门控作用域、以及把修复推上 198 生产车道的换代流程。
  Use when the user mentions Codex 压缩失败 / remote compact / compaction /
  max_output_tokens / 会话压缩报错 / 聊了没几句就说要压缩，或者 Codex 长会话
  在 198 上走 responses 路径时报 incomplete。
metadata:
  requires:
    bins: ["ssh", "python3", "kubectl"]
  related_skills:
    - litellm-hook-dev
    - litellm-198-router-patch
    - litellm-198-fallback-chain
  related_memories:
    - feedback_codex_compaction_budget_4096_is_eaten_by_reasoning
    - feedback_reasoning_model_probe_needs_headroom_for_reasoning_tokens
    - feedback_prod_lane_label_is_on_pods_not_deployments
    - feedback_rolling_restart_window_is_not_a_code_regression
    - feedback_pushing_worktree_file_ships_uncommitted_changes
---

# Codex 远程压缩失败：定位与修复

## 0. 先认清这个报错**不是**什么

客户端那句 `reason: max_output_tokens` 里的 "max_output_tokens" 说的是
**输出**预算耗尽，不是输入超长。见到它第一反应去查上下文长度 / token 计数，
是这条路上最常见的走偏。

同样别被「我才聊了几句怎么就压缩了」带偏 —— 触发压缩的是客户端自己的
阈值判断，跟服务端无关；服务端这边要回答的只有一个问题：
**压缩那一轮的请求，为什么没产出摘要。**

## 1. 协议：压缩轮跟普通轮只差一个 item

读 `openai/codex` 源码确认（非推测）：

* `codex-rs/core/src/compact_remote_v2.rs` —— compaction **v2 不打**
  `/v1/responses/compact`（那是 v1）。它复用标准 Responses 流，只在
  `input` **末尾追加 `{"type":"compaction_trigger"}`**；然后只数
  `OutputItemDone` 里的 `Compaction` 变体，`compaction_count != 1` 就 Fatal。
* `codex-rs/codex-api/src/common.rs` 的 `ResponsesApiRequest` ——
  🔴 **结构体里没有 `max_output_tokens` 字段**。Codex 在压缩轮
  **一个输出上限都发不出来**。

第二条是整个诊断的支点：**只要抓到的请求里有这个字段，它就一定是链路上
某一层塞进去的，不是客户端发的。**

## 2. 唯一干净的尺子：同一份真实 payload，只变一个变量

```bash
# 在 198 上。KEY 必须是**用户那把 key**（per-key alias 只挂在它上面，
# master key 会走到别的落点 ⇒ 线型假绿）
KEY=<user key> scripts/codex-compaction-budget-sweep.py \
    --payload /tmp/replay.json --model gpt-5.5 \
    --budgets absent,4096,16384
```

* payload 要**真实长会话**，合成小对话复现不出来（reasoning 吃不满小预算）。
  从 `~/.codex/sessions/**/rollout-*.jsonl` 里取 `input` 数组。
* 🔴 **`absent`（完全不发这个字段）是必扫且最重要的一格** —— 那才是 Codex
  的真实形状。只扫 4096/16384 会停在「4096 太小」这个**正确但不完整**的结论上，
  漏掉「这 4096 是谁塞的」。
* 🔴 **判据先看 `usage`，不是 HTTP 码**：预算被 reasoning 吃光时返的是
  200 + 空 content，跟成功同形（[[feedback_reasoning_model_probe_needs_headroom_for_reasoning_tokens]]）。
  四个数一起看：`output_tokens` / `reasoning_tokens` / 摘要字符数 / compaction item 数。
* 对照组 `--no-trigger`：不带 trigger 的普通轮能跑到 8,381 output
  ⇒ 证明那个上限**只在压缩轮出现**，不是上游默认也不是部署上限。

**输入一个字节没变、只有预算变了就从 incomplete 翻成 completed ⇒ 定性完成：
是输出预算，跟长度无关。**

## 3. 2026-09-23 那次的完整结论（已修复上线）

| 我发的 max_output_tokens | 结果 |
|---|---|
| **完全不发**（Codex 真实形状）| `incomplete`，output=**4096** reasoning=4096，摘要 **0 字** |
| 4096 | 一模一样 |
| 16384 | `completed`，output=5400 reasoning=2024，摘要 8826 字，compaction item=1 |
| 修复后 + 完全不发 | `completed`，output=8665 **reasoning=4516**，摘要 10516 字 |

最后一行的 `reasoning=4516 > 4096` 直接说明旧上限**不可能**成功。

根因在 `k8s/litellm-callbacks/deepseek_responses_adapt.py` 的
`_rewrite_compaction_request`，判据原本写的是：

```python
if not isinstance(cur, int) or cur < 4096:   # ⛔ None 也进这一支
    out["max_output_tokens"] = 4096
```

🔴 **写「下限保护」时 `None` 和「小于下限」不是一回事。** 客户端没发这个
字段时把下限合并进去，等于**给一个本来无上限的请求装上了上限** —— 保护
措施自己造出了它要防的故障。已改成按输入规模上浮
`max(16384, min(65536, chars//40))`，客户端给得更大就不往下压。

## 4. 两层抬预算的补丁，门控作用域**不一样**

| 文件 | 抬到 | 门控 | 覆盖谁 |
|---|---|---|---|
| `codex_compaction_v2.py` | `_MIN_OUTPUT_TOKENS=8192` | `_is_target()`：`api_base` 含 `://zero-` **或** `model_id` 前缀 `zk-` | **只有 zerokey 落点** |
| `deepseek_responses_adapt.py` | `_compaction_output_budget()` | `_is_deepseek(model)`（模型名含 "deepseek"）+ `_is_responses_call(call_type)` | deepseek 落点 |

⛔ 所以 grok / chatgpt-acct 两类落点**两层都兜不住**，`codex_compaction_v2`
会明明白白打 `gate MISS` 的 WARNING 日志。**「代码里有一层保护」≠「你这条路
上有保护」** —— 门控没命中的保护等于不存在
（memory: 🔴 指向不存在路径的"保护"永不触发）。

环境变量 `CODEX_COMPACTION_API_BASE_MARK` / `CODEX_COMPACTION_MODEL_ID_PREFIX`
可以放宽门控，但 2026-09-23 核过：**各 Deployment 里一个都没设**。

## 5. 改完怎么推上生产

用 `scripts/litellm-198-callback-update.py`（`plan` → `apply` → `verify`），
别再手搓 /tmp 脚本。它替你挡住这轮踩到的三个坑：

1. 🔴 **`git diff HEAD -- <file>` 必须为空。** 2026-09-23 实测：我把工作区
   整份文件 scp 上去，里面带着**本轮没打算发的未提交改动**（`_drop_null_tool_strict`，
   25 行），跟着上了生产还被写进 commit。「我只改了 X」的判据不是记忆，是
   `git diff HEAD`。
2. gray 车道挂的是**内容哈希命名的不可变 CM**，只能「复制现有 CM → 替换一个
   key → 新名 `create`」。⛔ 不能 `kubectl apply`（33 个 key 撞
   `last-applied-configuration` 262144 字节上限直接失败）；⛔ 更不能照 repo
   目录重建 CM（CM 里有 8 个文件仓库里没有）。
3. 共享 CM `litellm-callbacks` 同时挂在 13 个 `chatgpt-acct-*` 上。
   🔴 **只 merge-patch data，永远不 rollout restart** —— 重启在服务的 acct
   号可能永久打死它且回退救不回。它们下次自然重启才生效，这是有意为之。

## 6. 上线后判「这个报错是不是我带入的」——分两层，缺一层都是瞎猜

滚动更新期间旧 pod 会 `connection reset by peer`，在途流式请求断成
**「event-stream 开了、0 个 event、几百字节」**。这个形状跟「新代码把流搞坏了」
一模一样，而且时间戳紧挨着上线，极易自证有罪。两层都要给数据：

**① 代码路径可达性**（结构性，不依赖现场）
读门控函数本体，看这个请求能不能走到改动的那几行。例：改动全在
`_adapt()` 里，而 `_adapt()` 第一行就是 `if not _is_deepseek(model): return data`，
判据是模型名含不含 "deepseek" —— 那么一个 `anthropic.claude-*` 的
`/v1/messages` 请求**一行都执行不到**，与改动无关，这是结论不是推测。

**② 报错时刻落不落在 rollout 窗口里**
```bash
kubectl -n litellm-product get po -l carher.net/litellm-production-route=enabled \
    -o custom-columns=N:.metadata.name,START:.status.startTime
kubectl -n litellm-product get events --sort-by=.lastTimestamp | grep litellm-proxy-gray
```
看旧 ReplicaSet 的 `Killing` / `connection reset by peer` 覆盖到几点。

**③ 有没有台阶**（SpendLogs 才是尺子，pod 日志只留 2~4 分钟）
```sql
SELECT date_trunc('minute',"startTime") m, count(*) n,
       count(*) FILTER (WHERE "status"='failure') fail
FROM "LiteLLM_SpendLogs" WHERE "startTime" > now() - interval '45 minutes'
GROUP BY 1 ORDER BY 1 DESC;
```
要拿**改动前**的分钟级失败数当基线再看改动后，别让 post 去判绝对全绿
（[[feedback_post_probe_must_subtract_the_pre_baseline]]）。那次实测改动前
就有 72/68/43 的尖峰，改动后 41 完全在区间内 ⇒ 无台阶。再按模型族切一刀：
claude 族失败行改动前后都散布着，出事那一分钟一条都没有。

## 7. 常踩的假红

- ⛔ **`-l app=litellm-proxy` 抓到的是另一个不在服务的 pod**，在它日志里
  grep 不到 compaction 记录 = 假红。判生产车道只认 Svc
  `litellm-proxy-nodeport` 的 selector `carher.net/litellm-production-route=enabled`
  → `litellm-proxy-gray-*`（[[feedback_prod_lane_label_is_on_pods_not_deployments]]）。
- ⛔ 上线后 grep 到一屏 `Traceback` 别急着认账：那边常驻的是 SpendLogs
  毒行的 `prisma DataError` / `ValueError` / `MidStreamFallbackError`。
  判**自己**的文件有没有炸，锚点只能是
  `ImportError|SyntaxError|ModuleNotFoundError`（本轮实测 0 命中）。
- ⛔ 客户端报的 input token 数（当时那个 314,009）跟服务端对不上时，别拿它
  当证据 —— 每次重放服务端都报 ~98k，而 98k 就能复现，所以它不是原因。
  **对不上就如实说「这个数字来源未查明」，别补故事。**

## 相关

- callback 怎么写 / 门控陷阱 / 流式 hook：[[litellm-hook-dev]]
- 装**新**补丁（CM + subPath volumeMount + config.yaml 三步）：[[litellm-198-router-patch]]
- 落点是被 fallback 换过去的：[[litellm-198-fallback-chain]]
