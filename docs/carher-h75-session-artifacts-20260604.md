# CarHer H75 Session Artifacts - 2026-06-04

This document indexes the skills, scripts, and docs moved into this repository from the 2026-06-04 H75 full-fleet upgrade and new-Her automation session.

## Skills

The following skills are now stored in both `.cursor/skills/` and `.codex/skills/` so Cursor-style and Codex-style agents can load the same runbooks.

| Skill | Purpose |
|---|---|
| `add-instances` | New H75 Her creation pipeline and fallback manual creation flow. |
| `carher-batch-her-upgrade` | Batch H75 upgrade manifest, wave rollout, hardening gates, and fleet scans. |
| `carher-upgrade-flow` | Front-door upgrade flow, readiness model, repeat-failure checklist, and H75 deployment-hardening gate. |
| `carher-her-reply-failure-triage` | No-reply and post-upgrade startup failure triage, including H75 `llm`, secret, and read-only mount signatures. |
| `carher-feishu-bench-regression` | Feishu smoke/bench rules, deployment-health scan contract, and smoke-only reporting boundaries. |

## Scripts

| Script | Purpose |
|---|---|
| `scripts/create-h75-her.py` | Create a Her through Admin API and converge it to H75 in one pipeline: create, harden, generated-config repair, budget, and readiness gates. |
| `scripts/h75-batch-upgrade.py` | In-cluster H75 hardening executor used by both batch upgrades and the new-Her pipeline. |

## Docs

| Doc | Purpose |
|---|---|
| `docs/carher-h75-batch-upgrade-retro-20260604.md` | Full-fleet H75 upgrade retrospective, root causes, fixes, and anti-regression checklist. |
| `docs/carher-h75-session-artifacts-20260604.md` | This index. |

External Feishu document:

```text
https://t83dfrspj4.feishu.cn/docx/DydTdJMAxouT8Txua4NcCUhGnjd
```

## New Her Standard Command

```bash
cd ~/codes/carher-admin
scripts/create-h75-her.py \
  --id 271 \
  --name "奕达的her" \
  --app-id "cli_xxx" \
  --app-secret "xxx" \
  --owner-name "朱奕达"
```

Use `--home-channel oc_xxx` only when the exact bot-visible Feishu chat id is known. Without it, report Feishu group smoke as `not_self_tested/no_home_channel`.

## Required Passing Gates

The new-Her pipeline must pass these gates before reporting runtime success:

- `openclaw_ready.ok=true`
- `deployment_hardening.base_config=carher-base-config-h75`
- `deployment_hardening.openai_base_ok=true`
- `deployment_hardening.dify_base_ok=true`
- `deployment_hardening.runtime_plugins_refresh=0`
- `deployment_hardening.prod_key_matches_litellm=true`
- `deployment_hardening.copy_deps_init=true`
- `deployment_hardening.readonly_h75_mounts=[]`
- `runtime_probes.hermes_deps_ok=true`
- `runtime_probes.dify_health_ok=true`

## Known Boundary

`oauth_callback_http=502` can occur on existing H75 reference Her instances too. Treat it as a callback/tunnel routing surface, not as proof that the Her runtime failed. When OAuth callback is in scope, investigate it separately.
