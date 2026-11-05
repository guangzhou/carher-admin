"""管理后台 /ai/admin/ 的离线回归(admin_console.py + server.py 里的钩子)。全程不碰线上:
假飞书 / 假 198 网关复用 test_server.py;账户库用本文件的假服务(形状照 scripts/account-store/server.py),
最后一组用**真账户库进程**(需要 cryptography,没有就跳过)。

拒绝类测试都配阳性对照(POSITIVE:只改掉攻击那一点 ⇒ 必须成功),
关键闸门另配放开对照(UNGUARDED:monkeypatch 关掉那道校验 ⇒ 攻击必须得逞,证明测试有牙)。
跑法:python3 -m pytest scripts/ai-portal/test_admin.py -q
"""
import csv, hashlib, http.server, io, json, os, re, sys, threading, urllib.parse
from pathlib import Path

import pytest

import test_server as T                     # 同目录;pytest 把本目录放进 sys.path
from test_server import clock, fake, lite   # noqa: F401  fixture(时钟 / 假飞书 / 假网关)

S = T.S
ORIGIN = T.ORIGIN
ADMIN, OTHER, EXT = T.USERS["A"]["union_id"], T.USERS["B"]["union_id"], T.USERS["X"]["union_id"]
# 假 key:形状像 sk-,从没在任何网关上存在过
PLAIN = {1: "sk-ADMINtest1111plainAAAA0001", 2: "sk-ADMINtest2222plainBBBB0002", 3: "sk-ADMINtest3333plainCCCC0003"}
STORE_TOKEN = "ast_test-adminweb-token-0123456789abcdef"


# ── 假账户库 ────────────────────────────────────────────────────────────
def _acct(i, name, alias, org, dept, status, typ, at, state="encrypted"):
    k = PLAIN.get(i, "")
    return {"id": i, "owner_name": name, "owner_union_id": "on_owner%04d" % i, "email_prefix": "user%d" % i,
            "org_name": org, "dept_names": json.dumps(dept, ensure_ascii=False) if dept else None,
            "status": status, "status_raw": "生成失败: timeout " + k if status == "failed" else "有效",
            "account_type": typ, "key_alias": alias, "applied_at": at, "lark_record_id": "rec%d" % i,
            "secrets": [{"kind": "api_key", "state": state, "sha256": hashlib.sha256(k.encode()).hexdigest() if k else None,
                         "hint": ("sk-…" + k[-4:]) if k else "sk-…0000", "key_len": len(k)}]}


ACCOUNTS = [
    _acct(1, "张三", "cursor-zhangsan-ab12", "无锡车联", ["AI 研究院"], "active", "cursor", "2026-11-04T02:00:00.000Z"),
    _acct(2, "李四", "cursor-lisi-cd34", "上海车联", ["智能座舱"], "disabled", "cursor", "2026-10-01T02:00:00.000Z"),
    _acct(3, "=HYPERLINK(\"http://x\")", "cc-evil", "北汽集团", None, "failed", "claude_code", "2026-11-03T02:00:00.000Z"),
    _acct(4, "王五", "cursor-wangwu", "无锡车联", None, "active", "cursor", None, state="hash_only"),
]


class FakeStore:
    def reset(self):
        self.calls, self.reveals, self.fail = [], [], None
        self.old_version = False        # True = 老账户库:回包没有 total

    def accounts(self, q):
        rows = list(ACCOUNTS)
        for p, k in (("org", "org_name"), ("status", "status"), ("type", "account_type")):
            if q.get(p):
                rows = [r for r in rows if r[k] == q[p]]
        if q.get("q"):
            rows = [r for r in rows if any(q["q"] in (r[k] or "") for k in ("owner_name", "key_alias", "email_prefix"))]
        if q.get("dept"):
            rows = [r for r in rows if q["dept"] in (r["dept_names"] or "")]
        off, lim = int(q.get("offset", 0)), int(q.get("limit", 200))
        out = {"items": rows[off:off + lim]}
        if not self.old_version:
            out["total"] = len(rows)
        return out


STORE = FakeStore()
STORE.reset()


class FakeStoreH(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def authed(self):
        if self.headers.get("Authorization") != "Bearer " + STORE_TOKEN:
            self.out(401, {"error": "unauthorized"})
            return False
        if STORE.fail:
            self.out(*STORE.fail)
            return False
        return True

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        STORE.calls.append(("GET", u.path, q))
        if not self.authed():
            return
        if u.path == "/v1/accounts":
            return self.out(200, STORE.accounts(q))
        if u.path == "/v1/stats":
            return self.out(200, {"kek_id": "kek-x", "accounts": 4, "persons": 4, "audit_rows": 9,
                                  "by_status": [["active", 2], ["disabled", 1], ["failed", 1]],
                                  "by_type": [["cursor", 3], ["claude_code", 1]],
                                  "by_org": [["无锡车联", 2], ["上海车联", 1], ["北汽集团", 1]],
                                  "secrets": [["api_key", "encrypted", 3]], "applied_7d": 2,
                                  "applied_by_day_7d": [["2026-11-03", 1], ["2026-11-04", 1]]})
        if u.path == "/v1/audit":
            items = [{"id": 10 - n, "ts": "2026-11-04T0%d:00:00.000Z" % n, "actor": "adminweb:admin-web:张三(on_x)",
                      "action": "reveal", "account_id": 1, "kind": "api_key", "reason": "排查 " + PLAIN[1],
                      "detail": json.dumps({"k": PLAIN[1]}), "owner_name": "张三", "key_alias": "cursor-zhangsan-ab12"}
                     for n in range(3)]
            return self.out(200, {"items": items, "next_before_id": None})
        self.out(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        b = json.loads(self.rfile.read(n) or b"{}")
        STORE.calls.append(("POST", u.path, b))
        if not self.authed():
            return
        m = re.fullmatch(r"/v1/accounts/(\d+)/reveal", u.path)
        if not m:
            return self.out(404, {"error": "not found"})
        if not b.get("reason") or not b.get("actor"):
            return self.out(400, {"error": "要给 kind / reason / actor"})
        aid = int(m.group(1))
        STORE.reveals.append((aid, b["actor"], b["reason"]))
        if aid not in PLAIN:
            return self.out(404, {"error": "没有这把 key"})
        return self.out(200, {"account_id": aid, "kind": "api_key", "hint": "sk-…" + PLAIN[aid][-4:], "key": PLAIN[aid]})


@pytest.fixture(scope="module")
def store_url():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeStoreH)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % s.server_port
    s.shutdown()


def token_file(tmp_path, mode=0o600, content=STORE_TOKEN, name="adminweb.token"):
    p = tmp_path / name
    p.write_text(content + "\n")
    os.chmod(p, mode)
    return str(p)


@pytest.fixture
def admin_factory(fake, store_url, tmp_path, clock):
    made = []

    def mk(**over):
        T.FAKE.reset()
        T.FAKE.tables[T.KEYTBL] = T.seed()
        STORE.reset()
        cfg = T.make_cfg(fake, tmp_path, ADMIN_UNION_IDS=ADMIN, ACCOUNT_STORE_URL=store_url,
                         ACCOUNT_STORE_TOKEN_FILE=token_file(tmp_path), INTERNAL_SECRET=T.INTERNAL)
        cfg.update(over)
        p = T.Portal(fake, cfg, clock)
        made.append(p)
        return p
    yield mk
    for p in made:
        p.close()


@pytest.fixture
def ap(admin_factory):
    return admin_factory()


XRW = {"X-Requested-With": "ai-admin"}


def aget(p, path, sid, headers=None):
    h = dict(XRW)
    h.update(headers or {})
    return p.req("GET", path, cookie=sid, headers=h)


def apost(p, path, body, sid, headers=None, origin=ORIGIN):
    h = dict(XRW)
    h.update(headers or {})
    return p.req("POST", path, body, cookie=sid, headers=h, origin=origin)


def reveal(p, sid, aid=1, reason="本人丢了 key 来要", **kw):
    body = {"account_id": aid, "kind": "api_key"}
    if reason is not None:
        body["reason"] = reason
    return apost(p, "/ai/api/admin/reveal", body, sid, **kw)


def local_audit(p, action=None):
    rows, _ = p.app.q("select union_id, action, account_id, reason, result from admin_audit"
                      + (" where action=?" if action else "") + " order by id", (action,) if action else ())
    return rows


GETS = ["/ai/api/admin/me", "/ai/api/admin/accounts", "/ai/api/admin/stats", "/ai/api/admin/audit",
        "/ai/api/admin/export.csv"]
# ════════════════════════════════════════════════════════════════════════
# 1. 谁能进:未登录 401 / 非管理员 403 / 外部人 403 / magic 会话 403 / 管理员 200
# ════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("path", GETS)
def test_unauthenticated_is_401(ap, path):
    sid = ap.sid("A")
    for bad in (None, "ai_sid=", "ai_sid=forged.sig", sid + "x"):
        assert aget(ap, path, bad)[0] == 401, bad
    assert aget(ap, path, sid)[0] == 200                               # POSITIVE


def test_unauthenticated_post_is_401(ap):
    assert reveal(ap, None)[0] == 401
    assert reveal(ap, ap.sid("A"))[0] == 200                           # POSITIVE


def test_page_unauthenticated_redirects_to_login(ap):
    s, _, loc, _ = ap.req("GET", "/ai/admin/")
    assert s == 302 and loc.startswith("/ai/auth/login?") and "redirect=%2Fai%2Fadmin%2F" in loc
    s, body, _, _ = ap.req("GET", "/ai/admin/", cookie=ap.sid("A"))   # POSITIVE
    assert s == 200 and "账户库后台" in body
    assert ap.req("GET", "/ai/admin")[2] == "/ai/admin/"


@pytest.mark.parametrize("path", GETS)
def test_non_admin_internal_is_403(ap, path):
    s, d, _, _ = aget(ap, path, ap.sid("B"))
    assert s == 403 and "管理员" in d["message"]
    assert aget(ap, path, ap.sid("A"))[0] == 200                       # POSITIVE:同一请求换成管理员


def test_non_admin_page_and_reveal_403_and_store_never_called(ap):
    sid = ap.sid("B")
    assert ap.req("GET", "/ai/admin/", cookie=sid)[0] == 403
    assert reveal(ap, sid)[0] == 403
    assert not [c for c in STORE.calls if c[0] == "POST"], "被拒的人不能让门户去账户库取明文"
    assert [r[1] for r in local_audit(ap)] == ["deny-page", "deny"] and local_audit(ap)[0][4] == "not-admin"
    assert reveal(ap, ap.sid("A"))[0] == 200                           # POSITIVE


def test_external_oauth_user_403_even_if_whitelisted(admin_factory):
    """外部人(租户不是我们的)哪怕被误加进白名单也进不来:租户是独立的一道闸。"""
    p = admin_factory(ADMIN_UNION_IDS=ADMIN + "," + EXT)
    sid = p.sid("X")
    assert p.req("GET", "/ai/api/me", cookie=sid)[1]["external"] is True
    for path in GETS:
        assert aget(p, path, sid)[0] == 403, path
    assert reveal(p, sid)[0] == 403 and p.req("GET", "/ai/admin/", cookie=sid)[0] == 403
    assert local_audit(p)[0][4] == "not-our-tenant"
    assert aget(p, "/ai/api/admin/me", p.sid("A"))[0] == 200            # POSITIVE


def test_external_tenant_unguarded_control(admin_factory, monkeypatch):
    """UNGUARDED:把租户那道闸拿掉,同一个外部人就能进 ⇒ 上一条的 403 确实来自租户校验。"""
    p = admin_factory(ADMIN_UNION_IDS=ADMIN + "," + EXT)
    monkeypatch.setitem(p.app.cfg, "OUR_TENANT_KEY", T.USERS["X"]["tenant_key"])
    sid = p.sid("X")
    assert aget(p, "/ai/api/admin/me", sid)[0] == 200


def test_magic_session_of_admin_is_403(ap):
    """管理员本人用机器人发的一次性链接登录(租户是我们的、在白名单里)⇒ 后台照样不让进:只认 OAuth。"""
    u = T.USERS["A"]
    s, d, _, _ = ap.req("POST", "/ai/api/internal/magic-link", {"union_id": u["union_id"], "open_id": u["open_id"],
                                                                 "tenant_key": u["tenant_key"], "name": u["name"]},
                        origin=None, headers={"Authorization": "Bearer " + T.INTERNAL})
    assert s == 200
    t = T.tok_of(d["url"])
    s, _, _, sc = T.consume(ap, t)
    sid = "ai_sid=" + T.cookie_of(sc, "ai_sid")
    me = ap.req("GET", "/ai/api/me", cookie=sid)[1]
    assert me["login"] == "magic" and me["external"] is False and me["union_id"] == ADMIN
    for path in GETS:
        assert aget(ap, path, sid)[0] == 403, path
    assert reveal(ap, sid)[0] == 403 and ap.req("GET", "/ai/admin/", cookie=sid)[0] == 403
    assert local_audit(ap)[0][4] == "not-oauth"
    assert aget(ap, "/ai/api/admin/me", ap.sid("A"))[0] == 200          # POSITIVE:同一个人走 OAuth


def test_magic_session_unguarded_control(ap, monkeypatch):
    """UNGUARDED:去掉「只认 OAuth」那一条,magic 会话就进得去 ⇒ 上一条的 403 确实来自登录方式校验。"""
    real = ap.app.admin.deny_reason
    monkeypatch.setattr(ap.app.admin, "deny_reason", lambda u: real({**u, "login": "oauth"}))
    sid = ap.app.new_session(ap.app.session_shape({**T.USERS["A"]}, "magic"))
    assert aget(ap, "/ai/api/admin/me", "ai_sid=" + sid)[0] == 200


def test_empty_whitelist_locks_everyone_out(admin_factory):
    p = admin_factory(ADMIN_UNION_IDS="")
    assert aget(p, "/ai/api/admin/me", p.sid("A"))[0] == 403
    p2 = admin_factory()                                                # POSITIVE:默认名单
    assert aget(p2, "/ai/api/admin/me", p2.sid("A"))[0] == 200


def test_default_whitelist_is_only_liuguoxian():
    assert S.load_config(environ={})["ADMIN_UNION_IDS"] == "on_0577e767b8995740ebbf83f06d9f8bd1"


def test_bad_whitelist_entry_refuses_to_start(fake, tmp_path):
    cfg = T.make_cfg(fake, tmp_path, ADMIN_UNION_IDS="on_0577e767b8995740ebbf83f06d9f8bd1,张三")
    assert any("ADMIN_UNION_IDS" in e for e in S.check_config(cfg))
    cfg["ADMIN_UNION_IDS"] = ADMIN                                       # POSITIVE
    assert not S.check_config(cfg)


def test_admin_whitelist_is_rechecked_every_request(ap, monkeypatch):
    """名单收紧即生效:已登录的管理员被移出名单后,旧 cookie 立刻 403。"""
    sid = ap.sid("A")
    assert aget(ap, "/ai/api/admin/me", sid)[0] == 200
    monkeypatch.setattr(ap.app.admin, "admins", set())
    assert aget(ap, "/ai/api/admin/me", sid)[0] == 403


# ════════════════════════════════════════════════════════════════════════
# 2. CSRF:X-Requested-With + Origin + Sec-Fetch-Site
# ════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("path", GETS)
def test_get_without_xrw_is_403(ap, path):
    sid = ap.sid("A")
    assert ap.req("GET", path, cookie=sid)[0] == 403
    assert ap.req("GET", path, cookie=sid, headers={"X-Requested-With": "ai-portal"})[0] == 403
    assert aget(ap, path, sid)[0] == 200                               # POSITIVE


def test_cross_site_get_is_403(ap):
    sid = ap.sid("A")
    assert aget(ap, "/ai/api/admin/accounts", sid, {"Origin": "https://evil.example"})[0] == 403
    assert aget(ap, "/ai/api/admin/accounts", sid, {"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert aget(ap, "/ai/api/admin/accounts", sid, {"Sec-Fetch-Site": "same-site"})[0] == 403
    assert aget(ap, "/ai/api/admin/accounts", sid, {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"})[0] == 200


def test_reveal_csrf(ap):
    sid = ap.sid("A")
    assert reveal(ap, sid, origin="https://evil.example")[0] == 403
    assert reveal(ap, sid, origin=None)[0] == 403                       # 没 Origin 也没 Referer
    s, _, _, _ = ap.req("POST", "/ai/api/admin/reveal", {"account_id": 1, "reason": "原因原因"}, cookie=sid)
    assert s == 403, "没有 X-Requested-With"
    assert not [c for c in STORE.calls if c[0] == "POST"]
    assert reveal(ap, sid)[0] == 200                                    # POSITIVE


def test_reveal_csrf_unguarded_control(ap, monkeypatch):
    """UNGUARDED:关掉 admin 自己的 CSRF 校验,不带 X-Requested-With 的 POST 就能取到明文。"""
    monkeypatch.setattr(ap.app.admin, "csrf", lambda h: None)
    s, d, _, _ = ap.req("POST", "/ai/api/admin/reveal", {"account_id": 1, "reason": "原因原因"}, cookie=ap.sid("A"))
    assert s == 200 and d["key"] == PLAIN[1]


def test_options_gives_no_cors(ap):
    s, _, _, _ = ap.req("OPTIONS", "/ai/api/admin/reveal", headers={"Origin": "https://evil.example",
                                                                    "Access-Control-Request-Method": "POST"})
    h = T.headers_of(ap, "OPTIONS", "/ai/api/admin/reveal")[1]
    assert s == 204 and not [k for k in h if k.startswith("access-control-")]


# ════════════════════════════════════════════════════════════════════════
# 3. 查询 / 统计 / 审计
# ════════════════════════════════════════════════════════════════════════
def test_admin_can_list_and_rows_are_masked(ap):
    s, d, _, _ = aget(ap, "/ai/api/admin/accounts", ap.sid("A"))
    assert s == 200 and d["total"] == 4 and [r["id"] for r in d["items"]] == [1, 2, 3, 4]
    r1 = d["items"][0]
    assert r1["name"] == "张三" and r1["org"] == "无锡车联" and r1["depts"] == ["AI 研究院"] and r1["type"] == "cursor"
    assert r1["masked"] == "sk-…" + PLAIN[1][-4:] and r1["kinds"] == ["api_key"]
    assert d["items"][3]["kinds"] == [] and d["items"][3]["secret_state"] == "hash_only"
    blob = json.dumps(d, ensure_ascii=False)
    assert not any(k in blob for k in PLAIN.values()), "列表里不能有明文(含 status_raw 里混进的)"
    assert "sha256" not in blob, "网关哈希也不出给浏览器"


def test_search_filters_are_forwarded(ap):
    sid = ap.sid("A")
    q = urllib.parse.urlencode({"q": "张", "org": "无锡车联", "dept": "AI", "status": "active", "type": "cursor",
                                "page": 2, "size": 20})
    assert aget(ap, "/ai/api/admin/accounts?" + q, sid)[0] == 200
    sent = [c[2] for c in STORE.calls if c[1] == "/v1/accounts"][-1]
    assert sent == {"q": "张", "org": "无锡车联", "dept": "AI", "status": "active", "type": "cursor",
                    "limit": "20", "offset": "20", "order": "applied_desc"}


@pytest.mark.parametrize("qs", ["status=deleted", "type=x", "size=7", "page=abc", "q=" + "x" * 101, "page=999999"])
def test_bad_filters_400(ap, qs):
    sid = ap.sid("A")
    assert aget(ap, "/ai/api/admin/accounts?" + qs, sid)[0] == 400
    assert aget(ap, "/ai/api/admin/accounts?status=active", sid)[0] == 200   # POSITIVE


def test_old_store_without_total_is_502_not_wrong_results(ap):
    """老账户库不认 q/offset:回整表 ⇒ 当成搜索结果给出去就是错的。宁可报错。"""
    STORE.old_version = True
    s, d, _, _ = aget(ap, "/ai/api/admin/accounts?q=" + urllib.parse.quote("张"), ap.sid("A"))
    assert s == 502 and "版本" in d["message"]


def test_list_rate_limited(admin_factory):
    p = admin_factory(ADMIN_LIST_PER_MIN="3")
    sid = p.sid("A")
    assert [aget(p, "/ai/api/admin/accounts", sid)[0] for _ in range(4)] == [200, 200, 200, 429]
    assert aget(p, "/ai/api/admin/stats", sid)[0] == 429, "列表 / 统计 / 审计共用一个桶"
    p.clock.t += 61
    assert aget(p, "/ai/api/admin/accounts", sid)[0] == 200             # POSITIVE:窗口过了


def test_stats(ap):
    s, d, _, _ = aget(ap, "/ai/api/admin/stats", ap.sid("A"))
    assert s == 200 and d["accounts"] == 4 and d["applied_7d"] == 2 and d["by_org"][0] == ["无锡车联", 2]
    assert "kek_id" not in d


def test_audit_view_is_masked(ap):
    s, d, _, _ = aget(ap, "/ai/api/admin/audit", ap.sid("A"))
    assert s == 200 and len(d["items"]) == 3 and d["items"][0]["owner_name"] == "张三"
    assert PLAIN[1] not in json.dumps(d, ensure_ascii=False) and "detail" not in d["items"][0]
    assert [c[2] for c in STORE.calls if c[1] == "/v1/audit"][-1] == {"limit": "50", "action_prefix": "reveal"}
    aget(ap, "/ai/api/admin/audit?scope=all&before_id=7", ap.sid("A"))
    assert [c[2] for c in STORE.calls if c[1] == "/v1/audit"][-1] == {"limit": "50", "before_id": "7"}
    assert aget(ap, "/ai/api/admin/audit?scope=x", ap.sid("A"))[0] == 400


def test_gateway_blocked_column(admin_factory, lite):
    """「网关」列按 sha256 查 /key/info:正常 / 已封禁 / 网关上没有 / 库里没 key。"""
    p = admin_factory(LITELLM_BASE=lite, LITELLM_MASTER_KEY=T.MASTER)
    T.LITE.reset()
    T.LITE.add(PLAIN[1], "cursor-zhangsan-ab12")
    T.LITE.add(PLAIN[2], "cursor-lisi-cd34", blocked=True)
    s, d, _, _ = aget(p, "/ai/api/admin/accounts", p.sid("A"))
    assert s == 200 and {r["id"]: r["gw"] for r in d["items"]} == {1: "ok", 2: "blocked", 3: "missing", 4: "nokey"}
    assert all(c[1].startswith("/pro/key/info") for c in T.LITE.calls)
    off = admin_factory(LITELLM_BASE=lite, LITELLM_MASTER_KEY=T.MASTER, ADMIN_GW_CHECK="0")
    assert {r["gw"] for r in aget(off, "/ai/api/admin/accounts", off.sid("A"))[1]["items"]} == {"unchecked"}


def test_store_token_problems_close_admin_only(admin_factory, tmp_path):
    """token 文件权限过宽 / 不存在 ⇒ 后台 503,门户其余功能照常。"""
    p = admin_factory(ACCOUNT_STORE_TOKEN_FILE=token_file(tmp_path, mode=0o644, name="wide.token"))
    sid = p.sid("A")
    assert aget(p, "/ai/api/admin/accounts", sid)[0] == 503
    assert aget(p, "/ai/api/admin/me", sid)[1]["store_ok"] is False
    assert p.req("GET", "/ai/api/keys", cookie=sid)[0] == 200
    p2 = admin_factory(ACCOUNT_STORE_TOKEN_FILE=str(tmp_path / "nope"))
    assert aget(p2, "/ai/api/admin/accounts", p2.sid("A"))[0] == 503
    p3 = admin_factory()                                                # POSITIVE:0600
    assert aget(p3, "/ai/api/admin/accounts", p3.sid("A"))[0] == 200


def test_store_rejects_token_is_502_without_leaking(admin_factory, tmp_path):
    p = admin_factory(ACCOUNT_STORE_TOKEN_FILE=token_file(tmp_path, content="ast_wrong-token-000000000000000", name="wrong.token"))
    s, d, _, _ = aget(p, "/ai/api/admin/accounts", p.sid("A"))
    assert s == 502 and "token" in d["message"] and STORE_TOKEN not in json.dumps(d)


# ════════════════════════════════════════════════════════════════════════
# 4. 显示明文:必须填原因;每次都审计;限流
# ════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("reason", [None, "", "   ", "a", 123, "\n\t"])
def test_reveal_without_reason_rejected(ap, reason):
    sid = ap.sid("A")
    s, d, _, _ = reveal(ap, sid, reason=reason)
    assert s == 400 and "原因" in d["message"]
    assert not STORE.reveals, "没原因不能去账户库取"
    assert local_audit(ap, "reveal")[-1][4] == "denied:no-reason"
    s, d, _, _ = reveal(ap, sid, reason="用户来要")                     # POSITIVE
    assert s == 200 and d["key"] == PLAIN[1]


def test_reveal_bad_input(ap):
    sid = ap.sid("A")
    for body in ({"account_id": "1", "reason": "原因原因"}, {"account_id": True, "reason": "原因原因"},
                 {"account_id": 0, "reason": "原因原因"}, {"account_id": 1, "kind": "x", "reason": "原因原因"},
                 {"account_id": 1, "reason": "x" * 201}):
        assert apost(ap, "/ai/api/admin/reveal", body, sid)[0] == 400, body
    assert not STORE.reveals


def test_every_reveal_is_audited_both_sides(ap):
    sid = ap.sid("A")
    for i, aid in enumerate((1, 2, 1)):
        s, d, _, _ = reveal(ap, sid, aid=aid, reason="第%d次 排查" % i)
        assert s == 200 and d["key"] == PLAIN[aid] and d["remask_after"] == 30
    rows = [r for r in local_audit(ap, "reveal") if r[4] == "ok"]
    assert [(r[0], r[2], r[3]) for r in rows] == [(ADMIN, 1, "第0次 排查"), (ADMIN, 2, "第1次 排查"), (ADMIN, 1, "第2次 排查")]
    att = [r for r in local_audit(ap, "reveal") if r[4] == "attempt"]
    assert len(att) == 3, "取之前先落一行 attempt"
    assert [r[0] for r in STORE.reveals] == [1, 2, 1]
    assert all(a == "admin-web:张三(%s)" % ADMIN and r.startswith("第") for _, a, r in STORE.reveals), \
        "账户库那边的审计 actor 要能认出是谁(姓名 + union_id),原因原样带过去"


def test_reveal_store_failure_is_audited(ap):
    sid = ap.sid("A")
    s, _, _, _ = reveal(ap, sid, aid=99)
    assert s == 404 and local_audit(ap, "reveal")[-1][4] == "fail:404"
    STORE.fail = (409, {"error": "这把 key 在库里是 hash_only,取不回(请重置)"})
    s, d, _, _ = reveal(ap, sid, aid=4)
    assert s == 409 and "hash_only" in d["message"] and local_audit(ap, "reveal")[-1][4] == "fail:409"


def test_reveal_rate_limited(admin_factory):
    p = admin_factory(ADMIN_REVEAL_PER_MIN="2")
    sid = p.sid("A")
    assert [reveal(p, sid)[0] for _ in range(3)] == [200, 200, 429]
    assert local_audit(p, "reveal")[-1][4] == "denied:rate" and len(STORE.reveals) == 2


def test_plaintext_only_on_reveal(ap):
    """明文只出现在 reveal 的回包里:其他接口 + 门户日志都没有。"""
    sid = ap.sid("A")
    bodies = [json.dumps(aget(ap, p, sid)[1], ensure_ascii=False) for p in GETS[:4]]
    assert not any(k in b for b in bodies for k in PLAIN.values())


def test_logs_never_contain_plaintext_or_store_token(admin_factory, capsys, tmp_path):
    p = admin_factory()
    sid = p.sid("A")
    reveal(p, sid)
    aget(p, "/ai/api/admin/export.csv", sid)
    aget(p, "/ai/api/admin/accounts", p.sid("B"))
    p2 = admin_factory(ACCOUNT_STORE_TOKEN_FILE=token_file(tmp_path, content="ast_wrong-token-000000000000000", name="wrong.token"))
    aget(p2, "/ai/api/admin/accounts", p2.sid("A"))
    out = capsys.readouterr()
    logs = out.out + out.err
    assert "管理后台 reveal" in logs                                     # POSITIVE:确实抓到了日志
    assert not any(k in logs for k in PLAIN.values()) and STORE_TOKEN not in logs and "wrong-token" not in logs


# ════════════════════════════════════════════════════════════════════════
# 5. 导出 CSV:不含明文、不含 sk-
# ════════════════════════════════════════════════════════════════════════
def export(p, sid, qs=""):
    c = T.http.client.HTTPConnection("127.0.0.1", p.port, timeout=10)
    c.request("GET", "/ai/api/admin/export.csv" + qs, headers={"Cookie": sid, **XRW})
    r = c.getresponse()
    b = r.read()
    c.close()
    return r.status, dict((k.lower(), v) for k, v in r.getheaders()), b


def test_export_has_no_sk_and_no_plaintext(ap):
    s, h, b = export(ap, ap.sid("A"))
    assert s == 200 and h["content-type"].startswith("text/csv") and "attachment" in h["content-disposition"]
    t = b.decode("utf-8-sig")
    assert "sk-" not in t and not any(k[3:] in t for k in PLAIN.values())
    rows = list(csv.reader(io.StringIO(t)))
    assert rows[0][0] == "账户ID" and len(rows) == 5
    r1 = dict(zip(rows[0], rows[1]))
    assert r1["姓名"] == "张三" and r1["key(末4位)"] == "…" + PLAIN[1][-4:] and r1["部门"] == "AI 研究院"
    evil = dict(zip(rows[0], rows[3]))
    assert evil["姓名"].startswith("'="), "CSV 公式注入要中和"
    assert local_audit(ap, "export")[-1][4] == "ok:4"


def test_export_respects_filters_and_pages_through_store(ap):
    s, _, b = export(ap, ap.sid("A"), "?org=" + urllib.parse.quote("无锡车联"))
    assert s == 200 and len(list(csv.reader(io.StringIO(b.decode("utf-8-sig"))))) == 3
    sent = [c[2] for c in STORE.calls if c[1] == "/v1/accounts"][-1]
    assert sent["limit"] == "1000" and sent["offset"] == "0" and sent["org"] == "无锡车联"


def test_export_fails_closed_if_sk_survives(ap, monkeypatch):
    """UNGUARDED 的反面:单元格清洗失效时,最后那道 sk- 检查要把整份导出拦下来。"""
    monkeypatch.setattr(S.adm.Admin, "_cell", staticmethod(lambda v: "sk-leak"))
    s, _, b = export(ap, ap.sid("A"))
    assert s == 500 and b"sk-" not in b and local_audit(ap, "export")[-1][4] == "blocked:sk"


def test_export_denied_for_non_admin_and_rate_limited(admin_factory):
    p = admin_factory(ADMIN_EXPORT_PER_MIN="1")
    assert export(p, p.sid("B"))[0] == 403
    sid = p.sid("A")
    assert [export(p, sid)[0] for _ in range(2)] == [200, 429]


# ════════════════════════════════════════════════════════════════════════
# 6. 页面:CSP 和门户一样严;不在 static/ 下(不能绕过闸门直接取)
# ════════════════════════════════════════════════════════════════════════
REAL_ADMIN = Path(__file__).resolve().parent / "admin" / "admin.html"


def test_admin_page_csp_is_strict(ap):
    import base64
    c = T.http.client.HTTPConnection("127.0.0.1", ap.port, timeout=5)
    c.request("GET", "/ai/admin/", headers={"Cookie": ap.sid("A")})
    r = c.getresponse()
    s, h, b = r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    c.close()
    assert s == 200
    csp = T.parse_csp(h["content-security-policy"])
    for d, want in (("default-src", ["'none'"]), ("frame-ancestors", ["'none'"]), ("base-uri", ["'none'"]),
                    ("connect-src", ["'self'"])):
        assert csp[d] == want, d
    for tag, d in (("script", "script-src"), ("style", "style-src")):
        blocks = re.findall(rb"<%s>(.*?)</%s>" % (tag.encode(), tag.encode()), b, re.S)
        want = {"'sha256-%s'" % base64.b64encode(hashlib.sha256(x).digest()).decode() for x in blocks}
        assert blocks and want == set(csp[d]) and "'unsafe-inline'" not in csp[d]
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer" and h["cache-control"] == "no-store"


def test_admin_html_not_reachable_as_static(ap):
    sid = ap.sid("B")
    for path in ("/ai/admin/admin.html", "/ai/admin.html", "/ai/admin/index.html", "/ai/../admin/admin.html"):
        s, body, _, _ = ap.req("GET", path, cookie=sid)
        assert s in (403, 404) and "账户库后台" not in str(body), path


def test_admin_html_has_no_inline_attrs_or_external_resources():
    t = REAL_ADMIN.read_text(encoding="utf-8")
    assert not re.search(r"<(link|script|img|iframe)\b[^>]*\b(href|src)=\"https?://", t)
    assert not re.search(r"<link\b", t) and "<script " not in t and "<style " not in t
    markup = re.sub(r"<script>.*?</script>|<style>.*?</style>", "", t, flags=re.S)
    assert not re.search(r"<[a-z][^>]*\sstyle=\"", markup) and not re.search(r"<[a-z][^>]*\son[a-z]+=", markup)
    assert t.count("<script>") == 1 and t.count("<style>") == 1
    js = re.search(r"<script>(.*?)</script>", t, re.S).group(1)
    assert "innerHTML" not in js and "console." not in js, "明文/用户数据只走 textContent,不进 console"
    assert "X-Requested-With\":\"ai-admin\"" in js and "REMASK_MS = 30000" in js
    assert "localStorage" in js and js.count("localStorage.") == 2, "localStorage 只存主题"
    # 复用门户的 CSS 变量
    idx = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    for var in ("--bg:#f4f6fb", "--surface:#ffffff", "--accent:#2f5bd3", "--bg:#0f141b"):   # 11-05 配色微调,两页同步
        assert var in idx and var in t, var


def test_portal_other_routes_unaffected(ap):
    """钩子只认 /admin、/admin/、/api/admin/*:门户原有接口照旧。"""
    sid = ap.sid("B")
    assert ap.req("GET", "/ai/api/me", cookie=sid)[0] == 200
    assert ap.req("GET", "/ai/api/keys", cookie=sid)[0] == 200
    assert ap.req("GET", "/ai/administrator", cookie=sid)[0] == 404
    assert ap.req("POST", "/ai/api/adminx", {}, cookie=sid)[0] == 404


# ════════════════════════════════════════════════════════════════════════
# 7. 联调:门户 → **真账户库进程**(scripts/account-store/server.py,临时库 + 临时主密钥)
#    证明 scope、actor、审计、搜索参数这几处约定两边真的对得上,而不只是对得上我的假账户库。
# ════════════════════════════════════════════════════════════════════════
AS_DIR = Path(__file__).resolve().parent.parent / "account-store"


@pytest.fixture
def real_store(tmp_path):
    pytest.importorskip("cryptography")
    import importlib.util, subprocess, socket, time as _t
    spec = importlib.util.spec_from_file_location("as_lib_for_admin_test", AS_DIR / "account_store.py")
    AS = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(AS)
    (tmp_path / "data").mkdir()
    (tmp_path / "kek").mkdir()
    db, kek = tmp_path / "data" / "a.db", tmp_path / "kek" / "kek.bin"
    AS.gen_kek(kek)
    k, kid = AS.load_kek(kek, db_path=db)
    conn = AS.connect(str(db))
    AS.init_schema(conn)
    ts = AS.now_iso()
    for i, name, org in ((1, "张三", "无锡车联"), (2, "李四", "上海车联")):
        conn.execute("INSERT INTO account(id, owner_name, org_name, status, account_type, key_alias, applied_at,"
                     " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (i, name, org, "active", "cursor", "cursor-u%d" % i, ts, ts, ts))
        sha, ct, wr = AS.encrypt_secret(k, kid, "api_key", PLAIN[i])
        conn.execute("INSERT INTO account_secret(account_id, kind, state, sha256, hint, key_len, ciphertext, wrapped_dek,"
                     " kek_id, created_at, updated_at) VALUES (?,'api_key','encrypted',?,?,?,?,?,?,?,?)",
                     (i, sha, AS.key_hint(PLAIN[i]), len(PLAIN[i]), ct, wr, kid, ts, ts))
    conn.close()
    tok = tmp_path / "tokens.json"
    tok.write_text(json.dumps({"clients": {
        "adminweb": {"sha256": hashlib.sha256(STORE_TOKEN.encode()).hexdigest(), "scopes": ["read", "reveal:any", "audit:read"]},
        "portal": {"sha256": "b" * 64, "scopes": ["read", "reveal:self"]}}}))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, ACCOUNT_STORE_DB=str(db), ACCOUNT_STORE_KEK=str(kek), ACCOUNT_STORE_TOKENS=str(tok),
               LISTEN="127.0.0.1:%d" % port)
    proc = subprocess.Popen([sys.executable, str(AS_DIR / "server.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            _t.sleep(0.05)
    yield {"url": "http://127.0.0.1:%d" % port, "db": db, "proc": proc}
    proc.terminate()
    out = proc.communicate(timeout=5)[0]
    assert not any(k in out for k in PLAIN.values()), "账户库日志里不能有明文"


def test_e2e_with_real_account_store(fake, tmp_path, clock, real_store):
    T.FAKE.reset()
    T.FAKE.tables[T.KEYTBL] = T.seed()
    cfg = T.make_cfg(fake, tmp_path, ADMIN_UNION_IDS=ADMIN, ACCOUNT_STORE_URL=real_store["url"],
                     ACCOUNT_STORE_TOKEN_FILE=token_file(tmp_path))
    p = T.Portal(fake, cfg, clock)
    try:
        sid = p.sid("A")
        s, d, _, _ = aget(p, "/ai/api/admin/accounts?q=" + urllib.parse.quote("张"), sid)
        assert s == 200 and d["total"] == 1 and d["items"][0]["name"] == "张三"
        assert aget(p, "/ai/api/admin/stats", sid)[1]["applied_7d"] == 2
        s, d, _, _ = reveal(p, sid, aid=2, reason="联调:本人来要")
        assert s == 200 and d["key"] == PLAIN[2]
        assert reveal(p, sid, aid=2, reason="")[0] == 400
        s, d, _, _ = aget(p, "/ai/api/admin/audit", sid)
        top = d["items"][0]
        assert s == 200 and top["action"] == "reveal" and top["reason"] == "联调:本人来要" and top["owner_name"] == "李四"
        assert top["actor"] == "adminweb:admin-web:张三(%s)" % ADMIN
        assert len([i for i in d["items"] if i["action"] == "reveal"]) == 1, "没原因那次不能到账户库"
        s, _, b = export(p, sid)
        assert s == 200 and b"sk-" not in b
        assert aget(p, "/ai/api/admin/accounts", p.sid("B"))[0] == 403          # 非管理员
    finally:
        p.close()
