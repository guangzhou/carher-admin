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

    # 用户只给了「API 异常 (req: 36ece9a2)」：反查上游原文 + 自动对签名
    python3 scripts/codex-deepseek-triage.py --rid 36ece9a2

    # 哪些 DeepSeek chat 组没有 Codex 的 -responses 改写（新建组后必跑）
    python3 scripts/codex-deepseek-triage.py --only groups

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
| 400 ``reasoning_content ... must be passed back``（aresponses）/ ``response_format ... unavailable`` | /responses 打到 mode:chat 组走 bridge | e291064（加 -responses 改写）|
| 400 ``reasoning_content ... must be passed back``（acompletion）| OpenAI 形客户端回放工具历史不带 reasoning_content | db186fd（chat 路径每条补占位）|

    # 全站近 24h 失败按「组 × 调用类型 × 签名」聚合 + 每个签名的受影响 key（SpendLogs，只读）
    python3 scripts/codex-deepseek-triage.py --only sigs --since 24h

    # 「大家都说 deepseek 不回」：先判是不是上游自己挂了（按小时 p50/p95，OWUI 单列）
    python3 scripts/codex-deepseek-triage.py --only health --since 8h

相关：``.claude/skills/codex-deepseek-tool-triage/SKILL.md``
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
    "chat_reasoning_content_fill": ("chat 工具轮补了占位 reasoning_content（正常，db186fd）", False),
    "chat_reasoning_content_restore": ("chat 从 provider_specific_fields 还原 reasoning_content（正常）", False),
}


def _ssh(script: str, timeout: int = 300) -> str:
    """把脚本 base64 内联送到 198 跑 —— 避免 scp 撞 /tmp 同名残留。走 ssh key + sudo -n，与 callback-update 同。"""
    b64 = base64.b64encode(script.encode()).decode()
    cmd = (
        "sudo -n bash -c "
        f"'echo {b64} | base64 -d > /tmp/_cdt_$$.sh; bash /tmp/_cdt_$$.sh; rm -f /tmp/_cdt_$$.sh'"
    )
    p = subprocess.run(
        ["ssh", "-n", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
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


# 上游原文片段 -> (人话根因, 修法)。用户只看得到「API 异常 (req: xxx)」，原文在 SpendLogs。
ERROR_SIGNATURES = [
    ("reasoning_content` in the thinking mode must be passed back",
     "带 tool_calls 的 assistant 没回传 reasoning_content。call_type=acompletion ⇒ VS Code/Cursor 等"
     "OpenAI 形客户端回放历史本来就不带（db186fd 起网关每条补占位）；aresponses ⇒ /responses 进了 chat 组",
     "acompletion：日志应有 chat_reasoning_content_fill，没有 ⇒ 部署 api_base 不是 api.deepseek.com"
     "或 callback 没上；aresponses：查 _RESPONSES_GROUP_REWRITE（groups 节）"),
    ("response_format type is unavailable",
     "text.format 经 bridge 译成 chat response_format，DeepSeek 不收",
     "同上：responses 调用要走原生 -responses 组"),
    ("No tool output found",
     "并行工具调用块被插入项劈开", "见 feedback_deepseek_pairs_tool_calls_by_adjacency"),
    ("Input items array must not be empty",
     "转出去的 input 是空的。进网关 body 大 ⇒ 我们 _trim_oversize_input 删光的（10-22：view_image 截图"
     "几 MB 但 token ~1k，按字节被当超限）；body 小才是客户端",
     "litellm-198-key-error-triage.py rid <id> 看 body 大小 + 前一轮工具；10-25 ea4da5c 起图片不计字节，"
     "再出现=新形状，见 SKILL 根因 6"),
    ("unknown variant",
     "tool_choice / tool type 形状 DeepSeek 不认", "见 test_deepseek_tool_choice_shapes.py 形状表"),
    ("Unsupported custom tool",
     "custom tool 名不是 apply_patch", "deepseek_responses_adapt 的 custom→function 降级"),
]


def section_rid(rid: str) -> None:
    """用户报错里的 `req: xxxxxxxx` = request_id 前 8 位（error_sanitize）。"""
    print(f"\n=== req {rid} 原文（SpendLogs）===")
    if not re.fullmatch(r"[0-9a-f-]{6,36}", rid):
        print("  ⛔ rid 只能是十六进制前缀")
        return
    # request_id 是主键，但 like 前缀 + 时间窗约 13s，不加时间窗会全表扫
    sql = f"""
SET statement_timeout='120s';
SELECT s.\\"request_id\\", s.\\"startTime\\", COALESCE(k.\\"key_alias\\",'?') who,
       s.\\"model_group\\", s.\\"model_id\\", s.\\"call_type\\", s.\\"status\\",
       LEFT(s.\\"metadata\\"->'error_information'->>'error_message', 400) err
FROM \\"LiteLLM_SpendLogs\\" s
LEFT JOIN \\"LiteLLM_VerificationToken\\" k ON k.\\"token\\"=s.\\"api_key\\"
WHERE s.\\"request_id\\" LIKE '{rid}%' AND s.\\"startTime\\" > NOW() - INTERVAL '7 days';
"""
    out = _ssh(f'kubectl -n {NS} exec litellm-db-0 -- psql -U litellm -d litellm -x -c "{sql}"')
    print(out.rstrip() or "  （查不到：超过 7 天，或 SpendLogs 没落这条）")
    if "ERROR:" in out:
        print("  ⛔ psql 报错 —— 不是「查无此条」")
    for sig, why, fix in ERROR_SIGNATURES:
        if sig in out:
            print(f"\n  命中签名：{sig}\n  根因：{why}\n  修法：{fix}")


def _psql(sql: str, flags: str = "-At -F ' | '") -> str:
    """SQL 走 base64 进 pod 再 psql -f，避开三层 shell 引号（2026-10-14 手写转义连错两次）。"""
    b64 = base64.b64encode(sql.encode()).decode()
    return _ssh(f"echo {b64} | base64 -d | kubectl -n {NS} exec -i litellm-db-0 -- "
                f"psql -U litellm -d litellm {flags} -f -", timeout=300)


def section_sigs(since_min: int) -> None:
    """全站 deepseek 组失败按 组×call_type×签名 聚合，再列每个签名的受影响 key。"""
    print(f"\n=== deepseek 失败签名（SpendLogs 近 {since_min} 分钟）===")
    win = f"\"startTime\" > now() - interval '{since_min} minutes'"
    sql = f"""
set statement_timeout='120s';
select 'MIN_START', min("startTime")::text, '', '' from "LiteLLM_SpendLogs";
select model_group, call_type,
       left(regexp_replace(coalesce(metadata->'error_information'->>'error_message','?'),
            '\\(request_id[^)]*\\)|No fallback model group.*$','','g'),130), count(*)
from "LiteLLM_SpendLogs"
where {win} and status='failure' and model_group ilike '%deepseek%'
group by 1,2,3 order by 4 desc limit 20;
select 'KEY', coalesce(v.key_alias,'?'), left(s.metadata->'error_information'->>'error_message',60), count(*)
from "LiteLLM_SpendLogs" s left join "LiteLLM_VerificationToken" v on v.token=s.api_key
where s.{win} and s.status='failure' and s.model_group ilike '%deepseek%'
  and s.metadata->'error_information'->>'error_message' like '%must be passed back%'
group by 2,3 order by 4 desc limit 20;
"""
    out = _psql(sql)
    if not out.strip() or "ERROR:" in out:
        print("  ⛔ 查询失败或空返回 —— 量具坏了，不当「零失败」\n" + out)
        return
    for line in out.splitlines():
        if line.startswith("MIN_START"):
            print(f"  SpendLogs 最早一行 = {line.split(' | ')[1]}（早于它的「零失败」是表被清空，不是没出事）")
        elif line.startswith("KEY"):
            print("  key  " + line[6:])
        else:
            print("  " + line)


def section_health(since_min: int) -> None:
    """按小时看 deepseek 各组 成功/失败/p50/p95，OWUI(master key) 单列。判「是上游自己挂了」用这个。"""
    print(f"\n=== deepseek 上游健康（SpendLogs 近 {since_min} 分钟，按小时）===")
    win = f"\"startTime\" > now() - interval '{since_min} minutes'"
    sql = f"""
set statement_timeout='120s';
select to_char(date_trunc('hour',"startTime"),'MM-DD HH24')||'Z',
       case when api_key like 'litellm_pr%' then 'owui' else 'keys' end,
       model_group, status, count(*),
       round(percentile_cont(0.5) within group (order by extract(epoch from ("endTime"-"startTime")))::numeric,1),
       round(percentile_cont(0.95) within group (order by extract(epoch from ("endTime"-"startTime")))::numeric,1)
from "LiteLLM_SpendLogs" where {win} and model_group ilike '%deepseek-v4.1%'
  and coalesce(metadata->>'user_api_key_alias','') not like 'tmp-%'
group by 1,2,3,4 order by 1,3,2,4;
"""
    out = _psql(sql)
    if not out.strip() or "ERROR:" in out:
        print("  ⛔ 查询失败或空返回 —— 量具坏了\n" + out)
        return
    print("  小时(UTC) | 来源 | 组 | status | n | p50s | p95s")
    for line in out.splitlines():
        if line.strip() and line != "SET":
            print("  " + line)
    print("  提示：p50 从个位数跳到几百秒 + 900-second / Service is too busy = 上游自身故障，"
          "先对 status.deepseek.com，别先怀疑网关补丁")


def _rewrite_table() -> dict:
    """从本地 callback 源码读 _RESPONSES_GROUP_REWRITE（与线上同版时才准，先跑 callbacks-drift）。"""
    import ast, os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "k8s", "litellm-callbacks", "deepseek_responses_adapt.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "_RESPONSES_GROUP_REWRITE" for t in node.targets):
            return ast.literal_eval(node.value)
    return {}


def section_groups() -> None:
    """DeepSeek 的 mode:chat 组若没有 -responses 改写，Codex 打它必走 bridge ⇒ 工具历史/结构化输出 400。"""
    print("\n=== DeepSeek 组 × Codex 改写覆盖 ===")
    out = _ssh(f"""
MK=$(kubectl -n {NS} get secret litellm-secrets -o jsonpath={{.data.LITELLM_MASTER_KEY}} | base64 -d)
P=$(kubectl -n {NS} get pods -l carher.net/litellm-production-route=enabled -o jsonpath='{{.items[0].status.podIP}}')
curl -s -m 60 http://$P:4000/model/info -H "Authorization: Bearer $MK" | python3 -c '
import json,sys
seen=set()
for m in json.load(sys.stdin)["data"]:
  n=m["model_name"]; mi=m.get("model_info") or {{}}
  if "deepseek" in n.lower() and (n,mi.get("id")) not in seen:
    seen.add((n,mi.get("id"))); print("%s\\t%s\\t%s" % (n, mi.get("mode"), (m.get("litellm_params") or {{}}).get("api_base")))
'
""")
    rows = [l.split("\t") for l in out.splitlines() if l.count("\t") == 2]
    if not rows:
        print("  ⛔ /model/info 没读到 —— 量具坏了，不当绿")
        return
    names = {r[0] for r in rows}
    table = _rewrite_table()
    print(f"  改写表（本地源码）：{table}")
    for n, mode, base in sorted(rows):
        if mode != "chat" or "api.deepseek.com" not in str(base):
            continue
        tgt = table.get(n)
        if tgt and tgt in names:
            print(f"  ✓ {n:34} → {tgt}")
        elif tgt:
            print(f"  ⛔ {n:34} → {tgt}（目标组不存在！改写后必 400）")
        else:
            print(f"  ⚠ {n:34} 无改写 —— Codex 打它走 bridge（是否有 key 只剩它可选？）")
    for src, tgt in table.items():
        if src not in names:
            print(f"  · 改写源 {src} 不在线上组里（死映射，无害）")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="30m", help="日志窗口，如 30m / 2h（默认 30m）")
    ap.add_argument("--key", help="只看某个 key_alias")
    ap.add_argument("--rid", help="用户报错里的 req id（API 异常 (req: xxxx)），反查原文 + 签名")
    ap.add_argument("--only", choices=["counts", "errors", "shape", "spend", "groups", "sigs", "health"],
                    help="只跑某一节")
    a = ap.parse_args()

    if a.rid:
        section_rid(a.rid)
        return 0
    if a.only == "groups":
        section_groups()
        return 0

    m = re.match(r"(\d+)([mh])$", a.since)
    since_min = int(m.group(1)) * (60 if m.group(2) == "h" else 1) if m else 30

    print(f"Codex × DeepSeek 体检  window={a.since}" + (f"  key={a.key}" if a.key else ""))

    if a.only == "spend":
        section_spend(a.key, since_min)
        return 0
    if a.only == "sigs":
        section_sigs(since_min)
        return 0
    if a.only == "health":
        section_health(since_min)
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
        section_groups()
    return 0


if __name__ == "__main__":
    sys.exit(main())
