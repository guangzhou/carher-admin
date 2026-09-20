#!/usr/bin/env python3
"""Fast, repeatable team spend reporting for Her and Cursor LiteLLM accounts.

The database performs the aggregation, so the report transfers only one row per
day/team/account type instead of downloading the complete SpendLogs history.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import html
import json
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
JMS = REPO_ROOT / "scripts" / "jms"
DEFAULT_CACHE_DIR = REPO_ROOT / ".cache" / "litellm-team-spend"

ENVIRONMENTS = {
    "aliyun": {
        "label": "阿里云 ACK LiteLLM",
        "ssh_target": "k8s-work-226",
        "kubectl_prefix": "kubectl -n carher exec litellm-db-0 --",
        "psql": "psql -U litellm -d litellm",
    },
    "198": {
        "label": "198 Pro LiteLLM",
        "ssh_target": "AIYJY-litellm",
        "kubectl_prefix": "kubectl -n litellm-product exec litellm-db-0 --",
        "psql": "psql -U litellm -d litellm",
    },
}


# One query per environment: filter and aggregate in PostgreSQL before crossing
# JumpServer. Team metadata is resolved with the same precedence as LiteLLM.
SPEND_SQL = r"""
SELECT
  DATE_TRUNC('day', sl."startTime")::date::text AS spend_day,
  CASE
    WHEN vt.key_alias LIKE 'carher-%%' THEN 'Her'
    WHEN vt.key_alias ILIKE 'cursor-%%' THEN 'Cursor'
  END AS account_type,
  COALESCE(
    NULLIF(TRIM(COALESCE(
      tt.team_alias,
      sl.team_id,
      vt.team_id,
      vt.metadata->>'team_alias',
      vt.metadata->>'department',
      vt.metadata->>'team',
      vt.metadata->>'dept',
      ''
    )), ''),
    '未归属'
  ) AS team,
  ROUND(SUM(sl.spend)::numeric, 6)::text AS spend_usd,
  COUNT(*)::text AS requests,
  COALESCE(SUM(sl.total_tokens), 0)::text AS total_tokens,
  COUNT(DISTINCT vt.key_alias)::text AS keys
FROM "LiteLLM_SpendLogs" sl
JOIN "LiteLLM_VerificationToken" vt ON vt.token = sl.api_key
LEFT JOIN "LiteLLM_TeamTable" tt ON tt.team_id = COALESCE(sl.team_id, vt.team_id)
WHERE sl."startTime" >= DATE '{start_date}'
  AND sl."startTime" < DATE '{end_date}'
  AND (vt.key_alias LIKE 'carher-%%' OR vt.key_alias ILIKE 'cursor-%%')
GROUP BY 1, 2, 3
ORDER BY 1, 2, SUM(sl.spend) DESC, 3;
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report recent Her/Cursor LiteLLM spend by team, with cache and Lark charts."
    )
    parser.add_argument("--days", type=int, default=14, help="inclusive UTC days to report (default: 14)")
    parser.add_argument("--end-date", help="exclusive UTC date, YYYY-MM-DD; default: tomorrow UTC")
    parser.add_argument("--env", choices=["both", "aliyun", "198"], default="both")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--cache-ttl-minutes", type=int, default=60)
    parser.add_argument("--refresh", action="store_true", help="ignore cached database aggregates")
    parser.add_argument("--json-out", type=Path, help="write report data JSON")
    parser.add_argument("--xml-out", type=Path, help="write generated Lark Doc XML")
    parser.add_argument("--create-doc", action="store_true", help="create a new Lark document")
    parser.add_argument("--as", dest="identity", choices=["bot", "user"], default="bot")
    return parser.parse_args()


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def run_psql(env_key: str, sql: str) -> str:
    env = ENVIRONMENTS[env_key]
    psql_cmd = (
        f"{env['kubectl_prefix']} {env['psql']} -F $'\\t' -A "
        "-P pager=off -P footer=off -v ON_ERROR_STOP=1 -q"
    )
    # jms --tty reserves stdin for its interactive shell. Decode SQL into a
    # remote shell variable and pass it to psql -c, so kubectl need not bind
    # stdin and psql cannot accidentally wait for terminal input.
    sql_b64 = base64.b64encode(sql.encode("utf-8")).decode("ascii")
    remote_cmd = f"SQL=$(printf '%s' '{sql_b64}' | base64 -d); {psql_cmd} -c \"$SQL\""
    # KoKo can leave non-TTY exec channels stuck. -t keeps remote psql reliable.
    proc = subprocess.run(
        [str(JMS), "ssh", "-t", "--timeout", "90", env["ssh_target"], remote_cmd],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{env_key} query failed: {proc.stderr.strip()}")
    # jms --tty may prefix the first line with terminal reset bytes.
    return proc.stdout.replace("\x1b", "").replace("\r", "").strip()


def cache_path(cache_dir: Path, env_key: str, start_date: dt.date, end_date: dt.date) -> Path:
    token = hashlib.sha256(f"v1:{env_key}:{start_date}:{end_date}".encode()).hexdigest()[:16]
    return cache_dir / f"{env_key}-{start_date}-{end_date}-{token}.json"


def read_cache(path: Path, ttl_minutes: int) -> list[dict[str, object]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(payload["saved_at_epoch"])
        if age <= ttl_minutes * 60:
            return list(payload["rows"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def query_rows(env_key: str, start_date: dt.date, end_date: dt.date) -> list[dict[str, object]]:
    sql = SPEND_SQL.format(start_date=start_date.isoformat(), end_date=end_date.isoformat())
    rows: list[dict[str, object]] = []
    for row in csv.DictReader(run_psql(env_key, sql).splitlines(), delimiter="\t"):
        if not row or not row.get("spend_day"):
            continue
        rows.append(
            {
                "environment": env_key,
                "environment_label": ENVIRONMENTS[env_key]["label"],
                "date": row["spend_day"],
                "account_type": row["account_type"] or "Unknown",
                "team": row["team"] or "未归属",
                "spend_usd": float(row["spend_usd"] or 0),
                "requests": int(row["requests"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "keys": int(row["keys"] or 0),
            }
        )
    return rows


def get_rows(
    env_key: str, start_date: dt.date, end_date: dt.date, cache_dir: Path, ttl_minutes: int, refresh: bool
) -> tuple[list[dict[str, object]], str]:
    path = cache_path(cache_dir, env_key, start_date, end_date)
    if not refresh:
        cached = read_cache(path, ttl_minutes)
        if cached is not None:
            return cached, "cache"
    rows = query_rows(env_key, start_date, end_date)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"saved_at_epoch": time.time(), "rows": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return rows, "database"


def aggregate(rows: Iterable[dict[str, object]], dimensions: tuple[str, ...]) -> list[dict[str, object]]:
    grouped: dict[tuple[object, ...], dict[str, object]] = {}
    for row in rows:
        key = tuple(row[dimension] for dimension in dimensions)
        item = grouped.setdefault(key, {dimension: row[dimension] for dimension in dimensions})
        item["spend_usd"] = float(item.get("spend_usd", 0)) + float(row["spend_usd"])
        item["requests"] = int(item.get("requests", 0)) + int(row["requests"])
        item["total_tokens"] = int(item.get("total_tokens", 0)) + int(row["total_tokens"])
        item["key_buckets"] = int(item.get("key_buckets", 0)) + int(row["keys"])
    return sorted(grouped.values(), key=lambda item: float(item["spend_usd"]), reverse=True)


def money(value: object) -> str:
    return f"{float(value):,.2f}"


def integer(value: object) -> str:
    return f"{int(value):,}"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def table(headers: list[str], rows: list[list[object]]) -> str:
    head = "".join(f'<th background-color="light-gray">{esc(header)}</th>' for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_bar_chart_svg(team_totals: list[dict[str, object]], total: float, limit: int = 20) -> str:
    top = team_totals[:limit]
    if not top:
        return ""
    width, left, right, row_height, top_pad = 1200, 230, 180, 34, 58
    chart_width = width - left - right
    height = top_pad + len(top) * row_height + 28
    max_spend = max(float(row["spend_usd"]) for row in top) or 1.0
    colors = ["#1456F0", "#00A870", "#F5A623", "#E64A19", "#7B61FF"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="24" y="34" font-size="22" font-weight="700" fill="#1f2329">按团队统计总消耗（USD）</text>',
    ]
    for index, row in enumerate(top):
        y = top_pad + index * row_height
        spend = float(row["spend_usd"])
        label = str(row["team"])
        label = label if len(label) <= 14 else label[:13] + "..."
        bar_width = max(2, int(chart_width * spend / max_spend))
        percentage = spend / total * 100 if total else 0
        parts.extend(
            [
                f'<text x="24" y="{y + 20}" font-size="15" fill="#333333">{esc(label)}</text>',
                f'<rect x="{left}" y="{y + 4}" width="{bar_width}" height="22" rx="3" fill="{colors[index % len(colors)]}"/>',
                f'<text x="{left + bar_width + 10}" y="{y + 21}" font-size="14" fill="#333333">${money(spend)} · {percentage:.2f}%</text>',
            ]
        )
    parts.append("</svg>")
    return "".join(parts)


def render_pie_chart_svg(team_totals: list[dict[str, object]], total: float, limit: int = 8) -> str:
    if not team_totals or not total:
        return ""
    # A compact donut is less noisy than a full pie when the team count is large.
    slices = team_totals[:limit]
    if len(team_totals) > limit:
        slices = [*slices, {"team": "其他团队", "spend_usd": sum(float(row["spend_usd"]) for row in team_totals[limit:])}]
    cx, cy, radius = 190, 205, 132
    colors = ["#1456F0", "#00A870", "#F5A623", "#E64A19", "#7B61FF", "#14A6B8", "#D05A9D", "#6B7280", "#9B7D25"]
    offset = 0.0
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="430" viewBox="0 0 900 430">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="24" y="34" font-size="22" font-weight="700" fill="#1f2329">团队消耗占比</text>',
    ]
    for index, row in enumerate(slices):
        fraction = float(row["spend_usd"]) / total
        dash = fraction * 2 * 3.14159265 * radius
        gap = 2 * 3.14159265 * radius - dash
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{colors[index % len(colors)]}" '
            f'stroke-width="74" stroke-dasharray="{dash:.3f} {gap:.3f}" stroke-dashoffset="{-offset:.3f}" '
            f'transform="rotate(-90 {cx} {cy})"/>'
        )
        offset += dash
    parts.append(f'<text x="{cx}" y="{cy - 5}" font-size="18" text-anchor="middle" fill="#333333">总消耗</text>')
    parts.append(f'<text x="{cx}" y="{cy + 25}" font-size="20" font-weight="700" text-anchor="middle" fill="#1f2329">${money(total)}</text>')
    for index, row in enumerate(slices):
        y = 82 + index * 34
        spend = float(row["spend_usd"])
        parts.extend(
            [
                f'<rect x="415" y="{y - 13}" width="16" height="16" fill="{colors[index % len(colors)]}"/>',
                f'<text x="443" y="{y}" font-size="15" fill="#333333">{esc(row["team"])}: ${money(spend)} ({spend / total * 100:.2f}%)</text>',
            ]
        )
    parts.append("</svg>")
    return "".join(parts)


def render_xml(rows: list[dict[str, object]], start_date: dt.date, end_date: dt.date, sources: dict[str, str]) -> str:
    team_totals = aggregate(rows, ("team",))
    team_type_totals = aggregate(rows, ("team", "account_type"))
    environment_type_totals = aggregate(rows, ("environment_label", "account_type"))
    total = sum(float(row["spend_usd"]) for row in rows)
    end_inclusive = end_date - dt.timedelta(days=1)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    source_text = "；".join(f"{ENVIRONMENTS[key]['label']}：{value}" for key, value in sources.items())
    parts = [
        f"<title>Her 和 Cursor 账户团队消费统计（{start_date} 至 {end_inclusive}）</title>",
        "<p>本报告统计 Her 与 Cursor 虚拟账户在 LiteLLM 的实际 <code>spend</code>，按团队汇总并计算占比。团队归属优先取 TeamTable、请求 team_id、虚拟 key metadata；缺失时标为“未归属”。</p>",
        f"<p><b>统计窗口：</b>{start_date} 至 {end_inclusive}（UTC，含首尾）；<b>生成时间：</b>{generated}；<b>数据读取：</b>{esc(source_text)}。</p>",
        "<h1>账户类型总览</h1>",
        table(
            ["环境", "账户类型", "消耗 USD", "占本报告总消耗", "请求数", "总 tokens", "key-日桶数"],
            [
                [
                    row["environment_label"], row["account_type"], money(row["spend_usd"]),
                    f"{float(row['spend_usd']) / total * 100:.2f}%" if total else "0.00%",
                    integer(row["requests"]), integer(row["total_tokens"]), integer(row["key_buckets"]),
                ]
                for row in environment_type_totals
            ] + [["合计", "Her + Cursor", money(total), "100.00%", integer(sum(int(row["requests"]) for row in rows)), integer(sum(int(row["total_tokens"]) for row in rows)), integer(sum(int(row["keys"]) for row in rows))]],
        ),
        "<h1>按团队统计</h1>",
        table(
            ["团队", "总消耗 USD", "占比", "请求数", "总 tokens", "key-日桶数"],
            [
                [row["team"], money(row["spend_usd"]), f"{float(row['spend_usd']) / total * 100:.2f}%" if total else "0.00%", integer(row["requests"]), integer(row["total_tokens"]), integer(row["key_buckets"])]
                for row in team_totals
            ],
        ),
        "<h1>团队 × 账户类型</h1>",
        table(
            ["团队", "账户类型", "消耗 USD", "占本报告总消耗", "请求数", "总 tokens"],
            [
                [row["team"], row["account_type"], money(row["spend_usd"]), f"{float(row['spend_usd']) / total * 100:.2f}%" if total else "0.00%", integer(row["requests"]), integer(row["total_tokens"])]
                for row in team_type_totals
            ],
        ),
        "<h1>团队总量图表</h1>",
    ]
    bar = render_bar_chart_svg(team_totals, total)
    pie = render_pie_chart_svg(team_totals, total)
    if bar:
        parts.append(f'<whiteboard type="svg">{bar}</whiteboard>')
    if pie:
        parts.append(f'<whiteboard type="svg">{pie}</whiteboard>')
    return "\n".join(parts) + "\n"


def create_lark_doc(xml: str, identity: str) -> str:
    tmp_dir = REPO_ROOT / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", prefix="team-spend-", dir=tmp_dir, encoding="utf-8", delete=False) as handle:
        handle.write(xml)
        xml_path = Path(handle.name)
    try:
        relative_path = xml_path.relative_to(REPO_ROOT)
        proc = subprocess.run(
            ["lark-cli", "docs", "+create", "--as", identity, "--content", f"@{relative_path}"],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        xml_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Lark document creation failed: {proc.stderr.strip()}")
    return proc.stdout


def main() -> int:
    args = parse_args()
    if args.days <= 0:
        raise SystemExit("--days must be positive")
    end_date = parse_date(args.end_date) if args.end_date else dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    start_date = end_date - dt.timedelta(days=args.days)
    env_keys = ["aliyun", "198"] if args.env == "both" else [args.env]
    rows: list[dict[str, object]] = []
    sources: dict[str, str] = {}
    for env_key in env_keys:
        print(f"[report] loading {ENVIRONMENTS[env_key]['label']}...", file=sys.stderr, flush=True)
        result, source = get_rows(env_key, start_date, end_date, args.cache_dir, args.cache_ttl_minutes, args.refresh)
        rows.extend(result)
        sources[env_key] = source
        print(f"[report] {env_key}: {len(result)} aggregate rows from {source}", file=sys.stderr, flush=True)
    xml = render_xml(rows, start_date, end_date, sources)
    payload = {
        "start_date": start_date.isoformat(),
        "end_date_exclusive": end_date.isoformat(),
        "sources": sources,
        "team_totals": aggregate(rows, ("team",)),
        "team_account_totals": aggregate(rows, ("team", "account_type")),
        "rows": rows,
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.xml_out:
        args.xml_out.parent.mkdir(parents=True, exist_ok=True)
        args.xml_out.write_text(xml, encoding="utf-8")
    if args.create_doc:
        print(create_lark_doc(xml, args.identity), end="")
    elif not args.xml_out:
        print(xml, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
