#!/usr/bin/env python3
"""意见反馈小服务:https://chat.auto-link.com.cn/feedback/api/*

身份:只认 Open WebUI 的登录态。浏览器带着 chat.auto-link.com.cn 的 `token` cookie(httponly,
前端 JS 读不到),本服务原样转给 Open WebUI 的 /api/v1/auths/ 问「这是谁」;
浏览器自己传来的姓名/邮箱一律不信。飞书 open_id 从 Open WebUI 的 user 表里读(只读打开),
它是 openchat 登录时用的同一个飞书应用发的,所以写表格、发通知用同一个应用,身份对得上。

写入:飞书多维表格「AI 反馈」/ 表「反馈」,应用身份(tenant_access_token)。
通知:后台每 60s 扫一次「状态=已解决 且 未通知」的行,给提交人发飞书消息,然后勾上「已通知提交人」。

只用 Python 标准库(198 宿主机 python3.10 直接跑,systemd 托管)。配置全走环境变量,见 feedback.env.example。
"""
import base64, http.cookies, http.server, json, os, re, socketserver, sqlite3, sys, threading, time
import urllib.error, urllib.parse, urllib.request, uuid

CFG = {k: os.environ.get(k, d) for k, d in {
    "LISTEN": "127.0.0.1:8792",
    "OWUI_URL": "http://127.0.0.1:30880",           # Open WebUI(NodePort)
    "OWUI_DB": "",                                  # webui.db 路径(只读)
    "FEISHU_APP_ID": "", "FEISHU_APP_SECRET": "",
    "BASE_TOKEN": "SQ4vbBJhcauRKpsuZrNczNCInxb", "TABLE_ID": "tblkckVodz4HHKZf",
    "TABLE_URL": "https://t83dfrspj4.feishu.cn/wiki/MEzOwVA9WiiNYikmhEFcQAqlnRe?table=tblkckVodz4HHKZf",
    "NOTIFY_INTERVAL": "60",
}.items()}
FS = "https://open.feishu.cn/open-apis"
MAX_IMG = 5 * 1024 * 1024
MAX_IMGS = 3
RATE = {}           # user_id -> [时间戳];每人每分钟最多 3 条
CHOICES = {
    "类型": ["用不了/报错", "想要新功能/新模型", "文档看不懂", "其他建议"],
    "工具": ["Cursor", "Codex", "Claude Code", "openchat 网页", "其他"],
    "电脑": ["Mac", "Windows", "Linux", "不确定"],
    "紧急程度": ["影响工作", "有替代办法", "不急"],
}
KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{8,}")


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


# ── 飞书 ────────────────────────────────────────────────────────────────
_tok = {"v": "", "exp": 0}
_tok_lock = threading.Lock()


def fs_token():
    with _tok_lock:
        if time.time() < _tok["exp"] - 120:
            return _tok["v"]
        r = http_json("POST", FS + "/auth/v3/tenant_access_token/internal",
                      {"app_id": CFG["FEISHU_APP_ID"], "app_secret": CFG["FEISHU_APP_SECRET"]})
        if not r.get("tenant_access_token"):
            raise RuntimeError("飞书取 token 失败:%s" % r.get("msg"))
        _tok.update(v=r["tenant_access_token"], exp=time.time() + int(r.get("expire", 3600)))
        return _tok["v"]


def http_json(method, url, body=None, headers=None, raw=None, ctype="application/json", timeout=30):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": ctype, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            return {"code": e.code, "msg": "HTTP %d" % e.code}


def fs(method, path, body=None, **kw):
    return http_json(method, FS + path, body, {"Authorization": "Bearer " + fs_token()}, **kw)


def table(path=""):
    return "/bitable/v1/apps/%s/tables/%s%s" % (CFG["BASE_TOKEN"], CFG["TABLE_ID"], path)


def upload_image(name, blob):
    bd = uuid.uuid4().hex
    parts = [("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (bd, k, v)).encode()
             for k, v in (("file_name", name), ("parent_type", "bitable_image"),
                          ("parent_node", CFG["BASE_TOKEN"]), ("size", str(len(blob))))]
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n" % (bd, name)).encode() + blob + b"\r\n")
    parts.append(("--%s--\r\n" % bd).encode())
    r = fs("POST", "/drive/v1/medias/upload_all", raw=b"".join(parts), ctype="multipart/form-data; boundary=" + bd)
    if r.get("code") != 0:
        raise RuntimeError("截图上传失败:%s" % r.get("msg"))
    return r["data"]["file_token"]


def text_of(v):
    """多维表格读出来的文本格可能是 str,也可能是 [{"text":..}]。"""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v)
    if isinstance(v, dict):
        return v.get("text") or v.get("value") or ""
    return "" if v is None else str(v)


# ── 身份 ────────────────────────────────────────────────────────────────
def whoami(handler):
    """返回 {id,name,email,open_id} 或 None(没登录)。"""
    c = http.cookies.SimpleCookie(handler.headers.get("Cookie", ""))
    tok = c["token"].value if "token" in c else None
    if not tok:
        return None
    r = urllib.request.Request(CFG["OWUI_URL"] + "/api/v1/auths/", headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(r, timeout=10) as x:
            u = json.load(x)
    except (urllib.error.URLError, ValueError):
        return None
    if not u.get("id"):
        return None
    open_id = None
    try:
        db = sqlite3.connect("file:%s?mode=ro" % CFG["OWUI_DB"], uri=True, timeout=5)
        row = db.execute("select oauth from user where id=?", (u["id"],)).fetchone()
        db.close()
        if row and row[0]:
            open_id = (json.loads(row[0]).get("feishu") or {}).get("sub")
    except sqlite3.Error as e:
        log("读 webui.db 失败:", e)
    return {"id": u["id"], "name": u.get("name") or "", "email": u.get("email") or "", "open_id": open_id}


# ── 业务 ────────────────────────────────────────────────────────────────
def pick(v, field):
    return v if v in CHOICES[field] else None


def create(user, form):
    title = (form.get("title") or "").strip()[:200]
    if not title:
        raise ValueError("「一句话说明」不能空")
    for f in ("类型", "工具"):
        if not pick(form.get(f), f):
            raise ValueError("请选择「%s」" % f)
    # 详细描述里如果粘了 key,打码后再存(自检报告里本来没有,但同事可能顺手粘了配置文件)
    detail = KEY_RE.sub("sk-***", (form.get("detail") or ""))[:20000]
    fields = {
        "一句话说明": KEY_RE.sub("sk-***", title),
        "类型": form["类型"], "工具": form["工具"],
        "详细描述": detail,
        "来源页面": (form.get("source") or "")[:300],
        "提交人邮箱": user["email"],
        "状态": "新提交",
    }
    for f in ("电脑", "紧急程度"):
        if pick(form.get(f), f):
            fields[f] = form[f]
    if user["open_id"]:
        fields["提交人"] = [{"id": user["open_id"]}]
    imgs = form.get("images") or []
    toks = []
    for im in imgs[:MAX_IMGS]:
        blob = base64.b64decode(im.get("data", ""), validate=False)
        if not blob or len(blob) > MAX_IMG:
            raise ValueError("截图要小于 5MB")
        name = re.sub(r"[^\w.\-]", "_", im.get("name") or "screenshot.png")[:80]
        toks.append({"file_token": upload_image(name, blob)})
    if toks:
        fields["截图"] = toks
    r = fs("POST", table("/records?user_id_type=open_id"), {"fields": fields})
    if r.get("code") != 0:
        raise RuntimeError("写入表格失败:%s" % r.get("msg"))
    rec = r["data"]["record"]
    # 编号是自动编号,新建返回里可能还没有 ⇒ 回读一次
    no = text_of(rec.get("fields", {}).get("编号"))
    if not no:
        g = fs("GET", table("/records/%s" % rec["record_id"]))
        no = text_of(((g.get("data") or {}).get("record") or {}).get("fields", {}).get("编号"))
    log("新反馈", no or rec["record_id"], user["email"], fields["类型"], fields["工具"])
    return {"id": rec["record_id"], "no": no}


def mine(user):
    if not user["open_id"]:
        return []
    r = fs("POST", table("/records/search?user_id_type=open_id&page_size=50"), {
        "filter": {"conjunction": "and", "conditions": [
            {"field_name": "提交人", "operator": "contains", "value": [user["open_id"]]}]},
        "sort": [{"field_name": "提交时间", "desc": True}],
        "field_names": ["编号", "一句话说明", "类型", "工具", "状态", "处理说明", "提交时间"],
    })
    out = []
    for it in (r.get("data") or {}).get("items") or []:
        f = it.get("fields", {})
        out.append({"no": text_of(f.get("编号")), "title": text_of(f.get("一句话说明")),
                    "type": text_of(f.get("类型")), "tool": text_of(f.get("工具")),
                    "status": text_of(f.get("状态")) or "新提交",
                    "reply": text_of(f.get("处理说明")), "at": f.get("提交时间")})
    return out


def notify_loop():
    """已解决 且 未通知 ⇒ 私信提交人,然后勾「已通知提交人」。失败下一轮再试。"""
    while True:
        try:
            r = fs("POST", table("/records/search?user_id_type=open_id&page_size=50"), {
                "filter": {"conjunction": "and", "conditions": [
                    {"field_name": "状态", "operator": "is", "value": ["已解决"]},
                    {"field_name": "已通知提交人", "operator": "is", "value": ["false"]}]},
                "field_names": ["编号", "一句话说明", "处理说明", "提交人"],
            })
            for it in (r.get("data") or {}).get("items") or []:
                f = it.get("fields", {})
                who = (f.get("提交人") or [{}])[0].get("id")
                if not who:
                    continue
                no, title, reply = text_of(f.get("编号")), text_of(f.get("一句话说明")), text_of(f.get("处理说明"))
                msg = "你提的问题已解决 ✅\n%s %s\n\n%s\n\n查看我的反馈:https://chat.auto-link.com.cn/feedback/#mine" % (
                    no, title, ("处理说明:" + reply) if reply else "")
                m = fs("POST", "/im/v1/messages?receive_id_type=open_id",
                       {"receive_id": who, "msg_type": "text", "content": json.dumps({"text": msg.strip()})})
                if m.get("code") == 0:
                    fs("PUT", table("/records/%s" % it["record_id"]), {"fields": {"已通知提交人": True}})
                    log("已通知", no, who[:8])
                else:
                    log("通知失败", no, m.get("msg"))
        except Exception as e:  # 后台循环不许挂
            log("通知循环出错:", e)
        time.sleep(int(CFG["NOTIFY_INTERVAL"]))


# ── HTTP ────────────────────────────────────────────────────────────────
class H(http.server.BaseHTTPRequestHandler):
    server_version = "carher-feedback"

    def log_message(self, fmt, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def route(self):
        return urllib.parse.urlparse(self.path).path.rstrip("/").rsplit("/api", 1)[-1] or "/"

    def do_GET(self):
        p = self.route()
        if p == "/healthz":
            return self.send(200, {"ok": True})
        u = whoami(self)
        if p == "/me":
            if not u:
                return self.send(401, {"login": False})
            return self.send(200, {"login": True, "name": u["name"], "email": u["email"], "feishu": bool(u["open_id"]),
                                   "choices": CHOICES})
        if p == "/mine":
            if not u:
                return self.send(401, {"login": False})
            try:
                return self.send(200, {"items": mine(u)})
            except Exception as e:
                log("mine 出错:", e)
                return self.send(502, {"error": "读取失败,稍后再试"})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.route() != "/submit":
            return self.send(404, {"error": "not found"})
        # 同源校验:只接受本站页面发来的提交(防别的网站借用户的登录态代提交)
        origin = self.headers.get("Origin") or ""
        if origin and origin != "https://chat.auto-link.com.cn":
            return self.send(403, {"error": "来源不对"})
        u = whoami(self)
        if not u:
            return self.send(401, {"login": False})
        now = time.time()
        hist = [t for t in RATE.get(u["id"], []) if now - t < 60]
        if len(hist) >= 3:
            return self.send(429, {"error": "提交太频繁,一分钟后再试"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > (MAX_IMG * MAX_IMGS * 4 // 3 + 200_000):
            return self.send(413, {"error": "内容太大,截图要小于 5MB、最多 3 张"})
        try:
            form = json.loads(self.rfile.read(n) or b"{}")
            res = create(u, form)
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:
            log("submit 出错:", e)
            return self.send(502, {"error": "提交失败,稍后再试(内容没丢,可以再点一次)"})
        RATE[u["id"]] = hist + [now]
        self.send(200, {"ok": True, **res})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "OWUI_DB"):
        if not CFG[k]:
            sys.exit("缺环境变量 %s" % k)
    threading.Thread(target=notify_loop, daemon=True).start()
    host, port = CFG["LISTEN"].rsplit(":", 1)
    log("listening", CFG["LISTEN"])
    Server((host, int(port)), H).serve_forever()


if __name__ == "__main__":
    main()
