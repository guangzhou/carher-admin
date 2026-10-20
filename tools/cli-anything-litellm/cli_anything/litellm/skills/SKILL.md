---
name: cli-anything-litellm
description: Use the installed cli-anything-litellm harness to inspect LiteLLM configs and call a proxy, and — for the 198 deployment — read the upstream source of the live version (src), see which of our patches own which upstream functions (patches), and query the production DB read-only (db). JSON output everywhere.
---

# CLI-Anything LiteLLM

Use `cli-anything-litellm` for agent-safe automation around a real LiteLLM installation and proxy,
and for read-only inspection of the 198 deployment (ns `litellm-product`).

## Rules

- Prefer `--json` for every non-interactive call
- Inspect `backend status` before assuming LiteLLM is installed locally (local = 1.79, not 198's version)
- Use `config inspect` before starting an unfamiliar proxy configuration; secret-like values are redacted
- Save only `base_url` and a default `model` with `profile set`
- Pass proxy credentials through `LITELLM_PROXY_API_KEY` or `--api-key`; never place keys in prompts, profile fields, or command logs
- Call `proxy health --ready` and `proxy models` before inference
- 198 commands (`src`, `patches`, `db`) need ssh to `cltx@10.68.13.198` with `sudo -n`; they never write
- `src` defaults to prod's **live** version tag; passing `--lane` with a different `--tag` is refused
- `patches targets` is the ruler for "is this patch installed"; `patches purpose` is prose only
- `db` runs in a read-only transaction with a statement timeout; a timeout means the query was too broad

## Commands

```bash
cli-anything-litellm --json backend status
cli-anything-litellm --json config inspect /path/to/config.yaml
cli-anything-litellm --json proxy health --ready

# 198: source of the live version
cli-anything-litellm src path                                   # request path + hooks per step
cli-anything-litellm --json src find Router.should_retry_this_error --path litellm/router.py
cli-anything-litellm src show litellm/router.py:7915
cli-anything-litellm src find _is_cooldown_required --tag v1.106.0   # read another version

# 198: patch layer
cli-anything-litellm patches targets --lane prod                # who replaced each upstream symbol
cli-anything-litellm patches compare                            # prod vs gray vs guarded-old
cli-anything-litellm patches purpose dead_deployment_retry

# 198: production DB (read-only)
cli-anything-litellm db anchor                                  # prove it's prod, not dev
cli-anything-litellm db catalog                                 # schema.prisma vs live tables
cli-anything-litellm db config-owner                            # config.yaml vs DB per setting
cli-anything-litellm db spend-reconcile 2026-10-20 --model-group 'kiro-claude-*'
cli-anything-litellm db retention
```

Exit codes: 0 ok; 1 backend error or refused input (JSON error on stderr with `--json`); 2 usage error.
`patches targets` exits 1 when any callback fails to import.
