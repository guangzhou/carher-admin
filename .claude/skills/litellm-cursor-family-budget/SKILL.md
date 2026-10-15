---
name: litellm-cursor-family-budget
description: "Manage and diagnose 198 prod per-key daily model-family buckets. Since 2026-10-15 every cursor-* key has ONE premium bucket (Opus family + gpt-6-astra shared) = 50% of the key's daily max_budget; the old Claude $100 / other $500 buckets only apply to non-cursor keys (claude-code-*). Use when the user asks about Cursor/Opus/astra limits, 高配额度, Claude family limits, /查余额 family lines, raising one person's limit, or family-budget resets."
metadata:
  requires:
    bins: ["ssh", "python3", "kubectl", "redis-cli"]
---

# 198 Cursor Model-Family Budgets

Authoritative implementation: `k8s/litellm-callbacks/budget_notice.py`.
`.codex/skills/litellm-cursor-family-budget/SKILL.md` is the older full runbook (pre-10-15 bucket model).

## 现行规则速查（2026-10-15）

| key | 系列额度 | 额度来源 |
|---|---|---|
| `cursor-*`（全部） | **只有高配桶**：Opus 系列 + `gpt-6-astra` 共用 | `metadata.budget_family_overrides.premium`，否则 `max_budget × 0.5` |
| `claude-code-*` 等其他 gated key | 旧三桶 `gpt53` / `claude`（$100）/ `other`（$500） | metadata 覆盖 > env > 默认 |

sonnet / haiku / GPT / Grok 在 cursor key 上**没有系列额度**，只受每日总额 `max_budget`。

## 常见操作

**给某人提额**（例：10-15 卜弋天 `cursor-buyitian-pwcf` 总额 10000、高配 10000）：
`/key/update` 同时传 `max_budget` 与**整份** `metadata`（读出来改 `budget_family_overrides.premium` 再写回，
不要只传 `{budget_family_overrides:…}` —— metadata 是整体替换）。写前存整行快照，写后比对 metadata 键数不变。
⚠️ 网关缓存 key 配置约 1 分钟：刚写完 `/查余额` 还是旧值，等 ~75s 再验。
改完同步飞书表：`sync-lark-key-models.py --with-limits --only-db-alias-file <文件> --apply`。

**改比例 / 退回旧规则**（不改代码，三车道 env）：`BUDGET_FAMILY_PREMIUM_RATIO=0.3`；
`BUDGET_FAMILY_PREMIUM_KEY_PREFIXES=""` = 全部 cursor key 退回 claude/other 桶。

**验收**：建临时 `cursor-zz-probe-premium-<ts>` key（`max_budget=500`），`/查余额` 必须显示
`高配模型(Opus/Astra) $0.00/$250.00` 且无「Claude 家族」行；往它的旧 claude 桶塞 $150 后 sonnet 仍 200；
高配桶塞 $250 后 opus/astra 被拦、gpt-6-sol 放行；跑完删 key 和塞的 Redis 键。别动真实用户的桶。

## 旧桶细节（非 cursor key 仍适用）

Do not use a broad `"claude" in model` test: `claude-glm`,
`claude-deepseek`, `claude-kimi`, `claude-grok`, and `claude-gpt` remain in
`other`.

Remove stale GPT-5.3 Redis buckets with the guarded command:

```bash
./scripts/litellm-198-cursor-remove-gpt53-bucket.sh
./scripts/litellm-198-cursor-remove-gpt53-bucket.sh --apply
```

Fast read-only audit:

```bash
./scripts/litellm-198-family-budget-audit.sh
```

For a per-owner or per-alias family-limit change, use the guarded helper:

```bash
./scripts/litellm-198-cursor-family-limit.sh --owner '杨丞' --limit 300
./scripts/litellm-198-cursor-family-limit.sh --owner '杨丞' --limit 300 --apply
./scripts/litellm-198-cursor-family-limit.sh --alias cursor-yangcheng-35mz --limit 300 --apply
```

It matches only `cursor-*`, preserves existing metadata, changes only
`metadata.budget_family_overrides.claude`, and read-back verifies every match.
It does not change total `max_budget`, spend, blocked state, models, or aliases.

Before a callback rollout, compare live lanes and use the guarded plan/apply/
verify flow from the full runbook. Never use Redis `FLUSHDB` or manually delete
serving pods.

## 🔴 现行规则（2026-10-15 起）：全部 cursor key 走高配桶，额度 = 每日总额 × 50%

**全部 `cursor-*`** 已没有 Claude 家族 / 其他模型 两个桶：只有 Opus 系列 + `gpt-6-astra` 共用一个
**高配桶**，额度取这把 key 的 `max_budget × 0.5`（$500 ⇒ $250，$800 ⇒ $400，$10000 ⇒ $5000）。
sonnet / haiku / GPT / Grok 等不受系列额度约束，只受 key 每日总额。`claude-code-*` 等非 cursor key 不变。

| env（不设 = 默认） | 默认 | 作用 |
|---|---|---|
| `BUDGET_FAMILY_PREMIUM_KEY_PREFIXES` | `cursor-` | 高配规则前缀；**设成空串 = 一键退回旧 claude/other 桶**（回滚开关） |
| `BUDGET_FAMILY_PREMIUM_RATIO` | `0.5` | 高配额度占总额比例 |
| `BUDGET_FAMILY_PREMIUM_USD` | `300` | key 没有 `max_budget` 时的兜底额度 |

metadata `budget_family_overrides.premium` 仍优先（10-15 时只有 std 和 `cursor-d95e9279-e7gq` 写了 8000）。
旧 metadata 里的 `claude` / `other` 覆盖（林森 2000 等）对 cursor key **已不生效**，未清理。
代码 `ab02195`，三车道 sha `ccc5b086c28d`；生产回滚 CM 见 `callback-update.py plan` 打印的旧名。

## 高配桶（premium，2026-10-13 起，10-15 前只对 std）

名单内的 key **不再有 claude / other 桶**：只有 Opus 系列（任何 `opus` 型号词）+ `gpt-6-astra`
记账和执法，两者**共用一个桶**，默认 `$300`/天；sonnet、haiku、其他 GPT/Grok 等一律不受系列额度约束，
只受 key 自身 `max_budget`（`cursor-liuguoxian-std` = `$800`/天，LiteLLM 原生）。

- 名单：env `BUDGET_FAMILY_PREMIUM_KEY_ALIASES`（逗号分隔，精确名）；**未设 = 只 `cursor-liuguoxian-std`**。
- 限额：metadata `budget_family_overrides.premium` > env `BUDGET_FAMILY_PREMIUM_USD` > 300。
- 为什么按 key 名放量而不是 metadata：记账路径（`async_log_success_event`）只拿得到 alias/hash，
  拿不到 key metadata ⇒ 用 metadata 选规则会让执法和记账各认各的桶。
- `/查余额` 显示 `系列额度：高配模型(Opus/Astra) $x/$300.00`；Codex 超额仍回 429 停机（无兄弟桶可提示切换）。
- 名单外的 key 行为不变。上线当天切换前的旧 claude 桶读数会留在 Redis 当天，不再被读。

上线走 `scripts/litellm-198-callback-update.py`：`--deploy litellm-proxy-gray` 先验（零流量，
30405 用真 key 打 `/查余额` + sonnet），再不带 `--deploy` 上生产（同步共享 CM、不重启 acct），
最后 `--deploy litellm-proxy-guarded-old`。gray/guarded-old 的 `deepseek_responses_adapt.py`
与 repo 不一致是**既存**漂移（换代前就是 `ec6b87ded799`），不是这次带入的。
