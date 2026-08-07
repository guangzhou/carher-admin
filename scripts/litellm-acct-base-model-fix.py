#!/usr/bin/env python3
"""给 198 prod chatgpt-acct entry 设 model_info.base_model, 让 anthropic_messages 路径
能解析到真实价表键(保留 priority/272k 分层价)。不改任何价格数字。
用法: patch_base_model.py <variant|ALL> [--apply]   /  --revert 撤销
"""
import json, subprocess, sys, urllib.request

BASE = {  # entry 变体 -> 内置价表键
    "gpt-5.5":       "gpt-5.5",
    "gpt-5.4":       "gpt-5.4",
    "gpt-5.6-sol":   "gpt-5.6-sol",
    "gpt-5.6-luna":  "gpt-5.6-luna",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.3-codex": "gpt-5.3-codex",
    # codex-auto-review 不动: 实测已解析到 gpt-5.6-luna 且正常计费
}
def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
MK = sh("kubectl -n litellm-product get secret litellm-secrets -o jsonpath={.data.LITELLM_MASTER_KEY} | base64 -d")
BACKUP = json.load(open("/tmp/acct35_backup_hzl.json"))
target, apply_, revert = sys.argv[1], "--apply" in sys.argv, "--revert" in sys.argv
for mid in sorted(BACKUP):
    variant = mid.split("-", 3)[3]
    if variant not in BASE or (target != "ALL" and variant != target):
        continue
    mi = {"id": mid, "mode": "responses", "db_model": True}
    if not revert:
        mi["base_model"] = BASE[variant]
    if not apply_:
        print(f"  DRY {mid} base_model={mi.get('base_model')}"); continue
    req = urllib.request.Request(f"http://127.0.0.1:30402/model/{mid}/update",
        data=json.dumps({"model_info": mi}).encode(),
        headers={"Authorization": f"Bearer {MK}", "Content-Type": "application/json"}, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=30) as r: print(f"  OK {mid} base_model={mi.get('base_model')} http={r.status}")
    except Exception as e: print(f"  FAIL {mid} {e}")
