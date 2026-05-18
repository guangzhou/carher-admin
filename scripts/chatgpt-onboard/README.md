# ChatGPT Pro 账号 OAuth 自动绑定

## 目标

10–50 个 ChatGPT Pro 账号扩容时，**避免人工绑定设备 + 输验证码**。
单账号上线时间从 5min → 30s，无人值守。

## 不解决什么

❌ 不自动**注册** OpenAI 账号（Cloudflare Turnstile）
❌ 不自动**订阅** Pro（信用卡支付）
❌ 不自动**注册** mail.com 邮箱

→ 这三步必须人工做完，账号信息（email + password + IMAP 凭据）写进 `secrets.yaml`，本工具接管 OAuth 设备绑定 + 验证码。

## 文件清单

| 文件 | 作用 |
|------|------|
| `secrets.example.yaml` | secrets 格式 spec |
| `Dockerfile` | onboarding 临时容器镜像（Playwright + IMAP） |
| `oauth-bind.py` | 浏览器自动化：填 user_code → 邮箱密码 → OTP |
| `onboard.sh` | 顶层编排：解密 secrets → device-code → playwright → 落盘 auth.json → 调用 add-chatgpt-account.sh |
| `README.md` | 本文件 |

## 已知未落地的细节（落地前必须解决）

### 1. litellm device-code 入口命令

`onboard.sh` 第 ~52 行的 `--device-code-flow` 是**占位**。litellm 实际怎么从 CLI 触发 device flow + stdout 打印 user_code，需要先在 188 上空跑一次确认。

候选方案：

- 复用 `chatgpt-pro-litellm` skill SKILL.md `OAuth 首次授权` 章节里那个 inline python 脚本（用 `ChatGPTConfig.get_device_code()`），改造成 stdout 打印 + 等待 + 写 auth.json
- 或：直接拿 ChatGPT 的 OAuth client_id 自己拉 device code（绕开 litellm，独立 python 脚本），完成后构造 `auth.json`

**建议**：先用方案 A（复用现成）；只有发现 litellm 内部 API 变化才退方案 B。

### 2. OpenAI 风控

- mail.com 同 IP 多账号注册可能被 OpenAI 风控当 fraud → 单 IP 多账号 OAuth 也可能触发
- 缓解：每次 OAuth 成功后等 30–60s 再 onboard 下一个；必要时配 SOCKS5 出口

### 3. OTP 邮件结构

mail.com 收件箱里 OpenAI 发的 OTP 邮件，From / 主题 / body 格式是猜的。**第一次跑必须 headed 模式截图 + IMAP debug**，确认 `OTP_FROM_HINT` 和 `OTP_RE` 命中。

### 4. 验证码可能是链接而非数字

某些 flow OpenAI 直接发"点这个链接确认"邮件。本脚本目前只覆盖"6 位数字 OTP"路径。若 flow 是 link 路径，需要：
- IMAP 取邮件 → 提取 link → playwright 打开 → 等跳转

### 5. age 解密的人工口令

`age -d /Data/chatgpt-auth/secrets.age > /tmp/...` 需要交互式口令。完全无人值守必须：
- 用 age recipient（公钥）模式而非 passphrase 模式
- 188 上放 age identity 文件 `/Data/chatgpt-auth/.age-identity`（chmod 600）
- 解密命令：`age -d -i /Data/chatgpt-auth/.age-identity ...`

## 落地步骤（10 个账号扩容时）

1. **一次性**：188 上初始化 age + 镜像
   ```bash
   age-keygen -o /Data/chatgpt-auth/.age-identity
   chmod 600 /Data/chatgpt-auth/.age-identity
   age-keygen -y /Data/chatgpt-auth/.age-identity > /Data/chatgpt-auth/.age-recipient

   # 把 secrets.example.yaml 改名 secrets.yaml，填 10 个账号
   age -e -R /Data/chatgpt-auth/.age-recipient secrets.yaml > /Data/chatgpt-auth/secrets.age

   scp -r scripts/chatgpt-onboard cltx@10.68.13.188:/tmp/
   ssh cltx@10.68.13.188 'cd /tmp/chatgpt-onboard && docker build -t chatgpt-onboard:latest .'
   ```

2. **每个账号**（onboard.sh 全自动）：
   ```bash
   ./scripts/chatgpt-onboard/onboard.sh acct-3
   ./scripts/chatgpt-onboard/onboard.sh acct-4
   # ...
   ```

3. **验证**：
   - 198 prod admin UI 看 `chatgpt-{acct}-*` deployment 是否齐 4 条
   - quota-cron 是否能识别新账号

## 测试计划

第一次跑必须用一个**测试账号**（已订阅 Pro 但只用来验证流程的）：

1. headed 模式（`docker run` 加 `--headed` 透到 oauth-bind.py）—— 看到浏览器全过程
2. 看 `/work/screenshots/` 里 5 张截图，每个关键节点都对
3. 确认 IMAP 取到 OTP 邮件
4. 跑通后再切 headless 跑剩余 9 个

## 单账号成本预算（无人值守）

| 步骤 | 用时 | 是否可并行 |
|------|------|----------|
| age 解密 | 1s | — |
| litellm device code | 5–10s | ❌ |
| playwright 加载 + 填表 | 15–20s | ❌（同 IP 怕风控）|
| IMAP 等 OTP | 10–60s | ❌ |
| OAuth 跳转 + auth.json 落盘 | 5s | — |
| add-chatgpt-account.sh 后续 | 30–60s | — |
| **总计** | **1–3min/账号** | 串行 |

10 账号预估 30min 总时长，全程无人值守。
