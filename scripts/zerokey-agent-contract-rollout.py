#!/usr/bin/env python3
"""zerokey-agent-contract-rollout.py — 给 zerokey 全队开启「agent 契约」。

**必须在 198 上以 root 执行**（需要 kubectl）::

    scp scripts/zerokey-agent-contract-rollout.py \\
        scripts/chatgpt-onboard/zerokey-codex/zerokey-patch/routes/bpi-codex.js \\
        cltx@10.68.13.198:/tmp/
    ssh cltx@10.68.13.198 'sudo python3 /tmp/zerokey-agent-contract-rollout.py status'
    ssh cltx@10.68.13.198 'sudo python3 /tmp/zerokey-agent-contract-rollout.py rollout'
    ssh cltx@10.68.13.198 'sudo python3 /tmp/zerokey-agent-contract-rollout.py revert'

它做两件事，每台 pod 独立、可单独回滚：

1. **Deployment 启动参数加一行** ``cp /patch/bpi-codex.js /app/routes/bpi-codex.js``。
   zk-image-patch 这个 CM 是全队共用的，但"把哪些文件 cp 进 /app"是**逐个
   Deployment 的启动参数**——只更新 CM 不改参数，那台 pod 就加载不到新模块。
   （``responses.js`` 里的 require 做了容错，加载不到就退化成改动前的行为，
   所以顺序错了也不会把 pod 弄挂。）
2. **写账号级 agent 契约**：``PATCH /backend-api/user_system_messages``，把
   ``engine/instructions.md``（BPI 契约）写进该账号的自定义指令。

为什么是账号级而不是在消息里注入（2026-08-07 实测）
--------------------------------------------------
挡在中间的是 ChatGPT 的**消费级产品外壳**（系统层告诉模型"你碰不到用户的
机器"）。在用户消息里贴契约等于隔着一层跟它对喊，实测天花板 2-3/5；把契约写进
账号级系统消息是直接换掉那层外壳：

    账号指令为空（zero-104 对照）  -> 拒答 5/5
    写入契约（zero-115）           -> 工具块 5/5，零拒答

对 codex 那条路零影响：同号(115)的 Codex OAuth 前后各打 3 发，均为
reasoning+function_call、无污染 —— codex 后端自带 instructions，不吃账号级
自定义指令。所以生产 acct 池(155-159)不用碰。

回滚：``revert`` 把账号指令清空（pod 参数留着无害，因为没有契约就不会吐 BPI 块）。
"""
import json
import re
import subprocess
import sys

NS = "litellm-product"
PATCH_FILE = "/tmp/bpi-codex.js"          # cp 进 CM 用
CONTRACT = "/tmp/upstream_instructions.md"  # BPI 契约原文
SYSMSG_JS = "/tmp/zk_sysmsg.js"           # 写账号指令的小脚本


def k(*a, check=True):
    r = subprocess.run(["kubectl", "-n", NS, *a], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"kubectl {' '.join(a[:3])}: {r.stderr[:200]}")
    return r.stdout


def registered_accounts():
    """只取 LiteLLM 路由里真在用的号。

    2026-08-07 教训：集群里有 68 个 zero-N Deployment，但路由里只注册了 18 个。
    对全部 68 个做 rollout restart 会把**早就死掉、只是靠内存里旧 session 还
    显示 Running** 的号打回原形（实测 4 台起不来，全是 Sentinel 401
    token_expired，且全都不在路由里）。它们本来就不接流量，但没必要去惊动。
    """
    sql = ("select distinct substring(model_id from '-([0-9]+)-') "
           "from \"LiteLLM_ProxyModelTable\" where model_id like '%zk-%'")
    out = k("exec", "litellm-db-0", "--", "psql", "-U", "litellm", "-d", "litellm",
            "-A", "-t", "-c", sql)
    return sorted({int(x) for x in out.split() if x.isdigit()})


def zero_deploys():
    names = [f"zero-{n}" for n in registered_accounts()]
    alive = set(k("get", "deploy", "-o",
                  "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}").split())
    return [n for n in names if n in alive]


def pod_of(dep):
    out = k("get", "pods", "-l", f"app={dep}", "--no-headers",
            "-o", "custom-columns=:metadata.name", check=False).split()
    if out:
        return out[0]
    # 有些 deploy 没打 app 标签，退回按名字前缀找
    all_pods = k("get", "pods", "--no-headers", "-o", "custom-columns=:metadata.name").split()
    for p in all_pods:
        if p.startswith(dep + "-"):
            return p
    return None


def ensure_cp_line(dep):
    """给启动参数加 cp 行；已存在返回 False。"""
    d = json.loads(k("get", "deploy", dep, "-o", "json"))
    args = d["spec"]["template"]["spec"]["containers"][0].get("args") or []
    idx = [i for i, a in enumerate(args) if "zerokey-serve-codex.js" in a]
    if not idx:
        raise RuntimeError("启动参数里找不到 zerokey-serve-codex.js")
    i = idx[0]
    if "bpi-codex.js" in args[i]:
        return False
    args[i] = args[i].replace(
        "exec node /app/zerokey-serve-codex.js",
        "cp /patch/bpi-codex.js /app/routes/bpi-codex.js\nexec node /app/zerokey-serve-codex.js")
    patch = [{"op": "replace", "path": "/spec/template/spec/containers/0/args", "value": args}]
    with open("/tmp/_zk_args_patch.json", "w") as f:
        json.dump(patch, f)
    k("patch", "deploy", dep, "--type", "json", "--patch-file", "/tmp/_zk_args_patch.json")
    return True


def set_contract(pod, mode):
    for f in (SYSMSG_JS, CONTRACT):
        subprocess.run(["kubectl", "-n", NS, "cp", f, f"{pod}:{f}"],
                       capture_output=True, text=True)
    r = subprocess.run(["kubectl", "-n", NS, "exec", pod, "--", "node", SYSMSG_JS, mode],
                       capture_output=True, text=True)
    out = (r.stdout or r.stderr).strip().splitlines()
    return out[0][:60] if out else "(无输出)"


def rollout(revert=False):
    deps = zero_deploys()
    print(f"共 {len(deps)} 个 zero deployment\n")
    for dep in deps:
        try:
            if not revert:
                changed = ensure_cp_line(dep)
                if changed:
                    k("rollout", "status", f"deploy/{dep}", "--timeout=180s")
            pod = pod_of(dep)
            if not pod:
                print(f"  {dep:10s} ❌ 找不到 pod")
                continue
            msg = set_contract(pod, "clear" if revert else "set")
            print(f"  {dep:10s} {'CLEAR' if revert else 'SET  '} {msg}")
        except Exception as e:  # noqa: BLE001
            print(f"  {dep:10s} ❌ {str(e)[:90]}")


def status():
    for dep in zero_deploys():
        pod = pod_of(dep)
        if not pod:
            print(f"  {dep:10s} 无 pod"); continue
        # 必须先 cp —— pod 重启后 /tmp 是空的（第一版忘了这步，导致已写契约的
        # 机器也被报成"未写"）
        for f in (SYSMSG_JS, CONTRACT):
            subprocess.run(["kubectl", "-n", NS, "cp", f, f"{pod}:{f}"],
                           capture_output=True, text=True)
        has = subprocess.run(["kubectl", "-n", NS, "exec", pod, "--",
                              "sh", "-c", "test -f /app/routes/bpi-codex.js && echo yes || echo no"],
                             capture_output=True, text=True).stdout.strip()
        r = subprocess.run(["kubectl", "-n", NS, "exec", pod, "--", "node", SYSMSG_JS, "get"],
                           capture_output=True, text=True).stdout
        primed = "bpi_syntax" in r or "BPI block" in r
        print(f"  {dep:10s} 编译器={has:3s} 账号契约={'已写' if primed else '未写'}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "rollout":
        rollout(False)
    elif cmd == "revert":
        rollout(True)
    elif cmd == "status":
        status()
    else:
        raise SystemExit(__doc__)
