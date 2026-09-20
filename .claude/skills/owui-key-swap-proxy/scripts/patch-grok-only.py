#!/usr/bin/env python3
"""把 key-swap-proxy 从「禁 Claude」改成「只放 sa-grok-4.6」。

改两处，不是一处：
  1. /v1/models 列表里只留白名单里的模型（用户看不见别的）
  2. 真发请求时，model 不在白名单就拦（历史会话继续发也会被拦）

只改列表不拦发送 = 半道门：OWUI 的历史会话里存着老模型名，
用户点开上周的对话继续发，列表滤不住它，照样发得出去。

原来的「禁 Claude」逻辑不用留 —— 白名单只有 sa-grok-4.6，Claude 天然在外面。
锚点全部要求唯一命中，命中数不对就报红退出，不静默跳过。
"""
from __future__ import annotations
import re
import sys

SRC = "/root/key-swap-proxy/main.py"
OUT = "/root/key-swap-proxy/main.py"

ALLOW_BLOCK = '''# ---- OWUI 只开放这一个模型（2026-09-15，全员一视同仁，含管理员）----
# 原来这里是「禁 Claude」黑名单。改成白名单：不在名单里的一个都不放。
# 环境变量可覆盖，改名单不用重新发版：ALLOWED_MODELS=a,b,c
ALLOWED_MODELS = {
    m.strip().lower()
    for m in os.environ.get("ALLOWED_MODELS", "sa-grok-4.6").split(",")
    if m.strip()
}


def _model_allowed(model: str | None) -> bool:
    return bool(model) and model.strip().lower() in ALLOWED_MODELS


def _model_blocked_response(model: str | None) -> JSONResponse:
    names = ", ".join(sorted(ALLOWED_MODELS))
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": (
                    f"Open WebUI 现在只提供 {names}，请在模型里选它。"
                    f"（你这次发的是 {model or '空'}）历史会话里的旧模型需要手动切换。"
                ),
                "type": "model_not_allowed",
                "code": "model_not_allowed",
            }
        },
    )
'''


def sub_once(text: str, pattern: str, repl: str, label: str, flags=0) -> str:
    hits = len(re.findall(pattern, text, flags))
    if hits != 1:
        sys.exit(f"锚点 [{label}] 命中 {hits} 次，期望 1 次 —— 不改，退出")
    print(f"  锚点 [{label}] 命中 1 次 OK")
    return re.sub(pattern, repl, text, count=1, flags=flags)


def main() -> int:
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()

    if "ALLOWED_MODELS" in src:
        sys.exit("已经改过了（找到 ALLOWED_MODELS）—— 不重复改")

    # 1) 把 CLAUDE_MODEL_RE 那行换成白名单常量
    src = sub_once(
        src,
        r'^CLAUDE_MODEL_RE = re\.compile\(r"\(\?i\)claude", re\.IGNORECASE\)$',
        lambda m: 'ALLOWED_MODELS_PLACEHOLDER',
        "CLAUDE_MODEL_RE 常量",
        re.M,
    )

    # 2) 列表过滤：留白名单，而不是删 Claude
    src = sub_once(
        src,
        r'body\["data"\] = \[m for m in body\["data"\] if not _is_claude_model\(m\.get\("id"\)\)\]',
        'body["data"] = [m for m in body["data"] if _model_allowed(m.get("id"))]',
        "列表过滤",
    )

    # 3) 发送时拦截：不在白名单就拦
    src = sub_once(
        src,
        r'if _is_claude_model\(body_json\.get\("model"\)\):',
        'if not _model_allowed(body_json.get("model")):',
        "发送拦截判断",
    )

    # 4) 把原来那段「禁 Claude」的 403 响应体换掉
    src = sub_once(
        src,
        r'            return JSONResponse\(\n'
        r'                status_code=403,\n'
        r'                content=\{\n'
        r'                    "error": \{\n'
        r'                        "message": f"Open WebUI 已禁用 Claude 系列模型\. '
        r'选 chatgpt-\* / gpt-\* / gemini-\* / glm-\* 等替代\.",\n'
        r'                        "type": "claude_blocked",\n'
        r'                        "code": "claude_blocked",\n'
        r'                    \}\n'
        r'                \},\n'
        r'            \)',
        '            return _model_blocked_response(body_json.get("model"))',
        "禁 Claude 的 403 响应体",
    )

    # 5) 删掉现在没人调的 _is_claude_model
    src = sub_once(
        src,
        r'\ndef _is_claude_model\(model: str \| None\) -> bool:\n'
        r'    return bool\(model and CLAUDE_MODEL_RE\.search\(model\)\)\n',
        "\n",
        "_is_claude_model 定义",
    )

    src = src.replace("ALLOWED_MODELS_PLACEHOLDER", ALLOW_BLOCK.rstrip("\n"))

    # 改完自查：不许有残留引用
    for dead in ("_is_claude_model", "CLAUDE_MODEL_RE", "claude_blocked"):
        if dead in src:
            sys.exit(f"改完还残留 {dead} —— 不写盘，退出")
    print("  残留检查 OK：_is_claude_model / CLAUDE_MODEL_RE / claude_blocked 都没了")

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(src)
    print(f"  已写 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
