# cli-anything-litellm

Agent-facing CLI for LiteLLM. Two backends:

1. a local LiteLLM install + any proxy over HTTP (`backend`, `config`, `profile`, `proxy`,
   `organization`, `native`)
2. **the 198 deployment** (ns `litellm-product`), read-only over ssh: the fork tag that matches
   the live version (`src`), the live interpreter of each lane (`patches`), and production
   Postgres (`db`)

Design notes and what reading the 1.105.1 source established: `../../LITELLM.md`.

## Install

```bash
uv tool install --editable tools/cli-anything-litellm   # or: uv pip install -e tools/cli-anything-litellm
```

The 198 commands need `ssh cltx@10.68.13.198` with a key and `sudo -n`.
Override the host with `LITELLM_198_HOST`.

## 198 commands

| Command | What it answers |
|---|---|
| `src path` | how a request flows through LiteLLM, and which patch hooks each step |
| `src find SYMBOL [--path P] [--tag T \| --lane L]` | where a def/class is in the version that's live |
| `src show PATH:LINE` | that def/class's body |
| `patches targets [--lane L]` | for 27 patched upstream symbols, who replaced each (identity check in the pod) |
| `patches compare` | the same for prod / gray / guarded-old, differences only |
| `patches purpose [NAME]` | what each patch is for (catalog) |
| `db anchor` | structural counts that prove you're on prod, plus connection headroom |
| `db catalog [--tag T]` | schema.prisma vs live DB: missing tables, extras, largest, migrations |
| `db config-owner` | for each router/general setting: config.yaml or DB row |
| `db spend-reconcile DATE [--model-group G] [--limit N]` | per busy key: daily requests vs SpendLogs rows |
| `db retention` | oldest/newest SpendLogs row and daily-table date |

## Tests

```bash
cd tools/cli-anything-litellm
CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest cli_anything/litellm/tests -v
```

`test_198_e2e.py` talks to the real 198 and fails if it can't reach it. See `tests/TEST.md`.
