#!/usr/bin/env python3
"""手动 OTP 变体: 跟 chatgpt-enable-codex-toggle.py 共用所有 login/toggle 逻辑,
只把 OTP 拉取从 "patchright 登 mail.com 抓邮件" 换成 "轮询人写入的 OTP 文件".

用途: mail.com 收件箱拉不到新 OTP (OpenAI rate-limit / mail.com 邮件丢失) 时,
人工肉眼看一次邮件, 把 6 位码写入 MANUAL_OTP_FILE, 容器自动取到提交.

不动 chatgpt-enable-codex-toggle.py — 后续 acct 走原路径不受影响. 见
[[feedback-chatgpt-otp-manual-fallback]].

新增 env:
  MANUAL_OTP_FILE      容器内 OTP 文件路径 (launcher 默认 /run/manual_otp.txt)
  MANUAL_OTP_TIMEOUT   等 OTP 文件出现 6 位码的最长秒数 (默认 900s = 15min)
其他 env 与原脚本一致 (CHATGPT_EMAIL / CHATGPT_PW_FILE / MAIL_PW_FILE 等).
"""

import os
import re
import sys
import time
from pathlib import Path

# 同目录原脚本 import (docker 内 /work/script.py = 本文件; 原脚本同 mount 到 /work/orig.py)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util


def _load_original():
    # launcher 把原脚本挂到 /work/orig.py
    candidate = Path("/work/orig.py")
    if not candidate.exists():
        # 本地开发兜底: 同目录寻原文件
        candidate = Path(__file__).resolve().parent / "chatgpt-enable-codex-toggle.py"
    spec = importlib.util.spec_from_file_location("toggle_orig", str(candidate))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


orig = _load_original()

MANUAL_OTP_FILE = Path(os.environ.get("MANUAL_OTP_FILE", "/run/manual_otp.txt"))
MANUAL_OTP_TIMEOUT = int(os.environ.get("MANUAL_OTP_TIMEOUT", "900"))
OTP_RE = re.compile(r"\b(\d{6})\b")


def fetch_manual_otp(pw, request_ts, prev_otp=None):
    # pw / request_ts / prev_otp 三参保持跟 orig.fetch_mailcom_otp 同签名 (向后兼容);
    # request_ts 用作 "文件 mtime 必须 ≥ 它" 的新鲜度门槛, prev_otp 用作去重.
    print(
        f"[manual-otp] waiting for OTP at host:$MANUAL_OTP_DIR/manual_otp.txt "
        f"(container={MANUAL_OTP_FILE}); timeout={MANUAL_OTP_TIMEOUT}s prev_otp={prev_otp or 'none'}",
        flush=True,
    )
    print(
        f"[manual-otp] 请用如下命令把 6 位 OTP 写入文件 (在 188 上执行):",
        flush=True,
    )
    print(
        f"[manual-otp]   echo 123456 > $MANUAL_OTP_DIR/manual_otp.txt",
        flush=True,
    )
    deadline = time.time() + MANUAL_OTP_TIMEOUT
    last_warn_ts = 0
    poll_interval = 3
    while time.time() < deadline:
        if MANUAL_OTP_FILE.exists():
            try:
                mtime = MANUAL_OTP_FILE.stat().st_mtime
                content = MANUAL_OTP_FILE.read_text().strip()
            except Exception as exc:
                print(f"  read manual otp file failed (continue): {exc}", flush=True)
                content = ""
                mtime = 0
            if content:
                match = OTP_RE.search(content)
                if match:
                    code = match.group(1)
                    # 新鲜度: 文件必须比当前 fetch 请求新; 否则跳过 (旧 OTP 残留)
                    fresh = mtime >= request_ts - 1.0
                    if not fresh:
                        if time.time() - last_warn_ts > 30:
                            age = int(request_ts - mtime)
                            print(
                                f"  ✗ manual otp file is stale by {age}s "
                                f"(mtime < request_ts), waiting for new write",
                                flush=True,
                            )
                            last_warn_ts = time.time()
                    elif prev_otp and code == prev_otp:
                        if time.time() - last_warn_ts > 30:
                            print(
                                f"  ✗ manual otp {code} == prev_otp, waiting for newer write",
                                flush=True,
                            )
                            last_warn_ts = time.time()
                    else:
                        print(f"  ✓ manual OTP picked: {code}", flush=True)
                        return code
        time.sleep(poll_interval)
    raise RuntimeError(
        f"manual OTP not provided within {MANUAL_OTP_TIMEOUT}s "
        f"(expected 6 digits in {MANUAL_OTP_FILE})"
    )


# Monkey-patch: 用文件版顶掉 patchright 版. orig.otp_loop / orig.login 调用 orig.fetch_mailcom_otp,
# 这里覆盖同 attribute 即可全局生效.
orig.fetch_mailcom_otp = fetch_manual_otp


if __name__ == "__main__":
    orig.main()
