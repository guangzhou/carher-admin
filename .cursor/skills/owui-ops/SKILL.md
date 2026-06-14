---
name: owui-ops
description: >-
  198 K3s 上 Open WebUI 内网 chat 服务 (https://chat.auto-link.com.cn) 的综合运维:
  部署/升级 OWUI 镜像、env 变更 (REDIS session / SSO / 模型路由)、查日志、
  account 合并/重置、Redis session 排障、Service NodePort/Ingress 切流。
  Use when 用户提到 "open-webui" / "OWUI" / "chat.auto-link" / "https://chat.auto-link.com.cn" /
  "OWUI 升级 / 卡死 / 登不上 / 模型不对 / cookie / session" / "/oauth/oidc/callback 502" /
  "改 OWUI env" / "OWUI Redis" 或要 audit OWUI 健康度。
---

# Open WebUI 综合运维 (198 内网)

OWUI 跑在 198 K3s `open-webui` namespace, 是内部 chat.auto-link.com.cn 的后端。
**所有上游 LiteLLM 调用都经反代** `key-swap-proxy` (见 [[owui-key-swap-proxy]]),
**身份认证经 Casdoor + 飞书** (见 [[owui-casdoor-sso]]),
**未来工具调用经 lark-mcp** (见 [[owui-lark-mcp]])。

## 1. 快速入口

```bash
# 198 节点
scripts/jms ssh AIYJY-litellm

# 健康检查
curl -sS https://chat.auto-link.com.cn/health    # {"status":true}
curl -sS http://10.68.13.198:30880/health        # 同上, 绕过 nginx 反代

# OWUI Pod 实时日志
kubectl logs -n open-webui -l app=open-webui -f --tail=50
```

**关键文件**:
- 198:`/root/open-webui-manifests/open-webui.yaml` (manifest)
- 198:`/root/open-webui-manifests/README.md` (本地运维笔记)
- `docs/openwebui-litellm-perkey-binding.md` (主文档)

## 2. 常见运维场景

### 2.1 改 OWUI env (热改, 不需 image rebuild)

```bash
scripts/jms ssh AIYJY-litellm "kubectl set env -n open-webui deployment/open-webui \
  KEY1=value1 KEY2=value2"

# 等滚动完成
scripts/jms ssh AIYJY-litellm "kubectl rollout status deployment/open-webui -n open-webui"

# 验证 env 生效
scripts/jms ssh AIYJY-litellm "kubectl exec -n open-webui deployment/open-webui -- env | grep -E 'KEY1|KEY2'"
```

**常改 env 速查**:
| Env | 作用 |
|-----|------|
| `OPENAI_API_BASE_URLS` | 上游 LLM URL (生产: 走反代) |
| `WEBUI_URL` | OAuth callback 基地址 |
| `ENABLE_FORWARD_USER_INFO_HEADERS=true` | 注入 X-OpenWebUI-User-Email |
| `ENABLE_SIGNUP=false` / `ENABLE_LOGIN_FORM=false` | 锁死本地注册, 强制 SSO |
| `REDIS_URL` + `ENABLE_STAR_SESSIONS_MIDDLEWARE=true` | Redis session backend (缩 cookie) |
| `OAUTH_*` | SSO 配置 |
| `DEFAULT_USER_ROLE=pending\|user\|admin` | 新 SSO 用户默认角色 |

完整 env 表见 [docs/openwebui-litellm-perkey-binding.md §7](../../docs/openwebui-litellm-perkey-binding.md)。

### 2.2 OWUI 镜像升级

```bash
scripts/jms ssh AIYJY-litellm '
NEW=v0.10.0   # 查 https://github.com/open-webui/open-webui/releases 最新 stable
docker pull ghcr.nju.edu.cn/open-webui/open-webui:$NEW
docker tag  ghcr.nju.edu.cn/open-webui/open-webui:$NEW 127.0.0.1:5000/open-webui:$NEW
docker push 127.0.0.1:5000/open-webui:$NEW
kubectl set image -n open-webui deployment/open-webui open-webui=127.0.0.1:5000/open-webui:$NEW
kubectl rollout status deployment/open-webui -n open-webui --timeout=180s
'
```

**禁止用 `:main` 漂移 tag**, 必须 pin。

### 2.3 查 OWUI sqlite user 表 / 合并账号 / 重置

```bash
scripts/jms ssh AIYJY-litellm "kubectl exec -n open-webui deployment/open-webui -- \
  python3 -c '
import sqlite3
c = sqlite3.connect(\"/app/backend/data/webui.db\")
for row in c.execute(\"SELECT id,email,name,role,oauth_sub FROM user\"):
    print(row)
'"
```

**改 email (用于把 admin 合并到 SSO 账号)**:
```python
c.execute('UPDATE user SET email=? WHERE id=?', ('liuguoxian@auto-link.com.cn','21c0ec...'))
c.commit()
```

**删除遗留 user (默认 admin 等)**:
```python
c.execute("DELETE FROM auth WHERE email IN ('admin@admin.admin','admin@example.com')")
c.execute("DELETE FROM user WHERE email IN ('admin@admin.admin','admin@example.com')")
c.commit()
```

### 2.4 Redis session 排障

```bash
# 1. 确认 REDIS_URL 生效
scripts/jms ssh AIYJY-litellm "kubectl exec -n open-webui deployment/open-webui -- env | grep -E 'REDIS|STAR_SESSIONS'"
# 期望: REDIS_URL=redis://litellm-redis.litellm-product.svc.cluster.local:6379/1
#        ENABLE_STAR_SESSIONS_MIDDLEWARE=true

# 2. Redis 连通性 (跨 ns 内网 DNS)
scripts/jms ssh AIYJY-litellm "kubectl exec -n open-webui deployment/open-webui -- \
  sh -c 'echo PING | nc -w 2 litellm-redis.litellm-product.svc.cluster.local 6379'"
# 期望: +PONG

# 3. 看 OWUI 启动日志确认 Using Redis for session
scripts/jms ssh AIYJY-litellm "kubectl logs -n open-webui -l app=open-webui --tail=200 | grep -i 'using redis\\|sessionmiddleware'"
```

**只设 REDIS_URL 不设 ENABLE_STAR_SESSIONS_MIDDLEWARE → fallback 到 cookie-based**, cookie 巨大, 边界 nginx 必 502。

### 2.4.5 流式不显示 (发消息要刷新才看到) — WS 被公司 LB 拦 🔴

**症状**: OWUI 发消息后页面"等待回复"卡住, 但**刷新页面消息已显示** + LiteLLM SpendLogs 已扣费

**根因**: OWUI Socket.IO 默认 WebSocket transport, 公司 LB (10.68.13.97 nginx) 不识别 WS upgrade, 吃掉 `Sec-WebSocket-Key` header → OWUI 返回 400 → 流式 token 推不到浏览器

**一行修法 (验证过)**:
```bash
scripts/jms ssh AIYJY-litellm "kubectl set env -n open-webui deployment/open-webui \
  ENABLE_WEBSOCKET_SUPPORT=false && \
  kubectl rollout status deployment/open-webui -n open-webui"
```

设 `false` 后 OWUI Socket.IO 走 HTTP long-polling, 公司 LB 当普通 HTTP 处理通过。

**3 层诊断 (定位卡点在哪)**:
```bash
# A. 198 nginx → OWUI (绕公司 LB)
scripts/jms ssh AIYJY-litellm "curl -i --max-time 5 -H 'Host: chat.auto-link.com.cn' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  'http://127.0.0.1/ws/socket.io/?EIO=4&transport=websocket' | head -8"
# 期望: HTTP/1.1 101 Switching Protocols + nginx/1.18.0 (Ubuntu)

# B. OWUI Pod 直访 (绕一切)
scripts/jms ssh AIYJY-litellm "curl -i --max-time 5 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  'http://127.0.0.1:30880/ws/socket.io/?EIO=4&transport=websocket' | head -8"
# 期望: HTTP/1.1 101 + server: uvicorn

# C. 浏览器走完整链路 (本地)
curl -i --max-time 5 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  'https://chat.auto-link.com.cn/ws/socket.io/?EIO=4&transport=websocket'
# 公司 LB 拦的话: 400 "missing Sec-WebSocket-Key" (LB 吃了 header)
```

A+B 通 + C 失败 → 100% 是公司 LB 不支持 WS, 关 `ENABLE_WEBSOCKET_SUPPORT` 改 polling。

### 2.5 OAuth callback 502 分诊决策树

**🔴 真根因 (2026-05-24 踩穿)**: 99% 是 OWUI 默认 `ENABLE_OAUTH_ID_TOKEN_COOKIE=true` 设了**完整 OIDC id_token (2-6KB) 进 Set-Cookie**, 公司 LB 接 response 时 `large_client_header_buffers` 默认 4×8k 撑不住, 拒绝并给浏览器 502。

**最快修法 (一行解决)**:
```bash
scripts/jms ssh AIYJY-litellm "kubectl set env -n open-webui deployment/open-webui \
  ENABLE_OAUTH_ID_TOKEN_COOKIE=false && \
  kubectl rollout status deployment/open-webui -n open-webui"
```
然后让用户**清 chat 域 cookie 重新走一次 SSO**。如果还 502 看下面决策树:

**完整分诊**:

```
1. F12 看 callback 那一行的 Response Headers (Server header 是关键)
   - Server: nginx (无版本) → 502 来自公司边界 LB (server_tokens off), 不是 198
   - Server: nginx/1.18.0 (Ubuntu) → 来自 198 本机 nginx, 检查 nginx error log
   - 没 Server header → 直连 OWUI uvicorn

2. F12 看 callback 后浏览器是否 follow redirect
   - 只有 /favicon.ico 后续请求 → 浏览器实际收到的就是 502, OWUI 307 在上游被吃掉
   - 有 /auth?error=... 或 /auth 请求 → callback 这一跳 OK, 502 在别的 request

3. 浏览器 F12 看 Request Cookie 大小 (request 端的, 看 Cookie: 那一行)
   - > 4KB → owui-session cookie 没走 Redis backend (§2.4 set ENABLE_STAR_SESSIONS_MIDDLEWARE=true)
   - < 200B → request 端 OK, 问题在 response

4. ⭐ 看 OWUI handle_callback 设的 cookie (源码 §2.5 真根因核心)
   scripts/jms ssh AIYJY-litellm "kubectl exec -n open-webui deployment/open-webui -- \
     sed -n '1700,1860p' /app/backend/open_webui/utils/oauth.py | grep -A3 set_cookie"
   - 看到 'oauth_id_token' + 'token' + 'oauth_session_id' 三个 set_cookie → 默认行为, Set-Cookie 含 OIDC id_token 2-6KB → **关 ENABLE_OAUTH_ID_TOKEN_COOKIE**
   - 只看到 'token' + 'oauth_session_id' → 已经关了 legacy cookie, 检查 token JWT 是不是超大

5. 198 nginx access log (诊断盲点!!!)
   scripts/jms ssh AIYJY-litellm 'grep callback /var/log/nginx/access.log | tail -5'
   - 显示 307 → ⚠️ **不代表浏览器收到 307**, 上游 LB 可能改 status code
   - 必须结合 §1 Server header 判断真正 502 来源

6. OWUI 日志看 handle_callback
   kubectl logs -n open-webui -l app=open-webui --tail=30 | grep -iE 'oauth|callback'
   - "Stored OAuth session server-side" + 后续 307 → OWUI 后端通了, 问题 100% 在 Set-Cookie 大小
```

**fake code 测不出真问题** (重要!): `curl /oauth/oidc/callback?code=fake&state=fake` 走的是 error 分支, OWUI 不设大 cookie, 永远返回 307 通畅. **必须看 OWUI 真实成功代码** (oauth.py:1780-1830) 才能看到 oauth_id_token / token 几个大 cookie。

**Response Server header 判断 502 来源 (2 行结论)**:
- `Server: nginx/1.18.0 (Ubuntu)` → 198 本机
- `Server: nginx` (无版本) → 公司边界 LB (`server_tokens off`)

### 2.6 NodePort vs 域名

- 内网 IP 直访 (PoC / 调试): `http://10.68.13.198:30880`
- 公网域名 (生产): `https://chat.auto-link.com.cn` (经 10.68.13.97 SSL 终结 → 198 nginx → OWUI)

### 2.7 OWUI 上线公告 + 用户 onboard

走 SSO 流程, 用户:
1. 飞书登 chat.auto-link.com.cn
2. 如果反代返回 401 (没 cursor-/claude-code- key), 引导去飞书审批表单申请
3. 拿到 key 后再登 OWUI 自动可用

## 3. 关键约束

1. **零中断**: 用 `kubectl set env` / `kubectl set image` 走 rollout, **禁止** `kubectl delete pod`
2. **K8s pod name 小写**: shell `--name owui-XYZ` 必须全小写
3. **镜像必须 push 到 127.0.0.1:5000**: K3s containerd 配的 insecure registry; 外部 ghcr.io/dockerhub 直接拉因 mirror manifest 问题不稳
4. **chmod 600**: `.secrets.env` / `open-webui.yaml` (内含 base64 master key + OAuth secret)
5. **manifest 不入 git**: 198 节点 `/root/open-webui-manifests/` 跟历史 LiteLLM 惯例一致, 不同步到 carher-admin repo

## 4. 踩过的坑

1. **同一台 198 NAT 出口 = 58.241.5.230** (公网入口), 但 198 nginx **只 listen :80**, **没 :443**
   → SSL 终结在 10.68.13.97 公司内网 nginx, **改不到**
2. **bash heredoc 变量替换**: `python3 <<PY` 不带引号, shell 会展开 `$VAR`, 但本地 mac shell 没 export 会变空; 必须 `set -a; . secrets.env; set +a` 后, 远端 python 才能 `os.environ` 读到
3. **kubectl set env 会触发 rollout**, 但 OWUI v0.9.5 启动慢 (~30-60s 跑完所有 alembic migrations); 别 timeout 设太短
4. **OWUI v0.9.5 OAuth merge 按 email**: 改 user 表 email 字段是合并 admin 到 SSO 账号的关键 (§2.3)
5. **OWUI 默认有个 `admin@example.com` user role=pending**: 跟新 admin 同名, 删掉避免迷惑 (§2.3)

## 5. 相关 skill / docs

- [[owui-casdoor-sso]] - SSO 端故障 / 加用户管理
- [[owui-key-swap-proxy]] - 反代代码 / 准入规则
- [[owui-lark-mcp]] - 工具调用
- [[litellm-pro-ops]] - 198 LiteLLM 升级 / 配置
- [[litellm-key-mapping]] - 看 LiteLLM key 分布
- 主文档: `docs/openwebui-litellm-perkey-binding.md`
