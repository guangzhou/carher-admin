#!/usr/bin/env python3
"""在 litellm-proxy pod 内跑：读本进程 router 实际生效的重试次数（不是 DB 里写的值）。

用法（宿主机）：kubectl exec -i <pod> -c litellm -- python3 - < live_retry_policy_check.py
输出一行：sa-grok-4.7 对 BadGatewayError / RateLimitError 实际会重试几次。

注意：这里起的是新 python 进程，读不到 proxy 进程内存里的 router。所以走 HTTP
问 proxy 自己（/router/settings 或 /get/config/... 的形状因版本而异，都试）。
"""
import json
import os
import urllib.error
import urllib.request

B = "http://127.0.0.1:4000"
H = {"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"]}


def get(path):
    try:
        with urllib.request.urlopen(urllib.request.Request(B + path, headers=H), timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


for path in ("/router/settings", "/get/router_settings", "/config/list?config_type=router_settings"):
    st, d = get(path)
    if st == 200 and isinstance(d, (dict, list)):
        blob = json.dumps(d)
        i = blob.find('"model_group_retry_policy"')
        j = blob.find('"sa-grok-4.7"', i) if i >= 0 else -1
        print(f"path={path} status=200 has_mgrp={i >= 0} sa-grok-4.7={blob[j:j + 140] if j >= 0 else None}")
        break
    print(f"path={path} status={st}")
