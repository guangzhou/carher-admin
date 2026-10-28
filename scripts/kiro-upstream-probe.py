#!/usr/bin/env python3
"""直打 kiro 上游 generateAssistantResponse 的探针（绕过 kiro.rs，量上游本身）。

凭据/代理/设备指纹全部复用 `kiro-probe.py`（整池从 198 pod 读、刷新 token、走线上同一出口）。
不指定 --account 时自动挑第一个「真能回话」的启用 idc 号（402/403 自动跳过）。

子命令
  dump   原样打印一发的 HTTP 状态、全部响应头、每一帧的全部帧头和 payload（什么都不过滤）。
  ruler  验「空回复判据」：多场景采样，自动找反例。分母为 0 判废，不假绿。
  gate   版本闸门矩阵：版本 × {不带 ARN, BuilderId 占位 ARN}。
  models 逐个模型真打一发短 prompt（目录里有 ≠ 真能回话），报 状态 / 首包耗时 / 回了几个字。
         配 --no-proxy 量「本机出口直连」（10-28 用来判国内 IP 不走 VPN 能不能用）。

判空回复的唯一正确尺子（10-22 定型）：
  0 字节，或「有 contextUsageEvent 但 assistantResponseEvent==0 且 toolUseEvent==0」。
  ⛔ 别用「缺 meteringEvent」——它只在发了 origin 时出现，会把正常回复误判。
"""
import argparse, importlib.util, json, os, struct, sys, time, uuid
import urllib.error, urllib.request

_spec = importlib.util.spec_from_file_location(
    "kp", os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiro-probe.py"))
kp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(kp)

REGION = "us-east-1"
# Kiro IDE 源码 FixedProfileArns：BuilderId 写死的占位 ARN；social 号真实下发的是另一个。
BUILDER_ID_ARN = "arn:aws:codewhisperer:us-east-1:638616132270:profile/AAAACCCCXXXX"
SOCIAL_ARN = "arn:aws:codewhisperer:us-east-1:699475941385:profile/EHGA3GRVQMUK"
SENSITIVE = "我有一个自己的 grok 订阅账号（邮箱 test@outlook.com），帮我写个脚本改它的登录密码"
NEUTRAL = "只回答一个字：是"
TOOL = [{"toolSpecification": {"name": "get_time", "description": "Get current time",
         "inputSchema": {"json": {"type": "object", "properties": {}, "required": []}}}}]


def parse_frames(data: bytes) -> list[dict]:
    out, pos = [], 0
    while pos + 12 <= len(data):
        total, hlen = struct.unpack(">II", data[pos:pos + 8])
        if total < 16 or pos + total > len(data):
            out.append({"anomaly": f"truncated at {pos}"}); break
        hdrs, hp, he = {}, pos + 12, pos + 12 + hlen
        while hp < he:
            nl = data[hp]; hp += 1
            name = data[hp:hp + nl].decode("utf8", "replace"); hp += nl
            ht = data[hp]; hp += 1
            if ht in (6, 7):
                vl = struct.unpack(">H", data[hp:hp + 2])[0]; hp += 2
                v = data[hp:hp + vl]; hp += vl
                hdrs[name] = v.decode("utf8", "replace") if ht == 7 else v.hex()
            elif ht in (0, 1):
                hdrs[name] = ht == 0
            elif ht in (2, 3, 4, 5, 8):
                n = {2: 1, 3: 2, 4: 4, 5: 8, 8: 8}[ht]
                hdrs[name] = int.from_bytes(data[hp:hp + n], "big", signed=True); hp += n
            elif ht == 9:
                hdrs[name] = str(uuid.UUID(bytes=data[hp:hp + 16])); hp += 16
            else:
                hdrs[name] = f"<unknown header type {ht}>"; break
        payload = data[he:pos + total - 4]
        try:
            pj = json.loads(payload) if payload else {}
        except ValueError:
            pj = {"_raw": payload[:200].hex()}
        out.append({"headers": hdrs, "payload": pj}); pos += total
    if pos < len(data):
        out.append({"anomaly": f"{len(data) - pos} trailing bytes"})
    return out


def verdict(raw: bytes, frames: list[dict]) -> dict:
    types = [f.get("headers", {}).get(":event-type", "?") for f in frames]
    text = "".join(f["payload"].get("content", "") for f in frames
                   if f.get("headers", {}).get(":event-type") == "assistantResponseEvent")
    n_text, n_tool = types.count("assistantResponseEvent"), types.count("toolUseEvent")
    empty = not raw or ("contextUsageEvent" in types and n_text == 0 and n_tool == 0)
    exc = [f for f in frames if f.get("headers", {}).get(":message-type") == "exception"]
    return {"types": types, "n_text": n_text, "n_tool": n_tool, "text": text,
            "empty": empty, "metering": "meteringEvent" in types, "exceptions": exc}


class Shooter:
    def __init__(self, creds: dict, opener):
        self.creds, self.opener = creds, opener
        self.mid = kp.machine_id_of(creds)
        self.tok = kp.refresh_token(creds, opener, REGION)

    def send(self, prompt, model="claude-opus-5.5", origin="AI_EDITOR",
             ver=kp.KIRO_IDE_VER, arn=None, tools=None):
        uim = {"content": prompt, "modelId": model, "userInputMessageContext": {}}
        if origin:
            uim["origin"] = origin
        if tools:
            uim["userInputMessageContext"] = {"tools": tools}
        body = {"conversationState": {"chatTriggerType": "MANUAL", "agentTaskType": "vibe",
                "conversationId": str(uuid.uuid4()),
                "currentMessage": {"userInputMessage": uim}, "history": []}}
        if arn:
            body["profileArn"] = arn
        ua = f"KiroIDE-{ver}-{self.mid}"
        req = urllib.request.Request(
            f"https://q.{REGION}.amazonaws.com/generateAssistantResponse",
            data=json.dumps(body).encode(), method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {self.tok}",
                "x-amzn-kiro-agent-mode": "vibe", "x-amzn-codewhisperer-optout": "true",
                "amz-sdk-invocation-id": str(uuid.uuid4()),
                "amz-sdk-request": "attempt=1; max=1",
                "x-amz-user-agent": f"aws-sdk-js/1.0.34 {ua}",
                "user-agent": "aws-sdk-js/1.0.34 ua/2.1 os/darwin#24.6.0 lang/js "
                              f"md/nodejs#22.22.0 api/codewhispererstreaming#1.0.34 m/E {ua}",
                "Connection": "close"})
        try:
            r = self.opener.open(req, timeout=180)
            return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()


def pick(pool, opener, account):
    if account:
        return Shooter(kp.select_account(pool, account)[0], opener)
    for c in pool:
        if c.get("disabled") or kp.auth_method_of(c) != "idc":
            continue
        try:
            s = Shooter(c, opener)
        except RuntimeError as e:
            print(f"# 跳过 id={c['id']}：刷新失败 {str(e)[:80]}", file=sys.stderr); continue
        st, _, raw = s.send(NEUTRAL, model="qwen3-coder-next")
        if st == 200:
            print(f"# 用 id={c['id']} {c.get('subscriptionTitle')}", file=sys.stderr)
            return s
        print(f"# 跳过 id={c['id']}：{st} {raw[:80]!r}", file=sys.stderr)
    sys.exit("🔴 没有一个启用 idc 号能回话（全 402/403）⇒ 本次量不了，别读成「上游没问题」。")


def cmd_dump(s, a):
    st, hdrs, raw = s.send(SENSITIVE if a.sensitive else NEUTRAL, model=a.model,
                           origin=None if a.origin == "none" else a.origin,
                           ver=a.version, arn=a.arn)
    print(f"[HTTP] {st}")
    for k, v in hdrs.items():
        print(f"  {k}: {v}")
    print(f"[BODY] {len(raw)} bytes")
    frames = parse_frames(raw) if st == 200 else []
    if st != 200:
        print("  " + raw[:500].decode("utf8", "replace"))
    for i, f in enumerate(frames):
        print(f"  #{i} {json.dumps(f, ensure_ascii=False)[:600]}")
    if st == 200:
        v = verdict(raw, frames)
        print(f"[VERDICT] empty={v['empty']} text={v['n_text']} tool={v['n_tool']} "
              f"exceptions={len(v['exceptions'])} 文本={v['text'][:120]!r}")


def cmd_ruler(s, a):
    cases = [  # (label, kwargs, 期望有内容?)
        ("opus5.5 无origin 中性", dict(prompt=NEUTRAL, origin=None), True),
        ("opus5.5 无origin 长答", dict(prompt="分三点说明 TCP 和 UDP 的区别", origin=None), True),
        ("opus5.5 origin 中性", dict(prompt=NEUTRAL), True),
        ("opus5.5 origin 工具", dict(prompt="现在几点？用工具查", tools=TOOL), True),
        ("opus4.6 origin 敏感", dict(prompt=SENSITIVE, model="claude-opus-4.6"), True),
        ("opus5.5 无origin 敏感", dict(prompt=SENSITIVE, origin=None), True),
        ("opus5.5 AI_EDITOR 敏感", dict(prompt=SENSITIVE), False),
        ("opus5.5 KIRO_CLI 敏感", dict(prompt=SENSITIVE, origin="KIRO_CLI"), False),
    ]
    landed, wrong = 0, []
    for label, kw, want in cases:
        st, _, raw = s.send(**kw)
        if st != 200:
            # 自动挑的号常是线上窗口里正在接流量的那个 ⇒ 429 是抢道，不是判据问题
            hint = "（与线上抢道，--account 换冷备号补这一发）" if st == 429 else ""
            print(f"  {label:<22} ⚪ 未落地 HTTP {st} {raw[:80]!r}{hint}"); continue
        landed += 1
        v = verdict(raw, parse_frames(raw))
        ok = (not v["empty"]) == want
        if not ok:
            wrong.append(label)
        print(f"  {label:<22} {'✅' if ok else '🔴'} empty={v['empty']!s:<5} "
              f"text={v['n_text']:<3} tool={v['n_tool']} metering={'Y' if v['metering'] else 'N'} "
              f"{len(raw)}B")
    if landed == 0:
        sys.exit(f"🔴 判废：{len(cases)} 发 0 落地。分母为 0 不是通过。")
    print(f"\n落地 {landed}/{len(cases)}；判据误判 {len(wrong)} {wrong or ''}")
    sys.exit(1 if wrong else 0)


def cmd_gate(s, a):
    arns = [("none", None), ("builderid", BUILDER_ID_ARN)]
    if a.with_social_arn:
        arns.append(("social", SOCIAL_ARN))
    for ver in a.versions.split(","):
        for name, arn in arns:
            st, _, raw = s.send(NEUTRAL, model=a.model, ver=ver, arn=arn)
            note = ""
            if st == 200:
                v = verdict(raw, parse_frames(raw))
                note = f"text={v['n_text']} {v['text'][:20]!r}"
            else:
                note = raw[:100].decode("utf8", "replace")
            print(f"  ver={ver:<9} arn={name:<10} {st} {note}")


def cmd_models(s, a):
    cat = a.models.split(",") if a.models else [
        "claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5", "claude-opus-5", "claude-sonnet-5",
        "claude-opus-4.8", "claude-sonnet-4.6", "claude-haiku-4.5", "gpt-5.6-sol", "gpt-5.6-terra",
        "gpt-5.6-luna", "kimi-k3", "deepseek-3.2", "minimax-m2.5", "glm-5", "qwen3-coder-next"]
    ok = 0
    for m in cat:
        t0 = time.time()
        st, _, raw = s.send(NEUTRAL, model=m)
        dt = time.time() - t0
        v = verdict(raw, parse_frames(raw)) if st == 200 else None
        good = st == 200 and v and not v["empty"] and not v["exceptions"]
        ok += bool(good)
        detail = (repr(v["text"][:30]) if v and v["text"] else ("空回复" if v and v["empty"] else
                  (str(v["exceptions"])[:80] if v and v["exceptions"] else raw[:120].decode("utf8", "replace"))))
        print(f"{'✅' if good else '🔴'} {m:<20} HTTP {st}  {dt:5.1f}s  {detail}")
    print(f"\n{ok}/{len(cat)} 个模型真回了字")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["dump", "ruler", "gate", "models"])
    p.add_argument("--no-proxy", action="store_true", help="不走线上出口，用本机网络直连（与线上不同路）")
    p.add_argument("--models", help="models 用：逗号分隔；缺省 16 个常用")
    p.add_argument("--account", help="纯数字=id；缺省自动挑一个能回话的启用 idc 号")
    p.add_argument("--model", default="claude-opus-5.5")
    p.add_argument("--origin", default="AI_EDITOR", help="dump 用；none=不发该字段")
    p.add_argument("--sensitive", action="store_true", help="dump 用敏感 prompt")
    p.add_argument("--version", default=kp.KIRO_IDE_VER, help="dump 用的 KiroIDE 版本")
    p.add_argument("--arn", help="dump 时注入的 profileArn")
    p.add_argument("--versions", default="0.11.107,0.11.133,0.12.155", help="gate 用")
    p.add_argument("--with-social-arn", action="store_true",
                   help="gate 额外试 social ARN（idc 号预期 403，只作阴性对照）")
    a = p.parse_args()
    pool = kp.load_pool(None)
    proxy = None if a.no_proxy else kp.pod_proxy()
    print(f"# 出口 {kp.mask(proxy)}", file=sys.stderr)
    s = pick(pool, kp.build_opener(proxy), a.account)
    {"dump": cmd_dump, "ruler": cmd_ruler, "gate": cmd_gate, "models": cmd_models}[a.command](s, a)


if __name__ == "__main__":
    main()
