#!/usr/bin/env python3
"""t0_sec_redact.py — sec_redact 全链 T0 / 灰度冒烟。真 HTTP 打 litellm，判据看两头：

  * 上游**真正收到**的请求体（mock 的 GET /dump）：请求方向替换是否生效
  * 客户端**真正收到**的正文（重组 SSE / JSON）：响应方向替换/掩码是否生效（10-29 起密钥也是替换，不阻断）

三种线型 × 流式/非流式 × her/agent 两类 key。前缀 t0_ 不会被 unittest discover 捞到。

用法（198 上）::

    python3 t0_sec_redact.py --base http://127.0.0.1:14100 --master sk-t0master \
        --mock http://<mock 容器 IP>:9999 [--shadow]

--shadow：期望插件只记不改（上游收到原文、客户端收到原文）。
脚本自己用 master key 建两把临时 key（carher-t0sec-* / cursor-t0sec-*），跑完删除。
"""
import argparse
import json
import sys
import time
import urllib.request

KEY = "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0kLmNoPqRsTuV"
PHONE = "13812345678"
IP = "10.68.13.198"
OLD_BLOCK = "已阻断输出"  # 旧版阻断文案片段：新版任何回包里都不该再出现
CRED = "[密钥凭证脱敏]"

A = argparse.ArgumentParser()
A.add_argument("--base", required=True)
A.add_argument("--master", required=True)
A.add_argument("--mock", required=True)
A.add_argument("--shadow", action="store_true")
A.add_argument("--chat-model", default="t0-chat")
A.add_argument("--claude-model", default="t0-claude")
ARGS = A.parse_args()

RESULTS = []


def http(method, url, body=None, key=None, timeout=60):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("content-type", "application/json")
    if key:
        req.add_header("authorization", f"Bearer {key}")
        req.add_header("x-api-key", key)
        req.add_header("anthropic-version", "2023-06-01")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def gen_key(alias):
    st, body = http("POST", f"{ARGS.base}/key/generate", {"key_alias": alias, "duration": "1h",
                                                          "models": [ARGS.chat_model, ARGS.claude_model]}, ARGS.master)
    assert st == 200, (st, body[:300])
    return json.loads(body)["key"]


def del_key(k):
    http("POST", f"{ARGS.base}/key/delete", {"keys": [k]}, ARGS.master)


def upstream_got():
    return http("GET", f"{ARGS.mock}/dump")[1]


def text_of(raw: str) -> str:
    """重组客户端看到的正文：SSE 逐帧取文本增量；JSON 取正文字段。"""
    out = []
    if "data:" in raw:
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
    try:
        o = json.loads(raw)
    except ValueError:
        return raw
    for ch in o.get("choices") or []:
        out.append((ch.get("message") or {}).get("content") or "")
    for item in o.get("output") or []:
        for part in item.get("content") or []:
            out.append(part.get("text") or "")
    for b in o.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text") or "")
    return "".join(out)


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def call(shape, key, user_text, stream):
    if shape == "chat":
        return http("POST", f"{ARGS.base}/v1/chat/completions", {
            "model": ARGS.chat_model, "stream": stream,
            "messages": [{"role": "user", "content": user_text}]}, key)
    if shape == "responses":
        return http("POST", f"{ARGS.base}/v1/responses", {
            "model": ARGS.chat_model, "stream": stream, "input": user_text}, key)
    return http("POST", f"{ARGS.base}/v1/messages", {
        "model": ARGS.claude_model, "stream": stream, "max_tokens": 256,
        "messages": [{"role": "user", "content": user_text}]}, key)


def main():
    sfx = str(int(time.time() * 1000))  # alias 全局唯一：上一轮异常退出时没删的 key 不能挡住这一轮
    her, agent = gen_key(f"carher-t0sec-her-{sfx}"), gen_key(f"cursor-t0sec-agent-{sfx}")
    try:
        for shape in ("chat", "responses", "anthropic"):
            for stream in (False, True):
                tag = f"{shape}/{'stream' if stream else 'json'}"
                print(f"== {tag}")
                # ---- 请求方向（her 全量规则）：上游收到的必须是替换后的
                st, raw = call(shape, her, f"帮我看下 key={KEY} 电话 {PHONE} 机器 {IP}", stream)
                got = upstream_got()
                check(f"{tag} her 请求 200", st == 200, f"{st} {raw[:200]}")
                if ARGS.shadow:
                    check(f"{tag} her 影子：上游收到原文密钥", KEY in got)
                else:
                    check(f"{tag} her 上游收不到密钥", KEY not in got and "[密钥凭证脱敏]" in got, got[:200])
                    check(f"{tag} her 上游收不到手机号原文", PHONE not in got and "138****5678" in got)
                    check(f"{tag} her 上游收不到内网 IP", IP not in got and "[内网IP脱敏]" in got)
                # ---- 请求方向（agent 只做密钥）
                st, raw = call(shape, agent, f"连 {IP} 电话 {PHONE} key {KEY}", stream)
                got = upstream_got()
                if not ARGS.shadow:
                    check(f"{tag} agent 上游收不到密钥", KEY not in got)
                    check(f"{tag} agent 上游 IP/手机号原样", IP in got and PHONE in got)
                # ---- 响应方向：上游回出密钥 ⇒ 替换（不阻断）
                st, raw = call(shape, her, "请 ECHO_KEY", stream)
                txt = text_of(raw)
                check(f"{tag} 回包 200", st == 200, f"{st} {raw[:200]}")
                if ARGS.shadow:
                    # 流式时密钥被切在多个 chunk 里，必须看重组后的正文，不能在原始 SSE 里找整串
                    check(f"{tag} 影子：密钥原样到客户端", KEY in txt, txt[:200])
                else:
                    check(f"{tag} 客户端收不到密钥（含 done/completed 副本）", KEY not in raw, raw[:300])
                    check(f"{tag} 客户端看到替换标记", CRED in txt, txt[:200])
                    check(f"{tag} 没有阻断文案", OLD_BLOCK not in raw, txt[:200])
                    check(f"{tag} 密钥之后的正文完整放出", txt.endswith("正文" * 3), txt[-120:])
                # ---- 响应方向：手机号/IP 掩码（her）、agent 原样
                st, raw = call(shape, her, "请 ECHO_PHONE ECHO_IP", stream)
                txt = text_of(raw)
                if not ARGS.shadow:
                    check(f"{tag} her 回包手机号掩码", "138****5678" in txt and PHONE not in raw, txt[:200])
                    check(f"{tag} her 回包内网 IP 替换", "[内网IP脱敏]" in txt and IP not in raw)
                    check(f"{tag} her 回包正文完整（尾巴放出）", txt.endswith("正文" * 3), txt[-60:])
                st, raw = call(shape, agent, "请 ECHO_IP", stream)
                txt = text_of(raw)
                if not ARGS.shadow:
                    check(f"{tag} agent 回包 IP 原样", IP in txt, txt[:200])
                # ---- 干净内容逐字不变
                st, raw = call(shape, her, "你好", stream)
                txt = text_of(raw)
                check(f"{tag} 干净回包逐字一致", txt.startswith("好的。以上。") and txt.endswith("正文" * 60), txt[:80])
    finally:
        del_key(her)
        del_key(agent)
    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} PASS")
    for n in bad:
        print("  FAIL", n)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
