---
name: kiro-account-manager-ops
description: >-
  Kiro 账号的登录与入库：Kiro IDE Account Manager（本机 accounts.json）批量登录自动化
  （BuilderId/IdC 真 Chrome CDP 状态机、GitHub/social kiro:// 深链、邮箱 MFA、AWS MFA 轮换），
  以及「用户贴来一份 refreshToken 导出 ⇒ 加进 198 kiro 池 + 开超额 + 写 Account Manager」的一条龙
  （social / IdC 混批，导出 apiRegion=eu-central-1 要改）。
  Use when 用户贴来 kiro 账号导出 JSON 说「加 kiro」「再增加 N 个」，或要批量登录 kiro 号、
  轮换 AWS MFA、把号写进 Account Manager。池子本身的运维（摘号/余额/模型/镜像）看 `kiro-rs-ops`。
---

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

🔴 **写之前先退出 KiroAccountManager.app**（`osascript -e 'quit app "KiroAccountManager"'`）：
App 开着时会用它内存里的旧列表整份覆盖 accounts.json（10-17 实测：脚本报 `SAVED total=128`，
约 1 分钟后文件回到 127、新号消失）。判写入成功只认事后现读 `len(accounts.json)` + 按 email 查到那条，
`kiro_batch.sh` 末尾的 `before -> after` 没变就是被盖了。

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

#### 跑完一轮后的状态（2026-10-13 更新）

accounts.json **111 个**（10-15 现量；不再按批次手记，判数一律现读 `len(accounts.json)`）。三条登录路径全通。

#### BuilderId MFA 两种路径

| MFA 类型 | 条件 | 处理 |
|---------|------|------|
| **TOTP** | 凭据有 `2fa` seed | `pyotp.TOTP(seed).now()` 自动填入，最快 |
| **邮箱验证码** | 无 `2fa`，**已有** `GRAPH_RT` | `kiro_builderid_email_mfa.py`（Graph API 取码，已走通 8/8） |
| **邮箱验证码** | 无 `2fa`，**没有** `GRAPH_RT`（新买的 outlook 号） | 先 `outlook_graph_rt.py` 取 RT，再走上一行。两步全自动，见 §「上游：GRAPH_RT 从哪来」 |

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
| setUserPreference | `POST https://management.us-east-1.kiro.dev/setUserPreference`（开 overage，social 号 only，见 `kiro-rs-ops` §B）|

### 加到 198 池子

#### 只拿到 `{email, refreshToken, provider}` 一份导出：两边都加 + 开超额（10-15 id141 全流程，~5 分钟）

一个或多个号、social/IdC 混着都走这一条（10-16 第三次直通：1 个 IdC + 5 个混批）：

```bash
cd ~/codes/carher-admin; W=work-kiro-add-<日期>; mkdir -p $W; umask 077               # 仓内工作目录（已 gitignore）
# 把用户贴的导出原样写进 $W/export.json（list 也行）
python3 scripts/kiro-batch-prepare.py --out $W/split $W/export.json                   # 1 判重 + 拍平（会改掉 eu-central-1）
for f in $W/split/creds/*.json; do                                                      # 2 只读验号，ok/tier/modelCount
  python3 scripts/kiro-probe.py both --creds "$f" --out-rotated "${f%.json}.rotated.json" --summary-json; done
python3 scripts/kiro-pool.py show | grep current=True                                     # 3 看在役 prio
python3 scripts/kiro-pool.py add-batch $W/split/creds/*.rotated.json --priority <对齐在役> # 4 加 198 池
cd /tmp && ~/.local/kiro-auto/venv/bin/python ~/codes/carher-admin/scripts/kiro-enable-overage.py <social ids> --wait 90  # 5 超额（IdC 跳过）
PYTHONPATH=<本skill>/scripts ~/.local/kiro-auto/venv/bin/python <本skill>/scripts/kiro_import_social_rt.py --pool-id <全部新 id>  # 6 AcctMgr（IdC 也收）
cd ~/codes/carher-admin && scripts/kiro-lane-regress.sh --rounds 1 && rm -rf $W          # 7 回归 + 删 live 凭据
```
最后在 `kiro-rs-ops` §D4 台账补一行。

- 🔴 **导出带 `apiRegion: eu-central-1`**（10-16 两批 6/6）：不改的话 Admin 看余额报 `403 User is not authorized`。
  第 1 步已自动处理；跳过筛子手搓时自己改成 us-east-1（`kiro-pool.py add` 闸门会拦）。
- IdC 号：⛔ 开不了 overage（token 不认 profileArn）；AcctMgr 里 usageData 为空是正常的。
- overage 45s 内常只翻一部分，`--wait 90` 一轮全过（10-16 4/4）；没翻就 1 分钟后 `--check`。

登完的 social 号只需 `refreshToken` + `email`（+ `authMethod=social`）即可用 `kiro-pool.py add` 加到 198 kiro-rs 池。
不需要 `clientId`/`clientSecret`/`accessToken`。脚本自动派生 `machineId = sha256("KotlinNativeAPI/<rt>")`。
详见 `kiro-rs-ops` §D3。

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
| `scripts/change_builderid_pwd_2fa.py` | **单号 BuilderId 改密码 + 轮转 2FA**（profile.aws.amazon.com 登录 → 安全 tab → credentials.signin.aws/#/security → 改密码/删旧 MFA/注册新 MFA）。env: `EMAIL`, `OLD_PWD`, `MFA_SECRET`, `DO_PWD`, `DO_2FA`, `CDP_PORT` |
| `scripts/batch_pwd_2fa.sh` | **批量改密码 + 轮转 2FA**：串行跑 `change_builderid_pwd_2fa.py`(改密码) + `aws_mfa_rotate.py`(`DELETE_OLD=1` 改2FA)，账号从 `$1` creds ndjson 读，summary → `/tmp/kiro-batch-pwd-2fa-results/summary.json`。🔴 **10-19 前它和 `change_builderid_pwd_2fa.py` 都只在磁盘上、从未 commit**（本文件曾写成「已入 git」）—— 又一次「磁盘≠git」 |
| `scripts/kiro_github_login.py` | **GitHub/social 账户登录**（已走通，9/9） |
| `scripts/outlook_graph_rt.py` | **给一个新 outlook 号取 Graph refresh_token**（`kiro_builderid_email_mfa.py` 的上游，无 TOTP seed 的消费级号走它）。env: `MS_EMAIL`, `MS_PW`(**Outlook 密码**), `MS_OUT_DIR?`。输出落 `~/.local/kiro-auto/outlook-rt/secrets/`（仓外） |
| `scripts/kiro_builderid_email_mfa.py` | **BuilderId + 邮箱 MFA 登录**（无 TOTP seed，Graph API 取码，8/8） |
| `scripts/kiro_import_social_rt.py` | **已有 refreshToken 的号直接入库**（不开浏览器）：`export.json` 或 `--pool-id <id…>` 从 198 池取，machineId 与池子一致；按 email 幂等。social 走 kiro.dev refresh（10-15 id141 固化）；**IdC 走 OIDC refresh + `build_idc_entry`**（10-16 id147/151 固化） |

**`drive_login()`** 在 `kiro_oneshot.py` 里，是所有登录流程共享的状态机
（WAF 点击挑战、图片验证码、双密码、MFA、consent）。
新增 `otp_callback` 参数：无 TOTP seed 时调它取码（典型：Graph 邮箱取码）。

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

## BuilderId 邮箱 MFA 登录（2026-10-11 走通，Graph API 取码）

```bash
TARGET_EMAIL=x@outlook.com PW='<AWS密码>' GRAPH_RT='<refresh-token>' GRAPH_CID='<client-id>' \
  PYTHONPATH=scripts ~/.local/kiro-auto/venv/bin/python scripts/kiro_builderid_email_mfa.py
```

适用：有 AWS 密码 + Outlook 邮箱的 Microsoft Graph refresh-token（无 TOTP seed）。
8/8 全成，约 40~90 秒/个。

**MFA 取码流程**：refresh-token → Graph access-token → 拉收件箱最新邮件 → 找 AWS 发的验证码。

🔴 **三个坑**：
1. **consumer 账号不支持 `$filter`**（400 InefficientFilter）——拉全部邮件代码内过滤
2. **HTML 邮件里 `color:#555555` 被 `\d{6}` 误匹配**——优先找 `class="code"` 标签
3. **同一个码只提交一次**（`used_codes` 去重 + `after_ts` 时间窗口）

**OTP Check 字段格式**：`邮箱账号|邮箱密码|refresh-token|client-id`
（5 个号共用同一个 client-id `9e5f94bc-...`）。

**替代了 `outlook_code.py`**（浏览器登 Outlook 取码）——Graph API 更干净、更快、更稳。

### 上游：`GRAPH_RT` 从哪来（`outlook_graph_rt.py`，10-18 走通 2/2）

上面那条命令要的 `GRAPH_RT`，对**新买的 outlook 号**是不存在的，得先自己跑出来：

```bash
cd /tmp && MS_EMAIL='x@outlook.com' MS_PW='<Outlook密码>' \
  python3 ~/codes/carher-admin/.claude/skills/kiro-account-manager-ops/scripts/outlook_graph_rt.py
# → ~/.local/kiro-auto/outlook-rt/secrets/<slug>.json  含 refresh_token + client_id
```

机制：Thunderbird 的公开 client_id `9e5f94bc-e8a4-4e73-b8be-63364c29d753` 走授权码流，
`redirect_uri=https://localhost`（本地没服务器，重定向必然失败——**code 就在那个失败请求的 URL 里**）。
无 2FA 的号全程无人工。整号链路：`outlook_graph_rt.py` → `kiro_builderid_email_mfa.py` → `kiro-pool.py add`。

🔴 **五个坑，每个都真卡过**：

1. **`MS_PW` 是 Outlook 密码，不是 AWS 密码** —— 供应商表里分两列且值不同。传错时微软明说
   「此密码不是你的 Microsoft 帐户的正确密码」，10-18 第一轮就是这个，磨了 2 分钟才看截图发现。
   脚本现在识别这行文案并立即退出。
2. **「验证你的电子邮件」页要填的是恢复邮箱地址，不是验证码** —— 输入框 label 写「电子邮件」、
   `#proof-confirmation-email-input`，页面只显示 `ms*****@maxxspace.com`。填错会报
   「正确的电子邮件应以 'ms' 开头」。该页底部有 **「使用密码」链接可整段绕过**，密码对就直接过，
   这是最快的路。
3. **FIDO/passkey 页点「取消」没用** —— `login.microsoft.com/consumers/fido/create`，
   标题「正在设置密钥...」。WebAuthn ceremony 一起来就等 OS 级弹窗，浏览器里点取消不生效，
   10-18 点了 **57 次**页面不动。解法是**开场就用 CDP 装虚拟 authenticator**
   (`WebAuthn.addVirtualAuthenticator`, `hasUserVerification=False`) 让 FIDO 自动失败；
   `go_back()` 作兜底。
   ⛔ **别用 Chrome flag `--disable-features=WebAuthentication`** —— 10-18 试过，
   它把登录重定向整条打断，页面直接变 `chrome-error://chromewebdata/`，比原问题更糟。
4. **最终 code 要挂 `page.on("request")`，不能只挂 `on("response")`** ——
   重定向到 `https://localhost?code=…` 没有服务器，TLS 握手就失败 ⇒ **没有 response 事件**，
   `page.url` 也只剩 `chrome-error://chromewebdata/`。10-18 密码其实已经验过了、code 已经发出来了，
   却因为只监听 response 而报 FAIL。
5. **6 格验证码框 `fill()` 只填第一格** —— `#codeEntry-0..5` 各 `maxlength=1`，
   必须逐格 click + 按 `HTMLInputElement.prototype` value descriptor 设值 + 派发 input/change；
   满 6 位自动提交，**没有提交按钮**。另：`input[type='text']:visible` 会匹配到这些格子，
   找邮箱输入框的选择器必须排掉 id 含 `codeEntry` 的。

**卡住检测**：同一 (url, body 指纹) 连续 12 轮无变化就带现场截图退出（`shots/*-stuck.png`）。
加这个的原因是上面坑 3、坑 4 两轮都磨满 300s 超时才报错，而现场其实第 10 秒就定格了。

🔴 **RT 落点刻意在仓外**（`~/.local/kiro-auto/outlook-rt/`，0700/0600）：脚本进 git，
refresh_token 不进 git。别改回脚本同级目录——那是 skill 的 git 跟踪区。

## AWS MFA 轮换（2026-10-08 实测）

### 已验证全链路（10-18 四号实证：richelle/gustino/fendry/joshua）

```
DELETE_OLD=1 模式（推荐）：
  profile.aws.amazon.com → redirect → signin.aws 登录
  → credentials.signin.aws/#/security
  → STEP 3.5: 先删旧 MFA（"delete" 确认弹窗 → 可能登出 → 自动重登）
  → 注册新设备 → 选「身份验证器应用程序」radio → 「下一步」
  → 「显示密钥」/「无法扫描 QR 码」→ 取出 base32 seed
  → pyotp 算码填入 → 「分配 MFA」→ 绑定成功
```

🔴 **为什么先删后注册**：有旧 MFA 时点「注册设备」会触发 AWS 重认证跳转（redirect 到 login），
处理不好就卡死。删掉旧设备后注册不需要重认证（richelle 0 设备直接成功验证了这一点）。

### 用法

```bash
# 单号，先删旧设备再注册新的（推荐）
TARGET_EMAIL=x@binus.ac.id PW='<buildid pw>' TOTP_SEED='<现有 seed>' DELETE_OLD=1 \
  BINUS_PW='<邮箱密码>' \
  PYTHONPATH=scripts ~/.local/kiro-auto/venv/bin/python scripts/aws_mfa_rotate.py

# 批量
DELETE_OLD=1 scripts/aws_mfa_rotate.sh /tmp/rotate-creds.ndjson

# 新 seed 写到 /tmp/aws-mfa-rotate-<user>-<HHMMSS>.json（chmod 600，不覆盖）
```

🔴 **BINUS_PW 必须传**：删除 MFA 后 AWS 可能登出，重登时无 TOTP，走邮箱验证码（Outlook）。

### 🔴 MFA 轮换后必须刷新 Account Manager + 198 池 token

MFA 轮换后旧 refreshToken 绑着旧 MFA session，Account Manager 里该号变 `status=invalid`，
kiro-rs 池里该号报 `TooManyRefreshFailures`。必须走以下流程恢复：

```bash
# 1. Account Manager：先退 App，删旧条目，用新 seed 重登
osascript -e 'quit app "KiroAccountManager"'
# 手动从 accounts.json 删该 email 的条目（备份先）
cd /tmp && TARGET_EMAIL=x@binus.ac.id PW='<pwd>' TOTP_SEED='<新seed>' \
  PYTHONPATH=~/codes/carher-admin/.claude/skills/kiro-account-manager-ops/scripts \
  ~/.local/kiro-auto/venv/bin/python \
  ~/codes/carher-admin/.claude/skills/kiro-account-manager-ops/scripts/kiro_oneshot.py

# 2. 198 池：读 admin key → reset+enable → 删旧 → 加新
ADMIN_KEY=$(sudo kubectl -n kiro-rs get secret kiro-rs-bootstrap -o jsonpath='{.data.adminApiKey}' | base64 -d)
# reset failure counter
curl -s -X POST "http://127.0.0.1:30510/api/admin/credentials/<old_id>/reset" -H "x-api-key: $ADMIN_KEY"
# enable
curl -s -X POST "http://127.0.0.1:30510/api/admin/credentials/<old_id>/disabled" -H "x-api-key: $ADMIN_KEY" \
  -H "Content-Type: application/json" -d '{"disabled":false}'
# remove old
python3 ~/codes/carher-admin/scripts/kiro-pool.py remove <old_id>
# re-login 后导出 refreshToken，再 add 回去
python3 ~/codes/carher-admin/scripts/kiro-pool.py add /tmp/<export>.json --priority 0
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

备选路径：走 `Verify via email`（已确认是按钮、能点），用 Graph API 取验证码
（`kiro_common.graph_fetch_aws_otp`，已在邮箱 MFA 登录中验证走通）。

## BuilderId 改密码 + 轮转 2FA（2026-10-17 实测通过）

**场景：** 用户给了一批 BuilderId 账号的原始凭据（邮箱密码 + AWS 密码 + TOTP seed），
需要全部改掉密码和 2FA 再加进 kiro 池。

### 流程（10-17 实测：改密码 + 2FA 轮转均已走通）

```
change_builderid_pwd_2fa.py (DO_PWD=1 DO_2FA=1)
  → profile.aws.amazon.com（触发 Builder ID 登录）
  → drive_login() 自动处理 email/pwd/captcha/WAF/MFA/consent
  → 登录后 profile 页点「安全」tab
  → 跳转 credentials.signin.aws/#/security
  → 改密码：点「更改密码」→ 填两次新密码 → 提交
  → 改 2FA：选中设备 radio → 删除（输入"delete"确认）
    → 会话可能丢失 → 自动重新登录（无 TOTP）
    → 注册新设备 → 选「身份验证器应用程序」
    → 显示密钥 → 提取 Base32 secret → pyotp 验证 → 完成
  → 结果写 /tmp/kiro-pwd-2fa-<user>.json
```

### 用法

```bash
# 单号改密码（不改 2FA）
cd /tmp
EMAIL=x@binus.ac.id OLD_PWD='<AWS密码>' MFA_SECRET='<TOTP seed>' \
  DO_PWD=1 DO_2FA=0 CDP_PORT=9222 \
  ~/.local/kiro-auto/venv/bin/python -I \
  ~/codes/carher-admin/.claude/skills/kiro-account-manager-ops/scripts/change_builderid_pwd_2fa.py

# 单号只改 2FA
cd /tmp
EMAIL=x@binus.ac.id OLD_PWD='<AWS密码>' MFA_SECRET='<TOTP seed>' \
  DO_PWD=0 DO_2FA=1 \
  ~/.local/kiro-auto/venv/bin/python -I \
  ~/codes/carher-admin/.claude/skills/kiro-account-manager-ops/scripts/change_builderid_pwd_2fa.py

# 批量改密码 + 2FA（2026-10-19 起从 creds ndjson 读账号，不再编辑脚本）
#   每行 {"email":..,"email_pw":<邮箱密码>,"pw":<AWS密码>,"alt_pw":"","totp":<现有2FA seed>}
#   与 kiro_batch.sh / aws_mfa_rotate.sh 同一种 creds 格式（多认 email_pw 一个字段）
scripts/batch_pwd_2fa.sh work-kiro-add-<日期>/creds-pwd2fa.ndjson
# 默认 DELETE_OLD=1（delete-before-register）+ 自动透传 BINUS_PW
# summary → /tmp/kiro-batch-pwd-2fa-results/summary.json（含明文新凭据，抄进飞书后即删）
```

### 🔴 10-17 2FA 实战教训（3 个 bug 修复后全程自动化走通）

| 问题 | 根因 | 修法 |
|------|------|------|
| `button:text-is("删除")` 10s 超时 | Playwright `:text-is()` 不匹配 CJK 文本 | `js_click_exact_button()` — 用 JS evaluate 精确匹配 `textContent.trim()` |
| 确认弹窗输错（该输 "delete" 输了 TOTP） | 只看了 body 有无 "delete" 字样，没看 input placeholder | 先读 placeholder，placeholder 含 "delete" → 输 placeholder 文本；否则 → TOTP |
| 注册设备后页面全白 | SPA 加载慢 + 等待不足 | 每步加 `wait_for_body()` + 等待翻倍（10→15s tab切换，5→10s 注册点击后） |
| 注册流程 reload 后 Timeout 30s | `wait_for_body()` 对有状态 URL（含 `workflowStateHandle`）执行了 reload → 状态销毁 | `wait_for_body(no_reload=True)` 跳过 reload，只等不刷；MFA 注册三步全用此模式 |

**MFA 删除后会话可能丢失**：删 MFA 后 AWS 可能自动登出。脚本检测后自动
`drive_login(page, ..., totp_seed=None)` 重新登录（此时无 TOTP 要求）。

### 🔴 Passkey 陷阱（10-17 两个号触发）

`aws_mfa_rotate.py` 在设备类型选择页等待不足时会注册 AWS **内置身份验证器（passkey/WebAuthn）**
而非 TOTP authenticator app。

**根因：** 页面等待逻辑把 Privacy/Site terms 页脚按钮当成业务按钮提前 `break`，
radio 还没渲染就点「下一步」→ 默认走 passkey 注册。已修补 lines 368-374
（过滤非业务按钮 + 等 radio 元素出现才 break）。

**关键发现：passkey 是叠加注册，不替换已有 TOTP。** 原 TOTP seed 仍可正常登录。
10-17 richelle/evan 被误判为「坏号」搁置 2 小时，实际用原始 TOTP 一次登录成功。

**判断逻辑：**
- 2FA 轮换失败（注册了 passkey）→ **先用原 TOTP seed 试登录**
- 登录成功 → 账号可用，只是 2FA 没换（如需换，先手动删 passkey 再重跑）
- 登录也失败 → 页面流程被 passkey 干扰（evan 重试时遇到），需手动浏览器干预

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

## 当前状态 (2026-10-19)

accounts.json **161 个账户**（10-19 现量）。kiro-rs 池 183 条。
三条登录路径全通：BuilderId+TOTP / BuilderId+邮箱MFA(Graph) / GitHub。

**MFA 轮换**：`aws_mfa_rotate.py` DELETE_OLD=1 模式（先删后注册）四号实战验证：
richelle/gustino/fendry/joshua 全部成功。关键改进：
- STEP 3.5 先删旧设备避免重认证跳转
- 删除后自动检测登出 + drive_login 重登
- `navigate_to_security` 修复 NameError（`click_by_label` → `click_by_text_js`）

**改密码 + 2FA 轮换**：`change_builderid_pwd_2fa.py` 单脚本链路（10-17 验证走通）。

🔴 **MFA 轮换后必做**：Account Manager 删旧条目 → kiro_oneshot 重登 → kiro-rs 池 remove+add。
🔴 **AWS Builder ID 登录页 10-17 改版**：旧 Google SSO 中转消失，按钮「继续」非「下一步」，密码页有 CAPTCHA。

历史遗留未加：**aldi.hafizh@binus.ac.id** — Outlook 密码错误（两个变体均被 M365
拒绝），需手动到 M365 后台核实/重置。

### 10-18：两个无 2FA 的消费级 outlook 号（全自动，首次跑通上游取 RT）

`justyceleonardosz@outlook.com`（AcctMgr #156，池 id=187）、
`christlemacin9005@outlook.com`（AcctMgr #155，池 id=186）。均 IdC / us-east-1 / priority=0。

两个号都**没有 TOTP seed**，AWS BuilderId 强制邮箱 MFA ⇒ 必须先有 Graph RT ⇒ 本轮现做了
`outlook_graph_rt.py`。链路：取 RT（~40s）→ `kiro_builderid_email_mfa.py` → `kiro-pool.py add-batch`。

两号差异，说明这条路两种形态都覆盖：
- christlemacin9005 的恢复邮箱**上一轮已绑好**，登录直接落「验证你的电子邮件」页 ⇒ 走「使用密码」绕过。
- justyceleonardosz **从零绑**，走完整 credentialaction：加恢复邮箱 → mail.tm 收 6 位码 → 填。
  且 AWS 侧还撞了一次点击式 WAF 挑战，`drive_login` 自动过了。

加池那一刻 `currentId` 仍是 164 没切新号，**这是对的**（priority 平级取先出现的），
老号有余额就别切，见 `kiro-pool.py` 自带那段提示。
