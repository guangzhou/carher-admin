#!/usr/bin/env python3
"""zerokey-pool-register.py — 批量注册 zero-N pods 到 198 prod LiteLLM zerokey-pool

用法:
  python3 zerokey-pool-register.py                    # 注册所有缺失的 zk-N-* 条目
  python3 zerokey-pool-register.py 18 19 20           # 只注册指定编号
  python3 zerokey-pool-register.py --dry-run          # 干跑
  python3 zerokey-pool-register.py --delete 18 19     # 摘除指定 pod 的全部条目
  python3 zerokey-pool-register.py --status           # 查看当前池状态

前提: 从能访问 198 的网络跑（188 或本地 Mac）
"""
import json
import urllib.request
import sys
import time
import os

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30402/pro")
def _require_env(name: str) -> str:
    """凭据只从环境变量读，缺了直接退出。

    不设内置默认值：脚本里写死一个真 master key 等于把凭据提交进仓库，
    而且改 key 之后老默认值还会静默生效。缺了就报错，比悄悄用错的 key 好。
    """
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(
            "缺少环境变量 %s —— 先 export %s=<198 prod master key>（别写进文件/命令行历史）" % (name, name)
        )
    return v

LITELLM_MK = _require_env("LITELLM_MK")

MODELS = [
    {"model_name": "zerokey-pool-gpt-5.5",       "model": "openai/gpt-5-5",       "id_suffix": "gpt-5.5",       "input_cost": 5e-6,   "output_cost": 3e-5},
    {"model_name": "zerokey-pool-gpt-5.4",       "model": "openai/gpt-5.4",       "id_suffix": "gpt-5.4",       "input_cost": 2e-6,   "output_cost": 8e-6},
    {"model_name": "zerokey-pool-gpt-5.3-codex", "model": "openai/gpt-5-3",       "id_suffix": "gpt-5.3-codex", "input_cost": 1e-6,   "output_cost": 4e-6},
    {"model_name": "zerokey-pool-gpt-5.6-sol",   "model": "openai/gpt-5.6-sol",   "id_suffix": "gpt-5.6-sol",   "input_cost": 5e-6,   "output_cost": 3e-5},
    {"model_name": "zerokey-pool-gpt-5.6-terra", "model": "openai/gpt-5.6-terra", "id_suffix": "gpt-5.6-terra", "input_cost": 2.5e-6, "output_cost": 1.5e-5},
    {"model_name": "zerokey-pool-gpt-5.6-luna",  "model": "openai/gpt-5.6-luna",  "id_suffix": "gpt-5.6-luna",  "input_cost": 1e-6,   "output_cost": 6e-6},
]


def api(method, path, data=None):
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        f"{LITELLM_BASE}{path}",
        data=body,
        headers={"Authorization": f"Bearer {LITELLM_MK}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def get_registered():
    """返回已注册的 zk-N 编号集合和全部 zk-* entry IDs"""
    d = api("GET", "/model/info")
    ids = set()
    pods = set()
    for m in d.get("data", []):
        mid = m.get("model_info", {}).get("id", "")
        if mid.startswith("zk-"):
            ids.add(mid)
            parts = mid.split("-")
            if len(parts) >= 3:
                try:
                    pods.add(int(parts[1]))
                except ValueError:
                    pass
    return pods, ids


def register_pod(num, dry_run=False):
    api_base = f"http://zero-{num}.litellm-product.svc.cluster.local:8200/v1"
    ok, fail = 0, 0
    for m in MODELS:
        entry_id = f"zk-{num}-{m['id_suffix']}"
        if dry_run:
            print(f"  DRY  {entry_id}")
            ok += 1
            continue
        payload = {
            "model_name": m["model_name"],
            "litellm_params": {
                "model": m["model"],
                "api_base": api_base,
                "api_key": "raw",
                "rpm": 30,
                "input_cost_per_token": m["input_cost"],
                "output_cost_per_token": m["output_cost"],
            },
            "model_info": {"id": entry_id, "mode": "responses"},
        }
        try:
            api("POST", "/model/new", payload)
            print(f"  OK   {entry_id}")
            ok += 1
        except Exception as e:
            err = e.read().decode()[:200] if hasattr(e, "read") else str(e)
            print(f"  FAIL {entry_id}: {err}")
            fail += 1
        time.sleep(0.05)
    return ok, fail


def delete_pod(num, dry_run=False):
    ok, fail = 0, 0
    for m in MODELS:
        entry_id = f"zk-{num}-{m['id_suffix']}"
        if dry_run:
            print(f"  DRY-DEL  {entry_id}")
            ok += 1
            continue
        try:
            api("POST", "/model/delete", {"id": entry_id})
            print(f"  DEL  {entry_id}")
            ok += 1
        except Exception as e:
            err = e.read().decode()[:200] if hasattr(e, "read") else str(e)
            print(f"  FAIL {entry_id}: {err}")
            fail += 1
        time.sleep(0.05)
    return ok, fail


def show_status():
    from collections import Counter
    d = api("GET", "/model/info")
    c = Counter()
    for m in d.get("data", []):
        mn = m.get("model_name", "")
        if mn.startswith("zerokey-pool"):
            c[mn] += 1
    print("=== zerokey-pool status ===")
    for k in sorted(c):
        print(f"  {k}: {c[k]} deployments")
    total = sum(c.values())
    print(f"  Total: {total} entries ({total // len(MODELS)} pods × {len(MODELS)} models)")


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    delete_mode = "--delete" in args
    status_mode = "--status" in args
    nums = [int(a) for a in args if a.isdigit()]

    if status_mode:
        show_status()
        return

    registered_pods, registered_ids = get_registered()

    if delete_mode:
        if not nums:
            print("--delete requires pod numbers")
            sys.exit(1)
        total_ok = total_fail = 0
        for n in nums:
            print(f"zero-{n}:")
            ok, fail = delete_pod(n, dry_run)
            total_ok += ok
            total_fail += fail
        print(f"\n=== DEL DONE: {total_ok} OK, {total_fail} FAIL ===")
        return

    if nums:
        targets = nums
    else:
        targets = [n for n in sorted(registered_pods | set(nums)) if n not in registered_pods]
        if not targets:
            print("All known pods already registered. Pass pod numbers explicitly to add new ones.")
            show_status()
            return

    total_ok = total_fail = 0
    for n in targets:
        already = sum(1 for m in MODELS if f"zk-{n}-{m['id_suffix']}" in registered_ids)
        if already == len(MODELS) and not dry_run:
            print(f"  SKIP zero-{n} (all {len(MODELS)} entries exist)")
            total_ok += len(MODELS)
            continue
        print(f"zero-{n} ({already}/{len(MODELS)} exist):")
        ok, fail = register_pod(n, dry_run)
        total_ok += ok
        total_fail += fail

    action = "DRY" if dry_run else "DONE"
    print(f"\n=== {action}: {total_ok} OK, {total_fail} FAIL ===")


if __name__ == "__main__":
    main()
