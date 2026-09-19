---
name: litellm-sse-bare-response-fix
description: >-
  诊断和修复 Codex CLI/Desktop "stream disconnected before completion: stream closed 
  before response.completed" + Reconnecting 1/5…5/5 的问题。根因：ChatGPT 上游
  （acct pod）发送裸 response 对象（无 type 字段），LiteLLM 的 gap filler 透传了
  但客户端需要 type:"response.completed"。修复方式：在 streaming_iterator.py 的
  _ResponsesLifecycleGapFiller.expand() 中注入 bare response handler。
  适用场景：用户报告 Codex 反复重连、"stream closed before response.completed"、
  SSE 流缺少 response.completed 事件、chatgpt-acct 池 Responses API 流格式异常。
metadata:
  requires:
    bins: ["kubectl", "ssh", "docker"]
  related_skills:
    - litellm-pro-ops
    - litellm-fix-or-feature
    - chatgpt-pool-model-variant
  related_memories:
    - feedback_litellm_vanilla_vs_patch_image
    - project_gpt_5_6_deployed_198_aliyun_2026_07_10
---

# LiteLLM SSE 裸 Response 对象修复

## 问题描述

Codex CLI/Desktop 通过 LiteLLM proxy 调用 ChatGPT 上游（chatgpt-acct pod）时，
SSE 流中出现裸 response 对象：

```json
data: {"id":"resp_xxx","object":"response","status":"completed","model":"chatgpt-gpt-5.6-sol","output":[...]}
```

而标准 Responses API 要求的格式是：

```json
data: {"type":"response.completed","response":{"id":"resp_xxx","object":"response",...}}
```

Codex 的 Rust SSE 引擎依赖 `type:"response.completed"` 判断流结束。
缺少 `type` 字段时，Codex 认为流意外断开，报错：
```
stream disconnected before completion: stream closed before response.completed
```
然后自动重试（Reconnecting 1/5…5/5），最多 5 次。

## 根因分析

### 事件流对比

**正常 provider（如 OpenAI 直连）**：
```
response.created → response.in_progress → output_item.added → 
content_part.added → output_text.delta(s) → output_text.done → 
content_part.done → output_item.done → response.completed
```

**ChatGPT acct pod 实际发送**：
```
output_text.delta(s) → {"object":"response","status":"in_progress",...} →
output_text.delta(s) → {"object":"response","status":"completed",...}
```

中间插入的裸对象没有 `type` 字段。LiteLLM 的 `_ResponsesLifecycleGapFiller.expand()`
收到事件后会检查 `event.type`（即 `etype`），裸对象的 `etype` 是 `None`（Pydantic
`model_construct()` 绕过验证创建），不匹配任何已知事件类型，直接走 fallback
`return (event,)` 原样透传。

### 关键代码路径

```
客户端请求 → LiteLLM proxy → chatgpt-acct pod（上游）
                ↓ SSE chunk
        _process_chunk() → transform_streaming_response()
            → GenericEvent.model_construct(**bare_dict)  # type=None
                ↓
        _lifecycle_gap_filler.expand(event)
            → etype = event.type  # None
            → 不匹配任何分支
            → return (event,)  # 原样透传，客户端看不到 response.completed
```

### 为什么用 model_construct

`GenericEvent` 的 `type: str` 是 required 字段。裸对象没有 `type`，
`GenericEvent(**bare_dict)` 会抛 Pydantic `ValidationError`。
LiteLLM 用 `model_construct()` 绕过验证，创建出 `type=None` 的实例，
额外字段（`object`, `status` 等）存在 `__pydantic_extra__` 里。

## 修复方案

在 `expand()` 的 `return (event,)` fallback 前，加一段检测逻辑：

1. 如果 `etype` 不是字符串（即 None / 缺失）
2. 且 `event.object == "response"` 且 `event.status` 是已知状态
3. 则包装为标准 lifecycle 事件并递归 `self.expand()`

```python
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
```

递归调用 `self.expand(wrapped)` 让包装后的事件走正常的 lifecycle 补全逻辑
（补 `response.created` / `response.in_progress` 前置 opener，
以及 `response.completed` 前的 teardown `*.done` 事件）。

## 操作步骤

### Hot-patch（临时，pod 重启丢失）

```bash
# 脚本位置: scripts/litellm-patch-streaming-bare-handler.py

# 1. scp 到 198
sshpass -p '<PWD>' scp scripts/litellm-patch-streaming-bare-handler.py cltx@10.68.13.198:/tmp/

# 2. 在 198 上 kubectl cp 到每个 pod 并执行
echo '<PWD>' | sudo -S sh -c '
for POD in $(kubectl -n litellm-product get pods -l app=litellm-proxy -o name | sed "s|pod/||"); do
  kubectl -n litellm-product cp /tmp/litellm-patch-streaming-bare-handler.py $POD:/tmp/patch.py
  kubectl -n litellm-product exec $POD -- python3 /tmp/patch.py
done
'

# 3. 杀 worker 进程（关键！LiteLLM 用 multiprocessing.spawn，不是 gunicorn）
echo '<PWD>' | sudo -S sh -c '
for POD in $(kubectl -n litellm-product get pods -l app=litellm-proxy -o name | sed "s|pod/||"); do
  PIDS=$(kubectl -n litellm-product exec $POD -- ps aux 2>/dev/null | \
    grep "multiprocessing.spawn" | grep -v grep | awk "{print \$1}")
  if [ -n "$PIDS" ]; then
    kubectl -n litellm-product exec $POD -- kill -9 $PIDS
    echo "$POD: killed workers $PIDS"
  fi
done
'
```

### 永久修复（烘进镜像）

```bash
# 1. 从已 patch 的 pod 提取文件
BUILDDIR=/root/litellm-product/sse-fix-bare-$(date +%Y%m%d-%H%M%S)
mkdir -p $BUILDDIR
POD=$(kubectl -n litellm-product get pods -l app=litellm-proxy -o name | head -1 | sed 's|pod/||')
kubectl -n litellm-product cp $POD:/app/.venv/lib/python3.13/site-packages/litellm/responses/streaming_iterator.py \
  $BUILDDIR/streaming_iterator.py

# 2. 复制已有的 capacity patch
cp /root/litellm-product/rebase-capacity-v1.90.2-235542/exception_mapping_utils.v1.90.2.py $BUILDDIR/

# 3. 写 Dockerfile
cat > $BUILDDIR/Dockerfile <<EOF
FROM 127.0.0.1:5000/litellm-carher:vanilla-v1.90.2
COPY exception_mapping_utils.v1.90.2.py /app/.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py
COPY streaming_iterator.py /app/.venv/lib/python3.13/site-packages/litellm/responses/streaming_iterator.py
EOF

# 4. 构建 + push + 滚动部署
TAG=vanilla-v1.90.2.capacity.sse-fix-bare-$(date +%Y%m%d-%H%M%S)
cd $BUILDDIR
docker build -t 127.0.0.1:5000/litellm-carher:$TAG -f Dockerfile .
docker push 127.0.0.1:5000/litellm-carher:$TAG
kubectl -n litellm-product set image deployment/litellm-proxy litellm=127.0.0.1:5000/litellm-carher:$TAG
kubectl -n litellm-product rollout status deployment/litellm-proxy --timeout=180s
```

## 验证

```bash
# 5 次 SSE 测试，全部应有 response.completed
for i in 1 2 3 4 5; do
  RESULT=$(curl -sN --max-time 30 https://cc.auto-link.com.cn/pro/v1/responses \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \   # 先 export，别把 key 写进文件
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-5.6-sol","input":[{"role":"user","content":"hi"}],"stream":true}')
  COMPLETED=$(echo "$RESULT" | grep -c '"response.completed"' || true)
  echo "Test $i: completed=$COMPLETED"
done
# 期望: 全部 completed=1
```

## 陷阱清单

### 1. Worker 进程重启方式

LiteLLM proxy 用 `multiprocessing.spawn`（不是 gunicorn）。
`pkill gunicorn` 不会匹配任何进程，需要 `kill -9` spawn worker 的 PID。
Master 进程（PID 1）会自动 respawn 新 worker。

```bash
# 查 worker PID
kubectl exec $POD -- ps aux | grep 'multiprocessing.spawn'
```

### 2. .pyc 缓存陈旧

`kubectl cp` 保留源文件原始 mtime。如果 pod 里已有更新的 `.pyc`（mtime 更大），
Python 会继续用旧 `.pyc`，忽略新 `.py`。补丁脚本已自动删除 `.pyc` 并 touch `.py`。

手动修复：
```bash
kubectl exec $POD -- find /app/.venv -name 'streaming_iterator*.pyc' -delete
kubectl exec $POD -- touch /app/.venv/.../streaming_iterator.py
```

### 3. input 必须是 list

ChatGPT 上游要求 `input` 为 list 格式，字符串会返回 400：
```json
{"input": [{"role":"user","content":"hi"}]}  # 正确
{"input": "hi"}                               # 400 "Input must be a list"
```

### 4. 双路径注意

裸对象的 `object` 和 `status` 可能在 Pydantic 模型的正常属性上，
也可能在 `__pydantic_extra__` 字典里（因为 `model_construct()` 绕过验证）。
补丁代码同时检查两个位置。

## 时间线

| 日期 | 事件 |
|------|------|
| 2026-07-11 | 发现根因：裸 response 对象缺 type 字段 |
| 2026-07-11 | Hot-patch 4 pod + 杀 multiprocessing worker |
| 2026-07-11 | 烘进镜像 `vanilla-v1.90.2.capacity.sse-fix-bare-20260711-122004` |
| 2026-07-11 | 滚动部署 4 pod，5/5 SSE 测试通过 |
