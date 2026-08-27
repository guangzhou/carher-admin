#!/usr/bin/env python3
"""digest_notify_offline_cases.py — ROI #3 摘要格式化 + 判据裁读离线单测。

只测纯函数 verdict_level / format_card(发送函数照抄参考、且默认 dry-run 不外推,不入网测)。
覆盖:①全绿→OK ②violation_rate>0→ALARM ③ack_rate<0.95→ALARM ④act_retry>=0.05→WARN
⑤ALARM 压过 WARN ⑥空 ack(无握手样本)不误判 ALARM ⑦format_card 含关键字段与 violation 样本
⑧--apply 未开时 send 走 dry-run 分支(不入网)。

用法: python3 digest_notify_offline_cases.py
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import digest_notify as dn  # noqa: E402

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


def S(vr=0.0, ar=0.0, ack=1.0, total=10, samples=None):
    return {
        "verdict_total": total,
        "verdict": {"complete_run": 5, "complete_prose": 4, "announce_retry": 1,
                    "violation_resend": 0, "violation_honest": 0,
                    "self_answer_suppressed": 0, "complete_run_no_shell": 0, "other": 0},
        "handshake": {"ack_ok": 9, "no_ack": 1, "no_ack_final": 1, "ack_rate": ack,
                      "ack_rate_attempt": ack},
        "conv": {"saved": 3, "delta_send": 2, "persist_loaded": 4},
        "rates": {"act_retry_rate": ar, "violation_rate": vr, "complete_rate": 0.9},
        "violation_samples": samples or [],
    }


# —— ① 全绿 OK ——
lvl, _ = dn.verdict_level(S(vr=0.0, ar=0.0, ack=1.0))
check("all-green OK", lvl == "OK", lvl)

# —— ② violation>0 ALARM ——
lvl, r = dn.verdict_level(S(vr=0.05))
check("violation ALARM", lvl == "ALARM" and any("violation" in x for x in r), f"{lvl} {r}")

# —— ③ ack<0.95 ALARM ——
lvl, r = dn.verdict_level(S(ack=0.9))
check("low-ack ALARM", lvl == "ALARM" and any("ack_rate" in x for x in r), f"{lvl} {r}")

# —— ④ act_retry>=0.05 WARN ——
lvl, r = dn.verdict_level(S(ar=0.06))
check("act-retry WARN", lvl == "WARN" and any("act_retry" in x for x in r), f"{lvl} {r}")

# —— ⑤ ALARM 压过 WARN(同时越两线)——
lvl, _ = dn.verdict_level(S(vr=0.1, ar=0.2))
check("ALARM over WARN", lvl == "ALARM", lvl)

# —— ⑥ 空 ack(ack_rate=None)不误判 ALARM ——
s = S()
s["handshake"] = {"ack_ok": 0, "no_ack": 0, "ack_rate": None}
lvl, _ = dn.verdict_level(s)
check("none-ack not ALARM", lvl == "OK", lvl)

# —— ④b act_retry 边界 0.05 恰好 WARN ——
lvl, _ = dn.verdict_level(S(ar=0.05))
check("act-retry boundary 0.05 WARN", lvl == "WARN", lvl)

# —— ⑦ format_card 关键字段 + violation 样本 ——
card = dn.format_card(S(vr=0.05, samples=[{"kind": "violation_honest", "raw": '""'}]), lane="82", window="24h")
check("card has lane/window", "cursor-g 82 soak · 24h" in card, card[:60])
check("card ALARM line", card.startswith("[cursor-g 82 soak · 24h] ALARM"), card.split(chr(10))[0])
check("card has handshake", "handshake ack=9/10会话" in card, "no handshake line")
check("card has conv reuse", "delta_send(复用)=2" in card, "no conv line")
check("card shows violation sample", "[violation_honest]" in card, "sample missing")

# —— ⑧ send 默认 dry-run 不入网 ——
buf = io.StringIO()
with redirect_stdout(buf):
    dn.send_feishu("hello", apply=False)
out = buf.getvalue()
check("send dry-run prints, no network", "DRY-RUN" in out and "hello" in out, out[:60])

# —— ⑧b --apply 但 webhook 未设 → stub 守卫跳过(不入网)——
_orig = dn.FEISHU_WEBHOOK
dn.FEISHU_WEBHOOK = ""
buf2 = io.StringIO()
with redirect_stdout(buf2):
    dn.send_feishu("hi", apply=True)
dn.FEISHU_WEBHOOK = _orig
check("apply+no-webhook skips", "未设置" in buf2.getvalue(), buf2.getvalue()[:60])

print(f"\n== {_pass}/{_pass + _fail} PASS ==")
if _fail:
    print("FAILS:", json.dumps(_fails, ensure_ascii=False, indent=2))
    sys.exit(1)
print("VERDICT: GO (OK/ALARM×2/WARN/优先级/空ack/边界/卡片字段/dry-run不入网/stub守卫)")
