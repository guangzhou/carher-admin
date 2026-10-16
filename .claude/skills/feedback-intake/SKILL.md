---
name: feedback-intake
description: 同事意见反馈通道(https://chat.auto-link.com.cn/feedback/)的运维与处理 SOP——飞书登录身份、写多维表格「AI 反馈」、解决后自动通知提交人;改页面/改小服务/改表字段/排查「提交失败」「没收到通知」时用。Use when 用户提到意见反馈、反馈页、FB-编号、AI 反馈表、同事说提交不了/没收到通知、要处理/统计反馈。
---

# 意见反馈通道

**入口**:https://chat.auto-link.com.cn/feedback/(一键配置页「意见反馈 / 报问题」按钮;`?tool=codex&type=…&from=…` 预选)
**表**:飞书 wiki「AI 反馈」https://t83dfrspj4.feishu.cn/wiki/MEzOwVA9WiiNYikmhEFcQAqlnRe (base `SQ4vbBJhcauRKpsuZrNczNCInxb`,表「反馈」`tblkckVodz4HHKZf`)

## 链路
```
浏览器(带 chat.auto-link.com.cn 的 httponly token cookie)
  └ /feedback/api/*  → nginx carher-feedback 段 → 127.0.0.1:8792 (systemd carher-feedback)
       ├ 身份:拿 cookie 问 Open WebUI /api/v1/auths/ ;open_id 从 webui.db user.oauth.feishu.sub 只读
       └ 写表/发消息:飞书应用 cli_a9278e26f138dbd3(= openchat 登录用的同一个应用 ⇒ open_id 同一套)
```
- 🔴 open_id 是**按应用**的:换别的飞书应用写表/发消息,同一个人 open_id 不同 ⇒ 人员列对不上、私信发不出。
- 该应用对这张表的权限是靠「把表分享给应用(appid, edit)」给的;它**没有**通讯录 user_id 权限,所以不靠邮箱反查 open_id。

## 文件 / 部署
`scripts/feedback/`:`feedback_server.py`(标准库)、`index.html`、`test_feedback_server.py`(9 条)、`fields.json`(建表字段)、`deploy_198.sh`
```bash
bash scripts/feedback/deploy_198.sh   # 先跑测试;传文件、写 /etc/carher-feedback.env(密钥现读 k8s)、重启服务;nginx 段只加一次;外网验收
```
- 日志:`sudo journalctl -u carher-feedback -f`(「新反馈 FB-xxxx」「已通知 FB-xxxx」)
- 回滚:删 `chat.auto-link.com.cn.conf` 里 `# >>> carher-feedback >>>` 段 → `nginx -t` → reload;`systemctl disable --now carher-feedback`。备份 `/etc/nginx/backup-feedback/`

## 处理反馈(运维)
1. 表里「待处理」视图(状态 新提交/已确认/处理中,按紧急程度排)。
2. 处理完:填「处理说明」(这段话会原样发给提交人)→ 状态改「已解决」。60 秒内自动私信提交人,并勾「已通知提交人」;**不会重发**。要重发就取消勾选。
3. 「已解决(FAQ素材)」视图 = 有处理说明的已解决项,定期挑高频的写进一键配置页「出问题了」表。

## 权限
- 「只看自己的」靠页面「我提过的」(服务端按提交人 open_id 过滤)。**表本身不对同事开放**(链接分享关),同事不需要进表。
- ⛔ 不要用多维表格高级权限给全员开表:管理员不受角色约束,自测必假绿;且要逐人加成员。10-16 试过已撤回。

## 坑
- Open WebUI 登录后回跳靠 `/auth?redirect=<path>`(前端存 localStorage.redirectPath,OAuth 回来后跳过去)。
- 自动编号在新建返回里可能为空 ⇒ 服务端回读一次再给前端编号。
- 测线上:在 open-webui pod 里 `create_token({'id':uid}, timedelta(minutes=5))` 造短期 token 当 cookie;测完删测试行。
