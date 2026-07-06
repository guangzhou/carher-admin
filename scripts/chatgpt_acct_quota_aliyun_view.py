#!/usr/bin/env python3
"""Render the aliyun (carher ns) ChatGPT acct pool table.

不同于 198 prod：
  - 阿里云没有 quota-rebalance state.json (tier/paused/restore 不可得)
  - 只跑 gpt-5.5 一档 (无 5.4 / 5.3-codex 独立池子)
数据源：
  1. kubectl -n carher get pod -l pool=chatgpt-acct  → pod readiness / restarts / age
  2. kubectl -n carher exec <pod> -- cat /chatgpt-auth/auth.json
       → email / expires_at / plan_type / subscription_active_until
  3. kubectl -n carher exec <pod> -- python3 <probe>
       → 上游 /codex/usage 拿 5h%/7d% 真实用量（pod 内出 CF, 带 ChatGPT-Account-ID
         + Originator codex_cli_rs 头, 阿里云 SG IP 也通)
  4. kubectl -n carher exec litellm-db-0 -- psql  → LiteLLM_SpendLogs 5h / 24h
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

USAGE_PROBE_CODE = r'''
import json, urllib.request, urllib.error, sys
try:
    with open("/chatgpt-auth/auth.json") as f:
        a = json.load(f)
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/usage",
        headers={
            "Authorization": "Bearer " + a["access_token"],
            "ChatGPT-Account-ID": a.get("account_id", ""),
            "Originator": "codex_cli_rs",
            "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as e:
    body = e.read().decode(errors="ignore")[:400]
    sys.stdout.write(json.dumps({"_err": e.code, "_body": body}))
except Exception as e:
    sys.stdout.write(json.dumps({"_err": -1, "_body": str(e)[:200]}))
'''


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


def claims_from_id_token(auth: dict[str, Any]) -> dict[str, Any]:
    token = auth.get("id_token") or ""
    if token.count(".") < 2:
        return {}
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment))
    except Exception:
        return {}


def email_from_auth(auth: dict[str, Any]) -> str:
    return claims_from_id_token(auth).get("email") or ""


def subscription_info(auth: dict[str, Any]) -> tuple[str, float | None]:
    """Extract (plan_type, subscription_active_until_epoch) from id_token claims."""
    claims = claims_from_id_token(auth)
    oai = claims.get("https://api.openai.com/auth") or {}
    plan = oai.get("chatgpt_plan_type") or ""
    until_raw = oai.get("chatgpt_subscription_active_until")
    until_ts: float | None = None
    if until_raw:
        try:
            until_ts = datetime.fromisoformat(
                str(until_raw).replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            until_ts = None
    return plan, until_ts


def probe_auth(pod: str) -> dict[str, Any]:
    """Return {email, expires_at, plan, sub_until, p5h, p7d, p_reset, w_reset, codex_5h, codex_7d}
    via kubectl exec.
    auth.json gives identity; codex/usage gives rate-limit windows.
    """
    info = {
        "email": "", "expires_at": None, "plan": "", "sub_until": None,
        "live_plan": None,
        "p5h": None, "p7d": None, "p_reset": None, "w_reset": None,
        "codex_5h": None, "codex_7d": None,
        "probe_err": None,
    }
    # 读 auth.json 拿身份。并发 kubectl exec 走 jms 隧道时偶发单个 exec
    # 超时/reset，必须重试；彻底失败要标 probe_err（否则 p5h/p7d 为 None
    # 会 fall through 到 ONLINE，把打满的号误判成可接单）。
    auth_exc = ""
    for attempt in range(3):
        try:
            raw = subprocess.check_output(
                ["kubectl", "-n", NS, "exec", pod, "--", "cat", AUTH_PATH],
                text=True, timeout=20, stderr=subprocess.DEVNULL,
            )
            auth = json.loads(raw)
            plan, sub_until = subscription_info(auth)
            info.update({
                "email": email_from_auth(auth),
                "expires_at": auth.get("expires_at"),
                "plan": plan,
                "sub_until": sub_until,
            })
            break
        except Exception as e:
            auth_exc = type(e).__name__
            time.sleep(1.5 * (attempt + 1))
    else:
        info["probe_err"] = f"auth_read_fail:{auth_exc}"
        return info

    last_exc = ""
    for attempt in range(3):
        try:
            raw = subprocess.check_output(
                ["kubectl", "-n", NS, "exec", "-i", pod, "--", "python3", "-c",
                 USAGE_PROBE_CODE],
                text=True, timeout=25, stderr=subprocess.DEVNULL,
            )
            usage = json.loads(raw)
            if usage.get("_err") is not None:
                body = (usage.get("_body") or "")
                if "token_invalidated" in body or usage["_err"] == 401:
                    info["probe_err"] = "token_invalidated"
                    return info
                last_exc = f"HTTP {usage['_err']}"
                time.sleep(1.5 * (attempt + 1))
                continue
            rl = usage.get("rate_limit") or {}
            pw = rl.get("primary_window") or {}
            sw = rl.get("secondary_window") or {}
            # live plan_type：上游实时会员档，比 id_token 缓存的 plan 准
            # （订阅到期后 id_token 仍写 pro，但这里会回 free）。
            info["live_plan"] = usage.get("plan_type")
            info["p5h"] = pw.get("used_percent")
            info["p7d"] = sw.get("used_percent")
            info["p_reset"] = pw.get("reset_at")
            info["w_reset"] = sw.get("reset_at")
            for extra in usage.get("additional_rate_limits") or []:
                if "codex" in (extra.get("limit_name") or "").lower():
                    erl = extra.get("rate_limit") or {}
                    info["codex_5h"] = (erl.get("primary_window") or {}).get("used_percent")
                    info["codex_7d"] = (erl.get("secondary_window") or {}).get("used_percent")
                    break
            return info
        except Exception as e:
            last_exc = type(e).__name__
            time.sleep(1.5 * (attempt + 1))
    info["probe_err"] = f"probe_fail:{last_exc}"
    return info


def gather_auth(pods: list[dict[str, Any]]) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futs = {pool.submit(probe_auth, p["pod"]): p for p in pods}
        for fut in concurrent.futures.as_completed(futs):
            p = futs[fut]
            try:
                p.update(fut.result())
            except Exception as e:
                p.update({
                    "email": "", "expires_at": None, "plan": "", "sub_until": None,
                    "p5h": None, "p7d": None,
                    "probe_err": f"gather_fail:{type(e).__name__}",
                })


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


def fmt_expires(epoch: float | int | None, now: float) -> str:
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


def fmt_sub_until(epoch: float | int | None) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


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

    def status_of(p: dict[str, Any]) -> str:
        if not p.get("ready"):
            return "OFFLINE"
        if p.get("probe_err") == "token_invalidated":
            return "TOKEN_X"
        # fail-closed：探针出错，或没拿到任一窗口用量，就不能断言可接单。
        # 否则 p5h/p7d 为 None 会 fall through 到 ONLINE，把打满/探测失败的号
        # 误判成 take=yes。
        if p.get("probe_err"):
            return "PROBE_ERR"
        if not isinstance(p.get("p5h"), (int, float)) and not isinstance(p.get("p7d"), (int, float)):
            return "PROBE_ERR"
        # 订阅已过期，或上游实时会员档掉成非 pro：不是有效 pro 号，不该接单。
        # sub_until 来自 id_token 缓存（过期后仍写 pro，靠 sub_until 揭穿）；
        # live_plan 来自实时 /codex/usage（掉档后直接回 free），二者任一命中即判死。
        if p.get("sub_until") and p["sub_until"] <= now:
            return "SUB_EXP"
        if p.get("live_plan") and p["live_plan"] != "pro":
            return "SUB_EXP"
        if isinstance(p.get("p7d"), (int, float)) and p["p7d"] >= 100:
            return "QUOTA"
        if isinstance(p.get("p5h"), (int, float)) and p["p5h"] >= 100:
            return "QUOTA"
        return "ONLINE"

    def take_of(p: dict[str, Any]) -> str:
        return "yes" if status_of(p) == "ONLINE" else "-"

    def cause_of(p: dict[str, Any]) -> str:
        causes = []
        if p.get("probe_err"):
            causes.append(p["probe_err"])
        if isinstance(p.get("expires_at"), int) and p["expires_at"] - now <= 0:
            causes.append("token_expired")
        elif isinstance(p.get("expires_at"), int) and p["expires_at"] - now < 3 * 86400:
            causes.append("token<3d")
        if p.get("sub_until") and p["sub_until"] <= now:
            causes.append("sub_expired")
        elif p.get("sub_until") and p["sub_until"] - now < 7 * 86400:
            causes.append("sub<7d")
        if p.get("live_plan") and p["live_plan"] != "pro":
            causes.append(f"live_{p['live_plan']}")
        if isinstance(p.get("p5h"), (int, float)) and p["p5h"] >= 100:
            causes.append("5h_full")
        if isinstance(p.get("p7d"), (int, float)) and p["p7d"] >= 100:
            causes.append("7d_full")
        s24 = spend_24h.get(p["acct"], {})
        if int(s24.get("calls") or 0) == 0:
            causes.append("idle_24h")
        if p.get("restarts", 0) > 0:
            causes.append(f"restarts={p['restarts']}")
        return ",".join(causes)

    print(
        f"{'acct':9s} {'email':32s} {'take':>4s} {'status':>8s} "
        f"{'5h%':>5s} {'5h_n':>6s} {'5h$':>7s} {'5h_reset':>9s} "
        f"{'7d%':>5s} {'7d_reset':>10s} "
        f"{'tok_left':>9s} {'sub_until':>10s} {'sub_left':>8s}  cause"
    )
    print("-" * 200)

    silent: list[str] = []
    expiring: list[str] = []
    sub_expiring: list[str] = []
    quota_high: list[str] = []
    total_5h_calls = total_5h_spend = 0.0
    total_24h_calls = total_24h_spend = 0.0

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

        if c24 == 0:
            silent.append(acct)
        if isinstance(p.get("expires_at"), int) and p["expires_at"] - now < 3 * 86400:
            expiring.append(acct)
        sub_until_ts = p.get("sub_until")
        if sub_until_ts and sub_until_ts - now < 7 * 86400:
            sub_expiring.append(acct)
        p5h = p.get("p5h"); p7d = p.get("p7d")
        if (isinstance(p5h, (int, float)) and p5h >= 90) or (isinstance(p7d, (int, float)) and p7d >= 90):
            quota_high.append(acct)

        def pct(v):
            return f"{int(v)}%" if isinstance(v, (int, float)) else "-"

        print(
            f"{acct:9s} {(p.get('email') or '-'):32s} "
            f"{take_of(p):>4s} {status_of(p):>8s} "
            f"{pct(p5h):>5s} "
            f"{(str(c5) if c5 else '-'):>6s} {(f'{v5:.1f}' if c5 else '-'):>7s} "
            f"{fmt_expires(p.get('p_reset'), now):>9s} "
            f"{pct(p7d):>5s} {fmt_expires(p.get('w_reset'), now):>10s} "
            f"{fmt_expires(p.get('expires_at'), now):>9s} "
            f"{fmt_sub_until(sub_until_ts):>10s} {fmt_expires(sub_until_ts, now):>8s}  "
            f"{cause_of(p)}"
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
    if sub_expiring:
        print(f"⚠ 订阅 <7d/已过期 ({len(sub_expiring)}): "
              f"{sorted(sub_expiring, key=acct_sort_key)}")
    if quota_high:
        print(f"⚠ quota ≥90% ({len(set(quota_high))}): "
              f"{sorted(set(quota_high), key=acct_sort_key)}")

    if not summary:
        return
    takers = [p["acct"] for p in pods if take_of(p) == "yes"]
    online = [p["acct"] for p in pods if status_of(p) == "ONLINE"]
    quota = [p["acct"] for p in pods if status_of(p) == "QUOTA"]
    token_x = [p["acct"] for p in pods if status_of(p) == "TOKEN_X"]
    sub_exp = [p["acct"] for p in pods if status_of(p) == "SUB_EXP"]
    offline = [p["acct"] for p in pods if status_of(p) == "OFFLINE"]
    probe_err = [p["acct"] for p in pods if status_of(p) == "PROBE_ERR"]
    sort = lambda rows: sorted(rows, key=acct_sort_key)
    print()
    print(f"take    ={len(takers):2d}  {sort(takers)}")
    print(f"online  ={len(online):2d}  {sort(online)}")
    print(f"quota   ={len(quota):2d}  {sort(quota)} (5h/7d 撞顶)")
    print(f"sub_exp ={len(sub_exp):2d}  {sort(sub_exp)} (订阅过期/非pro, 需续订或摘除)")
    print(f"token_x ={len(token_x):2d}  {sort(token_x)} (token_invalidated, 走 re-OAuth)")
    print(f"probe_err={len(probe_err):2d}  {sort(probe_err)} (探针失败, 状态未知不计入 take)")
    print(f"offline ={len(offline):2d}  {sort(offline)} (pod not ready)")


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
