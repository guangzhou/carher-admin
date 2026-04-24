---
name: litellm-hook-dev
description: >-
  开发 LiteLLM 自定义 CustomLogger hook —— pre-call（请求改写 / 脏数据清洗 /
  schema 转换）或 post-call streaming iterator（SSE 心跳 / TTFT 打点 / 迭代器
  monkey-patch）—— 并用独立 canary Deployment 或 env-var gate 做灰度验证。
  Use when the user mentions "litellm" + 加 hook / callback / 改写请求 /
  sanitize / schema fix / pre-call / 流式 hook / heartbeat / 524 / SSE /
  TTFT，or when a new request transformation or streaming-time behavior
  needs to be injected (e.g. rewrite thinking schema, strip bad unicode,
  keepalive pings, stamp completion_start_time, patch an upstream
  iterator class).
---

# 开发 LiteLLM CustomLogger Hook + 灰度

## 现有 hook 参考

| 文件 | 作用 | Hook 类型 | Gate 方式 |
|---|---|---|---|
| `k8s/litellm-callbacks/opus_47_fix.py` | 把老的 `thinking.type=enabled` / `reasoning_effort` 改写成 opus-4-7+ 的 adaptive schema + 强制 streaming | `async_pre_call_hook` (chat/completion) | `call_type` + 模型名前缀 |
| `k8s/litellm-callbacks/embedding_sanitize.py` | 清洗 embedding input 里的 lone UTF-16 surrogate（Node.js bot 脏数据防御）| `async_pre_call_hook` (embedding) | `call_type` |
| `k8s/litellm-callbacks/streaming_bridge.py` | 1) 全局 monkey-patch `BaseAnthropicMessagesStreamingIterator.__init__` 修正 `startTime`（所有 `anthropic_messages` 请求）<br>2) SSE 心跳防 Cloudflare 524 + 首个 `content_block_delta` 打 `completion_start_time` 修 TTFT（按 `key_alias` 前缀 gate）| `async_post_call_streaming_iterator_hook` + 模块级 monkey-patch | `STREAMING_BRIDGE_KEY_PREFIXES` / `STREAMING_BRIDGE_KEY_ALIASES` env |

**所有 hook 挂到同一个 ConfigMap `litellm-callbacks` 里，写在 `litellm_settings.callbacks` 列表里按顺序执行。**

## Hook 类型选择指南

| 想做什么 | 选什么 hook |
|---|---|
| 改写 request body（参数、schema、内容清洗） | `async_pre_call_hook` |
| 注入 request 之外的行为（限额、审计、拒绝） | `async_pre_call_hook` 里 `raise` |
| 修改 / 观察 streaming 响应字节流，注入心跳 | `async_post_call_streaming_iterator_hook` |
| 在流式结束时打点某个 metric（TTFT、first-content） | `async_post_call_streaming_iterator_hook` 内 stamp `logging_obj._update_completion_start_time(...)` |
| 修正 LiteLLM 内部类行为（logging 字段不对 / 时钟源不对） | **模块级 monkey-patch**（在 py 文件最后调用一次 `_patch_xxx()`），通过 ConfigMap 一并加载 |
| 不需要改 request，只想观测 | 挂 `async_log_success_event` / `async_log_failure_event` |

## Hook 代码骨架

继承 `CustomLogger`，实现 `async_pre_call_hook`：

```python
# 文件：k8s/litellm-callbacks/<module_name>.py
# 例如 embedding_sanitize.py -> 模块名就是 embedding_sanitize
from litellm.integrations.custom_logger import CustomLogger
import litellm

class MyHook(CustomLogger):
    _CALL_TYPES = frozenset({"completion", "acompletion"})  # 或 embedding/aembedding

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            if not isinstance(data, dict): return data
            if call_type not in self._CALL_TYPES: return data

            # 改写 data（推荐就地改）；如果确实做了修改，可以加一行 verbose log：
            # data["some_field"] = new_value
            # try: litellm.print_verbose("[my_hook] rewrote some_field")
            # except Exception: pass

        except Exception as exc:
            # 任何异常都不能让用户请求失败 —— 原样放行
            try: litellm.print_verbose(f"[my_hook] ERROR: {exc!r}")
            except Exception: pass
        return data


# 模块级实例名（`callbacks: ["<module>.<instance>"]` 里的 <instance>）
my_hook_instance = MyHook()
```

**在 config 里引用**：`callbacks: ["<module_name>.<instance_name>"]`，例如：
- `embedding_sanitize.embedding_sanitize`（模块和实例重名也可以）
- `opus_47_fix.thinking_schema_fix`（模块 / 实例名可不同，看 py 文件里 `<name> = <Class>()` 那行）

关键原则：
- **绝不抛异常**给调用方（try/except 吞掉所有 exception）
- **必须过滤 `call_type`**，避免误伤非目标请求（embedding hook 不要碰 chat，反之亦然）
- **就地修改 data**（改 dict 字段即可），不要返回新 dict
- **`litellm.print_verbose` 只在 `LITELLM_LOG=DEBUG` 时输出**，排查时用 `kubectl set env deploy/litellm-proxy -n carher LITELLM_LOG=DEBUG` 临时打开

## 完整开发流程（6 步）

### Step 1: 写 hook + 本地单元测试

```python
# 在 /tmp/test_hook.py 或直接内联 python3 <<'PY' ... PY 里验证核心逻辑
# 不依赖 litellm，只测纯函数/正则
```

**必须覆盖**：正常输入、边界输入、异常输入、副作用（日志）。

### Step 2: 写文件到 `k8s/litellm-callbacks/<name>.py`

与 ConfigMap 内嵌版保持一致；注释里写清 "keep two in sync"。

### Step 3: 建 canary（独立 Deployment + Service，不影响主流量）

4 个临时资源（名字都加 `-canary` 后缀）：

| 资源 | 作用 |
|---|---|
| `cm/litellm-config-canary` | 复制 `litellm-config`，在 `litellm_settings.callbacks` 列表追加新 hook |
| `cm/litellm-callbacks-canary` | 复制 `litellm-callbacks`，加入新 py 文件 |
| `deploy/litellm-proxy-canary`（1 副本）| 和主 Deployment 同 image 同 env，但 volumes 引用 canary CM、labels 改 `app=litellm-proxy-canary` |
| `svc/litellm-proxy-canary` | selector 用 `app=litellm-proxy-canary`，内部访问点 |

```bash
# 1. 派生 canary config
kubectl get cm litellm-config -n carher -o jsonpath='{.data.config\.yaml}' > /tmp/canary.yaml
sed -i.bak 's|callbacks: \[\(.*\)\]|callbacks: [\1, "<module>.<instance>"]|' /tmp/canary.yaml

# 2. 构造完整 yaml：ConfigMap × 2 + Deployment + Service
#    参考模板：git log --diff-filter=D -- k8s/litellm-canary.yaml  (若历史有)
#    或 copy 主 deploy yaml 改 name/labels/CM 引用
#    关键点：Deployment selector & labels 都是 app=litellm-proxy-canary
#           主容器多挂一个 /app/<new_hook>.py volumeMount

kubectl apply -f /tmp/litellm-canary.yaml
kubectl rollout status deploy/litellm-proxy-canary -n carher --timeout=240s
```

**核心模板骨架**（根据实际参数替换）：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: litellm-proxy-canary
  namespace: carher
spec:
  replicas: 1
  selector:
    matchLabels: {app: litellm-proxy-canary}
  template:
    metadata:
      labels: {app: litellm-proxy-canary}
    spec:
      # 直接复用 k8s/litellm-proxy.yaml 里主 Deployment 的 spec.template.spec
      # 只改：
      #   - volumes: 把 config/callbacks 的 configMap.name 改成 -canary 后缀
      #   - volumeMounts 里加 /app/<new_hook>.py subPath mount
---
apiVersion: v1
kind: Service
metadata: {name: litellm-proxy-canary, namespace: carher}
spec:
  selector: {app: litellm-proxy-canary}
  ports: [{port: 4000, targetPort: 4000, name: http}]
```

### Step 4: 四格对比测试（严格灰度验证）

| Test | 路径 | 输入 | 预期 |
|---|---|---|---|
| A | 主 svc | 能触发 bug 的 payload | **仍然失败**（对照组确认 bug 真实）|
| B | canary svc | 同上 payload | **成功**（确认 fix 有效）|
| C | canary svc | 正常 payload | **成功**（确认无副作用）|
| D | 主 svc | 正常 payload | **成功**（无 regression 基线）|

只有 A=失败、B/C/D=成功，才允许继续。

```bash
MK=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
# 用临时 curl pod 测（主 svc + canary svc 分别打）
kubectl run ck --image=curlimages/curl:latest --restart=Never -n carher --quiet --rm -i --command -- \
  curl -sS -o /dev/null -w "%{http_code}\n" -X POST "http://<svc>.carher.svc:4000/v1/<endpoint>" \
  -H "Authorization: Bearer ${MK}" -H "Content-Type: application/json" -d @payload.json
```

### Step 5: 合并到主 `k8s/litellm-proxy.yaml`

3 处改动：
1. `litellm-callbacks` ConfigMap 的 `data` 下新增 `<name>.py: |` + 缩进内容
2. `litellm-config` ConfigMap 的 `callbacks: [...]` 列表追加 `<module>.<instance>`
3. 主容器 `volumeMounts` 下新增：
   ```yaml
   - name: callbacks
     mountPath: /app/<name>.py
     subPath: <name>.py
     readOnly: true
   ```

apply + rollout restart（主 Deployment 是双副本 + `maxUnavailable=0` + preStop sleep 15 + grace 60s → 零中断）：

```bash
kubectl apply -f k8s/litellm-proxy.yaml
kubectl rollout restart deploy/litellm-proxy -n carher
kubectl rollout status deploy/litellm-proxy -n carher --timeout=600s
```

### Step 6: 清理 canary + commit + push

```bash
kubectl delete -f /tmp/litellm-canary.yaml
cd <repo> && git add -p k8s/litellm-proxy.yaml  # 精挑 hunks（避开无关改动）
git add k8s/litellm-callbacks/<name>.py
git commit -m "feat(litellm): <描述>" && git push origin main
```

## 把 ConfigMap 文本用 Python 嵌入（避免 heredoc 陷阱）

`k8s/litellm-proxy.yaml` 里的 ConfigMap data 字段是内联 YAML 多行字符串，缩进 4 空格：

```python
with open('k8s/litellm-callbacks/<name>.py') as f:
    py = f.read()
indented = '\n'.join('    ' + l for l in py.splitlines())
# 插入位置：`thinking_schema_fix = ThinkingSchemaFix()\n---` 之前
```

## 回滚预案

| 方式 | 命令 |
|---|---|
| Git revert | `git revert <sha> && kubectl apply -f k8s/litellm-proxy.yaml && kubectl rollout restart deploy/litellm-proxy -n carher` |
| 临时禁用（保留代码，不执行）| `kubectl edit cm litellm-config -n carher`，把 callbacks 列表里的 `<module>.<instance>` 删掉 → rollout restart |
| Canary 阶段止损 | `kubectl delete -f /tmp/litellm-canary.yaml`（从没接入主流量，无影响）|

## 注意事项

- **永远不能让 hook 抛未捕获的异常**——所有 `except` 都要覆盖，否则 bot 请求会 500
- **pre-call hook 改不了 streaming 注入**——`stream_options.include_usage` 要走 `general_settings.always_include_stream_usage`，不是 pre-call hook
- **Hook 改 data 是就地修改**（直接修改传入的 dict），不要返回新 dict
- **Hook 顺序执行**：如果多个 hook 操作同一字段，注意 callbacks 列表顺序

## Post-call streaming iterator hook 模式

### 用途

包一层 async 生成器，在 SSE 字节流穿过 LiteLLM 代理时做额外动作。典型场景：

- **SSE 心跳注入**：防止长思考期间 Cloudflare/反向代理的 idle timeout（~100s）。LiteLLM 自己不发 keepalive。
- **精确 TTFT 打点**：对于 `anthropic_messages`（passthrough）路径，LiteLLM 不会 set `completion_start_time`，会 fallback 到 `endTime` → TTFT ≡ Duration。解法是在看到首个 `content_block_delta`（Anthropic SSE 中代表首个用户可见 token 的事件）时调用 `logging_obj._update_completion_start_time(datetime.now())`。
- **响应观测**：统计首字节延迟、chunk 大小分布、错误事件频率等。

### 骨架

```python
from litellm.integrations.custom_logger import CustomLogger
import asyncio, datetime

class MyStreamingBridge(CustomLogger):
    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data,
    ):
        # 判断是否要包一层（gate：key_alias / call_type / model ...）
        logging_obj = request_data.get("litellm_logging_obj") if isinstance(request_data, dict) else None
        if not self._should_bridge(user_api_key_dict, request_data):
            async for item in response:  # 原样透传
                yield item
            return

        # === 包裹上游 iterator ===
        upstream = response.__aiter__()
        heartbeat_seconds = 25.0
        first_content_seen = False
        _CONTENT_DELTA = b"content_block_delta"

        while True:
            next_task = asyncio.ensure_future(upstream.__anext__())
            try:
                done, pending = await asyncio.wait(
                    {next_task}, timeout=heartbeat_seconds,
                )
                if not done:
                    yield b": keepalive\n\n"  # SSE comment = 忽略事件
                    continue
                try:
                    item = next_task.result()
                except StopAsyncIteration:
                    return

                # 观察字节流，打 TTFT
                if not first_content_seen and isinstance(item, (bytes, bytearray)):
                    if _CONTENT_DELTA in item:
                        first_content_seen = True
                        try:
                            logging_obj._update_completion_start_time(datetime.datetime.now())
                        except Exception:
                            pass
                yield item
            finally:
                if not next_task.done():
                    next_task.cancel()

my_streaming_bridge = MyStreamingBridge()
```

### 坑

1. **upstream item 的类型**：passthrough 路径通常是 `bytes`，但 `acompletion` 路径是 `ModelResponseStream` 对象。包之前 print 一次看看类型。
2. **心跳内容**：SSE 协议里 `:` 开头的行是 comment，客户端会忽略但会重置 idle 计时器。`b": keepalive\n\n"` 是最低干扰方案。**不要** 发自造的 data 事件（会让客户端 parser 吐 warning）。
3. **chunk 会 bundle**：实测 Anthropic/Wangsu 会把 HTTP headers + `message_start` + 首个 `content_block_delta` 合并到同一个 TCP chunk。想打 TTFT 必须扫 `content_block_delta`，不能打 "第一个非空 chunk"（那个 chunk 此时其实是 headers 到达的瞬间）。
4. **`response` 可能是生成器也可能是对象带 `__aiter__`**，`response.__aiter__()` 都能工作，但 `async for ... in response` 在外层包层里要先判断是否真的是 async iterable。
5. **gate 一定要在最前**：只要不想包的分支，直接 `async for ... yield`，不要进入复杂逻辑。否则会增加延迟、还可能把 `acompletion` 路径的流式 chunk 破坏。

### Env-var gate 模式

比 pre-call hook 直接写死常量更灵活。支持 alias 精确列表 + prefix 前缀列表两种：

```python
import os
_DEFAULT_ALIASES = frozenset({"claude-code-xxx"})  # 兜底 canary，仅在两个 env 都未设置时生效

def _load_aliases():
    raw = os.environ.get("MY_HOOK_KEY_ALIASES")
    if raw is None:
        if os.environ.get("MY_HOOK_KEY_PREFIXES") is None:
            return set(_DEFAULT_ALIASES)
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}

def _load_prefixes():
    raw = os.environ.get("MY_HOOK_KEY_PREFIXES")
    if raw is None: return ()
    return tuple(x.strip() for x in raw.split(",") if x.strip())
```

Deployment env 里这样配：

```yaml
env:
  - name: MY_HOOK_KEY_ALIASES
    value: ""                        # 显式空，避免默认 canary 自动回填
  - name: MY_HOOK_KEY_PREFIXES
    value: "claude-code-"            # 前缀匹配覆盖整个 cohort
```

渐进灰度路径：
1. 无 env → 默认 canary 一个人
2. 加 `MY_HOOK_KEY_ALIASES=a,b,c` → 手工扩几人
3. 全量 → 清空 ALIASES、用 `MY_HOOK_KEY_PREFIXES=claude-code-` 一把过
4. 回滚 → `kubectl set env deploy/litellm-proxy -n carher MY_HOOK_KEY_PREFIXES- MY_HOOK_KEY_ALIASES-`（瞬间恢复默认 canary）

### 模块级 monkey-patch 模式

用来**修 LiteLLM 自身**（比如 `BaseAnthropicMessagesStreamingIterator.__init__` 用的时钟源不对，导致 `startTime` 偏晚）。在 py 文件末尾做一次 patch：

```python
def _patch_xxx():
    try:
        from litellm.llms.anthropic.experimental_pass_through.messages.streaming_iterator \
            import BaseAnthropicMessagesStreamingIterator as Cls
    except Exception:
        return
    orig = Cls.__init__
    if getattr(orig, "_my_patched", False):
        return  # 防二次 patch（模块可能被重复 import）
    def patched(self, logging_obj, request_body):
        self.litellm_logging_obj = logging_obj
        self.request_body = request_body
        t = getattr(logging_obj, "start_time", None)
        self.start_time = t if isinstance(t, datetime.datetime) else datetime.datetime.now()
    patched._my_patched = True
    Cls.__init__ = patched

_patch_xxx()  # ConfigMap 加载 callbacks 时即生效
```

**关键点**：
- 必须做 idempotent 判断（`_my_patched` 标志），否则 reload 会叠加 patch 变成无限递归
- monkey-patch 是**全局生效**的，不能按 key gate，所以只适合用于「**修正所有人的错误行为**」
- patch 失败必须吞异常并打 warn，不能让 callbacks module import 失败拖垮整个 proxy

## 案例：streaming_bridge (2026-04-24)

- **问题 1（startTime 偏晚）**：`anthropic_messages` 路径下 `BaseAnthropicMessagesStreamingIterator.__init__` 里 `self.start_time = datetime.now()` 是在收到上游 HTTP headers **之后**才执行的，而 `LiteLLM_SpendLogs.startTime` 就拿这个值 → 比真实 proxy 入口时刻晚 0.5~10s
- **问题 2（TTFT ≡ Duration）**：`anthropic_messages` 路径全程不 set `completion_start_time`，fallback 到 `endTime`，导致 SpendLogs 里 TTFT 永远等于 Duration。对比 `acompletion` 路径有 `CustomStreamWrapper` 正确 stamp 所以 carher 实例正常
- **问题 3（524 超时）**：Cloudflare Tunnel 对外部 client 有 ~100s idle 超时，Opus 4.7 长思考期间上游不吐任何字节 → 间歇 524。内部 carher bot 走 ClusterIP 不经 Cloudflare 所以无感
- **修复**：
  - monkey-patch `BaseAnthropicMessagesStreamingIterator.__init__`，把 `self.start_time` 改用 `logging_obj.start_time`（全局，修所有 `anthropic_messages`）
  - 新 `StreamingBridge(CustomLogger)` 实现 `async_post_call_streaming_iterator_hook`，25s 发 SSE comment keepalive，首个 `content_block_delta` 打 `completion_start_time`（按 key prefix gate，初期只给 claude-code-*）
- **灰度**：`STREAMING_BRIDGE_KEY_ALIASES=claude-code-liuguoxian-50gj` → `,claude-code-buyitian` → 最终 `STREAMING_BRIDGE_KEY_PREFIXES=claude-code-` 全量（286 个 key）
- **验证**：过去 24h `anthropic_messages` 请求 ~30k 条，healthy_ttft 比例从 ~0% 涨到 ≥ 99%，同时观测到 214s 的超长请求完整落地（心跳防住 Cloudflare 524）
- **commit**：`719018e feat(litellm): stream TTFT fix + Cloudflare 524 keepalive for claude-code-*`

## 案例：embedding_sanitize (2026-04-21)

- **问题**：bot 向 `bge-m3` 发送含 lone UTF-16 surrogate 的 text，Python httpx UTF-8 encode 失败 → HTTP 500 → fallback 找不到 → HTTP 404
- **影响**：4.5% 的 bge-m3 调用失败（10 min 窗口 639 成功 / 30 失败），涉及 ~8 个活跃实例
- **修复**：canary 验证 A/B/C/D 四格通过 → 合并到主 → rollout restart → 自然流量失败率归 0
- **耗时**：hook 开发 + canary + 合并 + 清理 ≈ 40 min
- **commit**：`7f584fc feat(litellm): sanitize lone surrogates from embedding inputs`

## 案例：opus_47_fix (早先)

同 pattern，改写 legacy thinking schema + force streaming。参考 `k8s/litellm-callbacks/opus_47_fix.py` 源码。

## 相关 skill

- LiteLLM Proxy 整体运维 → [litellm-ops](../litellm-ops/SKILL.md)
- 零中断 rollout 主 Deployment 细节 → [carher-k8s-zero-downtime-rollout](../carher-k8s-zero-downtime-rollout/SKILL.md)
- memorySearch / bge-m3 相关的 hook 场景 → [carher-memorysearch-config](../carher-memorysearch-config/SKILL.md)
