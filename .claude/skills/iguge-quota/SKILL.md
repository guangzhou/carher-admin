---
name: iguge-quota
description: >-
  查 iGuge（谷歌学术助手）账户的流量余额：已用/剩余/总量、每月重置日、账号状态、到期日，全部账户一次查完。
  Use when 用户说「查 iGuge 余额/流量还剩多少/iGuge 额度/重置日/账号暂停了没/订阅到期/插件里 客户端下载与订阅 那页的数字」。
  底层是 skill `cli-anything-mihomo` 的 `sub quota`。
---

# iguge-quota

## 一条命令

```bash
scripts/mihomo-198.sh quota          # 所有已登记的 iGuge 账户；会重新拉一次订阅；188 隧道自动拉起
```

输出（10-31 实测）：

```
status  account  account_status  used                       left_gb  reset_date  expire_date  days_left
OK      default  正常              3.545 / 200.0 GB (1.77%)   196.46   2026-11-14  2027-10-14   347
OK      acct2    正常              25.206 / 200.0 GB (12.6%)  174.79   2026-11-28  2027-10-28   361
```

- `status`：已用 ≥ 80% 或离到期 < 30 天 ⇒ `WARN`，命令 exit 1（阈值 `cli-anything-mihomo sub quota --warn-pct/--warn-days`）。
- 只要 JSON：`cli-anything-mihomo --json sub quota --all-accounts`；不想重新拉订阅：加 `--cached`（读上次的结果）。
- 单个账户：`cli-anything-mihomo -a acct2 sub quota`。
- **不用命令行也能看**（11-01 slots59 起）：kiro Admin `http://10.68.13.198:30891/admin` 首页「代理余额（iGuge 订阅流量）」卡片，
  同一份 `subscription-userinfo` 头（后端 `GET /api/admin/egress/quota` 现查，10 分钟缓存，「刷新」现查，≥80% / <30 天标提醒）。
  它读的订阅地址在 Secret `kiro-rs/kiro-egress-subs`（`subs.json`），**和本机 `~/.config/cli-anything-mihomo*/sub.url` 是两份**：
  新加账户 / 重置订阅链接后两边都要更新，否则卡片少一个账户或报错。细节见 skill `kiro-rs-slots` §1.4。

## 数字从哪来、和插件页对不对得上

- 唯一的机器可读来源 = 拉订阅时的响应头 `subscription-userinfo: upload=…; download=…; total=…; expire=<unix>`。
- 10-31 和插件页对过：插件「可用 175.65 / 已用 24.35 GB / 12%」，脚本同一时刻 24.37 已用、175.63 剩余、12.18% ⇒ **同一份数据**（差 0.02GB 是两次读取之间又用了一点）。
- `reset_date`：插件页写「每月流量重置日 /28日」，正好是到期日（2027-10-28）的那一天 ⇒ 按到期日的「日」算下一次重置（老账户到期 10-14 ⇒ 每月 14 日）。老账户的重置日是按这个规律推的，**没在插件里核对过**。
- `account_status`：插件页规则「当月用到 200G，手机端（客户端订阅）账号自动暂停，到重置日恢复；插件端不受影响」⇒ 已用 ≥ 总量记「暂停」。
- 插件页其它说明：**不限设备数**（只限流量）；流量按节点「倍率」扣（1.5 倍率节点下 1G 扣 1.5G）；插件端流量不计入这 200G。

## 为什么不去调插件的接口

插件那页（`chrome-extension://ncldcbhpeplkfijdhnoepdgdnmjkckij/page.html?/client/feed`）是 `POST <api>/page/client/feed`，
body 带登录 token（Chrome 存储里的 `mzk_token`），返回的是**服务端渲染的整页 HTML**（可能还加密），没有结构化字段。
要用它就得把 iGuge 登录 token 拿出来放进脚本 —— token 等于账号登录态，泄露 = 账号被接管。它能多给的只有「重置日、账号状态」，
而这两样上面已经能从订阅头推出来 ⇒ **不用**。
🔴 同目录还有 `page/client/resetfeedlink`（重置订阅链接，所有客户端失效）、`applyopenclient`（开通客户端），任何脚本都别调。
（调用细节已读源码确认：插件 2.3.8，`.work/shellcrash/igg/x/js/iggservice.js` 的 `getjsonData`、`page.js` 的 `page_init`。）

## 新增一个账户

插件「客户端下载与订阅 → 订阅/服务器」复制订阅地址，然后：

```bash
printf '%s\n' '<订阅地址>' > /tmp/u && cli-anything-mihomo -a acct3 sub set-url --from-file /tmp/u && rm /tmp/u
scripts/mihomo-198.sh quota
```
订阅地址 = 凭据（谁拿到都能用这个账户的流量），只存在本机 `~/.config/cli-anything-mihomo[-<账户>]/sub.url`（0600），别贴群里。

## 坑

- 198 自己拉订阅会 403 ⇒ 必须经 188 SOCKS（工具自动起；起隧道别加 `ClearAllForwardings`）。
- 隧道偶发 `curl (97)`：自动重试 3 次，失败不覆盖上次的结果。
- 198 上 mihomo 进程自己显示的流量是「进程启动后累计、重启清零」，不是账户额度，别拿来当余额。
