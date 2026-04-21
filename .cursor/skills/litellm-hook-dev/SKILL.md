---
name: litellm-hook-dev
description: >-
  开发 LiteLLM pre-call hook（请求改写、脏数据清洗、schema 转换）并用独立
  canary Deployment 做灰度验证。Use when the user mentions "litellm" + 加 hook /
  callback / 改写请求 / sanitize / schema fix / pre-call，or when a new
  request transformation needs to run before upstream LLM calls (e.g.
  inject stream_options, rewrite thinking schema, strip bad unicode).
---

# 开发 LiteLLM Pre-call Hook + Canary 灰度

## 现有 hook 参考

| 文件 | 作用 | 类型 |
|---|---|---|
| `k8s/litellm-callbacks/opus_47_fix.py` | 把老的 `thinking.type=enabled` / `reasoning_effort` 改写成 opus-4-7+ 的 adaptive schema + 强制 streaming | chat/completion |
| `k8s/litellm-callbacks/embedding_sanitize.py` | 清洗 embedding input 里的 lone UTF-16 surrogate（Node.js bot 脏数据防御）| embedding |

**所有 hook 挂到同一个 ConfigMap `litellm-callbacks` 里，写在 `litellm_settings.callbacks` 列表里按顺序执行。**

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
