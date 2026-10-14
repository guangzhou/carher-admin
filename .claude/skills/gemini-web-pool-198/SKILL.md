---
name: gemini-web-pool-198
description: >-
  Google AI Pro 号的 gemini.google.com 网页 → 198 两个网关 → LiteLLM 的全套运维：
  `gemini-web`（Gemini-FastAPI，多号池，聊天 gemini-4-argon / 3.1-pro / 3.8-flash-web / 3.5-flash-lite）
  与 `gemini-media`（gemini-web-to-api，单号，生图 / 音乐 / 视频 / Deep Research）。
  Use when 用户要「gemini-4 / Argon 怎么开」「网页能用 API 不能用」「加个 gemini 号 / 给卡密加到池子」、
  问「这个号能用什么模型」、要 gemini 出图/音乐/视频/深度研究、gemini-4 在 Claude Code 里
  「API error · Retrying」/ 空回复 / 不调工具 / 说"我无法访问你的电脑" / 不是流式一下子出来、
  cookie 过期、额度 0/2400、升级或重建这两个网关。
  含：为什么 Argon 只能走网页（Antigravity 只给 Ultra）、一条命令全自动加号（188 浏览器 + TOTP）、
  本地补丁 p1~p3.2 及其踩坑、LiteLLM 接法里四个必踩的配置坑、工具调用天花板约 8 成。
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
   └─> litellm-dev/deploy/gemini-media :4981  gemini-web-to-api@681ca80 + 越界补丁        单号
         env: Secret gemini-media-env(GEMINI_COOKIES)；**无鉴权** ⇒ NetworkPolicy gemini-media-ingress 只放 litellm-product
         LiteLLM: web-gemini-image / web-gemini-music / web-gemini-video + pass-through /gemini-deepresearch
```

198 文件：`/root/gemini-web-build/`（config.yaml、accounts/*.json|*.cookies.json、api_key，全 root 600）、`/root/gwt-build/`。
🔴 **两个网关必须用同一个号的两份独立登录**：共用一份 cookie 会互相轮换 1PSIDTS 把对方踢掉。

**规模（10-15 00:40 现量，别照抄，跑 `kubectl -n litellm-dev logs deploy/gemini-web | grep "clients initialized"` 现看）**：
gemini-web 52 号（jio + agoro + 10-14 夜 50 张卡密全进）；同 50 号也进了 cli-proxy-api（Secret 60 个 `antigravity-*`）与 sub2api（组 10，id 到 ~130）。
同一出口 IP（Linode 大阪）一晚登 50+ 号未见风控（abuse=Clean、无验证码墙）；但当晚 `gemini-3.1-pro`/偶发 3.8-flash 回 Google 通用失败文案（I'm having a hard time / Sorry, something went wrong），**argon 同时正常** —— 用户定性「3.1 Pro 老模型不用管」，未查因。

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
- Antigravity consent 有一页「Make sure that you downloaded this app from Google」，按钮叫 **Sign in**；且 URL 里带 `totp` 字样，TOTP 判定要同时看输入框存在。
- Antigravity 新号 hub UA 报 403 VALIDATION_REQUIRED；`google_validate.py` 能走完验证页（有时显示 Authentication successful、有时 Google 回 500）**都没用** ⇒ 照老号钉 `1.0.0` UA + `gemini-3.8-flash-tiered→high` alias。50/50 新号都是这样装进去的。

`gemini-media` 不在脚本里：它只吃一个号。换号 = 188 跑一次 `google_login.py`（用一个新的 name 拿独立 session）→ 改 Secret `gemini-media-env` 的 `GEMINI_COOKIES` → rollout restart。

## LiteLLM 接法：四个必踩的坑

1. **前缀用 `custom_openai/`，不能用 `openai/`**。`openai/` 会让 LiteLLM 把 `/v1/messages`（Claude Code）桥到 `/v1/responses`，而 gemini-web 的 responses 端点不认扁平 tool 形状 ⇒ 带工具必 **422** ⇒ Claude Code「API error · Retrying」。
2. **窗口字段只放 `model_info`**。`max_input_tokens` 写进 `litellm_params` 会被 custom_openai 原样透给 SDK ⇒ `unexpected keyword argument` ⇒ **500**。
3. **不传 `model_info.mode`**（聊天条目）。见 [[feedback_model_new_mode_poisons_shared_backend_key]]。
4. **视频**：`GET /v1/videos/{id}` 从 id 反解 `model_id` 再按**私有组名**验权 ⇒
   - key 的 `models` 里必须**同时**有公开名 `gemini-video` 和私有名 `web-gemini-video`；
   - 各媒体条目的上游 `model` **不能同名**（image=`gemini-3.8-flash`、video=`gemini-advanced`、music=`gemini-3.8-flash-music`），否则反解到别的组 ⇒ 403。
   Deep Research 走 pass-through，key 要 `metadata.allowed_passthrough_routes:["/gemini-deepresearch"]`（per-key 白名单管不到 pass-through）。

建条目照抄现网：`/model/info` 里 `gemini-web/*`、`gemini-media/*` 两组 `model_info.id`。开给某把 key 用 skill `litellm-198-key-allowlist`（`--add-model X --alias X=web-...`，自带 backup + 回读）。
⚠️ std key 上原有 `gemini-3.8-flash`（走 Antigravity），网页版另起名 `gemini-3.8-flash-web`，别覆盖。

## 本地补丁（`scripts/gemini-web/patches/`，`apply.sh fastapi|media` 从上游重建）

| 补丁 | 解决什么 |
|---|---|
| p1 namedmodel | 上游自定义模型走 `Model.from_dict`，gemini_webapi 2.1 已删 ⇒ 全 500。改成传名字串，由库按账号实时模型表解析（Argon 内部 id `95a4c0abcba24364`） |
| p2 pool_failover | 跳过 quota remaining=0 的号 + 冷却表；新会话遇限额/封禁/超时换号重试；流式先拉首块再交出 |
| p3 tool_nudge | 带 tools 的 chat 先非流式拿全文；无 tool_calls 且像推脱（「无法访问」「你可以运行」「结构如下」…）⇒ 追加强制提示重发 |
| p3.1 as_dict | 🔴 内部返回的是 pydantic 对象不是 dict ⇒ p3 原版**从不触发、带工具的流式回成整块 JSON ⇒ LiteLLM 读空 ⇒ Claude Code 15/15 空回复** |
| p3.2 | 正则补 `you can share` / `To view…you` / `I don't have access`，最多重试 2 次 |

`apply.sh fastapi` 在干净 checkout 上重放 p1→p3.2 与现网 `src/app` **逐字节一致**（10-14 已验）。

## 能力与边界（10-14 实测）

- **工具调用天花板 ≈ 8 成**：Claude Code 线型 5×3 回归两轮都是 12/15（nonce 5/5、主动调工具 4/5、tool_result 续答 3/5），重试救回约 2/3。剩余失败是模型自称「作为 Claude Code…虚拟沙盒」直接编目录 —— 和正常总结同形，正则不能再放宽。
  已证伪的假设（别再测）：末轮追加提醒、系统提示含「You are Claude Code」、流式 vs 非流式、system 字符串 vs 列表 —— 都不是变量。根因判为网页消费者人设与提示词模拟工具协议冲突。
  ⇒ 适合聊天/轻工具；Claude Code 重度 agent 不稳。真稳 = Ultra 号进 Antigravity 或 AI Studio key。
- **带工具时不是流式**：p3 为了判断推脱要先攒全文，整段一次出。不带工具仍真流式。待做：只攒开头 ~200 字再转实时流。
- **延迟**：短问 2~3s、80~150KB Claude Code 体量 4~7s、冷启动 12s；经 LiteLLM ≈ 直打网关。
- **额度**：新号 **2400 credits / ~5h 窗**，Flash/Pro/Argon 共池；一轮 5×3 Claude Code 回归≈几百 credits。10-14 夜 jio/agoro 两个老号的日志读数变成 `/48384`（原因未查：库换口径 or 档位不同）。视频另有很小的日额度（`quota_exceeded`）。p2 会跳过 remaining=0 的号，52 号池子下单号耗尽基本无感。
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
- 网关镜像：`set image deploy/gemini-web gemini-web=127.0.0.1:5000/gemini-fastapi:20261014-1731445-p2`（p2 无工具重试，最后一个无 p3 的版本）
- 删整套：`kubectl -n litellm-dev delete deploy/gemini-web deploy/gemini-media svc/gemini-web svc/gemini-media networkpolicy/gemini-media-ingress`

## 相关

skill `cliproxy-antigravity-ops`（同号进 Antigravity）· `litellm-198-key-allowlist` · `litellm-passthrough-endpoint` · `muse2api-pool-ops`（同类网页反代）·
记忆 [[project_198_gemini_web_argon_live_2026_10_14]] · [[project_gemini4_argon_no_upstream_carries_it_2026_10_01]]