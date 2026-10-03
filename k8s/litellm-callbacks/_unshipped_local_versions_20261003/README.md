# 2026-10-03 本地未上线版本（保留，不是生产在跑的）

`k8s/litellm-callbacks/` 下同名文件已换成 198 生产 `litellm-proxy` 实际挂载的版本（逐字节）。
这两份是当时工作区里的版本，和线上**互有对方没有的改动**，不能直接覆盖，存在这里待人工合并：

| 文件 | 本地独有 | 线上独有 |
|---|---|---|
| `codex_custom_tool_bridge.py` | 改走 `async_post_call_streaming_iterator_hook` 官方钩子（注释称 patch `__anext__` 实测零命中） | 仍用 patch `LiteLLMCompletionStreamingIterator.__anext__` |
| `cursor_web_fc_sys_rewrite.py` | `_VERSION="v2"`、`_TARGET_PREFIX`、标签剥离正则 4 行 | 79 行（线上更长） |

合并前先确认线上那版在 1.103.2 上是否真的生效（本地注释说 `__anext__` 那条路零命中）。
