#!/usr/bin/env python3
"""patch_strip_plan.py — 把 hook 的 `_STRIP` 从「已不存在的旧段名」扩到 Cursor 3.17.19 现役段名。

## 三段式

假设：本线首轮 100% `announce-without-action`（宣告而不动手）来自 Cursor system prompt 里
      两段**诱导规划/宣告、且指向本线不存在的工具**的小节：
        <task_management> —— "You have access to the todo_write tool ... use it to plan"
                              （本线没有 todo_write）
        <mode_selection>  —— "call `SwitchMode` now and include a brief explanation" +
                              "Be proactive about switching"（本线没有 SwitchMode，
                              模型唯一能执行的部分就剩那段 explanation = 宣告）

证伪条件：剥掉这两段后，真 Cursor 首轮 `announce-without-action` 占比不下降。
      当前基线 = 5/5（100%），2026-09-01 05:14–05:22 我自己用 cursor_gui_e2e_driver.py
      驱动的 4 发 + 1 发探针，逐发有 pod 日志。

数据：同窗口 post-hook 载荷里
      - 老 `_STRIP` 的五个目标段名 status_update_spec / summary_spec / flow /
        completion_spec / todo_spec **出现 0 次**——那段剥离逻辑对这版客户端是死代码；
      - `<task_management>` / `<mode_selection>` **每一发都在**（各 5 次）；
      - 5 发首轮全部 `announce-without-action`，其中 2 发靠强制重问救回 complete-run。

## 只改两处

① `_STRIP` 的段名 alternation 追加 `task_management|mode_selection`（旧五名保留，
   别的 Cursor 版本可能还在用，留着零成本）；
② `_transform_text` 改用 `subn` 并把命中数打日志——原来的 `_STRIP.sub` 无计数，
   正因为无计数，它空转了不知道多久没人发现。

`<making_code_changes>` / `<citing_code>` / `<terminal_files_information>` /
`<tone_and_style>` 一律不动（本轮只验这一个变量）。

## 回滚

`CURSOR_G_STRIP_PLAN=0`（proxy env，秒级）；或恢复 CM 备份 + rollout。

用法：python3 patch_strip_plan.py <src.py> <out.py>
"""
import sys

A_STRIP = '''_STRIP = re.compile(
    r"<(status_update_spec|summary_spec|flow|completion_spec|todo_spec)>.*?</\\1>\\s*",
    re.DOTALL | re.IGNORECASE,
)'''

NEW_STRIP = '''# 2026-09-01：原表只有 status_update_spec/summary_spec/flow/completion_spec/todo_spec，
# 而 Cursor 3.17.19 的真载荷里这五个名字**一个都没有**（实测 0/5）——这条正则一直在空转。
# 现役的诱导宣告段是下面两个，且都指向本线不存在的工具：
#   task_management → todo_write（不存在）；mode_selection → SwitchMode（不存在，
#   模型唯一能照办的只剩 "include a brief explanation" = 宣告）。
# 旧五名保留：别的 Cursor 版本可能还在用，留着零成本。
# 关：CURSOR_G_STRIP_PLAN=0
_STRIP_PLAN_ON = os.environ.get("CURSOR_G_STRIP_PLAN", "1") != "0"

_STRIP_LEGACY = "status_update_spec|summary_spec|flow|completion_spec|todo_spec"
_STRIP_PLAN = "task_management|mode_selection"

_STRIP = re.compile(
    r"<(" + _STRIP_LEGACY + r")>.*?</\\1>\\s*",
    re.DOTALL | re.IGNORECASE,
)

_STRIP2 = re.compile(
    r"<(" + _STRIP_PLAN + r")>.*?</\\1>\\s*",
    re.DOTALL | re.IGNORECASE,
)


def _strip_sections(text: str) -> str:
    """剥叙述/规划段。带计数日志——原版没有计数，所以它空转了很久没人发现。"""
    text, n1 = _STRIP.subn("", text)
    n2 = 0
    if _STRIP_PLAN_ON:
        text, n2 = _STRIP2.subn("", text)
    if n1 or n2:
        try:
            _logger.info(
                "cursor_web_fc_sys_rewrite: stripped legacy=%d plan=%d section(s)", n1, n2
            )
        except Exception:
            pass
    return text'''

A_APPLY = '    stripped = _deconflict(_STRIP.sub("", text))'
NEW_APPLY = '    stripped = _deconflict(_strip_sections(text))'


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src = open(sys.argv[1], encoding="utf-8").read()

    for name, anchor in (("_STRIP 定义", A_STRIP), ("_transform_text 应用点", A_APPLY)):
        n = src.count(anchor)
        if n != 1:
            sys.exit("!! 锚点 %s 出现 %d 次（期望 1）——拒绝生成" % (name, n))
    if "_STRIP2" in src or "CURSOR_G_STRIP_PLAN" in src:
        sys.exit("!! 已打过，别重复")
    if "import os" not in src:
        sys.exit("!! 前置：需先打 patch_sys_deconflict.py（它引入 import os）")

    out = src.replace(A_STRIP, NEW_STRIP, 1).replace(A_APPLY, NEW_APPLY, 1)
    out = out.replace('_VERSION = "v3"', '_VERSION = "v4"', 1)
    if out == src:
        sys.exit("!! 空转，拒绝写出")

    import ast
    ast.parse(out)
    open(sys.argv[2], "w", encoding="utf-8").write(out)
    print("OK  %s -> %s  (%d -> %d chars)" % (sys.argv[1], sys.argv[2], len(src), len(out)))


if __name__ == "__main__":
    main()
