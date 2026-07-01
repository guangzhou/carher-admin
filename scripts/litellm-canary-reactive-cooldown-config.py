#!/usr/bin/env python3
"""
T2 canary apply/restore for reactive cooldown POC on 198 litellm-product ns.

Plan B (升级版): canary 跑独立 ConfigMap + STORE_MODEL_IN_DB=False，注册的产品名
是 `chatgpt-canary-gpt-5.5`（prod 没有），上游 deployment_id 是
`chatgpt-acct-canary-49/68`（prod 不路由该 id），api_base 指向 prod 的真
chatgpt-acct svc。Redis cooldown key `deployment:chatgpt-acct-canary-N:cooldown`
prod 不识别 → 0 污染。

Actions:
  --apply      起 canary CM + Deploy + Svc；scale=1 acct-49/68；等 svc endpoint
  --teardown   反向：删 canary CM/Deploy/Svc；scale=0 acct-49/68 回原；清 Redis canary cooldown key
  --status     show canary live status (CM revision / proxy pod / cooldown keys)

Driver scp's scripts/litellm-canary-reactive-cooldown/remote_config.py to
AIYJY-litellm:/tmp/_litellm_canary_reactive_cooldown_config.py and ssh-executes
with --apply / --teardown / --status passed through.

Safety:
  - Prod CM/Deploy 0 改动
  - Prod DB ProxyModelTable/VerificationToken 0 写入（canary store_model_in_db=False）
  - Prod Redis cooldown keys：扫描期间 snapshot prod-side count，apply 后再对比
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true")
    group.add_argument("--teardown", action="store_true")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    jms = os.path.join(here, "jms")
    local = os.path.join(here, "litellm-canary-reactive-cooldown", "remote_config.py")
    if not os.path.isfile(local):
        sys.exit(f"FATAL: inner script not found: {local}")

    flag = "--apply" if args.apply else ("--teardown" if args.teardown else "--status")
    remote = "/tmp/_litellm_canary_reactive_cooldown_config.py"

    subprocess.check_call([jms, "scp", local, f"AIYJY-litellm:{remote}"])
    return subprocess.call([jms, "ssh", "AIYJY-litellm",
                            "python3 " + shlex.quote(remote) + " " + flag])


if __name__ == "__main__":
    sys.exit(main())
