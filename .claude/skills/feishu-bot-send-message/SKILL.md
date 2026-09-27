---
name: feishu-bot-send-message
description: >-
  用一个**自建飞书 App**（app_id + app_secret，不走 lark-cli、不走 her 实例）
  给指定的人或群发消息：邮箱 / user_id / union_id / per-app open_id / chat_id
  五种寻址，text 与 markdown 卡片两种内容。Use when the user says
  "给 XXX 发个飞书消息 / 用我的机器人发消息 / 写个程序发飞书 / bot 发消息给某人 /
  发到某个群 / 飞书通知脚本", or when a script needs to push an alert into Lark
  with its own bot credentials. 核心是**收件人寻址**这一关：open_id 是 per-app 的，
  别的 app 查来的 `ou_` 塞进来必定 `99992361 open_id cross app`；而"这个 app 没有
  contact 权限"这句话要分清 `batch_get_id`（要 scope）和 `users/{id}` 单查（不要 scope）。
  区别于 `carher-feishu-bot-direct-install`（把发消息能力装进 her 实例、含建文档建表格）
  和 `backend/feishu_bridge.py`（Admin 后端里的入站事件 + 按 chat_id 回消息）——
  本 skill 是**独立 CLI 的出站发送**。
---

# feishu-bot-send-message — 自建 App 给人/群发飞书消息

脚本：`scripts/feishu_send_message.py`（本仓，已入 git）

```bash
export FEISHU_APP_ID=cli_xxx
export FEISHU_APP_SECRET=xxx          # 只从 env 读，脚本里没有兜底默认值

python3 scripts/feishu_send_message.py --to zhangkairui@auto-link.com.cn --text "hello"
python3 scripts/feishu_send_message.py --to zhangkairui --email-domain auto-link.com.cn --text "hi"
python3 scripts/feishu_send_message.py --to 4ae52ag1 --to-type user_id --text "hello"
python3 scripts/feishu_send_message.py --to oc_xxx --markdown-file note.md
python3 scripts/feishu_send_message.py --to 4ae52ag1 --resolve-only   # 换本 app 视角 open_id
```

退出码：`0` 成功 / `2` 参数或收件人解析错 / `3` 鉴权或权限失败 / `4` 发送失败。

## 收件人寻址：先选对 id 种类，再谈权限

| 你手里有 | 怎么发 | 要不要 contact scope |
|---|---|---|
| 企业邮箱 | `--to a@b.com`（`receive_id_type=email`） | **不要** ← 首选 |
| `user_id`（8 位 base36，如 `4ae52ag1`） | `--to 4ae52ag1 --to-type user_id` | 不要 |
| `union_id`（`on_`） | 直接传，自动识别 | 不要 |
| **本 app 视角**的 `open_id`（`ou_`） | 直接传 | 不要 |
| 别的 app 的 `open_id` | ❌ 不行，见下 | — |
| 群 `chat_id`（`oc_`） | 直接传 | 不要（机器人得在群里） |
| 只有中文姓名 | 拼邮箱，或退回"扫机器人所在群的成员名单按姓名匹配" | 不要 |
| 手机号 | ❌ `receive_id_type` 根本没有 mobile 这一档 | — |

### 🔴 open_id 是 per-app 的

同一个人，在每个 app 眼里 `open_id` 不同。`lark-cli contact +search-user` 返回的
`ou_xxx` 是 **lark-cli 那个 app 的视角**，塞进你自己的 app 会直接：

```
[99992361] open_id cross app
```

2026-09-27 实测（张凯瑞）：lark-cli app 看到 `ou_df3b6983…`，另一个自建 app 看到
`ou_f23cfc94…`。跨 app 稳定的只有 `user_id` / `union_id` / 邮箱。
背景见 memory `[[feedback_per_app_open_id_workaround]]`、`[[feedback_larkcli_api_for_union_id]]`。

### 🔴 "这个 app 没有 contact 权限"要分两种接口说

同一个自建 app、同一个 tenant token，2026-09-27 实测：

| 接口 | 结果 |
|---|---|
| `POST /contact/v3/users/batch_get_id`（邮箱/手机 → id） | ❌ `99991672 需要 contact:user.id:readonly` |
| `GET /contact/v3/users/{id}?user_id_type=user_id\|union_id\|open_id` 单查 | ✅ `code 0` |

所以看到 `batch_get_id` 报 99991672，**不能推出"这 app 查不了人"**——单查这条路是通的，
`--resolve-only` 就是走它，把 `user_id`/`union_id` 换成本 app 的 `open_id` 并强制
round-trip 反查（跨租户会在 round-trip 这一步暴露，而不是等发消息时报看不懂的错）。

## 内容形态

- `--text` → `msg_type=text`
- `--markdown` / `--markdown-file` → `msg_type=interactive`，card schema 2.0 的单个
  `markdown` 元素。飞书没有裸 markdown 消息类型，必须包成卡片。
- `--uuid` 幂等键：同一 uuid 重发不会重复投递，适合脚本重试。

## 验收（2026-09-27 实跑，全部有 message_id）

| 路径 | 结果 |
|---|---|
| `tenant_access_token` | ✅ `code 0` |
| email → 私聊 text | ✅ `om_x100b64736deffca8b171f25fb3533ec` |
| `chat_id` → markdown 卡片 | ✅ `om_x100b64736d3e9cb0b393a901d07c24c` |
| `user_id` → 私聊 text | ✅ `om_x100b64bac259f0a0c124817cf1b5ab4` |
| 跨 app `open_id` | ✅ 如期 `99992361`，exit 3 |
| 手机号 | ✅ 如期拒绝，exit 2 |
| 缺 `--text/--markdown` | ✅ exit 2 |

## 踩过的坑

1. **`user_id` 不是 hex**。`4ae52ag1` 带 `g`——是 8 位 base36。按 `[0-9a-f]{8}` 写正则会
   让自动识别静默失手，回落到"按姓名找"然后报"找不到这个人"，症状和权限问题同形。
   现在的判据是 `^(?=.*\d)[0-9a-z]{8}$`（要求含数字，免得 8 字母的英文名被误判），
   拿不准就 `--to-type` 显式指定。
2. **zsh 里 `UID` 是只读变量**。`UID=$(...)` 会 `failed to change user ID: operation not permitted`，
   和飞书毫无关系。换个变量名。
3. **飞书业务错误走 HTTP 4xx**，body 里才有 `code`/`msg`。只看 HTTP 状态码会把
   "权限不足"读成"网络挂了"。脚本在 `HTTPError` 分支里照样解析 body。
4. **别把 app_secret 写进脚本兜底默认值**——那等于提交凭据。只从 env 读；
   secret 一旦在对话/日志里出现过，轮转是唯一真解，摘掉明文不消除暴露。

## 别跑偏

- 要让 **her 实例自己**能建文档/表格/发消息（零 OAuth）→ `carher-feishu-bot-direct-install`。
- 要在 **Admin 后端**里收飞书事件并回消息 → `backend/feishu_bridge.py`（异步，按 chat_id）。
- 要用**你自己的账号身份**（user_access_token）操作 → `lark-cli` + `lark-im` skill，不是本 skill。
