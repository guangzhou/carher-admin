#!/usr/bin/env python3
"""Render the 198 ChatGPT acct quota state as the canonical ops table.

Runs on JSZX-AI-03. Reads the quota-rebalance state file locally and resolves
emails from readable local creds plus 198 K3s pod auth claims.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILE = Path("/home/cltx/.chatgpt-quota/state/state.json")
LOCAL_AUTH_DIR = Path("/Data/chatgpt-auth")
K8S_198_HOST = "10.68.13.198"
K8S_198_USER = "cltx"
K8S_NS = "litellm-product"


def load_state() -> dict[str, dict[str, Any]]:
    return json.loads(STATE_FILE.read_text())


def duration(epoch: Any, now: float, *, days: bool = True) -> str:
    if not epoch:
        return "-"
    try:
        seconds = int(float(epoch) - now)
    except (TypeError, ValueError):
        return "-"
    if seconds <= 0:
        return "past"
    day, rem = divmod(seconds, 86400)
    hour, rem = divmod(rem, 3600)
    minute = rem // 60
    if days and day:
        return f"{day}d{hour:02d}h"
    return f"{day * 24 + hour}h{minute:02d}m"


def next_reset(row: dict[str, Any], now: float) -> str:
    candidates = []
    for key in ("primary_reset_at", "weekly_reset_at"):
        ts = row.get(key)
        if ts and ts > now:
            candidates.append(ts)
    return duration(min(candidates), now) if candidates else "-"


def parse_subscription_until(value: Any) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def sub_until(value: Any) -> str:
    ts = parse_subscription_until(value)
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def sub_left(value: Any, now: float) -> str:
    ts = parse_subscription_until(value)
    if not ts:
        return "-"
    seconds = int(ts - now)
    if seconds < 0:
        return "expired"
    return f"{seconds // 86400}d"


def status(row: dict[str, Any]) -> str:
    if row.get("manual_offline"):
        return "OFFLINE"
    if row.get("paused"):
        return "PAUSED"
    return "ONLINE"


def take(row: dict[str, Any]) -> str:
    return "yes" if not row.get("manual_offline") and not row.get("paused") else "-"


def email_from_auth(auth: dict[str, Any]) -> str:
    token = auth.get("id_token") or ""
    if token.count(".") < 2:
        return ""
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(segment))
        return claims.get("email") or ""
    except Exception:
        return ""


def local_cred_emails() -> dict[str, str]:
    emails: dict[str, str] = {}
    for creds in LOCAL_AUTH_DIR.glob("acct-*/.creds"):
        try:
            for line in creds.read_text().splitlines():
                if line.startswith("email="):
                    emails[creds.parent.name] = line.split("=", 1)[1].strip()
        except Exception:
            continue
    return emails


def remote_198_email_probe_code() -> str:
    return r'''
import base64, json, os, subprocess
os.environ["KUBECONFIG"] = os.path.expanduser("~/.kube/config")

def email_from_auth(auth):
    token = auth.get("id_token") or ""
    if token.count(".") < 2:
        return ""
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(segment))
        return claims.get("email") or ""
    except Exception:
        return ""

pods = json.loads(subprocess.check_output(
    ["kubectl", "-n", "litellm-product", "get", "pod", "-o", "json"],
    text=True,
))
out = {}
for item in pods.get("items", []):
    labels = item.get("metadata", {}).get("labels", {}) or {}
    app = labels.get("app", "")
    if not app.startswith("chatgpt-acct-"):
        continue
    acct = app.replace("chatgpt-", "", 1)
    pod = item["metadata"]["name"]
    try:
        raw = subprocess.check_output(
            ["kubectl", "-n", "litellm-product", "exec", pod, "--", "cat", "/chatgpt-auth/auth.json"],
            text=True,
            timeout=8,
        )
        email = email_from_auth(json.loads(raw))
        if email:
            out[acct] = email
    except Exception:
        pass
print(json.dumps(out, ensure_ascii=False))
'''


def remote_198_emails() -> dict[str, str]:
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=8",
                "-o",
                "StrictHostKeyChecking=no",
                f"{K8S_198_USER}@{K8S_198_HOST}",
                "python3",
                "-",
            ],
            input=remote_198_email_probe_code(),
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


def remote_198_spend_5h_code() -> str:
    return r'''
import json, os, subprocess
os.environ["KUBECONFIG"] = os.path.expanduser("~/.kube/config")
sql = (
    "SELECT split_part(model_id, '-gpt-', 1) AS acct, "
    "CASE WHEN model_id LIKE '%gpt-5.3%' THEN 'codex' ELSE 'main' END AS bucket, "
    "COUNT(*) AS n, ROUND(SUM(spend)::numeric, 2) AS spend "
    "FROM \"LiteLLM_SpendLogs\" "
    "WHERE model_id LIKE 'chatgpt-acct-%-gpt-%' "
    "AND \"startTime\" > NOW() - INTERVAL '5 hours' "
    "GROUP BY acct, bucket;"
)
try:
    raw = subprocess.check_output(
        ["kubectl", "-n", "litellm-product", "exec", "litellm-db-0", "--",
         "psql", "-U", "litellm", "-d", "litellm", "-A", "-F|", "-t", "-c", sql],
        text=True, timeout=20,
    )
except Exception:
    print("{}")
else:
    out = {}
    for line in raw.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 4:
            continue
        acct = parts[0].replace("chatgpt-", "", 1)
        bucket = parts[1]
        try:
            calls = int(parts[2])
            spend = float(parts[3])
        except ValueError:
            continue
        out.setdefault(acct, {})[bucket] = {"calls": calls, "spend": spend}
    print(json.dumps(out))
'''


def remote_198_spend_5h() -> dict[str, dict[str, float]]:
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=8",
                "-o",
                "StrictHostKeyChecking=no",
                f"{K8S_198_USER}@{K8S_198_HOST}",
                "python3",
                "-",
            ],
            input=remote_198_spend_5h_code(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


def email_map() -> dict[str, str]:
    emails = local_cred_emails()
    emails.update(remote_198_emails())
    return emails


def acct_sort_key(acct: str) -> int:
    try:
        return int(acct.split("-", 1)[1])
    except Exception:
        return 10**9


def render_table(state: dict[str, dict[str, Any]], *, summary: bool) -> None:
    now = time.time()
    emails = email_map()
    spend_5h = remote_198_spend_5h()
    print(
        f"{'acct':9s} {'email':32s} {'take':>4s} {'status':>7s} {'tier':>16s} "
        f"{'5h%':>5s} {'main_n':>7s} {'main$':>7s} {'codex_n':>8s} {'codex$':>7s} {'5h_reset':>12s} "
        f"{'7d%':>5s} {'7d_reset':>12s} "
        f"{'next_reset':>12s} {'restore':>9s} {'sub_until':>20s} {'sub_left':>8s}  cause"
    )
    print("-" * 250)
    for acct in sorted(state, key=acct_sort_key):
        row = state[acct]
        buckets = spend_5h.get(acct) or {}
        main = buckets.get("main") or {}
        codex = buckets.get("codex") or {}
        main_calls = main.get("calls")
        main_spend = main.get("spend")
        codex_calls = codex.get("calls")
        codex_spend = codex.get("spend")
        try:
            p_pct = int(row.get("primary_pct") or 0)
        except (TypeError, ValueError):
            p_pct = 0
        pct_cell = str(row.get("primary_pct", ""))
        # 上游 probe 0% 但 LiteLLM main pool ≥50 calls → 上游 usage 落后 / probe stale
        if main_calls and main_calls >= 50 and p_pct < 5:
            pct_cell = f"{pct_cell}*"
        main_n_cell = f"{main_calls}" if main_calls else "-"
        main_s_cell = f"{main_spend:.1f}" if main_spend is not None else "-"
        codex_n_cell = f"{codex_calls}" if codex_calls else "-"
        codex_s_cell = f"{codex_spend:.1f}" if codex_spend is not None else "-"
        print(
            f"{acct:9s} {emails.get(acct, '-'):32s} {take(row):>4s} {status(row):>7s} "
            f"{str(row.get('tier', '-')):>16s} {pct_cell:>5s} "
            f"{main_n_cell:>7s} {main_s_cell:>7s} {codex_n_cell:>8s} {codex_s_cell:>7s} "
            f"{duration(row.get('primary_reset_at'), now):>12s} {str(row.get('weekly_pct', '')):>5s} "
            f"{duration(row.get('weekly_reset_at'), now):>12s} {next_reset(row, now):>12s} "
            f"{duration(row.get('restore_at'), now, days=False):>9s} "
            f"{sub_until(row.get('subscription_active_until')):>20s} "
            f"{sub_left(row.get('subscription_active_until'), now):>8s}  {row.get('cause', '')}"
        )

    stale = [
        acct
        for acct, row in state.items()
        if ((spend_5h.get(acct, {}).get("main") or {}).get("calls") or 0) >= 50
        and int(row.get("primary_pct") or 0) < 5
    ]
    if stale:
        print()
        print(f"⚠ probe-stale ({len(stale)}): 上游 5h%≈0 但 LiteLLM main pool 5h 流量≥50 calls → "
              f"{sorted(stale, key=acct_sort_key)}")

    codex_total_calls = sum(
        ((spend_5h.get(acct, {}).get("codex") or {}).get("calls") or 0)
        for acct in state
    )
    codex_total_spend = sum(
        ((spend_5h.get(acct, {}).get("codex") or {}).get("spend") or 0.0)
        for acct in state
    )
    if codex_total_calls:
        codex_active = [
            acct for acct in state
            if ((spend_5h.get(acct, {}).get("codex") or {}).get("calls") or 0) > 0
        ]
        print()
        print(f"ⓘ codex (gpt-5.3) 独立配额池 5h: "
              f"{codex_total_calls} calls / ${codex_total_spend:.1f}, "
              f"active={len(codex_active)} {sorted(codex_active, key=acct_sort_key)}")

    if not summary:
        return
    takers = [acct for acct, row in state.items() if take(row) == "yes"]
    online = [acct for acct, row in state.items() if status(row) == "ONLINE"]
    paused = [acct for acct, row in state.items() if status(row) == "PAUSED"]
    offline = [acct for acct, row in state.items() if status(row) == "OFFLINE"]
    sort = lambda rows: sorted(rows, key=acct_sort_key)
    print()
    print(f"take    ={len(takers):2d}  {sort(takers)}")
    print(f"online  ={len(online):2d}  {sort(online)}")
    print(f"paused  ={len(paused):2d}  {sort(paused)} (5h/7d quota pause)")
    print(f"offline ={len(offline):2d}  {sort(offline)} (manual_offline)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    render_table(load_state(), summary=args.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
