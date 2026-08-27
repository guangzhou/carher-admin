#!/usr/bin/env python3
"""verdict_digest.py — ROI #3 ack/verdict 聚合器(离线纯解析,不外推)。

把 82 pod 日志(kubectl logs -l ... --tail=-1 的原文)按字面文法聚合成一份 JSON 摘要,
供每日推送与 #4「82 soak ≥3 天」判据取数。**只读日志、只聚合、不推送、无第三方依赖。**
飞书推送是另一层(digest_notify.py,动工前确认频率/目标群),本文件绝不触外部通道。

解析的字面标记(逐字取自活字节 d050ac3a,见 /tmp/resp_live_82.js):
  [turn-verdict-v2] complete-run (...)          → verdict.complete_run
  [turn-verdict-v2] complete-prose (...)        → verdict.complete_prose
  [turn-verdict-v2] announce-without-action ... → verdict.announce_retry(=act-retry 触发)
  [turn-verdict-v2] violation (empty/undeliverable) -> same-conv resend ... raw=<json>
                                                → verdict.violation_resend + 收 raw 样本
  [turn-verdict-v2] violation (still empty after resend) -> honest error raw=<json>
                                                → verdict.violation_honest + 收 raw 样本
  [handshake] ack ok try=N conv=xxxx            → hs.ack_ok
  [handshake] no-ack try=N head=<json>          → hs.no_ack(总)、no_ack_final(try==HS_MAX_TRY)
  [conv] saved items=N conv=xxxx                → conv.saved
  [conv] delta send N new items ...             → conv.delta_send(握手税穿越/复用)
  [conv-persist] loaded N skipped M ...         → conv.persist_loaded(累加 N)

派生指标:
  ack_rate       = ack_ok / (ack_ok + no_ack_final)   ← **按会话**,门控口径(见下)
  ack_rate_attempt = ack_ok / (ack_ok + no_ack)       ← 按 attempt,仅诊断对照,不用于门控
  act_retry_rate = announce_retry / verdict_total
  violation_rate = (violation_resend + violation_honest) / verdict_total
  conv_reuse     = delta_send  (复用会话、免握手的轮数)

**ack_rate 会话口径推导(耦合 82 活字节握手循环 `_hsTry <= HS_MAX_TRY && !convSess`)**:
ack 成功即置 convSess 退出 → 每个**成功握手会话**恰好产 1 行 `ack ok`(任意 try);只有两次都
失败的**降级会话**才走到 `no-ack try=HS_MAX_TRY`(先败后成会话的末次是 ack ok)。故
`ack_ok 行数 = 成功会话数`、`no-ack try==HS_MAX_TRY 行数 = 降级会话数`,两者皆不依赖行相邻、
不受并发交织干扰。旧口径按 attempt(ack_ok+no_ack)会把"先 no-ack 后 ack ok"的**成功**会话
记成 50%,产假 ALARM——这正是本次修复点。HS_MAX_TRY 必须与 82 字节握手上限同步(现=2)。
"""
import json
import re
import sys

HS_MAX_TRY = 2  # 与 82 活字节握手循环上限 `_hsTry <= 2` 同步;字节改上限必须同步改此常量

VERDICT_RE = re.compile(r'\[turn-verdict-v2\]\s+(.*)$')
HS_ACK_RE = re.compile(r'\[handshake\]\s+ack ok\b')
HS_NOACK_RE = re.compile(r'\[handshake\]\s+no-ack try=(\d+)')
CONV_SAVED_RE = re.compile(r'\[conv\]\s+saved\b')
CONV_DELTA_RE = re.compile(r'\[conv\]\s+delta send\b')
PERSIST_LOADED_RE = re.compile(r'\[conv-persist\]\s+loaded\s+(\d+)\s+skipped\s+(\d+)')
RAW_RE = re.compile(r'\braw=(.*)$')


def new_acc():
    return {
        "verdict": {
            "complete_run": 0,
            "complete_prose": 0,
            "announce_retry": 0,
            "violation_resend": 0,
            "violation_honest": 0,
            "self_answer_suppressed": 0,
            "complete_run_no_shell": 0,
            "other": 0,
        },
        "hs": {"ack_ok": 0, "no_ack": 0, "no_ack_final": 0},
        "conv": {"saved": 0, "delta_send": 0, "persist_loaded": 0},
        "violation_samples": [],
    }


def _classify_verdict(tail):
    """把 [turn-verdict-v2] 之后的字面文本归到一个桶,返回桶名。"""
    if tail.startswith("complete-run but no shellTool"):
        return "complete_run_no_shell"
    if tail.startswith("complete-run"):
        return "complete_run"
    if tail.startswith("complete-prose"):
        return "complete_prose"
    if tail.startswith("announce-without-action"):
        return "announce_retry"
    if tail.startswith("self-answer SUPPRESSED"):
        return "self_answer_suppressed"
    if tail.startswith("violation (empty/undeliverable)"):
        return "violation_resend"
    if tail.startswith("violation (still empty after resend)"):
        return "violation_honest"
    return "other"


def aggregate(lines, raw_max=5):
    acc = new_acc()
    for line in lines:
        line = line.rstrip("\n")
        m = VERDICT_RE.search(line)
        if m:
            tail = m.group(1).strip()
            bucket = _classify_verdict(tail)
            acc["verdict"][bucket] += 1
            if bucket in ("violation_resend", "violation_honest"):
                rm = RAW_RE.search(tail)
                sample = rm.group(1).strip() if rm else tail
                if len(acc["violation_samples"]) < raw_max:
                    acc["violation_samples"].append({"kind": bucket, "raw": sample})
            continue
        if HS_ACK_RE.search(line):
            acc["hs"]["ack_ok"] += 1
            continue
        nm = HS_NOACK_RE.search(line)
        if nm:
            acc["hs"]["no_ack"] += 1
            if int(nm.group(1)) >= HS_MAX_TRY:
                # 末次 attempt 仍 no-ack = 该会话降级(唯一、按会话准确);先败后成的会话末次是 ack ok
                acc["hs"]["no_ack_final"] += 1
            continue
        if CONV_SAVED_RE.search(line):
            acc["conv"]["saved"] += 1
            continue
        if CONV_DELTA_RE.search(line):
            acc["conv"]["delta_send"] += 1
            continue
        pm = PERSIST_LOADED_RE.search(line)
        if pm:
            acc["conv"]["persist_loaded"] += int(pm.group(1))
            continue
    return acc


def _rate(num, den):
    return round(num / den, 4) if den else None


def summarize(acc):
    v = acc["verdict"]
    vt = sum(v.values())
    hs = acc["hs"]
    # 会话口径:分母 = 成功会话(ack_ok 行)+ 降级会话(no-ack 末次行);不含"先败后成"的中间失败
    hs_sessions = hs["ack_ok"] + hs["no_ack_final"]
    hs_attempts = hs["ack_ok"] + hs["no_ack"]
    return {
        "verdict_total": vt,
        "verdict": v,
        "handshake": {
            **hs,
            "ack_rate": _rate(hs["ack_ok"], hs_sessions),           # 会话口径,门控用
            "ack_rate_attempt": _rate(hs["ack_ok"], hs_attempts),   # attempt 口径,仅诊断对照
        },
        "conv": acc["conv"],
        "rates": {
            "act_retry_rate": _rate(v["announce_retry"], vt),
            "violation_rate": _rate(v["violation_resend"] + v["violation_honest"], vt),
            "complete_rate": _rate(v["complete_run"] + v["complete_prose"], vt),
        },
        "violation_samples": acc["violation_samples"],
    }


def main(argv):
    raw_max = 5
    files = []
    it = iter(argv[1:])
    for a in it:
        if a == "--raw-max":
            raw_max = int(next(it))
        else:
            files.append(a)
    if files:
        lines = []
        for f in files:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                lines.extend(fh.readlines())
    else:
        lines = sys.stdin.readlines()
    acc = aggregate(lines, raw_max=raw_max)
    print(json.dumps(summarize(acc), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv)
