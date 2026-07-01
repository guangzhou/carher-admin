---
name: zerokey-batch-onboard
description: >-
  Serially onboard / re-onboard multiple zerokey ChatGPT-web accounts on 188 with
  cooldown-aware resume + profile-cookie reuse. Use when expanding 198 zerokey-pool
  beyond 1-2 accts, or recovering after a half-stopped batch (users.json present but
  container missing).
---

# Zerokey Batch Onboard (188 / mail.com)

Wraps `scripts/chatgpt-onboard/zerokey-codex/ops/add-account.sh` with a serial loop that:

- skips accounts already captured + serving (resume-safe)
- restarts containers for accounts captured but not running
- runs `add-account.sh` only for accounts missing `state/users.json`
- 60s cooldown between accts (lowers OpenAI submission rate-limit signal)
- relies on patched `capture.py` to fail-fast on `max_check_attempts`
  (see [[zerokey-capture-max-check-attempts]])

Parent runbook: [[chatgpt-login-session]] · Pool register: [[zerokey-pool-198-expand-2026-06-25]]

## When to use

| Scenario | This skill? |
|----------|-------------|
| Add 1 acct, fresh creds | ❌ use `add-account.sh` directly |
| Add 3+ accts | ✅ |
| Half-stopped prior batch (users.json present, container down) | ✅ — `SKIP_OK=1` mode handles it |
| Re-capture after expired auth.json | ✅ `SKIP_OK=0 batch-retry.sh acctN` |

## Pre-flight (on 188)

Each candidate `acctN` must already have:

```
~/zerokey-codex-accounts/acctN/
  ops.env                     # MAIL_USER=... PORT=...
  secrets/mail_pw.txt
  secrets/chatgpt_pw.txt
```

If any are missing, first scaffold via `add-account.sh acctN <email> <mail_pw> <gpt_pw> <port>`
(one-shot run creates the dir).

## Run

```bash
# from 188
cd ~/zerokey-codex/ops
./batch-retry.sh acct50 acct51 acct52 acct53 acct60 acct61 acct63 acct64 acct66

# or via Mac
~/codes/carher-admin/scripts/jms ssh JSZX-AI-03 'bash ~/zerokey-codex/ops/batch-retry.sh acct50 acct51'
```

Tail per-acct logs at `/tmp/zk-batch-<acct>.log`; final table at `/tmp/zk-batch.summary`.

## Result tags (in summary)

| Tag | Meaning | Next action |
|-----|---------|-------------|
| `OK (Ns)` | captured + container healthy | register to pool |
| `ALREADY_OK` | users.json + container exist (resume hit) | register to pool if not yet |
| `COMPOSED_UP` | users.json was there, container started by this run | register to pool |
| `COMPOSE_FAIL` | docker compose up failed | check `docker logs zerokey-codex-acctN` |
| `CAPTURED_NO_COMPOSE` | capture OK but container missing (rare — batch interrupted) | re-run with same acct |
| `COOLDOWN_MAX_CHECK` | OpenAI OTP submission rate-limit hit | wait ≥10min, then re-run |
| `OTP_NOT_DELIVERED` | mail.com inbox auto-read timed out | check Spam, or `manual-onboard.sh` |
| `TIMEOUT` | hit `CAPTURE_TIMEOUT` (default 600s) | grep log for stuck step |
| `FAIL rc=N` | other capture/compose failure | read log |

## Profile cookie reuse (the real speedup)

A retry on an acct that **already has** `state/profile/` typically completes in **~75s**
(cookie skips OTP). Empirical: 2026-06-25 acct-50/51/52/53 all OK 72-76s after a prior
cooldown — profile dir was untouched between attempts. **Never `rm -rf state/profile`
blindly** when retrying.

## Common traps

1. **`SKIP_OK=1` (default) means won't re-capture good accts** — if you really want to
   re-seed (e.g. token expired), pass `SKIP_OK=0 batch-retry.sh acctN`
2. **Container name mismatch**: `add-account.sh acct50` → `zerokey-codex-acct50`, but
   `add-account.sh 50` → `zerokey-codex-50`. Stick to `acctNN` form for new batches;
   see [[zerokey-pool-198-expand-2026-06-25]]
3. **jms ssh transient `Permission denied`**: per [[jms-stale-ssh-kill-fixes-tunnel]]
   wait ≥10s for JMS self-heal, don't kill local ssh aggressively while batch runs on 188
4. **Batch killed mid-acct**: capture may have written `users.json` but container never
   came up. Re-run same acct under default `SKIP_OK=1` mode — the script detects this
   and only does `docker compose up -d`, no fresh capture
5. **No NOPASSWD sudo on 188**: don't bake any `sudo` calls into the driver; cltx only
   has docker group access
6. **Docker bridge network pool exhaustion** (2026-06-25 实证): each acct uses its own
   docker-compose project → its own bridge network. After ~30 accts, default 172.x pool
   is full and `docker compose up` returns `all predefined address pools have been fully
   subnetted` → `COMPOSE_FAIL`. Quick fix: `docker network prune -f` (only removes
   networks with no containers). Real fix (TODO): switch
   `docker-compose.account.yml` to `network_mode: host` (zerokey already publishes PORT
   on host) or share one named external network across all accts. Check current pressure:
   `docker network ls | wc -l` — anything ≥30 is a yellow flag.

## After batch — register to 198 zerokey-pool

```bash
# from Mac
python3 ~/codes/carher-admin/scripts/prod-add-zerokey-accounts.py \
  --ports 8144,8145,8146,8147 --apply
```

See [[zerokey-pool-198-expand-2026-06-25]] for pool semantics + manifest sync.
