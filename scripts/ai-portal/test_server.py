"""ai-portal/server.py 的离线回归:本地起一个**假飞书** http 服务,全程不碰线上。

跑法:python3 -m pytest scripts/ai-portal -q

拒绝类测试(DENY)都配了对照,防「桩不完整导致的假绿」:
  - 阳性对照(POSITIVE):同一个请求只改掉攻击那一点 ⇒ 必须成功。证明被拒是因为那道校验,
    而不是路由不存在 / 桩没实现 / 参数本来就错。
  - 放开对照(UNGUARDED):用 monkeypatch 把那道校验关掉 ⇒ 攻击必须得逞。证明这条测试有牙。
"""
import http.client, http.server, importlib.util, json, os, socket, subprocess, sys, threading, time
import urllib.parse
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ai_portal_server", HERE / "server.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

ORIGIN = "https://chat.auto-link.com.cn"
OUR_TENANT = "1088406f1c155740"
APP_ID, APP_SECRET = "cli_aa8505afb0f89cb2", "fake-app-secret-value"   # app id 不是凭据;secret 是假的
INTERNAL = "test-internal-secret-0123456789-ABCDEFGHIJKLMN"
SECRET = "test-session-secret-0123456789-abcdefghijklmnop"
REDIRECT = ORIGIN + "/ai/auth/callback"

USERS = {
    "A": {"union_id": "on_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "open_id": "ou_aaaa", "tenant_key": OUR_TENANT,
          "name": "张三", "email": "zs@auto-link.com.cn", "avatar_url": "https://img/a"},
    "B": {"union_id": "on_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "open_id": "ou_bbbb", "tenant_key": OUR_TENANT,
          "name": "李四", "email": "ls@auto-link.com.cn", "avatar_url": "https://img/b"},
    # 外部人:租户不同、没有 email 字段
    "X": {"union_id": "on_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "open_id": "ou_xxxx", "tenant_key": "2a0b_other_tenant",
          "name": "外部王五", "avatar_url": "https://img/x"},
    # 新人:表里没有任何行
    "N": {"union_id": "on_nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn", "open_id": "ou_nnnn", "tenant_key": OUR_TENANT,
          "name": "新人", "email": "new@auto-link.com.cn", "avatar_url": ""},
}
KEY_A1 = "sk-AAAAaaaa1111cursorA0001"
KEY_A2 = "sk-AAAAaaaa2222claudeA0002"
KEY_B = "sk-BBBBbbbb3333cursorB0003"
KEY_X = "sk-XXXXxxxx4444cursorX0004"


def person(u):
    return [{"id": USERS[u]["union_id"], "name": USERS[u]["name"]}]


DAY, MIN = 86_400_000, 60_000
T0 = 1_790_000_000_000        # 申请时间基准(毫秒)


def row(who, key, status, at, typ="Cursor", org="", **extra):
    f = {"姓名": person(who), "API Key": key, "账户类型": [typ], "状态": status, "申请时间": at}
    if org:
        f["组织名称"] = org
    f.update(extra)
    return f


def seed():
    d = {
        # A:current = 状态「有效」开头 + Cursor + sk- 里申请时间最新的 recA1;明文还散在体检链接/Cursor Key 里
        "recA1": row("A", [{"type": "text", "text": KEY_A1}], "有效", T0 + 3 * DAY,
                     体检链接={"link": "https://chat.auto-link.com.cn/keyprobe/#k=" + KEY_A1, "text": "体检"},
                     点我体检="https://chat.auto-link.com.cn/keyprobe/#k=" + KEY_A1, **{"Cursor Key": KEY_A1}),
        "recA2": row("A", KEY_A2, "有效（已重新生成）", T0 + 1 * DAY, **{"Claude Code Key": KEY_A2}),
        "recA3": row("A", "sk-AAAAold9999disabled", "已禁用", T0 + 5 * DAY),
        "recA4": row("A", "__provisioning__", "", T0 + 6 * DAY),
        "recA5": row("A", "", "生成失败: 网关创建 key 超时 sk-LEAKinREASON1234", T0 + 7 * DAY),
        "recA6": row("A", "sk-AAAAdone8888finished", "已完成", T0 + 2 * DAY),
        # A 名下的客户 key:组织名称命中 EXCLUDE_ORGS
        "recQ1": row("A", "sk-QIAOHUqiaohu00001111", "有效", T0 + 9 * DAY, org="巧虎"),
        "recB1": row("B", KEY_B, "有效", T0 + DAY),
        "recX1": row("X", KEY_X, "有效", T0 + DAY),
        "recAB": row("A", "sk-SHAREDsharedshared77", "有效", T0 + 4 * DAY),
    }
    d["recAB"]["姓名"] = person("A") + person("B")
    # A 名下连续批量生成的 6 行(组织名不在名单里,间隔 1 秒)⇒ 按「连续 ≥5 行」排除
    for i in range(6):
        d["recBT%d" % i] = row("A", "sk-BATCHbatch%04dxxxx" % i, "有效", T0 + 10 * DAY + i * 1000, org="某新客户")
    return d


# ── 假飞书 ──────────────────────────────────────────────────────────────
class Fake:
    def reset(self):
        self.calls = []            # (method, path)
        self.tables = {}           # (app, table) -> {rid: fields}
        self.codes = {}            # code -> user key
        self.uats = {}             # user_access_token -> user key
        self.tenant_tokens = set()
        self.tenant_issued = 0
        self.leaky = False         # True = search 忽略过滤条件,把整张表回出去(模拟过滤器失效)
        self.userinfo_flat = False
        self.fail_create_person = False
        self.n = 0
        self.search_error = None   # 非 None ⇒ search 直接回这个错误码(模拟权限/限流等真失败)


FAKE = Fake()
FAKE.reset()


class FakeFeishu(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, obj, loc=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        if loc:
            self.send_header("Location", loc)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def bearer(self):
        return (self.headers.get("Authorization") or "").removeprefix("Bearer ")

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        FAKE.calls.append(("GET", u.path))
        if u.path == "/authorize":
            # 模拟「用户在飞书授权页点了同意」:测试用 as= 指定是谁
            if q.get("client_id") != APP_ID or q.get("redirect_uri") != REDIRECT:
                return self.out(400, {"code": 20029, "msg": "redirect_uri 不在白名单"})
            if q.get("as") == "deny":
                return self.out(302, {}, q["redirect_uri"] + "?" + urllib.parse.urlencode(
                    {"error": "access_denied", "state": q["state"]}))
            FAKE.n += 1
            code = "fake-code-%04d-%s" % (FAKE.n, q["as"])
            FAKE.codes[code] = q["as"]
            return self.out(302, {}, q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]}))
        if u.path == "/open-apis/authen/v1/user_info":
            who = FAKE.uats.get(self.bearer())
            if not who:
                return self.out(401, {"code": 99991668, "msg": "invalid access token"})
            d = dict(USERS[who])
            return self.out(200, d if FAKE.userinfo_flat else {"code": 0, "msg": "success", "data": d})
        if u.path.startswith("/open-apis/contact/"):
            # 跨租户用户查通讯录必 41050;门户不该调这里
            return self.out(400, {"code": 41050, "msg": "no user authority error"})
        self.out(404, {"code": 404, "msg": "fake: no route " + u.path})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        b = self.body()
        FAKE.calls.append(("POST", u.path))
        if u.path == "/open-apis/authen/v2/oauth/token":
            who = FAKE.codes.pop(b.get("code"), None)        # 授权码一次性
            if (b.get("client_id"), b.get("client_secret")) != (APP_ID, APP_SECRET):
                return self.out(400, {"code": 20002, "error": "invalid_client"})
            if not who or b.get("redirect_uri") != REDIRECT or b.get("grant_type") != "authorization_code":
                return self.out(400, {"code": 20003, "error": "invalid_grant"})
            FAKE.n += 1
            tok = "u-fake-uat-%04d" % FAKE.n
            FAKE.uats[tok] = who
            return self.out(200, {"code": 0, "access_token": tok, "expires_in": 7200, "token_type": "Bearer"})
        if u.path == "/open-apis/auth/v3/tenant_access_token/internal":
            if (b.get("app_id"), b.get("app_secret")) != (APP_ID, APP_SECRET):
                return self.out(400, {"code": 10014, "msg": "app secret invalid"})
            FAKE.tenant_issued += 1
            tok = "t-fake-%04d" % FAKE.tenant_issued
            FAKE.tenant_tokens.add(tok)
            return self.out(200, {"code": 0, "tenant_access_token": tok, "expire": 7200})
        if u.path.startswith("/open-apis/bitable/v1/apps/"):
            if self.bearer() not in FAKE.tenant_tokens:
                return self.out(400, {"code": 99991663, "msg": "Invalid access token for authorization"})
            parts = u.path.split("/")      # ['', open-apis, bitable, v1, apps, APP, tables, TBL, records, (search)]
            tbl = FAKE.tables.setdefault((parts[5], parts[7]), {})
            if u.path.endswith("/records/search"):
                return self.search(tbl, q, b)
            if u.path.endswith("/records"):
                f = b["fields"]
                if FAKE.fail_create_person and "提交人" in f:
                    return self.out(400, {"code": 1254066, "msg": "UserFieldConvFail"})
                FAKE.n += 1
                rid = "recNEW%04d" % FAKE.n
                tbl[rid] = f
                return self.out(200, {"code": 0, "data": {"record": {"record_id": rid, "fields": f}}})
        self.out(404, {"code": 404, "msg": "fake: no route " + u.path})

    def search(self, tbl, q, b):
        id_type = q.get("user_id_type", "open_id")
        conds = (b.get("filter") or {}).get("conditions") or []
        if FAKE.search_error is not None:
            return self.out(400, {"code": FAKE.search_error, "msg": "fake error"})
        # 真飞书:人员格 is 一个对不上 user_id_type 形态的值(假 id/截断 id/类型不匹配)⇒ 1254018 InvalidFilter
        for c in conds:
            if c["field_name"] == "姓名" and id_type == "union_id" and not all(
                    isinstance(v, str) and v.startswith("on_") and len(v) == 35 for v in c["value"]):
                return self.out(400, {"code": 1254018, "msg": "InvalidFilter"})
        items = []
        for rid, f in tbl.items():
            ok = True
            for c in conds:
                if FAKE.leaky:
                    break
                cell = f.get(c["field_name"])
                if not isinstance(cell, list) or not cell or not isinstance(cell[0], dict):
                    ok = False
                    continue
                # 人员格:按 user_id_type 呈现 id;不是 union_id 的话 on_ 值永远对不上
                # 真飞书对「拿不到 union_id 的成员」会回什么形状没实测过 ⇒ 桩要容得下
                # 缺 id / id 为空 / 非 dict 的成员,否则这类输入永远测不到(红队 11-04 指出)
                ids = [i if id_type == "union_id" else i.replace("on_", "ou_")
                       for i in ((x.get("id") or "") if isinstance(x, dict) else "" for x in cell) if i]
                if c["operator"] == "is":
                    ok &= len(ids) == 1 and ids[0] in c["value"]   # 多人格 is 单人 ⇒ 不命中
                elif c["operator"] == "contains":
                    ok &= any(i in c["value"] for i in ids)
                else:
                    ok = False
            if ok:
                items.append({"record_id": rid, "fields": f})
        size = min(int(q.get("page_size", 20)), 2)        # 故意 2 条一页,逼出分页
        start = int(q.get("page_token") or 0)
        page = items[start:start + size]
        more = start + size < len(items)
        return self.out(200, {"code": 0, "data": {"items": page, "has_more": more,
                                                  "page_token": str(start + size) if more else "",
                                                  "total": len(items)}})


@pytest.fixture(scope="module")
def fake():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeFeishu)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % s.server_port
    s.shutdown()


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def make_cfg(fake_url, tmp_path, **over):
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("<!doctype html><title>AI 自助</title>PORTAL-INDEX", encoding="utf-8")
    (static / "app.js").write_text("console.log(1)", encoding="utf-8")
    cfg = S.load_config(environ={})
    cfg.update(FEISHU_APP_ID=APP_ID, FEISHU_APP_SECRET=APP_SECRET, SESSION_SECRET=SECRET,
               FEISHU_OPEN_BASE=fake_url + "/open-apis", FEISHU_AUTHORIZE_URL=fake_url + "/authorize",
               DB_PATH=str(tmp_path / "portal.db"), STATIC_DIR=str(static), TRUST_PROXY="1")
    cfg.update(over)
    return cfg


class Portal:
    """一个跑起来的门户 + 一个不跟随跳转、手工管 cookie 的客户端(cookie 带 Secure,http 下 cookiejar 不会回传)。"""

    def __init__(self, fake_url, cfg, clock):
        self.fake_url, self.clock = fake_url, clock
        self.app = S.App(cfg, now=clock)
        self.srv = S.make_server(self.app)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def req(self, method, path, body=None, cookie=None, origin=ORIGIN, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        if cookie:
            h["Cookie"] = cookie
        if origin and method == "POST":
            h["Origin"] = origin
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        b = r.read()
        try:
            j = json.loads(b)
        except ValueError:
            j = b.decode("utf-8", "replace")
        out = (r.status, j, r.getheader("Location"), r.headers.get_all("Set-Cookie") or [])
        c.close()
        return out

    def login(self, who, nxt=None):
        """走完整流程,返回 (callback 的 status, Location, sid cookie 串)。"""
        path = "/ai/auth/login" + ("?" + urllib.parse.urlencode({"next": nxt}) if nxt is not None else "")
        st, _, loc, sc = self.req("GET", path)
        assert st == 302 and loc.startswith(self.fake_url + "/authorize?")
        state_cookie = cookie_of(sc, "ai_oauth")
        st2, _, loc2, _ = fake_get(loc + "&as=" + who)
        assert st2 == 302 and loc2.startswith(REDIRECT + "?")
        cb = loc2[len(ORIGIN):]
        st3, body3, loc3, sc3 = self.req("GET", cb, cookie="ai_oauth=" + state_cookie)
        return st3, loc3, ("ai_sid=" + cookie_of(sc3, "ai_sid")) if cookie_of(sc3, "ai_sid") else None, body3

    def sid(self, who):
        st, _, sid, _ = self.login(who)
        assert st == 302 and sid
        return sid

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def cookie_of(set_cookies, name):
    for c in set_cookies:
        k, _, rest = c.partition("=")
        if k == name:
            return rest.split(";", 1)[0]
    return None


def fake_get(url):
    u = urllib.parse.urlsplit(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    c.request("GET", u.path + "?" + u.query)
    r = c.getresponse()
    b = r.read()
    c.close()
    return r.status, b, r.getheader("Location"), None


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def portal(fake, tmp_path, clock):
    FAKE.reset()
    FAKE.tables[("DlT9bsrwMad12VsogEpcK9Ptncc", "tblJT2s6Y6xjYj5A")] = seed()
    p = Portal(fake, make_cfg(fake, tmp_path), clock)
    yield p
    p.close()


@pytest.fixture
def portal_factory(fake, tmp_path, clock):
    made = []

    def mk(**over):
        FAKE.reset()
        FAKE.tables[("DlT9bsrwMad12VsogEpcK9Ptncc", "tblJT2s6Y6xjYj5A")] = seed()
        p = Portal(fake, make_cfg(fake, tmp_path, **over), clock)
        made.append(p)
        return p
    yield mk
    for p in made:
        p.close()


KEYTBL = ("DlT9bsrwMad12VsogEpcK9Ptncc", "tblJT2s6Y6xjYj5A")


# ════════════════════════════════════════════════════════════════════════
# 1. 登录全流程
# ════════════════════════════════════════════════════════════════════════
def test_login_full_flow_internal(portal):
    st, loc, sid, _ = portal.login("A", nxt="/ai/#keys")
    assert st == 302 and loc == "/ai/#keys" and sid
    # 授权页参数
    _, _, auth, sc = portal.req("GET", "/ai/auth/login")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(auth).query))
    assert q["client_id"] == APP_ID and q["redirect_uri"] == REDIRECT and len(q["state"]) >= 40
    c = [x for x in sc if x.startswith("ai_oauth=")][0]
    assert "HttpOnly" in c and "Secure" in c and "SameSite=Lax" in c and "Path=/ai/auth/" in c
    # me
    s, d, _, _ = portal.req("GET", "/ai/api/me", cookie=sid)
    assert s == 200 and d["union_id"] == USERS["A"]["union_id"] and d["external"] is False
    assert d["email"] == "zs@auto-link.com.cn" and d["name"] == "张三" and d["avatar"] == "https://img/a"
    # 身份全来自 user_info,从没碰通讯录
    assert not [c for c in FAKE.calls if "/contact/" in c[1]]


def test_session_cookie_flags(portal):
    _, _, loc, sc = portal.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc + "&as=A")
    _, _, _, sc3 = portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc, "ai_oauth"))
    c = [x for x in sc3 if x.startswith("ai_sid=")][0]
    assert all(f in c for f in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/ai/"))
    assert any(x.startswith("ai_oauth=;") and "Max-Age=0" in x for x in sc3), "state cookie 用完要清掉"


def test_userinfo_flat_shape_also_accepted(portal):
    """OWUI 实测 user_info 是平铺的;文档是 data 包装。两种都要认。"""
    FAKE.userinfo_flat = True
    st, _, sid, _ = portal.login("A")
    assert st == 302 and sid
    assert portal.req("GET", "/ai/api/me", cookie=sid)[1]["union_id"] == USERS["A"]["union_id"]


def test_logout_kills_session_server_side(portal):
    sid = portal.sid("A")
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 200
    assert portal.req("POST", "/ai/auth/logout", {}, cookie=sid)[0] == 200
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 401, "退出后旧 cookie 必须作废(服务端删行)"


def test_session_expires(portal, clock):
    sid = portal.sid("A")
    clock.t += 43200 + 1
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 401
    clock.t -= 2          # POSITIVE: 没过期时同一个 cookie 能用
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 200


def test_user_denied_on_feishu(portal):
    _, _, loc, sc = portal.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc + "&as=deny")
    st, body, _, sc3 = portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc, "ai_oauth"))
    assert st == 400 and not cookie_of(sc3, "ai_sid")


def test_code_cannot_be_replayed(portal):
    """同一个 callback URL 再打一次(state 已用 + code 已用)⇒ 不发会话。"""
    _, _, loc, sc = portal.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc + "&as=A")
    ck = "ai_oauth=" + cookie_of(sc, "ai_oauth")
    assert portal.req("GET", loc2[len(ORIGIN):], cookie=ck)[0] == 302
    st, _, _, sc2 = portal.req("GET", loc2[len(ORIGIN):], cookie=ck)
    assert st == 400 and not cookie_of(sc2, "ai_sid")


# ════════════════════════════════════════════════════════════════════════
# 2. 外部用户
# ════════════════════════════════════════════════════════════════════════
def test_external_user_without_email_logs_in_and_sees_own_keys(portal):
    assert "email" not in USERS["X"]
    st, _, sid, _ = portal.login("X")
    assert st == 302 and sid
    s, d, _, _ = portal.req("GET", "/ai/api/me", cookie=sid)
    assert s == 200 and d["external"] is True and d["email"] == "" and d["tenant_key"] != OUR_TENANT
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sid)
    assert s == 200 and [x["record_id"] for x in d["keys"]] == ["recX1"]
    s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recX1"}, cookie=sid)
    assert s == 200 and d["key"] == KEY_X
    assert not [c for c in FAKE.calls if "/contact/" in c[1]], "外部人查通讯录必 41050,门户不许调"


# ════════════════════════════════════════════════════════════════════════
# 3. 未登录 401
# ════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("method,path,body", [
    ("GET", "/ai/api/me", None), ("GET", "/ai/api/keys", None),
    ("POST", "/ai/api/keys/reveal", {"record_id": "recA1"}), ("POST", "/ai/api/apply", {"account_type": "Cursor"}),
    ("POST", "/ai/api/install-code", {"tool": "cursor"}),
    ("POST", "/ai/api/feedback", {"title": "x", "类型": "其他建议", "工具": "其他"}),
])
def test_unauthenticated_is_401(portal, method, path, body):
    sid = portal.sid("A")
    for bad in (None, "ai_sid=", "ai_sid=forged.sig", sid + "x", sid.split(".")[0] + ".deadbeef"):
        assert portal.req(method, path, body, cookie=bad)[0] == 401, bad
    # POSITIVE:同一请求带真 cookie 不是 401(证明 401 来自鉴权,不是路由不存在)
    assert portal.req(method, path, body, cookie=sid)[0] != 401


def test_cookie_from_other_secret_rejected(fake, tmp_path, clock):
    """另一个 SESSION_SECRET 签的 cookie(比如别的环境)不能用。"""
    FAKE.reset()
    FAKE.tables[KEYTBL] = seed()
    p1 = Portal(fake, make_cfg(fake, tmp_path), clock)
    sid = p1.sid("A")
    p1.close()
    p2 = Portal(fake, make_cfg(fake, tmp_path, SESSION_SECRET="another-secret-0123456789-abcdefghijklmnop"), clock)
    try:
        assert p2.req("GET", "/ai/api/me", cookie=sid)[0] == 401
    finally:
        p2.close()
    p3 = Portal(fake, make_cfg(fake, tmp_path), clock)   # POSITIVE:同库同密钥 ⇒ 认
    try:
        assert p3.req("GET", "/ai/api/me", cookie=sid)[0] == 200
    finally:
        p3.close()


# ════════════════════════════════════════════════════════════════════════
# 4. key 列表 / 打码 / reveal 越权
# ════════════════════════════════════════════════════════════════════════
A_OWN = ["recA1", "recA2", "recA3", "recA4", "recA5", "recA6"]


def test_keys_are_masked_and_exact(portal):
    sid = portal.sid("A")
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sid)
    ids = sorted(x["record_id"] for x in d["keys"])
    assert s == 200 and ids == A_OWN, "精确:不含 B 的行、A+B 多人格行、客户批量行"
    raw = json.dumps(d, ensure_ascii=False)
    assert "sk-" in raw and KEY_A1 not in raw and KEY_A2 not in raw and "AAAAaaaa" not in raw
    assert "LEAKinREASON" not in raw, "生成失败原因里夹的 key 也要打码"
    m = {x["record_id"]: x for x in d["keys"]}
    assert m["recA1"]["masked"] == "sk-…0001" and m["recA1"]["account_type"] == "Cursor"
    assert m["recA1"]["account_types"] == ["Cursor"]
    # 过滤用的是 union_id
    assert ("POST", "/open-apis/bitable/v1/apps/DlT9bsrwMad12VsogEpcK9Ptncc/tables/tblJT2s6Y6xjYj5A/records/search") \
        in FAKE.calls


def test_keys_follow_pagination(portal):
    """假飞书 2 条一页;A 名下 13 行(含排除的)⇒ 必须翻 7 页才拿得全。"""
    sid = portal.sid("A")
    FAKE.calls.clear()
    assert len(portal.req("GET", "/ai/api/keys", cookie=sid)[1]["keys"]) == 6
    assert len([c for c in FAKE.calls if c[1].endswith("/records/search")]) == 7


def test_a_cannot_see_b_keys(portal):
    sa, sb = portal.sid("A"), portal.sid("B")
    da = portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]
    assert "recB1" not in json.dumps(da) and "3333" not in json.dumps(da)
    # POSITIVE:B 自己能看见 recB1(证明 recB1 真在表里、不是因为数据不存在才看不到)
    db = portal.req("GET", "/ai/api/keys", cookie=sb)[1]["keys"]
    assert [x["record_id"] for x in db] == ["recB1"]


def test_a_cannot_see_b_keys_even_if_filter_leaks(portal, monkeypatch):
    """过滤器失效(假飞书把全表回出来)时,服务端的归属复核仍把别人的行挡掉。"""
    sa = portal.sid("A")
    FAKE.leaky = True
    d = portal.req("GET", "/ai/api/keys", cookie=sa)[1]
    assert sorted(x["record_id"] for x in d["keys"]) == A_OWN
    # UNGUARDED:把归属复核关掉 ⇒ 同一请求就会看到 B 的行(证明是 owner_ok 在挡,不是桩碰巧没回)
    monkeypatch.setattr(S, "owner_ok", lambda cell, uid: True)
    d2 = portal.req("GET", "/ai/api/keys", cookie=sa)[1]
    assert "recB1" in [x["record_id"] for x in d2["keys"]]


def test_reveal_own_ok_other_denied(portal):
    sa = portal.sid("A")
    s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recA1"}, cookie=sa)
    assert s == 200 and d["key"] == KEY_A1                                  # POSITIVE
    for rid in ("recB1", "recX1", "recAB", "recNOPE"):
        s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": rid}, cookie=sa)
        assert s == 404 and "sk-" not in json.dumps(d), rid
    # 别人的行 和 不存在的行 回答完全一样(不泄露存在性)
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=sa)[1] == \
        portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recNOPE"}, cookie=sa)[1]
    # POSITIVE:B 自己 reveal recB1 能成功(证明 recB1 可被 reveal,A 被拒是因为归属)
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=portal.sid("B"))[1]["key"] == KEY_B


def test_reveal_other_denied_even_if_filter_leaks(portal, monkeypatch):
    sa = portal.sid("A")
    FAKE.leaky = True
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=sa)[0] == 404
    # UNGUARDED:关掉归属复核 ⇒ A 就能拿到 B 的明文 key。这条变红说明上面那条是真在挡。
    monkeypatch.setattr(S, "owner_ok", lambda cell, uid: True)
    s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=sa)
    assert s == 200 and d["key"] == KEY_B


def test_reveal_bad_record_id(portal):
    sa = portal.sid("A")
    for bad in (None, 123, "", "../x", "recA1' or 1=1", ["recA1"]):
        assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": bad}, cookie=sa)[0] == 400


def test_mask_key():
    assert S.mask_key("sk-abcdefghijklmnop1234") == "sk-…1234"
    assert S.mask_key("sk-short") == "sk-…"            # 太短不露尾
    assert S.mask_key("") == "" and S.mask_key(None) == ""
    assert S.mask_key("  sk-abcdefghijkWXYZ \n") == "sk-…WXYZ"
    assert S.mask_key("pk-abcdefghijk9876") == "…9876"
    for k in ("sk-abcdefghijklmnop1234", "sk-short"):
        assert "abcdefgh" not in S.mask_key(k)


def test_owner_ok():
    u = "on_1"
    assert S.owner_ok([{"id": u}], u)
    assert not S.owner_ok([{"id": u}, {"id": "on_2"}], u), "多人格不算任何人的"
    assert not S.owner_ok([{"id": "on_2"}], u)
    assert not S.owner_ok([], u) and not S.owner_ok(None, u) and not S.owner_ok("on_1", u)
    assert not S.owner_ok([{"id": ""}], "") and not S.owner_ok([{}], None)


def test_feishu_tenant_token_expired_is_refreshed(portal):
    sa = portal.sid("A")
    portal.req("GET", "/ai/api/keys", cookie=sa)
    issued = FAKE.tenant_issued
    FAKE.tenant_tokens.clear()                           # 飞书侧把 token 作废
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sa)
    assert s == 200 and len(d["keys"]) == 6 and FAKE.tenant_issued == issued + 1


def test_feishu_down_is_502_not_leak(portal, monkeypatch):
    sa = portal.sid("A")
    monkeypatch.setattr(portal.app.client, "base", "http://127.0.0.1:1/open-apis")
    monkeypatch.setattr(portal.app.client, "_tok", "")
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sa)
    assert s == 502 and "sk-" not in json.dumps(d)


# ════════════════════════════════════════════════════════════════════════
# 5. OAuth state / 开放重定向 / Origin
# ════════════════════════════════════════════════════════════════════════
def _get_callback(portal, who="A"):
    _, _, loc, sc = portal.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc + "&as=" + who)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc2).query))
    return q["code"], q["state"], cookie_of(sc, "ai_oauth")


def _cb(portal, code, state, ck):
    st, _, loc, sc = portal.req("GET", "/ai/auth/callback?" + urllib.parse.urlencode({"code": code, "state": state}),
                                cookie=("ai_oauth=" + ck) if ck else None)
    return st, cookie_of(sc, "ai_sid")


def test_state_mismatch_rejected(portal):
    code, state, ck = _get_callback(portal)
    _, state2, ck2 = _get_callback(portal)
    # 1) 浏览器 cookie 和 URL 里的 state 对不上(登录 CSRF:攻击者把自己的 callback 塞给受害者)
    assert _cb(portal, code, state2, ck) == (400, None)
    # 2) 没有 cookie
    assert _cb(portal, code, state, None) == (400, None)
    # 3) 服务端没签发过的 state(cookie 和 URL 一致但是伪造的)
    assert _cb(portal, code, "forged" * 8, "forged" * 8) == (400, None)
    # POSITIVE:同一个 code + 配对的 state/cookie ⇒ 成功(证明 code 本身有效,上面被拒是 state 造成的)
    st, sid = _cb(portal, code, state, ck)
    assert st == 302 and sid


def test_state_mismatch_unguarded_control(portal, monkeypatch):
    """UNGUARDED:把 state 比较改成恒真 ⇒ 不配对的 state 就能登录。证明上一条测试真在测 cookie 比对。"""
    code, state, ck = _get_callback(portal)
    _, state2, _ = _get_callback(portal)
    monkeypatch.setattr(S.hmac, "compare_digest", lambda a, b: True)
    st, sid = _cb(portal, code, state2, state2)     # cookie=URL=别人那次的 state(服务端有且未用)
    assert st == 302 and sid


def test_state_expires(portal, clock):
    code, state, ck = _get_callback(portal)
    clock.t += 601
    assert _cb(portal, code, state, ck) == (400, None)


@pytest.mark.parametrize("nxt", [
    "https://evil.example/", "//evil.example/ai/", "/\\evil.example", "\\\\evil.example", "http:/evil.example",
    "/ai/../admin", "/ai/%2e%2e/admin", "/other/", "javascript:alert(1)", "/ai/\r\nSet-Cookie:x=1", "ai/x",
    "https://chat.auto-link.com.cn.evil.example/ai/",
])
def test_open_redirect_rejected(portal, nxt):
    st, loc, sid, _ = portal.login("A", nxt=nxt)
    assert st == 302 and sid and loc == "/ai/", nxt


def test_open_redirect_positive_and_unguarded(portal, monkeypatch):
    # POSITIVE:本站相对路径原样保留
    assert portal.login("A", nxt="/ai/keys?tab=1#x")[1] == "/ai/keys?tab=1#x"
    # UNGUARDED:放开 safe_next ⇒ 外站地址就会被原样跳过去
    monkeypatch.setattr(S, "safe_next", lambda n, base="/ai": n)
    assert portal.login("A", nxt="https://evil.example/")[1] == "https://evil.example/"


@pytest.mark.parametrize("path,body", [
    ("/ai/api/keys/reveal", {"record_id": "recA1"}), ("/ai/api/apply", {"account_type": "Cursor"}),
    ("/ai/api/install-code", {"tool": "cursor"}), ("/ai/auth/logout", {}),
    ("/ai/api/feedback", {"title": "t", "类型": "其他建议", "工具": "其他"}),
])
def test_write_endpoints_check_origin(portal, path, body):
    sid = portal.sid("A")
    for o in ("https://evil.example", "null", "https://chat.auto-link.com.cn.evil.example", "http://chat.auto-link.com.cn"):
        assert portal.req("POST", path, body, cookie=sid, origin=o)[0] == 403, o
    # 没 Origin 也没 Referer ⇒ 拒;只有本站 Referer ⇒ 放
    assert portal.req("POST", path, body, cookie=sid, origin=None)[0] == 403
    assert portal.req("POST", path, body, cookie=sid, origin=None,
                      headers={"Referer": "https://evil.example/ai/"})[0] == 403
    # POSITIVE:同一请求换成本站 Origin 就不是 403
    assert portal.req("POST", path, body, cookie=sid)[0] != 403


def test_post_requires_json_content_type(portal):
    """text/plain 的跨站「简单请求」不触发预检 ⇒ 只收 application/json 再加一层。"""
    sid = portal.sid("A")
    s = portal.req("POST", "/ai/api/keys/reveal", cookie=sid, raw=b'{"record_id":"recA1"}',
                   headers={"Content-Type": "text/plain"})[0]
    assert s == 415


# ════════════════════════════════════════════════════════════════════════
# 6. 申请:幂等 / dry-run
# ════════════════════════════════════════════════════════════════════════
def test_apply_dry_run_does_not_write(portal):
    sn = portal.sid("N")
    before = json.dumps(FAKE.tables[KEYTBL], sort_keys=True)
    FAKE.calls.clear()
    s, d, _, _ = portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sn)
    assert s == 200 and d["dry_run"] is True
    assert d["fields"] == {"姓名": [{"id": USERS["N"]["union_id"]}], "账户类型": ["Cursor"]}
    assert json.dumps(FAKE.tables[KEYTBL], sort_keys=True) == before
    assert not [c for c in FAKE.calls if c[1].endswith("/records")], "dry-run 不许调 create"
    # 写之前先精确查过一遍
    assert [c for c in FAKE.calls if c[1].endswith("/records/search")]


def test_apply_dry_run_is_default():
    assert S.DEFAULTS["APPLY_DRY_RUN"] == "1"


def test_apply_writes_exact_shape_when_enabled(portal_factory):
    p = portal_factory(APPLY_DRY_RUN="0")
    sn = p.sid("N")
    FAKE.calls.clear()
    s, d, _, _ = p.req("POST", "/ai/api/apply", {}, cookie=sn)            # account_type 省略 ⇒ Cursor
    assert s == 200 and d["dry_run"] is False and d["record_id"].startswith("recNEW")
    assert FAKE.tables[KEYTBL][d["record_id"]] == {"姓名": [{"id": USERS["N"]["union_id"]}], "账户类型": ["Cursor"]}
    creates = [c for c in FAKE.calls if c[1].endswith("/records")]
    assert len(creates) == 1
    forbidden = {"API Key", "open_id", "邮箱前缀", "状态", "key_alias"}
    assert not forbidden & set(FAKE.tables[KEYTBL][d["record_id"]])


def test_apply_org_name_optional(portal_factory):
    p = portal_factory(APPLY_DRY_RUN="0", APPLY_ORG_NAME="AI Infra部")
    d = p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=p.sid("N"))[1]
    assert FAKE.tables[KEYTBL][d["record_id"]]["组织名称"] == "AI Infra部"


def test_apply_uses_union_id_type(portal_factory, monkeypatch):
    p = portal_factory(APPLY_DRY_RUN="0")
    seen = []
    real = p.app.client._http
    monkeypatch.setattr(p.app.client, "_http", lambda m, u, b=None, token=None: (seen.append(u), real(m, u, b, token))[1])
    p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=p.sid("N"))
    assert [u for u in seen if u.endswith("/records?user_id_type=union_id")]


def test_apply_returns_existing_current_without_writing(portal_factory, monkeypatch):
    p = portal_factory(APPLY_DRY_RUN="0")
    sa = p.sid("A")
    n = len(FAKE.tables[KEYTBL])
    s, d, _, _ = p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sa)
    assert s == 200 and d["existing"] is True and d["record_id"] == "recA1" and len(FAKE.tables[KEYTBL]) == n
    # UNGUARDED:让「先精确查一遍」返回空 ⇒ 就会重复写一行
    monkeypatch.setattr(p.app, "my_rows", lambda u: [])
    assert p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sa)[1]["dry_run"] is False
    assert len(FAKE.tables[KEYTBL]) == n + 1


def test_apply_when_only_disabled_rows_writes(portal_factory):
    """只有已禁用的行(没有 current)⇒ 允许申请新的。"""
    p = portal_factory(APPLY_DRY_RUN="0")
    FAKE.tables[KEYTBL]["recB1"]["状态"] = "已禁用"
    d = p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=p.sid("B"))[1]
    assert d.get("record_id", "").startswith("recNEW")


def test_apply_pending_row_not_duplicated(portal_factory):
    p = portal_factory(APPLY_DRY_RUN="0")
    FAKE.tables[KEYTBL]["recB1"].update({"状态": "", "API Key": "__provisioning__"})
    n = len(FAKE.tables[KEYTBL])
    d = p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=p.sid("B"))[1]
    assert d["pending"] is True and len(FAKE.tables[KEYTBL]) == n


def test_apply_double_click_while_table_lags(portal_factory, monkeypatch):
    """表格搜索有延迟:第一次写的行还没被搜到时连点第二下 ⇒ 本地锁兜住。"""
    p = portal_factory(APPLY_DRY_RUN="0")
    sn = p.sid("N")
    real = p.app.my_rows
    monkeypatch.setattr(p.app, "my_rows", lambda u: [r for r in real(u) if not r["record_id"].startswith("recNEW")])
    assert p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sn)[1]["dry_run"] is False
    assert p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sn)[1]["pending"] is True
    assert len([r for r in FAKE.tables[KEYTBL] if r.startswith("recNEW")]) == 1


def test_apply_bad_type(portal):
    sn = portal.sid("N")
    for bad in ("", "Admin", None, ["Cursor"], "Claude Code"):
        assert portal.req("POST", "/ai/api/apply", {"account_type": bad}, cookie=sn)[0] == 400, bad


# ════════════════════════════════════════════════════════════════════════
# 7. 一次性安装码
# ════════════════════════════════════════════════════════════════════════
def _redeem(p, code, ip="10.0.0.1", origin=None):
    s, d, _, _ = p.req("POST", "/ai/api/install-code/redeem", {"code": code}, origin=origin,
                       headers={"X-Real-IP": ip})
    return s, d


def test_install_code_happy_path_no_login_needed(portal):
    sa = portal.sid("A")
    s, d, _, _ = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)
    assert s == 200 and d["expires_in"] == 600
    import re
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,63}", d["code"]), "前端 CODE_RE:会拼进 shell 命令"
    raw = d["code"].replace("-", "")
    assert len(raw) * 5 >= 128 and set(raw) <= set(S.CODE_ALPHABET), "base32×32 = 160 bit ≥ 128 bit"
    s, r = _redeem(portal, d["code"])                  # 无 cookie、无 Origin(脚本)
    assert s == 200 and r["key"] == KEY_A1 and r["account_type"] == "Cursor"
    # 三个工具共用同一把 current 的 Cursor key(A 名下另有一把 Claude Code 也不用)
    for tool in ("claude", "codex"):
        c = portal.req("POST", "/ai/api/install-code", {"tool": tool}, cookie=sa)[1]["code"]
        r = _redeem(portal, c)[1]
        assert (r["key"], r["account_type"], r["tool"]) == (KEY_A1, "Cursor", tool), tool
    # 脚本那边把码小写 / 去掉横线也能换(人肉抄码时常见)
    c3 = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(portal, c3.lower().replace("-", ""))[1]["key"] == KEY_A1


def test_install_code_stored_hashed_only(portal):
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    db = Path(portal.app.cfg["DB_PATH"]).read_bytes()
    raw = code.replace("-", "")
    assert code.encode() not in db and raw.encode() not in db and raw.lower().encode() not in db
    row = portal.app.q("select code_hash from install_codes", one=True)
    assert row and len(row[0]) == 64 and row[0] not in (S.sha(code), S.sha(raw)), "HMAC(带服务端密钥),不是裸 sha256"


def test_install_code_single_use(portal):
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(portal, code)[0] == 200                        # POSITIVE:第一次成功
    s, d = _redeem(portal, code)
    assert s == 400 and "key" not in d


def test_install_code_single_use_unguarded(portal, monkeypatch):
    """UNGUARDED:把「标记已用」那条 update 换成无条件成功 ⇒ 同一个码能换两次。"""
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    real_q = portal.app.q

    def q(sql, args=(), one=False):
        if sql.startswith("update install_codes"):
            return [], 1
        if sql.startswith("select union_id, account_type from install_codes"):
            sql = sql.replace(" and used is null", "")
        return real_q(sql, args, one)
    monkeypatch.setattr(portal.app, "q", q)
    assert _redeem(portal, code)[0] == 200 and _redeem(portal, code)[0] == 200


def test_install_code_expires(portal, clock):
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    clock.t += 601
    assert _redeem(portal, code)[0] == 400
    # POSITIVE:599 秒时还能用
    clock.t -= 601
    code2 = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    clock.t += 599
    assert _redeem(portal, code2)[0] == 200


def test_install_code_wrong_code(portal):
    sa = portal.sid("A")
    good = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    flipped = good[:-1] + ("A" if good[-1] != "A" else "B")
    answers = set()
    for bad in (flipped, "A" * 32, "", "nope", None, 123, good + "A", good[:-1], "0" + good[1:]):
        s, d = _redeem(portal, bad, ip="10.9.%d.1" % len(answers))
        assert s == 400 and "key" not in d
        answers.add(json.dumps(d, ensure_ascii=False))
    assert len(answers) == 1, "错码/格式错/过期 同一个回答"
    assert _redeem(portal, good)[0] == 200                        # POSITIVE:真码没被上面的错码影响


def test_install_code_binds_to_issuer(portal):
    """B 签的码只换得出 B 的 key,和谁来换无关(换取不看 cookie)。"""
    sa, sb = portal.sid("A"), portal.sid("B")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sb)[1]["code"]
    s, d, _, _ = portal.req("POST", "/ai/api/install-code/redeem", {"code": code}, cookie=sa,
                            headers={"X-Real-IP": "10.0.0.2"})
    assert s == 200 and d["key"] == KEY_B


def test_install_code_skips_disabled_and_needs_key(portal):
    sn = portal.sid("N")
    assert portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sn)[0] == 404
    assert portal.req("POST", "/ai/api/install-code", {"tool": "rm -rf"}, cookie=sn)[0] == 400
    # A 的已禁用 recA3 申请时间比 recA1 新,客户 key 更新 ⇒ 换出来的仍一定是 current 那把
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(portal, code)[1]["key"] == KEY_A1


def test_redeem_rate_limited_per_ip(portal, clock):
    sa = portal.sid("A")
    good = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    codes = [_redeem(portal, "Z" * 32, ip="10.1.1.1")[0] for _ in range(10)]
    assert codes == [400] * 10
    # 第 11 次:即使是真码也 429(限流先于校验 ⇒ 撞库拿不到「对/错」信号)
    assert _redeem(portal, good, ip="10.1.1.1")[0] == 429
    # POSITIVE:换个 IP 同一个真码能换(证明 429 是限流,不是码坏了)
    assert _redeem(portal, good, ip="10.1.1.2")[0] == 200
    # 窗口过去后恢复
    clock.t += 61
    assert _redeem(portal, "Z" * 32, ip="10.1.1.1")[0] == 400


def test_redeem_rate_limit_unguarded(portal, monkeypatch):
    """UNGUARDED:限流关掉 ⇒ 同 IP 打 30 次都不会 429。证明上一条的 429 来自限流器。"""
    monkeypatch.setattr(portal.app.rl_redeem_ip, "hit", lambda k: True)
    assert 429 not in [_redeem(portal, "Z" * 32, ip="10.1.1.9")[0] for _ in range(30)]


def test_redeem_global_rate_limit(portal_factory):
    p = portal_factory(REDEEM_GLOBAL_PER_MIN="5")
    codes = [_redeem(p, "Q" * 32, ip="10.2.0.%d" % i)[0] for i in range(7)]
    assert codes == [400] * 5 + [429] * 2, "换 IP 也绕不过全局闸"


def test_redeem_ignores_spoofed_xff_when_not_trusted(portal_factory):
    p = portal_factory(TRUST_PROXY="0")
    codes = [_redeem(p, "W" * 32, ip="10.3.0.%d" % i)[0] for i in range(11)]
    assert codes[-1] == 429, "不信代理头时,伪造 X-Real-IP 换不出新额度"


def test_redeem_cross_origin_browser_rejected(portal):
    """redeem 免登录给脚本用;但浏览器带着外站 Origin 来调 ⇒ 拒(本站 Origin 或无 Origin 才放)。"""
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(portal, code, origin="https://evil.example")[0] == 403
    assert _redeem(portal, code, origin=ORIGIN)[0] == 200


def test_issue_code_rate_limited(portal):
    sa = portal.sid("A")
    codes = [portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[0] for _ in range(6)]
    assert codes == [200] * 5 + [429]


# ════════════════════════════════════════════════════════════════════════
# 8. 反馈(复用 feedback_server 的 build_fields / KEY_RE)
# ════════════════════════════════════════════════════════════════════════
FB = {"title": "选 opus 报 403", "类型": "用不了/报错", "工具": "Cursor", "电脑": "Mac",
      "detail": "报错:key sk-abcdefghijklmnop1234 not allowed", "source": "ai#keys"}
FBTBL = ("SQ4vbBJhcauRKpsuZrNczNCInxb", "tblkckVodz4HHKZf")


def test_feedback_reuses_feedback_server():
    assert S.KEY_RE is S.fb.KEY_RE and S.fb.build_fields.__module__ == "feedback_server"


def test_feedback_writes_masked_and_server_identity(portal):
    sa = portal.sid("A")
    s, d, _, _ = portal.req("POST", "/ai/api/feedback", dict(FB, 提交人邮箱="evil@x.com"), cookie=sa)
    assert s == 200
    f = FAKE.tables[FBTBL][d["id"]]
    assert f["提交人"] == [{"id": USERS["A"]["union_id"]}] and f["提交人邮箱"] == "zs@auto-link.com.cn"
    assert "sk-abcdefghijklmnop1234" not in json.dumps(f, ensure_ascii=False) and "sk-***" in f["详细描述"]
    assert "张三(内部)" in f["详细描述"] and f["类型"] == "用不了/报错"


def test_feedback_external_falls_back_without_person(portal):
    sx = portal.sid("X")
    FAKE.fail_create_person = True
    s, d, _, _ = portal.req("POST", "/ai/api/feedback", FB, cookie=sx)
    assert s == 200
    f = FAKE.tables[FBTBL][d["id"]]
    assert "提交人" not in f and f["提交人邮箱"] == "" and "外部王五(外部)" in f["详细描述"]


def test_feedback_validation_and_rate_limit(portal):
    sa = portal.sid("A")
    assert portal.req("POST", "/ai/api/feedback", dict(FB, 类型="乱写"), cookie=sa)[0] == 400
    assert portal.req("POST", "/ai/api/feedback", dict(FB, title=" "), cookie=sa)[0] == 400
    codes = [portal.req("POST", "/ai/api/feedback", FB, cookie=sa)[0] for _ in range(4)]
    assert codes == [200, 200, 200, 429]


# ════════════════════════════════════════════════════════════════════════
# 9. 静态文件
# ════════════════════════════════════════════════════════════════════════
def test_static_index_and_assets(portal):
    s, b, _, _ = portal.req("GET", "/ai/")
    assert s == 200 and "PORTAL-INDEX" in b
    assert portal.req("GET", "/ai")[2] == "/ai/"
    assert portal.req("GET", "/ai/app.js") == (200, "console.log(1)", None, [])


@pytest.mark.parametrize("path", ["/ai/../server.py", "/ai/%2e%2e/server.py", "/ai/..%2fserver.py",
                                  "/ai/static/../../x", "/ai/.env", "/ai/nope.js", "/elsewhere"])
def test_static_traversal_and_missing(portal, path):
    assert portal.req("GET", path)[0] == 404


def test_static_dir_is_repo_static_by_default():
    assert Path(S.DEFAULTS["STATIC_DIR"]) == HERE / "static"


# ════════════════════════════════════════════════════════════════════════
# 10. 配置:SESSION_SECRET 缺失拒绝启动
# ════════════════════════════════════════════════════════════════════════
def test_session_secret_has_no_default():
    assert "SESSION_SECRET" not in S.DEFAULTS
    assert S.load_config(environ={})["SESSION_SECRET"] == ""


@pytest.mark.parametrize("secret", ["", "   ", "short-secret"])
def test_app_refuses_without_good_secret(fake, tmp_path, secret):
    with pytest.raises(S.ConfigError):
        S.App(make_cfg(fake, tmp_path, SESSION_SECRET=secret))
    # POSITIVE:同一份配置只把密钥换成合格的 ⇒ 能起
    S.App(make_cfg(fake, tmp_path))


def test_process_refuses_to_start_without_secret(tmp_path):
    env_file = tmp_path / "ai-portal.env"
    env_file.write_text("FEISHU_APP_ID=" + APP_ID + "\nFEISHU_APP_SECRET=y\n# SESSION_SECRET 故意没配\nLISTEN=127.0.0.1:0\n")
    env = {k: v for k, v in os.environ.items() if k not in ("SESSION_SECRET", "FEISHU_APP_ID", "FEISHU_APP_SECRET")}
    r = subprocess.run([sys.executable, str(HERE / "server.py"), "--env", str(env_file)], env=env,
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "SESSION_SECRET" in r.stderr
    # POSITIVE:补上密钥 ⇒ 进程起来并开始监听(超时被杀 = 在跑)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env_file.write_text(env_file.read_text().replace("LISTEN=127.0.0.1:0", "LISTEN=127.0.0.1:%d" % port)
                        + "SESSION_SECRET=%s\nDB_PATH=%s\n" % (SECRET, tmp_path / "x.db"))
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--env", str(env_file)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(50):
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                c.request("GET", "/ai/healthz")
                assert c.getresponse().status == 200
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("配了密钥却没起来")
    finally:
        proc.terminate()
        proc.wait(5)


def test_env_file_parsing_and_process_env_wins(tmp_path):
    f = tmp_path / "e.env"
    f.write_text("# c\nSESSION_SECRET='from-file'\nFEISHU_APP_ID=\"cli_file\"\n\nBAD LINE\n")
    cfg = S.load_config(environ={"FEISHU_APP_ID": "cli_env"}, env_file=str(f))
    assert cfg["SESSION_SECRET"] == "from-file" and cfg["FEISHU_APP_ID"] == "cli_env"


# ════════════════════════════════════════════════════════════════════════
# 11. 日志不带秘密
# ════════════════════════════════════════════════════════════════════════
def test_logs_never_contain_secrets(portal_factory, capsys):
    p = portal_factory(APPLY_DRY_RUN="0")
    capsys.readouterr()
    _, _, loc, sc = p.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc + "&as=A")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc2).query))
    _, _, _, sc3 = p.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc, "ai_oauth"))
    sid = "ai_sid=" + cookie_of(sc3, "ai_sid")
    p.req("GET", "/ai/api/keys", cookie=sid)
    p.req("POST", "/ai/api/keys/reveal", {"record_id": "recA1"}, cookie=sid)
    p.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=sid)
    code = p.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sid)[1]["code"]
    _redeem(p, code)
    _redeem(p, code)
    p.req("POST", "/ai/api/feedback", FB, cookie=sid)
    p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sid)
    out = capsys.readouterr()
    logs = out.out + out.err
    assert "reveal" in logs and "redeem" in logs, "阳性对照:确实在打日志,不是什么都没打"
    secrets_ = [KEY_A1, KEY_B, code, code[4:], q["code"], q["state"], cookie_of(sc3, "ai_sid").split(".")[0],
                APP_SECRET, SECRET, "sk-abcdefghijklmnop1234"] + list(FAKE.uats) + list(FAKE.tenant_tokens)
    for s in secrets_:
        assert s not in logs, s[:6]


# ════════════════════════════════════════════════════════════════════════
# 12. 和前端(static/index.html + static/dev_mock.py 约定)的接口形状
# ════════════════════════════════════════════════════════════════════════
def test_frontend_login_uses_redirect_param(portal):
    st, _, loc, sc = portal.req("GET", "/ai/auth/login?redirect=" + urllib.parse.quote("/ai/?x=1#keys"))
    _, _, loc2, _ = fake_get(loc + "&as=A")
    st3, _, loc3, _ = portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc, "ai_oauth"))
    assert st3 == 302 and loc3 == "/ai/?x=1#keys"
    # redirect 也走同一道开放重定向校验
    st, _, loc, sc = portal.req("GET", "/ai/auth/login?redirect=https://evil.example/")
    _, _, loc2, _ = fake_get(loc + "&as=A")
    assert portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc, "ai_oauth"))[2] == "/ai/"


def test_frontend_get_logout(portal):
    sid = portal.sid("A")
    st, _, loc, sc = portal.req("GET", "/ai/auth/logout?redirect=/ai/", cookie=sid)
    assert st == 302 and loc == "/ai/" and any(c.startswith("ai_sid=;") for c in sc)
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 401
    assert portal.req("GET", "/ai/auth/logout?redirect=//evil.example", cookie=sid)[2] == "/ai/"


def test_errors_carry_message_for_frontend(portal):
    """前端 api() 只读 j.message;没有就显示泛化文案。"""
    sa = portal.sid("A")
    s, d, _, _ = portal.req("POST", "/ai/api/apply", {"account_type": "乱写"}, cookie=sa)
    assert s == 400 and d["message"] and d["message"] == d["error"]
    s, d, _, _ = portal.req("GET", "/ai/api/me")
    assert s == 401 and d["message"]


def test_frontend_feedback_shape(portal):
    sa = portal.sid("A")
    body = {"type": "用不了/报错", "tool": "Claude Code", "title": "装不上", "detail": "点了没反应",
            "report": "env: OPENAI_API_KEY=sk-leakleakleakleak9999\nos=mac", "os": "Mac", "page": "/ai/"}
    s, d, _, _ = portal.req("POST", "/ai/api/feedback", body, cookie=sa)
    assert s == 200
    f = FAKE.tables[FBTBL][d["id"]]
    assert f["类型"] == "用不了/报错" and f["工具"] == "Claude Code" and f["电脑"] == "Mac" and f["来源页面"] == "/ai/"
    assert "自检报告" in f["详细描述"] and "os=mac" in f["详细描述"]
    assert "sk-leakleak" not in json.dumps(f, ensure_ascii=False), "自检报告里的 key 也要打码"
    # 200KB 自检报告(前端上限)能收
    big = dict(body, report="x" * 200 * 1024)
    assert portal.req("POST", "/ai/api/feedback", big, cookie=sa)[0] == 200


def test_dev_mock_not_served(portal):
    """static/ 里同事放了 dev_mock.py;只出白名单后缀。"""
    (Path(portal.app.cfg["STATIC_DIR"]) / "dev_mock.py").write_text("SECRET = 1")
    assert portal.req("GET", "/ai/dev_mock.py")[0] == 404
    assert portal.req("GET", "/ai/app.js")[0] == 200                      # POSITIVE


def test_real_static_index_is_served(fake, tmp_path, clock):
    """仓库里同事真写的 static/index.html 能从 /ai/ 出来(只读,不改它)。"""
    real = HERE / "static" / "index.html"
    if not real.is_file():
        pytest.skip("static/index.html 还没有")
    FAKE.reset()
    cfg = make_cfg(fake, tmp_path)
    cfg["STATIC_DIR"] = str(HERE / "static")
    p = Portal(fake, cfg, clock)
    try:
        s, b, _, _ = p.req("GET", "/ai/")
        assert s == 200 and b == real.read_text(encoding="utf-8")
        assert p.req("GET", "/ai/dev_mock.py")[0] == 404
    finally:
        p.close()


# ════════════════════════════════════════════════════════════════════════
# 13. key 表真实数据规则(数据组实测)
# ════════════════════════════════════════════════════════════════════════
def test_status_mapping_by_prefix():
    f = S.front_status
    assert f("有效", "sk-x") == "可用" and f("有效（已重新生成）", "sk-x") == "可用" and f("已完成", "sk-x") == "可用"
    assert f("已禁用", "sk-x") == "已禁用" and f("已禁用（离职）", "sk-x") == "已禁用"
    assert f("生成失败: 网关超时", "") == "生成失败: 网关超时"
    assert f("生成中", "") == "生成中" and f("", "__provisioning__") == "生成中" and f("有效", "__provisioning__") == "生成中"
    assert f("", "sk-x") == "" and f("奇怪的值", "sk-x") == "奇怪的值"


def test_keys_status_and_single_current(portal):
    sa = portal.sid("A")
    m = {k["record_id"]: k for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]}
    assert {r: m[r]["status"] for r in m} == {
        "recA1": "可用", "recA2": "可用", "recA3": "已禁用", "recA4": "生成中",
        "recA5": "生成失败: 网关创建 key 超时 sk-…1234", "recA6": "可用"}
    assert [r for r in m if m[r]["current"]] == ["recA1"], "只有一行 current"
    # recA3 已禁用但申请时间更新、recA6 已完成、recA2 是 Claude Code ⇒ 都不是 current
    assert m["recA4"]["masked"] == "" and "__provisioning__" not in json.dumps(m)


def test_current_picks_latest_valid_cursor(portal):
    sa = portal.sid("A")
    FAKE.tables[KEYTBL]["recA7"] = row("A", "sk-AAAAnewest777newest", "有效", T0 + 8 * DAY)
    cur = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if k["current"]]
    assert cur == ["recA7"]
    # 「有效」但 key 不是 sk-(比如还在占位)⇒ 不当 current
    FAKE.tables[KEYTBL]["recA7"]["API Key"] = "__provisioning__"
    cur = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if k["current"]]
    assert cur == ["recA1"]


def test_customer_batch_keys_excluded(portal, monkeypatch):
    sa = portal.sid("A")
    ids = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]]
    assert "recQ1" not in ids and not [i for i in ids if i.startswith("recBT")]
    # 巧虎那行申请时间最新、状态有效、Cursor ⇒ 如果没排除它会被当成 current
    assert [k for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if k["current"]][0]["record_id"] == "recA1"
    # 客户 key 不能 reveal、不能拿去换安装码
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recQ1"}, cookie=sa)[0] == 404
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recBT0"}, cookie=sa)[0] == 404
    # UNGUARDED:关掉排除 ⇒ 客户 key 就会出现、并抢走 current
    monkeypatch.setattr(portal.app, "drop_batches", lambda rows, user: rows)
    d = portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]
    assert "recQ1" in [k["record_id"] for k in d] and [k for k in d if k["current"]][0]["record_id"] == "recBT5"


def test_batch_threshold_boundary(portal):
    """连续 4 行不算批量(阈值 5),5 行算。"""
    sa = portal.sid("A")
    for i in range(5, 6):
        del FAKE.tables[KEYTBL]["recBT%d" % i]
    del FAKE.tables[KEYTBL]["recBT4"]
    ids = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]]
    assert len([i for i in ids if i.startswith("recBT")]) == 4                         # POSITIVE:4 行照常返回
    FAKE.tables[KEYTBL]["recBT4"] = row("A", "sk-BATCHbatch0004xxxx", "有效", T0 + 10 * DAY + 4000)
    ids = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]]
    assert not [i for i in ids if i.startswith("recBT")]


def test_plaintext_in_other_columns_never_returned(portal):
    sa = portal.sid("A")
    raw = json.dumps(portal.req("GET", "/ai/api/keys", cookie=sa)[1], ensure_ascii=False)
    assert KEY_A1 not in raw and KEY_A2 not in raw and "#k=sk-AAAA" not in raw
    # 后端拿到的行里确实有这些列(阳性:假飞书真的把体检链接/Cursor Key 回给了后端)
    assert KEY_A1 in json.dumps(FAKE.tables[KEYTBL]["recA1"]["体检链接"])


def test_mask_deep():
    v = {"a": "x https://h/keyprobe/#k=sk-abcdefghijklmnop1234 y", "b": [{"text": "sk-zzzzzzzzzzzz9999"}], "c": 3}
    out = S.mask_deep(v)
    assert out == {"a": "x https://h/keyprobe/#k=sk-…1234 y", "b": [{"text": "sk-…9999"}], "c": 3}


def test_reveal_disabled_denied(portal):
    sa = portal.sid("A")
    for rid in ("recA3", "recA4", "recA5"):           # 已禁用 / 生成中 / 生成失败(无 sk-)
        s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": rid}, cookie=sa)
        assert s == 403 and "sk-" not in json.dumps(d), rid
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recA6"}, cookie=sa)[0] == 200   # POSITIVE 已完成可 reveal


def test_reveal_disabled_unguarded(portal, monkeypatch):
    """UNGUARDED:状态映射把「已禁用」当成可用 ⇒ 已禁用的 key 就能被 reveal。证明上一条挡的是状态。"""
    sa = portal.sid("A")
    monkeypatch.setattr(S, "front_status", lambda raw, key: "可用")
    s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recA3"}, cookie=sa)
    assert s == 200 and d["key"] == "sk-AAAAold9999disabled"


def test_reveal_audit(portal):
    sa = portal.sid("A")
    portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recA1"}, cookie=sa, headers={"X-Real-IP": "10.7.7.7"})
    portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=sa, headers={"X-Real-IP": "10.7.7.8"})
    portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recA3"}, cookie=sa, headers={"X-Real-IP": "10.7.7.9"})
    rows, _ = portal.app.q("select union_id, name, record_id, ip, login, result, at from reveal_audit order by id")
    assert [(r[2], r[3], r[4], r[5]) for r in rows] == [
        ("recA1", "10.7.7.7", "oauth", "ok"), ("recB1", "10.7.7.8", "oauth", "denied:not-owner"),
        ("recA3", "10.7.7.9", "oauth", "denied:disabled")]
    assert all(r[0] == USERS["A"]["union_id"] and r[1] == "张三" and r[6] for r in rows)
    db = Path(portal.app.cfg["DB_PATH"]).read_bytes()
    assert KEY_A1.encode() not in db, "审计不记明文"


def test_invalid_filter_is_no_match_other_errors_fail(portal, capsys):
    """1254018 ⇒ 查无记录 + 一条日志;其它错误码 ⇒ 502,不能说成「没有 key」。"""
    bad = {"union_id": "on_truncated", "name": "x"}
    assert portal.app.my_rows(bad) == []
    assert "InvalidFilter" in capsys.readouterr().out
    # POSITIVE:同一路径,真 id ⇒ 有行(证明空结果来自 1254018 分支,不是搜索本来就坏)
    assert portal.app.my_rows({"union_id": USERS["A"]["union_id"]})
    sa = portal.sid("A")
    FAKE.search_error = 91403                       # 比如 carher-1000 那种无权限
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sa)
    assert s == 502 and "keys" not in d
    assert portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[0] == 502
    FAKE.search_error = 1254018                     # UNGUARDED 对照:换成 InvalidFilter ⇒ 就是空列表 200
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sa)
    assert s == 200 and d["keys"] == []


def test_wrong_app_id_refused():
    cfg = S.load_config(environ={})
    cfg.update(FEISHU_APP_ID="cli_carher1000xxxx", FEISHU_APP_SECRET="x", SESSION_SECRET=SECRET)
    assert any("carher-236" in e for e in S.check_config(cfg))
    cfg["FEISHU_APP_ID"] = APP_ID                    # POSITIVE
    assert S.check_config(cfg) == []


def test_rotate_is_501(portal):
    sa = portal.sid("A")
    s, d, _, _ = portal.req("POST", "/ai/api/keys/rotate", {"record_id": "recA1"}, cookie=sa)
    assert s == 501 and d["message"] == "需要换 key 请提反馈"
    assert FAKE.tables[KEYTBL]["recA1"]["API Key"][0]["text"] == KEY_A1, "没动表"


# ════════════════════════════════════════════════════════════════════════
# 14. 一次性登录链接(magic link):外部人唯一的登录方式
# ════════════════════════════════════════════════════════════════════════
@pytest.fixture
def mportal(portal_factory):
    return portal_factory(INTERNAL_SECRET=INTERNAL)


def issue(p, who="X", secret=INTERNAL, body=None):
    u = USERS[who]
    b = body if body is not None else {"union_id": u["union_id"], "open_id": u["open_id"],
                                       "tenant_key": u["tenant_key"], "name": u["name"]}
    h = {"Authorization": "Bearer " + secret} if secret is not None else {}
    return p.req("POST", "/ai/api/internal/magic-link", b, origin=None, headers=h)


def tok_of(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["t"]


def consume(p, t, xrw="ai-portal", origin=ORIGIN, ip="10.5.0.1"):
    h = {"X-Real-IP": ip}
    if xrw:
        h["X-Requested-With"] = xrw
    return p.req("POST", "/ai/auth/magic", {"t": t}, origin=origin, headers=h)


def magic_sid(p, who="X"):
    t = tok_of(issue(p, who)[1]["url"])
    s, d, _, sc = consume(p, t)
    assert s == 200 and d == {"ok": True}
    return "ai_sid=" + cookie_of(sc, "ai_sid")


def test_magic_full_flow_external(mportal):
    s, d, _, _ = issue(mportal)
    assert s == 200 and d["url"].startswith("https://chat.auto-link.com.cn/ai/auth/magic?t=") and d["expires_in"] == 600
    t = tok_of(d["url"])
    import re
    assert re.fullmatch(r"[A-Za-z0-9._~-]{8,512}", t) and len(t) >= 43, "前端 MAGIC_T_RE + ≥256 bit"
    s, d, _, sc = consume(mportal, t)
    c = [x for x in sc if x.startswith("ai_sid=")][0]
    assert s == 200 and d == {"ok": True} and all(f in c for f in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/ai/"))
    sid = "ai_sid=" + cookie_of(sc, "ai_sid")
    me = mportal.req("GET", "/ai/api/me", cookie=sid)[1]
    assert me["union_id"] == USERS["X"]["union_id"] and me["external"] is True and me["login"] == "magic"
    assert me["tenant"] == USERS["X"]["tenant_key"]
    # 和 OAuth 同一种会话结构:能查自己的 key、能 reveal
    ks = mportal.req("GET", "/ai/api/keys", cookie=sid)[1]["keys"]
    assert [k["record_id"] for k in ks] == ["recX1"]
    assert mportal.req("POST", "/ai/api/keys/reveal", {"record_id": "recX1"}, cookie=sid)[1]["key"] == KEY_X
    assert not [x for x in FAKE.calls if "/authen/" in x[1]], "magic 登录不走飞书 OAuth"


def test_magic_and_oauth_same_session_shape(mportal):
    so = mportal.sid("A")
    sm = magic_sid(mportal, "A")
    mo = mportal.req("GET", "/ai/api/me", cookie=so)[1]
    mm = mportal.req("GET", "/ai/api/me", cookie=sm)[1]
    assert set(mo) == set(mm) and mo["login"] == "oauth" and mm["login"] == "magic"
    rows, _ = mportal.app.q("select user from sessions")
    users = [json.loads(r[0]) for r in rows]
    assert {u["login"] for u in users} == {"oauth", "magic"}
    assert all(set(u) == {"union_id", "open_id", "tenant_key", "name", "avatar", "email", "external", "login",
                          "app_id"} and u["app_id"] == APP_ID for u in users)


def test_magic_get_does_not_consume(mportal):
    t = tok_of(issue(mportal)[1]["url"])
    for _ in range(3):                                   # 飞书链接预览 / 安全扫描 / 用户刷新
        s, b, _, sc = mportal.req("GET", "/ai/auth/magic?t=" + t)
        assert s == 200 and "PORTAL-INDEX" in b and not cookie_of(sc, "ai_sid")
    assert consume(mportal, t)[0] == 200                 # POSITIVE:GET 过 3 次后 POST 仍能用


def test_magic_get_page_headers(mportal):
    t = tok_of(issue(mportal)[1]["url"])
    c = http.client.HTTPConnection("127.0.0.1", mportal.port, timeout=5)
    c.request("GET", "/ai/auth/magic?t=" + t)
    r = c.getresponse()
    r.read()
    assert r.getheader("Referrer-Policy") == "no-referrer" and r.getheader("Cache-Control") == "no-store"


def test_magic_replay_rejected(mportal):
    t = tok_of(issue(mportal)[1]["url"])
    assert consume(mportal, t)[0] == 200                 # POSITIVE 第一次
    s, d, _, sc = consume(mportal, t)
    assert s == 410 and d["error"] == "used" and d["message"] and not cookie_of(sc, "ai_sid")


def test_magic_replay_unguarded(mportal, monkeypatch):
    """UNGUARDED:不检查 used、标记已用恒成功 ⇒ 同一个 token 能登两次。"""
    t = tok_of(issue(mportal)[1]["url"])
    real_q = mportal.app.q

    def q(sql, args=(), one=False):
        if sql.startswith("update magic_tokens"):
            return [], 1
        if sql.startswith("select user, expires, used from magic_tokens"):
            r = real_q(sql, args, one)
            return (r[0], r[1], None) if r else r
        return real_q(sql, args, one)
    monkeypatch.setattr(mportal.app, "q", q)
    assert consume(mportal, t)[0] == 200 and consume(mportal, t)[0] == 200


def test_magic_expired(mportal, clock):
    t = tok_of(issue(mportal)[1]["url"])
    clock.t += 601
    s, d, _, _ = consume(mportal, t)
    assert s == 410 and d["error"] == "expired"
    t2 = tok_of(issue(mportal)[1]["url"])                # POSITIVE:599 秒时可用
    clock.t += 599
    assert consume(mportal, t2)[0] == 200


def test_magic_forged_token(mportal):
    good = tok_of(issue(mportal)[1]["url"])
    flipped = good[:-1] + ("A" if good[-1] != "A" else "B")
    for bad in (flipped, "A" * 43, "", "short", None, 123, good + "x" * 30, "../" + good):
        s, d, _, sc = consume(mportal, bad, ip="10.6.%d.1" % (hash(str(bad)) % 200))
        assert s == 400 and d["error"] == "invalid" and not cookie_of(sc, "ai_sid"), bad
    assert consume(mportal, good)[0] == 200              # POSITIVE:真 token 没被上面影响


def test_magic_token_stored_hashed(mportal):
    t = tok_of(issue(mportal)[1]["url"])
    assert t.encode() not in Path(mportal.app.cfg["DB_PATH"]).read_bytes()
    h = mportal.app.q("select token_hash from magic_tokens", one=True)[0]
    assert len(h) == 64 and h != S.sha(t)


def test_magic_post_needs_xrw_and_origin(mportal):
    t = tok_of(issue(mportal)[1]["url"])
    assert consume(mportal, t, xrw=None)[0] == 403
    assert consume(mportal, t, xrw="evil")[0] == 403
    assert consume(mportal, t, origin="https://evil.example")[0] == 403
    assert consume(mportal, t)[0] == 200                 # POSITIVE:上面都没消费掉它


def test_magic_consume_rate_limited(mportal):
    codes = [consume(mportal, "B" * 43, ip="10.8.8.8")[0] for _ in range(11)]
    assert codes == [400] * 10 + [429]


def test_internal_wrong_secret(mportal):
    for sec in ("wrong-" + INTERNAL, "", INTERNAL[:-1], SECRET):
        s, d, _, _ = issue(mportal, secret=sec)
        assert s == 401 and "url" not in d, sec
    s, d, _, _ = issue(mportal, secret=None)
    assert s == 401
    s, d, _, _ = mportal.req("POST", "/ai/api/internal/magic-link", {"union_id": USERS["X"]["union_id"]},
                             origin=None, headers={"Authorization": INTERNAL})    # 没有 Bearer 前缀
    assert s == 401
    assert issue(mportal)[0] == 200                      # POSITIVE


def test_internal_wrong_secret_unguarded(mportal, monkeypatch):
    """UNGUARDED:鉴权恒过 ⇒ 错密钥也能签发。证明上一条 401 来自 internal_ok。"""
    monkeypatch.setattr(mportal.app, "internal_ok", lambda h: None)
    assert issue(mportal, secret="wrong")[0] == 200


def test_internal_endpoint_closed_without_secret(portal):
    """没配 INTERNAL_SECRET ⇒ 接口关闭:任何密钥都 404(连「密钥不对」都不说)。"""
    assert "INTERNAL_SECRET" not in S.DEFAULTS and portal.app.internal_secret == ""
    for sec in (INTERNAL, "", "anything"):
        assert issue(portal, secret=sec)[0] == 404
    assert issue(portal, secret=None)[0] == 404


def test_internal_endpoint_open_with_secret_positive(portal_factory):
    """POSITIVE 对照:同一份配置只多配一个 INTERNAL_SECRET ⇒ 200。证明上一条 404 是因为没配。"""
    p = portal_factory(INTERNAL_SECRET=INTERNAL)
    assert issue(p)[0] == 200


def test_internal_secret_config_checks(fake, tmp_path):
    with pytest.raises(S.ConfigError):
        S.App(make_cfg(fake, tmp_path, INTERNAL_SECRET="short"))
    with pytest.raises(S.ConfigError):
        S.App(make_cfg(fake, tmp_path, INTERNAL_SECRET=SECRET))       # 和 SESSION_SECRET 一样
    S.App(make_cfg(fake, tmp_path, INTERNAL_SECRET=INTERNAL))          # POSITIVE


def test_internal_bad_body(mportal):
    for b in ({}, {"union_id": "ou_xxxx"}, {"union_id": "on_"}, {"union_id": 1}, {"union_id": USERS["X"]["union_id"],
                                                                                   "name": ["x"]}):
        assert issue(mportal, body=b)[0] == 400, b


def test_internal_not_reachable_with_session_cookie(mportal):
    """登录用户(有 cookie、本站 Origin)也不能签 magic link,只认内部密钥。"""
    sid = mportal.sid("A")
    s, _, _, _ = mportal.req("POST", "/ai/api/internal/magic-link", {"union_id": USERS["A"]["union_id"]}, cookie=sid)
    assert s == 401


def test_magic_logs_no_secrets(mportal, capsys):
    capsys.readouterr()
    t = tok_of(issue(mportal)[1]["url"])
    consume(mportal, t)
    consume(mportal, t)
    out = capsys.readouterr().out
    assert "magic 签发" in out and "magic 拒绝 used" in out          # 阳性:确实在打日志
    assert t not in out and INTERNAL not in out


# ── 11-04 拿真表(1452 行)复核后补的三条 ──────────────────────────────────

def test_batch_across_minute_boundary_is_still_one_batch(portal, monkeypatch):
    """🔴 真表:刘国现名下 10 把零跑汽车客户 key 每 ~3 秒一把,跨了自然分钟边界(8+2)。
    旧规则按「同一分钟 ≥5」切桶 ⇒ 尾巴 2 把漏过,且因为最新被选成了**他本人的当前 key**。"""
    sa = portal.sid("A")
    for i in range(6):
        del FAKE.tables[KEYTBL]["recBT%d" % i]
    edge = (T0 // 60000 + 20 * 24 * 60) * 60000 + 59000          # 某分钟的第 59 秒起步
    for i in range(10):
        FAKE.tables[KEYTBL]["recLP%d" % i] = row("A", "sk-LEAPMOTORclient%04dx" % i, "有效", edge + i * 3000, org="零跑汽车")
    ks = portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]
    assert not [k for k in ks if k["record_id"].startswith("recLP")], ks
    assert [k for k in ks if k["current"]][0]["record_id"] == "recA1"
    # 阳性对照:用旧的「同一自然分钟」切桶,尾巴就会漏过并抢走 current —— 证明这条测得出那个 bug
    def by_minute(rows, user):
        pm = {}
        for r in rows:
            if r["at"]:
                pm[r["at"] // 60000] = pm.get(r["at"] // 60000, 0) + 1
        return [r for r in rows if r["org"] not in portal.app.exclude_orgs and not (r["at"] and pm[r["at"] // 60000] >= 5)]
    monkeypatch.setattr(portal.app, "drop_batches", by_minute)
    ks = portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]
    assert [k for k in ks if k["current"]][0]["record_id"].startswith("recLP")


def test_undated_legacy_rows_are_not_a_batch(portal):
    """🔴 真表:早年导入的行没有申请时间(at=0)。旧规则把它们同算进「第 0 分钟」,
    凑满 5 行就整组藏掉 —— 刘国现那 5 行恰好是他本人的 Cursor / Claude / CarHer key。"""
    sa = portal.sid("A")
    for i in range(6):
        del FAKE.tables[KEYTBL]["recBT%d" % i]
    for i in range(5):
        FAKE.tables[KEYTBL]["recOLD%d" % i] = row("A", "sk-LEGACYnodate%04dxxx" % i, "有效", None)
    ids = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]]
    assert len([i for i in ids if i.startswith("recOLD")]) == 5, ids


@pytest.mark.parametrize("raw", ["重新生成中", "待生成（第三方合作伙伴）", "待生成（第三方合作伙伴补录）"])
def test_real_pending_statuses_map_to_pending(portal, raw):
    """🔴 真表 16 个状态选项里有这三个「在等 key」的值。只认「生成中」开头会漏掉它们
    (「重新生成中」不以「生成中」开头)⇒ 前端当未知状态置灰、不轮询,看着像坏了。"""
    sa = portal.sid("A")
    FAKE.tables[KEYTBL]["recPEND"] = row("A", "", raw, T0 + 30 * DAY)
    k = [x for x in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if x["record_id"] == "recPEND"][0]
    assert k["status"] == "生成中" and k["masked"] == ""


def test_redeem_returns_tool_and_base_url_for_installer(portal):
    """🔴 11-04 联调发现:签发时存了 tool,换取时却没回 ⇒ 安装器(installer/install.sh)
    拿不到 tool 就 die「安装服务没说要装哪个工具」⇒ **每一次安装都在第一步失败**。
    后端测试和安装器测试各自都是绿的(各对着自己写的假对端),只有接起来才看得见。
    这里钉住安装器依赖的契约;端到端版本在 .work/ai-portal/e2e/e2e.py(真后端 + 真安装器)。"""
    sa = portal.sid("A")
    for tool in ("cursor", "codex", "claude"):
        code = portal.req("POST", "/ai/api/install-code", {"tool": tool}, cookie=sa)[1]["code"]
        s, b = _redeem(portal, code)
        assert s == 200 and b["key"] == KEY_A1, (s, b)
        assert b["tool"] == tool, b                          # 安装器按它决定装哪个工具
        assert "base_url" not in b, "安装器不读它;网关地址在子安装器自己的 BASE_URL 里,别留死字段"


def test_pilot_gate_blocks_both_login_paths(mportal):
    """灰度闸:名单非空时只有名单里的人能登录 —— OAuth 和一次性登录链接**两条路都要被管住**。
    阳性对照:名单里的人两条路都能进;清空名单 ⇒ 所有人都能进。"""
    mportal.app.cfg["PILOT_UNION_IDS"] = USERS["A"]["union_id"] + "," + USERS["X"]["union_id"]
    try:
        assert mportal.sid("A")                                   # POSITIVE:名单内 OAuth 能进
        assert magic_sid(mportal, "X")                            # POSITIVE:名单内 magic 能进
        st, _, sid, _ = mportal.login("B")
        assert not sid, "名单外的人走 OAuth 不该拿到会话"
        mportal.app.cfg["PILOT_UNION_IDS"] = USERS["A"]["union_id"]
        t = tok_of(issue(mportal, "X")[1]["url"])
        s, d, _, sc = consume(mportal, t)
        assert s == 403 and "内测" in d.get("message", "") and not cookie_of(sc, "ai_sid"), (s, d)
    finally:
        mportal.app.cfg["PILOT_UNION_IDS"] = ""
    assert mportal.sid("B")                                       # 清空 ⇒ 对所有人开放


@pytest.mark.parametrize("ghost", [{"name": "张三(拿不到 union_id)"}, {"id": ""}, {"id": None}, {}, "字符串成员"])
def test_shared_row_with_idless_member_is_nobodys(portal, ghost):
    """🔴 11-04 红队:人员格里混进一个**没有 id 的成员**时,原实现先过滤再数个数
    ⇒ `[我, {"name":…}]` 被判成「只属于我」⇒ 共用行的明文 key 被当成自己的交出去。
    缺 id 的成员也算一个人 ⇒ 这一格不是独占 ⇒ 不属于任何人。"""
    FAKE.tables[KEYTBL]["recGHOST"] = row("A", "sk-GHOSTghost0000shared", "有效", T0 + 40 * DAY)
    FAKE.tables[KEYTBL]["recGHOST"]["姓名"] = person("A") + [ghost]
    assert not S.owner_ok(FAKE.tables[KEYTBL]["recGHOST"]["姓名"], USERS["A"]["union_id"])
    sa = portal.sid("A")
    ids = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]]
    assert "recGHOST" not in ids, ids
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recGHOST"}, cookie=sa)[0] == 404
    # POSITIVE:同一行只留本人一个成员 ⇒ 立刻变成自己的(证明上面拒绝是因为那个幽灵成员,不是别的原因)
    FAKE.tables[KEYTBL]["recGHOST"]["姓名"] = person("A")
    assert S.owner_ok(FAKE.tables[KEYTBL]["recGHOST"]["姓名"], USERS["A"]["union_id"])


def test_keys_and_apply_are_rate_limited(portal):
    """🔴 11-04 红队 P1-1:这两个接口每次都真打飞书(全租户共享的 tenant token),
    原来一点限流都没有 ⇒ 一个人就能把飞书接口打满,连带 key poller / 反馈服务一起受影响。"""
    sa = portal.sid("A")
    portal.app.cfg["KEYS_PER_MIN"], portal.app.cfg["APPLY_PER_MIN"] = "3", "2"
    portal.app.rl_keys = S.RateLimiter(3, 60, portal.app.now)
    portal.app.rl_apply = S.RateLimiter(2, 60, portal.app.now)
    codes = [portal.req("GET", "/ai/api/keys", cookie=sa)[0] for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429], codes
    codes = [portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sa)[0] for _ in range(4)]
    assert codes.count(429) == 2, codes


def test_rate_limiter_forgets_idle_keys(portal):
    """P1-4:常驻进程里每个 IP / union_id 都留一个条目,只增不减会慢慢涨内存。"""
    t = [1000.0]
    rl = S.RateLimiter(5, 60, lambda: t[0])
    for i in range(2000):
        rl.hit("ip-%d" % i)
    assert len(rl.hits) == 2000
    t[0] += 120                      # 全部过期
    for i in range(2000):
        rl.allowed("ip-%d" % i)      # 只读一次就该把空条目清掉
    assert len(rl.hits) == 0, len(rl.hits)
    # 阳性对照:限流本身还管用(清理不能把计数清掉 —— 我第一版就这么挂的)
    assert [rl.hit("x") for _ in range(7)] == [True] * 5 + [False] * 2


def test_tightening_pilot_gate_kills_existing_sessions(portal):
    """🔴 11-04 红队 P1-5:灰度闸原来只在建会话时判 ⇒ 收紧名单后,已经登录的人
    拿着旧 cookie 还能用满 12 小时(实测还能签安装码)。现在每次请求都过闸。"""
    sa = portal.sid("A")
    assert portal.req("GET", "/ai/api/me", cookie=sa)[0] == 200          # POSITIVE:名单为空时正常
    portal.app.cfg["PILOT_UNION_IDS"] = USERS["B"]["union_id"]           # 收紧成只剩 B
    try:
        assert portal.req("GET", "/ai/api/me", cookie=sa)[0] == 401
        assert portal.req("POST", "/ai/api/install-code", {"tool": "codex"}, cookie=sa)[0] == 401
        assert portal.req("GET", "/ai/api/keys", cookie=sa)[0] == 401
    finally:
        portal.app.cfg["PILOT_UNION_IDS"] = ""
    assert portal.req("GET", "/ai/api/me", cookie=sa)[0] == 200          # 放开后旧会话照常可用
