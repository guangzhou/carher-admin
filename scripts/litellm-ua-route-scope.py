#!/usr/bin/env python3
"""litellm-ua-route-scope.py — UA 分流的作用范围开关（含自动验证 / 一键回退）。

**在 198 上以 root 执行**::

    scp scripts/litellm-ua-route-scope.py cltx@10.68.13.198:/tmp/
    ssh cltx@10.68.13.198 'sudo python3 /tmp/litellm-ua-route-scope.py status'
    ssh cltx@10.68.13.198 'sudo PROBE_KEY=sk-xxx python3 /tmp/litellm-ua-route-scope.py all'
    ssh cltx@10.68.13.198 'sudo python3 /tmp/litellm-ua-route-scope.py off'

三档范围（改的是 litellm-proxy 的两个环境变量，回调按它决定管不管这把 key）::

    one <alias>   只对这一把 key 生效（灰度）
    all           对全部 cursor-* key 生效，**包括以后新建的**
    off           两个变量清空 -> 回调完全不生效（= 一键回退）

为什么范围要放在环境变量而不是代码里
------------------------------------
出问题时要能在**一条命令、两分钟内**退干净，不需要改代码、不需要重新走
CM 发布。``off`` 之后回调虽然还加载着，但 ``key_in_scope()`` 恒为 False，
一个请求都不碰。

为什么必须门控（2026-08-07 实测教训）
------------------------------------
第一版没门控就上线，立刻被验收测出来：master key 明确点名
``chatgpt-gpt-5.6-terra``（acct 池）被改写去了 zerokey。因为其它 546 把 cursor
key 的 gpt 请求经全局 ``model_group_alias`` 之后正好都变成 ``chatgpt-gpt-*``,
等于**把全员 gpt 流量都扳去了 zerokey**。范围能收就必须收。

自动验证
--------
给了 ``PROBE_KEY`` 就在切换后**立刻打真实请求**：同一个模型名，Desktop UA 应落
acct、CLI UA 应落 zerokey。**任一格不符就自动回退到切换前的值**，避免"改完就走、
坏了没人知道"——上一版 UA 分流就是这么静默坏掉的。
"""
import json
import os
import subprocess
import sys
import urllib.request

NS = os.environ.get("LITELLM_NS", "litellm-product")
DEPLOY = "deployment/litellm-proxy"
E_ALIAS = "UA_ROUTE_KEY_ALIASES"
E_PREFIX = "UA_ROUTE_KEY_PREFIXES"
# 集群内直连 svc，绕开公网/CF，避免探测本身受外部因素影响
# 在 proxy pod 内部打自己，最短路径：不依赖集群 DNS、不出公网、不受 CF 影响。
PROBE_URL = os.environ.get("PROBE_URL", "http://127.0.0.1:4000/v1/responses")
PROBE_MODEL = os.environ.get("PROBE_MODEL", "gpt-5.6-sol")
DESKTOP_UA = "Codex Desktop/0.147.0 (Mac OS 26.2.0; arm64) unknown (Codex Desktop; 1)"
CLI_UA = "codex-tui/0.146.1 (Mac OS 26.2.0; arm64) unknown"


def kubectl(*args, check=True):
    r = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"kubectl {' '.join(args[:3])} FAILED:\n{r.stderr[:600]}")
    return r.stdout


def current_env():
    out = kubectl("get", DEPLOY, "-o", "json")
    envs = json.loads(out)["spec"]["template"]["spec"]["containers"][0].get("env") or []
    got = {e["name"]: e.get("value", "") for e in envs if e["name"] in (E_ALIAS, E_PREFIX)}
    return got.get(E_ALIAS, ""), got.get(E_PREFIX, "")


def set_env(alias, prefix, why):
    print(f"[set] {E_ALIAS}={alias!r} {E_PREFIX}={prefix!r}  ({why})")
    # 一律显式写值（含空串），**不要用 NAME- 删除** —— 显式声明能让 `status`
    # 一眼看出当前范围，也避免以后改回调默认值时又出"删了反而还生效"的歧义。
    args = ["set", "env", DEPLOY, f"{E_ALIAS}={alias}", f"{E_PREFIX}={prefix}"]
    kubectl(*args)
    kubectl("rollout", "status", DEPLOY, "--timeout=300s")


def probe(key, ua):
    """在 proxy pod **内部**发请求。

    脚本跑在 198 宿主机上，解析不了 *.svc.cluster.local —— 第一版就栽在这，
    把 DNS 失败误判成"分流坏了"并触发了自动回退。所以借 pod 执行。
    """
    pods = kubectl("get", "pods", "-l", "app=litellm-proxy", "--no-headers",
                   "-o", "custom-columns=:metadata.name").split()
    if not pods:
        return "ERR no proxy pod"
    payload = {"model": PROBE_MODEL, "instructions": "",
               "input": [{"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": "好"}]}],
               "stream": False}
    # 所有可变值走 json.dumps 注入，避免拼字符串时的引号地狱
    # 注意：body 必须以**字符串字面量**注入。第一版直接嵌 json.dumps(dict) 的结果，
    # 里面的 false/true/null 不是 Python 字面量，pod 里 NameError。
    snippet = (
        "import json,urllib.request\n"
        "body=%s.encode()\n"
        "req=urllib.request.Request(%s,data=body,headers={"
        "'Authorization':'Bearer '+%s,'Content-Type':'application/json','User-Agent':%s})\n"
        "r=urllib.request.urlopen(req,timeout=180)\n"
        "print(r.headers.get('x-litellm-model-id') or '?')\n"
    ) % (json.dumps(json.dumps(payload)), json.dumps(PROBE_URL),
         json.dumps(key), json.dumps(ua))
    r = subprocess.run(["kubectl", "-n", NS, "exec", pods[0], "--", "python3", "-c", snippet],
                       capture_output=True, text=True)
    # pod 的 sitecustomize 每次启动都会往输出里塞几行噪声，得滤掉再取结果
    noise = ("[sitecustomize]", "[bpi]", "Warning:")
    out = [l for l in (r.stdout or "").splitlines()
           if l.strip() and not l.startswith(noise)]
    if out:
        return out[-1].strip()
    err = " ".join(l for l in (r.stderr or "").splitlines()
                   if l.strip() and not l.startswith(noise))
    return ("ERR " + err)[:100]


def verify(expect_split: bool) -> bool:
    key = os.environ.get("PROBE_KEY")
    if not key:
        print("[verify] 未设 PROBE_KEY，跳过自动验证（强烈建议设上）")
        return True
    d, c = probe(key, DESKTOP_UA), probe(key, CLI_UA)
    print(f"[verify] Desktop -> {d}")
    print(f"[verify] CLI     -> {c}")
    if expect_split:
        ok = ("acct" in d) and ("zk-" in c)
        print("[verify] 期望 Desktop=acct / CLI=zerokey ->", "PASS" if ok else "FAIL")
    else:
        ok = ("acct" not in d) or (d == c)  # 关闭后两者应落同一族（都按 alias 走）
        print("[verify] 期望分流已关闭 ->", "PASS" if ok else "FAIL")
    return ok


def apply(alias, prefix, why, expect_split):
    before = current_env()
    print(f"[before] {E_ALIAS}={before[0]!r} {E_PREFIX}={before[1]!r}")
    set_env(alias, prefix, why)
    if verify(expect_split):
        print("[done] 生效并通过验证")
        return
    print("[!!] 验证未通过 —— 自动回退到切换前的值")
    set_env(before[0], before[1], "auto-rollback")
    verify(expect_split=bool(before[0] or before[1]))
    raise SystemExit(1)


def status():
    a, p = current_env()
    print(f"{E_ALIAS}  = {a!r}")
    print(f"{E_PREFIX} = {p!r}")
    print("范围:", "全部 cursor-*" if p else (f"仅 {a}" if a else "**未启用**（回调不动任何请求）"))
    verify(expect_split=bool(a or p))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        status()
    elif cmd == "all":
        apply("", "cursor-", "全部 cursor key（含新建）", True)
    elif cmd == "one":
        if len(sys.argv) < 3:
            raise SystemExit("用法: one <key_alias>")
        apply(sys.argv[2], "", f"仅 {sys.argv[2]}", True)
    elif cmd == "off":
        apply("", "", "一键回退：回调不再生效", False)
    else:
        raise SystemExit(__doc__)
