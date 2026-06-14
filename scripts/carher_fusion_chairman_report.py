#!/usr/bin/env python3
"""Render and optionally publish an executive fusion-diagnosis summary report.

Inputs are the already-scripted aggregate artifacts. This stage does not read
raw chat/PVC data and does not reinterpret individual low-level evidence.
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any


GRADE_ORDER = ["S·深度融合", "A·主动协作", "B·日常使用", "C·初步接入", "D·浅层接触"]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def td(value: Any) -> str:
    return f"<td>{e(value)}</td>"


def th(value: Any) -> str:
    return f'<th background-color="light-gray">{e(value)}</th>'


def pct(count: int, total: int) -> str:
    return f"{round(count * 100 / total, 1)}%" if total else "0%"


def run_json(cmd: list[str], cwd: Path | None = None) -> dict[str, Any]:
    proc = subprocess.run(cmd, text=True, capture_output=True, cwd=str(cwd) if cwd else None)
    if proc.returncode:
        raise RuntimeError(proc.stderr or proc.stdout)
    data = json.loads(proc.stdout)
    if not data.get("ok"):
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
    return data


def rank_rows(items: list[dict[str, Any]], manifest: dict[str, dict[str, Any]]) -> str:
    rows = ""
    for index, score in enumerate(items, start=1):
        url = manifest.get(score["her_id"], {}).get("url", "")
        rows += (
            "<tr>"
            + td(index)
            + td(score["her_id"])
            + td(score["scores"]["total"])
            + td(score["grade"])
            + f'<td><a href="{e(url)}">报告</a></td>'
            + "</tr>"
        )
    return rows


def distribution_svg(grade_counts: Counter[str], total: int) -> str:
    max_count = max(grade_counts.values()) if grade_counts else 0
    colors = {
        "S·深度融合": "#2f8f5b",
        "A·主动协作": "#2d6cdf",
        "B·日常使用": "#c99a1f",
        "C·初步接入": "#d97706",
        "D·浅层接触": "#c43b3b",
    }
    bars = ""
    x0 = 170
    y0 = 40
    row_h = 34
    for index, grade in enumerate(GRADE_ORDER):
        count = grade_counts.get(grade, 0)
        width = round((count / max_count) * 430) if max_count else 0
        y = y0 + index * row_h
        bars += (
            f'<text x="20" y="{y + 18}" font-size="14" fill="#222">{e(grade)}</text>'
            f'<rect x="{x0}" y="{y}" width="{width}" height="22" fill="{colors[grade]}" rx="4"/>'
            f'<text x="{x0 + width + 10}" y="{y + 17}" font-size="13" fill="#222">{count} ({pct(count, total)})</text>'
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="760" height="230" viewBox="0 0 760 230">
<rect width="760" height="230" fill="#ffffff"/>
<text x="20" y="24" font-size="16" font-weight="700" fill="#111">Her 融合度等级分布（n={total}）</text>
{bars}
<text x="20" y="222" font-size="12" fill="#666">口径：系统证据分；C类融合效果需Her/owner确认后形成最终确认分。</text>
</svg>"""


def render(args: argparse.Namespace) -> str:
    scores = load_jsonl(Path(args.scores))
    evidence_summary = json.loads(Path(args.evidence_summary).read_text())
    score_summary = json.loads(Path(args.score_summary).read_text())
    manifest = {item["her_id"]: item for item in load_jsonl(Path(args.manifest))}
    total = len(scores)
    grade_counts = Counter(score["grade"] for score in scores)
    mature_count = grade_counts["S·深度融合"] + grade_counts["A·主动协作"]
    base_count = grade_counts["B·日常使用"]
    weak_count = grade_counts["C·初步接入"] + grade_counts["D·浅层接触"]
    metrics = evidence_summary["metrics"]
    c_avg = round(sum(score["scores"]["C_total"] for score in scores) / total, 1)

    def avg(key: str) -> float:
        return round(sum(score["scores"][key] for score in scores) / total, 1)

    grade_rows = "".join(
        "<tr>" + td(grade) + td(grade_counts.get(grade, 0)) + td(pct(grade_counts.get(grade, 0), total)) + "</tr>"
        for grade in GRADE_ORDER
    )
    dim_rows = "".join(
        "<tr>" + td(name) + td(full) + td(value) + td(note) + "</tr>"
        for name, full, value, note in [
            ("A 连接强度", 30, avg("A_total"), "使用频次、群覆盖、记忆沉淀整体已形成差异化分布"),
            ("B 融合质量", 40, avg("B_total"), "质量最高的维度，说明有效 Her 正在进入课题协作"),
            ("C 融合效果", 20, c_avg, "系统只能估算证据强弱，组织传导成功必须由 Her/owner 复核"),
            ("D 进化能力", 10, avg("D_total"), "纠偏、模板、能力扩展证据已可捕捉，复发率仍需长期跟踪"),
            ("总分", 100, score_summary["mean"], "系统证据均分，不等于 owner 最终确认分"),
        ]
    )
    metric_rows = "".join(
        "<tr>" + td(label) + td(data["p50"]) + td(data["p75"]) + td(data["p90"]) + td(data["max"]) + "</tr>"
        for key, label in [
            ("group_recent_messages", "群消息数"),
            ("group_bot_mentions", "Bot mention"),
            ("groups_with_recent_messages", "覆盖群数"),
            ("files_recent_count", "近期文件数"),
            ("memory_chunks", "Memory chunks"),
            ("files_recent_active_days", "文件活跃天数"),
        ]
        for data in [metrics[key]]
    )
    warn_rows = "".join(
        "<tr>" + td(name) + td(count) + td("已在个体报告中标注；低置信字段不作为最终确认分") + "</tr>"
        for name, count in evidence_summary.get("data_quality_warning_counts", {}).items()
    )
    top = sorted(scores, key=lambda item: item["scores"]["total"], reverse=True)[:10]
    bottom = sorted(scores, key=lambda item: item["scores"]["total"])[:10]

    return f"""<title>Her 融合自诊断总体统计报告（董事长版 v2）</title>
<callout emoji="ℹ️" background-color="light-blue" border-color="blue">
  <p><b>核心结论：</b>本次已完成 {total} 个 Her 的基础数据抽取、系统证据评分、个体报告生成和飞书表格回填。整体呈现“少数深度融合、约四分之一主动协作、半数以上仍需运营牵引”的结构。</p>
  <p><b>v2校准：</b>根据洪源反馈，C类“融合效果”从最终判断降级为系统证据分；组织传导是否成功、产出是否不可替代、认知时间是否释放，必须由 Her/owner 自检确认。</p>
</callout>

<h1>一、董事长看板</h1>
<table><thead><tr>{th('指标')}{th('结果')}{th('管理含义')}</tr></thead><tbody>
<tr>{td('覆盖 Her 数')}{td(total)}{td('已形成全量基线，可用于后续月度复盘和横向对比')}</tr>
<tr>{td('数据采集成功率')}{td(f"{evidence_summary['ok_count']}/{evidence_summary['record_count']}，100%")}{td('本轮 K8s/PVC 聚合采集可支撑规模化评估')}</tr>
<tr>{td('均分 / 最高 / 最低')}{td(f"{score_summary['mean']} / {score_summary['max']} / {score_summary['min']}")}{td('分数有区分度，不是普遍高分')}</tr>
<tr>{td('S+A 成熟融合')}{td(f"{mature_count} 个，{pct(mature_count, total)}")}{td('可优先沉淀标杆打法和复用模板')}</tr>
<tr>{td('B 日常使用')}{td(f"{base_count} 个，{pct(base_count, total)}")}{td('已有使用基础，关键是推动从调用走向业务闭环')}</tr>
<tr>{td('C+D 弱融合')}{td(f"{weak_count} 个，{pct(weak_count, total)}")}{td('需要 owner 牵引、场景明确和低活跃治理')}</tr>
<tr>{td('C类复核状态')}{td('待 Her/owner 确认')}{td('系统只能证明有证据，不能替代组织采纳判断')}</tr>
<tr>{td('个体报告回填')}{td(f"{len(manifest)}/{total}，100%")}{td('董事长看总体，负责人可下钻到每个 Her 报告')}</tr>
</tbody></table>

<whiteboard type="svg">{distribution_svg(grade_counts, total)}</whiteboard>

<hr/>
<h1>二、等级分布</h1>
<table><thead><tr>{th('等级')}{th('数量')}{th('占比')}</tr></thead><tbody>{grade_rows}</tbody></table>
<callout emoji="❗" background-color="light-yellow" border-color="yellow"><p><b>判断：</b>S 只有 {grade_counts['S·深度融合']} 个，A 有 {grade_counts['A·主动协作']} 个，说明真正深度融合仍是少数；C+D 合计 {weak_count} 个，占 {pct(weak_count, total)}，后续最大提升空间在“弱融合到日常使用”的转化。</p></callout>

<hr/>
<h1>三、维度均分与C类校准</h1>
<table><thead><tr>{th('维度')}{th('满分')}{th('均分')}{th('解释')}</tr></thead><tbody>{dim_rows}</tbody></table>
<callout emoji="❗" background-color="light-yellow" border-color="yellow">
  <p><b>C类不能自动定论：</b>群消息、文件、Memory 可以证明“传播和沉淀迹象”，但不能证明“组织传导成功”。因此 C1/C2/C3 后续应拆为系统证据分、Her自检分、最终确认分三层。</p>
</callout>

<hr/>
<h1>四、基础数据分位</h1>
<table><thead><tr>{th('指标')}{th('P50')}{th('P75')}{th('P90')}{th('最高')}</tr></thead><tbody>{metric_rows}</tbody></table>
<p>这组分位数是后续评分的核心基础：先用脚本把每个 Her 的基础数据抽出来，再按全员分布归一化，最后生成系统证据分和报告，避免人工逐个翻海量底层数据。</p>

<hr/>
<h1>五、头部与尾部样本</h1>
<h2>Top 10</h2>
<table><thead><tr>{th('排名')}{th('Her')}{th('总分')}{th('等级')}{th('报告')}</tr></thead><tbody>{rank_rows(top, manifest)}</tbody></table>
<h2>Bottom 10</h2>
<table><thead><tr>{th('排名')}{th('Her')}{th('总分')}{th('等级')}{th('报告')}</tr></thead><tbody>{rank_rows(bottom, manifest)}</tbody></table>

<hr/>
<h1>六、数据可信度与限制</h1>
<table><thead><tr>{th('告警项')}{th('数量')}{th('处理方式')}</tr></thead><tbody>{warn_rows}</tbody></table>
<callout emoji="❗" background-color="light-yellow" border-color="yellow"><p><b>关键限制：</b>owner alias 当前未完整配置，因此 owner 主动性、组织采纳、过度依赖仍需人工复核。本报告将这些字段明确标为低置信，不把系统证据分包装成最终确认分。</p></callout>

<hr/>
<h1>七、管理建议</h1>
<table><thead><tr>{th('优先级')}{th('动作')}{th('目标')}</tr></thead><tbody>
<tr>{td('P0')}{td('补齐 owner alias / 组织归属映射')}{td('让 owner 主动性、组织采纳、过度依赖从人工判断变成可程序化评估')}</tr>
<tr>{td('P0')}{td('新增 C类复核流程')}{td('Her/owner 确认 C1/C2/C3 后再形成最终确认分')}</tr>
<tr>{td('P1')}{td('复盘 S+A 头部 Her 的协作模式')}{td('沉淀标杆任务模板、场景清单和 owner 触发方式')}</tr>
<tr>{td('P1')}{td('对 C+D 建立低活跃治理名单')}{td('明确是无场景、无 owner 牵引、无记忆沉淀，还是机器人配置问题')}</tr>
<tr>{td('P2')}{td('建立月度固定跑批机制')}{td('持续比较趋势，而不是单次静态排名')}</tr>
</tbody></table>

<h1>八、数据源与下钻入口</h1>
<table><thead><tr>{th('对象')}{th('链接')}</tr></thead><tbody>
<tr>{td('her基础数据表')}{td('https://t83dfrspj4.feishu.cn/wiki/VaoGw1CFMii4NJk0vzdcliL5nGg?table=tbln4E5SyJNoOi2g&view=vewQzcqLx3')}</tr>
<tr>{td('her自我检查表')}{td('https://t83dfrspj4.feishu.cn/wiki/CjAjwRWmai9hghkg0y9cm4Donha?table=tblHoikuAaekwki5&view=vewWQCXAvU')}</tr>
<tr>{td('carher-3 个体报告')}{td(manifest['carher-3']['url'])}</tr>
<tr>{td('carher-10 个体报告')}{td(manifest['carher-10']['url'])}</tr>
</tbody></table>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", required=True)
    parser.add_argument("--evidence-summary", required=True)
    parser.add_argument("--score-summary", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--create-doc", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(args))
    result = {"output": str(output)}
    if args.create_doc:
        data = run_json([
            "lark-cli",
            "docs",
            "+create",
            "--api-version",
            "v2",
            "--content",
            f"@{output.name}",
            "--format",
            "json",
        ], cwd=output.parent)
        result["url"] = data["data"]["document"]["url"]
        result["document_id"] = data["data"]["document"]["document_id"]
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
