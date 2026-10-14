---
name: grok-reset-card
description: >-
  查/兑 Grok（SuperGrok / Heavy）官方「重置卡」= rate-limit reset token，兑一张把周用量池一次回满。
  一条命令 `scripts/grok-onboard/grok_reset_card.py`（Mac 上跑：198 sub2api 取 token → 225 WARP 打 grok.com
  gRPC-web，自带阴性对照，凭据不落盘）。Use when 用户说「这个号有官方发的重置卡 / 重置一下 / 兑一下重置卡」
  「grok 额度用完了能不能重置」「把所有 grok 号的重置卡查一遍」「xxx@outlook.com 这个呢」。
  ⛔ 和 ChatGPT 的重置卡（`rate_limit_reset_credits`，`scripts/chatgpt-acct-reset-cards.py`）是两套东西。
---

# Grok 重置卡

## 一条命令

```bash
cd ~/codes/carher-admin/scripts/grok-onboard
python3 grok_reset_card.py                                   # 全部 sub2api grok 号，只读
python3 grok_reset_card.py --acct 75 qrhojxepcm@outlook.com  # 点名（id 或邮箱）
python3 grok_reset_card.py --acct 75 --redeem                # 有未过期卡就兑一张，兑完回查剩几张
python3 grok_reset_card.py --sso-file F                      # 不在 sub2api 的网页号，F 每行 邮箱----密码----eyJ..
```

全池 56 号约 1 分钟。`--redeem` 不可逆，必须点名，脚本拒绝对整池兑。
用户说「重置下 xxx」= 先不带 `--redeem` 查一次；有 `HAS` 再带 `--redeem` 兑，汇报 `REDEEM ... status=0` 和 `after=`。

## 读输出

| 行 | 含义 |
|---|---|
| `CONTROL bogus-bearer status=7 ok` | 第 0 步阴性对照通过。不是 7 ⇒ `ABORT`，这一轮的「空」不可信，别出结论 |
| `ROW id\|邮箱\|NONE\|total=0` | 认证通过、真没卡 |
| `ROW ...\|HAS\|live=N <tokenId> exp=日期` | 有未过期卡 |
| `ROW ...\|AUTH\|status=7/16` | token 失效 / 没凭据 ⇒ 号要重认证（skill `sub2api-grok-ops`），不等于没卡 |
| `MISS` | 不在 sub2api 或已删除 ⇒ 让用户给 SSO 走 `--sso-file` |
| `REDEEM ...status=9` | 已兑过 / 没额度；`3` = tokenId 无效 |

## 已知事实（2026-10-14）

- sub2api 57 个 grok 号全量扫过：56 active 全 `NONE`，已删的 id 32 token 失效。grok2api 的 2 个 heavy 号就是 75/76，同样 0 张。Bearer 和网页 SSO cookie 两种认证结果一致。
- 公开过的那批卡是 2026-08 Grok 4.6 发布时发的，一号一张，9 月中旬过期。用户再说「有官方卡」⇒ 请他截网页 Settings → Usage 的图，确认有卡再兑。
- 号的真实订阅到期看 grok.com，sub2api DB 里的到期日是旧快照（acct 75 DB 写 10-16，实际 2027-01-13）。

## 坑

- 成功响应的 `grpc-status` 只在 body 的 0x80 trailer 帧里，HTTP header 里没有；错误才放 header。只看 header 会把「真没卡」读成异常。`test_grok_reset_card.py` 固定了这段真实字节。
- grok.com 挡机房 IP，必须走 225 WARP（socks5h 127.0.0.1:40000）。225 系统 python 没装 pysocks，所以 worker 用 curl 发请求，不用 requests。
- 188 连不上 x.ai，Outlook IMAP 的 basic auth 被微软关掉了，靠收信找卡这条路走不通。
- 端点和 proto 定义源自 stablyai/orca PR #18116，具体见脚本 docstring。

测试：`python3 test_grok_reset_card.py`（离线 6 例）。
