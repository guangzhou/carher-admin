#!/usr/bin/env python3
"""
T2 canary reactive-cooldown verify suite.

Runs on 198 via jms wrapper. Tests:
  TC-A          /v1/model/info on canary returns chatgpt-canary-gpt-5.5 x 2
  TC-H1         5 calls happy path 全 200
  TC-E1-real    pin to specific deployment id, capture full response shape
                + Redis cooldown delta
  TC-E5         exhaust both canary deployments (when real failure inducible)
  TC-fallback   验证 canary fallbacks=[] 时整组耗尽返 500 (无 wangsu 兜底)
  TC-prod-pollution
                snap Redis prod-side cooldown set before/after; assert
                'deployment:chatgpt-acct-{49,68}:cooldown' 永远不出现

Driver scp's scripts/litellm-canary-reactive-cooldown/remote_verify.py to
AIYJY-litellm:/tmp/_litellm_canary_verify.py and ssh-executes.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tc", default="all",
                        help="comma-sep TC names; default: all")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    jms = os.path.join(here, "jms")
    local = os.path.join(here, "litellm-canary-reactive-cooldown", "remote_verify.py")
    if not os.path.isfile(local):
        sys.exit(f"FATAL: inner script not found: {local}")
    remote = "/tmp/_litellm_canary_verify.py"

    subprocess.check_call([jms, "scp", local, f"AIYJY-litellm:{remote}"])
    return subprocess.call([jms, "ssh", "AIYJY-litellm",
                            "python3 " + shlex.quote(remote) + " --tc " + shlex.quote(args.tc)])


if __name__ == "__main__":
    sys.exit(main())
