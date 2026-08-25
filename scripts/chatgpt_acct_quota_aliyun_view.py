#!/usr/bin/env python3
"""Render the aliyun (carher ns) ChatGPT acct pool table.

不同于 198 prod：
  - 阿里云没有 quota-rebalance state.json（paused/restore 不可得）
  - 但**上游配额照样拿得到**：直接在 pod 内探 /codex/usage，是**实时**值，
    比 198 那边的 state.json 快照（最多陈旧 25min / manual_offline 6h）更新鲜
  - 只跑 gpt-5.5 一档（无 5.4 / 5.3-codex 独立池子）→ codex_* 列恒空
数据源：
  1. kubectl -n carher get deploy            → 行宇宙 + readyReplicas 采样 N 次
     （**用 deploy 不用 pod**：scale=0 的号没有 pod，走 pod 会让它们整行消失）
  2. kubectl -n carher logs --since=15m      → refresh-401 条数（时间窗，非行数窗）
  3. kubectl -n carher exec <pod> -- sh -c 'stat; cat auth.json'
       → email / expires_at / sub_until / mtime / device_code_requested_at / jwt exp
         （stat 与 cat 合并进同一次 exec，不额外增加 exec 次数）
  4. kubectl -n carher exec <pod> -- python3 <probe>
       → 上游 /codex/usage 拿 7d% 真实用量（pod 内出 CF，带 ChatGPT-Account-ID
         + Originator codex_cli_rs 头，阿里云 SG IP 也通 —— 2026-08-03 复测
         acct-122 PROBE_OK plan=pro used_pct=26，wrapper 里"被 CF 403"的旧注释是错的）
  5. kubectl -n carher exec litellm-db-0 -- psql  → LiteLLM_SpendLogs 24h+7d

2026-07-13: OpenAI Pro 配额从双窗口 (5h primary + 7d secondary) 合并为
单 7d 窗口 (primary_window.limit_window_seconds=604800, secondary_window=null)。

**运行位置**：本脚本要求 `kubectl -n carher` 可用。首选由 wrapper
chatgpt-acct-quota-aliyun.sh 把整份文件送到 k8s-work-226 上跑（226 自带 kubectl
和集群凭证）。本地经 jms proxy 的 16443 隧道路径 2026-08-03 实测 TLS handshake
timeout，已不可用。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import functools
import json
import os
import re
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

# /accounts/check/v4 的响应把每个账号包在 `accounts[<account_id>]` 下，
# entitlement / account / last_active_subscription 都是**该节点的兄弟键**，不在顶层；
# will_renew 在 last_active_subscription（不是 entitlement）里。
# 2026-08-20 实证（198 acct-237 同结构）：旧版从顶层取 → 全 None、sub_src=live 零命中。
# 这段既 exec 进模块供单测，又原样拼进 in-pod 探针串，单一真相不漂移。
_SUB_PARSE_SRC = r'''
def _sub_from_check(chk, aid):
    accts = (chk.get("accounts") or {})
    node = accts.get(aid) or accts.get("default")
    if node is None and accts:
        node = next(iter(accts.values()))
    node = node or {}
    ent = (node.get("entitlement") or {})
    acc = (node.get("account") or {})
    las = (node.get("last_active_subscription") or {})
    exp = ent.get("expires_at")
    until = None
    if exp:
        try:
            until = int(datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp())
        except Exception:
            until = None
    return {
        "sub_until_live": until,
        "has_active": ent.get("has_active_subscription"),
        "will_renew": las.get("will_renew"),
        "sub_plan_live": ent.get("subscription_plan"),
        "is_deactivated": acc.get("is_deactivated"),
    }
'''

exec(_SUB_PARSE_SRC, globals())

USAGE_PROBE_CODE = r'''
import json, urllib.request, urllib.error, sys
from datetime import datetime

CODEX_HEADERS = {
    "Originator": "codex_cli_rs",
    "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)",
}

def _get(url, token, acct_id, timeout):
    req = urllib.request.Request(url, headers=dict(CODEX_HEADERS, **{
        "Authorization": "Bearer " + token,
        "ChatGPT-Account-ID": acct_id,
    }))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())
''' + _SUB_PARSE_SRC + r'''
try:
    with open("/chatgpt-auth/auth.json") as f:
        a = json.load(f)
    token = a["access_token"]
    acct_id = a.get("account_id", "")
    usage = _get("https://chatgpt.com/backend-api/codex/usage", token, acct_id, 12)
    # accounts/check 是 best-effort：失败只标 _sub._err，不影响 usage 的 200 判定
    try:
        chk = _get("https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                   token, acct_id, 12)
        usage["_sub"] = _sub_from_check(chk, acct_id)
    except urllib.error.HTTPError as e:
        usage["_sub"] = {"_err": e.code}
    except Exception as e:
        usage["_sub"] = {"_err": -1, "_body": str(e)[:120]}
    sys.stdout.write(json.dumps(usage))
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
    """行宇宙 = **deployment**，不是 pod。

    2026-08-03 改。原来走 `get pod -l pool=chatgpt-acct`，scale=0 的号根本没有 pod
    → 整行从表上消失。实测阿里云 7/11/123 就是 scale=0，它们「该退役还是该救」的
    状态正是最需要被记录的，静默消失最糟。deploy 才是声明式的号清单。

    readiness 口径用 deploy 的 `readyReplicas`（K8s 自己的聚合），不用
    `pod.containerStatuses[0].ready` —— 后者在多 pod / 滚动时 items[0] 指向不确定，
    198 侧 2026-08-02 就是这么把结论采成互斥的。
    """
    raw = kubectl("get", "deploy", "-o", "json", timeout=30)
    items = json.loads(raw).get("items", [])
    out = []
    for it in items:
        meta = it.get("metadata", {})
        name = meta.get("name", "")
        m = re.match(r"^chatgpt-acct-(\d+)$", name)
        if not m:
            continue
        spec = (it.get("spec") or {}).get("replicas")
        st = it.get("status") or {}
        out.append({
            "acct": f"acct-{m.group(1)}",
            "deploy": name,
            "pod": "",                      # 由 attach_pods() 补
            "spec_replicas": int(spec or 0),
            "ready": bool(st.get("readyReplicas") or 0),
            "restarts": 0,
            "started_at": meta.get("creationTimestamp"),
        })
    return out


def attach_pods(rows: list[dict[str, Any]]) -> None:
    """给有 pod 的行补 pod 名 / restarts（exec 需要具体 pod 名）。"""
    try:
        raw = kubectl("get", "pod", "-l", POD_LABEL, "-o", "json", timeout=30)
    except Exception as exc:
        print(f"# WARN: list pods failed: {type(exc).__name__}", file=sys.stderr)
        return
    by_acct: dict[str, dict[str, Any]] = {}
    for it in json.loads(raw).get("items", []):
        meta, status = it.get("metadata", {}), it.get("status", {})
        app = (meta.get("labels") or {}).get("app", "")
        if not app.startswith("chatgpt-acct-"):
            continue
        if status.get("phase") != "Running":
            continue
        cs = (status.get("containerStatuses") or [{}])[0]
        by_acct[app.replace("chatgpt-", "", 1)] = {
            "pod": meta.get("name", ""),
            "restarts": cs.get("restartCount", 0),
        }
    for r in rows:
        r.update(by_acct.get(r["acct"], {}))


# ── 服务平面 ────────────────────────────────────────────────────────────────
# 为什么必须单独采：上游探针（/codex/usage）走 access_token，JWT 还有效就返 200
# → 上游看着全绿；但 pod 内 litellm 到期前主动 refresh，refresh_token 死了就
# 401 → 打印设备码 → 拒绝进 Ready。两个平面在死 refresh_token 上给出相反答案。
SERVING_SAMPLES = int(os.environ.get("SERVING_SAMPLES", "2"))
SERVING_SAMPLE_GAP = int(os.environ.get("SERVING_SAMPLE_GAP", "20"))
# 窗口必须是**时间**不是行数。行数窗（--tail=N）会把「正在失败」与「保留日志里
# 曾经失败过」混成一个数：陈旧的几条会让 verdict 永远卡 REFRESH_DYING，而日志
# 变啰嗦时又漏判。15min 覆盖约 3 轮重试（refresh 每 5min 一轮）。
SERVING_LOG_SINCE = os.environ.get("SERVING_LOG_SINCE", "15m")


def sample_serving(rows: list[dict[str, Any]]) -> int:
    """deploy readyReplicas 采样 N 次 + 每号数近 15m 的 refresh-401 条数。

    单点 ready 自己就是噪声，所以采 N 次并把 ready_n/samples 一起留下 —— 抖动要
    可见，不能被一次快照收敛成一个假结论。
    局限要说出来：几十秒间隔只抓得住**秒级**抖动，分钟级慢抖仍会显示 n/n。
    所以 ready 只用来定位，定性靠 refresh_err（死 refresh 不会抖，稳定报）。
    """
    live = [r for r in rows if r["spec_replicas"] > 0]
    for r in rows:
        r["ready_n"] = 0
        r["ready_samples"] = 0
    for i in range(SERVING_SAMPLES):
        if i:
            time.sleep(SERVING_SAMPLE_GAP)
        try:
            raw = kubectl("get", "deploy", "-o", "json", timeout=30)
        except Exception:
            continue
        ready_by = {}
        for it in json.loads(raw).get("items", []):
            m = re.match(r"^chatgpt-acct-(\d+)$", it.get("metadata", {}).get("name", ""))
            if m:
                ready_by[f"acct-{m.group(1)}"] = int(
                    (it.get("status") or {}).get("readyReplicas") or 0)
        for r in rows:
            if r["spec_replicas"] <= 0:
                continue
            r["ready_samples"] += 1
            if ready_by.get(r["acct"], 0) > 0:
                r["ready_n"] += 1

    def refresh_err(r: dict[str, Any]) -> int | None:
        n = r["acct"].split("-")[1]
        try:
            log = subprocess.check_output(
                ["kubectl", "-n", NS, "logs", "-l", f"app=chatgpt-acct-{n}",
                 f"--since={SERVING_LOG_SINCE}"],
                text=True, stderr=subprocess.DEVNULL, timeout=25)
        except Exception:
            return None          # 取不到 ≠ 0 条，必须区分
        return log.count("refresh token failed")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for r, cnt in zip(live, ex.map(refresh_err, live)):
            r["refresh_err"] = cnt
    return int(time.time())


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
    """Return {email, expires_at, plan, sub_until, p7d, p_reset, codex_7d}
    via kubectl exec.
    auth.json gives identity; codex/usage gives rate-limit window (single 7d).
    """
    info = {
        "email": "", "expires_at": None, "plan": "", "sub_until": None,
        "live_plan": None,
        "p7d": None, "p_reset": None,
        "codex_7d": None,
        "probe_err": None,
        "mtime": None, "dcr": None, "jwt_exp": None,
        # 实时订阅口径（/accounts/check/v4）——探到才有值，探不到退回 JWT sub_until
        "sub_until_live": None, "has_active": None, "will_renew": None,
        "sub_plan_live": None, "is_deactivated": None,
    }
    # 读 auth.json 拿身份。并发 kubectl exec 走 jms 隧道时偶发单个 exec
    # 超时/reset，必须重试；彻底失败要标 probe_err（否则 p7d 为 None
    # 会 fall through 到 PROBE_ERR，fail-closed 不接单）。
    #
    # stat 与 cat 合并进**同一次 exec**（`sh -c`）：pod_cred 龄不额外增加 exec 次数，
    # 也不用靠"窗口恒为 10d"反推。第一行是 mtime，其余是 auth.json 正文。
    auth_exc = ""
    for attempt in range(3):
        try:
            raw = subprocess.check_output(
                ["kubectl", "-n", NS, "exec", pod, "--", "sh", "-c",
                 f"stat -c %Y {AUTH_PATH}; cat {AUTH_PATH}"],
                text=True, timeout=20, stderr=subprocess.DEVNULL,
            )
            head, _, body = raw.partition("\n")
            auth = json.loads(body)
            plan, sub_until = subscription_info(auth)
            claims = claims_from_id_token(auth)
            info.update({
                "email": email_from_auth(auth),
                "expires_at": auth.get("expires_at"),
                "plan": plan,
                "sub_until": sub_until,
                "mtime": int(head.strip()) if head.strip().isdigit() else None,
                # device_code_requested_at 存在 = 已掉进 device-code 流程。
                # 这一条抓得到 ready=n/n 且 refresh_err=0 的号（198 侧 acct-114
                # 就是只有这个判据能抓）——与 ready/refresh_err 互补，缺一漏一半。
                "dcr": auth.get("device_code_requested_at"),
                "jwt_exp": claims.get("exp"),
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
            info["live_plan"] = usage.get("plan_type")
            info["p7d"] = pw.get("used_percent")
            info["p_reset"] = pw.get("reset_at")
            # `used_percent=100` 与 `allowed=False` 是**两个不同的事实**。
            # 2026-08-05 在 198 侧实测：35 个 used_pct=100 的号里 7 个仍
            # allowed=True / limit_reached=False，官方还在放行。只看 pct 会把
            # 这批还能服务的号和真封顶的并成一类 → 对它们做下线动作是净损失。
            # 两个集群的 tier 词表必须一致（同一张表同一列），所以这里也要取。
            info["allowed"] = rl.get("allowed")
            info["limit_reached"] = rl.get("limit_reached")
            for extra in usage.get("additional_rate_limits") or []:
                if "codex" in (extra.get("limit_name") or "").lower():
                    erl = extra.get("rate_limit") or {}
                    info["codex_7d"] = (erl.get("primary_window") or {}).get("used_percent")
                    break
            sub = usage.get("_sub") or {}
            if sub.get("_err") is None:
                info["sub_until_live"] = sub.get("sub_until_live")
                info["has_active"] = sub.get("has_active")
                info["will_renew"] = sub.get("will_renew")
                info["sub_plan_live"] = sub.get("sub_plan_live")
                info["is_deactivated"] = sub.get("is_deactivated")
            return info
        except Exception as e:
            last_exc = type(e).__name__
            time.sleep(1.5 * (attempt + 1))
    info["probe_err"] = f"probe_fail:{last_exc}"
    return info


def gather_auth(pods: list[dict[str, Any]]) -> None:
    """只探有 pod 的行。scale=0 的号没有 pod，不算「探针失败」。

    区分这两者很重要：scale=0 是**预期内下线**（upstream_tier=SCALED_DOWN），
    而 probe_err 是「本该能探却探不到」。混成一类会让退役号一直挂在告警里。
    """
    targets = [p for p in pods if p.get("pod")]
    for p in pods:
        if not p.get("pod"):
            p.setdefault("probe_err", None)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futs = {pool.submit(probe_auth, p["pod"]): p for p in targets}
        for fut in concurrent.futures.as_completed(futs):
            p = futs[fut]
            try:
                p.update(fut.result())
            except Exception as e:
                p.update({
                    "email": "", "expires_at": None, "plan": "", "sub_until": None,
                    "p7d": None,
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


def effective_sub_until(p: dict[str, Any]) -> float | int | None:
    """订阅到期以**实探 /accounts/check/v4** 为准（续订后立刻反映），探不到才退回
    JWT claim 的 `sub_until`。判 SUB_EXP / sub_expired 都用这个——否则续订过的号
    会因为 JWT claim 冻结在旧到期日而被误报过期（acct-84 实证）。"""
    return p.get("sub_until_live") or p.get("sub_until")


# ── 判定函数（模块层，不是 render() 里的闭包）───────────────────────────────
# 2026-08-03 从 render() 内提出来。原来是闭包 → backend/tests 够不着 → 阿里云侧
# 的 status/verdict 逻辑一行都没被测过。198 view 把 serving_verdict 等放模块层就是
# 为了让测试能喂手写 dict 直接断言，这里对齐同一个理由。
#
# ⚠ 为什么这些纯函数与 198 view 里的同名函数是两份而不是共用一个模块：
# 两个 view 都是**整份文件被送到远端 python3 执行**的自包含脚本（198 走
# `ssh JSZX-AI-03 python3 -` 喂 stdin，阿里云走 gzip+base64 落盘到 226 再跑），
# 远端没有这个 repo，`import` 任何本地模块都会 ImportError。重复是被传输方式
# 逼出来的，不是疏忽 —— 改判定逻辑时两边都要改。


def status_of(p: dict[str, Any], now: float) -> str:
    """上游+pod 两平面合成的粗判定（沿用原有语义，仅提到模块层）。

    顺序刻意：探针失败一律 PROBE_ERR，**绝不 fall through 成 ONLINE**。
    取不到数不等于健康 —— fail-closed，宁可不接单也不要把未知当好用。
    """
    if p.get("spec_replicas") == 0:
        return "OFFLINE"          # 预期内下线（scale=0），与「取不到」区分
    if not p.get("ready"):
        return "OFFLINE"
    if p.get("probe_err") == "token_invalidated":
        return "TOKEN_X"
    if p.get("probe_err"):
        return "PROBE_ERR"
    if not isinstance(p.get("p7d"), (int, float)):
        return "PROBE_ERR"
    _sub = effective_sub_until(p)
    if _sub and _sub <= now:
        return "SUB_EXP"
    if p.get("live_plan") and p["live_plan"] != "pro":
        return "SUB_EXP"
    if isinstance(p.get("p7d"), (int, float)) and p["p7d"] >= 100:
        return "QUOTA"
    return "ONLINE"


def take_of(p: dict[str, Any], now: float) -> str:
    return "yes" if status_of(p, now) == "ONLINE" else "-"


def cause_of(p: dict[str, Any], now: float,
             spend_24h: dict[str, dict[str, float]]) -> str:
    causes = []
    if p.get("probe_err"):
        causes.append(p["probe_err"])
    if isinstance(p.get("expires_at"), int) and p["expires_at"] - now <= 0:
        causes.append("token_expired")
    elif isinstance(p.get("expires_at"), int) and p["expires_at"] - now < 3 * 86400:
        causes.append("token<3d")
    _sub = effective_sub_until(p)
    if _sub and _sub <= now:
        causes.append("sub_expired")
    elif _sub and _sub - now < 7 * 86400:
        causes.append("sub<7d")
    if p.get("live_plan") and p["live_plan"] != "pro":
        causes.append(f"live_{p['live_plan']}")
    if isinstance(p.get("p7d"), (int, float)) and p["p7d"] >= 100:
        causes.append("7d_full")
    s24 = spend_24h.get(p["acct"], {})
    if int(s24.get("calls") or 0) == 0 and p.get("spec_replicas") != 0:
        causes.append("idle_24h")
    if p.get("restarts", 0) > 0:
        causes.append(f"restarts={p['restarts']}")
    return ",".join(causes)


def upstream_tier_of(p: dict[str, Any]) -> str:
    """把探针结果翻成与 198 表同名的 upstream_tier 取值。

    阿里云没有 quota-rebalance 引擎，这一列是**本次实时探测**得出的，
    不是 state.json 快照 —— 所以 probe_age 恒为 'live'。

    探针失败 → PROBE_ERR，**不是 HEALTHY**。空白/绿色会被读成「没问题」，
    而这里的真相是「不知道」。

    2026-08-05 词表与 198 侧对齐（`live_tier()`）。原来只有 `OFFLINE-7D` 一档，
    把「官方已拒」和「用满但仍放行」压成一个标签 —— 后者还能服务，一起下线是净损失。
    **两个 view 的纯函数各自一份（远端执行、不能 import），改判定逻辑时两边都要改。**
    """
    if p.get("spec_replicas") == 0:
        return "SCALED_DOWN"
    if p.get("probe_err") == "token_invalidated":
        return "401 需re-OAuth"
    if p.get("probe_err"):
        return "PROBE_ERR"
    p7d = p.get("p7d")
    if not isinstance(p7d, (int, float)):
        return "PROBE_ERR"
    if p.get("limit_reached") or p.get("allowed") is False:
        return "7D_CAP"
    if p7d >= 100:
        return "7D_BRIM"
    if p7d >= 90:
        return "7D_HIGH"
    return "HEALTHY"


def ready_cell(p: dict[str, Any]) -> str:
    """恒为 n/N 形式。'off' = 有意 scale=0，'-' = 取不到，两者不能混。"""
    if p.get("spec_replicas") == 0:
        return "off"
    samples = p.get("ready_samples") or 0
    if not samples:
        return "-"
    return f"{p.get('ready_n') or 0}/{samples}"


def serving_verdict(p: dict[str, Any], upstream_tier: str) -> str:
    """三平面合成的唯一总判定 —— 与 198 表同一条优先级链。

    服务平面刻意盖过上游：撞顶会被摘掉，服务平面坏掉却一直留在池里当黑洞。
    这一列存在的理由是 acct-121（上游 HEALTHY 7d=3%，同时刻 deploy 0/1 READY、
    每 5min 一条 refresh 401）—— 没有它，「有余量但一个请求都服务不了」和
    「真健康」在表上完全同形。
    """
    if p.get("spec_replicas") == 0:
        return "OFFLINE"
    samples = p.get("ready_samples") or 0
    if not samples:
        return "?"                      # 没取到服务平面，不假装知道
    n = p.get("ready_n") or 0
    if n == 0:
        return "SERVING_DEAD"
    if p.get("dcr"):
        return "DEVICE_CODE_HELL"
    if (p.get("refresh_err") or 0) > 0:
        return "REFRESH_DYING"
    if n < samples:
        return "FLAPPING"
    if upstream_tier and upstream_tier != "HEALTHY":
        return f"UPSTREAM:{upstream_tier}"
    return "OK"


def exp_gap_cell(p: dict[str, Any]) -> float | None:
    """JWT exp 与 auth.json expires_at 的差（天）。>0 = 从未成功 refresh 过。

    必须归一化毫秒口径：acct-66 实测 expires_at 是毫秒、JWT exp 是秒，没归一化
    时算出 ≈-5.6 万年，而 never_refreshed 判据是 >0 → 垃圾值静默混在表里。
    """
    exp, jwt_exp = p.get("expires_at"), p.get("jwt_exp")
    if not exp or not jwt_exp:
        return None
    gap = (_exp_seconds(jwt_exp) - _exp_seconds(exp)) / 86400.0
    if abs(gap) > 60:
        print(f"# WARN: {p.get('acct')} exp_gap={gap:.1f}d 超出合理范围，置空",
              file=sys.stderr)
        return None
    return round(gap, 1)


def _exp_seconds(exp: Any) -> float:
    """秒/毫秒混用的 epoch 归一化成秒。"""
    v = float(exp)
    return v / 1000.0 if v > 1e11 else v


# ── 飞书表 schema（与 198 的 --json rows 对齐）─────────────────────────────
SITE = "aliyun"


def build_rows(rows: list[dict[str, Any]], *, now: float,
               spend_24h: dict[str, dict[str, float]],
               spend_7d: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """产出与 198 view `--json` 同构的 row dict，供 chatgpt_acct_quota_to_lark.py 消费。

    列的诚实性规则（别把「没有」印成「正常」）：
      · probe_age = 'live' —— 阿里云没有 quota 引擎，这一列是本次实测，不是快照。
        留空会被读成"取不到"，而真相是"比 198 那边还新"。
      · auth_sync = 'no-local' —— 阿里云没有 /Data/chatgpt-auth 镜像可比，
        方向判定（local↑/pod↑）在这里**不存在**，不是"一致"。
      · restore = '-' —— auto-revive 是 quota 引擎的概念，阿里云没有引擎。
      · codex_* / zk_* = None —— 阿里云只跑 gpt-5.5 一档；zk 用量聚合本期未接。
        空白**不代表 0**，调用方会另打一行 stderr 说明。
    """
    out = []
    for p in sorted(rows, key=lambda x: acct_sort_key(x["acct"])):
        acct = p["acct"]
        s24 = spend_24h.get(acct, {})
        calls = int(s24.get("calls") or 0)
        spend = float(s24.get("spend") or 0.0)
        s7 = spend_7d.get(acct, {})
        calls7 = int(s7.get("calls") or 0)
        spend7 = float(s7.get("spend") or 0.0)
        tier = upstream_tier_of(p)
        scaled = p.get("spec_replicas") == 0
        p7d = p.get("p7d")
        # 订阅到期：实探 /accounts/check/v4 优先，探不到退回 JWT 快照并用 sub_src 标明来源。
        live_sub = p.get("sub_until_live")
        if live_sub:
            eff_sub: Any = live_sub
            sub_src = "live"
        else:
            eff_sub = p.get("sub_until")
            if scaled:
                sub_src = "off"
            elif not p.get("pod"):
                sub_src = "no-pod"
            elif p.get("probe_err") == "token_invalidated":
                sub_src = "401"
            elif p.get("probe_err"):
                sub_src = "ERR"
            else:
                sub_src = "state"
        will_renew_cell = (
            ("yes" if p.get("will_renew") else "no")
            if p.get("will_renew") is not None else "-"
        )
        out.append({
            "site": SITE,
            "acct": acct,
            "email": p.get("email") or "",
            "take": take_of(p, now),
            "status": status_of(p, now),
            "verdict": serving_verdict(p, tier),
            "ready": ready_cell(p),
            "refresh_err": p.get("refresh_err"),
            "device_code": ("-" if not p.get("pod")
                            else ("yes" if p.get("dcr") else "no")),
            "exp_gap": exp_gap_cell(p),
            "upstream_tier": tier,
            "probe_age": "live",
            "auth_sync": "no-local",
            "pod_cred": fmt_age_epoch(p.get("mtime"), now),
            "pct7d": int(p7d) if isinstance(p7d, (int, float)) else None,
            "reset": fmt_expires(p.get("p_reset"), now),
            "main_n": None if scaled or not calls else calls,
            "main_spend": None if scaled or not calls else round(spend, 2),
            "main_n7": None if scaled or not calls7 else calls7,
            "main_spend7": None if scaled or not calls7 else round(spend7, 2),
            "codex_n": None,
            "codex_spend": None,
            "codex_n7": None,
            "codex_spend7": None,
            "zk_n": None, "zk_spend": None, "zk_n7": None, "zk_spend7": None,
            "zk_lat_avg": None, "zk_lat_p95": None, "zk_empty_pct": None,
            "next_reset": fmt_expires(p.get("p_reset"), now),
            "restore": "-",
            "sub_until": fmt_sub_until_full(eff_sub),
            "sub_left": fmt_expires(eff_sub, now),
            "sub_src": sub_src,
            "will_renew": will_renew_cell,
            "cause": cause_of(p, now, spend_24h),
        })
    return out


def fmt_age_epoch(epoch: Any, now: float) -> str:
    """now - epoch，纯描述「多久没更新」。

    刻意**不**做活性断言：198 侧实测 9 个 expires_at 已过的号全是 HEALTHY 且
    24h 各跑 400~550 calls —— pod 内部刷新 access_token 不回写 auth.json。
    所以这一列只能回答"这份文件多久没动过"，不能回答"凭证还能用吗"。
    """
    if not epoch:
        return "-"
    seconds = int(now - _exp_seconds(epoch))
    if seconds < 0:
        return "0h"
    day, rem = divmod(seconds, 86400)
    hour = rem // 3600
    return f"{day}d{hour:02d}h" if day else f"{hour}h{(rem % 3600) // 60:02d}m"


def fmt_sub_until_full(epoch: float | int | None) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def render(*, summary: bool, as_json: bool, json_rows: bool = False) -> None:
    now = time.time()
    # 每阶段计时都打到 stderr（wrapper 会把 `[` 开头的行透出来）。
    # 2026-08-04 加：整轮曾经 12-15min，而实测远端干活只要 34s，差额全在链路。
    # 但"链路慢"和"某个 kubectl exec 在重试"在外面看是同一个现象 —— 没有这几行
    # 就只能猜。慢的时候先看这里，别再靠感觉优化。
    _t = [time.time()]

    def lap(name: str) -> None:
        _t.append(time.time())
        print(f"[t] {name}={_t[-1] - _t[-2]:.1f}s", file=sys.stderr)

    pods = list_pods()
    lap("list_deploys")
    if not pods:
        print("# no chatgpt-acct deployments found in ns=carher", file=sys.stderr)
        sys.exit(1)
    attach_pods(pods)
    lap("attach_pods")
    sampled_at = sample_serving(pods)
    lap("sample_serving")
    gather_auth(pods)
    lap("gather_auth")

    spend_24h = spend_window(24)
    # 2026-08-25 加 7d 窗口（与 24h 并存）：24h 看近况、7d 与上游配额窗口同尺度
    spend_7d = spend_window(7 * 24)
    lap("spend_window")
    print(f"[t] TOTAL={time.time() - _t[0]:.1f}s rows={len(pods)}", file=sys.stderr)

    if json_rows:
        rows = build_rows(pods, now=now, spend_24h=spend_24h, spend_7d=spend_7d)
        print(json.dumps({
            "generated_at": datetime.fromtimestamp(now, timezone.utc)
                                    .strftime("%Y-%m-%d %H:%M UTC"),
            "site": SITE,
            "source": ("aliyun carher: deploy.readyReplicas + pod:/chatgpt-auth/auth.json"
                       " + pod:/codex/usage(实时) + LiteLLM_SpendLogs"),
            "serving_sampled_at": datetime.fromtimestamp(sampled_at, timezone.utc)
                                          .strftime("%Y-%m-%d %H:%M:%S UTC"),
            "serving_samples": SERVING_SAMPLES,
            "rows": rows,
            "diagnostics": {},
        }, ensure_ascii=False))
        # 空白 ≠ 0：不说出来，看表的人会把空的 zk 列当成"阿里云没有 zk 消耗"。
        print("[zk] 阿里云 zk_* 列本期未接入 —— 表上空白不代表 0", file=sys.stderr)
        print("[codex] 阿里云只跑 gpt-5.5 一档，codex_* 恒空", file=sys.stderr)
        return

    if as_json:
        out = []
        for p in sorted(pods, key=lambda x: acct_sort_key(x["acct"])):
            acct = p["acct"]
            row = {
                **p,
                "spend_24h": spend_24h.get(acct, {}),
                "spend_7d": spend_7d.get(acct, {}),
            }
            out.append(row)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 绑定 now / spend_24h。局部名故意与模块层函数不同名 —— 同名会让 Python 把
    # 整个函数体内的该名字判为 local，赋值前引用直接 UnboundLocalError。
    _status = functools.partial(status_of, now=now)
    _take = functools.partial(take_of, now=now)
    _cause = functools.partial(cause_of, now=now, spend_24h=spend_24h)

    print(
        f"{'acct':9s} {'email':32s} {'take':>4s} {'status':>8s} "
        f"{'7d%':>5s} {'24h_n':>6s} {'24h$':>7s} {'7d_n':>6s} {'7d$':>8s} {'reset':>9s} "
        f"{'tok_left':>9s} {'sub_until':>10s} {'sub_left':>8s}  cause"
    )
    print("-" * 160)

    silent: list[str] = []
    expiring: list[str] = []
    sub_expiring: list[str] = []
    quota_high: list[str] = []
    total_24h_calls = total_24h_spend = 0.0
    total_7d_calls = total_7d_spend = 0.0

    for p in sorted(pods, key=lambda x: acct_sort_key(x["acct"])):
        acct = p["acct"]
        s24 = spend_24h.get(acct, {})
        c24 = int(s24.get("calls") or 0)
        v24 = float(s24.get("spend") or 0.0)
        total_24h_calls += c24; total_24h_spend += v24
        s7 = spend_7d.get(acct, {})
        c7 = int(s7.get("calls") or 0)
        v7 = float(s7.get("spend") or 0.0)
        total_7d_calls += c7; total_7d_spend += v7

        if c24 == 0:
            silent.append(acct)
        if isinstance(p.get("expires_at"), int) and p["expires_at"] - now < 3 * 86400:
            expiring.append(acct)
        sub_until_ts = effective_sub_until(p)   # 实探优先，与 Lark 表口径一致
        if sub_until_ts and sub_until_ts - now < 7 * 86400:
            sub_expiring.append(acct)
        p7d = p.get("p7d")
        if isinstance(p7d, (int, float)) and p7d >= 90:
            quota_high.append(acct)

        def pct(v):
            return f"{int(v)}%" if isinstance(v, (int, float)) else "-"

        print(
            f"{acct:9s} {(p.get('email') or '-'):32s} "
            f"{_take(p):>4s} {_status(p):>8s} "
            f"{pct(p7d):>5s} "
            f"{(str(c24) if c24 else '-'):>6s} {(f'{v24:.1f}' if c24 else '-'):>7s} "
            f"{(str(c7) if c7 else '-'):>6s} {(f'{v7:.1f}' if c7 else '-'):>8s} "
            f"{fmt_expires(p.get('p_reset'), now):>9s} "
            f"{fmt_expires(p.get('expires_at'), now):>9s} "
            f"{fmt_sub_until(sub_until_ts):>10s} {fmt_expires(sub_until_ts, now):>8s}  "
            f"{_cause(p)}"
        )

    print()
    print(
        f"Σ 24h: {int(total_24h_calls)} calls / ${total_24h_spend:.2f}  ·  Σ 7d: {int(total_7d_calls)} calls / ${total_7d_spend:.2f}    "
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
    takers = [p["acct"] for p in pods if _take(p) == "yes"]
    online = [p["acct"] for p in pods if _status(p) == "ONLINE"]
    quota = [p["acct"] for p in pods if _status(p) == "QUOTA"]
    token_x = [p["acct"] for p in pods if _status(p) == "TOKEN_X"]
    sub_exp = [p["acct"] for p in pods if _status(p) == "SUB_EXP"]
    offline = [p["acct"] for p in pods if _status(p) == "OFFLINE"]
    probe_err = [p["acct"] for p in pods if _status(p) == "PROBE_ERR"]
    sort = lambda rows: sorted(rows, key=acct_sort_key)
    print()
    print(f"take    ={len(takers):2d}  {sort(takers)}")
    print(f"online  ={len(online):2d}  {sort(online)}")
    print(f"quota   ={len(quota):2d}  {sort(quota)} (7d 撞顶)")
    print(f"sub_exp ={len(sub_exp):2d}  {sort(sub_exp)} (订阅过期/非pro, 需续订或摘除)")
    print(f"token_x ={len(token_x):2d}  {sort(token_x)} (token_invalidated, 走 re-OAuth)")
    print(f"probe_err={len(probe_err):2d}  {sort(probe_err)} (探针失败, 状态未知不计入 take)")
    print(f"offline ={len(offline):2d}  {sort(offline)} (pod not ready)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", action="store_true",
                        help="附加 ready/not_ready 分组")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="原样输出 JSON (pod + auth + 24h/7d spend)")
    parser.add_argument("--json-rows", dest="json_rows", action="store_true",
                        help="结构化行，schema 与 198 view --json 对齐（供飞书写表消费）")
    args = parser.parse_args()
    render(summary=args.summary, as_json=args.as_json, json_rows=args.json_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
