"""管理后台要用的三处只读扩展:GET /v1/audit(scope audit:read)、/v1/accounts 的 q/dept/offset/total、
/v1/stats 的近 7 天新发。起一个真服务(临时库 + 临时主密钥 + 假 key),走 HTTP 打。

拒绝类测试都配阳性对照:同一个请求只换成有权限的 token ⇒ 必须 200(证明 403 来自 scope,不是路由不存在)。
跑法:python3 -m pytest scripts/account-store/test_admin_read.py -q    (需要 cryptography)
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import account_store as AS  # noqa: E402
import server as S  # noqa: E402

FAKE = "sk-" + "B" * 44 + "Qq77"            # 假 key,从没在任何网关上存在过
TOK = {"portal": "tok-portal-" + "p" * 20, "admin": "tok-admin-" + "a" * 20,
       "adminweb": "tok-adminweb-" + "w" * 20, "report": "tok-report-" + "r" * 20}
SCOPES = {"portal": ["read", "reveal:self"], "admin": ["read", "reveal:any", "backup"],
          "adminweb": ["read", "reveal:any", "audit:read"], "report": ["read"]}


def _iso(sec_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - sec_ago)) + ".000Z"


@pytest.fixture(scope="module")
def svc(tmp_path_factory):
    d = tmp_path_factory.mktemp("acct-admin")
    (d / "data").mkdir()
    (d / "kek").mkdir()
    db, kek_path, tok_path = d / "data/accounts.db", d / "kek/kek.bin", d / "tokens.json"
    AS.gen_kek(kek_path)
    tok_path.write_text(json.dumps({"clients": {n: {"sha256": hashlib.sha256(t.encode()).hexdigest(),
                                                    "scopes": SCOPES[n]} for n, t in TOK.items()}}))
    kek, kek_id = AS.load_kek(kek_path, db_path=db)
    conn = AS.connect(str(db))
    AS.init_schema(conn)
    ts = AS.now_iso()
    rows = [  # id, owner, name, alias, org, dept_names, status, type, applied_at
        (1, "on_zhang001", "张三", "cursor-zhangsan-ab12", "无锡车联", '["AI 研究院"]', "active", "cursor", _iso(86400)),
        (2, "on_li000002", "李四", "cursor-lisi-cd34", "上海车联", '["智能座舱"]', "disabled", "cursor", _iso(30 * 86400)),
        (3, "on_wang0003", "王五", "cc-wangwu_x", "北汽集团", None, "active", "claude_code", _iso(2 * 86400)),
        (4, "on_zhao0004", "赵六", "cursor-zhao%liu", "无锡车联", '["AI 研究院","平台"]', "active", "cursor", None),
    ]
    for i, uid, name, alias, org, dept, st, typ, at in rows:
        conn.execute("INSERT INTO person(union_id, name, updated_at) VALUES (?,?,?)", (uid, name, ts))
        conn.execute("INSERT INTO account(id, lark_record_id, owner_union_id, owner_name, key_alias, org_name, dept_names,"
                     " status, account_type, applied_at, email_prefix, created_at, updated_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (i, f"rec{i}", uid, name, alias, org, dept, st, typ, at, f"user{i}", ts, ts))
    sha, ct, wr = AS.encrypt_secret(kek, kek_id, "api_key", FAKE)
    conn.execute("INSERT INTO account_secret(account_id, kind, state, sha256, sha256_prefix, hint, key_len, ciphertext,"
                 " wrapped_dek, kek_id, created_at, updated_at) VALUES (1,'api_key','encrypted',?,?,?,?,?,?,?,?,?)",
                 (sha, sha[:8], AS.key_hint(FAKE), len(FAKE), ct, wr, kek_id, ts, ts))
    # 一条 detail / reason 里混进明文的审计(模拟历史脏数据):读出来必须是打码的
    conn.execute("INSERT INTO audit_log(ts, actor, action, account_id, reason, detail) VALUES (?,?,?,?,?,?)",
                 (ts, "cli:root@37", "reveal", 1, "排查 " + FAKE, json.dumps({"k": FAKE})))
    conn.close()

    S.Handler.store = S.Store(str(db), str(kek_path), str(tok_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{httpd.server_port}", "db": db, "sha": sha}
    httpd.shutdown()


def call(svc, method, path, token=None, body=None):
    req = urllib.request.Request(svc["base"] + path, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ── /v1/audit 鉴权 ────────────────────────────────────────────────────────
def test_audit_read_no_token_401(svc):
    assert call(svc, "GET", "/v1/audit")[0] == 401
    assert call(svc, "GET", "/v1/audit", token="nope")[0] == 401
    assert call(svc, "GET", "/v1/audit", token=TOK["adminweb"])[0] == 200        # POSITIVE


@pytest.mark.parametrize("client", ["report", "portal", "admin"])
def test_audit_read_needs_audit_scope(svc, client):
    """read / reveal:self / reveal:any+backup 都不含 audit:read ⇒ 403。连 CLI 用的 admin token 也不行(最小权限)。"""
    code, j = call(svc, "GET", "/v1/audit", token=TOK[client])
    assert code == 403 and "audit:read" in j["error"]
    assert call(svc, "GET", "/v1/audit", token=TOK["adminweb"])[0] == 200        # POSITIVE


def test_audit_scope_does_not_grant_backup(svc):
    assert call(svc, "GET", "/v1/backup", token=TOK["adminweb"])[0] == 403


def test_audit_is_get_only(svc):
    assert call(svc, "POST", "/v1/audit", TOK["adminweb"], {})[0] == 404


def test_audit_shows_reveal_with_reason_and_owner_and_no_plaintext(svc):
    code, j = call(svc, "POST", "/v1/accounts/1/reveal", TOK["adminweb"], {"reason": "用户丢了 key", "actor": "admin-web:on_x"})
    assert code == 200 and j["key"] == FAKE
    code, j = call(svc, "GET", "/v1/audit?action=reveal", token=TOK["adminweb"])
    assert code == 200
    top = j["items"][0]
    assert top["action"] == "reveal" and top["actor"] == "adminweb:admin-web:on_x" and top["reason"] == "用户丢了 key"
    assert top["account_id"] == 1 and top["owner_name"] == "张三" and top["key_alias"] == "cursor-zhangsan-ab12"
    s = json.dumps(j, ensure_ascii=False)
    assert FAKE not in s and "sk-…Qq77" in s, "历史审计里混进的明文必须在读出时打码"
    assert [i["id"] for i in j["items"]] == sorted((i["id"] for i in j["items"]), reverse=True)


def test_audit_filters_and_paging(svc):
    for _ in range(3):
        call(svc, "POST", "/v1/accounts/1/reveal", TOK["adminweb"], {"reason": "翻页", "actor": "t"})
    code, j = call(svc, "GET", "/v1/audit?limit=2", token=TOK["adminweb"])
    assert code == 200 and len(j["items"]) == 2 and j["next_before_id"] == j["items"][-1]["id"]
    code, j2 = call(svc, "GET", f"/v1/audit?limit=2&before_id={j['next_before_id']}", token=TOK["adminweb"])
    assert code == 200 and all(i["id"] < j["next_before_id"] for i in j2["items"])
    code, j3 = call(svc, "GET", "/v1/audit?action_prefix=reveal&account_id=2", token=TOK["adminweb"])
    assert code == 200 and j3["items"] == []
    assert call(svc, "GET", "/v1/audit?account_id=abc", token=TOK["adminweb"])[0] == 400


def test_tokens_file_accepts_audit_scope(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"clients": {"x": {"sha256": "a" * 64, "scopes": ["read", "audit:read"]}}}))
    assert S.load_tokens(str(p))["x"]["scopes"] == {"read", "audit:read"}
    p.write_text(json.dumps({"clients": {"x": {"sha256": "a" * 64, "scopes": ["audit:write"]}}}))
    with pytest.raises(SystemExit):
        S.load_tokens(str(p))


# ── /v1/accounts 搜索 / 分页 ─────────────────────────────────────────────
def ids(j):
    return [i["id"] for i in j["items"]]


def test_accounts_fuzzy_search_and_total(svc):
    tok = TOK["report"]
    assert ids(call(svc, "GET", "/v1/accounts?q=" + urllib.request.quote("张"), tok)[1]) == [1]
    assert ids(call(svc, "GET", "/v1/accounts?q=lisi", tok)[1]) == [2]
    assert ids(call(svc, "GET", "/v1/accounts?q=user3", tok)[1]) == [3]          # 邮箱前缀
    j = call(svc, "GET", "/v1/accounts?dept=" + urllib.request.quote("AI 研究院"), tok)[1]
    assert ids(j) == [1, 4] and j["total"] == 2
    j = call(svc, "GET", "/v1/accounts?org=" + urllib.request.quote("无锡车联") + "&status=active&type=cursor", tok)[1]
    assert ids(j) == [1, 4]


def test_accounts_like_wildcards_are_literal(svc):
    """q 里的 % / _ 当普通字符:「%」只命中 alias 里真有 % 的那一行,不是全表。"""
    tok = TOK["report"]
    assert ids(call(svc, "GET", "/v1/accounts?q=%25", tok)[1]) == [4]
    assert ids(call(svc, "GET", "/v1/accounts?q=_x", tok)[1]) == [3]
    assert call(svc, "GET", "/v1/accounts?q=" + "x" * 101, tok)[0] == 400


def test_accounts_offset_and_order(svc):
    tok = TOK["report"]
    j = call(svc, "GET", "/v1/accounts?limit=2&offset=0&order=applied_desc", tok)[1]
    assert j["total"] == 4 and ids(j) == [1, 3]                                   # 申请时间新 → 旧
    j = call(svc, "GET", "/v1/accounts?limit=2&offset=2&order=applied_desc", tok)[1]
    assert ids(j) == [2, 4], "没申请时间的行排最后"
    assert ids(call(svc, "GET", "/v1/accounts", tok)[1]) == [1, 2, 3, 4]          # 默认仍按 id(老调用方不变)


def test_stats_has_recent_7d(svc):
    code, j = call(svc, "GET", "/v1/stats", token=TOK["report"])
    assert code == 200 and j["applied_7d"] == 2 and sum(n for _, n in j["applied_by_day_7d"]) == 2
    assert j["accounts"] == 4
