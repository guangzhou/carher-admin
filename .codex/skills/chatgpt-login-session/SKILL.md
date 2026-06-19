---
name: chatgpt-login-session
description: >-
  Fully automated ChatGPT web login + OTP (mail.com) and session capture for zerokey
  on 188. Adapted from joeeeeey/chatgpt-codex-skills-bundle chatgpt-login-session;
  our output is zerokey users.json (parsedFetch), not session.json. Use when onboarding
  a new mail.com ChatGPT account, re-seeding a profile, or debugging OTP/capture failures.
---

# ChatGPT Login Session (zerokey / 188)

Upstream reference: [chatgpt-login-session](https://github.com/joeeeeey/chatgpt-codex-skills-bundle)
(produces `storage_state.json` + `session.json` for Pro delegation).

**Our stack** uses `scripts/chatgpt-onboard/zerokey-codex/capture/zerokey-web-capture.py`
instead — same login + OTP flow, but captures `POST /backend-api/f/conversation` into
zerokey's `users.json` (`parsedFetch` with sentinel proof token + cookies).

## Credentials (per account)

| Field | Env / file |
|---|---|
| mail.com email | `MAIL_USER` |
| mail.com webmail password | `secrets/mail_pw.txt` → `MAIL_LOGIN_PW_FILE` |
| ChatGPT login password | `secrets/chatgpt_pw.txt` → `CHATGPT_PW_FILE` |
| zerokey user key | `ZK_USER` (usually account id) |

Never commit passwords. Write only on 188 under `~/zerokey-codex-accounts/<account>/secrets/`.

## Full-auto OTP (mail.com)

Capture script polls mail.com inbox in-browser (patchright, same context as ChatGPT login):

1. `mailcom_login()` → webmail navigator
2. `find_mail_frame()` → iframe `name=mail`
3. `get_otp()` → scan OpenAI sender lines, click mail, extract 6-digit code
4. Fill OTP on `auth.openai.com`, late-cookie reload-retry on chatgpt.com

Strict unattended mode (no manual `otp.txt` fallback):

```bash
-e OTP_AUTO_ONLY=1
-e OTP_AUTO_MAX=300
-e OTP_FILE_WAIT=0
```

## Onboard a new account (188)

Main install must exist at `~/zerokey-codex` (see `zerokey-codex/install.sh`).

```bash
cd ~/zerokey-codex/ops
./add-account.sh <account_id> <email> '<mail_pw>' '<chatgpt_pw>' [port]
# e.g. timothy on 8124:
./add-account.sh timothy timothy_mossey871@mail.com '<mail_pw>' '<gpt_pw>' 8124
```

Creates `~/zerokey-codex-accounts/<account>/` with profile, secrets, `state/users.json`,
container `zerokey-codex-<account>` on `<port>`.

## Refresh (cron)

```bash
MAIL_USER=... ZK_USER=... PORT=8124 ~/zerokey-codex-accounts/<account>/refresh.sh
```

Reuses profile → usually no OTP. On failure keeps old session + `REFRESH_STALE`.

## Wire into 198 LiteLLM Pro

Add `model_list` entries with `api_base: http://10.68.13.188:<port>/v1`, `api_key: raw`.
See `chatgpt-web-to-codex-zerokey` skill §198 LiteLLM Pro.

## Verify

```bash
curl -s http://127.0.0.1:<port>/health
curl -s -H 'Authorization: Bearer raw' -X POST http://127.0.0.1:<port>/v1/chat/completions \
  -d '{"model":"gpt-5-5","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

Screenshots on failure: `state/out/screenshots/`.
