"""账户库迁移工具的离线测试。不连飞书、不连 198,不读真表明文。

跑法:python3 -m pytest scripts/account-store -q    (需要 cryptography)

数据源:
  * SNAPSHOT = .work/ai-portal/data/records_union_scrubbed.json —— 真表 1452 行的脱敏快照(明文已打码成 sk-…后4位)。
  * synthetic_plain —— 在快照上把每个打码 key 换成**本地随机生成的假明文**(保留后 4 位和长度),
    用来测加解密 / sha256 / 网关对账 / 日志打码。假 key 从没在任何网关上存在过。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import re
import sqlite3
import string
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import account_store as AS  # noqa: E402
import migrate_from_lark as M  # noqa: E402

ROOT = HERE.parents[1]
SNAPSHOT = ROOT / ".work/ai-portal/data/records_union_scrubbed.json"
pytestmark = pytest.mark.skipif(not SNAPSHOT.exists(), reason="脱敏快照不在本机")

MASK = re.compile(r"sk-…([A-Za-z0-9_\-]{1,8})(?:\(len=(\d+)\))?")
ALPH = string.ascii_letters + string.digits


def _fake_key(rid: str, tail: str, length: int | None) -> str:
    rnd = random.Random(f"{rid}|{tail}")
    n = max((length or 51) - 3 - len(tail), 8)
    return "sk-" + "".join(rnd.choice(ALPH) for _ in range(n)) + tail


@pytest.fixture(scope="module")
def scrubbed():
    return json.loads(SNAPSHOT.read_text())


@pytest.fixture(scope="module")
def synthetic_plain(scrubbed):
    """把打码 key 还原成同一行内一致的假明文。返回 (records, {record_id: api_key 明文})。"""
    recs = copy.deepcopy(scrubbed)
    api = {}
    for r in recs:
        rid = r["record_id"]
        lens = {}
        s = json.dumps(r["fields"], ensure_ascii=False)
        for m in MASK.finditer(s):
            if m.group(2):
                lens[m.group(1)] = int(m.group(2))
        s = MASK.sub(lambda m: _fake_key(rid, m.group(1), lens.get(m.group(1))), s)
        r["fields"] = json.loads(s)
        k = r["fields"].get("API Key")
        if isinstance(k, str) and k.startswith("sk-"):
            api[rid] = k
    return recs, api


@pytest.fixture()
def kek_file(tmp_path):
    d = tmp_path / "keys"
    d.mkdir()
    p = d / "kek.bin"
    AS.gen_kek(p)
    return p


@pytest.fixture()
def wrong_kek_file(tmp_path):
    d = tmp_path / "keys2"
    d.mkdir()
    p = d / "wrong.bin"
    AS.gen_kek(p)
    return p


def _write(tmp_path, recs, name="src.json"):
    p = tmp_path / name
    p.write_text(json.dumps(recs, ensure_ascii=False))
    return p


def _run(argv):
    return M.run(M.parse_args([str(a) for a in argv]))


def _db(tmp_path):
    d = tmp_path / "db"
    d.mkdir(exist_ok=True)
    return d / "accounts.db"


# ----------------------------------------------------------------------------- 脱敏快照


def test_scrubbed_snapshot_all_rows_land(tmp_path, scrubbed):
    db = _db(tmp_path)
    res = _run(["--snapshot", SNAPSHOT, "--db", db, "--apply"])
    rc = res["reconcile"]
    assert len(scrubbed) == 1452
    assert (rc["source_rows"], rc["db_rows"], rc["missing_in_db"], rc["extra_in_db"]) == (1452, 1452, 0, 0)
    assert rc["secret_sha_bad"] == 0 and rc["masked_hint_bad"] == 0
    conn = AS.connect(str(db))
    q = lambda sql, *a: conn.execute(sql, a).fetchall()  # noqa: E731
    # 状态:4 种原文 + 1 行空
    assert dict(q("SELECT status_raw, count(*) FROM account GROUP BY 1")) == {
        "有效": 1275, "已禁用": 173, "已完成": 2, "有效（已重新生成）": 1, None: 1}
    assert dict(q("SELECT status, count(*) FROM account GROUP BY 1")) == {
        "active": 1276, "disabled": 173, "completed": 2, "unknown": 1}
    assert dict(q("SELECT coalesce(account_type,'-'), count(*) FROM account GROUP BY 1")) == {
        "cursor": 743, "claude_code": 705, "carher": 3, "-": 1}
    # 一人多行:697 人;一人最多 22 行(批量客户 key 挂在同一个提交人名下)
    assert q("SELECT count(*) FROM person")[0][0] == 697
    top = q("SELECT owner_union_id, count(*) c FROM account GROUP BY 1 ORDER BY c DESC LIMIT 1")[0]
    assert top[1] == 22
    assert q("SELECT count(DISTINCT org_name) FROM account WHERE owner_union_id=?", top[0])[0][0] == 2
    # 批量客户 key:美团 20、巧虎 20、北汽集团 71
    orgs = dict(q("SELECT org_name, count(*) FROM account GROUP BY 1"))
    assert (orgs["美团"], orgs["巧虎"], orgs["北汽集团"], orgs[None]) == (20, 20, 71, 101)
    # 空行:只有提交人 + 父记录
    empty = q("SELECT * FROM account WHERE lark_record_id='reczz28TXLGC7EFE'")[0]
    assert empty["owner_union_id"] is None and empty["status"] == "unknown"
    assert empty["lark_parent_record_id"] == "reczz28SjnZ9fl47" and empty["submitter_name"]
    assert q("SELECT count(*) FROM account_secret WHERE account_id=?", empty["id"])[0][0] == 0
    # 补录行:表里本来就没明文 ⇒ sentinel,带 sha256 前缀
    sent = q("SELECT a.key_alias, s.sha256_prefix FROM account_secret s JOIN account a ON a.id=s.account_id "
             "WHERE s.state='sentinel' ORDER BY 1")
    assert [r[0] for r in sent] == ["cursor-xueqingwen-v2", "cursor-zhuyida-3pfs"] and all(len(r[1]) >= 6 for r in sent)
    # 旧双 key 列:39/38 行写的是 "blocked" ⇒ 进 legacy_notes,不当 key
    assert q("SELECT count(*) FROM account WHERE legacy_notes LIKE '%blocked%'")[0][0] == 39
    # 另 3 行旧双 key 列里就是 API Key 那一把 ⇒ 不存第二份,只记 same_as_api_key
    assert q("SELECT count(*) FROM account_secret WHERE kind IN ('cursor_key','claude_code_key')")[0][0] == 0
    assert q("SELECT count(*) FROM account WHERE legacy_notes LIKE '%same_as_api_key%'")[0][0] == 3
    # 点我体检残留旧 key(改发后 URL 没跟着改):259 行
    assert q("SELECT count(*) FROM account_secret WHERE kind='stale_probe_link'")[0][0] == 259
    # 人员字段:union_id + open_id + 名字
    assert q("SELECT count(*) FROM account WHERE owner_union_id IS NOT NULL AND owner_open_id LIKE 'ou\\_%' ESCAPE '\\' AND owner_name IS NOT NULL")[0][0] == 1378
    # 2 行「公共Key」的 open_id 列填的是字面量 PUBLIC_KEY:原样保留,不伪造
    assert q("SELECT count(*) FROM account WHERE owner_open_id='PUBLIC_KEY' AND dept_text='公共Key'")[0][0] == 2
    # 空字段保持 NULL,不是 0 / 空串
    assert q("SELECT count(*) FROM account WHERE premium_daily_budget IS NULL")[0][0] == 736
    assert q("SELECT count(*) FROM account WHERE applied_at IS NULL")[0][0] == 8
    # 快照里明文都已打码:库里一行 encrypted 都不该有
    assert q("SELECT count(*) FROM account_secret WHERE state='encrypted'")[0][0] == 0


def test_dry_run_writes_nothing(tmp_path):
    db = _db(tmp_path)
    res = _run(["--snapshot", SNAPSHOT, "--db", db])
    assert res["mode"].startswith("DRY-RUN") and res["reconcile"]["db_rows"] == 1452
    assert not db.exists()


def test_rerun_is_idempotent(tmp_path):
    db = _db(tmp_path)
    _run(["--snapshot", SNAPSHOT, "--db", db, "--apply"])
    res = _run(["--snapshot", SNAPSHOT, "--db", db, "--apply"])
    ch = res["changes"]
    assert ch.get("account.same") == 1452 and "account.insert" not in ch and "account.update" not in ch
    assert not any(k.endswith((".insert", ".update")) for k in ch), ch
    conn = AS.connect(str(db))
    assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 1452
    # dry-run 在已有库上:看到的是「无变化」,且不落盘
    before = db.stat().st_mtime_ns, conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    res = _run(["--snapshot", SNAPSHOT, "--db", db])
    assert res["changes"].get("account.same") == 1452
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == before[1]


def test_source_change_updates_only_that_row(tmp_path, scrubbed):
    db = _db(tmp_path)
    _run(["--snapshot", SNAPSHOT, "--db", db, "--apply"])
    recs = copy.deepcopy(scrubbed)
    recs[0]["fields"]["状态"] = "已禁用"
    res = _run(["--snapshot", _write(tmp_path, recs), "--db", db, "--apply"])
    assert res["changes"]["account.update"] == 1 and res["changes"]["account.same"] == 1451
    conn = AS.connect(str(db))
    assert conn.execute("SELECT status FROM account WHERE lark_record_id=?", (recs[0]["record_id"],)).fetchone()[0] == "disabled"


# ----------------------------------------------------------------------------- 假明文:加解密 / 对账


def test_fixture_really_has_plaintext(synthetic_plain):
    """阳性对照:合成源里确实有可用明文,否则后面「库里/日志里找不到明文」的断言没有意义。"""
    recs, api = synthetic_plain
    assert len(api) == 1449
    assert all(re.fullmatch(r"sk-[A-Za-z0-9_\-]{8,}", k) for k in api.values())
    assert any(k in json.dumps(r, ensure_ascii=False) for r in recs[:5] for k in api.values())


def test_encrypt_round_trip_and_gateway_ratio(tmp_path, synthetic_plain, kek_file):
    recs, api = synthetic_plain
    src, db = _write(tmp_path, recs), _db(tmp_path)
    # 假网关:只「认识」其中 90% 的 key
    keep = sorted(api)[: int(len(api) * 0.9)]
    gw = tmp_path / "gw.txt"
    gw.write_text("\n".join(AS.sha256_hex(api[r]) for r in keep) + "\n" + "0" * 64 + "\n")
    res = _run(["--snapshot", src, "--db", db, "--kek-file", kek_file, "--gateway-hashes", gw, "--apply"])
    rc = res["reconcile"]
    assert rc["missing_in_db"] == 0 and rc["secret_sha_bad"] == 0 and rc["decrypt_bad"] == 0
    assert rc["decrypt_ok"] == 1449                  # 每把 api_key 都解得开,且 sha256 对得上
    assert rc["gateway"]["comparable"] == 1449 and rc["gateway"]["matched"] == len(keep)
    conn = AS.connect(str(db))
    kek, kid = AS.load_kek(kek_file, db_path=db)
    rows = conn.execute("SELECT s.*, a.lark_record_id FROM account_secret s JOIN account a ON a.id=s.account_id "
                        "WHERE s.kind='api_key' AND s.state='encrypted'").fetchall()
    assert len(rows) == 1449
    for r in rows:
        pt = AS.decrypt_secret(kek, kid, r["kind"], r["sha256"], r["ciphertext"], r["wrapped_dek"], row_kek_id=r["kek_id"])
        assert pt == api[r["lark_record_id"]]
        assert r["sha256"] == hashlib.sha256(pt.encode()).hexdigest()      # = LiteLLM hash_token
        assert r["hint"] == "sk-…" + pt[-4:]
    # 残留旧 key 只存 hash,取不回
    assert {tuple(x) for x in conn.execute("SELECT state, ciphertext IS NULL FROM account_secret WHERE kind='stale_probe_link'")} == {("hash_only", 1)}
    assert AS.verify(conn, kek, kid) == {"encrypted": 1449, "ok": 1449, "fail": 0, "other_kek": 0}


def test_no_plaintext_on_disk_or_in_logs(tmp_path, synthetic_plain, kek_file, capsys):
    recs, api = synthetic_plain
    src, db = _write(tmp_path, recs), _db(tmp_path)
    rc = M.main(["--snapshot", str(src), "--db", str(db), "--kek-file", str(kek_file), "--apply", "--verbose"])
    assert rc == 0
    out = capsys.readouterr()
    logs = out.out + out.err
    assert "row " in logs and "sk-…" in logs          # verbose 确实打了行
    conn = AS.connect(str(db))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = b"".join(p.read_bytes() for p in db.parent.iterdir())
    leaked_log = [k for k in api.values() if k in logs]
    leaked_db = [k for k in api.values() if k.encode() in blob]
    assert leaked_log == [] and leaked_db == []
    # 审计 detail 也不能带明文
    assert not any(k in (d or "") for (d,) in conn.execute("SELECT detail FROM audit_log") for k in api.values())


def test_wrong_kek_cannot_decrypt(tmp_path, synthetic_plain, kek_file, wrong_kek_file):
    recs, api = synthetic_plain
    db = _db(tmp_path)
    _run(["--snapshot", _write(tmp_path, recs), "--db", db, "--kek-file", kek_file, "--apply"])
    conn = AS.connect(str(db))
    good, good_id = AS.load_kek(kek_file, db_path=db)
    bad, bad_id = AS.load_kek(wrong_kek_file, db_path=db)
    assert good_id != bad_id
    r = conn.execute("SELECT * FROM account_secret WHERE state='encrypted' AND kind='api_key' LIMIT 1").fetchone()
    # 阳性对照:对的钥匙能解
    assert AS.decrypt_secret(good, good_id, r["kind"], r["sha256"], r["ciphertext"], r["wrapped_dek"], row_kek_id=r["kek_id"]).startswith("sk-")
    # 错的钥匙:kek_id 不符直接拒
    with pytest.raises(AS.DecryptError):
        AS.decrypt_secret(bad, bad_id, r["kind"], r["sha256"], r["ciphertext"], r["wrapped_dek"], row_kek_id=r["kek_id"])
    # 就算伪造 kek_id 绕过指纹检查,GCM 也解不开
    with pytest.raises(AS.DecryptError, match="GCM"):
        AS.decrypt_secret(bad, good_id, r["kind"], r["sha256"], r["ciphertext"], r["wrapped_dek"])
    assert AS.verify(conn, bad, bad_id)["ok"] == 0
    # 走 CLI 的 verify:错钥匙 ⇒ ok=0(全部算 other_kek)
    assert AS.verify(conn, bad, good_id)["fail"] == 1449


def test_ciphertext_bound_to_its_row(tmp_path, synthetic_plain, kek_file):
    """把 A 行的密文挪给 B 行(AAD 里绑了 sha256)⇒ 解不开。"""
    recs, _ = synthetic_plain
    db = _db(tmp_path)
    _run(["--snapshot", _write(tmp_path, recs), "--db", db, "--kek-file", kek_file, "--apply"])
    conn = AS.connect(str(db))
    kek, kid = AS.load_kek(kek_file, db_path=db)
    a, b = conn.execute("SELECT * FROM account_secret WHERE state='encrypted' LIMIT 2").fetchall()
    with pytest.raises(AS.DecryptError):
        AS.decrypt_secret(kek, kid, b["kind"], b["sha256"], a["ciphertext"], a["wrapped_dek"])
    with pytest.raises(AS.DecryptError):   # kind 也在 AAD 里
        AS.decrypt_secret(kek, kid, "cursor_key", a["sha256"], a["ciphertext"], a["wrapped_dek"])


def test_reveal_audits_and_cli(tmp_path, synthetic_plain, kek_file, wrong_kek_file, capsys):
    recs, api = synthetic_plain
    db = _db(tmp_path)
    _run(["--snapshot", _write(tmp_path, recs), "--db", db, "--kek-file", kek_file, "--apply"])
    rid = next(r["record_id"] for r in recs if r["record_id"] in api)
    assert AS.main(["reveal", "--db", str(db), "--kek-file", str(kek_file), "--record-id", rid, "--reason", "测试"]) == 0
    assert capsys.readouterr().out.strip() == api[rid]
    with pytest.raises(AS.DecryptError):
        AS.main(["reveal", "--db", str(db), "--kek-file", str(wrong_kek_file), "--record-id", rid, "--reason", "错钥匙"])
    conn = AS.connect(str(db))
    acts = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'reveal%' ORDER BY id")]
    assert acts == ["reveal", "reveal", "reveal.fail"]
    with pytest.raises(SystemExit):        # 没有理由不让解
        AS.reveal(conn, *AS.load_kek(kek_file, db_path=db), AS.find_secret(conn, record_id=rid), reason=" ")
    with pytest.raises(SystemExit, match="重名"):
        AS.find_secret(conn, alias="cursor-liuguoxian")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET reason='x'")


def test_rotated_key_replaces_and_audits(tmp_path, synthetic_plain, kek_file):
    recs, api = synthetic_plain
    db = _db(tmp_path)
    _run(["--snapshot", _write(tmp_path, recs), "--db", db, "--kek-file", kek_file, "--apply"])
    recs2 = copy.deepcopy(recs)
    rid = next(r["record_id"] for r in recs2 if r["record_id"] in api)
    new = _fake_key(rid, "ZZZZ", 51)
    for r in recs2:
        if r["record_id"] == rid:
            r["fields"]["API Key"] = new
    res = _run(["--snapshot", _write(tmp_path, recs2, "src2.json"), "--db", db, "--kek-file", kek_file, "--apply"])
    assert res["changes"]["secret.api_key.update"] == 1 and res["changes"]["secret.api_key.same"] == 1450   # 1449-1 + 2 行补录 sentinel
    conn = AS.connect(str(db))
    row = AS.find_secret(conn, record_id=rid)
    assert AS.decrypt_secret(*AS.load_kek(kek_file, db_path=db), row["kind"], row["sha256"], row["ciphertext"], row["wrapped_dek"]) == new
    assert conn.execute("SELECT count(*) FROM audit_log WHERE action='secret.replace'").fetchone()[0] == 1


def test_hash_only_mode_keeps_no_ciphertext(tmp_path, synthetic_plain):
    recs, api = synthetic_plain
    db = _db(tmp_path)
    res = _run(["--snapshot", _write(tmp_path, recs), "--db", db, "--secret-mode", "hash-only", "--apply"])
    assert res["reconcile"]["secret_sha_bad"] == 0
    conn = AS.connect(str(db))
    assert conn.execute("SELECT count(*) FROM account_secret WHERE ciphertext IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM account_secret WHERE state='hash_only' AND kind='api_key'").fetchone()[0] == 1449


def test_plaintext_without_kek_refuses(tmp_path, synthetic_plain):
    recs, _ = synthetic_plain
    with pytest.raises(SystemExit, match="kek"):
        _run(["--snapshot", _write(tmp_path, recs), "--db", _db(tmp_path), "--apply"])


# ----------------------------------------------------------------------------- 主密钥文件纪律


def test_kek_file_guards(tmp_path):
    p = tmp_path / "k" / "kek.bin"
    p.parent.mkdir()
    fp = AS.gen_kek(p)
    assert oct(p.stat().st_mode & 0o777) == "0o600" and fp.startswith("kek-")
    with pytest.raises(FileExistsError):       # 不覆盖已有钥匙
        AS.gen_kek(p)
    os.chmod(p, 0o644)
    with pytest.raises(AS.KeyError_, match="权限"):
        AS.load_kek(p)
    os.chmod(p, 0o600)
    with pytest.raises(AS.KeyError_, match="目录树"):
        AS.load_kek(p, db_path=tmp_path / "k" / "accounts.db")
    assert AS.load_kek(p, db_path=tmp_path / "db" / "accounts.db")[1] == fp


def test_redact():
    k = "sk-" + "A" * 40 + "wxyz"
    assert AS.redact({"a": [k, f"https://x/#k={k}"]}) == '{"a": ["sk-…wxyz", "https://x/#k=sk-…wxyz"]}'
    assert k not in json.dumps(M.scrub_record({"record_id": "r", "fields": {"体检链接": {"value": [{"text": k}]}}}))


def test_unguarded_control_redaction_has_teeth(tmp_path, synthetic_plain, kek_file, capsys, monkeypatch):
    """放开对照:把 redact 关掉 ⇒ verbose 日志里必须出现明文。证明 test_no_plaintext_on_disk_or_in_logs 不是假绿。"""
    recs, api = synthetic_plain
    monkeypatch.setattr(AS, "redact", lambda s: s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str))
    M.main(["--snapshot", str(_write(tmp_path, recs)), "--db", str(_db(tmp_path)), "--kek-file", str(kek_file), "--verbose"])
    out = capsys.readouterr()
    assert any(k in out.err for k in api.values())
