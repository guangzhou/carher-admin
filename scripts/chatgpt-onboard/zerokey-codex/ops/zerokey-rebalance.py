#!/usr/bin/env python3
"""
zerokey-rebalance.py — zerokey-pool 健康自愈调度器 (v2 架构 P1)

部署位置：188 主机 crontab（每 5min）。脚本在 188 上运行，可以：
  - 本地探测 6 个 zerokey 桥端口（127.0.0.1:8123-8128）
  - 本地读取每个账号的 state/REFRESH_STALE 标记（refresh.sh 写）
  - 本地触发 <dir>/ops/refresh.sh 重新抓取会话（可选，REFRESH=1）
  - 通过 198 NodePort 调 LiteLLM /model/new、/model/delete 管理 zerokey-pool

与 acct quota-rebalance.py 的本质区别（zerokey 没有 /codex/usage 配额 API）：
  - acct：读 auth.json → /codex/usage → 5h/7d 百分比 → 撞限下线
  - zerokey：无配额数值。健康信号 = (a) 端口可达 (b) REFRESH_STALE 标记
    (c) 可选真实 1-token chat 深探（消耗极小 web 额度，默认关闭）

健康分级（每端口独立）：
  ┌────────────────────────────────────────────────────────────────┐
  │ 信号                                  │ 分级           │ 动作    │
  ├────────────────────────────────────────────────────────────────┤
  │ /v1/models 不可达 (conn refused/超时) │ DEAD_CONTAINER │ 下线    │
  │ REFRESH_STALE 标记存在                │ DEAD_SESSION   │ 下线+刷 │
  │ 深探 401/403                          │ DEAD_SESSION   │ 下线+刷 │
  │ 深探 429                              │ RATE_LIMITED   │ 保留*   │
  │ 深探 5xx/超时                         │ DEGRADED       │ 保留*   │
  │ 深探 200 / 不深探且端口可达           │ HEALTHY        │ 上线    │
  └────────────────────────────────────────────────────────────────┘
  * RATE_LIMITED / DEGRADED 保留在池里，交给 LiteLLM 自身的 rpm 上限 +
    429 cooldown(默认 5s) 做短期节流，不物理摘除（避免抖动雪崩）。

防抖：连续 DEAD 达 CONSECUTIVE_DEAD_THRESHOLD 次才 /model/delete（端口探测在
本机极廉价，阈值设 2 足够过滤瞬态）。HEALTHY 立即补回（低风险）。

幂等：每个端口对应固定 model_info.id = "zk-pool-<port>"。reconcile 只比较
"期望在池(healthy 端口)" vs "实际在池(DB 里 zk-pool-* id)"，加缺失、删多余。
zerokey-pool 必须是 DB-managed（cm 里不能再有 zerokey-pool 块，否则重复）。

环境变量：
  LITELLM_BASE   默认 http://10.68.13.198:30400   (dev NodePort；prod 换 30402)
  LITELLM_MK     该环境 master key (必填)
  POOL_MODEL     默认 zerokey-pool
  RPM_PER_ACCT   每账号 rpm 上限/权重，默认 10
  DEEP_PROBE     =1 对端口可达且无 STALE 的账号发真实 1-token chat 验证会话
  DEEP_MIN       每账号深探最小间隔秒，默认 300 (5min)
  DEEP_MAX       每账号深探最大间隔秒，默认 600 (10min)
                 —— 每个账号独立随机抽 [DEEP_MIN,DEEP_MAX]，互相错开，
                    不再 6 号齐刷（避免被识别为扫描特征）
  DEEP_PRESLEEP  深探前秒级随机抖动上限，默认 30，防同 tick 到期撞同秒
  REFRESH        =1 对 DEAD_SESSION 触发 refresh.sh（默认 0，避免误触 OTP 重抓）
  REVIVE         =1 对 DEAD_CONTAINER 触发 docker start（默认 0）
  FEISHU_WEBHOOK 边沿告警 webhook（状态切换才发）
  REBALANCE_JITTER 启动随机抖动上限秒，默认 60
  DRY_RUN        =1 只打印计划不改 LiteLLM / 不触发 refresh
"""
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ---- 188 zerokey 桥队列：port → 账号元数据 ----
# dir   : 账号根目录（state/REFRESH_STALE、ops/refresh.sh 都在 dir 下）
# cont  : docker 容器名（REVIVE 用）
HOME = os.path.expanduser("~")
POOL_ACCOUNTS = {
    8123: {"name": "kristine", "dir": f"{HOME}/zerokey-codex",                 "cont": "zerokey-codex"},
    8124: {"name": "timothy",  "dir": f"{HOME}/zerokey-codex-accounts/timothy", "cont": "zerokey-codex-timothy"},
    8125: {"name": "zyq",      "dir": f"{HOME}/zerokey-codex-accounts/zyq",     "cont": "zerokey-codex-zyq"},
    8126: {"name": "owp",      "dir": f"{HOME}/zerokey-codex-accounts/owp",     "cont": "zerokey-codex-owp"},
    8127: {"name": "hgg",      "dir": f"{HOME}/zerokey-codex-accounts/hgg",     "cont": "zerokey-codex-hgg"},
    8128: {"name": "dvo",      "dir": f"{HOME}/zerokey-codex-accounts/dvo",     "cont": "zerokey-codex-dvo"},
}
# 198 从 188 拉的地址（LiteLLM 用，必须是 188 内网 IP 不是 127.0.0.1）
UPSTREAM_HOST = os.environ.get("ZK_UPSTREAM_HOST", "10.68.13.188")
UPSTREAM_SLUG = "gpt-5-5"  # zerokey 桥对外暴露的 model slug

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30400").rstrip("/")
LITELLM_MK = os.environ.get("LITELLM_MK", "")
POOL_MODEL = os.environ.get("POOL_MODEL", "zerokey-pool")
RPM_PER_ACCT = int(os.environ.get("RPM_PER_ACCT", "10"))
DEEP_PROBE = os.environ.get("DEEP_PROBE", "") == "1"
DEEP_MIN = int(os.environ.get("DEEP_MIN", "300"))
DEEP_MAX = int(os.environ.get("DEEP_MAX", "600"))
DEEP_PRESLEEP = int(os.environ.get("DEEP_PRESLEEP", "30"))
REFRESH = os.environ.get("REFRESH", "") == "1"
REVIVE = os.environ.get("REVIVE", "") == "1"
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
JITTER_MAX = int(os.environ.get("REBALANCE_JITTER", "60"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

STATE_DIR = os.environ.get("ZK_STATE_DIR", f"{HOME}/.zerokey-rebalance/state")
STATE_FILE = f"{STATE_DIR}/state.json"

CONSECUTIVE_DEAD_THRESHOLD = int(os.environ.get("CONSECUTIVE_DEAD_THRESHOLD", "2"))
REFRESH_RETRY_INTERVAL = int(os.environ.get("REFRESH_RETRY_INTERVAL", str(3 * 3600)))
REFRESH_TIMEOUT = int(os.environ.get("REFRESH_TIMEOUT", "330"))
PROBE_TIMEOUT = 6
DEEP_TIMEOUT = 25

# 保留在池中的健康分级
KEEP_STATES = {"HEALTHY", "RATE_LIMITED", "DEGRADED"}
# 需要摘除的分级
DROP_STATES = {"DEAD_CONTAINER", "DEAD_SESSION"}


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def now_ts():
    return int(time.time())


def load_state():
    try:
        text = Path(STATE_FILE).read_text().strip()
        if text:
            return json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_state(state):
    Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))


def pool_id(port):
    return f"zk-pool-{port}"


# ---- 健康探测 ----

def port_reachable(port):
    """本机探 /v1/models —— 只证明容器/进程活着（不证明会话有效）。"""
    url = f"http://127.0.0.1:{port}/v1/models"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError as e:
        # 4xx 仍说明端口在监听（容器活）
        return e.code < 500
    except Exception:
        return False


def stale_marker(meta):
    return Path(meta["dir"]) / "state" / "REFRESH_STALE"


def has_stale(meta):
    return stale_marker(meta).exists()


def deep_probe(port):
    """对端口发一条真实 1-token chat，验证 ChatGPT 会话是否还活。
    返回 (state, detail)。消耗极小 web 额度，按 DEEP_INTERVAL 节流。
    """
    body = json.dumps({
        "model": UPSTREAM_SLUG,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body,
        headers={"Authorization": "Bearer raw", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEEP_TIMEOUT) as r:
            if r.status == 200:
                return "HEALTHY", "deep 200"
            return "DEGRADED", f"deep {r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "DEAD_SESSION", f"deep {e.code}"
        if e.code == 429:
            return "RATE_LIMITED", "deep 429"
        if e.code >= 500:
            return "DEGRADED", f"deep {e.code}"
        return "DEGRADED", f"deep {e.code}"
    except Exception as e:
        return "DEGRADED", f"deep {type(e).__name__}"


def classify(port, meta, state):
    """综合分级。深探按"每账号独立随机 [DEEP_MIN,DEEP_MAX]"调度，互相错开。

    端口可达探测(port_reachable)打 127.0.0.1 不碰 chatgpt，零额度，每轮都做；
    只有深探(deep_probe)真打 chatgpt.com，故只对深探做随机错开 + 秒级抖动。
    """
    if not port_reachable(port):
        return "DEAD_CONTAINER", "port unreachable"
    if has_stale(meta):
        return "DEAD_SESSION", "REFRESH_STALE marker"
    if DEEP_PROBE:
        s = state.get(str(port), {})
        now = now_ts()
        next_deep = s.get("next_deep_at")
        if next_deep is None:
            # 首次：在 [0,DEEP_MAX] 随机错开起点，避免 6 号同时进入深探节奏
            s["next_deep_at"] = now + random.randint(0, DEEP_MAX)
            state[str(port)] = s
            return "HEALTHY", f"port ok (deep scheduled +{(s['next_deep_at']-now)//60}min)"
        if now >= next_deep:
            # 秒级抖动：即便两个号恰好同轮到期，也不在同一秒打 chatgpt
            if DEEP_PRESLEEP > 0:
                time.sleep(random.randint(0, DEEP_PRESLEEP))
            st, detail = deep_probe(port)
            # 打完立刻为该账号抽下一个独立随机间隔
            s["next_deep_at"] = now_ts() + random.randint(DEEP_MIN, DEEP_MAX)
            state[str(port)] = s
            return st, detail
        return "HEALTHY", f"port ok (deep in {(next_deep-now)//60}min)"
    return "HEALTHY", "port ok (no deep probe)"


# ---- LiteLLM API ----

def api_request(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    headers = {"Authorization": f"Bearer {LITELLM_MK}"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(LITELLM_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:300]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def current_pool_ids():
    """返回 LiteLLM 里属于本池的 DB 部署 id 集合 (zk-pool-*)。
    api_base 在 /model/info 里是加密的，所以只能靠我们写入的稳定 id 识别。
    返回 (ok, set_of_ids)。ok=False 时调用方应跳过本轮变更（防 5xx 误删）。
    """
    status, data = api_request("GET", "/v1/model/info")
    if status != 200 or not isinstance(data, dict):
        return False, set()
    ids = set()
    for e in data.get("data", []):
        if e.get("model_name") != POOL_MODEL:
            continue
        mid = (e.get("model_info") or {}).get("id", "")
        if mid.startswith("zk-pool-"):
            ids.add(mid)
    return True, ids


def add_pool(port):
    if DRY_RUN:
        log(f"  [DRY_RUN] would /model/new {pool_id(port)} -> {UPSTREAM_HOST}:{port}")
        return True
    entry = {
        "model_name": POOL_MODEL,
        "litellm_params": {
            "model": f"openai/{UPSTREAM_SLUG}",
            "api_base": f"http://{UPSTREAM_HOST}:{port}/v1",
            "api_key": "raw",
            "use_chat_completions_api": True,
            "rpm": RPM_PER_ACCT,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
        "model_info": {"id": pool_id(port)},
    }
    status, resp = api_request("POST", "/model/new", entry)
    if status == 200:
        log(f"  + added {pool_id(port)} (rpm={RPM_PER_ACCT})")
        return True
    if "already exists" in str(resp).lower():
        log(f"  = exists {pool_id(port)}")
        return True
    log(f"  ! add {pool_id(port)} failed HTTP {status}: {str(resp)[:120]}")
    return False


def del_pool(port):
    if DRY_RUN:
        log(f"  [DRY_RUN] would /model/delete {pool_id(port)}")
        return True
    status, resp = api_request("POST", "/model/delete", {"id": pool_id(port)})
    if status == 200:
        log(f"  - deleted {pool_id(port)}")
        return True
    log(f"  ! delete {pool_id(port)} failed HTTP {status}: {str(resp)[:120]}")
    return False


# ---- 自愈动作 ----

def trigger_refresh(port, meta, state, transitions):
    """对 DEAD_SESSION 触发 refresh.sh（节流 REFRESH_RETRY_INTERVAL）。"""
    s = state.get(str(port), {})
    if now_ts() - s.get("last_refresh_at", 0) < REFRESH_RETRY_INTERVAL:
        return
    script = Path(meta["dir"]) / "ops" / "refresh.sh"
    if not script.exists():
        log(f"  {meta['name']}: refresh.sh missing ({script})")
        return
    s["last_refresh_at"] = now_ts()
    state[str(port)] = s
    if DRY_RUN or not REFRESH:
        log(f"  [{'DRY_RUN' if DRY_RUN else 'REFRESH=0'}] would refresh {meta['name']}")
        return
    log(f"  {meta['name']}: triggering refresh.sh")
    try:
        subprocess.run(["bash", str(script)], timeout=REFRESH_TIMEOUT,
                       capture_output=True, text=True)
        transitions.append(f"🛠 {meta['name']}(:{port}) refresh.sh triggered (DEAD_SESSION self-heal)")
    except subprocess.TimeoutExpired:
        log(f"  {meta['name']}: refresh.sh timeout >{REFRESH_TIMEOUT}s")
    except Exception as e:
        log(f"  {meta['name']}: refresh.sh error {type(e).__name__}: {e}")


def try_revive(port, meta):
    if DRY_RUN or not REVIVE:
        log(f"  [{'DRY_RUN' if DRY_RUN else 'REVIVE=0'}] would docker start {meta['cont']}")
        return
    try:
        subprocess.run(["docker", "start", meta["cont"]], timeout=30,
                       capture_output=True, text=True)
        log(f"  {meta['name']}: docker start {meta['cont']} issued")
    except Exception as e:
        log(f"  {meta['name']}: docker start error {type(e).__name__}: {e}")


def alert_feishu(text):
    if not FEISHU_WEBHOOK or FEISHU_WEBHOOK.startswith("stub"):
        return
    try:
        body = {"msg_type": "text", "content": {"text": text}}
        req = urllib.request.Request(FEISHU_WEBHOOK, method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log(f"feishu alert failed: {e}")


# ---- main ----

def main():
    if not LITELLM_MK:
        log("FATAL: LITELLM_MK not set")
        return 2

    if JITTER_MAX > 0 and not DRY_RUN:
        j = random.randint(0, JITTER_MAX)
        log(f"jitter sleep {j}s")
        time.sleep(j)

    state = load_state()
    transitions = []

    # 1) 分级所有端口
    classed = {}
    for port, meta in POOL_ACCOUNTS.items():
        st, detail = classify(port, meta, state)
        classed[port] = (st, detail)
        log(f"{meta['name']}(:{port}): {st} ({detail})")

    # 2) 读 LiteLLM 当前池
    ok, present = current_pool_ids()
    if not ok:
        log("WARN: /model/info unavailable — skip mutations this cycle")
        save_state(state)
        return 1
    log(f"current pool DB ids: {sorted(present) or '(empty)'}")

    # 3) reconcile
    healthy_ports = []
    for port, meta in POOL_ACCOUNTS.items():
        st, detail = classed[port]
        s = state.get(str(port), {})
        was = s.get("state")
        pid = pool_id(port)
        in_pool = pid in present

        if st in KEEP_STATES:
            healthy_ports.append(port)
            s["consecutive_dead"] = 0
            if not in_pool:
                if add_pool(port):
                    transitions.append(f"🟢 {meta['name']}(:{port}) {st} → join pool")
        elif st in DROP_STATES:
            dead = s.get("consecutive_dead", 0) + 1
            s["consecutive_dead"] = dead
            if in_pool and dead >= CONSECUTIVE_DEAD_THRESHOLD:
                if del_pool(port):
                    transitions.append(
                        f"🔴 {meta['name']}(:{port}) {st} x{dead} → drop from pool")
            elif in_pool:
                log(f"  {meta['name']}: {st} x{dead} (<{CONSECUTIVE_DEAD_THRESHOLD}, keep, anti-flap)")
            # 自愈
            if st == "DEAD_CONTAINER":
                try_revive(port, meta)
            elif st == "DEAD_SESSION":
                trigger_refresh(port, meta, state, transitions)

        s["state"] = st
        s["detail"] = detail
        s["ts"] = now_ts()
        state[str(port)] = s

        if was and was != st:
            transitions.append(f"↕ {meta['name']}(:{port}) {was} → {st}")

    log(f"healthy ports → desired pool: {healthy_ports}")

    if DRY_RUN:
        log("DRY_RUN: state not saved")
    else:
        save_state(state)

    if transitions and not DRY_RUN:
        alert_feishu("zerokey-rebalance:\n" + "\n".join(transitions))
    log(f"done: pool_size_desired={len(healthy_ports)} transitions={len(transitions)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
