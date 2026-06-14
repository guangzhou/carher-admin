#!/usr/bin/env python3
"""把 1 个新 chatgpt acct 加入 188 cron 的 POOL_ACCOUNTS + state.json。

幂等：重复跑无害，已存在则 SKIP；写完会 dry-run 跑一次 quota-rebalance.py 自检。

用法:
    ./chatgpt-pool-account-add.py <N> <PORT>
    例: ./chatgpt-pool-account-add.py 26 4026

约束:
    - 通过 jms ssh JSZX-AI-03 远程改文件，本地不直接写
    - quota-rebalance.py 改 POOL_ACCOUNTS 字典 (text-based 编辑)
    - state.json 用 json round-trip 改
    - 写前都备份: .bak-<acct>-<stamp>
"""

import json
import os
import subprocess
import sys
import time

REMOTE = "JSZX-AI-03"
QR_PY = "/home/cltx/quota-rebalance.py"
STATE_JSON = "/home/cltx/.chatgpt-quota/state/state.json"
ENV_FILE = "/home/cltx/.chatgpt-quota/env"


def jssh(cmd: str, check: bool = True) -> str:
    """jms ssh wrapper. Returns stdout."""
    full = ["jms", "ssh", REMOTE, cmd]
    r = subprocess.run(full, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"FATAL: jms ssh failed (rc={r.returncode})", file=sys.stderr)
        print(f"  cmd: {cmd}", file=sys.stderr)
        print(f"  stderr: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    return r.stdout


def main():
    if len(sys.argv) != 3:
        print("usage: chatgpt-pool-account-add.py <N> <PORT>", file=sys.stderr)
        sys.exit(1)
    acct_n = int(sys.argv[1])
    port = int(sys.argv[2])
    acct_key = f"acct-{acct_n}"
    stamp = time.strftime("%Y%m%d-%H%M%S")

    print(f"=== add {acct_key} port={port} location=198 ===")

    # ── 1. POOL_ACCOUNTS in quota-rebalance.py ─────────────────────
    print(f"[1] check POOL_ACCOUNTS in {QR_PY}")
    src = jssh(f"cat {QR_PY}")
    line_marker = f'"{acct_key}": {{"port": {port}, "location": "198"}},'
    if f'"{acct_key}":' in src:
        print(f"  SKIP: {acct_key} already in POOL_ACCOUNTS")
    else:
        print(f"  ADD: {line_marker}")
        jssh(f"cp {QR_PY} {QR_PY}.bak-{acct_n}-{stamp}")
        # 在 POOL_ACCOUNTS 字典结尾的 } 前插入 (找最后一个 acct-NN 条目所在的块)
        # 安全做法: 在 "POOL_ACCOUNTS = {" 行下找 closing brace
        new_src = []
        in_pool = False
        brace_depth = 0
        inserted = False
        for line in src.splitlines(keepends=True):
            if "POOL_ACCOUNTS" in line and "{" in line:
                in_pool = True
                brace_depth = line.count("{") - line.count("}")
                new_src.append(line)
                continue
            if in_pool:
                brace_depth += line.count("{") - line.count("}")
                if brace_depth <= 0 and not inserted:
                    # 在 closing } 行前插入
                    indent = " " * 4
                    new_src.append(f'{indent}{line_marker}\n')
                    inserted = True
                    in_pool = False
            new_src.append(line)
        if not inserted:
            print("FATAL: 无法定位 POOL_ACCOUNTS 字典", file=sys.stderr)
            sys.exit(2)
        tmpfile = f"/tmp/qr-{acct_n}-{stamp}.py"
        # base64 encode 写过去防 quote 问题
        import base64
        b64 = base64.b64encode("".join(new_src).encode()).decode()
        jssh(f"echo {b64} | base64 -d > {tmpfile} && mv {tmpfile} {QR_PY}")
        print(f"  ✅ written, backup={QR_PY}.bak-{acct_n}-{stamp}")

    # ── 2. state.json ───────────────────────────────────────────────
    print(f"[2] check state.json")
    state_raw = jssh(f"cat {STATE_JSON}")
    state = json.loads(state_raw)
    if acct_key in state:
        cur = state[acct_key]
        print(f"  SKIP: {acct_key} already in state.json")
        print(f"        manual_offline={cur.get('manual_offline')}, paused={cur.get('paused')}")
    else:
        new_entry = {
            "tier": "FAST",
            "paused": False,
            "manual_offline": False,
            "restore_at": None,
            "primary_pct": 0,
            "weekly_pct": 0,
            "last_check": 0,
            "cause": f"onboarded 198 K3s {stamp}",
        }
        state[acct_key] = new_entry
        print(f"  ADD: {json.dumps(new_entry)}")
        jssh(f"cp {STATE_JSON} {STATE_JSON}.bak-{acct_n}-{stamp}")
        new_json = json.dumps(state, indent=2)
        import base64
        b64 = base64.b64encode(new_json.encode()).decode()
        jssh(f"echo {b64} | base64 -d > {STATE_JSON}.new && mv {STATE_JSON}.new {STATE_JSON}")
        print(f"  ✅ written, backup={STATE_JSON}.bak-{acct_n}-{stamp}")

    # ── 3. dry-run quota-rebalance.py 自检 ──────────────────────────
    print(f"[3] dry-run quota-rebalance.py")
    out = jssh(
        f"set -a; source {ENV_FILE}; set +a; "
        f"DRY_RUN=1 REBALANCE_JITTER=0 python3 {QR_PY} 2>&1 | grep -E '{acct_key}|ERROR|FATAL' | head -10",
        check=False,
    )
    print(out or "  (no output)")
    if "ERROR" in out or "FATAL" in out or "Traceback" in out:
        print("FATAL: dry-run hit error, please rollback", file=sys.stderr)
        sys.exit(3)

    print(f"\n✅ {acct_key} synced. Next cron (≤5min) will start tracking.")


if __name__ == "__main__":
    main()
