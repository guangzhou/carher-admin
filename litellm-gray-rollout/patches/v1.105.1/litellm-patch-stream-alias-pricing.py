#!/usr/bin/env python3
"""
LiteLLM 上游 #44341 回移（1.103.3 才有，1.105.1 没有）：流式请求少计费。

问题：proxy 在每个流式 chunk 上把 model 原地改成客户端请求的别名
（`_restamp_streaming_chunk_model`）。流包装器拿的是同一批 chunk 对象来拼最终 response
并计价 ⇒ 计价时看到的是别名而不是 deployment 的真实模型 ⇒ 别名没价或价低时少计费。

上游修法（逐行照搬 dabf756927，不加任何自创逻辑）：
  1) proxy_server._restamp_streaming_chunk_model：改成给客户端一个拷贝（dict 解包 / model_copy），
     原 chunk 保留 provider 的 model 供计价；同时把别名记到 logging_obj.client_facing_stream_model。
  2) litellm_logging.Logging：新增 client_facing_stream_model 字段与 _with_client_facing_stream_model，
     计价之后、生成 standard_logging_object 之前，把记录用的 response 换成「model=别名」的拷贝，
     ⇒ SpendLogs 的 model 列照旧是别名，只有 response_cost 变成按真实 deployment 计。

用法：python3 litellm-patch-stream-alias-pricing.py [site-packages/litellm 目录]
幂等：已打过（有 client_facing_stream_model）就 SKIP 返回 1；任何锚点次数≠期望 ⇒ 整体不写返回 2。
"""

import glob
import os
import sys

ROOT = "/app/.venv/lib/python3.13/site-packages/litellm"
MARKER = "client_facing_stream_model"

LOGGING_EDITS = [
    (
        "logging-init",
        1,
        "        self.completion_start_time: datetime.datetime | None = None\n",
        "        self.completion_start_time: datetime.datetime | None = None\n"
        "        # The model the proxy shows the client on streamed chunks. The logged streamed response carries it\n"
        "        # once that response is priced, the same way a non-streamed response is logged\n"
        "        self.client_facing_stream_model: str | None = None\n",
    ),
    (
        "logging-helper",
        1,
        "        return result\n\n    def _success_handler_helper_fn(\n",
        "        return result\n\n"
        "    def _with_client_facing_stream_model(\n"
        "        self,\n"
        "        response: ModelResponse | TextCompletionResponse | ResponsesAPIResponse | InteractionsAPIResponse,\n"
        "    ) -> ModelResponse | TextCompletionResponse | ResponsesAPIResponse | InteractionsAPIResponse:\n"
        "        model: Final = self.client_facing_stream_model\n"
        "        if model is None or getattr(response, \"model\", None) in (None, model):\n"
        "            return response\n"
        "        return response.model_copy(update={\"model\": model})\n\n"
        "    def _success_handler_helper_fn(\n",
    ),
    (
        "logging-sync-success",
        1,
        "                self._merge_hidden_params_from_response_into_metadata(complete_streaming_response)\n"
        "                ## STANDARDIZED LOGGING PAYLOAD\n"
        "                self.model_call_details[\"standard_logging_object\"] = self._build_standard_logging_payload(\n"
        "                    complete_streaming_response, start_time, end_time\n"
        "                )\n",
        "                self._merge_hidden_params_from_response_into_metadata(complete_streaming_response)\n"
        "                logged_streaming_response: Final = self._with_client_facing_stream_model(complete_streaming_response)\n"
        "                self.model_call_details[\"complete_streaming_response\"] = logged_streaming_response\n"
        "                ## STANDARDIZED LOGGING PAYLOAD\n"
        "                self.model_call_details[\"standard_logging_object\"] = self._build_standard_logging_payload(\n"
        "                    logged_streaming_response, start_time, end_time\n"
        "                )\n",
    ),
    (
        "logging-async-success",
        1,
        "            await self._prepare_baseline_cache_estimate(complete_streaming_response)\n"
        "\n"
        "            ## STANDARDIZED LOGGING PAYLOAD\n"
        "            try:\n"
        "                self.model_call_details[\"standard_logging_object\"] = self._build_standard_logging_payload(\n"
        "                    complete_streaming_response, start_time, end_time\n"
        "                )\n",
        "            await self._prepare_baseline_cache_estimate(complete_streaming_response)\n"
        "\n"
        "            logged_streaming_response: Final = self._with_client_facing_stream_model(complete_streaming_response)\n"
        "            self.model_call_details[\"async_complete_streaming_response\"] = logged_streaming_response\n"
        "\n"
        "            ## STANDARDIZED LOGGING PAYLOAD\n"
        "            try:\n"
        "                self.model_call_details[\"standard_logging_object\"] = self._build_standard_logging_payload(\n"
        "                    logged_streaming_response, start_time, end_time\n"
        "                )\n",
    ),
]

PROXY_EDITS = [
    (
        "proxy-restamp",
        1,
        "        model_mismatch_logged = True\n"
        "\n"
        "    if isinstance(chunk, dict):\n"
        "        chunk[\"model\"] = target_model\n"
        "        return chunk, model_mismatch_logged\n"
        "\n"
        "    try:\n"
        "        chunk.model = target_model\n"
        "    except Exception as e:\n",
        "        model_mismatch_logged = True\n"
        "\n"
        "    # The streaming wrapper keeps these same chunk objects to assemble the response it\n"
        "    # prices, so stamp a copy for the client and leave the provider's model for pricing.\n"
        "    # The logging object stamps the same model on the assembled response after pricing it.\n"
        "    logging_obj: Final = request_data.get(\"litellm_logging_obj\")\n"
        "    if isinstance(logging_obj, LiteLLMLoggingObj):\n"
        "        logging_obj.client_facing_stream_model = target_model\n"
        "    if isinstance(chunk, dict):\n"
        "        return {**chunk, \"model\": target_model}, model_mismatch_logged\n"
        "\n"
        "    try:\n"
        "        return chunk.model_copy(update={\"model\": target_model}), model_mismatch_logged\n"
        "    except Exception as e:\n",
    ),
]

FILES = [
    ("litellm_core_utils/litellm_logging.py", LOGGING_EDITS),
    ("proxy/proxy_server.py", PROXY_EDITS),
]


def main(root: str) -> int:
    loaded = []
    for rel, edits in FILES:
        path = os.path.join(root, rel)
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        if MARKER in content:
            print(f"SKIP: {MARKER} already present in {path}")
            return 1
        loaded.append((path, content, edits))

    bad = [
        (name, content.count(old), want)
        for _path, content, edits in loaded
        for name, want, old, _new in edits
        if content.count(old) != want
    ]
    # proxy_server 里必须已有 LiteLLMLoggingObj / Final 这两个名字，否则补丁会 NameError
    proxy_content = loaded[1][1]
    for needed in ("LiteLLMLoggingObj", "Final"):
        if needed not in proxy_content:
            bad.append((f"name:{needed}", 0, ">=1"))
    if bad:
        for name, n, want in bad:
            print(f"ERROR: anchor {name!r} matched {n} times (want {want})")
        print("未写入任何内容。base 版本换代后需要人工重定锚点。")
        return 2

    for path, content, edits in loaded:
        for _name, _want, old, new in edits:
            content = content.replace(old, new, 1)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        stem = os.path.splitext(os.path.basename(path))[0]
        for pyc in glob.glob(os.path.join(os.path.dirname(path), "__pycache__", f"{stem}*.pyc")):
            os.remove(pyc)
        print(f"OK: #44341 applied to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ROOT))
