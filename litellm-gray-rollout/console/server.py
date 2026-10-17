#!/usr/bin/env python3
"""升级驾驶台 HTTP 服务。只监听 127.0.0.1（浏览器经 ssh -L 进来），只用标准库（198 上不装新依赖）。

  python3 server.py [--port 8795] [--no-auto]

后台线程每 60 秒：采集 → 判定 → 若 PASS 且自动推进开着 ⇒ 调 step.sh 升一档；其它情况只更新页面，不动手。
驾驶台挂了 / 被杀了 = 什么都不发生：路由真相在 /etc/nginx/gray-route/active，自动止损由 monitor loop 独立负责。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hmac
import json
import os
import secrets
import sys
import threading
import time
import traceback
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import actions  # noqa: E402
import collect  # noqa: E402
import engine  # noqa: E402

STATE: dict = {"verdict": None, "readings": None, "raw": None, "at": None, "error": None}
SETTINGS = {"auto": True, "paused_by": None}
_lock = threading.Lock()
TOKEN = ""  # 每次启动随机生成，写进 0600 文件；写操作必须带它（防同机其它进程/页面误触）


def _settings_file() -> Path | None:
    rd = collect.run_dir()
    return rd / "console" / "settings.json" if rd else None


def load_settings():
    f = _settings_file()
    if f and f.exists():
        try:
            SETTINGS.update(json.loads(f.read_text()))
        except Exception:
            pass


def save_settings():
    f = _settings_file()
    if not f:
        return
    f.parent.mkdir(mode=0o700, exist_ok=True)
    f.write_text(json.dumps(SETTINGS, ensure_ascii=False))
    os.chmod(f, 0o600)


def tick(auto_act: bool = True):
    try:
        r, raw = collect.collect()
        v = engine.evaluate(r, auto_enabled=SETTINGS["auto"])
        busy = actions.lock_holder_busy()
        running = actions.running()
        acting = any(not x["done"] for x in running.values())
        # 每个 generation 最多自动尝试一次：成功的升档会换 generation，所以「本 generation 里已结束的机器 ramp」
        # = 它跑完了档位却没变（门禁拒了 / STOP）。不再重敲，交给人。先改写结论，再记日志、再存。
        attempt = _last_auto_attempt(r.generation)
        if attempt is not None and v.state == "PASS" and v.next_action == "ramp":
            why = f"升档脚本跑完了（rc={attempt.get('rc')}）但档位没变：门禁没让过，本档不再自动重试"
            v = engine.Verdict("FAIL", f"自动升到 {v.next_arg}% 没成功，停下等你",
                               [why, (attempt.get("tail") or "")[-300:]],
                               decision=engine._decision_hold(r, [why + "。看「正在执行」里的日志找原因"]))
        with _lock:
            prev = STATE.get("verdict")
            STATE.update(verdict=asdict(v), readings=asdict(r), raw=raw,
                         at=dt.datetime.now(dt.timezone.utc).isoformat(), error=None,
                         takeover=busy and not acting, running=running)
        if prev is None or prev.get("state") != v.state or prev.get("headline") != v.headline:
            actions.log_event("verdict", "machine", state=v.state, headline=v.headline, reasons=v.reasons[:6])
        if (auto_act and SETTINGS["auto"] and v.state == "PASS" and v.next_action == "ramp"
                and not busy and not acting):
            res = actions.start("ramp", v.next_arg, who="machine", generation=r.generation)
            actions.log_event("auto_ramp", "machine", target=v.next_arg, result=res, generation=r.generation)
    except Exception as e:  # 采集失败 ≠ 线上有事；页面显示「读不到」，不动手
        with _lock:
            STATE.update(error=f"{type(e).__name__}: {e}", at=dt.datetime.now(dt.timezone.utc).isoformat())
        traceback.print_exc()


def _last_auto_attempt(generation: str | None) -> dict | None:
    """本 generation 里机器发起过、且已经结束的 ramp。还在跑的不算（acting 会挡住）。"""
    if not generation:
        return None
    started = {e.get("pid"): e for e in actions.read_events(400)
               if e.get("kind") == "action_start" and e.get("action") == "ramp" and e.get("who") == "machine"
               and e.get("generation") == generation}
    for e in reversed(actions.read_events(400)):
        if e.get("kind") == "action_end" and e.get("action") == "ramp" and e.get("pid") in started:
            return e
    return None


def loop(interval: int):
    while True:
        tick()
        time.sleep(interval)


class H(BaseHTTPRequestHandler):
    server_version = "gray-console/1"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "static" / "index.html").read_bytes(), "text/html")
        if self.path == "/api/state":
            with _lock:
                body = dict(STATE, settings=SETTINGS, events=actions.read_events(80))
            return self._send(200, body)
        if self.path.startswith("/api/log?"):
            from urllib.parse import parse_qs, urlparse
            p = parse_qs(urlparse(self.path).query).get("path", [""])[0]
            return self._send(200, {"tail": actions.tail_log(p)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get("X-Console-Token", ""), TOKEN):
            return self._send(403, {"error": "缺少或错误的 token（见启动时打印的 token 文件）"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        who = str(req.get("who") or "human")[:40]
        if self.path == "/api/auto":
            SETTINGS["auto"] = bool(req.get("on"))
            SETTINGS["paused_by"] = None if SETTINGS["auto"] else who
            save_settings()
            actions.log_event("auto_toggle", who, on=SETTINGS["auto"])
            threading.Thread(target=tick, kwargs={"auto_act": False}, daemon=True).start()
            return self._send(200, {"ok": True, "auto": SETTINGS["auto"]})
        if self.path == "/api/action":
            act = str(req.get("action") or "")
            if act in ("noop", ""):
                actions.log_event("decision", who, choice="noop")
                return self._send(200, {"ok": True})
            if act == "pause_auto":
                SETTINGS["auto"] = False; SETTINGS["paused_by"] = who; save_settings()
                actions.log_event("auto_toggle", who, on=False)
                return self._send(200, {"ok": True})
            if act == "ramp_force":
                with _lock:
                    split = (STATE.get("readings") or {}).get("split")
                nxt = engine.next_split(split or 0)
                if nxt is None:
                    return self._send(400, {"error": "没有下一档"})
                act, req["arg"] = "ramp", nxt
            if not req.get("confirm"):
                return self._send(400, {"error": "人工动作需要 confirm=true"})
            actions.log_event("decision", who, choice=act, arg=req.get("arg"))
            res = actions.start(act, req.get("arg"), who=who)
            threading.Thread(target=tick, kwargs={"auto_act": False}, daemon=True).start()
            return self._send(200 if res.get("ok") else 409, res)
        if self.path == "/api/refresh":
            threading.Thread(target=tick, kwargs={"auto_act": False}, daemon=True).start()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def main():
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8795)
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--no-auto", action="store_true", help="启动时就关掉自动推进")
    ap.add_argument("--token-file", default="/root/l3/.console-token")
    a = ap.parse_args()
    load_settings()
    if a.no_auto:
        SETTINGS["auto"] = False
    TOKEN = secrets.token_urlsafe(24)
    fd = os.open(a.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, TOKEN.encode()); os.close(fd)
    threading.Thread(target=loop, args=(a.interval,), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"gray console on http://127.0.0.1:{a.port}  token file {a.token_file}  auto={SETTINGS['auto']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
