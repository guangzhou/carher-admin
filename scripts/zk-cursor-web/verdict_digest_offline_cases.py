#!/usr/bin/env python3
"""verdict_digest_offline_cases.py — ROI #3 聚合器离线单测。

关键纪律:合成日志行**逐字复刻活字节 d050ac3a 的 console.log 输出文法**(见
/tmp/resp_live_82.js L669/L1255/L1334/L1323/L1349/L1359/L369/L487/L407),这样测的是
「聚合器能否解析上线那份代码真打出来的行」,不是我自造的格式。

覆盖:①五态 verdict 各归其桶 + verdict_total ②ack_rate 由 ok/no-ack 算对
③act_retry_rate/violation_rate/complete_rate 派生对 ④violation raw 样本被收(含 kind)
⑤conv saved/delta_send/persist_loaded(loaded 累加 N)⑥噪音行/空行不误计
⑦dialect-translated 的 complete-run 仍归 complete_run ⑧--raw-max 截断样本数。

用法: python3 verdict_digest_offline_cases.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verdict_digest as vd  # noqa: E402

_pass = 0
_fail = 0
_fails = []


def check(name, cond, why=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail += 1
        _fails.append({"name": name, "why": why})
        print(f"[FAIL] {name} — {why}")


# —— 逐字复刻的样本行(前缀带真实时间戳/pod 噪音,验证 .search 而非 .match)——
LINES = [
    "2026-08-27T12:00:01Z [turn-verdict-v2] complete-run (shell, cmd 12 chars, prose 40 chars)",
    "[turn-verdict-v2] complete-run (dialect-translated ls,glob, prose 18 chars)",
    "[turn-verdict-v2] complete-prose (self-answer unwrapped 220 chars)",
    "[turn-verdict-v2] complete-prose (ask-translated 33 chars)",
    "[turn-verdict-v2] complete-prose (120 chars, bracket residue stripped)",
    "[turn-verdict-v2] announce-without-action (55 chars) -> forced action retry",
    '[turn-verdict-v2] violation (empty/undeliverable) -> same-conv resend (budget 1) raw="\\n\\n"',
    '[turn-verdict-v2] violation (still empty after resend) -> honest error raw=""',
    "[turn-verdict-v2] complete-run but no shellTool -> deliver prose",
    "[turn-verdict-v2] self-answer SUPPRESSED (redirect/pipe) -> execute: printf 'x' > f",
    "[handshake] ack ok try=1 conv=6a8fa9ed",
    "[handshake] ack ok try=2 conv=deadbeef",
    '[handshake] no-ack try=1 head="hello there"',
    "[conv] saved items=8 conv=6a8fa9ed",
    "[conv] delta send 1 new items, 4200 chars (conv=6a8fa9ed)",
    "[conv] delta send 2 new items, 5000 chars (conv=deadbeef)",
    "[conv-persist] loaded 4 skipped 0 from /app/convcache/conv-cache.json",
    "[conv-persist] loaded 3 skipped 1 from /app/convcache/conv-cache.json",
    "some unrelated pod noise line",
    "",
    "[bpi] GET /f/conversation 200",
]

acc = vd.aggregate(LINES)
s = vd.summarize(acc)

# —— ① 五态归桶 + total ——
v = s["verdict"]
check("complete_run=2", v["complete_run"] == 2, f'got {v["complete_run"]}')
check("complete_prose=3", v["complete_prose"] == 3, f'got {v["complete_prose"]}')
check("announce_retry=1", v["announce_retry"] == 1, f'got {v["announce_retry"]}')
check("violation_resend=1", v["violation_resend"] == 1, f'got {v["violation_resend"]}')
check("violation_honest=1", v["violation_honest"] == 1, f'got {v["violation_honest"]}')
check("self_answer_suppressed=1", v["self_answer_suppressed"] == 1, f'got {v["self_answer_suppressed"]}')
check("complete_run_no_shell=1", v["complete_run_no_shell"] == 1, f'got {v["complete_run_no_shell"]}')
check("other=0 (no misclassify)", v["other"] == 0, f'got {v["other"]}')
check("verdict_total=10", s["verdict_total"] == 10, f'got {s["verdict_total"]}')

# —— ② ack_rate(会话口径 vs attempt 口径)——
#    样本:2 行 ack ok(2 个成功会话)+ 1 行 no-ack try=1(非末次 → 不计降级会话)。
#    会话口径:ack_rate = 2/(2+0)=1.0(no-ack try=1 是"先败后成"会话的中间步,不算失败会话)。
#    attempt 口径:ack_rate_attempt = 2/(2+1)=0.6667(诊断对照用)。
check("ack_ok=2", s["handshake"]["ack_ok"] == 2, f'got {s["handshake"]["ack_ok"]}')
check("no_ack=1", s["handshake"]["no_ack"] == 1, f'got {s["handshake"]["no_ack"]}')
check("no_ack_final=0", s["handshake"]["no_ack_final"] == 0, f'got {s["handshake"]["no_ack_final"]}')
check("ack_rate(session)=1.0", abs(s["handshake"]["ack_rate"] - 1.0) < 1e-9, f'got {s["handshake"]["ack_rate"]}')
check("ack_rate_attempt=0.6667", abs(s["handshake"]["ack_rate_attempt"] - 0.6667) < 1e-4,
      f'got {s["handshake"]["ack_rate_attempt"]}')

# —— ②bis 核心 bug 复现:先败后成会话不该被记成失败 ——
#    一个会话 try=1 no-ack → try=2 ack ok = **成功会话**;旧 attempt 口径把它算 50%(假 ALARM)。
BUG = [
    '[handshake] no-ack try=1 head="garbage"',
    "[handshake] ack ok try=2 conv=aaaa1111",
]
sb = vd.summarize(vd.aggregate(BUG))
check("先败后成: ack_rate(session)=1.0 不假警", abs(sb["handshake"]["ack_rate"] - 1.0) < 1e-9,
      f'session-based must be 1.0 (one successful session), got {sb["handshake"]["ack_rate"]}')
check("先败后成: no_ack_final=0", sb["handshake"]["no_ack_final"] == 0, f'got {sb["handshake"]["no_ack_final"]}')
check("先败后成: attempt 口径确记 0.5(对照证明旧口径会误判)",
      abs(sb["handshake"]["ack_rate_attempt"] - 0.5) < 1e-9, f'got {sb["handshake"]["ack_rate_attempt"]}')

# —— ②ter 真降级会话(两次都失败,末次 try=2)才计入分母 ——
DEG = [
    '[handshake] no-ack try=1 head="x"',
    '[handshake] no-ack try=2 head="y"',
    "[handshake] ack ok try=1 conv=bbbb2222",
]
sd = vd.summarize(vd.aggregate(DEG))
check("降级会话: no_ack_final=1", sd["handshake"]["no_ack_final"] == 1, f'got {sd["handshake"]["no_ack_final"]}')
check("降级会话: ack_rate(session)=0.5 (1成功/1降级)", abs(sd["handshake"]["ack_rate"] - 0.5) < 1e-9,
      f'got {sd["handshake"]["ack_rate"]}')

# —— ③ 派生率 ——
r = s["rates"]
check("act_retry_rate=0.1", abs(r["act_retry_rate"] - 0.1) < 1e-9, f'got {r["act_retry_rate"]}')
check("violation_rate=0.2", abs(r["violation_rate"] - 0.2) < 1e-9, f'got {r["violation_rate"]}')
check("complete_rate=0.5", abs(r["complete_rate"] - 0.5) < 1e-9, f'got {r["complete_rate"]}')

# —— ④ violation raw 样本(含 kind)——
vs = s["violation_samples"]
check("2 violation samples", len(vs) == 2, f'got {len(vs)}')
kinds = {x["kind"] for x in vs}
check("sample kinds cover both", kinds == {"violation_resend", "violation_honest"}, f'got {kinds}')
check("resend raw captured", any(x["kind"] == "violation_resend" and "\\n" in x["raw"] for x in vs), f'{vs}')

# —— ⑤ conv 三事件 ——
c = s["conv"]
check("conv.saved=1", c["saved"] == 1, f'got {c["saved"]}')
check("conv.delta_send=2", c["delta_send"] == 2, f'got {c["delta_send"]}')
check("conv.persist_loaded=7 (4+3 累加)", c["persist_loaded"] == 7, f'got {c["persist_loaded"]}')

# —— ⑥ 噪音/空行不误计:total 已 =10,已隐式验;显式再验无关行不进任何桶 ——
acc2 = vd.aggregate(["random", "", "[bpi] 200", "[conv-persist] load miss (ENOENT) — cold start"])
s2 = vd.summarize(acc2)
check("noise-only zero", s2["verdict_total"] == 0 and s2["conv"]["persist_loaded"] == 0
      and s2["handshake"]["ack_ok"] == 0, f'{s2["verdict_total"]}/{s2["conv"]["persist_loaded"]}')

# —— ⑦ dialect-translated 归 complete_run(不另立桶)——
acc3 = vd.aggregate(["[turn-verdict-v2] complete-run (dialect-translated read, prose 5 chars)"])
check("dialect->complete_run", vd.summarize(acc3)["verdict"]["complete_run"] == 1,
      f'{vd.summarize(acc3)["verdict"]}')

# —— ⑧ --raw-max 截断 ——
many = ['[turn-verdict-v2] violation (still empty after resend) -> honest error raw="x"'] * 9
acc4 = vd.aggregate(many, raw_max=3)
check("raw-max truncates", len(vd.summarize(acc4)["violation_samples"]) == 3,
      f'{len(vd.summarize(acc4)["violation_samples"])}')

# —— 空输入不崩,rate=None ——
s5 = vd.summarize(vd.aggregate([]))
check("empty-safe", s5["verdict_total"] == 0 and s5["rates"]["act_retry_rate"] is None
      and s5["handshake"]["ack_rate"] is None, f'{s5["rates"]}')

# —— ⑨ 08-29 ISSUE-1:dialect-translated 按 kind 分桶(write/replace 首次出现即证修法生效) ——
acc6 = vd.aggregate([
    "[turn-verdict-v2] complete-run (dialect-translated write, prose 0 chars)",
    "[turn-verdict-v2] complete-run (dialect-translated read,replace, prose 12 chars)",
    "[turn-verdict-v2] complete-run (Shell, cmd 40 chars, prose 5 chars)",  # 非方言不入桶
])
s6 = vd.summarize(acc6)
check("dialect_kinds counts by kind",
      s6["dialect_kinds"] == {"write": 1, "read": 1, "replace": 1},
      f'{s6["dialect_kinds"]}')

# —— ⑩ violation raw 首标记方言分类(修法覆盖内的写族记录到 hist) ——
acc7 = vd.aggregate([
    '[turn-verdict-v2] violation (empty/undeliverable) -> same-conv resend (budget 1) raw="⟦write¦path=/tmp/a¦content=hi⟧"',
    '[turn-verdict-v2] violation (empty/undeliverable) -> same-conv resend (budget 1) raw="⟦replace¦path=/tmp/b¦old=x¦new=y⟧"',
])
s7 = vd.summarize(acc7)
check("violation_dialect_hist counts",
      s7["violation_dialect_hist"] == {"write": 1, "replace": 1},
      f'{s7["violation_dialect_hist"]}')
check("known dialects no alarm",
      s7["unknown_dialect_alarm"] == [],
      f'{s7["unknown_dialect_alarm"]}')

# —— ⑪ 未知方言告警(soak 期发现新变体的信号:如 ⟦edit⟧/⟦patch⟧/⟦append⟧) ——
acc8 = vd.aggregate([
    '[turn-verdict-v2] violation (empty/undeliverable) -> same-conv resend (budget 1) raw="⟦edit¦path=/tmp/x⟧"',
    '[turn-verdict-v2] violation (still empty after resend) -> honest error raw="⟦append¦path=/tmp/y¦content=hi⟧"',
])
s8 = vd.summarize(acc8)
check("unknown_dialect_alarm fires",
      s8["unknown_dialect_alarm"] == ["append", "edit"],
      f'{s8["unknown_dialect_alarm"]}')

print(f"\n== {_pass}/{_pass + _fail} PASS ==")
if _fail:
    print("FAILS:", json.dumps(_fails, ensure_ascii=False, indent=2))
    sys.exit(1)
print("VERDICT: GO (五态归桶/ack率/三派生率/violation样本/conv三事件/噪音不误计/方言归并/raw截断/空安全/dialect_kinds/violation_dialect_hist/unknown_alarm)")
