#!/usr/bin/env python3
"""
quota-rebalance.py — ChatGPT Pro 198 prod 池智能自动调度

部署位置：188 主机 crontab（每 5min 触发，脚本内部自决是否执行）
设计目标：
  - 撞限自动下线（/model/delete），reset 后自动上线（/model/new）
  - 智能探测频率：离限远→少探测，接近→频繁探测，已下线且未到 reset→不探测
  - 随机抖动：每次实际执行带 random jitter，避免固定模式
  - 区分"quota 下线"vs"token_invalidated 下线"，后者不自动上线

决策矩阵（每个 acct 独立决策）：
  ┌─────────────────────────────────────────────────────────────┐
  │ 条件                          │ 动作                         │
  ├─────────────────────────────────────────────────────────────┤
  │ manual_offline + <6h           │ SKIP（防瞬态雪崩）            │
  │ manual_offline + ≥6h           │ PROBE 重试（自愈通道）         │
  │ paused + now < restore_at     │ SKIP（不探测，等 reset）       │
  │ paused + now >= restore_at    │ PROBE → 如果恢复则 resume      │
  │ online + 5h<50% + wk<50%     │ 低频（上次<25min→SKIP）        │
  │ online + 5h 50~80%           │ 中频（上次<12min→SKIP）        │
  │ online + 5h>80%              │ 高频（每次都探）                │
  │ 探测连续 401 < 3              │ consecutive_401++ 不删 entry   │
  │ 探测连续 401 ≥ 3              │ 标记 manual_offline + 下线     │
  │ probe error 连续 ≥3 (例如 SSH) │ 飞书边沿告警                  │
  └─────────────────────────────────────────────────────────────┘

  manual_offline 自愈机制（2026-06-12 加固）：
    - 6h 间隔重试一次；探到健康会自动 resume_acct + 清标记
    - 连续 401 阈值 3 次防 CF 瞬态/access_token 刷新窗口误杀
    - probe_error（含 187 SSH 不通）连续 3 次走飞书告警，避免静默

  187 账号（acct-2/15/17）通过 SSH 远程读取 auth.json 探测，
  不再是盲恢复。需要 188→187 SSH 免密。

环境变量（/home/cltx/.chatgpt-quota/env）：
  LITELLM_BASE      例 http://10.68.13.198:30402/pro
  LITELLM_MK        198 prod LITELLM_MASTER_KEY
  FEISHU_WEBHOOK    飞书告警 webhook（边沿触发）
  REBALANCE_JITTER  随机延迟上限秒数（默认 180）
  DRY_RUN           =1 只打印不操作
"""
import json
import re
import base64
import random
import subprocess
import urllib.request
import urllib.error
import socket
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---- 配置 ----
ACCOUNTS_DIR = "/Data/chatgpt-auth"
STATE_DIR = "/home/cltx/.chatgpt-quota/state"
STATE_FILE = f"{STATE_DIR}/state.json"

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30402/pro")
LITELLM_MK = os.environ.get("LITELLM_MK", "")
LITELLM_MK_188 = os.environ.get("LITELLM_MK_188", "")
LITELLM_MK_187 = os.environ.get("LITELLM_MK_187", "")
LITELLM_MK_198 = os.environ.get("LITELLM_MK_198", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
JITTER_MAX = int(os.environ.get("REBALANCE_JITTER", "180"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

# 198 prod 的 4 个 chatgpt model groups
CHATGPT_MODELS = [
    {"model_name": "chatgpt-gpt-5.5", "litellm_model": "openai/chatgpt-gpt-5.5"},
    {"model_name": "chatgpt-gpt-5.4", "litellm_model": "openai/chatgpt-gpt-5.4"},
    # 5.3-codex upstream replaced with spark (Codex/ChatGPT plan restriction, 2026-06-13 pool-wide verified)
    {"model_name": "chatgpt-gpt-5.3-codex", "litellm_model": "openai/chatgpt-gpt-5.3-codex-spark"},
    # chatgpt-gpt-5.4-pro removed: 0/13 acct support upstream (Codex/ChatGPT plan limit)
]

# 198 prod pool 中的账号
# location=188 → auth.json 在本机 /Data/chatgpt-auth/acct-N/
# location=187 → auth.json 在 187，通过 SSH 远程读取探测
POOL_ACCOUNTS = {
    "acct-13": {"port": 4013, "location": "198"},
    "acct-14": {"port": 4014, "location": "198"},
    "acct-16": {"port": 4016, "location": "198"},
    "acct-22": {"port": 4022, "location": "198"},
    "acct-23": {"port": 4023, "location": "198"},
    "acct-24": {"port": 4024, "location": "198"},
    "acct-25": {"port": 4025, "location": "198"},
    "acct-17": {"port": 4017, "location": "198"},
    "acct-27": {"port": 4027, "location": "198"},
    "acct-28": {"port": 4028, "location": "198"},
    "acct-29": {"port": 4029, "location": "198"},
    "acct-30": {"port": 4030, "location": "198"},
    "acct-31": {"port": 4031, "location": "198"},
    "acct-32": {"port": 4032, "location": "198"},
    "acct-33": {"port": 4033, "location": "198"},
    "acct-34": {"port": 4034, "location": "198"},
    "acct-35": {"port": 4035, "location": "198"},
    "acct-36": {"port": 4036, "location": "198"},
    "acct-37": {"port": 4037, "location": "198"},
    "acct-38": {"port": 4038, "location": "198"},
    "acct-39": {"port": 4039, "location": "198"},
    "acct-40": {"port": 4040, "location": "198"},
    "acct-41": {"port": 4041, "location": "198"},
    "acct-42": {"port": 4042, "location": "198"},
    "acct-43": {"port": 4043, "location": "198"},
    "acct-44": {"port": 4044, "location": "198"},
    "acct-45": {"port": 4045, "location": "198"},
    "acct-46": {"port": 4046, "location": "198"},
    "acct-47": {"port": 4047, "location": "198"},
    "acct-48": {"port": 4048, "location": "198"},
    "acct-49": {"port": 4049, "location": "198"},
    "acct-50": {"port": 4050, "location": "198"},
    "acct-51": {"port": 4051, "location": "198"},
    "acct-52": {"port": 4052, "location": "198"},
    "acct-53": {"port": 4053, "location": "198"},
    "acct-54": {"port": 4054, "location": "198"},
    "acct-55": {"port": 4055, "location": "198"},
    "acct-56": {"port": 4056, "location": "198"},
    "acct-57": {"port": 4057, "location": "198"},
    "acct-58": {"port": 4058, "location": "198"},
    "acct-60": {"port": 4060, "location": "198"},
    "acct-61": {"port": 4061, "location": "198"},
    "acct-62": {"port": 4062, "location": "198"},
    "acct-63": {"port": 4063, "location": "198"},
    "acct-64": {"port": 4064, "location": "198"},
    "acct-65": {"port": 4065, "location": "198"},
    "acct-66": {"port": 4066, "location": "198"},
    "acct-67": {"port": 4067, "location": "198"},
    "acct-68": {"port": 4068, "location": "198"},
}

SSH_187_HOST = os.environ.get("SSH_187_HOST", "10.68.13.187")
SSH_187_USER = os.environ.get("SSH_187_USER", "cltx")
SSH_187_AUTH_DIR = os.environ.get("SSH_187_AUTH_DIR", "/Data/chatgpt-auth")

# 198 K3s litellm-product ns: ssh AIYJY-litellm + kubectl exec to read auth.json
SSH_198_HOST = os.environ.get("SSH_198_HOST", "10.68.13.198")
SSH_198_USER = os.environ.get("SSH_198_USER", "cltx")
K8S_198_NS   = os.environ.get("K8S_198_NS", "litellm-product")

PROBE_INTERVAL_LOW = 25 * 60   # 5h<50% → 至少 25min 间隔
PROBE_INTERVAL_MID = 12 * 60   # 5h 50~80% → 至少 12min 间隔
MANUAL_OFFLINE_RETRY_INTERVAL = 6 * 3600  # manual_offline 每 6h 重新尝试一次
CONSECUTIVE_401_THRESHOLD = 3  # 连续 401 ≥ 3 次才标记 manual_offline
SSH_FAIL_ALERT_THRESHOLD = 3   # 187 SSH 连续失败 ≥ 3 次才告警（边沿）
SUBSCRIPTION_META_TTL = 12 * 3600  # 订阅到期元数据每天最多刷新两次
PAUSED_FORCE_PROBE_INTERVAL = 6 * 3600  # paused acct 即便 restore_at 未到，至少 6h 强制 probe 一次（防 cron-SKIP 把 stale 卡死）
TMP_AUTH_TMPL = "/tmp/auth-{acct}.json"  # 188:/tmp 备用 token 副本（K3s exec 失败 fallback）

# ---- TOKEN_INVALID 自动修复配置 ----
# 触发条件：tier=TOKEN_INVALID + manual_offline + 距上次修复 ≥12h + 累计 <5 次
# 修复链路：re-oauth.sh GEN_ONLY=1 → ssh AIYJY-litellm kubectl cp + rollout restart
REPAIR_RETRY_INTERVAL = 12 * 3600   # 12h 一次
MAX_REPAIR_ATTEMPTS = 5             # 5 次后 repair_frozen
REPAIR_TIMEOUT = 600                # re-oauth.sh 单次 10min 上限
REPAIR_KUBECTL_TIMEOUT = 90         # kubectl cp / rollout 单条 90s
REPAIR_SCRIPT = "/Data/chatgpt-auth/re-oauth.sh"
REPAIR_AUTH_OUT_TMPL = "/tmp/auth-{acct}.json"


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


# ---- 探测频率决策 ----

def should_probe(acct, meta, state):
    s = state.get(acct, {})
    now = now_ts()
    last_probe = s.get("ts", 0)
    elapsed = now - last_probe

    # manual_offline 不再 hard skip：每 6h 给一次自愈试探机会
    # 上游 token 自然恢复（CF 拦截解除、access_token 刷新成功）后能被自动召回
    if s.get("manual_offline"):
        if elapsed < MANUAL_OFFLINE_RETRY_INTERVAL:
            return False, f"manual_offline, retry in {(MANUAL_OFFLINE_RETRY_INTERVAL-elapsed)//60}min"
        return True, f"manual_offline, retry window reached ({elapsed//3600}h)"

    # 已 quota 下线 → 看 restore_at 是否到期
    if s.get("paused"):
        restore_at = s.get("restore_at", 0)
        if restore_at and now < restore_at:
            # 即便没到 restore_at，每 PAUSED_FORCE_PROBE_INTERVAL 强制探一次：
            # 防 banked redeem / 上游周期重置但 cron 探测失败导致 state 永卡 stale
            # （acct-32 实证：上游 5h=0/7d=0 已 reset 但 state.weekly_pct=100 卡 12h+）
            if elapsed >= PAUSED_FORCE_PROBE_INTERVAL:
                return True, f"paused but {elapsed//3600}h since last probe, force re-check"
            remaining = (restore_at - now) // 60
            return False, f"paused, reset in {remaining}min"
        return True, "paused, reset window reached"

    # 在线 → 按上次 5h% 决定探测频率
    p_pct = s.get("primary_pct", 0)
    if p_pct >= 80:
        return True, f"5h={p_pct}%>=80, high freq"
    elif p_pct >= 50:
        if elapsed < PROBE_INTERVAL_MID:
            return False, f"5h={p_pct}%, {elapsed//60}min ago (<12min)"
        return True, f"5h={p_pct}%, interval ok"
    else:
        if elapsed < PROBE_INTERVAL_LOW:
            return False, f"5h={p_pct}%, {elapsed//60}min ago (<25min)"
        return True, f"5h={p_pct}%, interval ok"


# ---- OpenAI usage 探测 ----

def parse_auth_json(auth):
    tok = auth.get("access_token")
    if not tok:
        raise ValueError("no access_token")
    # id_token 可能是占位符（如 "x"）或缺失；非标准 JWT 时跳过 claims 解析
    # access_token + 顶层 account_id 已足够探测，claims 仅用来拿 sub_until 兜底
    claims = {}
    id_token = auth.get("id_token") or ""
    parts = id_token.split(".")
    if len(parts) >= 2:
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            claims = json.loads(base64.urlsafe_b64decode(seg))
        except Exception:
            claims = {}
    auth_claims = claims.get("https://api.openai.com/auth", {})
    aid = auth.get("account_id") or auth_claims.get("chatgpt_account_id")
    if not aid:
        raise ValueError("no account_id")
    sub_until = auth_claims.get("chatgpt_subscription_active_until")
    return tok, aid, sub_until


def parse_account(auth_path):
    auth = json.load(open(auth_path))
    return parse_auth_json(auth)


def subscription_meta_from_auth(acct, meta):
    """只读 auth.json 中的订阅到期元数据，不打 /codex/usage。"""
    if meta["location"] == "187":
        _, _, sub_until = parse_account_remote(acct)
    elif meta["location"] == "198":
        _, _, sub_until = parse_account_198(acct)
    else:
        auth_path = f"{ACCOUNTS_DIR}/{acct}/auth.json"
        if not Path(auth_path).exists():
            raise FileNotFoundError(f"{auth_path} not found")
        _, _, sub_until = parse_account(auth_path)
    return sub_until


def refresh_subscription_meta_if_needed(acct, meta, state):
    s = state.get(acct, {})
    now = now_ts()
    if s.get("subscription_active_until") and now - s.get("subscription_checked_at", 0) < SUBSCRIPTION_META_TTL:
        return
    try:
        sub_until = subscription_meta_from_auth(acct, meta)
    except Exception as e:
        log(f"{acct}: subscription meta skip ({type(e).__name__}: {str(e)[:80]})")
        return
    state[acct] = {
        **s,
        "subscription_active_until": sub_until,
        "subscription_checked_at": now,
    }


def parse_account_remote(acct):
    """SSH 到 187 读取 auth.json 并解析。"""
    remote_path = f"{SSH_187_AUTH_DIR}/{acct}/auth.json"
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
             f"{SSH_187_USER}@{SSH_187_HOST}", f"cat {remote_path}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise FileNotFoundError(f"ssh cat failed: {result.stderr.strip()}")
        auth = json.loads(result.stdout)
        return parse_auth_json(auth)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"SSH to 187 timeout reading {acct}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON from 187 {acct}: {e}")


def parse_account_198(acct):
    """SSH 到 AIYJY-litellm (198) + kubectl exec 读取 K3s Pod 内 auth.json。

    用 ControlMaster 复用单条长连接（同一轮 cron 内 N 个 198 acct 共享一次握手），
    并对 ssh 网络瞬态失败做 2 次重试；JSON 解析失败不重试。
    Pod 不存在时远端返回 exit 42 → 立刻 raise，不进重试。
    """
    # 远端 bash：先确认 Pod 存在，否则 exit 42（避免 kubectl exec 没 pod 名 hang 整个 timeout）
    kc_cmd = (
        f"set -e; export KUBECONFIG=$HOME/.kube/config; "
        f"POD=$(kubectl -n {K8S_198_NS} get pod -l app=chatgpt-{acct} "
        f"-o jsonpath='{{.items[0].metadata.name}}' 2>/dev/null); "
        f'if [ -z "$POD" ]; then echo "pod chatgpt-{acct} not found" >&2; exit 42; fi; '
        f"kubectl -n {K8S_198_NS} exec $POD -- cat /chatgpt-auth/auth.json"
    )
    ssh_args = [
        "ssh",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=/tmp/cm-quota-198-%r@%h:%p",
        "-o", "ControlPersist=10m",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        f"{SSH_198_USER}@{SSH_198_HOST}",
        kc_cmd,
    ]
    mux_path = f"/tmp/cm-quota-198-{SSH_198_USER}@{SSH_198_HOST}:22"

    def _reset_mux():
        # ssh -O exit 优雅关 master, 失败再 unlink socket 文件兜底（死 socket 占位）
        try:
            subprocess.run(
                ["ssh", "-O", "exit", "-o", f"ControlPath={mux_path}",
                 f"{SSH_198_USER}@{SSH_198_HOST}"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            pass
        try:
            if Path(mux_path).exists():
                Path(mux_path).unlink()
        except Exception:
            pass

    last_err = None
    for attempt in range(1, 4):  # 总共最多 3 次（仅对瞬态网络错误重试）
        try:
            result = subprocess.run(
                ssh_args, capture_output=True, text=True, timeout=20,
            )
            if result.returncode == 42:
                # Pod 不存在：确定性失败，不重试
                raise FileNotFoundError(f"pod chatgpt-{acct} not found on 198")
            if result.returncode != 0:
                stderr = result.stderr.strip()
                last_err = FileNotFoundError(f"ssh+kubectl failed: {stderr[:200]}")
                # mux 死掉的典型字串 → 下轮重试前先重建 master
                if any(s in stderr for s in (
                    "Session open refused",
                    "mux_client_request_session",
                    "disabling multiplexing",
                    "ControlSocket",
                )):
                    _reset_mux()
                if attempt < 3:
                    time.sleep(0.5 * attempt)
                    continue
                break
            auth = json.loads(result.stdout)
            return parse_auth_json(auth)
        except subprocess.TimeoutExpired:
            last_err = RuntimeError(f"SSH to 198 timeout reading {acct}")
            if attempt < 3:
                time.sleep(0.5 * attempt)
                continue
            break
        except json.JSONDecodeError as e:
            # JSON 解析错说明 stdout 已返回但格式坏，重试无意义
            raise RuntimeError(f"invalid JSON from 198 {acct}: {e}")

    # 3 次 ssh+kubectl 全失败 → 回落 188:/tmp/auth-{acct}.json 副本（feedback_chatgpt_48b_check_198_tmp_first）
    fallback = TMP_AUTH_TMPL.format(acct=acct)
    if Path(fallback).exists():
        try:
            auth = json.load(open(fallback))
            log(f"  {acct}: K3s exec failed, using {fallback} fallback")
            return parse_auth_json(auth)
        except (json.JSONDecodeError, ValueError) as e:
            log(f"  {acct}: fallback {fallback} broken: {e}")
    raise last_err


def fetch_usage(tok, aid):
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/usage",
        headers={
            "Authorization": f"Bearer {tok}",
            "chatgpt-account-id": aid,
            "Originator": "codex_cli_rs",
            "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)",
        },
    )
    last_401 = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # 401 也走重试（区分 CF 瞬态拦截 vs token 真死）
                last_401 = e
                if attempt < 3:
                    time.sleep(15 * attempt + random.random())
                    continue
                raise
            if attempt < 3:
                time.sleep(2 * attempt + random.random())
        except Exception:
            if attempt < 3:
                time.sleep(2 * attempt + random.random())
    if last_401 is not None:
        raise last_401
    raise RuntimeError("fetch_usage failed after 3 attempts")


def classify(usage):
    """返回 (tier, cause, p_pct, w_pct, restore_at_epoch, p_reset_at, w_reset_at)

    p_reset_at / w_reset_at 是上游 /codex/usage 给的 5h 和 7d 窗口真实归零时间
    (epoch seconds, 由 reset_after_seconds + ts 计算)；与 restore_at(cron 管控)区分。
    """
    rl = usage["rate_limit"]
    pw = rl["primary_window"] or {}
    sw = rl["secondary_window"] or {}
    p_pct = pw.get("used_percent", 0)
    w_pct = sw.get("used_percent", 0)
    # 兼容两种字段约定：上游裸 payload 给 reset_after_seconds 相对值，
    # 老逻辑里有的版本是 reset_at 绝对值。优先 reset_at，否则 now+after。
    now = time.time()
    p_reset_at = pw.get("reset_at")
    if p_reset_at is None and pw.get("reset_after_seconds") is not None:
        p_reset_at = int(now + pw["reset_after_seconds"])
    w_reset_at = sw.get("reset_at")
    if w_reset_at is None and sw.get("reset_after_seconds") is not None:
        w_reset_at = int(now + sw["reset_after_seconds"])

    if w_pct >= 100:
        return "OFFLINE-WEEK", f"wk={w_pct}%>=100", p_pct, w_pct, w_reset_at, p_reset_at, w_reset_at
    if p_pct >= 99:
        return "OFFLINE-5H", f"5h={p_pct}%>=99", p_pct, w_pct, p_reset_at, p_reset_at, w_reset_at
    if p_pct >= 50 or w_pct >= 50:
        return "SLOW", f"5h={p_pct}%/wk={w_pct}%", p_pct, w_pct, None, p_reset_at, w_reset_at
    return "HEALTHY", None, p_pct, w_pct, None, p_reset_at, w_reset_at


# ---- LiteLLM API ----

def api_request(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    headers = {"Authorization": f"Bearer {LITELLM_MK}"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        LITELLM_BASE + path, data=data, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:300]
    except (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout, OSError) as e:
        # transient socket / connection-reset / timeout → 0 让 caller 按"无 200"分支走，不抛 traceback
        return 0, f"{type(e).__name__}: {str(e)[:200]}"


def model_info_snapshot():
    """一次拉 /v1/model/info → dict[api_base] = list[model_name]。

    返回 None 表示拉取失败（5xx/network），调用方应回退到"假设健康"避免风暴 /model/new。
    主 loop 开头调用一次，传给 router_has_entries / SCALE0 preflight 避免 N×720KB 重复拉。
    存 list[model_name] 而非 bool，便于排除 chatgpt-vip-* (pause_acct 不删) 后判定真 ghost。
    """
    status, data = api_request("GET", "/v1/model/info")
    if status != 200:
        return None
    by_base = {}
    for e in data.get("data", []):
        ab = (e.get("litellm_params") or {}).get("api_base", "")
        name = e.get("model_name", "")
        if ab:
            by_base.setdefault(ab, []).append(name)
    return by_base


def router_has_entries(acct, meta, snapshot=None, exclude_vip=False):
    """Return True if LiteLLM router currently has any entry whose api_base matches this acct.

    Self-heal guard: pause/resume only mutates router via /model/delete + /model/new and
    relies on state.paused as the trigger. If entries vanish for any external reason
    (manual /model/delete, DB corruption, restore_at race) while state.paused stays False,
    cron never rebuilds them — the acct goes silently dark. This check makes the main
    loop notice that drift on the next probe.

    snapshot: optional dict[api_base]=list[model_name] from model_info_snapshot(); when
    supplied skip the per-call HTTP roundtrip. snapshot=None → live GET (legacy path).
    exclude_vip: skip chatgpt-vip-* entries when judging "has entries" — needed for the
    SCALE0 ghost detect so it doesn't infinitely re-pause VIP acct (whose vip entries are
    intentionally kept across pause for CM fallback chain).
    """
    ab = acct_api_base(acct, meta)
    if snapshot is not None:
        names = snapshot.get(ab, [])
        if exclude_vip:
            names = [n for n in names if not (n or "").startswith("chatgpt-vip-")]
        return bool(names)
    status, data = api_request("GET", "/v1/model/info")
    if status != 200:
        # don't auto-heal on transient 5xx — better to skip than spam /model/new
        return True
    for e in data.get("data", []):
        if (e.get("litellm_params") or {}).get("api_base", "") != ab:
            continue
        if exclude_vip and (e.get("model_name", "") or "").startswith("chatgpt-vip-"):
            continue
        return True
    return False


def acct_api_base(acct, meta):
    if meta["location"] == "188":
        return f"http://10.68.13.188:{meta['port']}"
    elif meta["location"] == "187":
        return f"http://10.68.13.187:{meta['port']}"
    elif meta["location"] == "198":
        return f"http://chatgpt-{acct}.{K8S_198_NS}.svc.cluster.local:4000"
    else:
        return f"http://localhost:{meta['port']}"



def deploy_scale_snapshot():
    """一次拉 198 K3s namespace 全部 chatgpt-acct-* deploy 的 spec.replicas。

    返回 {"acct-N": spec_replicas}。任何 ssh/kubectl 失败 → 返回 {} (空 dict)，
    主 loop 会保持现有 probe 行为（不会因此 pause 健康 acct）。

    Source of truth：deploy.spec.replicas（不是 status.readyReplicas，避免新 pod 没 ready
    时被误判 scale=0）。spec=0 等价"操作人/手动 scale 0 释放内存"显式意图。
    """
    cmd = (
        "export KUBECONFIG=$HOME/.kube/config; "
        f"kubectl -n {K8S_198_NS} get deploy -o json 2>/dev/null"
    )
    ssh_args = [
        "ssh",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=/tmp/cm-quota-198-%r@%h:%p",
        "-o", "ControlPersist=10m",
        f"{SSH_198_USER}@{SSH_198_HOST}",
        cmd,
    ]
    try:
        result = subprocess.run(ssh_args, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            log(f"  deploy_scale_snapshot ssh failed rc={result.returncode}: {result.stderr.strip()[:120]}")
            return {}
        d = json.loads(result.stdout)
    except Exception as e:
        log(f"  deploy_scale_snapshot error {type(e).__name__}: {str(e)[:120]}")
        return {}
    out = {}
    for it in d.get("items", []):
        name = (it.get("metadata") or {}).get("name", "")
        m = re.match(r"chatgpt-(acct-\d+)$", name)
        if not m:
            continue
        out[m.group(1)] = (it.get("spec") or {}).get("replicas", 1)
    return out


def acct_api_key(meta):
    if meta["location"] == "188":
        return LITELLM_MK_188
    elif meta["location"] == "187":
        return LITELLM_MK_187
    elif meta["location"] == "198":
        return LITELLM_MK_198
    else:
        return LITELLM_MK_188


# ---- Active probe (≥99% 上游不信，发 'hi' 探测 LiteLLM router) ----
PROBE_ENDPOINT  = os.environ.get(
    "PROBE_ENDPOINT", "https://cc.auto-link.com.cn/pro/v1/chat/completions"
)
PROBE_MK        = os.environ.get("PROBE_MK", "")
PROBE_TIMEOUT   = 20
PROBE_MAX_TRIES = 2  # 一轮内最多 2 次；都失败才视为 fail


def probe_acct(acct):
    """主动探测 acct：向 LiteLLM router 发一条 'hi'，看是否直命中该 acct。
    判定 OK 条件：HTTP 200 + x-litellm-attempted-fallbacks=0（无 wangsu 兜底）。
    返回 (ok, detail)
    """
    body = json.dumps({
        "model": f"chatgpt-{acct}-gpt-5.5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    }).encode()
    headers = {
        "Authorization": f"Bearer {PROBE_MK}",
        "Content-Type": "application/json",
    }
    last = ""
    for i in range(1, PROBE_MAX_TRIES + 1):
        req = urllib.request.Request(
            PROBE_ENDPOINT, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
                fb = r.headers.get("x-litellm-attempted-fallbacks", "0")
                if r.status == 200 and fb == "0":
                    return True, f"try{i}: 200 fb=0"
                last = f"try{i}: HTTP {r.status} fb={fb}"
        except urllib.error.HTTPError as e:
            last = f"try{i}: HTTP {e.code}"
        except Exception as e:
            last = f"try{i}: {type(e).__name__}"
        if i < PROBE_MAX_TRIES:
            time.sleep(1)
    return False, last



AUTO_SCALE_ON_PAUSE = os.environ.get("AUTO_SCALE_ON_PAUSE", "1") == "1"


def scale_deploy(acct, replicas, wait_ready=False, timeout=120):
    """198 only。kubectl scale --replicas=N + 可选 wait endpoint ready。

    返回 True=成功（含已是目标态）/ False=失败（ssh 报错或 wait 超时）。
    AUTO_SCALE_ON_PAUSE=0 时整体 no-op（返回 True）。

    wait_ready=True 用于 resume_acct：必须等 svc 有 endpoint 再让 router 注册 entry，
    否则 simple-shuffle 路由到 0 endpoint svc 立刻超时（同 §0b scale=0 ghost 问题反向）。
    """
    if not AUTO_SCALE_ON_PAUSE:
        return True
    if DRY_RUN:
        log(f"  [DRY_RUN] would kubectl scale chatgpt-{acct} --replicas={replicas} (wait_ready={wait_ready})")
        return True
    scale_cmd = (
        f"export KUBECONFIG=$HOME/.kube/config; "
        f"kubectl -n {K8S_198_NS} scale deploy/chatgpt-{acct} --replicas={replicas}"
    )
    if wait_ready and replicas > 0:
        # endpoint 有 IP = 至少 1 个 ready pod；比 rollout status 更精准（rollout 看 replica
        # 计数，可能 Running 但 readinessProbe 没过；endpoint 是 router 真实能路由的口径）
        wait_cmd = (
            f" && end=$(($(date +%s)+{timeout})); "
            f"while [ $(date +%s) -lt $end ]; do "
            f"ips=$(kubectl -n {K8S_198_NS} get endpoints chatgpt-{acct} "
            f"-o jsonpath='{{.subsets[*].addresses[*].ip}}' 2>/dev/null); "
            f"if [ -n \"$ips\" ]; then echo \"ready ips=$ips\"; exit 0; fi; "
            f"sleep 3; "
            f"done; echo timeout; exit 1"
        )
        cmd = scale_cmd + wait_cmd
    else:
        cmd = scale_cmd
    mux_path = f"/tmp/cm-quota-198-{SSH_198_USER}@{SSH_198_HOST}:22"
    ssh_args = [
        "ssh",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={mux_path}",
        "-o", "ControlPersist=10m",
        f"{SSH_198_USER}@{SSH_198_HOST}",
        cmd,
    ]
    try:
        result = subprocess.run(ssh_args, capture_output=True, text=True, timeout=timeout + 30)
    except Exception as e:
        log(f"  scale_deploy {acct}={replicas} ssh err {type(e).__name__}: {str(e)[:120]}")
        return False
    if result.returncode != 0:
        log(f"  scale_deploy {acct}={replicas} rc={result.returncode} "
            f"stderr={result.stderr.strip()[:200]} stdout={result.stdout.strip()[:160]}")
        return False
    out = result.stdout.strip()
    log(f"  scaled chatgpt-{acct} -> replicas={replicas}{' (wait_ready)' if wait_ready and replicas > 0 else ''}: {out[:200]}")
    return True


def pause_acct(acct, meta):
    if DRY_RUN:
        log(f"  [DRY_RUN] would pause {acct}")
        return 0
    ab = acct_api_base(acct, meta)
    deleted = 0
    # 通过 api_base 匹配删除（比 ID 匹配更可靠）
    status, data = api_request("GET", "/v1/model/info")
    if status != 200:
        log(f"  pause {acct}: /model/info failed HTTP {status} {str(data)[:120]}")
        return 0
    for e in data.get("data", []):
        e_ab = (e.get("litellm_params") or {}).get("api_base", "")
        e_id = (e.get("model_info") or {}).get("id", "")
        e_name = e.get("model_name", "")
        if e_ab != ab:
            continue
        # VIP 独占 entry (chatgpt-vip-<group>-gpt-5.X) 不删: 撞限走 CM router_settings.fallbacks
        # (vip -> 主池 -> wangsu), 跟 pause 协同；删了就 BadRequest no healthy deployments
        if e_name.startswith("chatgpt-vip-"):
            log(f"  skip vip entry {e_id} ({e_name}) — pause leaves it for fallback chain")
            continue
        s2, resp2 = api_request("POST", "/model/delete", {"id": e_id})
        if s2 == 200:
            deleted += 1
            log(f"  deleted {e_id}")
        else:
            log(f"  delete {e_id} failed: HTTP {s2} {str(resp2)[:120]}")
    log(f"  {acct} paused: {deleted} entries deleted")
    # 2026-06-25: cooldown / quota-hit / scale=0 preflight 都走 pause；删完 entry 后顺手
    # 把 pod 也 scale=0 释放 198 内存（等价 [[feedback_198_mem_released_by_scaling_paused_acct_deploys]]
    # 的人工动作）。preflight 分支 deploy 已是 0 → kubectl scale 是 no-op，幂等。
    # 198 only；187/188 location 跳过（不存在 K3s deploy）。
    # 2026-06-26: 失败不再静默 — 写 state.pending_scale_down=True，下一轮 cron 顶部重试，
    # 否则 transient ssh 抖动 → entry 删了 pod 没缩 → 永远占内存（SCALE0 preflight 因
    # replicas=1 不会再 fire，pause 分支因 paused=True 也不会再 fire 这一段）。
    scaled_ok = True
    if meta.get("location") == "198":
        scaled_ok = scale_deploy(acct, 0, wait_ready=False)
        if not scaled_ok:
            log(f"  ⚠️  {acct} pause: scale=0 FAILED — state.pending_scale_down=True for next cron retry")
    return {"deleted": deleted, "scaled_ok": scaled_ok}


def resume_acct(acct, meta):
    if DRY_RUN:
        log(f"  [DRY_RUN] would resume {acct}")
        return 0
    # 2026-06-25: 先 scale=1 + 等 endpoint ready 再注册 entry。entry 一进 router
    # simple-shuffle 立即可被选中，pod 没起来 = 路由到空 svc → 客户端撞 fallback chain。
    # 198 only；如果 scale/wait 失败：不 register（避免 ghost entry 复发，跟 §0b 同源）。
    if meta.get("location") == "198":
        if not scale_deploy(acct, 1, wait_ready=True, timeout=120):
            log(f"  resume {acct}: scale=1 / wait endpoint failed → skip register (next cron will retry)")
            return 0
    ab = acct_api_base(acct, meta)
    ak = acct_api_key(meta)
    created = 0
    # 幂等：先拿一次 /model/info, 后续 POST /model/new 失败时按 id 判定 DB 残留
    status, info = api_request("GET", "/v1/model/info")
    existing = {}  # id → api_base
    if status == 200:
        for e in info.get("data", []):
            eid = (e.get("model_info") or {}).get("id", "")
            eab = (e.get("litellm_params") or {}).get("api_base", "")
            if eid:
                existing[eid] = eab
    for m in CHATGPT_MODELS:
        mid = f"chatgpt-{acct}-{m['model_name'].replace('chatgpt-','')}"
        # 下游 chatgpt-acct sub-proxy 也跑 LiteLLM，要求 Authorization: Bearer $POOL_KEY 经
        # user_api_key_auth；router 转发时 entry 没设 api_key → 不带 header → 下游 400
        # "No connected db."（2026-06-25 acct-67/68 + 18 acct 全集群事故实证）。
        # v1.89 入参层接收裸值会自动补 Bearer，不要手动拼前缀（双前缀）。
        entry = {
            "model_name": m["model_name"],
            "litellm_params": {
                "model": m["litellm_model"],
                "api_base": ab,
                "api_key": ak,
            },
            "model_info": {"id": mid, "mode": "responses"},
        }
        # 已存在且 api_base 一致 → 真正幂等 skip（视为 created，状态就是想要的）
        if existing.get(mid) == ab:
            created += 1
            log(f"  skip existing {mid} (router & db in sync)")
            continue
        # 已存在但 api_base 不同（旧 acct 漂移 / 历史残留）→ 先 delete 再 create
        if mid in existing:
            ds, dr = api_request("POST", "/model/delete", {"id": mid})
            log(f"  pre-delete drifted {mid} (was {existing[mid]}): HTTP {ds} {str(dr)[:80]}")
        status, resp = api_request("POST", "/model/new", entry)
        if status == 200:
            created += 1
            log(f"  created {mid}")
            continue
        # POST 失败：fallback 一次 — 大概率是 DB 历史残留 (router 未持有但 ProxyModelTable 有)
        if status == 500 or "already exists" in str(resp).lower() or "failed to add" in str(resp).lower():
            ds, dr = api_request("POST", "/model/delete", {"id": mid})
            log(f"  retry: pre-delete {mid} HTTP {ds} {str(dr)[:80]}")
            status, resp = api_request("POST", "/model/new", entry)
            if status == 200:
                created += 1
                log(f"  created {mid} (after pre-delete)")
                continue
        log(f"  create {mid} failed: HTTP {status} {str(resp)[:160]}")
    log(f"  {acct} resumed: {created}/{len(CHATGPT_MODELS)} entries in router")
    return created


# ---- 飞书告警 ----

def alert_feishu(text):
    if not FEISHU_WEBHOOK or FEISHU_WEBHOOK.startswith("stub"):
        return
    try:
        body = {"msg_type": "text", "content": {"text": text}}
        req = urllib.request.Request(
            FEISHU_WEBHOOK, method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log(f"feishu alert failed: {e}")


def fmt_eta(epoch):
    if not epoch:
        return "?"
    remaining = epoch - now_ts()
    if remaining <= 0:
        return "now"
    h, m = divmod(remaining // 60, 60)
    return f"{h}h{m}m"


# ---- TOKEN_INVALID 自动修复 ----

def _read_creds_email(acct):
    """读 .creds 拿 email 域名，决定 OTP provider；缺失返回 None。"""
    creds_path = Path(f"{ACCOUNTS_DIR}/{acct}/.creds")
    if not creds_path.exists():
        return None
    try:
        for line in creds_path.read_text().splitlines():
            if line.startswith("email="):
                return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None


def _pick_otp_provider(email):
    if not email:
        return None
    dom = email.split("@")[-1].lower()
    if dom == "qq.com":
        return "imap_qq"
    if dom == "mail.com":
        return "mailcom"
    return None


def should_attempt_repair(acct, meta, state, repair_lock):
    """判定是否对 acct 触发自动 re-OAuth 修复。

    repair_lock: 单元素 list，[True] 表示本轮已触发过 repair（串行约束）
    """
    if repair_lock[0]:
        return False
    if meta.get("location") != "198":
        return False
    s = state.get(acct, {})
    if s.get("tier") != "TOKEN_INVALID":
        return False
    if not s.get("manual_offline"):
        return False
    if s.get("repair_frozen"):
        return False
    if s.get("repair_attempts", 0) >= MAX_REPAIR_ATTEMPTS:
        return False
    if (now_ts() - s.get("last_repair_at", 0)) < REPAIR_RETRY_INTERVAL:
        return False
    if not Path(f"{ACCOUNTS_DIR}/{acct}/.creds").exists():
        return False
    email = _read_creds_email(acct)
    if not _pick_otp_provider(email):
        return False
    return True


def attempt_repair_198(acct, meta, state, transitions):
    """对 198 K3s acct 触发完整自动修复流程。

    Steps:
      1. re-oauth.sh GEN_ONLY=1 + MAIL_OTP_PROVIDER=<imap_qq|mailcom>
      2. kubectl cp /tmp/auth-acct-N.json → Pod:/chatgpt-auth/auth.json
      3. kubectl rollout restart deployment/chatgpt-acct-N
      4. sleep 30s 让 Pod 起来；下一轮 cron 自然 probe 验证

    state 落字段:
      repair_attempts++, last_repair_at=now, ts=now
      达 5 次 → repair_frozen=True
      为让下一轮 probe 不被 6h manual_offline 拒，强制 ts=now-6h-1
        (探测窗口立即打开，但仍保持 manual_offline 直到 probe 验证)
    """
    s = state.get(acct, {})
    n = s.get("repair_attempts", 0) + 1
    email = _read_creds_email(acct)
    provider = _pick_otp_provider(email)

    if DRY_RUN:
        log(f"  [DRY_RUN] would attempt_repair {acct} ({n}/{MAX_REPAIR_ATTEMPTS}) provider={provider}")
        return False

    hours_dead = (now_ts() - s.get("ts", now_ts())) // 3600
    transitions.append(
        f"✨ {acct} auto-repair attempt {n}/{MAX_REPAIR_ATTEMPTS} starting "
        f"(TOKEN_INVALID since ~{hours_dead}h, provider={provider})"
    )
    log(f"  {acct}: auto-repair attempt {n}/{MAX_REPAIR_ATTEMPTS} starting")

    ok = False
    err_msg = ""

    # Phase 1: re-oauth.sh GEN_ONLY=1
    auth_out = REPAIR_AUTH_OUT_TMPL.format(acct=acct)
    try:
        result = subprocess.run(
            ["bash", REPAIR_SCRIPT, acct],
            env={**os.environ, "GEN_ONLY": "1", "MAIL_OTP_PROVIDER": provider},
            capture_output=True, text=True, timeout=REPAIR_TIMEOUT,
        )
        if result.returncode != 0 or not Path(auth_out).exists():
            tail = (result.stderr or result.stdout)[-200:]
            err_msg = f"re-oauth.sh rc={result.returncode}: {tail}"
        else:
            # auth.json shape check
            auth = json.loads(Path(auth_out).read_text())
            if not all(auth.get(k) for k in ("access_token", "refresh_token", "account_id")):
                err_msg = "auth.json missing required fields"
            else:
                ok = True
    except subprocess.TimeoutExpired:
        err_msg = f"re-oauth.sh timeout >{REPAIR_TIMEOUT}s"
    except Exception as e:
        err_msg = f"re-oauth.sh exception: {type(e).__name__}: {e}"

    # Phase 2: kubectl cp + rollout restart（仅 OAuth 成功才做）
    if ok:
        try:
            # Step 2a: lookup current pod name
            pod_cmd = (
                f"KUBECONFIG=$HOME/.kube/config kubectl -n {K8S_198_NS} "
                f"get pod -l app=chatgpt-{acct} "
                f"-o jsonpath='{{.items[0].metadata.name}}'"
            )
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
                 f"{SSH_198_USER}@{SSH_198_HOST}", pod_cmd],
                capture_output=True, text=True, timeout=REPAIR_KUBECTL_TIMEOUT,
            )
            pod_name = (r.stdout or "").strip()
            if r.returncode != 0 or not pod_name:
                err_msg = f"kubectl get pod failed: {(r.stderr or '')[:120]}"
                ok = False
        except Exception as e:
            err_msg = f"kubectl get pod exception: {type(e).__name__}: {e}"
            ok = False

    if ok:
        # Step 2b: kubectl cp via stdin (scp file to 198 first, then cp into pod)
        try:
            # scp auth.json to 198 /tmp first
            r = subprocess.run(
                ["scp", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
                 auth_out, f"{SSH_198_USER}@{SSH_198_HOST}:/tmp/{Path(auth_out).name}"],
                capture_output=True, text=True, timeout=REPAIR_KUBECTL_TIMEOUT,
            )
            if r.returncode != 0:
                err_msg = f"scp to 198 failed: {(r.stderr or '')[:120]}"
                ok = False
            else:
                cp_cmd = (
                    f"KUBECONFIG=$HOME/.kube/config kubectl -n {K8S_198_NS} cp "
                    f"/tmp/{Path(auth_out).name} {pod_name}:/chatgpt-auth/auth.json"
                )
                r = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=10",
                     f"{SSH_198_USER}@{SSH_198_HOST}", cp_cmd],
                    capture_output=True, text=True, timeout=REPAIR_KUBECTL_TIMEOUT,
                )
                if r.returncode != 0:
                    err_msg = f"kubectl cp failed: {(r.stderr or '')[:120]}"
                    ok = False
        except Exception as e:
            err_msg = f"kubectl cp exception: {type(e).__name__}: {e}"
            ok = False

    if ok:
        # Step 2c: rollout restart
        try:
            restart_cmd = (
                f"KUBECONFIG=$HOME/.kube/config kubectl -n {K8S_198_NS} "
                f"rollout restart deployment/chatgpt-{acct}"
            )
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10",
                 f"{SSH_198_USER}@{SSH_198_HOST}", restart_cmd],
                capture_output=True, text=True, timeout=REPAIR_KUBECTL_TIMEOUT,
            )
            if r.returncode != 0:
                err_msg = f"rollout restart failed: {(r.stderr or '')[:120]}"
                ok = False
            else:
                log(f"  {acct}: rollout restart issued, sleeping 30s for pod readiness")
                time.sleep(30)
        except Exception as e:
            err_msg = f"rollout restart exception: {type(e).__name__}: {e}"
            ok = False

    # Phase 3: 落 state
    now = now_ts()
    new_s = {
        **s,
        "repair_attempts": n,
        "last_repair_at": now,
        # 强制把上次探测时间推到 6h+1s 前，让 should_probe 下一轮直接打开
        "ts": now - MANUAL_OFFLINE_RETRY_INTERVAL - 1,
    }
    if ok:
        log(f"  {acct}: auto-repair {n}/{MAX_REPAIR_ATTEMPTS} OAuth+deploy SUCCESS (verify next probe)")
        transitions.append(
            f"🛠 {acct} auto-repair {n}/{MAX_REPAIR_ATTEMPTS} OAuth+deploy ok — awaiting probe verify"
        )
    else:
        log(f"  {acct}: auto-repair {n}/{MAX_REPAIR_ATTEMPTS} FAILED: {err_msg}")
        transitions.append(
            f"⚠️ {acct} auto-repair {n}/{MAX_REPAIR_ATTEMPTS} failed: {err_msg[:160]}"
        )
        if n >= MAX_REPAIR_ATTEMPTS:
            new_s["repair_frozen"] = True
            transitions.append(
                f"🧊 {acct} repair_frozen after {n} attempts — needs manual intervention"
            )

    state[acct] = new_s
    return ok


# ---- main ----

def main():
    missing = [n for n in ("LITELLM_MK_188", "LITELLM_MK_187", "LITELLM_MK_198", "PROBE_MK")
               if not os.environ.get(n)]
    if missing:
        print(f"FATAL: required env unset: {','.join(missing)}", file=sys.stderr)
        sys.exit(2)

    if JITTER_MAX > 0 and not DRY_RUN:
        jitter = random.randint(0, JITTER_MAX)
        log(f"jitter sleep {jitter}s")
        time.sleep(jitter)

    state = load_state()
    # 2026-06-25: scale=0 preflight。pod 不存在的 acct 走 pause_acct 清 router entry，
    # 防 simple-shuffle 把流量打到死 svc → wangsu fallback。dict 为空说明 ssh/kubectl 失败 → 跳过本轮 preflight。
    scale_snap = deploy_scale_snapshot()
    if scale_snap:
        log(f"deploy_scale_snapshot: total={len(scale_snap)} zero={sum(1 for v in scale_snap.values() if v == 0)}")
    # 2026-06-26: 一次拉 /v1/model/info 全表（720KB），供 SCALE0 preflight 和 router-drift
    # self-heal 用，避免 N×45 重复 GET。None=拉取失败 → 下面相关分支按 "假设健康" 跳过自愈。
    mi_snap = model_info_snapshot()
    if mi_snap is None:
        log("model_info_snapshot: failed; router-drift self-heal & SCALE0 ghost detect skipped this tick")
    else:
        log(f"model_info_snapshot: {len(mi_snap)} distinct api_base entries")
    probed = 0
    skipped = 0
    transitions = []
    repair_lock = [False]  # 单元素 list：本轮 cron 最多触发 1 个 re-OAuth（防并发触 CF Turnstile）

    for acct, meta in POOL_ACCOUNTS.items():
        # 优先：TOKEN_INVALID 自动修复（meta+state 条件全满足才进入；不阻塞下方 probe 逻辑——
        # 修复后强制把 ts 推回 6h+1s 前，下一轮 5min 后 should_probe 会立刻打开验证）
        if should_attempt_repair(acct, meta, state, repair_lock):
            attempt_repair_198(acct, meta, state, transitions)
            repair_lock[0] = True
            skipped += 1
            continue

        refresh_subscription_meta_if_needed(acct, meta, state)
        # 2026-06-25: scale 反向 (0→1) 恢复 — 手动 kubectl scale 起 pod 后，清掉
        # SCALED_DOWN 标记 + paused=False，让 should_probe 下一轮当作正常 paused
        # 走探测→resume 流程（resume_acct 自己会再调 scale_deploy 确保活体 + 注册 entry）。
        # 不直接在这里调 resume_acct：probe 一次重新校准 quota 更稳，且 router-drift self-heal
        # 兜底任何遗漏。
        if (meta.get("location") == "198"
                and scale_snap.get(acct, 0) > 0
                and state.get(acct, {}).get("tier") == "SCALED_DOWN"):
            old_s = state.get(acct, {})
            log(f"{acct}: scale=0→{scale_snap[acct]} detected, clearing SCALED_DOWN tier")
            transitions.append(f"🟡 {acct} scale 0→{scale_snap[acct]} → clear SCALED_DOWN, awaiting probe")
            state[acct] = {
                **old_s,
                "paused": False,
                # 2026-06-26: 反向恢复时同步清 manual_offline / consecutive_401 / pending_scale_down，
                # 否则 should_probe 走 manual_offline 6h gate → 看起来"scale 上来了但 cron 不 resume"。
                # 手动 scale 上来本身就是操作人对 token 有效性的隐式确认（OAuth 已重做 / token 已修），
                # 这里清掉跟"应用层 reset state.json" 等价，免去额外手动步骤。
                "manual_offline": False,
                "consecutive_401": 0,
                "pending_scale_down": False,
                "tier": None,
                "cause": "scaled_back_up",
                # 强制 ts 推到 6h+1s 前让 should_probe 立刻打开探测
                "ts": now_ts() - MANUAL_OFFLINE_RETRY_INTERVAL - 1,
            }
            # 不 continue — 让下面 should_probe 直接探一次决定要不要 resume

        # 2026-06-26: pending_scale_down retry — 上一轮 pause_acct 的 scale_deploy 失败
        # （ssh 抖动 / kubectl timeout），现在 paused=True 但 deploy.replicas>0 还在烧内存。
        # 此处单独重试 scale=0，成功就清 flag，失败就保留下一轮再试。不参与下面的 SCALE0 detect
        # 分支（那是 ghost entry 清理，已经 paused 的 acct entry 也已删完无 ghost）。
        if (meta.get("location") == "198"
                and state.get(acct, {}).get("pending_scale_down")
                and scale_snap.get(acct, 0) > 0):
            old_s = state.get(acct, {})
            log(f"{acct}: retry pending scale=0 (replicas={scale_snap[acct]})")
            if scale_deploy(acct, 0, wait_ready=False):
                state[acct] = {**old_s, "pending_scale_down": False}
                transitions.append(f"🟦 {acct} pending scale=0 retry → ok")
            else:
                log(f"{acct}: pending scale=0 retry FAILED — will try again next cron")
            skipped += 1
            continue

        # 2026-06-25: scale=0 detect (198 only)。pod 不存在不能服务流量，必须
        # pause_acct 清 router entry（否则 simple-shuffle 仍会路由到死 svc → fallback wangsu）。
        # 之后 SKIP probe — scale=0 期间 codex/usage 数值毫无意义。
        # 2026-06-26: ghost-entry 清理不再 gate 在 not paused — operator/audit --fix 给
        # 已 paused acct 重新 /model/new 注册过的 entry，老版本永远漏；只要 router 实测有
        # 残留就 pause_acct（pause_acct 自己幂等：state.paused 已 True 也会扫 entry 删干净）。
        if meta.get("location") == "198" and scale_snap.get(acct) == 0:
            old_s = state.get(acct, {})
            had_entries = router_has_entries(acct, meta, snapshot=mi_snap, exclude_vip=True)
            if had_entries:
                pause_acct(acct, meta)
                if not old_s.get("paused"):
                    transitions.append(f"🔴 {acct} scale=0 (pod=0) ghost entries → pause")
                    log(f"{acct}: scale=0 detected → pause (ghost entries cleared)")
                else:
                    transitions.append(f"🔴 {acct} scale=0 ghost entries (paused re-add) → re-clean")
                    log(f"{acct}: scale=0 + paused but ghost entries → re-clean")
            elif not old_s.get("paused"):
                log(f"{acct}: scale=0, no router entries, sync state.paused=True")
            # 写 state.paused=True + tier SCALED_DOWN（不动 manual_offline，让 scale=1 时自然 resume）
            state[acct] = {
                **old_s,
                "paused": True,
                "tier": "SCALED_DOWN",
                "cause": "deploy.spec.replicas=0",
                "ts": now_ts(),
            }
            skipped += 1
            continue

        do_probe, reason = should_probe(acct, meta, state)

        if not do_probe:
            skipped += 1
            log(f"{acct}: SKIP ({reason})")
            continue

        try:
            if meta["location"] == "187":
                tok, aid, sub_until = parse_account_remote(acct)
            elif meta["location"] == "198":
                tok, aid, sub_until = parse_account_198(acct)
            else:
                auth_path = f"{ACCOUNTS_DIR}/{acct}/auth.json"
                if not Path(auth_path).exists():
                    log(f"{acct}: auth.json not found, skip")
                    skipped += 1
                    continue
                tok, aid, sub_until = parse_account(auth_path)
            usage = fetch_usage(tok, aid)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                old_s = state.get(acct, {})
                cnt = old_s.get("consecutive_401", 0) + 1
                log(f"{acct}: 401 ({cnt}/{CONSECUTIVE_401_THRESHOLD})")
                if cnt < CONSECUTIVE_401_THRESHOLD:
                    # 瞬态 401 → 不删 entry，仅记数；下次 5min 后再探
                    state[acct] = {**old_s, "consecutive_401": cnt, "ts": now_ts()}
                else:
                    # 连续 N 次 → 真死，下线（仍允许 manual_offline 6h 自愈）
                    pause_pending = False
                    if not old_s.get("manual_offline"):
                        pr = pause_acct(acct, meta)
                        pause_pending = not pr.get("scaled_ok", True)
                        transitions.append(
                            f"🔴 {acct} token_invalidated x{cnt} → auto pause"
                            + (" (scale=0 retry pending)" if pause_pending else "")
                        )
                    state[acct] = {
                        **old_s,
                        "manual_offline": True,
                        "paused": True,
                        "tier": "TOKEN_INVALID",
                        "consecutive_401": cnt,
                        "ts": now_ts(),
                        "pending_scale_down": pause_pending,
                    }
            else:
                log(f"{acct}: HTTP {e.code}, keeping state")
            continue
        except Exception as e:
            # SSH/网络/JSON 解析失败：累计计数，连续 N 次发飞书边沿告警
            log(f"{acct}: probe error {type(e).__name__}: {e}")
            old_s = state.get(acct, {})
            err_cnt = old_s.get("consecutive_probe_err", 0) + 1
            already_alerted = old_s.get("probe_err_alerted", False)
            new_s = {**old_s, "consecutive_probe_err": err_cnt, "ts": now_ts()}
            if err_cnt >= SSH_FAIL_ALERT_THRESHOLD and not already_alerted:
                transitions.append(
                    f"⚠️ {acct} probe error x{err_cnt}: {type(e).__name__}: {str(e)[:80]}"
                )
                new_s["probe_err_alerted"] = True
            state[acct] = new_s
            continue

        probed += 1
        tier, cause, p_pct, w_pct, restore_at, p_reset_at, w_reset_at = classify(usage)
        old = state.get(acct, {})
        was_paused = old.get("paused", False)
        was_manual_offline = old.get("manual_offline", False)

        # 上游 /codex/usage 的 99%+ 值不可信。≥99% 且当前在线时不再盲 pause，先 probe 验证。
        # - probe OK (≥1 次直命中)         → 留线，下轮 cron 5min 后再 probe
        # - probe 全失败 (2 次都 fail)     → 视为真超额，pause_acct() 删 entry
        # 已 paused 的 acct entry 已被删，probe 必失败 → 跳过 probe，仅靠上游数值回落自动 resume
        quota_high = tier in ("OFFLINE-5H", "OFFLINE-WEEK")
        should_offline = False
        probe_detail = None
        if tier == "OFFLINE-WEEK" and not was_paused:
            # 2026-06-26: 周 quota 100% 是硬上限不瞬态恢复，但 fetch_usage 偶发返坏 100 →
            # 单次就 pause + scale=0 太激进；改为连续 2 次才下线（间隔 5min cron，最坏延迟 5min
            # 才真下线，可接受）。计数器进 state，下面成功探测重置为 0。
            old_wk_cnt = old.get("consecutive_week_offline", 0) + 1
            if old_wk_cnt >= 2:
                should_offline = True
                probe_detail = f"wk=100 x{old_wk_cnt} no-probe"
            else:
                probe_detail = f"wk=100 x{old_wk_cnt} (need 2 for offline)"
                transitions.append(
                    f"⚠️ {acct} wk=100 first hit — defer offline, recheck next cron"
                )
                log(f"  {acct}: {probe_detail}")
        elif quota_high and not was_paused:
            # OFFLINE-5H 仍走双 probe（5h 窗口边沿 + codex 子池易误判）
            ok, probe_detail = probe_acct(acct)
            log(f"  {acct}: upstream {tier} ({cause}) — probe {probe_detail}")
            if not ok:
                should_offline = True

        pause_pending = False
        if should_offline and not was_paused:
            pr = pause_acct(acct, meta)
            pause_pending = not pr.get("scaled_ok", True)
            transitions.append(
                f"🔴 {acct} {cause} + probe fail ({probe_detail}) → pause (reset ~{fmt_eta(restore_at)})"
                + (" [scale=0 retry pending]" if pause_pending else "")
            )
        elif quota_high and not was_paused and not should_offline:
            # ≥99% 但 probe 通过 — 不动 entry，仅 transitions 留痕，下轮 cron 再 probe
            transitions.append(
                f"⚠️ {acct} {cause} but probe OK ({probe_detail}) → keep online, retry next cron"
            )
        elif not quota_high and was_paused:
            # 涵盖两种自愈：(a) 普通 quota paused 到 reset 时间窗；(b) manual_offline 6h 重试探到健康
            resume_acct(acct, meta)
            tag = "manual_offline self-heal" if was_manual_offline else "quota recovered"
            transitions.append(
                f"🟢 {acct} {tag} → resume (5h={p_pct}% wk={w_pct}%)"
            )
        elif not quota_high and not was_paused:
            # Router-drift self-heal: 上游健康 + state 标 online，但 LiteLLM router 没 entry。
            # 触发场景：手工 /model/delete、ProxyModelTable 历史 bug、resume_acct 半成功。
            # 不依赖 state.paused（一旦失同步会永远漏） — 直接看 router 真实状态。
            if not router_has_entries(acct, meta, snapshot=mi_snap):
                log(f"  {acct}: router-drift detected (online but no entries) → resume")
                resume_acct(acct, meta)
                transitions.append(
                    f"🟢 {acct} router-drift self-heal → resume (5h={p_pct}% wk={w_pct}%)"
                )

        # paused 状态落库：
        # - quota_high + was_paused 跳过 probe：保持 paused=True 等上游回落
        # - quota_high + 在线 + probe OK：keep online (paused=False)
        # - quota_high + 在线 + probe FAIL：pause_acct 已删 entry → paused=True
        # - !quota_high + was_paused：刚 resume_acct → paused=False
        # - !quota_high：在线 → paused=False
        if quota_high and was_paused:
            new_paused = True
        else:
            new_paused = should_offline

        state[acct] = {
            "tier": tier,
            "cause": cause,
            "primary_pct": p_pct,
            "weekly_pct": w_pct,
            "paused": new_paused,
            "manual_offline": False,
            "restore_at": restore_at,
            "primary_reset_at": p_reset_at,
            "weekly_reset_at": w_reset_at,
            "subscription_active_until": sub_until,
            "subscription_checked_at": now_ts(),
            "consecutive_401": 0,
            "consecutive_probe_err": 0,
            "probe_err_alerted": False,
            "pending_scale_down": pause_pending,
            # 2026-06-26: wk=100 计数器：未撞顶或已 should_offline 都重置；只有"撞 100 但 defer"那次保留
            "consecutive_week_offline": (
                old.get("consecutive_week_offline", 0) + 1
                if (tier == "OFFLINE-WEEK" and not was_paused and not should_offline)
                else 0
            ),
            # 探到健康/quota 限速即清理 repair 状态（自动修复链路完成）
            "repair_attempts": 0,
            "last_repair_at": 0,
            "repair_frozen": False,
            "ts": now_ts(),
        }

        log(f"{acct}: {tier} 5h={p_pct}% wk={w_pct}% paused={new_paused}")
        time.sleep(random.uniform(0.5, 2.0))

    if DRY_RUN:
        log("DRY_RUN: state not saved")
    else:
        save_state(state)
    log(f"done: probed={probed} skipped={skipped} transitions={len(transitions)}")

    if transitions:
        alert_feishu("ChatGPT quota-rebalance:\n" + "\n".join(transitions))


if __name__ == "__main__":
    main()
