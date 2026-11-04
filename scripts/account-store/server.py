#!/usr/bin/env python3
"""账户库 HTTP 服务:唯一能打开库文件 + 主密钥的进程。其余读写方一律走这里。

    ACCOUNT_STORE_DB=/data/accounts.db ACCOUNT_STORE_KEK=/kek/kek.bin \
    ACCOUNT_STORE_TOKENS=/etc/account-store-tokens/tokens.json python3 server.py

接口(Authorization: Bearer <token>;token 只存 sha256,见 tokens.json):
  GET  /healthz                         不鉴权,不碰库(/health 里不做重活)
  GET  /v1/accounts?owner_union_id=&alias=&org=&status=&type=&limit=   scope read   元数据,不含明文
  GET  /v1/accounts/{id}                scope read
  GET  /v1/lookup?sha256=               scope read   网关 token(sha256)→ 账户
  POST /v1/accounts/{id}/reveal         scope reveal:self | reveal:any
       body {"kind":"api_key","reason":"...","actor":"<谁在取>","on_behalf_of":"on_xxx"}
       reveal:self 只能取 on_behalf_of 名下的行(门户:登录人的 union_id)。先写审计再解密。
  GET  /v1/stats                        scope read
  GET  /v1/backup                       scope backup  在线一致性备份(只有密文;主密钥不在库里)

tokens.json 形状:{"clients": {"portal": {"sha256": "<hex>", "scopes": ["read", "reveal:self"]}, ...}}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_store as AS  # noqa: E402

SCOPES = {"read", "reveal:self", "reveal:any", "backup"}
REVEAL_PER_MIN = int(os.environ.get("ACCOUNT_STORE_REVEAL_PER_MIN", "30"))
_ACCT_COLS = ("id, lark_record_id, lark_serial, owner_union_id, owner_open_id, owner_name, submitter_name, "
              "account_type, status, status_raw, org_name, dept_names, email_prefix, key_alias, models, "
              "daily_budget, premium_daily_budget, total_spend_usd, applied_at, created_at, updated_at")
_SECRET_COLS = "account_id, kind, state, sha256, hint, key_len, kek_id, updated_at"


def load_tokens(path: str) -> dict[str, dict]:
    data = json.loads(Path(path).read_text())
    out = {}
    for name, c in (data.get("clients") or {}).items():
        sha, scopes = c.get("sha256", ""), set(c.get("scopes") or [])
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or not scopes or not scopes <= SCOPES:
            raise SystemExit(f"tokens.json 里 {name} 不合法(sha256 要 64 位 hex,scopes ⊆ {sorted(SCOPES)})")
        out[name] = {"sha256": sha, "scopes": scopes}
    if not out:
        raise SystemExit("tokens.json 里一个 client 都没有")
    return out


class Store:
    def __init__(self, db: str, kek_file: str, tokens_file: str):
        self.db = db
        self.kek, self.kek_id = AS.load_kek(kek_file, db_path=db)
        self.tokens = load_tokens(tokens_file)
        conn = AS.connect(db)
        AS.init_schema(conn)
        conn.close()
        self._rl: dict[str, list[float]] = {}
        self._rl_lock = threading.Lock()

    def conn(self) -> sqlite3.Connection:
        return AS.connect(self.db)

    def auth(self, header: str | None) -> tuple[str, set[str]] | None:
        if not header or not header.startswith("Bearer "):
            return None
        presented = hashlib.sha256(header[7:].strip().encode()).hexdigest()
        hit = None
        for name, c in self.tokens.items():   # 全部比一遍,常量时间
            if hmac.compare_digest(presented, c["sha256"]):
                hit = (name, c["scopes"])
        return hit

    def allow_reveal(self, client: str) -> bool:
        now = time.monotonic()
        with self._rl_lock:
            q = [t for t in self._rl.get(client, []) if now - t < 60]
            if len(q) >= REVEAL_PER_MIN:
                self._rl[client] = q
                return False
            q.append(now)
            self._rl[client] = q
            return True


def _account_view(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    secrets_: dict[int, list[dict]] = {}
    for s in conn.execute(f"SELECT {_SECRET_COLS} FROM account_secret WHERE account_id IN ({','.join('?' * len(ids))})", ids):
        secrets_.setdefault(s["account_id"], []).append({k: s[k] for k in s.keys() if k != "account_id"})
    return [{**dict(r), "secrets": secrets_.get(r["id"], [])} for r in rows]


class Handler(BaseHTTPRequestHandler):
    store: Store
    server_version = "account-store/1"
    sys_version = ""

    def log_message(self, fmt, *args):  # 只记方法/路径/状态,不记 query(里面可能有 sha256/alias)
        AS.log(f"{self.client_address[0]} {self.command} {urlparse(self.path).path} {args[1] if len(args) > 1 else ''}")

    def _send(self, code: int, obj=None, *, raw: bytes | None = None, ctype="application/json; charset=utf-8"):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _need(self, scope: str) -> str | None:
        a = self.store.auth(self.headers.get("Authorization"))
        if a is None:
            self._send(401, {"error": "unauthorized"})
            return None
        name, scopes = a
        ok = scope in scopes or (scope == "reveal" and scopes & {"reveal:self", "reveal:any"})
        if not ok:
            self._send(403, {"error": f"client {name} 没有 {scope} 权限"})
            return None
        self.client, self.scopes = name, scopes
        return name

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/healthz":
                return self._send(200, {"ok": True})
            if u.path == "/v1/backup":
                return self._backup() if self._need("backup") else None
            if not self._need("read"):
                return None
            conn = self.store.conn()
            try:
                if u.path == "/v1/stats":
                    return self._send(200, {"kek_id": self.store.kek_id, **AS.stats(conn)})
                if u.path == "/v1/lookup":
                    sha = q.get("sha256", "")
                    if not re.fullmatch(r"[0-9a-f]{64}", sha):
                        return self._send(400, {"error": "sha256 要 64 位小写 hex"})
                    rows = conn.execute(f"SELECT {_ACCT_COLS} FROM account WHERE id IN "
                                        "(SELECT account_id FROM account_secret WHERE sha256 = ?)", (sha,)).fetchall()
                    return self._send(200, {"items": _account_view(conn, rows)})
                m = re.fullmatch(r"/v1/accounts/(\d+)", u.path)
                if m:
                    rows = conn.execute(f"SELECT {_ACCT_COLS} FROM account WHERE id = ?", (int(m.group(1)),)).fetchall()
                    return self._send(200, _account_view(conn, rows)[0]) if rows else self._send(404, {"error": "not found"})
                if u.path == "/v1/accounts":
                    where, args = [], []
                    for p, col in (("owner_union_id", "owner_union_id"), ("alias", "key_alias"), ("org", "org_name"),
                                   ("status", "status"), ("type", "account_type"), ("record_id", "lark_record_id")):
                        if q.get(p):
                            where.append(f"{col} = ?")
                            args.append(q[p])
                    limit = min(max(int(q.get("limit", "200")), 1), 5000)
                    sql = f"SELECT {_ACCT_COLS} FROM account" + (" WHERE " + " AND ".join(where) if where else "") \
                          + " ORDER BY id LIMIT ?"
                    rows = conn.execute(sql, (*args, limit)).fetchall()
                    return self._send(200, {"items": _account_view(conn, rows)})
                return self._send(404, {"error": "not found"})
            finally:
                conn.close()
        except ValueError:
            return self._send(400, {"error": "bad request"})
        except Exception as exc:  # noqa: BLE001
            AS.log("GET 出错", u.path, repr(exc))
            return self._send(500, {"error": "internal"})

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        m = re.fullmatch(r"/v1/accounts/(\d+)/reveal", u.path)
        if not m:
            return self._send(404, {"error": "not found"})
        if not self._need("reveal"):
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "body 要是 JSON"})
        kind = body.get("kind") or "api_key"
        reason, actor, behalf = (str(body.get(k) or "").strip() for k in ("reason", "actor", "on_behalf_of"))
        if kind not in AS.SECRET_KINDS or not reason or not actor:
            return self._send(400, {"error": "要给 kind / reason / actor"})
        if not self.store.allow_reveal(self.client):
            return self._send(429, {"error": "取 key 太频繁"})
        conn = self.store.conn()
        try:
            row = conn.execute("SELECT s.*, a.key_alias, a.lark_record_id, a.status, a.owner_union_id FROM account_secret s "
                               "JOIN account a ON a.id = s.account_id WHERE a.id = ? AND s.kind = ?",
                               (int(m.group(1)), kind)).fetchone()
            who = f"{self.client}:{actor}"
            if row is None:
                return self._send(404, {"error": "没有这把 key"})
            if "reveal:any" not in self.scopes:
                if not behalf or behalf != row["owner_union_id"]:
                    AS.audit(conn, "reveal.denied", actor=who, account_id=row["account_id"], kind=kind, reason=reason,
                             detail={"on_behalf_of": behalf, "owner": row["owner_union_id"]})
                    return self._send(403, {"error": "只能取自己名下的 key"})
            if row["state"] != "encrypted":
                return self._send(409, {"error": f"这把 key 在库里是 {row['state']},取不回(请重置)"})
            try:
                pt = AS.reveal(conn, self.store.kek, self.store.kek_id, row, reason=reason, actor=who)
            except AS.DecryptError:
                return self._send(500, {"error": "解密失败,已记审计"})
            return self._send(200, {"account_id": row["account_id"], "kind": kind, "hint": row["hint"], "key": pt})
        finally:
            conn.close()

    def _backup(self):
        conn = self.store.conn()
        fd, tmp = tempfile.mkstemp(prefix="acct-bak-", suffix=".db", dir=os.environ.get("TMPDIR", "/tmp"))
        os.close(fd)
        try:
            dst = sqlite3.connect(tmp)
            conn.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
            dst.close()
            if ok != "ok":
                return self._send(500, {"error": f"integrity_check: {ok}"})
            AS.audit(conn, "backup.export", actor=f"{self.client}:http", detail={"bytes": os.path.getsize(tmp)})
            return self._send(200, raw=Path(tmp).read_bytes(), ctype="application/vnd.sqlite3")
        finally:
            conn.close()
            os.unlink(tmp)


def main() -> int:
    store = Store(os.environ.get("ACCOUNT_STORE_DB", "/data/accounts.db"),
                  os.environ.get("ACCOUNT_STORE_KEK", "/kek/kek.bin"),
                  os.environ.get("ACCOUNT_STORE_TOKENS", "/etc/account-store-tokens/tokens.json"))
    conn = store.conn()
    AS.log("启动", {"kek_id": store.kek_id, "clients": sorted(store.tokens), "verify": AS.verify(conn, store.kek, store.kek_id)})
    conn.close()
    Handler.store = store
    host, _, port = os.environ.get("LISTEN", "0.0.0.0:8080").rpartition(":")
    httpd = ThreadingHTTPServer((host, int(port)), Handler)
    httpd.daemon_threads = True
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
