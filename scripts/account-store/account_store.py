#!/usr/bin/env python3
"""账户库(account store):把飞书「账户表」里的 key 搬到 198 本机,明文只以信封加密的形态落盘。

设计见 .work/ai-portal/account-store/DESIGN.md。这里是库 + 一个小 CLI:

    python3 account_store.py gen-kek /etc/account-store/kek-2026-11.bin      # 生成主密钥(0600,不打印)
    python3 account_store.py init   --db /Data/account-store/accounts.db
    python3 account_store.py stats  --db ...                                 # 只出计数
    python3 account_store.py verify --db ... --kek-file ...                  # 逐行解密 + 对 sha256,不打印明文
    python3 account_store.py reveal --db ... --kek-file ... --alias cursor-xxx --reason "用户丢了 key 来要"

约束(写进代码,不靠自觉):
  * 主密钥文件必须 0600/0400 且归当前用户,且不能和库在同一个目录树下(库的备份不应带走钥匙)。
  * 每把明文用独立随机 DEK 做 AES-256-GCM;DEK 再用主密钥(KEK)包一层。AAD 绑定 kind + sha256,
    密文挪到别的行会解不开;解出来还要再对一次 sha256。
  * reveal 先写审计行(提交)再解密;审计表有触发器,只能追加。
  * 任何要打日志的字符串先过 redact():sk- 开头的 token 一律只留后 4 位。

兼容 198 现网:Python 3.10 + cryptography 3.4.8 + SQLite 3.37。
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCHEMA_VERSION = "1"
SECRET_KINDS = ("api_key", "cursor_key", "claude_code_key", "stale_probe_link")
_ENVELOPE_VERSION = b"\x01"
_AAD_PREFIX = "account-store/v1"

# --------------------------------------------------------------------------- redaction

_SK = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")


def redact(s: object) -> str:
    """把任意对象转成可以打日志的字符串:每个 sk- token 只留后 4 位。"""
    text = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    return _SK.sub(lambda m: "sk-…" + m.group(0)[-4:], text)


def log(*parts: object, file=None) -> None:
    print(" ".join(redact(p) for p in parts), file=file or sys.stderr, flush=True)


def key_hint(plaintext: str) -> str:
    return "sk-…" + plaintext[-4:]


def sha256_hex(plaintext: str) -> str:
    """和 LiteLLM proxy/_types.py:hash_token 一致:sha256(token.encode()).hexdigest()。"""
    return hashlib.sha256(plaintext.encode()).hexdigest()


# --------------------------------------------------------------------------- KEK


class KeyError_(Exception):
    """主密钥相关的错误(文件权限、位置、指纹不符)。"""


class DecryptError(Exception):
    pass


def kek_fingerprint(kek: bytes) -> str:
    return "kek-" + hashlib.sha256(b"account-store kek id|" + kek).hexdigest()[:12]


def _parse_kek(raw: bytes) -> bytes:
    if len(raw) == 32:
        return raw
    txt = raw.strip()
    if re.fullmatch(rb"[0-9a-fA-F]{64}", txt):
        return bytes.fromhex(txt.decode())
    import base64

    try:
        b = base64.b64decode(txt, validate=True)
    except Exception:  # noqa: BLE001
        b = b""
    if len(b) == 32:
        return b
    raise KeyError_("主密钥文件格式不对:要 32 字节原始 / 64 位 hex / base64(32 字节)")


def load_kek(path: str | os.PathLike, *, db_path: str | os.PathLike | None = None) -> tuple[bytes, str]:
    p = Path(path)
    st = p.stat()
    if st.st_mode & 0o077:
        raise KeyError_(f"主密钥文件权限过宽({stat.filemode(st.st_mode)}),必须 0600 或 0400:{p}")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise KeyError_(f"主密钥文件不归当前用户所有:{p}")
    if db_path is not None and str(db_path) != ":memory:":
        db_dir = Path(db_path).resolve().parent
        if Path(path).resolve().is_relative_to(db_dir):
            raise KeyError_("主密钥不能放在库所在目录树下(库的备份会把钥匙一起带走)")
    kek = _parse_kek(p.read_bytes())
    return kek, kek_fingerprint(kek)


def gen_kek(path: str | os.PathLike) -> str:
    """生成 32 字节随机主密钥,O_EXCL + 0600 写盘,返回指纹。绝不打印密钥本身。"""
    kek = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(kek)
    return kek_fingerprint(kek)


# --------------------------------------------------------------------------- envelope crypto


def _aad(kind: str, sha: str) -> bytes:
    return f"{_AAD_PREFIX}|{kind}|{sha}".encode()


def encrypt_secret(kek: bytes, kek_id: str, kind: str, plaintext: str) -> tuple[str, bytes, bytes]:
    """返回 (sha256, ciphertext, wrapped_dek)。"""
    sha = sha256_hex(plaintext)
    aad = _aad(kind, sha)
    dek = AESGCM.generate_key(bit_length=256)
    n1, n2 = os.urandom(12), os.urandom(12)
    ct = _ENVELOPE_VERSION + n1 + AESGCM(dek).encrypt(n1, plaintext.encode(), aad)
    wrapped = _ENVELOPE_VERSION + n2 + AESGCM(kek).encrypt(n2, dek, aad + b"|dek|" + kek_id.encode())
    return sha, ct, wrapped


def decrypt_secret(kek: bytes, kek_id: str, kind: str, sha: str, ct: bytes, wrapped: bytes,
                   *, row_kek_id: str | None = None) -> str:
    if row_kek_id is not None and row_kek_id != kek_id:
        raise DecryptError(f"主密钥不对:这一行用 {row_kek_id} 加密,手上的是 {kek_id}")
    aad = _aad(kind, sha)
    try:
        if ct[:1] != _ENVELOPE_VERSION or wrapped[:1] != _ENVELOPE_VERSION:
            raise DecryptError("未知的信封版本")
        dek = AESGCM(kek).decrypt(wrapped[1:13], wrapped[13:], aad + b"|dek|" + kek_id.encode())
        pt = AESGCM(dek).decrypt(ct[1:13], ct[13:], aad).decode()
    except InvalidTag as exc:
        raise DecryptError("解密失败(GCM 校验不过:主密钥不对或密文被改/被挪行)") from exc
    if sha256_hex(pt) != sha:
        raise DecryptError("解出来的明文 sha256 和库里记录的不一致")
    return pt


# --------------------------------------------------------------------------- schema

DDL = r"""
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

-- 人:按飞书 union_id(on_ 开头,跨应用稳定)去重。open_id 按应用区分,只作参考。
CREATE TABLE IF NOT EXISTS person (
  union_id    TEXT PRIMARY KEY CHECK (union_id LIKE 'on\_%' ESCAPE '\'),
  open_id     TEXT,
  name        TEXT,
  en_name     TEXT,
  email       TEXT,
  employment  TEXT CHECK (employment IS NULL OR employment IN ('在职','离职')),
  title       TEXT,
  lark_depts  TEXT,                       -- JSON 数组:飞书人员的部门(姓名.部门)
  updated_at  TEXT NOT NULL
);

-- 账户:一行 = 飞书表一行 = 一把 key 的元数据。不含任何明文。
CREATE TABLE IF NOT EXISTS account (
  id                    INTEGER PRIMARY KEY,
  lark_record_id        TEXT UNIQUE,      -- 迁移期对照;新库原生行可为 NULL
  lark_serial           INTEGER,          -- 编号
  lark_parent_record_id TEXT,             -- 父记录
  owner_union_id        TEXT REFERENCES person(union_id),
  owner_open_id         TEXT,             -- 表里 open_id 列(发 key 时写入的那个应用的 open_id)
  owner_name            TEXT,
  submitter_union_id    TEXT,
  submitter_open_id     TEXT,             -- 飞书「提交人」字段不返回 open_id,迁移时为 NULL
  submitter_name        TEXT,
  account_type          TEXT CHECK (account_type IS NULL OR account_type IN ('cursor','claude_code','carher')),
  status                TEXT NOT NULL CHECK (status IN ('active','disabled','completed','failed','unknown')),
  status_raw            TEXT,             -- 飞书原文,如「有效（已重新生成）」「生成失败: …」
  org_name              TEXT,             -- 组织名称(无锡车联/上海车联/北汽集团/美团/…)
  dept_names            TEXT,             -- JSON 数组:部门名称
  dept_text             TEXT,             -- 「部门」文本列(目前只有 2 行「公共Key」)
  email_prefix          TEXT,
  key_alias             TEXT,             -- 不唯一:现表里有 3 个 alias 各出现 2 次
  models                TEXT,
  cursor_models         TEXT,
  claude_code_models    TEXT,
  daily_budget          REAL,
  premium_daily_budget  REAL,
  total_spend_usd       REAL,
  pioneer               TEXT,             -- JSON 数组:先锋
  experiment            TEXT,             -- JSON 数组:实验
  legacy_notes          TEXT,             -- JSON:旧双 key 列里的非 key 文本(如 "blocked")
  applied_at            TEXT,             -- ISO8601 UTC
  source_digest         TEXT,             -- 非机密字段的摘要,用来判断重跑是否有变化
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS account_owner  ON account(owner_union_id);
CREATE INDEX IF NOT EXISTS account_alias  ON account(key_alias);
CREATE INDEX IF NOT EXISTS account_org    ON account(org_name);
CREATE INDEX IF NOT EXISTS account_status ON account(status);

-- 机密:每把 key 一行。encrypted = 可取回;hash_only = 只留 sha256(够和网关对账/封禁,取不回);
-- masked_in_source = 源里就是打码的(只在本地测试快照里出现);sentinel = 表里本来就没明文(补录行)。
CREATE TABLE IF NOT EXISTS account_secret (
  id            INTEGER PRIMARY KEY,
  account_id    INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL CHECK (kind IN ('api_key','cursor_key','claude_code_key','stale_probe_link')),
  state         TEXT NOT NULL CHECK (state IN ('encrypted','hash_only','masked_in_source','sentinel')),
  sha256        TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),   -- = LiteLLM_VerificationToken.token
  sha256_prefix TEXT,
  hint          TEXT,                                                  -- sk-…后4位
  key_len       INTEGER,
  ciphertext    BLOB,
  wrapped_dek   BLOB,
  kek_id        TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (account_id, kind),
  CHECK ((state = 'encrypted') = (ciphertext IS NOT NULL AND wrapped_dek IS NOT NULL
                                  AND kek_id IS NOT NULL AND sha256 IS NOT NULL)),
  CHECK (state = 'encrypted' OR (ciphertext IS NULL AND wrapped_dek IS NULL))
);
CREATE INDEX IF NOT EXISTS secret_sha ON account_secret(sha256);

-- 审计:只能追加。
CREATE TABLE IF NOT EXISTS audit_log (
  id         INTEGER PRIMARY KEY,
  ts         TEXT NOT NULL,
  actor      TEXT NOT NULL,
  action     TEXT NOT NULL,
  account_id INTEGER,
  kind       TEXT,
  reason     TEXT,
  detail     TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def now_iso() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int((t % 1) * 1000)


def _refuse_network_fs(db_path: str) -> None:
    """SQLite 不放 NFS(仓库 CLAUDE.md)。只在有 /proc/mounts 的 Linux 上检查。"""
    if db_path == ":memory:" or not Path("/proc/mounts").exists():
        return
    target = str(Path(db_path).resolve().parent)
    best, fstype = "", ""
    for line in Path("/proc/mounts").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and (target == parts[1] or target.startswith(parts[1].rstrip("/") + "/")):
            if len(parts[1]) > len(best):
                best, fstype = parts[1], parts[2]
    if fstype.startswith(("nfs", "cifs", "smb", "fuse.sshfs")):
        raise SystemExit(f"拒绝:库路径在网络文件系统上({fstype} {best}),SQLite 会有锁问题")


def connect(db_path: str) -> sqlite3.Connection:
    _refuse_network_fs(db_path)
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o077)
    try:
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    finally:
        if db_path != ":memory:":
            os.umask(old)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))


def audit(conn: sqlite3.Connection, action: str, *, actor: str | None = None, account_id: int | None = None,
          kind: str | None = None, reason: str | None = None, detail: object = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, account_id, kind, reason, detail) VALUES (?,?,?,?,?,?,?)",
        (now_iso(), actor or current_actor(), action, account_id, kind, reason,
         None if detail is None else redact(detail)),
    )


def current_actor() -> str:
    user = os.environ.get("SUDO_USER") or getpass.getuser()
    return f"{user}@{os.uname().nodename}" if hasattr(os, "uname") else user


# --------------------------------------------------------------------------- reveal / verify


def find_secret(conn: sqlite3.Connection, *, alias: str | None = None, record_id: str | None = None,
                sha256: str | None = None, kind: str = "api_key") -> sqlite3.Row:
    where, arg = (("a.key_alias = ?", alias) if alias else
                  ("a.lark_record_id = ?", record_id) if record_id else
                  ("s.sha256 = ?", sha256) if sha256 else (None, None))
    if where is None:
        raise SystemExit("要给 --alias / --record-id / --sha256 其中一个")
    rows = conn.execute(
        f"SELECT s.*, a.key_alias, a.lark_record_id, a.status FROM account_secret s "
        f"JOIN account a ON a.id = s.account_id WHERE {where} AND s.kind = ?", (arg, kind)).fetchall()
    if not rows:
        raise SystemExit("没找到这把 key")
    if len(rows) > 1:
        raise SystemExit(f"匹配到 {len(rows)} 行(alias 有重名),改用 --record-id 指定")
    return rows[0]


def reveal(conn: sqlite3.Connection, kek: bytes, kek_id: str, row: sqlite3.Row, *, reason: str,
           actor: str | None = None) -> str:
    if not reason or not reason.strip():
        raise SystemExit("reveal 必须写 --reason(会进审计)")
    if row["state"] != "encrypted":
        raise SystemExit(f"这把 key 在库里是 {row['state']},没有可解密的密文")
    audit(conn, "reveal", actor=actor, account_id=row["account_id"], kind=row["kind"], reason=reason,
          detail={"alias": row["key_alias"], "record_id": row["lark_record_id"], "hint": row["hint"]})
    try:
        return decrypt_secret(kek, kek_id, row["kind"], row["sha256"], row["ciphertext"], row["wrapped_dek"],
                              row_kek_id=row["kek_id"])
    except DecryptError as exc:
        audit(conn, "reveal.fail", actor=actor, account_id=row["account_id"], kind=row["kind"],
              reason=reason, detail=str(exc))
        raise


def verify(conn: sqlite3.Connection, kek: bytes, kek_id: str) -> dict:
    out = {"encrypted": 0, "ok": 0, "fail": 0, "other_kek": 0}
    for r in conn.execute("SELECT * FROM account_secret WHERE state = 'encrypted'"):
        out["encrypted"] += 1
        if r["kek_id"] != kek_id:
            out["other_kek"] += 1
            continue
        try:
            decrypt_secret(kek, kek_id, r["kind"], r["sha256"], r["ciphertext"], r["wrapped_dek"])
            out["ok"] += 1
        except DecryptError:
            out["fail"] += 1
    return out


def stats(conn: sqlite3.Connection) -> dict:
    q = lambda sql: [tuple(r) for r in conn.execute(sql)]  # noqa: E731
    return {
        "accounts": conn.execute("SELECT count(*) FROM account").fetchone()[0],
        "persons": conn.execute("SELECT count(*) FROM person").fetchone()[0],
        "by_status": q("SELECT status, count(*) FROM account GROUP BY 1 ORDER BY 2 DESC"),
        "by_type": q("SELECT coalesce(account_type,'(空)'), count(*) FROM account GROUP BY 1 ORDER BY 2 DESC"),
        "by_org": q("SELECT coalesce(org_name,'(空)'), count(*) FROM account GROUP BY 1 ORDER BY 2 DESC"),
        "secrets": q("SELECT kind, state, count(*) FROM account_secret GROUP BY 1, 2 ORDER BY 1, 2"),
        "audit_rows": conn.execute("SELECT count(*) FROM audit_log").fetchone()[0],
    }


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen-kek"); g.add_argument("path")
    for name in ("init", "stats", "verify", "reveal"):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
        if name in ("verify", "reveal"):
            p.add_argument("--kek-file", required=True)
        if name == "reveal":
            p.add_argument("--alias"); p.add_argument("--record-id"); p.add_argument("--sha256")
            p.add_argument("--kind", default="api_key", choices=SECRET_KINDS)
            p.add_argument("--reason", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "gen-kek":
        log("已生成主密钥,指纹", gen_kek(a.path), "(文件 0600;密钥本身不打印。请另做离线托管)")
        return 0
    if a.cmd != "init" and not Path(a.db).exists():
        raise SystemExit(f"库不存在:{a.db}")
    conn = connect(a.db)
    init_schema(conn)
    if a.cmd == "init":
        log("ok", a.db)
    elif a.cmd == "stats":
        print(json.dumps(stats(conn), ensure_ascii=False, indent=1))
    else:
        kek, kek_id = load_kek(a.kek_file, db_path=a.db)
        if a.cmd == "verify":
            res = verify(conn, kek, kek_id)
            print(json.dumps({"kek_id": kek_id, **res}, ensure_ascii=False))
            return 0 if res["fail"] == 0 else 2
        row = find_secret(conn, alias=a.alias, record_id=a.record_id, sha256=a.sha256, kind=a.kind)
        sys.stdout.write(reveal(conn, kek, kek_id, row, reason=a.reason) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
