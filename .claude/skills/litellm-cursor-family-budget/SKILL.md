---
name: litellm-cursor-family-budget
description: "Manage and diagnose 198 prod per-Cursor-key daily model-family buckets, including the Claude family $100/day bucket. Use when the user asks about Cursor family limits, Claude/Fable/Opus/Sonnet/Haiku usage, /查余额, or family-budget resets."
metadata:
  requires:
    bins: ["ssh", "python3", "kubectl", "redis-cli"]
---

# 198 Cursor Model-Family Budgets

Use the repository skill at `.codex/skills/litellm-cursor-family-budget/SKILL.md`
as the full runbook. The authoritative implementation is
`k8s/litellm-callbacks/budget_notice.py`.

Cursor has two per-key Beijing-day buckets (GPT-5.3 was retired as a separate
Cursor bucket on 2026-09-24):

- `claude`: Claude family model words `fable`, `opus`, `sonnet`, or `haiku`, default `$100`.
- `other`: everything else, including GPT-5.3, default `$500`.

Non-Cursor gated aliases retain the legacy `gpt53` bucket; this runbook's
delete operation is restricted to `cursor-*` tokens.

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
