#!/usr/bin/env python3
"""codex-deepseek-triage.py — Codex × DeepSeek 静默故障一键体检。

为什么有这个脚本
================
2026-08-06 排查 Codex 打 deepseek 的三类故障（工具调用泄漏成裸文本 /
工具执行不了 / compaction 报 Fatal）时，同一批查询手搓了十几遍。
这些故障**上游全程 200、零错误**，只看状态码永远发现不了，判据全在
callback 的 counts 和请求形状里。

它只读不写，安全。

用法
====
    # 全量体检（默认看最近 30 分钟）
    python3 scripts/codex-deepseek-triage.py

    # 指定窗口 / 只看某个 key
    python3 scripts/codex-deepseek-triage.py --since 2h
    python3 scripts/codex-deepseek-triage.py --key cursor-liyun1-t6xm

    # 只跑某一节
    python3 scripts/codex-deepseek-triage.py --only counts

判据速查（脚本会自动标注）
==========================
| 现象 | 根因 | 已修复于 |
|---|---|---|
| ``tools=[]`` + ``seq0=additional_tools`` | Codex responses-lite 把工具塞 input 里 | 2b14561 |
| 输出 ``function_call`` 而客户端声明 custom | DeepSeek 只收 apply_patch 一个 custom | 090a1d7 |
| ``compaction`` item 数 != 1 | DeepSeek 忽略 compaction_trigger | f6a038f |
| ``counts={}`` 紧跟 400 | 网关没动手，是**客户端载荷**问题 | — |
| ``reasoning_insert: N`` 紧跟 400 | 网关**自己插的**，见并行块配对 | 0ea2d71/b72e4ea |
| ``completion_tokens`` 卡在某个整数反复重试 | 客户端 max_output_tokens 太小被截断 | 客户端侧 |

相关：``~/.claude/skills/codex-deepseek-tool-triage/SKILL.md``
"""
from __future__ import annotations

import argparse
import base64
import re
import subprocess
import sys
from collections import Counter

HOST = "10.68.13.198"
USER = "cltx"
PW = "Hn8#mKLp3QxZ"
NS = "litellm-product"

# counts 键 -> (人话, 是否代表异常)
COUNT_MEANING = {
    "additional_tools_hoisted": ("Codex 工具已提到顶层（正常，2b14561）", False),
    "custom_call_restored": ("custom tool 已还原（正常，090a1d7）", False),
    "arg_event_renamed": ("参数事件已换名（正常，090a1d7）", False),
    "custom_to_function": ("custom 降级成 function（正常，DeepSeek 硬约束）", False),
    "compaction_trigger_rewritten": ("compaction 请求已改写（正常，f6a038f）", False),
    "compaction_wrapped": ("摘要已包装成 compaction item（正常，f6a038f）", False),
    "reasoning_insert": ("补了占位 reasoning —— 若紧跟 400，是网关自己插的", True),
    "message_between_calls_drop": ("删了并行块中间的 message（正常，b72e4ea）", False),
    "reasoning_between_calls_drop": ("删了并行块中间的 reasoning（正常）", False),
    "tool_choice_unwrapped": ("tool_choice 对象拆成裸串（正常，8d891d0）", False),
    "tool_choice_force_dropped": ("强制类 tool_choice 已删（正常）", False),
}


def _ssh(script: str, timeout: int = 300) -> str:
    """把脚本 base64 内联送到 198 跑 —— 避免 scp 撞 /tmp 同名残留。"""
    b64 = base64.b64encode(script.encode()).decode()
    cmd = (
        f"echo '{PW}' | sudo -S bash -c "
        f"'echo {b64} | base64 -d > /tmp/_cdt_$$.sh; bash /tmp/_cdt_$$.sh; rm -f /tmp/_cdt_$$.sh'"
    )
    p = subprocess.run(
        ["sshpass", "-p", PW, "ssh", "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=25", f"{USER}@{HOST}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return p.stdout


def _logs(since: str) -> str:
    return _ssh(f"""
for p in $(kubectl -n {NS} get pods -l app=litellm-proxy -o jsonpath='{{.items[*].metadata.name}}'); do
  kubectl -n {NS} logs $p --since={since} 2>/dev/null
done
""", timeout=420)


def section_counts(log: str) -> None:
    print("\n=== callback counts（网关到底做了什么）===")
    hits = Counter(re.findall(r"counts=\{([^}]*)\}", log))
    if not hits:
        print("  （无 deepseek 流量）")
        return
    agg: Counter = Counter()
    for body, n in hits.items():
        for k in re.findall(r"'(\w+)':", body):
            agg[k] += n
    if not agg:
        print(f"  全部是 no-op（{sum(hits.values())} 次）—— 网关未改写任何请求")
        return
    for k, n in agg.most_common():
        desc, bad = COUNT_MEANING.get(k, ("未知计数器", False))
        print(f"  {'⚠' if bad else ' '} {k:32} {n:6}  {desc}")


def section_errors(log: str) -> None:
    print("\n=== 上游错误（不预设关键词）===")
    msgs = Counter(m[:110] for m in re.findall(r'"message":"([^"]*)"', log))
    ds = [(m, n) for m, n in msgs.items() if "deepseek" in m.lower() or "tool" in m.lower()]
    if not ds:
        print("  ✓ 零 deepseek 相关上游错误")
    for m, n in sorted(ds, key=lambda x: -x[1])[:8]:
        print(f"  {n:5}  {m}")

    # 静默故障专项
    print("\n=== 静默故障专项（200 但结果不对）===")
    checks = [
        ("DSML 裸文本泄漏", r"DSML|invoke name="),
        ("No tool output found", r"No tool output found"),
        ("compaction 相关", r"compaction"),
    ]
    for name, pat in checks:
        n = len(re.findall(pat, log))
        print(f"  {'⚠' if n else '✓'} {name:24} {n}")


def section_shape(log: str) -> None:
    print("\n=== 请求形状（需线上挂 DSPROBE2 探针，默认无）===")
    rows = re.findall(r"DSPROBE2 .*?tool_choice=(\S+) tools=(\[[^\]]*\]) n_in=(-?\d+)", log)
    if not rows:
        print("  （未挂探针 —— 见 SKILL.md「排查 SOP」第 3 步）")
        return
    for tc, tools, n in rows[-8:]:
        flag = "  ⚠ tools 为空 → 模型会把工具调用写成文本" if tools == "[]" else ""
        print(f"  tool_choice={tc:8} n_in={n:>5} tools={tools[:60]}{flag}")


def section_spend(key_alias: str | None, since_min: int) -> None:
    print(f"\n=== SpendLogs 近 {since_min} 分钟（谁在打 deepseek）===")
    where = "s.\\\"model\\\" ILIKE '%deepseek%'"
    if key_alias:
        where += f" AND k.\\\"key_alias\\\"='{key_alias}'"
    sql = f"""
SELECT COALESCE(k.\\"key_alias\\",'?') who, COUNT(*) n,
       MAX(s.\\"completion_tokens\\") max_out,
       COUNT(DISTINCT s.\\"completion_tokens\\") distinct_out
FROM \\"LiteLLM_SpendLogs\\" s
LEFT JOIN \\"LiteLLM_VerificationToken\\" k ON k.\\"token\\"=s.\\"api_key\\"
WHERE s.\\"startTime\\" > NOW() - INTERVAL '{since_min} minutes' AND {where}
GROUP BY 1 ORDER BY n DESC LIMIT 10;
"""
    out = _ssh(f'kubectl -n {NS} exec litellm-db-0 -- psql -U litellm -d litellm -c "{sql}"')
    print(out.rstrip() or "  （无数据）")
    print("  提示：max_out 反复等于同一个整数（如 4096）= 客户端 max_output_tokens 撑爆")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="30m", help="日志窗口，如 30m / 2h（默认 30m）")
    ap.add_argument("--key", help="只看某个 key_alias")
    ap.add_argument("--only", choices=["counts", "errors", "shape", "spend"],
                    help="只跑某一节")
    a = ap.parse_args()

    m = re.match(r"(\d+)([mh])$", a.since)
    since_min = int(m.group(1)) * (60 if m.group(2) == "h" else 1) if m else 30

    print(f"Codex × DeepSeek 体检  window={a.since}" + (f"  key={a.key}" if a.key else ""))

    if a.only == "spend":
        section_spend(a.key, since_min)
        return 0

    log = _logs(a.since)
    print(f"日志行数：{len(log.splitlines())}")

    if a.only in (None, "counts"):
        section_counts(log)
    if a.only in (None, "errors"):
        section_errors(log)
    if a.only in (None, "shape"):
        section_shape(log)
    if a.only is None:
        section_spend(a.key, since_min)
    return 0


if __name__ == "__main__":
    sys.exit(main())
