#!/usr/bin/env python3
"""grok_bitable.py — 飞书 grok 账号表的唯一读写入口 + sub2api ↔ 表 对账。

    cd <repo>/scripts/grok-onboard
    python3 grok_bitable.py sync            # 只读：列出 sub2api grok 号里表上缺行/缺 ID 的
    python3 grok_bitable.py sync --apply    # 补：缺行建行、空的 Sub2API账户ID/Grok UserID 填上

被 `grok-warp-onboard.py`（建号后登记）和 `grok-pw-rotate.py`（改密写表）import。

为什么有它（2026-10-23）：建号脚本从不写表、改密脚本只写账号/密码/SSO 三列 ⇒
acct 164 建完表里没有行、165 的 `Sub2API账户ID`/`Grok UserID` 是手补的。
「没登记」不报错，只有对账能抓到 ⇒ `sync` 就是那条自查命令。

列（全是文本列）：账号 / Grok SSO / Grok密码(=x.ai 登录密码，不是邮箱密码) /
新Grok密码 / Grok UserID / Sub2API账户ID / 邮箱密码 / 手机号 / Cursor…
🔴 只「填空」不「覆盖」：已有值和 sub2api 不一致时报 MISMATCH 让人判，不替人改。
⛔ 密码/sso 不进 argv：lark-cli 用 `--data @0600临时文件`。
⚠️ lark-cli 的 allowed roots 看 cwd ⇒ 从 repo 内目录跑。
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
BASE = os.environ.get("GROK_BITABLE_BASE", "Ukpobc4fcaNwGJsZz5cc0GnFnfh")
TABLE = os.environ.get("GROK_BITABLE_TABLE", "tblP0Dpf2BQE3mTR")
COL_MAIL, COL_SSO, COL_PW, COL_NEW = "账号", "Grok SSO", "Grok密码", "新Grok密码"
COL_UID, COL_ID = "Grok UserID", "Sub2API账户ID"
REC = "/open-apis/bitable/v1/apps/%s/tables/%s" % (BASE, TABLE)


def log(*a):
    print(*a, flush=True)


def lark(method, path, data=None, params=None):
    cmd = ["lark-cli", "api", method, path]
    if params:
        cmd += ["--params", json.dumps(params)]
    tmp = None
    if data is not None:
        tmp = os.path.join(HERE, ".lark-%d.json" % os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, ensure_ascii=False)
        cmd += ["--data", "@" + tmp]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    try:
        d = json.loads(r.stdout)
    except Exception:
        sys.exit("lark-cli %s %s: unparseable output %r" % (method, path, r.stdout[:300]))
    if not d.get("ok"):
        sys.exit("lark-cli %s %s failed: %s" % (method, path, d.get("error")))
    return d.get("data") or {}


def cell(v):
    if isinstance(v, list):
        return "".join(x.get("text", "") for x in v if isinstance(x, dict))
    return str(v or "")


def field_names():
    return {f["field_name"] for f in
            lark("GET", REC + "/fields", params={"page_size": 100}).get("items", [])}


def all_rows():
    """{email_lower: (record_id, fields)}，翻完所有页。同一邮箱多行 ⇒ 喊出来、取第一行。"""
    rows, token = {}, None
    while True:
        params = {"page_size": 500}
        if token:
            params["page_token"] = token
        d = lark("POST", REC + "/records/search", data={}, params=params)
        for it in d.get("items", []):
            e = cell(it["fields"].get(COL_MAIL)).strip().lower()
            if not e:
                continue
            if e in rows:
                log("  ⚠️ duplicate rows for %s: %s, %s (using the first)"
                    % (e, rows[e][0], it["record_id"]))
                continue
            rows[e] = (it["record_id"], it["fields"])
        if not d.get("has_more"):
            return rows
        token = d.get("page_token")


def find_row(email):
    hit = all_rows().get(email.strip().lower())
    return hit[0] if hit else None


def read_row(rid):
    return lark("GET", REC + "/records/%s" % rid)["record"]["fields"]


def upsert(email, fields, verify=None):
    """按邮箱 update / create 一行，再逐字回读 verify 里列出的列（默认全部写入列）。

    回读不等 ⇒ sys.exit（调用方据此拒绝做后面不可逆的那步）。返回 record_id。"""
    fields = dict(fields, **{COL_MAIL: email})
    rid = find_row(email)
    if rid:
        lark("POST", REC + "/records/batch_update",
             data={"records": [{"record_id": rid, "fields": fields}]})
    else:
        rid = lark("POST", REC + "/records/batch_create",
                   data={"records": [{"fields": fields}]})["records"][0]["record_id"]
        log("  + created row %s (account was not in the table)" % rid)
    back = read_row(rid)
    cols = verify if verify is not None else list(fields)
    bad = [c for c in cols if cell(back.get(c)) != fields[c]]
    if bad:
        sys.exit("read-back mismatch on %s (row %s)" % (bad, rid))
    log("  table OK (row %s, %d/%d cells read back equal)" % (rid, len(cols), len(cols)))
    return rid


# ---------------------------------------------------------------- sub2api 侧
def sub2api_grok():
    """{email_lower: {"id": "165", "sub": "<grok userid>"}}，经 scripts/sub2api-cli.sh（只读）。"""
    r = subprocess.run([os.path.join(REPO, "scripts", "sub2api-cli.sh"), "accounts", "list",
                        "--platform", "grok", "--page-size", "500"],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except Exception:
        sys.exit("sub2api-cli accounts list: unparseable (rc=%s) %s"
                 % (r.returncode, (r.stderr or r.stdout)[-300:]))
    items = d.get("items", d) if isinstance(d, dict) else d
    if isinstance(d, dict) and d.get("pages", 1) > 1:
        sys.exit("sub2api returned %s pages; this lookup reads one" % d["pages"])
    out = {}
    for x in items:
        e = (x.get("name") or "").strip().lower()
        if "@" in e:
            out[e] = {"id": str(x["id"]), "sub": (x.get("credentials") or {}).get("sub") or ""}
    return out


def id_fields(have, s2a):
    """表上为空、sub2api 有值的 ID 列 ⇒ 要补的；两边都有但不等 ⇒ mismatch。"""
    fill, mism = {}, []
    for col, val in ((COL_ID, s2a["id"]), (COL_UID, s2a["sub"])):
        cur = cell(have.get(col)).strip()
        if not val:
            continue
        if not cur:
            fill[col] = val
        elif cur != val:
            mism.append("%s table=%s sub2api=%s" % (col, cur, val))
    return fill, mism


def fill_ids(email, s2a_map=None):
    """给一个号补空的 ID 列（建号/改密收尾调用）。sub2api 里没这个号 ⇒ 只喊不写。"""
    s2a = (s2a_map or sub2api_grok()).get(email.lower())
    if not s2a:
        log("  ⚠️ %s is not a grok account in sub2api -- %s/%s left as is"
            % (email, COL_ID, COL_UID))
        return
    rid = find_row(email)
    have = read_row(rid) if rid else {}
    fill, mism = id_fields(have, s2a)
    for m in mism:
        log("  🔴 MISMATCH %s: %s (not overwritten)" % (email, m))
    if fill:
        upsert(email, fill)
    else:
        log("  table ids already set (%s=%s)" % (COL_ID, s2a["id"]))


def sync(apply):
    s2a, rows = sub2api_grok(), all_rows()
    todo, n_mism = [], 0
    for e, v in sorted(s2a.items(), key=lambda kv: int(kv[1]["id"])):
        rid, have = rows.get(e, (None, {}))
        fill, mism = id_fields(have, v)
        for m in mism:
            n_mism += 1
            log("  🔴 MISMATCH acct %-4s %-34s %s" % (v["id"], e, m))
        if not rid:
            todo.append((e, v, dict(fill)))
            log("  NO ROW   acct %-4s %s" % (v["id"], e))
        elif fill:
            todo.append((e, v, fill))
            log("  EMPTY    acct %-4s %-34s %s" % (v["id"], e, ",".join(fill)))
    log("\nsub2api grok accounts %d · table rows %d · to fix %d · mismatch %d"
        % (len(s2a), len(rows), len(todo), n_mism))
    if not todo:
        return 1 if n_mism else 0
    if not apply:
        log("(read-only; re-run with --apply to fill)")
        return 1
    for e, v, fill in todo:
        log("== %s" % e)
        upsert(e, fill)
    log("\n⚠️ new rows only carry 账号/ID; sso/密码 are not in sub2api -- fill them by hand")
    return 1 if n_mism else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("sync", help="sub2api grok accounts vs the table")
    s.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.cmd == "sync":
        return sync(a.apply)


if __name__ == "__main__":
    raise SystemExit(main())
