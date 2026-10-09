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

⚠️ **这条流程尚未进 git**（原 `/tmp/kiro_batch_login.py` 已随 /tmp 清空丢失）。
2026-10-08 那批全是 BuilderId，没用到。需要时重写进 `scripts/`，别再放 `/tmp`。

### B — BuilderId/IdC（provider=`BuilderId`, authMethod=`IdC`）

适用：有 AWS BuilderId 密码 + TOTP seed。**必须真 Chrome CDP，非 Playwright Chromium。**

```
POST oidc.amazonaws.com/client/register → PKCE + 本地 HTTP callback
  → 真 Chrome CDP → AWS BuilderId portal
  → Email → Password → CAPTCHA(ddddocr+human_type) → Password再填 → MFA → Consent
  → callback code → token → getUsageLimits → append
```

核心脚本：`scripts/kiro_oneshot.py`（状态机 + 双密码切换 + ddddocr OCR）

#### 跑完一轮后的状态（2026-10-08）

accounts.json **60 个**。本轮 10 个 binus 号 **10/10 全成**（全 BuilderId/IdC）。
`rhania.putri` 第一次撞点击式 WAF 挑战失败，修完重跑 **87s 一次过且走首选密码**
——证明它密码从头就对，是验证码的锅被记到了密码头上（见坑 14/15）。

#### BuilderId MFA 两种路径

| MFA 类型 | 条件 | 处理 |
|---------|------|------|
| **TOTP** | 凭据有 `2fa` seed | `pyotp.TOTP(seed).now()` 自动填入，最快 |
| **邮箱验证码** | 无 `2fa` | Outlook 取码（脚本待重建，见下） |

#### BuilderId 关键陷阱（15 条）

**2026-10-08 新增 11~15**，共同特征：**不报错，只超时**。页面状态不变
（一直停在 password），状态机就不打印，日志能连续几分钟一行不出，
看着像进程挂了其实在空转。**排查第一步：看最后几条 `page ->` 停在哪一步**，
别信超时文案的猜测。

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

14. 🔴 **「还停在密码页」≠「密码错」**：验证码答错也停在密码页。
    实测 (rhania)：页面**同时**有「您输入的某些信息与记录不完全相符」和
    「**验证码无效**」。无条件累加 `pw_fail_streak` 会把验证码的锅记到密码头上，
    两次就切到真正错的那个密码，然后一路错下去。
    计数前先 `captcha_rejected(page)`，验证码错就不记密码失败。

15. 🔴 **点击式 WAF 挑战**（2026-10-08 新出现）：
    「Please click verify to start your security challenge」+ 一个 `Verify` 按钮，
    **没有图片可 OCR**，必须真点一下。不点就一直停在密码页，
    表现成「这个号密码怎么都不对」。ddddocr 那套对它完全无效。
    处理顺序：**点击式挑战优先于图片验证码**（两者可能同时在页上）。

#### 原有 10 条

1. **human_type()** — `fill()` 被 AWS 判机器人，逐字 0.08~0.25s 随机
2. **iframe DOM 复用** — CAPTCHA 过后 iframe 仍在，判新 CAPTCHA 看 `img.is_visible()`
3. **双密码切换** — `pw_fail_streak >= 2` 后切 ALT_PASSWORD
   ⚠️ **已被第 14 条修正**：计数前必须排除「验证码错」，否则会冤枉对的密码
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

## 脚本清单（git 内，非 /tmp）

| 脚本 | 角色 |
|------|------|
| `scripts/kiro_common.py` | PKCE / OIDC / callback server / getUsageLimits / accounts.json 幂等写入 |
| `scripts/kiro_oneshot.py` | 单账户 BuilderId 登录状态机（真 Chrome CDP + ddddocr + TOTP）+ **`drive_login()` 共享函数** |
| `scripts/kiro_batch.sh` | 批量加号壳（inter-account `cleanup_chrome()` + 10s 间隔） |
| `scripts/aws_mfa_rotate.py` | AWS Builder ID MFA 设备轮换（加新 seed → 验证 → 可选删旧） |
| `scripts/aws_mfa_rotate.sh` | 批量 MFA 轮换壳 |
| `scripts/kiro_github_login.py` | **GitHub/social 账户登录**（已走通，9/9） |

**`drive_login()`** 在 `kiro_oneshot.py` 里，是 BuilderId 加号和 MFA 轮换共享的登录状态机
（WAF 点击挑战、图片验证码、双密码、MFA、consent 全在里面）。改一处两边都生效。

## GitHub/social 账户登录（2026-10-09 走通）

```bash
TARGET_EMAIL=x@binus.ac.id PW='<GitHub 密码>' TOTP_SEED='<seed>' \
  PYTHONPATH=scripts ~/.local/kiro-auto/venv/bin/python scripts/kiro_github_login.py
```

流程 = Kiro Desktop Auth social：
`prod.us-east-1.auth.desktop.kiro.dev/login?idp=Github&redirect_uri=kiro://kiro.kiroAgent/authenticate-success`
→ GitHub 登录（`#login_field`→`#password`→`#app_totp`）→ 授权 → `kiro://...?code=xxx`
→ POST `/oauth/token` 换 token。约 20 秒/个。

🔴 **两条路只有一条通**（反复失败的根因）：
- ❌ `http://127.0.0.1:3128` + `app.kiro.dev/signin` —— signin callback 回不来，卡死
- ✅ `kiro://` 深链 + `/login?idp=Github` —— GitHub 授权后发 `kiro://...?code=xxx`

🔴 **拦 code 必须用 `page.on("request")`**（三路都挂：request / requestfailed / framenavigated）。
`kiro://` 是自定义 scheme，浏览器 abort 它，但 request 事件能看到完整 URL。
❌ **别用 `page.route`** —— 它 abort 掉请求反而拦不到。

token exchange 走 `/oauth/token`（Kiro Desktop Auth），**不是** AWS OIDC `/token`。
social 号 entry：`provider=Github, authMethod=social, clientId/clientSecret/clientIdHash 全 null`。

**待补**：`outlook_code.py`（无 TOTP seed 时登 Outlook 取邮箱验证码）。

## AWS MFA 轮换（2026-10-08 实测）

### 已验证全链路（rhania.putri 实证）

```
profile.aws.amazon.com → redirect → signin.aws 登录
  → credentials.signin.aws/#/（「我的详细信息」页）
  → 点左栏「安全」tab
  → 点「注册设备」→ 跳到 signin.aws/.../mfa/register
  → 选「身份验证器应用程序」radio → 点「下一步」
  → 点「显示密钥」→ 取出 base32 seed（32 字符）
  → pyotp 算码填入 → 点「分配 MFA」→ 绑定成功
```

### 用法

```bash
# 单号（旧设备不删，可回退）
TARGET_EMAIL=x@binus.ac.id PW='<buildid pw>' TOTP_SEED='<现有 seed>' \
  PYTHONPATH=scripts ~/.local/kiro-auto/venv/bin/python scripts/aws_mfa_rotate.py

# 批量
scripts/aws_mfa_rotate.sh /tmp/rotate-creds.ndjson           # 只加新设备
DELETE_OLD=1 scripts/aws_mfa_rotate.sh /tmp/rotate-creds.ndjson  # 加完再删旧的

# 新 seed 写到 /tmp/aws-mfa-rotate-<user>-<HHMMSS>.json（chmod 600，不覆盖）
```

### 🔴 关键限制：只对已有 authenticator app 的号可行

实测 2026-10-08：

| 号 | 已有 authenticator app | 点「注册设备」后 | 能选 authenticator app |
|---|---|---|---|
| rhania.putri | ✅ 有 1 个 | 弹出**设备类型选择**（三 radio） | ✅ 能选 |
| jason.tanean | ❌ 从未设过 | 直接进 **passkey/FIDO 注册** | ❌ 不给选 |

**AWS 对从未设过 authenticator app 的号，走「注册设备」时直接推 passkey，跳过设备类型选择。**
这意味着：
- 表里已有 `2fa` seed 的号（=已注册过 authenticator app）→ **能自动 rotate**
- 从未注册过的号 → **需要先手动在 AWS 网页上注册一次 authenticator app**，之后才能自动

### 🔴 实测踩过的 6 个坑

| # | 坑 | 现象 | 修法 |
|---|---|---|---|
| 1 | `profile.aws.amazon.com/security` 返回 S3 404 | `NoSuchKey` XML | 走根路径进 SPA |
| 2 | 登录后落到 `credentials.signin.aws`，不是 `profile.aws.amazon.com` | 域名检测全错 | 检测 `credentials.signin.aws` |
| 3 | 落地页是「我的详细信息」不是 Security | 按钮找不到 | 先点左栏「安全」tab |
| 4 | 「注册设备」后页面全白 | 按钮列表空 | SPA 导航需等页面加载 |
| 5 | radio 选中了父 div 标题而非具体选项 | 选到「注册 MFA 设备」而非「身份验证器应用程序」 | 找最短 innerText 匹配 + index fallback |
| 6 | `list_mfa_devices` 永远返回 0 | 误判设备没绑上 | SPA 用 div/card 非 table，量具坏了不影响操作 |

## AWS 改密码（2026-10-09 实测，半成品）

脚本 `scripts/aws_change_password.py` 骨架已写，流程卡在二次 MFA 验证。

### 已验证路径

```
登录 profile.aws.amazon.com（WebAuthn 拦截 + TOTP 登录）
  → credentials.signin.aws/#/security
  → 点「更改密码」
  → AWS 要求二次 MFA 验证（调 WebAuthn API）
  → WebAuthn 失败后出现 fallback 选项：
      「身份验证器应用程序」（= TOTP）
      「Verify via email」（= 邮件验证码）
```

### 🔴 WebAuthn 拦截方案（cut.ansari 实测）

| 方案 | 效果 |
|------|------|
| `add_init_script` 注入 `NotAllowedError` | 登录 OK（走 TOTP），但二次验证也被拦 → AWS 报错 |
| CDP `WebAuthn.enable` + Virtual Authenticator | passkey 验证失败（没真凭据）→ **AWS 显示 fallback 选项** ✅ |
| 登录后 `evaluate("window.__blockWebAuthn=false")` | `add_init_script` 在新导航后重新注入，解除无效 |

**CDP Virtual Authenticator 是正确方案**：让 passkey 失败后 AWS 提供 TOTP fallback。

### 🔴 当前卡点：「身份验证器应用程序」的 DOM 交互

fallback 页面的按钮只有 `重试` 和 `Verify via email`。
「身份验证器应用程序」在 body 文本里但**不在按钮列表中**——`dump_buttons` 找不到。
用 `evaluate` 点它所在的最小 DOM 元素会**关闭所有 tab**（原因不明，可能是
scheme handler 或 SPA 路由副作用）。

**下一步：在 fallback 页面做完整 DOM dump**（所有可交互元素 + 「身份验证器应用程序」
的父子元素结构），确认它到底是什么元素、怎么交互。

备选路径：走 `Verify via email`（已确认是按钮、能点），用 Outlook 取验证码完成二次认证。
需要 `outlook_code.py`（尚未重建）或类似的取码逻辑。

## 工具依赖

```bash
~/.local/kiro-auto/venv/bin/python   # playwright pyotp httpx ddddocr Pillow
```

BuilderId 额外需要本机真 Chrome（`/Applications/Google Chrome.app`）。

## 飞书表格更新

```bash
# 改已有记录：update_records 是 **map**（recId -> 字段 map），select 值用**数组**
lark-cli base +record-batch-update \
  --base-token IhSNbXxl5azo9xsKxyhcO0lEnnO --table-id tbludyQfSIZq6pLM \
  --json '{"update_records":{"recXXX":{"添加状态":["已添加"],"登录方式":["BuilderId"]}}}' --as user

# 新建记录
lark-cli base +record-batch-create \
  --base-token IhSNbXxl5azo9xsKxyhcO0lEnnO --table-id tbludyQfSIZq6pLM \
  --json '{"create_records":[{"账户":"x@binus.ac.id","密码":"...","邮箱密码":"...","添加状态":["已添加"]}]}' --as user

# 全量导出（NDJSON，含 record_id）；--limit 上限 200，ndjson 默认 2000
lark-cli base +record-list --base-token IhSNbXxl5azo9xsKxyhcO0lEnnO \
  --table-id tbludyQfSIZq6pLM --format ndjson --output ./creds.ndjson --overwrite --as user
```

🔴 **`+record-batch-update` 曾经的 `[{record_id, fields}]` 数组写法已过时**，
现在是 map 形状、select 用数组（`--help` 实测确认）。本文件里曾同时留着两段
互相矛盾的说法，已删旧的。单批最多 200 条。

🔴 **`+record-search` 必须带 `--keyword` 或 `--json`**，只想全量读用 `+record-list`。

## 操作经验

- 单号登录 70~155s；批量间隔 10s 避免 rate limit
- accounts.json 幂等：按 email 去重，已存在则**就地替换**不追加；写前自动备份
  `accounts.json.bak-<ts>`
- 密码失效路径：手动改密码 → 更新飞书表 → 重拉 ndjson → 重跑脚本
- KIRO_APP_VERSION = `0.6.18`
- creds ndjson 放 `/tmp` 且 `chmod 600`，跑完即删，**禁 git add**
  （脚本本身不回显密码/token，日志可安全留存——本轮实测确认）

## 当前状态 (2026-10-08)

accounts.json **60 个账户**。10-08 新增 10 个 binus BuilderId，**10/10 全成**。

历史遗留未加：**aldi.hafizh@binus.ac.id** — Outlook 密码错误（两个变体均被 M365
拒绝），需手动到 M365 后台核实/重置。
