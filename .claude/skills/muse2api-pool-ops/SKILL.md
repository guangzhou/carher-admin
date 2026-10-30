---
name: muse2api-pool-ops
description: >-
  muse.ai（Meta 个人助手 Muse）成品号 → 198 k3s 上的 muse2api 账号池 → 198 LiteLLM
  （`muse-spark` 聊天 / `muse-image` 生图）的全套运维。Use when 用户发来 Muse 成品号卡密
  要「加号/加个账户」、问「这个号能用什么模型/10 亿 token/能不能接 Cursor」、
  要把 muse 给某把 key、muse 报 401/「云端 VM 连接超时 (Still sending)」、
  cookie 过期要重导、升级/重建 muse2api、看池子状态。含：为什么只能走网页反代
  （dev.meta.ai 登录必报 Token exchange failed、聊天是 Noise 加密 WebSocket）、
  `add_acct.py` 一条命令加号（两种卡密格式）、Meta reCAPTCHA checkpoint 必须人过、
  集群 DNS 污染要 coredns-custom 两个 zone、k8s Service 名撞 `MUSE2API_PORT`、
  LiteLLM 注册与只给指定 key、全放行 key 会顺带拿到、能力边界（无工具调用）。
---

# muse2api 号池运维（198）

## 一句话拓扑

```
同事 key → 198 LiteLLM litellm-product（组 muse-spark / muse-image，DB 注册，价格 0）
  → http://muse2api.muse2api.svc.cluster.local:18610/v1   （ns muse2api，钉 242 节点）
  → 无头 Chromium，每号一个隔离 BrowserContext，在 muse.ai 网页里打字/读回复
  → muse.ai（每号自己的周额度 + 一次性 10 亿额外额度）
```

| 东西 | 值 |
|---|---|
| 管理页 / 直连 API | `http://10.68.13.198:30610/admin`、`/v1`（NodePort `muse2api-admin`，内网可达，靠 Bearer key 挡） |
| 池 key（m2a_，管理员级） | Secret `muse2api/muse2api-env` 的 `MUSE2API_KEY`。能删号/轮换 key，别外发；给人用发 LiteLLM key |
| 账号/媒体持久化 | PVC `muse2api-data`（local-path，242） |
| 镜像 | 线上 **`127.0.0.1:5000/muse2api:5b8bba9561fd-h10`**（10-30 晚实测现量；上游 v1.6.4 锁 commit + `harden.py` #1~#8 + `harden_av.py` #9~**#11**）；回滚 `-h9` = 视频会全挤一个号，`-h6` = 没有音视频/没有 OpenAI 视频形状，`-h5` = 一号一任务。**两个 harden 文件都由 `deploy-198.sh` 顺序执行**（先 harden.py 再 harden_av.py，再 `py_compile`），任一锚点对不上就构建失败。🔴 **`deploy-198.sh` 的 `MUSE2API_HARDEN_REV` 默认值必须跟着线上走**（现为 `h10`）：老 tag 还在 registry 里，那段「已有 tag 就跳过构建」会直接 `set image` 回去 = 静默回滚线上。查现量：`kubectl -n muse2api get deploy muse2api -o jsonpath='{.spec.template.spec.containers[0].image}'` |
| 每号一个 IP（10-30 已上线） | ✅ 12 个活号 = 12 个**不同**出口 IP，`exit_via` 全是「对照表」。Secret `muse2api-egress-map` + 持久台账 `198:/opt/chatgpt-acct-console/data/muse_egress_assign.json`。看一眼：8796 管理台 muse 页「一号一 IP」KPI，或 `sudo python3 /opt/muse-egress-tools/verify_serving.py`。细节见下面「每号一个出口 IP」一节 |
| 并发（10-30） | harden #8：`MUSE2API_PER_ACCOUNT_SLOTS=5`（一个号同时 5 个任务，各自独立 BrowserContext）、`MUSE2API_CONCURRENCY=活号数×5`、`MUSE2API_LOGIN_WAIT=150`。10-30 实测：12 个活号、并发 60，**同时交 10 个视频全部在第 10 秒开跑**（串行时第 10 个要等 15 分钟）。加号/废号后用 `pool_status.py` 核这三个数，验收 `probes/video_concurrency.py N`。🔴 **h10（10-30 晚）视频分号**：上游优先「最近做完视频的号」，配上 5 槽 ⇒ 10-30 e2e 5/5 视频全落 muse-gen-12（并发时单条 71s→121s、周额度只扣一个号）。`harden_av.py` #11：那个号手上有任务就换号；`MUSE2API_VIDEO_STICKY=1` 恢复原行为。上线复测 3 路并发落 3 个号、3/3 成功（`.work/1031-p2/vid3.py`，任务归属读 `/app/data/tasks.json` 的 `account`，不是 `/data/`）。🔴 LiteLLM 侧 `muse-video`/`muse-image` 已设 0 重试（重试 = 再生成一份、多扣额度）。🔴 成功率另算：那轮 4/10 成功，6 次失败是 muse 自己「回文字不给视频」（新号+啰嗦提示词易触发）+ 1 次消息没送达，不是并发问题。视频标签页偶尔收不到推送 ⇒ 200s 起自动刷新线程最多 4 次，日志 `reloading thread` |
| 仓库代码 | `muse2api/`：`deploy-198.sh` `k8s.yaml` `harden.py` `harden_av.py` `proxy_bridge.py`<br>**加号（10-30 全自动）**：`add_batch.sh`（总调度）→ `login_http.py`（纯 HTTP 登录，集群内跑）+ `genemail_code.py`（188 上取码）<br>其它：`add_acct.py`（旧浏览器加号，仅撞 checkpoint 时兜底）`import_cookies.py` `outlook_code.py` `pool_status.py` `litellm_register.py` `litellm_e2e.sh` `probes/video_concurrency.py` |
| 外网 | 本机是大陆机器出不去。浏览器走 188 SOCKS：`ssh -fN -D 18880 cltx@10.68.13.188`，188/198 出口都是日本 `172.235.204.67` |

## 为什么只能这么做（别再重查）

- **没有官方 key 可拿。** muse.ai 设置 9 个分页全翻过，无 API key/开发者/MCP 入口。
  官方 Model API（`api.meta.ai/v1`，`LLM|` key）只能在 dev.meta.ai 建；成品号登 dev.meta.ai
  在 `auth.meta.com` 验证码通过后，回调 `dev.meta.ai/api/auth/callback` 必报
  `?error=Token+exchange+failed`（设备码流程和普通 Login 都一样，原因未定，日本不在禁区名单）。
  即便将来能建，也是按量付费，**花不到 muse.ai 那 10 亿**。
- **CLIProxyAPI 的 `meta` 通道不行**：只认 Muse Code 设备码换出的 `LLM|` key，打 `/v1/responses`，
  无 cookie 路径；同样卡在 dev.meta.ai。sub2api 不支持 Muse。
- **纯 HTTP 直调不现实**：发消息无业务 HTTP，走 `wss://hatch.metaaivm.com/v1/noise`，
  帧在 TLS 外再加 `Noise_XX_25519_AESGCM_SHA256`（前端 bundle 里写死）。
  所以 muse2api 用浏览器在网页里打字——这是它慢、易随改版坏的根因，也是唯一现成路。

## 能力边界（用户问「能不能用在 X」先看这里）

| 问题 | 结论（均实测） |
|---|---|
| 有哪些模型 | 只有 Muse Spark（聊天）、Muse Image（生图/改图）、Muse Video（≈10s，图生视频实测 6s，带音轨）。传 gpt-4o/claude 名一律映射到 spark；免费号无 Wonder，不能切模型 |
| Cursor / Codex / Claude Code | **不行**。给 `read_file` 工具它不回 `tool_calls`，而是用 muse 自己的云电脑去找文件。Claude Code 还要 `/v1/messages`，muse2api 没有。只适合 Chatbox/Cherry/NextChat 类聊天+生图 |
| 速度 | 冷 38s、热 14~16s；生图 30~60s（10-16 实测）；流式是伪流式；h6 起单号同时 5 个任务（之前 1 个）。🔴 **一个号页面加载不出来，每个请求都先在它身上白等 60s 再切号**（10-15 探针 muse-spark 75~83s；10-16 禁用 ausley-01/doyle-03 后 17~26s）⇒ 慢先跑 `pool_status.py --test`，`test=False` 连续两次的号 `PATCH /admin/accounts/<id> {"enabled":false}`（持久化在 PVC，重启不丢） |
| 用量 | muse2api 不回 usage，LiteLLM 里 token 恒 0，只能数次数 |
| 额度 | 免费版**无使用期限**；周额度每周按各号注册时刻重置（各号不同天）；10 亿是一次性奖励「从不过期」、**不按月补**；先扣周额度再动 10 亿（测完一轮周额度 6%、10 亿 0%）；周额度绝对值查不到（只给百分比） |
| 生图链接 | ✅ 10-16 修：① LiteLLM 行 `muse2api-muse-image` 的 `litellm_params.extra_body={"response_format":"b64_json"}` ⇒ `/v1/images/generations` 永远回 b64（LiteLLM 不放行客户端传的 `response_format`，只有 deployment 级 extra_body 进得去）；② env `MUSE2API_PUBLIC_BASE=https://cc.auto-link.com.cn/muse-media` + 198 nginx 段 `carher-muse-media` ⇒ chat/视频里的链接是公网可开的绝对地址；③ harden #4：chat 选 `muse-image` 走生图流水线回 `![image](...)`（上游会把它当聊天，40s 后 502） |
| 视频 | ✅ 10-29 已接 LiteLLM 生产：组 `muse-video`（id `muse2api-muse-video`，`video_generation`，timeout 900）。标准 OpenAI 三段式 `POST :30402/v1/videos` → `GET /v1/videos/{id}` → `GET /v1/videos/{id}/content`。实测 92s、10s/1280x720/h264+aac、画面对得上提示词 |
| 听写（ASR） | ✅ 10-29 上线：组 `muse-asr`（id `muse2api-muse-asr`，`audio_transcription`，timeout 600）⇒ `POST :30402/v1/audio/transcriptions`（标准 multipart）。走 Meta Voyager WebSocket，**不开浏览器、不占生成槽**；9s 中文 14s、英文 10s，中英都准。`language` 收下但忽略（Voyager 自动判语种）。🔴 偶发叠词（「三三点」「大约大约」）是 Voyager 自己的毛病，不是我们的节奏 |
| 看视频 / 听录音 | ✅ 10-29 上线：`muse-spark` 的 `/v1/chat/completions` 收 `input_audio` / `video_url`(data:) / `file`(file_data data:) ⇒ 当聊天附件发给 muse。实测视频 35s 说对画面+球上 `ADVENTURE`，英文录音 48s 原句引回。**只接内联 base64，远端 URL 一律不抓（无 SSRF 面）**；音频非 wav 会先用 ffmpeg 转 16k 单声道 wav；单附件 ≤40MiB、一次 ≤4 个。这条**是一次正常聊天回合，算周额度** |
| 视频里的说话 | ❌ 附件路径**不转写语音**：让它逐字转写热气球视频（音轨 -19dB 有人说话），回「这个视频里没有人说话」⇒ 要文字就把音轨喂 `/v1/audio/transcriptions` |
| TTS | ❌ `/v1/audio/speech` 回 **501** 并说明原因（muse.ai `/api/hatch/voice/tts` 对我们的号 404，语音开关 `572a7c97=false`、vm_type `hatch_vm`）。不要再接，先拿一个开了开关的号对照 |

## 加号：一条命令，全自动（10-30 重写）

```bash
# 卡密文件每行：<邮箱> <取码链接>   （放 .work/ 下，chmod 600，用完删）
bash muse2api/add_batch.sh <卡密文件> [起始序号]
```

它做：读旧码当基准 → **在集群内发起纯 HTTP 登录**（见下）→ muse 发码 → 本机去 188 取新码喂进去
→ 拿 cookie → 导入号池 → 真登录测试 → 按行号轮流分配出口 IP（一号一 IP）。
**全程零人工、零人机验证**；10-30 实测 11 个号 11/11 成功。

两种取码源都支持，按链接域名自动选：

| 链接 | 怎么取 |
|---|---|
| `generator.email/<邮箱>` 一次性邮箱 | `genemail_code.py`（在 188 上跑）。**没有 JSON 接口**：收件箱服务端渲染，先 GET `/<邮箱>` 让它下 `inbox_ctx` cookie，再读 `/inbox1/`；只认 Meta 来信里的码（页面上还有广告六位数字，裸 `\d{6}` 会误抓） |
| 卖家 `gg.00yx.cn/xxx` | 回 JSON，`subject` 里是 `Use code NNNNNN`；只给最新一封 ⇒ 判新鲜只看「码变没变」 |

### 🔴 为什么不用浏览器（10-30 调研 + 实测，别再走回头路）

muse 登录是 Meta 的 native-auth 流程，**整条链路没有任何验证码**
（36 个 JS chunk + 登录页全文搜 `recaptcha/grecaptcha/hcaptcha/turnstile/arkose` = **0 命中**），
五步纯 JSON 接口就能登完 —— `login_http.py`：

```
① GET  /                              跟跳转链拿 datr
② POST /api/auth/native/restart       {"resume":false} → 201 csrf_token + 三个 native_auth cookie
③ POST /api/auth/native/send-otp      {"contact_point":<邮箱>,"region":"US"}
④ POST /api/auth/native/confirm-otp   {"otp_code":<6位>,"remember_device":true}
⑤ POST /api/auth/save-account  +  GET /api/auth/check
   然后轮询 /thread/new + /api/session 等 hatch_vml
```

②~④ 必带 `X-Hatch-CSRF-Token`（少了 403 `csrfFailed`）、`X-Hatch-Waterfall-Id`、
`X-Hatch-Caa-Reg-Entry-Point: login_home` + 同源 Origin/Referer/Sec-Fetch-*。TLS 指纹要像浏览器
（`curl_cffi` impersonate chrome；有项目用 cycletls 被挡回去只好退回 playwright）。
**人机验证不是页面跳转**，而是接口回 `403 {"type":"checkpointRequired"}` ⇒ 脚本能自己判出
「这号要人过」直接跳过，不用等人。23 个同类开源项目里只有 2 家真自动化了登录，
其余都从「已登录的 cookie」起步 —— 浏览器那条路是大多数人放弃的路。
另有一条**口令登录**（不发验证码邮件）：`restart` 回包就带 `password_encryption{key_id,public_key}`，
libsodium `crypto_box_seal` 包成 `#PWD_BROWSER:5:<ts>:<b64>` 发 `/api/auth/native/confirm-password`。验证码这条不稳时的备选。

### 🔴 踩过的坑（都实测过，别重犯）

- **`hatch_gw` 不是必需的**：它只是网关路由缓存（约 1 年），续签和 VM 唤醒只依赖
  `hatch_sess` + `hatch_vml`（上游 `engine.py:41` / `store.py:14` 自己写明）。纯 HTTP 登录拿不到它，
  一直等它 = 等一个不需要的东西。必需项 = `hatch_sess` / `hatch_vml` / `hatch_native_auth_device`。
- **curl_cffi 和 ssh 的 SOCKS5 不兼容**（`curl: (97) Failed to receive SOCKS response`），
  同一个隧道 curl/浏览器都正常 ⇒ 登录必须在**集群内**跑（`litellm-dev/gemini-web` 那个 pod 带 curl_cffi），
  直接用 mihomo 的 HTTP 代理。顺带这也是「每号一个独立登录 IP」的唯一做法（出口只对集群内 pod 放行）。
- **阿里云那几个 squid（47.84.85.100 / 101.218 / 112.136）只放行白名单域名**，muse.ai 不在表里
  ⇒ `CONNECT tunnel failed 403`。登录出口只用 mihomo 那几个 + 198 默认出口 172.235.204.67。
- **一个验证码只能用一次**，重试必须换新码（复用回 400 `invalidCode`）。
- 集群内 pod **没有 ssh/scp**（取码只能在本机）；`kubectl cp /dev/stdin pod:/x` 建出来的是指向
  `fd/0` 的**软链不是文件**（脚本读不到、日志全空 ⇒ 先落到 198 本地再 cp）。
- bash 循环里的 ssh 会**吃掉标准输入** ⇒ `while read` 只跑第一行（8 行只加了 1 个号）；
  改从 `fd 3` 读 + `ssh -n`。`$var` 紧跟全角字符会被吞 ⇒ 一律 `${var}`。
- 浏览器那条路（`login_local.py` 已删）最后卡的也不是验证码：覆盖层 `native-auth-access-pane`
  拦住**所有点击**（连点输入框都不行，要 `focus()` + 真键盘；JS 直接塞 `.value` React 不认），
  "Next" 是 `type=submit` 且**初始 disabled**，要等框里真有 6 位才可用 —— 填完立刻回车等于没提交。
- **号自己没分到云端工作区**时 `/api/session` 回 `unavailable`（不是 `assigned RUNNING`）⇒
  拿不到 `hatch_vml`，这是号的状态不是我们的问题（10-30 doyle 就是这样，同批 henn 正常）。

🔴 **不替用户改绑邮箱**（卖家建议「尽快改成自己的邮箱」）——账户层不可逆，必须用户拍板。

## 每号一个出口 IP（10-30 已上线，12 号 = 12 IP）

用户 10-30 的原话：「每个 ip 绑定一个账户。而不是一个 ip 绑定多账户。」

**10-30 之前是假的**：`add_batch.sh` 按行号给每个号分了**登录**出口，但 Secret
`muse2api-egress-map` **根本没建**（`kubectl get secret` → NotFound），而 k8s.yaml 里是
`optional: true` ⇒ 静默回落，**12 个号服务时全从默认隧道 `47.84.85.100` 出去**。
登录 IP 和服务 IP 对不上，正是 harden #7 要消掉的那种关联。

```bash
# 台账（一号一辈子一个 IP 的真源，198 上）
sudo python3 /opt/muse-egress-tools/egress_assign.py show
sudo python3 /opt/muse-egress-tools/egress_assign.py claim <label> [出口名]   # 幂等，重登复用同一个
sudo python3 /opt/muse-egress-tools/egress_assign.py sync --apply            # 台账 -> Secret（之后要 rollout）
# 改之前逐条验（harden #7：错的 entry 会让那个号直接报错，不回落）
sudo python3 /opt/muse-egress-tools/verify_entries.py
# 改之后逐号验（走进程自己的 proxy_for_account + bridge，和聊天同一条路）
sudo python3 /opt/muse-egress-tools/verify_serving.py
# 新造一条 muse 专属出口（一个订阅单节点 = 一个固定 IP）
bash /opt/muse-egress-tools/muse_px_mint.sh test  "<节点名>"      # 先测这个节点通不通 muse.ai
bash /opt/muse-egress-tools/muse_px_mint.sh apply <序号> "<节点名>"
```

| 东西 | 位置 |
|---|---|
| 持久台账 | `198:/opt/chatgpt-acct-console/data/muse_egress_assign.json`（0600，只记出口**名字**+实测 IP，**不记凭据**） |
| 服务侧 | Secret `muse2api/muse2api-egress-map`（`MUSE2API_EGRESS_MAP`，`label=代理URL` 逗号分隔，含凭据） |
| 登录侧 | `add_batch.sh` 从同一个台账 `claim`，所以登录 IP == 服务 IP |
| muse 专属出口 | `kiro-rs` 里 `muse-px-1..5`（Deployment/Service/NetworkPolicy 各一套），口令 `198:~/muse-px-egress/<n>/pw` |
| 页面 | 8796 muse 页：每号一行「出口 IP + 对照表（出口名）」+ KPI「一号一 IP」（有共用会标红） |

### 🔴 出口够不够 / 哪些能用（10-30 全部从**真 pod** 实测，别照抄旧表）

判据三件全要：① 经这条腿的真实出口 IP ② `muse.ai/` <400 ③ `/api/session` 是业务回包（401 算通）。

- **18 个候选里只有 12 个能给 muse 钉号**，刚好够 12 个活号。
- ❌ 阿里云 4 台 squid（`47.84.85.100` / `47.84.101.218` / `47.84.112.136` / `47.236.200.98`）：
  能打通 ipify，但 muse.ai 一律 **CONNECT 403**（域名白名单里没有）。
  **默认隧道 `47.84.85.100` 就是其中一台** —— 所以「落默认隧道」对 muse 等于不能用。
- ❌ `cac-us-egress`：出口 IP **会漂**（同一个 pod、restarts=0，先后量到 `67.21.77.249` /
  `170.178.162.206` / `67.21.77.249`）⇒ 钉不住号。
- ❌ `us-170.178.173.10`（kiro-us-egress）：上游是 5 节点 fallback **组**，IP 在组内漂，同理。
- ✅ 能用：`jp-172.235.204.67`、`kr-203.231.254.184`、`ph-49.157.61.223`、`sg-109.176.19.134`、
  `sg-103.47.211.19`、`de-64.118.159.27`、`uk-213.255.246.28` + muse 专属 `muse-px-1..5`。
- **要更多 IP**：订阅里 62 个节点、额度只用了 0.77% / 4.21%（200GB×2，到 2027-10）⇒ 还能造。
  但**节点能不能连 muse 要一个个试**：10-30 试了 16 个单节点，通 7 个
  （JP大阪03 / JP06 / TW台湾01 / SG02 / FR巴黎 / DE德国03 / UK英国02），
  挂 9 个（US纽约10、US硅谷12、US洛杉矶13、KR首尔、MY、SG NF04、TW ECO 02…）。
- **余量**：10-30 时 12 个活号正好吃满 12 条腿，**再加号就没有空闲出口了**
  （`egress_assign.py claim` 会明说「没有空闲出口了」并让新号落默认隧道，exit 1，不会偷偷共用）。
  再来号先造腿：已验证能连 muse 但还没用的节点 —— `SG - 新加坡- 05`(64.118.136.106)、
  `US - 圣何塞 - 04`(198.2.237.21)；另外池里还有 `ausley-01`/`doyle-03` 两个停用号要救，
  救回来也要各占一条腿 ⇒ 至少再造 2 条。
- 🔴 **节点在 198 主机上能通 ≠ 在集群 pod 里能通**：`TW - 台湾01` 主机侧 200，pod 侧 100% 超时
  （mihomo 日志 `connect error: context deadline exceeded`；集群 DNS 和 8.8.8.8 解析一致、
  TCP 从 198 和从 242 都通 ⇒ 不是 DNS 也不是防火墙，原因未查）。**一律以 pod 侧为准。**
- 🔴 **稳定 ≠ 可靠**：`muse-px-1`(JP大阪03) 和 `muse-px-3` 原来的 FR 巴黎都是「大多数时候通，
  偶发 timeout」。harden #7 不回落 ⇒ 腿抖一次 = 那个号失败一次。所以
  **最忙的号要放最稳的腿**（`reliability.py` 连打 8 次看成功率），抖的腿换节点。

### 🔴 harden 7f：app.py 自己那两处续签也必须走该号的腿（10-30 新增）

7d 只修了 `engine.ensure_page` 里的续签。`app.py` 另有两处 `renew_session_http` **没传 proxy**：
`_renew_and_persist`（每个请求前的预续签）和 `_probe_account_sync`（15 分钟保活循环）。
**在线上 pod 实测**：`requests` 不带 `proxies=` 会跟 `ALL_PROXY=socks5h://127.0.0.1:1080`，
出口是 `47.84.85.100` ⇒ 每个号的 `/api/session` + VM wake 都从共用默认隧道出去，
而聊天 WebSocket 从自己的 IP 出去。**正好是「cookie 从 A 续、聊天从 B 发」那种关联。**
现在镜像 `5b8bba9561fd-h8` 两处都带 `engine.proxy_for_account(...)` 的 bridge。

怎么验（10-30 用的，可复现）：`sudo python3 /opt/muse-egress-tools/prove_renew_proxy.py <label>`
—— 在真 pod 里给 `engine.renew_session_http` 套 spy，抓 `_probe_account_sync` 真正传进去的
`proxy_server`。判据：必须是非空的 `http://127.0.0.1:<port>`，且用这条 bridge 量到的出口 IP
== 台账 IP（10-30 实测 `muse-gen-07` 抓到 `127.0.0.1:36549` → 172.235.204.67，对得上）。
🔴 **别用 mihomo 日志当量具**：它的 log-level 是 `warning`，只记错误不记成功连接
⇒「5 分钟 0 行 muse.ai」既可能是没流量也可能是没记日志（10-30 踩过这个假阴性）。

## 看池子 / cookie 过期

```bash
python3 muse2api/pool_status.py            # 号、ok、use_count、cookie 剩余天数、并发是否=号数
python3 muse2api/pool_status.py --test     # 逐号真登录检查
python3 muse2api/pool_status.py --quota    # 逐号读额度（每号≈30s）
```

cookie 约 2 天有效，服务每 15 分钟 `/api/session` 续签。续不上 ⇒ 该号 401/`ok=False` ⇒
在管理页删掉旧号再 `add_acct.py` 同 label 重导（脚本拒绝重复 label）。

🔴 **续签是进程内的**（`app.py` 的 `_keepalive_loop`，`asyncio.create_task`），**不是 cron**——
198 上的 crontab / systemd timer / k8s CronJob 都不碰 muse 凭据。找「谁在续 cookie」别去翻 cron。

🔴 **15 分钟是「检查周期」，不是「每号每 15 分钟都续」**（10-30 读 `run_keepalive_all` 确认）：
每轮只处理 `now - last_keepalive > 6h` 且（`expires_at` 剩 < 36h 或 `ok is not True`）的号，
其余记 `skipped=会话充足且近期已保活`。所以 `last_keepalive` 停在几小时前是**正常的**；
真正的判据是 `expires_at` 有没有被推到 ~+48h（10-30 实测 12 个活号都在 +39~48h、`ok` 全 True）。

**看门狗（10-30 新增）** `muse2api/cookie_watch.py`，198 `root` crontab 每小时第 17 分跑：

```bash
sudo python3 /opt/chatgpt-acct-console/muse_cookie_watch.py --thresh 24 --notify   # 手跑
sudo tail /var/log/muse-cookie-watch.log                                          # 看历史
```

① 列 `expires_at` 剩 < 24h 或 `ok != True` 的号；② 对它们调
`POST /admin/accounts/<id>/keepalive` **主动踢一次**（上游自己的接口，走该号自己的腿、不开浏览器、
不占生成槽）；③ 踢完**回读**，还不行就发飞书给刘国现，正文写清哪个号、剩几小时、怎么救
（同一行卡密重跑 `add_batch.sh`，`--relogin` 保留 id/用量，出口按台账复用同一个 IP）。
阳性对照：`--thresh 999` 必须判出「12 个要过期」并真发出去（10-30 验过，回了 message_id）；
阴性：`--thresh 24` 全绿 exit 0。
🔴 它**不自动重登**（重登要卡号密码/卡密，脚本不替人决定用哪份凭据）。

## 查额度（积分）

- 网页：198:8796 管理台 muse 页「本周已用 / 额外 token」列，每 30 分钟自动刷（`chatgpt-acct-console/quota_extra.py`，就是下面那条 server action）。
- 日常：`python3 muse2api/pool_status.py --quota`（只查 enabled 的号；死号不再让整个脚本崩）。底层 = `POST /admin/accounts/<id>/quota`，开浏览器点 Settings→Usage，每号约 30s。
- 免费号只有两个数：**周额度已用 %**（每周固定时间重置）+ **额外 token**（`1B tokens left`，永不过期）。没有「积分余额」这种数字。
- 更快的读法（10-29 抓包，0.4s，不开浏览器）：muse 网页自己用的 Next.js server action `fetchSubscriptionAction`。细节、必需的头（少 `Next-Router-State-Tree`/`Sec-Fetch-*` ⇒ 403）和 action id 会随 muse 发版变的坑见 memory `reference_muse_quota_read_via_server_action`。脚本 `.work/muse-quota/direct_sub_action.py`（pod 内跑，走该号自己的出口）。

## 部署 / 升级 / 回滚

```bash
MUSE2API_SHA=<上游新commit> ./muse2api/deploy-198.sh   # 188 构建 → 推 198 registry → apply
MUSE2API_BUILD_ONLY=1 ... ./muse2api/deploy-198.sh     # 只构建+推，不建 Secret 不 apply
sudo kubectl -n muse2api set image deploy/muse2api muse2api=127.0.0.1:5000/muse2api:<旧tag>   # 回滚
```

- `harden.py` / `harden_av.py` 每个补丁要求**恰好匹配 1 处**，上游改了就直接失败，不会静默漏补。
  `harden.py` #1~#8：禁 `/admin/update*`/`/admin/repo*`（上游会从 GitHub 拉代码覆盖自己、还能
  git push）；自动生成的 key 不打日志；参考图两个开关钉死 False；chat 选 muse-image 走生图；
  选号跳过 `ok=False`；Pod 级出口代理；**每号固定出口**（见下一节）；单号 5 槽。
  `harden_av.py` #9~#10（10-29 新增，**单独一个文件**，整块音视频功能可以整块不跑而不碰 #1~#8）：
  ffmpeg 进镜像、Voyager 听写、聊天附件、`/v1/audio/transcriptions`、`/v1/audio/speech`→501、
  `/v1/videos` 改成 OpenAI 形状 + `/content`。
  🔴 **加新补丁前先在线上代码上干跑**：`kubectl exec` 把 `/app/{app,engine,config,store}.py` 拷出来，
  放进一个临时目录（Dockerfile 用一份带 `    chromium \` 那行的假的就行）再跑 harden 脚本，13 个
  锚点要全 ok；装上去才发现对不上就是一次 Recreate 中断。
- 188 只当构建机，**不在 188 跑服务**（用户 10-13 明确要求放 198）。
- `Recreate` 单副本：accounts.json 单写者 + 一个 Chromium，别开第二副本。
- 🔴 **本机（Mac）不要当 `docker save | docker load` 的中转**：188→Mac→198 两跳 ssh 串流
  ~960MB 必断（`Can't assign requested address` / broken pipe）。188 上有到 198 的免密 ssh +
  免密 sudo，**在 188 上直接 `docker save | ssh cltx@10.68.13.198 "sudo docker load"`**。
  188 的 docker 没把 `10.68.13.198:5000` 列进 insecure-registries，所以不能从 188 直接 push。

### 升级到 v1.6.4 + 每号固定出口（10-29 已构建，**未部署**）

镜像 `127.0.0.1:5000/muse2api:5b8bba9561fd-h5` 已在 198 registry（上游 tag `v1.6.4`
= `5b8bba9561fd`，领先老 pin 17 commit）。线上仍是 `b19b4842eedf-h4`，**k8s.yaml 写好了但没 apply**。

底座结论（10-29 调研）：**继续用 `czg86389-hub/muse2api`**，不换。
- `its-benjamin/muse2api-http` 是唯一真正免 Chromium 的（curl_cffi + Noise XX 直连，
  `noise.py`/`wire.py`/`protocol.json`，协议来自 `minhquan130599/MuseAI-API`）。**技术方向更对**：
  免浏览器 ⇒ 没有 per-context 代理这套复杂度（每号一个 `AsyncSession`，`proxy=` 天然一号一 IP）、
  内存从 8Gi 降到几百 Mi、还有 `/v1/responses` 和工具桥。**但 10-23 才建仓、★0、v0.1.0→v0.1.7
  全在 10-27~28 两天内发**，没有第二个人在用；协议层靠 muse.ai 的 Next.js bundle 逆推，
  改版即失效，而我们没有 muse.ai 的协议知识去修。**先观察，别拿生产池做它的第一个用户。**
- `www222fff/muse2api`（★283）宣传 pluggable driver，但 `drivers/http/driver.py` 只有 1.7KB
  **回 501 占位**，README 自己写 browser driver「needs live testing」；`DaBinBinah/muse-bridge`
  是同一套代码。两个都不如我们现在跑通的。几个 Go 实现（vip0014/RefuseOdd/ninja-c9）都是
  ★0~2 的新仓，同样是浏览器方案，没有换的理由。
- 升级本身是净收益：上游 PR #24 **自己实现了参考图 SSRF 防护，比我们原来的补丁好**
  （逐跳重解析防 DNS rebinding、禁重定向 handler），还有多参考图、配额读数隔离、保活调度。

harden h5 的变化（`python3 muse2api/harden.py <解压目录>` 干跑 26 处全部恰好匹配 1 处）：
- **补丁 3 改写法**：老补丁往 `engine.py` 插 `_public_url()`，对 v1.6.4 `got 0`（上游改了那段）。
  现在改成在 `config.py` 把上游两个开关**钉死** `ref_image_local_files = False` /
  `ref_image_private_fetch = False`（上游默认就是 0，钉死是防 env/.env/未来默认翻转）。
- **新增补丁 7 = 每号固定出口**，移植 `sunxu/muse2api@feature/account-proxy-isolation`，
  `proxy_bridge.py` 整个文件 vendor 进本仓（harden 用 `add_file()` 拷进镜像，Dockerfile 加一行 COPY）。

每号一 IP 的做法（为什么要本地中转）：
```
号A → proxy_bridge 在 127.0.0.1 开一个「无认证」口 → 加 Proxy-Authorization → mihomo ClusterIP:7890
号B → 另一个本地口                                  → 另一条腿
```
- **CDP 的 `proxyServer` 不能带凭据**，而集群 7 个 mihomo 全部要 HTTP Basic（不带就 **407**，
  10-29 从 muse pod 里实测 7/7 都是 407）⇒ 必须有本地无认证口这一层。顺带好处：
  凭据只在 muse2api 进程里，不进 Chromium argv、不进任何 CDP 消息。
- 映射走 env **`MUSE2API_EGRESS_MAP`**=`label=代理URL,label=代理URL`，键是账号 label（查不到再查 id），
  不改 accounts.json、不改管理页。没写进 map 的号继续走 Pod 级 1080 新加坡隧道（即老行为）。
  格式错**启动就炸**（`parse_egress_map` 逐条 `parse_proxy`），不留到第一个请求。
- 🔴 **代理失败一律报错，绝不回退**：上游 `_open_page` 在 `createBrowserContext` 失败时会退回普通
  标签页（= Pod IP），对固定出口来说那等于悄悄把号搬到错的 IP 上 ⇒ 补丁里带 proxy 时直接
  `MuseGenerationError`。
- 🔴 **报的是 `MuseGenerationError` 不是 `MuseAuthError`**：只有 `MuseAuthError` 会让 app.py
  `store.mark(id, False)` 把号判死。我们自己配错代理**不许把活号的 cookie 标成失效**。
- 🔴 **页面缓存的 key 带上代理**：`ensure_page` 原来只比 `current_acc_id`，复用上一个 context
  就会沿用上一条腿 ⇒ 补丁加 `current_proxy_url == configured_proxy`。
- 🔴 **`/api/session` 续签和 VM wake 也必须走同一条腿**（`renew_session_http(proxy_server=...)`）。
  不然同一个号「cookie 从 A 续、聊天从 B 发」，正好是想消掉的那种关联。

出口只能用集群内 7 个 mihomo（ns `kiro-rs`，`:7890`）：
`kiro-px-1..5`、`kiro-uk-egress`、`kiro-us-egress`。
- **必须写 ClusterIP 不能写 svc 名**：muse pod 的 DNS 是公网 DNS（coredns-custom 两个 zone），
  解析不了 `*.svc.cluster.local`。10-29 的 IP：`10.43.25.225 / 10.43.89.121 / 10.43.38.234 /
  10.43.11.21 / 10.43.50.224 / 10.43.226.25 / 10.43.195.12`，**apply 前重新 `kubectl -n kiro-rs
  get svc` 核一遍**（Service 重建会换 IP）。
- 放行靠 netpol `kiro-rs/cliproxy-muse-to-kiro-egress`，匹配 **`app=muse2api` 标签**。
  🔴 起临时 pod 测代理时没这个标签就是 `Connection refused`，别误判成「代理挂了」——
  但也**不要给临时 pod 打 `app=muse2api`**，那会让它变成 Service 的 endpoint 抢真流量。
  要测就在真 pod 里 `kubectl exec`。
- 新加坡 4 台 squid 的域名白名单里没有 muse 域名，用不了；日本 `243:8120` tinyproxy 实测通。

✅ **per-context 代理在新镜像上实测通过**（`.work/muse-per-ip/ctx_proxy_test_v2.py`，
`docker run --rm` 一次性容器，不碰 muse 账号/cookie，用本地假认证代理当出口）：
同一个 Chromium 两个 context **同时**各走自己那条带认证的腿、页面内容证明是哪条腿回的、
不带 `proxyServer` 的 context 打不到任何固定出口。
🔴 **Chromium 从 155 变 156（h4→h5），换镜像必须重跑这个测试**。

待 apply 的改动（都已写进文件，**没 apply**）：
1. `set image` 到 `5b8bba9561fd-h5`（或 `MUSE2API_SHA=5b8bba9561fd... MUSE2API_HARDEN_REV=h5 ./deploy-198.sh`）。
2. 建 Secret `muse2api-egress-map`（一个 key `MUSE2API_EGRESS_MAP`），内容是 label→代理URL；
   k8s.yaml 里已 `secretKeyRef` + `optional: true`，**不建这个 Secret 就是纯升级、不开每号 IP**，
   可以分两步上。
3. 升级与开每号 IP **分开验**：先只换镜像跑一轮回归，再加 map。

### 🔴 两个上线时踩过的坑（manifest/集群已带上，别回退）

1. **Service 名撞环境变量**：Service 叫 `muse2api`，k8s 自动注入
   `MUSE2API_PORT=tcp://10.43.x.x:18610`，应用把它当端口 `int()` ⇒ CrashLoop。
   manifest 里 `enableServiceLinks: false` + 显式 `MUSE2API_PORT=18610`。
2. **集群 DNS 污染**：CoreDNS 转发的上游把 `hatch.metaaivm.com` 解成 `2001::1`/
   `104.244.42.x`/`185.45.5.35`，`auth.muse.ai` 也污染（`muse.ai` 主域恰好没事）⇒
   `/test` 还显示「会话有效」，但聊天 21s 报 **「云端 VM 连接超时 (Still sending)」**。
   判据：`kubectl exec <pod> -- getent ahosts hatch.metaaivm.com` 对比 `dig +short @8.8.8.8`。
   已在 `kube-system/coredns-custom` 加 `metaaivm.server` + `muse.server` 两个 zone 转
   `8.8.8.8 1.1.1.1`（只影响这两个域，约 50~90s 生效；备份 198:`~/backups/coredns-custom-20261013-131936.yaml`）。
   **别删这两个 zone。** 没选 pod 级 `dnsPolicy: None`：muse2api 不需要集群内解析，但 zone 方式对以后同类 pod 也有效。

## LiteLLM 侧

```bash
scp muse2api/litellm_register.py cltx@10.68.13.198:~/muse-reg-<sid>.py
ssh cltx@10.68.13.198 'python3 ~/muse-reg-<sid>.py'                 # dry-run + 列出谁已能调到
ssh cltx@10.68.13.198 'python3 ~/muse-reg-<sid>.py --apply'         # 建/重建两组，等全副本连续 3 次一致
ssh cltx@10.68.13.198 'python3 ~/muse-reg-<sid>.py --delete --apply'   # 回滚
```

- 组 `muse-spark`（id `muse2api-muse-spark`，chat）、`muse-image`（id `muse2api-muse-image`，
  image_generation），`openai/` 前缀，timeout 300，`num_retries 0`，价格 0；upstream key 从
  Secret 读，不进命令行。
- **10-29 新增两组**（`.work/1029-batch/c-muse/register_video.py`，198 上 `~/muse-video-reg-c.py`，
  同样 dry-run → `--apply` → `--delete --apply` 回滚）：
  `muse-video`（id `muse2api-muse-video`，`video_generation`，timeout 900）、
  `muse-asr`（id `muse2api-muse-asr`，`audio_transcription`，timeout 600）。
- 🔴 **各媒体条目的上游 `model` 必须互不相同**（`openai/muse-video` / `muse-asr` / `muse-image` /
  `muse-spark`）：`GET /v1/videos/{id}` 从 id 里 base64 解出 model_id，再用
  `Router.resolve_model_name_from_model_id` 反查组名（**也按 `litellm_params.model` 匹配**），
  同名就反解到别的组 ⇒ 403。
- 🔴 **LiteLLM 的 `/v1/videos` 不是「配上就能用」**：POST 无条件 multipart（上游 PR #38104，
  2026-08-24 合入，比我们任何版本都早，升级永远不修），且 POST/GET 两个回包都过
  `VideoObject.model_validate`，强制 `object=="video"`（Literal）、`status` 是 str、
  `error` 是对象或 null。上游 muse2api 只吃 JSON、回 `object="video.task"` + 字符串 error
  ⇒ 行写进去了但一发也打不通。**harden #10 把 muse2api 侧改成 OpenAI 形状**（两种编码都收，
  回包同时带 OpenAI 的 `status` 和原来的 `task_status`，并补 `GET /v1/videos/{id}/content`），
  所以这次没用 pass-through（pass-through 不进 SpendLogs）。
- **给 key** 用 skill `litellm-198-key-allowlist` 的脚本，**按 `--only` 点名**，dry-run → `--apply --backup`
  → 再跑一遍应 `planned=0`。10-13 只给了 `cursor-liuguoxian-std`（35→37，快照
  198:`~/cursor-std-pre-muse-20261013T073036Z.json`）。
- 🔴 **不要加进全员 key**：不支持工具调用，Cursor 里选到只会报错/卡很久。
- 🔴 `models` 为空的 key = 全放行，**不授权也能调 muse-***（10-13 是 4 把 claude-code-canary、
  `stability-probe`、2 把无 alias 临时 key）。dry-run 会列出来，汇报「谁能用」时要单列。
- 验收（真 key，正反双对照）：
  `printf '%s' "$SK" | ssh cltx@10.68.13.198 'read -r STD_KEY; export STD_KEY; bash -s' < muse2api/litellm_e2e.sh`
  阳性：`/v1/models` 见两名、chat 原样回 nonce、流式多段、出图 items=1；
  阴性：临时 key（只有 deepseek）打 muse-spark 必须 **403**，脚本自删临时 key 并回读为 0；
  SpendLogs 有 `success` 行、spend=0。
- 视频 / ASR 验收：`.work/1029-batch/c-muse/e2e_30402.py`（198 上 `~/muse-e2e-c.py`），自建 30m 临时 key、
  跑完自删。默认只跑 ASR + 阴性；`--video` 才烧**一次**视频生成。判据是内容不是 200：
  ASR 文本要命中「会议室/评审/电脑/四十五」至少两个；视频要 ffprobe 出 video 流且 duration>1。
  🔴 **198 上没有 ffprobe**，mp4 要拉回本机验（脚本里那一步在 198 上会 FileNotFoundError）。

## 池子现状（10-30：12 个活号）

- **10-30 一晚从 1 个活号加到 12 个**：`muse-gen-05`、`07`~`16`（11 个新号）+ 原有 `horwich-04`，
  全部真登录测试通过，分散在 7 个出口 IP。并发 `MUSE2API_CONCURRENCY=60`（12×5），manifest 已同步。
  加号命令和坑见上面「加号」那节。
- **没救回来的两个**：`ausley-01`（手上没卡密，要用户重发）、`doyle-03`（卡密有、登录也通，
  但 `/api/session` 回 `unavailable` —— Meta 没给这个号分云端工作区，是号自身状态）。两个都 `enabled=false`。
- 历史（10-16）：4 号里 3 个被 muse.ai 登出（`/api/session HTTP 401`）⇒ 曾只剩 1 个活号、并发降到 1。
- harden #5（h3）：选号跳过 `ok=False` 的号。上游在活号忙时会挑死号 ⇒ 当场 401。
  保活每 15 分钟会重探 ok≠True 的号，恢复了自动回到候选里。
- 🔴 **杭州 gw 每 5 分钟拿 `Hey how's it going?` 打 muse-spark 3 遍**（`background_health_checks: true`，
  `muse-spark` 是 chat 行没被豁免）。每发占一个号 15~40s。✅ 10-16 已改杭州 `scripts/hangzhou-gw/sync_models.py`：
  `muse-*`/`web-gemini-*` 一律 `disable_background_health_check`。以后再接浏览器类上游，名字套不上这两个前缀就要补正则。
- 🔴 **cookie 约 2 天有效**，服务每 15 分钟 `/api/session` 续签 ⇒ 只要服务不停就不会过期；
  真过期了用 `add_batch.sh` 同一行卡密重跑即可（`login_http.py` 带 `--relogin`，保留 id/用量）。

## 出图回归（改了 muse2api / gemini-media / 图片行之后必跑）

用户层四线型 + 阴性对照，判据是「客户端从公网**不带 cookie** 真能打开图」，不是 200、也不是「有 url」：
脚本在 `scripts/image-ux/`（用法见 `regress.sh` 头部：克隆 std 形状起 30m 临时 key → `userlayer.py`/`stream.py` → 删 key）。
10-16 基线：images gemini 15s jpeg / muse 43s webp；chat gemini 15s / muse 40s，两条链接都能从外网打开；流式 muse 首包 0.1s。
keyprobe（`chat.auto-link.com.cn/keyprobe/`）现在对 url 形态**会真去取图**，取不到就判 BROKEN 并写明原因——它变红就是用户看不到图。

## 已知未修

- muse2api 长回复偶发把同一段**重复多遍**（10-13 一次 subscription 查询重复 5 遍；短回复没见过）。根因未查。
- 中文界面下额度读数依赖 UI 被切成英文才能解析；页面上的「检查更新」按钮会报错（接口被 harden 关了，正常）。
- ~~线上所有号仍共用一个新加坡出口 IP~~ → **10-30 已解决**：12 个活号 = 12 个不同出口 IP，
  进程内 `proxy_for_account` 逐号验证过（`verify_serving.py` 全绿），真聊天/真出图都通。
  **仍未知**：Meta 会不会把「同一个号换 IP」本身当风险信号（10-30 那次一次性把 12 个号都从
  `47.84.85.100` 挪走了；观察期到 11-01，看有没有号被登出/要重新验证）。
- 🔴 **腿抖 = 那个号失败一次**（harden #7 不回落）。10-30 实测 `muse-px-1` 原来挂的
  「JP 日本大阪 - 03」一小时里 2 次 timeout、连打 8 次 7 成；换成「JP 东京 - 10」后 12/12。
  所以**最忙的号（horwich-04）必须放最稳的腿**，加新腿先跑 `reliability.py` 连打 8~12 次。
- 上游长回复重复输出的修复 PR #28 **仍未合并**，v1.6.4 里没有，所以那个已知问题还在。
- `its-benjamin/muse2api-http`（免 Chromium 直连协议）值得每隔几周回看一次：成熟了就是
  更好的底座（省 Chromium、省这整套 per-context 代理）。10-29 评估：太新，★0，不能上生产。
- 音视频（h7，10-29）三条没查清：
  - 聊天里带远端 `video_url`（`http://...` 而不是 `data:`）现在是**静默跳过**（当成普通文字处理完），
    不是 400。不抓远端是故意的（无 SSRF 面），但客户端看不出附件被丢了 ⇒ 应该改成显式 400。
  - 听写**是否算周额度**没证据：免费号界面没有 STT 计数器，Voyager 也不走聊天回合。
    观察法 = 连续只打 `/v1/audio/transcriptions`，前后读 `percentUsed`（见
    `reference_muse_quota_read_via_server_action`）。
  - Voyager token 的有效期、`/v1/audio/transcriptions` 的长音频上限（>160s 分块并行只测过 159s/5 块）
    都没压过。
