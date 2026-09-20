#!/usr/bin/env python3
"""从飞书导出的 ndjson 里选出待灰度的 key，产出三个文件。

产物:
  <out>.keys    明文，一行一把，0600 —— 用完必须 shred -u
  <out>.b64     上面那个的 base64，用于传到 198（避免明文过 argv）
  <out>_expect.json  {n, alias, sid} 三元组，无明文，用于事后对账

用法:
  # 编号最小的 50%
  ./select-keys.py all.ndjson --pct 50 --out half
  # 编号最小的 262 把
  ./select-keys.py all.ndjson --count 262 --out b20
  # 按 alias 前缀点名
  ./select-keys.py all.ndjson --alias-prefix liuguoxian --out lgx
  # 排除已经在灰度里的（live.sids 来自 gray-key-route.sh list）
  ./select-keys.py all.ndjson --pct 50 --exclude-live live.sids --out half

数据源约定（踩过的坑，别改）:
  - 明文只在 'API Key' 列。'Cursor Key'/'Claude Code Key' 实际是空的
  - '编号' 返回字符串不是数字，必须 int(str(...).strip())
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys


def flat(v) -> str:
    """飞书字段可能是 str / list / dict，统一拍平成 str。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                out.append(str(x.get("text") or x.get("name") or ""))
            else:
                out.append(str(x))
        return "".join(out).strip()
    if isinstance(v, dict):
        return str(v.get("text") or v.get("name") or "").strip()
    return str(v).strip()


def load(path: str) -> list[dict]:
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        f = r.get("fields", r)
        key = flat(f.get("API Key"))
        if not key:
            continue
        raw_n = flat(f.get("编号"))
        try:
            n = int(str(raw_n).strip())
        except (TypeError, ValueError):
            continue  # 编号是字符串，非数字的行跳过
        rows.append(
            {
                "n": n,
                "key": key,
                "alias": flat(f.get("key_alias")),
                "mail": flat(f.get("邮箱前缀")),
                "zaizhi": flat(f.get("是否在职")),
                "status": flat(f.get("状态")),
            }
        )
    rows.sort(key=lambda r: r["n"])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ndjson")
    ap.add_argument("--out", required=True, help="产物前缀")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pct", type=int, help="按编号从小到大取百分比")
    g.add_argument("--count", type=int, help="按编号从小到大取固定把数")
    g.add_argument("--alias-prefix", help="按 key_alias 前缀点名")
    g.add_argument("--all", action="store_true", help="全取")
    ap.add_argument("--exclude-live", help="gray-key-route.sh list 导出的 sid 文件")
    ap.add_argument("--exclude-departed", action="store_true", help="排除离职")
    args = ap.parse_args()

    rows = load(args.ndjson)
    if not rows:
        print("有明文的行数为 0 —— 检查是不是没投影 'API Key' 列", file=sys.stderr)
        return 1
    print(f"有明文总数: {len(rows)}  编号区间: {rows[0]['n']}~{rows[-1]['n']}")

    if args.pct is not None:
        if not 0 < args.pct <= 100:
            print("pct 必须在 1..100", file=sys.stderr)
            return 1
        sel = rows[: len(rows) * args.pct // 100]
    elif args.count is not None:
        sel = rows[: args.count]
    elif args.alias_prefix:
        sel = [r for r in rows if r["alias"].startswith(args.alias_prefix)]
    else:
        sel = rows

    if args.exclude_departed:
        before = len(sel)
        sel = [r for r in sel if r["zaizhi"] != "离职"]
        print(f"排除离职: -{before - len(sel)}")

    if not sel:
        print("选中 0 把，检查筛选条件", file=sys.stderr)
        return 1

    print(f"选中: {len(sel)} 把  编号 {sel[0]['n']}~{sel[-1]['n']}")
    for field, label in (("zaizhi", "在职分布"), ("status", "状态分布")):
        dist: dict[str, int] = {}
        for r in sel:
            dist[r[field]] = dist.get(r[field], 0) + 1
        print(f"  {label}: {dist}")

    recs = []
    for r in sel:
        recs.append({**r, "sid": hashlib.sha256(r["key"].encode()).hexdigest()})

    # 硬校验：格式和唯一性。任一条不过就不出文件。
    bad = [r for r in recs if len(r["key"]) < 20 or not r["key"].startswith("sk-")]
    if bad:
        print(f"格式异常 {len(bad)} 把（编号 {[r['n'] for r in bad[:5]]}…）", file=sys.stderr)
        return 1
    if len({r["sid"] for r in recs}) != len(recs):
        print("sid 有重复 —— 表里有重复明文，先查表", file=sys.stderr)
        return 1
    print(f"  格式校验: 通过   sid 唯一: 通过")

    todo = recs
    if args.exclude_live:
        live = {x.strip() for x in open(args.exclude_live) if x.strip()}
        todo = [r for r in recs if r["sid"][:12] not in live]
        print(f"已在灰度: {len(recs) - len(todo)}   本轮需新增: {len(todo)}")

    if not todo:
        print("没有需要新增的 key —— 全都已经在灰度里了")
        return 0

    keys_path = f"{args.out}.keys"
    with open(keys_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(r["key"] for r in todo) + "\n")
    os.chmod(keys_path, 0o600)

    import base64

    b64_path = f"{args.out}.b64"
    with open(b64_path, "w", encoding="utf-8") as fh:
        fh.write(base64.b64encode(open(keys_path, "rb").read()).decode())
    os.chmod(b64_path, 0o600)

    # expect 覆盖全部选中（含已在灰度的），因为对账要比的是最终态
    exp_path = f"{args.out}_expect.json"
    json.dump(
        [{"n": r["n"], "alias": r["alias"], "sid": r["sid"]} for r in recs],
        open(exp_path, "w", encoding="utf-8"),
        ensure_ascii=False,
        indent=1,
    )

    print(f"\n产物:")
    print(f"  {keys_path}      {len(todo)} 行明文 (0600) ← 用完 shred -u")
    print(f"  {b64_path}       传输用")
    print(f"  {exp_path}  {len(recs)} 条对账基准 (无明文)")
    print(f"\n下一步: 先跑 reconcile-keys.py 做库对账，剔掉无行的废 key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
