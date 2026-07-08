#!/usr/bin/env python3
"""
188-zerokey-join-pool.py — 让 188 zerokey 成员并入/退出 chatgpt-gpt-5.5
循环池(her 发 gpt-5.5 → model_group_alias → chatgpt-gpt-5.5 组 least-busy)。

机制(DB 重注册,零改 CM、零 rollout litellm-proxy):
  join     : 把指定 zerokey 成员从 model_name=zerokey-pool 改注册为
             model_name=chatgpt-gpt-5.5(delete 旧 + new 新,同 id/api_base/rpm)。
  rollback : 反向,改回 model_name=zerokey-pool。
  status   : 列 chatgpt-gpt-5.5 / zerokey-pool 两组成员。

灰度:传端口子集,如 `join 8123 8124 8125`。
回滚:`rollback 8123 8124 8125`(或任意子集),秒级摘除。
幂等:已在目标组的成员跳过;api_base/rpm 从 litellm 实时读取。

用法(需要 LITELLM_MK 环境变量):
  LITELLM_MK=$MK python3 scripts/188-zerokey-join-pool.py status
  LITELLM_MK=$MK python3 scripts/188-zerokey-join-pool.py join 8123 8124 8125 --apply
  LITELLM_MK=$MK python3 scripts/188-zerokey-join-pool.py join all --apply
  LITELLM_MK=$MK python3 scripts/188-zerokey-join-pool.py rollback 8123 8124 --apply

不传 --apply = dry-run(只打印将做什么)。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30402").rstrip("/")
LITELLM_MK = os.environ.get("LITELLM_MK", "")
POOL = "zerokey-pool"
TARGET = "chatgpt-gpt-5.5"
TARGET_ALIASES = {"chatgpt-gpt-5.5", "gpt-5.5"}
ID_PREFIX = "zk-pool-"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    headers = {"Authorization": f"Bearer {LITELLM_MK}"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(LITELLM_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:300]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def all_zk_members():
    status, data = api("GET", "/v1/model/info")
    if status != 200:
        print(f"ERROR: /v1/model/info returned {status}"); sys.exit(1)
    out = {}
    for m in data.get("data", []):
        mi = m.get("model_info") or {}
        mid = str(mi.get("id", ""))
        if mid.startswith(ID_PREFIX):
            out[mid] = (m.get("model_name"), m.get("litellm_params") or {})
    return out


def reregister(mid, lp, new_model_name, apply):
    api_base = lp.get("api_base")
    rpm = lp.get("rpm", 30)
    if not api_base:
        print(f"  ! {mid} missing api_base, skip"); return False
    if not apply:
        print(f"  would: {mid} → model_name={new_model_name} (api_base={api_base})")
        return True
    api("POST", "/model/delete", {"id": mid})
    _, r = api("POST", "/model/new", {
        "model_name": new_model_name,
        "litellm_params": {
            "model": "openai/gpt-5-5",
            "api_base": api_base,
            "api_key": "raw",
            "rpm": rpm,
            "input_cost_per_token": 5e-6,
            "output_cost_per_token": 3e-5,
        },
        "model_info": {"id": mid},
    })
    ok = isinstance(r, dict) and r.get("model_id") == mid
    print(f"  {'ok' if ok else 'FAIL'} {mid} → {new_model_name}" + ("" if ok else f"  {r}"))
    return ok


def cmd_join(ports, apply):
    cur = all_zk_members()
    ids = [f"{ID_PREFIX}{p}" for p in ports] if ports != ["all"] else sorted(cur.keys())
    print(f"JOIN → {TARGET} (apply={apply})")
    moved = 0
    for mid in ids:
        if mid not in cur:
            print(f"  ! {mid} not registered, skip"); continue
        name, lp = cur[mid]
        if name == TARGET:
            print(f"  = {mid} already in {TARGET}, skip"); continue
        if reregister(mid, lp, TARGET, apply):
            moved += 1
    print(f"moved: {moved}")
    if apply:
        cur2 = all_zk_members()
        in_target = sum(1 for _, (n, _) in cur2.items() if n == TARGET)
        print(f"zk-pool-* now in {TARGET}: {in_target}")


def cmd_rollback(ports, apply):
    cur = all_zk_members()
    ids = [f"{ID_PREFIX}{p}" for p in ports] if ports != ["all"] else sorted(cur.keys())
    print(f"ROLLBACK → {POOL} (apply={apply})")
    for mid in ids:
        if mid not in cur:
            print(f"  ! {mid} not registered, skip"); continue
        name, lp = cur[mid]
        if name == POOL:
            print(f"  = {mid} already in {POOL}, skip"); continue
        reregister(mid, lp, POOL, apply)


def cmd_status():
    cur = all_zk_members()
    in_target = sorted(mid for mid, (n, _) in cur.items() if n in TARGET_ALIASES)
    in_pool = sorted(mid for mid, (n, _) in cur.items() if n == POOL)
    other = sorted(mid for mid, (n, _) in cur.items() if n not in TARGET_ALIASES and n != POOL)

    print(f"=== {TARGET} ({len(in_target)} zk members) ===")
    for mid in in_target:
        _, lp = cur[mid]
        print(f"  {mid}  {lp.get('api_base','?')}")

    print(f"=== {POOL} ({len(in_pool)} zk members) ===")
    for mid in in_pool:
        _, lp = cur[mid]
        print(f"  {mid}  {lp.get('api_base','?')}")

    if other:
        print(f"=== other ({len(other)}) ===")
        for mid in other:
            n, lp = cur[mid]
            print(f"  {mid}  model_name={n}  {lp.get('api_base','?')}")

    # total chatgpt-gpt-5.5 members (including acct)
    status, data = api("GET", "/v1/model/info")
    if status == 200:
        total = sum(1 for m in data.get("data", []) if m.get("model_name") == TARGET)
        print(f"\n{TARGET} total members (incl. acct): {total}")


def main():
    if not LITELLM_MK:
        sys.exit("LITELLM_MK not set")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["join", "rollback", "status"])
    ap.add_argument("ports", nargs="*", help="port numbers (e.g. 8123 8124) or 'all'")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        cmd_status()
    elif a.cmd == "join":
        if not a.ports: sys.exit("join needs port list, e.g.: join 8123 8124 or join all")
        cmd_join(a.ports, a.apply)
    elif a.cmd == "rollback":
        if not a.ports: sys.exit("rollback needs port list")
        cmd_rollback(a.ports, a.apply)


if __name__ == "__main__":
    sys.exit(main())
