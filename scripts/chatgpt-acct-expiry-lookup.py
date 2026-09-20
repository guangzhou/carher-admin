#!/usr/bin/env python3
"""查单个/多个 ChatGPT 号的「会员到期」与「额度恢复」——回答"我那个号什么时候到期"。

不是新探针：只消费 chatgpt-acct-quota.sh --json-rows already 产出的
/tmp/chatgpt-acct-quota-rows.json（含 sub_until / sub_left / next_reset / verdict）。
所以它天然只在探针跑过的 ops box（JSZX-AI-03）上有新鲜数据；别的机器上是快照。

两个时间别混（本仓库反复踩）：
  - next_reset / reset  = 额度限流恢复（Codex 7d 桶重置），号还活着，恢复后能继续用。
  - sub_until / sub_left = 会员本身到期，到点号就废，续费或换号才行。
  两者可能就差几小时（如 acct-82：额度恢复后 2.5h 会员就到期），只看 reset 会误判"还能用"。

用法（在 ops box 上）：
  # 先刷新快照（可选，数据旧了才需要）
  scripts/chatgpt-acct-quota.sh --json-rows
  # 查
  scripts/chatgpt-acct-expiry-lookup.py acct-82
  scripts/chatgpt-acct-expiry-lookup.py acct-82 acct-233     # 多个
  scripts/chatgpt-acct-expiry-lookup.py franco               # email 子串也行
  scripts/chatgpt-acct-expiry-lookup.py --expiring 3         # 3 天内会员到期的全列出
  scripts/chatgpt-acct-expiry-lookup.py --rows /path/rows.json acct-82
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROWS = Path("/tmp/chatgpt-acct-quota-rows.json")
BEIJING_OFFSET_H = 8  # UTC+8，把 sub_until 的 UTC 顺手换成北京时间给人看


def parse_utc(s: str | None) -> datetime | None:
    """'2026-08-20 08:23 UTC' → aware datetime；'-'/None/垃圾 → None。"""
    if not s or s in ("-", "unknown"):
        return None
    s = s.strip().removesuffix("UTC").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def to_beijing(dt: datetime) -> str:
    from datetime import timedelta
    return (dt + timedelta(hours=BEIJING_OFFSET_H)).strftime("%Y-%m-%d %H:%M 北京")


def load_rows(path: Path) -> tuple[list[dict], str]:
    if not path.exists():
        sys.exit(
            f"[fatal] 找不到 {path}\n"
            f"        先在 ops box(JSZX-AI-03) 上跑：scripts/chatgpt-acct-quota.sh --json-rows"
        )
    doc = json.loads(path.read_text())
    return doc.get("rows", []), doc.get("generated_at", "unknown")


def match(row: dict, needles: list[str]) -> bool:
    hay = f"{row.get('acct','')} {row.get('email','')}".lower()
    return any(n.lower() in hay for n in needles)


def render(row: dict, now: datetime) -> str:
    acct = row.get("acct", "?")
    email = row.get("email", "?")
    sub_until = row.get("sub_until")
    sub_left = row.get("sub_left", "?")
    verdict = row.get("verdict", "?")
    reset = row.get("next_reset") or row.get("reset") or "-"

    dt = parse_utc(sub_until)
    if dt is None:
        exp_line = f"会员到期: {sub_until or '未知(号没探到 / 无凭据)'}"
    else:
        days = (dt - now).total_seconds() / 86400
        if days < 0:
            tag = f"已过期 {abs(days):.1f}d"
        elif days < 1:
            tag = f"⚠️ 不到 1 天({days*24:.1f}h)"
        elif days < 3:
            tag = f"⚠️ 仅剩 {days:.1f}d"
        else:
            tag = f"剩 {days:.1f}d"
        exp_line = f"会员到期: {to_beijing(dt)}  [{tag}]  (原始 {sub_until}, sub_left={sub_left})"

    return (
        f"● {acct}  {email}\n"
        f"    {exp_line}\n"
        f"    额度恢复(≠到期): {reset}    verdict={verdict}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="查 ChatGPT 号会员到期/额度恢复")
    ap.add_argument("needles", nargs="*", help="acct id 或 email 子串，可多个")
    ap.add_argument("--rows", type=Path, default=DEFAULT_ROWS, help="rows json 路径")
    ap.add_argument("--expiring", type=float, metavar="DAYS",
                    help="列出 N 天内会员到期(含已过期)的所有号，忽略 needles")
    args = ap.parse_args()

    rows, gen = load_rows(args.rows)
    # 快照时间当"现在"，避免用本机时钟去比一个可能几天前的快照
    now = parse_utc(gen) or datetime.now(timezone.utc)
    print(f"# 数据快照: {gen}  (来源 {args.rows})\n")

    if args.expiring is not None:
        hits = []
        for r in rows:
            dt = parse_utc(r.get("sub_until"))
            if dt is None:
                continue
            if (dt - now).total_seconds() / 86400 <= args.expiring:
                hits.append((dt, r))
        hits.sort(key=lambda x: x[0])
        if not hits:
            print(f"(没有 {args.expiring} 天内到期的号)")
        for _, r in hits:
            print(render(r, now) + "\n")
        return

    if not args.needles:
        ap.error("给个 acct id / email 子串，或用 --expiring N")

    found = [r for r in rows if match(r, args.needles)]
    if not found:
        print(f"(没匹配到 {args.needles};号可能没被探针覆盖)")
    for r in found:
        print(render(r, now) + "\n")


if __name__ == "__main__":
    main()
