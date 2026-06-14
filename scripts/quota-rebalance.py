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
  │ manual_offline                │ SKIP（不探测，不自动恢复）     │
  │                               │ ⚠️ 瞬态401也会触发，恢复需   │
  │                               │   手动重置state+重注册entry  │
  │ paused + now < restore_at    │ SKIP（不探测，等 reset）       │
  │ paused + now >= restore_at   │ PROBE → 如果恢复则 resume      │
  │ online + 5h<50% + wk<50%    │ 低频（上次<25min→SKIP）        │
  │ online + 5h 50~80%          │ 中频（上次<12min→SKIP）        │
  │ online + 5h>80%             │ 高频（每次都探）                │
  │ 探测 401                     │ 标记 manual_offline + 下线      │
  └─────────────────────────────────────────────────────────────┘

  手动恢复 manual_offline 账号：
    1. 确认 token 恢复（chatgpt-acct-usage.sh 能正常探测）
    2. 编辑 state.json: manual_offline→false, paused→false
    3. 重新注册 4 个 entry: /model/new chatgpt-acct-N-gpt-5.{5,4,3-codex,4-pro}

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
import base64
import random
import subprocess
import urllib.request
import urllib.error
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
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
JITTER_MAX = int(os.environ.get("REBALANCE_JITTER", "180"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

# 198 prod 的 4 个 chatgpt model groups
CHATGPT_MODELS = [
    {"model_name": "chatgpt-gpt-5.5", "litellm_model": "openai/chatgpt-gpt-5.5"},
    {"model_name": "chatgpt-gpt-5.4", "litellm_model": "openai/chatgpt-gpt-5.4"},
    {"model_name": "chatgpt-gpt-5.3-codex", "litellm_model": "openai/chatgpt-gpt-5.3-codex"},
    {"model_name": "chatgpt-gpt-5.4-pro", "litellm_model": "openai/chatgpt-gpt-5.4-pro"},
]

# 198 prod pool 中的账号
# location=188 → auth.json 在本机 /Data/chatgpt-auth/acct-N/
# location=187 → auth.json 在 187，通过 SSH 远程读取探测
POOL_ACCOUNTS = {
    "acct-1":  {"port": 4001, "location": "188"},
    "acct-4":  {"port": 4004, "location": "188"},
    "acct-22": {"port": 4022, "location": "188"},
    "acct-23": {"port": 4023, "location": "188"},
    "acct-24": {"port": 4024, "location": "188"},
    "acct-25": {"port": 4025, "location": "188"},
    "acct-2":  {"port": 4002, "location": "187"},
    "acct-15": {"port": 4015, "location": "187"},
    "acct-17": {"port": 4017, "location": "187"},
}

SSH_187_HOST = os.environ.get("SSH_187_HOST", "10.68.13.187")
SSH_187_USER = os.environ.get("SSH_187_USER", "cltx")
SSH_187_AUTH_DIR = os.environ.get("SSH_187_AUTH_DIR", "/Data/chatgpt-auth")

PROBE_INTERVAL_LOW = 25 * 60   # 5h<50% → 至少 25min 间隔
PROBE_INTERVAL_MID = 12 * 60   # 5h 50~80% → 至少 12min 间隔


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

    if s.get("manual_offline"):
        return False, "manual_offline"

    # 已 quota 下线 → 看 restore_at 是否到期
    if s.get("paused"):
        restore_at = s.get("restore_at", 0)
        if restore_at and now < restore_at:
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
    aid = auth.get("account_id")
    if not aid:
        seg = auth["id_token"].split(".")[1]
        seg += "=" * (-len(seg) % 4)
        claims = json.loads(base64.urlsafe_b64decode(seg))
        aid = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
    return tok, aid


def parse_account(auth_path):
    auth = json.load(open(auth_path))
    return parse_auth_json(auth)


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
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise
            if attempt < 3:
                time.sleep(2 * attempt + random.random())
        except Exception:
            if attempt < 3:
                time.sleep(2 * attempt + random.random())
    raise RuntimeError("fetch_usage failed after 3 attempts")


def classify(usage):
    """返回 (tier, cause, p_pct, w_pct, restore_at_epoch)"""
    rl = usage["rate_limit"]
    pw = rl["primary_window"]
    sw = rl["secondary_window"]
    p_pct = pw["used_percent"]
    w_pct = sw["used_percent"]
    p_reset_at = pw.get("reset_at")
    w_reset_at = sw.get("reset_at")

    if w_pct >= 95:
        return "OFFLINE-WEEK", f"wk={w_pct}%>=95", p_pct, w_pct, w_reset_at
    if p_pct >= 95:
        return "OFFLINE-5H", f"5h={p_pct}%>=95", p_pct, w_pct, p_reset_at
    if p_pct >= 50 or w_pct >= 50:
        return "SLOW", f"5h={p_pct}%/wk={w_pct}%", p_pct, w_pct, None
    return "HEALTHY", None, p_pct, w_pct, None


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


def acct_api_base(acct, meta):
    if meta["location"] == "188":
        return f"http://10.68.13.188:{meta['port']}"
    elif meta["location"] == "187":
        return f"http://10.68.13.187:{meta['port']}"
    else:
        return f"http://localhost:{meta['port']}"


def acct_api_key(meta):
    if meta["location"] == "188":
        return LITELLM_MK_188
    elif meta["location"] == "187":
        return LITELLM_MK_187
    else:
        return LITELLM_MK_188


def pause_acct(acct, meta):
    if DRY_RUN:
        log(f"  [DRY_RUN] would pause {acct}")
        return 0
    ab = acct_api_base(acct, meta)
    deleted = 0
    # 通过 api_base 匹配删除（比 ID 匹配更可靠）
    status, data = api_request("GET", "/v1/model/info")
    if status != 200:
        log(f"  pause {acct}: /model/info failed HTTP {status}")
        return 0
    for e in data.get("data", []):
        e_ab = (e.get("litellm_params") or {}).get("api_base", "")
        e_id = (e.get("model_info") or {}).get("id", "")
        if e_ab == ab:
            s2, _ = api_request("POST", "/model/delete", {"id": e_id})
            if s2 == 200:
                deleted += 1
                log(f"  deleted {e_id}")
            else:
                log(f"  delete {e_id} failed: HTTP {s2}")
    log(f"  {acct} paused: {deleted} entries deleted")
    return deleted


def resume_acct(acct, meta):
    if DRY_RUN:
        log(f"  [DRY_RUN] would resume {acct}")
        return 0
    ab = acct_api_base(acct, meta)
    ak = acct_api_key(meta)
    created = 0
    for m in CHATGPT_MODELS:
        mid = f"chatgpt-{acct}-{m['model_name'].replace('chatgpt-','')}"
        entry = {
            "model_name": m["model_name"],
            "litellm_params": {
                "model": m["litellm_model"],
                "api_base": ab,
                "api_key": ak,
            },
            "model_info": {"id": mid, "mode": "responses"},
        }
        status, resp = api_request("POST", "/model/new", entry)
        if status == 200:
            created += 1
            log(f"  created {mid}")
        elif "already exists" in str(resp).lower():
            log(f"  skip existing {mid}")
        else:
            log(f"  create {mid} failed: HTTP {status} {str(resp)[:100]}")
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
        do_probe, reason = should_probe(acct, meta, state)

        if not do_probe:
            skipped += 1
            log(f"{acct}: SKIP ({reason})")
            continue

        try:
            if meta["location"] == "187":
                tok, aid = parse_account_remote(acct)
            else:
                auth_path = f"{ACCOUNTS_DIR}/{acct}/auth.json"
                if not Path(auth_path).exists():
                    log(f"{acct}: auth.json not found, skip")
                    skipped += 1
                    continue
                tok, aid = parse_account(auth_path)
            usage = fetch_usage(tok, aid)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log(f"{acct}: 401 → token_invalidated")
                old_s = state.get(acct, {})
                if not old_s.get("manual_offline"):
                    pause_acct(acct, meta)
                    transitions.append(f"🔴 {acct} token_invalidated → auto pause")
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
        tier, cause, p_pct, w_pct, restore_at = classify(usage)
        old = state.get(acct, {})
        was_paused = old.get("paused", False)
        should_offline = tier in ("OFFLINE-5H", "OFFLINE-WEEK")

        if should_offline and not was_paused:
            pause_acct(acct, meta)
            transitions.append(
                f"🔴 {acct} {cause} → pause (reset ~{fmt_eta(restore_at)})"
            )
        elif not should_offline and was_paused and not old.get("manual_offline"):
            resume_acct(acct, meta)
            transitions.append(
                f"🟢 {acct} recovered → resume (5h={p_pct}% wk={w_pct}%)"
            )

        state[acct] = {
            "tier": tier,
            "cause": cause,
            "primary_pct": p_pct,
            "weekly_pct": w_pct,
            "paused": should_offline,
            "manual_offline": False,
            "restore_at": restore_at,
            "ts": now_ts(),
        }

        log(f"{acct}: {tier} 5h={p_pct}% wk={w_pct}% paused={should_offline}")
        time.sleep(random.uniform(0.5, 2.0))

    save_state(state)
    log(f"done: probed={probed} skipped={skipped} transitions={len(transitions)}")

    if transitions:
        alert_feishu("ChatGPT quota-rebalance:\n" + "\n".join(transitions))


if __name__ == "__main__":
    main()
