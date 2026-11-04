"""POST /v1/accounts(发 key)的离线测试:真账户库服务 + 假 198 网关,全程不碰线上。

跑法:python3 -m pytest scripts/account-store/test_issue.py -q    (需要 cryptography;等价性那条需要 node)

拒绝类用例都带阳性对照(同一请求只改掉被拒的那一点 ⇒ 必须成功),防「桩不完整导致的假绿」。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import account_store as AS  # noqa: E402
import acct_issue as ISS  # noqa: E402
import server as S  # noqa: E402

POLLER = HERE.parent / "hermestest-cron" / "litellm-key-poller.js"
MASTER = "mk-test-" + "m" * 24          # 假 master key
TOK = {"portal": "tok-portal-" + "p" * 20, "poller": "tok-poller-" + "q" * 20,
       "report": "tok-report-" + "r" * 20, "admin": "tok-admin-" + "a" * 20}
SCOPES = {"portal": ["read", "reveal:self", "issue"], "poller": ["issue", "reveal:issued"],
          "report": ["read"], "admin": ["read", "reveal:any"]}
UID_N, UID_M, UID_Q = "on_" + "n" * 32, "on_" + "m" * 32, "on_" + "q" * 32

# 参照 key:形状照 198 上 cursor-wangzhongwei-w1qn(个人字段故意填满,用来证明一个都不会漏进新 key)
REFERENCE = {
    "key_alias": ISS.REFERENCE_ALIAS, "blocked": None, "token": "f" * 64,
    "models": ["gpt-5.5", "claude-haiku-4-5", "deepseek-v4-pro", "kimi-k3"],
    "aliases": {"claude-4.5-haiku": "claude-haiku-4-5"},
    "max_budget": 20.0, "budget_duration": "1d", "team_id": "team-of-wangzhongwei", "user_id": "cursor-wangzhongwei",
    "spend": 3.21, "tpm_limit": None,
    "metadata": {
        "budget_family_overrides": {"premium": {"max_budget": 5, "models": ["claude-opus-5-5"]}},
        "allowed_passthrough_routes": ["/v1/messages"], "tag_rpm_limit": {"cursor": 60},
        "enable_prompt_caching": True, "throttle_on_budget_exceeded": False,
        # ↓ 参照人自己的身份:绝不能出现在新 key 上
        "email": "wangzhongwei@auto-link.com.cn", "lark_open_id": "ou_wangzhongwei", "display_name": "王忠伟",
        "job_title": "工程师", "department": "参照人的部门", "source_record_id": "recREFERENCE",
        "owner_name": "wangzhongwei", "base_alias": "cursor-wangzhongwei", "secret_note": "参照人私有",
    },
}
TEAMS = [
    {"team_id": "t-old", "team_alias": "AI 工程部", "created_at": "2026-06-12T00:00:00", "models": []},
    {"team_id": "t-new", "team_alias": "AI 工程部", "created_at": "2026-10-20T00:00:00", "models": []},
    {"team_id": "t-budget", "team_alias": "AI 工程部", "created_at": "2026-11-01T00:00:00", "max_budget": 999999},
    {"team_id": "t-models", "team_alias": "AI 工程部", "created_at": "2026-11-02T00:00:00", "models": ["x"]},
    {"team_id": "t-blocked", "team_alias": "AI 工程部", "created_at": "2026-11-03T00:00:00", "blocked": True},
    {"team_id": "t-baic", "team_alias": "北汽集团", "created_at": "2026-09-01T00:00:00"},
]
APPLICANT = {"union_id": UID_N, "open_id": "ou_applicant01", "name": "陈一圣", "email": "chenyisheng@auto-link.com.cn",
             "email_prefix": "chenyisheng", "tenant_key": "1088406f1c155740", "department": "AI 工程部",
             "organization": "无锡车联", "job_title": "算法工程师", "record_id": "recFORM0001",
             "actor": "poller", "source": "test"}


# --------------------------------------------------------------------------- 假 198 网关


class GW:
    def reset(self):
        self.calls = []            # (method, path, body)
        self.keys = {}             # alias -> {"plain", "body"}
        self.n = 0
        self.fail = {}             # path 片段 -> (status, body)
        self.gen_delay = 0.0
        self.reference = json.loads(json.dumps(REFERENCE))
        self.teams = json.loads(json.dumps(TEAMS))
        self.drop_after_create = False   # 建出来了但连接断掉(模拟超时)


G = GW()
G.reset()


class FakeGW(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self):
        if (self.headers.get("Authorization") or "") != "Bearer " + MASTER:
            self.out(401, {"error": {"message": "Invalid proxy server token passed"}})
            return False
        return True

    def _fail(self, path):
        for frag, (st, body) in G.fail.items():
            if frag in path:
                self.out(st, body)
                return True
        return False

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        G.calls.append(("GET", u.path, q))
        if not self._auth() or self._fail(u.path):
            return
        if u.path == "/pro/key/list":
            keys = [G.reference] if q.get("key_alias") == G.reference.get("key_alias") else []
            return self.out(200, {"keys": keys, "total_pages": 1})
        if u.path == "/pro/team/list":
            return self.out(200, G.teams)
        self.out(404, {"error": {"message": "no route"}})

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        G.calls.append(("POST", u.path, b))
        if not self._auth() or self._fail(u.path):
            return
        if u.path == "/pro/key/generate":
            if G.gen_delay:
                time.sleep(G.gen_delay)
            if b.get("key_alias") in G.keys:
                return self.out(400, {"error": {"message": "Unique key aliases across all keys are required."}})
            G.n += 1
            plain = "sk-gw%04d" % G.n + hashlib.sha256(str(G.n).encode()).hexdigest()[:24]
            G.keys[b["key_alias"]] = {"plain": plain, "body": b}
            if G.drop_after_create:
                self.connection.close()       # 建出来了,但调用方拿不到回包
                return
            return self.out(200, {"key": plain, "key_alias": b["key_alias"]})
        if u.path == "/pro/key/delete":
            gone = [a for a in b.get("key_aliases") or [] if G.keys.pop(a, None)]
            return self.out(200, {"deleted_keys": gone})
        self.out(404, {"error": {"message": "no route"}})


@pytest.fixture(scope="module")
def gw():
    s = ThreadingHTTPServer(("127.0.0.1", 0), FakeGW)
    s.daemon_threads = True
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}/pro"
    s.shutdown()


# --------------------------------------------------------------------------- 账户库服务


def _mkstore(d: Path, gw_url: str, with_key=True):
    (d / "data").mkdir()
    (d / "kek").mkdir()
    (d / "gw").mkdir()
    db, kek, tok = d / "data/accounts.db", d / "kek/kek.bin", d / "tokens.json"
    AS.gen_kek(kek)
    tok.write_text(json.dumps({"clients": {n: {"sha256": hashlib.sha256(t.encode()).hexdigest(), "scopes": SCOPES[n]}
                                           for n, t in TOK.items()}}))
    keyf = d / "gw/master_key"
    if with_key:
        keyf.write_text(MASTER + "\n")
    store = S.Store(str(db), str(kek), str(tok))
    store.gateway = ISS.Gateway(gw_url, str(keyf), timeout=5, backoff=(0, 0))
    return store, db


@pytest.fixture
def svc(gw, tmp_path):
    G.reset()
    store, db = _mkstore(tmp_path, gw)
    S.Handler.store = store
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{httpd.server_port}", "db": db, "store": store}
    httpd.shutdown()


def call(svc, method, path, token=None, body=None):
    req = urllib.request.Request(svc["base"] + path, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def audits(svc, prefix="issue"):
    c = sqlite3.connect(svc["db"])
    rows = c.execute("SELECT action, actor, account_id, reason, detail FROM audit_log WHERE action LIKE ? ORDER BY id",
                     (prefix + "%",)).fetchall()
    c.close()
    return rows


def gens():
    return [c for c in G.calls if c[1].endswith("/key/generate")]


def issue(svc, who="poller", **over):
    return call(svc, "POST", "/v1/accounts", TOK[who], {**APPLICANT, **over})


# --------------------------------------------------------------------------- 1. 鉴权


def test_issue_needs_issue_scope(svc):
    for who in ("report", "admin"):                     # 有 read / reveal:any,没有 issue
        st, j = issue(svc, who)
        assert st == 403 and "issue" in j["error"], (who, st, j)
    assert call(svc, "POST", "/v1/accounts", None, APPLICANT)[0] == 401
    assert call(svc, "POST", "/v1/accounts", "wrong-token", APPLICANT)[0] == 401
    assert not gens(), "没权限的请求不许碰网关"
    # 阳性对照:同一个 body 换成有 issue 的 token ⇒ 成功
    st, j = issue(svc, "poller")
    assert st == 200 and j["account_id"] and len(gens()) == 1


def test_link_needs_issue_scope(svc):
    st, j = issue(svc, record_id="")
    assert st == 200
    assert call(svc, "POST", f"/v1/accounts/{j['account_id']}/link", TOK["report"], {"record_id": "recX1"})[0] == 403


def test_bad_inputs_rejected_before_gateway(svc):
    for over in ({"union_id": "ou_not_union"}, {"union_id": ""}, {"actor": ""}, {"record_id": "not-a-rec"},
                 {"email_prefix": "has space"}, {"name": 123}, {"account_type": "Claude Code"},
                 {"department": "x" * 300}):
        st, j = issue(svc, **over)
        assert st == 400, (over, st, j)
    assert not gens()
    assert len(audits(svc, "issue.fail")) == 8, "参数错也要进审计"


# --------------------------------------------------------------------------- 2. 生成 + 明文不出接口


def test_issue_ok_returns_no_plaintext_and_encrypts(svc):
    st, j = issue(svc)
    assert st == 200 and j["existing"] is False
    alias = j["key_alias"]
    plain = G.keys[alias]["plain"]
    assert plain not in json.dumps(j) and "key" not in j
    assert j["masked"] == "sk-…" + plain[-4:]
    assert alias.startswith("cursor-chenyisheng-") and len(alias) == len("cursor-chenyisheng-") + 4
    c = sqlite3.connect(svc["db"])
    c.row_factory = sqlite3.Row
    a = c.execute("SELECT * FROM account WHERE id=?", (j["account_id"],)).fetchone()
    s = c.execute("SELECT * FROM account_secret WHERE account_id=?", (j["account_id"],)).fetchone()
    raw = open(svc["db"], "rb").read() + (Path(str(svc["db"]) + "-wal").read_bytes()
                                          if Path(str(svc["db"]) + "-wal").exists() else b"")
    c.close()
    assert (a["owner_union_id"], a["account_type"], a["status"], a["key_alias"], a["lark_record_id"]) == \
           (UID_N, "cursor", "active", alias, "recFORM0001")
    assert json.loads(a["dept_names"]) == ["AI 工程部"] and a["org_name"] == "无锡车联"
    assert s["state"] == "encrypted" and s["sha256"] == AS.sha256_hex(plain)
    assert plain.encode() not in raw, "库文件里不能有明文"
    st, stats = call(svc, "GET", "/v1/accounts?owner_union_id=" + UID_N, TOK["report"])
    assert plain not in json.dumps(stats)


def test_reveal_self_works_for_just_issued_key(svc):
    """门户:申请成功后立刻给本人看 —— reveal:self + on_behalf_of=本人 能取到刚生成那把。"""
    st, j = issue(svc, "portal", actor=UID_N)
    plain = G.keys[j["key_alias"]]["plain"]
    st, r = call(svc, "POST", f"/v1/accounts/{j['account_id']}/reveal", TOK["portal"],
                 {"reason": "申请成功当场显示", "actor": UID_N, "on_behalf_of": UID_N})
    assert st == 200 and r["key"] == plain
    st, _ = call(svc, "POST", f"/v1/accounts/{j['account_id']}/reveal", TOK["portal"],
                 {"reason": "x", "actor": UID_M, "on_behalf_of": UID_M})
    assert st == 403, "别人不能取"
    assert [a[0] for a in audits(svc, "reveal")] == ["reveal", "reveal.denied"]


def test_reveal_issued_only_for_rows_this_client_issued(svc):
    """poller(reveal:issued):只能取自己刚发 / 刚复用的行,别的行 403;过了窗口也 403。"""
    st, j = issue(svc, "poller")
    plain = G.keys[j["key_alias"]]["plain"]
    body = {"reason": "写回飞书表", "actor": "poller"}
    assert call(svc, "POST", f"/v1/accounts/{j['account_id']}/reveal", TOK["poller"], body) == \
           (200, {"account_id": j["account_id"], "kind": "api_key", "hint": j["masked"], "key": plain})
    st2, j2 = issue(svc, "portal", union_id=UID_M, record_id="", email_prefix="other", actor=UID_M)
    assert st2 == 200
    assert call(svc, "POST", f"/v1/accounts/{j2['account_id']}/reveal", TOK["poller"], body)[0] == 403, \
        "门户发的行 poller 取不到"
    old = ISS.ISSUED_REVEAL_WINDOW
    try:
        ISS.ISSUED_REVEAL_WINDOW = -1                 # 窗口过了
        assert call(svc, "POST", f"/v1/accounts/{j['account_id']}/reveal", TOK["poller"], body)[0] == 403
    finally:
        ISS.ISSUED_REVEAL_WINDOW = old


# --------------------------------------------------------------------------- 3. 幂等 / 去重


def test_idempotent_existing_cursor_key_returned(svc):
    st, j1 = issue(svc)
    st, j2 = issue(svc, record_id="recFORM0002")
    assert st == 200 and j2["existing"] is True and j2["account_id"] == j1["account_id"]
    assert j2["key_alias"] == j1["key_alias"] and j2["masked"] == j1["masked"]
    assert len(gens()) == 1, "第二次不许再调网关"
    assert [a[0] for a in audits(svc)] == ["issue.ok", "issue.existing"]


def test_dedup_rules_match_current_key_semantics(svc):
    """已禁用 / 客户批量 / 只有 hash 的行都不算「当前可用」⇒ 照常发;有效的那把才算。"""
    conn = svc["store"].conn()
    ts = AS.now_iso()
    kek, kid = svc["store"].kek, svc["store"].kek_id
    conn.execute("INSERT INTO person(union_id, name, updated_at) VALUES (?,?,?)", (UID_N, "陈一圣", ts))

    def add(status, org=None, applied=None, state="encrypted", typ="cursor"):
        cur = conn.execute("INSERT INTO account(owner_union_id, account_type, status, org_name, key_alias, applied_at,"
                           " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                           (UID_N, typ, status, org, "cursor-old", applied, ts, ts))
        if state == "encrypted":
            sha, ct, wr = AS.encrypt_secret(kek, kid, "api_key", "sk-old%08d" % cur.lastrowid + "x" * 20)
            conn.execute("INSERT INTO account_secret(account_id, kind, state, sha256, hint, ciphertext, wrapped_dek,"
                         " kek_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (cur.lastrowid, "api_key", "encrypted", sha, "sk-…old1", ct, wr, kid, ts, ts))
        else:
            conn.execute("INSERT INTO account_secret(account_id, kind, state, sha256, created_at, updated_at)"
                         " VALUES (?,?,?,?,?,?)", (cur.lastrowid, "api_key", "hash_only", "0" * 64, ts, ts))
        return cur.lastrowid

    add("disabled")
    add("active", org="巧虎")
    add("active", state="hash_only")
    add("active", typ="claude_code")
    for i in range(5):
        add("active", applied="2026-10-01T00:00:%02d.000Z" % (i * 3))
    assert ISS.current_key(conn, UID_N) is None, "以上都不算当前可用"
    # 阳性对照:同样的库,再加一把单独的有效 Cursor key ⇒ current_key 认它,接口复用它、不调网关
    good = add("active", applied="2026-11-01T00:00:00.000Z")
    assert ISS.current_key(conn, UID_N)["id"] == good
    conn.close()
    st, j = issue(svc)
    assert st == 200 and j["existing"] is True and j["account_id"] == good and not gens()


def test_dedup_ignores_non_current_rows_and_generates(svc):
    conn = svc["store"].conn()
    ts = AS.now_iso()
    conn.execute("INSERT INTO person(union_id, name, updated_at) VALUES (?,?,?)", (UID_N, "陈一圣", ts))
    conn.execute("INSERT INTO account(owner_union_id, account_type, status, key_alias, created_at, updated_at)"
                 " VALUES (?,?,?,?,?,?)", (UID_N, "cursor", "disabled", "cursor-old", ts, ts))
    conn.close()
    st, j = issue(svc)
    assert st == 200 and j["existing"] is False and len(gens()) == 1


def test_concurrent_only_one_generates(svc):
    G.gen_delay = 0.6
    out = []

    def go():
        out.append(issue(svc))
    ts_ = [threading.Thread(target=go) for _ in range(6)]
    for t in ts_:
        t.start()
    for t in ts_:
        t.join()
    assert len(gens()) == 1, "并发 6 个请求只能有一次真生成"
    oks = [j for st, j in out if st == 200 and not j["existing"]]
    assert len(oks) == 1
    assert all(st == 409 and j["error"] == "in_progress" for st, j in out if not (st == 200)), out
    c = sqlite3.connect(svc["db"])
    assert c.execute("SELECT count(*) FROM account WHERE owner_union_id=?", (UID_N,)).fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM issue_lock").fetchone()[0] == 0, "锁要放掉"
    c.close()
    # 跑完以后再来一次 ⇒ existing,不是 409
    st, j = issue(svc)
    assert st == 200 and j["existing"] is True


def test_lock_released_after_failure(svc):
    G.fail["/key/generate"] = (400, {"error": {"message": "bad"}})
    assert issue(svc)[0] == 502
    G.fail.clear()
    st, j = issue(svc)
    assert st == 200 and j["existing"] is False


# --------------------------------------------------------------------------- 4. 失败路径 + 审计


def test_gateway_failures_audited_and_nothing_stored(svc):
    cases = [("/key/list", (500, {"error": {"message": "db down"}}), "gateway_error"),
             ("/key/list", (200, {"keys": []}), "reference_missing"),
             ("/team/list", (401, {"error": {"message": "nope"}}), "gateway_error"),
             ("/key/generate", (400, {"error": {"message": "budget invalid sk-LEAKLEAKLEAKLEAK1234"}}), "generate_failed")]
    for frag, resp, code in cases:
        G.fail = {frag: resp}
        st, j = issue(svc)
        assert st == 502 and j["error"] == code, (frag, st, j)
    G.fail = {}
    c = sqlite3.connect(svc["db"])
    assert c.execute("SELECT count(*) FROM account").fetchone()[0] == 0
    c.close()
    rows = audits(svc, "issue.fail")
    assert [r[3] for r in rows] == [c[2] for c in cases]
    assert "sk-LEAKLEAKLEAKLEAK1234" not in json.dumps(rows), "审计 detail 要打码"


def test_reference_without_budget_refused(svc):
    G.reference["max_budget"] = None
    st, j = issue(svc)
    assert st == 502 and j["error"] == "reference_bad" and not gens()


def test_timeout_after_create_cleans_up_on_gateway(svc):
    """网关建出来了但回包丢了 ⇒ 不重试(poller 会重试 ⇒ 多一把),按 alias 删掉,库里不留行。"""
    G.drop_after_create = True
    st, j = issue(svc)
    assert st == 502
    assert len(gens()) == 1, "不重试"
    assert G.keys == {}, "网关上那把要被删掉"
    rows = audits(svc, "issue.fail")
    assert json.loads(rows[-1][4])["cleanup"] is True


def test_no_gateway_key_is_503(gw, tmp_path):
    store, db = _mkstore(tmp_path, gw, with_key=False)
    st, j = ISS.issue(store, "poller", dict(APPLICANT))
    assert st == 503 and j["error"] == "gateway_not_configured"


def test_master_key_never_logged_or_returned(svc, capsys):
    capsys.readouterr()
    st, j = issue(svc)
    G.fail = {"/key/generate": (500, {"error": {"message": "boom"}})}
    issue(svc, union_id=UID_Q, record_id="")
    out = capsys.readouterr()
    logs = out.out + out.err + json.dumps(audits(svc, ""))
    plain = G.keys[j["key_alias"]]["plain"]
    assert "issue.ok" in logs, "阳性对照:确实打了日志"
    assert MASTER not in logs and plain not in logs


# --------------------------------------------------------------------------- 5. 迁移期:表单行已在库里 / link


def test_form_row_already_migrated_is_filled_in_place(svc):
    conn = svc["store"].conn()
    ts = AS.now_iso()
    conn.execute("INSERT INTO person(union_id, updated_at) VALUES (?,?)", (UID_N, ts))
    conn.execute("INSERT INTO account(lark_record_id, owner_union_id, status, created_at, updated_at)"
                 " VALUES ('recFORM0001', ?, 'unknown', ?, ?)", (UID_N, ts, ts))
    conn.close()
    st, j = issue(svc)
    c = sqlite3.connect(svc["db"])
    assert c.execute("SELECT count(*) FROM account").fetchone()[0] == 1
    assert c.execute("SELECT status FROM account WHERE lark_record_id='recFORM0001'").fetchone()[0] == "active"
    c.close()


def test_link_backfills_record_id_once(svc):
    st, j = issue(svc, "portal", record_id="", actor=UID_N)
    aid = j["account_id"]
    assert call(svc, "POST", f"/v1/accounts/{aid}/link", TOK["portal"], {"record_id": "recNEW0001", "actor": UID_N})[0] == 200
    assert call(svc, "POST", f"/v1/accounts/{aid}/link", TOK["portal"], {"record_id": "recNEW0001"})[0] == 200
    assert call(svc, "POST", f"/v1/accounts/{aid}/link", TOK["portal"], {"record_id": "recOTHER01"})[0] == 409
    assert call(svc, "POST", f"/v1/accounts/{aid}/link", TOK["poller"], {"record_id": "recNEW0001"})[0] in (200, 403)
    # 别的 client 发的行:poller 不能 link
    st, j2 = issue(svc, "portal", union_id=UID_M, record_id="", email_prefix="mm", actor=UID_M)
    assert call(svc, "POST", f"/v1/accounts/{j2['account_id']}/link", TOK["poller"], {"record_id": "recNEW0002"})[0] == 403


# --------------------------------------------------------------------------- 6. 和 poller generateKey 逐字段等价


def _poller_payload(now: int, prefix: str, ident: dict, reference: dict, teams) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node,跑不了 poller 等价性")
    r = subprocess.run([node, str(HERE / "poller_harness.js"), str(POLLER)], capture_output=True, text=True, timeout=60,
                       input=json.dumps({"now": now, "email_prefix": prefix, "identity": ident,
                                         "reference": reference, "teams": teams}))
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout)


CASES = [
    ("内部人·有部门 team", APPLICANT, REFERENCE, TEAMS),
    ("外部人·部门=公司", {**APPLICANT, "union_id": UID_M, "name": "金明超", "email": "", "email_prefix": "jinmingchao",
                     "department": "北汽集团", "organization": "北汽集团", "job_title": "", "open_id": "ou_ext01"},
     REFERENCE, TEAMS),
    ("没部门 ⇒ 不查 team、无 team_id", {**APPLICANT, "department": "", "organization": ""}, REFERENCE, TEAMS),
    ("部门没有纯分组 team", {**APPLICANT, "department": "不存在的部门"}, REFERENCE, TEAMS),
    ("参照 key 无 aliases、缺部分策略键",
     APPLICANT, {**REFERENCE, "aliases": {}, "metadata": {"tag_rpm_limit": {"cursor": 1}, "enable_prompt_caching": None,
                                                          "email": "ref@x"}}, TEAMS),
    ("team/list 回 {teams:[...]} 形状", APPLICANT, REFERENCE, {"teams": TEAMS}),
]


@pytest.mark.parametrize("name,app,ref,teams", CASES, ids=[c[0] for c in CASES])
def test_payload_equals_poller_generateKey(name, app, ref, teams):
    now = 1793800000000 + len(name) * 7919          # 同一时间 ⇒ 同一 4 位后缀
    ident_js = {"recordId": app["record_id"], "name": app["name"], "openId": app["open_id"], "email": app["email"],
                "jobTitle": app["job_title"], "department": app["department"], "organization": app["organization"]}
    js = _poller_payload(now, app["email_prefix"], ident_js, ref, teams)
    ts = teams if isinstance(teams, list) else teams["teams"]
    team_id = ISS.pick_team_id(ts, app["department"])
    ours = ISS.build_payload(ISS.check_reference(json.loads(json.dumps(ref))), app["email_prefix"],
                             {"record_id": app["record_id"], "name": app["name"], "open_id": app["open_id"],
                              "email": app["email"], "job_title": app["job_title"], "department": app["department"],
                              "organization": app["organization"]}, team_id, now)
    assert team_id == js["team_id"]
    theirs = js["payload"]
    assert sorted(ours) == sorted(theirs), (sorted(ours), sorted(theirs))
    for k in theirs:
        assert ours[k] == theirs[k], (name, k, ours[k], theirs[k])
    assert sorted(ours["metadata"]) == sorted(theirs["metadata"])
    for k in theirs["metadata"]:
        assert ours["metadata"][k] == theirs["metadata"][k], (name, "metadata." + k)
    # 参照人的个人字段一个都不在
    blob = json.dumps(ours, ensure_ascii=False)
    for leak in ("wangzhongwei@", "ou_wangzhongwei", "王忠伟", "参照人", "recREFERENCE", "team-of-wangzhongwei"):
        assert leak not in blob, leak


def test_server_payload_equals_poller_end_to_end(svc):
    """服务实际发到假网关的 body(走完 HTTP、读参照、查 team)== poller 同输入算出来的 body。"""
    now = 1793811111111
    old = ISS.now_ms
    ISS.now_ms = lambda: now
    try:
        st, j = issue(svc)
    finally:
        ISS.now_ms = old
    assert st == 200
    sent = gens()[0][2]
    js = _poller_payload(now, APPLICANT["email_prefix"],
                         {"recordId": APPLICANT["record_id"], "name": APPLICANT["name"], "openId": APPLICANT["open_id"],
                          "email": APPLICANT["email"], "jobTitle": APPLICANT["job_title"],
                          "department": APPLICANT["department"], "organization": APPLICANT["organization"]},
                         REFERENCE, TEAMS)["payload"]
    assert sent == js
    assert sent["team_id"] == "t-new" and sent["metadata"]["display_name"] == "陈一圣"
    Path(__file__).resolve().parents[2].joinpath(".work/account-store/issue").mkdir(parents=True, exist_ok=True)
    Path(__file__).resolve().parents[2].joinpath(".work/account-store/issue/equivalence-sample.json").write_text(
        json.dumps({"server_sent": sent, "poller_generateKey": js, "equal": sent == js}, ensure_ascii=False, indent=1))


def test_b36_matches_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node")
    ns = [0, 35, 36, 1793800000000, 1893456000000]
    js = subprocess.run([node, "-e", "console.log(JSON.stringify(%s.map(n=>n.toString(36))))" % json.dumps(ns)],
                        capture_output=True, text=True).stdout
    assert [ISS.b36(n) for n in ns] == json.loads(js)
