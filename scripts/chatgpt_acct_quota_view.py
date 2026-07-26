#!/usr/bin/env python3
"""Render the 198 ChatGPT acct quota state as the canonical ops table.

Runs on JSZX-AI-03. Reads the quota-rebalance state file locally and resolves
emails from readable local creds plus 198 K3s pod auth claims.

2026-07-13: OpenAI 将 Pro 配额从双窗口 (5h primary + 7d secondary) 合并为
单 7d 窗口 (primary_window.limit_window_seconds=604800, secondary_window=null)。
state.json 中 primary_pct / primary_reset_at 现在代表 7d 窗口，weekly_pct 不再更新。
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
    """未来时长 → "1d02h" / "3h45m"；过去或无效 → "-"。
    reset/restore 本就该是未来时间；已过=数据无意义，统一 "-"。"""
    if not epoch:
        return "-"
    try:
        seconds = int(float(epoch) - now)
    except (TypeError, ValueError):
        return "-"
    if seconds <= 0:
        return "-"
    day, rem = divmod(seconds, 86400)
    hour, rem = divmod(rem, 3600)
    minute = rem // 60
    if days and day:
        return f"{day}d{hour:02d}h"
    return f"{day * 24 + hour}h{minute:02d}m"


def stale_notes(row: dict[str, Any], now: float) -> list[str]:
    """SCALED_DOWN / TOKEN_INVALID 等冻结态的诊断注释。"""
    tier = str(row.get("tier") or "").upper()
    notes: list[str] = []
    if tier == "SCALED_DOWN":
        p_reset = row.get("primary_reset_at")
        if p_reset and float(p_reset) < now:
            notes.append("7d_reset elapsed")
        revive_cooldown = row.get("revive_probe_cooldown_until")
        if revive_cooldown:
            cd = duration(revive_cooldown, now, days=False)
            if cd != "-":
                notes.append(f"REVIVE_PROBE cooldown {cd}")
            else:
                notes.append("REVIVE_PROBE_401")
        if row.get("revive_probe_still_cap"):
            notes.append("STILL_CAP")
        if row.get("revive_probe_error"):
            notes.append(f"ERROR: {row['revive_probe_error']}")
    elif tier == "TOKEN_INVALID":
        notes.append("需 re-OAuth")
    return notes


def next_reset(row: dict[str, Any], now: float) -> str:
    """primary_reset_at 倒计时（现在代表单 7d 窗口）。已过="-"。"""
    p_reset = row.get("primary_reset_at")
    if p_reset and float(p_reset) > now:
        return duration(p_reset, now)
    return "-"


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


def remote_198_spend_recent_code() -> str:
    return r'''
import json, os, subprocess
os.environ["KUBECONFIG"] = os.path.expanduser("~/.kube/config")
sql = (
    "SELECT split_part(model_id, '-gpt-', 1) AS acct, "
    "CASE WHEN model_id LIKE '%gpt-5.3%' THEN 'codex' ELSE 'main' END AS bucket, "
    "COUNT(*) AS n, ROUND(SUM(spend)::numeric, 2) AS spend "
    "FROM \"LiteLLM_SpendLogs\" "
    "WHERE model_id LIKE 'chatgpt-acct-%-gpt-%' "
    "AND \"startTime\" > NOW() - INTERVAL '24 hours' "
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


def remote_198_spend_recent() -> dict[str, dict[str, float]]:
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
            input=remote_198_spend_recent_code(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


def remote_198_zk_usage_code() -> str:
    """zerokey 累计消耗（全量 + 7d），按 acct 聚合。

    映射依据（2026-07-26 实测三重验证，勿改成猜的）：
      1. zero-N svc/deploy 编号集合 == acct 池编号集合（无差集）
      2. zk-N-* 的 api_base 恒为 http://zero-N.litellm-product.svc:8200（1:1）
      3. zero-N pod 内 /app/temp/users.json → chatgpt.acctN.username == "acctN"

    延迟用 request_duration_ms 而非 completionStartTime-startTime：后者不是首 token
    时刻。实测 endTime-completionStartTime 中位数 1ms、仅 1.8% 超 1s，即 LiteLLM 对
    responses 流式把 completionStartTime 记成了响应结束点（对照组 gpt-5.5 走真 API
    同样 99.4%，是通用记账行为不是 zerokey 特有）。DB 内不存在真 TTFT。
    """
    return r'''
import json, os, subprocess
os.environ["KUBECONFIG"] = os.path.expanduser("~/.kube/config")
sql = (
    "SELECT substring(model_id from '^zk-([0-9]+)-')::int AS n, "
    "COUNT(*) AS calls, ROUND(SUM(spend)::numeric,2) AS spend, "
    "ROUND(AVG(request_duration_ms)) AS lat_avg, "
    "ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY request_duration_ms)) AS lat_p95, "
    "COUNT(*) FILTER (WHERE completion_tokens=0) AS empty_n, "
    "COUNT(*) FILTER (WHERE \"startTime\" > NOW() - INTERVAL '7 days') AS calls7, "
    "ROUND(COALESCE(SUM(spend) FILTER "
    "(WHERE \"startTime\" > NOW() - INTERVAL '7 days'),0)::numeric,2) AS spend7, "
    "MAX(\"startTime\")::date AS last_seen "
    'FROM "LiteLLM_SpendLogs" '
    "WHERE model_id ~ '^zk-[0-9]+-' AND COALESCE(status,'success')='success' "
    "GROUP BY 1 ORDER BY 1;"
)
try:
    raw = subprocess.check_output(
        ["kubectl", "-n", "litellm-product", "exec", "litellm-db-0", "--",
         "psql", "-U", "litellm", "-d", "litellm", "-A", "-F|", "-t", "-c", sql],
        text=True, timeout=90,
    )
except Exception:
    print("{}")
else:
    # zero-N svc 存在性 —— 用于判孤儿路由（LiteLLM 注册着但后端服务已没了）
    try:
        svc_raw = subprocess.check_output(
            ["kubectl", "-n", "litellm-product", "get", "svc",
             "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"],
            text=True, timeout=20,
        )
        live = {s.strip()[len("zero-"):] for s in svc_raw.splitlines()
                if s.strip().startswith("zero-") and s.strip()[len("zero-"):].isdigit()}
    except Exception:
        live = None
    out = {}
    for line in raw.splitlines():
        p = line.strip().split("|")
        if len(p) < 9:
            continue
        try:
            num = int(p[0])
        except ValueError:
            continue
        def f(v, cast):
            v = v.strip()
            if not v:
                return None
            try:
                return cast(v)
            except ValueError:
                return None
        out["acct-%d" % num] = {
            "calls": f(p[1], int), "spend": f(p[2], float),
            "lat_avg_ms": f(p[3], float), "lat_p95_ms": f(p[4], float),
            "empty_n": f(p[5], int), "calls7": f(p[6], int),
            "spend7": f(p[7], float), "last_seen": p[8].strip() or None,
            "svc_live": (None if live is None else str(num) in live),
        }
    print(json.dumps(out))
'''


def remote_198_zk_usage() -> dict[str, dict[str, Any]]:
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
            input=remote_198_zk_usage_code(),
            capture_output=True,
            text=True,
            timeout=150,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


def ms_to_s(value: Any) -> float | None:
    """毫秒 → 秒（1 位小数）。表格里秒比毫秒可读。"""
    if value is None:
        return None
    try:
        return round(float(value) / 1000.0, 1)
    except (TypeError, ValueError):
        return None


# 空返告警阈值：≥50% 且 n≥10。低样本下 1-2 次空返是噪声，不是坏号
# （依据 zerokey 稳定空返=订阅失效；单次空返可能只是上游抖动）。
ZK_EMPTY_PCT_ALERT = 50
ZK_EMPTY_MIN_N = 10


def zk_empty_pct(usage: dict[str, Any]) -> int | None:
    calls = usage.get("calls") or 0
    if not calls:
        return None
    return round(100.0 * (usage.get("empty_n") or 0) / calls)


def email_map() -> dict[str, str]:
    emails = local_cred_emails()
    emails.update(remote_198_emails())
    return emails


def acct_sort_key(acct: str) -> int:
    try:
        return int(acct.split("-", 1)[1])
    except Exception:
        return 10**9


def build_rows(
    state: dict[str, dict[str, Any]],
    *,
    now: float,
    emails: dict[str, str],
    spend_recent: dict[str, dict[str, Any]],
    zk: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """state → 每 acct 一条结构化行（唯一事实源）。

    文本表和 --json 都消费这里的输出。历史上 to_lark.py 用固定宽度字符偏移
    (line[100:107]) 反解渲染后的文本，加一列就要重算全部偏移，且错位是静默的
    ——切出半个数字仍能 float()。所以数值留 raw、格式化留 cell，两条消费路径分开。
    """
    rows: list[dict[str, Any]] = []
    for acct in sorted(state, key=acct_sort_key):
        row = state[acct]
        buckets = spend_recent.get(acct) or {}
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
        # primary_pct 是 pause 时刻的快照（不再变化但有意义——能反推
        # 当时配额状态）。reset 时刻是死数据，渲染。
        # main/codex 流量列 mute——pod=0 无新流量；SpendLogs 残留可能误导。
        is_scaled_down = str(row.get("tier") or "").upper() == "SCALED_DOWN"
        if is_scaled_down:
            main_n_cell = main_s_cell = codex_n_cell = codex_s_cell = "-"
            pct_cell = str(row.get("primary_pct") or "-")
            main_calls = main_spend = codex_calls = codex_spend = None
        else:
            pct_cell = str(row.get("primary_pct", ""))
            if main_calls and main_calls >= 50 and p_pct < 5:
                pct_cell = f"{pct_cell}*"
            main_n_cell = f"{main_calls}" if main_calls else "-"
            main_s_cell = f"{main_spend:.1f}" if main_spend is not None else "-"
            codex_n_cell = f"{codex_calls}" if codex_calls else "-"
            codex_s_cell = f"{codex_spend:.1f}" if codex_spend is not None else "-"
        p_reset_cell = duration(row.get("primary_reset_at"), now)
        next_reset_cell = next_reset(row, now)
        # cause 列：SCALED_DOWN 时优先显 state.cause（2026-06-29 后 cron preflight 保留首因
        # OFFLINE 等；老脏数据兜底 'deploy.spec.replicas=0' → 直接渲染原值）。
        # 之前的 pct 反推逻辑（pct≥95 → pause 触发；pct<95 → manual scale=0）已删 ——
        # 真因写入后反推 stale state pct 既不准也误导（多维数据压一维标签）。
        cause = row.get("cause", "")
        notes = stale_notes(row, now)
        if notes:
            cause = f"{cause} [stale: {', '.join(notes)}]" if cause else f"[stale: {', '.join(notes)}]"
        tier_display = str(row.get("tier", "-"))
        if tier_display.upper() == "TOKEN_INVALID":
            tier_display = "401 需re-OAuth"
        # zerokey 累计消耗：与 7d 配额窗口不同尺度，全量累计看总投入、7d 看近期活跃。
        # 不受 SCALED_DOWN mute 影响 —— 累计消耗是历史事实，pod 停了也仍然成立。
        zu = zk.get(acct) or {}
        rows.append({
            "acct": acct,
            "email": emails.get(acct) or None,
            "take": take(row),
            "status": status(row),
            "tier": tier_display,
            "pct7d": to_num_or_none(pct_cell),
            "pct7d_cell": pct_cell,
            "reset": p_reset_cell,
            "main_n": main_calls,
            "main_spend": main_spend,
            "codex_n": codex_calls,
            "codex_spend": codex_spend,
            "main_n_cell": main_n_cell,
            "main_s_cell": main_s_cell,
            "codex_n_cell": codex_n_cell,
            "codex_s_cell": codex_s_cell,
            "next_reset": next_reset_cell,
            "restore": duration(row.get("restore_at"), now, days=False),
            "sub_until": sub_until(row.get("subscription_active_until")),
            "sub_left": sub_left(row.get("subscription_active_until"), now),
            "cause": cause or None,
            "zk_n": zu.get("calls"),
            "zk_spend": zu.get("spend"),
            "zk_n7": zu.get("calls7"),
            "zk_spend7": zu.get("spend7"),
            "zk_lat_avg": ms_to_s(zu.get("lat_avg_ms")),
            "zk_lat_p95": ms_to_s(zu.get("lat_p95_ms")),
            "zk_empty_n": zu.get("empty_n"),
            "zk_empty_pct": zk_empty_pct(zu) if zu else None,
            "zk_last_seen": zu.get("last_seen"),
            "zk_svc_live": zu.get("svc_live"),
        })
    return rows


def to_num_or_none(cell: str) -> int | None:
    """'6' → 6；'3*'（probe-stale 标记）→ 3；'-'/'' → None。"""
    cell = cell.rstrip("*").strip()
    if not cell or cell == "-":
        return None
    try:
        return int(cell)
    except ValueError:
        return None


def zk_diagnostics(
    rows: list[dict[str, Any]],
    zk: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """zerokey 三类异常：孤儿路由 + 稳定空返 + 未归属消耗。

    未归属是必报项：zk 聚合按 zk-N 分组，但表格行来自 state.json。state 里没有
    acct-N 的 zk 消耗会被静默丢掉（实测差额 8566 calls / ~$964，含 zk-25 这种
    svc 已删但仍在收流量的条目）。不报出来，表格合计就是个假数。
    """
    def orphan(r: dict[str, Any]) -> bool:
        return r["zk_svc_live"] is False and (r["zk_n7"] or 0) > 0

    orphans = [
        {"acct": r["acct"], "zk_n7": r["zk_n7"], "zk_n": r["zk_n"],
         "empty_pct": r["zk_empty_pct"], "last_seen": r["zk_last_seen"],
         "in_state": True}
        for r in rows if orphan(r)
    ]
    empties = [
        {"acct": r["acct"], "empty_pct": r["zk_empty_pct"],
         "empty_n": r["zk_empty_n"], "zk_n": r["zk_n"], "in_state": True}
        for r in rows
        if (r["zk_empty_pct"] is not None
            and r["zk_empty_pct"] >= ZK_EMPTY_PCT_ALERT
            and (r["zk_n"] or 0) >= ZK_EMPTY_MIN_N)
    ]

    unattributed: list[dict[str, Any]] = []
    if zk:
        known = {r["acct"] for r in rows}
        for acct, u in sorted(zk.items(), key=lambda kv: acct_sort_key(kv[0])):
            if acct in known:
                continue
            pct = zk_empty_pct(u)
            unattributed.append({
                "acct": acct, "zk_n": u.get("calls"), "zk_spend": u.get("spend"),
                "zk_n7": u.get("calls7"), "zk_spend7": u.get("spend7"),
                "empty_pct": pct, "last_seen": u.get("last_seen"),
                "svc_live": u.get("svc_live"), "in_state": False,
            })
            # state 外的号同样要参与孤儿/空返判定，否则漏报
            if u.get("svc_live") is False and (u.get("calls7") or 0) > 0:
                orphans.append({
                    "acct": acct, "zk_n7": u.get("calls7"), "zk_n": u.get("calls"),
                    "empty_pct": pct, "last_seen": u.get("last_seen"),
                    "in_state": False,
                })
            if (pct is not None and pct >= ZK_EMPTY_PCT_ALERT
                    and (u.get("calls") or 0) >= ZK_EMPTY_MIN_N):
                empties.append({
                    "acct": acct, "empty_pct": pct, "empty_n": u.get("empty_n"),
                    "zk_n": u.get("calls"), "in_state": False,
                })

    orphans.sort(key=lambda o: acct_sort_key(o["acct"]))
    empties.sort(key=lambda e: acct_sort_key(e["acct"]))
    return {
        "zk_orphan_route": orphans,
        "zk_empty_output": empties,
        "zk_unattributed": unattributed,
    }


def print_zk_diagnostics(rows: list[dict[str, Any]],
                         zk: dict[str, dict[str, Any]] | None = None) -> None:
    diag = zk_diagnostics(rows, zk)
    tag = lambda d: "" if d.get("in_state", True) else " [state 外]"
    if diag["zk_unattributed"]:
        un = diag["zk_unattributed"]
        n = sum(u["zk_n"] or 0 for u in un)
        sp = sum(u["zk_spend"] or 0.0 for u in un)
        n7 = sum(u["zk_n7"] or 0 for u in un)
        print()
        print(f"⚠ zk 未归属消耗 ({len(un)} 个编号): {n} calls / ${sp:.2f} 累计, "
              f"7d={n7} calls —— 这些 zk-N 有流量但 state.json 无对应 acct-N，"
              f"不计入上面的表格行")
        for u in un:
            live = {True: "svc活", False: "svc缺失", None: "svc未知"}[u["svc_live"]]
            print(f"    {u['acct']}: 累计={u['zk_n']} calls/${u['zk_spend']:.2f}, "
                  f"7d={u['zk_n7']}, 空返={u['empty_pct']}%, {live}, last={u['last_seen']}")
    if diag["zk_orphan_route"]:
        print()
        print(f"✗ zk 孤儿路由 ({len(diag['zk_orphan_route'])}): "
              f"LiteLLM 注册着 zk-N 但 198 无 zero-N svc —— 请求打到不存在的后端")
        for o in diag["zk_orphan_route"]:
            print(f"    {o['acct']}{tag(o)}: 7d={o['zk_n7']} calls, 累计={o['zk_n']}, "
                  f"空返={o['empty_pct']}%, last={o['last_seen']} "
                  f"— 建议 DELETE /model/delete 清 zk-{o['acct'].split('-')[1]}-* 条目")
    if diag["zk_empty_output"]:
        print()
        print(f"⚠ zk 稳定空返 ({len(diag['zk_empty_output'])}): "
              f"completion_tokens=0 占比 ≥{ZK_EMPTY_PCT_ALERT}% (n≥{ZK_EMPTY_MIN_N}) "
              f"—— 稳定空返通常=网页端订阅失效，需查 sub 状态")
        for e in diag["zk_empty_output"]:
            print(f"    {e['acct']}{tag(e)}: {e['empty_pct']}% ({e['empty_n']}/{e['zk_n']})")


def render_table(state: dict[str, dict[str, Any]], *, summary: bool) -> None:
    now = time.time()
    emails = email_map()
    spend_recent = remote_198_spend_recent()
    zk = remote_198_zk_usage()
    rows = build_rows(state, now=now, emails=emails, spend_recent=spend_recent, zk=zk)
    ts = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = len(state)
    print(f"=== BEGIN chatgpt-acct-quota @ {ts} | source=198:state.json | rows={total} ===")
    print("legend: reset/restore 列显示未来倒计时或 '-'；已过时刻由下一 cron tick 触发 auto-revive")
    print("legend: zk_* = zerokey 累计消耗；zk_lat 是端到端总时延（非首 token —— "
          "LiteLLM 未记录真 TTFT）")
    print(
        f"{'acct':9s} {'email':32s} {'take':>4s} {'status':>7s} {'tier':>16s} "
        f"{'7d%':>5s} {'reset':>12s} "
        f"{'main_n':>7s} {'main$':>7s} {'codex_n':>8s} {'codex$':>7s} "
        f"{'zk_n':>6s} {'zk$':>8s} {'zk_n7':>6s} {'zk$7':>7s} "
        f"{'lat_avg':>7s} {'lat_p95':>7s} "
        f"{'next_reset':>12s} {'restore':>9s} {'sub_until':>20s} {'sub_left':>8s}  cause"
    )
    print("-" * 268)
    for r in rows:
        def num(v: Any, fmt: str) -> str:
            return format(v, fmt) if v is not None else "-"
        print(
            f"{r['acct']:9s} {r['email'] or '-':32s} {r['take']:>4s} {r['status']:>7s} "
            f"{r['tier']:>16s} {r['pct7d_cell']:>5s} "
            f"{r['reset']:>12s} "
            f"{r['main_n_cell']:>7s} {r['main_s_cell']:>7s} "
            f"{r['codex_n_cell']:>8s} {r['codex_s_cell']:>7s} "
            f"{num(r['zk_n'], 'd'):>6s} {num(r['zk_spend'], '.2f'):>8s} "
            f"{num(r['zk_n7'], 'd'):>6s} {num(r['zk_spend7'], '.2f'):>7s} "
            f"{num(r['zk_lat_avg'], '.1f'):>7s} {num(r['zk_lat_p95'], '.1f'):>7s} "
            f"{r['next_reset']:>12s} "
            f"{r['restore']:>9s} "
            f"{r['sub_until']:>20s} "
            f"{r['sub_left']:>8s}  {r['cause'] or ''}"
        )

    zk_tot_n = sum(r["zk_n"] or 0 for r in rows)
    zk_tot_sp = sum(r["zk_spend"] or 0.0 for r in rows)
    zk_tot_n7 = sum(r["zk_n7"] or 0 for r in rows)
    zk_tot_sp7 = sum(r["zk_spend7"] or 0.0 for r in rows)
    zk_active = [r["acct"] for r in rows if (r["zk_n7"] or 0) > 0]
    # 全池合计 vs 表格行合计：差额=未归属（state.json 无对应 acct）。两个都印，
    # 否则表格合计会被当成全池真值。
    all_n = sum((u.get("calls") or 0) for u in zk.values())
    all_sp = sum((u.get("spend") or 0.0) for u in zk.values())
    if zk_tot_n or all_n:
        print()
        print(f"ⓘ zerokey 表内 {len(zk_active)} 号: 累计 {zk_tot_n} calls / ${zk_tot_sp:.2f}  |  "
              f"近 7d {zk_tot_n7} calls / ${zk_tot_sp7:.2f}")
        if all_n != zk_tot_n:
            print(f"  全池累计 {all_n} calls / ${all_sp:.2f} "
                  f"(差 {all_n - zk_tot_n} calls / ${all_sp - zk_tot_sp:.2f} 未归属，见下)")

    print_zk_diagnostics(rows, zk)

    stale = [
        acct
        for acct, row in state.items()
        if ((spend_recent.get(acct, {}).get("main") or {}).get("calls") or 0) >= 50
        and int(row.get("primary_pct") or 0) < 5
    ]
    if stale:
        print()
        print(f"⚠ probe-stale ({len(stale)}): 上游 7d%≈0 但 LiteLLM 24h 流量≥50 calls → "
              f"{sorted(stale, key=acct_sort_key)}")

    codex_total_calls = sum(
        ((spend_recent.get(acct, {}).get("codex") or {}).get("calls") or 0)
        for acct in state
    )
    codex_total_spend = sum(
        ((spend_recent.get(acct, {}).get("codex") or {}).get("spend") or 0.0)
        for acct in state
    )
    if codex_total_calls:
        codex_active = [
            acct for acct in state
            if ((spend_recent.get(acct, {}).get("codex") or {}).get("calls") or 0) > 0
        ]
        print()
        print(f"ⓘ codex (gpt-5.3) 独立配额池 24h: "
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
    print(f"paused  ={len(paused):2d}  {sort(paused)} (7d quota pause)")
    print(f"offline ={len(offline):2d}  {sort(offline)} (manual_offline)")
    if zombie:
        print(f"zombie  ={len(zombie):2d}  {sort(zombie)} (state placeholder; no probe data — likely deploy scale=0 + router cleared)")


def render_json(state: dict[str, dict[str, Any]]) -> None:
    """结构化输出，供 chatgpt_acct_quota_to_lark.py 消费（取代固定宽度反解）。"""
    now = time.time()
    zk = remote_198_zk_usage()
    rows = build_rows(
        state,
        now=now,
        emails=email_map(),
        spend_recent=remote_198_spend_recent(),
        zk=zk,
    )
    print(json.dumps({
        "generated_at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "198:state.json + LiteLLM_SpendLogs",
        "rows": rows,
        "diagnostics": zk_diagnostics(rows, zk),
        "zk_pool_totals": {
            "calls": sum((u.get("calls") or 0) for u in zk.values()),
            "spend": round(sum((u.get("spend") or 0.0) for u in zk.values()), 2),
            "calls7": sum((u.get("calls7") or 0) for u in zk.values()),
            "spend7": round(sum((u.get("spend7") or 0.0) for u in zk.values()), 2),
        },
    }, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--json", action="store_true",
                        help="结构化 JSON 输出（无文本表 / 无 BEGIN-END frame）")
    args = parser.parse_args()
    state = load_state()
    if args.json:
        render_json(state)
        return 0
    render_table(state, summary=args.summary)
    print(f"=== END chatgpt-acct-quota | rows={len(state)} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
