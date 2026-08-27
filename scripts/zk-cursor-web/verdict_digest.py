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
  [handshake] no-ack try=N head=<json>          → hs.no_ack
  [conv] saved items=N conv=xxxx                → conv.saved
  [conv] delta send N new items ...             → conv.delta_send(握手税穿越/复用)
  [conv-persist] loaded N skipped M ...         → conv.persist_loaded(累加 N)

派生指标:
  ack_rate       = ack_ok / (ack_ok + no_ack)
  act_retry_rate = announce_retry / verdict_total
  violation_rate = (violation_resend + violation_honest) / verdict_total
  conv_reuse     = delta_send  (复用会话、免握手的轮数)

用法:
  kubectl logs -l app=zero-cursor-bpi-82 --tail=-1 | python3 verdict_digest.py
  python3 verdict_digest.py /path/to/pod.log [more.log ...]
  # --raw-max N 控制每类 violation 保留的 raw 样本数(默认 5)
"""
import json
import re
import sys

VERDICT_RE = re.compile(r'\[turn-verdict-v2\]\s+(.*)$')
HS_ACK_RE = re.compile(r'\[handshake\]\s+ack ok\b')
HS_NOACK_RE = re.compile(r'\[handshake\]\s+no-ack\b')
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
        "hs": {"ack_ok": 0, "no_ack": 0},
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
        if HS_NOACK_RE.search(line):
            acc["hs"]["no_ack"] += 1
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
    hs_total = hs["ack_ok"] + hs["no_ack"]
    return {
        "verdict_total": vt,
        "verdict": v,
        "handshake": {**hs, "ack_rate": _rate(hs["ack_ok"], hs_total)},
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
