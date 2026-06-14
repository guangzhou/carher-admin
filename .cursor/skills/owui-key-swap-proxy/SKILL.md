---
name: owui-key-swap-proxy
description: >-
  OWUI → LiteLLM 中间反代 key-swap-proxy 的代码维护、镜像 build、K8s rollout、
  规则调整 (准入闸门 / 模型 allowlist / 域名白名单 / 注入 user=email).
  Use when 用户提到 "key-swap-proxy" / "OWUI 反代" / "禁 Claude" / "禁用模型" /
  "准入闸门" / "OWUI 模型 401" / "OWUI 反代 403" / "ALLOWED_EMAIL_DOMAIN" /
  "改反代规则" / "反代升级" / "提示申请入口".
---

# key-swap-proxy 反代维护

OpenAI 协议反代 (FastAPI), 部署在 198 K3s `open-webui` namespace, 2 副本。
所有 OWUI 用户的 LLM 调用都先经它, 再到真正的 LiteLLM。

## 1. 核心责任

```
OWUI → key-swap-proxy → LiteLLM (litellm-product)

反代在中间做 4 件事:
1. 读 X-OpenWebUI-User-Email (OWUI ENABLE_FORWARD_USER_INFO_HEADERS=true 注入)
2. 准入闸门: 查 LiteLLM /user/info?user_id=cursor-{local_part}; 没 → 401 + apply_url
3. 域名白名单: ALLOWED_EMAIL_DOMAIN 不匹配 → 403
4. 模型 filter: /v1/models 响应过滤 Claude; /v1/chat/completions 体内 model=claude-* → 403
转发时用 master key + body.user=<email>, LiteLLM 按 SpendLogs.end_user 归类
```

**为什么不转发用户自己的 raw sk-xxx**: LiteLLM 不存 raw key (DB 是 hash), `/key/regenerate` 是 Enterprise 付费功能。详见 `docs/openwebui-litellm-perkey-binding.md §2`。

## 2. 快速入口

```bash
# Pod 健康
scripts/jms ssh AIYJY-litellm "kubectl get pod -n open-webui -l app=key-swap-proxy"

# 实时日志 (看 gate deny / domain deny / claude_blocked)
scripts/jms ssh AIYJY-litellm "kubectl logs -n open-webui -l app=key-swap-proxy -f --tail=50"

# 测试 ClusterIP
scripts/jms ssh AIYJY-litellm "kubectl run owui-test --rm -i --restart=Never --image=curlimages/curl:8.10.1 --quiet -n open-webui --command -- \
  curl -sS -w 'HTTP=%{http_code}\\n' \
  -H 'X-OpenWebUI-User-Email: liuguoxian@auto-link.com.cn' \
  http://key-swap-proxy.open-webui.svc.cluster.local:8081/v1/models"
```

**关键文件**:
- 198:`/root/key-swap-proxy/main.py` (源码 ~250 行)
- 198:`/root/key-swap-proxy/Dockerfile`
- 198:`/root/open-webui-manifests/key-swap-proxy.yaml` (manifest)

## 3. 改代码 → 升级流程

```bash
scripts/jms ssh AIYJY-litellm '
NEW=v0.2.1  # bump version

# 1. 改源码 (vim/python script patch)
cd /root/key-swap-proxy
vim main.py    # 改逻辑

# 2. build + push
docker build -t 127.0.0.1:5000/key-swap-proxy:$NEW .
docker push 127.0.0.1:5000/key-swap-proxy:$NEW

# 3. rollout (2 副本, maxUnavailable=0 零中断)
kubectl set image -n open-webui deployment/key-swap-proxy proxy=127.0.0.1:5000/key-swap-proxy:$NEW
kubectl rollout status deployment/key-swap-proxy -n open-webui --timeout=120s
'
```

## 4. 5 种 ConfigMap 改动场景

ConfigMap = `key-swap-proxy-config` in `open-webui` ns。改完反代 Pod **必须 rollout restart** (ConfigMap 不像 Secret 自动注 env reload)。

### 4.1 改申请入口 URL

```bash
scripts/jms ssh AIYJY-litellm "
kubectl patch configmap key-swap-proxy-config -n open-webui --type=merge \
  -p '{\"data\":{\"APPLY_URL\":\"https://new-apply.example.com\"}}'
kubectl rollout restart deployment/key-swap-proxy -n open-webui
"
```

### 4.2 改域名白名单 (默认 auto-link.com.cn)

```bash
kubectl patch configmap key-swap-proxy-config -n open-webui --type=merge \
  -p '{"data":{"ALLOWED_EMAIL_DOMAIN":"new-domain.com"}}'
kubectl rollout restart deployment/key-swap-proxy -n open-webui
```

设空字符串 `""` 可关闭域名校验。

### 4.3 改 cache TTL (准入闸门缓存)

```bash
kubectl patch configmap key-swap-proxy-config -n open-webui --type=merge \
  -p '{"data":{"CACHE_TTL":"300"}}'   # 默认 600s
```

### 4.4 改 LiteLLM 目标 URL (比如切到 dev)

```bash
kubectl patch configmap key-swap-proxy-config -n open-webui --type=merge \
  -p '{"data":{"LITELLM_URL":"http://litellm-proxy.litellm-dev.svc.cluster.local:4000"}}'
```

### 4.5 调日志级别

```bash
kubectl patch configmap key-swap-proxy-config -n open-webui --type=merge \
  -p '{"data":{"LOG_LEVEL":"DEBUG"}}'
```

## 5. 反代行为速查表

| 请求场景 | 反代行为 | HTTP code |
|---------|---------|-----------|
| 没 X-OpenWebUI-User-Email 头 | 立刻拒 | 401 missing_user_header |
| Email 不是 @auto-link.com.cn | 域名 deny | 403 domain_not_allowed |
| Email 是合法域, 但 198 没 cursor-/claude-code- key | 准入 deny | 401 no_litellm_key + apply_url |
| 请求 model = claude-* | 模型 deny | 403 claude_blocked |
| `/v1/models` (有 key) | 转发到 LiteLLM, 过滤掉 claude-* 模型 | 200 |
| `/v1/chat/completions` (合法 model) | 注入 body.user=email, master key 转发 | 200 (SSE 流式透传) |

## 6. 模型 allowlist 改动 (改 main.py)

当前禁 Claude (正则 `re.compile(r'(?i)claude')`)。要禁其他模型:

```python
# main.py 改 CLAUDE_MODEL_RE 行
CLAUDE_MODEL_RE = re.compile(r'(?i)claude|gpt-4|deprecated-model')

# /v1/models filter 跟 chat completion model 校验都用这个正则
```

或更精细: 加 `MODEL_DENYLIST` ConfigMap env 实现 hot reload。

## 7. 故障排查

### 7.1 反代 502 (Pod 起不来)

```bash
scripts/jms ssh AIYJY-litellm "kubectl get pod -n open-webui -l app=key-swap-proxy
kubectl logs -n open-webui -l app=key-swap-proxy --previous | tail -30"
```

常见原因:
- `LITELLM_URL` 不可达
- `LITELLM_MASTER_KEY` Secret 没拉到
- FastAPI 路由 type annotation 错误 (用 `response_model=None`)

### 7.2 SpendLogs.end_user 不是 email

```sql
SELECT end_user, model, "startTime" FROM "LiteLLM_SpendLogs"
WHERE "startTime" > NOW() - INTERVAL '10 minutes' ORDER BY "startTime" DESC LIMIT 10;
```

- end_user 显示 `default_user_id` → 反代没注入 user, 检查 ENABLE_FORWARD_USER_INFO_HEADERS=true on OWUI
- end_user 显示 `{"device_id": ...}` JSON → 这是 Claude Code 自带的 end_user metadata, 不是 OWUI 流量

### 7.3 所有用户都被反代拒 (401 no_litellm_key)

- LiteLLM /user/info 接口失败? → 跨 ns DNS 问题 / LiteLLM proxy 挂了
- LITELLM_MASTER_KEY 改了但反代 Secret 没同步

## 8. 关键约束

1. **反代用 master key 转发**: 安全敏感, 任何登录用户消费都按 master key 计 spend, 只通过 body.user 字段归类报表
2. **个人 budget 不强制**: 反代不查个人累计 spend, 超额不阻断。F4 (反代加 30 天累计 spend deny cache) 待做
3. **响应必须 streaming 透传**: SSE 不能 buffer, FastAPI `StreamingResponse` 已配
4. **2 副本 + maxUnavailable=0**: 零中断 rolling update
5. **ClusterIP only**: 不暴露 NodePort, 外部不可直接调反代

## 9. 踩过的坑

1. **FastAPI 路由 union 返回类型报 FastAPIError**: catch-all route 必须加 `response_model=None`
2. **K8s pod name 大小写**: `owui-spkA-1` → K8s 拒 (必须 lowercase RFC 1123). 用 `owui-spk1` 等小写
3. **alpine pip 镜像**: 阿里云 pypi `https://mirrors.aliyun.com/pypi/simple` 在 Dockerfile 走代理
4. **OWUI ENABLE_FORWARD_USER_INFO_HEADERS=true 必须设**: 不设的话反代收不到 X-OpenWebUI-User-Email, 所有请求被 401
5. **/v1/models 响应不是流式**: 普通 JSON, 反代用 `JSONResponse` 直接重写 body 过滤 claude (不用 streaming)
6. **测试反代时用 ClusterIP 跑 in-cluster Pod**: 直接 curl ClusterIP 在节点 host 上不通 (K3s cni0 网络限制); 用 `kubectl run --rm -i` ephemeral pod 跑 curl
7. **httpx AsyncClient timeout**: read=600s (LLM 慢请求), pool=5s, connect=5s

## 10. 相关 skill / docs

- [[owui-ops]] - OWUI 端配 ENABLE_FORWARD_USER_INFO_HEADERS + OPENAI_API_BASE_URLS 指反代
- [[litellm-pro-ops]] - LiteLLM /user/info / SpendLogs 查询 / 配 master key
- [[litellm-key-mapping]] - 看 198 现存 cursor-*/claude-code-* key 分布
- 主文档: `docs/openwebui-litellm-perkey-binding.md`
