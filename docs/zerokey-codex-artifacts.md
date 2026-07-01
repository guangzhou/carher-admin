# zerokey-codex 沉淀索引

ChatGPT **网页聊天额度** → OpenAI 兼容 API（188 zerokey）→ 可选挂 198 LiteLLM Pro。
本页是 skill / 文档 / 脚本的导航入口；细节见各链接，避免三处内容漂移。

## 架构一图

```text
Codex / Cursor / curl
        │
        ├─ 推荐 ──► 198 LiteLLM :30402  /v1/responses
        │              use_chat_completions_api: true
        │              model: zerokey-* / zerokey-timothy-*
        │                    │
        │                    ▼
        └─ 直连 ──► 188 zerokey :8123|:8124  /v1/chat/completions
                       Authorization: Bearer raw  (LiteLLM 上游固定 raw)
                       Authorization: Bearer vscode (ToolCompiler，VS Code 工具语法)
                              │
                              ▼
                    chatgpt.com /backend-api/f/conversation
                    （网页 Chat 额度池，与 Codex 专用后端无关）
```

## 文档

| 文件 | 用途 |
|------|------|
| [chatgpt-web-to-codex-zerokey.md](./chatgpt-web-to-codex-zerokey.md) | **主设计 + 完整 runbook**（原理、Codex 配置、198 接入、踩坑、现状） |
| [zerokey-fleet-pool-plan.md](./zerokey-fleet-pool-plan.md) | **10+ 账号机群化 × 双额度池 × 多人共享 + 回归计划**（额度模型澄清、批量上号、池组、fallback） |
| [zerokey-codex-agent-bridge-plan.md](./zerokey-codex-agent-bridge-plan.md) | **Agent 全能力落地方案**（审阅用 MD + 分阶段 checklist） |
| [zerokey-codex-agent-bridge-design.md](./zerokey-codex-agent-bridge-design.md) | Agent 桥设计备忘（短版；细节见 plan） |
| 本文件 | 索引 |

## Skills（Agent 触发用）

| Skill | 路径 | 何时用 |
|-------|------|--------|
| chatgpt-web-to-codex-zerokey | `.codex/skills/chatgpt-web-to-codex-zerokey/SKILL.md` | 部署/刷新 zerokey、198 注册、排错 trap |
| chatgpt-web-to-codex-zerokey | `.claude/skills/chatgpt-web-to-codex-zerokey/SKILL.md` | 同上（Claude Code 中文详版） |
| chatgpt-login-session | `.codex/skills/chatgpt-login-session/SKILL.md` | mail.com OTP、新账号 capture、登录态调试 |

Sibling：`litellm-pro-ops`（198 key allowlist）、`chatgpt-pool-on-198`（chatgpt-acct 池，**另一条线**）。

## 仓库脚本（`scripts/chatgpt-onboard/zerokey-codex/`）

| 路径 | 作用 |
|------|------|
| `install.sh` | 188 首装：克隆 upstream zerokey + 套补丁 + 目录布局 |
| `zerokey-patch/` | 最小改动：`raw.js`、`chatgpt.js`、Dockerfile、compose |
| `capture/zerokey-web-capture.py` | patchright 登录 + 抓 `f/conversation` → `users.json` |
| `capture/Dockerfile` | capture 镜像（xvfb-run PID1 修复） |
| `ops/refresh.sh` | 自动重抓 → 校验 → 原子换 session → 重启 |
| `ops/capture-manual.sh` | 需 OTP 的交互式重抓 |
| `ops/add-account.sh` | **多账号**：新 port + profile + 容器 + 首次 capture |
| `ops/docker-compose.account.yml` | 每账号 compose 模板 |
| `ops/litellm-register-zerokey.py` | **198 幂等注册** 8 个 zerokey 模型 + `use_chat_completions_api` + manifest 同步 |
| `ops/README.md` | 188 上运维手册（与主文档互补） |
| `README.md` | bundle 简介 |

## 188 运行布局（install 生成，不入 git）

```text
~/zerokey-codex/                          # kristine，:8123
~/zerokey-codex-accounts/<id>/            # 其它账号，如 timothy :8124
  secrets/  state/  ops/  zerokey/  logs/
```

## 198 LiteLLM 注册（标准命令）

```bash
# 本机
./scripts/jms scp scripts/chatgpt-onboard/zerokey-codex/ops/litellm-register-zerokey.py \
  AIYJY-litellm:/tmp/litellm-register-zerokey.py
./scripts/jms ssh AIYJY-litellm 'python3 /tmp/litellm-register-zerokey.py'           # dry-run
./scripts/jms ssh AIYJY-litellm 'python3 /tmp/litellm-register-zerokey.py --apply --sync-manifest'
```

**禁止**对 stale manifest 做 `kubectl apply` 覆盖 live cm；改 zerokey 条目只用上述脚本或等价 splice + replace。

## 已落地账号（2026-06-18）

| 账号 | 188 | LiteLLM 模型前缀 |
|------|-----|------------------|
| kristine | `:8123` | `zerokey-gpt-5.5` … `zerokey-o3` |
| timothy | `:8124` | `zerokey-timothy-gpt-5.5` … `zerokey-timothy-o3` |

每账号 4 模型 × `use_chat_completions_api: true`；cron 每 6h `refresh.sh`。

## Codex 客户端（网页额度，对话/补代码）

```toml
model = "zerokey-gpt-5.5"          # 或 zerokey-timothy-gpt-5.5
model_provider = "carher_dev"      # 任意已配 wire_api=responses 的 LiteLLM provider

[profiles.zerokey]
model = "zerokey-gpt-5.5"
model_provider = "carher_dev"
```

Provider 需 `wire_api = "responses"`，`base_url` 指向 198 或 `https://cc.auto-link.com.cn/pro/v1`。

**Agent 改文件**：当前 LiteLLM→raw 路径**不含**完整 `apply_patch` loop；见 [agent-bridge 落地方案](./zerokey-codex-agent-bridge-plan.md)。

## 验证清单

```bash
# 188
curl -s localhost:8123/health
curl -s localhost:8124/health   # timothy

# 198（在 AIYJY-litellm 上）
MK=$(kubectl get secret litellm-secrets -n litellm-product -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
curl -s -H "Authorization: Bearer $MK" localhost:30402/v1/models | grep zerokey
curl -s -X POST -H "Authorization: Bearer $MK" localhost:30402/v1/responses \
  -d '{"model":"zerokey-gpt-5.5","input":"Say OK","stream":false}'
```
