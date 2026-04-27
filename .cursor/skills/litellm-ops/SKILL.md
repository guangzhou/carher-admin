---
name: litellm-ops
description: >-
  LiteLLM Proxy 运维：升级镜像、Prisma DB 迁移、故障排查、性能调优、
  YAML-as-source-of-truth 不变量维护、callback 源↔ConfigMap 同步。
  Use when the user mentions "litellm" + 升级/部署/502/挂了/故障/重启/schema/prisma/
  探针/OOM/性能/日志级别/fallback 没生效/callback 没注册/yaml 改了不生效, or when
  litellm.carher.net returns 502/503, or when an admin UI config change needs to
  be made permanent.
---

# LiteLLM Proxy 运维

## 架构概览

| 组件 | K8s 资源 | 镜像 | 端口 |
|------|---------|------|------|
| LiteLLM Proxy | `deploy/litellm-proxy` | `ghcr.io/berriai/litellm` | 4000 |
| PostgreSQL | `sts/litellm-db` | `docker.io/library/postgres` | 5432 |

外部访问：`https://litellm.carher.net` → Cloudflare Tunnel → `svc/litellm-proxy:4000`

清单文件：`k8s/litellm-proxy.yaml`（ConfigMap + Deployment + Service）、`k8s/litellm-postgres.yaml`

## 连接集群

按 `k8s-via-bastion` skill 启动 kubectl proxy（已在跑则跳过）：

```bash
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes
```

## DB 凭证

```bash
kubectl get secret litellm-secrets -n carher \
  -o jsonpath='{.data.DATABASE_URL}' | base64 -d
# postgresql://litellm:<password>@litellm-db.carher.svc:5432/litellm
```

---

## 升级 LiteLLM 镜像

### 关键：Prisma Schema 必须同步

LiteLLM 用 Prisma ORM。新版镜像可能新增 DB 列，如果不执行迁移会导致：
`column XXX does not exist` → Prisma 连接池崩溃 → 所有请求 401 → Liveness 探针超时 → CrashLoop → 502

当前部署已包含 initContainer `prisma-migrate`，每次 Pod 启动前自动执行 `prisma db push`。

### 升级流程

1. **在构建服务器拉取新镜像**（走堡垒机，主构建机 = `k8s-work-227`）：
```bash
scripts/jms ssh k8s-work-227 \
  'nerdctl pull ghcr.io/berriai/litellm:<new-tag>'
```

2. **推到 ACR**（构建服务器对 `her/litellm-proxy` 无 push 权限，需用 Kaniko Job）：
```bash
# 更新 k8s/litellm-build-job.yaml 中的 --destination tag
# 然后 kubectl apply -f k8s/litellm-build-job.yaml
```

3. **更新 Deployment 镜像**：
```bash
# 修改 k8s/litellm-proxy.yaml 中 initContainers 和 containers 的 image
# 两处必须同步更新为同一个镜像
kubectl apply -f k8s/litellm-proxy.yaml
# 让 K8s 滚动更新，不要手动 delete pod
kubectl rollout status deploy/litellm-proxy -n carher --timeout=300s
```

4. **验证**：
```bash
kubectl get pods -n carher | grep litellm-proxy   # 1/1 Running
kubectl logs <pod> -c prisma-migrate -n carher     # 应显示 "Your database is now in sync"
curl -s -o /dev/null -w "%{http_code}" https://litellm.carher.net/health  # 应返回 401（非 502）
kubectl logs litellm-db-0 -n carher --since=2m | grep ERROR  # 不应有 "column XXX does not exist"
```

---

## 故障排查：502 Bad Gateway

### 快速诊断

```bash
# 1. Pod 状态
kubectl get pods -n carher | grep litellm-proxy

# 2. 看 Events（探针失败、OOM、镜像拉取问题）
kubectl describe pod <pod> -n carher | grep -A15 "Events:"

# 3. Proxy 日志（看 Prisma 错误）
kubectl logs <pod> -n carher --tail=50 | grep -iE "error|column|does not exist|ClientNotConnected"

# 4. DB 日志（看 schema 错误）
kubectl logs litellm-db-0 -n carher --tail=30 | grep ERROR
```

### 常见原因及修复

| 现象 | 原因 | 修复 |
|------|------|------|
| `column XXX does not exist` | 镜像升级后未迁移 DB | 在 Pod 内执行 `prisma db push`（见下方） |
| `ClientNotConnectedError` | Prisma 连接池崩溃 | 修复 schema 后重启 Pod |
| Liveness probe timeout | DEBUG 日志导致 I/O 过高 | 改 `LITELLM_LOG=INFO` |
| initContainer OOMKilled | Prisma 引擎内存不足 | initContainer limits 至少 1536Mi |
| 镜像拉取超时 | 从 ghcr.io 公网拉取慢 | 推到 ACR，用 VPC 地址 |
| YAML 改了 fallback / callback 不生效 | DB `LiteLLM_Config` row 覆盖 YAML | 检查 wipe-db-config-rows initContainer 是否在跑（见 "SoT 不变量"章节） |
| `No fallback model group found` 但 YAML 里写了 | 同上，runtime fallback 比 YAML 少 | 同上 |
| 改了 streaming_bridge.py 看不到 patch 生效 | YAML 内嵌副本和源 drift | `python3 scripts/sync-litellm-callbacks.py check` 然后 write + 重启 |

### 手动执行 Prisma 迁移（紧急修复）

如果 initContainer 不存在或失败，在当前运行的容器内执行：

```bash
kubectl exec <proxy-pod> -n carher -- sh -c \
  'DATABASE_URL="postgresql://litellm:<password>@litellm-db.carher.svc:5432/litellm" \
   prisma db push --schema /app/litellm/proxy/schema.prisma --accept-data-loss'
```

注意：这只修复当前容器，Pod 重启后需要重新执行。应确保 initContainer 正常工作。

### 验证 DB Schema 同步

```bash
# 检查特定列是否存在
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm \
  -c "SELECT column_name FROM information_schema.columns WHERE table_name='LiteLLM_MCPServerTable';"
```

---

## 故障排查：524 / 流式假成功 / TTFT 异常

claude-code-* 等 SSE 流式客户端最常见的疑难杂症。**先按"症状字典"对号入座，再按"排查路径"逐项过**。

### 症状字典

| 用户体验 | DB SpendLogs 特征 | 真实根因 | 责任 callback / patch |
|---------|------------------|---------|----------------------|
| 客户端等约 100s 后 Cloudflare 524 | duration ≈ 100s | 同步上游慢，CF 100s 整 body timeout 触发 | `force_stream.force_stream` 把同步包成 SSE 流 |
| 客户端等约 600s 后 524 | duration ≈ 600s，无 token | 流式连接长时间无字节，CF 看作 idle 杀连接 | `streaming_bridge` SSE heartbeat（每 ~20s 发心跳） |
| 客户端 200 但只有 8-20 个 token，duration 60-600s | `dur > 60s AND completion_tokens ≤ 20` | 上游流式**中途断流**，LiteLLM 没合成 SSE error frame，客户端死等 socket 关闭 | `streaming_bridge.async_post_call_streaming_iterator_hook` 合成 `event: error overloaded_error` + `event: message_stop` |
| 日志里 TTFT == Duration | `spend_logs.completion_start_time = endTime` | LiteLLM 上游对 Anthropic streaming 用错 start_time | `streaming_bridge` 的 `BaseAnthropicMessagesStreamingIterator.__init__` patch |
| 请求总卡 600s 整数倍才超时 | duration ≈ 600s | httpx Anthropic client 默认 `read=600s` | `streaming_bridge` 的 httpx timeout monkey-patch（read=120s） |
| 上游临时挂（5xx/timeout） | dur 短，no_log_completion | 单点失败 | `router_settings.fallbacks`（13 条） |

### 一键 SQL：拉假成功记录（最常用）

```bash
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm -c "
SELECT
  to_char(sl.\"startTime\" AT TIME ZONE 'UTC',          'MM-DD HH24:MI UTC')       AS utc,
  to_char(sl.\"startTime\" AT TIME ZONE 'Asia/Shanghai','MM-DD HH24:MI')           AS bjt,
  EXTRACT(EPOCH FROM (sl.\"endTime\" - sl.\"startTime\"))::int                     AS dur_s,
  sl.completion_tokens                                                             AS toks,
  sl.model,
  vt.key_alias
FROM \"LiteLLM_SpendLogs\" sl
JOIN \"LiteLLM_VerificationToken\" vt ON sl.api_key = vt.token
WHERE sl.\"startTime\" > NOW() - INTERVAL '6 hours'
  AND vt.key_alias LIKE 'claude-code-%'
  AND EXTRACT(EPOCH FROM (sl.\"endTime\" - sl.\"startTime\")) > 60
  AND COALESCE(sl.completion_tokens, 0) <= 20
ORDER BY sl.\"startTime\" DESC LIMIT 30;"
```

健康基线（参考 2026-04-27 实测）：
- claude-code-* 6h 内 ≥ 8000 请求 → 假成功 ≤ 5 条（即 ~0.05%）
- carher-* 48h 内 40 万+ 请求 → 假成功 ≤ 10 条（多为合理长 embedding）

如果数量级超过这个 → 立刻按下面"排查路径"过一遍。

### 排查路径（5 分钟版）

```bash
POD=$(kubectl get po -n carher -l app=litellm-proxy --sort-by=.metadata.creationTimestamp \
       -o jsonpath='{.items[-1].metadata.name}')

# 1. boot log 上 patch 双签名
kubectl logs "$POD" -c litellm -n carher 2>&1 | grep "streaming_bridge: patched" | head -3
# 必须看到两行：
#   streaming_bridge: patched BaseAnthropicMessagesStreamingIterator.__init__ ...
#   streaming_bridge: patched anthropic httpx client timeout (read=120.0s; ...)
# 缺一条 = streaming_bridge 是旧版（drift），跑 sync 脚本

# 2. wipe initContainer 跑了
kubectl logs "$POD" -c wipe-db-config-rows -n carher
# 必须看到：[wipe-db-config-rows] OK: removed N row(s); LiteLLM_Config is clean ...

# 3. runtime 4 个 callback + 13 fallback
MK=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
kubectl port-forward svc/litellm-proxy 4000:4000 -n carher >/dev/null 2>&1 &
PF=$!; sleep 3
curl -sf -H "Authorization: Bearer $MK" http://127.0.0.1:4000/get/config/callbacks \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
cbs = [c['name'] for c in d.get('callbacks', [])]
fbs = d.get('router_settings', {}).get('fallbacks', [])
print('callbacks:', cbs)
print('fallback count:', len(fbs))
expect_cbs = {'streaming_bridge.streaming_bridge','opus_47_fix.thinking_schema_fix',
              'force_stream.force_stream','embedding_sanitize.embedding_sanitize'}
miss = expect_cbs - set(cbs)
print('MISSING:', miss if miss else 'none')
"
kill $PF 2>/dev/null

# 4. (可选) 验证 streaming_bridge 当前是 1100 行新版且含错误帧合成
kubectl exec "$POD" -n carher -c litellm -- python3 -c "
import sys, importlib, inspect
sys.path.insert(0, '/etc/litellm/callbacks')
m = importlib.import_module('streaming_bridge')
src = inspect.getsource(m)
print('lines:', len(src.splitlines()))
for k in ['event: error', 'overloaded_error',
          'async_post_call_streaming_iterator_hook',
          'patched anthropic httpx client timeout']:
    print(f'  {k}: {k in src}')
"
```

### 用户具体 case：怎么定位他/她的请求

```bash
# 把 KEY_ALIAS 替换成具体 key（如 claude-code-tenggeer-qvaz）
# 把 START / END 替换成报告时间窗口（UTC）
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm -c "
SELECT to_char(sl.\"startTime\" AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS UTC') t,
       EXTRACT(EPOCH FROM (sl.\"endTime\"-sl.\"startTime\"))::int dur_s,
       sl.completion_tokens toks, sl.prompt_tokens, sl.model, sl.spend
FROM \"LiteLLM_SpendLogs\" sl
JOIN \"LiteLLM_VerificationToken\" vt ON sl.api_key = vt.token
WHERE vt.key_alias = '<KEY_ALIAS>'
  AND sl.\"startTime\" BETWEEN '<START>' AND '<END>'
ORDER BY sl.\"startTime\" DESC LIMIT 30;"
```

判断准则：
- `dur > 60s AND toks ≤ 20` → 流式中途断流（看 streaming_bridge 是否在跑）
- `dur ≈ 100s 且无成功 row` → 同步整 body 被 CF 杀（看 force_stream）
- `dur ≈ 600s 整数` → httpx timeout 没 patch 上（看 streaming_bridge boot 行）
- 大量 `dur < 5s 且 toks=0` → 上游 5xx，看 fallback 是否触发

---

## 健康检查清单（每次部署后跑一遍）

封装好的一键脚本：`scripts/litellm-healthcheck.sh`。

```bash
bash scripts/litellm-healthcheck.sh
```

11 项检查，覆盖：

| 类别 | 检查项 |
|------|--------|
| Pod | 1/1 Running |
| InitContainers | wipe-db-config-rows 跑了；prisma-migrate done |
| streaming_bridge boot | iterator init patch；httpx Anthropic timeout patch (read=120s) |
| Runtime registry | 4 个 callback 全注册（streaming_bridge / opus_47_fix / force_stream / embedding_sanitize） |
| Runtime registry | fallback count = 13 |
| Runtime registry | opus-4.7 fallback 第一跳 = `anthropic.openrouter.claude-opus-4-7` |

期望输出 `PASS=11 FAIL=0`。任一项 `✗` → 不要进入下一步，对照"症状字典"和"排查路径"定位。

CI / 运维场景建议：
- 每次 `kubectl apply -f k8s/litellm-proxy.yaml` 后立刻跑
- 也可以接进定时任务（Cron / Argo Workflow）做日常巡检

---

## ⚠️ Source of Truth 不变量（必读）

**铁律**：配置 = `k8s/litellm-proxy.yaml`，回调代码 = `k8s/litellm-callbacks/*.py`。
**任何其他地方（DB 行、admin UI、ConfigMap 内嵌副本）都是派生物，不允许独立修改。**

### 为什么有这条铁律

LiteLLM Proxy 启动时配置来源有三个，**优先级是 DB > YAML（DB 全胜）**：

1. `k8s/litellm-proxy.yaml` 中 `litellm-config` ConfigMap → `/app/proxy_config.yaml`
2. ConfigMap `litellm-callbacks` → `/etc/litellm/callbacks/*.py`（被 YAML 内嵌副本生成）
3. PostgreSQL 表 `LiteLLM_Config` 中三行：
   - `router_settings` — fallbacks / model_group_alias / num_retries / cooldowns
   - `litellm_settings` — callbacks / log_raw_request_response / 等
   - `general_settings` — `store_model_in_db` 等（最危险：开启后 admin UI 改的东西也会落 DB）

历史踩坑：有人通过 admin UI 或脚本写过这 3 行 row，导致 YAML 改完不生效，pod 实际跑 DB 旧值。
症状：明明 YAML 写了 13 条 fallback、4 个 callback，runtime 只剩 4 条 + 1 个。

### 三层防御机制（已部署）

```
┌────────────────────────────────────────────────────────────┐
│ 1. YAML 唯一权威：所有配置写进 k8s/litellm-proxy.yaml      │
│    （general_settings / litellm_settings / router_settings │
│      / model_list / 全部 callback 注册）                    │
└────────────────────────────────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ 2. wipe-db-config-rows initContainer                       │
│    每次 pod 启动前用 prisma 物理删除上述 3 行 row          │
│    → main 容器永远从 0 加载，DB 没有覆盖机会               │
└────────────────────────────────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────┐
│ 3. store_model_in_db: false（YAML 显式设）                  │
│    → admin UI 改的东西不落盘，最多影响一个 pod 生命周期    │
└────────────────────────────────────────────────────────────┘
```

回调代码（streaming_bridge.py / opus_47_fix.py / force_stream.py / embedding_sanitize.py）
有第二道同类防御：源文件在 `k8s/litellm-callbacks/`，YAML 内嵌副本由
`scripts/sync-litellm-callbacks.py` 再生。

### 当前 4 个 callback 各自的职责（重要 — 别归错位）

| 文件 | 行数 | 注册符号 | 干什么 |
|------|------|---------|--------|
| `streaming_bridge.py` | ~1100 | `streaming_bridge.streaming_bridge` | **核心**：① 流式响应注 SSE 心跳防 Cloudflare 524；② 流式中途上游断流时合成 `event: error overloaded_error + event: message_stop` 给客户端，避免客户端死挂出现"600s 假成功"；③ 进程级 monkey-patch httpx Anthropic client `read=120s`（默认 600s 太长）；④ 修 `BaseAnthropicMessagesStreamingIterator.__init__` 的 `start_time`，让 TTFT 日志正确 |
| `force_stream.py` | ~190 | `force_stream.force_stream` | 把同步（`stream=False`）请求强制包成 SSE 流式上游调用，绕开 Cloudflare 100s 整 body timeout |
| `opus_47_fix.py` | ~145 | `opus_47_fix.thinking_schema_fix` | 仅做 thinking schema 改写（兼容 Wangsu 4.7 的 schema 差异），**不**负责 SSE 错误帧——常被误以为它管 524，实际不管 |
| `embedding_sanitize.py` | ~95 | `embedding_sanitize.embedding_sanitize` | 清洗 embedding 输入里的孤立 surrogate（避免 bge-m3 400） |

启动时 boot log 中应能看到（仅 streaming_bridge 会打印这两行）：
```
streaming_bridge: patched BaseAnthropicMessagesStreamingIterator.__init__ to use logging_obj.start_time
streaming_bridge: patched anthropic httpx client timeout (read=120.0s; was hardcoded 600s in LiteLLM upstream)
```
没看到 = 这次启动的 streaming_bridge 是旧版（drift），跑 sync 脚本。

### 改配置的标准流程

**任何改动都从源头开始**，永远不要绕过 YAML：

```bash
# (1) 改 YAML 或 .py 源
$EDITOR k8s/litellm-proxy.yaml
# 如果改的是 callback 源代码：
$EDITOR k8s/litellm-callbacks/streaming_bridge.py
python3 scripts/sync-litellm-callbacks.py write   # 把源同步进 YAML 内嵌副本

# (2) 一致性检查（CI 也跑这个）
python3 scripts/sync-litellm-callbacks.py check   # 退出码 0 = 一致；1 = 有 drift

# (3) 部署
kubectl apply -f k8s/litellm-proxy.yaml
kubectl rollout restart deploy/litellm-proxy -n carher
kubectl rollout status deploy/litellm-proxy -n carher --timeout=300s

# (4) 验证 wipe initContainer 真跑了
POD=$(kubectl get po -n carher -l app=litellm-proxy --sort-by=.metadata.creationTimestamp \
       -o jsonpath='{.items[-1].metadata.name}')
kubectl logs "$POD" -n carher -c wipe-db-config-rows
# 期望看到：[wipe] before=[...]; deleted N rows; after=[]

# (5) 验证 runtime 真按 YAML 跑（用 admin master key）
MK=$(kubectl get secret litellm-secrets -n carher \
       -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
kubectl port-forward svc/litellm-proxy 4000:4000 -n carher >/dev/null 2>&1 &
PF=$!
sleep 2
curl -s -H "Authorization: Bearer $MK" http://127.0.0.1:4000/router/settings \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("fallbacks:",len(d.get("fallbacks",[])))'
curl -s -H "Authorization: Bearer $MK" http://127.0.0.1:4000/get/config/callbacks \
  | python3 -c 'import sys,json;print("callbacks:",[c["name"] for c in json.load(sys.stdin)])'
kill $PF
# 期望：fallbacks 数量、callback 列表 = YAML 里写的
```

### 应急 / 反 SoT 操作（只在已知坏配置阻塞启动时使用）

如果有人在 admin UI 改坏了什么导致启动失败，可以手动直接清那 3 行（**临时手段，事后必须把正确配置写进 YAML**）：

```bash
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm -c \
  "DELETE FROM \"LiteLLM_Config\" WHERE param_name IN
   ('router_settings','litellm_settings','general_settings');"
kubectl rollout restart deploy/litellm-proxy -n carher
```

正常运维下不需要这一步 — initContainer 已经在每次启动时自动做了。

### 破坏性测试（季度回归）

确认 wipe 机制还活着：

```bash
# 1. 往 DB 写一条垃圾 router_settings
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm -c \
  "INSERT INTO \"LiteLLM_Config\"(param_name, param_value)
   VALUES ('router_settings','{\"fallbacks\":[{\"poison\":[\"poison\"]}]}'::jsonb)
   ON CONFLICT (param_name) DO UPDATE SET param_value=EXCLUDED.param_value;"

# 2. 重启
kubectl rollout restart deploy/litellm-proxy -n carher
kubectl rollout status deploy/litellm-proxy -n carher --timeout=300s

# 3. 检查 wipe log 应能看到 deleted 1 row，runtime fallback 数量恢复正常
```

---

## 配置变更

### 模型路由配置

路由配置在 `litellm-config` ConfigMap 中（定义在 `k8s/litellm-proxy.yaml`）。
**先读上面"Source of Truth 不变量"章节，按标准流程改**：

```bash
$EDITOR k8s/litellm-proxy.yaml          # 改 router_settings.fallbacks 等
kubectl apply -f k8s/litellm-proxy.yaml
kubectl rollout restart deploy/litellm-proxy -n carher
kubectl rollout status deploy/litellm-proxy -n carher --timeout=300s
```

### 修改 callback / hook 源代码

callback 源在 `k8s/litellm-callbacks/*.py`，被复制进 `k8s/litellm-proxy.yaml` 的 `litellm-callbacks` ConfigMap。**两份必须保持一致**，由 sync 脚本管理：

```bash
$EDITOR k8s/litellm-callbacks/streaming_bridge.py  # 改源
python3 scripts/sync-litellm-callbacks.py write    # 同步进 YAML
python3 scripts/sync-litellm-callbacks.py check    # 验证 drift = 0
git diff k8s/litellm-proxy.yaml                    # 应有 ConfigMap 段更新
kubectl apply -f k8s/litellm-proxy.yaml
kubectl rollout restart deploy/litellm-proxy -n carher
```

注意 callback 模块名必须在 `litellm_settings.callbacks` 中显式声明（`<module>.<symbol>` 形式），proxy 启动时按这个清单 import。漏了不会报错，只会静默跳过。

### Fallback 链设计原则

写在 `router_settings.fallbacks`，每个主模型一行 `{primary: [hop1, hop2, hop3]}`。设计原则：

1. **同档优先**：先尝试同质量同价位的替代供应商（OpenRouter 同款），用户感知最小
2. **再降档兜底**：同档全失败再降一档（如 4.7 → 4.6）
3. **最后是不同供应商的降档**（OpenRouter 4.6 之类）
4. 链长度建议 ≤ 3 —— 太长会拖慢真正失败请求的总耗时

例：`anthropic.claude-opus-4-7` 当前链 = `[anthropic.openrouter.claude-opus-4-7, anthropic.claude-opus-4-6, openrouter-claude-opus-4-6]`。

### 日志级别

```bash
# 查看当前
kubectl get deploy litellm-proxy -n carher -o jsonpath='{.spec.template.spec.containers[0].env}' | python3 -m json.tool

# 修改（会触发滚动更新）
kubectl set env deploy/litellm-proxy -n carher LITELLM_LOG=INFO
```

生产环境禁止使用 DEBUG 级别（会导致大量日志 I/O，拖慢 health 端点响应）。

### 探针参数

当前配置（在 `k8s/litellm-proxy.yaml` 中）：

```yaml
livenessProbe:
  initialDelaySeconds: 90   # LiteLLM 启动慢，不能低于 90
  periodSeconds: 15
  failureThreshold: 5       # 5 次失败才杀，避免误杀
  timeoutSeconds: 15        # 高负载下 health 端点可能慢
readinessProbe:
  # 同上
```

---

## 监控

```bash
# Pod 资源使用
kubectl top pod -n carher | grep litellm

# DB 大小
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm \
  -c "SELECT pg_size_pretty(pg_database_size('litellm'));"

# 各表大小（SpendLogs 是最大的表）
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm \
  -c "SELECT tablename, pg_size_pretty(pg_total_relation_size('public.\"' || tablename || '\"')) AS size FROM pg_tables WHERE schemaname='public' ORDER BY pg_total_relation_size('public.\"' || tablename || '\"') DESC LIMIT 5;"
```

---

## 零中断操作原则

- **禁止手动 `kubectl delete pod`**，必须依赖 Deployment 滚动更新
- 变更通过 `kubectl apply` 或 `kubectl set image/env`，让 K8s 自动完成：新 Pod Ready → 流量切换 → 旧 Pod 终止
- LiteLLM 有 90s initialDelay，要有耐心等滚动更新完成
- 用 `kubectl rollout status` 监控进度

---

## 附录 A：客户端类型差异（claude-code-* vs carher-*）

不同 key alias 走不同路径，回调对它们的接管程度也不同：

| Key alias | 客户端 SDK | API 路径 | 流式 | 同步 |
|-----------|-----------|---------|------|------|
| `claude-code-*` | Anthropic Python/TS SDK 直连 `/v1/messages` | Anthropic-native | `streaming_bridge` 接管（含 SSE error frame、heartbeat、TTFT 修正、httpx 120s） | `force_stream` 包成 SSE 流，再走 streaming_bridge |
| `carher-*` | 自家 OpenAI 兼容 client → `/v1/chat/completions` | OpenAI-compat | streaming_bridge 同样发 heartbeat（OpenAI 格式 `data: [DONE]`） | force_stream 适用；embedding 端点走 `embedding_sanitize` |

也就是说**两类 key 都被 4 个 callback 覆盖**，但只有 claude-code-* 会走到 Anthropic SDK 的 `BaseAnthropicMessagesStreamingIterator` 那条 hot path —— 这就是为什么 4/24 的 TTFT==Duration / 600s timeout 问题只在 claude-code-* 上出现，carher-* 不受影响。

调试时如果两类表现差异巨大，先看 `vt.key_alias` 前缀对应的实际请求路径（curl 上游能看到 `path=/v1/messages` 还是 `/v1/chat/completions`）。

---

## 附录 B：历史 fix 索引（按问题→修复）

| 日期 | 报告问题 | 根因 | 修复 |
|------|---------|------|------|
| 2026-04-18 | opus-4.7 上游 Wangsu 400 | thinking schema 字段不兼容 | `aa5a6d5` rewrite thinking schema (`opus_47_fix.thinking_schema_fix`) |
| 2026-04-18 | streaming usage 缺失 | client 没传 `stream_options.include_usage` | `8a6d6fd` 强制注入 stream_options |
| 2026-04-21 | bge-m3 embedding 偶发 400 | 输入字符串含孤立 surrogate | `7f584fc` 加 `embedding_sanitize` 清洗 callback |
| 2026-04-22 | OpenRouter 上各家路径混乱 | provider routing 不固定 | `9387096` / `81e68da` / `ca1cf3d` pin OpenRouter provider，给 claude family 加 OR primary 选项 |
| 2026-04-25 | claude-code opus-4.7 一堆 524 + TTFT==Duration | (a) 流式无心跳被 Cloudflare 杀；(b) httpx 默认 600s read timeout；(c) Anthropic streaming iterator init 时 LiteLLM 上游用错 start_time | `719018e` `streaming_bridge.py` 引入 SSE heartbeat + httpx 120s monkey-patch + iterator init patch |
| 2026-04-25 | tenggeer sonnet-4-6 仍 524 | 同步 endpoint 没流式，CF 100s 整 body timeout | 引入 `force_stream.py` 把同步包成 SSE 流；heartbeat 扩到 OpenAI-compat |
| 2026-04-27 凌晨 | 多个 claude-code 用户 600s "假成功" duration + 个位 token | 流式中途上游 socket 断开，LiteLLM 没合成 SSE error frame，客户端死等 | `streaming_bridge.async_post_call_streaming_iterator_hook` 包流，断流时合成 `event: error overloaded_error` + `event: message_stop` |
| 2026-04-27 下午 | 改 YAML fallback 不生效 / callback 注册不完整 | DB `LiteLLM_Config` 三行覆盖 YAML | 加 `wipe-db-config-rows` initContainer + 在 YAML 里显式 `store_model_in_db: false`；引入 `scripts/sync-litellm-callbacks.py` 防 ConfigMap 内嵌副本 drift |
| 2026-04-27 下午 | opus-4.7 fallback 直接降一档丢质量 | 链没考虑 OpenRouter 同档 | fallback 改为 `[anthropic.openrouter.claude-opus-4-7, anthropic.claude-opus-4-6, openrouter-claude-opus-4-6]` |

下次遇到类似问题，**先来这张表里搜关键词**，往往同事已经踩过。
