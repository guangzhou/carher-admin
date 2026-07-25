#!/usr/bin/env python3
"""
LiteLLM streaming_iterator.py — bare response handler 补丁。

问题：ChatGPT 上游（acct pod）在 SSE 流中发送裸 response 对象：
  {"id":"resp_xxx","object":"response","status":"completed",...}
而非标准 Responses API lifecycle 事件：
  {"type":"response.completed","response":{...}}

Codex CLI/Desktop 的 Rust 引擎需要 type="response.completed" 才认为流结束，
缺失时报 "stream closed before response.completed" 然后 Reconnecting 1/5…5/5。

修复：在 _ResponsesLifecycleGapFiller.expand() 的 fallback return 前，
检测 "object":"response" + "status" 但无 "type" 的裸对象，自动包装为标准事件。

用法：
  # 1) scp 到 198
  scp scripts/litellm-patch-streaming-bare-handler.py cltx@10.68.13.198:/tmp/

  # 2) 在 198 上对每个 pod 执行
  for POD in $(kubectl -n litellm-product get pods -l app=litellm-proxy -o name | sed 's|pod/||'); do
    kubectl -n litellm-product cp /tmp/litellm-patch-streaming-bare-handler.py $POD:/tmp/patch.py
    kubectl -n litellm-product exec $POD -- python3 /tmp/patch.py
  done

  # 3) 杀 worker 进程让新代码生效（LiteLLM 用 multiprocessing.spawn，非 gunicorn）
  for POD in $(kubectl -n litellm-product get pods -l app=litellm-proxy -o name | sed 's|pod/||'); do
    PIDS=$(kubectl -n litellm-product exec $POD -- \
      ps aux | grep 'multiprocessing.spawn' | grep -v grep | awk '{print $1}')
    kubectl -n litellm-product exec $POD -- kill -9 $PIDS
  done

  # 4) 验证
  curl -sN https://cc.auto-link.com.cn/pro/v1/responses \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"model":"gpt-5.6-sol","input":[{"role":"user","content":"hi"}],"stream":true}' \
    | grep -c 'response.completed'
  # 期望: 1

注意：此为 hot-patch，pod 重启会丢失。永久修复需烘进镜像：
  docker build -t 127.0.0.1:5000/litellm-carher:<new-tag> -f Dockerfile .
  其中 Dockerfile COPY streaming_iterator.py 到
    /app/.venv/lib/python3.13/site-packages/litellm/responses/streaming_iterator.py

镜像 tag 约定：vanilla-v<VER>.capacity.sse-fix-bare-<YYYYMMDD-HHMMSS>
"""

import re
import sys
import os

TARGET = "/app/.venv/lib/python3.13/site-packages/litellm/responses/streaming_iterator.py"

BARE_HANDLER = '''
        # --- Bare response objects (no "type" but has "object":"response" + "status") ---
        # Some upstreams (e.g. chatgpt-acct pods) emit the raw response object instead
        # of wrapping it in {"type":"response.<status>","response":{...}}.
        # Codex CLI requires "type" field; without it "response.completed" never arrives,
        # triggering "stream closed before response.completed" retries.
        if not isinstance(etype, str):
            _obj = _obj_get(event, "object")
            if _obj is None:
                _obj = getattr(getattr(event, "__pydantic_extra__", None) or {}, "get", lambda k,d=None: d)("object")
            _status = _obj_get(event, "status")
            if _status is None:
                _status = getattr(getattr(event, "__pydantic_extra__", None) or {}, "get", lambda k,d=None: d)("status")
            if _obj == "response" and isinstance(_status, str):
                _BARE_STATUS_MAP = {
                    "in_progress": ev.RESPONSE_IN_PROGRESS,
                    "completed": ev.RESPONSE_COMPLETED,
                    "failed": ev.RESPONSE_FAILED,
                    "incomplete": ev.RESPONSE_INCOMPLETE,
                }
                _mapped = _BARE_STATUS_MAP.get(_status)
                if _mapped is not None:
                    wrapped = BaseLiteLLMOpenAIResponseObject.model_construct(type=_mapped, response=event)
                    return self.expand(wrapped)
'''


def apply_patch(filepath: str) -> bool:
    with open(filepath, "r") as f:
        content = f.read()

    if "_BARE_STATUS_MAP" in content:
        print(f"SKIP: bare handler already present in {filepath}")
        return False

    # Find the final "return (event,)" in the expand() method
    # It's the fallback at the end of all the elif chains
    pattern = r'(        return \(event,\)\n\n    def _response_openers)'
    match = re.search(pattern, content)
    if not match:
        # Try alternative pattern without double newline
        pattern = r'(        return \(event,\)\n\n    def _response_openers)'
        match = re.search(pattern, content)
    if not match:
        # Last resort: find the return (event,) right before _response_openers
        lines = content.split('\n')
        insert_idx = None
        for i, line in enumerate(lines):
            if line.strip() == 'return (event,)' and i + 1 < len(lines):
                # Check if next non-blank line is _response_openers
                for j in range(i + 1, min(i + 5, len(lines))):
                    if '_response_openers' in lines[j]:
                        insert_idx = i
                        break
                if insert_idx is not None:
                    break
        if insert_idx is None:
            print(f"ERROR: cannot find insertion point in {filepath}")
            return False
        lines.insert(insert_idx, BARE_HANDLER.rstrip())
        content = '\n'.join(lines)
    else:
        content = content[:match.start()] + BARE_HANDLER + "        return (event,)\n\n    def _response_openers" + content[match.end():]

    with open(filepath, "w") as f:
        f.write(content)

    # Delete .pyc cache to force recompilation
    pyc_dir = os.path.join(os.path.dirname(filepath), "__pycache__")
    if os.path.isdir(pyc_dir):
        import glob
        for pyc in glob.glob(os.path.join(pyc_dir, "streaming_iterator*.pyc")):
            os.remove(pyc)
            print(f"  deleted: {pyc}")

    # Touch the .py to ensure mtime > any remaining .pyc
    os.utime(filepath, None)

    print(f"OK: bare handler injected into {filepath}")
    return True


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else TARGET
    if not os.path.exists(target):
        print(f"ERROR: {target} not found")
        sys.exit(1)
    success = apply_patch(target)
    sys.exit(0 if success else 1)
