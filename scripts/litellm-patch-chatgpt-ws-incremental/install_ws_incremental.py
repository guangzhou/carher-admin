#!/usr/bin/env python3
"""
acct 多账户链路 —— WS 增量传输补丁 installer（幂等）。

做两件事：
  1) 把同目录的 ws_transport.py 装到 litellm 的 chatgpt/responses/ 下。
  2) 在 llm_http_handler.py :: async_response_api_handler 里，pre_call 之后、
     `try:/if stream:` 之前，插一段锚定分支调用 try_ws_incremental。

结构性安全属性（硬约束 1）：分支对一切非成功收 None → 落原生 HTTP POST，字节级不变；
外层 429/换号/冷却逻辑字节级同今天。补丁本身 + 网关默认 OFF ⇒ 装了不开 = 行为不变。

锚定函数名 / 代码形状，绝不用行号（in-pod v1.89 vs 本地 HEAD 行号会漂）。
清 __pycache__ + os.utime 保证重载。

⚠ acct pod 现实（2026-08-23 实测，颠覆了原 Phase 0 计划）：
  - acct pod 是 **num_workers=1 单进程**（PID 1 = `litellm --config ... --port 4000`，
    无 multiprocessing.spawn worker）。→ "杀 worker 让 on-disk 补丁重载" **对 acct pod
    无效**：live PID 1 早已把 llm_http_handler 模块导入内存，改盘上文件不重载；重启
    PID 1 = 容器重启 = 跑回 stock 镜像抹掉补丁。
  - acct pod 内存限 2Gi，litellm 进程静息就吃 ~1.4Gi；在容器里再起第二个 litellm
    进程（想用它 fresh 导入补丁）**实测秒级 OOMKill 整个 pod**（cgroup 2×1.4Gi>2Gi）。
  → 结论：**Phase 0 hot-patch canary 对 acct pod 不成立**。canary 只能走烘镜像
    （Phase 1），用一个**专属 pod**（manifest 里抬内存到 3Gi + 设 CHATGPT_WS_INCREMENTAL=1）。
  本 installer 仍是 Phase 1 烘镜像时在 build 阶段 RUN 的装配器（见下）。

pck 传参已由源码坐实（2026-08-23，acct pod v1.90.2 transformation.py）：
  transform 的 allowed_keys 含 "prompt_cache_key" → 出站 data 携带 pck（客户端带时，
  Codex CLI 每会话必带）→ _resolve_pck 扫 data 命中。非 Codex 无 pck → HTTP 兜底。

永久化 / canary（Phase 1，唯一可行路径）：docker build FROM 现役 base，RUN 本 installer
  在镜像内装配，tag vanilla-v<VER>.cache-session-fix-v2.ws-incr-<YYYYMMDD-HHMMSS>，
  push 127.0.0.1:5000。镜像本身**行为中性**（网关默认 OFF）；canary pod 才 set env。
  Dockerfile 骨架：
    FROM 127.0.0.1:5000/litellm-carher:vanilla-v1.90.2.cache-session-fix-v2-20260817-103630
    COPY ws_transport.py install_ws_incremental.py /tmp/
    RUN python3 /tmp/install_ws_incremental.py && rm -f /tmp/install_ws_incremental.py

回滚（秒级）：canary deployment `set image` 指回 acct-stable + 去掉 CHATGPT_WS_INCREMENTAL
  env（Recreate 拉起 stock）。全程零 schema / 零 PVC / 零 CM 改动。
"""

import glob
import os
import re
import sys

MARKER = "[carher-ws-incr]"

# 注入分支：pre_call 之后、`try:/if stream:` 之前。缩进 8 空格（函数体内）。
INJECT = '''        # [carher-ws-incr] acct 多账户链路：服务端合成 WS 增量传输（默认 OFF，见 ws_transport.py）。
        # 对一切非成功返 None → 落下面原生 HTTP POST，字节级不变；异常同样吞掉走 HTTP。
        if stream and not fake_stream and custom_llm_provider == "chatgpt":
            try:
                from litellm.llms.chatgpt.responses.ws_transport import (
                    try_ws_incremental as _carher_try_ws_incremental,
                )

                _carher_ws_it = await _carher_try_ws_incremental(
                    data=data,
                    headers=headers,
                    api_base=api_base,
                    model=model,
                    logging_obj=logging_obj,
                    responses_api_provider_config=responses_api_provider_config,
                    litellm_metadata=litellm_metadata,
                    custom_llm_provider=custom_llm_provider,
                    request_context=request_context,
                )
                if _carher_ws_it is not None:
                    return _carher_ws_it
            except Exception:
                pass

'''


def _discover_litellm_root() -> str:
    """返回 litellm 包根目录（含 llms/、responses/）。"""
    # 1) import 定位（最可靠）
    try:
        import litellm  # type: ignore
        root = os.path.dirname(os.path.abspath(litellm.__file__))
        if os.path.isdir(os.path.join(root, "llms")):
            return root
    except Exception:
        pass
    # 2) glob site-packages（python3.x 版本无关）
    for pat in (
        "/app/.venv/lib/python3.*/site-packages/litellm",
        "/usr/lib/python3.*/site-packages/litellm",
        "/usr/local/lib/python3.*/site-packages/litellm",
    ):
        hits = sorted(glob.glob(pat))
        for h in hits:
            if os.path.isdir(os.path.join(h, "llms")):
                return h
    raise SystemExit("ERROR: cannot locate litellm package root")


def _purge_pyc(filepath: str) -> None:
    pyc_dir = os.path.join(os.path.dirname(filepath), "__pycache__")
    if os.path.isdir(pyc_dir):
        base = os.path.splitext(os.path.basename(filepath))[0]
        for pyc in glob.glob(os.path.join(pyc_dir, base + "*.pyc")):
            try:
                os.remove(pyc)
                print(f"  purged {pyc}")
            except Exception:
                pass
    os.utime(filepath, None)


def install_module(litellm_root: str) -> None:
    """装 ws_transport.py 到 chatgpt/responses/。"""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ws_transport.py")
    if not os.path.exists(src):
        # hot-patch 时两文件都在 /tmp
        alt = "/tmp/ws_transport.py"
        if os.path.exists(alt):
            src = alt
        else:
            raise SystemExit(f"ERROR: ws_transport.py not found next to installer or at {alt}")
    dst_dir = os.path.join(litellm_root, "llms", "chatgpt", "responses")
    if not os.path.isdir(dst_dir):
        raise SystemExit(f"ERROR: {dst_dir} missing (chatgpt provider not present in this build)")
    dst = os.path.join(dst_dir, "ws_transport.py")
    with open(src, "r") as f:
        code = f.read()
    with open(dst, "w") as f:
        f.write(code)
    _purge_pyc(dst)
    print(f"OK: installed ws_transport.py -> {dst}")


def patch_handler(litellm_root: str) -> bool:
    """在 async_response_api_handler 内注入分支。幂等。"""
    target = os.path.join(litellm_root, "llms", "custom_httpx", "llm_http_handler.py")
    if not os.path.exists(target):
        raise SystemExit(f"ERROR: {target} not found")
    with open(target, "r") as f:
        content = f.read()

    if MARKER in content:
        print(f"SKIP: {MARKER} already present in {target}")
        return False

    # 1) 界定 async_response_api_handler 函数体（def 起点 → 下一个同级 def）。
    m_def = re.search(r"\n    async def async_response_api_handler\(", content)
    if not m_def:
        raise SystemExit("ERROR: async_response_api_handler def not found (shape drift?)")
    span_start = m_def.start()
    m_next = re.search(r"\n    (?:async def|def) \w+\(", content[m_def.end():])
    span_end = m_def.end() + (m_next.start() if m_next else len(content) - m_def.end())
    fn = content[span_start:span_end]

    # 2) 函数体内锚定：`\n        try:\n            if <is_stream_request|stream>:`（首次出现）。
    #    vanilla HEAD 用 `if stream:`；198 carher 镜像(cache-session-fix-v2, v1.90.2)用
    #    `if is_stream_request:`。两形状都吃，锚在 try 上方插分支。
    m_anchor = re.search(r"\n        try:\n            if (?:is_stream_request|stream):", fn)
    if not m_anchor:
        raise SystemExit("ERROR: `try:/if <is_stream_request|stream>:` anchor not found inside async_response_api_handler")

    # 3) 校验前置局部变量在作用域内（防止锚到错误位置）。
    pre = fn[: m_anchor.start()]
    for need in ("request_context", "logging_obj.pre_call(", "api_base =", "data =", "headers ="):
        if need not in pre:
            raise SystemExit(f"ERROR: expected `{need}` before anchor; refusing to patch (shape drift?)")

    # 4) 注入：在锚点 `\n` 之后、`        try:` 之前插 INJECT。
    insert_at_in_fn = m_anchor.start() + len("\n")
    new_fn = fn[:insert_at_in_fn] + INJECT + fn[insert_at_in_fn:]
    content = content[:span_start] + new_fn + content[span_end:]

    with open(target, "w") as f:
        f.write(content)
    _purge_pyc(target)
    print(f"OK: injected {MARKER} branch into {target}")
    return True


def main() -> int:
    litellm_root = sys.argv[1] if len(sys.argv) > 1 else _discover_litellm_root()
    print(f"litellm root: {litellm_root}")
    install_module(litellm_root)
    patch_handler(litellm_root)
    print("DONE. Now: touch /app/chatgpt_ws_incremental.flag + kill multiprocessing.spawn worker.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
