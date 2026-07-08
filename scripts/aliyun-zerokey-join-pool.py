#!/usr/bin/env python3
"""
aliyun-zerokey-join-pool.py — 让阿里云原生 zerokey 成员并入/退出 chatgpt-gpt-5.5
轮询组(her 发 gpt-5.5 → model_group_alias → chatgpt-gpt-5.5 组 least-busy)。

机制(DB 重注册,零改 CM、零 rollout litellm-proxy):
  join     : 把指定 zerokey 成员从 model_name=zerokey-pool 改注册为
             model_name=chatgpt-gpt-5.5(delete 旧 + new 新,同 id/api_base/rpm)。
             → 立即进 17 成员轮询,所有 her 自动生效。
  rollback : 反向,改回 model_name=zerokey-pool(退出 her 轮询,回落纯 acct 池)。
  status   : 列 chatgpt-gpt-5.5 / zerokey-pool 两组成员 + 最近 SpendLogs 流量。

灰度:传账号子集,如 `join 69 71 72`(先并 3 个观察),稳了再 `join 73 74 75 77 78`。
回滚:`rollback 69 71 72 73 74 75 77 78`(或任意子集),秒级摘除。
幂等:已在目标组的成员跳过;api_base/rpm 从 litellm 实时读取(不硬编码,防漂移)。

在有 carher kubectl 的机器上跑(经 litellm-proxy pod exec,免隧道):
  python3 scripts/aliyun-zerokey-join-pool.py status
  python3 scripts/aliyun-zerokey-join-pool.py join 69 71 72 --apply
  python3 scripts/aliyun-zerokey-join-pool.py rollback 69 71 72 --apply

不传 --apply = dry-run(只打印将做什么)。
"""
import argparse
import json
import subprocess
import sys

NS = "carher"
POOL = "zerokey-pool"            # 原始组名(退出态)
TARGET = "chatgpt-gpt-5.5"       # her 轮询组(并入态)
ID_PREFIX = "zerokey-pool-aliyun-"


def proxy_api(path, body=None, method="GET"):
    """经 litellm-proxy pod 内 localhost:4000 调 API(用 pod 自身 master key)。
    body 经 stdin 传入(避免命令行内联 JSON 的 shell 转义问题)。"""
    stdin_payload = json.dumps({"path": path, "method": method, "body": body})
    # pod 内脚本从 stdin 读参数,零命令行转义
    script = (
        "import os,sys,json,urllib.request,urllib.error\n"
        "a=json.load(sys.stdin)\n"
        "MK=os.environ['LITELLM_MASTER_KEY']\n"
        "data=json.dumps(a['body']).encode() if a['body'] is not None else None\n"
        "req=urllib.request.Request('http://localhost:4000'+a['path'],data=data,"
        "headers={'Authorization':'Bearer '+MK,'Content-Type':'application/json'},method=a['method'])\n"
        "try:\n"
        "    r=urllib.request.urlopen(req,timeout=90); print(r.read().decode())\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(json.dumps({'_http_error':e.code,'_body':e.read().decode()[:300]}))\n"
    )
    p = subprocess.run(
        ["kubectl", "exec", "-i", "-n", NS, "deploy/litellm-proxy", "-c", "litellm",
         "--", "python3", "-c", script],
        input=stdin_payload.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out = p.stdout.decode().strip()
    if p.returncode != 0:
        return {"_exec_error": p.returncode, "_stderr": p.stderr.decode()[:300], "_out": out[:300]}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"_raw": out}


def members(model_name):
    """返回 {id: litellm_params} for a model group (仅 zerokey-pool-aliyun-* id)。"""
    d = proxy_api("/v1/model/info")
    out = {}
    for m in d.get("data", []):
        mi = m.get("model_info") or {}
        mid = mi.get("id", "")
        if m.get("model_name") == model_name and str(mid).startswith(ID_PREFIX):
            out[mid] = m.get("litellm_params") or {}
    return out


def all_zerokey_members():
    """所有 zerokey 成员(不论在哪个组): {id: (current_model_name, litellm_params)}。"""
    d = proxy_api("/v1/model/info")
    out = {}
    for m in d.get("data", []):
        mi = m.get("model_info") or {}
        mid = mi.get("id", "")
        if str(mid).startswith(ID_PREFIX):
            out[mid] = (m.get("model_name"), m.get("litellm_params") or {})
    return out


def reregister(mid, lp, new_model_name, apply):
    api_base = lp.get("api_base")
    rpm = lp.get("rpm", 30)
    if not api_base:
        print(f"  ! {mid} 缺 api_base,跳过"); return False
    if not apply:
        print(f"  would: {mid} → model_name={new_model_name} (api_base={api_base})")
        return True
    proxy_api("/model/delete", {"id": mid}, method="POST")
    r = proxy_api("/model/new", {
        "model_name": new_model_name,
        "litellm_params": {"model": "openai/gpt-5-5", "api_base": api_base,
                           "api_key": "raw", "rpm": rpm,
                           "input_cost_per_token": 5e-6,
                           "output_cost_per_token": 3e-5},
        "model_info": {"id": mid},
    }, method="POST")
    ok = r.get("model_id") == mid
    print(f"  {'✓' if ok else '✗'} {mid} → {new_model_name}" + ("" if ok else f"  {r}"))
    return ok


def cmd_join(accts, apply):
    cur = all_zerokey_members()
    ids = [ID_PREFIX + str(a) for a in accts]
    print(f"JOIN → {TARGET} (apply={apply})")
    for mid in ids:
        if mid not in cur:
            print(f"  ! {mid} 不存在(未注册),跳过"); continue
        name, lp = cur[mid]
        if name == TARGET:
            print(f"  = {mid} 已在 {TARGET},跳过"); continue
        reregister(mid, lp, TARGET, apply)
    if apply:
        n = len(members(TARGET))
        print(f"现 {TARGET} 组 zerokey 成员数: {n}")


def cmd_rollback(accts, apply):
    cur = all_zerokey_members()
    ids = [ID_PREFIX + str(a) for a in accts]
    print(f"ROLLBACK → {POOL} (apply={apply})")
    for mid in ids:
        if mid not in cur:
            print(f"  ! {mid} 不存在,跳过"); continue
        name, lp = cur[mid]
        if name == POOL:
            print(f"  = {mid} 已在 {POOL},跳过"); continue
        reregister(mid, lp, POOL, apply)


def cmd_status(*_):
    d = proxy_api("/v1/model/info")
    from collections import defaultdict
    groups = defaultdict(list)
    for m in d.get("data", []):
        mi = m.get("model_info") or {}
        mid = mi.get("id", "")
        if str(mid).startswith(ID_PREFIX):
            groups[m.get("model_name")].append(mid)
    print("=== zerokey 成员当前分组 ===")
    for g in (TARGET, POOL):
        ms = sorted(groups.get(g, []))
        print(f"  {g}: {len(ms)} 个  {[x.replace(ID_PREFIX,'') for x in ms]}")
    # chatgpt-gpt-5.5 总成员数(含 acct)
    total = sum(1 for m in d.get("data", []) if m.get("model_name") == TARGET)
    print(f"  {TARGET} 组总成员(含 acct): {total}")
    # 最近流量(SpendLogs)
    print("=== 最近 SpendLogs 里 zerokey 成员流量 ===")
    try:
        logs = proxy_api("/spend/logs")
        from collections import Counter
        c = Counter()
        for row in (logs if isinstance(logs, list) else []):
            mid = str(row.get("model_id") or row.get("model") or "")
            if ID_PREFIX in mid:
                c[mid] += 1
        if c:
            for k, v in c.most_common():
                print(f"  {v}  {k}")
        else:
            print("  (SpendLogs 中暂无 zerokey 成员流量 / 或 /spend/logs 未返回明细)")
    except Exception as e:
        print(f"  spend/logs 查询失败: {str(e)[:80]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["join", "rollback", "status"])
    ap.add_argument("accts", nargs="*", help="账号编号,如 69 71 72(status 不需要)")
    ap.add_argument("--apply", action="store_true", help="真正执行(默认 dry-run)")
    a = ap.parse_args()
    if a.cmd == "status":
        cmd_status()
    elif a.cmd == "join":
        if not a.accts: sys.exit("join 需要账号列表,如 join 69 71 72")
        cmd_join(a.accts, a.apply)
    elif a.cmd == "rollback":
        if not a.accts: sys.exit("rollback 需要账号列表")
        cmd_rollback(a.accts, a.apply)


if __name__ == "__main__":
    sys.exit(main())
