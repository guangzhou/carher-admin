---
name: owui-lark-mcp
description: >-
  lark-mcp (官方 @larksuiteoapi/lark-mcp) 部署在 198 K3s open-webui ns,
  通过 streamable HTTP 给 OWUI 当 MCP 工具, 让大模型直接读飞书 docs/wiki/bitable/sheets/calendar 等.
  Use when 用户提到 "lark-mcp" / "飞书 MCP" / "OWUI 接飞书文档" / "工具调用" /
  "大模型读飞书 wiki" / "MCP server" / "扩飞书 scope" / "feishu OpenAPI 工具".
---

# lark-mcp 飞书 MCP 工具集

跑在 198 K3s `open-webui` ns, 1 副本, 暴露 streamable HTTP MCP 协议给 OWUI 用。
OWUI v0.6.31+ 原生支持外接 MCP server, 大模型自己决定何时调飞书 API。

## 1. 快速入口

```bash
scripts/jms ssh AIYJY-litellm "kubectl get pod -n open-webui -l app=lark-mcp -o wide"

# MCP endpoint
http://lark-mcp.open-webui.svc.cluster.local:3000/mcp    # in-cluster
# 外部访问 (调试): 暴露 NodePort 或 port-forward

# 实时日志
scripts/jms ssh AIYJY-litellm "kubectl logs -n open-webui -l app=lark-mcp -f --tail=50"

# 协议握手验证
kubectl run mcp-test --rm -i --restart=Never --image=curlimages/curl:8.10.1 --quiet -n open-webui --command -- \
  curl -sS -X POST http://lark-mcp.open-webui.svc.cluster.local:3000/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# 期望返回 {"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{...}},"serverInfo":{...}}}
```

**关键文件**:
- 198:`/root/lark-mcp/Dockerfile` (基于 node:20-slim + 阿里云 mirror)
- 198:`/root/lark-mcp-manifests/lark-mcp.yaml` (Deployment + Service + Secret)

## 2. 飞书 app 凭据

当前**复用 Casdoor SSO 同一个 app** `cli_a9278e26f138dbd3` (历史决策, 关注点未隔离)。

凭据存 K8s `lark-mcp-secrets`:
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

## 3. 必须的飞书 scope (你去飞书后台审批)

到 [飞书开放平台](https://open.feishu.cn) → app `cli_a9278e26f138dbd3` → **权限管理** → 申请:

| 业务 | scope | 用途 |
|------|-------|------|
| **Docs (只读)** | `docx:document:readonly` | 读 docx 文档 |
| **Docs (读写)** | `docx:document` | 创建 / 修改 文档 |
| **Docs 媒体** | `docx:document.media:download` | 文档图片附件下载 |
| **Wiki** | `wiki:wiki:readonly` + `wiki:node:read` | 知识库浏览 |
| **Bitable (只读)** | `bitable:app:readonly` | 多维表格读 |
| **Bitable (读写)** | `bitable:app` | 多维表格 CRUD |
| **Sheets (只读)** | `sheets:spreadsheet:readonly` | 表格读 |
| **Sheets (读写)** | `sheets:spreadsheet` | 表格读写 |
| **Drive** | `drive:drive:readonly` + `drive:file:readonly` | 云空间浏览 |
| **IM** | `im:message`, `im:chat:readonly` | (谨慎) 发消息、读群聊 |
| **Calendar** | `calendar:calendar`, `calendar:calendar.event:create` | 日程读写 |

**审批可能要 1-3 工作日**。审批通过后 `lark-mcp` Pod **重启即可生效** (token 是 tenant_access_token, 启动时拉一次):

```bash
kubectl rollout restart deployment/lark-mcp -n open-webui
```

## 4. tools preset 调整

`-t` / `--tools` 参数控制启用哪些 tools。默认配置 (`/root/lark-mcp-manifests/lark-mcp.yaml`):

```yaml
args:
  - mcp
  - --mode
  - streamable
  - --host
  - "0.0.0.0"
  - --port
  - "3000"
  - --app-id
  - $(FEISHU_APP_ID)
  - --app-secret
  - $(FEISHU_APP_SECRET)
  - --tools
  - "preset.default,preset.doc.default,preset.base.default,preset.calendar.default,preset.im.default"
  - --language
  - "zh"
```

**preset 速查**:
- `preset.default` - 常用基础工具
- `preset.doc.default` - 文档 Docs
- `preset.base.default` - 多维表格 Bitable
- `preset.calendar.default` - 日历
- `preset.im.default` - 即时消息
- `preset.task.default` - 任务

加 preset 改 manifest 后 `kubectl apply`。如果 V8 heap OOM (`Reached heap limit`), 调大:
```yaml
env:
  - name: NODE_OPTIONS
    value: --max-old-space-size=3000   # 默认 1.4GB 不够
resources:
  limits:
    memory: 4Gi
```

## 5. 镜像 build / 升级

```bash
scripts/jms ssh AIYJY-litellm '
NEW=v0.1.1
cd /root/lark-mcp
# 改 Dockerfile 里 lark-mcp@latest 改 lark-mcp@<具体版本>
docker build -t 127.0.0.1:5000/lark-mcp:$NEW .
docker push 127.0.0.1:5000/lark-mcp:$NEW
kubectl set image -n open-webui deployment/lark-mcp lark-mcp=127.0.0.1:5000/lark-mcp:$NEW
kubectl rollout status deployment/lark-mcp -n open-webui
'
```

**禁止 alpine base**: `keytar` native 模块在 alpine 缺 python+make+g+++libsecret-dev, 编译失败。固定 `node:20-slim` (debian)。

## 6. OWUI 接 MCP (Phase 5c)

OWUI v0.6.31+ Admin Panel → Settings → **External Tools** → Add MCP server:

```
Type: streamable_http
URL: http://lark-mcp.open-webui.svc.cluster.local:3000/mcp
Authentication: None
```

或通过 OWUI API 自动加 (脚本化, 待补)。

加完后:
- Admin Panel → Workspace → Models → 创建或编辑模型, **Tools** 里勾 `lark-mcp`
- 用户在对话框点 "Tool" 按钮启用 lark 工具
- 模型问答时自动 tool_call 调飞书

## 7. 典型对话效果验证

启用 lark MCP 后:
```
用户: 帮我看一下 "Q3 OKR 规划" 文档讲了什么
模型: (自动 tool_call: lark.search → 找文档 token → lark.doc.read → 拿内容)
模型: 这份文档主要讲了三个目标: ...
```

## 8. 故障排查

### 8.1 lark-mcp Pod 起不来 CrashLoopBackOff

```bash
kubectl logs -n open-webui -l app=lark-mcp --previous | tail -30
```

常见原因:
- **OOM**: `Reached heap limit Allocation failed - JavaScript heap out of memory` → 加 `NODE_OPTIONS=--max-old-space-size=3000` + memory limit 4Gi
- **app_id 无效**: `FEISHU_APP_ID/FEISHU_APP_SECRET invalid` → 检查 secret 内容
- **machine-id warn**: `StorageManager Failed to initialize encryption: Cannot spawn a message bus without a machine-id` — **可忽略**, 只 disable User Access Token store, tenant_access_token 模式正常

### 8.2 OWUI 调 MCP 工具失败

```bash
# 1. OWUI Pod 能否调 lark-mcp
kubectl exec -n open-webui deployment/open-webui -- curl -sS -o /dev/null -w '%{http_code}\\n' \
  http://lark-mcp.open-webui.svc.cluster.local:3000/mcp -X POST \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# 期望: 200

# 2. lark-mcp 实时日志看请求是否到达
kubectl logs -n open-webui -l app=lark-mcp -f
```

### 8.3 工具调用返回飞书权限错误

99% scope 不够。回 §3 重新审批所需 scope, audit 通过后 `kubectl rollout restart deployment/lark-mcp -n open-webui`。

## 9. 关键约束

1. **token 是 app-level tenant_access_token**: 任何 OWUI 用户调工具都用同一个 app token; 看到的资源 = 该 app 已被 add_member 的资源 (不是单个员工的飞书可见范围)
2. **写操作有风险**: 用户问"帮我把文档删掉"模型可能真删. 一期建议只开 readonly scope (`*:readonly`)
3. **app token 全自动**: lark-mcp 启动时拉 tenant_access_token, 2 小时自动 refresh; 不需要存 auth.json
4. **不能跟 Casdoor SSO 共用 token mode 字段**: token-mode 默认 auto, 不要改成 user_access_token (OAuth Beta, 需要每个用户单独走 OAuth)

## 10. 踩过的坑

1. **lark-mcp v0.5.1 默认 transport=stdio**: 必须显式 `--mode streamable` + `--host 0.0.0.0` + `-p 3000`, 否则 OWUI 接不上
2. **OOM 在 lark-mcp 加载工具列表时**: V8 heap 默认不够, 必须 `--max-old-space-size=3000`
3. **K8s envFrom secretRef args 引用**: yaml args 数组里 `$(FEISHU_APP_ID)` 才会替换 (K8s native substitution), shell `$` 不会
4. **alpine vs debian**: alpine 缺 keytar native deps, 用 node:20-slim 比较省事
5. **larksuite/cli vs lark-openapi-mcp 区别**: 前者是 CLI + AI Agent Skills (给 humans 用), 后者是 MCP server (给 LLM 用); 我们用后者
6. **machine-id 警告可忽略**: keytar 系统密钥环不可用, 但 lark-mcp 只用 app-level token, 不需要 user OAuth keyring

## 11. 相关 skill / docs

- [[owui-ops]] - OWUI 端启用 External Tools / MCP server
- [[owui-casdoor-sso]] - 飞书 app 凭据共用 (复用 Casdoor 同一个 app)
- 主文档: `docs/openwebui-litellm-perkey-binding.md`
- 上游: https://github.com/larksuite/lark-openapi-mcp
- 飞书开放平台: https://open.feishu.cn
