# zerokey-codex — ChatGPT web → OpenAI API bridge

Self-contained bundle for **ChatGPT web chat quota** → OpenAI-compatible API on
`188` / `10.68.13.188:8123+`. Standalone Docker; does not touch K8s / carher-admin.

## Documentation

| Doc | Content |
|-----|---------|
| [docs/zerokey-codex-artifacts.md](../../../docs/zerokey-codex-artifacts.md) | **Index** — skills, scripts, verify commands |
| [docs/chatgpt-web-to-codex-zerokey.md](../../../docs/chatgpt-web-to-codex-zerokey.md) | Design, traps, 198 integration |
| [docs/zerokey-codex-agent-bridge-design.md](../../../docs/zerokey-codex-agent-bridge-design.md) | Planned Codex Agent bridge |

## Skills

- `.codex/skills/chatgpt-web-to-codex-zerokey/SKILL.md`
- `.codex/skills/chatgpt-login-session/SKILL.md`
- `.claude/skills/chatgpt-web-to-codex-zerokey/SKILL.md` (中文详版)

## Contents

```text
install.sh                         # clone upstream + overlay patches
zerokey-patch/                     # raw.js, chatgpt.js, Docker, compose
capture/
  Dockerfile
  zerokey-web-capture.py           # login + OTP + f/conversation capture
ops/
  refresh.sh                       # cron refresh
  capture-manual.sh
  add-account.sh                   # multi-account onboarding
  docker-compose.account.yml
  litellm-register-zerokey.py      # 198 LiteLLM register (8 models)
  README.md                        # on-host runbook
```

## Quick start (on 188)

```bash
./install.sh
# secrets + docker build + capture + compose up — see install.sh output
```

## 198 LiteLLM (from Mac)

```bash
./scripts/jms scp scripts/chatgpt-onboard/zerokey-codex/ops/litellm-register-zerokey.py \
  AIYJY-litellm:/tmp/litellm-register-zerokey.py
./scripts/jms ssh AIYJY-litellm \
  'python3 /tmp/litellm-register-zerokey.py --apply --sync-manifest'
```
