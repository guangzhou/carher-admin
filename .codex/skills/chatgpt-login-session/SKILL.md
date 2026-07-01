---
name: chatgpt-login-session
description: >-
  Fully automated ChatGPT web login + OTP (mail.com) and session capture for zerokey
  on 188. Adapted from joeeeeey/chatgpt-codex-skills-bundle chatgpt-login-session;
  our output is zerokey users.json (parsedFetch), not session.json. Use when onboarding
  a new mail.com ChatGPT account, re-seeding a profile, or debugging OTP/capture failures.
  Parent: chatgpt-web-to-codex-zerokey. Index: docs/zerokey-codex-artifacts.md.
---

# ChatGPT Login Session (zerokey / 188)

Upstream reference: [chatgpt-login-session](https://github.com/joeeeeey/chatgpt-codex-skills-bundle).

**Our capture:** `scripts/chatgpt-onboard/zerokey-codex/capture/zerokey-web-capture.py`
→ zerokey `users.json` with sentinel proof token + cookies.

## Related docs / skills

| Resource | Path |
|----------|------|
| Index | `docs/zerokey-codex-artifacts.md` |
| Main runbook | `docs/chatgpt-web-to-codex-zerokey.md` |
| zerokey skill | `.codex/skills/chatgpt-web-to-codex-zerokey/SKILL.md` |

## Credentials (per account, on 188 only)

| Field | File / env |
|-------|------------|
| mail.com email | `MAIL_USER` |
| mail.com password | `secrets/mail_pw.txt` |
| ChatGPT password | `secrets/chatgpt_pw.txt` |
| zerokey user key | `ZK_USER` (usually account id) |

Never commit passwords.

## Full-auto OTP

`zerokey-web-capture.py`: `find_mail_frame()` → `get_otp()` → late-cookie reload.

Unattended flags (used by `add-account.sh`):

```bash
-e OTP_AUTO_ONLY=1
-e OTP_AUTO_MAX=300
-e OTP_FILE_WAIT=0
```

Manual fallback: after `>>> OTP_WAIT_FILE`, write `state/out/otp.txt` (file cleared on entry).

## Onboard new account

Requires main install `~/zerokey-codex` from `install.sh`.

```bash
cd ~/zerokey-codex/ops
./add-account.sh <account_id> <email> '<mail_pw>' '<chatgpt_pw>' [port]
```

Creates `~/zerokey-codex-accounts/<id>/`, container `zerokey-codex-<id>`.

## After onboard — register 198 LiteLLM

From Mac (updates all 8 zerokey models for kristine + timothy):

```bash
./scripts/jms scp scripts/chatgpt-onboard/zerokey-codex/ops/litellm-register-zerokey.py \
  AIYJY-litellm:/tmp/litellm-register-zerokey.py
./scripts/jms ssh AIYJY-litellm \
  'python3 /tmp/litellm-register-zerokey.py --apply --sync-manifest'
```

Per-user LiteLLM keys: add `zerokey-*` / `zerokey-<account>-*` to allowlist (`litellm-pro-ops`).

## Refresh (cron)

```bash
~/zerokey-codex-accounts/<account>/ops/refresh.sh
# or env: MAIL_USER ZK_USER PORT SERVER_CONTAINER
```

Reuses profile → usually no OTP. Failure → `REFRESH_STALE` + optional webhook.

## Verify

```bash
curl -s http://127.0.0.1:<port>/health
curl -s -H 'Authorization: Bearer raw' -X POST http://127.0.0.1:<port>/v1/chat/completions \
  -d '{"model":"gpt-5-5","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

Screenshots: `state/out/screenshots/`.

## Known issues (see main doc traps)

1. xvfb-run PID1 hang → fixed in capture Dockerfile ENTRYPOINT  
2. OTP then anonymous on chatgpt.com → late-cookie reload retry  
3. mail.com skeleton stall → `otp.txt` fallback  
4. **OTP submission rate-limit → SSO fallback → "composer not found" (2026-06-25, confirmed).**
   Symptom: `[1b] still anonymous → silent SSO via Log in` → lands on `accounts.google.com`
   sign-in → `composer not found`. **Root cause** (proved by `post-otp-settled.png` body
   text "Too many attempts" + `error_code: max_check_attempts` + request_id):
   OpenAI rate-limits **OTP submission** on `auth.openai.com/email-verification`
   (NOT OTP-email-send side — emails arrive normally in mail.com inbox).
   Repeated capture retries on the same account → cooldown ~10min.
   - **capture.py is patched (line 526-536)**: detects `max_check_attempts` body text
     post-OTP-submit and `sys.exit` immediately. No more 30s SSO fall-through, no
     wasted second attempt that extends cooldown.
   - **Profile reuse is the real speedup**: with `state/profile/` kept from a prior run,
     a retry typically skips OTP entirely via cookie → ~75s capture (acct-50/51/52/53
     2026-06-25, 4/4 one-shot pass after cooldown). **Never `rm -rf state/` blindly**.
   - **Batch driver**: `~/zerokey-codex/ops/batch-retry.sh` (repo: same path) iterates
     existing acct dirs serially, 60s inter-acct cooldown, capture-only-if-missing,
     resume-safe. See [[zerokey-batch-onboard]] skill.
   - mail.com OTP auto-reader only scans **Inbox** (not Spam) — still a latent risk;
     low-priority since rate-limit was the dominant failure mode.
