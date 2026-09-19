#!/usr/bin/env python3
"""zk-image-pool-register.py —— 只管 198 上 `image-2` 这一个组的成员进出。

为什么不复用 188 的 zk-backfill.py：那个脚本以 `zerokey-pool-gpt-5.5` 组成员为基准
补齐所有变体（含 image-2）。本轮目标集是「57 个活跃 acct」，远大于 5.5 组（15 个），
用它会顺手把一堆号塞进 5.5 **聊天**路由 —— 那是没被批准的路由扩面。所以这里只碰
image-2，账号列表必须显式给。

子命令：
  audit                    只读：列出 image-2 成员 + 各自后端 pod 是否在跑
  probe   N [N...]         直连 zero-N:8200 出图判活（**唯一判活判据**，不走组级）
  register N [N...]        幂等注册（先 delete 同 id 再 create）
  prune   [N...]           摘条目；不给账号则摘掉所有「后端不在跑」的条目

判据纪律：
  · 组级 `model=image-2` 打通 ≠ 某个后端活 —— 组里有别的活成员会把死的盖住。
    所以每加一个号，判活一律用 `probe`（直连该 pod），不许拿组级绿灯当证据。
  · register 只在 probe 200 之后做。

用法：
  python3 scripts/zk-image-pool-register.py audit
  python3 scripts/zk-image-pool-register.py probe 81
  python3 scripts/zk-image-pool-register.py register 81 --apply
  python3 scripts/zk-image-pool-register.py prune 86 116 --apply
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

BASE = "http://10.68.13.198:30402/pro"
def _require_env(name: str) -> str:
    """凭据只从环境变量读，缺了直接退出（不内置默认值，避免真 key 落进仓库）。"""
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(
            "缺少环境变量 %s —— 先 export %s=<198 prod master key>（别写进文件/命令行历史）" % (name, name)
        )
    return v

MK = _require_env("LITELLM_MASTER_KEY")
NS = "litellm-product"
H198 = "cltx@10.68.13.198"
GROUP = "image-2"

# 与现网 11 条存量条目逐字段对齐（2026-09-06 从 /model/info 读出）
UPSTREAM = "openai/gpt-image-2"
RPM = 3
IN_COST = 5e-6
OUT_COST = 3e-5


def api(method, path, data=None, timeout=30):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method,
        headers={"Authorization": f"Bearer {MK}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def ssh198(cmd, timeout=90):
    """在 198 上跑 kubectl（本机没有 kubeconfig）。"""
    full = f"export KUBECONFIG=$HOME/.kube/config; {cmd}"
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=15", H198, full],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def members():
    """{acct_int: entry} —— image-2 组现有成员。"""
    out = {}
    for m in api("GET", "/model/info")["data"]:
        if m.get("model_name") != GROUP:
            continue
        mid = str(m.get("model_info", {}).get("id", ""))
        n = re.match(r"zk-(\d+)-image-2$", mid)
        if n:
            out[int(n.group(1))] = m
    return out


def ready_zero_pods():
    """{acct_int: readyReplicas} —— zero-N deployment 的就绪副本数。"""
    rc, out, err = ssh198(f"kubectl -n {NS} get deploy -o json")
    if rc != 0:
        sys.exit(f"kubectl get deploy 失败: {err}")
    res = {}
    for i in json.loads(out)["items"]:
        n = re.fullmatch(r"zero-(\d+)", i["metadata"]["name"])
        if n:
            res[int(n.group(1))] = i["status"].get("readyReplicas", 0) or 0
    return res


def cmd_audit(_args):
    mem = members()
    ready = ready_zero_pods()
    print(f"image-2 成员 {len(mem)} 条")
    dead = []
    for n in sorted(mem):
        r = ready.get(n)
        state = "pod 不存在" if r is None else ("在跑" if r else "0 副本")
        if not r:
            dead.append(n)
        print(f"  zk-{n}-image-2  后端 zero-{n}: {state}")
    if dead:
        print(f"\n后端不在跑的条目 {len(dead)} 条: {' '.join(str(x) for x in dead)}")
        print("  → 摘除: prune --apply")
    return dead


PROBE_JS = r"""
const http=require("http");
const body=JSON.stringify({prompt:process.env.ZKP_PROMPT,n:1});
const req=http.request({host:"127.0.0.1",port:8200,path:"/v1/images/generations",
  method:"POST",headers:{"Content-Type":"application/json","Content-Length":Buffer.byteLength(body)},
  timeout:175000},res=>{let s="";res.on("data",c=>{s+=c});res.on("end",()=>{
    let ok=false,note=s.slice(0,180);
    try{const j=JSON.parse(s);ok=!!(j.data&&j.data[0]&&j.data[0].b64_json&&j.data[0].b64_json.length>1000);
        if(ok)note="b64 "+j.data[0].b64_json.length+" bytes";}catch(e){}
    console.log(JSON.stringify({status:res.statusCode,ok,note}));
  })});
req.on("error",e=>console.log(JSON.stringify({status:0,ok:false,note:e.message})));
req.on("timeout",()=>{req.destroy();console.log(JSON.stringify({status:0,ok:false,note:"timeout"}))});
req.write(body);req.end();
"""


def probe_one(n, prompt):
    """直连 zero-N pod 出图。返回 (ok, note)。"""
    rc, pod, err = ssh198(
        f"kubectl -n {NS} get pod -l app=zero-{n} "
        f"--field-selector=status.phase=Running -o jsonpath='{{.items[0].metadata.name}}'"
    )
    if rc != 0 or not pod:
        return False, "没有 Running 的 zero-%d pod" % n
    js = PROBE_JS.replace("'", "'\"'\"'")
    cmd = (f"kubectl -n {NS} exec {pod} -- env ZKP_PROMPT='{prompt}' node -e '{js}'")
    rc, out, err = ssh198(cmd, timeout=240)
    if rc != 0:
        return False, (err or out)[:200]
    line = [l for l in out.splitlines() if l.startswith("{")]
    if not line:
        return False, out[:200]
    d = json.loads(line[-1])
    return bool(d["ok"]), f"HTTP {d['status']} {d['note']}"


def cmd_probe(args):
    bad = []
    for n in args.accts:
        ok, note = probe_one(n, args.prompt)
        print(f"  zero-{n}: {'✅' if ok else '❌'} {note}", flush=True)
        if not ok:
            bad.append(n)
    if bad:
        print(f"\n不活: {' '.join(str(x) for x in bad)}")
    return bad


def entry(n):
    return {
        "model_name": GROUP,
        "litellm_params": {
            "model": UPSTREAM,
            "api_base": f"http://zero-{n}.{NS}.svc.cluster.local:8200/v1",
            "api_key": "raw",
            "rpm": RPM,
            "input_cost_per_token": IN_COST,
            "output_cost_per_token": OUT_COST,
        },
        "model_info": {"id": f"zk-{n}-image-2", "mode": "image_generation"},
    }


def wait_visible(accts, tries=12, gap=10):
    """litellm-proxy 是 2 副本，/model/new 写 DB 后各副本自刷新有延迟：实测同一秒
    连读三次会先拿到旧表再拿到新表。所以「写完立刻读一次」不是生效判据 ——
    这里要求 **连续 3 次读到全部目标 id** 才算生效。"""
    want = set(accts)
    hit = 0
    for _ in range(tries):
        if want <= set(members()):
            hit += 1
            if hit >= 3:
                return True
        else:
            hit = 0
        __import__("time").sleep(gap)
    return False


def cmd_register(args):
    mem = members()
    for n in args.accts:
        mid = f"zk-{n}-image-2"
        act = "重建(已存在)" if n in mem else "新建"
        if not args.apply:
            print(f"  [dry-run] {act} {mid}")
            continue
        if n in mem:
            try:
                api("POST", "/model/delete", {"id": mid})
            except urllib.error.HTTPError as e:
                print(f"  ⚠ delete {mid} 返回 {e.code}，继续 create")
        api("POST", "/model/new", entry(n))
        print(f"  ✅ {act} {mid}")
    if args.apply:
        ok = wait_visible(args.accts)
        print(f"\n全副本可见: {'✅' if ok else '❌ 超时，别当已生效'}  组内成员数: {len(members())}")


def cmd_prune(args):
    mem = members()
    targets = args.accts
    if not targets:
        ready = ready_zero_pods()
        targets = [n for n in sorted(mem) if not ready.get(n)]
        print(f"后端不在跑 → 待摘 {len(targets)} 条: {' '.join(str(x) for x in targets)}")
    for n in targets:
        mid = f"zk-{n}-image-2"
        if n not in mem:
            print(f"  - {mid} 不在组里，跳过")
            continue
        if not args.apply:
            print(f"  [dry-run] 摘除 {mid}")
            continue
        api("POST", "/model/delete", {"id": mid})
        print(f"  ✅ 摘除 {mid}")
    if args.apply:
        print(f"\n组内成员数: {len(members())}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("audit")

    p = sub.add_parser("probe")
    p.add_argument("accts", nargs="+", type=int)
    p.add_argument("--prompt", default="a small red cube on a white background")

    p = sub.add_parser("register")
    p.add_argument("accts", nargs="+", type=int)
    p.add_argument("--apply", action="store_true")

    p = sub.add_parser("prune")
    p.add_argument("accts", nargs="*", type=int)
    p.add_argument("--apply", action="store_true")

    a = ap.parse_args()
    {"audit": cmd_audit, "probe": cmd_probe,
     "register": cmd_register, "prune": cmd_prune}[a.cmd](a)


if __name__ == "__main__":
    main()
