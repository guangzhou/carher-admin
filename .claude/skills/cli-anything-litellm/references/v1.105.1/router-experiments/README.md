# Router 离线实验（v1.105.1）

`01-router.md` 里标「实测」的数字都来自这里。真 `litellm.Router` + `mock_response=<异常>`，真的失败日志/冷却回调都会跑；不联网。

需要一棵 v1.105.1 源码树（不在 git 里）：

```bash
W=.work/litellm-src-study-1020 && mkdir -p $W/src
ssh cltx@10.68.13.198 'sudo -n git -C /root/litellm-fork archive v1.105.1 litellm' | tar -x -C $W/src
cd $W/src && PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=. \
  <python3.12 且装了 litellm 依赖> /path/to/router-experiments/01-router-exp.py
```

| 脚本 | 测什么（取自各脚本 docstring） |
|---|---|
| `01-router-exp.py` | 每次上游调用计数（按组/腿），看重试、兜底、退避、冷却实际怎么走 |
| `01-router-exp2.py` | 把 live-cb 的 dead_deployment_retry / encrypted_content_degrade_strip / chatgpt_responses_normalize / streaming_output_backfill / weighted_affinity 叠在 1.105.1 上跑，看各自装没装、边界情况 |
| `01-router-exp3.py` | 跟踪 `run_async_fallback` 每次调用的深度、列表、已试集合 |
| `01-router-exp4.py` / `exp4b.py` | weighted_affinity 钉住的腿回 400/500 时，和 1.105 的「重试跳过失败台」怎么配合，每枪落在哪 |
| `01-router-exp5.py` | `fallbacks` / `model_group_retry_policy` 按 alias 查找；`context_window_fallbacks` 的门控 |
| `01-router-exp6.py` | `ResponsesAPIStreamingIterator._handle_failure` 上游版 vs 我们补丁版各自触达哪些失败处理器（冷却回归的离线证据） |
| `01-router-exp7.py` | 生产规模组（A=40 腿兜底到 B=20、C=10）全失败时一个请求打多少次 |

每个脚本开头的 docstring 写了它的前提和读数方法。
