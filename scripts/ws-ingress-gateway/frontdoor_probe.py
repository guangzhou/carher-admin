#!/usr/bin/env python3
"""ws-ingress 正门/隧道 WebSocket 验收探针（纯标准库，无需 pip 装包）。

用途：验证 responses-over-WebSocket 这条腿是否端到端通。两种模式：

  1) 握手模式（默认，--turns 0）：只发 WS Upgrade 握手，断言返回
     `101 Switching Protocols`。无需有效 key（网关先升级、转发时才校验 key），
     用来验证「入口机 Upgrade 透传 → 198 nginx 分流 → ws-ingress」整条链路。
     405 = 某一跳把 Upgrade 头剥了（历史上是 IT 入口机 58.241.5.230）。

  2) 两轮增量模式（--turns 2 + 有效 key）：T1 全量问 PING、T2 带
     previous_response_id 追问 PONG，断言两轮都 completed。配合 198 侧
     `kubectl -n litellm-product logs deploy/ws-ingress | grep ws_ingress`
     看 `mode=incremental` 才算增量真的命中。

示例：
  # 正门握手验收（无需 key）
  python3 frontdoor_probe.py
  # 明文直打 198 NodePort（绕过正门，隔离 198 侧）
  python3 frontdoor_probe.py --scheme ws --host 127.0.0.1 --port 30403
  # 两轮增量（临时 key 用完即删）
  python3 frontdoor_probe.py --turns 2 --key sk-xxx --model gpt-5.6-sol

判据记忆：reference_ws_ingress_front_door_blocked_by_it_entry_upgrade_strip
设计文档：docs/ws-ingress-gateway-plan-20260824.md
"""
import argparse, base64, json, os, socket, ssl, struct, sys, time


def connect(host, port, path, scheme, key, timeout):
    raw = socket.create_connection((host, port), timeout=timeout)
    if scheme == "wss":
        ctx = ssl.create_default_context()
        s = ctx.wrap_socket(raw, server_hostname=host)
    else:
        s = raw
    k = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
           f"Sec-WebSocket-Version: 13\r\nAuthorization: Bearer {key}\r\n\r\n")
    s.sendall(req.encode())
    s.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            raise RuntimeError("handshake connection closed: " + buf.decode(errors="replace"))
        buf += d
    head = buf.decode(errors="replace").split("\r\n\r\n")[0]
    status = head.split("\r\n")[0]
    return s, status, head


def send_text(s, obj):
    p = json.dumps(obj).encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(p))
    n = len(p)
    if n < 126:
        hdr = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    s.sendall(hdr + mask + masked)


def recv_frame(s):
    def rd(n):
        b = b""
        while len(b) < n:
            d = s.recv(n - len(b))
            if not d:
                return None
            b += d
        return b
    h = rd(2)
    if not h:
        return None
    b0, b1 = h[0], h[1]
    ln = b1 & 0x7f
    if ln == 126:
        ln = struct.unpack("!H", rd(2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", rd(8))[0]
    pay = rd(ln) if ln else b""
    return b0 & 0x0f, pay


def drive(s, frame, label, timeout):
    send_text(s, frame)
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = recv_frame(s)
        if r is None:
            print(f"[{label}] conn closed by server")
            return None
        op, pay = r
        if op == 8:
            print(f"[{label}] CLOSE frame")
            return None
        if op != 1:
            continue
        ev = json.loads(pay)
        t = ev.get("type")
        if t == "response.completed":
            rid = ev["response"]["id"]
            print(f"[{label}] completed rid={rid}")
            return rid
        if t == "error":
            print(f"[{label}] ERROR frame:", json.dumps(ev)[:200])
            return None
    print(f"[{label}] timeout")
    return None


def user_msg(t):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}


def main():
    ap = argparse.ArgumentParser(description="ws-ingress front-door WebSocket probe")
    ap.add_argument("--host", default="cc.auto-link.com.cn")
    ap.add_argument("--port", type=int, default=0, help="default 443 for wss, 80 for ws")
    ap.add_argument("--path", default="/pro/v1/responses")
    ap.add_argument("--scheme", choices=["wss", "ws"], default="wss")
    ap.add_argument("--key", default="probe-invalid", help="Bearer token; handshake works with any")
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--turns", type=int, default=0, choices=[0, 2], help="0=handshake only, 2=two-turn incremental")
    ap.add_argument("--timeout", type=int, default=15)
    args = ap.parse_args()
    port = args.port or (443 if args.scheme == "wss" else 80)

    try:
        s, status, head = connect(args.host, port, args.path, args.scheme, args.key, args.timeout)
    except Exception as e:
        print("HANDSHAKE FAIL:", repr(e))
        sys.exit(2)

    print("STATUS LINE:", status)
    if "101" not in status:
        print("---- response head ----")
        print(head[:800])
        print("RESULT: FAIL (not 101 — some hop stripped the Upgrade header)")
        sys.exit(1)
    print("RESULT: handshake OK (101 Switching Protocols) — Upgrade passthrough works")

    if args.turns == 2:
        if args.key == "probe-invalid":
            print("NOTE: --turns 2 needs a real --key; skipping turns.")
            s.close()
            sys.exit(0)
        rid1 = drive(s, {"type": "response.create", "generate": True, "model": args.model,
                         "input": [user_msg("Reply with exactly the word: PING")]}, "T1", args.timeout)
        if rid1:
            time.sleep(0.5)
            rid2 = drive(s, {"type": "response.create", "generate": True, "model": args.model,
                             "input": [user_msg("Now reply with exactly the word: PONG")],
                             "previous_response_id": rid1}, "T2", args.timeout)
            print("TURNS RESULT:", "PASS both turns" if rid2 else "T2 FAILED")
            print("→ 确认增量命中请到 198 侧 grep ws_ingress 日志看 T2 mode=incremental")
            sys.exit(0 if rid2 else 1)
        else:
            print("TURNS RESULT: T1 FAILED")
            sys.exit(1)
    try:
        s.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
