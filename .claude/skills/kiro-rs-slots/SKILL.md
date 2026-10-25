---
name: kiro-rs-slots
description: >-
  kiro.rs 槽位实例（补丁 0011，`kiro-rs-slots` :30891）的运维：一槽 = 一个 Kiro 号 + 一个固定出口 IP，
  用户按 LiteLLM key 绑槽；槽的增删停用、并发上限、冷备顶号、灰度名单、按号用量统计、换镜像、
  LiteLLM 侧 `kiro-slots-*` 组 / 兜底 / 重试 0 / 身份头 callback、按 key 切过去或切回、故障演练与回滚。
  Use when 用户说 "槽位 / 30891 / 新版 kiro admin"、"已封口 / 暂不接新人 / 冷备"、"把某些 key 切到新版 / 切回老版"、
  "老环境还有哪些 key"、"槽被禁用了用户会不会受影响"、"按 kiro 账户看用了什么模型多少 token"、
  "用量对不上 / 次数对不上"、"30891 打不开"、"加个槽 / 减个槽 / 换出口"、"kiro 上 haiku-5.5"。
---

# kiro.rs 槽位实例（0011）

老实例 / 加号 / 改镜像的通用流程在 skill `kiro-rs-ops`；加号 SOP 在 `kiro-account-manager-ops`。本篇只讲槽位实例特有的东西。

## 1. 现状（一律现读，下面只是 10-25 的形状）

```bash
python3 scripts/kiro-slots-watch.py --minutes 30   # 新老两条道成功率/耗时/key 数 + 每槽 在途/排队/429/被限/用尽/排队超时 + 冷备数
```

| 项 | 值 |
|---|---|
| 实例 | deploy `kiro-rs/kiro-rs-slots`（242，单副本 Recreate，grace 300s），Admin `http://10.68.13.198:30891/admin`，adminApiKey 在实例自己的 `config.json`（和老实例不同）|
| 老实例 | deploy `kiro-rs`（:30890，镜像 ctxwin）。只剩 `carher-hangzhou`、`claude-code-buyitian` 两把 key 主走它，另外是新实例的兜底 |
| 镜像 | `kiro-rs:v2026.10.1-carher-slots30`（10-25 20:53，账户表点表头排序（ID/邮箱/状态/等级/已用额度/7日请求/7日token/最后使用/优先级，分页前排）；slots29：积分消耗趋势图 `GET /credits/trend?hours=24|168|720`、每页 10/100/1000、批量查额度含停用号、用量统计单位统一万/亿；回滚 tag slots28）= 0001~0008 + 0010 + 0011；补丁 `k8s/kiro-rs/patches/0011-*.patch` |
| manifest | `k8s/kiro-rs/slots/kiro-rs-slots.yaml`（PVC `kiro-rs-slots-config`、svc `kiro-rs-slots`、NodePort 30891、secret `kiro-rs-slots-bootstrap`）|
| 槽 | 4 个：`jp-172.235.204.67`（经 243 代理）、`sg-47.84.112.136`、`sg-47.84.85.100`、`sg-47.236.200.98`；出口配在 `config.json.slots[]`，全局 `proxyUrl=null` |
| 并发参数 | 起步 16 / 最小 2 / 最大 24 / 自动调；排队超时 120s；「暂不接新人」倍数 1.5 |
| 状态文件（PVC） | `config.json` `credentials.json` `kiro_slots.json`（绑定/槽→号/退役/原代理）`kiro_account_usage.json`（按号×模型×北京日，留 8 天）|
| 状态备份 | `198:~/kiro-deploy/backup/slots-pre-<tag>-<ts>/`（每次换镜像前四个文件）；实例初始配置 `198:~/kiro-deploy/slots-inst/` |

## 2. 调度规则（界面上的字是什么意思）

- **绑人**：只有带身份的请求（`u:<key hash>`）才写绑定表，新人分给（在途少 → 绑定人数少 → 30 分钟用量低）的槽（`slots.rs::choose_slot_for_new_user`）。没身份的按会话哈希临时挑槽，不写绑定。
- **已封口 / 暂不接新人**：某槽 30 分钟用量 > 平均 ×1.5 且 ≥20 次 ⇒ 不再分新人，**已绑的人照常用**。四个全封口时忽略封口照同样顺序挑，不会没人接（10-24 用户问过）。
- **并发**：每槽 AIMD —— 429 时上限 ×0.7；安静 5 分钟且打满 ⇒ +1。满了排队，排队超 120s 回 529（LiteLLM 兜底到老实例）。
- **429**：先按官方退避 + retry-after 重试；**402 额度用尽 / 被限（restricted）才退役换号**，冷备里挑一个顶上（要 opus 的槽只挑能跑 opus 的号）。别的错误不退役。
- **判「号被限」**（opus 空回复后）：用同号、号**自己的** profileArn 打 runtime 入口，发**固定中性问题**（不带用户内容）。中性问题也 CONTENT_FILTERED ⇒ 被限换号；正常出字 ⇒ 号没事；403/超时 ⇒ 测不了、不换。
  🔴 slots24 及以前拿**用户原文**去探 ⇒ 敏感内容在任何号上都 CONTENT_FILTERED ⇒ 10-25 #3 justin 被冤退役；且用占位 ARN ⇒ social 号恒 403 永远测不出。slots25 修。
- **槽停用 / 删除**：在 Admin「槽位设置」关开关 ⇒ 该槽的人在下一个请求自动换到别的槽（`SLOT_GONE` ⇒ 重选），在途的跑完。删除只允许先停用。8 个停用分支都有单测。
- **冷备数** = 启用 − 在槽 − 退役（只算 quota/restricted）。手动在 Admin 启用一个号会清掉它的退役标记。
- **槽全停** ⇒ 529，不回老路径（老路径同一出口会和槽撞 IP）；LiteLLM 层兜底到老实例。

## 3. 换镜像

```bash
scripts/kiro-rs-build.sh 22f36e69 v2026.10.1-carher-slotsNN      # 188：cargo test 对照 + 镜像（0011 目前 321 测试）
scripts/kiro-rs-ship.sh v2026.10.1-carher-slotsNN <sha> --switch --slots
python3 scripts/kiro-slots-watch.py --minutes 10
```

- `--slots` 自动先备份四个状态文件；回滚命令脚本会打印（`set image deploy/kiro-rs-slots kiro-rs=<旧> seed-config=<旧>`）。
- 🔴 **换镜像期间 Admin 页面打不开 1~2 分钟**：老 pod 收 SIGTERM 后等在途流跑完（最长 300s）才退，新 pod 才起来。用户请求不断（排队的走兜底），先跟用户说。10-25 用户以为页面挂了。
- 🔴 grace 300s 之前（slots23 及以前）每次换镜像都切断在途请求 ⇒ 按号用量会比上游记录少十几次，那是丢的，不是统计错。
- 🔴 `kiro-lane-regress.sh` 只测老实例的 `kiro-*` 道，**不经过 30891**，别拿它当槽位回归。
- 本地改代码：工作树 `.work/kiro-0011/work`（基线 `.work/kiro-0011/base`），改完 `diff` 重新生成 0011 补丁再走 build。

## 4. LiteLLM 侧（198 `litellm-product`）

四件东西，缺一件都不对：

| 件 | 脚本（在 198 / pod 里跑） | 要点 |
|---|---|---|
| `kiro-slots-*` 组 | `scripts/kiro-slots-groups.py plan|apply|remove`（pod 内，`ONLY_GROUPS=` 限定） | 照抄 `kiro-*` 组只换 api_base/api_key/id；🔴 不传 `model_info.mode` |
| 兜底 + 重试 0 | `scripts/kiro-slots-router.py plan|apply|restore`（198 上） | `kiro-slots-X → kiro-X → 老链`；重试全 0。加新组后必跑 |
| 身份头 | `k8s/litellm-callbacks/kiro_slots_identity.py`（装/卸走 `litellm-198-lane-callback-add.py`，测试用 `python3 k8s/litellm-callbacks/tests/test_kiro_slots_identity.py` 直接跑，5 条；🔴 本机用 pytest 跑会在收集阶段报 RecursionError，原因还没查，直接用 python3 跑；防泄漏实测 `scripts/kiro-slots-identity-leak-probe.py`） | 只在 api_base 主机 = `kiro-rs-slots…` 时加 `x-litellm-user_api_key_hash/alias` |
| 按 key 切换 | `scripts/kiro-slots-switch-keys.py to-slots|to-old|restore`（198 上，dry-run 默认，`--backup` 必填） | 只改值以 `kiro-` 开头的别名；当前值不是预期旧值就跳过 |

🔴 **为什么身份头不用 LiteLLM 自带的** `add_user_information_to_llm_headers` + `model_group_settings.forward_client_headers_to_llm_api`：
它判断用的是**客户端原始模型名（per-key 别名改写之前）** ⇒ 别名指到 `kiro-slots-*` 的永远不命中；而且一旦命中，兜底到第三方时头也一起带过去。
callback 用 `async_pre_call_deployment_hook` 看 `kwargs['api_base']`，兜底到别家时 api_base 变了就不加 —— 已用假第三方实测收不到。
kiro.rs 发给 Kiro 官方的头是写死集合，有测试守（`upstream_request_carries_no_user_identity_headers`）。用户三条前提：**身份头只给新链路、不传 Kiro 官方、槽停用用户无感**。

🔴 **为什么重试必须 0**（10-24 演练 1）：全局 `num_retries=5` ⇒ 新实例停掉后同组重试，请求挂 120s+ 直到实例恢复，**兜底根本没走到**。全 0 后停机期间 7/7 立刻落老实例。

切 key 的标准动作（名单一行一个 key_alias）：

```bash
ssh cltx@10.68.13.198
export LITELLM_MASTER_KEY=...   # 从 pod env 现取
python3 kiro-slots-switch-keys.py to-slots keys.txt --backup ~/kiro-deploy/backup/alias-XXX-$(date +%Y%m%dT%H%M%S).json          # 先看
python3 kiro-slots-switch-keys.py to-slots keys.txt --backup ... --apply
python3 kiro-slots-switch-keys.py restore ~/kiro-deploy/backup/alias-XXX-*.json --apply                                          # 回滚
```

「老环境还有哪些 key」：查 `LiteLLM_VerificationToken.aliases` 里值是 `kiro-`（不是 `kiro-slots-`）开头的 key。10-25 只剩 `carher-hangzhou`（用户定不动）、`claude-code-buyitian`。

## 5. haiku-5.5

- 官方 kiro.rs 截至 10-25 不认 5.5 ⇒ 0011 里 `map_model` 加了 `haiku-5-5|haiku-5.5 → claude-haiku-5.5`（1M 窗口）。**老实例不认**：发 5.5 会悄悄落 4.5。
- LiteLLM 组 `kiro-claude-haiku-5-5` / `kiro-slots-claude-haiku-5-5`：`scripts/kiro-haiku55-groups.py plan|apply|remove`（官方价 ≤100K $0.10/$0.50、缓存写 $0.125、1h $0.20、读 $0.01；>100K $0.50/$2.50/$0.625/$1.00/$0.05）。兜底先回 `kiro-claude-haiku-4-5`。
- 只给 cursor key：`claude-haiku-4-5` 别名改指 `kiro-slots-claude-haiku-5-5`（用户无感升级，651 把，blocked 的 61 把不动），另外加 `claude-haiku-5-5` 名字到白名单（713 把，走 skill `litellm-198-key-allowlist`，备份 `haiku55-full-*`）。

## 5.1 「预计还能撑 N 小时」怎么算

每 10 分钟查一次所有**启用号**的剩余积分。消耗 = 相邻两次都在池里的**同一个号**的下降之和（加号/换号/月初重置让总额上跳的不算）。
`还能撑 = 当前总剩余 ÷（近 24 小时总消耗 ÷ 实际跨度小时）`，不足 30 分钟或 3 个点不估，< 24h 标红。
⚠️ 这是**24 小时平均**：白天高峰每小时 1300~2900、夜里 100~500，按高峰算会短得多；「总剩余」含冷备号（真到冷备用完才算撑不住）。

## 5.2 「这个号每次比别的号贵」先看它上下文多长

Kiro 按 token 计 credit：10-25 实测 4 个能算的号**每百万输入 token 都是 ≈5 credit**（opus-5.5 为主），每次贵的号就是每次输入多。
槽 3（sg-47.84.85.100）绑的 cursor-buyitian / claude-code-ruanyitai / sunlei 每次输入 47~67 万 token，其它槽 14~19 万 ⇒ 槽 3 上的号每次 2.2~2.4 credit，其它 0.8。
判据：`credit ÷ 统计后输入 token`，**只用统计前=0 的号算**（已用额度含统计前的消耗，分母对不上）。号本身没问题，换号也一样贵。

## 6. 按号用量统计（Admin 抽屉 / 表格「7 日请求」）

- 来源：kiro.rs 转发时旁路解码事件流，按 **号 × 模型 × 北京日** 记次数、输入/输出 token（`kiro_account_usage.json`，30s 落盘）。
- 🔴 **「用量对不上」先分清两个口径**：卡片上的「成功次数」是号**一辈子**的累计（含统计功能上线前）；用量统计只从**统计开始那刻**算。抽屉里写成「历史累计成功 = 统计前 + 统计后」。10-25 #10：186 = 76（上线前）+ 110（统计）。上线时会给每个号记一份基线。
- 剩下的小差（十几次）= grace 300s 以前换镜像切断的在途请求。

## 7. 回滚台账

| 回滚什么 | 命令 |
|---|---|
| 所有 key 切回老实例 | `kiro-slots-switch-keys.py restore <备份>`（备份在 `198:~/kiro-deploy/backup/alias-*`）或 `to-old <名单>` |
| 兜底/重试 | `kiro-slots-router.py restore <router_settings-pre-*.json>` |
| 身份头 callback | `sudo python3 litellm-198-lane-callback-add.py rollback litellm-proxy /root/cb-add-backup/litellm-proxy.<ts>.json`（gray 同理）|
| LiteLLM 组 | `kiro-slots-groups.py remove` / `kiro-haiku55-groups.py remove` |
| 镜像 | ship 打印的 `set image` 回滚命令；状态文件从 `slots-pre-<tag>-*` 拷回 PVC |
| 整个新实例 | `kubectl -n kiro-rs scale deploy/kiro-rs-slots --replicas=0`（LiteLLM 兜底接住，前提是重试 0 已在）|

## 8. 坑

- 🔴 **上下文治理判据**：`scripts/litellm-198-model-ceilings.json` 要有每个 `kiro-X` 的条目（数取 `kiro-probe.py --json catalog` 的 `tokenLimits`）；`kiro-slots-X` 审计时自动映射到 `kiro-X`（10-25 前 13 个 kiro-slots 组 + haiku-5.5/kimi-k3 全落 UNKNOWN）。新加 kiro 模型要同时加这张表。

- 🔴 **换号只认上游 402**：额度显示 0（10 分钟一查）≠ 上游开始拒。10-25 #3 justin 页面早显示用尽，上游实际到 09:24 才回 402，09:25 第一个 402 进来当场换 #13，那个请求自动重发不失败。
- 🔴 **兜底到老实例 = 兜到一个死池**：10-25 实测老实例 305 号 available=0（257 手动禁用 + 46 额度用尽），兜底请求在老实例回「所有凭据已用尽 403」⇒ 再往下兜 grok/deepseek。换镜像空窗的失败就是这么来的。老池补号或兜底链跳过老实例前，换镜像空窗用户会看到失败或降级到别家模型。
- 🔴 **换镜像那 15 秒新连接会失败，且没被兜底接住**（原因见上一条）：10-25 slots26 切换时 `claude-code-ruanyitai` 4 发「Cannot connect to host kiro-rs-slots」，SpendLogs 里没有落老实例的行（原因未查；演练 1 scale 0 时兜底是生效的），客户端自己重试第 5 发成功。
- 🔴 **新老两个实例的号 id 都从 1 编，会撞**：说「#6」要带实例。
- 🔴 现在新实例扛大部分流量 ⇒ 任何操作先想「会不会打断在途请求」：改配置走 Admin API（热生效），换镜像提前说，别 `kubectl delete pod`。
- 🔴 绑定表里的测试绑定（`anc:` `sid:` `u:smoke-*`）会虚增「绑定人数」，影响新人均摊。
- 判槽健康看 `kiro-slots-watch.py` 的「首发 429 / 被限 / 排队超时」，不是看 LiteLLM 失败率（兜底把失败吞了）。
- 兜底到老实例的量：SpendLogs 里 `model_group like 'kiro-slots-%'` 但 `api_base` 是老实例的行；10-25 有 54 次，原因未查。
- 新加坡 `kiro-sg` 两个测试实例还挂着老池 7 个号凭据（建议 scale 0，等用户定）。
