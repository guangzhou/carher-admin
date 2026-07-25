#!/usr/bin/env python3
# manual-probe-offline.py — 一次性对 manual_offline=True 的 acct 强制 pod-probe
#
# 用法（188 上跑）：
#   python3 /home/cltx/manual-probe-offline.py
#
# 输入：TARGETS 列表（下方硬编码，按 acct-id 顺序）
# 复用：/home/cltx/quota-rebalance.py 里的 probe_upstream_via_pod / resume_acct /
#       scale_deploy / load_state / save_state / POOL_ACCOUNTS
# 输出：
#   - 每 acct 3-5 行进度到 stdout + 日志文件
#   - 结尾汇总表
#   - state.json 就地更新（走 quota-rebalance.py 的 save_state，跟 cron 一套路径）
#   - 完整日志落 /home/cltx/.chatgpt-quota/manual-probe-YYYYMMDD-HHMMSS.log
#
# 决策约定（与 chat 对齐 2026-07-01）：
#   Q1=B: ALIVE → 完整 resume（scale=1 + 注册 3 entries + clear manual_offline + tier 交 classify）
#   Q2=A: 完全串行，每 acct 间隔 5s
#   Q3:   不熔断，跑完全部
#   Q5:   ALIVE 复活；DEAD 写 cause + manual_offline 保持；STILL_CAP/ERROR 实事求是；
#         异常导致 pod 起来但探测失败 → 留着 pod 明说，不再叠 scale 操作

import sys
import os
import time
from datetime import datetime, timezone

# 复用 quota-rebalance.py（放同目录 /home/cltx/）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util
_qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quota-rebalance.py")
_spec = importlib.util.spec_from_file_location("quota_rebalance", _qr_path)
qr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qr)

TARGETS = [
    "acct-47", "acct-57", "acct-58",
    "acct-63", "acct-64", "acct-65",
    "acct-2", "acct-15",
]

INTER_ACCT_SLEEP = 5  # 秒

STAMP = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
LOG_PATH = f"/home/cltx/.chatgpt-quota/manual-probe-{STAMP}.log"


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def probe_one(acct, state):
    """返回 (verdict, detail_dict)。verdict 就是 result 字段。"""
    meta = qr.POOL_ACCOUNTS.get(acct)
    if not meta:
        return "no_meta", {"err": "not in POOL_ACCOUNTS"}

    s = state.get(acct, {})
    _log(f"{acct}: START tier={s.get('tier')} mo={s.get('manual_offline')} "
         f"c401={s.get('consecutive_401')} cause={s.get('cause')!r}")

    # 走已有的 probe_upstream_via_pod（内部 scale=1 + wait_ready + sync_tmp + curl usage）
    result, p_pct, w_pct, p_reset, w_reset, err = qr.probe_upstream_via_pod(acct)
    _log(f"{acct}:   probe result={result} 5h={p_pct} wk={w_pct} err={err!r}")

    now = qr.now_ts()

    if result == "alive":
        # Q5: 完整 resume（跟 v3 alive 分支等价）
        n = qr.resume_acct(acct, meta)
        _log(f"{acct}:   resume registered {n}/{len(qr.CHATGPT_MODELS)} entries")
        # 刷 state：清 manual_offline / consecutive_401 / paused，tier 交下轮 classify
        new_s = {
            **s,
            "manual_offline": False,
            "consecutive_401": 0,
            "paused": False,
            "primary_pct": p_pct,
            "weekly_pct": w_pct,
            "primary_reset_at": p_reset,
            "weekly_reset_at": w_reset,
            "cause": "revived_by_manual_probe",
            "ts": now,
            "revive_probe_cooldown_until": 0,
            "revive_probe_consecutive_fails": 0,
            "pending_scale_down": False,
        }
        # tier 由 classify 决定 —— 但我们已有 p_pct/w_pct，直接算
        if w_pct >= 100:
            new_s["tier"] = "OFFLINE-WEEK"
        elif p_pct >= 100:
            new_s["tier"] = "OFFLINE-5H"
        elif p_pct >= 50 or w_pct >= 50:
            new_s["tier"] = "SLOW"
        else:
            new_s["tier"] = "HEALTHY"
        state[acct] = new_s
        return result, {"entries_registered": n, "5h": p_pct, "wk": w_pct, "new_tier": new_s["tier"]}

    if result == "token_invalid":
        # 真死 → 保持 SCALED_DOWN + manual_offline + 明确 cause
        # 顺手 scale=0（探测起了 pod，不留活）
        qr.scale_deploy(acct, 0)
        new_s = {
            **s,
            "manual_offline": True,
            "paused": True,
            "tier": "TOKEN_INVALID",
            "cause": "token_dead_401 (manual probe confirmed 2026-07-01)",
            "ts": now,
        }
        state[acct] = new_s
        return result, {"action": "scale=0, manual_offline kept"}

    if result == "still_cap":
        # 上游真 cap
        qr.scale_deploy(acct, 0)
        new_s = {
            **s,
            "manual_offline": True,
            "paused": True,
            "tier": "SCALED_DOWN",
            "cause": f"REVIVE_PROBE_STILL_CAP:5h={int(p_pct)}%/wk={int(w_pct)}% (manual probe 2026-07-01)",
            "primary_pct": p_pct,
            "weekly_pct": w_pct,
            "primary_reset_at": p_reset,
            "weekly_reset_at": w_reset,
            "ts": now,
        }
        state[acct] = new_s
        return result, {"5h": p_pct, "wk": w_pct}

    # error — pod 可能 scale=1 起来但后续步骤挂了；按 Q3 决策：留 pod 明说，不再叠 scale
    new_s = {
        **s,
        "cause": f"REVIVE_PROBE_ERROR:{err} (manual probe 2026-07-01)",
        "ts": now,
    }
    state[acct] = new_s
    return result, {"err": err, "action": "pod left as-is (see error)"}


def main():
    _log(f"=== BEGIN manual-probe-offline stamp={STAMP} targets={len(TARGETS)} ===")
    _log(f"targets: {TARGETS}")

    state = qr.load_state()

    results = {}   # acct → (verdict, detail)
    for i, acct in enumerate(TARGETS):
        try:
            verdict, detail = probe_one(acct, state)
        except Exception as e:
            _log(f"{acct}: FATAL {type(e).__name__}: {e}")
            verdict, detail = "fatal", {"err": f"{type(e).__name__}: {e}"}
        results[acct] = (verdict, detail)
        # 每探完一个就落盘一次，防中途挂掉丢进度
        try:
            qr.save_state(state)
            _log(f"{acct}: state saved (verdict={verdict})")
        except Exception as e:
            _log(f"{acct}: save_state FAIL {type(e).__name__}: {e}")
        if i < len(TARGETS) - 1:
            _log(f"--- sleep {INTER_ACCT_SLEEP}s ---")
            time.sleep(INTER_ACCT_SLEEP)

    # 汇总
    _log("")
    _log("=== SUMMARY ===")
    buckets = {"alive": [], "token_invalid": [], "still_cap": [], "error": [], "fatal": [], "no_meta": []}
    for a, (v, d) in results.items():
        buckets.setdefault(v, []).append((a, d))
    for k in ("alive", "token_invalid", "still_cap", "error", "fatal", "no_meta"):
        rows = buckets.get(k, [])
        if not rows:
            continue
        _log(f"  {k:14s} ({len(rows)}):")
        for a, d in rows:
            _log(f"    {a}: {d}")
    _log(f"=== END manual-probe-offline stamp={STAMP} log={LOG_PATH} ===")


if __name__ == "__main__":
    main()
