#!/usr/bin/env python3
"""
chatgpt-acct-status.py — 一键查看所有 ChatGPT Pro 账号的流量分布 + Codex 配额消耗

在 188 上运行：
  python3 /tmp/chatgpt-acct-status.py

输出三张表：
  1. 流量分布（近 2h / 近 30min docker 请求数）
  2. Codex 配额（/codex/usage: 5h% / 周%）
  3. 综合状态汇总
"""

import json, base64, subprocess, urllib.request, urllib.error, sys, os
from datetime import datetime
from pathlib import Path

ACCOUNTS = [
    ("acct-1",  "litellm-chatgpt",    4001),
    ("acct-2",  "litellm-chatgpt-2",  4002),
    ("acct-3",  "litellm-chatgpt-3",  4003),
    ("acct-4",  "litellm-chatgpt-4",  4004),
    ("acct-5",  "litellm-chatgpt-5",  4005),
    ("acct-6",  "litellm-chatgpt-6",  4006),
    ("acct-7",  "litellm-chatgpt-7",  4007),
    ("acct-8",  "litellm-chatgpt-8",  4008),
    ("acct-9",  "litellm-chatgpt-9",  4009),
    ("acct-10", "litellm-chatgpt-10", 4010),
    ("acct-11", "litellm-chatgpt-11", 4011),
]
ACCOUNTS_DIR = "/Data/chatgpt-auth"

# ── 1. 流量分布 ────────────────────────────────────────────────────────────────

def docker_reqs(container, window):
    r = subprocess.run(["docker", "logs", container, "--since", window],
                       capture_output=True, text=True)
    logs = r.stdout + r.stderr
    return (logs.count('POST /responses HTTP/1.1" 200') +
            logs.count('POST /v1/chat/completions HTTP/1.1" 200') +
            logs.count('POST /chat/completions HTTP/1.1" 200'))

def container_running(container):
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container],
                       capture_output=True, text=True)
    return r.stdout.strip() == "true"

print("\n" + "=" * 65)
print("  ChatGPT Pro 账号状态总览  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 65)

# 收集流量数据
traffic = {}
for acct, container, port in ACCOUNTS:
    running = container_running(container)
    r2h  = docker_reqs(container, "2h")  if running else 0
    r30m = docker_reqs(container, "30m") if running else 0
    traffic[acct] = {"running": running, "2h": r2h, "30m": r30m}

total_2h  = sum(v["2h"]  for v in traffic.values()) or 1
total_30m = sum(v["30m"] for v in traffic.values()) or 1

print(f"\n── 流量分布 ──────────────────────────────────────────────")
print(f"{'acct':<8} {'状态':<6} {'2h':>5} {'占比':>6}  {'30m':>5} {'占比':>6}")
print("-" * 50)
for acct, container, port in ACCOUNTS:
    t = traffic[acct]
    status = "UP  " if t["running"] else "DOWN"
    r2h, r30m = t["2h"], t["30m"]
    print(f"{acct:<8} {status:<6} {r2h:>5} {r2h/total_2h*100:>5.1f}%  {r30m:>5} {r30m/total_30m*100:>5.1f}%")
print(f"{'total':<8} {'':6} {sum(v['2h'] for v in traffic.values()):>5}          {sum(v['30m'] for v in traffic.values()):>5}")

# ── 2. Codex 配额消耗 ──────────────────────────────────────────────────────────

def load_auth(acct):
    path = Path(ACCOUNTS_DIR) / acct / "auth.json"
    if not path.exists():
        return None, None
    try:
        auth = json.loads(path.read_text())
        tok = auth["access_token"]
        aid = auth.get("account_id", "")
        if not aid:
            raw = tok.split(".")[1]
            raw += "=" * (-len(raw) % 4)
            claims = json.loads(base64.urlsafe_b64decode(raw))
            aid = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
        return tok, aid
    except Exception as e:
        return None, str(e)

def get_usage(tok, aid):
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/usage",
        headers={
            "Authorization": f"Bearer {tok}",
            "chatgpt-account-id": aid,
            "Originator": "codex_cli_rs",
            "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)",
        }
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:120]
        return None, f"HTTP {e.code}: {body}"
    except Exception as e:
        return None, str(e)

def classify(rl):
    p = rl["primary_window"]["used_percent"]
    w = rl["secondary_window"]["used_percent"]
    if p >= 95: return "OFFLINE-5H", p, w
    if p >= 85: return "THROTTLE",   p, w
    if w >= 75: return "THROTTLE",   p, w
    if p >= 60 or w >= 50: return "SLOW", p, w
    return "HEALTHY", p, w

print(f"\n── Codex 配额（/codex/usage）─────────────────────────────")
print(f"{'acct':<8} {'plan':<6} {'5h%':>5} {'week%':>6} {'tier':<12} {'说明'}")
print("-" * 60)

quota = {}
for acct, container, port in ACCOUNTS:
    tok, aid = load_auth(acct)
    if not tok:
        quota[acct] = {"tier": "NO-AUTH", "p": 0, "w": 0, "err": aid}
        print(f"{acct:<8} {'?':<6} {'—':>5} {'—':>6} {'NO-AUTH':<12} {aid or '无 auth.json'}")
        continue

    usage, err = get_usage(tok, aid)
    if err:
        quota[acct] = {"tier": "ERROR", "p": 0, "w": 0, "err": err}
        # 判断是 token 失效还是其他错误
        label = "token_invalidated" if "401" in str(err) or "invalid" in err.lower() else err[:40]
        print(f"{acct:<8} {'?':<6} {'—':>5} {'—':>6} {'TOKEN-ERR':<12} {label}")
        continue

    plan = usage.get("plan_type", "?")
    rl = usage.get("rate_limit", {})
    if not rl:
        quota[acct] = {"tier": "NO-RL", "p": 0, "w": 0}
        print(f"{acct:<8} {plan:<6} {'—':>5} {'—':>6} {'NO-RL':<12}")
        continue

    tier, p, w = classify(rl)
    quota[acct] = {"tier": tier, "p": p, "w": w}
    tier_icon = {"HEALTHY": "🟢", "SLOW": "🟡", "THROTTLE": "🟠", "OFFLINE-5H": "🔴"}.get(tier, "⚪")
    print(f"{acct:<8} {plan:<6} {p:>4.1f}% {w:>5.1f}% {tier_icon} {tier:<10}")

# ── 3. 综合汇总 ────────────────────────────────────────────────────────────────

print(f"\n── 综合状态 ───────────────────────────────────────────────")
print(f"{'acct':<8} {'容器':>4} {'流量(2h)':>8} {'5h%':>5} {'week%':>6}  状态")
print("-" * 58)
for acct, container, port in ACCOUNTS:
    t = traffic[acct]
    q = quota.get(acct, {})
    running_icon = "✅" if t["running"] else "❌"
    r2h = t["2h"]
    p   = q.get("p", 0)
    w   = q.get("w", 0)
    tier = q.get("tier", "?")
    tier_icon = {"HEALTHY": "🟢", "SLOW": "🟡", "THROTTLE": "🟠",
                 "OFFLINE-5H": "🔴", "TOKEN-ERR": "🔴", "NO-AUTH": "⚫", "ERROR": "🔴"}.get(tier, "⚪")
    print(f"{acct:<8} {running_icon}  {r2h:>8}  {p:>4.1f}% {w:>5.1f}%  {tier_icon} {tier}")

print()
