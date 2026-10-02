# feishu-broadcast — 飞书群发页

**线上：<http://10.68.13.198:30930/>**（内网，需口令）

从多维表「cursor&claude code 账户表格」（wiki `UvIxwsWLQivn1RkrdPTcVGGnnze` → base
`DlT9bsrwMad12VsogEpcK9Ptncc` / table `tblJT2s6Y6xjYj5A` / 视图「全部」`vewISEKPg7`）
按字段筛人，用机器人 `cli_aa8505…` 群发纯文本或 Markdown 卡片。

| 文件 | |
|---|---|
| `broadcast/server.py` | 纯标准库服务端 |
| `broadcast/index.html` | 页面 |
| `k8s/feishu-broadcast.yaml` | ns / Deployment / NodePort 30930；部署与改 Secret 的命令写在文件头 |
| `backend/tests/test_feishu_broadcast.py` | 52 个单测 |

## 前置：bot 要能读到**行**，光是协作者不够

bot 读不到行时，飞书一律返回 **`code 0` + 0 行**，不报错。2026-10-01 实测有两个原因，现象一样：

| 原因 | 怎么分辨 | 解法 |
|---|---|---|
| bot 不是协作者 | 连字段都读不到 | 多维表「…」→「添加文档应用」加上 bot |
| **bot 是「可阅读」协作者，但表开了高级权限**（这张表就是这样） | 能读表、能读 31 个字段，行是 0；`GET /bitable/v1/apps/<token>` 返回 `is_advanced: true` | 二选一：bot 升「可管理」；或在高级权限里给 bot 单独建一个「所有行可读、只开需要的字段」的角色 |

高级权限下，「可阅读」协作者能看到哪些行由角色的行规则决定，只有「可管理」不受限。这张表的「角色3」规则是
「提交人 包含 …」（只能看自己提交的行），bot 一行都没提交过，所以是 0 行。页面会按上表区分原因并显示对应的红条。

验收：页面右上角的**行数要和你本人现读同一视图的行数一样**。表一直在长（10-01 当天就从 1391 涨到 1399），
别拿记下来的数比。一条命令跑完：

```bash
FEISHU_APP_ID=... FEISHU_APP_SECRET=... python3 scripts/feishu-bot-base-access-check.py \
  --wiki UvIxwsWLQivn1RkrdPTcVGGnnze --table tblJT2s6Y6xjYj5A --view vewISEKPg7
```

退出码 0 才开始群发。

## 用法

1. **选收件人**：同一字段点多个值 = 或，不同字段 = 且。默认只选「在职」。一个人在表里有多行
   （Cursor 一行、Claude Code 一行）时按并集算、只发一条。可按姓名手动加人或排除。
2. **写消息**：纯文本，或 Markdown 卡片（可加标题）。`{name}` 会换成收件人姓名。草稿存在浏览器本地。
3. **先发给我看看**：只发给 `BROADCAST_TEST_OPEN_ID`（刘国现），看渲染。
4. **群发**：手输人数确认才能点。服务端按同样的条件重算名单，人数对不上就拒发（防止预览后表被改过）。
5. **发送记录**：每个人的结果都留着（成功给 `message_id`，失败给飞书原始 code/msg）。
   失败按原因分开数。「重发未成功的」只重发失败和状态未知的人。

## 设计要点

- **寻址用人员字段里的 open_id**（`user_id_type=open_id` 读出来的就是本 app 视角）。
  表里那列文本 `open_id` 是别的 app 的，塞进来会 `99992361`；192 人没有 email，按邮箱发会漏人。
- **不会重复发送**：每个收件人的 `uuid = sha256(批次:open_id)`，重试和「重发未成功的」都用同一个 uuid，
  飞书 1 小时内按 uuid 去重。所以「网络错但其实已经送达」的人重发也不会收到第二条。
  超过 1 小时再重发就没有这层保护了。
- **pod 死在发送途中**：重启后这批标「中断」，没确认送达的人标「未知」，**不会自动续发**。
  看一眼明细再决定点不点「重发未成功的」。
- **限速 5 条/秒**（`BROADCAST_RATE`），661 人约 2.5 分钟。限流和网络错误自动重试 3 次；
  「不在可用范围」这类永久错误不重试。
- **登录**：单口令，连错 5 次全局锁 10 分钟（NodePort 后面看到的源 IP 常是节点 IP，按 IP 锁等于没锁）。
  会话在内存里，滚 pod 后要重新登录。口令在你 Mac 的 `~/.feishu-broadcast-password`。
- 发送记录存 198 的 `/var/lib/feishu-broadcast/jobs/`，滚 pod 不丢。

## 常见失败

| 飞书返回 | 页面分桶 | 意思 |
|---|---|---|
| `230013` | 不在机器人可用范围 | 去开放平台「可用范围」把这人（或部门）加进去 |
| `99992361` / `99992351` | open_id 无效或不属于本 app | 表里人员字段坏了，或代码被改成用了文本 `open_id` 列 |
| `99991400` | 被限流 | 已自动重试；常出现就调低 `BROADCAST_RATE` |
