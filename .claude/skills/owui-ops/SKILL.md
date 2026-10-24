---
name: owui-ops
description: >-
  198 K3s 上 Open WebUI 内网 chat 服务 (https://chat.auto-link.com.cn) 的综合运维:
  部署/升级 OWUI 镜像、env 变更 (REDIS session / SSO / 模型路由)、查日志、
  account 合并/重置、Redis session 排障、Service NodePort/Ingress 切流。
  Use when 用户提到 "open-webui" / "OWUI" / "chat.auto-link" / "https://chat.auto-link.com.cn" /
  "OWUI 升级 / 卡死 / 登不上 / 模型不对 / cookie / session" / "/oauth/oidc/callback 502" /
  "改 OWUI env" / "OWUI Redis" / "升级 openwebui" / "OWUI 只能用某个模型" 或要 audit OWUI 健康度。
---

# Open WebUI 综合运维 (198 内网)

**现状 (2026-10-24)**: 镜像 `v0.12.3`, 反代 `key-swap-proxy:v0.3.1`, 全员只放 `deepseek-v4.1-flash`(DeepSeek 官方)。

🔴 **OWUI 不吃任何 per-key alias / per-key 兜底**:反代用 **master key** 转发(见 [[owui-key-swap-proxy]]),
所以给 cursor/claude/carher key 批量改 `deepseek-v4.1-flash` 的映射**管不到 OWUI**,它只走该组的**全局**兜底行。
SpendLogs 里认它:`api_key like 'litellm_pr%'`、`requester_ip` = key-swap-proxy 两个 pod IP。
10-15 DeepSeek 官方故障期间(北京 08:11~约 12:00)OWUI 发消息"没有回复" = 官方 p50 170~340s,
不是 OWUI/反代问题;判断先看该时段 `deepseek-v4.1-flash` 的耗时分布,再看 OWUI 日志。

OWUI 跑在 198 K3s `open-webui` namespace, 是内部 chat.auto-link.com.cn 的后端。
**所有上游 LiteLLM 调用都经反代** `key-swap-proxy` (见 [[owui-key-swap-proxy]]),
**身份认证经 Casdoor + 飞书** (见 [[owui-casdoor-sso]]),
**未来工具调用经 lark-mcp** (见 [[owui-lark-mcp]])。

## 1. 快速入口

**登 198 = ssh key 直连 `cltx@10.68.13.198` + `sudo -n`**(本机已配免密), 不走 jms(jms 吞 sudo 密码、PTY 丢块)。
设了 env `P198` 才退回 sshpass+密码。下文老命令里的 `scripts/jms ssh AIYJY-litellm "..."` 一律等价替换成 `_198.sh` 的 `r198 '...'`。

### 1.1 脚本 (`scripts/`, 不用设 env)

| 脚本 | 干什么 | 写? |
|---|---|---|
| `owui-model-gate-audit.sh` | 三层验收「用户只能用名单里的模型」, 名单读反代 CM; 含非流式真发 + 反代回包不是裸 gzip | 只读 |
| `owui-set-allowed-models.sh <a,b> [--apply]` | 换名单一条命令(见 [[owui-key-swap-proxy]] §6.0) | 默认 dry-run |
| `owui-upgrade.sh vX.Y.Z [--set-env K=V] [--apply --announced]` | 升级: 推镜像 → 查 pypatch 挂点 / 残留 Pod → 备份 → 一次重启 → 验收 | 默认 dry-run; 🔴 apply 硬中断 ~1~2min, 不带 `--announced` 拒跑 |
| `_198.sh` | 公共函数: `r198 '<bash>'` 远端 root 跑; `pypod <ns> <deploy> <file.py> [json]` pod 里跑本地 python | — |
| 仓库 `scripts/host-198-registry-mirror.sh <src> <dst>` | 公网多架构镜像 → 198 `127.0.0.1:5000`(只 amd64, 走 k3s ctr) | 只写 registry |
| `pod_*.py` | 上面几个脚本在 pod 里执行的部分, 不单独跑 | — |

`pypod` 把 python 文件 base64 后下发, 参数以 `ARGS` 变量注入(已 import `json`)。在 OWUI pod 里
`from open_webui.utils.auth import create_token` 可以按**真实用户 id** 签 token 测 `/api/*`,
不用拿 admin 代测(admin 看到的和普通用户不一样)。

站点 502 / 告警说「nginx 502」⇒ `scripts/host-198-nginx-5xx.sh [分钟]` (本机直接跑, ssh key + `sudo -n`):
按站点拆 502, 同窗口列 OOMKilled; chat.auto-link 的上游是 `:30880`。

### 1.2 手工

```bash
curl -sS https://chat.auto-link.com.cn/api/version   # 公网, 带版本号
curl -sS http://10.68.13.198:30880/health            # 绕 nginx
kubectl logs -n open-webui -l app=open-webui -f --tail=50
```

**关键文件**:
- 198:`/root/open-webui-manifests/` (manifest + README, ⚠️ 镜像 tag 已陈旧, 现状只认 `kubectl get`)
- PVC `/app/backend/data/pypatch/sitecustomize.py` (本地补丁, 靠 env `PYTHONPATH` 加载)
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

### 2.2 OWUI 镜像升级 —— 用 `scripts/owui-upgrade.sh`

```bash
scripts/owui-upgrade.sh v0.12.4                                    # dry-run: 推镜像+查挂点+查残留 Pod, 不碰线上
# ↓ 先跟用户说一句「站点会断 1~2 分钟」, 再跑
scripts/owui-upgrade.sh v0.12.4 --apply --announced
scripts/owui-upgrade.sh v0.12.4 --set-env A=1 --apply --announced  # 顺带改 env, 只重启一次
```

**升之前**:读 GitHub release 的 `### Changed` 段 + 全文搜 `Database Migrations`/`breaking`/
我们用到的东西(`REDIS`/`WEBSOCKET`/`OAUTH`/`SQLite`)。有 DB 迁移 ⇒ 回滚必须连 PVC 库一起恢复。
给用户讲新特性只挑我们用得上的(飞书 SSO、SQLite、单副本), MCP/Ollama/向量库的修复略过。

脚本把下面这些坑全做成了门禁:
1. **pypatch 挂点**: 补丁换的是 `utils/misc.convert_output_to_messages`、
   `utils/middleware.get_source_context`、`config.OAUTH_PROVIDERS['feishu']` 三个模块属性。
   新版改名不会报错, 只会静默失效 ⇒ 升前 grep 新版镜像里的文件, 升后要日志 `pypatch final:` 三个 True。
2. **残留 Pod**: Recreate 会被非 Running 的老 Pod 永久卡成 0 副本 ⇒ 已退出的(Succeeded/Failed)apply 时自动删, Pending 才拦。
3. **只中断一次**: `rollout pause` → `set image` + `set env` → `resume`。
4. **判版本用 `/api/version`**, 别看 `last-applied-configuration`(陈旧)。
5. 镜像源 `ghcr.nju.edu.cn` → `host-198-registry-mirror.sh` → `127.0.0.1:5000`, 禁止 `:main` 漂移 tag。
6. **验收必须含非流式 chat**(gate audit L1 chat + L2 non-stream)。只看 `/api/version` + 页面能开是假绿:
   10-24 主对话(流式)全好, 标题/追问(非流式)全挂, 只有这一发抓得到。
7. **验收 FAIL 先分锅再回滚**: 看报错落在 OWUI 还是反代/LiteLLM(10-24 是反代), 有 DB 迁移的版本回滚代价大。

v0.11.4 要注意的 changelog: slim 镜像砍了大量能力(**我们用标准镜像, 不受影响**);
`langchain-community` 不再内置(自定义 tool/function import 它要自己声明; 10-07 库里 function 0 个);
Integrations 个人设置页默认隐藏。

| 日期 | 版本 | 停机 | 备注 |
|---|---|---|---|
| 09-19 | v0.9.5 → v0.11.3 | — | 并行会话升的; `config` 表变 key/value 三列 |
| 10-07 | v0.11.3 → v0.11.4 | 19:49~19:51 ≈110s | 同批 `ENABLE_DIRECT_CONNECTIONS=false`; 备份 `webui.db.bak-pre-0114-ds41-20261007` |
| 10-24 | v0.11.4 → v0.12.3 | 15:51:37~15:52:28 ≈51s | 含 DB 迁移(不可降级, 回滚要恢复 `webui.db.bak-20261024T155051`); 升后非流式 0x8b ⇒ 反代 v0.3.1 修(见 [[owui-key-swap-proxy]]); 删了 1 个 16 天前的 Completed Pod; 备份 `/Data/owui-ops/backup-20261024T155051-upgrade-v0.12.3` |

🔴 **拉/推公网镜像一律 `scripts/host-198-registry-mirror.sh`, 别手打 docker pull/tag/push**(10-24 OWUI、sub2api 各撞一次):
198 docker 29 开了 containerd snapshotter, 拉多架构镜像只落 amd64 层, tag 后仍是整个 index ⇒ push 报
`does not provide any platform`; 加 `--platform`、按 amd64 digest 拉都不行。k3s ctr `push --platform` 能推。

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

### 2.3.5 🔴 重启 OWUI = 硬中断几十秒, 必须提前说

OWUI 是 **`strategy: Recreate` + 1 副本** ⇒ 任何 `set env` / `set image` /
`rollout restart` 都是先杀后起, 站点**真的会打不开几十秒**。

这跟 [[owui-key-swap-proxy]] 不一样 —— 反代是 2 副本 RollingUpdate/maxUnavailable=0,
无感。**别把"反代可以随时滚"的直觉套到 OWUI 上。**

⇒ 动 OWUI 之前**先跟用户说一声**。另见
[[feedback_recreate_strategy_stuck_on_completed_pod]] (Recreate 会被僵尸 Pod 永久卡成 0 副本)。

### 2.3.6 知识库 / 文档搜索报 400 —— 向量化模型名不对

**症状**: 上传文档或搜知识库报错, OWUI 日志里 embedding 请求 400。

**根因形状**: OWUI 要的向量化模型名在 LiteLLM 里**不存在**。
2026-09-15 实测: OWUI 存的是 `text-embedding-3-small` (早先禁掉的 OpenAI 那个),
LiteLLM 里现役的叫 **`BAAI/bge-m3`** (openrouter, `model: openrouter/BAAI/bge-m3`,
`model_info.id: openrouter/bge-m3`, `mode: embedding`)。名字对不上 ⇒ 400。

**改法 (只改 OWUI 一侧, 不碰 LiteLLM)**:

```python
# 备份先做: sqlite3 .backup 整库 + 单独导 rag.* 配置
import sqlite3, json
c = sqlite3.connect("/app/backend/data/webui.db")
c.execute("UPDATE config SET value=? WHERE key='rag.embedding_model'",
          (json.dumps("BAAI/bge-m3"),))
c.commit()   # 期望 rowcount == 1
```
然后 `kubectl rollout restart deployment/open-webui` (**注意 §2.3.5, 会中断**)。

⚠️ **`rag.embedding_engine` 保持 `"openai"`** —— 它指的是"走 OpenAI 兼容协议",
不是"用 OpenAI 家的模型", 别顺手改掉。

⚠️ **PersistentConfig: `config` 表压过 env**, 改 Deployment 的环境变量没用。
表结构 `config(key TEXT PK, value JSON, updated_at BIGINT)`。

**验证必须用 OWUI 自己存的那三个值**(base url / key / model), 不要自己挑参数 ——
拿手挑的参数测出 200 是假绿, 证明不了 OWUI 会成功。期望: `200`, 2 条向量, **1024 维**。
另见 [[feedback_verify_embedding_dim_before_claiming]]。

⚠️ 换向量化模型 ⇒ 新老索引坐标系不同, **已有知识库理论上要重建**。
2026-09-15 那次查了库里 **0 个知识库 / 0 条 document / 向量库仅 188K** ⇒ 无事可做。
**先查再说**, 别默认要通知用户重建。

### 2.3.7 "是不是要加内存" —— 先量, 基线在这

2026-09-15 实测基线 (稳态, 无告警):

| | 在用 | limit | |
|---|---|---|---|
| CPU | 119m | 2000m | 6% |
| 内存 | 1001Mi | 4Gi | 24% |
| 磁盘 | 135G | 492G | 29% |

外加 node 的 MemoryPressure / DiskPressure / PIDPressure **全 False**, Pod **0 次重启**。

⇒ **加内存治的是 OOMKilled, 没有 OOM 就不要加**。0 restarts 是最强的反证。
当用户问"是不是要加内存"时, 先把这四个数量出来再回答; 这一轮真正的故障是
§2.3.6 的向量化模型名, 跟资源无关。

**2026-10-21 真 OOM 了, 已加到 6Gi**:
- 形状: 10:17:36 容器 `OOMKilled` exit 137 (内核 memcg: python3 anon-rss ≈4.17GB 顶到 4Gi limit), 40 秒后新容器起来;
  这 40 秒 nginx `connect() failed (111)` → 60 个 502 (终端 `execute`/`ports` + socket.io), heartbeat 报成「LiteLLM 198 告警」。
  稳态只用 0.7~1.1Gi ⇒ 是某个操作瞬间撑爆, 什么撑爆的**没查** (内核日志只留 ~1 天)。
- 判 OOM 的证据: `kubectl -n open-webui get pod <p> -o json` 的 `lastState.terminated.reason` +
  `journalctl -k | grep 'Memory cgroup out of memory'` 里 `cpuset=cri-containerd-<容器id>` 对得上。
  ⚠️ pod 被 rollout 换掉后 lastState 跟着没了; 7 天 4 次重启里前 3 次原因已查不到。
- 改法: `kubectl -n open-webui set resources deploy/open-webui -c open-webui --requests=memory=2Gi --limits=memory=6Gi`
  (备份 `198:/home/cltx/backups/open-webui-deploy-20261021150830.yaml`)。**会触发 Recreate 中断 ~45s (§2.3.5)**。
  选 6Gi 的依据: 节点 127G、可用 ~38G、requests 只分配 20% ⇒ limit 是上限不是占用, request 只多 1G。
- ⚠️ `/root/open-webui-manifests/open-webui.yaml` 仍写 4Gi (cpu limit 也是旧的 2, 线上 4) —— 别 `kubectl apply` 它, 会把内存打回去。

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
6. **改 `webui.db` 前先 `sqlite3 .backup` 整库**: OWUI 只有 1 副本, 改坏了没有第二份。
   备份惯例 `198:/Data/owui-ops/backup-<RUN_ID>/` (整库快照 + 相关配置单独导一份)
7. **dump `rag.*` 配置会连 `rag.openai.api_key` 一起打出来** —— 用 key 名指代, 别回显值

## 5. 相关 skill / docs

- [[owui-casdoor-sso]] - SSO 端故障 / 加用户管理
- [[owui-key-swap-proxy]] - 反代代码 / 准入规则 / 模型白名单
- [[litellm-owui-user-row-reconcile]] - 新人有 key 却登不上 (门禁缺人行)
- [[owui-lark-mcp]] - 工具调用
- [[litellm-pro-ops]] - 198 LiteLLM 升级 / 配置
- [[litellm-key-mapping]] - 看 LiteLLM key 分布
- 主文档: `docs/openwebui-litellm-perkey-binding.md`
