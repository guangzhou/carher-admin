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
