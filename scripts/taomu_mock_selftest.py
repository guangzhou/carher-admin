"""taomu 开通脚本的闭环自测:本地起一个复刻 cz.taomu.xyz 协议的 mock 站点(HTTP + WebSocket),
再用 chatgpt-acct-taomu-activate.py 的真实代码路径跑完整流程。不碰真站、不耗卡。

mock 复刻的行为(都来自页面 JS 里读到的协议):
  GET  /api/public/recharge-status       维护开关
  GET  /api/cdk/query?cdk=               未使用/开通中/已使用 + plan_type
  POST /api/verify-cdk                   开通中的卡带回 jobKey
  POST /api/run-process                  校验 session 必填字段 → 起任务(卡置「开通中」)
  WS   subscribe{jobKey,cdk}             snapshot/progress → (可选)verificationRequired → success/failed
场景:
  ok            完整成功
  3ds           中途 verificationRequired=true,要人工 → 退出码 2
  fail_rollback 开通失败,卡自动回滚成「未使用」→ 退出码 1
  used          卡已使用 → 预检拒绝,run-process 一次都不能被调
  maintenance   站点 blocking → 预检拒绝
  bad_plan      账号当前 plan 不满足 → 预检拒绝,run-process 一次都不能被调
  resume        卡已在「开通中」→ 不重复提交,直接续看
  ws_down       WS 不可用 → 降级轮询
"""
import base64
import hashlib
import importlib.util
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def load_script():
    spec = importlib.util.spec_from_file_location("taomu", os.path.join(HERE, "chatgpt-acct-taomu-activate.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def fake_jwt(exp_in=86400, scp=("model.request",), acct="acct-mock-1"):
    def b(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    h = {"alg": "RS256", "typ": "JWT"}
    p = {"iss": "https://auth.openai.com", "aud": ["https://api.openai.com/v1"], "scp": list(scp),
         "exp": int(time.time()) + exp_in,
         "https://api.openai.com/auth": {"chatgpt_account_id": acct, "chatgpt_user_id": "user-mock"}}
    return b(h) + "." + b(p) + ".sig"


def fake_session(plan="free", **kw):
    return {"user": {"id": "user-mock", "email": "mock@mail.com"}, "expires": "2027-01-01T00:00:00Z",
            "account": {"id": "acct-mock-1", "planType": plan}, "sessionToken": "st-mock",
            "accessToken": fake_jwt(**kw)}


class MockState:
    def __init__(self, scenario):
        self.scenario = scenario
        self.cdk_status = {"used": "已使用", "resume": "开通中"}.get(scenario, "未使用")
        self.plan_type = "to_20x" if scenario == "bad_plan" else "pro_20x"
        self.run_calls = 0
        self.verify_calls = 0
        self.job = "job-mock-1"
        self.sub_count = 0
        self.lock = threading.Lock()


def make_handler(st):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if self.headers.get("Upgrade", "").lower() == "websocket":
                return self.ws()
            if u.path == "/api/public/recharge-status":
                blk = st.scenario == "maintenance"
                return self._json({"success": True, "maintenance": blk, "blocking": blk, "message": "维护" if blk else ""})
            if u.path == "/api/cdk/query":
                return self._json({"success": True, "data": {
                    "status": st.cdk_status, "plan_type": st.plan_type, "plan_label": "Pro20x开通",
                    "checkout_country": "PH", "checkout_currency": "PHP",
                    "jobKey": st.job if st.cdk_status == "开通中" else None,
                    "usedAt": "2026-10-07 14:55:09" if st.cdk_status == "已使用" else None}})
            self._json({"success": False}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            u = urlparse(self.path)
            if u.path == "/api/verify-cdk":
                st.verify_calls += 1
                d = {"plan_type": st.plan_type, "plan_label": "Pro20x开通", "checkout_country": "PH",
                     "checkout_currency": "PHP", "status": "processing" if st.cdk_status == "开通中" else "ok"}
                if st.cdk_status == "开通中":
                    d["jobKey"] = st.job
                return self._json({"success": st.cdk_status != "已使用", "data": d,
                                   "message": "卡密已使用" if st.cdk_status == "已使用" else ""})
            if u.path == "/api/run-process":
                st.run_calls += 1
                try:
                    s = json.loads(body.get("session") or "{}")
                except Exception:
                    return self._json({"success": False, "message": "Session 格式错误"})
                for k in ("sessionToken", "accessToken"):
                    if not s.get(k):
                        return self._json({"success": False, "message": "缺 " + k})
                if st.cdk_status != "未使用":
                    return self._json({"success": False, "message": "卡密不可用"})
                st.cdk_status = "开通中"
                if st.scenario == "ws_down":
                    # WS 不可用时任务照常在后台跑完:模拟真站「进度推送挂了但开通没挂」
                    threading.Timer(1.0, lambda: setattr(st, "cdk_status", "已使用")).start()
                return self._json({"success": True, "jobKey": st.job})
            self._json({"success": False}, 404)

        def ws(self):
            if st.scenario == "ws_down":
                self.send_response(503); self.end_headers(); return
            key = self.headers["Sec-WebSocket-Key"]
            acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket"); self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", acc); self.end_headers()
            self.wfile.flush()
            sock = self.connection
            sock.settimeout(5)

            def rd(n):
                b = b""
                while len(b) < n:
                    c = sock.recv(n - len(b))
                    if not c:
                        raise ConnectionError
                    b += c
                return b

            def recv_text():
                b0, b1 = rd(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", rd(2))[0]
                mask = rd(4) if b1 & 0x80 else b"\0\0\0\0"
                pay = bytes(c ^ mask[i % 4] for i, c in enumerate(rd(n)))
                return pay.decode() if (b0 & 0x0F) == 1 else ""

            def send(obj):
                pay = json.dumps(obj).encode()
                hdr = bytes([0x81]) + (bytes([len(pay)]) if len(pay) < 126 else bytes([126]) + struct.pack(">H", len(pay)))
                sock.sendall(hdr + pay)

            try:
                while True:
                    m = recv_text()
                    if not m:
                        continue
                    m = json.loads(m)
                    if m.get("type") == "ping":
                        send({"type": "pong"}); continue
                    if m.get("type") == "subscribe":
                        break
                if m.get("jobKey") != st.job:
                    send({"type": "subscription_error"}); return
                st.sub_count += 1
                send({"type": "snapshot", "jobKey": st.job, "status": "running", "progress": 8, "message": "正在连接代理网络"})
                steps = [(25, "打开官方 Checkout"), (50, "提交支付信息")]
                for p, msg in steps:
                    time.sleep(0.15); send({"type": "progress", "jobKey": st.job, "status": "running", "progress": p, "message": msg})
                if st.scenario == "3ds":
                    send({"type": "status", "jobKey": st.job, "status": "running", "progress": 70,
                          "message": "等待银行安全验证", "verificationRequired": True})
                    time.sleep(0.3)
                    send({"type": "status", "jobKey": st.job, "status": "manual", "progress": 70, "message": "需要持卡人完成安全验证"})
                    return
                if st.scenario == "fail_rollback":
                    st.cdk_status = "未使用"
                    send({"type": "status", "jobKey": st.job, "status": "failed", "progress": 60, "message": "支付被拒,卡密已回滚"})
                    return
                time.sleep(0.15)
                st.cdk_status = "已使用"
                send({"type": "status", "jobKey": st.job, "status": "success", "progress": 100, "message": "激活成功",
                      "email": "mock@mail.com", "planType": st.plan_type})
            except Exception:
                pass
    return H


def start_mock(scenario):
    st = MockState(scenario)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return st, srv


class Args:
    def __init__(self, **kw):
        self.timeout = 20
        self.yes = True
        self.__dict__.update(kw)


def run_scenario(mod, scenario, session_obj, expect_rc, expect_run_calls, expect_final_cdk=None):
    st, srv = start_mock(scenario)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(session_obj, tf); tf.close()
    site = mod.Site(base)
    a = Args(cdk="TZ-MOCK", session=tf.name)
    t0 = time.time()
    rc = mod.cmd_run(a, site)
    os.unlink(tf.name)
    srv.shutdown()
    ok = (rc == expect_rc and st.run_calls == expect_run_calls
          and (expect_final_cdk is None or st.cdk_status == expect_final_cdk))
    print("  %-14s rc=%s(期望%s) run-process调用=%s(期望%s) 卡末态=%s%s  %.1fs  %s" % (
        scenario, rc, expect_rc, st.run_calls, expect_run_calls, st.cdk_status,
        "(期望%s)" % expect_final_cdk if expect_final_cdk else "", time.time() - t0, "✓" if ok else "✗ FAIL"))
    return ok


def unit_validate(mod):
    good = fake_session("free")
    cases = [
        ("合法 free + pro_20x", lambda: mod.validate_session(good, "pro_20x"), True),
        ("缺 sessionToken", lambda: mod.validate_session({**good, "sessionToken": ""}, "pro_20x"), False),
        ("缺 expires", lambda: mod.validate_session({k: v for k, v in good.items() if k != "expires"}, "pro_20x"), False),
        ("AT 已过期", lambda: mod.validate_session(fake_session("free", exp_in=-10), "pro_20x"), False),
        ("AT 缺 model.request", lambda: mod.validate_session(fake_session("free", scp=("openid",)), "pro_20x"), False),
        ("AT 不是 JWT", lambda: mod.validate_session({**good, "accessToken": "abc"}, "pro_20x"), False),
        ("pro 账号买 pro_20x(要求 free)", lambda: mod.validate_session(fake_session("pro"), "pro_20x"), False),
        ("pro 账号买 renew_20x(要求 pro)", lambda: mod.validate_session(fake_session("pro"), "renew_20x"), True),
        ("free 账号买 renew_20x", lambda: mod.validate_session(good, "renew_20x"), False),
        ("free 账号买 codex(any)", lambda: mod.validate_session(good, "codex"), True),
        ("plus 买 to_20x(plus_or_pro)", lambda: mod.validate_session(fake_session("plus"), "to_20x"), True),
    ]
    ok = True
    for name, fn, want in cases:
        got = fn()[0]
        flag = got == want
        ok &= flag
        print("  %-34s ok=%-5s %s" % (name, got, "✓" if flag else "✗ FAIL(期望%s)" % want))
    return ok


def run_selftest():
    mod = load_script()
    print("== 1 Session 校验(复刻站点规则,含负例)")
    r = unit_validate(mod)
    print("== 2 mock 站点闭环(HTTP + WebSocket,真实代码路径)")
    free = fake_session("free")
    r &= run_scenario(mod, "ok", free, 0, 1, "已使用")
    r &= run_scenario(mod, "3ds", free, 2, 1)
    r &= run_scenario(mod, "fail_rollback", free, 1, 1, "未使用")
    r &= run_scenario(mod, "used", free, 1, 0, "已使用")
    r &= run_scenario(mod, "maintenance", free, 1, 0, "未使用")
    r &= run_scenario(mod, "bad_plan", free, 1, 0, "未使用")
    r &= run_scenario(mod, "resume", free, 0, 0)
    r &= run_scenario(mod, "ws_down", free, 0, 1)
    print("== 3 --yes 门禁:预检通过但不加 --yes 时一次 run-process 都不能发")
    st, srv = start_mock("ok")
    tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(free, tf); tf.close()
    rc = mod.cmd_run(Args(cdk="TZ-MOCK", session=tf.name, yes=False), mod.Site("http://127.0.0.1:%d" % srv.server_address[1]))
    os.unlink(tf.name); srv.shutdown()
    g = rc == 0 and st.run_calls == 0
    print("  不加 --yes   rc=%s run-process调用=%s  %s" % (rc, st.run_calls, "✓" if g else "✗ FAIL"))
    r &= g
    print("\nSELFTEST=%s" % ("PASS" if r else "FAIL"))
    return 0 if r else 1


if __name__ == "__main__":
    sys.exit(run_selftest())
