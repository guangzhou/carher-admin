#!/usr/bin/env python3
"""一次性迁移:飞书「账户表」全量导出 → 账户库(SQLite)。默认 dry-run,幂等,可重跑。

    # 本地验证(脱敏快照,明文已打码 ⇒ key 进库状态为 masked_in_source):
    python3 migrate_from_lark.py --snapshot .work/ai-portal/data/records_union_scrubbed.json --db /tmp/a.db
    python3 migrate_from_lark.py --snapshot ... --db /tmp/a.db --kek-file ~/kek.bin --apply

    # 正式迁移(在 198 上 root 跑;本仓库这一轮不执行):
    python3 migrate_from_lark.py --from-lark --lark-creds /root/.config/account-store/lark.json \
        --db /Data/account-store/accounts.db --kek-file /etc/account-store/kek.bin \
        --gateway-198-local --apply

来源二选一:
  --snapshot FILE   records/search 返回的 items 列表(JSON)。
  --from-lark       直接调飞书 bitable records/search(user_id_type=union_id),只读。凭据从
                    --lark-creds(JSON: {"app_id":..,"app_secret":..},须 0600)读,不走命令行参数。

网关对账(可选,只读):
  --gateway-hashes FILE   每行一个 sha256(LiteLLM_VerificationToken.token)
  --gateway-198-ssh       ssh 198 → kubectl exec litellm-db-0 → psql,READ ONLY 事务
  --gateway-198-local     在 198 本机直接 kubectl exec(迁移在 198 上跑时用)

日志纪律:任何一行记录在打印/写盘前都先整行过 scrub(sk- 只留后 4 位);异常信息同样过 scrub。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_store as AS  # noqa: E402

APP_TOKEN = "DlT9bsrwMad12VsogEpcK9Ptncc"
TABLE_ID = "tblJT2s6Y6xjYj5A"
LARK_API = "https://open.feishu.cn/open-apis"

STATUS_MAP = {"有效": "active", "有效（已重新生成）": "active", "已禁用": "disabled", "已完成": "completed"}
TYPE_MAP = {"Cursor": "cursor", "Claude Code": "claude_code", "CarHer": "carher"}
_SK_FULL = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_SK_MASKED = re.compile(r"^sk-…([A-Za-z0-9_\-]{1,8})(?:\(len=(\d+)\))?$")


def scrub_record(rec: dict) -> dict:
    """整行打码:任何位置出现的 sk- token 都只留后 4 位。打日志/落盘前必须先过这里。"""
    return json.loads(AS.redact(json.dumps(rec, ensure_ascii=False)))


# --------------------------------------------------------------------------- field parsing


def text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        if "value" in v:                     # formula / lookup
            return text(v["value"])
        if "link" in v:                      # url
            return v.get("link")
        return v.get("text") or v.get("name")
    if isinstance(v, list):
        parts = [text(x) for x in v]
        s = "".join(p for p in parts if p)
        return s or None
    return str(v)


def str_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    out = []
    for x in v:
        t = x if isinstance(x, str) else text(x)
        if t:
            out.append(t)
    return out


def user(v) -> dict | None:
    if not v:
        return None
    u = v[0] if isinstance(v, list) else v
    return {"union_id": u.get("id"), "name": u.get("name"), "en_name": u.get("en_name"), "email": u.get("email")}


def num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ms_to_iso(v) -> str | None:
    if v in (None, ""):
        return None
    import time as _t

    sec = int(v) / 1000
    return _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime(sec)) + ".%03dZ" % (int(v) % 1000)


def probe_link_key(v) -> str | None:
    """从 …/keyprobe/#k=<key> 里取出 key 部分(可能是明文,也可能是打码形态)。"""
    s = text(v)
    if not s or "#k=" not in s:
        return None
    return urllib.parse.unquote(s.split("#k=", 1)[1]) or None


def classify_secret(raw: str | None) -> dict | None:
    """把一格 key 文本分成 plaintext / masked / sentinel / note。返回 None 表示空。"""
    if raw is None or not str(raw).strip():
        return None
    s = str(raw).strip()
    if _SK_FULL.fullmatch(s):
        return {"form": "plaintext", "plaintext": s}
    m = _SK_MASKED.match(s)
    if m:
        return {"form": "masked", "hint": "sk-…" + m.group(1)[-4:], "key_len": int(m.group(2)) if m.group(2) else None}
    if s.startswith("<non-sk:") or s.startswith("补录"):
        return {"form": "sentinel", "note": s[:60]}
    if s.startswith("sk-"):
        # 以 sk- 开头但不像 key(太短、含其他字符):绝不当明文落库,也绝不打印原文
        return {"form": "sentinel", "note": AS.redact(s)[:60]}
    return {"form": "note", "note": s[:60]}


def normalize(rec: dict) -> dict:
    """飞书一行 → 账户行(非机密)+ 机密候选列表。不做任何 IO。"""
    f = rec.get("fields") or {}
    owner, submitter = user(f.get("姓名")), user(f.get("提交人"))
    status_raw = f.get("状态")
    if status_raw is None:
        status = "unknown"
    elif str(status_raw).startswith("生成失败"):
        status = "failed"
    else:
        status = STATUS_MAP.get(status_raw, "unknown")
    types = f.get("账户类型") or []
    parent = (f.get("父记录") or {}).get("link_record_ids") or []
    serial = f.get("编号")
    acct = {
        "lark_record_id": rec["record_id"],
        "lark_serial": int(serial) if serial not in (None, "") and str(serial).isdigit() else None,
        "lark_parent_record_id": parent[0] if parent else None,
        "owner_union_id": owner and owner["union_id"],
        "owner_open_id": text(f.get("open_id")),
        "owner_name": owner and owner["name"],
        "submitter_union_id": submitter and submitter["union_id"],
        "submitter_open_id": None,
        "submitter_name": submitter and submitter["name"],
        "account_type": TYPE_MAP.get(types[0]) if types else None,
        "status": status,
        "status_raw": status_raw,
        "org_name": f.get("组织名称") or None,
        "dept_names": json.dumps(str_list(f.get("部门名称")), ensure_ascii=False) if f.get("部门名称") else None,
        "dept_text": text(f.get("部门")),
        "email_prefix": text(f.get("邮箱前缀")),
        "key_alias": text(f.get("key_alias")),
        "models": text(f.get("可用模型")),
        "cursor_models": text(f.get("Cursor 可用模型")),
        "claude_code_models": text(f.get("Claude Code 可用模型")),
        "daily_budget": num(f.get("每日预算")),
        "premium_daily_budget": num(f.get("高配额度($/天)")),
        "total_spend_usd": num(f.get("累计消费($)")),
        "pioneer": json.dumps(str_list(f.get("先锋")), ensure_ascii=False) if f.get("先锋") else None,
        "experiment": json.dumps(str_list(f.get("实验")), ensure_ascii=False) if f.get("实验") else None,
        "applied_at": ms_to_iso(f.get("申请时间")),
    }
    warnings = []
    if len(types) > 1:
        warnings.append(f"账户类型多选 {types},只取第一个")
    if types and types[0] not in TYPE_MAP:
        warnings.append(f"未知账户类型 {types[0]}")
    if status == "unknown" and status_raw is not None:
        warnings.append(f"未知状态 {status_raw}")

    person = None
    if owner and owner["union_id"]:
        person = {**owner, "open_id": acct["owner_open_id"], "employment": f.get("是否在职"),
                  "title": text(f.get("姓名.职务")),
                  "lark_depts": json.dumps(str_list(f.get("姓名.部门")), ensure_ascii=False) if f.get("姓名.部门") else None,
                  "_applied_at": acct["applied_at"] or ""}

    secrets_, notes = {}, {}
    for kind, col in (("api_key", "API Key"), ("cursor_key", "Cursor Key"), ("claude_code_key", "Claude Code Key")):
        c = classify_secret(text(f.get(col)))
        if c is None:
            continue
        if c["form"] == "note":
            notes[col] = c["note"]
        else:
            secrets_[kind] = c
    api = secrets_.get("api_key")

    def _same(a: dict, b: dict) -> bool:
        if a["form"] == "plaintext" and b["form"] == "plaintext":
            return a["plaintext"] == b["plaintext"]
        ha = a.get("hint") or AS.key_hint(a.get("plaintext", ""))
        hb = b.get("hint") or AS.key_hint(b.get("plaintext", ""))
        return a["form"] in ("plaintext", "masked") and b["form"] in ("plaintext", "masked") and ha == hb

    # 旧双 key 列里如果就是 API Key 那一把,不再存第二份密文,只记一笔
    for kind, col in (("cursor_key", "Cursor Key"), ("claude_code_key", "Claude Code Key")):
        if kind in secrets_ and api and _same(secrets_[kind], api):
            del secrets_[kind]
            notes[col] = "same_as_api_key"
    if api and api["form"] == "sentinel":  # 补录行:sha256 前缀在体检链接里
        m = re.search(r"sha256 ([0-9a-f]{6,})", text(f.get("体检链接")) or "")
        if m:
            api["note"] += f" sha256 {m.group(1)}"
    # 体检链接(公式)就是 API Key 的派生,不单独存;「点我体检」是 URL 字段,改发后可能残留旧 key。
    for col in ("点我体检", "体检链接"):
        k = probe_link_key(f.get(col))
        c = classify_secret(k) if k else None
        if not c or c["form"] not in ("plaintext", "masked") or not api:
            continue
        if not _same(c, api) and "stale_probe_link" not in secrets_:
            secrets_["stale_probe_link"] = c
            warnings.append(f"{col} 里的 key 和 API Key 不是同一把(改发后残留的旧 key)")
    acct["legacy_notes"] = json.dumps(notes, ensure_ascii=False) if notes else None
    digest_src = {k: v for k, v in acct.items()}
    acct["source_digest"] = hashlib.sha256(json.dumps(digest_src, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return {"account": acct, "person": person, "secrets": secrets_, "warnings": warnings}


# --------------------------------------------------------------------------- sources


def load_snapshot(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data.get("items") or (data.get("data") or {}).get("items") or []
    return data


def _http(method, path, token=None, body=None, params=None):
    url = LARK_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:  # noqa: BLE001
            return {"code": -1, "msg": f"http {e.code}"}


def load_from_lark(creds_path: str) -> list[dict]:
    st = os.stat(creds_path)
    if st.st_mode & 0o077:
        raise SystemExit(f"飞书凭据文件权限过宽({stat.filemode(st.st_mode)}),要 0600")
    c = json.loads(Path(creds_path).read_text())
    j = _http("POST", "/auth/v3/tenant_access_token/internal", body={"app_id": c["app_id"], "app_secret": c["app_secret"]})
    if j.get("code") != 0:
        raise SystemExit(f"取 tenant token 失败 code={j.get('code')} msg={j.get('msg')}")
    tok, items, pt = j["tenant_access_token"], [], None
    while True:
        params = {"page_size": 500, "user_id_type": "union_id", **({"page_token": pt} if pt else {})}
        j = _http("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/search", tok, body={}, params=params)
        if j.get("code") != 0:
            raise SystemExit(f"records/search 失败 code={j.get('code')} msg={AS.redact(j.get('msg'))}")
        items += j["data"].get("items") or []
        if not j["data"].get("has_more"):
            total = j["data"].get("total")
            break
        pt = j["data"]["page_token"]
    if total is not None and total != len(items):
        raise SystemExit(f"分页读到 {len(items)} 行,但表报 total={total};不完整,拒绝继续")
    return items


GATEWAY_SQL = "select token from \"LiteLLM_VerificationToken\""


def load_gateway_hashes(args) -> set[str] | None:
    if args.gateway_hashes:
        return {l.strip() for l in Path(args.gateway_hashes).read_text().splitlines() if re.fullmatch(r"[0-9a-f]{64}", l.strip())}
    if not (args.gateway_198_ssh or args.gateway_198_local):
        return None
    inner = ("kubectl exec -i -n litellm-product litellm-db-0 -- sh -c "
             + shlex.quote('psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -q -At -v ON_ERROR_STOP=1 '
                           '-c "SET default_transaction_read_only=on" -c "SET statement_timeout=20000" -c "$1"')
             + " sh " + shlex.quote(GATEWAY_SQL))
    script = "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; " + inner
    cmd = (["ssh", "-o", "BatchMode=yes", "cltx@10.68.13.198", "sudo -n bash -c " + shlex.quote(script)]
           if args.gateway_198_ssh else ["bash", "-c", script])
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    hs = {l.strip() for l in p.stdout.splitlines() if re.fullmatch(r"[0-9a-f]{64}", l.strip())}
    if not hs:
        raise SystemExit(f"读网关 token 失败 rc={p.returncode}: {AS.redact(p.stderr[-300:])}")
    return hs


# --------------------------------------------------------------------------- write


ACCOUNT_COLS = ["lark_record_id", "lark_serial", "lark_parent_record_id", "owner_union_id", "owner_open_id",
                "owner_name", "submitter_union_id", "submitter_open_id", "submitter_name", "account_type",
                "status", "status_raw", "org_name", "dept_names", "dept_text", "email_prefix", "key_alias",
                "models", "cursor_models", "claude_code_models", "daily_budget", "premium_daily_budget",
                "total_spend_usd", "pioneer", "experiment", "legacy_notes", "applied_at", "source_digest"]


def _secret_target(kind: str, c: dict, kek, kek_id, mode: str) -> dict:
    """候选 → 目标 account_secret 行(不含 id/account_id/时间)。"""
    if c["form"] == "masked":
        return {"state": "masked_in_source", "sha256": None, "sha256_prefix": None, "hint": c["hint"],
                "key_len": c.get("key_len"), "ciphertext": None, "wrapped_dek": None, "kek_id": None}
    if c["form"] == "sentinel":
        m = re.search(r"sha256 ([0-9a-f]{6,})", c["note"])
        return {"state": "sentinel", "sha256": None, "sha256_prefix": m.group(1) if m else None, "hint": None,
                "key_len": None, "ciphertext": None, "wrapped_dek": None, "kek_id": None}
    pt = c["plaintext"]
    base = {"sha256": AS.sha256_hex(pt), "hint": AS.key_hint(pt), "key_len": len(pt)}
    base["sha256_prefix"] = base["sha256"][:8]
    if kind == "stale_probe_link" or mode == "hash-only":
        return {**base, "state": "hash_only", "ciphertext": None, "wrapped_dek": None, "kek_id": None}
    if kek is None:
        raise SystemExit("源里有明文 key,但没给 --kek-file;要么给主密钥,要么 --secret-mode hash-only")
    sha, ct, wrapped = AS.encrypt_secret(kek, kek_id, kind, pt)
    return {**base, "state": "encrypted", "ciphertext": ct, "wrapped_dek": wrapped, "kek_id": kek_id}


def apply_rows(conn: sqlite3.Connection, normalized: list[dict], *, kek, kek_id, mode: str) -> Counter:
    stat_ = Counter()
    ts = AS.now_iso()
    # 人:同一 union_id 取申请时间最新的那行的属性
    persons = {}
    for n in normalized:
        p = n["person"]
        if p and (p["union_id"] not in persons or p["_applied_at"] >= persons[p["union_id"]]["_applied_at"]):
            persons[p["union_id"]] = p
    for p in persons.values():
        cur = conn.execute("SELECT open_id, name, en_name, email, employment, title, lark_depts FROM person WHERE union_id=?",
                           (p["union_id"],)).fetchone()
        vals = (p["open_id"], p["name"], p["en_name"], p["email"], p["employment"], p["title"], p["lark_depts"])
        if cur is None:
            conn.execute("INSERT INTO person(union_id, open_id, name, en_name, email, employment, title, lark_depts, updated_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?)", (p["union_id"], *vals, ts))
            stat_["person.insert"] += 1
        elif tuple(cur) != vals:
            conn.execute("UPDATE person SET open_id=?, name=?, en_name=?, email=?, employment=?, title=?, lark_depts=?, updated_at=?"
                         " WHERE union_id=?", (*vals, ts, p["union_id"]))
            stat_["person.update"] += 1
        else:
            stat_["person.same"] += 1

    for n in normalized:
        a = n["account"]
        row = conn.execute("SELECT id, source_digest FROM account WHERE lark_record_id=?", (a["lark_record_id"],)).fetchone()
        if row is None:
            cur = conn.execute(
                f"INSERT INTO account({', '.join(ACCOUNT_COLS)}, created_at, updated_at) VALUES ({', '.join('?' * (len(ACCOUNT_COLS) + 2))})",
                (*[a[c] for c in ACCOUNT_COLS], ts, ts))
            acct_id = cur.lastrowid
            stat_["account.insert"] += 1
        else:
            acct_id = row["id"]
            if row["source_digest"] != a["source_digest"]:
                conn.execute(f"UPDATE account SET {', '.join(c + '=?' for c in ACCOUNT_COLS)}, updated_at=? WHERE id=?",
                             (*[a[c] for c in ACCOUNT_COLS], ts, acct_id))
                stat_["account.update"] += 1
            else:
                stat_["account.same"] += 1

        existing = {r["kind"]: r for r in conn.execute("SELECT * FROM account_secret WHERE account_id=?", (acct_id,))}
        for kind, c in n["secrets"].items():
            old = existing.pop(kind, None)
            # 幂等:同一把明文(sha256 相同)且状态不变 ⇒ 不重新加密
            if old is not None and c["form"] == "plaintext" and old["sha256"] == AS.sha256_hex(c["plaintext"]) \
                    and old["state"] == ("hash_only" if (kind == "stale_probe_link" or mode == "hash-only") else "encrypted") \
                    and (old["state"] != "encrypted" or old["kek_id"] == kek_id):
                stat_[f"secret.{kind}.same"] += 1
                continue
            t = _secret_target(kind, c, kek, kek_id, mode)
            if old is not None and c["form"] != "plaintext" and all(old[k] == t[k] for k in ("state", "hint", "sha256_prefix", "key_len")):
                stat_[f"secret.{kind}.same"] += 1
                continue
            cols = ["state", "sha256", "sha256_prefix", "hint", "key_len", "ciphertext", "wrapped_dek", "kek_id"]
            if old is None:
                conn.execute(f"INSERT INTO account_secret(account_id, kind, {', '.join(cols)}, created_at, updated_at)"
                             f" VALUES (?,?,{','.join('?' * len(cols))},?,?)", (acct_id, kind, *[t[k] for k in cols], ts, ts))
                stat_[f"secret.{kind}.insert"] += 1
            else:
                conn.execute(f"UPDATE account_secret SET {', '.join(k + '=?' for k in cols)}, updated_at=? WHERE id=?",
                             (*[t[k] for k in cols], ts, old["id"]))
                stat_[f"secret.{kind}.update"] += 1
                AS.audit(conn, "secret.replace", actor="migrate_from_lark", account_id=acct_id, kind=kind,
                         detail={"old_hint": old["hint"], "new_hint": t["hint"], "old_state": old["state"], "new_state": t["state"]})
        for kind, old in existing.items():   # 源里这一格已经空了:库里保留(不静默删),只统计
            stat_[f"secret.{kind}.gone_in_source"] += 1
    return stat_


# --------------------------------------------------------------------------- reconcile


def reconcile(conn: sqlite3.Connection, normalized: list[dict], gateway: set[str] | None, *, kek, kek_id) -> dict:
    src_ids = {n["account"]["lark_record_id"] for n in normalized}
    db_ids = {r[0] for r in conn.execute("SELECT lark_record_id FROM account WHERE lark_record_id IS NOT NULL")}
    rep = {"source_rows": len(normalized), "db_rows": len(db_ids), "missing_in_db": len(src_ids - db_ids),
           "extra_in_db": len(db_ids - src_ids)}
    sha_ok = sha_bad = decrypt_ok = decrypt_bad = masked = sentinel = hint_bad = 0
    gw_total = gw_hit = 0
    gw_by_status = Counter()
    for n in normalized:
        rid = n["account"]["lark_record_id"]
        for kind, c in n["secrets"].items():
            r = conn.execute("SELECT s.* FROM account_secret s JOIN account a ON a.id=s.account_id "
                             "WHERE a.lark_record_id=? AND s.kind=?", (rid, kind)).fetchone()
            if r is None:
                sha_bad += 1
                continue
            if c["form"] == "masked":          # 源里没有明文:只能核对后 4 位
                masked += 1
                hint_bad += r["hint"] != c["hint"] or r["state"] != "masked_in_source"
                continue
            if c["form"] == "sentinel":
                sentinel += 1
                continue
            want = AS.sha256_hex(c["plaintext"])
            good = r["sha256"] == want
            if good and r["state"] == "encrypted" and kek is not None:
                try:
                    AS.decrypt_secret(kek, kek_id, kind, r["sha256"], r["ciphertext"], r["wrapped_dek"], row_kek_id=r["kek_id"])
                    decrypt_ok += 1
                except AS.DecryptError:
                    decrypt_bad += 1
                    good = False
            sha_ok += good
            sha_bad += not good
            if gateway is not None and kind in ("api_key", "cursor_key", "claude_code_key"):
                gw_total += 1
                hit = want in gateway
                gw_hit += hit
                gw_by_status[(n["account"]["status"], "hit" if hit else "miss")] += 1
    rep.update({"secret_sha_ok": sha_ok, "secret_sha_bad": sha_bad, "decrypt_ok": decrypt_ok, "decrypt_bad": decrypt_bad,
                "masked_in_source": masked, "masked_hint_bad": hint_bad, "sentinel": sentinel})
    sha_bad += hint_bad
    rep["secret_sha_bad"] = sha_bad
    if gateway is None:
        rep["gateway"] = "未给网关 token 集合(--gateway-*),跳过"
    elif gw_total == 0:
        rep["gateway"] = "源里没有可算 sha256 的明文(脱敏快照),无法对网关"
    else:
        rep["gateway"] = {"comparable": gw_total, "matched": gw_hit, "ratio": round(gw_hit / gw_total, 4),
                          "by_status": {f"{s}:{h}": v for (s, h), v in sorted(gw_by_status.items())}}
    return rep


# --------------------------------------------------------------------------- main


def run(args) -> dict:
    items = load_snapshot(args.snapshot) if args.snapshot else load_from_lark(args.lark_creds)
    ids = [r.get("record_id") for r in items]
    if len(set(ids)) != len(ids) or None in ids:
        raise SystemExit("源里 record_id 有重复或缺失,拒绝继续")
    normalized, warn = [], Counter()
    for rec in items:
        try:
            n = normalize(rec)
        except Exception as exc:  # noqa: BLE001
            AS.log("解析失败,行(已打码):", scrub_record(rec)["record_id"], "原因:", str(exc))
            raise SystemExit(2)
        for w in n["warnings"]:
            warn[re.sub(r"\[.*?\]|\S+$", "…", w) if "多选" in w else w.split("(")[0]] += 1
        if args.verbose:   # 整行先打码再打印
            AS.log("row", json.dumps(scrub_record(rec), ensure_ascii=False)[:400],
                   {k: v.get("form") for k, v in n["secrets"].items()})
        normalized.append(n)

    kek = kek_id = None
    if args.kek_file:
        kek, kek_id = AS.load_kek(args.kek_file, db_path=args.db)
    gateway = load_gateway_hashes(args)

    target = AS.connect(args.db) if args.apply else sqlite3.connect(":memory:", isolation_level=None)
    if not args.apply:
        target.row_factory = sqlite3.Row
        if Path(args.db).exists():          # dry-run:在库的内存副本上完整跑一遍,然后丢掉
            src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
            src.backup(target)
            src.close()
        target.execute("PRAGMA foreign_keys=ON")
    AS.init_schema(target)
    target.execute("BEGIN IMMEDIATE")
    try:
        st = apply_rows(target, normalized, kek=kek, kek_id=kek_id, mode=args.secret_mode)
        AS.audit(target, "migrate.apply" if args.apply else "migrate.dry_run", actor="migrate_from_lark",
                 detail={"source": "snapshot" if args.snapshot else "lark", "rows": len(items), "kek_id": kek_id,
                         "mode": args.secret_mode, "changes": {k: v for k, v in st.items() if not k.endswith(".same")}})
        rep = reconcile(target, normalized, gateway, kek=kek, kek_id=kek_id)
        bad = rep["missing_in_db"] or rep["secret_sha_bad"] or rep["decrypt_bad"]
        if bad and args.apply:
            raise RuntimeError("对账不过,回滚本次写入")
        target.execute("COMMIT")
    except BaseException:
        target.execute("ROLLBACK")
        raise
    result = {"mode": "APPLY" if args.apply else "DRY-RUN(未写盘)", "db": args.db, "kek_id": kek_id,
              "changes": dict(sorted(st.items())), "warnings": dict(warn), "reconcile": rep,
              "stats": AS.stats(target)}
    target.close()
    return result


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--snapshot")
    src.add_argument("--from-lark", action="store_true")
    ap.add_argument("--lark-creds")
    ap.add_argument("--db", required=True)
    ap.add_argument("--kek-file")
    ap.add_argument("--secret-mode", choices=["encrypt", "hash-only"], default="encrypt",
                    help="encrypt=明文信封加密可取回;hash-only=只留 sha256(取不回)")
    gw = ap.add_mutually_exclusive_group()
    gw.add_argument("--gateway-hashes")
    gw.add_argument("--gateway-198-ssh", action="store_true")
    gw.add_argument("--gateway-198-local", action="store_true")
    ap.add_argument("--apply", action="store_true", help="真写;不给就是 dry-run")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)
    if a.from_lark and not a.lark_creds:
        ap.error("--from-lark 需要 --lark-creds")
    return a


def main(argv=None) -> int:
    res = run(parse_args(argv))
    print(AS.redact(json.dumps(res, ensure_ascii=False, indent=1)))
    r = res["reconcile"]
    return 0 if not (r["missing_in_db"] or r["secret_sha_bad"] or r["decrypt_bad"]) else 3


if __name__ == "__main__":
    sys.exit(main())
