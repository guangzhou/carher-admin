#!/usr/bin/env python3
"""chatgpt-acct-taomu-activate.py — 用 cz.taomu.xyz 的卡密给 ChatGPT 账号自动开通订阅(Pro20x 等)。

站点流程(从页面 JS 读出,2026-10-07):
  1 GET  /api/public/recharge-status      维护开关;blocking=true 时站点拒绝验卡
  2 GET  /api/cdk/query?cdk=              只读:卡状态(未使用/开通中/已使用)+ plan_type
  3 POST /api/verify-cdk {cdk}            验卡(不消耗);开通中的卡会带回 jobKey 可续进度
  4 POST /api/run-process {session,cdk}   🔴 不可逆:消耗卡密、占并发名额,起开通任务,回 jobKey
  5 WS  wss://host  {type:subscribe,jobKey,cdk}   推 progress/status,终态 success/failed/manual/maintenance
  6 POST /api/task-verification/<jobKey> {cdk}    遇 verificationRequired(银行 3DS)才需要;
                                                  回 Stripe clientSecret,必须由持卡人在浏览器完成

提交前本地复刻站点的 Session 校验(缺任一字段站点会拒,卡不会烧):
  sessionToken / accessToken / account.id / account.planType / user.id / user.email / expires 必填;
  accessToken 是 RS256 JWT、iss=https://auth.openai.com、aud 含 https://api.openai.com/v1、
  scp 含 model.request、未过期;账号当前 plan 必须满足该卡类型(见 REQUIRED_PLAN)。

用法:
  # 只读预检(不提交,不消耗卡):查卡 + 校验 session + 站点状态
  chatgpt-acct-taomu-activate.py check   --cdk TZ-XXXX --session /tmp/session-acct-165.json
  # 真提交(消耗卡密)。没有 --yes 不会提交
  chatgpt-acct-taomu-activate.py run     --cdk TZ-XXXX --session /tmp/session-acct-165.json --yes
  # 续看一个已在跑的任务
  chatgpt-acct-taomu-activate.py watch   --cdk TZ-XXXX
  # 全套流程用本地 mock 站点闭环验证(不碰真站、不耗卡)
  chatgpt-acct-taomu-activate.py selftest

退出码: 0 成功 · 1 失败/不满足 · 2 需人工(银行验证/站点要求人工) · 3 用法/前置错误
"""
import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("TAOMU_BASE", "https://cz.taomu.xyz")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/126 Safari/537.36"

# 卡类型 → 账号当前 plan 的要求(同站点 REQUIRED_ACCOUNT_PLAN)
REQUIRED_PLAN = {
    "plus": "free", "go": "free", "codex": "any", "pro_5x": "free", "pro_20x": "free",
    "pro_25x": "any", "plus_to_5x": "plus", "to_20x": "plus_or_pro", "renew_20x": "pro",
}
TERMINAL_FAIL = ("failed", "maintenance", "manual")


def classify_plan(raw):
    n = (raw or "").lower()
    if n in ("go", "chatgptgoplan"):
        return "go"
    if "free" in n:
        return "free"
    if "plus" in n:
        return "plus"
    if "pro" in n:
        return "pro"
    return "unknown"


def b64json(part):
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def validate_session(sess, plan_type=None, now=None):
    """返回 (ok, 说明, 摘要)。复刻站点校验;ok=False 时站点必拒,不要去烧卡。"""
    now = now or time.time()
    miss = [k for k, v in (
        ("sessionToken", sess.get("sessionToken")), ("accessToken", sess.get("accessToken") or sess.get("access_token")),
        ("account.id", (sess.get("account") or {}).get("id")),
        ("account.planType", (sess.get("account") or {}).get("planType")),
        ("user.id", (sess.get("user") or {}).get("id")), ("user.email", (sess.get("user") or {}).get("email")),
        ("expires", sess.get("expires"))) if not str(v or "").strip()]
    if miss:
        return False, "Session 缺字段: " + ",".join(miss) + "(要从 chatgpt.com/api/auth/session 取完整 JSON)", {}
    at = sess.get("accessToken") or sess.get("access_token")
    try:
        h, p = b64json(at.split(".")[0]), b64json(at.split(".")[1])
    except Exception:
        return False, "accessToken 不是合法 JWT", {}
    if h.get("typ") != "JWT" or h.get("alg") != "RS256":
        return False, "accessToken 头不对 typ=%s alg=%s" % (h.get("typ"), h.get("alg")), {}
    if p.get("iss") != "https://auth.openai.com":
        return False, "accessToken iss 不对", {}
    aud = p.get("aud") if isinstance(p.get("aud"), list) else [p.get("aud")]
    if "https://api.openai.com/v1" not in aud:
        return False, "accessToken aud 不含 https://api.openai.com/v1", {}
    auth = p.get("https://api.openai.com/auth") or {}
    if not auth.get("chatgpt_account_id") or not auth.get("chatgpt_user_id"):
        return False, "accessToken 缺账户信息", {}
    if "model.request" not in (p.get("scp") or []):
        return False, "accessToken 缺 model.request 权限(不是 codex/web 登录签的 AT)", {}
    if float(p.get("exp", 0)) <= now:
        return False, "accessToken 已过期 %s" % time.strftime("%m-%d %H:%M", time.gmtime(p.get("exp", 0))), {}
    if auth.get("chatgpt_account_id") != (sess.get("account") or {}).get("id") and classify_plan(
            sess["account"]["planType"]) == "plus":
        return False, "Session 账号信息不一致(AT 的 account_id ≠ session.account.id)", {}
    have = classify_plan(sess["account"]["planType"])
    summary = {"email": sess["user"]["email"], "plan": have, "account": sess["account"]["id"],
               "at_exp": time.strftime("%m-%d %H:%M", time.gmtime(p["exp"]))}
    if plan_type:
        need = REQUIRED_PLAN.get(plan_type, "free")
        okp = need == "any" or (have in ("plus", "pro") if need == "plus_or_pro" else have == need)
        if not okp:
            return False, "该卡(%s)要求账号当前为 %s,而这个账号是 %s" % (plan_type, need, have), summary
    return True, "ok", summary


class Site:
    def __init__(self, base=BASE, timeout=40):
        self.base, self.timeout = base.rstrip("/"), timeout
        self.ctx = ssl.create_default_context()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=self.timeout, context=self.ctx) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"success": False, "message": raw[:200].decode(errors="replace")}

    def status(self):
        return self.req("GET", "/api/public/recharge-status")[1]

    def query(self, cdk):
        return self.req("GET", "/api/cdk/query?cdk=" + urllib.parse.quote(cdk))[1]

    def verify(self, cdk):
        return self.req("POST", "/api/verify-cdk", {"cdk": cdk})[1]

    def run(self, cdk, session_text):
        return self.req("POST", "/api/run-process", {"session": session_text, "cdk": cdk})[1]

    def task_verification(self, job, cdk):
        return self.req("POST", "/api/task-verification/" + urllib.parse.quote(job), {"cdk": cdk})[1]


class MiniWS:
    """标准库 WebSocket 客户端(文本帧、客户端掩码、ping/pong/close)。只为订阅进度,不求完备。"""

    def __init__(self, url, timeout=30):
        import socket
        import secrets
        u = urllib.parse.urlparse(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if u.scheme == "wss" else raw
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        self.sock.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nUser-Agent: %s\r\n\r\n"
                           % (path, u.netloc, key, UA)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = self.sock.recv(4096)
            if not c:
                raise ConnectionError("握手中断")
            head += c
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("WS 握手失败: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise ConnectionError("连接关闭")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        import secrets
        pay = text.encode()
        mask = secrets.token_bytes(4)
        n = len(pay)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big")
                               if n < 65536 else bytes([0x80 | 127]) + n.to_bytes(8, "big"))
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(pay)))

    def recv(self):
        """返回文本;超时抛 socket.timeout。"""
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), "big")
            elif n == 127:
                n = int.from_bytes(self._read(8), "big")
            pay = self._read(n)
            if op == 0x8:
                raise ConnectionError("服务端关闭连接")
            if op == 0x9:  # ping → pong
                self.sock.sendall(bytes([0x8A, 0x80]) + b"\0\0\0\0")
                continue
            if op in (0x1, 0x0):
                return pay.decode(errors="replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def watch_ws(base, job, cdk, on_event, deadline_s=900):
    """订阅任务进度。返回终态事件 dict;超时返回 {'status':'timeout'};WS 不通则降级轮询。"""
    import socket
    url = base.replace("https://", "wss://").replace("http://", "ws://")
    try:
        ws = MiniWS(url, timeout=30)
    except Exception as e:
        on_event({"status": "running", "message": "WS 连不上(%s),降级轮询" % e, "progress": None})
        return poll_fallback(Site(base), cdk, deadline_s, on_event)
    ws.send(json.dumps({"type": "subscribe", "jobKey": job, "cdk": cdk}))
    t0 = last_ping = time.time()
    try:
        while time.time() - t0 < deadline_s:
            if time.time() - last_ping > 20:
                ws.send(json.dumps({"type": "ping"}))
                last_ping = time.time()
            try:
                msg = json.loads(ws.recv())
            except socket.timeout:
                continue
            except ConnectionError as e:
                on_event({"status": "running", "message": "WS 断开(%s),降级轮询" % e, "progress": None})
                return poll_fallback(Site(base), cdk, deadline_s - (time.time() - t0), on_event)
            if msg.get("type") == "pong":
                continue
            if msg.get("type") == "subscription_error":
                return {"status": "failed", "message": "订阅被拒(jobKey/cdk 不匹配)"}
            on_event(msg)
            if msg.get("status") in ("success",) + TERMINAL_FAIL:
                return msg
    finally:
        ws.close()
    return {"status": "timeout", "message": "等待 %ds 无终态" % deadline_s}


def poll_fallback(site, cdk, deadline_s, on_event):
    """没装 websocket-client 时的降级:轮询 /api/cdk/query,只能判 已使用/开通中。"""
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        d = (site.query(cdk) or {}).get("data") or {}
        on_event({"status": "running", "message": "query: %s" % d.get("status"), "progress": None})
        if d.get("status") == "已使用":
            return {"status": "success", "message": "卡已使用(降级轮询无法区分成功/失败,请验 live plan)"}
        time.sleep(10)
    return {"status": "timeout", "message": "轮询超时"}


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def cmd_check(a, site=None):
    site = site or Site()
    st = site.status()
    say("站点: maintenance=%s blocking=%s message=%r" % (st.get("maintenance"), st.get("blocking"), st.get("message")))
    q = (site.query(a.cdk) or {})
    d = q.get("data") or {}
    if not q.get("success"):
        say("✗ 查卡失败: %s" % q.get("message")); return 1, None
    say("卡: status=%s plan=%s(%s) 地区=%s/%s usedAt=%s jobKey=%s" % (
        d.get("status"), d.get("plan_type"), d.get("plan_label"), d.get("checkout_country"),
        d.get("checkout_currency"), d.get("usedAt"), d.get("jobKey")))
    sess = json.load(open(a.session))
    ok, why, sm = validate_session(sess, d.get("plan_type"))
    say("Session: %s  %s" % ("✓" if ok else "✗", why) + ("  %s" % sm if sm else ""))
    if d.get("status") == "已使用":
        say("✗ 卡已使用,换一张"); return 1, d
    if st.get("blocking") or st.get("maintenance"):
        say("✗ 站点维护中,稍后再试"); return 1, d
    return (0 if ok else 1), d


def cmd_run(a, site=None):
    site = site or Site()
    rc, d = cmd_check(a, site)
    if rc != 0:
        return rc
    if not a.yes:
        say("预检通过。未加 --yes,不提交(提交会消耗卡密)。"); return 0
    v = site.verify(a.cdk)
    if not v.get("success"):
        say("✗ verify-cdk: %s" % v.get("message")); return 1
    job = (v.get("data") or {}).get("jobKey") if (v.get("data") or {}).get("status") == "processing" else None
    if job:
        say("该卡已有任务在跑,接着看 jobKey=%s(不重复提交)" % job)
    else:
        text = open(a.session).read().strip()
        r = site.run(a.cdk, text)
        if not (r.get("success") and r.get("jobKey")):
            say("✗ run-process: %s(卡会回滚,可换账号/稍后重试)" % r.get("message")); return 1
        job = r["jobKey"]
        say("任务已启动 jobKey=%s" % job)
    state = {"verif_hint": False}

    def on_event(m):
        if m.get("progress") is not None or m.get("message"):
            say("  [%s] %s%% %s" % (m.get("status"), m.get("progress"), m.get("message") or ""))
        if m.get("verificationRequired") and not state["verif_hint"]:
            state["verif_hint"] = True
            say("  ⚠️ 需要银行卡安全验证(3DS):必须持卡人在浏览器里完成。打开 %s/ → 输入卡密 → 页面会继续该订单。" % site.base)
    end = watch_ws(site.base, job, a.cdk, on_event, a.timeout)
    s = end.get("status")
    say("终态: %s %s" % (s, end.get("message") or ""))
    if s == "success":
        return 0
    if s in ("manual",) or state["verif_hint"]:
        return 2
    return 1


def cmd_watch(a, site=None):
    site = site or Site()
    v = site.verify(a.cdk)
    d = v.get("data") or {}
    if not d.get("jobKey"):
        say("没有在跑的任务: %s" % (v.get("message") or d.get("status"))); return 1
    end = watch_ws(site.base, d["jobKey"], a.cdk, lambda m: say("  [%s] %s%% %s" % (
        m.get("status"), m.get("progress"), m.get("message") or "")), a.timeout)
    return 0 if end.get("status") == "success" else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for n in ("check", "run", "watch"):
        p = sp.add_parser(n)
        p.add_argument("--cdk", required=True)
        if n != "watch":
            p.add_argument("--session", required=True)
        if n == "run":
            p.add_argument("--yes", action="store_true", help="确认提交(消耗卡密)")
        if n != "check":
            p.add_argument("--timeout", type=int, default=900)
    sp.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "selftest":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from importlib import import_module
        return import_module("taomu_mock_selftest").run_selftest()
    if a.cmd == "check":
        return cmd_check(a)[0]
    return {"run": cmd_run, "watch": cmd_watch}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
