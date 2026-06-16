# zerokey-codex — ChatGPT web → Codex/OpenAI API bridge

Self-contained bundle to deploy the ChatGPT **web-chat** → OpenAI-compatible API
bridge on `188` / `10.68.13.188:8123` (for when the Codex 5h/7d quota is spent but
web chat still works). Standalone Docker stack; does not touch K8s / carher-admin.

- **Design + full runbook:** `docs/chatgpt-web-to-codex-zerokey.md`
- **Skill:** `.codex/skills/chatgpt-web-to-codex-zerokey/SKILL.md`
- **On-host ops:** `ops/README.md`

## Contents

```
install.sh          clone upstream zerokey + overlay patches + build dir layout
zerokey-patch/      our minimal changes + new files over upstream zerokey
  routes/raw.js       NEW: raw passthrough + model resolver
  routes/chatgpt.js   raw branch + model passthrough (VS Code path unchanged)
  core/chatgpt/api.js per-request model override
  config/constants.js real /v1/models slugs
  zerokey-serve-codex.js  headless launcher (Bearer → vscode | raw)
  Dockerfile / .dockerignore / docker-compose.yml
capture/
  Dockerfile          patchright capture image (xvfb-run PID1 fix)
  zerokey-web-capture.py  login chatgpt.com + capture /backend-api/f/conversation
ops/
  refresh.sh          re-capture → validate → atomic swap → restart + alert
  capture-manual.sh   interactive (OTP) re-capture
  README.md
```

## Quick start

```bash
./install.sh                 # → ~/zerokey-codex (on 188)
# then follow install.sh's printed next steps (secrets, build, capture, up)
```
