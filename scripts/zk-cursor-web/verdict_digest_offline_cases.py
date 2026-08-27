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

# —— ② ack_rate ——
check("ack_ok=2", s["handshake"]["ack_ok"] == 2, f'got {s["handshake"]["ack_ok"]}')
check("no_ack=1", s["handshake"]["no_ack"] == 1, f'got {s["handshake"]["no_ack"]}')
check("ack_rate=0.6667", abs(s["handshake"]["ack_rate"] - 0.6667) < 1e-4, f'got {s["handshake"]["ack_rate"]}')

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

print(f"\n== {_pass}/{_pass + _fail} PASS ==")
if _fail:
    print("FAILS:", json.dumps(_fails, ensure_ascii=False, indent=2))
    sys.exit(1)
print("VERDICT: GO (五态归桶/ack率/三派生率/violation样本/conv三事件/噪音不误计/方言归并/raw截断/空安全)")
