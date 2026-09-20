---
name: her-xai-video-generation
description: >-
  给 her 实例开通 / 排障 **xAI grok-imagine 视频生成**：198 上的 `xai-vidshim.service`
  为什么必须存在（openclaw 不发 `storage_options` ⇒ xAI 只给短命 ephemeral 地址，而
  openclaw 只读 `video.url`）、鉴权翻译为什么能免掉重建容器（openclaw 只认 env 来源的
  secret ref）、以及**「飞书发消息没反应」在视频这条路上是预期形状不是故障**
  （`video_generate` 是异步工具，当场 `sessions_yield` ⇒ `replies=0`，视频 ~100s 后
  以另一条消息投递）。含 6 个会把成功读成失败的坏尺子。
  Use when the user mentions her 实例生成视频 / 视频没反应 / grok-imagine-video /
  vidshim / videoGenerationModel / 视频发不出来 / replies=0 / 图片还是 gpt 为主。
---

# her 实例 xAI 视频生成：开通、验收、排障

> 作用域：her 实例（13 / 14 / 75 已开通），边缘在 **198**。
> **图片不看这篇** —— 图片是 `litellm/image-2` 当 primary、grok imagine 两个只做
> `fallbacks`，那条链没经过 shim。动图片看 [[her-public-model-name]]。

脚本：`scripts/her-video-config-patch.py`（默认干跑，`--apply` 才落地且先备份）
产物：`.claude/skills/her-xai-video-generation/files/`（shim 本体 + systemd unit + drop-in）

## 0. 一句话架构

```
her 容器 openclaw
  └─ providers.xai.baseUrl = https://cc.auto-link.com.cn/xaivid-9f4c2e7a1b/v1
     apiKey = ${CARHER_PROD_KEY}          ← 实例本来就有的，当门票用
        ↓ 公网 hostname（必须，见 §1 第 3 条）
     198 nginx :80 → location ^~ /xaivid-9f4c2e7a1b/v1/videos
        ↓
     xai-vidshim.service :18098
        ├─ 去程：补 storage_options
        ├─ 鉴权：门票 sha256 对白名单 → 换成 /etc/xai-vidshim/upstream.key
        └─ 回程：file_output.public_url → video.url
        ↓
     sub2api 10.43.97.195:8080 → x.ai
```

## 1. 为什么每一层都不能省

**shim 不能省。** openclaw 从不发 `storage_options`，xAI 就只返回**短命的 ephemeral
相对地址**；`file_output.public_url`（永久、免鉴权）只在发了 `storage_options` 时才出现。
而 openclaw 的 `readXaiStatusResponse` **只读 `video.url` 这一个字段**。两边对不上，
所以必须有人去程注入、回程搬字段。

**鉴权翻译不能换成"给容器发个新 key"。** openclaw 的 `coerceSecretRef`
（`secret-input-B4ViYdFq.js:64`）只解析 env 来源——`${VAR}` 模板和 legacy env marker，
**没有 `file:` 来源**；auth profile 存在 **SQLite**（`resolveAuthProfileDatabasePath`），
不是能手写的 JSON；`openclaw models auth paste-api-key` 会试图回写配置，被 `$include`
布局挡死（`Config write would flatten $include-owned config`）。
⇒ 想给 openclaw 一个新凭据**只能加 env var，而加 env var 就要重建容器**（生产动作）。
所以改成：实例出示它已有的 `CARHER_PROD_KEY`，shim 校验 sha256 白名单后换上游 key。
**纯配置改动 ⇒ openclaw 热加载 ⇒ 不重建容器。**

**必须走公网 hostname，不能走 podIP/内网。** `generateVideo` **硬编码**
`allowPrivateNetwork: false`，配置覆盖不了（`false ?? x === false`），而且拒绝发生在
**创建那个 POST** 上。

**`mediaGenerationAutoProviderFallback: false` 不能省。** 默认开启时它会静默追加别家
provider 的 `defaultModel`（实测追加过 `openai/sora-2`、`openrouter/google/veo-3.1-fast`）
—— 等于把请求和凭据发给别的厂商。

## 2. 开通一个新实例

```bash
# 1) 门票入白名单：拿该实例 CARHER_PROD_KEY 的 sha256（禁回显明文）
docker exec <容器> sh -lc 'printf %s "$CARHER_PROD_KEY" | sha256sum'
# 2) 追加到 198 的 drop-in，然后 systemctl restart xai-vidshim
#    /etc/systemd/system/xai-vidshim.service.d/auth.conf 里 VIDSHIM_ALLOWED_SHA256
# 3) 打配置（先干跑看 diff，再 --apply）
python3 scripts/her-video-config-patch.py <runtime.json5 绝对路径>
python3 scripts/her-video-config-patch.py <runtime.json5 绝对路径> --apply
```

配置落地后**不用重启容器**，openclaw 自己热加载
（`server-reload-handlers-C41Iem2T.js` 的 `buildGatewayReloadPlan`）。

## 3. 验收（读回 + 真跑，两样都要）

读回**必须显式给配置路径**，否则尺子是坏的（见 §4）：

```bash
docker exec -e OPENCLAW_CONFIG_PATH=/data/.openclaw/openclaw.json <容器> \
  openclaw config get agents.defaults.videoGenerationModel.primary
```

真跑（CLI **没有 `--local` 这个 flag**，给了会被拒）：

```bash
docker exec <容器> openclaw capability video generate \
  --prompt "<带唯一 nonce 的提示词>" --duration 4 --resolution 480p \
  --aspect-ratio 16:9 --output /tmp/vt.mp4
```

判成功要**三样一起**，缺一样就不算：
1. 文件字节数 > 0 且头部是 `ftypisom`（`200 空壳`会骗人）
2. shim journal 里该轮有 `injected=True … auth=ok` 和 `promoted=True`
3. 输出里 `provider: xai / model: grok-imagine-video-1.5`

## 4. 会把成功读成失败的坏尺子（都踩过）

**🔴 `replies=0` / 「飞书发消息没反应」是预期形状，不是故障。**
`video_generate` 是**异步工具**：主回合调完它当场返回
`Background task started … async:true`，紧接着 `openclaw.sessions_yield`
**主动让出这一回合** ⇒ 主回合**本来就没有正文可回**，于是
`dispatch complete (queuedFinal=false, replies=0)`。视频在 ~100s 后由完成事件
**以另一条独立消息**投递。判「到底发没发出去」只认这一行：
```
feishu[...]: media sent: messageId=om_xxx      ← 飞书回了新消息 id 才算送达
```
⛔ 别拿 `replies=0` 当故障证据；⛔ 别拿同一时刻的
`phase transition rejected (from=idle, to=completed)` 当「卡片吞掉了正文」——
查过会话 jsonl，那一轮 assistant 内容里**只有 toolCall、压根没有正文**。

**`openclaw config get` 在容器里对所有路径都报 `Config path not found`** ——
以 root 跑时 CLI 解析 `$HOME/.openclaw`（不存在）。这是尺子坏了不是配置没落地，
判据：连**已知good**的 `imageGenerationModel.primary` 也读不出来。
修法：`-e OPENCLAW_CONFIG_PATH=/data/.openclaw/openclaw.json`。

**图片这一轮走了 x 的 fallback ≠ 我把 primary 改坏了。** 曾经见到一次
图片没用 `litellm/image-2`；显式 `--model litellm/image-2` 正常、改前的备份配置
也挑 `image-2`、重跑两次都挑 `image-2` ⇒ 是 primary 瞬时失败被兜底吸收。
容错会把「首选偶尔失败」藏成零症状，见 [[feedback_probe_rotation_hides_permanent_first_choice_waste]]。

**`--output x.png` 落地成 `.jpg`** ⇒ 按原名 `ls` 会报 NO_FILE。按目录看，别按名字看。

**`\${CARHER_PROD_KEY}`（多一个反斜杠）** env 引用直接失效。经多层 ssh 引号传递时
极易发生；脚本里已有 `assert "\\${" not in out` 挡住。

**LiteLLM `/sa-video` passthrough 的 403 与本路径无关**，别拿它当 shim 的证据。
passthrough 也**不进 SpendLogs**。

## 5. 动了啥 / 备份在哪 / 怎么回滚

| 动过的东西 | 位置 | 回滚 |
|---|---|---|
| 三份实例配置 | 188: `carher-14`,`carher-75`；186: `carher-13` 的 `openclaw.runtime.json5` | `cp` 回同目录 `.bak-vidgen-*`（热加载，不重启） |
| shim + unit | 198 `/opt/xai-vidshim/`、`/etc/systemd/system/xai-vidshim.service{,.d/auth.conf}` | `systemctl disable --now xai-vidshim` |
| nginx 一个 location | 198 `/etc/nginx/sites-enabled/cc.auto-link.com.cn.conf` | `cp` 回 `.bak-xaivid-20260920T200654` → `nginx -t` → `nginx -s reload` |
| 上游 key | 198 `/etc/xai-vidshim/upstream.key`（0640 root，**不在** unit 的 `Environment=`，`ps` 看不到） | 轮转即改文件 + restart |

## 6. 两个必须告诉用户的事实

- **生成出来的视频链接是无鉴权公开地址**，谁拿到谁能下载；`expires_after=604800`
  （7 天）过期，这同时也防 xAI Files 配额涨满。
- 模型自己填参数时可能挑 **8 秒 16:9**（比验收用的 4s/480p 贵）。

## 7. 已知隐患（与本功能无关但同一份日志里）

- `hermestest-14` 内存告警：`rss=2.5 GiB / 阈值 1.5 GiB = 166.5%`，持续中。
- 实例侧 `MEMORY.md is 25769 chars (limit 20000); truncating` —— 注入上下文被截断，
  与 [[feedback_memory_index_over_limit_silently_drops_the_tail]] 同病，只是发生在实例侧。
