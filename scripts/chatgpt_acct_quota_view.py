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
import hashlib
import json
import os
import subprocess
import sys
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


def status(row: dict[str, Any], *, tracked: bool = True) -> str:
    """引擎视角的账号状态。`tracked=False` = 集群上有 deploy 但 state.json 里没有。

    `UNTRACKED` 必须与 `ZOMBIE` 分开。2026-08-05 实测 198 上 74 个 deploy、
    state.json 只有 60 个 —— 差的 14 个（acct-1..24 段）里 **acct-2 / acct-15 是
    spec=1、ready=1 正在跑的**，官方实探却分别是 401 `could_not_parse` 和
    auth.json 里根本没有 access_token。它们既不是"引擎清理后的残留"（ZOMBIE 的
    语义），也不是下线号 —— 是**引擎完全看不见、却占着 pod 的号**。
    走 ZOMBIE 会暗示"该清行"，方向反了。
    """
    if not tracked:
        return "UNTRACKED"
    if row.get("manual_offline"):
        return "OFFLINE"
    if row.get("paused"):
        return "PAUSED"
    if is_zombie(row):
        return "ZOMBIE"
    return "ONLINE"


def take(row: dict[str, Any], *, tracked: bool = True) -> str:
    if not tracked:
        return "?"          # 引擎不管它 → 取不取流量这件事引擎答不了，不能印 "-"
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


def remote_198_pod_auth_code() -> str:
    """pod 内 /chatgpt-auth/auth.json —— 引擎探测唯一读取的凭证副本。

    email 之外顺带带回 refresh_token 指纹 + expires_at + 文件 mtime：同一次
    kubectl exec（`sh -c 'stat; cat'`），零额外开销。指纹是判「认证是否真的进了
    PVC」的唯一硬凭据（明文绝不回传）；mtime 用来判「401 判定是否早于当前凭证」。
    """
    return r'''
import base64, hashlib, json, os, subprocess
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
            ["kubectl", "-n", "litellm-product", "exec", pod, "--", "sh", "-c",
             "stat -c %Y /chatgpt-auth/auth.json; cat /chatgpt-auth/auth.json"],
            text=True,
            timeout=8,
        )
        mtime_line, _, body = raw.partition("\n")
        auth = json.loads(body)
        rt = auth.get("refresh_token") or ""
        try:
            mtime = float(mtime_line.strip())
        except ValueError:
            mtime = None
        # access_token 的 JWT exp —— LiteLLM 信 expires_at 不信这个，两者不一致
        # 就是故障触发器（详见 pod_auth_flags 注释）。同一个文件顺手取，零成本。
        jwt_exp = None
        tok = auth.get("access_token") or ""
        if tok.count(".") >= 2:
            try:
                seg = tok.split(".")[1]
                seg += "=" * (-len(seg) % 4)
                jwt_exp = json.loads(base64.urlsafe_b64decode(seg)).get("exp")
            except Exception:
                pass
        out[acct] = {
            "email": email_from_auth(auth),
            "rt": hashlib.sha256(rt.encode()).hexdigest()[:10] if rt else "",
            "exp": auth.get("expires_at"),
            "mtime": mtime,
            # 存在即"正在/曾掉进 device-code 登录地狱"——最干净的单一判据
            "dcr": auth.get("device_code_requested_at"),
            "jwt_exp": jwt_exp,
        }
    except Exception:
        pass
print(json.dumps(out, ensure_ascii=False))
'''


def remote_198_pod_auth() -> dict[str, dict[str, Any]]:
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
            input=remote_198_pod_auth_code(),
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


# ---- 服务平面（2026-08-02 加）----------------------------------------------
# 为什么必须单独取：表原有 28 列全部落在**上游配额平面**（state.json 的 tier/7d%）
# 和**凭证文件平面**（auth.json 的 auth_sync/pod_cred），没有任何一列回答
# 「这个号现在能不能服务一个请求」。两者在死 refresh_token 上给出相反答案：
#   · access_token 仍被 /codex/usage 接受 → 引擎探测 200 → tier=HEALTHY
#   · pod 内 litellm authenticator 到期前主动 refresh → auth.openai.com/oauth/token
#     401 → 打印设备码提示、拒绝进 Ready
# 2026-08-02 实证 acct-121：state.json 写 HEALTHY 7d=3% 订阅到 8/25，同一时刻
# deploy 是 0/1 READY、AVAILABLE=0，pod 日志每 5min 一条 refresh 401。
# 表"正确地印了一个不完整的事实"，人读成"这号好用"。
#
# 采样次数 >1 是被当晚打脸打出来的：deploy 级 readyReplicas 与 pod 级
# containerStatuses[0].ready 两次单点采样结论互斥（112/121 一次 0/1 一次 true，
# 115 反向）。单点 ready 自己就是噪声，所以采 N 次并把 ready_n/samples 一起印出来
# —— 抖动要可见，不能被一次快照收敛成一个假结论。
#
# 局限（必须说出来，不能让 '2/2' 被读成"稳定"）：两次采样只隔几十秒，只抓得住
# **秒级**抖动。2026-08-02 观察到的抖动是分钟级（02:10 测 0/1、02:14 测 true、
# 02:33 测 2/2），这种慢抖会以 2/2 或 0/2 的形态出现。所以 ready 只用来定位，
# 定性靠 refresh_err —— 它是稳定信号。
SERVING_SAMPLES = int(os.environ.get("SERVING_SAMPLES", "2"))
SERVING_SAMPLE_GAP = int(os.environ.get("SERVING_SAMPLE_GAP", "20"))
# refresh_err 才是**稳定**信号：死 refresh_token 不会抖，每 5min 稳定报一条。
# ready 用来定位、refresh_err 用来定性，两者不能互相替代。
#
# 窗口必须是**时间**不是行数。2026-08-02 实测同一时刻同一 acct：
#   acct-121  tail80=0  tail200=0  tail500=4  since15m=0（15min 内共 10 行日志）
#   acct-118  tail80=1  tail200=3  tail500=5  since15m=1
# 行数窗口把「正在失败」与「保留日志里曾经失败过」混成一个数：tail500 那 4 条已陈旧
# （since15m=0 证明近 15min 没再报），会让 verdict 永远卡在 REFRESH_DYING；tail80
# 又在日志变啰嗦时漏判。时间窗直接对应「现在是否在失败」，与日志密度无关。
# 15min 覆盖 ~3 轮重试（refresh 每 5min 一轮），余量够。
SERVING_LOG_SINCE = os.environ.get("SERVING_LOG_SINCE", "15m")


def remote_198_serving_plane_code() -> str:
    """198 上跑：deploy readyReplicas 采样 N 次 + 每 acct 数 refresh 401 行数。

    口径固定为 deploy 的 `readyReplicas`（K8s 自己的聚合），不用
    pod.containerStatuses[0].ready —— 后者在多 pod / 滚动时 items[0] 指向不确定，
    当晚就是这么把结论采成互斥的。
    """
    return (
        r'''
import json, os, re, subprocess, time
from concurrent.futures import ThreadPoolExecutor
os.environ["KUBECONFIG"] = os.path.expanduser("~/.kube/config")
SAMPLES = ''' + str(SERVING_SAMPLES) + r'''
GAP = ''' + str(SERVING_SAMPLE_GAP) + r'''
SINCE = "--since=''' + str(SERVING_LOG_SINCE) + r'''"
PAT = re.compile(r"^chatgpt-acct-(\d+)$")

def snapshot():
    d = json.loads(subprocess.check_output(
        ["kubectl", "-n", "litellm-product", "get", "deploy", "-o", "json"],
        text=True, timeout=30))
    out = {}
    for item in d.get("items", []):
        m = PAT.match(item["metadata"]["name"])
        if not m:
            continue
        spec = (item.get("spec") or {}).get("replicas") or 0
        st = item.get("status") or {}
        out["acct-" + m.group(1)] = (int(spec), int(st.get("readyReplicas") or 0))
    return out

acc = {}
for i in range(SAMPLES):
    if i:
        time.sleep(GAP)
    for acct, (spec, ready) in snapshot().items():
        e = acc.setdefault(acct, {"spec": spec, "ready_n": 0, "samples": 0})
        e["spec"] = spec
        e["samples"] += 1
        if ready > 0:
            e["ready_n"] += 1

def refresh_err(acct):
    n = acct.split("-")[1]
    try:
        log = subprocess.check_output(
            ["kubectl", "-n", "litellm-product", "logs",
             "-l", "app=chatgpt-acct-" + n, SINCE],
            text=True, stderr=subprocess.DEVNULL, timeout=20)
    except Exception:
        return None          # 取不到 ≠ 0 条，必须区分
    return log.count("refresh token failed")

targets = [a for a, e in acc.items() if e["spec"] > 0]
with ThreadPoolExecutor(max_workers=8) as ex:
    for acct, cnt in zip(targets, ex.map(refresh_err, targets)):
        acc[acct]["refresh_err"] = cnt
print(json.dumps({"sampled_at": int(time.time()), "accts": acc}))
'''
    )


def remote_198_serving_plane() -> dict[str, Any]:
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
            input=remote_198_serving_plane_code(),
            capture_output=True,
            text=True,
            timeout=240,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        print(f"[serving] 取数失败 rc={result.returncode} "
              f"{(result.stderr or '')[-200:]}", file=sys.stderr)
    except Exception as exc:
        print(f"[serving] 取数异常 {type(exc).__name__}", file=sys.stderr)
    return {}


# ---- 上游实探平面（2026-08-05 加）------------------------------------------
# 为什么必须加：在此之前 `upstream_tier`/`7d%`/`reset` 三列全部来自引擎 state.json
# 快照，`probe_age` 就是它的年龄 —— 实测有 4h46m 这种。用户手动问配额时拿快照当
# 现状交出去，等于把陈旧值冒充事实（2026-08-05 被当场指出）。
#
# 阿里云那侧从一开始就是 in-pod 实探（`probe_age=live`），198 反而是唯一还在读
# 快照的。两个集群同一张表却两种口径，读者无从分辨。
#
# 成本：一次 ssh + N 个并发 kubectl exec，2026-08-05 实测 59 个 pod **16.4s**。
# 这是**只读**探针，引擎自己每 5min 就在打同一个端点 —— 不属于
# 「在真号上做有代价的探测」（那指的是实跑 refresh-grant / OAuth）。
#
# ⚠ 字段是 `rate_limit`（**单数**）。2026-08-05 我写成 `rate_limits` → 40 个号
# `used_percent` 全取到 None，差点报成「官方返回空、表上 100% 是错的」。
# 抓原始响应才发现是自己的 bug。**加字段前先 dump 一份原文。**
# /accounts/check/v4 的响应把每个账号包在 `accounts[<account_id>]` 下，
# entitlement / account / last_active_subscription 都是**该节点的兄弟键**，不在顶层；
# will_renew 在 last_active_subscription（不是 entitlement）里。
# 2026-08-20 实证 acct-237：旧版从顶层取 → 全 None，18 个 live 号全部退回 state、
# sub_src=live 零命中。这段既 exec 进模块供单测，又原样拼进 in-pod 探针串，
# 单一真相不漂移（内嵌字符串里的解析逻辑单测够不着，正是它带病上线的原因）。
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

LIVE_USAGE_PROBE = r'''
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
    usage = _get("https://chatgpt.com/backend-api/codex/usage", token, acct_id, 15)
    # accounts/check 是 best-effort：失败只标 _sub._err，不影响 usage 的 200 判定
    try:
        chk = _get("https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                   token, acct_id, 15)
        usage["_sub"] = _sub_from_check(chk, acct_id)
    except urllib.error.HTTPError as e:
        usage["_sub"] = {"_err": e.code}
    except Exception as e:
        usage["_sub"] = {"_err": -1, "_body": type(e).__name__ + ":" + str(e)[:120]}
    sys.stdout.write(json.dumps(usage))
except urllib.error.HTTPError as e:
    sys.stdout.write(json.dumps({"_err": e.code,
                                 "_body": e.read().decode(errors="ignore")[:300]}))
except Exception as e:
    sys.stdout.write(json.dumps({"_err": -1,
                                 "_body": type(e).__name__ + ":" + str(e)[:150]}))
'''


def remote_198_live_usage_code() -> str:
    """198 上跑：每个 Running 的 acct pod 内实探官方 /codex/usage。

    行宇宙同样从 deploy 取（与服务平面同口径），这样 `spec=0` / `无 pod` 与
    `探测失败` 三种情况在结果里是**可区分的三个值**，而不是统一的空白。
    """
    return (
        r'''
import json, os, re, subprocess, time
from concurrent.futures import ThreadPoolExecutor
os.environ["KUBECONFIG"] = os.path.expanduser("~/.kube/config")
NS = "''' + K8S_NS + r'''"
PAT = re.compile(r"^chatgpt-acct-(\d+)$")
PROBE = ''' + repr(LIVE_USAGE_PROBE) + r'''

def kc(*args, timeout=40):
    return subprocess.check_output(["kubectl", "-n", NS, *args], text=True,
                                   stderr=subprocess.DEVNULL, timeout=timeout)

pods = {}
for it in json.loads(kc("get", "pod", "-o", "json")).get("items", []):
    app = ((it.get("metadata") or {}).get("labels") or {}).get("app", "")
    m = PAT.match(app)
    if not m or (it.get("status") or {}).get("phase") != "Running":
        continue
    pods.setdefault("acct-" + m.group(1), it["metadata"]["name"])

def probe(acct):
    pod = pods[acct]
    last = ""
    for attempt in range(2):
        try:
            u = json.loads(subprocess.check_output(
                ["kubectl", "-n", NS, "exec", pod, "--", "python3", "-c", PROBE],
                text=True, stderr=subprocess.DEVNULL, timeout=45))
            if u.get("_err") is not None:
                return acct, {"http": u["_err"], "body": (u.get("_body") or "")[:200]}
            rl = u.get("rate_limit") or {}
            pw = rl.get("primary_window") or {}
            extras = []
            for ex in u.get("additional_rate_limits") or []:
                epw = (ex.get("rate_limit") or {}).get("primary_window") or {}
                extras.append({"name": ex.get("limit_name"),
                               "pct": epw.get("used_percent"),
                               "reset_at": epw.get("reset_at")})
            sub = u.get("_sub") or {}
            return acct, {
                "http": 200,
                "plan": u.get("plan_type"),
                "email": u.get("email"),
                "allowed": rl.get("allowed"),
                "limit_reached": rl.get("limit_reached"),
                "used_pct": pw.get("used_percent"),
                "reset_at": pw.get("reset_at"),
                "window_s": pw.get("limit_window_seconds"),
                "reached_type": (u.get("rate_limit_reached_type") or {}).get("type"),
                "reset_credits": (u.get("rate_limit_reset_credits") or {}).get(
                    "available_count"),
                "extras": extras,
                "sub_until_live": sub.get("sub_until_live"),
                "has_active": sub.get("has_active"),
                "will_renew": sub.get("will_renew"),
                "sub_plan_live": sub.get("sub_plan_live"),
                "is_deactivated": sub.get("is_deactivated"),
            }
        except Exception as e:
            last = type(e).__name__ + ":" + str(e)[:100]
            time.sleep(1.5 * (attempt + 1))
    return acct, {"http": None, "body": "exec_fail:" + last}

out = {}
if pods:
    with ThreadPoolExecutor(max_workers=10) as ex:
        for acct, info in ex.map(probe, sorted(pods)):
            out[acct] = info
print(json.dumps({"probed_at": int(time.time()), "accts": out}))
'''
    )


def remote_198_live_usage() -> dict[str, Any]:
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
            input=remote_198_live_usage_code(),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        print(f"[live] 实探取数失败 rc={result.returncode} "
              f"{(result.stderr or '')[-200:]}", file=sys.stderr)
    except Exception as exc:
        print(f"[live] 实探取数异常 {type(exc).__name__}", file=sys.stderr)
    return {}


# 401 body 的四种指纹 → 处置动作不同，不能并成一句"401"。
# 2026-08-05 实测 19 个 401 的分布：token_expired 12 / invalidated 5 /
# could_not_parse 1 / auth.json 无 access_token 1。
def live_401_kind(body: str) -> str:
    text = (body or "").lower()
    if "keyerror" in text and "access_token" in text:
        return "no_access_token"
    if "invalidated" in text:
        return "invalidated"
    if "expired" in text:
        return "token_expired"
    if "parse" in text:
        return "could_not_parse"
    return "other_401"


def live_probe_cell(spec: int | None, info: dict[str, Any] | None,
                    *, fetched: bool = True) -> str:
    """实探结果这一格。**五种情况必须可区分**，不能都渲染成空白。

    `off`(spec=0) / `no-pod`(spec>0 但无 Running pod) / `live`(200) /
    `401:<kind>` / `ERR:<http>` / `FETCH_FAIL`(整条链路没取到，见下)。

    `FETCH_FAIL` 与 `live` 的区别是整张表可信度的分界：前者意味着这一行的
    `upstream_tier`/`7d%` 退回了引擎快照，读者必须按快照解读。留空会被读成
    「这号没问题」——这正是 2026-08-02 acct-121 那次的失效形态。
    """
    if not fetched:
        return "FETCH_FAIL"
    if (spec or 0) == 0:
        return "off"
    if info is None:
        return "no-pod"
    http = info.get("http")
    if http == 200:
        return "live"
    if http == 401:
        return f"401:{live_401_kind(info.get('body') or '')}"
    if http is None:
        return "ERR:exec"
    if http == -1:
        return f"401:{live_401_kind(info.get('body') or '')}" \
            if "access_token" in (info.get("body") or "") else "ERR:probe"
    return f"ERR:{http}"


def live_tier(spec: int | None, info: dict[str, Any] | None,
              *, fetched: bool = True, state_tier: str = "") -> str:
    """实探 → 上游配额平面的判定。取代原来直接搬 state.json 的 `tier`。

    ⚠ `used_pct=100` 与 `allowed=False` 是**两个不同的事实**，不能压成一个标签。
    2026-08-05 实测 40 个探通的号里，35 个 `used_pct=100`，但其中 7 个
    （78/90/92/115/118/119/121）仍是 `allowed=True / limit_reached=False`
    —— 官方还在放行。把它们和真封顶的 28 个并成"撞顶"会导致对能用的号做下线动作。

      7D_CAP   官方已拒（allowed=False / limit_reached=True）
      7D_BRIM  用满 100% 但**官方仍放行**
      7D_HIGH  ≥90% 未满
      HEALTHY  其余

    链路整体失败时**退回快照并保留原值**，由 `live_probe=FETCH_FAIL` 标注，
    绝不假装是实探值。
    """
    if not fetched:
        return state_tier or "-"
    if (spec or 0) == 0:
        return "SCALED_DOWN"
    if info is None:
        return "NO_POD"
    http = info.get("http")
    if http == 200:
        if info.get("limit_reached") or info.get("allowed") is False:
            return "7D_CAP"
        pct = info.get("used_pct")
        try:
            pct = int(pct)
        except (TypeError, ValueError):
            return "PROBE_NO_PCT"
        if pct >= 100:
            return "7D_BRIM"
        if pct >= 90:
            return "7D_HIGH"
        return "HEALTHY"
    if http == 401 or (http == -1 and "access_token" in (info.get("body") or "")):
        # 保持这个**字面值**：auth_plane_diagnostics() 按它分桶（认证没进 PVC /
        # repair_frozen / 判定已陈旧 / 需实跑 OAuth）。改字符串会让整段诊断静默失效。
        return "401 需re-OAuth"
    return "PROBE_ERR"


def live_plane_diagnostics(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """上游实探平面。2026-08-05 新增 —— 在此之前 198 侧这三列全是引擎快照。

    `untracked` 是最该被看见的一段：集群上有 deploy 但 state.json 里没有的号，
    **引擎既不探测也不轮转，撞顶不会被摘、死了不会被修**。实测 198 有 14 个，
    其中 acct-2/15 是 spec=1 正在跑且官方 401 —— 表在改之前它们整行不存在。

    `live_vs_state` 不做归因，只把两个平面的分歧摆出来：分歧既可能是快照陈旧
    （引擎 6h 才重探 manual_offline），也可能是实探那一刻的抖动。**这一段不回答
    "谁对"**，只回答"这两个数不一样，别只看一个"。
    """
    def probe_failed(r: dict[str, Any]) -> bool:
        p = str(r.get("live_probe") or "")
        return p.startswith("401:") or p.startswith("ERR:") or p == "no-pod"

    # 比 tier **字符串**是错的比较：引擎写 `OFFLINE-7D`、实探写 `7D_CAP`/`7D_BRIM`，
    # 三者说的是同一件事（7d 窗口已满），只是我 2026-08-05 换了词表。
    # 第一版就这么比，74 行里报出 35 个"不一致"—— 全是改名噪声，把真分歧埋了。
    # 改为先归成粗类再比，并额外要求 pct 差 ≥10 才算数值分歧。
    def tier_class(tier: str) -> str:
        t = (tier or "").upper()
        if t in ("OFFLINE-7D", "7D_CAP", "7D_BRIM"):
            return "FULL"
        if t == "7D_HIGH":
            return "HIGH"
        if t == "HEALTHY":
            return "OK"
        if "401" in t or t == "TOKEN_INVALID":
            return "DEAD"
        if t == "SCALED_DOWN":
            return "OFF"
        return "UNKNOWN"

    def diverged(r: dict[str, Any]) -> bool:
        if r["live_probe"] != "live" or r["state_tier"] in ("-", "", None):
            return False
        if tier_class(r["upstream_tier"]) != tier_class(r["state_tier"]):
            return True
        lp, sp = r.get("pct7d"), r.get("state_pct7d")
        return lp is not None and sp is not None and abs(lp - sp) >= 10

    return {
        # 引擎看不见的号：不参与配额轮转，也不参与 auto-revive
        "untracked": [
            {"acct": r["acct"], "ready": r["ready"], "live_probe": r["live_probe"],
             "upstream_tier": r["upstream_tier"], "pct7d": r["pct7d"],
             "main_n": r["main_n"]}
            for r in rows if not r.get("tracked")
        ],
        # 官方当场拒了 —— 实时证据，不是快照
        "live_401": [
            {"acct": r["acct"], "live_probe": r["live_probe"], "ready": r["ready"],
             "state_tier": r["state_tier"], "pod_cred": r["pod_cred"],
             "auth_sync": r["auth_sync"]}
            for r in rows if str(r.get("live_probe") or "").startswith("401:")
        ],
        # 官方已拒新请求（allowed=False）——与"用满 100% 但仍放行"必须分开，
        # 后者还能服务，对它做下线动作是净损失
        "capped": [
            {"acct": r["acct"], "pct7d": r["pct7d"], "reset": r["reset"],
             "main_n": r["main_n"]}
            for r in rows if r["upstream_tier"] == "7D_CAP"
        ],
        "brim_but_allowed": [
            {"acct": r["acct"], "pct7d": r["pct7d"], "reset": r["reset"],
             "main_n": r["main_n"]}
            for r in rows if r["upstream_tier"] == "7D_BRIM"
        ],
        # 实探与引擎快照**实质**不一致（已排除纯改名差异，见 diverged()）
        "live_vs_state": [
            {"acct": r["acct"], "live_tier": r["upstream_tier"],
             "state_tier": r["state_tier"], "live_pct": r["pct7d"],
             "state_pct": r["state_pct7d"]}
            for r in rows if diverged(r)
        ],
        # 探不通 —— 空白不代表 0，必须报
        "probe_failed": [
            {"acct": r["acct"], "live_probe": r["live_probe"], "ready": r["ready"]}
            for r in rows if probe_failed(r)
        ],
    }


def print_live_plane_diagnostics(rows: list[dict[str, Any]]) -> None:
    diag = live_plane_diagnostics(rows)
    if d := diag["untracked"]:
        print()
        print(f"✗✗ 引擎不认识的号 ({len(d)}): 集群上有 deployment 但 state.json 无此 key "
              f"—— quota 引擎既不探测也不轮转，**撞顶不会被摘、401 不会被修**，"
              f"改表之前这些行在表上根本不存在")
        for x in d:
            print(f"    {x['acct']}: ready={x['ready']} live={x['live_probe']} "
                  f"tier={x['upstream_tier']} 7d={x['pct7d']} 24h_main={x['main_n']}")
    if d := diag["capped"]:
        print()
        print(f"⛔ 官方已拒新请求 ({len(d)}): allowed=false / limit_reached=true —— "
              f"实探当场拿到的，不是快照")
        for x in d:
            print(f"    {x['acct']}: 7d={x['pct7d']}% reset={x['reset']} "
                  f"24h_main={x['main_n']}")
    if d := diag["brim_but_allowed"]:
        print()
        print(f"⚠ 用满 100% 但官方仍放行 ({len(d)}): allowed=true / limit_reached=false "
              f"—— **与上面那批不是一回事**，这些还能服务，别一起下线")
        for x in d:
            print(f"    {x['acct']}: 7d={x['pct7d']}% reset={x['reset']} "
                  f"24h_main={x['main_n']}")
    if d := diag["live_401"]:
        print()
        print(f"✗ 官方实探 401 ({len(d)}): 按 body 指纹分类 —— token_expired 要重认证，"
              f"invalidated 多半是账号侧被吊销，could_not_parse / no_access_token 是"
              f"凭证文件本身坏了。处置动作不同，别并成一句")
        for x in d:
            print(f"    {x['acct']}: {x['live_probe']} ready={x['ready']} "
                  f"引擎快照={x['state_tier']} pod_cred={x['pod_cred']} "
                  f"auth_sync={x['auth_sync']}")
    if d := diag["live_vs_state"]:
        print()
        print(f"ⓘ 实探与引擎快照不一致 ({len(d)}): 表上 upstream_tier/7d% 已改为**实探值**，"
              f"引擎那份留在 state_tier 列。分歧可能是快照陈旧（manual_offline 6h 才重探），"
              f"也可能是探测那一刻的抖动 —— 这一段不回答谁对")
        for x in d:
            print(f"    {x['acct']}: 实探={x['live_tier']}({x['live_pct']}%) "
                  f"快照={x['state_tier']}({x['state_pct']}%)")
    if d := diag["probe_failed"]:
        print()
        print(f"⚠ 实探失败 ({len(d)}): 这些行的 7d%/reset 是**空白而非 0** —— "
              f"空白代表取不到，不代表没用量")
        for x in d:
            print(f"    {x['acct']}: {x['live_probe']} ready={x['ready']}")


def local_auth_plane() -> dict[str, dict[str, Any]]:
    """/Data/chatgpt-auth/acct-N/auth.json —— re-oauth.sh / 手工认证的落地处。

    引擎**不读**这里做探测（只在 attempt_repair 时 kubectl cp 进 pod），
    所以这份比 pod 新 = 认证没生效。指纹口径必须与 pod 侧一致（sha256[:10]）。
    """
    out: dict[str, dict[str, Any]] = {}
    for path in LOCAL_AUTH_DIR.glob("acct-*/auth.json"):
        try:
            auth = json.loads(path.read_text())
            rt = auth.get("refresh_token") or ""
            out[path.parent.name] = {
                "rt": hashlib.sha256(rt.encode()).hexdigest()[:10] if rt else "",
                "exp": auth.get("expires_at"),
                "mtime": path.stat().st_mtime,
            }
        except Exception:
            continue
    return out


def remote_198_spend_recent_code() -> str:
    return r'''
import json, os, subprocess
os.environ["KUBECONFIG"] = os.path.expanduser("~/.kube/config")
sql = (
    "SELECT split_part(model_id, '-gpt-', 1) AS acct, "
    "CASE WHEN model_id LIKE '%gpt-5.3%' THEN 'codex' ELSE 'main' END AS bucket, "
    # 2026-08-25 双窗口并存：24h 看近况（main_n/main$），7d 与上游配额窗口同尺度
    # （main_n7/main$7）。同一次查询用 FILTER 聚合，扫描仍只有 7d 范围一遍。
    "COUNT(*) FILTER (WHERE \"startTime\" > NOW() - INTERVAL '24 hours') AS n24, "
    "ROUND(COALESCE(SUM(spend) FILTER (WHERE \"startTime\" > NOW() - INTERVAL '24 hours'), 0)::numeric, 2) AS s24, "
    "COUNT(*) AS n7, ROUND(SUM(spend)::numeric, 2) AS s7 "
    "FROM \"LiteLLM_SpendLogs\" "
    "WHERE model_id LIKE 'chatgpt-acct-%-gpt-%' "
    "AND \"startTime\" > NOW() - INTERVAL '7 days' "
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
        if len(parts) < 6:
            continue
        acct = parts[0].replace("chatgpt-", "", 1)
        bucket = parts[1]
        try:
            calls = int(parts[2])
            spend = float(parts[3])
            calls7 = int(parts[4])
            spend7 = float(parts[5])
        except ValueError:
            continue
        out.setdefault(acct, {})[bucket] = {"calls": calls, "spend": spend,
                                            "calls7": calls7, "spend7": spend7}
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


def email_map(pod_auth: dict[str, dict[str, Any]]) -> dict[str, str]:
    """本机 .creds 兜底 + pod 内 id_token claims 覆盖（后者更权威）。

    pod_auth 由调用方取一次后传入 —— 它是 N 次 kubectl exec，绝不能重复调用。
    """
    emails = local_cred_emails()
    for acct, info in pod_auth.items():
        if info.get("email"):
            emails[acct] = info["email"]
    return emails


def auth_sync_verdict(local: dict[str, Any] | None,
                      pod: dict[str, Any] | None,
                      refresh_err: int | None = None) -> str:
    """凭证平面一致性。引擎探测只读 pod 那份，所以方向决定严重性：

      local↑    = /Data 比 pod 新 → **认证没进 PVC，引擎永远看不到**（真故障）
      pod↑      = pod 比 /Data 新 → 无害（引擎读的就是新的，/Data 只是旧副本）
      same      = 两份指纹一致
      both_dead = 两份一致**且 pod 正在报 refresh 401** → 一致但都是死 RT
      no-pod    = pod 内读不到 auth.json（pod 不存在 / PVC 空 / 未 ready）

    2026-08-02 把 'ok' 改名 'same'：acct-121 两份指纹一致所以印 'ok'，而那个
    RT 已经被 auth.openai.com 拒了 —— 'ok' 这个词让"两份都是死的"看起来是绿的。
    这一列只回答"两份一不一样"，从不回答"有没有效"。

    方向靠 expires_at 比较（= 凭证签发时刻 + 固定窗口，实测恒为写入时刻+10d），
    不用文件 mtime —— pod 侧的 mtime 是 kubectl cp 时刻，与凭证新旧无关。
    """
    if not pod or not pod.get("rt"):
        return "no-pod"
    if not local or not local.get("rt"):
        return "no-local"
    if local["rt"] == pod["rt"]:
        return "both_dead" if (refresh_err or 0) > 0 else "same"
    l_exp, p_exp = local.get("exp") or 0, pod.get("exp") or 0
    if l_exp > p_exp:
        return "local↑"
    return "pod↑"


def device_code_cell(pod: dict[str, Any] | None) -> str:
    """auth.json 里有 `device_code_requested_at` = 这个 pod 掉进过 device-code 登录地狱。

    机制（2026-08-02 另一路实测，比数日志更根因）：LiteLLM 信 `expires_at` 不信 JWT
    `exp`。`expires_at` 越过后**每个请求**都先试 refresh → refresh_token 401 →
    进 device-code 流程，而 `_poll_for_authorization_code()` 是**同步阻塞**轮询、
    容器单进程 → event loop 被堵 6~17min → `/health/readiness` 一起哑 → kubelet 摘
    endpoints → proxy 报 `Cannot connect to host`。表面像网络故障，实为凭证。

    这一列是**零成本判据**（auth.json 本来就在读），但**不能单独用**：
    2026-08-02 实测 acct-120 有 dcr、acct-115/118 没有，而三者同时 ready=0/2。
    115/118 是 refresh 已成功过（expires_at 已推到 8-11）、凭证轴自愈但 readiness
    还在事故尾巴上。所以 dcr 抓根因、ready+refresh_err 抓现状，缺一漏一半。
    """
    if not pod:
        return "-"
    return "yes" if pod.get("dcr") else "no"


def exp_gap_cell(pod: dict[str, Any] | None) -> float | None:
    """JWT exp 与 expires_at 的天数差。>0 = 从未成功 refresh 过一次。

    gap 来自 onboard 写 `expires_at=issue+7d` 而真 token 活 10d。一旦有过一次成功
    refresh，两个字段会被一起改成一致（gap→0）并永久自愈。所以 gap>0 是
    「这个号从上线起就没刷成功过」的硬证据，不是"快到期了"。

    两个字段**口径可能不同**：`expires_at` 实测有毫秒的（acct-66），JWT `exp` 恒为秒。
    必须各自过 `_exp_seconds()` 归一化 —— 2026-08-02 没归一化时 acct-66 算出
    `exp_gap=-20656055.3`（≈-5.6 万年），而 `never_refreshed` 的判据是 `>0`，
    所以这个垃圾值当时**没触发告警、静默混在表里**。
    """
    if not pod:
        return None
    exp, jwt_exp = pod.get("exp"), pod.get("jwt_exp")
    if not exp or not jwt_exp:
        return None
    gap = (_exp_seconds(jwt_exp) - _exp_seconds(exp)) / 86400.0
    if abs(gap) > 60:
        # token 窗口是天级，差 60 天以上说明字段解析仍有问题 —— 报出来别当真值用
        print(f"[exp_gap] 异常: exp={exp} jwt_exp={jwt_exp} gap={gap:.1f}d，置 None",
              file=sys.stderr)
        return None
    return round(gap, 1)


def ready_cell(serving: dict[str, Any] | None) -> str:
    """'2/2' 全程 Ready / '0/2' 全程不 Ready / '1/2' 抖动 / 'off' 已下线 / '-' 无数据。

    永远带分母 —— 单点 ready 是噪声（当晚实证），分母是读者判断该不该信这一格的依据。
    """
    if not serving:
        return "-"
    if (serving.get("spec") or 0) == 0:
        return "off"
    n, total = serving.get("ready_n"), serving.get("samples")
    if not total:
        return "-"
    return f"{n}/{total}"


def serving_verdict(upstream_tier: str, serving: dict[str, Any] | None,
                    pod: dict[str, Any] | None = None) -> str:
    """两平面合成的最终判定 —— 这一列是"这个号现在好不好用"的唯一答案。

    存在的理由：`upstream_tier` 只覆盖上游配额，它在 acct-121 上是 HEALTHY 而 pod
    是 0/1 READY。**任何一行都不允许在服务平面坏掉时读出 OK。**

    优先级刻意让服务平面盖过上游：上游有余量但服务不了，比上游撞顶更毒
    —— 撞顶会被引擎 scale=0 摘掉，服务平面坏掉却一直留在池子里（Ready=False
    绕过 preflight）。
    """
    if serving is None:
        return "?"                      # 没取到服务平面数据，不假装知道
    if (serving.get("spec") or 0) == 0:
        return "OFFLINE"                # 预期内下线
    n, total = serving.get("ready_n") or 0, serving.get("samples") or 0
    err = serving.get("refresh_err")
    if total and n == 0:
        return "SERVING_DEAD"           # 上游可能全绿，但一个请求都服务不了
    if pod and pod.get("dcr"):
        # 还在 Ready，但已掉进 device-code 地狱：每个请求都试 refresh→401→同步
        # 阻塞轮询，随时会把 event loop 堵死。比 REFRESH_DYING 更确定，排在前面。
        return "DEVICE_CODE_HELL"
    if (err or 0) > 0:
        return "REFRESH_DYING"          # 还在服务，但 token 刷新已断 → 早晚死
    if total and n < total:
        return "FLAPPING"               # Ready 在抖，不能算健康也不能算死
    if upstream_tier and upstream_tier != "HEALTHY":
        return f"UPSTREAM:{upstream_tier}"
    return "OK"


def pod_cred_age(pod: dict[str, Any] | None, now: float) -> str:
    """pod 内凭证文件写入至今多久（引擎读的这份有多老）。

    **不要用 auth.json 的 `expires_at` 判活性。** 2026-08-01 实证证伪：
    acct-112..121 共 9 个号 `expires_at` 已过，却全是 HEALTHY 且 24h 各跑
    400~550 calls —— pod 内部刷新 access_token 不回写这个文件。所以 `expires_at`
    只能用来比较两份文件的**先后**（内容派生、与时钟无关），不能断言"还有效"。
    """
    if not pod:
        return "-"
    mtime = pod.get("mtime")
    if not mtime:
        return "-"
    seconds = int(now - float(mtime))
    if seconds < 0:
        return "0h"
    day, rem = divmod(seconds, 86400)
    return f"{day}d{rem // 3600:02d}h" if day else f"{rem // 3600}h{(rem % 3600) // 60:02d}m"


def _exp_seconds(exp: Any) -> float:
    """expires_at 可能是秒或毫秒 —— 按量级判定，别按字段名猜。"""
    try:
        value = float(exp)
    except (TypeError, ValueError):
        return 0.0
    return value / 1000.0 if value > 1e12 else value


def probe_age(row: dict[str, Any], now: float) -> str:
    """这一行距**引擎上次真探测**多久（不是距上次写入）。

    2026-08-02 实证过口径，别再改回"写入"的说法：state.json 每 5min tick 全量重写，
    但 `ts` 只在真探测时更新 —— acct-121 `ts`=01:43:59 与 cron.log 最后一条
    `HEALTHY` 日志 01:43:59 完全对齐，之后的 tick 全是
    `SKIP (7d=3%, Nmin ago (<25min))`，那个 Nmin 正是从 `ts` 算的。acct-112 同样对齐。
    （原名 state_age + "距上次写入"的注释是错的，2026-08-02 改名纠正。）

    这一列的用途：tier/status/cause 都是快照。HEALTHY 行最多陈旧 25min
    （PROBE_INTERVAL_LOW），manual_offline 行最多 6h —— 没这列就会把陈旧值当现状。
    """
    ts = row.get("ts")
    if not ts:
        return "-"
    seconds = int(now - float(ts))
    if seconds < 0:
        return "0m"
    hour, rem = divmod(seconds, 3600)
    return f"{hour}h{rem // 60:02d}m"


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
    local_auth: dict[str, dict[str, Any]] | None = None,
    pod_auth: dict[str, dict[str, Any]] | None = None,
    serving: dict[str, dict[str, Any]] | None = None,
    live: dict[str, dict[str, Any]] | None = None,
    live_fetched: bool | None = None,
) -> list[dict[str, Any]]:
    """state ∪ deploy → 每 acct 一条结构化行（唯一事实源）。

    文本表和 --json 都消费这里的输出。历史上 to_lark.py 用固定宽度字符偏移
    (line[100:107]) 反解渲染后的文本，加一列就要重算全部偏移，且错位是静默的
    ——切出半个数字仍能 float()。所以数值留 raw、格式化留 cell，两条消费路径分开。

    **行宇宙 = state.json 的 key ∪ deploy 全集**（2026-08-05 改）。原来只列 state
    的 key，于是「引擎不认识的号」在表上根本不存在 —— 实测 198 有 74 个 deploy 而
    state 只有 60 个，差的 14 个里 acct-2/15 正跑着且官方 401。**表上没有的行，
    看表的人不会去找。** 与阿里云侧同口径（那边一直是走 deploy）。

    ⚠ 四个平面各自独立，**禁止用一个平面的列去否证另一个平面的信号**：
      · 上游配额平面 = upstream_tier / pct7d / reset（源：**in-pod 实探 /codex/usage**）
      · 引擎快照平面 = state_tier / status / cause / restore（源：state.json）
      · 凭证文件平面 = auth_sync / pod_cred（源：auth.json 指纹与 mtime）
      · 服务平面     = ready / refresh_err（源：deploy readyReplicas + pod 日志）
    2026-08-02 踩过：拿 `tier=HEALTHY` 当"这号没问题"的真值，去否证
    `pod_cred 过期` 这个服务平面信号，结论"9 个 HEALTHY 也过期 → 该信号是噪声"。
    实际那 9 个里 7 个 deploy 是 readyReplicas=0。**用被质疑的指标去否证质疑它的
    信号，永远得到"信号是噪声"。** 要否证服务平面信号，对照组必须取自服务平面。
    """
    rows: list[dict[str, Any]] = []
    local_auth = local_auth or {}
    pod_auth = pod_auth or {}
    serving = serving or {}
    live = live or {}
    # `live_fetched` 缺省时按「有没有实探数据」推断，**不默认 True**。
    # 默认 True 的话，任何没传 live 的调用方（含全部单测）都会让每一行走
    # live_tier(spec=None, info=None) → `SCALED_DOWN`、live_probe → `off`，
    # 即把「没探」静默渲染成「这号已下线」—— 最坏的一种失效方向。
    # 显式传 True 而 live 为空是另一回事（链路通但没有 Running pod），予以保留。
    if live_fetched is None:
        live_fetched = bool(live)
    universe = set(state) | set(serving)
    for acct in sorted(universe, key=acct_sort_key):
        tracked = acct in state
        row = state.get(acct) or {}
        buckets = spend_recent.get(acct) or {}
        main = buckets.get("main") or {}
        codex = buckets.get("codex") or {}
        main_calls = main.get("calls")
        main_spend = main.get("spend")
        codex_calls = codex.get("calls")
        codex_spend = codex.get("spend")
        # 2026-08-25 加 7d 窗口（与 24h 并存）：24h 看近况、7d 与上游配额窗口同尺度
        main_calls7 = main.get("calls7")
        main_spend7 = main.get("spend7")
        codex_calls7 = codex.get("calls7")
        codex_spend7 = codex.get("spend7")
        sv = serving.get(acct)
        spec = (sv or {}).get("spec")
        lv = live.get(acct)
        # 上游配额三列的**唯一来源**：in-pod 实探。链路整体失败时退回快照，
        # 由 live_probe=FETCH_FAIL 显式标注（见 live_tier / live_probe_cell）。
        state_tier_raw = str(row.get("tier", "-"))
        if state_tier_raw.upper() == "TOKEN_INVALID":
            state_tier_raw = "401 需re-OAuth"
        tier_display = live_tier(spec, lv, fetched=live_fetched,
                                 state_tier=state_tier_raw)
        probe_cell = live_probe_cell(spec, lv, fetched=live_fetched)
        live_ok = probe_cell == "live"
        is_scaled_down = (spec or 0) == 0 if (live_fetched and sv) else \
            str(row.get("tier") or "").upper() == "SCALED_DOWN"
        # SCALED_DOWN: pod=0 无新流量，main/codex 流量列 mute
        # ——SpendLogs 残留可能误导。
        if live_ok:
            pct_val = lv.get("used_pct")
            pct_cell = "-" if pct_val is None else str(pct_val)
        elif live_fetched:
            pct_cell = "-"          # 探不通就是探不通，不拿快照冒充
            pct_val = None
        else:
            pct_cell = str(row.get("primary_pct") or "-")
            pct_val = to_num_or_none(pct_cell)
        if is_scaled_down:
            main_n_cell = main_s_cell = codex_n_cell = codex_s_cell = "-"
            main_calls = main_spend = codex_calls = codex_spend = None
            main_calls7 = main_spend7 = codex_calls7 = codex_spend7 = None
        else:
            main_n_cell = f"{main_calls}" if main_calls else "-"
            main_s_cell = f"{main_spend:.1f}" if main_spend is not None else "-"
            codex_n_cell = f"{codex_calls}" if codex_calls else "-"
            codex_s_cell = f"{codex_spend:.1f}" if codex_spend is not None else "-"
        # reset 倒计时同样改吃实探的 reset_at；探不通才退回快照
        if live_ok and lv.get("reset_at"):
            p_reset_cell = duration(lv.get("reset_at"), now)
            next_reset_cell = duration(lv.get("reset_at"), now)
        else:
            p_reset_cell = duration(row.get("primary_reset_at"), now)
            next_reset_cell = next_reset(row, now)
        # cause 列：SCALED_DOWN 时优先显 state.cause（2026-06-29 后 cron preflight 保留首因
        # OFFLINE 等；老脏数据兜底 'deploy.spec.replicas=0' → 直接渲染原值）。
        cause = row.get("cause", "")
        notes = stale_notes(row, now)
        if notes:
            cause = f"{cause} [stale: {', '.join(notes)}]" if cause else f"[stale: {', '.join(notes)}]"
        # zerokey 累计消耗：与 7d 配额窗口不同尺度，全量累计看总投入、7d 看近期活跃。
        # 不受 SCALED_DOWN mute 影响 —— 累计消耗是历史事实，pod 停了也仍然成立。
        zu = zk.get(acct) or {}
        la, pa = local_auth.get(acct), pod_auth.get(acct)
        sv_err = (sv or {}).get("refresh_err")
        spark = None
        if live_ok and lv.get("extras"):
            spark = (lv["extras"][0] or {}).get("pct")
        # 订阅到期：实探 /accounts/check/v4 优先（续订后立刻反映，不像 JWT claim
        # 冻结在签发时刻）；实探不可用时退回 state.json 的 JWT 快照。
        # 与 pct7d 的三分支不同——sub 用两分支回退：快照仍是合法信息（scale=0 号
        # 没 pod 可探，快照是唯一来源），只用 sub_src 如实标明来源，不置空。
        live_sub = lv.get("sub_until_live") if live_ok else None
        if live_sub:
            sub_ts: Any = live_sub
            sub_src_cell = "live"
        else:
            sub_ts = row.get("subscription_active_until")
            sub_src_cell = "state" if live_ok else probe_cell
        will_renew_cell = (
            ("yes" if lv.get("will_renew") else "no")
            if (live_ok and lv.get("will_renew") is not None) else "-"
        )
        rows.append({
            "acct": acct,
            # email 三源：本机 .creds → pod id_token claims → 实探响应体。
            # 最后这个最权威（官方回的就是这个号的注册邮箱），放最后覆盖。
            "email": (lv.get("email") if live_ok else None) or emails.get(acct) or None,
            "take": take(row, tracked=tracked),
            "status": status(row, tracked=tracked),
            "tracked": tracked,
            # 2026-08-05 改：这一列现在是**实探结论**，不再是 state.json 的 tier。
            # 引擎那份留在 state_tier 里并排放，两者不一致本身就是要看的信号。
            "upstream_tier": tier_display,
            "state_tier": state_tier_raw,
            # 实探这一格：live / off / no-pod / 401:<kind> / ERR:* / FETCH_FAIL
            "live_probe": probe_cell,
            "live_allowed": lv.get("allowed") if live_ok else None,
            "live_limit_reached": lv.get("limit_reached") if live_ok else None,
            "plan": lv.get("plan") if live_ok else row.get("plan"),
            "spark_pct": spark,
            # 服务平面：能不能真服务一个请求。ready 带分母因为单点 ready 是噪声。
            "ready": ready_cell(sv),
            "refresh_err": sv_err,
            # 凭证侧的根因判据（零成本，auth.json 本来就在读）
            "device_code": device_code_cell(pa),
            "exp_gap": exp_gap_cell(pa),
            # 两平面合成的最终判定 —— 看表的人应该只看这一列
            "verdict": serving_verdict(tier_display, sv, pa),
            # 实探成功的行恒为 'live'；退回快照的行才显示快照年龄
            "probe_age": "live" if live_ok else probe_age(row, now),
            "state_ts": row.get("ts"),
            # 凭证平面：auth_sync='local↑' 表示认证落在 /Data 但没进 PVC，
            # 引擎读不到 → 认证多少次都不会变绿（需手动 kubectl cp + restart）
            "auth_sync": auth_sync_verdict(la, pa, sv_err),
            "pod_cred": pod_cred_age(pa, now),
            # pod 凭证落地时刻 vs 401 判定时刻。凭证更新 = 判定是对旧凭证下的
            # → 下一次探测会自己翻绿，不该被当成"需人工判活"
            "cred_after_verdict": (
                bool(pa and pa.get("mtime") and row.get("ts")
                     and float(pa["mtime"]) > float(row["ts"]))
            ),
            "repair_attempts": row.get("repair_attempts") or 0,
            "repair_frozen": bool(row.get("repair_frozen")),
            "pct7d": pct_val if live_ok else to_num_or_none(pct_cell),
            "pct7d_cell": pct_cell,
            "state_pct7d": to_num_or_none(str(row.get("primary_pct") or "-")),
            "reset": p_reset_cell,
            "main_n": main_calls,
            "main_spend": main_spend,
            "codex_n": codex_calls,
            "codex_spend": codex_spend,
            "main_n7": main_calls7,
            "main_spend7": main_spend7,
            "codex_n7": codex_calls7,
            "codex_spend7": codex_spend7,
            "main_n_cell": main_n_cell,
            "main_s_cell": main_s_cell,
            "codex_n_cell": codex_n_cell,
            "codex_s_cell": codex_s_cell,
            "next_reset": next_reset_cell,
            "restore": duration(row.get("restore_at"), now, days=False),
            "sub_until": sub_until(sub_ts),
            "sub_left": sub_left(sub_ts, now),
            "sub_src": sub_src_cell,
            "will_renew": will_renew_cell,
            "state_sub_until": sub_until(row.get("subscription_active_until")),
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


STALE_PROBE_ALERT_S = 6 * 3600  # 引擎 manual_offline 重探间隔；超过即判定可能已过期


def serving_plane_diagnostics(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """服务平面故障。2026-08-02 新增 —— 在此之前表上完全没有这个平面。

    触发这一段存在的实证：acct-121 表上 `tier=HEALTHY / 7d=3% / 订阅到 8/25`，
    同一时刻 `deploy 0/1 READY, AVAILABLE=0`，pod 日志每 5min 报
    `refresh token failed: 401 auth.openai.com/oauth/token` 并打印设备码。
    用户问「明明有问题为什么检查不出来」，答案就是这里少了一整个平面。

    `serving_dead_upstream_ok` 是最毒的一格：上游全绿所以引擎不会 scale=0、
    preflight 不拦，号一直挂在池子里当黑洞（Ready=False 比 scale=0 更毒）。
    """
    return {
        # 上游绿但一个请求都服务不了 —— 表在改之前会把这些印成 HEALTHY
        "serving_dead_upstream_ok": [
            {"acct": r["acct"], "ready": r["ready"], "refresh_err": r["refresh_err"],
             "upstream_tier": r["upstream_tier"], "probe_age": r["probe_age"]}
            for r in rows
            if r["verdict"] == "SERVING_DEAD" and r["upstream_tier"] == "HEALTHY"
        ],
        # token 刷新已断：现在还能服务，但每 5min 报一次 401，早晚掉
        "refresh_dying": [
            {"acct": r["acct"], "ready": r["ready"], "refresh_err": r["refresh_err"],
             "auth_sync": r["auth_sync"], "pod_cred": r["pod_cred"]}
            for r in rows if (r["refresh_err"] or 0) > 0
        ],
        # Ready 在抖 —— 单点采样会随机给出健康或死亡两种结论
        "flapping": [
            {"acct": r["acct"], "ready": r["ready"], "refresh_err": r["refresh_err"]}
            for r in rows if r["verdict"] == "FLAPPING"
        ],
        # 被我误杀过的那个交集：pod 凭证长期没更新 ∧ 服务平面不健康。
        # 2026-08-02 实测 8 个 readyReplicas=0 的号 8/8 都落在 pod_cred 陈旧集合里
        # （召回 100%、精度 8/13）。当晚我拿 tier=HEALTHY 当对照组把它判成噪声了。
        "stale_cred_and_unhealthy": [
            {"acct": r["acct"], "pod_cred": r["pod_cred"], "ready": r["ready"],
             "verdict": r["verdict"]}
            for r in rows
            if r["verdict"] in ("SERVING_DEAD", "REFRESH_DYING", "FLAPPING")
            and r["pod_cred"] not in ("-",)
        ],
        # device-code 地狱：根因判据，与 ready/refresh_err 互补（缺一漏一半）
        "device_code_hell": [
            {"acct": r["acct"], "ready": r["ready"], "exp_gap": r["exp_gap"],
             "refresh_err": r["refresh_err"], "verdict": r["verdict"]}
            for r in rows if r["device_code"] == "yes"
        ],
        # exp_gap>0 = 从上线起从未成功 refresh 过一次（onboard 写死的 expires_at）
        "never_refreshed": [
            {"acct": r["acct"], "exp_gap": r["exp_gap"], "ready": r["ready"],
             "device_code": r["device_code"]}
            for r in rows if (r["exp_gap"] or 0) > 0
        ],
        # 服务平面没取到数 —— 必须报，否则空白会被读成"没问题"
        "serving_unknown": [
            {"acct": r["acct"], "upstream_tier": r["upstream_tier"]}
            for r in rows if r["verdict"] == "?"
        ],
    }


def print_serving_plane_diagnostics(rows: list[dict[str, Any]]) -> None:
    diag = serving_plane_diagnostics(rows)
    if d := diag["serving_dead_upstream_ok"]:
        print()
        print(f"✗✗ 上游绿但服务已死 ({len(d)}): deploy readyReplicas=0 —— 上游配额没问题"
              f"所以引擎不会 scale=0、preflight 也不拦，这些号挂在池子里当黑洞")
        for x in d:
            print(f"    {x['acct']}: ready={x['ready']} refresh_401={x['refresh_err']} "
                  f"upstream={x['upstream_tier']} probe_age={x['probe_age']}")
    if d := diag["refresh_dying"]:
        print()
        print(f"✗ token 刷新已断 ({len(d)}): pod 日志有 `refresh token failed` "
              f"(auth.openai.com/oauth/token 401) —— 需 re-OAuth/设备码，不会自愈")
        for x in d:
            print(f"    {x['acct']}: ready={x['ready']} 401x{x['refresh_err']} "
                  f"auth_sync={x['auth_sync']} pod_cred={x['pod_cred']}")
    if d := diag["flapping"]:
        print()
        print(f"⚠ Ready 抖动 ({len(d)}): 多次采样结果不一致 —— 单点采样会随机给出"
              f"「健康」或「已死」两种结论，别用一次快照下判定")
        for x in d:
            print(f"    {x['acct']}: ready={x['ready']} refresh_401={x['refresh_err']}")
    if d := diag["device_code_hell"]:
        print()
        print(f"✗✗ device-code 登录地狱 ({len(d)}): auth.json 有 device_code_requested_at"
              f" —— 每个请求都试 refresh→401→**同步阻塞**轮询，event loop 会被堵 6~17min，"
              f"readiness 跟着哑。表面像网络故障，实为凭证")
        for x in d:
            print(f"    {x['acct']}: verdict={x['verdict']} ready={x['ready']} "
                  f"exp_gap={x['exp_gap']}d refresh_401={x['refresh_err']}")
    if d := diag["never_refreshed"]:
        print()
        print(f"⚠ 从未成功 refresh 过 ({len(d)}): exp_gap>0（JWT exp 比 expires_at 晚）"
              f" —— onboard 写死 expires_at=issue+7d 而真 token 活 10d；"
              f"刷成功一次就永久自愈")
        for x in d:
            print(f"    {x['acct']}: exp_gap={x['exp_gap']}d ready={x['ready']} "
                  f"device_code={x['device_code']}")
    if d := diag["serving_unknown"]:
        print()
        print(f"⚠ 服务平面取数失败 ({len(d)}): verdict='?' —— 这些行的「能不能服务」未知，"
              f"不要当成没问题")
        for x in d:
            print(f"    {x['acct']}: upstream={x['upstream_tier']}")


def auth_plane_diagnostics(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """凭证平面三类故障 —— 三者原因不同，处置动作也不同，不能并成一句"需 re-OAuth"。

    2026-08-01 实证：表上 12 个 `401 需re-OAuth` 展开后是四组
      ①认证已进 PVC 只等 6h 探测窗口重测 ②认证没进 PVC（真故障）
      ③凭证确实过期（要重认证）④凭证未过期仍 401（要实跑 OAuth 判 deactivated）
    只报"12 个需 re-OAuth"会让人对 ①③④ 做同一个无效动作。
    """
    invalid = [r for r in rows if r["upstream_tier"] == "401 需re-OAuth"]
    # 只对 401 行报"认证没进 PVC"。HEALTHY 行同样可能 local↑（有人在 /Data 上重认证但
    # pod 那份还在用且好使），那不是故障 —— 对正在服务的号做 cp + rollout restart
    # 反而有害（违反零中断），所以不能进这个桶。
    not_in_pvc = [r for r in invalid if r["auth_sync"] == "local↑"]
    self_heal = [r for r in invalid if r.get("cred_after_verdict")]
    no_pod = [r for r in invalid if r["auth_sync"] == "no-pod"]
    handled = {r["acct"] for r in not_in_pvc} | {r["acct"] for r in self_heal} \
        | {r["acct"] for r in no_pod}
    return {
        # 认证落在 /Data 但没进 PVC —— 引擎只读 pod 副本，认证再多次也不会生效
        "auth_not_in_pvc": [
            {"acct": r["acct"], "pod_cred": r["pod_cred"],
             "repair_frozen": r["repair_frozen"],
             "repair_attempts": r["repair_attempts"]}
            for r in not_in_pvc
        ],
        # 引擎已放弃自动修复：认证成功也不会有人把它 cp 进 pod
        "repair_frozen": [
            {"acct": r["acct"], "repair_attempts": r["repair_attempts"],
             "auth_sync": r["auth_sync"], "pod_cred": r["pod_cred"]}
            for r in rows if r["repair_frozen"] and r["upstream_tier"] == "401 需re-OAuth"
        ],
        # 401 判定本身已陈旧 > 6h：可能你已重认证但还没被探到
        "verdict_stale": [
            {"acct": r["acct"], "probe_age": r["probe_age"],
             "auth_sync": r["auth_sync"], "pod_cred": r["pod_cred"]}
            for r in invalid
            if r.get("state_ts") and (time.time() - float(r["state_ts"])) > STALE_PROBE_ALERT_S
        ],
        # pod 凭证在判定之后才落地 → 判定是对旧凭证下的，下轮探测会**重测**。
        # 注意只能声称"会被重测"，不能声称"会翻绿" —— 凭证新 ≠ 账号活：
        # acct-107 于 2026-08-01 re-OAuth 明确返回 ACCOUNT_DEACTIVATED，
        # 其 pod 凭证事后仍被刷新过，照样落进这个桶。
        "verdict_predates_cred": [
            {"acct": r["acct"], "probe_age": r["probe_age"],
             "pod_cred": r["pod_cred"], "auth_sync": r["auth_sync"]}
            for r in self_heal
        ],
        # pod 内读不到 auth.json（pod 不存在 / PVC 空 / 未 ready）—— 不是凭证问题
        "no_pod_auth": [
            {"acct": r["acct"], "probe_age": r["probe_age"], "status": r["status"]}
            for r in no_pod
        ],
        # 排除以上全部之后剩下的 401：判定针对的就是当前这份 pod 凭证，且凭证没被更新过
        # → 只能实跑 refresh-grant 判活。**不能用 auth.json 的 expires_at 代替这一步**
        # （见 pod_cred_age 注释：9 个 HEALTHY 高流量号的 expires_at 也是过期的）
        "needs_live_probe": [
            {"acct": r["acct"], "pod_cred": r["pod_cred"], "probe_age": r["probe_age"]}
            for r in invalid if r["acct"] not in handled
        ],
    }


def print_auth_plane_diagnostics(rows: list[dict[str, Any]]) -> None:
    diag = auth_plane_diagnostics(rows)
    if d := diag["auth_not_in_pvc"]:
        print()
        print(f"✗ 认证没进 PVC ({len(d)}): /Data/chatgpt-auth 的凭证比 pod 内新 —— "
              f"引擎探测只读 pod:/chatgpt-auth/auth.json，**在 /Data 上认证多少次都不会生效**")
        for x in d:
            frozen = "，且 repair_frozen 引擎已放弃自动 cp" if x["repair_frozen"] else ""
            print(f"    {x['acct']}: pod 凭证已 {x['pod_cred']} 未更新, "
                  f"repair={x['repair_attempts']}/5{frozen} — 需手动 "
                  f"kubectl cp + rollout restart deployment/chatgpt-{x['acct']}")
    if d := diag["repair_frozen"]:
        print()
        print(f"🧊 repair_frozen ({len(d)}): repair_attempts 撞上限，引擎不再自动 re-OAuth/cp "
              f"—— 这些行的 401 不会自愈，必须人工介入并 reset repair_frozen")
        for x in d:
            print(f"    {x['acct']}: attempts={x['repair_attempts']}, "
                  f"auth_sync={x['auth_sync']}, pod 凭证龄={x['pod_cred']}")
    if d := diag["verdict_stale"]:
        print()
        print(f"⚠ 401 判定已陈旧 ({len(d)}): probe_age > 6h（引擎对 manual_offline 每 6h 才重探一次，"
              f"其余 tick 全是 SKIP）—— 若你刚重认证过，这里的 401 是旧结论，等下一次探测")
        for x in d:
            print(f"    {x['acct']}: probe_age={x['probe_age']}, "
                  f"auth_sync={x['auth_sync']}, pod 凭证龄={x['pod_cred']}")
    if d := diag["verdict_predates_cred"]:
        print()
        print(f"↻ 判定将被重测 ({len(d)}): pod 凭证在 401 判定之后才落地 —— 现在这个 401 是对"
              f"旧凭证下的结论，下次探测（manual_offline 6h 一次）会重测，**不用再认证一遍**。"
              f"但凭证新 ≠ 账号活：acct-107 曾 re-OAuth 明确返回 ACCOUNT_DEACTIVATED，"
              f"其 pod 凭证事后仍被刷新过 —— 重测结果可能仍是 401")
        for x in d:
            print(f"    {x['acct']}: probe_age={x['probe_age']}, "
                  f"pod 凭证 {x['pod_cred']} 前刚更新 (auth_sync={x['auth_sync']})")
    if d := diag["no_pod_auth"]:
        print()
        print(f"⚠ pod 内读不到凭证 ({len(d)}): pod 不存在 / PVC 空 / 未 ready —— "
              f"不是凭证问题，先确认 deploy 是否 scale=0")
        for x in d:
            print(f"    {x['acct']}: status={x['status']}, probe_age={x['probe_age']}")
    if d := diag["needs_live_probe"]:
        print()
        print(f"⚠ 需实跑 OAuth 判活 ({len(d)}): 判定针对的就是当前这份 pod 凭证、且凭证未被更新过 "
              f"—— 排除法剩下的只能实跑 refresh-grant 看 ACCOUNT_DEACTIVATED / 订阅失效。"
              f"注意 auth.json 的 expires_at **不能**代替这一步（9 个 HEALTHY 高流量号的 "
              f"expires_at 同样是过期的）。真号探测有代价，先确认再动")
        for x in d:
            print(f"    {x['acct']}: pod 凭证龄={x['pod_cred']}, probe_age={x['probe_age']}")


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


def render_table(state: dict[str, dict[str, Any]], *, summary: bool) -> int:
    now = time.time()
    pod_auth = remote_198_pod_auth()   # N 次 kubectl exec，取一次给 email + 凭证平面共用
    emails = email_map(pod_auth)
    local_auth = local_auth_plane()
    spend_recent = remote_198_spend_recent()
    zk = remote_198_zk_usage()
    serving_raw = remote_198_serving_plane()
    serving = serving_raw.get("accts") or {}
    live_raw = remote_198_live_usage()
    live = live_raw.get("accts") or {}
    live_fetched = bool(live_raw)
    if not live_fetched:
        print("[live] 实探链路整体失败 —— 本次 upstream_tier/7d%/reset **退回引擎快照**，"
              "live_probe 列全为 FETCH_FAIL，按快照口径解读", file=sys.stderr)
    rows = build_rows(state, now=now, emails=emails, spend_recent=spend_recent, zk=zk,
                      local_auth=local_auth, pod_auth=pod_auth, serving=serving,
                      live=live, live_fetched=live_fetched)
    ts = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = len(rows)
    src = "198:in-pod实探/codex/usage" if live_fetched else "198:state.json(实探失败)"
    print(f"=== BEGIN chatgpt-acct-quota @ {ts} | source={src} | rows={total} ===")
    print(f"legend: 行宇宙 = state.json 的 key ∪ deploy 全集 —— state 里有 {len(state)} 个、"
          f"表上 {total} 行，差额是引擎不认识的号（见下方诊断段，它们不参与配额轮转）")
    print("legend: 7d%/reset/upstream_tier = **in-pod 实探 chatgpt.com/backend-api/codex/usage**"
          "（2026-08-05 起）。引擎快照留在 state_tier 列并排；probe_age='live' 即本次实探")
    print("legend: 7D_CAP = 官方已拒(allowed=false)；7D_BRIM = 用满 100% 但官方仍放行 —— "
          "**两者不是一回事**，BRIM 还能服务，别一起下线")
    print("legend: live 列 = off(spec=0)/no-pod/live/401:<kind>/ERR:*/FETCH_FAIL —— "
          "空白与 0 语义不同，探不通一律显式标注")
    print("legend: reset/restore 列显示未来倒计时或 '-'；已过时刻由下一 cron tick 触发 auto-revive")
    print("legend: zk_* = zerokey 累计消耗；zk_lat 是端到端总时延（非首 token —— "
          "LiteLLM 未记录真 TTFT）")
    print("legend: auth = 凭证平面（引擎只读 pod:/chatgpt-auth/auth.json）。"
          "'local↑'=认证落在 /Data 没进 PVC，认证不会生效；'both_dead'=两份一致但都在报 "
          "refresh 401；pod_cred = pod 内凭证文件多久没更新")
    print(f"legend: verdict = 上游+服务两平面合成的**唯一总判定**。upstream_tier 只覆盖上游"
          f"配额（acct-121 曾在 upstream=HEALTHY 时 deploy 0/1 READY）。"
          f"ready='n/{SERVING_SAMPLES}' 采样 {SERVING_SAMPLES} 次、间隔 {SERVING_SAMPLE_GAP}s"
          f" —— 只抓得住秒级抖动，分钟级慢抖仍会显示 2/2，别把 2/2 读成「稳定」；"
          f"rfx = 近 {SERVING_LOG_SINCE} 内 pod 日志 refresh-401 条数（时间窗，稳定信号）")
    sv_ts = (datetime.fromtimestamp(serving_raw["sampled_at"], timezone.utc)
             .strftime("%H:%M:%S UTC") if serving_raw.get("sampled_at") else "取数失败")
    lv_ts = (datetime.fromtimestamp(live_raw["probed_at"], timezone.utc)
             .strftime("%H:%M:%S UTC") if live_raw.get("probed_at") else "取数失败")
    print(f"legend: 服务平面采样时刻 = {sv_ts}；上游实探时刻 = {lv_ts}"
          f"（与表头时刻差几十秒；ready 是会抖的量）")
    print(
        f"{'acct':9s} {'email':32s} {'take':>4s} {'status':>9s} "
        f"{'verdict':>16s} {'ready':>5s} {'rfx':>3s} {'upstream_tier':>16s} "
        f"{'live':>18s} {'state_tier':>16s} "
        f"{'age':>7s} {'auth':>9s} {'pod_cred':>8s} "
        f"{'7d%':>5s} {'spark':>5s} {'reset':>12s} "
        f"{'main_n':>7s} {'main$':>7s} {'codex_n':>8s} {'codex$':>7s} "
        f"{'zk_n':>6s} {'zk$':>8s} {'zk_n7':>6s} {'zk$7':>7s} "
        f"{'lat_avg':>7s} {'lat_p95':>7s} "
        f"{'next_reset':>12s} {'restore':>9s} {'sub_until':>20s} {'sub_left':>8s}  cause"
    )
    print("-" * 360)
    for r in rows:
        def num(v: Any, fmt: str) -> str:
            return format(v, fmt) if v is not None else "-"
        print(
            f"{r['acct']:9s} {r['email'] or '-':32s} {r['take']:>4s} {r['status']:>9s} "
            f"{r['verdict']:>16s} {r['ready']:>5s} "
            f"{('-' if r['refresh_err'] is None else str(r['refresh_err'])):>3s} "
            f"{r['upstream_tier']:>16s} "
            f"{r['live_probe']:>18s} {r['state_tier']:>16s} "
            f"{r['probe_age']:>7s} {r['auth_sync']:>9s} {r['pod_cred']:>8s} "
            f"{r['pct7d_cell']:>5s} "
            f"{('-' if r['spark_pct'] is None else str(r['spark_pct'])):>5s} "
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
    print_live_plane_diagnostics(rows)
    print_serving_plane_diagnostics(rows)
    print_auth_plane_diagnostics(rows)

    # probe-stale 判据改吃**实探** 7d%：原来比的是 state.primary_pct，而那本来就是
    # 快照，"快照≈0 但有流量"多半只是快照没跟上，不是异常。实探≈0 且 24h 有真流量
    # 才是矛盾（同一时刻两个平面互相打脸）。
    stale = [
        r["acct"] for r in rows
        if r["live_probe"] == "live" and (r["main_n"] or 0) >= 50
        and (r["pct7d"] or 0) < 5
    ]
    if stale:
        print()
        print(f"⚠ 实探 7d%<5 但 24h 流量≥50 calls ({len(stale)}): 上游说几乎没用量、"
              f"LiteLLM 说跑了几百次 —— 两个平面矛盾，别只信一边 → "
              f"{sorted(stale, key=acct_sort_key)}")

    codex_total_calls = sum((r["codex_n"] or 0) for r in rows)
    codex_total_spend = sum((r["codex_spend"] or 0.0) for r in rows)
    codex_total_calls7 = sum((r.get("codex_n7") or 0) for r in rows)
    codex_total_spend7 = sum((r.get("codex_spend7") or 0.0) for r in rows)
    if codex_total_calls:
        codex_active = [r["acct"] for r in rows if (r["codex_n"] or 0) > 0]
        print()
        print(f"ⓘ codex (gpt-5.3) 独立配额池 24h: "
              f"{codex_total_calls} calls / ${codex_total_spend:.1f} "
              f"(7d: {codex_total_calls7} calls / ${codex_total_spend7:.1f}), "
              f"active={len(codex_active)} {sorted(codex_active, key=acct_sort_key)}")

    zombies = [r["acct"] for r in rows if r["status"] == "ZOMBIE"]
    token_bad = [r["acct"] for r in rows if r["upstream_tier"] == "401 需re-OAuth"]
    sub_expired = [r["acct"] for r in rows if r["sub_left"] == "expired"]
    if zombies or token_bad or sub_expired:
        print()
        print("✗ 不健康账号汇总（需人工处置）")
        if zombies:
            print(f"  ZOMBIE         ({len(zombies)}): {sorted(zombies, key=acct_sort_key)} "
                  f"— state 残留无 probe 数据；deploy scale=0 + router 已清；建议清行 + 删 deploy")
        if token_bad:
            print(f"  官方实探 401   ({len(token_bad)}): {sorted(token_bad, key=acct_sort_key)} "
                  f"— 别对这批做同一个动作：先看上面 live_401 的 body 指纹分类"
                  f"（token_expired / invalidated / could_not_parse / no_access_token）"
                  f"与 auth-plane 分组（认证没进 PVC / repair_frozen / 需实跑 OAuth 判活）")
        if sub_expired:
            print(f"  SUB_EXPIRED    ({len(sub_expired)}): {sorted(sub_expired, key=acct_sort_key)} "
                  f"— sub_until 已过；订阅周期结束（27d reset 是周期残留），需续订或删除")

    if not summary:
        return total
    takers = [r["acct"] for r in rows if r["take"] == "yes"]
    online = [r["acct"] for r in rows if r["status"] == "ONLINE"]
    paused = [r["acct"] for r in rows if r["status"] == "PAUSED"]
    offline = [r["acct"] for r in rows if r["status"] == "OFFLINE"]
    zombie = [r["acct"] for r in rows if r["status"] == "ZOMBIE"]
    untracked = [r["acct"] for r in rows if r["status"] == "UNTRACKED"]
    sort = lambda items: sorted(items, key=acct_sort_key)
    print()
    print(f"take     ={len(takers):2d}  {sort(takers)}")
    print(f"online   ={len(online):2d}  {sort(online)}")
    print(f"paused   ={len(paused):2d}  {sort(paused)} (7d quota pause)")
    print(f"offline  ={len(offline):2d}  {sort(offline)} (manual_offline)")
    if zombie:
        print(f"zombie   ={len(zombie):2d}  {sort(zombie)} (state placeholder; no probe data — likely deploy scale=0 + router cleared)")
    if untracked:
        print(f"untracked={len(untracked):2d}  {sort(untracked)} "
              f"(集群有 deploy 但 state.json 无此 key —— 引擎不探测不轮转)")
    return total


def render_json(state: dict[str, dict[str, Any]]) -> None:
    """结构化输出，供 chatgpt_acct_quota_to_lark.py 消费（取代固定宽度反解）。"""
    now = time.time()
    zk = remote_198_zk_usage()
    pod_auth = remote_198_pod_auth()
    serving_raw = remote_198_serving_plane()
    serving = serving_raw.get("accts") or {}
    live_raw = remote_198_live_usage()
    live = live_raw.get("accts") or {}
    live_fetched = bool(live_raw)
    if not live_fetched:
        print("[live] 实探链路整体失败 —— upstream_tier/7d%/reset **退回引擎快照**，"
              "live_probe 列全为 FETCH_FAIL", file=sys.stderr)
    rows = build_rows(
        state,
        now=now,
        emails=email_map(pod_auth),
        spend_recent=remote_198_spend_recent(),
        zk=zk,
        local_auth=local_auth_plane(),
        pod_auth=pod_auth,
        serving=serving,
        live=live,
        live_fetched=live_fetched,
    )
    print(json.dumps({
        "generated_at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": ("198:in-pod实探 chatgpt.com/backend-api/codex/usage"
                   if live_fetched else "198:state.json(实探链路失败,退回快照)")
                  + " + LiteLLM_SpendLogs + pod:/chatgpt-auth/auth.json"
                  + " + deploy.readyReplicas + pod.log(refresh 401)",
        "live_fetched": live_fetched,
        "live_probed_at": (
            datetime.fromtimestamp(live_raw["probed_at"], timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S UTC")
            if live_raw.get("probed_at") else None
        ),
        "state_keys": len(state),
        # 服务平面的采样时刻必须单独带出来：它和 generated_at 差几十秒，而 ready
        # 是会抖的量 —— 读者得知道这一格是什么时候的
        "serving_sampled_at": (
            datetime.fromtimestamp(serving_raw["sampled_at"], timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S UTC")
            if serving_raw.get("sampled_at") else None
        ),
        "serving_samples": SERVING_SAMPLES,
        "rows": rows,
        "diagnostics": {
            **zk_diagnostics(rows, zk),
            **live_plane_diagnostics(rows),
            **auth_plane_diagnostics(rows),
            **serving_plane_diagnostics(rows),
        },
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
    total = render_table(state, summary=args.summary)
    # 行数必须与 BEGIN frame 一致 —— wrapper 的自检拿 BEGIN/END/数据行三者对比，
    # 三者任一不等即判输出被截断。行宇宙改成 state ∪ deploy 之后，这里不能再用
    # len(state)（2026-08-05 起两者不再相等）。
    print(f"=== END chatgpt-acct-quota | rows={total} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
