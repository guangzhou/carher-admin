---
name: litellm-claude-family-budget
description: "Manage 198 prod Claude-family daily limits for claude-code keys; use for Fable/Opus/Sonnet/Haiku quota changes without changing total key budgets."
---

Use the full runbook at `.codex/skills/litellm-claude-family-budget/SKILL.md`.
The reusable helper is:

```bash
./scripts/litellm-198-claude-family-limit.sh --owner '杨丞' --limit 300
./scripts/litellm-198-claude-family-limit.sh --owner '杨丞' --limit 300 --apply
./scripts/litellm-198-claude-family-limit.sh --owner '杨丞' --limit 2000 --max-budget 2000 --apply
./scripts/litellm-198-claude-family-limit.sh --all --limit 300 --max-budget 300 --apply
```

It defaults to dry-run, matches only `claude-code-*`, preserves metadata, and
changes only `metadata.budget_family_overrides.claude` unless `--max-budget` is
explicitly provided. Full balance resets use
`scripts/litellm-198-claude-reset-all.sh` instead.
