---
name: litellm-198-key-error-triage
description: >-
  198 LiteLLM（cc.auto-link.com.cn/pro）上「某人的 key 报错」和「某家上游挂了影响多大」的一键排查，只读。
  `scripts/litellm-198-key-error-triage.py rid <8位>` 按报错里的 `API 异常 (req: xxxxxxxx)` 反查：
  key 预算/封禁、**请求名 vs 实际落点**、兜底链、客户端 UA、上游原文、一句话判定；
  `upstream <子串>` 把匹配上游的组逐个直打，再交叉全库 key 白名单 / per-key 兜底 / 全局兜底 / 7 天流量，
  并把杭州 gw 巡检、库里查不到的探针 key、经兜底落进来的流量分开。
  Use when 用户转来「xxx 的 key 报异常 402/400/403/429/500 … API 异常 (req: …)」「你去看看什么原因」、
  「某供应商欠费/挂了，影响哪些 key」「哪些模型直接映射到 X」「他们请求的哪个模型」，
  或要把某个死模型从所有 key 摘掉之前先量影响面。
---

# 198 key 报错 / 上游挂了：先跑脚本，再说话

```bash
python3 scripts/litellm-198-key-error-triage.py rid c7cd902a            # 单条报错
python3 scripts/litellm-198-key-error-triage.py upstream openrouter     # 某家上游的影响面（约 2 分钟）
python3 scripts/litellm-198-key-error-triage.py upstream 10.68.13.188:4130 --no-probe   # 不直打
```

本机跑即可：脚本把自己整份 base64 送到 198 执行（psql / kubectl / 30402 只在那边可达），
走 ssh key + `sudo -n`，不落盘。单测 `backend/tests/test_litellm_198_key_error_triage.py`。

## 给用户的答案长什么样

刘国现要的是三句话：**谁的问题 / 用户现在怎么办 / 要不要他拍板**。例（10-20 实际）：

> liujinling 选的是 `jev`，它背后是 OpenRouter，OpenRouter 账号欠费了。key 本身没问题，换个模型就能用。
> `jev` 还挂在 1305 把 key 上，要不要摘掉你定。

表格、SpendLogs 字段、兜底链放后面或不放。他追问「他们请求的哪个模型」就只答那一句。

## 四个会把结论带偏的地方（10-20 一轮差点踩，第 1 条 10-22 又踩一次）

1. **报错里出现的供应商 ≠ 用户选的模型。** 落点 `model_group` 是兜底链最后一跳；
   用户选的在 `proxy_server_request.model`。10-20 4 把 cursor key 的 400 落在
   `openrouter-glm-5.3-flash`，实际请求 `deepseek-v4.1-flash`（/responses 改写成 `-responses`），
   原因是 input 为空，链上每一跳都拒 —— 和 OpenRouter 欠费无关。脚本会打
   「⚠️ 落点≠请求名」并把这类流量放进「经兜底落进来」一栏。
   🔴 **空 input ≠ 客户端发了空请求**（10-22 我照旧判据跟用户说「他程序有 bug」，错）。
   脚本 `rid` 现在打两行判据：「进网关时 body xMB」（大 ⇒ 我们删空的）+「前一轮调的工具」。
   10-22 那批 13 次全是 Codex `view_image` 截图（base64 几 MB、token ~1k）被 deepseek 裁剪按字节删光，
   10-25 已修（`ea4da5c`），细节见 skill `codex-deepseek-tool-triage` 根因 6。再出现 = 新形状，按那三步查。
   UA `Python/aiohttp` 是我们 ws-ingress（Codex WS 入口）转发的，不是用户脚本。
   ⛔ 别给用户「开新会话 / 命令加 `| head`」这类绕路，修网关。
2. **死组流量绝大部分是巡检。** `carher-hangzhou`（杭州 gw 的上游凭据）每组每小时约 10 发
   "What's 1+1"、prompt ≤15 token；7 天 5.7 万次失败几乎全是它。另有约 750 次来自库里查不到的
   key（master key、用完即删的探针 key），也不是人。脚本默认把两者都剔出真人统计。
3. **网关把原文脱敏成 `API 异常 (req: xxxxxxxx)`**，响应体分不出欠费还是连不上。
   原文在 SpendLogs `metadata.error_information.error_message`；**连不上上游的 500 不落
   SpendLogs**，只在 pod 日志（`litellm_call_id=<rid>`）。脚本按 req 前缀回查，查不到就提示去日志。
4. **key 自己的额度先看但别停在那。** `spend/max_budget/budget_reset_at` 正常不代表请求成功；
   cursor key 还有 `metadata.budget_family_overrides`（premium 分族额度）。

## `upstream` 的五节怎么读

| 节 | 内容 | 判据 |
|---|---|---|
| 1 | 匹配组逐个 `disable_fallbacks`+`num_retries=0` 直打 | 欠费=402 `Insufficient credits`；端口关=500 `Connection error`（充值也救不回）；`⚠️组内还有别家部署` 说明不是单腿 |
| 2 | 白名单名经 per-key alias → 全局 alias → 同名解析后落在死组 | 只有用户点名选它才出错；`models=[]` 全放行的 key 摘不掉，单列数字 |
| 3 | per-key `router_settings.fallbacks` 含死腿，★=第一跳就死 | ★ 最伤：主腿一挂就多等一次死腿再报错。per-key 表**整表替换**全局表 |
| 4 | 全局 fallbacks / `model_group_alias` | 「源组已不存在」的行不会触发，属遗留清理 |
| 5 | 7 天流量：巡检 / 查不到的 key / 真人直选 / 经兜底落进来 | 判「有没有人在用」只看真人直选；SpendLogs 只留约 7 天，更早看 `LiteLLM_DailyUserSpend` |

匹配看 `model`/`api_base`/`custom_llm_provider` 三个字段：`openrouter-gpt-5.6-*` 写的是
`openai/gpt-5.6-sol` + `api_base=openrouter.ai`，只看前缀会漏。`openrouter-gpt-5.2~5.5-pro`
不是 OpenRouter 原生，是 188:4130 的转发桥（10-20 端口已关）。

## 查完要动手时

| 要做的事 | 用什么 |
|---|---|
| 把死模型从**所有** key 白名单摘掉 | `scripts/litellm-198-key-allowlist.py --prefix "" --include-blocked --rm-model <M>`（skill `litellm-198-key-allowlist`）。⚠️ 默认范围只有 `cursor-`/`claude-`，「所有 key」必须 `--prefix ""` |
| per-key 兜底行里换掉/摘掉死腿 | `scripts/litellm-198-key-fallback-swap-leg.py`（skill `litellm-198-fallback-chain` §11） |
| 全局兜底整行换 | `scripts/litellm-198-fallback-set-rows.py`（同上 §9） |

10-20 实跑 `jev` 摘除：dry-run `planned=1420`（=库里带 jev 的 1305 未封 + 115 已封）→ 金丝雀 1 →
全量 1419，**5 分 26 秒**，`applied_ok=1419/1419`。验收三件：快照 vs 库逐 key 比对 0 差异
（阳性对照：快照对快照必须报 1420 差异）、全库 `still_has_jev=0`、临时 key 打 jev=403
`key_model_access_denied` / 打活模型=200，测完删 key。快照留 `198:~/jev-rm-1020/`。

## 不是单条报错，而是「CC 卡死 / 重复说话 / 失忆 / 花太多」

用 `scripts/litellm-198-key-session-health.py <key_alias>`（`--minutes N` / `--compactions --hours 48` /
`--alias-like 人名`）。每个会话一行（上下文平均/最大、每次花费、UA）+ 每 10 分钟最大上下文，再列异常行：
压缩（rejected / EMPTY / ok-closed / **ok-NOT-closed=半截⇒失忆**）、超长、兜底到非 kiro、复读。
只走 `(api_key,startTime)` 索引、≤2h 分段串行，不压库。

10-23 这一族的结论（细节 ⇒ memory `project_cc_repeat_loop_is_grok_fallback_on_kiro_too_long_2026_10_23`）：

| 症状 | 真因 | 现状 |
|---|---|---|
| CC 不断重复同一句 | kiro 超长 400 文案非标准 ⇒ 网关当普通故障兜底到 `sa-grok-4.20`，grok 读不了 80~99 万上下文只会复读；`/goal` 把同一请求反复送回 | ✅ 10-23 修：kiro.rs 0010 回 `prompt is too long: N tokens > M maximum` + `error_sanitize` 保留原句 ⇒ 现有 `context_window_fallbacks` 规则直接 400 不兜底 |
| CC 满了一直重试压缩 | 压缩要把整段对话发给模型总结；>100 万时总结请求自己也超长；旧版 2.1.259 不提前压 | 升级到新版（默认 ~96.7 万压缩）；救旧会话用 `/rewind` →「Summarize up to here」只压前半段 |
| 压缩后「失忆」/ `no assistant message in summarization response` | kiro opus-5.5 写长总结随机提前 `end_turn`（重放 6 发 4 发半截、无错误帧，kiro.rs 没丢）；CC 拿半截/空总结替换历史 | ⏸ 未修（候选：总结无 `</summary>` 就重试/换 opus-4.6）；本地 transcript 全量还在，可读回 |
| 一小时几百美元 | kiro 无 prompt cache，每次按全价收全部上下文 | 建议用户 `CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000` |

## 相关

- 10-20 OpenRouter 全盘点结论：memory `project_198_openrouter_impact_survey_2026_10_20`
- Codex × DeepSeek 专项（DSML/工具/compaction）：skill `codex-deepseek-tool-triage`（也有 `--rid`，签名表偏 DeepSeek）
- 杭州 gw 凭据是什么：memory `reference_carher_hangzhou_key_is_the_hangzhou_gw_upstream_credential`
