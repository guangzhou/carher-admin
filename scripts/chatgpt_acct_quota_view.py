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


def past_label(row: dict[str, Any], window: str) -> str:
    """裸 'past' 拆三类后缀，让用户一眼读出来根因：
      past⊘ = state 冻结（manual_offline TOKEN 死 / deploy.scale=0），probe 不再写
      past· = 该窗口自然过但被对侧窗口卡 paused（5h 过但 wk=100%，或反之）
      past! = ONLINE 但 probe stale，cron 下一 tick 会刷新
    """
    if row.get("manual_offline"):
        return "past⊘"
    cause = (row.get("cause") or "")
    if cause == "deploy.spec.replicas=0":
        return "past⊘"
    if row.get("paused"):
        # paused but not manual_offline → 7d/5h 自然 cap
        # window=='5h' & cause 主要是 wk=100% → 5h 列 past 是配对窗口的副作用
        # window=='7d' & cause 主要是 5h=100% → 反之
        if window == "5h" and "wk=" in cause:
            return "past·"
        if window == "7d" and "5h=" in cause:
            return "past·"
        return "past·"
    return "past!"


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


def is_zombie(row: dict[str, Any]) -> bool:
    """A row that has only bookkeeping fields (ts / consecutive_probe_err / probe_err_alerted)
    and never carried real quota probe data is a zombie — typically a deploy
    scaled to 0 + router entries cleared but the state.json line not pruned.
    Surfaces as ZOMBIE so it doesn't get counted as ONLINE."""
    if row.get("manual_offline") or row.get("paused"):
        return False
    has_probe = any(
        row.get(k) is not None
        for k in (
            "primary_pct",
            "weekly_pct",
            "tier",
            "primary_reset_at",
            "weekly_reset_at",
            "subscription_active_until",
            "plan",
        )
    )
    return not has_probe


def status(row: dict[str, Any]) -> str:
    if row.get("manual_offline"):
        return "OFFLINE"
    if row.get("paused"):
        return "PAUSED"
    if is_zombie(row):
        return "ZOMBIE"
    return "ONLINE"


def take(row: dict[str, Any]) -> str:
    if row.get("manual_offline") or row.get("paused") or is_zombie(row):
        return "-"
    return "yes"


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
    ts = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = len(state)
    print(f"=== BEGIN chatgpt-acct-quota @ {ts} | source=198:state.json | rows={total} ===")
    print("legend: past⊘=state 冻结 (TOKEN/scale=0)  past·=另一窗口卡 paused  past!=ONLINE probe stale")
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
        # SCALED_DOWN: deploy.replicas=0 时 rebalance preflight 短路不 probe，
        # primary_pct / weekly_pct 是 pause 时刻的快照（不再变化但有意义——能反推
        # 当时是 5h 满还是 wk 满才被自动 scale=0）。reset 时刻是死数据，渲染。
        # main/codex 流量列 mute——pod=0 无新流量；SpendLogs 5h 残留可能误导。
        is_scaled_down = str(row.get("tier") or "").upper() == "SCALED_DOWN"
        if is_scaled_down:
            main_n_cell = main_s_cell = codex_n_cell = codex_s_cell = "-"
            pct_cell = str(row.get("primary_pct") or "-")
            w_pct_cell = str(row.get("weekly_pct") or "-")
            p_reset_cell = duration(row.get("primary_reset_at"), now)
            w_reset_cell = duration(row.get("weekly_reset_at"), now)
            if p_reset_cell == "past":
                p_reset_cell = past_label(row, "5h")
            if w_reset_cell == "past":
                w_reset_cell = past_label(row, "7d")
            next_reset_cell = next_reset(row, now)
        else:
            pct_cell = str(row.get("primary_pct", ""))
            # 上游 probe 0% 但 LiteLLM main pool ≥50 calls → 上游 usage 落后 / probe stale
            if main_calls and main_calls >= 50 and p_pct < 5:
                pct_cell = f"{pct_cell}*"
            main_n_cell = f"{main_calls}" if main_calls else "-"
            main_s_cell = f"{main_spend:.1f}" if main_spend is not None else "-"
            codex_n_cell = f"{codex_calls}" if codex_calls else "-"
            codex_s_cell = f"{codex_spend:.1f}" if codex_spend is not None else "-"
            p_reset_cell = duration(row.get("primary_reset_at"), now)
            w_reset_cell = duration(row.get("weekly_reset_at"), now)
            if p_reset_cell == "past":
                p_reset_cell = past_label(row, "5h")
            if w_reset_cell == "past":
                w_reset_cell = past_label(row, "7d")
            next_reset_cell = next_reset(row, now)
            w_pct_cell = str(row.get("weekly_pct", ""))
        # cause 列：SCALED_DOWN 时优先显 state.cause（2026-06-29 后 cron preflight 保留首因
        # OFFLINE-5H/OFFLINE-WEEK；老脏数据兜底 'deploy.spec.replicas=0' → 直接渲染原值）。
        # 之前的 pct 反推逻辑（pct≥95 → pause 触发；pct<95 → manual scale=0）已删 ——
        # 真因写入后反推 stale state pct 既不准也误导（多维数据压一维标签）。
        cause = row.get("cause", "")
        restore_cell = duration(row.get('restore_at'), now, days=False)
        if restore_cell == "past":
            restore_cell = past_label(row, "restore")
        print(
            f"{acct:9s} {emails.get(acct, '-'):32s} {take(row):>4s} {status(row):>7s} "
            f"{str(row.get('tier', '-')):>16s} {pct_cell:>5s} "
            f"{main_n_cell:>7s} {main_s_cell:>7s} {codex_n_cell:>8s} {codex_s_cell:>7s} "
            f"{p_reset_cell:>12s} {w_pct_cell:>5s} "
            f"{w_reset_cell:>12s} {next_reset_cell:>12s} "
            f"{restore_cell:>9s} "
            f"{sub_until(row.get('subscription_active_until')):>20s} "
            f"{sub_left(row.get('subscription_active_until'), now):>8s}  {cause}"
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

    zombies = [acct for acct, row in state.items() if status(row) == "ZOMBIE"]
    token_bad = [acct for acct, row in state.items()
                 if str(row.get("tier") or "").upper() == "TOKEN_INVALID"]
    sub_expired = [acct for acct, row in state.items()
                   if sub_left(row.get("subscription_active_until"), now) == "expired"]
    if zombies or token_bad or sub_expired:
        print()
        print("✗ 不健康账号汇总（需人工处置）")
        if zombies:
            print(f"  ZOMBIE         ({len(zombies)}): {sorted(zombies, key=acct_sort_key)} "
                  f"— state 残留无 probe 数据；deploy scale=0 + router 已清；建议清行 + 删 deploy")
        if token_bad:
            print(f"  TOKEN_INVALID  ({len(token_bad)}): {sorted(token_bad, key=acct_sort_key)} "
                  f"— 走 quota_rebalance_manual_offline_transient_401 三步：验 token → reset state → 重注册 entry")
        if sub_expired:
            print(f"  SUB_EXPIRED    ({len(sub_expired)}): {sorted(sub_expired, key=acct_sort_key)} "
                  f"— sub_until 已过；订阅周期结束（27d reset 是周期残留），需续订或删除")

    if not summary:
        return
    takers = [acct for acct, row in state.items() if take(row) == "yes"]
    online = [acct for acct, row in state.items() if status(row) == "ONLINE"]
    paused = [acct for acct, row in state.items() if status(row) == "PAUSED"]
    offline = [acct for acct, row in state.items() if status(row) == "OFFLINE"]
    zombie = [acct for acct, row in state.items() if status(row) == "ZOMBIE"]
    sort = lambda rows: sorted(rows, key=acct_sort_key)
    print()
    print(f"take    ={len(takers):2d}  {sort(takers)}")
    print(f"online  ={len(online):2d}  {sort(online)}")
    print(f"paused  ={len(paused):2d}  {sort(paused)} (5h/7d quota pause)")
    print(f"offline ={len(offline):2d}  {sort(offline)} (manual_offline)")
    if zombie:
        print(f"zombie  ={len(zombie):2d}  {sort(zombie)} (state placeholder; no probe data — likely deploy scale=0 + router cleared)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    state = load_state()
    render_table(state, summary=args.summary)
    print(f"=== END chatgpt-acct-quota | rows={len(state)} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
