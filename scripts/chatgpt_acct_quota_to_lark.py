#!/usr/bin/env python3
"""chatgpt_acct_quota_to_lark.py — 将 198 chatgpt-acct 配额数据写入飞书多维表格。

用法：
  # 先运行 quota 脚本生成结构化数据，再写入飞书
  bash scripts/chatgpt-acct-quota.sh --json-rows --quiet
  python3 scripts/chatgpt_acct_quota_to_lark.py

  # 一步到位
  python3 scripts/chatgpt_acct_quota_to_lark.py --run-quota

  # 指定目标表格（默认使用硬编码的表格）
  python3 scripts/chatgpt_acct_quota_to_lark.py --base-token XXX --table-id tblXXX

数据源：/tmp/chatgpt-acct-quota-rows.json（chatgpt-acct-quota.sh --json-rows 输出）

2026-07-26：从「固定宽度反解渲染后的文本表」改为消费 view 的 --json。旧解析靠
line[100:107] 这类字符偏移，加一列就要重算全部偏移，且错位静默（切出半个数字仍能
float()）。新增 zerokey 累计消耗 6 列时正是踩到这个点，故一并重构。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
QUOTA_SCRIPT = SCRIPT_DIR / "chatgpt-acct-quota.sh"
ROWS_JSON = Path("/tmp/chatgpt-acct-quota-rows.json")

DEFAULT_BASE_TOKEN = "YdrZb5xaNaziDHssawGcDpd1nqf"
DEFAULT_TABLE_ID = "tblsw3E1zZ2IuBbW"

# 表格列 → view --json 的 row key。顺序即表格列顺序。
# zk_lat_* 是端到端总时延（秒），不是首 token 时间 —— LiteLLM 的 completionStartTime
# 在流式下几乎等于响应结束点（实测 endTime-cst 中位数 1ms），DB 内无真 TTFT。
COLUMNS: list[tuple[str, str]] = [
    ("acct", "acct"),
    ("email", "email"),
    ("take", "take"),
    ("status", "status"),
    ("tier", "tier"),
    ("7d%", "pct7d"),
    ("reset", "reset"),
    ("main_n", "main_n"),
    ("main$", "main_spend"),
    ("codex_n", "codex_n"),
    ("codex$", "codex_spend"),
    ("zk_n", "zk_n"),
    ("zk$", "zk_spend"),
    ("zk_n7", "zk_n7"),
    ("zk$7", "zk_spend7"),
    ("zk_lat_avg", "zk_lat_avg"),
    ("zk_lat_p95", "zk_lat_p95"),
    ("zk_empty%", "zk_empty_pct"),
    ("next_reset", "next_reset"),
    ("restore", "restore"),
    ("sub_until", "sub_until"),
    ("sub_left", "sub_left"),
    ("reset_cards", "reset_cards"),
    ("cause", "cause"),
]

FIELDS = [name for name, _ in COLUMNS]

# 新列的建表定义（幂等；已存在则跳过）
NEW_FIELD_SPECS = [
    {"name": "zk_n", "type": "number"},
    {"name": "zk$", "type": "number"},
    {"name": "zk_n7", "type": "number"},
    {"name": "zk$7", "type": "number"},
    {"name": "zk_lat_avg", "type": "number"},
    {"name": "zk_lat_p95", "type": "number"},
    {"name": "zk_empty%", "type": "number"},
]

LARK_ENV = {
    "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
}


def run_quota_script() -> None:
    print("[quota] running chatgpt-acct-quota.sh --json-rows --quiet ...", file=sys.stderr)
    r = subprocess.run(
        ["bash", str(QUOTA_SCRIPT), "--json-rows", "--quiet"],
        capture_output=True, text=True, timeout=420,
    )
    if r.returncode != 0:
        print(f"[quota] FAILED rc={r.returncode}", file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(1)
    print(f"[quota] OK → {ROWS_JSON}", file=sys.stderr)


def load_rows(cards: dict | None = None) -> tuple[list[list], dict]:
    if not ROWS_JSON.exists():
        print(f"[parse] {ROWS_JSON} not found — 先跑 "
              f"chatgpt-acct-quota.sh --json-rows（或加 --run-quota）", file=sys.stderr)
        sys.exit(1)
    cards = cards or {}
    payload = json.loads(ROWS_JSON.read_text())
    src_rows = payload.get("rows") or []
    if not src_rows:
        print("[parse] rows 为空", file=sys.stderr)
        sys.exit(1)

    def blank_to_none(v):
        # 文本列的 '-' 是"无数据"占位，写进表格会被当成真值
        return None if v in ("", "-") else v

    rows: list[list] = []
    for r in src_rows:
        # reset_cards: 从 cards dict 取 available_count(banked reset 卡数);
        # 探测失败/未探则 None(表格显示空)
        card_info = cards.get(r["acct"]) or {}
        r = {**r, "reset_cards": (card_info.get("credits")
                                  if card_info.get("s") == "OK" else None)}
        rows.append([blank_to_none(r.get(key)) for _, key in COLUMNS])

    print(f"[parse] {len(rows)} rows loaded from {ROWS_JSON}", file=sys.stderr)
    return rows, payload


def lark_cli(args: list[str], *, input_data: str | None = None, timeout: int = 30) -> dict:
    env = {**os.environ, **LARK_ENV}
    r = subprocess.run(
        ["lark-cli"] + args,
        input=input_data, capture_output=True, text=True,
        timeout=timeout, env=env,
    )
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": {"message": r.stderr or r.stdout or f"rc={r.returncode}"}}


def report_zk_coverage(rows: list[list], payload: dict) -> None:
    """自检 + 诊断：表内 zk 合计 vs 全池合计，差额=未归属（state.json 无对应 acct）。

    写表格前必须印出来 —— 表格只有 state 内的 64 行，若不报差额，看表的人会把
    表内合计当成全池真值。
    """
    idx = {name: i for i, (name, _) in enumerate(COLUMNS)}
    tbl_n = sum(r[idx["zk_n"]] or 0 for r in rows)
    tbl_sp = sum(r[idx["zk$"]] or 0.0 for r in rows)
    pool = payload.get("zk_pool_totals") or {}
    pool_n, pool_sp = pool.get("calls"), pool.get("spend")
    print(f"[zk] 表内合计 {tbl_n} calls / ${tbl_sp:.2f}", file=sys.stderr)
    if pool_n is not None:
        d_n, d_sp = pool_n - tbl_n, (pool_sp or 0.0) - tbl_sp
        print(f"[zk] 全池合计 {pool_n} calls / ${pool_sp:.2f}"
              + (f"  → 未归属 {d_n} calls / ${d_sp:.2f}" if d_n else "  → 全部归属"),
              file=sys.stderr)

    diag = payload.get("diagnostics") or {}
    for key, label in (("zk_unattributed", "未归属编号"),
                       ("zk_orphan_route", "孤儿路由(无 zero-N svc)"),
                       ("zk_empty_output", "稳定空返(疑订阅失效)")):
        items = diag.get(key) or []
        if items:
            print(f"[zk] {label} ({len(items)}): "
                  f"{[i['acct'] for i in items]}", file=sys.stderr)


def ensure_fields(base_token: str, table_id: str) -> None:
    """幂等建新列。字段列表在 data.fields（不是 data.items —— 和 record 系列的
    data.record_id_list 一样属于 lark-cli 的响应形状坑）。"""
    resp = lark_cli([
        "base", "+field-list",
        "--base-token", base_token, "--table-id", table_id,
        "--as", "user", "--format", "json",
    ])
    existing = {f.get("name") for f in (resp.get("data", {}).get("fields") or [])}
    if not existing:
        print("[fields] WARN: 读不到现有字段，跳过建列（避免重复建）", file=sys.stderr)
        return
    missing = [s for s in NEW_FIELD_SPECS if s["name"] not in existing]
    if not missing:
        print(f"[fields] OK: {len(existing)} 列已齐，无需新建", file=sys.stderr)
        return
    for spec in missing:
        tmp = Path("_lark_field.json")
        tmp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        try:
            r = lark_cli([
                "base", "+field-create",
                "--base-token", base_token, "--table-id", table_id,
                "--as", "user", "--json", f"@{tmp.name}", "--format", "json",
            ])
            if r.get("ok"):
                print(f"[fields] created {spec['name']} ({spec['type']})", file=sys.stderr)
            else:
                print(f"[fields] ERROR creating {spec['name']}: "
                      f"{r.get('error', {}).get('message', '?')}", file=sys.stderr)
                sys.exit(3)
        finally:
            tmp.unlink(missing_ok=True)


def delete_all_records(base_token: str, table_id: str) -> int:
    deleted = 0
    while True:
        resp = lark_cli([
            "base", "+record-list",
            "--base-token", base_token, "--table-id", table_id,
            "--limit", "200", "--as", "user", "--format", "json",
        ])
        d = resp.get("data", {})
        ids = d.get("record_id_list", [])
        if not ids:
            break
        payload = json.dumps({"record_id_list": ids})
        tmp = Path("_lark_del_batch.json")
        tmp.write_text(payload)
        try:
            dr = lark_cli([
                "base", "+record-delete",
                "--base-token", base_token, "--table-id", table_id,
                "--as", "user", "--json", f"@{tmp.name}", "--yes", "--format", "json",
            ])
            if not dr.get("ok"):
                print(f"[delete] ERROR: {dr.get('error', {}).get('message', '?')}", file=sys.stderr)
                break
            deleted += len(ids)
            print(f"[delete] {deleted} records deleted so far", file=sys.stderr)
        finally:
            tmp.unlink(missing_ok=True)
    return deleted


def batch_create_records(base_token: str, table_id: str, rows: list[list]) -> int:
    created = 0
    for i in range(0, len(rows), 200):
        batch = rows[i:i + 200]
        payload = json.dumps({"fields": FIELDS, "rows": batch}, ensure_ascii=False)
        tmp = Path("_lark_create_batch.json")
        tmp.write_text(payload, encoding="utf-8")
        try:
            resp = lark_cli([
                "base", "+record-batch-create",
                "--base-token", base_token, "--table-id", table_id,
                "--as", "user", "--json", f"@{tmp.name}", "--format", "json",
            ], timeout=60)
            if not resp.get("ok"):
                print(f"[create] ERROR: {resp.get('error', {}).get('message', '?')}", file=sys.stderr)
                break
            rid_list = resp.get("data", {}).get("record_id_list", [])
            created += len(rid_list)
            print(f"[create] batch {i // 200 + 1}: {len(rid_list)} records created", file=sys.stderr)
        finally:
            tmp.unlink(missing_ok=True)
    return created


def verify_records(base_token: str, table_id: str, expected: int) -> bool:
    total = 0
    page_token = None
    while True:
        cmd = [
            "base", "+record-list",
            "--base-token", base_token, "--table-id", table_id,
            "--limit", "200", "--as", "user", "--format", "json",
        ]
        if page_token:
            cmd.extend(["--page-token", page_token])
        resp = lark_cli(cmd)
        d = resp.get("data", {})
        ids = d.get("record_id_list", [])
        total += len(ids)
        if not d.get("has_more"):
            break
        page_token = d.get("page_token", "")
        if not page_token:
            break
    ok = total == expected
    tag = "OK" if ok else "MISMATCH"
    print(f"[verify] {tag}: {total} records in table, expected {expected}", file=sys.stderr)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Write 198 chatgpt-acct quota data to Lark Base")
    parser.add_argument("--run-quota", action="store_true", help="Run chatgpt-acct-quota.sh first")
    parser.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    parser.add_argument("--table-id", default=DEFAULT_TABLE_ID)
    parser.add_argument("--skip-delete", action="store_true", help="Skip deleting old records")
    parser.add_argument("--cards-json", default="",
                        help="reset 卡数据 JSON 文件路径(chatgpt-acct-reset-cards.py 输出)")
    args = parser.parse_args()

    if args.run_quota:
        run_quota_script()

    cards = {}
    if args.cards_json:
        try:
            cards = json.loads(Path(args.cards_json).read_text())
            n_with = sum(1 for v in cards.values() if v.get("s") == "OK")
            print(f"[cards] loaded {len(cards)} accts, {n_with} probed OK", file=sys.stderr)
        except Exception as e:
            print(f"[cards] WARN failed to load {args.cards_json}: {e}", file=sys.stderr)

    rows, payload = load_rows(cards)
    if not rows:
        print("[ERROR] no data to write", file=sys.stderr)
        return 1

    report_zk_coverage(rows, payload)
    ensure_fields(args.base_token, args.table_id)

    if not args.skip_delete:
        deleted = delete_all_records(args.base_token, args.table_id)
        print(f"[delete] total deleted: {deleted}", file=sys.stderr)

    created = batch_create_records(args.base_token, args.table_id, rows)
    print(f"[create] total created: {created}", file=sys.stderr)

    if created != len(rows):
        print(f"[WARN] created {created} != parsed {len(rows)}", file=sys.stderr)
        return 2

    verify_records(args.base_token, args.table_id, len(rows))

    print(f"\n[DONE] {created} records written to Lark Base", file=sys.stderr)
    print(f"  https://t83dfrspj4.feishu.cn/base/{args.base_token}?table={args.table_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
