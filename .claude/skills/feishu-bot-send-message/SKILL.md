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
  **群发给多维表里的人 / 群发页 / 198:30930 / feishu-broadcast** 也走本 skill 的「群发页」一节。
  **给每个 Cursor Key 持有人发「模型权限已更新」/ 按 key 逐人生成卡片 / cursor key 通知**
  走「按 key 逐人群发卡片」一节（`scripts/cursor-key-notice-build.py`）。
  **发不出去 / 230013 / 跨租户 / 外部公司的人收不到** 先读「跨租户」一节——那是平台边界不是权限没开。
  **卡片排版 / 下划线 / 背景色 / 字号 / 配色违和** 读「卡片排版」一节。
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
- `--card-file x.json` → 原样发一张完整 schema 2.0 卡片（要 header / 按钮 / 分栏时用这个）。
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

## 群发页（多维表里的人）

**线上 <http://10.68.13.198:30930/>**，ns `feishu-broadcast`，源码 `broadcast/`，
清单 `k8s/feishu-broadcast.yaml`（文件头有部署、改 Secret 的命令），完整说明 `broadcast/README.md`。

- **bot 读不到行时返回 `code 0` + 0 行，不报错，而且有两个原因现象一样**（2026-10-01 两个都踩到了）：
  ① bot 不是协作者：连字段都读不到；
  ② **bot 已经是「可阅读」协作者，但表开了高级权限**：表和字段都能读，行被角色的行规则滤光
  （这张表「角色3」的规则是「提交人 包含 …」，bot 没提交过任何一行）。
  区分方法：先查字段接口能不能读，再看 `GET /bitable/v1/apps/<token>` 的 `is_advanced`。
  ⛔ **「已加协作者」不等于「能读到行」**。我第一版的红条把两种情况都说成「去加协作者」，
  结果用户加完之后又白等了一轮。现在页面会按 `diagnose_empty` 分开提示。
  ② 的解法要改共享表的权限，**由用户决定**。✅ 10-10 已定：bot 升「可管理」（`lark-cli drive +member-add
  --member-type appid --perm full_access --yes`），因为**「单建只读角色」走不通——应用加不进自定义角色**
  （`app`/`appid`/`app_id`/`bot` 四种写法全 `MemberNotFound`，同接口加真人能成，阳性对照已撤）。
  升之前查过 `perm=view` 的成员只有 2 个、都是用户自己的 app。回滚：同命令 `--perm view`。
  系统「阅读者」角色自带行筛选（`CreatedUser 包含` 或 `姓名 包含`），可阅读协作者就是被它滤光的。
- 判 bot 能不能读表，**现跑** `scripts/feishu-bot-base-access-check.py --wiki <token> --table <tbl> --view <vew>`
  （只读，四层 token/元信息/字段/行，加上用户身份对照）：退出码 0 = bot 行数等于你本人的行数，
  1 = 读不全且会说是哪一层，3 = 量具自己坏了（换不到 token / 对照读不出来），这时候结论不可信。
  ⛔ **别拿记下来的行数当验收线**：同一视图 10-01 上午 1391 行，几小时后变成 1399 行。只认现跑出来的对照。
- **寻址用人员字段，不用表里的文本 `open_id` 列。** 用 bot 自己的 token、`user_id_type=open_id` 读人员字段，
  拿到的就是本 app 视角的 open_id；文本那列是别的 app 写进去的。192/661 人没有 email，按邮箱发会漏人。
- **一人多行**（Cursor 一行、Claude Code 一行）⇒ 按 open_id 去重，筛选字段取各行并集。
- **不重复发送靠 `uuid`**：`sha256(批次 root_id:open_id)[:40]`，重试和「重发未成功的」沿用同一个 root_id。
  飞书只在 1 小时内按 uuid 去重。
- **e2e 实测到的失败码**：塞一个格式对但不存在的 open_id ⇒ `99992351 not a valid {open_id}`
  （不是 `99992361`），两者都归「open_id 无效或不属于本 app」。
- 改源码后：scp 到 198 → 重建 ConfigMap → `rollout restart`，**用 pod 内 `sha256sum` 对一下本地的 sha** 再算上线完成。

## 🔴 跨租户：机器人发不过去是平台边界，不是权限没开

外部公司的人（上海车联、北汽研究院等关联组织）机器人发私聊几乎都是 `230013 Bot has NO availability`。
2026-10-10 用户把「可用范围」开到全员、又开了「对外共享」两个开关，**照样不通**——我在这里绕了
六轮，用户一度以为是我在逃避。证据链（李官玉，同一时刻同租户对照全绿）：

| 尝试 | 结果 |
|---|---|
| bot 私聊（open_id，id 已证明正确） | `230013` |
| bot 主动建 p2p 会话 | `232043 bot is invisible to user ids` |
| bot 拉他进群 | `232024` 无协作权限 |
| 用户本人身份经 API 私聊 | `230038 cross tenant p2p chat operate forbid` |
| 多维表格自动化「发送飞书消息」 | 收件人带上他，**整条连对照组都收不到** |
| 服务台推送 | 文档写明只能同租户 |

**真正的解释在官方文档**（[外部群文档](https://open.feishu.cn/document/develop-robots/add-bot-to-external-group)）：
「机器人仍不支持主动发起单聊」，外部用户必须**先**给机器人发过消息，`230013` 对外部用户的含义就是
「开关没开 **或** 对方还没跟机器人说过话」。可用范围的「全员」只覆盖本租户，外部的人不在那个列表里。

⇒ 实际做法：**跳过 + 记名单**，不要再加开关、重试或换 id。唯一能自动化的外部通道是「机器人进一个
已存在的外部群发群消息」；建外部群需要管理员开外部群权限（`232033`），机器人自己建群对外部用户是 `232043`。
外部的人用户身份查不到 user_id（`41050`），所以「解析失败 = 跨租户」，不要当「人不存在」。
发之前想知道谁会失败：bot 查 `contact/v3/users/{open_id}` 返 `41050` 的那批基本就是。

## 按姓名找人：人员字段 > 通讯录

表里只有姓名、没有人员字段时用 `scripts/feishu-resolve-names-to-open-id.py`：
```bash
python3 scripts/feishu-resolve-names-to-open-id.py \
  --from-person-field "DlT9bsrwMad12VsogEpcK9Ptncc/tblJT2s6Y6xjYj5A/姓名" --names-file names.txt -o out.json
```
- **优先从另一张表的人员字段按姓名交叉匹配**——人员字段给的就是本 app 视角 open_id，不碰通讯录、
  不受跨租户限制。10-10 走通讯录只解出 4/142（全卡在 `41050`），换这条 142/142。
- 同名多 id ⇒ 只认**本租户查得到**的那一个（杜小畔、刘咏强各有一个跨租户同名者）；0 个或多个都可查就失败，不猜。
- ⛔ 表里的**文本** `open_id` 列是别的 app 写进去的，6/6 全对不上，别用。
- lark-cli 把错误写到 **stderr**、stdout 为空；只读 stdout 会把 `41050` 误报成「没返回 JSON」。脚本已两路都收。

## 按 key 逐人群发卡片（Cursor Key 模型权限通知）

`scripts/cursor-key-notice-build.py`，三步，2026-10-19 全员实跑 453 人 408 成功：
```bash
python3 scripts/cursor-key-notice-build.py pull                    # 现拉飞书表 + 198 库
python3 scripts/cursor-key-notice-build.py build                   # 逐人生成卡片 + 门禁
python3 scripts/cursor-key-notice-build.py send --only-me          # 先只发刘国现看效果
python3 scripts/cursor-key-notice-build.py send --batch <批次名>   # 全员（批次名定 uuid）
```
- **每人按自己那把 key 生成**，模型以 198 库为准（表是快照；10-19 有一行还写着旧内部名）。
  ⛔ 别拿客户端菜单 / router alias 推「能不能用」：我 10-15 这么推出过 24 个、17 个假死名
  （读错 alias 字段 `metadata->model_aliases`，实际是 `aliases` 列；把 `ProxyModelTable` 225 个当组表，权威是 `/model_group/info` 341 个）。
- **选 key**：刘国现永远 `cursor-liuguoxian-std`；其他人多把取带姓名拼音那把；组织共享
  `cursor-{meituan,qiaohu,beiqiyanjiuyuan}-N` 忽略；只有 `deepseek-v4.1-flash` 的（合规收窄）跳过。
- **表里的 key_alias 常缺随机后缀**（表 `cursor-buyitian` / 库 `cursor-buyitian-pwcf`），按去后缀基名匹配。
- 额度不写数字（$70~$10000 各不相同），只告诉对方输入 `查余额` / `cye` 自己看。
- 定稿卡片钉在 `backend/tests/fixtures/cursor_notice_std_v7.json`，改样式要连夹具一起改并重新发刘国现确认。
- `feishu_send_message.py --card-file x.json` 可以原样发任意 schema 2.0 卡片（预览单张用）。

## 卡片排版（10-19 改了 7 版才定稿）

能用 / 不能用（188 上 curl `open.feishu.cn/document_portal/v1/document/get_detail?fullPath=...` 拿官方原文，
网页本身是空壳 SPA；本地 WebFetch 被挡）：
- ⛔ **没有下划线**：`<u>` `<ins>` 原样显示成字。下划线只有 post 富文本消息有，但 post 不能上色。
- ⛔ **没有任意背景色**：带底色的只有 `<text_tag>`（13 色，字号固定约 12px，`text_size` 改不动）。
- ⛔ **`column` 没有 `corner_radius`**：飞书 `200621` 拒收整张卡（连 lark-im 卡片规范里的示例都写错了）；
  圆角块只能用 `interactive_container`（它要 `behaviors`，不点击给空数组）。
- ⛔ 图标 token 必须在官方 1154 个里，写错**不报错只是不显示**；我猜的 `down-small-ccm_outlined` 不存在。
- API 回读 2.0 卡片只给「请升级客户端」占位 ⇒ **渲染效果后台看不到**，只能发给人看；
  拿不准的写法发一张 A/B/C 对照卡比空想快。
- 配色：一张卡里蓝 + 绿 + 红会打架（色环跳跃）。用户最终接受的是 11:17 那版（绿主推、灰清单、浅红提醒）
  + 天蓝自助查询；更进一步的「只留一个色相」版本他没选。
- 用户说「很 low / 一坨」时的真实问题：一路 `---` 平铺无层次、模型用 `·` 连成长串换行后糊在一起。
  解法是每个模型一个标签、每类一张白卡、出图折叠、链接改按钮（一主两次）。

## 别跑偏

- 要让 **her 实例自己**能建文档/表格/发消息（零 OAuth）→ `carher-feishu-bot-direct-install`。
- 要在 **Admin 后端**里收飞书事件并回消息 → `backend/feishu_bridge.py`（异步，按 chat_id）。
- 要用**你自己的账号身份**（user_access_token）操作 → `lark-cli` + `lark-im` skill，不是本 skill。
