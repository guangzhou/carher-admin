#!/usr/bin/env python3
"""chatgpt-acct-reset-bank.py — query / redeem ChatGPT banked rate-limit resets.

OpenAI gave Pro/Plus subscribers banked rate-limit resets (launched 2026-06-11).
The Codex Desktop app is the only documented surface, but the underlying server
endpoints work from any OAuth-authenticated client. We exploit that to redeem
from inside our chatgpt-acct K8s pods (which already hold a fresh access_token
on /chatgpt-auth/auth.json).

Endpoints (from openai/codex PR #28143 Rust source):
  GET  https://chatgpt.com/backend-api/wham/usage
       → adds `rate_limit_reset_credits.available_count` to the usage payload
  POST https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume
       Body: {"redeem_request_id": "<uuid>"}
       Returns {"code": "reset|no_credit|nothing_to_reset|already_redeemed",
                "credit": {...}, "windows_reset": <int>}
  Headers (same as Codex CLI):
    Authorization: Bearer <access_token>
    ChatGPT-Account-ID: <account_id>
    OpenAI-Beta: codex-1
    originator: codex_cli_rs

This script is meant to run **inside a chatgpt-acct pod** (it reads
/chatgpt-auth/auth.json directly). For batch ops from outside, see the
companion bash driver `chatgpt-acct-reset-bank.sh` which kubectl-cp's this
in and runs it per pod.

Modes:
  probe        — GET /wham/usage and print one-line summary
  redeem       — if credits >= 1, POST consume once (idempotent via UUID)
  probe-json   — full JSON dump of /wham/usage payload

2026-06-23 实证：acct-26..30/37/40 redeem 全部 200 + code:reset + 7d% 100→0.
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

AUTH_PATH = "/chatgpt-auth/auth.json"
WHAM_USAGE = "https://chatgpt.com/backend-api/wham/usage"
WHAM_CONSUME = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"

USAGE = """Usage:
  chatgpt-acct-reset-bank.py probe         # one-line summary
  chatgpt-acct-reset-bank.py probe-json    # full /wham/usage JSON
  chatgpt-acct-reset-bank.py redeem        # POST consume if credits>=1
  chatgpt-acct-reset-bank.py redeem --force  # POST consume even if usage probe disagrees (server is source of truth)
"""


def load_auth():
    with open(AUTH_PATH) as fh:
        d = json.load(fh)
    tok = d.get("access_token") or ""
    acct = d.get("account_id") or ""
    if not tok or not acct:
        raise SystemExit(f"BAD_AUTH keys={list(d.keys())} tok_len={len(tok)} acct_len={len(acct)}")
    return tok, acct


def headers(tok, acct, post=False):
    h = {
        "Authorization": f"Bearer {tok}",
        "ChatGPT-Account-ID": acct,
        "OpenAI-Beta": "codex-1",
        "originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.41.0 (chatgpt-acct-reset-bank)",
        "Accept": "application/json",
    }
    if post:
        h["Content-Type"] = "application/json"
    return h


def usage(tok, acct):
    req = urllib.request.Request(WHAM_USAGE, headers=headers(tok, acct))
    r = urllib.request.urlopen(req, timeout=20)
    return json.loads(r.read())


def consume(tok, acct, rid):
    body = json.dumps({"redeem_request_id": rid}).encode()
    req = urllib.request.Request(
        WHAM_CONSUME, data=body, method="POST", headers=headers(tok, acct, post=True)
    )
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:800]


def fmt(j):
    rl = j.get("rate_limit") or {}
    p = rl.get("primary_window") or {}
    s = rl.get("secondary_window") or {}
    c = (j.get("rate_limit_reset_credits") or {}).get("available_count")
    # 子配额 (additional_rate_limits[]) — banked redeem 不动这里。
    # 主 vs 子区分见 memory feedback_chatgpt_usage_main_vs_addl_rate_limits。
    addl = []
    for entry in (j.get("additional_rate_limits") or []):
        a_rl = entry.get("rate_limit") or {}
        a_p = a_rl.get("primary_window") or {}
        a_s = a_rl.get("secondary_window") or {}
        addl.append({
            "name": entry.get("limit_name"),
            "5h": a_p.get("used_percent"),
            "7d": a_s.get("used_percent"),
            "7d_reset_at": a_s.get("reset_at"),
        })
    return {
        "email": j.get("email"),
        "plan": j.get("plan_type"),
        # main quota — banked redeem 清这两个
        "5h": p.get("used_percent"),
        "7d": s.get("used_percent"),
        "allowed": rl.get("allowed"),
        "credits": c,
        "5h_reset_at": p.get("reset_at"),
        "7d_reset_at": s.get("reset_at"),
        # additional rate limits — banked redeem 不动
        "addl": addl,
    }


def main():
    if len(sys.argv) < 2:
        sys.stderr.write(USAGE)
        sys.exit(2)
    mode = sys.argv[1]
    force = "--force" in sys.argv[2:]
    tok, acct = load_auth()

    if mode == "probe":
        try:
            j = usage(tok, acct)
        except urllib.error.HTTPError as e:
            print(f"HTTP_{e.code} {e.read().decode()[:200]}")
            sys.exit(3)
        print(json.dumps(fmt(j)))
        return

    if mode == "probe-json":
        j = usage(tok, acct)
        print(json.dumps(j, indent=2))
        return

    if mode == "redeem":
        try:
            before = usage(tok, acct)
        except urllib.error.HTTPError as e:
            print(f"PROBE_HTTP_{e.code}")
            sys.exit(3)
        b = fmt(before)
        print(f"BEFORE {json.dumps(b)}")
        credits = (before.get("rate_limit_reset_credits") or {}).get("available_count", 0) or 0
        if credits < 1 and not force:
            print("SKIP no_credit")
            return
        rid = str(uuid.uuid4())
        code, resp = consume(tok, acct, rid)
        if isinstance(resp, dict):
            r_code = resp.get("code")
            r_win = resp.get("windows_reset")
        else:
            r_code = "?"; r_win = "?"
        print(f"CONSUME HTTP={code} code={r_code} windows_reset={r_win} rid={rid}")
        time.sleep(2)
        try:
            after = usage(tok, acct)
            print(f"AFTER  {json.dumps(fmt(after))}")
        except urllib.error.HTTPError as e:
            print(f"AFTER_HTTP_{e.code}")
        return

    sys.stderr.write(USAGE)
    sys.exit(2)


if __name__ == "__main__":
    main()
