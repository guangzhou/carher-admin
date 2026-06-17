#!/usr/bin/env python3
"""Render the aliyun (carher ns) ChatGPT acct pool table.

不同于 198 prod：
  - 阿里云没有 quota-rebalance state.json (5h%/7d%/tier/paused/restore 都不可得)
  - 阿里云 SG IP 直调 chatgpt.com/backend-api/codex/usage 被 CF 403 拦
    (memory: aliyun-ip-blocked-chatgpt-web) → 不做 upstream usage 探针
数据源：
  1. kubectl -n carher get pod -l pool=chatgpt-acct  → pod readiness / restarts / age
  2. kubectl -n carher exec <pod> -- cat /chatgpt-auth/auth.json  → email + expires_at
  3. kubectl -n carher exec litellm-db-0 -- psql  → LiteLLM_SpendLogs 5h / 24h
model_id 形态: chatgpt-acct-N/chatgpt-gpt-5.5 (aliyun 只跑 5.5, 无 5.4/codex)
本脚本要求本地 kubectl 已通过 jms tunnel 连上 aliyun k8s (默认 127.0.0.1:16443);
wrapper chatgpt-acct-quota-aliyun.sh 会负责拉起 tunnel。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

NS = "carher"
POD_LABEL = "pool=chatgpt-acct"
DB_POD = "litellm-db-0"
DB_USER = "litellm"
DB_NAME = "litellm"
DB_PWD_ENV = "PGPASSWORD"
DB_PWD_DEFAULT = "nlacVBVCRgnjEEKZDK81Bw"
AUTH_PATH = "/chatgpt-auth/auth.json"


def kubectl(*args: str, timeout: int = 20) -> str:
    return subprocess.check_output(
        ["kubectl", "-n", NS, *args], text=True, timeout=timeout
    )


def list_pods() -> list[dict[str, Any]]:
    raw = kubectl("get", "pod", "-l", POD_LABEL, "-o", "json")
    items = json.loads(raw).get("items", [])
    pods = []
    for it in items:
        meta = it.get("metadata", {})
        status = it.get("status", {})
        labels = meta.get("labels", {}) or {}
        app = labels.get("app", "")
        if not app.startswith("chatgpt-acct-"):
            continue
        acct = app.replace("chatgpt-", "", 1)
        cs = (status.get("containerStatuses") or [{}])[0]
        pods.append({
            "acct": acct,
            "pod": meta.get("name", ""),
            "phase": status.get("phase"),
            "ready": cs.get("ready"),
            "restarts": cs.get("restartCount", 0),
            "started_at": meta.get("creationTimestamp"),
        })
    return pods


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


def probe_auth(pod: str) -> tuple[str, int | None]:
    """Return (email, expires_at_epoch) via kubectl exec cat auth.json."""
    try:
        raw = subprocess.check_output(
            ["kubectl", "-n", NS, "exec", pod, "--", "cat", AUTH_PATH],
            text=True,
            timeout=20,
            stderr=subprocess.DEVNULL,
        )
        auth = json.loads(raw)
        return email_from_auth(auth), auth.get("expires_at")
    except Exception:
        return "", None


def gather_auth(pods: list[dict[str, Any]]) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(probe_auth, p["pod"]): p for p in pods}
        for fut in concurrent.futures.as_completed(futs):
            p = futs[fut]
            try:
                email, exp = fut.result()
            except Exception:
                email, exp = "", None
            p["email"] = email
            p["expires_at"] = exp


def db_query(sql: str) -> str:
    pwd = os.environ.get(DB_PWD_ENV, DB_PWD_DEFAULT)
    return subprocess.check_output(
        [
            "kubectl", "-n", NS, "exec", "-i", DB_POD, "--",
            "bash", "-c",
            f"PGPASSWORD={pwd} psql -U {DB_USER} -d {DB_NAME} -A -F'|' -t -c \"{sql}\"",
        ],
        text=True, timeout=25,
    )


def spend_window(hours: int) -> dict[str, dict[str, float]]:
    sql = (
        "SELECT split_part(model_id, '/', 1) AS acct, "
        "COUNT(*) AS n, ROUND(SUM(spend)::numeric, 2) AS spend "
        "FROM \\\"LiteLLM_SpendLogs\\\" "
        f"WHERE model_id LIKE 'chatgpt-acct-%/%' "
        f"AND \\\"startTime\\\" > NOW() - INTERVAL '{hours} hours' "
        "GROUP BY acct;"
    )
    try:
        raw = db_query(sql)
    except Exception as e:
        print(f"# WARN: spend_window({hours}h) failed: {e}", file=sys.stderr)
        return {}
    out: dict[str, dict[str, float]] = {}
    for line in raw.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        acct = parts[0].replace("chatgpt-", "", 1)
        try:
            out[acct] = {"calls": int(parts[1]), "spend": float(parts[2])}
        except ValueError:
            continue
    return out


def acct_sort_key(acct: str) -> int:
    try:
        return int(acct.split("-", 1)[1])
    except Exception:
        return 10**9


def fmt_age(iso: str | None, now: float) -> str:
    if not iso:
        return "-"
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return "-"
    seconds = int(now - ts)
    if seconds < 0:
        return "future"
    day, rem = divmod(seconds, 86400)
    hour = rem // 3600
    if day:
        return f"{day}d{hour:02d}h"
    minute = (rem % 3600) // 60
    return f"{hour}h{minute:02d}m"


def fmt_expires(epoch: int | None, now: float) -> str:
    if not epoch:
        return "-"
    seconds = int(epoch - now)
    if seconds <= 0:
        return "expired"
    day, rem = divmod(seconds, 86400)
    hour = rem // 3600
    if day:
        return f"{day}d{hour:02d}h"
    return f"{hour}h{(rem % 3600) // 60:02d}m"


def render(*, summary: bool, as_json: bool) -> None:
    now = time.time()
    pods = list_pods()
    if not pods:
        print("# no chatgpt-acct pods found in ns=carher (tunnel up?)", file=sys.stderr)
        sys.exit(1)
    gather_auth(pods)

    spend_5h = spend_window(5)
    spend_24h = spend_window(24)

    if as_json:
        out = []
        for p in sorted(pods, key=lambda x: acct_sort_key(x["acct"])):
            acct = p["acct"]
            row = {
                **p,
                "spend_5h": spend_5h.get(acct, {}),
                "spend_24h": spend_24h.get(acct, {}),
            }
            out.append(row)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print(
        f"{'acct':12s} {'pod':50s} {'email':32s} {'ready':>5s} {'rst':>3s} "
        f"{'age':>7s} {'tok_left':>9s} "
        f"{'5h_n':>6s} {'5h$':>7s} {'24h_n':>7s} {'24h$':>8s}  notes"
    )
    print("-" * 180)

    total_5h_calls = total_5h_spend = 0.0
    total_24h_calls = total_24h_spend = 0.0
    silent: list[str] = []
    expiring: list[str] = []
    for p in sorted(pods, key=lambda x: acct_sort_key(x["acct"])):
        acct = p["acct"]
        s5 = spend_5h.get(acct, {})
        s24 = spend_24h.get(acct, {})
        c5 = int(s5.get("calls") or 0)
        v5 = float(s5.get("spend") or 0.0)
        c24 = int(s24.get("calls") or 0)
        v24 = float(s24.get("spend") or 0.0)
        total_5h_calls += c5; total_5h_spend += v5
        total_24h_calls += c24; total_24h_spend += v24

        notes = []
        if not p.get("ready"):
            notes.append("POD_NOT_READY")
        if p.get("restarts", 0) > 0:
            notes.append(f"restarts={p['restarts']}")
        if c24 == 0:
            notes.append("idle_24h")
            silent.append(acct)
        elif c5 == 0 and c24 > 0:
            notes.append("idle_5h")
        if c24 > 0 and v24 == 0:
            notes.append("price$0?")
        tok_left = fmt_expires(p.get("expires_at"), now)
        if isinstance(p.get("expires_at"), int) and p["expires_at"] - now < 3 * 86400:
            notes.append("token_soon")
            expiring.append(acct)

        print(
            f"{acct:12s} {p['pod']:50s} {(p.get('email') or '-'):32s} "
            f"{('Y' if p.get('ready') else 'N'):>5s} "
            f"{str(p.get('restarts', 0)):>3s} {fmt_age(p.get('started_at'), now):>7s} "
            f"{tok_left:>9s} "
            f"{(str(c5) if c5 else '-'):>6s} {(f'{v5:.1f}' if c5 else '-'):>7s} "
            f"{(str(c24) if c24 else '-'):>7s} {(f'{v24:.1f}' if c24 else '-'):>8s}  "
            f"{','.join(notes)}"
        )

    print()
    print(
        f"Σ 5h:  {int(total_5h_calls)} calls / ${total_5h_spend:.2f}    "
        f"24h: {int(total_24h_calls)} calls / ${total_24h_spend:.2f}    "
        f"pods: {sum(1 for p in pods if p.get('ready'))}/{len(pods)} ready"
    )
    if silent:
        print(f"⚠ idle 24h ({len(silent)}): {sorted(silent, key=acct_sort_key)}")
    if expiring:
        print(f"⚠ token <3d ({len(expiring)}): {sorted(expiring, key=acct_sort_key)}")

    if not summary:
        return
    ready = [p["acct"] for p in pods if p.get("ready")]
    not_ready = [p["acct"] for p in pods if not p.get("ready")]
    print()
    print(f"ready    ={len(ready):2d}  {sorted(ready, key=acct_sort_key)}")
    print(f"not_ready={len(not_ready):2d}  {sorted(not_ready, key=acct_sort_key)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", action="store_true",
                        help="附加 ready/not_ready 分组")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="原样输出 JSON (pod + auth + 5h/24h spend)")
    args = parser.parse_args()
    render(summary=args.summary, as_json=args.as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
