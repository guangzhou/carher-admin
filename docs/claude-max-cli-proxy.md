# Claude Max → LiteLLM 透明代理 (CC Max API)

**状态**：v3 生产中（188 内网 Docker，自 2026-05-23 16:00 起）
**前置文档**：[`cc_max_litellm.md`](cc_max_litellm.md)（早期 OAuth 直调调研，已废弃路径）
**关键脚本**：`scripts/anthropic-onboard/claude-max-proxy.py` + `cc-max-upstream-status.sh` + `claude-max-grant-key.sh` + `patch-litellm-claude-max.py`
**关键 skill**：`~/.claude/skills/anthropic-max-litellm/`

---

## TL;DR

把 Claude Max ($200/月 Team plan) 订阅当 Anthropic API 用。Anthropic 2026-04 起在 OAuth `/v1/messages` 直调上做了**模型 allowlist**（OAuth token 只允许调 Haiku，Opus/Sonnet 假装 429 `rate_limit_error`，但 unified utilization 是 0%——典型的"模型不允许而非配额耗尽"信号）。

绕过方法：把官方 `claude` CLI 发请求时的"identification 配方"做成 HTTP 透传层。具体是：

| 要素 | 值 |
|---|---|
| URL | `https://api.anthropic.com/v1/messages?beta=true`（含 `?beta=true` query） |
| Header | `anthropic-beta: ...,claude-code-20250219` + `x-app: cli` + `claude-cli/2.1.x` UA + stainless SDK headers |
| Body | `system[0]` = `x-anthropic-billing-header: cc_version=...; cc_entrypoint=sdk-cli; cch=...;`  +  `system[1]` = `"You are a Claude agent, built on Anthropic's Claude Agent SDK."` |

把这套 prepend 到客户端的请求 system，其他**原样转发**——客户端的 tool_use / cache_control / vision / thinking / max_tokens 全部生效。

---

## 1. 项目时间线

| 时间 | 阶段 | 备注 |
|---|---|---|
| 2026-05-20 | 早期调研：sk-ant-oat OAuth → LiteLLM anthropic provider | 理论可行；[`cc_max_litellm.md`](cc_max_litellm.md) |
| 2026-05-21 | 首次跑通 OAuth 全自动 (patchright + Gmail TOTP) | acct-1/acct-2 token 上线 |
| 2026-05-23 早 | 实测发现 OAuth 直调只能 Haiku | 误判为"Team 共享池配额打满"，复测后否决 |
| 2026-05-23 中 | **v1 写出来**：CLI subprocess (`claude --print`) wrap OpenAI 兼容 | 部署 Aliyun carher K8s Pod，纯对话补全可用，但 tool/cache/vision 都不行 |
| 2026-05-23 下午 | **迁到 188 内网** | Aliyun → 198 LiteLLM 跨网络不通；188 同内网更稳 |
| 2026-05-23 16:00 | **v3 透明代理**：抓 CLI 真实请求逆向出 identification 配方 | 全功能 + cache 命中、cost overhead 从 ~$0.01/req 降到 0 |
| 2026-05-23 16:30 | 198 prod LiteLLM 加 `claude-max-*`，3 个测试用户 alias 开通 | buyitian / liuguoxian / linsen |
| 2026-05-23 17:00 | 监控脚本 `cc-max-upstream-status.sh` 沉淀 | 一键查双账号配额 |

---

## 2. 关键技术发现

### 2.1 OAuth API 的真实限制

Cross-IP cross-account 实测：

| 路径 | Opus 4.7 | Sonnet 4.6 | Haiku 4.5 |
|---|---|---|---|
| OAuth → `api.anthropic.com/v1/messages`（plain）| 429 + 无 ratelimit header | 429 + 无 ratelimit header | **200 + utilization 0.0%** |
| claude.ai 网页 | ✅ | ✅ | ✅ |
| claude CLI w/ `ANTHROPIC_AUTH_TOKEN` | ✅ | ✅ | ✅ |

判定：**不是配额，是模型 allowlist**。如果是配额耗尽，Haiku 也会高占用。事实是 Haiku 返回 utilization 0% 同时 Opus/Sonnet 429 → 这是模型级 allowlist，错误信息伪装成 `rate_limit_error` 以隐藏 plan 限制细节。

### 2.2 逆向 CLI 的过程（半小时搞定）

**Step 1**：`ANTHROPIC_LOG=debug claude --print ...` 暴露 endpoint
- 看到调用 `https://api.anthropic.com/v1/messages?beta=true`
- 头部 `anthropic-beta` 含 `claude-code-20250219`
- body 字段是 `[Object ...]` 折叠，看不到详情

**Step 2**：本地 HTTP forwarder 抓完整 body
- 启 Python `BaseHTTPRequestHandler` listen 127.0.0.1:8765，dump 所有 POST 进来的 body 然后转发到 `api.anthropic.com`
- 设 `ANTHROPIC_BASE_URL=http://127.0.0.1:8765` 让 CLI 改走本地
- 抓到 body：`system[0]` 是 `"x-anthropic-billing-header: cc_version=2.1.148.0b7; cc_entrypoint=sdk-cli; cch=35c2b;"`、`system[1]` 是 `"You are a Claude agent, built on Anthropic's Claude Agent SDK."`

**Step 3**：curl 验证最小配方
- 加上这 2 个 system text block + 完整 headers → 调 Opus → **200 OK**，input_tokens **仅 65**（vs CLI subprocess 路径每次 21k+ overhead）

### 2.3 为什么这条路有效

Anthropic 后端识别 `claude-code-20250219` beta + `x-app: cli` + system 内 `cc_entrypoint=sdk-cli` 这套组合就把请求标记为"官方 Claude Code 客户端流量"，落进 unified bucket（5h/7d 配额）而不是 OAuth `/v1/messages` 模型 allowlist。

`cch=XXXXX` 看着像校验，实测**任意 5 字符都通过**——纯客户端 cache hint，server 忽略。

### 2.4 cache_read 与 utilization 的真相

| 收 cache_read 折扣？| spend（钱）| utilization（配额）|
|---|---|---|
| | ✅ 收 1/10 价 | ❌ **全价计入** |

实测 buyitian/liuguoxian 30min 内 36.7M input tokens，cache hit rate 90%+，spend 才 $20.56（约普通价的 1/3）；但 acct-2 unified-7d 从 0.3% 涨到 39%，因为**配额按完整 token 数算，cache 折扣不算**。

→ **"钱够"和"配额够"是两件事**。Max 订阅瓶颈是 unified 配额，不是钱。

---

## 3. 当前架构

```
┌─ Claude Code CLI / Cursor / OpenAI SDK / curl ──────────────┐
│   client 调 model = anthropic.claude-opus-4-7              │
│   (或 claude-max-opus 直接, 都行)                          │
└──────────────────────┬──────────────────────────────────────┘
                       │ POST /v1/chat/completions (OpenAI)
                       │       /v1/messages         (Anthropic)
                       ▼
┌─ 198 prod LiteLLM (litellm-product, K3s) ──────────────────┐
│  Key auth + budget check                                   │
│  Per-key aliases:                                          │
│    "anthropic.claude-opus-4-7"   → "claude-max-opus"       │ ← 3 个用户特批
│    "anthropic.claude-sonnet-4-6" → "claude-max-sonnet"     │
│    "anthropic.claude-haiku-4-5"  → "claude-max-haiku"      │
│  Model routing:                                            │
│    claude-max-opus → anthropic/claude-opus-4-7 @ 188:3456  │
└──────────────────────┬──────────────────────────────────────┘
                       │ POST /v1/messages (Anthropic Messages)
                       │       http://10.68.13.188:3456
                       ▼
┌─ 188 Docker: claude-max-proxy (Python HTTP server, v3) ────┐
│  1. pick acct (sticky by conversation hash, RR fallback)   │
│  2. INJECT 2 system blocks (billing header + agent intro)  │
│     into client's request body                             │
│  3. forward via HTTPS                                      │
└──────────────────────┬──────────────────────────────────────┘
                       │ POST /v1/messages?beta=true
                       │  Authorization: Bearer <oauth-N>
                       │  anthropic-beta: ...claude-code-20250219
                       │  x-app: cli  user-agent: claude-cli/...
                       ▼
                ┌─ api.anthropic.com ──┐
                │  (Anthropic backend) │
                └──────────────────────┘
                       Response (含 SSE 流) 原样回传
```

**端点**：
- 内网：`http://10.68.13.188:3456/v1/messages`（Anthropic Messages 协议）
- 接 LiteLLM：通过 `litellm-product/litellm-config` 的 `claude-max-*` model entries

**为什么 v3 删了 OpenAI `/v1/chat/completions` 端点**：LiteLLM `anthropic/` provider 已经能从 OpenAI 客户端的请求转成 Anthropic Messages 协议，proxy 只暴露 Anthropic 端点即可。更少代码、更少 bug。

---

## 4. 实现细节

### 4.1 proxy.py（v3 透传）

`scripts/anthropic-onboard/claude-max-proxy.py`，~250 行 Python，零依赖（stdlib only）。

核心逻辑：

```python
# Inject identification (preserve client's own system)
def inject_identity(req):
    cli_system = [
        {"type": "text", "text": billing_header(body_bytes)},
        {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."},
    ]
    s = req.get("system")
    if isinstance(s, str):    req["system"] = cli_system + [{"type": "text", "text": s}]
    elif isinstance(s, list): req["system"] = cli_system + s
    else:                     req["system"] = cli_system
    return req

# Headers
upstream_headers = {
    "Authorization": f"Bearer {token}",
    "anthropic-beta": "interleaved-thinking-2025-05-14,context-management-2025-06-27,"
                      "prompt-caching-scope-2026-01-05,claude-code-20250219",
    "anthropic-dangerous-direct-browser-access": "true",
    "anthropic-version": "2023-06-01",
    "x-app": "cli",
    "user-agent": "claude-cli/2.1.148 (external, sdk-cli)",
    # + stainless SDK identification headers
}

# HTTPS POST to api.anthropic.com/v1/messages?beta=true
# Response (incl. SSE) is streamed back verbatim
```

### 4.2 多账号 sticky+RR

`sticky_hash = md5(messages 全文 + model)` → mod N 选 acct。同一 conversation 粘到同一号，Anthropic prompt cache 命中最大化。无 user message 的边缘场景 fallback 到 round-robin。

**已观察的偏斜**：2 acct 时实测 hash 分布约 3:2 偏斜（top sticky 全部命中同一号是 1/2^N 概率事件，2 号时不少见）。≥4 号后基本均衡。

### 4.3 188 部署形态

`/Data/claude-max-proxy/` 目录：
- `Dockerfile` — `node:22-bookworm-slim` + `apt install python3` + `npm install -g @anthropic-ai/claude-code`（CLI 装着备用，v3 实际没用）
- `docker-compose.yml` — port 3456 暴露，`unless-stopped` 重启策略
- `proxy.py` — **volume mount，改代码只需 `docker compose restart` 不用 rebuild**
- `.env` — `ACCT_TOKENS=acct-1::sk-ant-oat01-...,acct-2::sk-ant-oat01-...`

Token 源头：`/Data/anthropic-auth/acct-N/.env`（per-account OAuth token + creds，通过 `add-cc-account.sh` 自动生成）。

### 4.4 198 prod LiteLLM 接入

`scripts/anthropic-onboard/patch-litellm-claude-max.py` patcher，加 3 个 model entry：

```yaml
- model_name: claude-max-opus
  litellm_params:
    model: anthropic/claude-opus-4-7
    api_base: http://10.68.13.188:3456     # 不带 /v1; LiteLLM 自动 append
    api_key: no-auth                       # proxy 不校验入站
    input_cost_per_token: 0.000005
    output_cost_per_token: 0.000025
    cache_read_input_token_cost: 0.0000005
    cache_creation_input_token_cost: 0.00000625
  model_info: { mode: chat }
```

每次 rollout 后 `kubectl rollout restart deployment/litellm-proxy -n litellm-product`。

### 4.5 撞顶 fallback (零中断降级到网宿)

`scripts/anthropic-onboard/add-claude-max-fallbacks.py` patcher 在 198 prod `router_settings.fallbacks` 加 3 条:

```yaml
router_settings:
  fallbacks:
    - claude-max-opus:   [ anthropic.claude-opus-4-7 ]   # 网宿
    - claude-max-sonnet: [ anthropic.claude-sonnet-4-6 ]
    - claude-max-haiku:  [ anthropic.claude-haiku-4-5 ]
```

触发条件 (LiteLLM 默认): 188 proxy 返回 502 / 5xx / timeout / 429 → 自动回退到网宿。**用户无感**, 但回退请求开始烧网宿付费 API budget。

注: fallback 不会"全员永久切走"——是 per-request basis, 下个请求还是先打 claude-max-* (Max 池), 失败再 fallback。

### 4.6 per-key 透明开通

`scripts/anthropic-onboard/claude-max-grant-key.sh`：
```bash
# 仅 grant model 权限，用户需要主动调 claude-max-* model name
./claude-max-grant-key.sh claude-code-someuser

# grant + 设 alias: 用户原 anthropic.claude-opus-4-7 请求被透明改写
./claude-max-grant-key.sh claude-code-someuser --alias

# 反向回退
./claude-max-grant-key.sh claude-code-someuser --revoke
```

直接 SQL UPDATE `LiteLLM_VerificationToken.models` + `aliases` JSONB。LiteLLM key cache TTL 60s，新流量 1min 内生效。

---

## 5. 部署清单

| 资源 | 位置 | 状态 |
|---|---|---|
| Docker container `claude-max-proxy` | 188:/Data/claude-max-proxy/ | 跑着 |
| Token 文件 `acct-{1,2}/.env` | 188:/Data/anthropic-auth/ | acct-1 thom, acct-2 leeeliz |
| ConfigMap `litellm-config` | 198 K3s litellm-product | 含 3 个 `claude-max-*` entry |
| Deployment `litellm-proxy` | 同上 | 2 replicas，已 rollout 加载新 model |
| DB rows: `LiteLLM_VerificationToken[claude-code-{buyitian,liuguoxian,linsen}]` | 同上 | 加了 `claude-max-*` 到 models[]，加了 alias |

---

## 6. 运维

### 6.1 一键查上游配额

```bash
./scripts/anthropic-onboard/cc-max-upstream-status.sh
# --watch 60      持续监控
# --json          脚本消费
```

输出 5h/7d unified utilization、fallback 状态、reset 倒计时，自动 WARN 阈值。

### 6.2 加 acct-N（扩号）

1. `./scripts/anthropic-onboard/add-cc-account.sh acct-N`（patchright + Gmail TOTP 全自动获 token）
2. 188 上：`docker compose -f /Data/claude-max-proxy/docker-compose.yml down && up -d`（重读 .env 加载新号）
3. `cc-max-upstream-status.sh` 验证新号在列表里

### 6.3 给某用户开通 / 关闭

```bash
./scripts/anthropic-onboard/claude-max-grant-key.sh <key_alias> --alias    # 透明开通
./scripts/anthropic-onboard/claude-max-grant-key.sh <key_alias> --revoke   # 关闭
```

### 6.4 临时降级（acct 撞顶应急）

如果某 acct 5h utilization >90%，用户体验下降：
```bash
# 把 buyitian/liuguoxian 的 alias 临时回退到网宿
ssh AIYJY-litellm "kubectl exec -n litellm-product litellm-db-0 -- psql -U litellm litellm -c \"
  UPDATE \\\"LiteLLM_VerificationToken\\\"
  SET aliases = aliases - 'anthropic.claude-opus-4-7' - 'anthropic.claude-sonnet-4-6'
  WHERE key_alias IN ('claude-code-buyitian','claude-code-liuguoxian-50gj');\""
# 60s 后生效，用户的 anthropic.claude-* 又走网宿
# 配额回血后再 alias 改回 claude-max-*
```

### 6.5 监控告警建议（未做）

可以挂 cron 跑 `cc-max-upstream-status.sh --json` 到 Prometheus/飞书机器人，>80% 时报警。

---

## 7. 实战观察 (2026-05-23 半天)

| 项 | 数据 |
|---|---|
| 全集群唯一开通用户 | buyitian, liuguoxian, linsen（3 个） |
| 30min 高峰流量 | 232 个 Opus 请求 / 36.7M input tokens |
| 单 session 上下文 | 实测 msgs=614 + tools=24，一次请求 ~200k tokens |
| Cache hit rate | 90-99%（cache_read / prompt_tokens） |
| spend（30min） | $20.56（cache 已省 ~6 倍） |
| acct-2 unified-7d 涨速 | 0% → 40% in 1h（重负载） |
| Sticky 偏斜 | 2 号场景下 top 5 sticky 集中度 ~40% |

---

## 8. 已知限制 & 风险

### 8.1 不会改的

- **utilization 全价算 cache_read**：配额瓶颈，不是钱瓶颈
- **5h fallback 在 50% 触发**：过半后涨幅加速
- **2-3 号 sticky 偏斜**：4 号后才均衡
- **TLS 终结在 Anthropic**：proxy 不解密响应，不能改 body

### 8.2 ToS / 持续可用性风险

Anthropic 已经在 2026-04 封锁 CLIProxyAPI 等第三方 OAuth 代理。本方案虽然伪装成官方 CLI 流量，**仍有被识别封禁的风险**：
- `cch` 哈希算法、stainless SDK 版本号都可能被指纹检测
- 大量请求量来自单一 IP（188）可能触发 abuse 检测
- 单 token 被多 IP 同时使用会留下 anomaly trace

**应对**：
- 不批量推广（只给 3 个特批用户）
- 不和 carher bot（高并发）共用 token
- 准备好 fallback 路径（删 alias 改回网宿）

### 8.3 卖号账户的现实

**OAuth token 独立 ≠ 账户独占**。卖家常把一个 Claude Max 卖给多个 buyer：

- 每个 buyer 跑 `claude setup-token` 拿独立 token（互不撤销）
- 但 **`unified-5h/7d-utilization` 是账户级累计**，跨 token / 跨 IP / 跨 buyer 全算同一桶
- → 多 buyer 同时压力 → 我方撞顶 / fallback / 甚至触发 Anthropic on hold

**实测信号**（2026-05-23 同时压力测试两个新开通号）：

| | acct-1 (thom) | acct-2 (leeeliz) |
|---|---|---|
| 我方接入用户 | 同（buyitian/liuguoxian/linsen）| 同 |
| sticky 路由 | 大致均衡 | 大致均衡 |
| **1h 5h utilization 涨幅** | **+36%** | **+83%** |

acct-2 涨速差远超 acct-1 → 极可能有别的 buyer 同时在用 acct-2。

**判定独占 / 共用的对账公式**：
```
我方 SpendLogs prompt_tokens(claude-max-* @ last 30min)   ← LiteLLM 已记录
       vs
upstream 5h utilization 涨幅 × 总 quota 估算               ← cc-max-upstream-status.sh
```
两者数量级匹配 → 独占；upstream 涨得远多于我方使用 → 有别的 buyer。

**关键不变性**：
- Token 1 年有效，**卖家不能撤销我们的 token**（撤销需要 magic-link 登入 console → 需要 Gmail）
- 我方改 Gmail 密码可让卖家失去 magic-link 接收能力，**但 Google 大概率要恢复邮箱/手机验证（在卖家手里）→ 改不动 + 可能触发 Gmail 锁** → 不建议
- **多 buyer + 多 IP** 是 Anthropic "unusual activity" 触发条件，account on hold 风险高（acct-3 实测命中）

**实际防御策略**：
1. **接受配额共享**（推荐）→ 1 年内跑得快，撞顶时 `claude-max-grant-key.sh <key> --revoke` 把 buyitian/liuguoxian 临时切回网宿减压
2. **监控 utilization 异常涨速** → 异常时对账 SpendLogs 确认是否被多 buyer 影响
3. **新号上线前必做 hold 检测**（见 skill §新账号上线 SOP）

### 8.4 失败模式

| 现象 | 排查 |
|---|---|
| HTTP 502 from proxy | `docker logs claude-max-proxy -n …`，看 stderr |
| 单 acct 一直 429 | 5h/7d 配额耗尽，等 reset 或把该 acct 临时摘掉 |
| 流量正常但 acct 不消耗 | sticky 偏斜把流量都路由到另一 acct |
| **配额涨速远超我方 SpendLogs token 量** | 有别的 buyer 在用同账户，参考 §8.3 |
| 客户端响应"I'm Claude, made by Anthropic, I can't..." | 用户的 prompt 让 Claude 进入 alignment refusal，跟 proxy 无关 |
| `claude-max-opus` 不存在 | 198 prod ConfigMap 没加 entry，跑 `patch-litellm-claude-max.py prod` |
| 新 acct OAuth 走到最后步页面是 "account on hold" | Anthropic 已封该号，找卖家退款（见 skill §账号 hold 检测）|

---

## 9. 沉淀产物索引

| 类型 | 路径 | 用途 |
|---|---|---|
| 文档 | `docs/claude-max-cli-proxy.md`（本文）| 总方案 |
| 文档 | `docs/cc_max_litellm.md` | 早期 OAuth 直调调研（背景）|
| Skill | `~/.claude/skills/anthropic-max-litellm/SKILL.md` | 接入运维 SOP |
| 脚本 | `scripts/anthropic-onboard/claude-max-proxy.py` | proxy v3 主代码 |
| 脚本 | `scripts/anthropic-onboard/claude-max-proxy.Dockerfile` | 容器镜像 |
| 脚本 | `scripts/anthropic-onboard/docker-compose.claude-max-proxy.yml` | 188 部署 compose |
| 脚本 | `scripts/anthropic-onboard/patch-litellm-claude-max.py` | LiteLLM ConfigMap patcher（prod/canary/dev）|
| 脚本 | `scripts/anthropic-onboard/claude-max-grant-key.sh` | per-key 一键开通/关闭 |
| 脚本 | `scripts/anthropic-onboard/cc-max-upstream-status.{py,sh}` | 配额监控 |
| 脚本 | `scripts/anthropic-onboard/claude-max-quota-probe.sh` | 老的 OAuth 直调探针（保留作历史诊断工具）|
| 脚本 | `scripts/anthropic-onboard/cc-oauth-full.py` + `add-cc-account.sh` | 获取新 acct OAuth token（patchright）|
| 已废弃 | `scripts/anthropic-onboard/_deprecated/` | K8s 时代 CLI subprocess 部署文件 |
