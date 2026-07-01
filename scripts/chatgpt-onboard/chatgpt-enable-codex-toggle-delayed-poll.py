#!/usr/bin/env python3
"""诊断变体: fetch_mailcom_otp 入口先 sleep FETCH_PRE_SLEEP 秒, 再调原 mail.com 自动 fetch.

用途: 验证假设 — toggle 之前自动路径拿到 stale OTP 是因为"OpenAI 刚发邮件但 mail.com
没及时刷出来" (邮件到达延迟), 而不是 "OpenAI 根本不发新 OTP".

env:
  FETCH_PRE_SLEEP   首次 fetch 之前的 sleep 秒数 (默认 120 = 2min)
其他 env 与原脚本一致.

不动原始脚本 — 同 manual-otp 思路, import + monkey-patch 单函数.
"""

import os
import sys
import time
from pathlib import Path
import importlib.util


def _load_original():
    candidate = Path("/work/orig.py")
    if not candidate.exists():
        candidate = Path(__file__).resolve().parent / "chatgpt-enable-codex-toggle.py"
    spec = importlib.util.spec_from_file_location("toggle_orig", str(candidate))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


orig = _load_original()
_orig_fetch = orig.fetch_mailcom_otp

FETCH_PRE_SLEEP = int(os.environ.get("FETCH_PRE_SLEEP", "120"))


def delayed_fetch(pw, request_ts, prev_otp=None):
    # 只在 attempt 1 (prev_otp=None) 时 sleep — retry 时本来就有 click_resend + sleep(5),
    # 不需要再加 120s.
    if FETCH_PRE_SLEEP > 0 and prev_otp is None:
        print(
            f"[diag] sleeping {FETCH_PRE_SLEEP}s before mail.com poll "
            f"(hypothesis: OpenAI email needs ~1-2min to land in mail.com inbox)",
            flush=True,
        )
        time.sleep(FETCH_PRE_SLEEP)
        # 更新 request_ts 让下游 stale-check (如果有) 用新基线
        request_ts = time.time()
    return _orig_fetch(pw, request_ts, prev_otp=prev_otp)


orig.fetch_mailcom_otp = delayed_fetch


if __name__ == "__main__":
    orig.main()
