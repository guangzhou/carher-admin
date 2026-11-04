#!/usr/bin/env python3
"""本地预览用的假后端（只用标准库）。不连任何线上，不发任何真 key。

    python3 scripts/ai-portal/static/dev_mock.py [--port 8977]
    打开 http://127.0.0.1:8977/ai/?mock=<场景>

场景（?mock= 写进 cookie，之后所有 /ai/api/* 都按这个场景回）：
    internal        内部同事，有一把可用 key（默认）
    external        外部合作方，有一把可用 key
    internal_nokey  内部同事，没有 key
    external_nokey  外部合作方，没有 key（要先跟机器人说话）
    pending         有一把「生成中」的 key；轮询 2 次后变成可用（演示自动刷新）
    disabled        当前唯一一把 key「已禁用」（离职 / 换新）⇒ 应出申请卡
    failed          当前 key「生成失败: <原因>」⇒ 行内「重新申请」，点了变生成中
    history         一把当前可用 + 3 把历史（current=false），历史默认折叠
    logged_out      所有接口 401（未登录）
    error           /ai/api/keys 回 500 JSON 错误信封
    down            /ai/api/keys 回 502 HTML（网关挂了，非 JSON）
  重置我的 key（POST /ai/api/keys/rotate）：
    rotate_ok       点「重置」⇒ 成功，回一把新的假 key；列表里那把跟着换
    rotate_warn     成功，但「旧 key 没能立刻停用」⇒ 结果卡片带黄色警告
    rotate_fail     失败：新 key 写不进表 ⇒ 已撤掉新的，旧 key 还能用（后端 502 + message）
    rotate_cool     失败：刚重置过，冷却中（429）
    rotate_dry      演练模式（后端 ROTATE_DRY_RUN=1）：只回将要怎么做，什么都不改
    rotate_off      后端没开重置（rotate_enabled=false）⇒ 退回「请提反馈」那句
    其余场景都按 rotate_enabled=true + 成功处理（同 rotate_ok）
登录相关：
    /ai/auth/magic?t=<任意>        一次性登录链接确认页（GET 只出页面，不消耗 token）
        t 以 expired / used / invalid 开头 ⇒ POST 回对应 error；其余 ⇒ 成功并登录成外部用户
    /ai/?mock=logged_out&login_error=expired|used|invalid   后端回跳带错误
另外：?mock=...&slow=1 让每个接口慢 1.5 秒（看加载态）；?mock=...&codettl=15 让安装码 15 秒过期。

接口约定（真后端 scripts/ai-portal/server.py 由同事实现，这里只是模拟）：
    GET  /ai/api/me              -> {name, external, tenant}
    GET  /ai/api/keys            -> {keys:[{record_id, account_type, masked, status, current}]}
                                    status 按前缀：可用 / 生成中 / 已禁用… / 生成失败: <原因>
    POST /ai/api/keys/reveal     {record_id} -> {key}
    POST /ai/api/apply           {account_type} -> {ok, eta_minutes}
    POST /ai/api/install-code    {tool} -> {code, expires_in, tool}
                                    tool = cursor / codex / claude / all（all = 一条命令装三个）
    POST /ai/api/feedback        {type, tool, title, detail, report, os, page} -> {ok, id}
    POST /ai/api/keys/rotate     {record_id?} -> {ok, dry_run, key, masked, old_alias, new_alias, warning?}
                                    失败 4xx/5xx {error, message}；前端只读 message
    GET  /ai/auth/login?redirect=…   -> 302（mock：切成 internal）
    GET  /ai/auth/logout?redirect=…  -> 302（mock：切成 logged_out）
    GET  /ai/auth/magic?t=…          -> 出页面（只显示「确认登录」按钮）
    POST /ai/auth/magic {t}          -> 200 {ok} 并 Set-Cookie；失败 4xx {error: expired|used|invalid, message}
"""
import argparse
import json
import os
import secrets
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIOS = {"internal", "external", "internal_nokey", "external_nokey",
             "pending", "disabled", "failed", "history", "logged_out", "error", "down",
             "rotate_ok", "rotate_warn", "rotate_fail", "rotate_cool", "rotate_dry", "rotate_off"}
TOOLS = {"cursor", "codex", "claude", "all"}   # all = 一条命令装三个（真后端 ISSUABLE 同形状）
# 明显是假的、每次启动随机，绝不是真 key
FAKE_KEYS = {rid: "sk-mock-" + secrets.token_hex(12) for rid in ("rec_mock_1", "rec_mock_2", "rec_mock_p", "rec_mock_n",
                                                      "rec_mock_d", "rec_mock_f", "rec_h1", "rec_h2", "rec_h3")}

STATE = {"polls": 0, "applied": set(), "feedback": 0, "rotated": None}   # rotated = 重置后的新假 key


def masked(k):
    return k[:5] + "…" + k[-4:]


class H(BaseHTTPRequestHandler):
    server_version = "ai-portal-mock"

    def log_message(self, fmt, *args):  # 不打 body，只打路径
        print("%s %s" % (self.command, self.path.split("?")[0]))

    # ---------- helpers ----------
    def _cookie(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return {k: v.value for k, v in c.items()}

    def _scenario(self):
        s = self._cookie().get("mock", "internal")
        return s if s in SCENARIOS else "internal"

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None

    def _slow(self):
        if self._cookie().get("slow") == "1":
            time.sleep(1.5)

    def _redirect(self, to, set_cookies):
        self.send_response(302)
        self.send_header("Location", to)
        for c in set_cookies:
            self.send_header("Set-Cookie", c + "; Path=/; SameSite=Lax")
        self.end_headers()

    @staticmethod
    def _safe_redirect(q):
        r = (q.get("redirect") or ["/ai/"])[0]
        return r if r.startswith("/ai/") and not r.startswith("//") else "/ai/"

    # ---------- data per scenario ----------
    def _me(self, s):
        rot = {"rotate_enabled": s != "rotate_off", "rotate_dry_run": s == "rotate_dry", "rotate_cooldown": 600}
        if s.startswith("external"):
            # 真后端:机器人签的登录链接 name 恒为空、tenant 是 tenant_key(一串 id),这里照实模拟
            return dict(rot, name="", external=True, tenant="2a0b_example_tenant", apply_review_at=0)
        return dict(rot, name="刘国现", external=False, tenant="CarHer")

    def _keys(self, s, poll=False):
        """poll=True 只在 GET /ai/api/keys 时传：只有真轮询才推进「生成中→可用」的计数。"""
        if s in ("internal", "external") or s.startswith("rotate_"):
            ks = [{"record_id": "rec_mock_1", "account_type": "Cursor", "status": "可用"}]
            if s == "internal":
                ks.append({"record_id": "rec_mock_2", "account_type": "Cursor", "status": "已禁用（换新）", "current": False})
        elif s == "pending":
            STATE["polls"] += 1 if poll else 0
            st = "可用" if STATE["polls"] > 2 else "生成中"
            ks = [{"record_id": "rec_mock_p", "account_type": "Cursor", "status": st}]
        elif s in STATE["applied"]:
            # 申请过（含「生成失败」后重新申请）⇒ 生成中，轮询 2 次后可用
            STATE["polls"] += 1 if poll else 0
            st = "可用" if STATE["polls"] > 2 else "生成中"
            ks = [{"record_id": "rec_mock_n", "account_type": "Cursor", "status": st}]
        elif s == "disabled":
            ks = [{"record_id": "rec_mock_d", "account_type": "Cursor", "status": "已禁用（离职）"}]
        elif s == "failed":
            ks = [{"record_id": "rec_mock_f", "account_type": "Cursor", "status": "生成失败: 网关创建 key 超时"}]
        elif s == "history":
            ks = [{"record_id": "rec_mock_1", "account_type": "Cursor", "status": "可用"},
                  {"record_id": "rec_h1", "account_type": "Cursor", "status": "已禁用", "current": False},
                  {"record_id": "rec_h2", "account_type": "OpenClaw-her", "status": "已禁用（换新）", "current": False},
                  {"record_id": "rec_h3", "account_type": "Cursor", "status": "生成失败: 飞书表写入冲突", "current": False}]
        else:
            ks = []
        for k in ks:
            k.setdefault("current", True)
            ok = not (k["status"].startswith("生成中") or k["status"].startswith("生成失败"))
            plain = STATE["rotated"] if (k["record_id"] == "rec_mock_1" and STATE["rotated"]) else FAKE_KEYS[k["record_id"]]
            k["masked"] = masked(plain) if ok else ""
        return ks

    # ---------- routes ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p == "/" or p == "/ai":
            return self._redirect("/ai/", [])
        if p == "/ai/auth/login":
            STATE["polls"] = 0
            ext = (q.get("as") or [""])[0] == "external"
            return self._redirect(self._safe_redirect(q), ["mock=" + ("external" if ext else "internal")])
        if p == "/ai/auth/logout":
            return self._redirect(self._safe_redirect(q), ["mock=logged_out"])
        if p.startswith("/ai/api/"):
            return self._api("GET", p, None)
        if p in ("/ai/", "/ai/index.html", "/ai/auth/magic"):
            # ?mock= 切场景：写 cookie 后照常出页面（URL 上留着 mock 参数无妨，它不是凭据）
            set_c = []
            m = (q.get("mock") or [None])[0]
            if p == "/ai/auth/magic":
                m = None  # GET 落地页绝不改登录态 / 不消耗 token（对着飞书链接预览的预取）
            if m in SCENARIOS:
                set_c.append("mock=" + m)
                STATE["polls"] = 0
                STATE["rotated"] = None
                STATE["applied"].discard(m)
            set_c.append("slow=" + ("1" if (q.get("slow") or ["0"])[0] == "1" else "0"))
            ttl = (q.get("codettl") or [""])[0]
            set_c.append("codettl=" + (ttl if ttl.isdigit() else "600"))
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for c in set_c:
                self.send_header("Set-Cookie", c + "; Path=/; SameSite=Lax")
            self.end_headers()
            self.wfile.write(body)
            return
        if p.startswith("/ai/i/"):
            return self._send(200, ("# mock：真安装脚本由后端提供（%s）\necho 'mock only'\n" % p).encode(),
                              "text/plain; charset=utf-8")
        return self._send(404, {"error": "not_found", "message": "没有这个地址"})

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/ai/auth/magic":
            if self.headers.get("X-Requested-With") != "ai-portal":
                return self._send(403, {"error": "csrf", "message": "请求来源不对，请刷新页面再试"})
            body = self._body() or {}
            t = str(body.get("t") or "")
            for code, msg in (("expired", "链接过期了"), ("used", "链接已经用过了"), ("invalid", "链接不对")):
                if t.startswith(code):
                    return self._send(400 if code == "invalid" else 410, {"error": code, "message": msg})
            if not t:
                return self._send(400, {"error": "invalid", "message": "链接不对"})
            STATE["polls"] = 0
            return self._send(200, {"ok": True}, headers={"Set-Cookie": "mock=external; Path=/; SameSite=Lax"})
        if not p.startswith("/ai/api/"):
            return self._send(404, {"error": "not_found", "message": "没有这个地址"})
        if self.headers.get("X-Requested-With") != "ai-portal":
            # 和真后端约定的 CSRF 最低要求：自定义头 + JSON body（跨站表单发不出来）
            return self._send(403, {"error": "csrf", "message": "请求来源不对，请刷新页面再试"})
        body = self._body()
        if body is None:
            return self._send(400, {"error": "bad_json", "message": "提交的内容格式不对"})
        return self._api("POST", p, body)

    def _api(self, method, p, body):
        self._slow()
        s = self._scenario()
        if s == "logged_out":
            return self._send(401, {"error": "unauthorized", "message": "请先登录"})
        if p == "/ai/api/me" and method == "GET":
            return self._send(200, self._me(s))
        if p == "/ai/api/keys" and method == "GET":
            if s == "error":
                return self._send(500, {"error": "lark_timeout", "message": "读取 key 列表超时，请稍后再试"})
            if s == "down":
                return self._send(502, b"<html><body><h1>502 Bad Gateway</h1></body></html>",
                                  "text/html; charset=utf-8")
            return self._send(200, {"keys": self._keys(s, poll=True)})
        if p == "/ai/api/keys/reveal" and method == "POST":
            rid = body.get("record_id")
            ks = {k["record_id"]: k for k in self._keys(s)}
            if rid not in ks:
                return self._send(404, {"error": "no_such_key", "message": "这把 key 不是你的，或者已经不存在"})
            if ks[rid]["status"] == "生成中":
                return self._send(409, {"error": "pending", "message": "这把 key 还在生成，稍等一会儿"})
            return self._send(200, {"key": STATE["rotated"] if (rid == "rec_mock_1" and STATE["rotated"]) else FAKE_KEYS[rid]})
        if p == "/ai/api/apply" and method == "POST":
            if [k for k in self._keys(s) if k["current"] and k["status"][:2] in ("可用", "生成")
                    and not k["status"].startswith("生成失败")]:
                return self._send(409, {"error": "already_has_key", "message": "你已经有 key 了，不用重复申请"})
            STATE["applied"].add(s)
            STATE["polls"] = 0
            return self._send(200, {"ok": True, "eta_minutes": 5})
        if p == "/ai/api/install-code" and method == "POST":
            if body.get("tool") not in TOOLS:
                return self._send(400, {"error": "bad_tool", "message": "不认识这个工具"})
            if not [k for k in self._keys(s) if k["status"] == "可用"]:
                return self._send(409, {"error": "no_active_key", "message": "你还没有可用的 key，先申请"})
            alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            ttl = int(self._cookie().get("codettl") or 600)
            return self._send(200, {"code": code[:4] + "-" + code[4:], "expires_in": ttl,
                                    "tool": body.get("tool")})
        if p == "/ai/api/keys/rotate" and method == "POST":
            return self._rotate(s, body)
        if p == "/ai/api/feedback" and method == "POST":
            if not (body.get("title") or "").strip():
                return self._send(400, {"error": "title_required", "message": "一句话说明不能为空"})
            if len(body.get("report") or "") > 200_000:
                return self._send(413, {"error": "too_large", "message": "自检报告太大了（上限 200KB）"})
            STATE["feedback"] += 1
            print("  feedback type=%s tool=%s title_len=%d report_len=%d" % (
                body.get("type"), body.get("tool"), len(body.get("title") or ""), len(body.get("report") or "")))
            return self._send(200, {"ok": True, "id": "FB-%04d" % (1000 + STATE["feedback"])})
        return self._send(404, {"error": "not_found", "message": "没有这个接口"})


def _rotate_impl(h, s, body):
    """形状照真后端 server.py App.rotate:成功 {ok, key, masked, ...};失败 {error, message}。"""
    time.sleep(1.2)                      # 真后端要生成 + 回读 + 试调一次,有几秒
    if s == "rotate_off":
        return h._send(501, {"error": "需要换 key 请提反馈", "message": "需要换 key 请提反馈"})
    if s == "rotate_cool":
        return h._send(429, {"error": "cool", "message": "刚刚才重置过。9 分钟 后才能再重置一次(这样做是为了避免连点生成一堆 key)"})
    if s == "rotate_fail":
        return h._send(502, {"error": "x", "message": "新 key 生成了但没写进表里(飞书 code=1254045)。已经把刚生成的那把撤掉了,你现在的 key 还能继续用,稍后再试"})
    if s == "rotate_dry":
        return h._send(200, {"ok": True, "dry_run": True, "record_id": "rec_mock_1", "old_alias": "cursor-liuguoxian-ab12",
                             "new_alias": "cursor-liuguoxian-q7k2", "carry": ["models", "max_budget", "team_id", "metadata"],
                             "message": "演练模式:没有生成新 key,也没有改任何东西。"})
    new = "sk-mock-" + secrets.token_hex(12)
    STATE["rotated"] = new
    out = {"ok": True, "dry_run": False, "record_id": "rec_mock_1", "key": new, "masked": masked(new),
           "old_alias": "cursor-liuguoxian-ab12", "new_alias": "cursor-liuguoxian-q7k2", "old_blocked": s != "rotate_warn"}
    if s == "rotate_warn":
        out["warning"] = "旧 key 没能立刻停用(我们会处理)。在那之前旧 key 可能还能用,别再把它给别人。"
    return h._send(200, out)


H._rotate = lambda self, s, body: _rotate_impl(self, s, body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8977)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print("mock 在 http://127.0.0.1:%d/ai/?mock=internal  （场景：%s）" % (a.port, " ".join(sorted(SCENARIOS))))
    srv.serve_forever()


if __name__ == "__main__":
    main()
