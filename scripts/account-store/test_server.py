"""账户库 HTTP 服务的离线测试:起一个真服务(临时库 + 临时主密钥 + 假 key),走 HTTP 打。

跑法:python3 -m pytest scripts/account-store/test_server.py -q    (需要 cryptography)
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import account_store as AS  # noqa: E402
import server as S  # noqa: E402

FAKE = "sk-" + "A" * 44 + "Zz09"            # 假 key,从没在任何网关上存在过
OWNER, OTHER = "on_owner0001", "on_other0002"
TOK = {"portal": "tok-portal-" + "p" * 20, "admin": "tok-admin-" + "a" * 20,
       "report": "tok-report-" + "r" * 20, "bak": "tok-bak-" + "b" * 20}
SCOPES = {"portal": ["read", "reveal:self"], "admin": ["read", "reveal:any", "backup"],
          "report": ["read"], "bak": ["backup"]}


@pytest.fixture(scope="module")
def svc(tmp_path_factory):
    d = tmp_path_factory.mktemp("acct")
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
    conn.execute("INSERT INTO person(union_id, name, updated_at) VALUES (?,?,?)", (OWNER, "主人", ts))
    conn.execute("INSERT INTO account(id, lark_record_id, owner_union_id, owner_name, status, key_alias, created_at, updated_at)"
                 " VALUES (1,'recA',?,'主人','active','cursor-owner',?,?)", (OWNER, ts, ts))
    conn.execute("INSERT INTO account(id, lark_record_id, status, created_at, updated_at) VALUES (2,'recB','active',?,?)", (ts, ts))
    sha, ct, wr = AS.encrypt_secret(kek, kek_id, "api_key", FAKE)
    conn.execute("INSERT INTO account_secret(account_id, kind, state, sha256, sha256_prefix, hint, key_len, ciphertext,"
                 " wrapped_dek, kek_id, created_at, updated_at) VALUES (1,'api_key','encrypted',?,?,?,?,?,?,?,?,?)",
                 (sha, sha[:8], AS.key_hint(FAKE), len(FAKE), ct, wr, kek_id, ts, ts))
    conn.execute("INSERT INTO account_secret(account_id, kind, state, sha256, hint, created_at, updated_at)"
                 " VALUES (2,'api_key','hash_only',?,'sk-…0000',?,?)", ("0" * 64, ts, ts))
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
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def audits(svc, action=None):
    c = sqlite3.connect(svc["db"])
    q = "SELECT action, actor, account_id, reason FROM audit_log" + (" WHERE action = ?" if action else "")
    rows = c.execute(q, (action,) if action else ()).fetchall()
    c.close()
    return rows


def test_healthz_no_auth(svc):
    assert call(svc, "GET", "/healthz") == (200, {"ok": True})


def test_no_token_and_wrong_token_401(svc):
    assert call(svc, "GET", "/v1/accounts")[0] == 401
    assert call(svc, "GET", "/v1/accounts", token="nope")[0] == 401


def test_list_never_contains_plaintext_or_ciphertext(svc):
    code, j = call(svc, "GET", "/v1/accounts", token=TOK["report"])
    assert code == 200 and len(j["items"]) == 2
    s = json.dumps(j)
    assert FAKE not in s and "ciphertext" not in s and "wrapped_dek" not in s
    assert j["items"][0]["secrets"][0]["hint"] == "sk-…Zz09"


def test_lookup_by_gateway_sha(svc):
    code, j = call(svc, "GET", "/v1/lookup?sha256=" + svc["sha"], token=TOK["report"])
    assert code == 200 and [i["id"] for i in j["items"]] == [1]
    assert call(svc, "GET", "/v1/lookup?sha256=xyz", token=TOK["report"])[0] == 400


def test_read_only_client_cannot_reveal(svc):
    code, _ = call(svc, "POST", "/v1/accounts/1/reveal", TOK["report"], {"reason": "r", "actor": "x"})
    assert code == 403


def test_backup_only_client_cannot_read(svc):
    assert call(svc, "GET", "/v1/accounts", token=TOK["bak"])[0] == 403


def test_reveal_requires_reason_and_actor(svc):
    assert call(svc, "POST", "/v1/accounts/1/reveal", TOK["admin"], {"actor": "a"})[0] == 400
    assert call(svc, "POST", "/v1/accounts/1/reveal", TOK["admin"], {"reason": "r"})[0] == 400


def test_reveal_self_other_person_denied_and_audited(svc):
    before = len(audits(svc, "reveal.denied"))
    code, _ = call(svc, "POST", "/v1/accounts/1/reveal", TOK["portal"],
                   {"reason": "我的 key", "actor": OTHER, "on_behalf_of": OTHER})
    assert code == 403
    rows = audits(svc, "reveal.denied")
    assert len(rows) == before + 1 and rows[-1][1] == f"portal:{OTHER}"
    # 不给 on_behalf_of 也拒
    assert call(svc, "POST", "/v1/accounts/1/reveal", TOK["portal"], {"reason": "r", "actor": OTHER})[0] == 403


def test_reveal_self_owner_ok_and_audited_before_returning(svc):
    before = len(audits(svc, "reveal"))
    code, j = call(svc, "POST", "/v1/accounts/1/reveal", TOK["portal"],
                   {"reason": "门户我的 key", "actor": OWNER, "on_behalf_of": OWNER})
    assert code == 200 and j["key"] == FAKE
    rows = audits(svc, "reveal")
    assert len(rows) == before + 1 and rows[-1] == ("reveal", f"portal:{OWNER}", 1, "门户我的 key")


def test_reveal_any_admin_ok(svc):
    code, j = call(svc, "POST", "/v1/accounts/1/reveal", TOK["admin"], {"reason": "用户来要", "actor": "刘国现"})
    assert code == 200 and j["key"] == FAKE
    assert audits(svc, "reveal")[-1][1] == "admin:刘国现"


def test_hash_only_cannot_be_revealed(svc):
    code, j = call(svc, "POST", "/v1/accounts/2/reveal", TOK["admin"], {"reason": "r", "actor": "a"})
    assert code == 409 and "hash_only" in j["error"]


def test_audit_is_append_only(svc):
    c = sqlite3.connect(svc["db"])
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE audit_log SET reason = 'x'")
    c.close()


def test_backup_is_valid_sqlite_with_ciphertext_only(svc, tmp_path):
    code, raw = call(svc, "GET", "/v1/backup", token=TOK["bak"])
    assert code == 200 and raw[:16] == b"SQLite format 3\x00"
    assert FAKE.encode() not in raw
    p = tmp_path / "b.db"
    p.write_bytes(raw)
    c = sqlite3.connect(p)
    assert c.execute("SELECT count(*) FROM account_secret WHERE state='encrypted'").fetchone()[0] == 1
    c.close()
    assert audits(svc, "backup.export")


def test_bad_tokens_file_refused(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"clients": {"x": {"sha256": "abc", "scopes": ["read"]}}}))
    with pytest.raises(SystemExit):
        S.load_tokens(str(p))
    p.write_text(json.dumps({"clients": {"x": {"sha256": "a" * 64, "scopes": ["root"]}}}))
    with pytest.raises(SystemExit):
        S.load_tokens(str(p))
