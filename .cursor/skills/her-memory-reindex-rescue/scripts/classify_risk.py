#!/usr/bin/env python3
"""读 deep_scan_one.sh 输出的 TSV，按 her-68 经验分类风险，输出 Markdown 报告。

风险等级判定（CRITICAL > HIGH > MED > OK）:
  CRITICAL —— 必救（reindex 死循环已成事实）:
    1) TMP_COUNT >= 1 且 MAIN_AGE_H >= 24      （her-68 模式：tmp 在 + 主库多天没更新）
    2) TMP_COUNT >= 2                         （历史多次失败）
    3) RESTARTS >= 5 且 TMP_COUNT >= 1         （反复 OOM + 仍有孤儿）

  HIGH —— 强烈建议救援:
    1) TMP_COUNT >= 1 且 MAIN_AGE_H >= 12
    2) RESTARTS >= 3 且 POD_AGE_M < 120 且 WS_READY != YES
    3) TMP_OLDEST_AGE_H >= 6 且 TMP_COUNT >= 1
    4) RESTARTS >= 5 且 LAST_OOM != "-"

  MED —— 观察:
    1) TMP_COUNT >= 1 且 TMP_ACTIVE >= 1 且 MAIN_AGE_H < 12   （正在 reindex，可能正常）
    2) RESTARTS >= 2 且 MAIN_AGE_H >= 6
    3) LAST_OOM != "-"

  OK —— 健康
"""

import sys
import os
import subprocess

COLS = [
    "POD", "HID", "RESTARTS", "LAST_OOM", "POD_AGE_M", "WS_READY",
    "MAIN_MB", "MAIN_AGE_H",
    "TMP_COUNT", "TMP_ACTIVE", "TMP_MB", "TMP_OLDEST_AGE_H",
    "CHUNKS", "EMB_MB", "EC_ROWS", "EC_MB", "TMP_CHUNKS", "TMP_HAS_META",
    "PROVIDER_MODEL", "PROVIDER_KEY", "MEM_MB", "STATUS",
]


def f(s, default=0.0):
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def i(s, default=0):
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def classify(r):
    if r["STATUS"] != "OK":
        return "ERR", [r["STATUS"]]
    main_age = f(r["MAIN_AGE_H"])
    tmp_count = i(r["TMP_COUNT"])
    tmp_active = i(r["TMP_ACTIVE"])
    tmp_oldest = f(r["TMP_OLDEST_AGE_H"])
    restarts = i(r["RESTARTS"])
    pod_age_m = i(r["POD_AGE_M"])
    ws_ready = r["WS_READY"]
    last_oom = r["LAST_OOM"]

    reasons = []
    if tmp_count >= 1 and main_age >= 24:
        reasons.append(f"reindex 死循环: tmp 在 + main {main_age:.0f}h 未更新")
        return "CRITICAL", reasons
    if tmp_count >= 2:
        reasons.append(f"多个孤儿 tmp ({tmp_count} 个)")
        return "CRITICAL", reasons
    if restarts >= 5 and tmp_count >= 1:
        reasons.append(f"反复 OOM (restart={restarts}) + 孤儿 tmp")
        return "CRITICAL", reasons

    if tmp_count >= 1 and main_age >= 12:
        reasons.append(f"tmp 在 + main {main_age:.0f}h 未更新")
        return "HIGH", reasons
    if restarts >= 3 and pod_age_m < 120 and ws_ready != "YES":
        reasons.append(f"近期反复重启 ({restarts}) + ws 未 ready")
        return "HIGH", reasons
    if tmp_oldest >= 6 and tmp_count >= 1:
        reasons.append(f"孤儿 tmp 已 {tmp_oldest:.1f}h")
        return "HIGH", reasons
    if restarts >= 5 and last_oom != "-":
        reasons.append(f"高 restart ({restarts}) + 历史 OOM")
        return "HIGH", reasons

    if tmp_count >= 1 and tmp_active >= 1 and main_age < 12:
        reasons.append(f"reindex 进行中 (tmp {tmp_oldest:.1f}h, main {main_age:.0f}h)")
        return "MED", reasons
    if restarts >= 2 and main_age >= 6:
        reasons.append(f"近期重启 ({restarts}) + main {main_age:.0f}h 旧")
        return "MED", reasons
    if last_oom != "-":
        reasons.append(f"曾 OOM @ {last_oom}")
        return "MED", reasons

    return "OK", []


def md_table_row(r):
    reasons = "; ".join(r["_REASONS"])
    return ("| {HID} | {RESTARTS} | {POD_AGE_M}m | {WS_READY} | {MAIN_MB} | {MAIN_AGE_H} | "
            "{TMP_COUNT}/{TMP_ACTIVE} | {TMP_MB} | {TMP_OLDEST_AGE_H} | {CHUNKS} | "
            "{EMB_MB} | {EC_MB} | {MEM_MB} | {PROV} | {REASONS} |").format(
        **r, PROV=r["PROVIDER_MODEL"][:24], REASONS=reasons)


def main():
    tsv = sys.argv[1] if len(sys.argv) > 1 else "/tmp/her-rescue/deep-scan.tsv"
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/her-rescue/deep-scan-report.md"

    rows = []
    with open(tsv, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            while len(parts) < len(COLS):
                parts.append("-")
            r = dict(zip(COLS, parts[:len(COLS)]))
            level, reasons = classify(r)
            r["_LEVEL"] = level
            r["_REASONS"] = reasons
            rows.append(r)

    levels = {"CRITICAL": [], "HIGH": [], "MED": [], "OK": [], "ERR": []}
    for r in rows:
        levels[r["_LEVEL"]].append(r)

    now = subprocess.run(["date", "-u", "+%FT%TZ"], capture_output=True, text=True).stdout.strip()
    md = []
    md.append("# Her Reindex 死循环 / OOM 风险扫描报告")
    md.append("")
    md.append(f"扫描时间: `{now}`")
    md.append(f"总实例: **{len(rows)}**  |  "
              f"CRITICAL: **{len(levels['CRITICAL'])}**  |  "
              f"HIGH: **{len(levels['HIGH'])}**  |  "
              f"MED: **{len(levels['MED'])}**  |  "
              f"OK: **{len(levels['OK'])}**  |  "
              f"ERR: **{len(levels['ERR'])}**")
    md.append("")
    md.append("> 风险参考 her-68 诊断模式：tmp 孤儿 + main.sqlite 多天不更新 = reindex 死循环。")
    md.append("")

    header = ("| HER | RESTART | POD_AGE | WS | MAIN_MB | MAIN_H | TMP/ACT | TMP_MB | TMP_H | "
              "CHUNKS | EMB | EC | MEM | PROVIDER | 原因 |")
    sep = "|---|---:|---:|---|---:|---:|:---:|---:|---:|---:|---:|---:|---:|---|---|"

    for lvl in ["CRITICAL", "HIGH", "MED"]:
        items = levels[lvl]
        if not items:
            continue
        md.append(f"## {lvl}  ({len(items)} 个)")
        md.append("")
        md.append(header)
        md.append(sep)
        for r in sorted(items, key=lambda x: (-f(x["MAIN_AGE_H"]), -i(x["RESTARTS"]))):
            md.append(md_table_row(r))
        md.append("")

    if levels["ERR"]:
        md.append(f"## ERR  ({len(levels['ERR'])} 个)")
        md.append("")
        for r in levels["ERR"]:
            md.append(f"- carher-{r['HID']}: STATUS={r['STATUS']}")
        md.append("")

    md.append(f"## OK  ({len(levels['OK'])} 个)")
    md.append("")
    md.append(f"略（{len(levels['OK'])} 个健康实例，TMP_COUNT=0、main 主库新鲜、无近期 OOM）")
    md.append("")

    md.append("## 处理建议")
    md.append("")
    md.append("- **CRITICAL**: 必救 — `bash .cursor/skills/her-memory-reindex-rescue/scripts/run_full_rescue.sh <HID>` 单实例约 8 min")
    md.append("- **HIGH**: 强烈建议 — 同上，可一行批量 `run_full_rescue.sh 1 2 3 ...`")
    md.append("- **MED**: 观察 30-60 分钟，若不自愈或 RESTARTS 继续涨 → 升 HIGH")
    md.append("- **OK**: 不动")
    md.append("")

    crit_hid = [r["HID"] for r in levels["CRITICAL"]]
    high_hid = [r["HID"] for r in levels["HIGH"]]
    if crit_hid or high_hid:
        md.append("### 一键命令")
        md.append("")
        md.append("```bash")
        if crit_hid:
            md.append(f"# CRITICAL ({len(crit_hid)} 个)")
            md.append(f"bash .cursor/skills/her-memory-reindex-rescue/scripts/run_full_rescue.sh {' '.join(crit_hid)}")
        if high_hid:
            md.append(f"# HIGH ({len(high_hid)} 个)")
            md.append(f"bash .cursor/skills/her-memory-reindex-rescue/scripts/run_full_rescue.sh {' '.join(high_hid)}")
        md.append("```")

    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))

    print(f"报告 → {out}")
    print(f"  CRITICAL={len(levels['CRITICAL'])}  HIGH={len(levels['HIGH'])}  "
          f"MED={len(levels['MED'])}  OK={len(levels['OK'])}  ERR={len(levels['ERR'])}")
    if crit_hid:
        print(f"  CRITICAL ids: {' '.join(crit_hid)}")
    if high_hid:
        print(f"  HIGH ids: {' '.join(high_hid)}")


if __name__ == "__main__":
    main()
