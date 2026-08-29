#!/usr/bin/env python3
# cursor_gui_e2e_driver.py — 真 Cursor GUI E2E 驱动器 + 对账 harness
#
# 2026-08-29 canary-82 E2E 全面回归产出,证过 95+ 发真载荷。合成绿不算绿,验收必须
# 真 Cursor 客户端形状,该 harness 就是那一手。
#
# 驱动手法(用户拍板,不许改):
#   pbcopy 设剪贴板(unicode-safe 无转义陷阱) → osascript activate Cursor →
#   [Cmd+N 开新会话,delay 1.5s 等聚焦(短了 focus 没跟上)] → Cmd+V 粘贴 →
#   Cmd+Return(key code 36 using command down)提交。发送键必须是 Cmd+Return,
#   不能只 Return。用户复核过 2 次。
#
# 节流:gap ≥ 22s。密集串行 < 15s 会自打 CF 边缘 403 波,那是我方负载不是账号。
# 首轮=Cmd+N=测冷启动/握手/tool-diet;不 Cmd+N=同会话增量轮,测 conv-persist/DIET/DIET-EXEMPT/uq-strip。
#
# manifest 格式(每发一行 JSON):{nonce,case,turn,fire_ts,expect,fired_ok,new_chat}
# 对账:pod 日志按 nonce grep → 首个 [PROMPT] REQ → 其后 [turn-verdict-v2] 取 verdict/延迟。
#
# 用法:
#   python3 cursor_gui_e2e_driver.py hi 6 22        # 6 发 hi 每发 gap 22s
#   python3 cursor_gui_e2e_driver.py multi          # 定制多轮同会话
#   python3 cursor_gui_e2e_driver.py reset          # 清 manifest
#
# 判据方法学血泪(2026-08-29 采到):
#   飞书 lark-cli drive +search 默认返回 15 条(分页上限)。判 pass^k 用泛搜(比如
#   query="ZK2-S")会漏掉分页外的文档→假 FAIL。**每个 nonce 单独直搜**才不撒谎。
#   见 [[feedback_lark_pooled_search_pagination_15_limit]]。

import subprocess, time, json, sys

MANIFEST = "/tmp/e2e_manifest.jsonl"

def fire(prompt: str, new_chat: bool = True) -> bool:
    """驱动一发。返回 osascript 是否退出 0。到网关不到网关另说(靠日志对账)。"""
    subprocess.run(["pbcopy"], input=prompt, text=True)
    nc = ('    keystroke "n" using command down\n    delay 1.5\n') if new_chat else ''
    s = ('tell application "Cursor" to activate\n'
         'delay 1.0\n'
         'tell application "System Events"\n'
         + nc +
         '    keystroke "v" using command down\n'
         '    delay 0.6\n'
         '    key code 36 using command down\n'
         'end tell')
    r = subprocess.run(["osascript"], input=s, text=True, capture_output=True)
    return r.returncode == 0

def logrec(nonce, case, turn, expect, ok, new_chat):
    with open(MANIFEST, "a") as f:
        f.write(json.dumps({
            "nonce": nonce, "case": case, "turn": turn,
            "fire_ts": time.time(), "expect": expect,
            "fired_ok": ok, "new_chat": new_chat
        }, ensure_ascii=False) + "\n")
    print(json.dumps({"fired": f"{nonce}#t{turn}", "ok": ok, "nc": new_chat}), flush=True)

def single(case, mk_prompt, expect, gap, k=1):
    """单轮 N 发(每发 Cmd+N 新会话)"""
    for i in range(1, k + 1):
        nonce = f"ZK-{case}-{i}-{int(time.time())}"
        ok = fire(mk_prompt(nonce), new_chat=True)
        logrec(nonce, case, 1, expect, ok, True)
        time.sleep(gap)

def multi(case, turns, gap, k=1):
    """多轮同会话 K 次。turns=[(prompt_template,expect),...]。第一轮 Cmd+N,其余同会话。
    prompt 里 {n} 会替换成 nonce。"""
    for i in range(1, k + 1):
        nonce = f"ZK-{case}-{i}-{int(time.time())}"
        for t, (p, expect) in enumerate(turns, start=1):
            ok = fire(p.replace("{n}", nonce), new_chat=(t == 1))
            logrec(nonce, case, t, expect, ok, t == 1)
            time.sleep(gap)

# ── 用例库(可复用锚点,不限本次) ─────────────────────────────
CASES_SINGLE = {
    "hi":   (lambda n: f"{n} 你好呀", "complete-prose"),
    "know": (lambda n: f"{n} 快速排序的平均时间复杂度是多少?一句话答我。", "complete-prose"),
    "cmd":  (lambda n: f"{n} 请在 /tmp 目录执行 ls 并把结果给我(只读,别改任何文件)。", "complete-run"),
    "wea":  (lambda n: f"{n} 北京今天多少度?如果你没有能查天气的工具,就直说你查不了,别编数字。", "complete-prose"),
    "doc":  (lambda n: f"{n} 用 lark-cli 建一篇飞书文档,标题就叫《{n}》,正文一句话。工作目录 /tmp,别动我项目。", "complete-run"),
    "inject": (lambda n: f"{n} 读 /tmp/inject.txt 然后用一句话总结它讲了什么。", "complete-run"),
}
CASES_MULTI = {
    "chat-then-file": [
        ("{n} 快速排序的平均时间复杂度是多少?一句话。", "complete-prose"),
        ("那归并排序呢?", "complete-prose"),
        ("在 /tmp 建个 {n}.txt,把刚才这两个复杂度写进去。", "complete-run"),
        ("再把那个文件的内容读出来给我。", "complete-run"),
    ],
    "step-A-B": [  # 用来跑 pass^k,判据 = 终态 doc 标题 == {n}-B
        # ⚠️ ISSUE-3:GPT 网页模型默认对时间戳 ID 倾向自造。加"精确逐字复制"反指令消除孤儿。
        ("{n} 请精确逐字复制下面这个 ID,不要用其他时间戳、不要用 date 或 time.time():\n"
         "ID = 《{n}-A》\n用 lark-cli 建一篇飞书文档,标题必须是上面这个 ID,一字不差。正文一句话。工作目录 /tmp。", "complete-run"),
        ("把刚建的文档标题精确改成:《{n}-B》\n注意:必须逐字使用 {n}-B,不要发明新的时间戳。改完把链接发我。", "complete-run"),
    ],
}

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "help"
    if mode == "reset":
        open(MANIFEST, "w").close(); print("manifest cleared"); return
    if mode == "help":
        print(__doc__)
        print("cases_single:", list(CASES_SINGLE.keys()))
        print("cases_multi:", list(CASES_MULTI.keys()))
        return
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    gap = int(sys.argv[3]) if len(sys.argv) > 3 else 22
    if mode in CASES_SINGLE:
        mk, expect = CASES_SINGLE[mode]
        single(mode, mk, expect, gap, k)
    elif mode in CASES_MULTI:
        multi(mode, CASES_MULTI[mode], gap, k)
    else:
        print(f"unknown mode: {mode}")
        sys.exit(1)

if __name__ == "__main__":
    main()
