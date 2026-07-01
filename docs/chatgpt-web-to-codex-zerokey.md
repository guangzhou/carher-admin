# ChatGPT Web → Codex/OpenAI API bridge (zerokey on 188)

> **沉淀索引**（skill / 脚本 / 验证命令）：[zerokey-codex-artifacts.md](./zerokey-codex-artifacts.md)  
> **Agent 全能力（规划中）**：[zerokey-codex-agent-bridge-plan.md](./zerokey-codex-agent-bridge-plan.md)

将 ChatGPT **网页版聊天额度**桥接成 OpenAI 兼容 API，给 Codex / VS Code / 任意
OpenAI 客户端使用。用于 Codex 自身的 5h/7d 额度耗尽、但网页对话仍可用时，借网页
额度继续跑。

- 落地服务器：`188` / `10.68.13.188`（JP 出口，cf_clearance 绑该出口 IP）
- 监听：`http://10.68.13.188:8123`
- 独立 Docker 栈，**不碰** K8s / carher-admin / operator / 任何现有服务
- 上游：`zerokey`（Node，回放一个抓到的浏览器 `fetch` 请求）

> 部署隔离提醒：本桥接与 `her/carher-admin`、`her/carher` 两条流水线无关，纯属
> 188 上的本地工具栈，勿与 bot 实例部署混用。

## 仓库内文件

```
scripts/chatgpt-onboard/zerokey-codex/
  install.sh                     # 克隆上游 + 套补丁 + 建目录布局
  zerokey-patch/                 # 我们对上游的最小改动 + 新增文件
    routes/raw.js                # ★ 新增：raw 直通 + 模型解析 WEB_MODELS/ALIASES
    routes/chatgpt.js            # 改：raw 分支 + model 透传（vscode 路径不变）
    core/chatgpt/api.js          # 改：chatCompletion/_prepareConversation 接收 model
    config/constants.js          # 改：/v1/models 返回真实 web slug
    zerokey-serve-codex.js       # 无头启动器（Bearer 选路：vscode / raw）
    Dockerfile / .dockerignore / docker-compose.yml   # 容器化（restart:always）
  capture/
    Dockerfile                   # patchright 抓取镜像（修了 xvfb-run PID1 死锁）
    zerokey-web-capture.py       # 登录 chatgpt.com 抓 /backend-api/f/conversation
  ops/
    refresh.sh                   # 重抓 session → 校验 → 原子换 users.json → 重启 + 告警
    capture-manual.sh            # 需 OTP 的交互式重抓
    add-account.sh               # 新账号 onboarding（全自动 OTP + 812x 端口）
    docker-compose.account.yml   # 每账号 compose 模板
    litellm-register-zerokey.py  # 198 幂等注册 zerokey 模型 + manifest 同步
    README.md                    # 运维手册（部署/客户端/刷新/排错）
```

188 上的运行布局（由 `install.sh` 生成）：`~/zerokey-codex/{zerokey,state,secrets,capture,ops,logs}`。

## 工作原理

1. **抓取**（`zerokey-web-capture.py`，patchright 真 Chrome + xvfb）：在 188 上登录
   chatgpt.com，发一条消息，拦截真正的 `POST /backend-api/f/conversation`，把
   完整 headers（含 `openai-sentinel-proof-token`、`cookie` 内 `cf_clearance`、
   `authorization`）+ body 存成 zerokey 的 `temp/users.json`。
   - **必须在 188 抓**：`cf_clearance` 与出口 IP 绑定，换机即失效。
   - sentinel proof token 是 zerokey 解出真实 UA + POW 配置的关键，纯 OAuth token 不够。
2. **回放**（zerokey + `zerokey-serve-codex.js`）：用抓到的请求作为模板，把
   OpenAI `/v1/chat/completions` 请求改写后打到 chatgpt.com 网页后端，流式回传。

## 两种请求模式（用 Authorization 选路）

| `Authorization` | 路径 | 行为 |
|---|---|---|
| `Bearer vscode`（默认） | ToolCompiler | 注入 VS Code 工具语法；有状态网页会话。**上游原行为，未改。** |
| `Bearer raw`（或 `codex`/`openai`/`plain`） | raw 直通（新增） | 不注入工具语法；无状态——每次把完整 message 历史拼平发送（标准 OpenAI 语义）；支持流式 + 非流式 |

raw 直通点在 `routes/chatgpt.js` 顶部分支 `RAW_IDES.has(req.ide)` →
`routes/raw.js:rawComplete()`，与 vscode 路径完全隔离。

## 模型选择（两路都生效）

请求 `model` 透传到 web 后端。`GET /v1/models` 列出该账号真实可用 slug：

```
gpt-5-5-pro  gpt-5-5-thinking  gpt-5-5  gpt-5-5-instant
gpt-5-4-pro  gpt-5-4-thinking  gpt-5-4-t-mini
gpt-5-3  gpt-5-3-instant  gpt-5-3-mini  gpt-5-2  gpt-5-1  gpt-5  gpt-5-mini
o3  o3-pro  gpt-4-5  research(Deep Research)  agent-mode
```

别名：`gpt-4o→gpt-5-mini`、`gpt-5.5→gpt-5-5`、`gpt-4.5→gpt-4-5`、`o3-mini→gpt-5-3-mini` 等。
省略 `model` 用 `ZK_DEFAULT_MODEL`（compose 默认 `gpt-5-5`）。来源：`GET /backend-api/models`。

## Codex 客户端配置（推荐：经 198 LiteLLM）

**2026 起 Codex IDE/CLI 只支持 `wire_api = "responses"`**（`/v1/responses`），
不再支持 `wire_api = "chat"`。zerokey 本身只有 `/v1/chat/completions`，因此 **Codex
必须走 198 LiteLLM**，并在 proxy 的 zerokey 模型上启用
`use_chat_completions_api: true`（已在 live ConfigMap 配置；Codex 打 responses，
LiteLLM 桥接到 zerokey 的 chat/completions）。

本机 `~/.codex/config.toml`（经公网入口或内网 NodePort 均可）：

```toml
model = "zerokey-gpt-5.5"          # 或 zerokey-timothy-gpt-5.5
model_provider = "litellm_pro"

[model_providers.litellm_pro]
name = "198 LiteLLM Pro"
base_url = "https://cc.auto-link.com.cn/pro/v1"   # 内网可用 http://10.68.13.198:30402/v1
env_key = "LITELLM_API_KEY"                       # 你的 LiteLLM sk- key
wire_api = "responses"
requires_openai_auth = false
```

验证（与 Codex 相同协议）：

```bash
curl -s -X POST "$BASE/v1/responses" -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" \
  -d '{"model":"zerokey-gpt-5.5","input":"Say hi","stream":true}'
```

> **Codex IDE（桌面 GUI）对自定义模型基本不可用**（调研 `openai/codex` issues，2026-06）：
> - picker 被 `requiresAuth` 门控：自定义 provider 设 `requires_openai_auth=false` 时
>   选择器 UI **直接不渲染**（#10867、#15138）。逼出来需 `requires_openai_auth=true` + 假 key。
> - 对话模型列表来自 OpenAI 账号维度的 Statsig 远程 allowlist；自定义 provider 返回空，
>   picker 无内容（#15138）。
> - `model_catalog_json` 只接受 OpenAI 已知 slug，`zerokey-pool` 等自定义 ID 会被过滤丢弃
>   （#10867、#19694）。
> → **Codex 这边请用 CLI/TUI**（`codex -p zerokey-pool` / `-p zkagent`），名字任意原样透传，
>   不受 GUI allowlist 限制。**Cursor 不受此限**，可自由添加自定义模型名。

### 直连 188 zerokey（仅旧版 Codex / 非 Codex 客户端）

若客户端仍走 OpenAI **Chat Completions**（Cursor 部分路径、curl 测试），可直连 188：

```toml
[model_providers.chatgpt-web]
base_url = "http://10.68.13.188:8123/v1"   # timothy 用 :8124
env_key = "ZK_KEY"                           # export ZK_KEY=raw
wire_api = "chat"                            # 新版 Codex 已不支持此值
```

`export ZK_KEY=raw` → `Authorization: Bearer raw`（raw 直通档）。

**限制（当前）**：经 LiteLLM + `Bearer raw` 时，Codex Agent 的 `apply_patch` / `shell`
工具链不完整（LiteLLM 桥会 drop 工具，raw 无 ToolCompiler）。对话与代码生成正常；
**完整 Agent 改文件**需专用 `Responses ↔ ToolCompiler` 桥（见
[zerokey-codex-agent-bridge-plan.md](./zerokey-codex-agent-bridge-plan.md)）。

## 接入 198 LiteLLM Pro（litellm-product）

zerokey 同时作为上游模型挂进 198 LiteLLM Pro（K3s，ns `litellm-product`，
NodePort 30402）。任意 LiteLLM 消费者（Cursor / Codex / claude-code 的 key）按
模型名即可借到 ChatGPT 网页额度。198（`AIYJY-litellm`）内网直连 188，无需隧道。

模型条目写在 ConfigMap `litellm-config` 的 `model_list`，照搬现有 188 来源
（openrouter）的写法，插在 `router_settings:` 之前：

```yaml
- model_name: zerokey-gpt-5.5         # 另含 -5.5-thinking / -5.5-pro / zerokey-o3
  litellm_params:
    model: openai/gpt-5-5             # web slug；openai/ provider
    api_base: http://10.68.13.188:8123/v1
    api_key: raw                      # 字面量 → Bearer raw → raw 直通
    use_chat_completions_api: true    # Codex /v1/responses → chat/completions 桥接
    input_cost_per_token: 0
    output_cost_per_token: 0
```

| LiteLLM 模型名 | web slug |
|---|---|
| `zerokey-gpt-5.5` | gpt-5-5 |
| `zerokey-gpt-5.5-thinking` | gpt-5-5-thinking |
| `zerokey-gpt-5.5-pro` | gpt-5-5-pro |
| `zerokey-o3` | o3 |
| `zerokey-timothy-gpt-5.5` | gpt-5-5（`:8124`） |
| `zerokey-timothy-gpt-5.5-thinking` | gpt-5-5-thinking |
| `zerokey-timothy-gpt-5.5-pro` | gpt-5-5-pro |
| `zerokey-timothy-o3` | o3 |

**标准注册**（仓库 `ops/litellm-register-zerokey.py`，仅 touch zerokey 条目）：

```bash
./scripts/jms scp scripts/chatgpt-onboard/zerokey-codex/ops/litellm-register-zerokey.py \
  AIYJY-litellm:/tmp/litellm-register-zerokey.py
./scripts/jms ssh AIYJY-litellm 'python3 /tmp/litellm-register-zerokey.py --apply --sync-manifest'
```

手动改 cm（cm 是 JSON-in-JSON，**别** `kubectl apply` 旧 manifest）：
`kubectl get cm ... -o json` → splice → `kubectl replace` →
`kubectl rollout restart deployment/litellm-proxy -n litellm-product`。
脚本会先备份 cm 到 `~/zerokey-litellm-backups/` 并同步
`~/litellm-product-manifests/30-cm-litellm-config.yaml`。

验证（NodePort 30402，master key 取自 `litellm-secrets`）：

```bash
# Codex 路径（responses）
curl -s -X POST -H "Authorization: Bearer $MK" localhost:30402/v1/responses \
  -d '{"model":"zerokey-gpt-5.5","input":"hi"}'
# Chat Completions 路径
curl -s -X POST localhost:30402/v1/chat/completions -H "Authorization: Bearer $MK" \
  -d '{"model":"zerokey-gpt-5.5","messages":[{"role":"user","content":"hi"}]}'
```

注册/修复脚本（仓库 `ops/litellm-register-zerokey.py`，198 上 `--apply --sync-manifest`）。

per-user 访问：master key 已可用；普通 key 要调，需把 `zerokey-*` 加进该 key 的
`models` allowlist（走 `/key/update`，见 `litellm-pro-ops`）。容量提醒：同一端口下的
模型名共用该端口的 web 会话，适合个人/低并发，高并发会被 web 端限流。

### 多账号（每账号独立端口）

| 账号 | 188 端口 | LiteLLM 模型前缀 | 目录 |
|---|---|---|---|
| kristine（默认） | 8123 | `zerokey-gpt-5.5` 等 | `~/zerokey-codex/` |
| timothy | 8124 | `zerokey-timothy-gpt-5.5` 等 | `~/zerokey-codex-accounts/timothy/` |
| zyq | 8125 | `zerokey-zyq-gpt-5.5` 等 | `~/zerokey-codex-accounts/zyq/` |
| owp | 8126 | `zerokey-owp-gpt-5.5` 等 | `~/zerokey-codex-accounts/owp/` |
| hgg | 8127 | `zerokey-hgg-gpt-5.5` 等 | `~/zerokey-codex-accounts/hgg/` |
| dvo | 8128 | `zerokey-dvo-gpt-5.5` 等 | `~/zerokey-codex-accounts/dvo/` |

**`zerokey-pool`（负载均衡）**：6 个账号同时挂在 litellm-dev（198 NodePort `30400`）的
router 下，模型名 `zerokey-pool` 会在 6 个端口间轮询/故障转移，适合多人共享、提高单账号
限流上限。本机 Codex 用 `codex -p zerokey-pool`（profile 见 `~/.codex/config.toml`，
provider `carher_dev_k8s` → `http://10.68.13.198:30400/v1`）。

新增账号（188 上，全自动 mail.com OTP + 首次 capture）：

```bash
cd ~/zerokey-codex/ops
./add-account.sh <account_id> <email> '<mail_pw>' '<chatgpt_pw>' [port]
# 已落地示例：
./add-account.sh timothy timothy_mossey871@mail.com '<mail_pw>' '<gpt_pw>' 8124
```

`add-account.sh` 会：`OTP_AUTO_ONLY=1` 跑 capture → 写 `state/users.json` →
起容器 `zerokey-codex-<account>`。登录/OTP 逻辑见 skill `chatgpt-login-session`
（改编自 [chatgpt-codex-skills-bundle](https://github.com/joeeeeey/chatgpt-codex-skills-bundle)）。

198 侧为每个端口各加一组 `model_list`（`api_base` 指向对应端口），命名建议
`zerokey-<account>-*` 避免与 kristine 冲突。

## 部署 / 管理

```bash
# 首装
./install.sh                 # 仓库内 scripts/chatgpt-onboard/zerokey-codex/
# 填密码 + 建镜像见 install.sh 末尾提示
# 抓一次 session（见 ops/README.md）后：
cd ~/zerokey-codex/zerokey && docker compose up -d --build
docker compose logs -f
curl -s localhost:8123/v1/models | head
```

## 会话刷新

`ops/refresh.sh`：复用 `state/profile` 重抓（登录态在则免 OTP）→ 校验关键头 →
原子替换 `state/users.json` → 重启容器；失败保留旧会话 + 写 `state/REFRESH_STALE`
+（设了 `ZK_ALERT_WEBHOOK` 则）推告警。cron 例：

```cron
0 */6 * * * ~/zerokey-codex/ops/refresh.sh >/dev/null 2>&1
0 */6 * * * ~/zerokey-codex-accounts/timothy/ops/refresh.sh >/dev/null 2>&1
0 */6 * * * ~/zerokey-codex-accounts/zyq/ops/refresh.sh >/dev/null 2>&1
0 */6 * * * ~/zerokey-codex-accounts/owp/ops/refresh.sh >/dev/null 2>&1
0 */6 * * * ~/zerokey-codex-accounts/hgg/ops/refresh.sh >/dev/null 2>&1
0 */6 * * * ~/zerokey-codex-accounts/dvo/ops/refresh.sh >/dev/null 2>&1
```

需 OTP 时（`REFRESH_STALE` / 告警）：`ops/capture-manual.sh` 然后
`echo <code> > ~/zerokey-codex/state/out/otp.txt`。

## 踩过的坑（诊断纪律：假设 → 证伪 → 数据）

### 1. capture 容器静默卡死，python 不启动
- **假设**：`ENTRYPOINT ["xvfb-run","-a","python",...]` 能正常起 python。
- **证伪条件**：若成立，容器内进程树应有 `python` + `chrome`，且有脚本日志。
- **数据**：进程树只有 `/bin/sh /usr/bin/xvfb-run`（PID 1）+ `Xvfb`，**无 python**，
  `docker logs` 全空。改 `bash -lc "xvfb-run ..."` 仍 PID1 —— 因 bash 对单条命令做
  exec 优化，xvfb-run 又变 PID1。→ 结论：**xvfb-run 当 PID1 会在 exec python 前卡住**。
- **修复**：`ENTRYPOINT ["bash","-lc","xvfb-run -a python /capture/...; exit $?"]`，
  末尾 `; exit $?` 使其成为命令列表、关闭 exec 优化，bash 保持 PID1、xvfb-run 作子进程。
  （验证：进程树出现 bash→xvfb-run→python→patchright，脚本正常打日志。）

### 2. OTP 成功但 chatgpt.com 落地匿名（已解）
- **假设**：OTP 过后 persistent profile 会持久化 chatgpt.com 登录态，后续刷新免 OTP。
- **证伪条件**：若成立，OTP 完成后用该 profile 开 chatgpt.com 应为登录态
  （有 composer、无 “Log in” 按钮）。
- **数据（初次）**：OTP 在 `auth.openai.com/email-verification` 提交成功 → 跳 chatgpt.com
  却 `login_btn=2, composer=1`（匿名落地页）；脚本正确拒绝保存匿名会话；事后单独
  探针复测 profile 仍 `ANON`。→ 指向 **session cookie 在 OAuth 回调后「迟到」**，
  脚本在 cookie 落地前就判定 anonymous 并关闭 context。
- **修复**：在 `zerokey-web-capture.py` 的 post-OTP settle 后增加 **late-cookie 重载重试**
  （`is_logged_in` 为假时 reload chatgpt.com 最多 4 次，每次 +clear_cf +sleep），让会话
  cookie 有时间落地；并加 `otp-submitted` / `post-otp-settled` 截图便于诊断。
- **数据（修复后）**：`post-OTP login state=True` → `[CAPTURED] f/conversation (24 headers)`
  → 探针复测 profile `LOGGED_IN`。**无人值守 refresh 实测**：`[1] reusing persisted
  session (already logged in)` → 抓取 → 换 → 重启，~26s，**全程无 OTP**。→ 已解。

### 3. mail.com 自动取 OTP 不稳
- 现象：inbox 骨架屏 stall、关键词不出现。→ 已加 **文件兜底**：
  脚本进入 `OTP_WAIT_FILE` 阶段后读 `state/out/otp.txt`，人工 `echo <code>` 即可。
- 注意：脚本先跑 ~90s mail.com 自动重试，**之后**才进文件等待阶段；该阶段进入时会
  先清空 otp.txt，故需在它打印 `>>> OTP_WAIT_FILE` 后再写（或被清空后补写一次）。

### 4. cf_clearance / sentinel 短寿 + IP 绑定
- 抓取与服务都必须在 188；这些头数小时即过期，故需周期性 refresh。

### 5. 经 LiteLLM 调用非流式报 “Empty or invalid response from LLM endpoint”（已解）
- **假设**：raw 路径没正确处理 `stream:false`。
- **证伪条件**：若 raw 支持非流式，直连 188 显式传 `stream:false` 应返回单个
  `chat.completion` JSON 而非 SSE。
- **数据**：直连 188 显式 `stream:false` → 返回了正确 JSON（`object:"chat.completion"`，
  content 正常）。假设被证伪。真因：`routes/raw.js` 默认 `stream = true`，而 OpenAI
  规范规定**省略** `stream` 即非流式；LiteLLM 的 OpenAI SDK 非流式调用会省略该字段，
  zerokey 却默认流式回 SSE → LiteLLM 解析失败。
- **修复**：`routes/raw.js` 默认改为 `stream = false`（显式 `stream:true` 仍流式）。
  改后须 `docker compose up -d --build` rebuild。验证：经 198 非流式 → `PONG-NS`，
  流式 → token 增量正常。

## 相关 Skill

| Skill | 路径 |
|-------|------|
| zerokey 主 skill | `.codex/skills/chatgpt-web-to-codex-zerokey/SKILL.md` |
| zerokey 主 skill（中文详版） | `.claude/skills/chatgpt-web-to-codex-zerokey/SKILL.md` |
| 登录 / OTP / 新账号 | `.codex/skills/chatgpt-login-session/SKILL.md` |

## 现状（截至 2026-06-18）

- ✅ vscode 路径不变（`VSCODE_OK`，grammar promptLength 2925）。
- ✅ raw 直通（`gpt-5-mini→42`、`PROD_OK`、流式 SSE 干净）。
- ✅ 多模型（请求 `model` 透传两路生效，`/v1/models` 真实 19 slug）。
- ✅ 容器化 `restart:always` + healthcheck（已替换裸 node 占 8123）。
- ✅ 自动刷新：late-cookie 修复后，profile 持久登录；**无人值守 refresh 实测通过**
  （reuse 已登录 profile，无 OTP，~26s 完成换会话 + 重启）。
- ✅ cron 已装（每 6h）：kristine `~/zerokey-codex/ops/refresh.sh`；timothy `~/zerokey-codex-accounts/timothy/ops/refresh.sh`（与 quota-rebalance cron 并存）。
- ✅ 接入 198 LiteLLM Pro：`zerokey-gpt-5.5 / -thinking / -pro / zerokey-o3` 四个模型，
  经 NodePort 30402 流式 + 非流式均验证通过；修复了 raw.js 的 stream 默认值（trap 5）。
- ✅ 第二账号 **timothy**：188:8124、`zerokey-codex-timothy`；198 侧 `zerokey-timothy-*`
  四模型；manifest 已 `--sync-manifest` 防 stale apply 回滚。
- ✅ Codex `wire_api=responses` 经 198 `/v1/responses` → zerokey 对话路径验证通过。
- ✅ **扩容到 6 账号（2026-06-21）**：新增 zyq:8125 / owp:8126 / hgg:8127 / dvo:8128，
  注册进 **litellm-dev**（198 NodePort `30400`），并加 `zerokey-pool` router 负载均衡组。
  Mac 直连 `30400/v1/responses` 实测 `zerokey-pool` 返回原生 Responses 对象（OK）。
  本机 `~/.codex/config.toml` 已加 `[profiles.zerokey-pool]`（provider `carher_dev_k8s`）。
- ✅ 新账号（zyq/owp/hgg/dvo）的 6h cron refresh 已在 188 安装（crontab 共 6 行，每账号一行）。
- ✅ **promote 到 litellm-product（2026-06-21）**：`ops/litellm-register-zerokey.py` 已含
  6 账号全量 + `zerokey-pool`（router 负载均衡 6 端口），`--apply --sync-manifest` 在 198
  执行；rollout 4/4 健康；NodePort `30402` master key 实测 `zerokey-pool` chat/completions
  返回 `PONG`、responses 返回原生对象。Cursor 用原 key + 原地址选模型 `zerokey-pool` 即可。
- ⏳ 按需把 `zerokey-pool` 加进具体用户 key 的 `models` allowlist（master 已可用）。
- ⏳ **Agent 全能力**：落地方案见 [zerokey-codex-agent-bridge-plan.md](./zerokey-codex-agent-bridge-plan.md)（未实现）。
- 兜底：profile 若最终彻底过期，refresh 会失败并写 `state/REFRESH_STALE`（设了
  `ZK_ALERT_WEBHOOK` 则告警），届时跑 `ops/capture-manual.sh` + 喂一次 OTP 重新 seed。
