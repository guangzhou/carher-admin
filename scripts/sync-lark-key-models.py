#!/usr/bin/env python3
"""把飞书 key 表的「可用模型」列刷成 198 LiteLLM 的当前真相。

**只读 LiteLLM**：一条 `SELECT`，不改它任何东西。写只写飞书那一列。

权威来源是 198 生产库 `LiteLLM_VerificationToken.models` —— 即这把 key 被允许
请求的模型名白名单。对账方式是 **sha256(明文 key) == DB 里的 `token`**，
不靠 `key_alias` 匹配（alias 会重名、会改，hash 是一一对应的）。

两条刻意不做的事：

1. **不把 `blocked` 折进这一列。** 被封禁的 key 一个模型都调不动，但"被封禁"和
   "白名单里有哪些名字"是两件事，混进同一列就没法行动了。脚本会**单独报**有多少把
   key 在 LiteLLM 里是 blocked。
2. **不把 DB 里查不到的 key 写空。** 查不到说明这把 key 在 198 上根本不存在
   （被删了），把它的模型列清空会让"这把 key 没了"看起来像"这把 key 没模型"。
   脚本会单独把它们列出来，由人决定怎么处理。

⚠️ 白名单里有这个名 ≠ 这个名真能用（缺 alias 目标会 400）。要判真能不能用，
用 keyprobe（<http://10.68.13.198:30920/>）真打一遍。

`--only` 把这一轮的写入范围限定到几个 key_alias（**匹配的是飞书表的
`key_alias` 列**，不是 DB 的 —— DB 那边仍靠 hash 认人）。没有它就只能全表刷
1372 行，而"用户点名 3 个实例"是最常见的请求形状。

    python3 scripts/sync-lark-key-models.py                       # 全表预览，不写
    python3 scripts/sync-lark-key-models.py --only carher-13 carher-14 carher-75
    python3 scripts/sync-lark-key-models.py --only carher-13 --apply   # 真写

写之前一定把目标行这一列的原值落盘（`--backup`，默认
`.tmp-lark/lark-key-models-backup-<ts>.json`）。飞书 batch-update 是**逐字段
覆盖**且不产生任何信号（没报错、没 diff、`ok:true`），能发现写坏的唯一时机
就是写前存过原值。
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE_TOKEN = "DlT9bsrwMad12VsogEpcK9Ptncc"   # wiki UvIxwsWLQivn1RkrdPTcVGGnnze 的 obj_token
TABLE_ID = "tblJT2s6Y6xjYj5A"
KEY_FIELD = "API Key"
ALIAS_FIELD = "key_alias"
MODELS_FIELD = "可用模型"
CHUNK = 200

SSH_HOST = "cltx@10.68.13.198"
NS = "litellm-product"          # ⛔ 生产库是 litellm-product，不是 dev
DB_POD = "litellm-db-0"
SQL = ('select json_build_object('
       "'token',token,'alias',key_alias,'models',models,'blocked',blocked)::text "
       'from "LiteLLM_VerificationToken";')


def lark(*args: str) -> dict:
    out = subprocess.run(["lark-cli", *args], capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "raw": out[:500]}


def read_rows() -> list[dict]:
    # lark-cli 的 --output 只认相对路径，绝对路径会被静默拒绝（文件根本不生成）。
    with tempfile.TemporaryDirectory(dir=".") as d:
        rel = Path(d).name + "/rows.ndjson"
        lark("base", "+record-list", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
             "--field-id", KEY_FIELD, "--field-id", ALIAS_FIELD, "--field-id", MODELS_FIELD,
             "--format", "ndjson", "--output", rel, "--overwrite")
        path = Path(rel)
        if not path.exists():
            sys.exit("读飞书表失败：--output 文件没生成")
        return [json.loads(line) for line in path.open()]


def read_litellm() -> dict[str, dict]:
    """只读一条 SELECT。多行 SQL 经 ssh 双引号会被吃掉，所以走 stdin。"""
    proc = subprocess.run(
        ["ssh", SSH_HOST,
         f"sudo kubectl -n {NS} exec -i {DB_POD} -- psql -U litellm -d litellm -At"],
        input=SQL, capture_output=True, text=True)
    rows = [json.loads(l) for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    if not rows:
        sys.exit(f"读 LiteLLM 库失败：{proc.stderr[:300]}")
    return {r["token"]: r for r in rows}


def want(models: list) -> str:
    # 排序是为了让这一列每次刷出来都一样 —— DB 里的顺序是插入顺序，会无意义地抖，
    # 不排序的话每次跑都"有 1300 行要改"，diff 就失去意义了。
    return ", ".join(sorted(models or []))


def diff(old: str, new: str) -> tuple[list[str], list[str]]:
    o = {x for x in (old.split(", ") if old else []) if x}
    n = {x for x in (new.split(", ") if new else []) if x}
    return sorted(o - n), sorted(n - o)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写；不带就是预览")
    ap.add_argument("--only", nargs="+", metavar="ALIAS",
                    help="只刷这些 key_alias 的行（匹配飞书表的 key_alias 列）")
    ap.add_argument("--backup", metavar="PATH",
                    help="目标行原值落盘路径（默认 .tmp-lark/ 下带时间戳）")
    ap.add_argument("--only-db-alias-file", metavar="PATH",
                    help="一行一个 **DB 的** key_alias；按 hash 找到表里对应的行再刷。"
                         "表里的 key_alias 列常与 DB 不同名（2026-10-14：545 个 DB alias "
                         "按表列名只匹配上 41 行），批量改完 key 后同步用这个")
    args = ap.parse_args()

    db = read_litellm()
    rows = read_rows()
    print(f"LiteLLM 库里 {len(db)} 把 key，飞书表 {len(rows)} 行")

    # 全表基线，在 --only 过滤之前取 —— 过滤之后就没法回答"有没有写坏别人"了。
    n_before = len(rows)
    baseline = {r["record_id"]: (r.get(MODELS_FIELD).strip()
                                 if isinstance(r.get(MODELS_FIELD), str) else "")
                for r in rows}

    if args.only and args.only_db_alias_file:
        sys.exit("--only 与 --only-db-alias-file 二选一")
    if args.only_db_alias_file:
        wanted = {l.strip() for l in open(args.only_db_alias_file, encoding="utf-8")
                  if l.strip() and not l.lstrip().startswith("#")}
        by_alias = {r["alias"]: t for t, r in db.items() if r.get("alias") in wanted}
        missing_db = sorted(wanted - set(by_alias))
        if missing_db:
            sys.exit(f"这些 DB alias 在 LiteLLM 库里查无此 key：{missing_db[:20]}")
        tokens = set(by_alias.values())
        def _tok(r):
            k = r.get(KEY_FIELD)
            return hashlib.sha256(k.strip().encode()).hexdigest() if isinstance(k, str) and k.strip() else None
        rows = [r for r in rows if _tok(r) in tokens]
        hit = {_tok(r) for r in rows}
        no_row = sorted(a for a, t in by_alias.items() if t not in hit)
        print(f"--only-db-alias-file 限定 {len(wanted)} 把 key，表里按 hash 匹配到 {len(rows)} 行"
              f"（{len(hit)} 把 key 有行，{len(no_row)} 把表里没有行，不新增）")
        if no_row:
            print(f"  表里没有行的 key（前 15）：{no_row[:15]}")

    only = set(args.only or [])
    if only:
        rows = [r for r in rows if (r.get(ALIAS_FIELD) or "").strip() in only]
        got = {(r.get(ALIAS_FIELD) or "").strip() for r in rows}
        print(f"--only 限定 {len(only)} 个 alias，表里匹配到 {len(rows)} 行")
        # 点名了却一行都没匹配上 ⇒ 是 scope 到 0，不是"已经最新"。必须响。
        missing = sorted(only - got)
        if missing:
            sys.exit(f"这些 alias 在飞书表里查无此行，先确认名字：{missing}")

    todo, same, blank, absent, blocked = {}, 0, 0, [], []
    orig = {}
    for r in rows:
        key = r.get(KEY_FIELD)
        if not (isinstance(key, str) and key.strip()):
            blank += 1
            continue
        rec = db.get(hashlib.sha256(key.strip().encode()).hexdigest())
        if rec is None:
            absent.append(r.get(ALIAS_FIELD))       # 这把 key 在 198 上不存在
            continue
        if rec.get("blocked"):
            blocked.append(r.get(ALIAS_FIELD))      # 单独报，不折进模型列
        cur = r.get(MODELS_FIELD)
        cur = cur.strip() if isinstance(cur, str) else ""
        new = want(rec.get("models"))
        if cur == new:
            same += 1
        else:
            todo[r["record_id"]] = {MODELS_FIELD: new}
            orig[r["record_id"]] = {"key_alias": r.get(ALIAS_FIELD), MODELS_FIELD: cur}

    print(f"已是最新 {same} 行，需要更新 {len(todo)} 行，空 key {blank} 行")
    print(f"⚠️ LiteLLM 里查无此 key（不动它的模型列）{len(absent)} 把：{absent[:10]}")
    print(f"⚠️ LiteLLM 里是 blocked（模型列照常刷）{len(blocked)} 把：{blocked[:10]}")

    # 点名少数几行时，把逐行加减都摊开 —— 只报"N 行要改"看不出会删掉什么。
    if todo and len(todo) <= 20:
        for rid, v in todo.items():
            gone, add = diff(orig[rid][MODELS_FIELD], v[MODELS_FIELD])
            n_old = len([x for x in orig[rid][MODELS_FIELD].split(", ") if x])
            n_new = len(v[MODELS_FIELD].split(", "))
            print(f"\n{orig[rid]['key_alias']} ({rid}): {n_old} -> {n_new}")
            print("  删:", ", ".join(gone) or "无")
            print("  加:", ", ".join(add) or "无")

    if not args.apply:
        if len(todo) > 20:
            for rid, v in list(todo.items())[:3]:
                print("  ", rid, v[MODELS_FIELD][:90])
        print("\n预览模式，没写。要真写加 --apply")
        return 0

    if todo:
        bak = Path(args.backup or
                   f".tmp-lark/lark-key-models-backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
        bak.parent.mkdir(parents=True, exist_ok=True)
        bak.write_text(json.dumps(orig, ensure_ascii=False, indent=2))
        print(f"\n目标行原值已备份 → {bak}（回滚就是把这一列写回去）")

    ids = list(todo)
    acked = 0
    with tempfile.TemporaryDirectory(dir=".") as d:
        # `--json @文件` 同样只认相对 / /tmp 路径，系统临时目录会让它静默返回空 stdout。
        body = Path(d) / "body.json"
        for i in range(0, len(ids), CHUNK):
            part = {rid: todo[rid] for rid in ids[i:i + CHUNK]}
            body.write_text(json.dumps({"update_records": part}, ensure_ascii=False))
            res = lark("base", "+record-batch-update", "--base-token", BASE_TOKEN,
                       "--table-id", TABLE_ID, "--json", f"@{body}")
            if not res.get("ok"):
                print("FAIL:", json.dumps(res, ensure_ascii=False)[:300])
                return 1
            acked += len(res.get("data", {}).get("record_id_list", []))

    # "ok": true 不证明写进去了 —— 回读比一遍才算数。
    print(f"写入 ack {acked}/{len(ids)} 行，正在回读校验…")
    after = read_rows()
    if len(after) != n_before:
        print(f"⚠️ 行数变了：写前 {n_before} → 写后 {len(after)}")
    bad, drift = [], []
    for r in after:
        key = r.get(KEY_FIELD)
        cur = r.get(MODELS_FIELD)
        cur = cur.strip() if isinstance(cur, str) else ""
        if r["record_id"] in todo:
            if cur != todo[r["record_id"]][MODELS_FIELD]:
                bad.append(r.get(ALIAS_FIELD))
            continue
        # 附带损伤：没在这一轮 patch 里的行，这一列必须逐字节没动。
        # --only 下这条才是真门禁 —— 只比"目标行对不对"answer 不了"有没有写坏别人"。
        if r["record_id"] in baseline and cur != baseline[r["record_id"]]:
            drift.append(r.get(ALIAS_FIELD))
    print(f"目标行回读不一致：{len(bad)} {bad[:5]}")
    print(f"非目标行被改动：{len(drift)} {drift[:5]}")
    return 0 if not bad and not drift else 1
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
