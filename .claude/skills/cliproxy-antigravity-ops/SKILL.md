---
name: cliproxy-antigravity-ops
description: >-
  运维 198 上把 Google AI Pro(Antigravity)订阅变成 API 的两条路：生产路 cli-proxy-api
  网关、以及 sub2api 那份拷贝。加新 Google 账号扩容（单个/批量 100 个，**两条路都要加**）、
  **三层判据验证"每条腿是不是真能用"并出测试报告**、逐号量模型可用性与配额、
  排查网关/模型故障、往 LiteLLM 加 ag-* entry、开 key 白名单。
  含 per-auth model_aliases（让钉旧 UA 的号够到 -high 名字）、四条量具纪律、
  `replicas=1` 单点。
  Use when the user mentions "Google AI Pro" / "Antigravity" / "gemini pro 账号" /
  "cli-proxy-api" / "CLIProxyAPI" / "ag-gemini" / "claude-ag-" / "加个 google 号" /
  "gemini 账号池" / "fetchAvailableModels" / "三条腿是不是都能用" /
  "验证下各个 gemini 模型" / "model_aliases" /
  "sub2api 上看不到这几个号" / "把 gemini/antigravity 号加到 sub2api"。
---

# CLIProxyAPI / Antigravity 运维

## 拓扑

```
Google AI Pro 订阅(个人号)
   └─ OAuth refresh_token ──> k8s Secret litellm-dev/cliproxy-secrets   ← 凭据源头
        │                           │ initContainer 铺进 emptyDir
        │                           ▼
        │                 路① deploy/cli-proxy-api (198 节点, **replicas=1**)   ← 生产
        │                 走 daily-cloudcode-pa.googleapis.com
        │                 暴露 OpenAI 协议 :8317 / NodePort 31882
        │                           ▼
        │                 LiteLLM 198 的 12 条 ag-* / claude-ag-* entry
        │                           ▼
        │                 633 把 cursor-* key（公开别名 gemini-3.8-flash）
        │
        └─ 同一份凭据拷一份 ──> 路② sub2api Postgres accounts 表   ← 目前零生产流量
                                 只有 group 10 一把 probe key
```

🔴 **加号/撤号要走两条路，只走一条会漏。** 详见下面 [两条路](#two-paths) 一节。

**池子现状（2026-09-09 实测，30/30 全绿）：3 个号都是真腿。**

（本仓 public，下表不写账号邮箱全名。真名单跑 `list` 现看 —— 本来也只该这么看。）

| 号 | Secret key | UA | 目录 | 10 模型命中 |
|---|---|---|---|---|
| 老号（`1632…`）| `antigravity-auth.json`（遗留名）| hub 默认 | 33 | ✅ 20/20 |
| 新号 A（`ikme…`）| slug 名 | 钉 `1.0.0` | 27 | ✅ 20/20（3.8/3.7 走别名）|
| 新号 B（`samu…`）| slug 名 | 钉 `1.0.0` | 27 | ✅ 20/20（同上）|

三个号 `loadCodeAssist` 都返 `paidTier: g1-pro-tier`（**订阅都是好的 Pro**），
`project_id` 都是 `aicode-consumers`。**别照记忆报数，跑 `list` 现看。**

⛔ **本 skill 09-08 版写的「`ag-gemini-3.8-flash` 只有老号 1 个号在扛，是单点」已被证伪**，
解法见下面「per-auth `model_aliases`」。**真正的单点在进程层不在账号层**：

<a id="single-point"></a>
🔴 **`cli-proxy-api` 是 `replicas=1`** —— 三个账号跑在同一个 pod、同一个节点。
pod 挂 ⇒ 三条腿一起没 ⇒ LiteLLM 侧 12 条 entry + 633 把 key 上的 `gemini-3.8-flash` 同时不可用，
**且 LiteLLM 侧没配 fallback**。09-09 凌晨它刚因节点 `fs.inotify.max_user_instances`
打满 CrashLoop 断了 3.2 小时（[[feedback_node_inotify_instances_128_crashloops_watcher_pods]]），
不是假想风险。**「三个号」带来的冗余被单副本部署整个吃掉了。**
用户 09-09 已知悉，暂未决定是否提副本 / 配 fallback。

⚠️ **网关日志不记是哪个号接的**（只有状态码和耗时）⇒ 某条腿静默失效时，
网关层和用户面**都不会变红**，只能靠主动跑矩阵探测发现。

<a id="model-aliases"></a>
## per-auth `model_aliases`：让钉 `1.0.0` 的号也够到 3.8/3.7-flash

27 个的旧目录里**没有 `gemini-3.8-flash-high` 这个名字**（`-high/-medium/-low`
只在 33 目录里），但**有** `gemini-3.8-flash-tiered` —— **模型在，缺的是名字**。
所以在这两个号的 auth JSON 里加：

```json
"model_aliases": [
  {"name": "gemini-3.8-flash-tiered", "alias": "gemini-3.8-flash-high"},
  {"name": "gemini-3.7-flash-tiered", "alias": "gemini-3.7-flash-high"}
]
```

**`name` 是上游真名，`alias` 是客户端可见名**（这个方向很容易记反）。
网关按 `alias`→`name` 改写后再发上游。**只对该号生效，优先于全局 `oauth-model-alias`。**
09-09 实测：加完两个号 3.8/3.7 各 2/2，用户面 4/4。

⚠️ 这仍是**绕过**不是修好，依赖两个不在我们控制内的外部条件：
「旧 UA 一直不被 Google 关掉」+「`-tiered` 这个名字一直保留」。
真修 = 两个号过完 Google 验证 → 摘 `user_agent` → **删掉这两条别名** → 重新 install。

| 东西 | 在哪 |
|---|---|
| manifest / 凭据 / 备份 / 本 skill 的脚本副本 | 198 `/root/cliproxy-manifests/`（600，**不入 git**）|
| 仓库里的脚本 | `scripts/cliproxy-antigravity-add-account.py`（加号 + 二/三层回归）<br>`scripts/cliproxy-antigravity-model-matrix.py`（逐号×逐模型量真实可用性）|
| Secret | `litellm-dev/cliproxy-secrets`：`config.yaml` + 每号一个 `antigravity-*.json` |
| 网关 | svc `cli-proxy-api.litellm-dev.svc.cluster.local:8317` / NodePort 31882 |
| LiteLLM | NodePort 30402，`store_model_in_db: true` ⇒ 走 `/model/new`，零重启 |

## 硬事实（都实测过，别再重推一遍）

- **判别变量是端点主机名，不是出口 IP。** `daily-cloudcode-pa.googleapis.com` 通，
  `cloudcode-pa.googleapis.com` 恒 429 —— 且该端点 25 个模型 `remainingFraction` 全 1.000，
  **那个 429 是误导文案，为什么 429 至今没定因**。换 WARP 出口结果一字不差。
  CLIProxyAPI 对 consumer 凭据默认就走 daily（`antigravity_executor_request.go:389`），
  不需要配任何端点。
- **UA 有两个独立作用，别混成一个**（2026-09-08 两轮才量清）：
  - **① UA 的「族」决定账号校验走不走。** 网关默认发 `antigravity/hub/<ver> darwin/arm64`
    （版本从 hub manifest 现拉，当时 2.12.2）。**没过 Google 账号验证的号，在任何
    「新」UA 上都 403 `VALIDATION_REQUIRED "Verify your account to continue."`**，
    只有 `antigravity/1.0.0 windows/amd64` 那条老路不查这道校验。
  - **② UA 的版本号决定拿到哪份模型目录。** 同一个老号、同一时刻只换 UA：
    `1.0.0 win` → **27 个模型、无 3.8/3.7-flash-high、打过去 404**；
    `2.12.2 win` / `hub/2.12.2 win` / `hub/2.12.2 mac` → **33 个、有、200**。
  ⇒ 所以钉 `1.0.0` 是**止血**：绕过校验的代价是被降级到旧目录、够不到 3.8/3.7-flash。
  真修 = 真人点 403 details 里的 `validation_url` 走完验证，**再把钉的 `user_agent` 摘掉**。
  `antigravityConfiguredUserAgent()` 先读 `auth.Attributes` 再读 `auth.Metadata`。
  ⚠️ 例外：拉模型清单那条路（`sdk/cliproxy/antigravity_models.go:53`）**写死用 hub UA，
  不读凭据里的 `user_agent`**。不影响生成，因为线上跑的是 `-local-model` 内置清单。
- ⛔ **「某个号缺某个模型」这句话，在没统一 UA 之前不许说。**
  09-08 我拿 `1.0.0` 量新号、拿 hub `2.12.2` 量老号，得出「新号没有 3.8-flash」——
  **一次改了两个变量，把 UA 的效果归给了账号。真相是没有任何账号缺 3.8-flash。**
  跨号比清单一律 `cliproxy-antigravity-model-matrix.py --ua 'antigravity/2.12.2 windows/amd64'`。
- **404 `Requested entity was not found` 和 403 `VALIDATION_REQUIRED` 是两回事**：
  404 = 这个 UA 的目录里没这个名字（换新 UA 就有）；
  403 = **账号没过验证**（`support.google.com/accounts?p=al_alert`，即"异常活动请验证身份"）。
  两者都**不是**订阅问题 —— `loadCodeAssist` 对两个新号都返 `paidTier: g1-pro-tier`
  （Google AI Pro），订阅是好的。
- **判活只认唯一 nonce 原样回读。** `:loadCodeAssist` 在坏端点上照样 200；
  网关 `/v1/models` 返 11 个也只证明配置加载了。
- **pod 必须 `dnsPolicy: None` + `1.1.1.1/8.8.8.8`**，集群 DNS 10.43.0.10 被投毒 → `SSL: UNEXPECTED_EOF`。
- **`/runtime` 必须 emptyDir**：Secret 挂载只读，而网关刷新 token 后要回写 auth 文件。
  代价是重启后回到 Secret 里的旧快照，靠 refresh_token 重新刷（已验证 15min 周期刷新真的在跑）。

## ⛔ 三层判据，别混用

| 问题 | 打哪 | 命令 |
|---|---|---|
| **这个号**还活着吗 | 拿它自己的 refresh_token **直打上游**，绕开网关 | `cliproxy-antigravity-model-matrix.py --only <email> -n 2` |
| **池子**还能服务吗 | 网关 nonce | `cliproxy-antigravity-add-account.py verify [--all]` |
| **用户**还能用吗 | LiteLLM 入口 + key 白名单 + strict 模式 | `cliproxy-antigravity-add-account.py regress [--all]` |

三层各自只回答自己那一行，**任意一层的绿都推不出另外两层**：
池子绿掩盖成员死（下面纪律 1），网关绿不覆盖 LiteLLM entry / 白名单，
用户面绿也不告诉你是哪个号在扛。

**要出「三条腿都真实可用」这种结论，三层必须全跑**（09-09 那次完整 SOP）：

```bash
# 第一层 逐号 × 逐模型（30 格，唯一能回答个体死活的层）
sudo sh -c "cd /root/cliproxy-manifests && python3 cliproxy-antigravity-model-matrix.py \
     -n 2 --json /home/cltx/matrix.json"
# 第二层 网关全模型扫（图像自动排除）
sudo sh -c "cd /root/cliproxy-manifests && python3 cliproxy-antigravity-add-account.py verify --all"
# 第三层 用户面：现拉全部 ag-* entry + 公开别名 + 白名单外反向对照
sudo sh -c "cd /root/cliproxy-manifests && python3 cliproxy-antigravity-add-account.py regress --all"
```

09-09 实测基线：第一层 **60/60**（3 号 × 10 模型 × 2 发）、第二层 **10/10**、
第三层 **13/13 + 403 反向对照**。第一层延迟 0.9~6.4s。

`regress` 用**临时受限 key**（`/key/generate` 限 models + 20min + $1，用完 `/key/delete`），
不用 master key —— master key 绕过白名单，拿它测等于没测白名单。
并且必带一个**白名单外**模型（`chatgpt-gpt-5.6-sol`）的反向对照：
只有「该通的通 + 该拒的拒」都对，这把尺子才算没坏。

⭐ **`regress` 一定会连公开别名 `gemini-3.8-flash` 一起测**（`PUBLIC_ALIASES`）。
公开名和真实组名是**两条不同的路** —— 公开名要靠 per-key `aliases` 改写才落得到组上。
只测真实组名，别名少写一半也全绿。09-09 那 19 把 blocked cursor key 就是
「`models` 里有名字、`aliases` 里没有」⇒ 实测解封即 **400**，而只测组名的尺子看不见。

## ⛔ 四条量具纪律（1~3 是 2026-09-08 栽的，4 是 09-09 开跑前抓到的）

### 1. 打网关判某个号的死活 = 假绿

网关是 round-robin，一个坏号 403/404 之后会被冷却+换号，请求悄悄落到健康号上，
**nonce 照样命中**。09-08 我的 `install` 脚本就是这么对一个 **100% 坏的新号报了「✅ 通过」**。

判某个号本身能不能用，**只能拿它自己的 refresh_token 直打 daily 端点**：

```bash
sudo python3 /root/cliproxy-manifests/cliproxy-antigravity-model-matrix.py \
     --only <email> -n 2
```

（旧版靠 `mv` 把其它号挪出 `/runtime/auths` 做隔离——100 个号扛不住，
且会打断在途的真实请求，已废弃。）

### 2. 探针的 body 形状必须和网关一字不差，否则会误报「这个号没有这个模型」

我曾用「裸 body」直探，得到 `gemini-3.8-flash-high` 两个号都 404，
而同样这两个模型走网关是 2/2 通的 —— **是尺子坏了**。
网关（`geminiToAntigravity()`）除了 `model`/`project`/`request` 还会塞四个字段：

```
"userAgent":   "antigravity"
"requestType": "agent"（模型名含 image 则 "image_gen"）
"requestId":   "agent-<uuid>"（image 是 "image_gen/<ms>/<uuid>/12"）
"request.sessionId": "-<int64>"   # 首条 user 文本 sha256 前 8 字节 & 0x7FFF…；image 不塞
```

并且会删掉 `request.safetySettings`。`cliproxy-antigravity-model-matrix.py` 已经 1:1 复刻。

### 3. ⚠️ 探图像模型 = 直接花掉用户真正稀缺的额度

`gemini-3.1-flash-image` 有一个**独立的、极小的、`quotaInfo` 完全不上报**的图像配额。
09-08 我拿它当普通模型探了十来发，**两个号双双打空**：
`429 You have exhausted your capacity on this model. Your quota will reset after 1h21m3s`
（另一个号 4h25m6s），而同一时刻 `quotaInfo.remainingFraction` 还显示 0.976 / 0.996 ——
**那个 fraction 是共享的 gemini 池，不覆盖图像配额**。
矩阵脚本因此**默认不探图像模型**，要 `--with-image` 才探。别随手加。

⭐ **只想知道「这个号有哪些模型」时用 `--quota`**：它只打 `:fetchAvailableModels`，
**一发生成请求都不发、不花任何额度**，还顺带把每个池的 `remainingFraction`/`resetTime`
和窗口打出来。跨号比清单一律 `--quota --ua '<统一 UA>'`，这是最便宜也最不会自伤的量法。

### 4. ⚠️ 探针必须解析 per-auth `model_aliases`，否则对钉 UA 的号必假红

（2026-09-09 第三处尺子缺陷，开跑前抓到的。）矩阵脚本原先**直接拿客户端名打上游**。
钉了 `1.0.0` 的号够不到 `gemini-3.8-flash-high` 这个名字，于是量出 404 ——
看起来像「这条腿没有 3.8」，而它**经网关是通的**（网关会按 `alias`→`name` 改写）。
又是尺子坏了，不是腿坏了。现在脚本按各号 `model_aliases` 解析后再探，输出标 `(alias→真名)`。

两种量法**回答的是不同问题，别混**：

| 命令 | 回答的问题 |
|---|---|
| 默认（解析别名）| **按当前线上配置，这条腿能不能服务** ← 判死活用这个 |
| `--no-alias` | **账号原生有没有这个名字** ← 只在判「验证过了能不能摘 UA/删别名」时用 |

`--no-alias` 同时是**阴性对照**：对两个钉 UA 的号跑它，3.8/3.7 那四格必须变
`0/1 + 404`。**不变红就说明尺子根本红不了，那一轮的全绿不作数。**
另一个更强的对照是喂一个不存在的名字（`--models gemini-9.9-does-not-exist`）。

⛔ **这两个对照的退出码都是 0，别拿 `rc` 当判据。** 脚本按设计把「上游清单里根本
没这个名字」归类为能力差异而非故障（网关会冷却换号，用户无感），只有「清单里**有**
却 0 命中」才 `rc=1`。**判红绿看矩阵格子。**

`--json PATH` 落盘结构化结果（含逐发延迟、`upstream` 真名、`last_error`），写报告直接引用。

## 加账号

🔴 **本节只讲路①。加完必须接着走路② sub2api，见 [两条路](#two-paths)** ——
只走路① = 号加了一半，`sub2api` 那边看不到（2026-09-09 栽过一次）。

脚本在 198：`/root/cliproxy-manifests/cliproxy-antigravity-add-account.py`（仓库同名文件是 SoT）。
子命令：`authurl` / `exchange` / `install` / `verify` / **`list`** / **`regress`**。

**动手前先 `list`**：只读解码 Secret 打印 email / key 名 / project_id / 有没有钉 UA，
一个请求都不发、不花任何额度，并直接标出「遗留 key 名」和盘上文件与 Secret 的差异。
（Secret 是运行时真相，盘上 `/root/cliproxy-manifests/antigravity-*.json` 只是 exchange 的产物。）

### 一个号

```bash
# 1. 出授权链接，发给账号持有人在浏览器里点
sudo python3 /root/cliproxy-manifests/cliproxy-antigravity-add-account.py authurl

# 2. 授权后浏览器跳到 localhost:54545 打不开（正常），把整条 URL 复制回来。
#    exchange 会：换 refresh_token → 从 id_token 解 email → **先用网关真正会发的 hub UA**
#    打 daily 端点唯一 nonce；不过就自动换 `antigravity/1.0.0 windows/amd64` 再试，
#    通了就把 user_agent 钉进凭据。两种 UA 都不过就拒绝生成凭据（硬来才 --force）。
sudo python3 .../cliproxy-antigravity-add-account.py exchange --code '<整条回调 URL>'

# 3. dry-run 看一眼，再 --apply
sudo python3 .../cliproxy-antigravity-add-account.py install --creds /root/cliproxy-manifests/antigravity-<email>.json
sudo python3 .../cliproxy-antigravity-add-account.py install --creds ... --apply
```

### 一批号（要加 100 个就走这条）

**先逐个 `exchange` 把凭据攒在 `/root/cliproxy-manifests/`，最后一次性装：**

```bash
sudo python3 .../cliproxy-antigravity-add-account.py install --creds-dir /root/cliproxy-manifests --apply
```

一次 Secret patch + **一次** rollout，不是每号一次（单副本 `Recreate`，每次 rollout
都是一小段真空）。已装过的号会被逐字节比对后跳过，所以这条命令可以反复跑。

`--apply` 干这些：备份整个 Secret → merge patch → **逐字节回读比对** →
`rollout restart` → 检查 initContainer seed 日志里每个 email 都在 →
**逐号直打上游验证**（不是打网关，见上面量具纪律 1）→ 全池回归。
最后自己再补一刀 `regress`（用户面）。

### 授权环节：一条链接可以喂给所有号

`authurl` **只用跑一次**。那条链接不绑账号 —— 账号由浏览器当时的 Google 登录态决定，
所以换个号（换 profile / 无痕窗）再点一次同一条链接，就多一条 callback URL。
code 是一次性的，链接不是。**100 个号的真实瓶颈是 consent 页要真人点，脚本代替不了。**

### 扩到 100 个号的账（都查过，不是推的）

| 项 | 结论 |
|---|---|
| Secret 容量 | 每份凭据 ~640B，100 个 ≈ 64KB，k8s Secret 上限 1MB —— **够** |
| rollout 次数 | 批量装 = **1 次**。别一号一 `--apply` |
| 各号模型清单不一样 | 不用管。(号×模型) 60s 冷却 + `max-retry-credentials: 0`（每轮试完所有凭据）兜住 |
| `request-retry: 3` | 是「第一轮试完所有凭据之后」再来几轮，**不是只试 3 个号**。不用调 |
| 授权环节 | Google consent 必须真人点浏览器，**这一步没法自动化**，是 100 个号的真实瓶颈 |
| 逐号验证 | `verify --only a@x,b@y` 直打上游，不挪文件不打断在途请求，可分批跑 |

**加号只是扩容，模型清单不变 ⇒ LiteLLM 的 entry 和 key 白名单一个字都不用动。**
网关按 `routing.strategy: round-robin` 自己在多个号之间轮。

### 三个必须知道的坑

1. **老号的 Secret key 是遗留名 `antigravity-auth.json`**，和脚本生成的
   `antigravity-<slug>.json` 不同名。**查重必须按 JSON 里的 `email` 字段，不能按 key 名**，
   否则同一个号会被装成两份 —— 同账号两个 listener 会互相抢 token。脚本里已经有这道门：
   显式 `--creds` 点名到重复号 = 硬拒 rc=1，`--creds-dir` 扫目录扫到 = 跳过 rc=0。
2. **Secret 的 key 名不能带 `@`**，所以落地成 slug；initContainer 照 JSON 里的 `email`
   字段还原成真实文件名 `antigravity-<email>.json`（网关认这个名字）。
   initContainer 是 `for f in /secrets/antigravity-*.json` 的循环 + `sed` 抽 email，
   **不是写死的文件名**（2026-09-08 改的，改前只支持一个号）。
   镜像里有 `sed`/`awk`，**没有** `python3`/`curl`/`wget`/`jq`。
3. `prompt=consent` 不能省，否则复授权拿不到 `refresh_token`。code 只能用一次。

### `:fetchAvailableModels` —— 唯一权威的「这个号有哪些模型」

```
POST https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels
Authorization: Bearer <access_token>
User-Agent: <hub UA>
{"project": "aicode-consumers"}
```

返回里 **`models` 是 dict（modelId → 详情），不是 list** —— 当 list 遍历会拿到一堆字符串
然后 `AttributeError`。每项带 `displayName` / `maxTokens` / `maxOutputTokens` /
`quotaInfo{remainingFraction,resetTime}`。顶层还有 `deprecatedModelIds`、
`webSearchModelIds`、`imageGenerationModelIds`、`tieredModelIds`。
`quotaInfo` 是**按池共享**的，**不覆盖图像模型那个独立小配额**。

**一个号有三个池**（2026-09-10 从 `1632004` 的一次原始响应里逐字段读出来的）：

| 池 | 覆盖哪些模型 | 那次的 `remainingFraction` / `resetTime` |
|---|---|---|
| gemini | **全部** gemini（2.5-flash / 2.5-flash-lite / 2.5-pro / 3-flash / 3.1~3.8-* / flash-image）| 0.6538 / `18:32:05Z` |
| claude+gpt | `claude-opus-4-6-thinking`、`claude-sonnet-4-6`、`gpt-oss-120b-medium` | 0.2312 / `18:13:57Z` |
| tab | `chat_20706`、`chat_23310`、`tab_flash_lite_preview`、`tab_jump_flash_lite_preview` | **1 / 没有 resetTime**（官方文案「Unlimited Tab completions」）|

⇒ **在 gemini 族里换模型换不来一点额度**（同一个桶）。
⛔ 由此作废我 09-10 早些时候「暴露 `gemini-2.5-flash`/`3.5-flash-lite` 来扩容」那条建议。
真正免费的那一格是**把负载挪到 claude/gpt 那个桶** —— 每个号等于多一个独立配额。

⚠️ **`remainingFraction` 是可用的尺子**（这条推翻了更早"它不可信"的说法）：
09-10 实测 `gjimibirrer509` 读数 0.0043 → 打真流量 0/1 且报
`429 Individual quota reached`；读数 ~0.8 的号 1/1。由此得到分诊判据：
**429 + remaining≈0 = 真的用完了；429 + remaining 还很高 = 上游在反代理**
（GitHub CLIProxyAPI #1015 那一类，同账号官方 IDE 能用、走代理全 429）。

⚠️ **`gemini.google.com` 那个「用量限额 X%」页面和 Antigravity 的编码配额不是同一把尺子。**
09-10 同一个号同一时刻：网页 1%、`fetchAvailableModels` 34.6%，重置点差 2.5 小时，
网页还多一个「每周」而 API 没有。用户看网页说"我还有很多余额"、我们这边 429，两边都没错。
（没有官方文档明说网页不含 Antigravity，这条结论**只立在数值和窗口对不上**这个证据上。）


### OAuth 常量（从运行中的二进制抠的，别凭记忆改）

```
client_id     1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com
client_secret 不进仓库（本仓 public）。198 上 /root/cliproxy-manifests/antigravity-oauth-client-secret
              （600，一行 GOCSPX-...），两个脚本都从这里读；也可 export ANTIGRAVITY_CLIENT_SECRET=...
              要重新取值：从运行中的 CLIProxyAPI 二进制里抠，别凭记忆填
redirect_uri  http://localhost:54545/callback
scopes        openid userinfo.email userinfo.profile cloud-platform cclog experimentsandconfigs
auth          https://accounts.google.com/o/oauth2/v2/auth
token         https://oauth2.googleapis.com/token
```

`project_id` 现有号是 `aicode-consumers`。脚本先拿它试 nonce，不中就留空让网关
`FetchAntigravityProjectID` 自己拉 —— **它是不是所有消费者账号通用，没验过**。

<a id="two-paths"></a>
## 🔴 加号是**两条路**，两条都要走一遍

2026-09-09 加三个新号我只走了路①，第二天用户问「为什么我在 sub2api 上看不到」才发现。
**「加完了」这句话，在两条路都跑过 `list` 对上账之前不许说。**

| | 路① `cli-proxy-api` | 路② `sub2api` |
|---|---|---|
| 凭据存哪 | k8s Secret `litellm-dev/cliproxy-secrets` | sub2api Postgres `accounts.credentials`（明文 JSON）|
| 谁在用 | **生产**：LiteLLM 12 条 `ag-*`/`claude-ag-*` entry + ~1488 把 key | 目前只有 group 10 一把 probe key，**零生产流量** |
| 脚本 | `cliproxy-antigravity-add-account.py` | `sub2api-antigravity-add-account.py` |
| 顺序 | **先**（Secret 是凭据源头）| **后**（从路① Secret 读凭据灌进去）|

两边**各自刷 token**。Google 这类 OAuth 通常不轮换 `refresh_token`，所以同刷理论上不冲突；
但「两边同刷同一个 token」是 2026-09-10 才有的组合，**还没跑满一天，别当成已验证的事实**。

⚠️ **撤号/换号同样要两边都动。** 只删一边会留下一条僵尸腿：路①删了、sub2api 还在拿
一个源头已经没有的凭据刷 token，`list` 会把它标成 `🔴 sub2api 有但路①没有`。

### 路② 怎么加（在 198 上，脚本在 `/root/cliproxy-manifests/`）

```bash
S=/root/cliproxy-manifests/sub2api-antigravity-add-account.py
sudo python3 $S list                    # 只读：两边对账，看差哪几个号。加之前先跑这个
sudo python3 $S apply                   # 默认 dry-run，打印将要加的号
sudo python3 $S apply --yes             # 真写：备份 accounts 表 → 建号 → 绑组 → 刷 token → 回读
sudo python3 $S verify                  # 打真流量，按 usage_logs 逐号判活
sudo python3 $S rollback --ids 21,22,23 --yes   # 只删 sub2api 那份拷贝，路①不受影响
```

`apply --yes` 写之前自动把整张 `accounts` 表（含 `account_groups` 绑定）落到
`/root/cliproxy-manifests/sub2api-accounts-backup-<ts>.json`（600）。
回滚 = `rollback --ids`，已有行一个字没改过所以不需要 restore 备份。

### 路② 的坑（都是 2026-09-10 踩出来的，5~7 出自 09-09）

1. **查重按 `credentials->>'email'`，不按账号名。** 和路①同一条纪律：名字是人取的会漂。
   显式 `--only` 点到已存在的号 = 硬拒 rc=1。
2. **创建响应里的 `group_ids` 是 `None`，但组其实绑上了。** 判据只认建完再 `GET` 一次。
   据响应判「没绑上」然后手工再绑一次 = 绑两遍。
3. **建完必须紧跟一刀 `batch-refresh`。** Secret 里的 `access_token` 是陈旧快照
   （`expired: 2020-01-01`），脚本故意把 `expires_at` 写成过期值逼它换新。
   不刷的话 `extra.privacy_mode` 停在 **`privacy_set_failed`**，刷完才变 `privacy_set`。
4. **`batch-refresh` 的参数名是 `account_ids` 不是 `ids`**，传 `ids` 返 400 `account_ids is required`。
5. **UA 在 sub2api 里由一个「全局设置」管，不是账号字段。**
   管用的是 `GET/PUT /api/v1/admin/settings` → `antigravity_user_agent_version`
   （09-09 起 = `"1.0.0"`，**对这台上所有 antigravity 号一起生效**）。
   账号 `extra.antigravity_user_agent_version` **是个同名死字段，写了不生效**
   （09-09 实测：只写它仍 403，改全局才通）。脚本仍然写它，只为和现有号形状对齐。
   ⇒ 将来要在 sub2api 上加一个「已过验证、想吃 33 模型新目录」的号，**做不到** ——
   全局设置只有一个值，改了会把其它五个号一起推到新 UA 上。
6. **`error_message` 会留着不清。** id 15/16 至今挂着 09-09 的
   `Validation required (403)`，但 09-10 实测它俩照样接单成功。
   **那个字段是历史残留，不是当前状态**，别拿它判死活。
7. **账号被摘后要清两个字段**：`status` 和 `schedulable`，调度器看后者，
   且 `schedulable` 有专用端点（`PUT` 里带它会被静默忽略）。见
   [[project_sub2api_antigravity_onboard_403_2026_09_09]]。

### 路② 判活：`usage_logs.account_id` 是唯一的逐腿尺子

sub2api 也是池子，**池子级 nonce 全绿可能是一条好腿在扛全部**（和路①纪律 1 同形）。
逐腿判据只有 `usage_logs.account_id` 这一张表。两个必须知道的量具缺陷：

- **单轮小样本会出假红。** sub2api 按「谁用得少先给谁」补：09-10 实测第一轮 12 发
  有三个号一发没接，第二轮 24 发全落到那三个号上。所以 `verify` 默认发 **4×号数**，
  且**两轮都 0 发**才值得单独查（那时用路①的 `--only <email>` 直打上游）。
- **切口必须用 `max(usage_logs.id)` 水位线，不许用时间窗。** 按
  `created_at > now()-interval` 统计会把**上一轮 verify 的请求**算成本轮的，
  把 0 发的腿显示成有单 —— 数字和上一轮一字不差就是这个病。第一版就栽在这。

### 路② 现状基线（2026-09-10 收盘）

6 个号全在，都绑 group 10 `ag-gemini-probe-s48`，`verify` 24 发 **24/24 nonce、6 条腿全接过单**。
组 10 只挂一把 key `s48-ag-probe`，**这条路目前不承载生产流量** —— 要让它真跑起来还得
建正式组 + 铺 key + 在 LiteLLM 加 `sa-ag-*` entry，那是另一件事。

⛔ **别指望在 sub2api 上复刻路①的 `model_aliases`。** 它的 antigravity 模型目录（29 个）
和 default-model-mapping（41 条）都是**编译进二进制的只读表**，里面没有 `gemini-3.8-flash`；
账号 `extra.model_mapping` 写得进去也回读得到，但**从不被读**。打 3.8 得
`404 Model ... is not supported by any configured account in this group`。
唯一出路是升级镜像 —— 而这个镜像同时扛着 kimi 和 grok 的生产流量。
**3.8 走路① 已经解决了，不需要动 sub2api。**
细节见 [[project_sub2api_antigravity_onboard_403_2026_09_09]]。

## 加/改 LiteLLM 侧的 entry

只有网关暴露了新模型时才需要动。现状 11 个上游模型 / 12 条 entry
（多的一条是 `claude-ag-gemini-3.1-pro`，因为 Claude Code 的选择器只列名字里含 `claude` 的）。

- provider 前缀 **`openai/`**，`api_base` 指向 svc，`model_info.id` = `cliproxy/<name>`。
- 一条 chat entry 同时服务 `/v1/chat/completions`、`/v1/responses`、`/v1/messages`。
- **窗口字段住 `model_info`，计价字段住 `litellm_params`。**
- ⛔ **`POST /model/update` 写不进 `model_info` 的窗口字段**：返 200、回读 `None`。
  唯一正解是 `/model/delete`（按 `model_info.id`）+ `/model/new` 重建。
- **写完约 90~130s 才在 4 个副本上收敛。** 8s 就探会拿到 `ProxyModelNotFoundError`，
  看起来像 entry 写坏了 —— **别据此回滚**，等 2 分钟重打。
- 模型清单**向网关 `GET /v1/models` 现拉，别写死**（Google 一直换名字）。
  ⚠️ 没有 `gemini-3.8-flash-lite`，flash-lite 只有 3.1 这一代。

## 排障

| 症状 | 先查什么 |
|---|---|
| 网关起不来 | `kubectl logs <pod> -c seed` —— 凭据没铺进去会直接 FATAL |
| 全部 429 | 是不是打到了 `cloudcode-pa`（prod）而不是 daily |
| 某个新号单独跑不通、403 `VALIDATION_REQUIRED` | **账号没过 Google 验证**（不是订阅、不是模型）。把 403 的 `details[].metadata.validation_url` 挖出来给账号持有人在浏览器里点完验证。钉 `"user_agent": "antigravity/1.0.0 windows/amd64"` 只是止血（绕过校验但降级到 27 个模型的旧目录），验证过了要**摘掉**再 install |
| 某个号「缺某个模型」/ 404 `Requested entity was not found` | **先排掉 UA 再说账号**：`1.0.0` 只给 27 个模型、`2.12.2` 给 33 个。跑 `--quota --ua 'antigravity/2.12.2 windows/amd64'` 跨号统一后再比（`--quota` 不花额度）。09-08 我就是没统一 UA，把 UA 的效果误报成「新号缺模型」 |
| 图像模型 429 `exhausted your capacity` | 图像有独立小配额，`quotaInfo` 不上报。等 reset（报文里有剩余时长）。**别拿探针再去打它** |
| LiteLLM 报 `API 异常 (req: <id>)` | **198 的 `error_sanitize` 换掉了真错误**。客户端拿到的报错文案在 198 上不是证据，要拿 req id 去所有 litellm-proxy 副本 `grep "error_sanitize: masked req=<id>"` |
| 403 `key_model_access_denied` | key 白名单没加这个模型（198 是 strict 模式）|
| 实收 spend 和按官方价算对不上 | 先怀疑我的公式：198 有全局 ×1.3（`pricing_overlay.py:32`）；Gemini 的思考 token 计费但不进 `completion_tokens` |
| **整条线突然全断**（三个号一起没） | 先看 pod 起没起来：`replicas=1`，pod 挂 = 全断。09-09 那次是节点 `fs.inotify.max_user_instances=128` 打满，症状是 CrashLoop + 误导性的 `too many open files` |
| 某把 key 打**公开名**（如 `gemini-3.8-flash`）报 400 | 这名字不是真实组，只能靠 per-key `aliases` 改写。`models` 里有名字但 `aliases` 里没有 ⇒ 400。查：`aliases ? 'gemini-3.8-flash'`，不是 `= any(models)` |
| 探针全绿，但心里没底 | **跑阴性对照**：`--no-alias`（钉 UA 的号 3.8/3.7 必 404）或 `--models gemini-9.9-does-not-exist`。红不了的尺子，绿不作数 |
| 「我在 sub2api 上看不到这几个号」 | **加号只走了路①**。`sub2api-antigravity-add-account.py list` 对账，`apply --yes` 补齐。见 [两条路](#two-paths) |
| sub2api 里某个号 `privacy_mode=privacy_set_failed` | 它的 `access_token` 还是陈旧快照。`POST /api/v1/admin/accounts/batch-refresh {"account_ids":[...]}`（**参数名不是 `ids`**），刷完自动转 `privacy_set` |
| sub2api 里某个号挂着 `Validation required (403)` 但其实在接单 | `error_message` 是历史残留不会自动清，**不是当前状态**。判死活看 `usage_logs.account_id` |
| sub2api `verify` 有几个号 0 发 | 调度器按「谁用得少先给谁」补，单轮小样本必然有号轮空。**再跑一次**；两轮都 0 发才单独查 |

## 相关记忆

- [[project_antigravity_daily_endpoint_unblocks_gemini_pro_2026_09_07]] —— 现场全档案（模型表/价格表/窗口/09-09 三层验证）
- [[feedback_endpoint_host_is_the_variable_not_egress_region]]
- [[feedback_two_variables_changed_dont_credit_the_one_you_care_about]] —— UA×账号那次误归因
- [[feedback_pool_level_nonce_is_false_green_for_one_member]] —— 三层判据的底座
- [[feedback_node_inotify_instances_128_crashloops_watcher_pods]] —— `replicas=1` 那个单点真被触发过
- [[feedback_api_silently_ignores_unknown_field_returns_200]] —— `/model/update` 那个坑
- [[feedback_spend_mismatch_check_the_ruler_before_the_config]]
- [[topic_ruler_failure_shapes]] —— 量具会坏的形状总表
- [[feedback_antigravity_account_must_be_added_to_both_paths]] —— 两条路那次漏加
- [[project_sub2api_antigravity_six_legs_2026_09_10]] —— 路② 六条腿的现场记录
- [[feedback_antigravity_gemini_models_share_one_quota_pool]] —— 三个池子，换 gemini 名字换不来额度
- skill `sub2api-grok-ops` —— sub2api 本身的运维（admin API / 账号 / 组 / 并发闸）
- skill `litellm-198-key-allowlist` —— 改 key 的 `models`/`aliases`（公开名那一跳住在这里）
