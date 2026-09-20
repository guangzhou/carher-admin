---
name: owui-key-swap-proxy
description: >-
  OWUI → LiteLLM 中间反代 key-swap-proxy 的代码维护、镜像 build、K8s rollout、
  规则调整 (准入闸门 / 模型白名单 / 域名白名单 / 注入 user=email).
  现役规则 = **只放 sa-grok-4.6**(白名单, 见 §6), 不再是"禁 Claude"黑名单。
  Use when 用户提到 "key-swap-proxy" / "OWUI 反代" / "只显示某个模型" / "禁用模型" /
  "OWUI 看不到模型" / "准入闸门" / "OWUI 模型 401" / "OWUI 反代 403" /
  "ALLOWED_EMAIL_DOMAIN" / "ALLOWED_MODELS" / "改反代规则" / "反代升级" / "提示申请入口".
---

# key-swap-proxy 反代维护

OpenAI 协议反代 (FastAPI), 部署在 198 K3s `open-webui` namespace, 2 副本。
所有 OWUI 用户的 LLM 调用都先经它, 再到真正的 LiteLLM。

**线上 tag: `v0.3.0`** (v0.1.0 / v0.1.1 / v0.2.0 是历史)。
镜像 registry 是 `127.0.0.1:5000`, **198 上用 `docker` 不是 nerdctl**。

## 1. 核心责任

```
OWUI → key-swap-proxy → LiteLLM (litellm-product)

反代在中间做 4 件事:
1. 读 X-OpenWebUI-User-Email (OWUI ENABLE_FORWARD_USER_INFO_HEADERS=true 注入)
2. 准入闸门: 查 LiteLLM /user/info?user_id=cursor-{local_part}; 没 → 401 + apply_url
3. 域名白名单: ALLOWED_EMAIL_DOMAIN 不匹配 → 403
4. 模型白名单 (两处都要): /v1/models 只留 ALLOWED_MODELS; /v1/chat/completions 体内
   model 不在名单 → 403 model_not_allowed
转发时用 master key + body.user=<email>, LiteLLM 按 SpendLogs.end_user 归类
```

⚠️ **准入闸门查的是「人行」不是「钥匙」** —— 人行由飞书批量同步补, 滞后 1~57 天,
所以新同事有 key 也可能被 401。粮草与根因见 [[litellm-owui-user-row-reconcile]]。
反代源码里那句 "A user record is auto-created by LiteLLM whenever a key is issued"
**是假的**, 1257 对实测全部证伪。

**为什么不转发用户自己的 raw sk-xxx**: LiteLLM 不存 raw key (DB 是 hash), `/key/regenerate` 是 Enterprise 付费功能。详见 `docs/openwebui-litellm-perkey-binding.md §2`。

## 2. 快速入口

```bash
# Pod 健康
scripts/jms ssh AIYJY-litellm "kubectl get pod -n open-webui -l app=key-swap-proxy"

# 实时日志 (看 gate deny / domain deny / model_not_allowed)
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
| 请求 model 不在 ALLOWED_MODELS | 模型 deny | 403 model_not_allowed |
| `/v1/models` (有 key) | 转发到 LiteLLM, **只留** ALLOWED_MODELS 里的 | 200 |
| `/v1/chat/completions` (合法 model) | 注入 body.user=email, master key 转发 | 200 (SSE 流式透传) |

## 6. 模型白名单 (2026-09-15 起, 现役 = 只放 sa-grok-4.6)

原来是「禁 Claude」黑名单, 已改成白名单。**LiteLLM 一个字没动** —— 反代本来就在
过滤模型列表, 所以"OWUI 只显示某个模型"这类需求**不需要碰 LiteLLM**。

### 6.1 改名单 = 改 env, 不用重新发版

```bash
kubectl patch configmap key-swap-proxy-config -n open-webui --type=merge \
  -p '{"data":{"ALLOWED_MODELS":"sa-grok-4.6,gpt-5.6-luna"}}'
kubectl rollout restart deployment/key-swap-proxy -n open-webui
```

代码里的 default 是 `sa-grok-4.6`; 名单大小写与空格都容忍 (`strip().lower()`)。

### 6.2 ⛔ 必须改两处, 只改列表 = 半道门

```python
# 列表 (可见性)
body["data"] = [m for m in body["data"] if _model_allowed(m.get("id"))]
# 发送 (真正拦住)
if not _model_allowed(body_json.get("model")):
    return _model_blocked_response(body_json.get("model"))
```

**只改列表拦不住**: OWUI 的历史会话里存着老模型名, 用户点开上周的对话继续发,
列表过滤器看不到那个请求, 照样发得出去。

### 6.3 代码改动脚本 (锚点式, 已用过一次)

`scripts/patch-grok-only.py` —— 5 个锚点, `sub_once()` 每个都要求**唯一命中**,
命中数不对就红着退出不静默跳过; 改完还自查 `_is_claude_model` / `CLAUDE_MODEL_RE` /
`claude_blocked` 三个死名字必须全没了, 有残留就不写盘。

它是**一次性迁移脚本**(黑名单→白名单), 已经跑过。再想改名单走 §6.1 的 env。
保留它是因为那套「锚点唯一命中 + 改完查残留」的形状值得照抄, 见
[[reference_litellm_version_pinning_and_patch_layering]]。

### 6.4 改白名单前要做的两项附带影响检查 (2026-09-15 都查过了)

| 担心的事 | 实测 | 结论 |
|---------|------|------|
| 向量化 (embedding) 会不会被拦 | `RAG_OPENAI_API_BASE_URL` **直指 LiteLLM**, 绕过反代 | 不受影响 |
| 标题/标签自动生成会不会挂 | `task.model.external = ""` ⇒ 复用聊天模型 | 不受影响 |

### 6.5 验证 (三个用户 + 一红一绿)

```
三个用户各自 /v1/models → 都只有 ['sa-grok-4.6']
sa-grok-4.6  → 200 (nonce 回显)
gpt-5.4      → 403
claude-opus-5 → 403
```

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

### 7.4 ⛔ 单个用户被 401 no_litellm_key：**先别信这个结论**（2026-09-14 实测翻车）

我据此断言过"某用户没有 LiteLLM key"，**错的** —— 他 `cursor-<local>` 和
`claude-code-<local>` 两把 key 都在，spend $32.96，用过。

**闸门的判据不是"key 存在"，是 `/user/info` 返回体里 `keys` 数组非空**，而且
`_check_user_in_litellm()` 里 **httpx 异常走 `continue` → 最终 `return False`**，
`_gate()` 又把这个 False 按 `CACHE_TTL`（默认 **600s**）缓存起来
⇒ **LiteLLM 抖一下，这个用户就被"没申请过 key"这条文案顶死 10 分钟**。
文案说的是"没申请过"，实际可能只是那一刻查不到。

所以看到单人 401，**判据顺序**（每步都带阳性对照，拿一个已知好用的同事同时打）：

```bash
# ① key/user 行到底在不在（DB 是权威，不是文案）
ssh: kubectl -n litellm-product exec litellm-db-0 -- psql -U litellm -d litellm -A -F'|' \
  -c 'select key_alias,user_id from "LiteLLM_VerificationToken" where user_id ilike $$%<local>%$$'

# ② 闸门实际看到的东西（在反代 pod 里打，别在外面猜）
kubectl -n open-webui exec deploy/key-swap-proxy -- python3 -c "...GET /user/info?user_id=cursor-<local>..."
#   期望 HTTP 200 且 len(keys)>=1

# ③ 端到端，两个用户对打
kubectl -n open-webui exec deploy/open-webui -- curl -s -o /dev/null -w '%{http_code}\n' \
  http://key-swap-proxy.open-webui.svc.cluster.local:8081/v1/models \
  -H "X-OpenWebUI-User-Email: <email>"
```

⚠️ **Service 端口是 8081**，不是 8000。打错端口的症状是 `curl: (28) Failed to connect`
挂满 135 秒，长得**不像**端口错，像反代挂了 —— 别照这个去查 pod/netpol。

⚠️ 401 是**缓存过的**：改完 LiteLLM 侧要么等 10 分钟，要么重启反代清缓存，
否则会读出"没修好"的假红。`/healthz` 的 `cache_size` 能看缓存里有多少条。

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
8. 🔴 **header 名字是 `X-OpenWebUI-User-Email`, 不是 `X-User-Email`**。用错的症状是
   **所有人都 401, 包括阳性对照** —— 这时候坏的是量具不是被测对象, 别去改门禁。
   见 [[feedback_synthetic_red_is_as_untrusted_as_synthetic_green]]
9. **反代 pod 里没有 curl** (`executable file not found`), 探针一律 `python3` + `urllib`
10. **`_gate` 把 deny 缓存 `CACHE_TTL`(默认 600s), 且没有 flush 接口** ⇒ 改完 LiteLLM 侧
    要么等 10 分钟, 要么 `kubectl rollout restart`, 否则读出"没修好"的假红。
    `/healthz` 的 `cache_size` 能看缓存里有多少条

## 10. 备份 / 回滚

`198:/Data/key-swap-proxy-ops/` 下:
- `backup-20260915T130000/main.py.orig` 与 `.prepatch` (改白名单前的源码, md5 `e60fea...997104`,
  与当时 `/root/key-swap-proxy/main.py` 及运行中容器逐字节一致)
- `deploy.yaml` (当时的 Deployment)
- `patch-grok-only.py` (那次的迁移脚本)

回滚 = 拿 `.orig` 覆盖 `main.py` 重 build 一个新 tag, 或 `kubectl set image` 回 `v0.2.0`。
**⛔ 不要 `kubectl apply`**, 用 `set image` / `patch`。

## 11. 相关 skill / docs

- [[litellm-owui-user-row-reconcile]] - **准入闸门的粮草**: 有 key 没人行 → 401 的对账器
- [[owui-ops]] - OWUI 端配 ENABLE_FORWARD_USER_INFO_HEADERS + OPENAI_API_BASE_URLS 指反代
- [[litellm-pro-ops]] - LiteLLM /user/info / SpendLogs 查询 / 配 master key
- [[litellm-key-mapping]] - 看 198 现存 cursor-*/claude-code-* key 分布
- 主文档: `docs/openwebui-litellm-perkey-binding.md`
