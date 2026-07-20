#!/usr/bin/env python3
"""chatgpt_acct_quota_to_lark.py — 将 198 chatgpt-acct 配额数据写入飞书多维表格。

用法：
  # 先运行 quota 脚本生成数据，再写入飞书
  bash scripts/chatgpt-acct-quota.sh --raw --quiet
  python3 scripts/chatgpt_acct_quota_to_lark.py

  # 一步到位
  python3 scripts/chatgpt_acct_quota_to_lark.py --run-quota

  # 指定目标表格（默认使用硬编码的表格）
  python3 scripts/chatgpt_acct_quota_to_lark.py --base-token XXX --table-id tblXXX

数据源：/tmp/chatgpt-acct-quota-last.txt（chatgpt-acct-quota.sh 的副本输出）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
QUOTA_SCRIPT = SCRIPT_DIR / "chatgpt-acct-quota.sh"
QUOTA_OUTPUT = Path("/tmp/chatgpt-acct-quota-last.txt")

DEFAULT_BASE_TOKEN = "YdrZb5xaNaziDHssawGcDpd1nqf"
DEFAULT_TABLE_ID = "tblsw3E1zZ2IuBbW"

FIELDS = [
    "acct", "email", "take", "status", "tier", "7d%", "reset",
    "main_n", "main$", "codex_n", "codex$",
    "next_reset", "restore", "sub_until", "sub_left", "reset_cards", "cause",
]

LARK_ENV = {
    "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
}


def run_quota_script() -> None:
    print("[quota] running chatgpt-acct-quota.sh --raw --quiet ...", file=sys.stderr)
    r = subprocess.run(
        ["bash", str(QUOTA_SCRIPT), "--raw", "--quiet"],
        capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        print(f"[quota] FAILED rc={r.returncode}", file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(1)
    print(f"[quota] OK → {QUOTA_OUTPUT}", file=sys.stderr)


def to_num(s: str) -> int | float | None:
    s = s.rstrip("*")
    if s in ("-", ""):
        return None
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def parse_quota_output(cards: dict | None = None) -> list[list]:
    if not QUOTA_OUTPUT.exists():
        print(f"[parse] {QUOTA_OUTPUT} not found — run quota script first", file=sys.stderr)
        sys.exit(1)
    cards = cards or {}

    rows: list[list] = []
    for line in QUOTA_OUTPUT.read_text().splitlines():
        if not re.match(r"^acct-\d+\s+", line):
            continue
        if len(line) < 100:
            continue
        # Fixed-width column positions matching chatgpt_acct_quota_view.py render_table format
        # (2026-07-13 single-window layout):
        # acct:9 _:1 email:32 _:1 take:4 _:1 status:7 _:1 tier:16 _:1
        # 7d%:5 _:1 reset:12 _:1
        # main_n:7 _:1 main$:7 _:1 codex_n:8 _:1 codex$:7 _:1
        # next_reset:12 _:1 restore:9 _:1 sub_until:20 _:1 sub_left:8 __:2 cause:rest
        acct_v = line[0:9].strip()
        email_v = line[10:42].strip()
        take_v = line[43:47].strip()
        status_v = line[48:55].strip()
        tier_v = line[56:72].strip()
        pct7d_v = line[73:78].strip()
        reset_v = line[79:91].strip()
        main_n_v = line[92:99].strip()
        main_s_v = line[100:107].strip()
        codex_n_v = line[108:116].strip()
        codex_s_v = line[117:124].strip()
        next_reset_v = line[125:137].strip()
        restore_v = line[138:147].strip()
        sub_until_v = line[148:168].strip()
        sub_left_v = line[169:177].strip()
        cause_v = line[179:].strip() if len(line) > 179 else ""

        def text_or_none(s: str) -> str | None:
            return s if s and s != "-" else None

        # reset_cards: 从 cards dict 取 available_count(banked reset 卡数);
        # 探测失败/未探则 None(表格显示空)
        card_info = cards.get(acct_v) or {}
        cards_v = card_info.get("credits") if card_info.get("s") == "OK" else None

        row = [
            acct_v,
            email_v or None,
            take_v,
            status_v,
            tier_v,
            to_num(pct7d_v),
            text_or_none(reset_v),
            to_num(main_n_v),
            to_num(main_s_v),
            to_num(codex_n_v),
            to_num(codex_s_v),
            text_or_none(next_reset_v),
            text_or_none(restore_v),
            text_or_none(sub_until_v),
            text_or_none(sub_left_v),
            cards_v,
            cause_v if cause_v else None,
        ]
        rows.append(row)

    print(f"[parse] {len(rows)} rows parsed from {QUOTA_OUTPUT}", file=sys.stderr)
    return rows


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

    rows = parse_quota_output(cards)
    if not rows:
        print("[ERROR] no data to write", file=sys.stderr)
        return 1

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
