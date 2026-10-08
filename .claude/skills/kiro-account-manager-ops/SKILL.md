# Kiro Account Manager — 批量登录自动化

> Kiro IDE 桌面端 (Tauri) 的 Account Manager，与 kiro.rs 反代/池管理（见 `kiro-rs-ops`）是**两套完全独立的系统**。

## 脚本优先（2026-10-08 起）

**工具链已进 git，不要再在 `/tmp` 现写。** 2026-10-03 那轮的 `/tmp/kiro-oneshot.py`
等四个脚本重启后被系统清空，磁盘和 git 都没副本，第二轮等于从零重建——
这就是「磁盘≠git」那条的又一次现场。

```
.claude/skills/kiro-account-manager-ops/scripts/
├── kiro_common.py     # PKCE / OIDC / getUsageLimits / accounts.json 幂等写入
├── kiro_oneshot.py    # 单账户 BuilderId 登录（真 Chrome CDP 状态机）
└── kiro_batch.sh      # 批量壳（含 inter-account Chrome cleanup）
```

**venv 也挪出 `/tmp`**：`~/.local/kiro-auto/venv`（uv + python3.11）。

```bash
# 建 venv（只需一次）
mkdir -p ~/.local/kiro-auto && cd ~/.local/kiro-auto
uv venv --python 3.11 venv
VIRTUAL_ENV=$HOME/.local/kiro-auto/venv uv pip install playwright pyotp httpx ddddocr Pillow
~/.local/kiro-auto/venv/bin/python -m playwright install chromium

# 批量跑（creds ndjson 每行 {"email","pw","alt_pw","totp"}）
.claude/skills/kiro-account-manager-ops/scripts/kiro_batch.sh /tmp/kiro-batch-creds.ndjson
```

🔴 **cwd 不能是 carher-admin 仓库根**：仓里有 `operator/__init__.py`，会 shadow
标准库 `operator`，`import enum` 直接 ImportError。脚本一律 `cd /tmp` 再跑。

### 规格来源 = Rust 源码，不是记忆

`~/codes/kiro-account-manager/src-tauri/src/` 是 OAuth 流程的唯一权威：

| 要点 | 文件 |
|------|------|
| register_client / create_token body | `clients/aws_sso_client.rs` |
| authorize URL、本地 callback、两个 redirect_uri | `auth/providers/idc.rs` |
| getUsageLimits URL + Kiro IDE UA headers | `clients/kiro_client.rs` |
| clientIdHash = sha1(紧凑 JSON) | `utils/client_id_hash.rs` |

🔴 **register 和 createToken 的 redirect_uri 不是一个值**（`idc.rs:296`）：
register 传**不带端口**的 `http://127.0.0.1/oauth/callback`，
authorize/createToken 传**带真实端口**的。混用 → createToken 400。

🔴 **authorize 的参数名是 `scopes`（复数、逗号分隔）**，不是 OAuth 标准的 `scope`。

> 仓里的 `agent-harness/cli_anything/kiro_account_manager` 是**只读**分析器，
> 不能登录、不碰 token，别指望它替代脚本。

## 目标文件

```
~/Library/Application Support/.kiro-account-manager/accounts.json
```

写入前自动备份到 `accounts.json.bak-<ts>`，按 email 幂等（已存在则就地替换，不追加重复行）。

## 凭据来源

飞书多维表格：
- base_token = `IhSNbXxl5azo9xsKxyhcO0lEnnO` / table_id = `tbludyQfSIZq6pLM`
- 字段：`账户`(email)、`密码`(GitHub pw)、`2fa`(TOTP seed)、`邮箱密码`(BuilderId pw)
- 追踪：`添加状态`(fld7F0BRq4)、`登录方式`(fldq2Yp1hW)

导出缓存 `/tmp/kiro-all-creds.ndjson`（NDJSON，每行一个账户）。⚠️ **含明文密码，禁回显，禁 git add**

## 两种 OAuth 流程

### A — GitHub OAuth（provider=`Github`, authMethod=`social`）

适用：有 GitHub 密码 + TOTP seed。

```
PKCE + state → {KIRO_AUTH_API}/login?idp=Github → Playwright Chromium
  → GitHub login (email → password → TOTP) → 拦截 kiro:// redirect
  → POST oauth/token → refreshToken → getUsageLimits → append accounts.json
```

核心库：`/tmp/kiro_batch_login.py`

### B — BuilderId/IdC（provider=`BuilderId`, authMethod=`IdC`）

适用：有 AWS BuilderId 密码 + TOTP seed。**必须真 Chrome CDP，非 Playwright Chromium。**

```
POST oidc.amazonaws.com/client/register → PKCE + 本地 HTTP callback
  → 真 Chrome CDP → AWS BuilderId portal
  → Email → Password → CAPTCHA(ddddocr+human_type) → Password再填 → MFA → Consent
  → callback code → token → getUsageLimits → append
```

核心脚本：`scripts/kiro_oneshot.py`（状态机 + 双密码切换 + ddddocr OCR）

#### BuilderId MFA 两种路径

| MFA 类型 | 条件 | 处理 |
|---------|------|------|
| **TOTP** | 凭据有 `2fa` seed | `pyotp.TOTP(seed).now()` 自动填入，最快 |
| **邮箱验证码** | 无 `2fa` | Outlook 取码（脚本待重建，见下） |

#### BuilderId 关键陷阱（13 条）

**2026-10-08 新增 3 条**（都是「静默卡死」型，不报错只超时，最难查）：

11. 🔴 **本地 callback server 必须过滤 favicon**：浏览器渲染完回调页会再发
    `GET /favicon.ico`，若 handler 无条件记录就会把真 code/state 冲成 `None`，
    报出来是「**state 不匹配，可能是串台的回调**」——看着像两个账户串台，
    其实是自己的 favicon 打自己。只认 `/oauth/callback` + 带 `code|error`，
    且**已有结果不覆盖**。

12. 🔴 **点按钮禁用 `.first`**：AWS 页面 DOM 里第一个 `button[type=submit]` 是
    **隐藏的** Cookie 同意按钮 (`data-id=awsccc-cb-btn-accept`)，真「继续」排它后面。
    `.first` + `is_visible()` 会永远卡在隐藏按钮上。必须 `.all()` 遍历取第一个可见的，
    并跳过文案含 Cookie 的。

13. 🔴 **MFA 页判别不能只看 input 属性**：AWS MFA 页的输入框是裸
    `input[type=text]`，既无 `name` 也无 `autocomplete=one-time-code`；
    文案是「需要额外验证 / MFA 代码 / 身份验证器」，**不含「验证码」**。
    只按属性或只匹配「验证码」会落 `unknown` 卡死。

#### 原有 10 条

1. **human_type()** — `fill()` 被 AWS 判机器人，逐字 0.08~0.25s 随机
2. **iframe DOM 复用** — CAPTCHA 过后 iframe 仍在，判新 CAPTCHA 看 `img.is_visible()`
3. **双密码切换** — `pw_fail_streak >= 2` 后切 ALT_PASSWORD
4. **consent 按钮** — `includes('允许')` 不能精确匹配
5. **MFA 后 page detachment** — post-submit screenshot 包 try/except
6. **Chrome CDP** — BuilderId 必须真 Chrome + `--remote-debugging-port=9222`
7. **Outlook 验证码在 Other 分栏** — 用全局搜索 `from:no-reply@login.awsapps.com`，不能逐文件夹遍历
8. **Outlook Chrome 必须 `--incognito`** — 清 profile 不够，M365 SSO cookie 残留会登上一个人的邮箱
9. **批量间必须 SIGKILL Chrome** — 账户之间 `pkill -9` 两个 CDP 端口 + 验证端口空闲 + 独立 tempdir
10. **全角密码** — Binus 表格密码含全角 `！`(U+FF01)，提交前 `normalize_pw()` 转半角，Outlook 登录自动尝试两个变体

## 🔴 getUsageLimits 对 IdC 新号已取不到（2026-10-08）

`getUsageLimits` 现在**强制**要 `profileArn`，而 BuilderId/IdC 流程拿不到。
**这不阻断加号**——池子用的是 token，不是 usageData。四组实测：

| 调用 | 结果 |
|------|------|
| social 号 + 它自己的 `profileArn` | ✅ `KIRO STUDENT` |
| social 号，**不带** profileArn | ❌ 400 `Invalid profileArn.` |
| 新 IdC token + 借 social 号的 ARN | ❌ 403 `Invalid token` |
| IdC token 调 `ListAvailableProfiles` | ❌ 400 `AccessDeniedException` |

结论：那个 ARN（`arn:aws:codewhisperer:us-east-1:699475941385:profile/EHGA3GRVQMUK`，
26 个 social 号共用同一个，**不是 per-account**）只认 social OAuth 那条通道的 token，
借不过来；IdC 侧又没有自助发现 ARN 的权限。

🔴 **400 文案 `Invalid profileArn.` 极度误导**：它是「该带没带」，不是「带错了」。
别顺着「我 ARN 填错了」的方向查。

🔴 **判这类失败必须先做阳性对照**：我第一反应是「kinara 这个新号有问题」，
拿 3 个**早就有 usageData** 的老号用同样调用形状一打，全是同一个 400 ——
一句话定位到是调用形状/端点侧，不是账号。

## API 端点

| 用途 | URL |
|------|-----|
| Kiro Auth | `https://prod.us-east-1.auth.desktop.kiro.dev` |
| AWS OIDC | `https://oidc.us-east-1.amazonaws.com` |
| Usage | `https://management.us-east-1.kiro.dev/getUsageLimits` |
| ListAvailableProfiles | `POST https://management.us-east-1.kiro.dev/` + `x-amz-target` |
| Redirect URI | `kiro://kiro.kiroAgent/authenticate-success` |

## Account Entry 关键字段

### GitHub (social)
```json
{"provider": "Github", "authMethod": "social", "label": "Kiro Github 账号",
 "region": null, "clientId": null, "clientSecret": null, "ssoSessionId": null}
```

### BuilderId (IdC)
```json
{"provider": "BuilderId", "authMethod": "IdC", "label": "Kiro BuilderId",
 "region": "us-east-1", "clientId": "(OIDC register)", "clientSecret": "(OIDC register)",
 "clientIdHash": "e909a0580879b06ece1202964fbe9dda95ea4ce3",
 "ssoSessionId": "(token response aws_sso_app_session_id)"}
```

clientIdHash = `sha1(JSON.stringify({"startUrl":"https://view.awsapps.com/start"}))`

## /tmp 脚本清单

| 脚本 | 角色 | 状态 |
|------|------|------|
| `kiro_batch_login.py` | 共享库：PKCE、token、GitHub OAuth 自动化 | 核心 |
| `kiro-oneshot.py` | BuilderId 完整流程（状态机 + 双密码 + ddddocr + TOTP/email MFA） | 核心 |
| `outlook_code.py` | Outlook 邮箱验证码取码（Chrome CDP 9223 + incognito + 多选择器搜索） | 核心 |
| `kiro-builderid-batch*.sh` | 批量 BuilderId 壳脚本（含 inter-account Chrome cleanup） | 模板 |
| `kiro-batch-github-8.py` | 批量 GitHub 8 账户 | ✅ 已完成 |
| `kiro-github-4.py` | 追加 GitHub 4 账户 | ✅ 已完成 |
| `kiro-github-reset-4.py` | GitHub 密码重置流程 | 📦 备用 |

## 工具依赖

```bash
source /tmp/kiro-auto-venv/bin/activate
# playwright pyotp httpx ddddocr Pillow
```

BuilderId 额外需要本机 Chrome（`/Applications/Google Chrome.app`）。

## 飞书表格更新

```bash
lark-cli base +record-batch-update \
  --base_token IhSNbXxl5azo9xsKxyhcO0lEnnO --table_id tbludyQfSIZq6pLM \
  --json '{"update_records":[{"record_id":"recXXX","fields":{"添加状态":"已添加","登录方式":"GitHub"}}]}'
```

select 字段值直接传字符串，不需要数组。单批最多 200 条。

## 批量 BuilderId 壳脚本模板

每次跑新一批 BuilderId 账户时，基于此模板生成 `/tmp/kiro-builderid-batch<N>.sh`：

```bash
#!/usr/bin/env bash
set -uo pipefail
VENV=/tmp/kiro-auto-venv/bin/python3
SCRIPT=/tmp/kiro-oneshot.py
ACCTS_FILE="$HOME/Library/Application Support/.kiro-account-manager/accounts.json"

TARGETS=("email1@binus.ac.id" "email2@binus.ac.id")

cleanup_chrome() {
  pkill -9 -f 'remote-debugging-port=9222' 2>/dev/null || true
  pkill -9 -f 'remote-debugging-port=9223' 2>/dev/null || true
  sleep 2
  for port in 9222 9223; do
    for attempt in $(seq 1 5); do
      lsof -i :$port -t >/dev/null 2>&1 || break
      pkill -9 -f "remote-debugging-port=$port" 2>/dev/null || true
      sleep 1
    done
  done
  rm -rf /tmp/kiro-outlook-* 2>/dev/null || true
}

# ... for 循环：skip if exists → cleanup_chrome → run oneshot → check SAVED
```

关键：**每个账户前调用 `cleanup_chrome()`**，保证端口空闲 + profile 清空。

## 飞书表格更新

```bash
# select 字段值用数组包裹
lark-cli base +record-batch-update \
  --base-token IhSNbXxl5azo9xsKxyhcO0lEnnO --table-id tbludyQfSIZq6pLM \
  --json '{"update_records":{"recXXX":{"添加状态":["已添加"],"登录方式":["BuilderId"]}}}' --as user

# 新建记录
lark-cli base +record-batch-create \
  --json '{"create_records":[{"账户":"x@binus.ac.id","密码":"...","邮箱密码":"...","添加状态":["已添加"]}]}' --as user
```

## 操作经验

- 批量间隔 10s 避免 rate limit
- accounts.json 幂等写入：每次检查 email 去重
- 密码失效路径：手动改密码 → 更新飞书表 → 重拉 ndjson → 重跑脚本
- KIRO_APP_VERSION = `0.6.18`
- Outlook 密码全角字符（`！` U+FF01）：`outlook_code.py` 自动尝试原始值 + normalize_pw() 两个变体
- 卡在 "STEP 5: Waiting for callback" 时，说明 MFA 验证码错误或过期，手动 `kill -9` 后 batch 会继续下一个

## 当前状态 (2026-10-03)

accounts.json **50 个账户**（本轮新增 14 个 BuilderId，累计 25+11+14）。
唯一未添加：**aldi.hafizh@binus.ac.id** — Outlook 密码错误（两个变体均被 M365 拒绝），需手动核实。
