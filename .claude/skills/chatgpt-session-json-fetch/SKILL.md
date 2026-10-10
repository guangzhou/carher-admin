---
name: chatgpt-session-json-fetch
description: >-
  「给我 acct-N 的 session JSON」「拿一下 chatgpt.com/api/auth/session 的输出」
  「要 sessionToken / storage_state」——一条命令
  `./scripts/chatgpt-acct-session-fetch.sh <N>`(凭据自动查飞书表)。含为什么必须走
  EIP 浏览器而不能 curl(CF 只挡 web 路径放过 codex API 路径)、session.json 与
  auth.json 是两套凭据的对照、三道取回门禁(sha256 两端对账而非比长度)、
  手机墙/停用两种失败面的辨识。
  Use when 用户要 session JSON / sessionToken / accessToken / next-auth cookie,
  or 提到 chatgpt-acct-session-fetch.sh / SESSION_DUMP / api/auth/session。
  OAuth 装回凭据在姊妹 skill chatgpt-acct-token-expiry-triage。
---

# 拿 chatgpt.com 的 session JSON

2026-10-05 为 acct-207 跑通两轮后沉淀。**一条命令**：

```bash
./scripts/chatgpt-acct-session-fetch.sh 207
```

凭据省略时自动从飞书密码表查。产物两份：

| 文件 | 内容 |
|---|---|
| `/tmp/session-acct-<N>.json` | 格式化(indent=2) |
| `/tmp/session-acct-<N>.raw.txt` | **页面原样一行**，就是浏览器里看到的那串 |

脚本末尾自动做「页面原文 ↔ 文件」逐字段比对并打印结论，所以**不必再手工回答「是不是少给了」**。

## 为什么必须走浏览器 —— 别再试 curl / Bearer

`/api/auth/session` 认的是 **`__Secure-next-auth.session-token` cookie**，不认 OAuth Bearer。
2026-10-05 拿 acct-216（当时在扛 12223 req/24h，凭据确定活）从 acct pod 内实测：

| 端点 | 结果 |
|---|---|
| `chatgpt.com/api/auth/session` | **CF 403 html** |
| `chatgpt.com/backend-api/me` | **CF 403 html** |
| `chatgpt.com/backend-api/accounts/check/v4-2023-04-27` | **CF 403 html** |
| `chatgpt.com/backend-api/wham/usage` | ✅ 200 |

**同一个出口、同一把 token，web 路径全 403 而 codex API 路径通** ⇒ CF 只挡 web 路径。
所以这个端点没有 HTTP 捷径，只能靠 EIP 节点的 headed 浏览器登录后 goto。
⚠️ 那个 403 回的是 **html 而不是 401**，很容易被误读成「号坏了」——
它只说明这条路被 CF 挡了，**不携带任何账号健康信息**。

## session.json 与 auth.json 是两套东西，别混

| | `auth.json`（OAuth device grant） | `session.json`（本 skill） |
|---|---|---|
| 认证方式 | Bearer `access_token` + `refresh_token` | cookie `__Secure-next-auth.session-token` |
| `client_id` | `app_EMoamEEZ73f0CkXaXp7hrann`（codex） | `app_X8zY6vW2pQ9tR3dE7nK1jL5gH`（web） |
| 能续期 | 能（refresh grant） | **不能**，过期只能重登 |
| 有效期 | AT ~10d，RT 轮转 | session 3 个月，内含 AT ~10d |
| 用途 | litellm acct pod 的上游凭据 | chatgpt.com web 端桥接（zerokey） |

**本 skill 全程只读**：不碰任何 acct pod 的 `auth.json`、不改 deployment、不 scale、不重启。
⇒ 对正在服务的号也安全（对比：改 pod-spec 可能永久打死号，见
[[feedback_restarting_a_serving_acct_can_kill_it_permanently]]）。

## 端点输出的完整形状（8 个顶层 key）

```
WARNING_BANNER  user{id,name,email,iat,amr,mfa}  expires  account{11 子字段}
accessToken(~1956)  authProvider  sessionToken(~4013 JWE)  rumViewTags
```

- `account.planType` —— **这是一把独立的订阅尺子**，比 id_token 的 `chatgpt_plan_type` 可信
  （后者是签发快照，见 [[feedback_idtoken_plan_pro_is_a_snapshot_not_live_health]]）。
  207 实测 session 与 AT claim 双向一致 = `free`。
- free 号的 AT claim 里**没有** `chatgpt_subscription_active_until` 字段（不是空值，是整个不存在）。
- `expires`（session，3 个月）与 AT 的 `exp`（~10 天）**是两个有效期**，别混。
- 每次重登会换 `session_id` / `jti` / `sessionToken`，**旧 session 不会被作废**，两份都能用。

## 三道取回门禁（都验过负例 + 阳性对照）

`aliyun-onboard-workspace.sh session <N>` 内置：

1. **sha256 两端对账** —— 本地算的必须等于 pod 内 `sha256sum`。
   🔴 **比长度没有判别力**：历史上 209/210/211 三号新旧 `access_token` 长度逐一恰好相等
   （[[feedback_token_length_equality_is_not_an_install_ruler]]）。
2. `accessToken` ≥1000 字符、`sessionToken` ≥500 字符 —— 拦空壳。
3. JSON 必须可解析。

**为什么非要 sha**：读取 pod 被驱逐时 `kubectl exec ... cat` 会**成功返回一个空文件**，
exit 0，不对账就会把空壳当成"取到了"（同 `auth` 动作那条教训）。
负例实测：空文件被 sha 拦住（`e3b0c442…` ≠ `5ea5ceab…`）；5 字符 token 被门禁 2 拦住；
干净工件放行（阳性对照，防门禁恒拦）。

## 两种失败面（都不是脚本故障）

**① `/add-phone` 手机墙** —— 日志 `⏸ MANUAL_PHONE_REQUIRED`，job Failed。
登录其实**成功了**（OTP 过、Codex toggle 开），卡在 OpenAI 要求绑手机才放行。
2026-10-05 实测 226-229 **4/4 全撞**。脚本会单独识别并报，别误读成坏了。
接码通道：`scripts/chatgpt-onboard/gen-phone-pool.sh` 有 16 个号码+key 的池
（`app.yuntl.cc`），env `PHONE_NUMBER` + `SMS_API_URL`；飞书表另登记了 `smsbee.cc`。
⚠️ 绑手机是**不可逆**的外部动作，而且如果订阅已掉成 free，绑完拿到的也只是 free 凭据 ⇒
**先用 `aliyun-eip-onboard.sh inspect <N>` 读 Billing 页确认档位再决定绑不绑**。

**② `account_deactivated`** —— 号被 OpenAI 停用，救不回，需新 email + 新 Pro。

## 实现落点

- `scripts/chatgpt-acct-session-fetch.sh` —— 驱动（本 skill 入口）
- `scripts/chatgpt-onboard/chatgpt-litellm-oauth.py` —— `SESSION_DUMP=1` 只读分支，
  插在 `if logged:` 之前：取完 session 就 `sys.exit(0)`，**不开 toggle、不走 device flow、
  不写 auth.json**。同文件已有 `MAIL_INSPECT` / `APPEAL_MODE=inspect` / `RESET_PROBE` 等同类只读模式。
- `scripts/aliyun-eip-onboard.sh` —— `session` ACTION（与 `toggle|oauth|inspect|renew` 并列）
- `scripts/aliyun-onboard-workspace.sh` —— `session <N> [out]` 动作，带上述三道门禁

## 操作纪律

- **launcher 退出 ≠ job 完成**。`aliyun-eip-onboard.sh` 发完 Job 就 return 0。
  判据只认 `.status.succeeded`（[[feedback_onboard_launcher_exit_is_not_job_completion]]）。
  驱动脚本已经按这条实现，别自己去看 launcher 的退出码。
- 删 job **只按显式编号**：`kubectl -n carher delete job cgpt-onboard-session-<N>`。
  ⛔ 绝不 `delete job -l job-name`（每个 Job 都带这标签 = 删光整个 ns）、不用 `cgpt-*` 通配
  （会扫掉别的会话在跑的 job）。
- 产物是**明文凭据**，能直接访问账号。用完清三处：
  本地 `/tmp/session-acct-*`、PVC `/work/session-acct-<N>.json`、读取 pod
  （`./scripts/aliyun-onboard-workspace.sh rm`，它自带 TTL 7200s 也会自退）。
  ⚠️ zsh 里一条 `rm -f a* b*` 任一 glob 无匹配则**整条不执行**，`no matches found` 时
  「清理跑过了」是假的 —— 分行删并 `ls` 复核。
- 耗时 5~8min/号（走 mail.com 取码）。并发 3~4 个舒服，别一次开太多抢 EIP 出口。
- 🔴 **并发的真实约束是 Xvfb 的 display，不是节点余量。** `DISPLAY` 按 acct 编号取（`:207`），
  节点按奇偶钉（偶 `.86` / 奇 `172.16.16.122`）⇒ **同节点上两个 job 只要编号不同就不撞**，
  但**同一个编号的两个 job（哪怕一个是 `oauth-207` 一个是 `session-207`）必然撞**，
  后起的那个报 `FATAL: Xvfb 30s 未就绪(display :N)` +
  `Cannot establish any listening sockets`，脚本自带 HINT「同节点已有 onboard Job 占着 X」。
  2026-10-05 实证：我的 `session-207` 正在跑时，别的会话起的 `oauth-207` 直接 Failed。
  ⇒ 起 job 前先 `kubectl -n carher get job | grep "onboard-.*-<N>"` 看该编号有没有在跑；
  撞了是**别人或自己的同号 job**，不是这个号坏了。
  ⚠️ 别去删那个撞掉的 job —— 它可能是别的会话正在用的（这也是禁 `cgpt-*` 通配删的理由）。
- 出口隔离铁律：必须 hostNetwork + 钉 EIP 节点（偶 → `.86`，奇 → `172.16.16.122`）。
  普通 pod 走共享 NAT `47.84.112.136` —— 那是**线上 codex acct 的出口**，
  在普通 pod 里跑浏览器撞 CF 会污染生产出口 IP。驱动脚本已钉好，别改。

## 相关

[[feedback_session_json_needs_browser_cf_blocks_web_path]] ·
[[feedback_idtoken_plan_pro_is_a_snapshot_not_live_health]] ·
[[feedback_token_length_equality_is_not_an_install_ruler]] ·
[[feedback_onboard_launcher_exit_is_not_job_completion]] ·
[[project_acct_209_213_226_229_two_death_modes_2026_10_05]] ·
[[reference_chatgpt_login_session_bundle]] · [[topic_chatgpt_acct_pool_index]]
skill `chatgpt-acct-token-expiry-triage`（OAuth 装回）· `aliyun-chatgpt-acct-onboard`（端到端接新号）
