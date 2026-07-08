#!/usr/bin/env python3
"""zerokey-rebalance-prod.py — 198 prod zerokey-pool 健康自愈调度器

fork 自 ops/zerokey-rebalance.py（dev/188 版），部署在 188 主机 crontab。

与 dev 版差异：
  - POOL_ACCOUNTS：20 桥全集（dev 只 6 桥），来自 188 实际 docker ps + P1 wave
    * bridge-mode  : 8123-8133  (11 acct，legacy 端口映射)
    * host-mode    : 8134-8136  (3 acct，32/34/37)
    * host-mode P1 : 8139-8141  (3 acct，18/19/20)
    * host-mode P2 : 8144/8146/8147 (3 acct，50/52/53)
  - LITELLM_BASE 默认 http://10.68.13.198:30402 (prod NodePort)
  - RPM_PER_ACCT 默认 30 (vs dev 10)，对齐 A 步骤 CM patch
  - POOL_MODEL   保持 "zerokey-pool"（不改产品名 alias）

前置条件（首次运行）：
  1. CM 里 zerokey-pool 静态块必须已删（否则 config+DB 双源路由不确定）
  2. LITELLM_MK 环境变量 = sk-pro-litellm-*

用法：
  # dry-run 看行为
  DRY_RUN=1 LITELLM_MK=$MK python3 zerokey-rebalance-prod.py

  # 首次注册（会 add 20 entries）
  LITELLM_MK=$MK python3 zerokey-rebalance-prod.py

  # crontab（*/5 min）
  */5 * * * * LITELLM_MK=... /usr/bin/python3 /home/cltx/zerokey-rebalance-prod.py >> /home/cltx/.zk-rebalance-prod.log 2>&1
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

HOME = os.path.expanduser("~")

# ---- prod POOL_ACCOUNTS: 20 桥全集 ----
# dir: 账号根目录 (state/REFRESH_STALE + ops/refresh.sh)
# cont: docker 容器名 (docker start / refresh 用)
POOL_ACCOUNTS = {
    # bridge-mode legacy (8123 主容器 + 10 named acct)
    8123: {"name": "kristine", "dir": f"{HOME}/zerokey-codex",                 "cont": "zerokey-codex"},
    8124: {"name": "timothy",  "dir": f"{HOME}/zerokey-codex-accounts/timothy", "cont": "zerokey-codex-timothy"},
    8125: {"name": "zyq",      "dir": f"{HOME}/zerokey-codex-accounts/zyq",     "cont": "zerokey-codex-zyq"},
    8126: {"name": "owp",      "dir": f"{HOME}/zerokey-codex-accounts/owp",     "cont": "zerokey-codex-owp"},
    8127: {"name": "hgg",      "dir": f"{HOME}/zerokey-codex-accounts/hgg",     "cont": "zerokey-codex-hgg"},
    8128: {"name": "dvo",      "dir": f"{HOME}/zerokey-codex-accounts/dvo",     "cont": "zerokey-codex-dvo"},
    8129: {"name": "elise",    "dir": f"{HOME}/zerokey-codex-accounts/elise",   "cont": "zerokey-codex-elise"},
    8130: {"name": "herbert",  "dir": f"{HOME}/zerokey-codex-accounts/herbert", "cont": "zerokey-codex-herbert"},
    8131: {"name": "olga",     "dir": f"{HOME}/zerokey-codex-accounts/olga",    "cont": "zerokey-codex-olga"},
    8132: {"name": "tania",    "dir": f"{HOME}/zerokey-codex-accounts/tania",   "cont": "zerokey-codex-tania"},
    8133: {"name": "iheyv",    "dir": f"{HOME}/zerokey-codex-accounts/iheyv",   "cont": "zerokey-codex-iheyv"},
    # host-mode P0 (acct-32/34/37 双桥)
    8134: {"name": "acct37",   "dir": f"{HOME}/zerokey-codex-accounts/acct37",  "cont": "zerokey-codex-acct37"},
    8135: {"name": "acct32",   "dir": f"{HOME}/zerokey-codex-accounts/acct32",  "cont": "zerokey-codex-acct32"},
    8136: {"name": "acct34",   "dir": f"{HOME}/zerokey-codex-accounts/acct34",  "cont": "zerokey-codex-acct34"},
    # host-mode P1 (acct-18/19/20 纯 web)
    8139: {"name": "acct18",   "dir": f"{HOME}/zerokey-codex-accounts/acct18",  "cont": "zerokey-codex-acct18"},
    8140: {"name": "acct19",   "dir": f"{HOME}/zerokey-codex-accounts/acct19",  "cont": "zerokey-codex-acct19"},
    8141: {"name": "acct20",   "dir": f"{HOME}/zerokey-codex-accounts/acct20",  "cont": "zerokey-codex-acct20"},
    # host-mode P2 (acct-50/51/52/53 双桥)
    8144: {"name": "acct50",   "dir": f"{HOME}/zerokey-codex-accounts/acct50",  "cont": "zerokey-codex-acct50"},
    8145: {"name": "acct51",   "dir": f"{HOME}/zerokey-codex-accounts/acct51",  "cont": "zerokey-codex-acct51"},
    8146: {"name": "acct52",   "dir": f"{HOME}/zerokey-codex-accounts/acct52",  "cont": "zerokey-codex-acct52"},
    8147: {"name": "acct53",   "dir": f"{HOME}/zerokey-codex-accounts/acct53",  "cont": "zerokey-codex-acct53"},
}

UPSTREAM_HOST = os.environ.get("ZK_UPSTREAM_HOST", "10.68.13.188")
UPSTREAM_SLUG = "gpt-5-5"

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30402").rstrip("/")
LITELLM_MK = os.environ.get("LITELLM_MK", "")
POOL_MODEL = os.environ.get("POOL_MODEL", "zerokey-pool")
RPM_PER_ACCT = int(os.environ.get("RPM_PER_ACCT", "30"))
DEEP_PROBE = os.environ.get("DEEP_PROBE", "") == "1"
DEEP_MIN = int(os.environ.get("DEEP_MIN", "300"))
DEEP_MAX = int(os.environ.get("DEEP_MAX", "600"))
DEEP_PRESLEEP = int(os.environ.get("DEEP_PRESLEEP", "30"))
REFRESH = os.environ.get("REFRESH", "") == "1"
REVIVE = os.environ.get("REVIVE", "") == "1"
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
JITTER_MAX = int(os.environ.get("REBALANCE_JITTER", "60"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

STATE_DIR = os.environ.get("ZK_STATE_DIR", f"{HOME}/.zerokey-rebalance-prod/state")
STATE_FILE = f"{STATE_DIR}/state.json"

CONSECUTIVE_DEAD_THRESHOLD = int(os.environ.get("CONSECUTIVE_DEAD_THRESHOLD", "2"))
REFRESH_RETRY_INTERVAL = int(os.environ.get("REFRESH_RETRY_INTERVAL", str(3 * 3600)))
REFRESH_TIMEOUT = int(os.environ.get("REFRESH_TIMEOUT", "330"))
PROBE_TIMEOUT = 6
DEEP_TIMEOUT = 25

KEEP_STATES = {"HEALTHY", "RATE_LIMITED", "DEGRADED"}
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


def port_reachable(port):
    """本机探 /v1/models。"""
    url = f"http://127.0.0.1:{port}/v1/models"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def stale_marker(meta):
    return Path(meta["dir"]) / "state" / "REFRESH_STALE"


def has_stale(meta):
    return stale_marker(meta).exists()


def deep_probe(port):
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
        return "DEGRADED", f"deep {e.code}"
    except Exception as e:
        return "DEGRADED", f"deep {type(e).__name__}"


def classify(port, meta, state):
    """
    prod 语义（vs dev）：
      - REFRESH_STALE flag != DEAD。P1 wave (2026-07-05) 实证：STALE 存在但
        chat_http 6/6 = 200（live cookie auto-renew）。STALE 只影响下轮
        refresh cadence 提示，不作 pool 摘除依据。判死只有两条：
        (a) 端口不通 → DEAD_CONTAINER
        (b) DEEP_PROBE 打真 chat 返 401/403 → DEAD_SESSION
      - STALE + DEEP_PROBE 关：视为 HEALTHY（保守：不假装深探过）
      - STALE + DEEP_PROBE fail (deep_probe 内部判) → 该函数自己升级为 DEAD_SESSION
    """
    if not port_reachable(port):
        return "DEAD_CONTAINER", "port unreachable"
    stale = has_stale(meta)
    if DEEP_PROBE:
        s = state.get(str(port), {})
        now = now_ts()
        next_deep = s.get("next_deep_at")
        # STALE 状态强制立即深探（跳过 next_deep 抖动）
        due = stale or next_deep is None or now >= next_deep
        if next_deep is None and not stale:
            s["next_deep_at"] = now + random.randint(0, DEEP_MAX)
            state[str(port)] = s
            return "HEALTHY", f"port ok (deep scheduled +{(s['next_deep_at']-now)//60}min)"
        if due:
            if DEEP_PRESLEEP > 0 and not stale:
                time.sleep(random.randint(0, DEEP_PRESLEEP))
            st, detail = deep_probe(port)
            s["next_deep_at"] = now_ts() + random.randint(DEEP_MIN, DEEP_MAX)
            state[str(port)] = s
            if stale:
                detail = f"{detail} (STALE flag present, chat verified)"
            return st, detail
        return "HEALTHY", f"port ok (deep in {(next_deep-now)//60}min)"
    # DEEP_PROBE 关：STALE 视 DEGRADED（保留在池，让 rpm 节流兜底），非 STALE HEALTHY
    if stale:
        return "DEGRADED", "REFRESH_STALE marker (no deep to verify, keep in pool)"
    return "HEALTHY", "port ok (no deep probe)"


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
    """返回本池 DB 部署 id 集合 (zk-pool-*)。db_model=False 的静态块不计。"""
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
            "input_cost_per_token": 5e-6,
            "output_cost_per_token": 3e-5,
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


def trigger_refresh(port, meta, state, transitions):
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

    classed = {}
    for port, meta in POOL_ACCOUNTS.items():
        st, detail = classify(port, meta, state)
        classed[port] = (st, detail)
        log(f"{meta['name']}(:{port}): {st} ({detail})")

    ok, present = current_pool_ids()
    if not ok:
        log("WARN: /model/info unavailable — skip mutations this cycle")
        save_state(state)
        return 1
    log(f"current pool DB ids: {sorted(present) or '(empty)'}")

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
        alert_feishu("zerokey-rebalance-prod:\n" + "\n".join(transitions))
    log(f"done: pool_size_desired={len(healthy_ports)} transitions={len(transitions)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
