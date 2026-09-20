#!/usr/bin/env python3
"""Codex Desktop 启动量具：把渲染层日志里的三行拍成"每个窗口挂载花了多久"。

判据（本文件是这个 skill 唯一权威量具）：
  [statsig-refresh-diagnostics] React root render requested rendererWebContentsId=N
  [statsig-refresh-diagnostics] ready provider mounted            rendererWebContentsId=N
  [startup][renderer] app routes mounted after Xms                rendererWebContentsId=N

routes mounted 之前 UI 发不出消息。所以 X 就是"新窗口第一条消息要等多久"的上界。
statsig gap = 前两行之差；它≈X 就说明瓶颈在 statsig 拉取（走 chatgpt.com）。

用法：
  startup_timing.py                # 扫最近 1 天
  startup_timing.py --days 3
  startup_timing.py --file <log>   # 只看一个文件
"""
import argparse
import glob
import os
import re
import sys
from datetime import datetime, timedelta, timezone

LOGDIR = os.path.expanduser("~/Library/Logs/com.openai.codex")
TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)")
WCID = re.compile(r"rendererWebContentsId=(\d+)")
MOUNTED = re.compile(r"app routes mounted after (\d+)ms")
APPEARANCE = re.compile(r"rendererWindowAppearance=(\w+)")
# 慢到这个程度就是用户会抱怨"转圈圈"的量级
SLOW_MS = 15000


def parse_ts(line):
    m = TS.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f%z" if "+" in m.group(1) else "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def collect(paths):
    """returns {(file, wcid): {...}} —— wcid 在不同启动里会重号，所以按文件分桶"""
    out = {}
    for p in paths:
        try:
            with open(p, errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            if "statsig-refresh-diagnostics" not in line and "app routes mounted" not in line:
                continue
            w = WCID.search(line)
            if not w:
                continue
            t = parse_ts(line)
            if t is None:
                continue
            key = (p, int(w.group(1)))
            rec = out.setdefault(key, {})
            ap = APPEARANCE.search(line)
            if ap:
                rec.setdefault("appearance", ap.group(1))
            if "React root render requested" in line:
                rec.setdefault("render_req", t)
            elif "ready provider mounted" in line:
                rec.setdefault("provider", t)
            elif "app routes mounted" in line:
                m = MOUNTED.search(line)
                if m:
                    rec.setdefault("mounted_ms", int(m.group(1)))
                    rec.setdefault("mounted_at", t)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--file", action="append", default=[])
    args = ap.parse_args()

    if args.file:
        paths = args.file
    else:
        paths = []
        now = datetime.now(timezone.utc)
        for d in range(args.days):
            day = now - timedelta(days=d)
            paths += glob.glob(os.path.join(LOGDIR, day.strftime("%Y/%m/%d"), "*.log"))
        # UTC 与本地差 8 小时，多带一天避免切边丢启动
        day = now - timedelta(days=args.days)
        paths += glob.glob(os.path.join(LOGDIR, day.strftime("%Y/%m/%d"), "*.log"))

    if not paths:
        print("NO_LOGS 在 %s 下没找到日志；Codex Desktop 装了吗？" % LOGDIR)
        return 2

    recs = collect(paths)
    rows = []
    for (path, wcid), r in recs.items():
        if "mounted_ms" not in r:
            continue  # 这个 renderer 还没挂载完 / 日志被切走了
        gap = None
        if "render_req" in r and "provider" in r:
            gap = (r["provider"] - r["render_req"]).total_seconds()
        rows.append((r.get("mounted_at"), os.path.basename(path), wcid,
                     r.get("appearance", "?"), r["mounted_ms"], gap))
    rows.sort(key=lambda x: x[0] or datetime.min.replace(tzinfo=timezone.utc))

    if not rows:
        print("NO_MOUNT_LINES 找到日志但没有 'app routes mounted' 行。")
        print("  → 启动那几行只在**本次启动的 t0 日志**里，日志切片后就没有了。")
        print("  → 想量必须重启一次 Codex，然后立刻跑本脚本。")
        return 2

    print("%-14s %-4s %-14s %10s %10s  %s" % ("mounted(本地)", "wcid", "appearance", "mounted", "statsig", "verdict"))
    worst = 0
    for at, _f, wcid, ap_, ms, gap in rows:
        local = (at + timedelta(hours=8)).strftime("%m-%d %H:%M:%S") if at else "?"
        worst = max(worst, ms)
        if ms >= SLOW_MS:
            if gap is not None and gap * 1000 >= ms * 0.7:
                v = "SLOW ⇒ statsig 吃掉 %.0f%%，查 chatgpt.com 是否 connect 挂住" % (gap * 1000 / ms * 100)
            else:
                v = "SLOW 但 statsig 只占 %s，另有其因" % ("%.1fs" % gap if gap is not None else "未知")
        else:
            v = "OK"
        print("%-14s %-4d %-14s %8dms %9s  %s"
              % (local, wcid, ap_, ms, ("%.1fs" % gap) if gap is not None else "-", v))

    print()
    print("窗口数 %d，最慢 %dms（阈值 %dms）" % (len(rows), worst, SLOW_MS))
    # 退出码只看**最后一次挂载**——历史里的慢行是档案，不是当前状态。
    # 想当回归门就直接用退出码：0=当前快，1=当前慢。
    last_ms = rows[-1][4]
    print("最后一次挂载 %dms ⇒ %s" % (last_ms, "当前正常" if last_ms < SLOW_MS else "当前仍慢"))
    if worst >= SLOW_MS and last_ms < SLOW_MS:
        print("  注：历史里有慢行=修复前的档案；只要最后一行快，就是修好了。")
    return 0 if last_ms < SLOW_MS else 1


if __name__ == "__main__":
    sys.exit(main())
