#!/usr/bin/env python3
"""
zerokey-pool-health-monitor.py — 阿里云原生 zerokey 网页池健康监控(边沿触发飞书告警)

监控 chatgpt-gpt-5.5 轮询组里的阿里云 zerokey serve 成员
(model_id zk-aliyun-N, serve pod zerokey-serve-N)。**零 LLM 调用、零 ChatGPT
token 成本**:只读 K8s pod/job 状态 + serve 的 /health 存活端点(返回进程 uptime,
不打 ChatGPT)。

## 为什么这么设计(实测依据,勿改回打真 ping)

- serve `/health` 只报进程 uptime,**不校验 session 令牌**(令牌死了也返回 200)。
  所以 /health 只用于 liveness(抓 serve 崩溃/CrashLoop,如 acct-69 restarts=2215)。
- 令牌两层:请求头 Bearer accessToken 是 10 天短令牌,但真凭证是 ~30 天滚动的
  __Secure-next-auth.session-token cookie。accessToken 到期由 capture 浏览器用
  cookie 自动换新 —— **这是正常自愈,不是故障**。别按"令牌同日过期"告警。
- 真正的故障 = session cookie 彻底失效。零 token 的领先信号:
    1. serve 容器 restartCount 飙升 + ready=False(CrashLoop)
    2. serve 日志出现 `token_invalidated` / `Sentinel 401`
    3. capture Job 连续 Failed(capture 日志 `refusing to capture anonymous session`)
  acct-69 三者齐现 = 教科书死法。任一出现即 cookie 死,靠 6h capture 自愈救不回,
  需人工重新登录(邮箱 OTP)。

## 病症分类(告警文案直接说该不该人工介入)

- DOWN      : serve /health 连不上或非 200 / 容器 CrashLoop → serve 挂了
- COOKIE_DEAD: serve 日志 token_invalidated/Sentinel 401 或 capture 连续 Failed
              → session cookie 死了,**需人工重登**(像 acct-69)
- STALE      : capture 最近一次 Failed(未连续)/ users.json 长时间未刷新
              → 刷新链路异常,观察,暂不用动手
- OK         : /health 200 + 容器 ready + capture 最近 Complete

## 边沿触发

上轮每个成员的状态存 ConfigMap `zerokey-pool-monitor-state`(annotations)。
只有 状态发生变化(OK→坏 或 坏→OK)才发飞书,平时静默不刷屏。首轮建立基线
(--announce-baseline 才在首轮把当前非 OK 的也报一次)。

## 用法

    # dry-run:只打印,不发飞书、不写 state configmap(上生产前先看输出)
    python3 scripts/zerokey-pool-health-monitor.py

    # 真跑(发飞书 + 写 state):
    FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx \
      python3 scripts/zerokey-pool-health-monitor.py --apply

环境变量:
    FEISHU_WEBHOOK       飞书自定义机器人 webhook(--apply 时必需;缺失则只打印)
    NS                   命名空间(默认 carher)
    KUBECTL              kubectl 路径(默认 kubectl)
    STALE_HOURS          users.json 超过 N 小时未刷新判 STALE(默认 8,= 6h cron + 裕量)
    HEALTH_TIMEOUT       /health 探针超时秒(默认 8)

在集群内(CronJob)跑:SA 需 pods,pods/log,jobs: get,list + 对 state configmap
get,create,patch。CronJob 需 hostNetwork 钉 EIP 节点才连得上 serve hostPort。
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

NS = os.environ.get("NS", "carher")
KUBECTL = os.environ.get("KUBECTL", "kubectl")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
STALE_HOURS = float(os.environ.get("STALE_HOURS", "8"))
HEALTH_TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT", "8"))
STATE_CM = "zerokey-pool-monitor-state"
POOL_LABEL = "pool=zerokey"
SERVE_PORT_BASE = 8100          # serve hostPort = 8100 + acct number
LOG_SCAN_LINES = 40            # serve/capture 日志尾部扫描行数

# 病症严重度(用于选 emoji / 排序)
SEV = {"OK": 0, "STALE": 1, "DOWN": 2, "COOKIE_DEAD": 3}
EMOJI = {"OK": "✅", "STALE": "🟡", "DOWN": "🔴", "COOKIE_DEAD": "⛔"}


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f"[{now_utc().strftime('%H:%M:%S')}] {msg}", flush=True)


def kubectl_json(args):
    """kubectl <args> -o json → dict。失败抛异常。"""
    out = subprocess.run(
        [KUBECTL, "-n", NS, *args, "-o", "json"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def kubectl_logs(pod, tail=LOG_SCAN_LINES):
    out = subprocess.run(
        [KUBECTL, "-n", NS, "logs", pod, "--tail", str(tail)],
        capture_output=True, text=True, timeout=40,
    )
    # CrashLoop pod 当前容器可能无日志;回退上一个容器
    if out.returncode != 0 or not out.stdout.strip():
        prev = subprocess.run(
            [KUBECTL, "-n", NS, "logs", pod, "-p", "--tail", str(tail)],
            capture_output=True, text=True, timeout=40,
        )
        return (out.stdout or "") + (prev.stdout or "")
    return out.stdout


# ── 发现 serve 成员 ───────────────────────────────────────────────

def discover_members():
    """返回 [{acct, pod, host_ip, port, restarts, ready, phase}] 按 acct 排序。
    仅取 zerokey-serve-* pod(排除 capture)。"""
    d = kubectl_json(["get", "pods", "-l", POOL_LABEL])
    members = []
    for p in d.get("items", []):
        name = p["metadata"]["name"]
        if "serve" not in name:
            continue
        # zerokey-serve-<N>-<hash>
        parts = name.split("-")
        if len(parts) < 3 or not parts[2].isdigit():
            continue
        acct = int(parts[2])
        cs = (p.get("status", {}).get("containerStatuses") or [{}])[0]
        members.append({
            "acct": acct,
            "pod": name,
            "host_ip": p.get("status", {}).get("hostIP", ""),
            "port": SERVE_PORT_BASE + acct,
            "restarts": cs.get("restartCount", 0),
            "ready": cs.get("ready", False),
            "phase": p.get("status", {}).get("phase", "?"),
        })
    return sorted(members, key=lambda m: m["acct"])


def capture_job_history(acct, want=3):
    """返回该 acct 最近 want 次 capture Job 的结果列表(旧→新),
    取值 Failed/Complete/Running。零 ChatGPT 调用。"""
    d = kubectl_json(["get", "jobs"])
    rows = []
    prefix = f"zerokey-capture-{acct}-"
    for j in d.get("items", []):
        n = j["metadata"]["name"]
        if not n.startswith(prefix):
            continue
        ts_str = n[len(prefix):]
        ts = int(ts_str) if ts_str.isdigit() else 0
        conds = j.get("status", {}).get("conditions", [])
        if any(c.get("type") == "Failed" and c.get("status") == "True" for c in conds):
            res = "Failed"
        elif any(c.get("type") == "Complete" and c.get("status") == "True" for c in conds):
            res = "Complete"
        else:
            res = "Running"
        rows.append((ts, res))
    rows.sort()
    return [r for _, r in rows[-want:]]


# ── 探针 + 日志信号(全部零 ChatGPT token)────────────────────────

def probe_health(host_ip, port):
    """serve /health(进程 uptime,不打 ChatGPT)。返回 (ok, detail)。"""
    url = f"http://{host_ip}:{port}/health"
    try:
        r = urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT)
        body = r.read(300).decode("utf-8", "ignore")
        if r.status == 200 and '"healthy"' in body:
            return True, "healthy"
        return False, f"HTTP {r.status} {body[:80]}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"unreachable ({type(e).__name__})"


COOKIE_DEAD_SIGS = ("token_invalidated", "Sentinel 401", "authentication token has been invalidated")
ANON_SIG = "refusing to capture anonymous"


def serve_log_signal(pod):
    """扫 serve 日志尾部找 cookie-death 签名。返回匹配到的签名或 None。"""
    try:
        txt = kubectl_logs(pod)
    except Exception:
        return None
    for sig in COOKIE_DEAD_SIGS:
        if sig in txt:
            return sig
    return None


def users_json_age_hours(pod):
    """serve pod 里 /state/users.json 的 mtime 距今小时数。取不到返回 None。"""
    out = subprocess.run(
        [KUBECTL, "-n", NS, "exec", pod, "--", "stat", "-c", "%Y", "/state/users.json"],
        capture_output=True, text=True, timeout=30,
    )
    line = "".join(l for l in out.stdout.splitlines() if l.strip().isdigit())
    if out.returncode != 0 or not line:
        return None
    mtime = int(line)
    return (time.time() - mtime) / 3600.0


# ── 综合判定 ─────────────────────────────────────────────────────

def classify(m):
    """对单个成员判定病症,返回 (status, detail)。"""
    acct, pod = m["acct"], m["pod"]

    # 1) 容器层:CrashLoop / 未就绪 → DOWN(高 restart 叠加 cookie 死日志 → COOKIE_DEAD)
    crashloopy = (not m["ready"]) or m["restarts"] >= 5
    log_sig = serve_log_signal(pod) if crashloopy else None
    if log_sig:
        return "COOKIE_DEAD", f"{log_sig}; restarts={m['restarts']} ready={m['ready']}"
    if crashloopy:
        return "DOWN", f"container not ready; restarts={m['restarts']} phase={m['phase']}"

    # 2) 存活探针(零 ChatGPT)
    ok, detail = probe_health(m["host_ip"], m["port"])
    if not ok:
        # 探针失败但容器 ready:可能刚崩,扫日志确认是否 cookie 死
        sig = serve_log_signal(pod)
        if sig:
            return "COOKIE_DEAD", f"{sig}; /health {detail}"
        return "DOWN", f"/health {detail}"

    # 3) capture 刷新链路(领先信号)
    hist = capture_job_history(acct)
    if hist and hist[-1] == "Failed":
        # 连续 2+ 次 Failed → cookie 大概率死(anonymous)
        if len(hist) >= 2 and hist[-2] == "Failed":
            return "COOKIE_DEAD", f"capture consecutive Failed {hist}"
        return "STALE", f"capture last Failed {hist}"

    # 4) users.json 新鲜度(兜底:cron 若彻底不跑,job 可能都不在了)
    age = users_json_age_hours(pod)
    if age is not None and age > STALE_HOURS:
        return "STALE", f"users.json {age:.1f}h old (>{STALE_HOURS}h)"

    return "OK", f"health ok, capture {hist or '?'}"


# ── 状态存储(ConfigMap,边沿触发)────────────────────────────────

def load_prev_state():
    try:
        d = kubectl_json(["get", "configmap", STATE_CM])
        return json.loads((d.get("data") or {}).get("state", "{}"))
    except Exception:
        return {}


def save_state(state, apply):
    if not apply:
        return
    payload = {"data": {"state": json.dumps(state),
                        "updated": now_utc().isoformat()}}
    # 存在则 patch,不存在则 create
    exists = subprocess.run(
        [KUBECTL, "-n", NS, "get", "configmap", STATE_CM],
        capture_output=True, text=True, timeout=30,
    ).returncode == 0
    if exists:
        subprocess.run(
            [KUBECTL, "-n", NS, "patch", "configmap", STATE_CM,
             "--type", "merge", "-p", json.dumps(payload)],
            capture_output=True, text=True, timeout=30,
        )
    else:
        subprocess.run(
            [KUBECTL, "-n", NS, "create", "configmap", STATE_CM,
             f"--from-literal=state={json.dumps(state)}"],
            capture_output=True, text=True, timeout=30,
        )


# ── 飞书 ─────────────────────────────────────────────────────────

def alert_feishu(text, apply):
    if not apply:
        log("DRY-RUN 飞书告警(未发送):\n" + text)
        return
    if not FEISHU_WEBHOOK or FEISHU_WEBHOOK.startswith("stub"):
        log("⚠ FEISHU_WEBHOOK 未设置,跳过发送。告警内容:\n" + text)
        return
    try:
        body = {"msg_type": "text", "content": {"text": text}}
        req = urllib.request.Request(
            FEISHU_WEBHOOK, method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
        log("飞书告警已发送")
    except Exception as e:
        log(f"飞书发送失败: {e}")


ADVICE = {
    "COOKIE_DEAD": "session cookie 已失效,6h capture 自愈救不回 → 需人工重新登录(邮箱 OTP)",
    "DOWN": "serve 进程不可达/崩溃 → 查 pod 日志与节点",
    "STALE": "刷新链路异常,先观察下一轮 capture 是否恢复,暂不用动手",
    "OK": "已恢复正常",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="真发飞书 + 写 state configmap(默认 dry-run 只打印)")
    ap.add_argument("--announce-baseline", action="store_true",
                    help="首轮(无历史 state)时也把当前非 OK 成员报一次")
    args = ap.parse_args()

    log(f"zerokey 池监控启动 ns={NS} apply={args.apply}")
    members = discover_members()
    if not members:
        log("未发现 zerokey serve 成员(-l pool=zerokey),退出")
        sys.exit(0)

    prev = load_prev_state()
    first_run = not prev
    cur = {}
    transitions = []       # (acct, old, new, detail)

    for m in members:
        acct = str(m["acct"])
        status, detail = classify(m)
        cur[acct] = status
        old = prev.get(acct)
        emoji = EMOJI[status]
        log(f"  {emoji} acct{acct:<3} {status:<12} {detail}")

        changed = (old is not None and old != status) or \
                  (old is None and status != "OK" and (args.announce_baseline or not first_run))
        if changed:
            transitions.append((acct, old or "NEW", status, detail))

    # 组装边沿告警(只报变化)
    if transitions:
        lines = ["🛰 CarHer 阿里云 zerokey 网页池状态变化"]
        for acct, old, new, detail in sorted(
                transitions, key=lambda t: -SEV.get(t[2], 0)):
            lines.append(f"{EMOJI[new]} acct{acct}: {old} → {new}")
            lines.append(f"   {detail}")
            lines.append(f"   → {ADVICE.get(new, '')}")
        # 附当前整体健康快照
        bad = [a for a, s in cur.items() if s != "OK"]
        lines.append(f"当前:{len(cur)-len(bad)}/{len(cur)} 健康" +
                     (f",异常 acct {','.join(bad)}" if bad else ""))
        alert_feishu("\n".join(lines), args.apply)
    else:
        bad = [a for a, s in cur.items() if s != "OK"]
        log(f"无状态变化({len(cur)-len(bad)}/{len(cur)} 健康),不发告警")

    save_state(cur, args.apply)
    log("完成")


if __name__ == "__main__":
    main()
