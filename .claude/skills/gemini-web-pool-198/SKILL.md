---
name: gemini-web-pool-198
description: >-
  Google AI Pro 号的 gemini.google.com 网页 → 198 两个网关 → LiteLLM 的全套运维：
  `gemini-web`（Gemini-FastAPI，多号池，聊天 gemini-4-argon / 3.1-pro / 3.8-flash-web / 3.5-flash-lite）
  与 `gemini-media`（gemini-web-to-api，生图 / 音乐 / 视频 / Deep Research；**视频和音乐已 40 腿，生图仍单号**）。
  Use when 用户要「gemini-4 / Argon 怎么开」「网页能用 API 不能用」「加个 gemini 号 / 给卡密加到池子」、
  问「这个号能用什么模型」、要 gemini 出图/音乐/视频/深度研究、gemini-4 在 Claude Code 里
  「API error · Retrying」/ 空回复 / 不调工具 / 说"我无法访问你的电脑" / 不是流式一下子出来、
  cookie 过期、额度 0/2400、升级或重建这两个网关、
  「哪些 google 号登不上 / 在飞书表里标出来 / 不在池中的号加进来」、要歌曲 / 歌词。
  「视频额度用光 / 只有一个号 / 给 gemini-media 扩号」、要并发出视频。
  含：为什么 Argon 只能走网页（Antigravity 只给 Ultra）、一条命令全自动加号（188 浏览器 + TOTP）、
  gemini-media 已扩到 39 个成员号（video/music 各 40 腿、image 仍单号）及其三个坑
  （出口 netpol 启动竞争要 initContainer、出不出视频按号不按出口、busybox wget 不认代理）、
  本地补丁 p1~p7 及其踩坑（p7 = /health 不再 init 停掉的号 + 选号跳过 TOS，修 10-30 整服务 NotReady 事故）、
  LiteLLM 接法里四个必踩的配置坑（视频验权**不用**再把内部名放进 key 允许列表，已被 `video_id_alias_auth` 推翻）、
  工具调用天花板约 8 成、10-31 现量（80 号 = 44 AVAILABLE + 27 未登录 + 9 TOS）、
  音乐能出但歌词只唱不返回文字、「歌 300s 出不来」= 网关两处写死 5 分钟上限。
---

# Gemini 网页号池（198）

## 为什么是网页这条路（先读，别重新调研）

| 入口 | gemini-4 Argon | 说明 |
|---|---|---|
| Gemini 网页 / App | ✅ Pro 和 Ultra | 2026-10-13 放给 Pro |
| Antigravity（cli-proxy-api / sub2api） | ❌ 只给 **Ultra** | 官方表 `antigravity.google/docs/models/`；目录里 Pro 号是 `gemini-4-argon-disabled` |
| AI Studio / Gemini API | ✅ 付费档 | 按量计费，不走订阅 |

⛔ 「网页上能看到 ⇒ API 应该也能用」是错的；⛔ 升级 CLIProxyAPI 也没用（上游 `gemini-4-argon-high` 对 Pro 号直打 404）。
Jio 送的 Pro 在 Antigravity 上还被当成**非付费**：Claude 5.5 拿不到，**11-02 起 Claude 4.6 / GPT-OSS 全断**（见 skill `cliproxy-antigravity-ops`）。

## 拓扑

```
Google AI Pro 号 ──浏览器登录(188)──> __Secure-1PSID/1PSIDTS
   ├─> litellm-dev/deploy/gemini-web   :8000  Gemini-FastAPI@1731445 + carher p1~p3.2   多号 round-robin
   │     config: Secret gemini-web-secrets(config.yaml)；cookie 轮换缓存 PVC gemini-web-data
   │     LiteLLM: web-gemini-4-argon / web-gemini-3.1-pro / web-gemini-3.8-flash / web-gemini-3.5-flash-lite
   └─> litellm-dev/deploy/gemini-media :4981  gemini-web-to-api@681ca80 + 越界补丁 + p2 chat-media
         + litellm-dev/deploy/gemini-media-<name> ×39（10-31 起，全在节点 242）= video/music 各 40 腿
         env: Secret gemini-media-env(GEMINI_COOKIES) + GEMINI_MEDIA_PUBLIC_BASE；**无鉴权** ⇒ NetworkPolicy gemini-media-ingress 只放 litellm-product
         manifest 真源 = `scripts/gemini-web/gemini-media-deploy.yaml`（198:/root/gwt-build/k8s.yaml 已过时）

🔴 **生图链接（10-16 修）**：Gemini 回的 `lh3.googleusercontent.com/gg-dl/...` **只认这个 Google 号的 cookie**
（无 cookie 302→302→403，同 URL 带 `__Secure-1PSID/1PSIDTS` 就是 200 jpeg）⇒ 原样透出去用户永远打不开。
- `/v1/images/generations`：LiteLLM 行 `gemini-media/image` 带 `extra_body={"response_format":"b64_json"}` ⇒ 网关用 cookie 下载后回 b64。
  客户端自己传 `response_format` 会被 LiteLLM 400（`UnsupportedParamsError`），只能在 deployment 级塞。
- chat 线型（Cursor/Cherry/OWUI）：p2 补丁让网关用 cookie 下图、内存留 24h（上限 512MB），回复里换成
  `https://chat.auto-link.com.cn/gemini-media/openai/v1/media/<32hex>`（10-29 起；chat 段 `chat-subpath:gemini-media` 转本机 cc，
  cc 段 `carher-muse-media` 只放这一条路径；老 cc 链接照样能开。挂法=`scripts/chat-model-subpath/cc-paths.sh`）。
  ⚠️ 副本在进程内存：**滚 gemini-media 一次，之前聊天里的图全部 404**。
         LiteLLM: web-gemini-image / web-gemini-music / web-gemini-video + pass-through /gemini-deepresearch
```

198 文件：`/root/gemini-web-build/`（config.yaml、accounts/*.json|*.cookies.json、api_key，全 root 600）、`/root/gwt-build/`。
🔴 **两个网关必须用同一个号的两份独立登录**：共用一份 cookie 会互相轮换 1PSIDTS 把对方踢掉。

**规模（别照抄数字，跑下面两条现看）**：
```bash
# ① 池子有几个号（启动日志，重启后才更新）
kubectl -n litellm-dev logs deploy/gemini-web --tail=4000 | grep "clients initialized" | tail -1
# ② 现在几个能服务（唯一判据，authed 不等于可用：TOS_PENDING 也是 authed=True）
POD=$(kubectl -n litellm-dev get pod -l app=gemini-web -o name | head -1)
kubectl -n litellm-dev exec $POD -- python3 -c 'import urllib.request,json,collections,os
r=urllib.request.Request("http://127.0.0.1:8000/carher/status",headers={"Authorization":"Bearer "+os.environ["K"]})
cl=json.load(urllib.request.urlopen(r,timeout=60))["clients"]
print(len(cl), collections.Counter(a["account_status"] for a in cl))'   # K=/root/gemini-web-build/api_key 第一行
```
10-31 02:4x 现量：**80 号 = 44 AVAILABLE + 27 UNAUTHENTICATED + 9 TOS_PENDING**（authed=True 53 个，running 80/80）。
cli-proxy-api Secret 84 个 `antigravity-*`（82 启用）；sub2api 82（组 10）。10-15 午那批 35 号只过 18：17 号登录前就 reCAPTCHA，新加坡两个 EIP 重试 9/9 同样拦 ⇒ 号风控，用户定「人机拦截的不搞，先加正常的」。
同一出口 IP（Linode 大阪）一晚登 50+ 号未见风控（abuse=Clean、无验证码墙）；但当晚 `gemini-3.1-pro`/偶发 3.8-flash 回 Google 通用失败文案（I'm having a hard time / Sorry, something went wrong），**argon 同时正常** —— 用户定性「3.1 Pro 老模型不用管」，未查因。

## 每号固定出口 IP + 按号统计（10-29）

- **出口**：Gemini-FastAPI 每个 client 自带 `proxy` 字段（curl_cffi 会话级）。10-29 起 76 号按「同一个 Google 号在 cli-proxy 的家 IP」分到 8 个出口（`google_pool.json` homes；jio-pro-1 没家 ⇒ 补号最少的 IP）。实测每个 proxy 的出口 IP 都对，重启后 8/8 真聊天全对。
  工具 `chatgpt-acct-console/gemini_web_pool.py`（198 上 root）：`plan` / `apply [id ...]` / `clear`（回滚全部 proxy=null）/ `status`；管理台 8796「Gemini 网页」页可单号换、一键按 cli-proxy 重分。每次改 = 写 `/root/gemini-web-build/config.yaml`（自动 `.bak-<ts>`）→ Secret → **重启 gemini-web（Recreate，约 2~3 分钟中断）**。
  mihomo 三个出口（109.176/49.157/64.118）要 netpol `kiro-rs/cliproxy-muse-to-kiro-egress` 放行 `app=gemini-web`（已加）。`build_config.py` 加号时**保留**已有号的 proxy（以前写死 None，加号会把绑定冲掉）。
- **按号统计 = p4 补丁** `GET /carher/status`（同 api_key）：每号 running / account_status / cooldown / proxy host / 进程内请求成败 / 上次错误 / usage_info。计数在进程内，**重启清零**；跨重启看 LiteLLM SpendLogs 按 `web-gemini-*` 组。
- **登录态 + 额度 = p5 补丁**（10-29 晚）：`/carher/status` 每号多 `authed` / `cookie_source` / `models` / `pro`；新增 `POST /carher/quota`（body `{"ids":[...]}`，只读 RPC 不花额度）。
  p5 当时的镜像是 `…:20261029-1731445-p5`；**现役已是 p7，见「回滚」节**（别照这一行 set image，会把 p6/p7 一起退掉）。
- 🔴 **出口 64.118.159.27 已摘**（10-29 晚实测）：经它打 `gemini.google.com/app` 恒 302 到
  `?google_abuse=GOOGLE_ABUSE_EXEMPTION%3D…`（Google 滥用拦截页），curl_cffi 直接
  `Redirect to internal IP … rejected (SSRF protection)` ⇒ 钉在它上面的 10 个号**连不上 Gemini**。
  另 7 个出口同一探针都 200（~827KB）。`gemini_web_pool.BLOCKED` 记了原因，`plan` 会把这 10 个号分走。

## 🔴 判登录态只认 SNlM0e，不认 account_status（10-29 查清）

**`account_status=UNAUTHENTICATED` 不能区分「cookie 过期」和「根本没给 cookie 的匿名 guest」** ——
实测（阳性对照：`secure_1psid=None` 完全不给 cookie）两者读数**逐项同形**：
`account_status=UNAUTHENTICATED` + 模型表只有 `gemini-flash-lite` + **纯聊天照样回话**。
唯一能分开的判据是 **`access_token`（页面里的 `SNlM0e`）**：认证会话有，guest 没有。

⇒ 「纯聊天正常」**根本不是登录正常的证据**：它正是 guest 身份在服务。

10-29 晚实测结论（76 号全查过）：
- `authed=False` **76/76**，`cookie_source` 全是 `Base Cookies`（缓存目录 `/app/data/cookies` 是空的，没有 issue #310 的「缓存盖掉新 cookie」问题）
- 模型表 **76/76 只有 `gemini-flash-lite`** ⇒ `gemini-4-argon` / `gemini-3.1-pro` 走 `_resolve_model_by_name` 直接
  `ValueError: Unknown model name` ⇒ 网关 **502**；`gemini-3.8-flash` 被 3 级「版本无关归一化」**静默落到 flash-lite**
  （⚠️ 所以「3.8-flash 能回话」也是假绿，回答它的是免费 flash-lite）
- `rotate_1psidts` 对 5/5 抽样号全 `AuthError` ⇒ **1PSID 本身死了**，不是只有 1PSIDTS 过期
- **根因 = 这些 Google 号的密码被外部改过**：188 上真浏览器登录，页面原话
  「**Your password was changed 8 days ago**」（56 个号）/ 「10 days ago」（18 个）。
  全池体检 75/75：**74 密码已改 + 1 reCAPTCHA + 0 还能用**。cookie 在 config 里存了 14.4~15.3 天 ⇒ 时间线吻合
  （cookie 是 10-14/15 存的，10-19 和 10-21 被改密 ⇒ 之后 cookie 逐步失效）
- **不是我们改的**：`config.yaml` 里 76 号的 cookie 指纹和 10-15 11:41 那份**逐字节相同**；
  同一批号在 **cli-proxy（Antigravity）那边的 OAuth refresh token 全活、全 Pro**（`remainingFraction=1.0`，16 个模型）
  ⇒ 号本身没被封、订阅没过期，**只有网页这条路的密码凭据过时**
- **「忘记密码」走不通**：带 TOTP 的 recovery 流程 Google 回「You didn't provide enough info for Google to be sure
  this account is really yours」⇒ **必须找卖家要新密码**，我们自己救不回来
- gemini-media 那个单号（Secret `gemini-media-env`）**同样 authed=False / 只有 flash-lite** ⇒ 它「带文件回 200 但内容是编的」
  也是这个根因（guest 身份收不到文件）

量具：`scripts/gemini-web/login_health.py`（`check` / `quota` / `report` / `cards-export` / `pw-ingest`）、
`scripts/gemini-web/pwcensus.sh`（本机编排的密码体检）、`scripts/gemini-web/relogin.sh`（只重登没登上的号）。
🔴 **198 ssh 不到 188**（没 publickey）⇒ 所有要开浏览器的步骤只能从**本机 Mac** 发起。

## 加号（一条命令，全自动）

```bash
# 卡密放仓库内 .work/（已 gitignore，chmod 600），一行一张；---- 两侧空格、空行自动忽略
scripts/gemini-web/add-account.sh -f .work/gemini-batch/cards.txt
scripts/gemini-web/add-account.sh 'Email----Password----2FA密钥'            # 单张；也认 4 段（带辅助邮箱）
STAGES=web         scripts/gemini-web/add-account.sh -f cards.txt            # 只网页池
STAGES=antigravity scripts/gemini-web/add-account.sh -f cards.txt            # 只 Antigravity（补跑用）
```

批量模式：逐号登录，**最后 gemini-web 只重启一次、cli-proxy-api 只装一次**（canary 挡流量）、sub2api 一次。
失败的号报出来跳过，其余照常进；重跑幂等（cookie/凭据覆盖刷新）。
补跑 Antigravity 时别靠手记名单：从 Secret `cliproxy-secrets` 现读已有 `antigravity-*` key，差集就是待补。
节奏（10-14 实测 50 号）：网页登录 ~30s/号，Antigravity ~1.5min/号；30 号一批 ≈ 15min + 45min。
浏览器在 **188 现成的 playwright 镜像 + Xvfb + patchright** 里跑（⛔别在 198 再造浏览器环境；本机 Mac 连不上 Google）。

踩过的坑（脚本已处理，改脚本时别回退）：
- TOTP 页 URL 的 `continue=` 里带 `gemini.google.com` ⇒ 判「登录完成」必须看 **host 前缀**。
- **自拍提示页**（Add a selfie for sign-in / just in case / Protect your account access with a selfie）可能出在 `myaccount.google.com`，**也可能出在 URL 仍含 `challenge/totp` 的 accounts 页** ⇒ 按页面文字识别；且它的「Not now」**常常点了不动** ⇒ 第 2 次碰到就直接 `goto gemini.google.com/app`（会话此时已登录）。按钮 click 必须 `timeout=3000`，默认 30s × 5 次会把整个 150s 预算吃光。
- 提交密码后仍停在密码页（慢）⇒ 重提最多 2 次；页面写 Wrong password 才判失败。
- 读页面时页面在跳转（Execution context was destroyed）⇒ 当瞬态重查；脚本任何异常都必须打出 `STOP …` 一行，否则批量日志里是「静默失败」（10-14 第一批 5 号就是这样丢的）。
- 到了 gemini.google.com 但 cookie 只有 `1PSIDTS` 没有 `1PSID` ⇒ 等 5s 刷新重读，最多 4 次。
- 🔴 bash 循环里 `ssh` 会吃掉 `while read` 的 stdin ⇒ 循环只跑第一个号（10-14 第一批 Antigravity 只进 1/10）。读名单用 `read -u 3 … 3<<<"$LIST"`。
- 🔴 **「Verify it's you / Confirm you're not a robot」出在输密码之前 = 号本身被风控，不是出口 IP**（10-15 实证：同一批 11 号在 188、阿里云 EIP .98、.100 三个出口全拦；同一 EIP 上老号 caplerial 对照照常登入）。reCAPTCHA 只能真人过 ⇒ 别换出口硬撞，报给用户（真人 VNC 过 / 找卖家换 / 隔天重试）。该批卡密留在 `.work/gemini-batch/cards-captcha-blocked-11.txt`。换出口工具：`scripts/gemini-web/aliyun-eip-login.sh login|oauth <name> < acct.json`（阿里云 EIP 节点 hostNetwork Job，规则同 aliyun-eip-onboard.sh）。
- Antigravity 403「**not eligible for Gemini Code Assist for individuals … must be 18 years old**」= 号的年龄/资格不够，网页 Gemini 照常能用但 Antigravity 永远不行 ⇒ cliproxy 凭据置 `disabled:true`（`patch secret` + canary 重启）+ sub2api `rollback --ids`。`install` 之后网关 verify 报「真故障」就先查这个（10-15 buxtsuruda85、culcasireidinger）。
- Antigravity consent 有一页「Make sure that you downloaded this app from Google」，按钮叫 **Sign in**；且 URL 里带 `totp` 字样，TOTP 判定要同时看输入框存在。
- Antigravity 新号 hub UA 报 403 VALIDATION_REQUIRED；`google_validate.py` 能走完验证页（有时显示 Authentication successful、有时 Google 回 500）**都没用** ⇒ 照老号钉 `1.0.0` UA + `gemini-3.8-flash-tiered→high` alias。50/50 新号都是这样装进去的。

`gemini-media` 不在脚本里：它只吃一个号。换号 = 188 跑一次 `google_login.py`（用一个新的 name 拿独立 session）→ 改 Secret `gemini-media-env` 的 `GEMINI_COOKIES` → rollout restart。

## 重新登录（cookie 失效时，不加号）

```bash
# 只重登「没登上」的号（判据 authed=false）。本机 Mac 上跑；一次重启收口（≈3 分钟中断）
DRY=1 bash scripts/gemini-web/relogin.sh                       # 先看要动哪些号
CARDS=.work/gemini-batch/new-cards.txt bash scripts/gemini-web/relogin.sh
bash scripts/gemini-web/relogin.sh id1 id2                      # 指定号
# 密码体检（在用户撞上之前发现 cookie 要失效）
bash scripts/gemini-web/pwcensus.sh                             # 全池，4 车道，每号 20~40s
```

和 `add-account.sh` 的分工：**加新号**走 add-account（web + antigravity 两段），**只刷 cookie** 走 relogin
（不碰 antigravity、不加号）。relogin 的几条硬规矩，改脚本时别回退：
- **密码被改过的号必须带 `CARDS=`**：用 198 上的旧密码重登必然在密码页就 STOP。
- 只有**真拿到两个 cookie** 的号才写回 198；失败的号保留旧 cookie ⇒ 半途失败不会把配置搞坏。
- 一个都没登上就**直接退出，不重启**。
- 重启前打印最近 10 分钟 LiteLLM 侧请求数并要人确认（Recreate = 全池中断）。
- 重启后用 `login_health check` 复核被改的号，**authed 必须翻成 true** 才算成功（别只看「重启成功」）。

**监控：怎么在用户撞上之前知道要重登** —— 管理台 8796「Gemini 网页」页：
- 「真登上的号 N/<池子总数>」KPI 和每号「登录」列（p5 `authed`，每 10 分钟自动体检一次）。
  🔴 **这个 KPI 的分子是 `authed`，不是「能服务」**：TOS_PENDING 的号 authed=True 但一派就 1040
  （10-31 现量 authed 53、能服务 44）⇒ 判容量看 `account_status=AVAILABLE` 那一列。
- 「密码体检」KPI（`pwcensus.sh` 的结果，按号显示「密码被改 8d」）
- 每号「cookie」列 = 配置里这份 cookie 存了多少天（经验值：10-29 这批是 ~15 天时发现全死的）

### ✅ 10-29 晚更正：密码是我们自己轮换的，新凭据在飞书多维表

下面那两节（「密码被卡商改过」「网页版出图已被 Google 关掉」）**都已被实测推翻**，保留只为留痕。

- **新密码 + 新 32 位 TOTP**：飞书多维表 base `RfL0bCL2JasVH3sh2y5c1U9dnGc` table `tbl2lpplaFBYjavy`
  （102 号有值 / 91 条 `密码状态=成功`）。读法：
  `lark-cli base +record-list --base-token RfL0bCL2JasVH3sh2y5c1U9dnGc --table-id tbl2lpplaFBYjavy --limit 200`
  （flag 是 `--base-token`，limit 上限 200，**输出是 markdown 表不是 JSON**）。
- 实测 6 号：**2 号登进去**（goeringcheney / konieczkabookman，Pro）；2 号在密码页**之前**就 reCAPTCHA
  （号级风控，和密码无关）；**2 号密码对但表里的 TOTP 被拒 `Wrong code. Try again.`** ⇒ 2FA 栏有漂移，别当权威；
  1 号登进去但不是 Pro ⇒ `Gemini account cannot select models (status 1040)`，gemini-media 用不了。
- 🔴 **出图能不能用是按号的**：同为 Pro、同样新登录、同一出口、同一镜像，goeringcheney 出真图，
  konieczkabookman 回 `provider returned no generated images`（上游原始帧 `failed to parse response. Sample: )]}'…`）
  而**纯聊天正常** ⇒ 诊断出图先**换号对照**，别先怀疑 cookie / 地区 / 代码。
- ✅ **视频实测通了**（我们第一条真实成功）：`POST /openai/v1/videos` → 96s completed → `/content` 下 2.9MB MP4，
  ffprobe `h264 1280x720 24fps + aac, 10.005s`。请求里的 `seconds` 确认被忽略；回包那句
  `I'm generating your video…` 是 Gemini 原话透出来的，**判完成只看 `status=completed`**。
- ~~❌ 音乐仍未产出~~（**10-30 已出成**，见「音乐 / 歌词 / 视频的真实边界」节）。
- 🔴 **图片这条路多号还不能上**（video / music 已于 10-31 扩到 40 腿，见「gemini-media 扩号」节）：
  公网图链接是 **pod 本地内存**，而 nginx 那段
  `^/gemini-media/openai/v1/media/([0-9a-f]{32})$` 写死 `svc gemini-media` 的 ClusterIP ⇒
  pool 成员出的图经公网必然 404 `expired or unknown`（10-29 实测）。要多号得先让这段能按 id 找对 pod，
  或让 chat 线型直回 b64。

### 📋 10-30 现量：76 号 = 40 可用 + 36 不可用，飞书表已加状态列

- 分类（判据 = `/carher/status` 的 `authed`+`account_status` + pod 里逐号 `GeminiClient.init()` 只读探针 + 10-29 relogin 截图序列）：
  **24 TOTP 错**（截图都到了 TOTP 页 ⇒ 密码是对的）· **9 TOS_PENDING**（已登录、只差点同意条款）·
  1 密码错 · 1 图形验证码 · 1 无卡密（`jio-pro-1`）。
- 🔴 **p6 拦不住 TOS_PENDING**：`_authed()` 只看 access_token，这 9 个 authed=True ⇒ 照常轮询，一轮到就 1040。
  ✅ **10-30 已修**：p7 把 `account_status` 1040/1042 也算进过滤（全池都是 TOS 时不跳过，避免池子变空），
  上线后 24 发真请求落 TOS 号 0 次（修前 42 次里 6 次）。这 9 个号**仍在池里**（10-31 现量 9 个 TOS_PENDING），
  只是不会被派到 ⇒ 看到 `TOS_PENDING>0` 不等于有故障。
- 🔴 **表里【密码状态】【2FA状态】不是可用性判据**：36 个坏号里 35 个写着双成功。它记的是「改凭据这步做完了」。
- **表里新增两列**（给人工分活用，按它筛选）：`池子状态(10-30)` 单选 `fldqrUnoCi`、`人工动作(10-30)` 文本 `fldxIvn9z5`。
  写之前逐格快照原 9 列，写后 diff=0。主键列 `Google用户名` 是「邮箱 + pro/pro 1」⇒ **正则抽邮箱 + 小写**再匹配。
  更新状态只改这两列，凭据列一格都别碰。
- 表里那 **27 号「不在池中」已收口（10-30 16:43）**：27 = **4 进池**（konieczkabookman/biffersarbacher/goeringcheney/kurpinskigibbons617）
  + 7 TOS_PENDING + 13 图形验证码（密码页之前）+ 3 TOTP 被拒。池子 76→80，可用 40→44，飞书表 27 行已标。
  🔴 **新登录的号 7/11 是 TOS_PENDING** ⇒ 加号判据必须看 `account_status`，authed=True 不够；
  🔴 **别用 `build_config.py` 做选择性加号**：它 glob 全部 `accounts/*.cookies.json`，会把没过判据的号一起带进去。
  选择性加号用 `.work/1031-gweb-add/add_selected.py`（只追加指定号 + 出口，复用 `gemini_web_pool._write_and_restart`）。
  探针 `.work/1031-gweb-add/probe_new.py`（pod 内 init + 指定出口 + gemini-pro nonce + 打 status）。
- 交付物：给人看的表 `.work/1031-gweb-dead/table.md`；标表的写前/回读快照 `.work/1031-gweb-dead/lark-mark/`（含凭据原文，别外传）。

### 🔴 10-29：这批号的密码被卡商改过，谁都登不回来（**已证伪，见上节**）

188 实跑 `google_login.py` 6 个号：**在池的 5 个（agorolloyd / caplerial / dorothykane172 /
bechtwelcome / tracymarin257）全部** 停在密码页红字 `Your password was changed 8 days ago`
（≈10-21 被改，仓库里没有任何我们改 Google 密码的脚本或记录）；备用号 abir9dd / sugaksawyer 是
老问题 reCAPTCHA。`/root/gemini-web-build/accounts/*.cookies.json` 最新 mtime = **10-15 11:40**，
即 10-15 之后没有任何号成功登录过，和"密码 10-21 被改"一致。

⇒ **76 个号现在全靠 10-15 的 cookie 吊着**（纯聊天正常），cookie 一死就没有回路 = 整池单点。
修法只有一条：**向卡商要新密码（和最新 TOTP），别拿旧密码反复试**（会把号打进风控，
见 [[feedback_google_login_human_drives_never_auto_retry]]）。

🔴 **别"修" `google_login.py` 里的 `password was changed` 正则**。我 10-29 一度以为它把提示文案
误判成失败，删掉后重跑 ⇒ 变成 150s 卡死 + `STOP unexpected challenge`。截图
（`.work/1029-batch/e-media/agorolloyd.03-unexpected-challenge.png`）证明 Google 在这个流程里就是用
**红字 + 清空密码框**的 "Your password was changed N days ago" 表达"你这个密码是旧的" —— 原正则是对的。

### 🔴 10-29：网页版出图已被 Google 关掉（**已证伪**：量的是 10-15 的死 cookie，见上面「10-29 晚更正」）

阴性对照：3 个号 / 3 份 cookie / 2 个网关（gemini-media + gemini-web）/ 3 个出口 IP
（JP 大阪 172.235.204.67、kiro-px-3、阿里云 47.84.112.136）要图 ⇒ **同一句拒绝**
"Are you signed in? I can search for images, but can't seem to create any for you right now.
It's also possible that image creation isn't available in your location yet."
阳性对照 = 同一批号**纯聊天全部正常**。`/v1/images/generations` 这条路上同样的拒绝会变成
500 `provider returned no generated images`（网关按 `GEMINI_MAX_RETRIES` 重试 ⇒ 日志里连刷十几行）。
~~⇒ 查出图别再怀疑 cookie/代码~~ —— **这个推论是错的**：那 3 份 cookie 全是 10-15 的死 session。
10-29 晚拿新登录的 cookie、**同一个 JP 172.235.204.67 出口、同一镜像**，出图直接成（真 JPEG）。
正确的结论是：**先确认 session 活（`/openai/v1/models` 非空 + 日志 `client initialized`），再换号对照**；
"地区"这一项连测的必要都没有。

🔴 **`web-gemini-{image,music,video}` 在 SpendLogs 里的"成功"大多是假的**：那三个组的行可能是
**chat 线型探活**（正文 `pong` / `Hi! How can I help you today?`），不是真生成。判真假只看 response
正文里有没有图/音频/视频（b64 或 `/openai/v1/media/<32hex>` 链接），**别只看 status 和 200**。
10-28~10-29 全部 11 行逐条看完：没有一条是真媒体。

## LiteLLM 接法：四个必踩的坑

1. **前缀用 `custom_openai/`，不能用 `openai/`**。`openai/` 会让 LiteLLM 把 `/v1/messages`（Claude Code）桥到 `/v1/responses`，而 gemini-web 的 responses 端点不认扁平 tool 形状 ⇒ 带工具必 **422** ⇒ Claude Code「API error · Retrying」。
2. **窗口字段只放 `model_info`**。`max_input_tokens` 写进 `litellm_params` 会被 custom_openai 原样透给 SDK ⇒ `unexpected keyword argument` ⇒ **500**。
3. **不传 `model_info.mode`**（聊天条目）。见 [[feedback_model_new_mode_poisons_shared_backend_key]]。
4. **视频**：`GET /v1/videos/{id}` 从 id 反解 `model_id` 再按**私有组名**验权（请求里没有模型名，key aliases 管不到）⇒
   - ✅ **10-30 起不用再把私有名放进允许列表**：callback `video_id_alias_auth`（commit 3af374e，已上生产 4 副本）
     把从 video_id 反解出的组名换成这把 key 里指向它的对外名再验权。只放 `gemini-video` + 别名
     `gemini-video→web-gemini-video` 即可：提交 / 查进度 / 下载全通（实测 80s completed、下载 2.9MB），
     没有视频权限的 key 查同一个视频仍 403。kill switch `VIDEO_ID_ALIAS_AUTH_DISABLED=1`，
     回滚 `litellm-198-lane-callback-add.py rollback litellm-proxy /root/cb-add-backup/litellm-proxy.20261030T135108Z.json`。
   - （补丁之前的老规矩：key 的 `models` 里必须**同时**有公开名 `gemini-video` 和私有名 `web-gemini-video`。存量 key 照旧能用，不用改。）
   - 各媒体条目的上游 `model` **不能同名**（image=`gemini-3.8-flash`、video=`gemini-advanced`、music=`gemini-3.8-flash-music`），否则反解到别的组 ⇒ 403。
   Deep Research 走 pass-through，key 要 `metadata.allowed_passthrough_routes:["/gemini-deepresearch"]`（per-key 白名单管不到 pass-through）。

建条目照抄现网：`/model/info` 里 `gemini-web/*`、`gemini-media/*` 两组 `model_info.id`。开给某把 key 用 skill `litellm-198-key-allowlist`（`--add-model X --alias X=web-...`，自带 backup + 回读）。
⚠️ std key 上原有 `gemini-3.8-flash`（走 Antigravity），网页版另起名 `gemini-3.8-flash-web`，别覆盖。

## gemini-media 扩号 · ✅ 10-31 已上线 39 号（video / music 各 40 腿）

**现状**：`web-gemini-video` / `web-gemini-music` 各 **40 腿**（原 goeringcheney + 39 个新成员），
`web-gemini-image` **仍 1 腿**（故意，见下）。成员 pod 全在 **242**（`dedicated=new-node` toleration），
198 自己 94/110 个 pod 放不下 40 个成员。

```bash
# 198 上 root。登录在本机 Mac 发起（198 连不上 188）：.work/1031-gmedia-pool/login.sh name...
python3 gemini-media-pool.py add --name X --cookies-from /root/gmp-1031/cookies/X.cookies.json --exit-ip 47.84.85.100
python3 gemini-media-pool.py verify --name X                 # init + /v1/models + 按 cap 查所需模型在不在
python3 gemini-media-pool.py wire   --name X --caps video,music
python3 gemini-media-pool.py remove --name X                 # 回滚：删腿 + 删 deploy/svc（Secret 留着）
```

- 🔴 **image 不挂多号（本轮选的办法 a）**：chat 线型的图副本在**那一个 pod 的内存**里，nginx 把
  `/gemini-media/openai/v1/media/<32hex>` 写死到原 svc 的 ClusterIP ⇒ 成员出的图经公网必然 404。
  `--caps` 默认就是 `video,music`，想挂 image 得先让那段按 id 找到对应 pod。
- 🔴 **内网出口（mihomo 10.43.x）的成员必须带 `wait-egress` initContainer**（工具已自动加）：
  网关在**启动后 3ms** 建一次 Gemini client 且**永不重试**（`provider_module.go` 一次性后台 init），
  而新 pod 的 IP 此刻还没进 egress netpol 的放行集合 ⇒ 那唯一一次 `proxyconnect … connection refused`，
  之后 pod 一直是死会话。10-31 实测 agorolloyd / carlybussey897 **连滚两次都复活不了**（每次重启都重新
  踩这个竞争），加了 initContainer（`nc -z` 等出口通）后一次就起来。判据不是「TCP 现在通不通」——
  事后手测永远是 OPEN，要看**那一行 init 日志**。
- ✅ **视频轮询会落回提交它的那个 pod**（10-31 实测，这是多腿视频能用的前提）：job 存在
  **pod 进程内存**（`gemini_video.go videoJobStore`），而 video id 里只编了上游模型名
  （`model_id:gemini-advanced`）不是 deployment id。实测：两个 id（一个在原 pod 提交、一个在成员
  pod 提交）用**同一把 key 交替**各打 8 次，16/16 各自回自己那个 pod（`x-litellm-model-id` 两个值
  不同、状态分别是 completed / failed），40 腿下仍然成立。
- ✅ **3 路并发视频**（10-31，经 `web-gemini-video` 公开名）：落在 3 个不同成员上，90s / 184s / 293s
  全部出片（5.8MB / 6.0MB / 12.7MB）。扩号前第二发必 429 `a video is already being generated`。
- 🔴 **「出不出视频」是按号的，和出口 IP 无关**：43 个候选号里 **5 个**反复回
  `quota_exceeded: Gemini is temporarily refusing video generation (capacity)` 或 10 分钟超时，
  其余 38 个（跨全部 6 个出口）90~120s 正常出片。阴性对照做过：同一个号**摘掉 proxy** 照样失败；
  同一出口上别的号照样成功 ⇒ 不是出口被地区拦。失败的号**不要 wire**，`remove` 掉。
- ⚠️ **`gemini-3.8-flash` 聊天在 gemini-media 上会挂死 180s+**（成员和原号**都一样**，10-31 实测），
  `gemini-3.1-pro` / `gemini-4-argon` / `flash-lite` 都 7~37s 正常 ⇒ 探活别用 3.8-flash。
- ⚠️ **音乐 10-30 夜到 10-31 凌晨上游自己不出**：成员 7~300s 回 400
  `Gemini Web reported an error while creating music`，**原号同形**（阴性对照）⇒ 不是扩号引入的。
  音乐的腿照样挂上了（40 腿），上游恢复就能用。
  🔴 **其中 300s 整那一类不是「上游不出」，是网关自己的 5 分钟上限掐的**（两处写死，见「音乐 / 歌词 /
  视频的真实边界」节）。分开看：`<300s` 的 400 是上游真回了错；**恰好 300.0~301.0s** 的 502/504 是我们掐的。
- 🔴 **判断「号还活着」别用 busybox `wget` 测出口**：镜像里 `/usr/bin/wget` 是 busybox，**忽略
  `https_proxy`**，读数永远是 pod 默认出口（10-31 一度误判成「proxy 没生效」）。要量出口用
  python `urllib` + `ProxyHandler`，或直接看 `/proc/net/tcp` 的 ESTABLISHED 对端。
- ✅ **两份 cookie 互不影响**（本轮 44 号全程复核）：给 gemini-media 重新登录一次，这些号在
  gemini-web 里 **44/44 仍然 authed+AVAILABLE**，包括 goeringcheney。
- `verify` 从 **pod 自己的 loopback** 问 `/v1/models`：netpol `gemini-media-ingress` 只放
  `litellm-product`，从 198 宿主机 curl 必然超时（那是策略在生效，不是号死了）。
- LiteLLM 腿的 `api_key` 照抄现网那三行的**字面值**（网关本身无鉴权，但 openai/ 部署必须有个 key）；
  `os.environ/GEMINI_MEDIA_API_KEY` 在 litellm pod 里**不存在**这个环境变量。
- 本轮产物：`.work/1031-gmedia-pool/`（login.sh / add_many.sh / video_survey.py / concur3.py /
  sticky_test.py / audit.py，以及逐批 OK/STOP 原始记录）。198 上 `/root/gmp-1031/`（含 cookie，0600）。

### 原 10-29 设计（下面这节的结论仍然成立）

上游**结构上就是单号**：一份 `GEMINI_COOKIES` 在启动时建**一个**全局 provider
（`provider_module.go`），项目里没有号池概念。⇒ 池子只能做在网关**外面**：

**一号一个 Deployment + 一号一份 cookie Secret + 一号一个出口 + LiteLLM 同名多行 round-robin。**
工具 `scripts/gemini-web/gemini-media-pool.py`（在 198 上 sudo 跑）：
`add --name <acct> --cookies-from <…>.cookies.json [--proxy …]` / `verify` / `wire` / `status` / `remove`。
成员叫 `gemini-media-<name>`（原来那个单号 `gemini-media` 不动），LiteLLM 行 id =
`gemini-media-<name>/{image,music,video}`，`model_name` 复用现有三个组名 ⇒ **不用给 key 加新模型名**。

- ✅ **每号绑出口 IP 不需要改一行代码**（10-29 实测）：上游**没有任何 `http.Transport` 覆写**
  （非测试文件 grep `Transport` 为空）⇒ 全走 `http.DefaultTransport` ⇒ 认 `HTTPS_PROXY`。
  阴性对照：同镜像 pod 设 `HTTPS_PROXY=http://127.0.0.1:1`，日志回
  `proxyconnect tcp: dial tcp 127.0.0.1:1: connect: connection refused`。
  🔴 用了 `--proxy` 就必须在 netpol `kiro-rs/cliproxy-muse-to-kiro-egress`（7890）里放行，否则**全部 fail closed**。
  ⚠️ **放行的是整个池子一条选择器，不是一号一条**（10-31 实测现网就是这样，工具也是这么写的）：
  `pool in (gemini-media)` —— 成员的 deploy/svc/pod 模板都带 `pool: gemini-media` 标签（原号也带），
  所以第一个成员之后加号**不用再动 netpol**。核法：`kubectl -n kiro-rs get netpol
  cliproxy-muse-to-kiro-egress -o json` 看 peer，再 `kubectl -n litellm-dev get pods -l pool=gemini-media`
  数一下（10-31：40 个 pod = 40 个 deploy，一个没漏标）。按名加 40 条是早期设计，已被现网推翻。
- 🔴 **cookie 缓存目录必须每 pod 一个 emptyDir，不能共用 PVC `gemini-media-data`**：
  两个网关共享缓存会互相轮换 `__Secure-1PSIDTS` 把对方踢下线（同 skill 顶部那条"两个网关必须用
  同一个号的两份独立登录"的同类坑）。
- 网关**无鉴权** ⇒ netpol `gemini-media-ingress` 要从 `matchLabels:app=gemini-media` 改成
  `pool in (gemini-media)` 并给原 deployment 补 `pool: gemini-media` 标签，否则新成员是裸的。
  ✅ **已做**（10-31 现网实测：`gemini-media-ingress` 的 podSelector 就是
  `matchExpressions: pool In [gemini-media]`，原号和 39 个成员全带标签）。

**上游新版有两个正好治我们痛点的东西（值得升级，但补丁要重做）**：
- `0b779167 fix(gemini): keep rotated-cookie cache across restarts (#97)` ——
  我们钉的 `681ca80` 在它之前，带着 #97 的 bug：`Init()` 只要 config 的 PSIDTS 和缓存不同就
  `ClearCookieCache()` 并用**较旧的 config 值**，轮换出来的新 cookie 每次重启都被丢掉 = 重启即死。
- `f4d0ccf0 feat: authenticated cookie-update endpoint` —— `GEMINI_COOKIE_API_KEY` +
  `POST /admin/cookies` **不重启换 cookie** ⇒ 消灭"滚一次 pod 聊天里的图全 404"这个代价。

补丁适配实测（`git apply --check`）：`681ca80`=两个都 APPLIES；`0b779167`=越界守卫 APPLIES、
**p2-chat-media FAILS**（`openai_service.go:82`）；最新 main `10368dfc`=**两个都 FAILS**。
⇒ 升级得先重写 p2。

## 音乐 / 歌词 / 视频的真实边界（10-29 查上游 docs，未烧额度）

- **音乐**：`POST /openai/v1/audio/speech` 驱动网页版 **Create music** 工具。
  `voice` 只认 `instrumental` / `vocals`（其它值交给 Gemini 决定），扩展字段 `length`(short/standard)、
  `genre`(pop/hip-hop/rock/k-pop/latin/electronic/r&b/country/afrobeats/reggae/jazz/classical/folk/
  lo-fi/acoustic/cinematic/ambient)。回的是 Gemini 原始格式音频，`response_format` 不能转码；
  客户端必须 `max_retries=0`（重试会再生成一首）。错误码：422 `refused`（Gemini 只回了文字没开工具）、
  429 `quota_exceeded`、502 `upstream_error`、504 `timeout`（5 分钟没出，可能稍后出现在网页里）。
- 🔴 **「歌出不来」的掐断点在网关，5 分钟上限写死在两处，必须一起改**（10-30 夜查清，三段式见
  `.work/1031-music/result.md`；本地 Go 计时实验 `.work/1031-music/goexp/`）：
  ① `internal/modules/providers/gemini_music.go:24` `musicJobTimeout = 5*time.Minute`（`:301` 的
  `context.WithTimeout` 管**整个任务**：读完 generate 流 + `waitForAudio` + 下载）；
  ② `internal/modules/providers/gemini_service.go:612` `plainClient := &http.Client{Timeout: 5*time.Minute}`。
  **只改 ①，到 5 分钟会被 ② 再掐一次**。判据 = 报错原文：先触发 ctx 时是裸的
  `context deadline exceeded`，先触发 http.Client 时会多一个后缀
  `(Client.Timeout or context cancellation while reading body)` —— 线上原文**没有**这个后缀 ⇒ 现在是 ① 先到。
  （原生口 `internal/modules/gemini/gemini_controller.go:146` 也是 5 分钟 ctx，LiteLLM 不走这条。）
  🔴 **「放宽上限就能出歌」没有被证实**：10-30 21:09 起同一个号 6 次真实生成 0 成功，Google 是不是
  干脆不出了**没查到证据**；同日白天 8/8 是成的。LiteLLM 侧那条腿 `timeout=600` 已经够，**不用动**。
  成功的歌历史耗时 95~279s，全部 <300s ⇒ 这个上限确实卡在真实分布的尾巴上。**补丁未做**。
- 🔴 **「音乐超时」和「音乐失败」在 SpendLogs 里不同形**：504 走
  `Music was not ready before the deadline`（Gemini 只回了文字编曲方案、没开作曲工具，网关空轮询到截止，
  `classifyMusicReply` 认不出这种回复）；502 走 `failed to read generate response: context deadline exceeded`。
  前者用户看到的是人话（白名单里有），后者只有 `API 异常 (req: …)`。
- 🔴 **"歌词"不是独立能力，别当新功能卖**：上游没有任何歌词端点。带唱的歌 = `voice=vocals`
  （歌词由 Gemini 按 prompt 自己写并唱进音轨，不单独返回文本）；要**纯文字歌词** = 普通聊天模型写字，
  和 `web-gemini-4-argon` 之类聊天组没有区别，**不需要新组名**。
- 🔴 **上游自己没验证过音乐**：docs/music.md 原文 `Status: … The audio card format in the reply
  could not be observed yet (Gemini Web did not return a track for the test account)` ——
  识别逻辑是"任何带 `audio/*` MIME 的 Google 媒体 URL"的猜测。
  ❌ **10-29 晚我们也没出成**：活 session（同号同时出图/出视频都成）上 `voice=instrumental, length=short`
  跑满 **300s** 回 502 `upstream_error … failed to read generate response: context deadline exceeded`，
  pod 日志 `GenerateContent failed after all attempts attempts=1`。
  ✅ **10-30 已出成（上面那条作废）**：经 LiteLLM `/v1/audio/speech` 08:42~09:00 连出 4 首 mp3（1.4~1.5MB，188~267s）；
  直打原生口 `POST :4981/gemini/v1beta/models/gemini-pro-music:generateContent`（带人声 prompt）73s 回 3.7MB / 157s mp3。
- 🔴 **歌词 = 唱进音轨，拿不到文字**（10-30 实测，判据=回包逐字段看 + mp3 拆 ID3）：
  原生口 parts 里的 text 只有 `I've generated your upbeat pop track…` + 一个 `googleusercontent.com/generated_music_content/0` 占位；
  mp3 的 ID3 只有一个 `GEOB` = C2PA 来源签名，**没有 USLT/SYLT 歌词帧**；LiteLLM 那条（OpenAI 形状）只回二进制，连那句话都没有。
  ⇒ 要歌词文字只能把歌丢给 `gemini-transcribe` 转（7s 出全文，会听错个别词，只能当草稿）。
  脚本 + 样本：`.work/1031-channel-models/lyrics_probe.py`、`song_vocals.mp3`。「给定歌词让它照唱」未测。
- **Lyria 3 在 Gemini 网页版可用**这件事 **10-29 没查到官方出处**（188 抓
  `gemini.google.com/updates` 是 JS 壳取不到正文）。别当既成事实。
- **视频**：`POST /openai/v1/videos` 异步任务；只支持 `1280x720`/`720x1280`（16:9 / 9:16），
  `seconds` **被接受但忽略**，**同时只允许一个任务**（并发第二个 429），
  超时 `GEMINI_VIDEO_TIMEOUT_MINUTES` 默认 10 分钟（最大 60）。`quota_exceeded` = 日额度用完。
  ✅ **10-29 晚第一条真实成功**（goeringcheney）：提交 → 96s `status=completed` → `/content` 下
  2.9MB MP4，ffprobe `h264 1280x720 24fps + aac, duration 10.005s`。`seconds` 确认被忽略
  （提交回显 8、完成回显 10）；回包里 `I'm generating your video…` 是 Gemini 原话透出来的，
  **判完成只看 `status=completed`**。"很小的日额度"仍未证实（只跑了 1 次）。

## 本地补丁（`scripts/gemini-web/patches/`，`apply.sh fastapi|media` 从上游重建）

| 补丁 | 解决什么 |
|---|---|
| p1 namedmodel | 上游自定义模型走 `Model.from_dict`，gemini_webapi 2.1 已删 ⇒ 全 500。改成传名字串，由库按账号实时模型表解析（Argon 内部 id `95a4c0abcba24364`） |
| p2 pool_failover | 跳过 quota remaining=0 的号 + 冷却表；新会话遇限额/封禁/超时换号重试；流式先拉首块再交出 |
| p3 tool_nudge | 带 tools 的 chat 先非流式拿全文；无 tool_calls 且像推脱（「无法访问」「你可以运行」「结构如下」…）⇒ 追加强制提示重发 |
| p3.1 as_dict | 🔴 内部返回的是 pydantic 对象不是 dict ⇒ p3 原版**从不触发、带工具的流式回成整块 JSON ⇒ LiteLLM 读空 ⇒ Claude Code 15/15 空回复** |
| p3.2 | 正则补 `you can share` / `To view…you` / `I don't have access`，最多重试 2 次 |
| p4 status_endpoint | `GET /carher/status`：按号 running / account_status / 出口 / 进程内成败 / usage_info |
| p5 auth_and_quota | status 里加 `authed`（= 有没有 SNlM0e，**唯一能分开 guest 和过期号**的判据）/ `cookie_source` / `models` / `pro`；新增 `POST /carher/quota` 现打 GetUsageInfo+CheckQuota（只读、不花额度，没登上的号显式回 `need_login` 而不是空额度） |
| p6 skip_guest | 选号跳过没真登录（无 access_token）的号；全池都没登上时退回原行为 |
| p7 health + skip TOS（10-30） | ① `/health` 不再 `await pool.init()`。🔴 上游每次健康检查都**串行** init 每个停掉的号（每个最长 120s），readinessProbe 只给 5s ⇒ 一个坏号把整个 pod 摘出流量（10-30 17:29~17:52 生产 4 组全不通）。停掉的号仍由 `acquire()→_ensure_client_ready()` 按需重启。`ok` 改成「至少一个号在跑」。② 选号跳过 account_status 1040/1042（TOS）的号——它们有 access_token，p6 放行 ⇒ 一派就 1040（e2e 42 次里 6 次）。全池都是 TOS 时不跳过。上线后 /health 2ms、24 发真请求落 TOS 0 次。行为测试 **两个文件**：`.work/1031-p0/test_p7.py`（打了补丁的 `sim/`，该绿）+ `.work/1031-p0/test_neg.py`（未打补丁的 `sim_neg/`，**该红**）—— 阴性对照是独立那一份，别只跑前一个 |

`apply.sh fastapi` 在干净 checkout 上重放 p1→p7，与 p6 线上镜像相比只差 p7 那两段（pool.py 19 行 / health.py 14 行，其余 .py 逐字节一致，10-30 已验）。
🔴 **198 `/root/gemini-web-build/src` 不是线上代码**（10-30 发现它停在 10-29 20:47、没有 p6）——p6 镜像是在别处构建的。改补丁时以 `kubectl exec … cat /app/app/<file>` 读到的线上文件为准，重建一律走 `apply.sh` 从上游 clone。

## 能力与边界（10-14 实测）

- **工具调用天花板 ≈ 8 成**：Claude Code 线型 5×3 回归两轮都是 12/15（nonce 5/5、主动调工具 4/5、tool_result 续答 3/5），重试救回约 2/3。剩余失败是模型自称「作为 Claude Code…虚拟沙盒」直接编目录 —— 和正常总结同形，正则不能再放宽。
  已证伪的假设（别再测）：末轮追加提醒、系统提示含「You are Claude Code」、流式 vs 非流式、system 字符串 vs 列表 —— 都不是变量。根因判为网页消费者人设与提示词模拟工具协议冲突。
  ⇒ 适合聊天/轻工具；Claude Code 重度 agent 不稳。真稳 = Ultra 号进 Antigravity 或 AI Studio key。
- **带工具时不是流式**：p3 为了判断推脱要先攒全文，整段一次出。不带工具仍真流式。待做：只攒开头 ~200 字再转实时流。
- **延迟**：短问 2~3s、80~150KB Claude Code 体量 4~7s、冷启动 12s；经 LiteLLM ≈ 直打网关。
- **额度**：新号 **2400 credits / ~5h 窗**，Flash/Pro/Argon 共池；一轮 5×3 Claude Code 回归≈几百 credits。10-14 夜 jio/agoro 两个老号的日志读数变成 `/48384`（原因未查：库换口径 or 档位不同）。视频另有很小的日额度（`quota_exceeded`）。p2 会跳过 remaining=0 的号，52 号池子下单号耗尽基本无感。
  **怎么读（10-29 查清）**：库里额度来自两个只读 RPC —— `GET_USAGE_INFO`（`jSf9Qc`，新口径 compute-usage：
  每个窗口的 `remaining_credits` / `usage_level` / `usage_percentage` / `reset_time`，外加 `ai_credits_remaining`）
  和 `CHECK_QUOTA`（`aPya6c`，旧的请求数口径）。**两个都被 `_check_account_status()` 挡着**
  ⇒ 🔴 **没登上的号额度恒读空 `{}`**，别把空额度当「额度充足」（p5 的 `need_login` 就是为了把这两件事分开）。
  生成完一发之后库会自动 `_fetch_usage_info()` 刷一次（`client.py:2067`）⇒ 有真流量的号读数自然是新的；
  管理台的「批量查额度」走 p5 `POST /carher/quota`，**只打这两个 RPC，不发生成请求、不花额度**。
- 媒体：图 ~15s；音乐 1.5~4 分钟 MP3（Google 偶回「I'm having a hard time」，换描述即可）；Deep Research 1~4 分钟万字报告。

## 回归（每次改补丁/换号后）

```bash
# 198 上：Claude Code 线型 5 轮（流式+工具 / 主动调 Bash / tool_result 续答）
MK=... python3 scripts/gemini-web/probes/cc_regress.py 5
# 新名字行为层（克隆 std 形状 + 阴性对照）
LITELLM_MASTER_KEY=... python3 scripts/litellm-198-key-behavior-regress.py --clone-from cursor-liuguoxian-std \
  --new gemini-3.1-pro --old gemini-4-argon --outside web-gemini-3.1-pro --protocol openai
# 媒体
MK=... python3 scripts/gemini-web/probes/media_e2e.py image music video research
```

🔴 `cc_regress.py` 的「tool_result follow-up」只看有没有内容，**模型拒绝也会算 OK** —— 看 RED 行之外还要扫一眼文本里有没有「无法」「inject」。
🔴 nonce 在 SSE 里会被拆进多个 chunk，`grep -c NONCE` 流式读数=0 不代表失败，要拼 delta 再比。
🔴 回归极耗额度：先看 `kubectl -n litellm-dev logs deploy/gemini-web | grep "credits remaining"`，0 时所有读数无意义。

## 回滚

- key：`litellm-198-key-allowlist.py --restore 198:~/cursor-std-pre-gemini*-<ts>.json --apply`
- 条目：`/model/delete {"id":"gemini-web/<name>"}` / `gemini-media/{image,music,video}`；pass-through `DELETE /config/pass_through_endpoint?endpoint_id=...`
- **gemini-media 池成员**（10-31 的 39 号）：单号回滚 `python3 scripts/gemini-web/gemini-media-pool.py remove --name <name>`
  （删 LiteLLM 腿 + deploy + svc，Secret 保留）；整池回到扩号前 =
  `for n in $(sudo k3s kubectl -n litellm-dev get deploy -l pool=gemini-media -o name | sed 's#.*/gemini-media-##' | grep -v '^gemini-media$'); do sudo python3 /root/gmp-1031/gemini-media-pool.py remove --name $n; done`
  （原 `gemini-media` 不带 `-<name>`，不会被它删到）。腿数复核 `python3 /root/gmp-1031/rows_ctl.py census`
- 网关镜像：**现役 `127.0.0.1:5000/gemini-fastapi:20261030-1731445-p7`**（10-31 02:4x 实测 `kubectl -n litellm-dev get deploy gemini-web -o jsonpath='{..image}'`）。
  回滚一级 `…:20261030-1731445-p6`（/health 会重新逐个 init 停掉的号 ⇒ 会把 17:29 那次整服务 NotReady 的事故放回来，且 TOS 号重新进轮询）；
  再往前 `…:20261029-1731445-p5` / `-p4`；p3.2 `…:20261014-1731445-p32`；`…:20261014-1731445-p2`（p2 无工具重试，最后一个无 p3 的版本）
- gemini-media 镜像：现役 `127.0.0.1:5000/gemini-web-to-api:20261016-681ca80-p2`（原号和 39 个成员**同一个 tag**）
- 删整套：`kubectl -n litellm-dev delete deploy/gemini-web deploy/gemini-media svc/gemini-web svc/gemini-media networkpolicy/gemini-media-ingress`

## 相关

skill `cliproxy-antigravity-ops`（同号进 Antigravity）· `litellm-198-key-allowlist` · `litellm-passthrough-endpoint` · `muse2api-pool-ops`（同类网页反代）·
记忆 [[project_198_gemini_web_argon_live_2026_10_14]] · [[project_gemini4_argon_no_upstream_carries_it_2026_10_01]]