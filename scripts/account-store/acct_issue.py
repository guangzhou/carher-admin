"""账户库发 key:POST /v1/accounts(scope issue)。server.py 只负责把请求路由过来。

11-04 用户拍板「以后所有的 cursor key 生成不走多维表格了,都走这儿」。规则和
scripts/hermestest-cron/litellm-key-poller.js 的 generateKey()(:687)**逐字段等价**:
  * 参照 key `cursor-wangzhongwei-w1qn`,每次现读(/key/list?key_alias=),抄 models / aliases /
    max_budget / budget_duration + metadata 里 REFERENCE_POLICY_METADATA_KEYS 这几个策略键;
  * 身份字段一律来自调用方传进来的申请人(账户库**不查飞书**),绝不抄参照 key 的个人信息;
  * team = 同名「纯分组」team 里最新建的那个(= findDepartmentTeamId,:671);
  * key_alias = cursor-<邮箱前缀>-<Date.now() 36 进制后 4 位>,user_id = cursor-<邮箱前缀>。
等价性由 test_issue.py::test_payload_equals_poller_generateKey 拿真 poller 代码逐字段比。

和 poller 有意不同的地方(都不影响 payload):
  * /key/generate **不重试**。poller 超时会重发(requestWithRetry),第一次其实建出来了就会多一把;
    这里超时/5xx 一律按 alias 删一次再报错。
  * 去重按 union_id 查**库里**的当前可用 Cursor key(status=active,客户批量 key 不算),
    poller 是按 open_id 查飞书表、不看状态。
  * 并发:库里一张锁表,同一个人同时只能有一个在生成;第二个请求拿 409 in_progress。

明文不出接口:回包只有 {account_id, key_alias, masked}。要明文另走 /reveal(进审计):
  * 门户:reveal:self,新行 owner_union_id = 申请人 ⇒ 刚生成的这把本人当场就能取;
  * poller:reveal:issued,只能取**本 client 在 ISSUED_REVEAL_WINDOW 秒内发出/复用过**的行(查审计)。
"""
from __future__ import annotations

import calendar
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

import account_store as AS

ACCOUNT_TYPE = "cursor"
REFERENCE_ALIAS = "cursor-wangzhongwei-w1qn"            # poller KEY_REFERENCE_ALIASES.cursor
REFERENCE_POLICY_METADATA_KEYS = ("budget_family_overrides", "allowed_passthrough_routes", "tag_rpm_limit",
                                  "enable_prompt_caching", "throttle_on_budget_exceeded")   # poller :92
SOURCE_BASE_TOKEN, SOURCE_TABLE_ID = "DlT9bsrwMad12VsogEpcK9Ptncc", "tblJT2s6Y6xjYj5A"   # poller APP_TOKEN/TABLE_ID
UNION_RE = re.compile(r"^on_[A-Za-z0-9]{8,64}$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REC_RE = re.compile(r"^rec[A-Za-z0-9]{1,40}$")
TEXT_MAX = 200
LOCK_TTL = int(os.environ.get("ACCOUNT_STORE_ISSUE_LOCK_TTL", "300"))
ISSUED_REVEAL_WINDOW = int(os.environ.get("ACCOUNT_STORE_ISSUED_REVEAL_WINDOW", "900"))
ISSUE_SCOPE = "issue"
LOCK_DDL = "CREATE TABLE IF NOT EXISTS issue_lock (union_id TEXT PRIMARY KEY, client TEXT, created REAL NOT NULL)"


class IssueError(Exception):
    def __init__(self, status: int, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.status, self.code, self.msg = status, code, msg


# --------------------------------------------------------------------------- 纯函数(等价性测试直接打)


def b36(n: int) -> str:
    """= JS Number.prototype.toString(36)(非负整数)。"""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def _js_str(t: dict, k: str) -> str:
    """= JS String(t[k]):缺 ⇒ "undefined",null ⇒ "null"。只给 team 排序用。"""
    if k not in t:
        return "undefined"
    v = t[k]
    return "null" if v is None else (v if isinstance(v, str) else json.dumps(v))


def pick_team_id(teams: list, department: str) -> str:
    """= poller findDepartmentTeamId 的过滤 + 排序(:679-683)。"""
    if not department:
        return ""
    ok = [t for t in teams if isinstance(t, dict) and t.get("team_alias") == department
          and t.get("max_budget") is None and t.get("tpm_limit") is None and t.get("rpm_limit") is None
          and not (t.get("models") or []) and not t.get("blocked")]
    ok = sorted(ok, key=lambda t: _js_str(t, "created_at"), reverse=True)   # 稳定排序,同 JS
    return (ok[0].get("team_id") or "") if ok else ""


def check_reference(ref: dict | None) -> dict:
    """= poller getKeyReference 的三道校验(:494-497)。"""
    if not ref:
        raise IssueError(502, "reference_missing", f"reference key not found: {REFERENCE_ALIAS}")
    if not isinstance(ref.get("models"), list) or not ref["models"]:
        raise IssueError(502, "reference_bad", f"reference key has no models: {REFERENCE_ALIAS}")
    if not ref.get("max_budget") or not ref.get("budget_duration"):
        raise IssueError(502, "reference_bad", f"reference key missing budget: {REFERENCE_ALIAS}")
    return ref


def build_payload(ref: dict, email_prefix: str, ident: dict, team_id: str, now_ms: int) -> dict:
    """= poller generateKey(masterKey, 'cursor', emailPrefix, identity) 发给 /key/generate 的 body(:687-724)。
    ident 键:record_id / name / open_id / email / job_title / department / organization。"""
    alias = f"cursor-{email_prefix}"
    ref_meta = ref.get("metadata") or {}
    policy = {k: ref_meta[k] for k in REFERENCE_POLICY_METADATA_KEYS if k in ref_meta}   # JS: !== undefined
    email = ident.get("email") or ""
    metadata = {
        **policy,
        "purpose": "cursor",
        "account_type": "Cursor",
        "source": "feishu_cursor_claude_code_base",
        "source_base_token": SOURCE_BASE_TOKEN,
        "source_table_id": SOURCE_TABLE_ID,
        "source_record_id": ident.get("record_id") or "",
        "owner_name": email_prefix,
        "base_alias": alias,
        "display_name": ident.get("name") or "",
        "lark_open_id": ident.get("open_id") or "",
        "email": email,
        "email_source": "enterprise_email" if email else "",
        "job_title": ident.get("job_title") or "",
        "department": ident.get("department") or "",
        "team_alias": ident.get("department") or "",
        "organization_alias": ident.get("organization") or "",
        "reference_alias": REFERENCE_ALIAS,
    }
    payload = {
        "key_alias": f"{alias}-{b36(now_ms)[-4:]}",
        "user_id": alias,
        "models": ref["models"],
        "max_budget": ref["max_budget"],
        "budget_duration": ref["budget_duration"],
        "metadata": metadata,
    }
    aliases = ref.get("aliases")
    if aliases and (not isinstance(aliases, dict) or len(aliases)):
        payload["aliases"] = aliases
    if team_id:
        payload["team_id"] = team_id
    return payload


def derive_prefix(email_prefix: str, email: str, open_id: str, union_id: str) -> str:
    """调用方没给前缀时:邮箱 @ 前 → open_id 尾 8 位 → union_id 尾 8 位(poller :891 同序,少了拼音那步)。"""
    for cand in (email_prefix, (email or "").split("@")[0], (open_id or "")[-8:], (union_id or "")[-8:]):
        if cand and PREFIX_RE.match(cand):
            return cand
    raise IssueError(400, "bad_prefix", "拿不到合法的邮箱前缀")


# --------------------------------------------------------------------------- 网关


class Gateway:
    """198 LiteLLM 的四个口。master key 每次现读文件(secret 轮换后挂载文件会自己更新),不进日志。"""

    def __init__(self, base: str, key_file: str, timeout: int = 60, backoff: tuple = (1, 3)):
        self.base, self.key_file, self.timeout, self.backoff = base.rstrip("/"), key_file, timeout, backoff

    def enabled(self) -> bool:
        try:
            return bool(self.base) and os.path.getsize(self.key_file) > 0
        except OSError:
            return False

    def _key(self) -> str:
        try:
            k = open(self.key_file).read().strip()
        except OSError:
            k = ""
        if not k:
            raise IssueError(503, "gateway_not_configured", "账户库没配网关 master key")
        return k

    def _http(self, method: str, path: str, body=None) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self._key(),
                                              **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                st = r.status
        except urllib.error.HTTPError as e:
            raw, st = e.read() or b"", e.code
        except (urllib.error.URLError, OSError) as e:
            raise IssueError(502, "gateway_unreachable", f"{path.split('?')[0]} {type(e).__name__}") from None
        try:
            return st, json.loads(raw or b"{}")
        except ValueError:
            return st, {"raw": raw[:200].decode("utf-8", "replace")}

    def get(self, path: str):
        """读接口:网络错 / 5xx 重试(同 poller requestWithRetry 的 1s、3s)。"""
        last = None
        for i in range(len(self.backoff) + 1):
            try:
                st, d = self._http("GET", path)
                if st < 500:
                    if st != 200:
                        raise IssueError(502, "gateway_error", f"{path.split('?')[0]} HTTP {st} {_msg(d)}")
                    return d
                last = IssueError(502, "gateway_error", f"{path.split('?')[0]} HTTP {st} {_msg(d)}")
            except IssueError as e:
                if e.code != "gateway_unreachable":
                    raise
                last = e
            if i < len(self.backoff):
                time.sleep(self.backoff[i])
        raise last

    def reference(self) -> dict:
        d = self.get("/key/list?return_full_object=true&size=100&key_alias=" + urllib.parse.quote(REFERENCE_ALIAS))
        keys = d.get("keys") if isinstance(d, dict) else None
        ref = next((k for k in (keys or []) if isinstance(k, dict) and k.get("key_alias") == REFERENCE_ALIAS
                    and k.get("blocked") is not True), None)
        return check_reference(ref)

    def team_id(self, department: str) -> str:
        if not department:
            return ""
        d = self.get("/team/list")
        teams = d if isinstance(d, list) else ((d or {}).get("teams") or (d or {}).get("data") or [])
        return pick_team_id(teams, department)

    def generate(self, payload: dict) -> str:
        st, d = self._http("POST", "/key/generate", payload)
        key = d.get("key") if isinstance(d, dict) else None
        if st != 200 or not isinstance(key, str) or not key.startswith("sk-"):
            raise IssueError(502, "generate_failed", f"HTTP {st} {_msg(d)}")
        return key

    def delete_alias(self, alias: str) -> bool:
        try:
            st, _ = self._http("POST", "/key/delete", {"key_aliases": [alias]})
            return st == 200
        except IssueError:
            return False


def _msg(d) -> str:
    """错误信封里的人话;不回显整包(请求体可能被回显)。"""
    if not isinstance(d, dict):
        return ""
    e = d.get("error")
    if isinstance(e, dict):
        return AS.redact(str(e.get("message") or e.get("type") or ""))[:200]
    return AS.redact(str(e or d.get("message") or d.get("detail") or d.get("raw") or ""))[:200]


# --------------------------------------------------------------------------- 去重 / 锁


def now_ms() -> int:          # 测试里换掉,让 poller 和这里拿同一个时间算后缀
    return int(time.time() * 1000)


EXCLUDE_ORGS = {s.strip() for s in os.environ.get("ACCOUNT_STORE_EXCLUDE_ORGS", "巧虎,美团,北汽研究院").split(",") if s.strip()}
BATCH_MIN_ROWS, BATCH_GAP_S = 5, 60          # = 门户 BATCH_MIN_ROWS / BATCH_GAP_MS


def _iso_s(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def current_key(conn: sqlite3.Connection, union_id: str) -> sqlite3.Row | None:
    """这个人**当前可用**的 Cursor key(规则照门户 my_rows 的 current):
    status=active + cursor + 有可解密的 api_key;客户批量 key 不算(组织命中 EXCLUDE_ORGS,或一串相邻 ≤60s、≥5 行)。"""
    rows = conn.execute("SELECT id, org_name, applied_at FROM account WHERE owner_union_id = ?", (union_id,)).fetchall()
    timed = sorted((r for r in rows if _iso_s(r["applied_at"]) is not None), key=lambda r: _iso_s(r["applied_at"]))
    batch, run = set(), []
    for r in timed + [None]:
        if r is not None and run and _iso_s(r["applied_at"]) - _iso_s(run[-1]["applied_at"]) <= BATCH_GAP_S:
            run.append(r)
            continue
        if len(run) >= BATCH_MIN_ROWS:
            batch.update(x["id"] for x in run)
        run = [r] if r is not None else []
    cand = conn.execute(
        "SELECT a.id, a.key_alias, a.org_name, a.applied_at, a.models, a.daily_budget, a.email_prefix, s.hint "
        "FROM account a JOIN account_secret s ON s.account_id = a.id AND s.kind = 'api_key' AND s.state = 'encrypted' "
        "WHERE a.owner_union_id = ? AND a.account_type = 'cursor' AND a.status = 'active'", (union_id,)).fetchall()
    cand = [c for c in cand if c["id"] not in batch and (c["org_name"] or "") not in EXCLUDE_ORGS]
    return max(cand, key=lambda c: (c["applied_at"] or "", c["id"])) if cand else None


def lock(conn: sqlite3.Connection, union_id: str, client: str) -> bool:
    """主键 = union_id ⇒ 同一人同时只有一个 INSERT 成功(跨线程、跨连接都成立)。"""
    conn.execute(LOCK_DDL)
    conn.execute("DELETE FROM issue_lock WHERE created < ?", (time.time() - LOCK_TTL,))
    try:
        conn.execute("INSERT INTO issue_lock(union_id, client, created) VALUES (?,?,?)", (union_id, client, time.time()))
        return True
    except sqlite3.IntegrityError:
        return False


def unlock(conn: sqlite3.Connection, union_id: str) -> None:
    conn.execute("DELETE FROM issue_lock WHERE union_id = ?", (union_id,))


# --------------------------------------------------------------------------- 主流程

_STR_FIELDS = ("name", "open_id", "open_id_app", "email", "tenant_key", "department", "organization",
               "job_title", "record_id", "email_prefix", "actor", "source")


def parse_body(body: dict) -> dict:
    if not isinstance(body, dict):
        raise IssueError(400, "bad_body", "body 要是 JSON 对象")
    if (body.get("account_type") or ACCOUNT_TYPE) not in (ACCOUNT_TYPE, "Cursor"):
        raise IssueError(400, "bad_type", "只发 Cursor key")
    uid = body.get("union_id")
    if not isinstance(uid, str) or not UNION_RE.match(uid):
        raise IssueError(400, "bad_union_id", "union_id 要形如 on_xxx")
    out = {"union_id": uid}
    for k in _STR_FIELDS:
        v = body.get(k)
        if v is not None and not isinstance(v, str):
            raise IssueError(400, "bad_field", f"{k} 要是字符串")
        v = (v or "").strip()
        if len(v) > TEXT_MAX:
            raise IssueError(400, "bad_field", f"{k} 太长")
        out[k] = v
    if out["record_id"] and not REC_RE.match(out["record_id"]):
        raise IssueError(400, "bad_record_id", "record_id 不对")
    if out["email_prefix"] and not PREFIX_RE.match(out["email_prefix"]):
        raise IssueError(400, "bad_prefix", "email_prefix 只能是字母数字 . _ -")
    if not out["actor"]:
        raise IssueError(400, "bad_actor", "要给 actor(谁触发的这次申请)")
    return out


def _view(acct_id: int, alias: str, hint: str, existing: bool, models, max_budget, budget_duration,
          team_id: str, prefix: str) -> dict:
    """回包:**没有明文**。models 等非机密字段给调用方回写飞书表用。"""
    return {"account_id": acct_id, "key_alias": alias, "masked": hint, "existing": existing,
            "models": models, "max_budget": max_budget, "budget_duration": budget_duration,
            "team_id": team_id, "email_prefix": prefix}


def _existing_view(row: sqlite3.Row) -> dict:
    models = [m.strip() for m in (row["models"] or "").split(",") if m.strip()]
    return _view(row["id"], row["key_alias"] or "", row["hint"] or "", True, models, row["daily_budget"],
                 None, "", row["email_prefix"] or "")


def issue(store, client: str, body: dict) -> tuple[int, dict]:
    """返回 (HTTP 状态码, 回包)。成功 / 复用 / 失败都写审计;回包里没有明文。"""
    who = f"{client}:{(body or {}).get('actor') or '?'}" if isinstance(body, dict) else f"{client}:?"
    conn = store.conn()
    try:
        try:
            p = parse_body(body)
        except IssueError as e:
            AS.audit(conn, "issue.fail", actor=who, kind="api_key", reason=e.code, detail={"msg": e.msg})
            return e.status, {"error": e.code, "message": e.msg}
        who = f"{client}:{p['actor']}"
        base = {"union_id": p["union_id"], "record_id": p["record_id"], "source": p["source"]}
        cur = current_key(conn, p["union_id"])
        if cur is not None:
            AS.audit(conn, "issue.existing", actor=who, account_id=cur["id"], kind="api_key",
                     detail={**base, "alias": cur["key_alias"], "hint": cur["hint"]})
            return 200, _existing_view(cur)
        if not store.gateway.enabled():
            AS.audit(conn, "issue.fail", actor=who, kind="api_key", reason="gateway_not_configured", detail=base)
            return 503, {"error": "gateway_not_configured", "message": "账户库没配网关,暂时不能发 key"}
        if not lock(conn, p["union_id"], client):
            AS.audit(conn, "issue.busy", actor=who, kind="api_key", reason="in_progress", detail=base)
            return 409, {"error": "in_progress", "message": "这个人的 key 正在生成,稍等几秒再查"}
        try:
            cur = current_key(conn, p["union_id"])      # 拿到锁后再查一遍:前一个请求可能刚刚建完
            if cur is not None:
                AS.audit(conn, "issue.existing", actor=who, account_id=cur["id"], kind="api_key",
                         detail={**base, "alias": cur["key_alias"], "hint": cur["hint"]})
                return 200, _existing_view(cur)
            return _issue_locked(store, conn, who, p, base)
        finally:
            unlock(conn, p["union_id"])
    finally:
        conn.close()


def _issue_locked(store, conn, who: str, p: dict, base: dict) -> tuple[int, dict]:
    gw = store.gateway
    try:
        prefix = derive_prefix(p["email_prefix"], p["email"], p["open_id"], p["union_id"])
        ref = gw.reference()
        team_id = gw.team_id(p["department"])
        ident = {k: p[k] for k in ("record_id", "name", "open_id", "email", "job_title", "department", "organization")}
        payload = build_payload(ref, prefix, ident, team_id, now_ms())
    except IssueError as e:
        AS.audit(conn, "issue.fail", actor=who, kind="api_key", reason=e.code, detail={**base, "msg": e.msg})
        return e.status, {"error": e.code, "message": e.msg}
    alias = payload["key_alias"]
    try:
        plaintext = gw.generate(payload)
    except IssueError as e:
        # 不重试。网络断在半路时网关可能已经建出来了 ⇒ 按 alias 删一次,不留一把没人知道的活 key
        cleaned = gw.delete_alias(alias) if e.code == "gateway_unreachable" or "HTTP 5" in e.msg else None
        AS.audit(conn, "issue.fail", actor=who, kind="api_key", reason=e.code,
                 detail={**base, "alias": alias, "msg": e.msg, "cleanup": cleaned})
        return e.status, {"error": e.code, "message": "网关发 key 失败,没有建出 key,稍后再试"}
    try:
        acct_id = _insert(store, conn, p, payload, plaintext, team_id, prefix)
    except Exception as e:  # noqa: BLE001 —— 入库失败:网关上那把删掉,宁可重来也不留孤儿
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        cleaned = gw.delete_alias(alias)
        AS.audit(conn, "issue.fail", actor=who, kind="api_key", reason="store_failed",
                 detail={**base, "alias": alias, "err": type(e).__name__, "cleanup": cleaned})
        return 500, {"error": "store_failed", "message": "key 入库失败,已撤销,稍后再试"}
    hint = AS.key_hint(plaintext)
    AS.audit(conn, "issue.ok", actor=who, account_id=acct_id, kind="api_key",
             detail={**base, "alias": alias, "hint": hint, "team_id": team_id, "department": p["department"],
                     "organization": p["organization"], "reference": REFERENCE_ALIAS})
    AS.log("issue.ok", who, acct_id, alias, hint)
    return 200, _view(acct_id, alias, hint, False, payload["models"], payload["max_budget"],
                      payload["budget_duration"], team_id, prefix)


def _insert(store, conn, p: dict, payload: dict, plaintext: str, team_id: str, prefix: str) -> int:
    ts = AS.now_iso()
    sha, ct, wrapped = AS.encrypt_secret(store.kek, store.kek_id, "api_key", plaintext)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR IGNORE INTO person(union_id, open_id, name, email, updated_at) VALUES (?,?,?,?,?)",
                 (p["union_id"], p["open_id"] or None, p["name"] or None, p["email"] or None, ts))
    vals = {"owner_union_id": p["union_id"], "owner_open_id": p["open_id"] or None, "owner_name": p["name"] or None,
            "submitter_union_id": p["union_id"], "submitter_open_id": p["open_id"] or None,
            "submitter_name": p["name"] or None, "account_type": ACCOUNT_TYPE, "status": "active", "status_raw": "有效",
            "org_name": p["organization"] or None,
            "dept_names": json.dumps([p["department"]], ensure_ascii=False) if p["department"] else None,
            "email_prefix": prefix, "key_alias": payload["key_alias"], "models": ", ".join(payload["models"]),
            "daily_budget": payload["max_budget"], "applied_at": ts}
    row = conn.execute("SELECT id FROM account WHERE lark_record_id = ?", (p["record_id"],)).fetchone() \
        if p["record_id"] else None
    if row is not None:
        # 表单行在迁移时已经进库(空 key、状态未知)⇒ 就地补上,不另起一行
        acct_id = row["id"]
        conn.execute(f"UPDATE account SET {', '.join(k + '=?' for k in vals)}, updated_at=? WHERE id=?",
                     (*vals.values(), ts, acct_id))
        conn.execute("DELETE FROM account_secret WHERE account_id = ? AND kind = 'api_key'", (acct_id,))
    else:
        cols = ["lark_record_id", *vals]
        cur = conn.execute(f"INSERT INTO account({', '.join(cols)}, created_at, updated_at) "
                           f"VALUES ({','.join('?' * (len(cols) + 2))})",
                           (p["record_id"] or None, *vals.values(), ts, ts))
        acct_id = cur.lastrowid
    conn.execute("INSERT INTO account_secret(account_id, kind, state, sha256, sha256_prefix, hint, key_len, ciphertext,"
                 " wrapped_dek, kek_id, created_at, updated_at) VALUES (?,'api_key','encrypted',?,?,?,?,?,?,?,?,?)",
                 (acct_id, sha, sha[:8], AS.key_hint(plaintext), len(plaintext), ct, wrapped, store.kek_id, ts, ts))
    conn.execute("COMMIT")
    return acct_id


def link_record(store, client: str, acct_id: int, body: dict) -> tuple[int, dict]:
    """双写期:门户先建库行、后写飞书,飞书那行的 record_id 回填到 lark_record_id(只填空的,不改已有的)。
    这样以后重跑迁移会认出同一行,不会再插一行重复的。"""
    rid = (body or {}).get("record_id") if isinstance(body, dict) else None
    actor = str((body or {}).get("actor") or "?")[:TEXT_MAX] if isinstance(body, dict) else "?"
    if not isinstance(rid, str) or not REC_RE.match(rid):
        return 400, {"error": "bad_record_id", "message": "record_id 不对"}
    conn = store.conn()
    try:
        row = conn.execute("SELECT lark_record_id FROM account WHERE id = ?", (acct_id,)).fetchone()
        if row is None:
            return 404, {"error": "not_found", "message": "没有这一行"}
        if row["lark_record_id"] == rid:
            return 200, {"account_id": acct_id, "lark_record_id": rid}
        if row["lark_record_id"]:
            return 409, {"error": "already_linked", "message": "这一行已经对应了别的飞书记录"}
        if not conn.execute("SELECT 1 FROM audit_log WHERE action = 'issue.ok' AND account_id = ? AND actor LIKE ?",
                            (acct_id, client + ":%")).fetchone():
            return 403, {"error": "not_issued_by_you", "message": "只能回填自己发的行"}
        try:
            conn.execute("UPDATE account SET lark_record_id = ?, updated_at = ? WHERE id = ? AND lark_record_id IS NULL",
                         (rid, AS.now_iso(), acct_id))
        except sqlite3.IntegrityError:
            return 409, {"error": "record_taken", "message": "这条飞书记录已经对应了库里另一行"}
        AS.audit(conn, "issue.link", actor=f"{client}:{actor}", account_id=acct_id, detail={"record_id": rid})
        return 200, {"account_id": acct_id, "lark_record_id": rid}
    finally:
        conn.close()


def issued_by(conn: sqlite3.Connection, client: str, acct_id: int) -> bool:
    """reveal:issued 的判据:本 client 在 ISSUED_REVEAL_WINDOW 秒内对这一行发过 / 复用过(查审计,不另存状态)。"""
    t = time.time() - ISSUED_REVEAL_WINDOW
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"
    return conn.execute("SELECT 1 FROM audit_log WHERE action IN ('issue.ok','issue.existing') AND account_id = ? "
                        "AND actor LIKE ? ESCAPE '\\' AND ts >= ?",
                        (acct_id, re.sub(r"([%_\\])", r"\\\1", client) + ":%", since)).fetchone() is not None
