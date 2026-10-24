---
name: owui-key-swap-proxy
description: >-
  OWUI → LiteLLM 中间反代 key-swap-proxy 的代码维护、镜像 build、K8s rollout、
  规则调整 (准入闸门 / 模型白名单 / 域名白名单 / 注入 user=email).
  现役规则 = **只放 deepseek-v4.1-flash**(2026-10-07 起, DeepSeek 官方; 白名单见 §6)。
  Use when 用户提到 "key-swap-proxy" / "OWUI 反代" / "只显示某个模型" / "禁用模型" /
  "OWUI 看不到模型" / "准入闸门" / "OWUI 模型 401" / "OWUI 反代 403" /
  "ALLOWED_EMAIL_DOMAIN" / "ALLOWED_MODELS" / "改反代规则" / "反代升级" / "提示申请入口" /
  "所有用户只能用某个模型" / "OWUI 只放 deepseek" / "OWUI 模型白名单验收".
---

# key-swap-proxy 反代维护

> **登 198**: 文中 `scripts/jms ssh AIYJY-litellm "..."` 是老写法, 现在一律 sshpass 直连
> `cltx@10.68.13.198` + sudo; 脚本里用 [[owui-ops]] `scripts/_198.sh` 的 `r198 '...'` / `pypod`。

OpenAI 协议反代 (FastAPI), 部署在 198 K3s `open-webui` namespace, 2 副本。
所有 OWUI 用户的 LLM 调用都先经它, 再到真正的 LiteLLM。

**线上 tag: `v0.3.1`** (10-24; v0.3.0 及更早是历史)。

🔴 v0.3.1 = 转发时写死 `Accept-Encoding: identity`。原因: `_iter()` 用 `aiter_raw` 透传原始字节,
却把 `content-encoding` 头丢了 ⇒ LiteLLM 回 gzip 时客户端拿到**没标头的裸 gzip**(首字节 `1f8b`)。
OWUI ≤v0.11 碰巧不发 gzip 没事; v0.12 非流式(标题/追问/审计)全报 `'utf-8' codec can't decode byte 0x8b`。
流式 SSE 不压缩, 不受影响。改透传逻辑时别把这行删了。备份 `198:/Data/key-swap-proxy-ops/backup-20261024T155537-identity/`。
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
- **仓库 `src/`(main.py / Dockerfile / requirements.txt) = 源码真身**, 10-24 从 198 拷进来, 与 v0.3.1 容器逐字节一致
- 198:`/root/key-swap-proxy/` = build 目录(从仓库 `src/` 同步过去再 build, 别直接在上面 vim)
- 198:`/root/open-webui-manifests/key-swap-proxy.yaml` (manifest)

## 3. 改代码 → 升级流程

反代是 2 副本 RollingUpdate / maxUnavailable=0 ⇒ **换镜像无感**, 不用提前告知(和 OWUI 相反)。

```bash
NEW=v0.3.2   # bump
# 0. 在仓库 src/main.py 改, 本地先过语法
python3 -c "import ast;ast.parse(open('.claude/skills/owui-key-swap-proxy/src/main.py').read())"
# 1. 备份 198 当前源码 + Deployment, 同步过去
ssh cltx@10.68.13.198 'sudo -n sh -c "BK=/Data/key-swap-proxy-ops/backup-$(date +%Y%m%dT%H%M%S); mkdir -p $BK;
  cp /root/key-swap-proxy/main.py $BK/main.py.orig; kubectl -n open-webui get deploy key-swap-proxy -o yaml > $BK/deploy.yaml; echo $BK"'
ssh cltx@10.68.13.198 'sudo -n tee /root/key-swap-proxy/main.py >/dev/null' < .claude/skills/owui-key-swap-proxy/src/main.py
# 2. build + push(本地 Dockerfile 自己 build, docker push 没问题; 推公网镜像才要 host-198-registry-mirror.sh)
ssh cltx@10.68.13.198 "cd /root/key-swap-proxy && sudo -n docker build -q -t 127.0.0.1:5000/key-swap-proxy:$NEW . && sudo -n docker push -q 127.0.0.1:5000/key-swap-proxy:$NEW"
# 3. rollout + 验收
ssh cltx@10.68.13.198 "sudo -n kubectl set image -n open-webui deployment/key-swap-proxy proxy=127.0.0.1:5000/key-swap-proxy:$NEW && sudo -n kubectl rollout status deployment/key-swap-proxy -n open-webui --timeout=180s"
.claude/skills/owui-ops/scripts/owui-model-gate-audit.sh     # 要 RESULT PASS, 含 non-stream body plain
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

## 6. 模型白名单 —— 「OWUI 所有人只能用 X」

**现役 (2026-10-07)**: `ALLOWED_MODELS=deepseek-v4.1-flash` (DeepSeek 官方)。

### 6.0 一条命令 (首选)

```bash
S=.claude/skills/owui-ops/scripts
P198=... $S/owui-set-allowed-models.sh deepseek-v4.1-flash            # dry-run: 前置检查 + 计划
P198=... $S/owui-set-allowed-models.sh deepseek-v4.1-flash --apply    # 备份 → 反代 → OWUI → 验收
P198=... $S/owui-model-gate-audit.sh                                   # 任何时候只读验收
```

`--apply` 做的事, 按顺序, 前一步不过就停:

| 步 | 动作 | 中断? |
|---|---|---|
| 0 | 每个名字在 **LiteLLM 每个副本** 都注册 + 真发 200 | — |
| 1 | 备份 CM / 两个 Deployment / `webui.db` 整库 → `198:/Data/owui-ops/backup-<TS>-allowed-models/` | — |
| 2 | 反代 CM `ALLOWED_MODELS` + rollout restart | 无 (2 副本滚动) |
| 3 | OWUI admin API: 关 direct connections、默认模型、停用 base 不在名单的 workspace 模型 | **无, 不重启 OWUI** |
| 4 | `owui-model-gate-audit.sh` 三层验收 | — |

**名字在 LiteLLM 里不存在 ⇒ 第 0 步红停**, 先建组。建组要点(10-07 实战):
- 走 `POST /model/new`, 不直写 `LiteLLM_ProxyModelTable`(直写要滚 proxy, 见 memory
  `feedback_proxymodeltable_write_reroutes_deployment_needs_restart_regress`)。
  `/model/new` 后有副本同步窗口(10-07 1/4 副本落后约 1 分钟), 第 0 步就是逐副本查的。
- `api_key` 传**真值**, 不能写 `os.environ/...`(DB 路径不解析)。复用同 provider 存量行的 key:
  DB 里是密文, 在 litellm pod 里 `LITELLM_SALT_KEY=$LITELLM_MASTER_KEY` + `decrypt_value_helper` 解。
- **对外名 ≠ 上游名**。先打 provider 自己的 `/v1/models` 拿真名。DeepSeek 官方只认
  `deepseek-flash`(=V4.1-Flash) / `deepseek-v4-pro`; 发 `deepseek-v4.1-flash` 回 400,
  旧名 `deepseek-v4-flash` 被官方静默落到 `deepseek-flash`。
  ⇒ 现役组 `deepseek-v4.1-flash` = `custom_openai/deepseek-flash` @ `https://api.deepseek.com/v1`,
  `model_info.id=deepseek-official/deepseek-flash`。
- LiteLLM 会把响应体 `model` 改写成对外名, 看真落点用响应头 `x-litellm-model-name`。
- 🔴 **建 DeepSeek 的 mode:chat 组 = 同时建它的 `-responses` 孪生组 + 加改写映射**(10-10 补课)。
  OWUI 只走 chat, 但同名会被 Cursor/Codex key 拿去打 `/v1/responses`。10-07 只建了 chat 组,
  Codex 走 bridge 就丢 `reasoning_content`/译坏 `text.format`, 3 天 400 了 235 次。
  孪生组: `deepseek-v4.1-flash-responses` = `openai/deepseek-flash`, `mode: responses`,
  `id=deepseek-official/deepseek-flash-responses`; 映射写在
  `k8s/litellm-callbacks/deepseek_responses_adapt.py` 的 `_RESPONSES_GROUP_REWRITE`。
  验收: `scripts/codex-deepseek-triage.py --only groups` 必须 ✓。

### 6.1 原理: 必须拦两处, 只改列表 = 半道门

```python
# 列表 (可见性)
body["data"] = [m for m in body["data"] if _model_allowed(m.get("id"))]
# 发送 (真正拦住)
if not _model_allowed(body_json.get("model")):
    return _model_blocked_response(body_json.get("model"))
```

OWUI 历史会话里存着老模型名, 点开继续发, 列表过滤器看不到那个请求。名单是 env,
改名单不用重新发版; 代码 default `sa-grok-4.6`, 大小写/空格容忍。

### 6.2 反代之外的三条旁路 (只改反代不够)

| 旁路 | 现象 | 堵法 |
|---|---|---|
| Direct Connections | 用户在个人设置里自填任意 API | `direct.enable=false` |
| workspace 模型 base 指旧模型 | 列表里多一个死项, 点了 `Model not found` | `is_active=0` |
| `BYPASS_MODEL_ACCESS_CONTROL=true` | 所有人看见所有 base 模型 | 不用动 —— base 列表已被反代过滤 |

⚠️ **改 OWUI 配置不用重启**: v0.11.x `Config.get` 每次直读 `config` 表、无缓存, 走 admin API
(`POST /api/v1/configs/connections` / `/configs/models`) 立刻生效。改 env 才要重启(硬中断)。
env `ENABLE_DIRECT_CONNECTIONS` 只是首次启动的默认值, config 表里有行就以表为准。

### 6.3 不受白名单影响的两件事 (09-15 实测)

- 向量化: `RAG_OPENAI_API_BASE_URL` 直指 LiteLLM, 绕过反代
- 标题/标签生成: `task.model.external=""` ⇒ 复用聊天模型

### 6.4 历史

| 日期 | 名单 | 备份 |
|---|---|---|
| 09-15 | 黑名单(禁 Claude) → 白名单 `sa-grok-4.6`, 用 `scripts/patch-grok-only.py` 改代码(一次性, 已跑过; 「锚点唯一命中 + 改完查残留」的形状值得照抄) | `198:/Data/key-swap-proxy-ops/backup-20260915T130000/` |
| 10-07 | `deepseek-v4.1-flash`, 新建 LiteLLM 组 + 关 direct + 停用 `gpt-5.6-sol-lark` | `198:/Data/owui-ops/backup-20261007T194225-ds41/` + PVC `webui.db.bak-pre-0114-ds41-20261007` |

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
3. **响应必须 streaming 透传**: SSE 不能 buffer, FastAPI `StreamingResponse` 已配。
   透传用的是 `aiter_raw`(原始字节) ⇒ **转发头必须保留 `Accept-Encoding: identity`**, 否则见 §9 第 11 条
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
11. 🔴 **裸 gzip(10-24, v0.3.1 修)**: `_iter()` 用 `aiter_raw` 原样转发, 却把 `content-encoding` 头 drop 了。
    客户端一发 `Accept-Encoding: gzip`, LiteLLM 就回 gzip ⇒ 下游拿到**没标头的 gzip**(首字节 `1f8b`)。
    OWUI ≤v0.11.4 时没出事, v0.12.3 非流式(标题/追问)全报 `'utf-8' codec can't decode byte 0x8b`;流式 SSE 不压缩所以主对话好的。
    修 = 往 LiteLLM 的请求头写死 `Accept-Encoding: identity`。判据: gate audit 的 `L2 proxy non-stream body plain`。
    ⚠️ v0.11.4 为什么没撞上**没查**(新旧 OWUI 的 aiohttp 请求头没对比过), 别当已知结论讲。

## 10. 备份 / 回滚

`198:/Data/key-swap-proxy-ops/` 下:
- `backup-20260915T130000/main.py.orig` 与 `.prepatch` (改白名单前的源码, md5 `e60fea...997104`,
  与当时 `/root/key-swap-proxy/main.py` 及运行中容器逐字节一致)
- `deploy.yaml` (当时的 Deployment)
- `patch-grok-only.py` (那次的迁移脚本)

- `backup-20261024T155537-identity/`: v0.3.0 源码 + Deployment(加 AE identity 之前)

回滚 = `kubectl set image` 回上一个 tag(v0.3.0 在 registry 里还在)。🔴 回 v0.3.0 会让 OWUI v0.12 非流式重新全挂, 除非 OWUI 也一起回。
**⛔ 不要 `kubectl apply`**, 用 `set image` / `patch`。

## 11. 相关 skill / docs

- [[litellm-owui-user-row-reconcile]] - **准入闸门的粮草**: 有 key 没人行 → 401 的对账器
- [[owui-ops]] - OWUI 端配 ENABLE_FORWARD_USER_INFO_HEADERS + OPENAI_API_BASE_URLS 指反代
- [[litellm-pro-ops]] - LiteLLM /user/info / SpendLogs 查询 / 配 master key
- [[litellm-key-mapping]] - 看 198 现存 cursor-*/claude-code-* key 分布
- 主文档: `docs/openwebui-litellm-perkey-binding.md`
