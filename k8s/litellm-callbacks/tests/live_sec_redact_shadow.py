#!/usr/bin/env python3
"""live_sec_redact_shadow.py — 影子模式下 sec_redact 的线上回归（198 上 sudo 跑）。

只打 gray 的 NodePort（30405）和 prod 的 NodePort（30402，阴性对照），不经过 nginx 分流。
临时 key 两把（carher-shadowreg-* 全量规则 / cursor-shadowreg-* agent 规则），1h 过期，跑完删除。
合成的假密钥/手机号，不是任何真实凭据。

逐项判据（每项单独 PASS/FAIL）：
  S1 三种线型（chat / responses / messages 流式）请求方向：gray 日志里该 call_id 有
     dir=request action=shadow 的 3-平台Token 和 1.1-手机号 —— 证明请求方向在扫
  S2 影子不改写：让模型原样复述，回包里能看到原文 —— 证明没被替换（影子只记不改）
  S3 响应方向：同一个 call_id 有 dir=response action=shadow 的命中 —— 证明流式响应也在扫
  S4 审计日志里没有任何原文（密钥、手机号）
  S5 agent 策略：cursor- 前缀 key 只记 3-* 规则，不记手机号/内网 IP
  S6 日志封顶：一段文本 200 个内网 IP ⇒ 恰好 20 行明细 + 1 行 hit-summary(more=180)
  S7 阴性对照：同样的请求打 prod（无插件）⇒ prod pod 日志里该 call_id 零条 sec_redact
"""
import base64
import json
import subprocess
import sys
import time
import urllib.request

ENV = {"KUBECONFIG": "/etc/rancher/k3s/k3s.yaml", "PATH": "/usr/local/bin:/usr/bin:/bin"}
import os
# 默认：被测车道=gray，阴性对照=prod。prod 也装了插件之后，用 LANE_UNDER_TEST=prod NEG_CONTROL=none 验 prod 本身
_L = {"gray": ("http://127.0.0.1:30405", "litellm-proxy-gray"), "prod": ("http://127.0.0.1:30402", "litellm-proxy"),
      "guarded-old": ("http://127.0.0.1:30406", "litellm-proxy-guarded-old")}
GRAY, GRAY_APP = _L[os.environ.get("LANE_UNDER_TEST", "gray")]
PROD, PROD_APP = _L["prod"]
NEG = os.environ.get("NEG_CONTROL", "prod")
CHAT_MODEL, RESP_MODEL = "deepseek-v4.1-flash", "deepseek-v4.1-flash-responses"
SECRET = "sk-proj-" + "Zq8Wn3Kd7Lp2Xr5Tv9Bm4Hc6Jf1Gs0Ya"
PHONE = "13812345678"
IP = "10.68.13.198"
R = []


def master():
    dep = json.loads(subprocess.run(["kubectl", "-n", "litellm-product", "get", "deploy", "litellm-proxy", "-o", "json"],
                                    capture_output=True, text=True, env=ENV, check=True).stdout)
    for ref in dep["spec"]["template"]["spec"]["containers"][0].get("envFrom", []):
        name = (ref.get("secretRef") or {}).get("name")
        out = subprocess.run(["kubectl", "-n", "litellm-product", "get", "secret", name or "-", "-o",
                              "jsonpath={.data.LITELLM_MASTER_KEY}"], capture_output=True, text=True, env=ENV)
        if name and out.returncode == 0 and out.stdout:
            return base64.b64decode(out.stdout).decode()
    raise SystemExit("no master key")


def http(base, path, body, key, sid=None, timeout=180):
    h = {"content-type": "application/json", "authorization": f"Bearer {key}", "x-api-key": key,
         "anthropic-version": "2023-06-01"}
    if sid:
        h["x-litellm-session-id"] = sid
    req = urllib.request.Request(base + path, method="POST", data=json.dumps(body).encode(), headers=h)
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


def logs(app, call_id, since="15m"):
    pods = subprocess.run(["kubectl", "-n", "litellm-product", "get", "pod", "-l", f"app={app}", "-o", "name"],
                          capture_output=True, text=True, env=ENV).stdout.split()
    out = []
    for p in pods:
        lg = subprocess.run(["kubectl", "-n", "litellm-product", "logs", p, "-c", "litellm", f"--since={since}"],
                            capture_output=True, text=True, env=ENV).stdout
        out += [l for l in lg.splitlines() if "sec_redact" in l and f"req={call_id}" in l]
    return out


def check(name, ok, detail=""):
    R.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  [{str(detail)[:220]}]" if not ok and detail else ""))


def call(base, shape, key, prompt):
    sid = f"shadowreg-{shape}-{time.time_ns()}"
    if shape == "chat":
        return http(base, "/v1/chat/completions", {"model": CHAT_MODEL, "stream": True, "max_tokens": 1500,
                                                   "messages": [{"role": "user", "content": prompt}]}, key, sid)
    if shape == "responses":
        return http(base, "/v1/responses", {"model": RESP_MODEL, "stream": True, "max_output_tokens": 1500,
                                            "input": prompt}, key, sid)
    return http(base, "/v1/messages", {"model": CHAT_MODEL, "stream": True, "max_tokens": 1500,
                                       "messages": [{"role": "user", "content": prompt}]}, key, sid)


def main():
    mk = master()
    ts = int(time.time())
    keys = {}
    for name in (f"carher-shadowreg-{ts}", f"cursor-shadowreg-{ts}"):
        st, b, _ = http(PROD, "/key/generate", {"key_alias": name, "duration": "1h", "max_budget": 1.0,
                                                 "models": [CHAT_MODEL, RESP_MODEL]}, mk)
        assert st == 200, (st, b[:200])
        keys[name.split("-")[0]] = json.loads(b)["key"]
    her, agent = keys["carher"], keys["cursor"]
    echo = f"请把下面引号里的内容一字不改地原样输出，不要加任何解释：「配置 api_key={SECRET} 联系电话 {PHONE}」"
    try:
        for shape in ("chat", "responses", "messages"):
            print(f"== {shape}")
            st, raw, cid = call(GRAY, shape, her, echo)
            time.sleep(3)
            lg = logs(GRAY_APP, cid)
            req_rules = {l.split("rule=")[1].split()[0] for l in lg if "dir=request action=shadow" in l and "rule=" in l}
            resp_rules = {l.split("rule=")[1].split()[0] for l in lg if "dir=response action=shadow" in l and "rule=" in l}
            txt = text_of(raw)
            check(f"{shape} 请求 200", st == 200, f"{st} {raw[:150]}")
            check(f"S1 {shape} 请求方向记到密钥+手机号", {"3-平台Token", "1.1-手机号"} <= req_rules, f"cid={cid} {req_rules}")
            # 阴性对照：同一句打 prod（无插件）—— 模型自己不复述时 S2 无法判定，不能算插件改写
            st_p, raw_p, _ = call(_L["guarded-old"][0] if GRAY == PROD else PROD, shape, her, echo)
            txt_p = text_of(raw_p)
            if not (SECRET in txt_p or PHONE in txt_p):
                print(f"  SKIP S2 {shape}：prod 上模型也没复述（{txt_p[:60]!r}），无法判定")
            else:
                check(f"S2 {shape} 影子不改写（模型看到并复述了原文，prod 对照也复述）", SECRET in txt or PHONE in txt, txt[:160])
            if SECRET in txt or PHONE in txt:
                check(f"S3 {shape} 响应方向也记到了", len(resp_rules) > 0, f"{resp_rules}")
            check(f"S4 {shape} 审计日志无原文", not any(SECRET in l or PHONE in l for l in lg))
            # S5 agent
            st, raw, cid = call(GRAY, shape, agent, f"只回复两个字：收到。连 {IP} 电话 {PHONE} key {SECRET}")
            time.sleep(3)
            lg = logs(GRAY_APP, cid)
            rules = {l.split("rule=")[1].split()[0] for l in lg if "dir=request" in l and "rule=" in l}
            check(f"S5 {shape} agent 只记密钥类", rules and all(r.startswith("3-") for r in rules), f"{rules}")
        # S6 封顶
        print("== audit cap")
        st, raw, cid = call(GRAY, "chat", her, "只回复两个字：收到。\n" + "".join(f"连 {IP}\n" for _ in range(200)))
        time.sleep(3)
        lg = logs(GRAY_APP, cid)
        detail = [l for l in lg if "sec_redact: hit dir=request" in l]
        summ = [l for l in lg if "sec_redact: hit-summary dir=request" in l]
        check("S6 200 个内网 IP ⇒ 20 行明细", len(detail) == 20, f"detail={len(detail)}")
        check("S6 ⇒ 1 行汇总 more=180", len(summ) == 1 and "more=180" in summ[0], summ[:1])
        # S7 阴性对照（没装插件的车道必须零条）
        if NEG != "none":
            nb, napp = _L[NEG]
            print(f"== {NEG} negative control")
            st, raw, cid = call(nb, "chat", her, f"只回复两个字：收到。key {SECRET} 电话 {PHONE}")
            time.sleep(3)
            lg = logs(napp, cid)
            check(f"S7 {NEG} 请求 200", st == 200, f"{st}")
            check(f"S7 {NEG} 无插件 ⇒ 零条 sec_redact 日志", len(lg) == 0, f"{len(lg)}")
    finally:
        http(PROD, "/key/delete", {"keys": [her, agent]}, mk)
    bad = [n for n, ok in R if not ok]
    print(f"\n{len(R) - len(bad)}/{len(R)} PASS")
    for n in bad:
        print("  FAIL", n)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
