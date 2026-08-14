# litellm-callbacks · regression tests

Self-contained regression tests for the Python files under
`k8s/litellm-callbacks/`.

The hooks live inline in the `litellm-callbacks` ConfigMap inside
`k8s/litellm-proxy.yaml` and are also kept at
`k8s/litellm-callbacks/<name>.py` for development. These tests exercise
the bare `.py` files without a real LiteLLM install — they stub out the
LiteLLM modules at import time so the tests are runnable on any laptop
with just Python + `httpx`.

## Run

```bash
cd k8s/litellm-callbacks/tests
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest test_streaming_bridge_done_filter -v
```

Expected: all tests pass in well under 10 seconds.

## Files

| File | Covers |
|---|---|
| `test_streaming_bridge_done_filter.py` | OpenAI-style `data: [DONE]` SSE residue filter inside `streaming_bridge.py`. Includes pure-function unit tests, end-to-end hook scenarios, an exhaustive split-position sweep across the `[DONE]` tail, and pathological 1/3/7/31/33-byte fragmentation runs. |
| `test_error_sanitize.py` | 对外报错脱敏 + 路由头假名化（`error_sanitize.py`）。钉住四条实测泄漏不出网（cooldown_list / api_base / traceback / 路由头），同时钉住三件不能被顺手打掉的东西：`status_code`/`type`/`code` 保留、假名"同 deployment 同值不同则不同"（换常量占位符会让 affinity 探针恒真）、以及入库那份 `error_information.error_message` 仍是原文。 |
| `test_deepseek_responses_group_rewrite.py` | `/v1/responses` 入口把裸名 `deepseek-v4-flash/pro` 改写到官方 `-responses` 组（`deepseek_responses_adapt.py`，让 Codex 免装 DeepSeek models.json）。钉住：chat 调用不改写、已是 -responses 名不改写、`DEEPSEEK_RESPONSES_REWRITE=off` 一键停用、不就地污染入参 dict。 |

> 表格历史上只列了一个文件，实际目录里有十几个 `test_*.py`；新增时顺手补一行。

## Adding a new hook test

1. Drop a new `test_<hook_name>.py` in this directory.
2. Re-use the `_install_litellm_stubs()` pattern at the top of the
   existing test to satisfy `import litellm.*` without a real install.
3. Set any env-var gates the hook reads **before** importing the
   bridge module (Python evaluates env vars at module load time for
   most of these hooks).
4. Use `importlib.util.spec_from_file_location("<module>", _PATH)` to
   load the hook directly from the `.py` file in the parent dir — do
   not rely on PYTHONPATH.
5. Drive the hook through `asyncio.run()` with a synthetic chunk
   sequence; assert on the bytes that reach the client.

## Why this exists

Some bugs in this layer (e.g. the `data: [DONE]` cross-chunk leak)
only surface under unlucky TCP fragmentation that staging traffic
rarely reproduces. The exhaustive split-position sweep in
`AllChunkSplitPositionsTest` was the test that caught the boundary
leak that broke `acpx`/`openclaw`. Keep that style of test for any
future stream-mutating hook.
