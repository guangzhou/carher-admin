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
  iterator class)，or when an existing hook 明明注册了却不生效 / 零触发 /
  日志里看不到 / 只在 fallback 兜底路径上不生效。
---

# 开发 LiteLLM CustomLogger Hook + 灰度

> **作用域：阿里云 ns `carher`**（`-n carher`、仓库内 `k8s/litellm-proxy.yaml`、允许 `kubectl apply`）。
> **要在 198（ns `litellm-product`）母 router 上装东西，用 [[litellm-198-router-patch]]** ——
> 那边是三步安装（CM + **独立 subPath volumeMount** + config.yaml），**禁 `kubectl apply`**，
> 漏 volumeMount 会静默失效；改路由决策本身（重试/拉黑/挑腿）也在那边讲。

> **写之前先看两节**：「call_type 门控的致命陷阱」和「Hook 生效范围：入口 vs 部署」。
> 2026-08-03 这两处各让我白跑一轮生产 rollout，且都是**静默失效**（hook 注册成功、
> 文件挂载成功、零报错，就是不干活）。

## 现有 hook 参考

| 文件 | 作用 | Hook 类型 | Gate 方式 |
|---|---|---|---|
| `k8s/litellm-callbacks/opus_47_fix.py` | 把老的 `thinking.type=enabled` / `reasoning_effort` 改写成 opus-4-7+ 的 adaptive schema + 强制 streaming | `async_pre_call_hook` (chat/completion) | `call_type` + 模型名前缀 |
| `k8s/litellm-callbacks/embedding_sanitize.py` | 清洗 embedding input 里的 lone UTF-16 surrogate（Node.js bot 脏数据防御）| `async_pre_call_hook` (embedding) | `call_type` |
| `k8s/litellm-callbacks/mock_heartbeat.py` | 短路 OpenClaw `[OpenClaw heartbeat poll]` 心跳，设 `data["mock_response"]="ok"` 不打上游零计费 | `async_pre_call_hook` (responses/completion) | 内容 marker + `MOCK_HEARTBEAT_DISABLED` env |
| `k8s/litellm-callbacks/deepseek_responses_adapt.py` | 让 deepseek 接住 Codex / gpt 形状的 Responses 请求：**提升 `input` 里的 `additional_tools` 到顶层 `tools`**（Codex responses-lite 形态，不提升则 deepseek 收到零工具→把调用写成文本）+ custom tool 名归一到 `apply_patch`（其余转 function）+ 补 `reasoning.content[reasoning_text]` | **`async_pre_call_deployment_hook`**（兜底路径唯一可靠挂点）+ pre_call + pre_routing | model 名含 `deepseek` |
| `k8s/litellm-callbacks/deepseek_id_prefix.py` | 出站：给裸 UUID item id 补 `fc_`/`rs_` 前缀 + **SSE 字节层**把降级过的 custom tool 还原成 `custom_tool_call`（客户端声明 custom，收到 function_call 认不出→工具执行不了）| monkey-patch `BaseResponsesAPIStreamingIterator._process_chunk` + `proxy_server._format_streaming_sse_chunk` | `_is_deepseek` + contextvar 隔离 |
| `k8s/litellm-callbacks/streaming_bridge.py` | 1) 全局 monkey-patch `BaseAnthropicMessagesStreamingIterator.__init__` 修正 `startTime`（所有 `anthropic_messages` 请求）<br>2) SSE 心跳防 Cloudflare 524 + 首个 `content_block_delta` 打 `completion_start_time` 修 TTFT（按 `key_alias` 前缀 gate）<br>3) 出口流过滤 OpenRouter `data: [DONE]` 残留（带 32B carry-over 处理跨 chunk 边界）| `async_post_call_streaming_iterator_hook` + 模块级 monkey-patch | `STREAMING_BRIDGE_KEY_PREFIXES` / `STREAMING_BRIDGE_KEY_ALIASES` env |
| `k8s/litellm-callbacks/budget_notice.py`（**198 prod**）| key 日额度可见性三件套：① `/查余额` pre-call 短路（mock_response / ModifyResponseException）② 90% 预警注入流式最后一个 text block/part 内部（三路由：anthropic 字节流状态机 / responses pydantic 事件 / chat 对象流）③ 超预算**软拦截成 200 友好文案**——monkey-patch `_virtual_key_max_budget_check` 吞 BudgetExceededError + module mark + pre-call 出 mock（Cursor 吞 429 body，200 是唯一可见通道）| `async_pre_call_hook` + `async_post_call_streaming_iterator_hook` + **auth 层函数 monkey-patch** | `BUDGET_NOTICE_KEY_PREFIXES` / `BUDGET_NOTICE_DISABLED` / `BUDGET_FRIENDLY_MOCK_DISABLED` env |
| `k8s/litellm-callbacks/local_gpu_max_tokens_clamp.py`（**阿里云 ns carher**）| 把打到自建 local-gpu 盒子（36.151.241.10:8000）的 `max_tokens`/`max_completion_tokens`/`max_output_tokens` 钳到 65536。her catalog 按厂商规格发 384000，盒子 `max_model_len=393216` 且输入+输出一起算 → 不钳则真实会话全 400 回落官方，盒子接不到流量 | `async_pre_call_deployment_hook`（兜底路径也过）| deployment `api_base` 含盒子 IP |

**所有 hook 挂到同一个 ConfigMap `litellm-callbacks` 里，写在 `litellm_settings.callbacks` 列表里按顺序执行。**

## 测试沉淀位置

仓库里的回归测试落在 `k8s/litellm-callbacks/tests/`，纯 unittest + httpx，stub 掉 litellm imports，零 K8s 依赖：

```bash
cd k8s/litellm-callbacks/tests
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
```

新写一个 hook **必须**在这里加一份回归测试。模板参考 `test_streaming_bridge_done_filter.py`：
- 顶部 `_install_litellm_stubs()` 装好 `litellm` / `litellm.integrations.custom_logger` / `litellm.llms.*` 桩
- `os.environ[...]` **在 import 目标 hook 之前**设置好 gate（很多 hook 在模块 load 时读 env）
- `importlib.util.spec_from_file_location` 直接把 `../<hook>.py` 作为模块 load 进来，不依赖 PYTHONPATH
- 用 `asyncio.run` 驱动 streaming hook，喂自定义 chunk 序列，断言 client 端收到的 bytes

### ⚠️ 流式/monkey-patch 类改动：T0 必须带 prod 完整 callback 链（2026-08-20/21 两次实证）

单测 + 2-callback 精简链 T0 对这两类 bug 会**假阴性**：
- 流式管线 bug 取决于 hook 在链条里的**位置**（如 responses mock choke：守卫在
  精简链恰好是最内层有效，prod 29-callback 链里不在最内层照崩 500）；
- monkey-patch 与其它 callback 的 patch 有加载顺序交互。

正解：`scripts/litellm-198-t0-fullchain.sh` —— dump prod CM 全部 callback +
实时提取 prod config 的 callbacks 列表，只换待验文件，起同镜像 docker 栈。
回归套件放 `tests/t0_<hook>.py`（`t0_` 前缀不会被 unittest discover 捞），
参考 `tests/t0_budget_notice.py`（21 检查项，三路由 × 流/非流）。
上线后 prod 冒烟另写（svc IP + master key 从 secret 实时取），参考
`scripts/litellm-198-budget-notice-smoke.sh`。
坑：流式 body 是 SSE 帧且中文常被 `\uXXXX` 转义，断言前必须重组正文
（`all_text()` 只收 content/text/delta 字段），直接子串匹配假阴。

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

_CALL_TYPES = frozenset({"completion", "acompletion"})  # 或 embedding/aembedding/responses/aresponses


def _is_target_call(call_type) -> bool:
    """必须取 ``.value``。见下方「call_type 门控的致命陷阱」。"""
    v = getattr(call_type, "value", call_type)
    return str(v) in _CALL_TYPES


class MyHook(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            if not isinstance(data, dict): return data
            if not _is_target_call(call_type):
                return data

            # 改写 data（推荐就地改）；如果确实做了修改，可以加一行 verbose log：
            # data["some_field"] = new_value

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
- **必须过滤 `call_type`**，避免误伤非目标请求（embedding hook 不要碰 chat，反之亦然）——
  但**写法必须取 `.value`**，见下节
- **就地修改 data**（改 dict 字段即可），不要返回新 dict
- **`litellm.print_verbose` 只在 `LITELLM_LOG=DEBUG` 时输出**，排查时用 `kubectl set env deploy/litellm-proxy -n carher LITELLM_LOG=DEBUG` 临时打开

## ⚠️ call_type 门控的致命陷阱（2026-08-03 实测）

**`if call_type not in _CALL_TYPES` / `if str(call_type) in _CALL_TYPES` 都可能永远不命中。**

`CallTypes` 是 `class CallTypes(str, Enum)`，Python 3.13 下：

```
repr(c)  = <CallTypes.aresponses: 'aresponses'>
str(c)   = 'CallTypes.aresponses'      # ← 拿它比集合,必 False
c.value  = 'aresponses'                # ← 只有这个对
```

所以 `str(call_type) in {"responses","aresponses"}` **恒为 False**，hook 静默空跑。
198 prod 上 `chatgpt_responses_normalize.py` 的 `async_pre_call_hook` /
`async_pre_call_deployment_hook` 就是这么废掉的（它实际靠
`async_pre_routing_hook` 的 model 门控在工作）。

正确写法：`getattr(call_type, "value", call_type)` 后再比，兼容枚举与字符串。
responses 家族别漏 `_aresponses_websocket`（Codex 会先试 WebSocket）。

**门控必须放在日志之后，或者门控外也留一条日志。** 门控在日志前面时，没命中会
连一行输出都没有，「hook 没被调用」和「调用了但无需改写」**无法区分** —— 我因此
把「门控挡死」误读成「转换已生效」，白跑两轮 rollout。

## Hook 生效范围：入口 vs 部署（决定兜底路径能不能覆盖）

| Hook | 何时跑 | `data/kwargs["model"]` 是什么 | fallback 换目标后会再跑吗 |
|---|---|---|---|
| `async_pre_call_hook` | 请求**入口**，一次 | 原始 model group（如 `gpt-5.6-sol`）| **不会** |
| `async_pre_call_deployment_hook` | **选定 deployment 后、发请求前** | 实际 deployment（如 `openai/deepseek-v4-flash`）| **会** |
| `async_pre_routing_hook` | 路由前 | 原始 model group | 视实现 |

**任何「只对某个兜底目标生效」的转换，必须挂 `async_pre_call_deployment_hook`。**
只挂入口钩子 + model 门控 = 兜底路径 0 触发：入口时 model 还是主路径的 group，
被门控挡掉；router fallback 到目标时入口钩子已经不再执行。
实例见 `k8s/litellm-callbacks/deepseek_responses_adapt.py`。

## ⚠️ `litellm_params` 里的参数是 default 不是 override（2026-08-19 实测）

想钳制客户端超发的 `max_tokens`，在 deployment `litellm_params` 里写
`max_tokens: 65536` **没用**——客户端请求带了值就以客户端为准，配置只在
客户端没发时兜默认。要强制钳制只能走 hook（`async_pre_call_deployment_hook`
按 deployment `api_base` 门控），见 `k8s/litellm-callbacks/local_gpu_max_tokens_clamp.py`。

**验证钳制的探针必须是判别式的**：小输入 + `max_tokens=384000` 是**假证明**
（20+384000 < 盒子上限 393216，上游本来就收）。判别条件是构造
`输入 + 384000 > 上限 > 输入 + 65536`（例：~20k token 输入），然后看
`x-litellm-model-id` 落点：钳制生效=落目标 deployment；没生效=被
`context_window_fallbacks` 兜走落 fallback（HTTP 都是 200，**只看状态码分不出**）。
现成探针：`scripts/litellm-aliyun-her-dsflash-local-box.py probe`。

## ⚠️ `_log.info` 在 198 proxy 上看不见

litellm 的 logger 只输出 **WARNING 以上**。实测 `encrypted_content_degrade_strip`
的 `_log.warning` 可见，`chatgpt_responses_normalize` 的 `_log.info` 不可见。
**要观测 hook 是否真的在跑，必须用 `_log.warning`**，并且日志里带上判据字段：

```
deepseek_responses_adapt: source=pre_deployment:CallTypes.aresponses \
    model=openai/deepseek-v4-flash counts={'apply_patch_rename': 1}
```

`source=` 告诉你哪个钩子命中、`counts=` 告诉你实际改了什么。
**没有这行日志，就不能宣布 hook 生效** —— 上游行为可能本来就通，容易把巧合当成果。


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
#    ✅ 现成模板就在仓里：k8s/litellm-proxy-canary.yaml（Deployment + Service）
#                       k8s/litellm-proxy-canary-config.yaml（canary CM）
#    ⛔ 别再找 k8s/litellm-canary.yaml —— 这个名字从来没存在过（2026-09-20 核查全历史 0 次）
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

**快路径（2026-08-21 起首选，budget_notice 上线验证）**：`scripts/litellm-aliyun-callback-add.sh`
在 226 上一次做完集群侧三处外科变更（CM merge-patch 单 key + config.yaml 只加一行
diff 校验 + deploy strategic patch mount/env），并带 rollout 死锁检测。**跑完必须**
回来同步 repo `k8s/litellm-proxy.yaml` 四处（callbacks 列表 / CM 内嵌 py / volumeMount /
env），否则下次有人 apply 整包 yaml 会把 callback 抹掉。禁止用
`kubectl create cm --dry-run | apply` 全量重建 CM（会吃掉别人的补丁）。

手动路径三处改动：
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
- **新 hook 必须显式加 `volumeMounts` subPath**（参考主 deploy `force_stream.py` 同款），仅写 ConfigMap 不加 mount 启动直接 `ImportError: Could not import <hook> from <hook>` 全部 Pod CrashLoopBackOff。prod **和 canary** 两套 deploy 都要加
- **Prod rollout 死锁陷阱（aliyun carher litellm-proxy）**：deploy 用 `hostPort=4000` + `nodeAffinity` 锁 226/227/229 三节点，maxSurge=0 起新 Pod 时常因端口未释放 Pending，老 Pod 反而被 deployment 保留接流量 → 你以为新版生效，实际 spend log 还从老 Pod 走。改完 ConfigMap **必须** `kubectl get pods | grep litellm-proxy` 确认无 Pending / Terminating 卡死项；卡死时 force-delete 已 Terminating 的老 Pod（新 RS 至少 1 个 Ready 在另一节点保可用）。**2026-08-21 再次复现**（budget_notice 上线）：两老 pod 0/1 Terminating 卡住占 hostPort、新 pod Pending ~7min 不自愈，force-delete 后 20s 解锁。注意 carher ns 有零中断 PreToolUse hook 拦 `kubectl delete pod`——先人工核对三条件（老 pod 0/1 不服务 / 新 RS ≥1 Ready / Pending 仅因 hostPort），再按 hook 提示加 override 注释执行
- **改 /key/update spend 后 auth 缓存 ~60s 才失效**：冒烟里改 spend 再立刻断言 ②预警/③预算门必假败（请求带着旧 spend 直达上游）。改完 sleep 70 再打。①查余额不受影响（不读 spend 门槛）
- **冒烟模型要选接纯 chat 的**：aliyun 的 `wangsu-deepseek-v4-pro` 对 plain chat 400 `field messages is required`（网关只接特定形态）；`local-deepseek-v4-flash` 仅流式。稳定便宜选 `wangsu-glm-5.2`
- **canary deploy 跟 prod 用不同 ConfigMap（`litellm-config-canary`）**：很多模型不在 canary 注册，直接 smoke `gpt-5.5` 会回 `Invalid model name`；通用 hook 想在 canary smoke，先看 canary CM 的 `model_list` 选个真存在的

## Pre-call mock_response 短路模式

### 用途

某些请求**根本不该打上游**：典型如 OpenClaw heartbeat poll（270 实例每轮 ~50K prompt + 47 tools / ~$0.22/次，命中 tool_call 比例极低纯浪费）。pre-call hook 设 `data["mock_response"] = "..."`，LiteLLM 在 `litellm/responses/main.py::aresponses` / `litellm/main.py::acompletion` 里 **provider 解析前** 检测到这个字段就直接构造合成响应返回，零 upstream call、零 token 计费。

### 骨架

```python
class MockHeartbeat(CustomLogger):
    _MARKER = "[OpenClaw heartbeat poll]"
    _TARGET_CALL_TYPES = frozenset({"responses","aresponses","acompletion","completion"})

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            if os.environ.get("MOCK_HEARTBEAT_DISABLED") == "1": return data
            # ⚠️ 原文这里是 `if call_type not in self._TARGET_CALL_TYPES`。
            # 传进来是 CallTypes 枚举时该判断恒为「不在集合里」→ hook 空跑。
            # 见上文「call_type 门控的致命陷阱」，一律先取 .value：
            if str(getattr(call_type, "value", call_type)) not in self._TARGET_CALL_TYPES: return data
            if not isinstance(data, dict): return data
            # 关键：必须从尾向前找 last role=user，跳过 assistant/tool；
            # 心跳触发 tool_call 后回流的请求 last role 是 tool，但 last user 仍是 marker
            text = _last_user_text(data)
            if self._MARKER not in text: return data
            data["mock_response"] = "ok"
        except Exception: pass
        return data
```

`_last_user_text(data)` 必须同时支持两种 schema：
- **Responses API**：`data["input"]`——可能是 `str` 也可能是 `list[{role,content}]`，content 可能是 `str` 或 `list[{type,text|input_text}]` 多段
- **Chat completion**：`data["messages"]`——同上 content 可 str/list 多段

从尾向前遍历，**找到第一条 `role=="user"` 就停**，不能匹配 assistant/tool（否则像 `# HEARTBEAT.md` 这种 tool result 也会被误判）。

### 识别 mock 是否生效

LiteLLM 用静态 fixture：input_tokens=36 / output_tokens=87 / **total_tokens=123**。

- **硬指纹**：`usage.total_tokens == 123` AND `output[0].content[0].text == "ok"`
- **id 不可靠**：fixture id 是 `resp_67ccd2bed1ec8190b14f964abc0542670bb6a6b452d3795b`，但 LiteLLM proxy 会把它重 base64 编码成 `resp_fvgQXp4vqZwH...` / `resp_8iknZ7...` 之类的新 id，**前缀判 mocked 100% 假阴**

### 灰度方式

mock_response 短路场景不太适合独立 canary deploy 验证——本质是请求级判断，prod 直接挂 + env 总开关 `MOCK_HEARTBEAT_DISABLED=1` 兜底即可：

```bash
# 紧急关闭
kubectl -n carher set env deploy/litellm-proxy MOCK_HEARTBEAT_DISABLED=1
```

Per-key opt-out：客户端在 `litellm_metadata._skip_mock_heartbeat: true` 标记，hook 内部检查跳过。

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
6. **要修改字节流就必须做 carry-over**（见下一节）。
7. **client 端 SSE 协议宽容度**（见下一节）——同样的 wire bytes，不同 SDK 反应不一样，靠"客户端没报错"判断兼容性会漏 bug。

### 改字节流的"跨 chunk 边界"陷阱（必读）

如果 hook 要 **删除/重写** SSE 字节流里的某段（例如剥掉 OpenRouter 漏出来的 `data: [DONE]`、 改写错误的 event name、过滤敏感 token），**永远不能只用"逐 chunk 跑一次正则"**。原因：

- 上游每次 `anext()` 给你的是任意大小的 TCP chunk。LiteLLM / httpx 不保证 chunk 边界对齐 SSE 行边界。
- 实测完全合法的拆分位置：`b"data: [D"` + `b"ONE]\n\n"`、`b"event: dat"` + `b"a\ndata: [DONE]\n\n"`、甚至每 1 byte 一个 chunk（极端但合法）。
- 单 chunk 跑正则时，被切散的特征**没人能匹配上**，残骸照样吐给 client。

**通用模式 — 32 byte carry-over buffer**：

```python
_TAIL_KEEP: int = 32   # 略大于待匹配最长串（"event: data\ndata: [DONE]\n\n" = 26B）

egress_carry: bytes = b""
async for chunk in upstream:
    if not isinstance(chunk, (bytes, bytearray)):
        # 非 bytes 路径（例如 ModelResponseStream），先把 carry flush 出去
        if egress_carry:
            yield _strip(egress_carry)
            egress_carry = b""
        yield chunk
        continue

    merged = egress_carry + bytes(chunk)
    if len(merged) > _TAIL_KEEP:
        body = merged[:-_TAIL_KEEP]
        egress_carry = merged[-_TAIL_KEEP:]
    else:
        body, egress_carry = b"", merged
    cleaned = _strip(body)
    if cleaned:
        yield cleaned

# EOF: flush 剩余 carry
if egress_carry:
    flushed = _strip(egress_carry)
    if flushed:
        yield flushed
```

**关键不变量**：
- `_TAIL_KEEP` ≥ 你要匹配的最长串（含可选前缀和换行符），**留一点余量更稳**。我们用 32B 兜住 26B 的 `event: data\ndata: [DONE]\n\n`。
- 只对"已确认安全"的部分（`body`）跑正则；不安全的尾巴留到下一次合并。
- EOF 必须 flush 一次 carry，否则最后一段被吞。
- 如果 hook 同时在做 **观察**（例如打 TTFT），观察还是该看原始 `chunk`/`merged`（首个 `content_block_delta` 不会被任何过滤删掉），**只有写出 client 的字节流才走 body / carry**。

**怎么验证 carry 写对了**——在仓库 `tests/` 里写一个 split-position sweep：

```python
for split in range(len(prefix), len(prefix) + len(target_substring)):
    chunks = [wire[:split], wire[split:]]
    out = run_hook(chunks)
    assert TARGET not in out, f"leak at split={split}"
```

只有这个穷举测试通过，才能说 "跨边界" 这个维度真的覆盖了。 `test_streaming_bridge_done_filter.py::AllChunkSplitPositionsTest` 是参考。

### Anthropic SSE 严格 vs 宽容（client SDK 兼容性）

走 LiteLLM 的 `/v1/messages` (`anthropic_messages`) 时，上游 provider 的 SSE 不一定严格遵守 Anthropic 协议——特别是 OpenRouter 的 Anthropic-compat endpoint 会在 `message_stop` 之后再送一段 OpenAI 协议的 `event: data\ndata: [DONE]\n\n` 终结符。

不同 client SDK 对未知 SSE 的反应：

| Client | 行为 | 后果 |
|---|---|---|
| Claude Code 官方 SDK | 静默忽略未知 event / 非 JSON `data:` 行 | **没人发现** |
| acpx (`@acpx/api`) | 每个 `data:` 行都 `JSON.parse` | `Could not parse Anthropic SSE event data: Unexpected token 'D', "[DONE]" is not valid JSON` |
| openclaw 同底 | 同 acpx | 同上 |

**教训**："看起来 Claude Code 跑得好"≠ wire 上没有协议噪音。换个严格 SDK 就炸。**协议层兼容性必须在 LiteLLM 出口侧做净化**（不是寄希望于 client）。

### 客户端渲染约束：想让用户**看到**的文字只有一条通道（200 助手消息）

协议合法 / HTTP 层正确 ≠ 用户看得到。三条实测判据（都只能真实客户端验，
任何规范查不到）：

| 客户端行为 | 后果 | 对策 |
|---|---|---|
| **Cursor agent 收到 429 完全不渲染 body**，只显示自家 "exceeded retry limit" 串（2026-08-21 活体探针）| 精心写的友好 429 文案用户一个字看不到 | 拦截改成 **200 mock 助手消息**（budget_notice ③ 软拦截模式）|
| **Claude Code 只把 anthropic 流最后一个 text block 当结果**（2026-08-19 canary）| 追加独立尾 block 会顶掉真正的答案 | 注入必须进**已存在的最后 text block 内部** |
| **Cursor 把 `/` 开头输入当命令面板拦截**，根本不发给模型（2026-08-20 SpendLogs 实证）| `/查余额` 永远到不了 proxy | 触发词须支持裸词（`查余额`），匹配器剥 `<user_query>` 标签按行全等 |

通用结论：**任何"给用户递话"的需求（额度提醒、超限提示、运维公告），最终
载体只能是 200 的正常 assistant 消息**；错误通道（4xx/5xx body）、独立
content block、SSE comment 在主流客户端里全部不可见或被顶掉。
验收必须真实客户端跑一遍，wire 字节正确不算数。

判断责任的方法：
1. 拿到 client 报错——记录精确 error message。
2. `kubectl exec` 进 litellm-proxy pod 用 curl 直接打上游 `/v1/messages`（绕过 LiteLLM proxy 自己），抓 wire bytes：
   ```bash
   curl -sN -H "Authorization: Bearer $OR_KEY" -H "Content-Type: application/json" \
     https://openrouter.ai/api/v1/messages \
     -d '{"model":"anthropic/claude-opus-4.7","stream":true,"max_tokens":50,
          "messages":[{"role":"user","content":"hi"}]}' | hexdump -C | tail -5
   ```
3. 如果上游 wire 上就有非协议 bytes，责任在 provider，但兜底必须在 LiteLLM 这一层（我们没法改 OpenRouter）。

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
- monkey-patch 是**全局生效**的，不能按 key gate，所以只适合用于「**修正所有人的错误行为**」——
  例外：patch 内部可以再按 gate 分流（budget_notice 的预算软拦截就是 patch 全局挂、
  函数体里按 `_gated(valid_token)` 决定吞异常还是原样 raise）
- patch 失败必须吞异常并打 warn，不能让 callbacks module import 失败拖垮整个 proxy

### ⚠️ patch 按名字 import 的函数：必须 patch **调用方命名空间**（2026-08-21 实测）

`from x import y` 会把 `y` **绑定成调用方模块的 global**，调用点在调用时从
**调用方的 globals** 解析。只替换源模块属性（`x.y = patched`）对已完成 import
的调用方**完全无效**。

实例：`_virtual_key_max_budget_check` 定义在 `litellm.proxy.auth.auth_checks`，
但 `user_api_key_auth.py` 顶部 `from ...auth_checks import _virtual_key_max_budget_check`
按名字引入、在 1885 行调用 —— 要拦它必须：

```python
for modpath in (
    "litellm.proxy.auth.user_api_key_auth",   # 调用方命名空间（关键）
    "litellm.proxy.auth.auth_checks",         # 源头（belt-and-suspenders，兜住未来新调用方）
):
    mod = __import__(modpath, fromlist=["_virtual_key_max_budget_check"])
    orig = getattr(mod, "_virtual_key_max_budget_check", None)
    if orig is None or getattr(orig, "_patched", False): continue
    setattr(mod, "_virtual_key_max_budget_check", _wrap(orig))
```

判据：patch 后日志应出现**两条** `patched ... in <modpath>`（每个命名空间一条）。
只 patch 源头时功能静默失效——请求照常走原函数，无任何报错。
反过来，patch **类的方法**（如 `ProxyLogging.async_post_call_streaming_iterator_hook`）
没有这个问题：方法查找走类属性，替换类属性即全局生效。

完整参考：`k8s/litellm-callbacks/budget_notice.py` 的 `_patch_virtual_key_budget()`。

## ⚠️ 改 Responses 流式事件：两层坑（2026-08-06 实测，各废一轮生产）

想改 Responses API 流式事件里的字段（item id、item 的 `type`、`item_id`）时，
**照搬 Chat Completions 的钩子经验会连踩两个静默失效**。

### 坑 1：钩子层拿到的是 Chat 形态，不是 Responses 事件

`async_post_call_streaming_iterator_hook` 收到的 chunk 是
`ModelResponseStream`（`object='chat.completion.chunk'`）。Responses 事件和它的
item id 是在**更下游**生成的。

**症状**：非流式钩子生效、流式完全不生效，日志里只有 `nonstream counts`。

**改挂**：monkey-patch
`litellm.responses.streaming_iterator.BaseResponsesAPIStreamingIterator._process_chunk`
—— 所有 Responses 流式路径（原生透传 / chat 转换）的单一收口。
model 从 `self.model` 或 `self.logging_obj.model` 取。

### 坑 2：事件字段在 `__pydantic_extra__`，不在 `__dict__`

- `OutputItemAddedEvent.item` 是 `BaseLiteLLMOpenAIResponseObject`，模型声明
  `extra="allow"`，上游多出来的 `id` / `call_id` **全落在 `__pydantic_extra__`**
- 各类 delta 事件是 `GenericEvent`，`__dict__` 里**只有 `type`**，`item_id` 也在 extra

只判 `isinstance(x, dict)` 或只读 `__dict__` → 第一行就 return，`changed` 恒为
`False`，事件明明进来了却一个字节没改。

读要「**先 extra 后 `__dict__`**」，写要**写回字段原本所在那一层**。参考
`deepseek_id_prefix.py` 的 `_View` 视图类。

### 🔴 红线：绝不要改 pydantic 事件对象的 `type` 字段

改了会让序列化器失配，**`response.completed` 那一帧序列化时崩**：

```
PydanticSerializationError: Error calling function
`_serialize_output_filter_reasoning_nulls`:
TypeError: 'MockValSer' object is not an instance of 'SchemaSerializer'
```

**整条流断在最后一帧** —— 无 `response.completed`、无 `[DONE]`，比原故障严重得多。
pydantic 的 serializer 按声明类型缓存，改 `type` 等于换了个模型。

改 `id` 之类的**普通字段没问题**，只有 `type` 会炸。

要改 `type`（比如 `function_call` → `custom_tool_call`），必须做在 **SSE 字节层**：
patch `litellm.proxy.proxy_server._format_streaming_sse_chunk`（SSE 单一收口），
那时已经是纯 JSON 文本，不碰任何 pydantic 机制。参考
`deepseek_id_prefix.py` 的 `_restore_custom_in_sse`。

### 并发隔离：用 contextvars，别用模块级 dict

SSE 格式化函数是模块级自由函数，拿不到请求上下文。如果用全局字典传状态，
**198 常态 40+ 并发，会把一个模型的改写规则串到同时在跑的其它模型请求上**。

```python
_ACTIVE: "contextvars.ContextVar[tuple | None]" = contextvars.ContextVar(
    "xxx_active", default=None)
```

在有 model 门控的地方（如 `_process_chunk` 的 `_is_deepseek` 分支内）`set()`，
其它模型保持 `None`，patch 第一行就 return。

**验收必须带隔离对照组**：拿 2~3 个别的模型跑一遍，断言它们
「改写计数=0 且流完整」，不能只测目标模型。

### 验证：必须逐事件核对，不能只看 `response.completed`

`response.completed` 里的 `output[]` 和逐个 `output_item.added` / delta 事件
**是分别构造的**。只读终态会看到「已生效」的假象，而客户端是**增量解析**的 ——
`added` 时刻不对就已经当文本渲染了。

正确验收：遍历所有 SSE 行，`item.id` / `item.type` / `item_id` 全查，统计残留数；
并断言 `response.completed` 和 `[DONE]` 都在。

## 案例：budget_notice 超额软拦截成 200 (2026-08-21)

- **问题**：claude-code-\*/cursor-\* key 超日额度后的友好 429（error_sanitize 精心构造的中文 body）在 Cursor 里**一个字看不到**——Cursor 吞 429 body 只显示自家 "exceeded retry limit"。用户以为服务故障。
- **前置诊断**：活体探针先证明友好 429 body 本身完全正确（两路由都对），**根因在客户端渲染层**，不是后端 —— 这一步防住了"去修没坏的东西"。
- **修复**：auth 层 monkey-patch `_virtual_key_max_budget_check`（**双命名空间**，见上文"patch 按名字 import 的函数"），gated key 超预算吞 BudgetExceededError + 按 token 打 mark（module dict，TTL 120s 兜底，auth 成功即清）→ pre-call 读 mark 出 200 mock「🚫 额度已用完」（chat/responses 用 `mock_response`，messages 用 ModifyResponseException，零上游零计费）；非可 mock call_type 重抛绝不放行上游；非 gated key 原样 429。止血 env `BUDGET_FRIENDLY_MOCK_DISABLED=1`。
- **验证链**：单测 50/50 → **全 prod 29-callback 链 T0** 21/21（`litellm-198-t0-fullchain.sh`）→ 止血开关回退 429 验证 → CM 外科 patch（patch 前 diff 确认无人动过）→ 4 pod 滚动 → prod 冒烟 6/6（`litellm-198-budget-notice-smoke.sh`）→ 15min 观察 429=0 / 5xx=0。
- **tradeoff（用户拍板）**：Cursor agent 循环会反复收到同一条 200 文案，接受（零计费且看得懂）。
- **08-21 阿里云上线（ns carher，全部 carher-\* her key）**：同一份文件（三拷贝 md5 全等），**只开①②**，③ 用 `BUDGET_FRIENDLY_MOCK_DISABLED=1` 关着（bot 超额行为不变）。用 `litellm-aliyun-callback-add.sh` 外科部署 + alias-gate canary 12 项 → 前缀放量 `carher-`。冒烟 `litellm-aliyun-budget-notice-smoke.sh`。踩坑三条已进上文注意事项（hostPort 死锁复现 / spend 缓存 60s / v4-pro 不接纯 chat）。
- **代码 + 测试**：`k8s/litellm-callbacks/budget_notice.py`（③ 部分）+ `tests/test_budget_notice.py`（OverBudgetSoftBlockTest 16 条）+ `tests/t0_budget_notice.py`。

## 案例：streaming_bridge `[DONE]` 残留过滤 (2026-04-28)

- **问题**：openclaw 用户 buyitian 用 acpx 报 `Could not parse Anthropic SSE event data: Unexpected token 'D', "[DONE]" is not valid JSON`。流式响应**全部内容已正确返回**，但末尾多出 OpenAI 协议的 `event: data\ndata: [DONE]\n\n`。Claude Code 官方 SDK 静默忽略，acpx 严格按 Anthropic 协议每个 `data:` 行 JSON.parse → 炸。
- **责任链**：OpenRouter 的 `/v1/messages` 实现复用了 OpenAI completion 的流式终结逻辑，没区分 Anthropic-compat。LiteLLM `anthropic_messages` 透传不洗。Anthropic 协议正确终结符是 `event: message_stop\ndata: {"type":"message_stop"}\n\n`，**没有** `[DONE]`。
- **历史伏笔**：之前已经有 `anthropic_passthrough_pingfix.py` 抑制 LiteLLM 自己 logging 时的 `JSONDecodeError`（server-side 噪音），但那条路径**只动了 logging**，没动出口字节流。client 端继续吃 `[DONE]` 残骸。
- **修复**：在 `streaming_bridge.py` 的 `async_post_call_streaming_iterator_hook` 里加出口过滤：
  - `_SSE_DONE_PATTERN = re.compile(rb"(?:^|\n)(?:event:\s*[^\n]*\n)?data:\s*\[DONE\]\s*\n+", re.IGNORECASE)`
  - `_strip_sse_done_lines(buf)` 含 `if b"[DONE]" not in buf: return buf` 快路径，避免给 99.99% 的正常 chunk 加 regex 开销。
  - `_EGRESS_TAIL_KEEP = 32`，在 egress 循环里维护 `egress_carry` 把每个 chunk 的尾部 32 byte 留给下一轮——见上面"改字节流的跨 chunk 边界陷阱"一节，这是**关键**。EOF 时 flush 一次。
- **第一版修复的 bug + 修法**：第一版只跑 per-chunk 正则没做 carry-over。本地穷举测试发现：把 wire bytes 在 `event: data\ndata: [DONE]\n\n` 内部任意位置 split，**12/26 个 split 位置 `[DONE]` 残骸照样泄漏到 client**（典型例如 `data: [D` + `ONE]\n\n` —— 第一段没 `[DONE]` 字面量、快路径直接 return；第二段不以 `data:` 或 `\n` 开头、regex 不匹配）。加 carry 后 26/26 全过。
- **测试方法**：本地 stub 掉 `litellm` imports，用 `importlib` 直接 load `streaming_bridge.py`，喂自定义 chunk 序列驱动 hook。三层覆盖：
  1. 单元测试 `_strip_sse_done_lines` 各种输入（含 `data:[DONE]`、`data: [DONE]`、CRLF、JSON 内含 `[DONE]` 字面量但不该删的负样本）
  2. 场景覆盖（fused / split-after-keyword / no-DONE 全透传）
  3. **穷举 split 位置** 26 个 + 1/3/7/31/33 byte 极端碎片化
- **代码 + 测试**：
  - 修复：`k8s/litellm-callbacks/streaming_bridge.py`（`_SSE_DONE_PATTERN` / `_strip_sse_done_lines` / `_EGRESS_TAIL_KEEP` / egress loop carry 维护）
  - 回归：`k8s/litellm-callbacks/tests/test_streaming_bridge_done_filter.py`（18 test）
- **验证**：`python3 -m unittest discover` 18/18 pass，27ms。Acpx client 报错消失。
- **注意**：`anthropic_passthrough_pingfix.py` 还要保留——它管的是 server-side logging 的 JSONDecodeError；本 fix 管的是 client-side egress 的字节流残骸。两个不冲突，叠加生效。

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

- [codex-deepseek-tool-triage](../codex-deepseek-tool-triage/SKILL.md) — Codex×DeepSeek 工具调用静默故障排障（本页两层坑的实战来源）

- LiteLLM Proxy 整体运维 → [litellm-ops](../litellm-ops/SKILL.md)
- 零中断 rollout 主 Deployment 细节 → [carher-k8s-zero-downtime-rollout](../carher-k8s-zero-downtime-rollout/SKILL.md)
- memorySearch / bge-m3 相关的 hook 场景 → [carher-memorysearch-config](../carher-memorysearch-config/SKILL.md)
