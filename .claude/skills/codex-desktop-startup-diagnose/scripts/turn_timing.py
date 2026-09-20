#!/usr/bin/env python3
"""Codex Desktop 端到端轮次量具：从 app-server 日志库配对"用户提交 → 模型首个 message"。

为什么不能用别的判据：
  - `[desktop-notifications] show turn-complete` **应用在前台时不弹**，拿它判会得到假 TIMEOUT。
  - 渲染层日志不含服务端耗时。
  - 所以唯一可信的服务端耗时 = 下面两行的 ts 差，**按 submission.id 配对**（不是按时间就近）。

数据源 `~/.codex/logs_2.sqlite`，表 logs：
  提交：target LIKE '%::handlers'，body 含 `op: TurnInput` + `UserInput { content: [Text { text: "..." }`
  完成：target LIKE '%stream_events_utils'，body 含 `Output item item_type="message"`
  两边 span 里都带 `submission.id="..."`（提交行的是 `id: "..."`）。
⚠️ `ts` 是 **unix epoch 秒**，1 秒分辨率 —— 所以耗时读数的误差是 ±1s，别拿它测亚秒差异。

用法：
  turn_timing.py                 # 最近 20 轮
  turn_timing.py -n 60
  turn_timing.py --since-min 30  # 只看最近 30 分钟
  turn_timing.py --show-title     # 把 thread_title 那种内部轮次也列出来
"""
import argparse
import os
import re
import sqlite3
import sys
import time

DB = os.path.expanduser("~/.codex/logs_2.sqlite")
SUBMIT_ID = re.compile(r'Submission \{ id: "([0-9a-f-]+)"')
SPAN_SUB_ID = re.compile(r'submission\.id="([0-9a-f-]+)"')
USER_TEXT = re.compile(r'UserInput \{ content: \[Text \{ text: "((?:[^"\\]|\\.)*)"')
MODEL = re.compile(r"model=([\w.\-]+)")
THREAD = re.compile(r"thread_id=([0-9a-f-]+)")
# 每个新会话都会额外跑一次这个内部轮次去生成标题（实测 ~20k input tokens）
TITLE_MARK = "You are a helpful assistant. You will be presented with a user prompt"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--since-min", type=int, default=None)
    ap.add_argument("--show-title", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("NO_DB 找不到 %s" % args.db)
        return 2

    # mode=ro：只读打开、不拿写锁、**会读 -wal**。
    # ⚠️ 绝不能用 immutable=1：那是"文件永不变"的承诺，SQLite 会跳过 -wal，
    #    2026-09-09 实测因此少读 45 秒的新行 ⇒ 刚发的那一轮凭空消失、探针假红。
    # 也别 cp 出来读（实测 950MB）。
    con = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    floor = int(time.time()) - args.since_min * 60 if args.since_min else 0

    submits = {}  # submission_id -> (ts, thread, text)
    for ts, body in con.execute(
        "select ts, feedback_log_body from logs where target like '%::handlers' "
        "and feedback_log_body like '%op: TurnInput%' and ts >= ? order by id", (floor,)):
        m = SUBMIT_ID.search(body)
        if not m:
            continue
        t = USER_TEXT.search(body)
        text = (t.group(1) if t else "?")
        submits.setdefault(m.group(1), (ts, (THREAD.search(body).group(1) if THREAD.search(body) else "?"), text))

    done = {}  # submission_id -> (ts, model)
    for ts, body in con.execute(
        "select ts, feedback_log_body from logs where target like '%stream_events_utils' "
        "and feedback_log_body like '%item_type=\"message\"%' and ts >= ? order by id", (floor,)):
        m = SPAN_SUB_ID.search(body)
        if not m:
            continue
        sid = m.group(1)
        mo = MODEL.search(body)
        prev = done.get(sid)
        # 同一 submission 会有两行（两层 span），取**最早**那条＝首个 message 落地
        if prev is None or ts < prev[0]:
            done[sid] = (ts, mo.group(1) if mo else (prev[1] if prev else "?"))
        elif mo and prev[1] == "?":
            done[sid] = (prev[0], mo.group(1))

    rows = []
    for sid, (ts, thread, text) in submits.items():
        is_title = TITLE_MARK in text or text.startswith("You are a helpful assistant")
        if is_title and not args.show_title:
            continue
        d = done.get(sid)
        rows.append((ts, thread, sid, text, d[0] - ts if d else None,
                     d[1] if d else "-", is_title))
    rows.sort(key=lambda r: r[0])
    rows = rows[-args.n:]

    if not rows:
        print("NO_TURNS 窗口内没有轮次。（app-server 没跑？或 --since-min 太小）")
        return 2

    print("%-8s %-10s %7s %-14s %s" % ("时刻", "thread", "耗时", "model", "输入"))
    slow = 0
    for ts, thread, _sid, text, dur, model, is_title in rows:
        clock = time.strftime("%H:%M:%S", time.localtime(ts))
        show = text.replace("\\n", " ").strip()[:44]
        if is_title:
            show = "[内部 thread_title 生成] " + show[:20]
        if dur is None:
            d = "未完成"
            slow += 1
        else:
            d = "%ds" % dur
            if dur >= 15:
                slow += 1
        print("%-8s %-10s %7s %-14s %s" % (clock, thread[:8], d, model, show))

    print()
    print("共 %d 轮，其中 %d 轮 ≥15s 或未完成（ts 是秒级，±1s）" % (len(rows), slow))
    print("『未完成』的三种无害成因，别当故障：用户改字重发（旧 submission 被顶掉）、")
    print("  纯工具轮次（没吐 message item）、以及本轮还在跑。")
    print("提示：若服务端耗时正常但用户体感慢，慢的是**渲染层挂载**——去跑 startup_timing.py")
    return 0 if slow == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
