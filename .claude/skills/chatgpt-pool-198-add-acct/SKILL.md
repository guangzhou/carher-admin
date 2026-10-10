---
name: chatgpt-pool-198-add-acct
description: |
  「acct-N 开通/续上 pro 了,加到 litellm 循环池」「阿里云那边停掉只加 198」——一条命令,可一次多号:
  `P198=.. P188=.. scripts/chatgpt-pool-198-add-acct.sh [--take-from-aliyun] <N> [N...]`。
  自动判:能打且 live pro ⇒ 只补腿不滚 pod;否则 refresh(200+pro 用它 / 200+free 停 / 401 阿里云 EIP 重烧 OAuth),
  门禁 plan=pro + account_id==PVC 原身份,rejoin + 照参照号逐字段补腿 + 每个 proxy 副本验收 + 跨轮复查 state + 入池流量。
  `--take-from-aliyun` 先把阿里云同号摘 prod/canary CM、rollout、逐 pod 验、scale=0。
  Use when 用户说「acct-N 是 pro 了/续费了/加回池/加到 litellm」「N 也做同样处理」「这个号是不是 pro」。
  198 上**没有** deploy 的全新号走 aliyun-chatgpt-acct-onboard;判 20x/10x 见 memory feedback_chatgpt_pro_tier_20x_10x_not_in_api。
---

# 198 号池:pro 号加回 litellm 循环池

```bash
P198='..' P188='..' scripts/chatgpt-pool-198-add-acct.sh 162 178 196          # 多号串行,末尾汇总
P198='..' P188='..' scripts/chatgpt-pool-198-add-acct.sh --take-from-aliyun 229  # 阿里云在跑 ⇒ 先停那边
P198='..' P188='..' scripts/chatgpt-pool-198-add-acct.sh --no-wait 207         # 跳过末尾 400s 复查
```

- 密码:memory `reference_198_direct_ssh`(198/188 同一个)。本机要:sshpass 进 198/188、
  `kubectl` 当前 context = 阿里云(`kubectl -n carher get job` 能通)、`lark-cli` 读密码表。
- **幂等**,任一段红退修好直接重跑。单号 ≈1~2min(refresh 路)/ ≈8min(OAuth 路),多号共用一次 400s 复查。
- 退出码:全部 OK 才 0。汇总表每号一行,OK 的附 live `plan/used/sub_active/expires/will_renew`。

## 动手前对用户说清两件事

1. **用户说「已经是 pro 了」就直接跑脚本**,别先预言「要重新登录/要手机号」。
   10-06 我说 226「大概率要手机号」被骂「这不是神经病吗,你之前是登录态啊」——
   实际 refresh 一次 200 pro。几小时前的探测结果不算数,凭据会被别的会话改。
2. 走到 OAuth 时一句话说明:**阿里云 EIP 节点只负责产出 auth.json,号装在 198**。
   (10-05 用户看到命令去阿里云直接打断:「不是已经在 198 上了吗?」)

## 判决树

| 段 | 判据 | 分支 |
|---|---|---|
| 0 预检 | 198 有 deploy+PVC;阿里云同号 0 副本 | 阿里云在跑:无选项 ⇒ 红退零变更;`--take-from-aliyun` ⇒ 先摘(见下) |
| 1 现状 | 叶子实打 `chatgpt-gpt-5.6-terra` + `chatgpt-acct-live-status-inpod.py` | **SERVES 且 live plan=pro** ⇒ 跳到 5,不碰凭据**不滚 pod** |
| 2 refresh | pod 内 refresh grant,200 同轮写回 | 200+pro ⇒ 用它 · 200+free ⇒ 停,并打出订阅到期日 · 401 ⇒ 3 |
| 3 OAuth | `FORCE_OTP=1 aliyun-eip-onboard.sh oauth`,密码表按 acct 取、**按邮箱核** | `MANUAL_PHONE`=手机墙(「卡密」列有值会自动 smsbee 接码)· `account_deactivated`=死号 |
| 4 门禁 | 新 id_token plan=pro 且 account_id == PVC 原身份 | 不符 ⇒ 红退,198 零变更 |
| 5 回池 | `chatgpt-pool-198-rejoin.sh N [auth]` | 换 PVC→补卷钉 digest→起 pod→叶子→resume arms→state |
| 6 补腿 | `arms.py clone`:照参照号(腿最多者)逐字段建 | 先等缺口数连续两次相同再建 |
| 7 验收 | **每个** `litellm-proxy-nodeport` endpoint 的 unique arms == 参照号、无坏行 | 12×15s |
| 8 复查 | ≥400s 后 state 仍 `HEALTHY False False`;SpendLogs 入池后的 status 计数 | 翻了 ⇒ 重跑 |

**「开了 pro」最常见的形状**(162/178/196/206/208/224/226/228/229 都是):盘上 AT 是开通前签的 ⇒
叶子 `token_expired`/free,refresh 一次签出 pro 的新 AT,不用重登。只有 RT 已被服务端作废
(`refresh_token_invalidated`,207/209)才走 OAuth。

## `--take-from-aliyun` 做了什么(10-06/07 四轮手工跑法收成的函数)

1. 备份两份 CM 到 `/tmp/ali<N>-<ts>/`(含 `*.cm.bak.yaml` 回滚件),`aliyun-cm-drop-acct.py` 出 new
2. 断言:new 里 0 引用、**无模型组被清空**、apply 前 live sha == 备份 sha(防别人中途改)、apply 后 == new sha
3. `rollout restart` prod+canary,等到只剩 3 个 1/1 新 pod(hostPort 排水 ~5min,卡 `free ports` 别回滚)
4. 逐 pod 验 `/app/config.yaml` sha + `/model/info` 该号 = 0
5. **最后**才 `scale chatgpt-acct-<N> --replicas=0` —— 先摘路由再停 pod,否则 router 打空 svc

回滚:`kubectl apply -f /tmp/ali<N>-<ts>/litellm-config*.cm.bak.yaml` + rollout + scale=1。

## 会骗人的量具

- 🔴 **配额表上「acct-N OFFLINE」先看 `site` 列**。`--take-from-aliyun` 之后阿里云那行就是 scale=0,
  10-07 用户据此以为 198 的 229 掉线(198 那行 ONLINE、10h 1018 次全 success)。表现在把这类行写成 `OFFLINE(198在服)`。

- 🔴 **叶子 SERVES ≠ pro**。165 订阅 09-26 就过期,叶子照样 `response.completed`、8h 吃 298 次,
  直到 free 周额度满才报错。旧脚本据此给它补了 2 条腿(已删)。现在第 1 段必须 SERVES **且** live pro。
- 🔴 **用户说 pro ≠ OpenAI 认 pro**。10-07 用户说 165 已是 pro,refresh 新签的 AT 仍 free、
  `accounts/check` `has_active_subscription=False expires=2026-09-26`。脚本红退时把这行打出来 ——
  直接转述给用户「OpenAI 那边这个邮箱还没生效」,别重烧 OAuth(没用)。
- 盘上 id_token/AT 的 plan 是签发快照;**live 只认 `wham/usage`**。`accounts/check` 是 web 路径常 CF 403,
  live-status 脚本会重试 6 次,读不到时 `sub_active=-` 不代表没订阅。
- `/model/info` 是 alias 展开的(207:28 行 / 11 个 id),**只按 unique `model_info.id` 数**。
- **只数 NodePort 会漏分歧**:10-05 补腿后 1 个副本晚 ~1min 才收敛。
- `rejoin.sh` J 段的 arms 数只当参考(10-05 读 0 实为 8,原因未查明),以 7 段 audit 为准。
- 入池后几分钟内 SpendLogs 0 行不算失败:池里 20+ 个号,轮到要时间。
- 入池前后的失败要按时间切开看:196 的 1 次 `token is expired` 在 refresh **之前**,之后 13 次全 success。
- 客户端发超大图(`48412 patches … limit 30000`)的 400 会落在任意号上,不是号坏。

## 脚本清单

| 脚本 | 跑在哪 | 作用 |
|---|---|---|
| `chatgpt-pool-198-add-acct.sh` | 本机 | 总入口(本 skill) |
| `chatgpt-acct-live-status-inpod.py` | acct pod 内,只读 | `LIVE plan= used= allowed= sub_active= expires= will_renew= err=` |
| `chatgpt-acct-refresh-grant-inpod.py` | acct pod 内 | refresh grant,200 同轮备份+原子写回+回读 |
| `chatgpt-pool-198-arms.py` | 198 本机 | `pick-ref / missing / clone [--apply] / audit` |
| `chatgpt-pool-198-rejoin.sh` | 本机 | 换 PVC→补卷→起 pod→叶子→resume→state(被总入口调用) |
| `aliyun-cm-drop-acct.py` | 本机 | 阿里云 CM 外科式摘号,五道 guard(被 `--take-from-aliyun` 调用) |
| `aliyun-eip-onboard.sh` + `aliyun-onboard-workspace.sh` | 本机→阿里云 | OAuth job + 取 auth(被 OAuth 段调用) |

⛔ 补腿别用 `chatgpt-pool-add-codex-model.py`:leaf 恒为 `openai/chatgpt-<slug>`(`gpt-5.3-codex` 真 leaf 是
`…-codex-spark`)、价格要手填;10-05 我给它喂 `acct-207` 造出 `chatgpt-acct-acct-207-*` 坏行(已加裸号门禁)。

⛔ 查 DB 别在 proxy pod 里起 prisma(卡在退出把 ssh 拖超时),直连 `litellm-db-0` psql。

## 不做的事

- 叶子 SERVES+pro 时**不跑 rejoin**(它的 patch 会滚 pod;重启在服务的号可能永久打死它,acct-237)。
- 不碰兜底链 / key 白名单:新号的腿进的是已有组。
- 不改续订:汇总里 `will_renew=False` 要提醒用户到期前手动续,花钱是用户的决定。
- 阿里云号没给 `--take-from-aliyun` 不动;阿里云池剩下的 free/作废号不顺手清。

## 战果(全部 2026-10)

| 号 | 日期 | 路径 | 备注 |
|---|---|---|---|
| 207 | 10-05 | 401 → OAuth | 首个,脚本由此沉淀 |
| 210 | 10-06 | SERVES+pro | 0 腿 + manual_offline;暴露 clone 撞 500(已修) |
| 206 208 224 | 10-06 | refresh 200 pro | |
| 226 228 229 | 10-06 | 阿里云摘 + refresh 200 pro | 226 被我误说要手机号 |
| 209 | 10-07 | 阿里云摘 + 401 → OAuth | 198 原在池但 6h 11 次全 `token_revoked` |
| 162 178 196 | 10-07 | refresh 200 pro | 补 6-sol/6.1-sol 到 11 |
| 165 | 10-07 | **红退** | 叶子能打但 free,订阅 09-26 过期;用户说已 pro,OpenAI 侧未生效 |

相关 memory:`project_198_acct_207_pro20x_rejoin_2026_10_05` · `project_198_acct_165_free_but_serving_2026_10_07` ·
`feedback_dont_predict_phone_wall_check_fresh_creds_first` · `feedback_chatgpt_pro_tier_20x_10x_not_in_api` · `topic_chatgpt_acct_pool_index`
相关 skill:`chatgpt-pool-198-pause-resume` · `chatgpt-acct-token-expiry-triage` · `aliyun-chatgpt-acct-onboard` · `chatgpt-acct-quota-lark`(配额表;迁完号阿里云那行显示 `OFFLINE(198在服)`,不是坏了)
