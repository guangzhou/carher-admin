#!/usr/bin/env python3
"""digest_notify.py — ROI #3 每日摘要格式化 + 飞书推送(默认 dry-run,零外推)。

链路:verdict_digest.py 产的聚合 JSON(stdin 或 --in 文件)→ format_card() 成一段可读文本
→ 默认只打印;仅 `--apply` 且 FEISHU_WEBHOOK 已设(非 stub)才真推。发送函数逐字照抄
scripts/zerokey-pool-health-monitor.py::alert_feishu 的安全语义(msg_type:text / stub 守卫 /
超时 10s / 异常不抛)。**动工前须与人确认目标群 + 频率;本文件不含任何硬编码 webhook。**

判据行(供 #4 soak 快速裁读):按阈值给 OK / WARN / ALARM。
  ALARM: violation_rate>0 或 ack_rate<0.95(有 ack 样本时)
  WARN : act_retry_rate>=0.05
  OK   : 其余

用法:
  ... | python3 verdict_digest.py | python3 digest_notify.py            # dry-run 打印
  python3 digest_notify.py --in agg.json --apply                        # 真推(需 FEISHU_WEBHOOK)
"""
import argparse
import json
import os
import sys
import urllib.request

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

ACK_ALARM = 0.95
ACT_RETRY_WARN = 0.05


def verdict_level(summary):
    """按阈值裁读一行判据。返回 (level, reasons[])。"""
    reasons = []
    rates = summary.get("rates", {})
    hs = summary.get("handshake", {})
    vr = rates.get("violation_rate")
    ar = rates.get("act_retry_rate")
    ack = hs.get("ack_rate")
    level = "OK"
    if vr and vr > 0:
        level = "ALARM"
        reasons.append(f"violation_rate={vr}")
    if ack is not None and ack < ACK_ALARM:
        level = "ALARM"
        reasons.append(f"ack_rate={ack}<{ACK_ALARM}")
    if level != "ALARM" and ar and ar >= ACT_RETRY_WARN:
        level = "WARN"
        reasons.append(f"act_retry_rate={ar}>={ACT_RETRY_WARN}")
    return level, reasons


def format_card(summary, lane="82", window="24h"):
    v = summary.get("verdict", {})
    hs = summary.get("handshake", {})
    conv = summary.get("conv", {})
    rates = summary.get("rates", {})
    level, reasons = verdict_level(summary)
    lines = [
        f"[cursor-g {lane} soak · {window}] {level}" + (f" — {'; '.join(reasons)}" if reasons else ""),
        f"verdict total={summary.get('verdict_total', 0)}  "
        f"complete_rate={rates.get('complete_rate')}  "
        f"act_retry={rates.get('act_retry_rate')}  violation={rates.get('violation_rate')}",
        f"  run={v.get('complete_run', 0)} prose={v.get('complete_prose', 0)} "
        f"announce_retry={v.get('announce_retry', 0)} "
        f"viol_resend={v.get('violation_resend', 0)} viol_honest={v.get('violation_honest', 0)}",
        f"handshake ack={hs.get('ack_ok', 0)}/{hs.get('ack_ok', 0) + hs.get('no_ack_final', 0)}会话 "
        f"(rate={hs.get('ack_rate')}; attempt={hs.get('ack_rate_attempt')})",
        f"conv saved={conv.get('saved', 0)} delta_send(复用)={conv.get('delta_send', 0)} "
        f"persist_loaded={conv.get('persist_loaded', 0)}",
    ]
    samples = summary.get("violation_samples") or []
    if samples:
        lines.append("violation raw 样本:")
        for s in samples[:5]:
            lines.append(f"  [{s.get('kind')}] {s.get('raw')}")
    return "\n".join(lines)


def send_feishu(text, apply):
    # 逐字照抄 zerokey-pool-health-monitor.py::alert_feishu 的安全语义
    if not apply:
        print("DRY-RUN 飞书摘要(未发送):\n" + text)
        return
    if not FEISHU_WEBHOOK or FEISHU_WEBHOOK.startswith("stub"):
        print("⚠ FEISHU_WEBHOOK 未设置,跳过发送。摘要内容:\n" + text)
        return
    try:
        body = {"msg_type": "text", "content": {"text": text}}
        req = urllib.request.Request(
            FEISHU_WEBHOOK, method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
        print("飞书摘要已发送")
    except Exception as e:
        print(f"飞书发送失败: {e}")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default=None, help="聚合 JSON 文件(默认读 stdin)")
    ap.add_argument("--lane", default="82")
    ap.add_argument("--window", default="24h")
    ap.add_argument("--apply", action="store_true", help="真推飞书(默认 dry-run 只打印)")
    args = ap.parse_args(argv[1:])
    raw = open(args.infile, encoding="utf-8").read() if args.infile else sys.stdin.read()
    summary = json.loads(raw)
    text = format_card(summary, lane=args.lane, window=args.window)
    send_feishu(text, args.apply)


if __name__ == "__main__":
    main(sys.argv)
