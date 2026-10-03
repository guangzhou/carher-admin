# 2026-10-03 本地未上线版本（保留，不是生产在跑的）

`k8s/litellm-callbacks/` 下同名文件已换成 198 生产 `litellm-proxy` 实际挂载的版本（逐字节）。
这两份是当时工作区里的版本，和线上**互有对方没有的改动**，不能直接覆盖，存在这里待人工合并：

| 文件 | 本地独有 | 线上独有 |
|---|---|---|
| `codex_custom_tool_bridge.py` | 改走 `async_post_call_streaming_iterator_hook` 官方钩子（注释称 patch `__anext__` 实测零命中） | 仍用 patch `LiteLLMCompletionStreamingIterator.__anext__` |
| `cursor_web_fc_sys_rewrite.py` | `_VERSION="v2"`、`_TARGET_PREFIX`、标签剥离正则 4 行 | 79 行（线上更长） |

合并前先确认线上那版在 1.103.2 上是否真的生效（本地注释说 `__anext__` 那条路零命中）。

## 2026-10-03 核实结论

`codex_custom_tool_bridge.py` 本地版注释「patch `__anext__` 装上了也永不触发（实测探针零命中）」**不成立**：
在 1.103.2 gray pod 内探针命中 17 次，且事件被正确改成 custom_tool_call。真正的缺陷在别处
（deepseek 上游对 custom `apply_patch` 回 function_call，见 commit af3a16a）。**本地这份 bridge 不要合并。**
