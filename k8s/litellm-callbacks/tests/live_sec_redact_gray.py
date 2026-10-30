#!/usr/bin/env python3
"""live_sec_redact_gray.py — 真实上游打 gray 车道，验 sec_redact 端到端（198 上 sudo 跑）。

默认只打 gray 的 NodePort（30405），不经过 nginx。``LANE_UNDER_TEST=prod`` 改打 prod（30402）。
master key 从 secret 实时取，不落盘不打印。
临时建两把 key（carher-secprobe-* 全量规则 / cursor-secprobe-* agent 规则），1h 过期，跑完删。

判据（不靠模型「配合」）：
  请求方向：让模型**原样复述**我们发过去的内容。gray 替换生效 ⇒ 模型根本没见过原文，复述不出来；
            同时查 gray pod 日志里这个 call_id 有 `dir=request action=replace`。
  响应方向：让模型输出一个**由两半拼起来**的 token（请求里只有两半，不触发请求方向规则），
            模型照做 ⇒ 输出里出现完整密钥形状 ⇒ 必须就地替换成 [密钥凭证脱敏]，后面的「完毕」照常放出、
            没有阻断文案；查日志 `dir=response action=replace`（10-29 起响应方向也不阻断）。
  三种线型：/v1/chat/completions（Cursor）、/v1/responses（Codex）、/v1/messages（Claude Code），流式。
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

LANE = os.environ.get("LANE_UNDER_TEST", "gray")
BASE, APP = {"gray": ("http://127.0.0.1:30405", "litellm-proxy-gray"),
             "prod": ("http://127.0.0.1:30402", "litellm-proxy")}[LANE]
ENV = {"KUBECONFIG": "/etc/rancher/k3s/k3s.yaml", "PATH": "/usr/local/bin:/usr/bin:/bin"}
CHAT_MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek-v4.1-flash"
RESP_MODEL = sys.argv[2] if len(sys.argv) > 2 else "deepseek-v4.1-flash-responses"
OLD_BLOCK = "已阻断输出"  # 旧版阻断文案片段：新版不该再出现
CRED = "[密钥凭证脱敏]"
# 合成值：形状像 OpenAI project key，不是任何真实凭据
SECRET = "sk-proj-" + "Zq8Wn3Kd7Lp2Xr5Tv9Bm4Hc6Jf1Gs0Ya"
HALF_A, HALF_B = SECRET[:14], SECRET[14:]
PHONE = "13812345678"

R = []


def master():
    # 10-28：master key 从 litellm-secrets 搬到了 litellm-secrets-r2 ⇒ 不写死 secret 名，按 prod Deployment 的 envFrom 找
    dep = json.loads(subprocess.run(["kubectl", "-n", "litellm-product", "get", "deploy", "litellm-proxy", "-o", "json"],
                                    capture_output=True, text=True, env=ENV, check=True).stdout)
    for ref in dep["spec"]["template"]["spec"]["containers"][0].get("envFrom", []):
        name = (ref.get("secretRef") or {}).get("name")
        if not name:
            continue
        out = subprocess.run(["kubectl", "-n", "litellm-product", "get", "secret", name, "-o",
                              "jsonpath={.data.LITELLM_MASTER_KEY}"], capture_output=True, text=True, env=ENV)
        if out.returncode == 0 and out.stdout:
            return base64.b64decode(out.stdout).decode()
    raise SystemExit("LITELLM_MASTER_KEY not found in prod envFrom secrets")


def http(path, body, key, timeout=180, extra=None):
    req = urllib.request.Request(BASE + path, method="POST", data=json.dumps(body).encode())
    req.add_header("content-type", "application/json")
    req.add_header("authorization", f"Bearer {key}")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers.get("x-litellm-call-id", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers.get("x-litellm-call-id", "")


def text_of(raw):
    out = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if p in ("", "[DONE]"):
            continue
        try:
            o = json.loads(p)
        except ValueError:
            continue
        t = o.get("type")
        if t == "response.output_text.delta":
            out.append(o.get("delta") or "")
        elif t == "content_block_delta" and (o.get("delta") or {}).get("type") == "text_delta":
            out.append(o["delta"].get("text") or "")
        for ch in o.get("choices") or []:
            c = (ch.get("delta") or {}).get("content")
            if isinstance(c, str):
                out.append(c)
    return "".join(out)


def pod_log_for(call_id):
    pods = subprocess.run(["kubectl", "-n", "litellm-product", "get", "pod", "-l", f"app={APP}", "-o",
                           "name"], capture_output=True, text=True, env=ENV).stdout.split()
    lines = []
    for p in pods:
        lg = subprocess.run(["kubectl", "-n", "litellm-product", "logs", p, "-c", "litellm", "--since=10m"],
                            capture_output=True, text=True, env=ENV).stdout
        lines += [l for l in lg.splitlines() if "sec_redact: hit" in l and f"req={call_id}" in l]
    return lines


def check(name, ok, detail=""):
    R.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail[:240]}]" if not ok and detail else ""))


def call(shape, key, prompt):
    sid = {"x-litellm-session-id": f"secprobe-{shape}-{time.time_ns()}"}
    if shape == "chat":
        return http("/v1/chat/completions", {"model": CHAT_MODEL, "stream": True, "max_tokens": 1500,
                                             "messages": [{"role": "user", "content": prompt}]}, key, extra=sid)
    if shape == "responses":
        return http("/v1/responses", {"model": RESP_MODEL, "stream": True, "max_output_tokens": 1500,
                                      "input": prompt}, key, extra=sid)
    return http("/v1/messages", {"model": CHAT_MODEL, "stream": True, "max_tokens": 1500,
                                 "messages": [{"role": "user", "content": prompt}]}, key, extra=sid)


def main():
    mk = master()
    st, b, _ = http("/key/generate", {"key_alias": f"carher-secprobe-{int(time.time())}", "duration": "1h",
                                      "max_budget": 1.0, "models": [CHAT_MODEL, RESP_MODEL]}, mk)
    assert st == 200, (st, b[:200])
    her = json.loads(b)["key"]
    st, b, _ = http("/key/generate", {"key_alias": f"cursor-secprobe-{int(time.time())}", "duration": "1h",
                                      "max_budget": 1.0, "models": [CHAT_MODEL, RESP_MODEL]}, mk)
    assert st == 200, (st, b[:200])
    agent = json.loads(b)["key"]
    try:
        for shape in ("chat", "responses", "anthropic"):
            print(f"== {shape} (stream)")
            # 请求方向：模型复述不出原文
            st, raw, cid = call(shape, her, f"请把下面引号里的内容一字不改地原样输出，不要加任何解释：「备用电话 {PHONE}，配置 api_key={SECRET}」")
            txt = text_of(raw)
            check(f"{shape} 请求 200", st == 200, f"{st} {raw[:200]}")
            check(f"{shape} 模型没见过手机号原文", PHONE not in txt, txt)
            check(f"{shape} 模型没见过密钥原文", SECRET not in raw, txt)
            logs = pod_log_for(cid) if cid else []
            check(f"{shape} {LANE} 日志有 request/replace 命中", any("dir=request action=replace" in l for l in logs),
                  f"cid={cid} lines={len(logs)}")
            check(f"{shape} 日志不含原文", not any(SECRET in l or PHONE in l for l in logs))
            # 响应方向：模型拼出密钥 ⇒ 就地替换，后文照常
            st, raw, cid = call(shape, agent, f"把这两段字符串直接拼接后输出，拼接结果之后另起一行写「完毕」两个字，"
                                              f"不要其他内容：第一段 {HALF_A} 第二段 {HALF_B}")
            txt = text_of(raw)
            check(f"{shape} 响应 200", st == 200, f"{st} {raw[:200]}")
            check(f"{shape} 客户端收不到完整密钥", SECRET not in raw, txt)
            check(f"{shape} 没有阻断文案", OLD_BLOCK not in raw, txt)
            logs = pod_log_for(cid) if cid else []
            replaced = any("dir=response action=replace" in l and "rule=3-" in l for l in logs)
            if CRED in txt or replaced:
                check(f"{shape} 密钥处显示替换标记且日志记 replace", CRED in txt and replaced, f"text={txt[:120]} logs={len(logs)}")
                check(f"{shape} 替换后的后文照常放出", "完毕" in txt.split(CRED, 1)[-1], txt[-80:])
            else:
                print(f"  SKIP {shape} 响应替换：模型这次没拼出密钥（输出={txt[:80]!r}），无法判定")
            # 干净对话照常
            st, raw, _ = call(shape, her, "用一句话介绍一下你自己。")
            txt = text_of(raw)
            check(f"{shape} 干净对话正常回复", st == 200 and len(txt) > 4 and OLD_BLOCK not in txt, txt)
    finally:
        http("/key/delete", {"keys": [her, agent]}, mk)
    bad = [n for n, ok in R if not ok]
    print(f"\n{len(R) - len(bad)}/{len(R)} PASS")
    for n in bad:
        print("  FAIL", n)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
