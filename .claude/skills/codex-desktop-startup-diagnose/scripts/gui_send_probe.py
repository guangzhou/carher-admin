#!/usr/bin/env python3
"""GUI 端到端探针：在 Codex Desktop 里真发一条消息，并用**服务端日志**判成败。

这个脚本存在的唯一理由是：2026-09-09 我在同一轮里四次用错判据、说出四个假结论。
它把当时踩出来的每一条纪律固化成代码：

  1. 中文输入法会吞键改字符 ⇒ 一律 `pbcopy` + Cmd+V，**绝不** `keystroke` 打正文。
  2. `click at {x,y}` 在 Electron 上是空转 ⇒ 定位 composer 靠 Cmd+N/点窗口，不靠坐标。
  3. 「新建窗口」**没有快捷键**；Cmd+N 是「新聊天」，不改窗口数
     ⇒ 别拿 `count of windows` 判 Cmd+N 生效。要读 `AXMenuItemCmdChar` 再下手。
  4. `show turn-complete` 通知**前台不弹** ⇒ 判完成只认 logs_2.sqlite 里的
     submission → `item_type="message"` 配对（turn_timing.py 那套）。
  5. 判据要先立**阳性对照**：发之前先记下 baseline 轮次数，没涨就是没发出去，
     不许把「日志没动」直接说成「应用卡死」。
  6. 读 logs_2.sqlite 一律 `mode=ro`，**永不 `immutable=1`**：后者让 SQLite 跳过 -wal，
     本脚本第一次跑就因此少读 45 秒的新行 ⇒ 明明 3 秒完成的轮次被判成 FAIL。
     量具自己会骗人，所以 FAIL 的第一步是换个读法复核，不是下结论。

用法：
  gui_send_probe.py                 # 在当前窗口新建聊天并发一条带 nonce 的探针
  gui_send_probe.py --timeout 90
  gui_send_probe.py --text "自定义正文"
  gui_send_probe.py --no-new-chat   # 在当前会话里发（测第二条消息是否快）
退出码：0=探针轮次在超时内完成；1=没完成；2=前置条件不满足。
"""
import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid

DB = os.path.expanduser("~/.codex/logs_2.sqlite")


def find_app():
    """按 bundle id 找 app，不写死路径。

    2026-09-10 这个包被从 /Applications/Codex.app 挪到了 /Applications/OpenAI Codex.app
    （那个路径下所有子进程一 exec 就被 SIGKILL，换个包名才能跑），写死路径的量具当场失效。
    环境变量 CODEX_APP 优先，其次扫 /Applications 里 CFBundleIdentifier 匹配的包。
    """
    env = os.environ.get("CODEX_APP")
    if env:
        return os.path.join(env, "Contents/MacOS/ChatGPT")
    import glob
    import plistlib
    for app in sorted(glob.glob("/Applications/*.app")):
        try:
            with open(os.path.join(app, "Contents/Info.plist"), "rb") as f:
                if plistlib.load(f).get("CFBundleIdentifier") == "com.openai.codex":
                    return os.path.join(app, "Contents/MacOS/ChatGPT")
        except Exception:
            continue
    return "/Applications/Codex.app/Contents/MacOS/ChatGPT"  # 兜底，让报错信息仍可读


APP_PROC = find_app()
SPAN_SUB_ID = re.compile(r'submission\.id="([0-9a-f-]+)"')
SUBMIT_ID = re.compile(r'Submission \{ id: "([0-9a-f-]+)"')


def osa(script):
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True)


def q(db, sql, params=()):
    # mode=ro 而不是 immutable=1 —— 见下方 6. 这条纪律是本脚本第一次运行时被咬出来的。
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        return list(con.execute(sql, params))
    finally:
        con.close()


def find_probe_turn(db, nonce, floor_ts):
    """返回 (submitted_ts, done_ts|None)；按 submission.id 配对，不按时间就近。"""
    rows = q(db,
             "select ts, feedback_log_body from logs where target like '%::handlers' "
             "and feedback_log_body like ? and ts >= ? order by id",
             ("%" + nonce + "%", floor_ts))
    sid = None
    sub_ts = None
    for ts, body in rows:
        m = SUBMIT_ID.search(body)
        if m:
            sid, sub_ts = m.group(1), ts
    if sid is None:
        return None, None
    done = q(db,
             "select ts, feedback_log_body from logs where target like '%stream_events_utils' "
             "and feedback_log_body like ? and feedback_log_body like ? and ts >= ? order by id",
             ("%" + sid + "%", '%item_type="message"%', sub_ts))
    return sub_ts, (done[0][0] if done else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--text", default=None)
    ap.add_argument("--no-new-chat", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    if subprocess.run(["pgrep", "-f", APP_PROC],
                      capture_output=True).returncode != 0:
        print("PRECOND_FAIL Codex Desktop 没在跑。先 relaunch_fix.sh --yes")
        return 2
    if not os.path.exists(args.db):
        print("PRECOND_FAIL 找不到 %s" % args.db)
        return 2

    nonce = "PROBE-" + uuid.uuid4().hex[:8].upper()
    text = args.text or ("reply with exactly: " + nonce)
    if nonce not in text:
        # nonce 必须出现在正文里，否则没法把这一轮从并发轮次里认出来
        text = text + " " + nonce
    floor = int(time.time()) - 5

    print("nonce = %s" % nonce)

    # 前台化。窗口标题/坐标都不可靠，只用 activate。
    osa('tell application "ChatGPT" to activate')
    time.sleep(1.5)

    if not args.no_new_chat:
        # Cmd+N = 新聊天（注意：不是新窗口，窗口数不会变）
        osa('tell application "System Events" to keystroke "n" using command down')
        time.sleep(1.5)

    # 正文只能走剪贴板，绕开中文输入法
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
    osa('tell application "System Events" to keystroke "v" using command down')
    time.sleep(0.8)
    osa('tell application "System Events" to key code 36')  # Return

    print("已发出，开始按服务端日志轮询（最多 %ds）…" % args.timeout)
    t0 = time.time()
    seen_submit = False
    while time.time() - t0 < args.timeout:
        sub_ts, done_ts = find_probe_turn(args.db, nonce, floor)
        if sub_ts and not seen_submit:
            seen_submit = True
            # 阳性对照成立：这一刀确实进了 app-server，不是"应用没收到按键"
            print("  提交已落库（+%.0fs）——按键生效，排除『应用收不到合成按键』" % (time.time() - t0))
        if done_ts:
            print("PASS 服务端耗时 %ds（提交→首个 message item，ts 秒级 ±1s）"
                  % (done_ts - sub_ts))
            return 0
        time.sleep(2)

    if not seen_submit:
        print("FAIL 超时且**提交都没落库** ⇒ 按键没送到 composer，"
              "或渲染层路由还没挂载（去跑 startup_timing.py，别下『卡死』结论）")
    else:
        print("FAIL 提交落库了但超时内没出 message item ⇒ 慢在上游/路由，不在客户端")
    return 1


if __name__ == "__main__":
    sys.exit(main())
