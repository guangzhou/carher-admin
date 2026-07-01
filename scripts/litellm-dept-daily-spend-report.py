#!/usr/bin/env python3
"""Generate a daily department spend report for Aliyun and 198 LiteLLM.

The script runs read-only SQL against both LiteLLM PostgreSQL databases, then
renders a Lark Doc XML report. It can either print/save the XML or create the
document via lark-cli.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
JMS = REPO_ROOT / "scripts" / "jms"


ENVIRONMENTS = {
    "aliyun": {
        "label": "阿里云 ACK LiteLLM",
        "ssh_target": "k8s-work-226",
        "kubectl_prefix": "kubectl -n carher exec -i litellm-db-0 --",
        "psql": "psql -U litellm -d litellm",
    },
    "198": {
        "label": "198 Pro LiteLLM",
        "ssh_target": "AIYJY-litellm",
        "kubectl_prefix": "kubectl -n litellm-product exec -i litellm-db-0 --",
        "psql": "psql -U litellm -d litellm",
    },
}


SPEND_BY_KEY_SQL_TEMPLATE = r"""
WITH logs AS (
  SELECT
    sl."startTime",
    sl.spend,
    sl.total_tokens,
    sl.api_key,
    vt.key_alias,
    vt.team_id AS vt_team_id,
    vt.metadata AS vt_metadata,
    sl.team_id AS sl_team_id
  FROM "LiteLLM_SpendLogs" sl
  LEFT JOIN "LiteLLM_VerificationToken" vt ON sl.api_key = vt.token
  WHERE sl."startTime" >= DATE '{start_date}'
    AND sl."startTime" < DATE '{end_date}'
), enriched AS (
  SELECT
    DATE_TRUNC('day', logs."startTime")::date AS spend_day,
    COALESCE(
      NULLIF(TRIM(COALESCE(
        tt.team_alias,
        logs.sl_team_id,
        logs.vt_team_id,
        logs.vt_metadata->>'team',
        logs.vt_metadata->>'department',
        logs.vt_metadata->>'dept',
        ''
      )), ''),
      '未归属'
    ) AS department,
    COALESCE(logs.key_alias, logs.api_key, 'unknown') AS key_alias,
    logs.spend,
    logs.total_tokens
  FROM logs
  LEFT JOIN "LiteLLM_TeamTable" tt ON tt.team_id = COALESCE(logs.sl_team_id, logs.vt_team_id)
)
SELECT
  spend_day::text,
  department,
  key_alias,
  ROUND(SUM(spend)::numeric, 2)::text AS spend_usd,
  COUNT(*)::text AS requests,
  COALESCE(SUM(total_tokens), 0)::text AS total_tokens
FROM enriched
GROUP BY 1, 2, 3
ORDER BY 1, SUM(spend) DESC, 2, 3;
"""

KEY_MAPPING_SQL = r"""
SELECT
  vt.key_alias,
  COALESCE(
    NULLIF(TRIM(COALESCE(
      tt.team_alias,
      vt.team_id,
      vt.metadata->>'team_alias',
      vt.metadata->>'department',
      vt.metadata->>'team',
      vt.metadata->>'dept',
      ''
    )), ''),
    ''
  ) AS department,
  COALESCE(NULLIF(vt.metadata->>'source', ''), '') AS source
FROM "LiteLLM_VerificationToken" vt
LEFT JOIN "LiteLLM_TeamTable" tt ON tt.team_id = vt.team_id
WHERE vt.key_alias IS NOT NULL
ORDER BY vt.key_alias;
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily LiteLLM department spend report for Aliyun ACK and 198 Pro."
    )
    parser.add_argument("--start-date", default="2026-06-01", help="inclusive UTC date, YYYY-MM-DD")
    parser.add_argument(
        "--end-date",
        default=None,
        help="exclusive UTC date, YYYY-MM-DD; default is tomorrow UTC so today is included as partial day",
    )
    parser.add_argument(
        "--env",
        choices=["both", "aliyun", "198"],
        default="both",
        help="which LiteLLM environment to query",
    )
    parser.add_argument("--json-out", type=Path, help="optional path for raw aggregated JSON")
    parser.add_argument("--xml-out", type=Path, help="optional path for rendered Lark Doc XML")
    parser.add_argument("--create-doc", action="store_true", help="create a Lark document with lark-cli docs +create")
    return parser.parse_args()


def validate_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"invalid date {value!r}, expected YYYY-MM-DD") from exc


def run_psql(env_key: str, sql: str) -> str:
    env = ENVIRONMENTS[env_key]
    remote_cmd = (
        f"{env['kubectl_prefix']} {env['psql']} -F $'\\t' -A "
        "-P pager=off -P footer=off -v ON_ERROR_STOP=1 -q"
    )
    cmd = [str(JMS), "ssh", env["ssh_target"], remote_cmd]
    proc = subprocess.run(
        cmd,
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{env_key} query failed with exit {proc.returncode}\nSTDERR:\n{proc.stderr.strip()}"
        )
    return proc.stdout


def read_key_mappings(env_key: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    reader = csv.DictReader(run_psql(env_key, KEY_MAPPING_SQL).splitlines(), delimiter="\t")
    for row in reader:
        alias = (row.get("key_alias") or "").strip()
        dept = clean_department(row.get("department"))
        if alias and dept:
            rows[alias] = dept
    return rows


def run_remote_query(env_key: str, start_date: dt.date, end_date: dt.date) -> list[dict[str, object]]:
    sql = SPEND_BY_KEY_SQL_TEMPLATE.format(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )
    rows: list[dict[str, object]] = []
    reader = csv.DictReader(run_psql(env_key, sql).splitlines(), delimiter="\t")
    for row in reader:
        if not row or row.get("spend_day") in (None, ""):
            continue
        rows.append(
            {
                "environment": env_key,
                "environment_label": ENVIRONMENTS[env_key]["label"],
                "date": row["spend_day"],
                "key_alias": row["key_alias"] or "unknown",
                "local_department": row["department"] or "",
                "spend_usd": float(row["spend_usd"] or 0),
                "requests": int(row["requests"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }
        )
    return rows


def clean_department(value: object) -> str:
    text = str(value or "").strip()
    if not text or text == "未归属":
        return ""
    return text


def apply_key_mappings(
    spend_rows: list[dict[str, object]],
    mappings: dict[str, dict[str, str]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Apply corrected key->department mapping, then aggregate by day/env/dept."""
    merged_map: dict[str, str] = {}
    merged_map.update(mappings.get("aliyun", {}))
    merged_map.update(mappings.get("198", {}))

    coverage = {
        "spend_rows": len(spend_rows),
        "mapped_spend_usd": 0.0,
        "unmapped_spend_usd": 0.0,
        "env": {},
    }
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    key_sets: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for row in spend_rows:
        env = str(row["environment"])
        alias = str(row.get("key_alias") or "unknown")
        local_dept = clean_department(row.get("local_department"))
        dept = local_dept or merged_map.get(alias) or "未归属"
        source = "local" if local_dept else ("key-map" if alias in merged_map else "unmapped")
        spend = float(row["spend_usd"])

        env_cov = coverage["env"].setdefault(
            env,
            {"mapped_spend_usd": 0.0, "unmapped_spend_usd": 0.0, "mapped_keys": set(), "unmapped_keys": set()},
        )
        if dept == "未归属":
            coverage["unmapped_spend_usd"] = float(coverage["unmapped_spend_usd"]) + spend
            env_cov["unmapped_spend_usd"] = float(env_cov["unmapped_spend_usd"]) + spend
            env_cov["unmapped_keys"].add(alias)
        else:
            coverage["mapped_spend_usd"] = float(coverage["mapped_spend_usd"]) + spend
            env_cov["mapped_spend_usd"] = float(env_cov["mapped_spend_usd"]) + spend
            env_cov["mapped_keys"].add(alias)

        key = (env, str(row["date"]), dept)
        item = grouped.setdefault(
            key,
            {
                "environment": env,
                "environment_label": ENVIRONMENTS[env]["label"],
                "date": row["date"],
                "department": dept,
                "spend_usd": 0.0,
                "requests": 0,
                "total_tokens": 0,
                "mapping_source": source,
            },
        )
        item["spend_usd"] = float(item["spend_usd"]) + spend
        item["requests"] = int(item["requests"]) + int(row["requests"])
        item["total_tokens"] = int(item["total_tokens"]) + int(row["total_tokens"])
        key_sets[key].add(alias)

    for key, item in grouped.items():
        item["keys"] = len(key_sets[key])

    # Convert sets so json.dumps works.
    for env_cov in coverage["env"].values():
        env_cov["mapped_keys"] = len(env_cov["mapped_keys"])
        env_cov["unmapped_keys"] = len(env_cov["unmapped_keys"])
    return sorted(grouped.values(), key=lambda r: (str(r["environment"]), str(r["date"]), str(r["department"]))), coverage


def money(value: float) -> str:
    return f"{value:,.2f}"


def integer(value: int) -> str:
    return f"{value:,}"


def x(value: object) -> str:
    return html.escape(str(value), quote=True)


def summarize(rows: Iterable[dict[str, object]]) -> dict[str, dict[str, float | int]]:
    summary: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"spend_usd": 0.0, "requests": 0, "total_tokens": 0, "keys": 0}
    )
    key_sets: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in rows:
        env = str(row["environment"])
        summary[env]["spend_usd"] = float(summary[env]["spend_usd"]) + float(row["spend_usd"])
        summary[env]["requests"] = int(summary[env]["requests"]) + int(row["requests"])
        summary[env]["total_tokens"] = int(summary[env]["total_tokens"]) + int(row["total_tokens"])
        key_sets[env].add((str(row["date"]), str(row["department"])))
    for env, pairs in key_sets.items():
        # The SQL returns distinct keys per day/department. This field is not a
        # global distinct-key count, so label it as key-day-department buckets.
        summary[env]["keys"] = len(pairs)
    return dict(summary)


def daily_totals(rows: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        key = (str(row["environment"]), str(row["environment_label"]), str(row["date"]))
        item = grouped.setdefault(
            key,
            {
                "environment": key[0],
                "environment_label": key[1],
                "date": key[2],
                "spend_usd": 0.0,
                "requests": 0,
                "total_tokens": 0,
                "departments": 0,
            },
        )
        item["spend_usd"] = float(item["spend_usd"]) + float(row["spend_usd"])
        item["requests"] = int(item["requests"]) + int(row["requests"])
        item["total_tokens"] = int(item["total_tokens"]) + int(row["total_tokens"])
        item["departments"] = int(item["departments"]) + 1
    return sorted(grouped.values(), key=lambda r: (str(r["environment"]), str(r["date"])))


def top_departments(rows: list[dict[str, object]], env_key: str, limit: int = 20) -> list[dict[str, object]]:
    totals: dict[str, dict[str, object]] = {}
    for row in rows:
        if row["environment"] != env_key:
            continue
        dept = str(row["department"])
        item = totals.setdefault(
            dept,
            {"department": dept, "spend_usd": 0.0, "requests": 0, "total_tokens": 0},
        )
        item["spend_usd"] = float(item["spend_usd"]) + float(row["spend_usd"])
        item["requests"] = int(item["requests"]) + int(row["requests"])
        item["total_tokens"] = int(item["total_tokens"]) + int(row["total_tokens"])
    return sorted(totals.values(), key=lambda r: float(r["spend_usd"]), reverse=True)[:limit]


def department_totals(rows: list[dict[str, object]], env_key: str | None = None) -> list[dict[str, object]]:
    totals: dict[str, dict[str, object]] = {}
    for row in rows:
        if env_key is not None and row["environment"] != env_key:
            continue
        dept = str(row["department"])
        item = totals.setdefault(
            dept,
            {"department": dept, "spend_usd": 0.0, "requests": 0, "total_tokens": 0, "keys": set()},
        )
        item["spend_usd"] = float(item["spend_usd"]) + float(row["spend_usd"])
        item["requests"] = int(item["requests"]) + int(row["requests"])
        item["total_tokens"] = int(item["total_tokens"]) + int(row["total_tokens"])
        # Aggregated rows only have per-day key counts, so this is a bucket count.
        item["keys"].add((str(row["environment"]), str(row["date"]), dept))
    result = []
    for item in totals.values():
        result.append(
            {
                "department": item["department"],
                "spend_usd": float(item["spend_usd"]),
                "requests": int(item["requests"]),
                "total_tokens": int(item["total_tokens"]),
                "buckets": len(item["keys"]),
            }
        )
    return sorted(result, key=lambda r: float(r["spend_usd"]), reverse=True)


def render_bar_chart_svg(rows: list[dict[str, object]], limit: int = 20) -> str:
    top = department_totals(rows)[:limit]
    if not top:
        return ""
    max_spend = max(float(row["spend_usd"]) for row in top) or 1.0
    total_spend = sum(float(row["spend_usd"]) for row in department_totals(rows)) or 1.0
    width = 1200
    row_h = 34
    left = 220
    right = 180
    top_pad = 54
    height = top_pad + row_h * len(top) + 34
    chart_w = width - left - right
    colors = ["#1456F0", "#00A870", "#F5A623", "#E64A19", "#7B61FF"]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="24" y="32" font-size="22" font-weight="700" fill="#1f2329">部门经费总额 Top 20（USD）</text>',
    ]
    for idx, row in enumerate(top):
        y = top_pad + idx * row_h
        spend = float(row["spend_usd"])
        bar_w = max(2, int(chart_w * spend / max_spend))
        pct = spend / total_spend * 100
        dept = str(row["department"])
        label = dept if len(dept) <= 14 else dept[:13] + "..."
        parts.extend(
            [
                f'<text x="24" y="{y + 20}" font-size="15" fill="#333333">{x(label)}</text>',
                f'<rect x="{left}" y="{y + 4}" width="{bar_w}" height="22" rx="3" fill="{colors[idx % len(colors)]}"/>',
                f'<text x="{left + bar_w + 10}" y="{y + 21}" font-size="14" fill="#333333">${money(spend)} · {pct:.2f}%</text>',
            ]
        )
    parts.append("</svg>")
    return "".join(parts)


def table(headers: list[str], body_rows: list[list[object]]) -> str:
    header_xml = "".join(f'<th background-color="light-gray">{x(h)}</th>' for h in headers)
    rows_xml = []
    for body_row in body_rows:
        rows_xml.append("<tr>" + "".join(f"<td>{x(cell)}</td>" for cell in body_row) + "</tr>")
    return f"<table><thead><tr>{header_xml}</tr></thead><tbody>{''.join(rows_xml)}</tbody></table>"


def render_xml(
    rows: list[dict[str, object]],
    start_date: dt.date,
    end_date: dt.date,
    coverage: dict[str, object] | None = None,
) -> str:
    summary = summarize(rows)
    day_rows = daily_totals(rows)
    actual_end = end_date - dt.timedelta(days=1)
    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    parts: list[str] = [
        "<title>LiteLLM 部门经费按天统计（2026-06-01 起）</title>",
        '<callout emoji="📌" background-color="light-yellow" border-color="yellow">',
        f"<p><b>统计窗口：</b>{x(start_date)} 至 {x(actual_end)}（按 LiteLLM DB UTC 日期切分；最后一天可能是不完整日）。</p>",
        f"<p><b>生成时间：</b>{x(now_utc)}。</p>",
        "<p><b>数据源：</b>阿里云 ACK namespace <code>carher</code> 的 LiteLLM PostgreSQL；198 Pro <code>AIYJY-litellm / litellm-product</code> 的 LiteLLM PostgreSQL。</p>",
        "<p><b>口径：</b>先按 <code>LiteLLM_SpendLogs.startTime + key_alias</code> 聚合 <code>spend</code>，再套用 key → 部门映射。映射优先级：当前环境 key 的 <code>TeamTable.team_alias</code> / <code>team_id</code> / metadata；若 198 Pro 的 <code>carher-*</code> 缺部门，则回退阿里云同名 <code>carher-*</code> 映射。仍无映射才记为“未归属”。</p>",
        "</callout>",
        "<h1>一、环境总览</h1>",
    ]

    summary_rows: list[list[object]] = []
    for env_key in [key for key in ("aliyun", "198") if key in summary]:
        data = summary[env_key]
        unassigned = sum(
            float(row["spend_usd"])
            for row in rows
            if row["environment"] == env_key and row["department"] == "未归属"
        )
        total = float(data["spend_usd"])
        ratio = f"{(unassigned / total * 100):.2f}%" if total else "0.00%"
        summary_rows.append(
            [
                ENVIRONMENTS[env_key]["label"],
                money(total),
                integer(int(data["requests"])),
                integer(int(data["total_tokens"])),
                money(unassigned),
                ratio,
            ]
        )
    if summary_rows:
        total_spend = sum(float(data["spend_usd"]) for data in summary.values())
        total_requests = sum(int(data["requests"]) for data in summary.values())
        total_tokens = sum(int(data["total_tokens"]) for data in summary.values())
        total_unassigned = sum(
            float(row["spend_usd"]) for row in rows if row["department"] == "未归属"
        )
        summary_rows.append(
            [
                "合计",
                money(total_spend),
                integer(total_requests),
                integer(total_tokens),
                money(total_unassigned),
                f"{(total_unassigned / total_spend * 100):.2f}%" if total_spend else "0.00%",
            ]
        )
    parts.append(table(["环境", "消耗 USD", "请求数", "总 tokens", "未归属消耗 USD", "未归属占比"], summary_rows))

    combined = department_totals(rows)
    total_department_spend = sum(float(row["spend_usd"]) for row in combined)
    parts.append("<h1>二、部门总计与占比</h1>")
    chart_svg = render_bar_chart_svg(rows)
    if chart_svg:
        parts.append(f'<whiteboard type="svg">{chart_svg}</whiteboard>')
    parts.append(
        table(
            ["部门", "消耗 USD", "占总经费", "请求数", "总 tokens"],
            [
                [
                    row["department"],
                    money(float(row["spend_usd"])),
                    f"{(float(row['spend_usd']) / total_department_spend * 100):.2f}%" if total_department_spend else "0.00%",
                    integer(int(row["requests"])),
                    integer(int(row["total_tokens"])),
                ]
                for row in combined
            ],
        )
    )

    if coverage:
        coverage_rows: list[list[object]] = []
        for env_key in [key for key in ("aliyun", "198") if key in coverage.get("env", {})]:
            env_cov = coverage["env"][env_key]
            mapped = float(env_cov["mapped_spend_usd"])
            unmapped = float(env_cov["unmapped_spend_usd"])
            total = mapped + unmapped
            coverage_rows.append(
                [
                    ENVIRONMENTS[env_key]["label"],
                    money(mapped),
                    money(unmapped),
                    f"{(unmapped / total * 100):.2f}%" if total else "0.00%",
                    integer(int(env_cov["mapped_keys"])),
                    integer(int(env_cov["unmapped_keys"])),
                ]
            )
        if coverage_rows:
            parts.append("<h1>三、映射覆盖率</h1>")
            parts.append(
                table(
                    ["环境", "已映射消耗 USD", "未归属消耗 USD", "未归属占比", "已映射 key 数", "未归属 key 数"],
                    coverage_rows,
                )
            )

    parts.extend(
        [
            '<callout emoji="⚠️" background-color="light-red" border-color="red">',
            "<p>“未归属”不是实际部门，而是 key 在两套 LiteLLM key 表中都没有可用部门映射。正式分摊前应优先补齐这些 key。</p>",
            "</callout>",
            "<h1>四、按天总览</h1>",
        ]
    )
    parts.append(
        table(
            ["环境", "日期(UTC)", "消耗 USD", "请求数", "总 tokens", "部门桶数"],
            [
                [
                    row["environment_label"],
                    row["date"],
                    money(float(row["spend_usd"])),
                    integer(int(row["requests"])),
                    integer(int(row["total_tokens"])),
                    integer(int(row["departments"])),
                ]
                for row in day_rows
            ],
        )
    )

    for env_key in [key for key in ("aliyun", "198") if any(row["environment"] == key for row in rows)]:
        label = ENVIRONMENTS[env_key]["label"]
        parts.append(f"<h1>{x(label)}：部门总额 Top 20</h1>")
        parts.append(
            table(
                ["部门", "消耗 USD", "请求数", "总 tokens"],
                [
                    [
                        row["department"],
                        money(float(row["spend_usd"])),
                        integer(int(row["requests"])),
                        integer(int(row["total_tokens"])),
                    ]
                    for row in top_departments(rows, env_key)
                ],
            )
        )

        parts.append(f"<h1>{x(label)}：按天 × 部门明细</h1>")
        env_rows = [row for row in rows if row["environment"] == env_key]
        parts.append(
            table(
                ["日期(UTC)", "部门", "消耗 USD", "请求数", "总 tokens", "key 数"],
                [
                    [
                        row["date"],
                        row["department"],
                        money(float(row["spend_usd"])),
                        integer(int(row["requests"])),
                        integer(int(row["total_tokens"])),
                        integer(int(row["keys"])),
                    ]
                    for row in sorted(
                        env_rows,
                        key=lambda r: (str(r["date"]), -float(r["spend_usd"]), str(r["department"])),
                    )
                ],
            )
        )

    return "\n".join(parts) + "\n"


def render_doc_sections(
    rows: list[dict[str, object]],
    start_date: dt.date,
    end_date: dt.date,
    coverage: dict[str, object] | None = None,
) -> list[str]:
    """Render smaller XML sections for Lark create + append."""
    full = render_xml(rows, start_date, end_date, coverage)
    marker = "<h1>阿里云 ACK LiteLLM：按天 × 部门明细</h1>"
    head = full.split(marker, 1)[0]
    sections = [head]

    for env_key in [key for key in ("aliyun", "198") if any(row["environment"] == key for row in rows)]:
        env_rows = [row for row in rows if row["environment"] == env_key]
        for day in sorted({str(row["date"]) for row in env_rows}):
            day_rows = sorted(
                [row for row in env_rows if str(row["date"]) == day],
                key=lambda r: (-float(r["spend_usd"]), str(r["department"])),
            )
            sections.append(
                f"<h1>{x(ENVIRONMENTS[env_key]['label'])}：{x(day)} 部门明细</h1>\n"
                + table(
                    ["部门", "消耗 USD", "请求数", "总 tokens", "key 数"],
                    [
                        [
                            row["department"],
                            money(float(row["spend_usd"])),
                            integer(int(row["requests"])),
                            integer(int(row["total_tokens"])),
                            integer(int(row["keys"])),
                        ]
                        for row in day_rows
                    ],
                )
                + "\n"
            )
    return sections


def _run_lark_with_content(args: list[str], content: str) -> subprocess.CompletedProcess[str]:
    tmp_dir = REPO_ROOT / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".xml",
        prefix="litellm-dept-daily-spend-",
        dir=tmp_dir,
        delete=False,
        encoding="utf-8",
    ) as handle:
        handle.write(content)
        path = Path(handle.name)
    rel_path = path.relative_to(REPO_ROOT)
    try:
        return subprocess.run(
            [*args, "--content", f"@{rel_path}"],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        path.unlink(missing_ok=True)


def create_lark_doc(xml: str) -> str:
    proc = _run_lark_with_content(
        ["lark-cli", "docs", "+create", "--api-version", "v2"],
        xml,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"lark-cli docs +create failed\nSTDERR:\n{proc.stderr.strip()}")
    return proc.stdout


def create_lark_doc_sections(sections: list[str]) -> str:
    first = _run_lark_with_content(
        ["lark-cli", "docs", "+create", "--api-version", "v2"],
        sections[0],
    )
    if first.returncode != 0:
        raise RuntimeError(f"lark-cli docs +create failed\nSTDERR:\n{first.stderr.strip()}")
    payload = json.loads(first.stdout)
    doc_url = payload["data"]["document"]["url"]
    for idx, section in enumerate(sections[1:], start=2):
        print(f"[report] appending section {idx}/{len(sections)}...", file=sys.stderr)
        proc = _run_lark_with_content(
            [
                "lark-cli",
                "docs",
                "+update",
                "--api-version",
                "v2",
                "--doc",
                doc_url,
                "--command",
                "append",
            ],
            section,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"lark-cli docs +update append failed at section {idx}\nSTDERR:\n{proc.stderr.strip()}"
            )
    return first.stdout


def main() -> int:
    args = parse_args()
    start_date = validate_date(args.start_date)
    end_date = validate_date(args.end_date) if args.end_date else dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    if end_date <= start_date:
        raise SystemExit("--end-date must be after --start-date")

    env_keys = ["aliyun", "198"] if args.env == "both" else [args.env]
    print("[report] reading key mappings...", file=sys.stderr)
    mappings = {env_key: read_key_mappings(env_key) for env_key in env_keys}

    raw_spend_rows: list[dict[str, object]] = []
    for env_key in env_keys:
        print(f"[report] querying {ENVIRONMENTS[env_key]['label']}...", file=sys.stderr)
        raw_spend_rows.extend(run_remote_query(env_key, start_date, end_date))

    rows, coverage = apply_key_mappings(raw_spend_rows, mappings)

    if args.json_out:
        args.json_out.write_text(
            json.dumps(
                {
                    "start_date": start_date.isoformat(),
                    "end_date_exclusive": end_date.isoformat(),
                    "mapping_counts": {env: len(items) for env, items in mappings.items()},
                    "coverage": coverage,
                    "department_totals": department_totals(rows),
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    xml = render_xml(rows, start_date, end_date, coverage)
    if args.xml_out:
        args.xml_out.write_text(xml, encoding="utf-8")

    if args.create_doc:
        print(create_lark_doc_sections(render_doc_sections(rows, start_date, end_date, coverage)), end="")
    else:
        print(xml, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
