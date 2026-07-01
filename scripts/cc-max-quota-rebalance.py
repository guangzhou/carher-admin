#!/usr/bin/env python3
"""
cc-max-quota-rebalance.py — CC Max 198 prod 智能自动调度

部署位置：224 (aiyjy-cc-proxy) crontab（每 5min 触发，脚本内部自决是否执行）
设计目标：
  - Haiku 探针读 anthropic-ratelimit-unified-5h/7d-utilization
  - 5h≥95% 或 7d≥95% → cooldown（从 198 prod LiteLLM 摘除 CC Max entry）
  - LiteLLM key fallback 自动接管（wangsu-direct）
  - reset 后探针验证恢复 → 重新注册 entry
  - 智能探测频率：离限远→少探测，接近→频繁探测
  - 随机抖动避免固定模式

决策矩阵（同 ChatGPT quota-rebalance）：
  ┌─────────────────────────────────────────────────────────────┐
  │ 条件                          │ 动作                         │
  ├─────────────────────────────────────────────────────────────┤
  │ manual_offline                │ SKIP（不探测，不自动恢复）     │
  │ paused + now < restore_at    │ SKIP（不探测，等 reset）       │
  │ paused + now >= restore_at   │ PROBE → 如果恢复则 resume      │
  │ online + 5h<50% + 7d<50%    │ 低频（上次<25min→SKIP）        │
  │ online + 5h 50~80%          │ 中频（上次<12min→SKIP）        │
  │ online + 5h>80%             │ 高频（每次都探）                │
  │ 探测到 5h>=80% 或 7d>=80%    │ cooldown（/model/delete）      │
  │ 探测到 401/403               │ manual_offline + cooldown      │
  └─────────────────────────────────────────────────────────────┘

  手动恢复 manual_offline 账号：
    1. 确认 token 恢复（Haiku 探针 200）
    2. 编辑 state.json: manual_offline→false, paused→false
    3. 等下次 crontab 自动 resume，或手动 REBALANCE_JITTER=0 跑一次

环境变量（/home/cltx/.ccmax-quota/env）：
  LITELLM_BASE      例 http://10.68.13.198:30402/pro
  LITELLM_MK        198 prod LITELLM_MASTER_KEY
  FEISHU_WEBHOOK    飞书告警 webhook（边沿触发）
  REBALANCE_JITTER  随机延迟上限秒数（默认 180）
  DRY_RUN           =1 只打印不操作
  AUTH_DIR          token 目录（默认 /Data/anthropic-auth）
"""
import json
import hashlib
import random
import urllib.request
import urllib.error
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---- 配置 ----
AUTH_DIR = os.environ.get("AUTH_DIR", "/Data/anthropic-auth")
STATE_DIR = "/home/cltx/.ccmax-quota/state"
STATE_FILE = f"{STATE_DIR}/state.json"

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30402/pro")
LITELLM_MK = os.environ.get("LITELLM_MK", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
JITTER_MAX = int(os.environ.get("REBALANCE_JITTER", "180"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

# CC Max 在 198 prod 注册的 model entries
# 每个账号注册 6 个 entry（对应 6 个 model group）
CCMAX_MODELS = [
    {"model_name": "claude-max-my-random-opus-4-8", "litellm_model": "anthropic/claude-opus-4-8"},
    {"model_name": "claude-max-my-random-opus-4-7", "litellm_model": "anthropic/claude-opus-4-7"},
    {"model_name": "claude-max-my-random-opus-4-6", "litellm_model": "anthropic/claude-opus-4-6"},
    {"model_name": "claude-max-my-random-sonnet", "litellm_model": "anthropic/claude-sonnet-4-6"},
    {"model_name": "claude-max-my-random-haiku", "litellm_model": "anthropic/claude-haiku-4-5"},
    {"model_name": "claude-max-my-random-fable-5", "litellm_model": "anthropic/claude-fable-5"},
]

# 账号池（目前只有 1 个）
# api_base 是 198 上的 SSH tunnel 端口，LiteLLM 通过这个访问 224:3456 proxy
POOL_ACCOUNTS = {
    "acct-16": {"api_base": "http://10.68.13.198:3467"},
}

# 198 prod LiteLLM 的 master key（用于注册的 api_key 字段，proxy 层的 API_KEYS）
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "d89f74ccaaa55b604a010c31be8e4c05d515e102b537c819")

PROBE_INTERVAL_LOW = 25 * 60   # 5h<50% → 至少 25min 间隔
PROBE_INTERVAL_MID = 12 * 60   # 5h 50~80% → 至少 12min 间隔

# ---- Haiku 探针配置 ----
PROBE_URL = "https://api.anthropic.com/v1/messages?beta=true"
CC_VERSION = "2.1.148.0b7"
PROBE_HEADERS = {
    "anthropic-beta": ("interleaved-thinking-2025-05-14,"
                       "context-management-2025-06-27,"
                       "prompt-caching-scope-2026-01-05,"
                       "claude-code-20250219"),
    "anthropic-dangerous-direct-browser-access": "true",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
    "x-app": "cli",
    "user-agent": f"claude-cli/{CC_VERSION.split('.0b')[0]} (external, sdk-cli)",
}
PROBE_BODY = json.dumps({
    "model": "claude-haiku-4-5",
    "max_tokens": 5,
    "messages": [{"role": "user", "content": "hi"}],
    "system": [
        {"type": "text",
         "text": f"x-anthropic-billing-header: cc_version={CC_VERSION}; "
                 f"cc_entrypoint=sdk-cli; cch=probe;"},
        {"type": "text",
         "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."},
    ],
}).encode()


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

def should_probe(acct, state):
    s = state.get(acct, {})
    now = now_ts()
    last_probe = s.get("ts", 0)
    elapsed = now - last_probe

    if s.get("manual_offline"):
        return False, "manual_offline"

    if s.get("paused"):
        restore_at = s.get("restore_at", 0)
        if restore_at and now < restore_at:
            remaining = (restore_at - now) // 60
            return False, f"paused, reset in {remaining}min"
        return True, "paused, reset window reached"

    h5_pct = s.get("h5_pct", 0)
    if h5_pct >= 80:
        return True, f"5h={h5_pct:.0f}%>=80, high freq"
    elif h5_pct >= 50:
        if elapsed < PROBE_INTERVAL_MID:
            return False, f"5h={h5_pct:.0f}%, {elapsed//60}min ago (<12min)"
        return True, f"5h={h5_pct:.0f}%, interval ok"
    else:
        if elapsed < PROBE_INTERVAL_LOW:
            return False, f"5h={h5_pct:.0f}%, {elapsed//60}min ago (<25min)"
        return True, f"5h={h5_pct:.0f}%, interval ok"


# ---- Haiku 探针 ----

def load_token(acct):
    env_file = Path(AUTH_DIR) / acct / ".env"
    if not env_file.exists():
        raise FileNotFoundError(f"{env_file} not found")
    for line in env_file.read_text().splitlines():
        if line.startswith("ANTHROPIC_OAUTH_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise ValueError(f"no ANTHROPIC_OAUTH_TOKEN in {env_file}")


def probe_quota(token):
    """Send Haiku probe, return (h5_util, d7_util, h5_reset, d7_reset) or raise."""
    req = urllib.request.Request(
        PROBE_URL, data=PROBE_BODY,
        headers={**PROBE_HEADERS, "Authorization": f"Bearer {token}"})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        h = dict(resp.headers)
        h5 = float(h.get("anthropic-ratelimit-unified-5h-utilization", -1))
        d7 = float(h.get("anthropic-ratelimit-unified-7d-utilization", -1))
        r5 = h.get("anthropic-ratelimit-unified-5h-reset", "")
        r7 = h.get("anthropic-ratelimit-unified-7d-reset", "")
        return h5, d7, int(r5) if r5 else 0, int(r7) if r7 else 0
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise
        try:
            body = json.loads(e.read().decode())
            msg = body.get("error", {}).get("message", "")
        except Exception:
            msg = ""
        if e.code == 429:
            raise RuntimeError(f"rate_limited: {msg}")
        raise RuntimeError(f"HTTP {e.code}: {msg}")


def classify(h5, d7, r5, r7):
    """返回 (tier, cause, restore_at_epoch)"""
    h5_pct = h5 * 100
    d7_pct = d7 * 100
    if d7_pct >= 80:
        return "OFFLINE-7D", f"7d={d7_pct:.0f}%>=80", r7
    if h5_pct >= 80:
        return "OFFLINE-5H", f"5h={h5_pct:.0f}%>=80", r5
    if h5_pct >= 50 or d7_pct >= 50:
        return "SLOW", f"5h={h5_pct:.0f}%/7d={d7_pct:.0f}%", None
    return "HEALTHY", None, None


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


def entry_id(acct, model_name):
    """生成 entry ID: ccmax-my-random-{model_suffix}"""
    suffix = model_name.replace("claude-max-my-random-", "")
    return f"ccmax-my-random-{suffix}"


def pause_acct(acct, meta):
    """摘除该账号在 198 prod 的所有 CC Max entry"""
    if DRY_RUN:
        log(f"  [DRY_RUN] would pause {acct}")
        return 0
    deleted = 0
    for m in CCMAX_MODELS:
        eid = entry_id(acct, m["model_name"])
        status, resp = api_request("POST", "/model/delete", {"id": eid})
        if status == 200:
            deleted += 1
            log(f"  deleted {eid}")
        elif "not found" in str(resp).lower():
            log(f"  skip {eid} (already gone)")
        else:
            log(f"  delete {eid} failed: HTTP {status} {str(resp)[:100]}")
    log(f"  {acct} paused: {deleted} entries deleted")
    return deleted


def resume_acct(acct, meta):
    """重新注册该账号的 6 个 CC Max entry"""
    if DRY_RUN:
        log(f"  [DRY_RUN] would resume {acct}")
        return 0
    ab = meta["api_base"]
    created = 0
    for m in CCMAX_MODELS:
        eid = entry_id(acct, m["model_name"])
        entry = {
            "model_name": m["model_name"],
            "litellm_params": {
                "model": m["litellm_model"],
                "api_base": ab,
                "api_key": PROXY_API_KEY,
            },
            "model_info": {"id": eid, "mode": "chat"},
        }
        status, resp = api_request("POST", "/model/new", entry)
        if status == 200:
            created += 1
            log(f"  created {eid}")
        elif "already exists" in str(resp).lower():
            log(f"  skip existing {eid}")
        else:
            log(f"  create {eid} failed: HTTP {status} {str(resp)[:100]}")
    log(f"  {acct} resumed: {created} entries created")
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


# ---- main ----

def main():
    if JITTER_MAX > 0 and not DRY_RUN:
        jitter = random.randint(0, JITTER_MAX)
        log(f"jitter sleep {jitter}s")
        time.sleep(jitter)

    state = load_state()
    probed = 0
    skipped = 0
    transitions = []

    for acct, meta in POOL_ACCOUNTS.items():
        current = state.get(acct, {})
        manual_mode = current.get("manual_mode", "auto")
        if manual_mode == "manual_offline":
            if not current.get("paused") or not current.get("manual_offline"):
                pause_acct(acct, meta)
                transitions.append(f"🔴 {acct} manual_offline lock → pause")
            state[acct] = {
                **current,
                "manual_mode": "manual_offline",
                "manual_offline": True,
                "paused": True,
                "tier": current.get("tier", "MANUAL_OFFLINE"),
                "cause": "manual_offline via acct web",
                "ts": now_ts(),
            }
            skipped += 1
            log(f"{acct}: SKIP (manual_offline lock)")
            continue
        if manual_mode == "manual_online" and (current.get("paused") or current.get("manual_offline")):
            resume_acct(acct, meta)
            transitions.append(f"🟢 {acct} manual_online lock → resume")
            state[acct] = {
                **current,
                "manual_mode": "manual_online",
                "manual_offline": False,
                "paused": False,
                "cause": "manual_online via acct web",
                "ts": now_ts(),
            }

        do_probe, reason = should_probe(acct, state)

        if not do_probe:
            skipped += 1
            log(f"{acct}: SKIP ({reason})")
            continue

        try:
            token = load_token(acct)
            h5, d7, r5, r7 = probe_quota(token)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                log(f"{acct}: {e.code} → token_invalidated")
                old_s = state.get(acct, {})
                if not old_s.get("manual_offline"):
                    pause_acct(acct, meta)
                    transitions.append(f"🔴 {acct} token_invalidated ({e.code}) → auto pause")
                state[acct] = {
                    **old_s,
                    "manual_offline": True,
                    "paused": True,
                    "tier": "TOKEN_INVALID",
                    "ts": now_ts(),
                }
            else:
                log(f"{acct}: HTTP {e.code}, keeping state")
            continue
        except Exception as e:
            log(f"{acct}: probe error {type(e).__name__}: {e}")
            continue

        probed += 1
        tier, cause, restore_at = classify(h5, d7, r5, r7)
        old = state.get(acct, {})
        manual_mode = old.get("manual_mode", "auto")
        was_paused = old.get("paused", False)
        should_offline = tier in ("OFFLINE-5H", "OFFLINE-7D")
        if manual_mode == "manual_online":
            should_offline = False
            cause = f"{cause}; manual_online lock" if cause else "manual_online lock"

        if should_offline and not was_paused:
            pause_acct(acct, meta)
            transitions.append(
                f"🔴 {acct} {cause} → cooldown (reset ~{fmt_eta(restore_at)})"
            )
        elif not should_offline and was_paused and not old.get("manual_offline"):
            resume_acct(acct, meta)
            transitions.append(
                f"🟢 {acct} recovered → resume (5h={h5*100:.0f}% 7d={d7*100:.0f}%)"
            )

        state[acct] = {
            "tier": tier,
            "cause": cause,
            "manual_mode": manual_mode,
            "h5_pct": h5 * 100,
            "d7_pct": d7 * 100,
            "paused": should_offline,
            "manual_offline": False,
            "restore_at": restore_at,
            "ts": now_ts(),
        }

        log(f"{acct}: {tier} 5h={h5*100:.0f}% 7d={d7*100:.0f}% paused={should_offline}")
        time.sleep(random.uniform(0.5, 2.0))

    save_state(state)
    log(f"done: probed={probed} skipped={skipped} transitions={len(transitions)}")

    if transitions:
        alert_feishu("CC Max quota-rebalance:\n" + "\n".join(transitions))


if __name__ == "__main__":
    main()
