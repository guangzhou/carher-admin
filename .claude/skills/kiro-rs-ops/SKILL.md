---
name: kiro-rs-ops
description: >-
  kiro.rs（198 集群 242 节点，Anthropic API → AWS Kiro/CodeWhisperer 代理）的全套运维：
  判某模型这个账号有没有（`ListAvailableModels` 是唯一权威判据）、查还剩多少 credit、
  加/改模型（**必须自建镜像，升 tag 是死路**）、加号/摘号（`scripts/kiro-pool.py`，零重启）、
  逐号判死活（`scripts/kiro-probe.py`）、接进 198 LiteLLM 19 条 `kiro-*` 道
  （含绕 LiteLLM 重名劫持的别名前缀）、回归压测、回滚。
  Use when 用户说 "kiro 还剩多少额度/credit"、"kiro 支持 xx 模型吗"、"kiro 打不通"、
  "给 kiro 加个模型"、"给 kiro 加个账号 / 把某个号摘掉"、"kiro-* 那些道怎么样了"、
  "升级 kiro.rs"、或者要给生产 key 授权 `kiro-*` 名字。
---

# kiro.rs 运维

## §0 拓扑速查

| 项 | 值 |
|---|---|
| ns / Service | `kiro-rs`，ClusterIP **`10.43.109.5:8990`**（仅集群内，无 NodePort/ingress） |
| 节点 | **`aiyjy-litellm-242`**，nodeSelector 硬钉 + toleration `dedicated=new-node:NoSchedule` |
| 镜像 | **`kiro-rs:master-kiroalias`** —— 自建，**只在 242 本地 containerd** |
| 账号 | 🔴 **一律现读，别信写死的数** —— 一条命令出全表（id / 邮箱 / `authMethod` / 档位 / live 余额 / 在役哪个 / 盘上一致否 / 零重启否）：<br>`python3 scripts/kiro-pool.py show --with-quota`<br>这张表 09-17~09-24 因为手维护漂过两次，**已不再在这里复述池子状态**。不可推导的部分（每个号的加入经过、备份路径、踩过的坑）在 §D4。<br>只留两条常驻事实：**档位不止一种**（PRO+ 2000 / **PRO MAX 5000**，算容量前读 live cap，别按 2000 硬算）；**`authMethod` 混编**（id≤10 全 `idc`，id11 起有 `social`，两条刷新路行为相反，见 §D3 第 0 步）。下次重置 **2026-10-01 00:00 UTC** |
| 出口 | **全部经 `10.68.13.243:8120`**（Astrill Los Angeles → `104.129.16.172`，09-17 19:36 从 8119 Seattle 改指），见 §D2。⚠️**全局唯一，无 per-account 路由**；⚠️ 8118/8119/8120 **三条都与 `chatgpt-acct-*` 共用**（分别 8/7/6 个对象），**没有独占 IP 可选** |
| 凭据 | PVC 内 `/app/config/credentials.json`（list = **真的多号池**，不是只用 `[0]`，见 §D3） |
| 对外 | 198 LiteLLM ns `litellm-product` 的 **19 条 `kiro-*` 道**（09-29 加 opus-5-5）|
| manifest | `198:~cltx/kiro-deploy/kiro-rs.yaml` |
| 登录 | `ssh cltx@10.68.13.198`，kubectl 要 `sudo` |

🔴 **Astrill 账号 2026-09-24 到期。** 到期后 `proxyUrl` 连不通 ⇒ **19 条道会再次全挂**，
且症状是**超时不是 403**（跟被封停长得不一样，别误判成又被封了）。
续费或把 `proxyUrl` 改指别的境外出口，二选一。

🔴 **比 Astrill 更近的一堵墙：credit —— 09-17 已经撞上一次并兑现。**
09-17 00:27 北京实测 `712.67 / 2000`（号 09-16 06:18Z 起服务 ⇒ ≈68 credits/h），
据此预测"约 09-17 19:30 北京打满" —— **实际 09-17 06:35 就打满了**（`successCount` 2895），
即真实速率比外推更快。⇒ **一个 PRO+ 2000 credits ≈ 撑 1 天多，撑不到月底是确定的**，
要么持续加号（§D3，零重启）、要么开 overage、要么把量大的道从高倍率往低倍率挪。
复核一行：`python3 scripts/kiro-pool.py show --with-quota`（**逐号打印，别只看池子总数**）。

⚠️ 上一个账号 `bradburybruns64@gmail.com` 2026-09-12 被 AWS 封停（403 suspended），
**触发原因至今未查清**。申诉入口 `https://app.kiro.dev/account/usage?support_form`。

🔴 **判一个号"还能不能用"必须打 `ListAvailableModels`，不能只看 credit 余额。**
09-17 逐号 live 复核 `bradburybruns64`：`getUsageLimits` 答得好好的、
**KIRO FREE `16.34/50` 还剩 33.66**，但同一凭据打 `ListAvailableModels` 直接 **403
`Your User ID (c488d458-…) temporarily is suspended`**。
⇒ **余额尺子会把封停号读成可用号**。清号/排障时先过 catalog 这道，再看余额。

⚠️ **镜像只在 242 本地**。Deployment 靠 nodeSelector 钉在 242 才成立，
换节点/重装节点必须重新 import，否则起不来。构建产物留在 `188:/Data/kiro-build/`。

## §0.1 三个脚本：读用 `kiro-probe.py`，写用 `kiro-pool.py`，回归用 `kiro-lane-regress.sh`

本仓 `scripts/` 下，都在**本机**跑（内部自己 ssh 198），不需要先登录：

| 脚本 | 性质 | 干什么 |
|---|---|---|
| `kiro-pool.py` | **写**（admin API，零重启） | `show [--with-quota]` / `add <导出json>` / `remove <id>` / `priority <id> <n>` / `extract <备份> <who>` |
| `kiro-probe.py` | **纯只读**（直打 AWS） | `catalog`（`ListAvailableModels` 逐号）/ `quota` / `both` / `has <模型…>`（退出码即判据）。凭据与出口都从 live pod 现取，401/403 自己走 refresh（**按 `authMethod` 分流 idc / social**）。social 号必带 `--out-rotated FILE`，见 §D3 第 0 步 |
| `kiro-lane-regress.sh` | 只读压测 | 19 条道回归一条龙：车道名现取 + **条数断言** + bench + in_tok 容差带比对。见 §E |

**日常这一条就够**：`python3 scripts/kiro-pool.py show --with-quota`
—— 池状态 + 盘上一致性 + pod 零重启 + **每个号的 live 档位/余额**，一屏出全。

🔴 **余额为什么走 `kiro-pool.py` 而不是 `kiro-probe.py quota`**：探针拿的是 pod 盘上那份
凭据，401/403 时**自己发起刷新**，而 social 号刷新**会轮转 refreshToken** ⇒ 新值只留在
我这边，**服务端那份当场变死 token**。`--with-quota` 走 admin 的 `GET /credentials/{id}/balance`，
是服务端用自己的 token manager 去刷并 `persist_credentials()` ⇒ 探不死线上，还快一个数量级。
⚠️ 但**余额判不出封停**（09-17 `bradburybruns64` 还剩 33.66 却已 403 suspended）
⇒ 判「能不能用」仍只认 `kiro-probe.py catalog`。

🔴 **`--account` 纯数字只按 id 精确匹配。** 09-24 踩过：`--account 8` 同时命中了
id8 和 id3（`rmiglio582@gmail.com` 这类邮箱里带 "8"），两个号的读数混在一起打出来，
差一眼就把 id3 的 `0.0/2000 已耗尽` 当成 id8 的余额 —— **容量判断会整个反过来**。
现在数字选择器精确、邮箱子串命中多个直接报错。

`kiro-pool.py` 把下面这些反复踩的坑固化进去了，**手搓 curl 之前先看它能不能用**：
adminApiKey 走 `curl -K` 不进 argv；body 走临时文件不做 shell 转义；
写前自动备份到 `198:~/kiro-deploy/backup/`（0600 并打印回滚提示）；
**`add` 自动做重复性检查**（比盘上每个号的 `refreshToken` sha256，且**先断言集合非空**
再比 —— 空集假绿见 §D3 第 0 步）；
每个写操作前后各打一次 `show`，**同时比 API 状态与盘上 `credentials.json`**（判落盘）
和 pod 的 `RESTARTS`/`startTime`（判零重启）；`remove` 自动先 `disabled` 再 `DELETE`。

```sh
python3 scripts/kiro-pool.py show                 # 池 + 盘上 + pod 代数，只读
python3 scripts/kiro-pool.py --dry-run remove 3   # 只打印将要发的写请求
```

🔴 **`--dry-run` 只是请求形状对照，不能替代写完的复核**（`show` 那两条尺子）。

🔴 **`extract` 输出的本地文件含 live `refreshToken`，探完立刻删。**

09-17 21:3x 补的两处（都是被全新导出逼出来的）：

- `kiro-probe.py` 现在能吃**只有 `refreshToken` 的全新导出**：没有 `accessToken` 就先 OIDC
  换一次（原来直接 `KeyError: 'accessToken'`），`machineId` 缺失就按 kiro.rs 同一条规则派生
  （`machine_id_of()`，见 §D3 第 0 步）。⇒ **加号前的只读验号不再需要手搓 refresh。**
- `kiro-pool.py` 的 dry-run **对 body 脱敏**（`refreshToken`/`clientSecret`/`accessToken`/
  `kiroApiKey`/`proxyPassword` 只留 `len=` 与首尾 6+4）。原来它把完整 refreshToken 和
  4.8KB clientSecret 原样打进终端 —— dry-run 是"不写生产"，不是"不泄露"。
  留下的 `len=` 仍够判「字段传丢没」（与探针读到的长度对齐即可）。

## §A 判「这个账号有没有某模型」—— 只认 `ListAvailableModels`

```bash
python3 scripts/kiro-probe.py catalog        # 整池逐号，凭据+出口都自动从 pod 读
python3 scripts/kiro-probe.py has fable gpt-6 kimi-k3   # 直接问「有没有」，退出码即判据
```

返回的是**这个账号的真实 entitlement**，带 `rateMultiplier` 和真实 token 上限。
比 kiro.dev 文档、比任何代理的内置清单都硬。

**⚠️ origin 只有四个合法值**（上游自己在报错里吐的清单）：
`AI_EDITOR` **19** / `KIRO_CLI` **19** / `IDE` 11 / `CLI` 3。
`CONSOLE` 直接 "not supported"，`CHAT` 回 `REQUEST_BODY_INVALID`。
⇒ **拿 `CLI`(3) 的结果说"没有某模型"是假红**，判缺失一律用 `AI_EDITOR`。

**09-16 实测 19 个（PRO 与 PRO+ 逐字相同）：**

| 上游 id | 倍率 | 输入窗口 | 上游 id | 倍率 | 输入窗口 |
|---|---|---|---|---|---|
| `auto` | 1.0 | 1M | `claude-sonnet-4.6` | 1.3 | 1M |
| `claude-opus-5` | 2.2 | 1M | `claude-opus-4.5` | 2.2 | 200K |
| `claude-sonnet-5` | 1.3 | 1M | `claude-sonnet-4.5` | 1.3 | 200K |
| `claude-opus-4.8` | 2.2 | 1M | `claude-sonnet-4` | 1.3 | 200K |
| **`gpt-5.6-sol`** | **4.4** | 1M | `claude-haiku-4.5` | 0.4 | 200K |
| `gpt-5.6-terra` | 2.2 | 1M | `deepseek-3.2` | 0.25 | 164K |
| `gpt-5.6-luna` | 1.1 | 1M | `minimax-m2.5` | 0.25 | 196K |
| `claude-opus-4.7` | 2.2 | 1M | `minimax-m2.1` | 0.15 | 196K |
| `claude-opus-4.6` | 2.2 | 1M | `glm-5` | 0.5 | 200K |
| | | | **`qwen3-coder-next`** | **0.05** | 256K |

🔴 **09-26 目录已变成 20 个：多了 `claude-opus-5.5`（2.0x / 1M / 输出 128K）。**
在新号 `gurjardevbax8`（PRO MAX）的 `AI_EDITOR` catalog 里实测到。
⇒ **上面这张 19 行表是 09-16 的快照，会漂；判"有没有某模型"仍只认现打 `catalog`/`has`，别读表。**

**09-29 补全这一项**（原来只有一个号的样本、198 侧也还没道）：
- 池里**5 个号逐个实打 `AI_EDITOR` catalog，全部有 `claude-opus-5.5`**
  （PRO+ ×2 / PRO MAX ×2 / POWER ×1），倍率一致 2.0x、输入 1M、输出 128K。
- ⛔ **`claude-sonnet-5.5` 一个号都没有** —— 官网页面写支持不等于 entitlement 里有，
  判据只认 `ListAvailableModels`。所以这轮只上了 opus-5.5。
- 198 侧已加 `kiro-claude-opus-5-5` 道，**回归从 18 条变 19 条**（§C / §E 已同步）。

⚠️ **opus-5 和 opus-5.5 的差异只在 schema，不在窗口/输出**，做对照时别拿窗口当尺子：

| 字段 | `claude-opus-5` | `claude-opus-5.5` |
|---|---|---|
| `rateMultiplier` | 2.2 | **2.0**（5.5 反而更便宜） |
| `thinking.type` enum | `adaptive` / `disabled` | **只有 `adaptive`** |
| `output_config.effort` 默认 | `high` | `medium` |
| 输入窗口 / 输出上限 | 1M / 128K | 1M / 128K（**同，判不了落点**） |

🔴 但 `thinking` **不是**上游结构化字段 —— kiro.rs 把它注成正文文本
（`converter.rs:690` 拼 `<thinking_mode>`），所以**上面那个 schema 差异不能当尺子**，
发 `thinking.type=disabled` 给 5.5 不会 400。判落点的唯一办法见 §G「落点尺子」。

`gpt-5.6-sol` 4.4 是全表最贵（是 `qwen3-coder-next` 的 **88 倍**），
要省 credit 先把量大的道往低倍率挪。⚠️ 倍率是**相对权重不是单价**，见 §B。

**⛔ 以下都不构成"能用"的证据：**

- `kiro chat --list-models` —— 那是 **catalog 不是 entitlement**，降档后照样列出跑不动的模型
- 任何代理仓库的内置清单（`xwteam/kiro2api` 的 `models_catalog.rs` 自己文档就写明
  "compiled in, not derived from your pool… not filtered by subscription tier"）
- kiro.dev 文档页
- **HTTP 200 且能回话** —— 见 §G 的静默降级

**⛔ Fable / GPT-6 在 Kiro 上不存在，已穷尽，别再查。** 三条独立证据：
① kiro.dev 26 个章节锚点无 fable，`Fable 5` 全站只出现在**对比文案**里
（Opus 5 段 "approaching Fable 5 at half the cost"）；
② 本账号 `ListAvailableModels` 任何 origin/region 都没有；
③ 真给 kiro.rs 打过 fable 补丁，上游直接回 `INVALID_MODEL_ID`。
四条闸（tier / 主机 / origin / 客户端版本）也全部证伪。
⇒ `claude-fable-*` 返回 400 `模型不支持` 是**正确的硬失败**，不是缺陷。
Fable 5.1 的真身是 Bedrock 的 `us.anthropic.claude-fable-5-1`，我们那条道在阿里云 kuaihui。
**⛔ 连"换个号/升档"这条翻案路也已经走完了**：09-16 用第三个号、且是**更高档的 PRO+**
跑 `ListAvailableModels`，`AI_EDITOR` 与 `KIRO_CLI` 各 19 个、与两个 PRO 号**逐字相同**。
⇒ **档位闸彻底死了，别再提"升档买新模型"。** `kimi` / `k3` 同样不在目录里。
（09-17 第四个号 `rmiglio582`（也是 PRO+）再验一次：`AI_EDITOR` 仍是**同样这 19 个**。）

## §B 查还剩多少 credit

```bash
python3 scripts/kiro-pool.py show --with-quota    # ← 日常用这条（服务端刷，探不死 social 号）
python3 scripts/kiro-probe.py quota               # 直打 AWS，要 subscriptionInfo/overage 细节时才用
```

⚠️ **两条路的取舍见 §0.1**：探针会自己刷凭据，而 social 号刷新**会轮转 refreshToken**
⇒ 常规查余额别用它。⚠️ 两条**都判不出封停**，那是 `catalog` 的活（§A）。

**三个坑，每个都咬过我：**

1. **`getUsageLimits` 是 GET 不是 POST。** 按 REST 直觉猜的三个 POST 变体全回
   `UnknownOperationException`。正确形状抄自源码 `src/kiro/token_manager.rs:341`。
2. **`rateMultiplier` 是相对权重，不是「每发扣这么多 credit」。**
   我按「1 次请求 = 1×倍率」算出"180 发压测耗 210 credits"，写进了飞书报告，**错了 13 倍**。
3. **计量滞后数分钟。** 打 10 发 opus-5 立刻复读 delta **0.00**，几分钟后才以 +0.33 浮现。
   ⇒ **「打完立刻读前后差」是恒读 0 的坏尺子**，要量单模型成本必须等沉降后做 A/B。

`currentUsageWithPrecision` 是**整个计费周期**的累计值，不是本次调用的。

### 📏 单发均价 ≈**0.57 credit**（09-16 实测，窗口对齐）

| 量 | 值 | 来源 |
|---|---|---|
| 窗口 | 06:18:04Z → 09:54:47Z = **3.61h** | pod `status.startTime` |
| 成功调用 | **475** | `GET /api/admin/credentials` 的 `successCount` |
| credit 消耗 | **269.39**（起点 ≈0，号是当天新加） | `getUsageLimits` |
| ⇒ 均价 | **≈0.57 credit/次**、**≈74.6 credits/h** | |

**为什么这次能信**：号是 09-16 06:00Z 新加的，计费月里**除这个 pod 外无其它消费者**，
且 pod 自始未重启 ⇒ 两个计数器窗口真的对齐。
**⛔ 旧记的「≈0.039/次」作废** —— 那次 `successCount` 是**进程生命周期**、
credit 是**计费月**，分母压根不是一个窗口（这正是当时 0.039 与 0.45 差一个量级的原因）。

**仍然没查清 / 别过度解读：**
- 这是**混合模型均价**（当天跑满了 18 条道、含 4.4 倍率的 `gpt-5.6-sol`），
  **不能当单模型价**。要单模型定价仍需 A/B + 等计量沉降。
- 当天含大量回归/压测流量 ⇒ **74.6 credits/h 不是稳态业务速率**，别直接外推。

`overageStatus: DISABLED` ⇒ 用完就停，不会产生额外账单；
该号 `overageCapability: OVERAGE_CAPABLE` / `overageCap 10000` / `overageRate 0.04 USD/credit`，
在 kiro.dev 打开即可付费续用 —— **账单决定，别自己开。**

## §C 加/改模型 = 改源码 + 自建镜像

**⛔ 升 tag 是死路。** ghcr `tags/list?n=500` 全量列完，官方镜像最新只到
**v2026.3.1（2026-03-30）**，**没有 5 系映射**。Opus 5 支持是 master 的 commit
`5ca5703a`（2026-07-27）。
（早前"仓库无 v2026.3.x tag"的说法是错的 —— `tags/list` 不带 `?n=` 只返回首页。）

**⚠️ 2026-09-26 更正：** 早前记的"`5ca5703a` 没出过任何镜像"是错的 —— ghcr 仓库真名是
`ghcr.io/hank9999/**kiro-rs**`（不是 `kiro.rs`，后者 anon token 直接 `DENIED`，
会被误读成"没有镜像"），`tags/list?n=500` 共 189 个 tag，尾部就有
`beta-5ca570`（amd64/arm64 多架构）。**但这不改变"自建"的结论**：
`beta-5ca570` 就是干净 master，**不含我们的 `PASSTHROUGH_MODELS` +
`strip_kiro_alias_prefix()` 两个补丁**，换上去 = 9 个透传 id 和 3 条 `kiro-gpt-*` 道全死。

**查"官方有没有新版"的标准动作（2026-09-26 实测）：**

```bash
# 三处都要查：release 会漏只打 tag 的版本，tag 会漏 master 上的新 commit
ssh <188> "curl -s https://api.github.com/repos/hank9999/kiro.rs/releases?per_page=10 | grep tag_name"
ssh <188> "curl -s https://api.github.com/repos/hank9999/kiro.rs/tags?per_page=10 | grep '\"name\"'"
ssh <188> "curl -s https://api.github.com/repos/hank9999/kiro.rs/commits?per_page=5 | grep -m1 '\"sha\"'"
```

🔴 **`pushed_at` 会骗人**：仓库 `pushed_at=2026-08-10`，但 master HEAD 停在 07-27 ——
08-10 那次 push 落在 **`refactor/v2`** 分支（另一条分支，非 release 线，
最近一条是 credential-pool 刷新写回改字段级）。判 master 动没动只认
`/commits`（默认分支）第一个 sha。

### 源码在哪、改什么

源码 `188:/Data/kiro-build/kiro.rs/`。已经打的补丁：

| 文件 | 改了什么 |
|---|---|
| `src/anthropic/converter.rs` | `PASSTHROUGH_MODELS` 表：9 个非 Claude id → 真实上下文窗口，命中即原样透传 |
| 同上 | `strip_kiro_alias_prefix()`：剥可选的 `kiro-` / `kiro/` 前缀再查表 —— **绕 LiteLLM 重名劫持的逃生舱**，见 §D |
| `src/anthropic/handlers.rs` | 9 个透传条目前置进 `/v1/models` ⇒ 当时 27 个（09-29 后 29）|
| `converter.rs` `map_model()` | **opus-5.5 分支（09-29 加）**：`contains("opus-5-5") \|\| contains("opus-5.5")` → 上游 `claude-opus-5.5`，**必须排在 opus-5 分支之前**（见下条红线）|
| `converter.rs` 窗口表 | `claude-opus-5.5` → 1M input / 128K output（与 entitlement 实测一致）|
| `handlers.rs` `/v1/models` | 加 `claude-opus-5-5` + `claude-opus-5-5-thinking` ⇒ **27 → 29 个** |

#### 🔴 `map_model()` 是子串匹配 —— 新分支写在旧分支后面 = 静默被劫持

`map_model()` 用的是 `contains()`，不是相等。`"claude-opus-5-5".contains("opus-5")`
**为真**，所以 5.5 分支若排在 opus-5 分支之后，发 `claude-opus-5-5` 会一路落到
`claude-opus-5`，**HTTP 200、回复正常、`model` 字段照样回显你发的新名**，零症状。

```rust
// ✅ 顺序：更长/更具体的先判
if m.contains("opus-5-5") || m.contains("opus-5.5") { return "claude-opus-5.5"; }
if m.contains("opus-5")   { return "claude-opus-5"; }
```

单测同理：只断言"发 5.5 得 5.5"会在分支顺序写错时照样绿（因为你只测了一半）。
**必须带阳性对照**：同一个测试里再断言"发 `claude-opus-5` 仍得 `claude-opus-5`"，
否则劫持方向反了也测不出来。

**本轮补丁落地记录（09-29）：** 4 处改动，备份
`188:/tmp/converter.rs.bak.20260929-092717`、`188:/tmp/handlers.rs.bak.20260929-092717`；
测试 200 passed → **207 passed**，失败集合仍是干净 master 那 8 条、逐条相同；
镜像 `kiro-rs:master-opus55` 已 import 进 242 containerd 并 `set image` 切换完成
（`strategy: Recreate`，短暂停机，已获用户同意）。

`-thinking` 后缀在 master 里**只是名字别名**（`converter.rs:100` 直接 strip），
真 thinking 由请求体的 `thinking` 字段（`enabled`/`adaptive`）驱动。

### 构建 → 落地流水线（每一步都有坑）

```bash
# 1. 188 上构建（188 有 docker 28.2.2，/Data 余量够；Dockerfile 三段式，约 4 分钟，产物 17.3MB）
ssh <188> 'cd /Data/kiro-build/kiro.rs && docker build -t kiro-rs:master-<tag> .'

# 2. 回归测试（188 无 rust 工具链，跑在容器里挂源码 + cargo registry 缓存）
#    判据不是"全绿"，是"失败集合与干净 master 基线逐条相同"
#    基线：干净 master 200 passed / 8 failed；打完补丁 207 passed / 同样 8 failed（09-29 opus-5.5 后）

# 3. save → scp（两端必须对 sha256）
ssh <188> 'docker save kiro-rs:master-<tag> | gzip -1 > /Data/kiro-build/img.tar.gz; sha256sum ...'
scp 188:/Data/kiro-build/img.tar.gz 242:/tmp/

# 4. 242 上 import —— ⚠️ 必须先 gunzip！
ssh 242 'gunzip -c /tmp/img.tar.gz | sudo k3s ctr -a /run/k3s/containerd/containerd.sock images import -'

# 5. 两个容器都要改（seed-config initContainer 用的是同一个镜像）
sudo kubectl -n kiro-rs set image deploy/kiro-rs \
     kiro-rs=docker.io/library/kiro-rs:master-<tag> seed-config=同
sudo kubectl -n kiro-rs rollout status deploy/kiro-rs
```

**坑：**
- **`k3s ctr images import` 不吃 gzip**。直接喂 `.tar.gz` 报 `archive/tar: invalid tar header`，
  而且**不给你看清退出码**。必须 `gunzip -c` 管道进去。
- 242 上 **`crictl` 不在 PATH**，`k3s crictl` 缺 config，只能用
  `k3s ctr -a /run/k3s/containerd/containerd.sock`。
- `strategy: Recreate` ⇒ **先杀后起**，镜像没预拉成功就是纯停机。先 `ctr images pull`/import 再 set image。
- **两个容器**（`kiro-rs` + `seed-config`）都得改，漏一个 initContainer 会拉不到镜像。
- 镜像 Dockerfile **只有 CMD 没有 ENTRYPOINT**，只写 `args:` 会让 runc 拿 `-c`
  当可执行文件（`RunContainerError`）；必须同时给 `command: ["./kiro-rs"]`。

### ⚠️ credentials 必须放**可写** PVC

**实测**每次刷新后 app 会把 `accessToken`/`expiresAt`/`machineId`/`subscriptionTitle`
回写进 credentials.json（5289→5738 字节）。只读挂载 ⇒ 这些全丢，Admin UI 加凭据直接失败。
现用 1Gi local-path PVC，initContainer **seed-once**（文件存在就 `keep existing`，绝不覆盖）。
升级后验收要看 initContainer 日志两行都是 `keep existing`。

（"refreshToken 每次刷新都轮换"这句是**未经证实的**，本账号 IdC 实测未轮换，别再引用。
PVC 的正当性由上面那条**已实测**的回写事实支撑。）

## §D 接进 198 LiteLLM

CM `litellm-config` 的 `model_list` **纯加法**（当时 74→92，added=18 / removed=0）。
Secret `litellm-secrets` 加 `KIRO_RS_API_KEY` 用 **merge patch**，别整体替换。
⛔ litellm-proxy 变更**禁 apply**，只用 `set image`/`patch`。

**⚠️ 三条 gpt 道必须带别名前缀。** `gpt-5.6-sol/terra/luna` 恰好是 LiteLLM 内置
OpenAI 名，`main.py:2656` 在判 provider **之前**先查
`litellm.open_ai_chat_completion_models`，于是即便写了 `anthropic/` 前缀 +
`custom_llm_provider: anthropic` 也会被劈去 `POST /chat/completions` ⇒ 恒 404。

- ⛔ **不要去改那张表** —— 生产的 zerokey/chatgpt 池子正靠这些同名模型走 OpenAI 路。
- ✅ 解法在**我们这侧**：`litellm_params.model` 写 `anthropic/kiro-gpt-5.6-sol`，
  这个名字不在 OpenAI 表里 ⇒ 走 Anthropic 路；到了 kiro.rs 再由
  `strip_kiro_alias_prefix()` 剥掉，上游拿到一字不差的原 id。
- 接新上游前自测一行：`python -c "import litellm; print('<id>' in litellm.open_ai_chat_completion_models)"`
- **判据是落点不是 200**：看 pod 日志打的是 `/chat/completions` 还是 `/v1/messages`。

其余 15 条走裸 id。全部 `input/output_cost_per_token: 0`，不进任何 fallback 链。

**⚠️ 生产 key 目前只有 `cursor-liuguoxian04-5rub` 被授权了这 18 个名**
（09-12 授权，143→161，备份 `198:~cltx/kiro-keygrant/models.before.20260912-124309.json`）。
要给别人用得先加。白名单有这个名 ≠ 能用，缺 alias 的裸名照样 400。

### §D1 给单把 key 开 kiro 的 claude 三件套（2026-09-22 实测，`cursor-liuguoxian-std`）

不是把 18 个 `kiro-*` 裸名铺出去，而是**对外用干净名、per-key alias 改写到 kiro 组**
（白名单闸门查的是改写**前**的名，所以目标组名不必进白名单）：

| 对外名（进白名单 + 作 alias 源） | alias 目标（198 组名） | 组的 `litellm_params.model` | 上游 catalog id |
|---|---|---|---|
| `claude-opus-5` | `kiro-claude-opus-5` | `anthropic/claude-opus-5` | `claude-opus-5` 2.2x / 1M |
| `claude-opus-5-5` | `kiro-claude-opus-5-5` | `anthropic/claude-opus-5-5` | **`claude-opus-5.5`** 2.0x / 1M（09-29 加）|
| `claude-sonnet-5` | `kiro-claude-sonnet-5` | `anthropic/claude-sonnet-5` | `claude-sonnet-5` 1.3x / 1M |
| `claude-haiku-4-5` | `kiro-claude-haiku-4-5` | `anthropic/claude-haiku-4-5-20251001` | **`claude-haiku-4.5`** 0.4x / 200K |

⚠️ **haiku 这条三处名字都不一样**，而且上游 catalog 里压根没有
`claude-haiku-4-5-20251001` 这个 id —— 照样 200 出字，因为 kiro.rs 那层
（镜像 `master-kiroalias`）吃得下。**所以"组里配的上游名不在 catalog 里"这个形状
不构成红**，别拿它当故障线索；判死活仍只认 `ListAvailableModels` + 真打。

🔴 **这三个对外名在 198 上全都撞了同名真实组**，per-key alias 压住才落 kiro：

| 对外名 | 同名真实组的落点（没 alias 就去这儿） |
|---|---|
| `claude-haiku-4.5` / `claude-haiku-4-5` | copilot2api-4/-5 · 网宿 `aigateway.edgecloudapp.com` |
| `claude-opus-5` | copilot2api（2 腿） |

⇒ 只加白名单不写 alias = **200 但换了供应商，零症状**。
判落点只认 SpendLogs 的 `model_id`（要看到 `kiro-rs/…`），⛔ 不认 `x-litellm-model-id`（hash）。

⚠️ 还有一层：**客户端发的名字写法**（`claude-haiku-4-5` vs `claude-haiku-4.5`）
差一个字符就 403 死在准入闸门、根本到不了 kiro，而我方探针全绿。
验收最后一道必须查他真 key 的 SpendLogs，见 skill `litellm-198-key-allowlist`
的「客户端发的名字只有他的 SpendLogs 知道」。

⚠️ 开完记得算容量：opus 2.2x / **opus-5.5 2.0x** / sonnet 1.3x / haiku 0.4x，池子是**全员共用**的，
一个 PRO+ 2000 credits ≈ 撑 1 天多（§0）。让人拿 opus-5 当常驻会明显加速烧池。

**09-29 开 `claude-opus-5-5` 的全量车队（三批共 1589 把，零失败）：**

```sh
# 每批都先 --limit 1 打头阵，验完再全量；--apply 必带 --backup
python3 litellm-198-key-allowlist.py --prefix cursor- \
  --add-model claude-opus-5-5 --alias claude-opus-5-5=kiro-claude-opus-5-5 \
  --limit 1 --apply --backup ~/keys-cursor-canary-<ts>.json
```

| 前缀 | scoped | 写入 | readback |
|---|---|---|---|
| `cursor-` | 678 | 677 + 1 头阵 | 678/678 mismatches=0 |
| `claude-` | 674 | 674（含 4 把 unrestricted 在域内）| 674/674 |
| `carher-` | 237 | 237 | 237/237 |

✅ 行为层三向对照（临时 key，用完即删）：
`claude-opus-5-5` → 200 走 alias；`claude-opus-5` → 200 仍落同名真实组（**没被新名劫走**）；
`claude-opus-4-8`（故意不进白名单）→ **403 `key_model_access_denied`**（证明闸门真在拦）。
⛔ 少了第三发就答不了"白名单到底有没有在生效"，只有前两发的绿是**恒绿**。

### §D1.1 kiro 组的兜底链（2026-09-26 补齐 7 条）

kiro 池是**共用且会被打满**的（§0 / §D4 id8 真兑现过 `QuotaExceeded`），
所以每个对外挂 kiro 的组都必须有兜底腿，否则池子一空就是用户面直接失败。
09-26 把 7 个 `kiro-claude-*` 组补进全局 `router_settings.fallbacks`
（**96 → 103 行**，脚本 `scripts/litellm-198-fallback-add-row.py`）：

| 组 | 兜底目标（按顺序） |
|---|---|
| `kiro-claude-opus-5` | `anthropic.wangsu5.claude-opus-5` |
| `kiro-claude-opus-4-8` | `anthropic.claude-opus-4-8` → `anthropic.wangsu.claude-opus-4-8` |
| `kiro-claude-opus-4-7` | `anthropic.claude-opus-4-7` → `anthropic.wangsu.claude-opus-4-7` |
| `kiro-claude-opus-4-6` | `anthropic.claude-opus-4-6` → `anthropic.wangsu.claude-opus-4-6` |
| `kiro-claude-sonnet-5` | `kuaihui.claude-sonnet-5` → `anthropic.wangsu.claude-sonnet-5` |
| `kiro-claude-sonnet-4-6` | `anthropic.claude-sonnet-4-6` → `anthropic.wangsu.claude-sonnet-4-6` |
| `kiro-claude-haiku-4-5` | `anthropic.claude-haiku-4-5` |

**09-29 补第 8 行（103 → 104 行）：**

| 组 | 兜底目标 |
|---|---|
| `kiro-claude-opus-5-5` | `anthropic.wangsu5.claude-opus-5` |

⚠️ **兜底降了一个小版本，不是同名兜同名** —— 线上**没有任何非 kiro 的 opus-5.5 组**
（09-29 逐个列过：最高就是 `anthropic.wangsu5.claude-opus-5`）。
⛔ 别为了"名字对齐"把它兜到 `kiro-claude-opus-5` —— 那是**同一个 kiro 池**，
池子打满时两条腿一起空，等于自环（§D1.1 全部 7 行都刻意兜到异族，就是这个原因）。

🔴 **这 7 行正是 per-key alias 那批用户唯一够得着的兜底。**
per-key `aliases` 在 **router 之前**就把 `data["model"]` 改掉了
（`litellm_pre_call_utils._update_model_if_key_alias_exists`，09-26 读运行 pod 源码确认），
所以 router 查 fallback 时看到的是**改写后**的名 `kiro-claude-*` ——
⛔ 别和 router 级 `model_group_alias`（查**改写前**）搞反，两条规则方向相反。

⇒ 09-24 记的「haiku 兜底自环」，真相是**那批 key 压根没有兜底行**：
查表用 `kiro-claude-haiku-4-5`，而当时这个 key 在表里不存在。09-26 这 7 行补的就是它。

裸名那几行（`claude-haiku-4-5` / `claude-haiku-4.5` → `kiro-claude-haiku-4-5`）**保持不动**：
它们服务的是**没有这条 alias** 的 key —— 那种 key 主路走同名真实组
（copilot2api / 网宿），兜到 kiro 是一条正经的异族腿，不是自环。

验收：层1 回读 103 行 ✅、层2 四个在服务 pod 逐个回读到 ✅、
🔴 **层3「真兜住」未证** —— 只能等池子真打满时看 SpendLogs 的
`model_group` 与 `model` 落在不同族。三层语义见 skill `litellm-198-fallback-chain`。

⛔ **kiro 整个没有 fable 族**。任何把 `*fable*` 名字往 kiro 上收敛的动作
都是「这个名字直接消失 ⇒ 403」，不是换供应商 —— fable 必须留在 cursor 腿上。

## §D2 换账号 + 挂境外出口（2026-09-14 实测 18/18，~6 分钟）

Kiro 账号会被 AWS 封（09-12 就封过一次），换号是常规动作，不是意外处理。
**换号和挂代理一次做完**，别分两轮 —— 新号第一次碰 AWS 就不该从国内 IP 出去。

### 1. 先在本地把新号验通（读操作，不动线上）

用户给的凭据一般只有 `email`/`refreshToken`/`clientId`/`clientSecret`，
**缺 `accessToken` 和 `machineId`**。`machineId` 自己造（`secrets.token_hex(32)`），
`accessToken` **不用手搓 curl 了** —— 写一个只含新号的临时 json 喂给探针，
它遇到 401/403 会自己走 OIDC refresh：

```sh
python3 scripts/kiro-probe.py both --creds /tmp/newcred_lgx.json
```

⚠️ **出口恒从线上 `config.json` 读，不认 `HTTPS_PROXY`**（09-16 改的）。
环境变量是隐式的，跑的人看不见自己在量哪条路；现在脚本第一行就把出口打出来（口令打码）。
要对照直连才加 `--no-proxy`。手搓 OIDC 的形状仍保留在这里备查：

```sh
curl -x http://astrill:<pw>@10.68.13.243:8119 -s https://oidc.us-east-1.amazonaws.com/token \
  -H 'content-type: application/json' \
  -d '{"clientId":"...","clientSecret":"...","grantType":"refresh_token","refreshToken":"..."}'
# → {accessToken, expiresIn:3600, refreshToken, tokenType:"Bearer"}
```

组 `credentials.json` 时**字段集必须跟线上逐字一致**（少一个字段不报错，行为却变）：
```
id, accessToken, refreshToken, expiresAt(ISO), authMethod, clientId, clientSecret,
authRegion, apiRegion, machineId, email, subscriptionTitle, disabled
```
- `machineId` = **64 位 hex**（`secrets.token_hex(32)`），不是 uuid。
- `authMethod` 取 `"idc"`：`idc`/`builder-id`/`iam` 在 `token_manager.rs` 走同一分支，
  但取线上已验证过的那个值最省事。
- 写完拿 `set(新) == set(线上)` 断言一次，**别肉眼比**。

### 2. kiro.rs 原生支持代理 —— 不要去折腾环境变量

`config.json` 加三个键（serde `rename_all="camelCase"`）：

```json
"proxyUrl": "http://10.68.13.243:8118",
"proxyUsername": "astrill",
"proxyPassword": "<pw>"
```

出口见 skill `astrill-243-exit-ops`（8118 San Jose / 8119 Seattle / 8120 LA）。
LXD proxy device listen 在 `0.0.0.0`，**198 集群的 pod 可以直接打**。

### 3. 落地与判据

```sh
# 先备份，再 kubectl cp 进 /app/config/（PVC），再 rollout restart
sudo kubectl -n kiro-rs cp credentials.new.json kiro-rs/$POD:/app/config/credentials.json -c kiro-rs
sudo kubectl -n kiro-rs cp config.new.json      kiro-rs/$POD:/app/config/config.json      -c kiro-rs
sudo kubectl -n kiro-rs rollout restart deploy kiro-rs   # strategy: Recreate，见 §G
```

**三把尺子，缺一条都不算完：**

| # | 尺子 | 绿的样子 |
|---|---|---|
| 1 | 启动日志 | `已配置 HTTP 代理: http://10.68.13.243:8118` ← **kiro.rs 内置的阳性对照** |
| 2 | pod 内 `netstat -tn` | 发请求时唯一出站是 `→ 10.68.13.243:8118 ESTABLISHED`，**零条直连 AWS** |
| 3 | 真实推理 | 19 条道**文本非空**（不是 200），且各道 `prompt_tokens` 互不相同 |

⛔ **别在 pod 里用 `wget` 打 IP 回显站验代理。Alpine busybox wget 完全无视
`https_proxy`** —— 带/不带代理都读出宿主 IP，看起来像"代理没生效"。
拿死代理 `http://127.0.0.1:1` 做阴性对照会发现它**照样成功** ⇒ 这把尺子零判别力。

尺子 2 的抓法（不需要节点 shell，也不需要 tcpdump）：后台发请求，同时循环
`kubectl exec $POD -- netstat -tn | grep ESTABLISHED | grep -v :8990`。

### 4. 验收 key 用完即删

生产 key 的明文拿不回来（DB 只存 hash）。造一把 2h 的临时 key 复刻形状：
`POST /key/generate {"key_alias":"tmp-kiro-verify-lgx","models":[18个名],"duration":"2h"}`
打 `http://10.43.149.225:4000`（svc `litellm-proxy`），master key 在
secret `litellm-secrets` 的 `LITELLM_MASTER_KEY`。**跑完 `/key/delete`**。

⚠️ 批量压这 19 条时 `curl -m 120` 会偶发切断慢道（opus-4-6 中过一次，
单独复打两次都秒回）。**别把超时读成"这条道坏了"**，复打再判。

## §D3 多号池：加一个账号（2026-09-16 实测，无需重建镜像）

**kiro.rs 是真的多凭据池**，不是"只用 `[0]`"：`credentials.json` 是 list，
`config.json` 的 `loadBalancingMode` 取 `priority`/`balanced`，并且有整套 admin API
（`src/admin/router.rs`）：

```
GET    /api/admin/credentials              # 池状态：total/available/currentId/每条 success·fail·disabled
POST   /api/admin/credentials              # 运行时加凭据（不重启）
DELETE /api/admin/credentials/{id}
POST   /api/admin/credentials/{id}/disabled|priority|reset|refresh
GET    /api/admin/credentials/{id}/balance
GET|PUT /api/admin/config/load-balancing
```

认证用 `config.json` 的 **`adminApiKey`**（`x-api-key` 头），业务用 `apiKey`。

### 加号步骤（09-17 走的是 **零重启 admin API**，6 分钟，推荐这条）

**不改出口就别重启。** 走脚本：

```sh
python3 scripts/kiro-probe.py both --creds /tmp/newcred.json   # 1. 只读验号，不碰线上
python3 scripts/kiro-pool.py --dry-run add /tmp/newcred.json   # 2. 看请求形状
python3 scripts/kiro-pool.py add /tmp/newcred.json             # 3. 真加（自动备份+去重+前后复核）
python3 scripts/kiro-pool.py show --with-quota                 # 4. 看在役号还剩多少，再决定切不切
scripts/kiro-lane-regress.sh --rounds 3                        # 5. 19 条道回归（§E）
```

⚠️ 第 4 步**默认不切**：在役号还有余额时 `priority <老号id> 9` 等于把两个号的余额
摊薄成半个。老号打满时 kiro.rs 自己 `QuotaExceeded` + `min_by_key` 重选 ——
**这条兜底 09-22~24 真的兑现过，全程零人工**（§D4 id8）。

每步的判据与背后的坑：

0. 🔴 **先把导出 json 拍平成线上形状 —— 两个脚本都只认扁平格式。**
   account-manager v1.7.x 导出把 token **嵌在 `credentials` 子对象里**
   （`accounts[0].credentials.{refreshToken,clientId,clientSecret,region,…}`），
   而 `machineId`/`email` 在**外层**；`authMethod` 导出写 **`"IdC"`**，线上是小写 **`"idc"`**。
   直接喂原始导出 ⇒ `kiro-pool.py` 报「缺必需字段 `['refreshToken',…]`」。
   拍平时**断言字段集 ⊆ 线上字段集**（`assert not set(flat)-live`），别肉眼比。
   线上那 14 个键：`accessToken apiRegion authMethod authRegion clientId clientSecret
   disabled email expiresAt id machineId refreshToken region subscriptionTitle`
   —— 拼 4 个必需的 + `email`/`region`/`authRegion`/`apiRegion`/`authMethod` 就够，
   `id`/`expiresAt`/`disabled`/`subscriptionTitle` 服务端自己填。

   **导出还有第二种形状（09-17 21:3x 遇到）：扁平单元素数组，没有 `accounts`/`credentials`
   包层，也没有 `machineId`/`region`/`authMethod`，另带一个 `provider: "BuilderId"`。**
   三件事要做，都有实证依据、别猜：
   - 🔴 **`provider` 不是 kiro.rs 读的字段**（源码里零引用，`AddCredentialRequest` 也没有
     `deny_unknown_fields` ⇒ 传进去只是被忽略）。真正分发 refresh 路径的是 **`authMethod`**：
     `token_manager.rs:131` 把 `idc`/`builder-id`/`iam` 都送去 `refresh_idc_token`
     （`https://oidc.<region>.amazonaws.com/token`），其余走 `refresh_social_token`
     （`prod.<region>.auth.desktop.kiro.dev/refreshToken`）。
     而 admin 的 **`authMethod` 默认值是 `"social"`**（`admin/types.rs:143`）⇒
     **BuilderId 的号必须显式传 `"idc"`**，漏了就去打另一条端点。
     （未指定时 `token_manager.rs:122` 还有个「有 clientId+clientSecret 就当 idc」的兜底，
     但那是 refresh 时的推断，admin 入口的 default 先落成 social 了，别指望它。）
   - **`machineId` 缺失可以补，但要补成「服务端此刻本来就会用的那个值」再钉死**：
     `machine_id.rs::generate_from_credentials` 的顺序是 凭据级 → 全局 `config.machineId`
     （**线上没配**，已核）→ 按类型派生（OAuth = `sha256("KotlinNativeAPI/<refreshToken>")`）
     → 随机兜底。所以不传也能跑，但 🔴 **refresh 成功后若响应带新 refreshToken 会被回写落盘**
     （`token_manager.rs:305`）⇒ **派生式的指纹会随 token 轮换而漂**。
     ⇒ 加号时自己算一次 `sha256("KotlinNativeAPI/<refreshToken>")` 写进 `machineId` 固化，
     既与服务端当前行为逐字节一致，又不会漂。`normalize_machine_id` 只认 64 位 hex
     或 UUID（去横线 32 位，重复一次补到 64），别塞别的形状。
   - **多个号的导出长得很像**（`clientSecret` 都是 4797 字符的三段 JWT）⇒
     **比 sha256，别比长度**。09-17 就是靠比线上 id3/id4 的三个字段哈希，才确认新号不是抄串。
     ⛔ 比之前先确认**集合非空** —— 09-17 第一次比出 `相同: False (比对了 0 个)`，
     那是**空集假绿**不是通过。
     ✅ **这一整步 09-24 起由 `kiro-pool.py add` 的 `dup_check()` 自动做**（比盘上每个号的
     `refreshToken` sha256，先断言盘上非空、命中就 `exit` 不发请求，邮箱同名但 token 不同
     会给 ⚠️）。手搓比哈希这件事不用再做了 —— **但也别把它当成"验过号了"，它只判重复。**
   **导出还有第三种形状（09-24 遇到）：只有 `email` + `refreshToken` + `provider:"Github"`。**
   🔴 **这是 social 号，不是 idc —— 「缺 clientId/clientSecret」是正常的，不是导出残缺。**
   分流的唯一依据是 `authMethod`（`token_manager.rs:122-138`）：`idc`/`builder-id`/`iam`
   走 AWS OIDC（**那条路第一件事就是 `ok_or_else(|| "IdC 刷新需要 clientId")`**），
   其余走 `refresh_social_token` → `https://prod.<region>.auth.desktop.kiro.dev/refreshToken`，
   body **只有 `refreshToken`**，压根没有 client 凭据。
   ⇒ 09-24 前 `kiro-pool.py` 硬写 `authMethod:"idc"` 且强制要求 clientId/clientSecret，
   **这种号一律被它拒收**；两个脚本已各加 social 分支（`auth_method_of()`，与源码逐字对齐）。
   ⛔ **`provider` 字段不要读**（`Github`/`BuilderId`/…）：源码零引用、
   `AddCredentialRequest` 也没有它 ⇒ 传进去只被忽略。判 social 看的是「有没有 client 凭据」。
   🔴 **social 刷新会轮转 refreshToken**（`token_manager.rs:206` 明写「响应带就覆盖」），
   **与 IdC 相反**（IdC 实测不轮转）⇒ **一次只读探测就可能把用户给的 token 换掉**。
   所以探针加了 `--out-rotated FILE`（0600，`finally` 里落盘 —— 探到一半抛异常时轮转
   **已经发生在服务端**了，不落盘就等于把凭据弄丢），**必须拿轮转后的那份去 `add`**。
   ⚠️ 09-24 这一次实测**没有轮转**（前后 sha256 相同，服务端盘上仍 len=232）——
   一个样本，**别据此说"social 不轮转"**，代码按会轮转处理。
   （09-26 `gurjardevbax8`（Google）第 2 个样本，同样没轮转 —— 两个样本仍不翻案。）

探针遇到 401/403 自己走
   OIDC refresh，**用户给的 `accessToken` 过期不影响**。
   绿的样子：`AI_EDITOR` **19 个模型** + 档位/额度读出来（这一步同时证明**不是封停号**）。
   ⚠️ 导出 json 里的 `expiresAt` 是**毫秒 epoch**，线上格式是 **ISO** ——
   `kiro-pool.py add` 索性**不传这个字段**，让服务端自己 OIDC 换，别手动转。
   ⚠️ `clientSecret` 是 ~4.8KB 的三段 JWT，**用 heredoc 单独写文件再注入**，
   跟别的字段挤在一个 heredoc 里容易被截断（09-17 就丢过一次，脚本当场拦下了）。
2. **备份**由 `add` 自动做（`198:~/kiro-deploy/backup/credentials.json.$TS`，0600，打印回滚提示）。
3. **POST /api/admin/credentials**（camelCase 字段：`refreshToken` `authMethod`
   `clientId` `clientSecret` `priority` `region` `authRegion` `apiRegion` `machineId` `email`）。
   🔴 **必需字段按 `authMethod` 分流，不是一刀切**（脚本 `REQUIRED` 表）：
   `idc` 要 `refreshToken`+`clientId`+`clientSecret`；**`social` 只要 `refreshToken`**。
   缺了当场拒绝而不是发出去；`machineId` 缺失则自动按 `sha256("KotlinNativeAPI/<rt>")` 派生并钉死。
   服务端自己走 OIDC 换 token、按 `max(id)+1` 分配 id、**并 `persist_credentials()` 落 PVC**
   （`token_manager.rs:1654`）⇒ **加完就能扛重启，不需要再 `kubectl cp`**。
   ⛔ 手搓时 body 一律走文件（`curl --data @file` / `wget --post-file`），
   **绝不嵌套 ssh + `\x27` 转义**（会被 shell 搞坏回 400）。
4. 🔴 **加号这一刻 `currentId` 通常不切到新号 —— 但"切不切"要看老号还剩多少，别条件反射地切。**
   这句只描述 **POST 返回后立刻复核** 的那一拍，不是稳态保证（09-28 自己切过，见 §D4）。
   priority 模式 **数字小 = 优先**（`min_by_key(priority)`），新号 priority 0 与老号并列时
   取**先出现的那条**，于是老号仍占着 `isCurrent: true`。
   - 老号**已耗尽/被封** ⇒ 必须切：`kiro-pool.py priority <老号> 9`
     （内部调 `select_highest_priority()`）。判据：`currentId` 变成新号。
   - 老号**还有余额**（09-17 21:19 加 id4 时 id3 还剩 1902/2000）⇒ **别切**，
     让新号当备用。老号打满时 kiro.rs 自己 `QuotaExceeded` 禁用它并 `min_by_key` 重选，
     **这才是真兜底**；提前切等于把两个号的余额都摊薄成半个。
   `add` 发现 currentId 没动会打警告 —— 那是提醒你**做判断**，不是让你无脑执行那条命令。
5. **回归**（真推理，判据不是 200）：`litellm-lane-bench.py --models @lanes.txt --rounds 1`。
   09-17 换到 id 3 后实测 **18/18 成功，TTFT p50 1.55 / p95 4.07**，
   `input_tokens` 逐道分层正常：opus-5 **6765** / sonnet-5 6482 / opus-4-6 4171 /
   sonnet-4-5 4143 ⇒ 无静默降级。
   ⚠️ 三条 gpt 道这次 `in≈429`（09-12 基线记的是 ≈1,580）—— 上游前导变了，不是降级信号；
   **同族三条一致是预期**，跨族雷同才是降级。
6. **收尾删密**：`kiro-pool.py` 自己 `trap` 删 198 上的 `/tmp/.kiro_{curlrc,body}.*`；
   手搓路径下 pod 内 `/tmp/{add_body,ak,prio}.json`、198 `/tmp/kiro_*`、本机临时 creds 要自己删。

**要顺带改全局出口才走"写文件 + rollout restart"那条路**（§D2 的 3.）。

### 换出口 IP（09-17 19:36 实测 8119→8120，约 40s）

只改 `config.json` 一个键 `proxyUrl`，`kubectl cp` + `rollout restart`（Recreate，短停机）。

```sh
# 备份 → python 改键并断言 set(keys) 不变 → cp → 复核 pod 内含新端口 → restart
```

🔴 **restart 前必须确认 priority/disabled 已落盘**，因为重启会重新反序列化那个文件：
启动选号是 `filter(!disabled).min_by_key(priority)`（`token_manager.rs:639`），
而 `priority` 带 `#[serde(default)]` + `skip_serializing_if = "is_zero"`
⇒ **priority 0 的号在文件里根本没有这个字段**，这是正常的，不是丢了。
09-17 实测：id3 无 `priority` 键、id2 `priority:9 disabled:true` ⇒ 重启后 `currentId` 仍是 3。

**四条尺子（缺一不算完）：**
① 启动日志 `已配置 HTTP 代理: http://10.68.13.243:8120` ← 内置阳性对照；
② seed-config 两行都是 `keep existing`（否则 PVC 被重新 seed，凭据会退回旧快照）；
③ `GET /credentials` 的 `total/available/currentId` 与重启前一致；
④ 18 条道真推理回归（09-17 换 8120 后 **18/18，TTFT p50 1.32**，`input_tokens` 与换之前
逐道吻合：opus-5 6765 / sonnet-5 6482 / opus-4-6 4171 ⇒ 换 IP 没引入降级）。
⚠️ 单发样本里 `opus-4-5` 17.03s、`minimax-m2.1` 8.77s 这种长尾**不是出口坏了**
（换之前同样出现过 5.45s 级别的抖动），判出口质量要多轮，别拿 1 发的 max 定性。

**⛔ 选不到"干净的独占 IP"**：三条出口都被 `chatgpt-acct-*` 共用，
换端口只是换共用对象，不构成"这个号独享一个 IP"。

### 🔴 三个坑

- **`id` 是 `Option<u64>`**（`src/kiro/model/credentials.rs:19`）⇒ 必须写 **JSON 整数**。
  写成 `"2"` 字符串 serde 直接失败。raw 文件里就是 `"id": 1`。
- **⛔ admin 的 POST body 不要用嵌套 ssh + `\x27` 转义**：body 被 shell 搞坏回 **400**，
  而 `/{id}/disabled` 那次**回了 400 却仍然把号禁用了**（我因此一度关停了 beltmike66）。
  ⇒ 正解 `printf %s '{"disabled":false}' > f; kubectl cp f pod:/tmp/; wget --post-file=/tmp/f`。
  **任何 admin 写操作之后都要用 `GET /credentials` 复核实际状态，别信返回码。**
- **pod 里只有 wget**，没有 netstat/curl/python3 ⇒ §D2 那把 netstat 尺子在容器内跑不了。
  但 kiro.rs **没有直连 fallback**（`build_client` 恒挂 proxy），
  ⇒ **一发真推理成功本身就是"走了代理"的证据**。（想抓 netstat 得上节点，
  但 `cltx` 登不上 242，publickey/password 全拒。）
- 🔴 **批量 disable/enable 测试禁用 `set -e`**：某号被禁用后打它会返回**空 body**，
  用 python 解析空 body 非零退出 → `set -e` 中断整块 → **re-enable 没跑，号被留在 disabled**。
  每一步 admin 写之后都 `GET /credentials` 复核实际状态。

### 判某个账号 opus-5 正不正常（回归判据）

- **不是 200，是 `input_tokens`**：opus-5 ≈**6.8k**（实测 6787），同句 sonnet-4.5 ≈**4.1k**（4121）。
  两者 token 一样大 = 静默降级（§G）。09-16 barisibetter9 实证 6787 ⇒ opus-5 真身正常。
- 🔴 **上游 `402 Payment Required / reason=MONTHLY_REQUEST_COUNT` 的真身就是那 1000/2000 credit 打满**，
  不是另一把独立的"月请求数"尺子。09-16 实探 beltmike66：
  `currentUsageWithPrecision 1000.0 / usageLimit 1000`、`unit:"INVOCATIONS"`、`resourceType:"CREDIT"`、
  `nextDateReset` = **每月 1 日 00:00 UTC**（1790812800 = 2026-10-01）。
  ⇒ **402 之后第一件事是重读 `getUsageLimits`**，它看得见；别拿 402 的 reason 字符串猜底层量具
  （我曾据此断言"getUsageLimits 不预告"，是无证据归因，已撤回）。
  命中后 kiro.rs 自动禁用该号（`disabledReason:"QuotaExceeded"`）**并回写 PVC**（`disabled` 真变 true）。
  ⇒ 判"某模型打不动"先看是不是**这个号**402 了，别赖到模型头上。
  🔑 续命选项：`overageCapability: OVERAGE_CAPABLE` + `overageCap 10000` + `overageRate 0.04 USD/credit`，
  默认 `overageStatus: DISABLED`；在 kiro.dev 打开即可付费续用。**账单决定，别自己开。**

### 摘掉一个账号 —— 走 `DELETE`，零重启（09-17 19:52 实测，约 1 分钟）

09-16 那版是「改文件 + `rollout restart`」，**已被这条取代**（那条只在 admin API 不可用时才回退用）。

```sh
python3 scripts/kiro-probe.py quota                 # 先逐号判死活（余额+catalog，见下 🔴）
python3 scripts/kiro-pool.py --dry-run remove <N>   # 看清要发什么
python3 scripts/kiro-pool.py remove <N>             # 备份 → disable → 验 → DELETE → 复核
```

脚本里 disable 与 DELETE 之间**卡了一道验证**（`GET /credentials` 确认真 disabled 了才继续），
因为 admin 写操作**回了 400 也可能已经生效**，返回码不是判据。

手搓回退路径（脚本坏了/admin API 不通时）：

```sh
BASE=http://10.43.109.5:8990/api/admin   # 在 198 host 上跑，pod 里没 curl

# 1) 备份（含被删号的完整凭据，将来重置后可据此复活）
sudo kubectl -n kiro-rs exec deploy/kiro-rs -c kiro-rs -- cat /app/config/credentials.json \
  > ~/kiro-deploy/backup/credentials.json.pre-del-id<N>.$(date +%Y%m%d-%H%M%S)
chmod 600 ~/kiro-deploy/backup/credentials.json.pre-del-id<N>.*

# 2) 先禁用（未禁用的删不掉，见下），再删
curl -K "$CFG" -X POST -H 'Content-Type: application/json' \
  --data '{"disabled":true}' "$BASE/credentials/<N>/disabled"
curl -K "$CFG" -X DELETE -w '\nHTTP %{http_code}\n' "$BASE/credentials/<N>"
```

🔴 **`delete_credential` 硬拦未禁用的号**：`token_manager.rs:1797` ——
`if !entry.disabled { bail!("只能删除已禁用的凭据") }`。
打满被自动 `QuotaExceeded` 的号已经是 `disabled:true`，可直接删；手动摘活号必须先 `disabled`。
删的是 `currentId` 时它会自己 `select_highest_priority()` 重选，
删空则 `current_id` 归 0；**`persist_credentials()` + `save_stats()` 都在函数里** ⇒ 落盘，无需 restart。

⛔ **`adminApiKey` 不进 argv。** pod 里是 **BusyBox wget，不认 `--method=DELETE`**，
也没有 `python3`/`curl` ⇒ **在 198 host 上用 `curl` 直打 ClusterIP `10.43.109.5:8990`**，
key 写进 `curl -K <配置文件>`（`header = "x-api-key: …"`），umask 077 + trap 删。
key 的来源是 `secret/kiro-rs-bootstrap` 的 `config.json`（ns 里**没有** `cm/kiro-rs-config`）。

验收四条（前三条 `kiro-pool.py` 每次写操作前后自动各打一遍）：
① `GET /credentials` 的 `total/available/currentId`；
② pod 里 `cat /app/config/credentials.json` 条数同步减少（证明落盘）；
③ `kubectl get pod` 的 `RESTARTS` 与 `startTime` **没变**（证明零重启）；
④ 18 条道真推理回归（09-17 删 id2 后 **18/18，TTFT p50 1.44 / p95 4.35**，
`input_tokens` 分层正常：opus-5 6765 / sonnet-5 6482 / opus-4-6 4171 / gpt 道 429-430）。

## §D4 账号台账 —— **只记不可推导的部分**

🔴 **池子的当前状态不在这里。** id / 档位 / live 余额 / 在役哪个 / disabled 与否，
一律现读：`python3 scripts/kiro-pool.py show --with-quota`。
（这张表 09-17~09-24 手维护漂过两次 —— 写死的余额和"在役是 id7"都过期了才被发现。）
这里只留**读不出来的东西**：每个号怎么进来的、备份在哪、踩过什么坑。

### 在池子里的（加入经过 + 备份路径）

| id | 账号 | 加入 | 备份 `198:~cltx/kiro-deploy/backup/` | 那次的特殊点 |
|---|---|---|---|---|
| 3 | `rmiglio582@gmail.com` | 09-17 | — | — |
| 4 | `alhassanrashida749@gmail.com` | 09-17 21:19 | `credentials.json.20260917-211915` | **故意不切 currentId**（id3 还剩 1902，见 §D3 第 4 步） |
| 5 | `kumistephen070@gmail.com` | 09-17 21:35 | `…20260917-213531` | 第一次遇到**扁平无 `machineId`** 形状（`provider: BuilderId`），`machineId` 按 `sha256("KotlinNativeAPI/<rt>")` 固化 |
| 6 | `adafiasenyo444@gmail.com` | 09-17 22:10 | `…20260917-221052` | 同 id5 形状，全程照 §D3 走**无新坑** |
| 7 | `edemamoahalbert@gmail.com` | 09-21 10:30 | `…20260921-103044` | 加时其余 4 个全 disabled ⇒ **服务端自己选中它，没执行 `priority` 命令** |
| 8 | `princessrit15@gmail.com` | 09-21 13:06 | `…20260921-130626` | 🔴 **故意不切 currentId 的那条兜底 09-22~24 真的兑现了**：id7 打满后 kiro.rs 自己 `QuotaExceeded` + `min_by_key` 选中 id8，**全程零人工** |
| 9 | `tetteyfrederick506@gmail.com` | 09-21 17:03 | `…20260921-170332` | — |
| 10 | `msajjadousavi@gmail.com` | 09-21 17:03 | `…20260921-170350` | id9+id10 合并跑一次回归即可 |
| 11 | `geovanamaya@googlegroups.com` | 09-24 09:01 | `…20260924-090110` | 🔴 **全池第一个 `authMethod:social`（Github）号**，也是第一个 **KIRO PRO MAX（5000）**。导出只有 `email`+`refreshToken`+`provider:"Github"`，两个脚本为此各加 social 分支（§D3 第 0 步第三种形状）。服务端日志实测打的是 `正在刷新 Social Token...`（不是 IdC），落盘带 `profileArn` |
| 12 | `gurjardevbax8@gmail.com` | 09-26 15:52 | `…20260926-155201` | `provider:"Google"` —— **第三种 social 来源**（前有 Github/BuilderId），走的路与 id11 逐字相同：导出只有 `email`+`refreshToken`，`authMethod` 由 `auth_method_of()` 判成 `social`，`machineId` 按 `sha256("KotlinNativeAPI/<rt>")` 派生固化。**全程零新坑，脚本直通**。PRO MAX 5000 满额。⚠️ **本次探测又没轮转 refreshToken**（前后 sha256 相同）—— 这是第 2 个样本，仍**不足以说"social 不轮转"**，`--out-rotated` 照带 |
| 13 | `ananada-yuutt@googlegroups.com` | 09-28 14:53 | `…20260928-145344` | `provider:"Github"`，形状与 id11/id12 相同，脚本直通零新坑。🔴 **全池第一个 `KIRO POWER`（10000 credits）** —— 见下「档位」那条。⚠️ social 第 3 个样本**又没轮转**（前后 sha256 相同，len=230）。🔴 **加完后 `currentId` 自己从 10 变成了 13，我没执行 `priority`** —— 见下 |

### 已经不在池子里的

| 账号 | 判定 | 处置 |
|---|---|---|
| `bradburybruns64@gmail.com` | ❌ **AWS 封停**（09-12 起，catalog 403 `User ID c488d458-… temporarily is suspended`），**触发原因至今未查清**。KIRO FREE 余额还剩 33.66 —— 🔴 **这就是"余额判不出封停"的原始证据** | 09-14 换号时移出；重置只补 credit，**补不了封停**。申诉 `https://app.kiro.dev/account/usage?support_form` |
| `beltmike66@gmail.com` | ✅ 真打满（KIRO PRO `1000/1000`） | 09-16 摘除，备份 `credentials.json.20260916-141659` |
| `barisibetter9@gmail.com` (id2) | ✅ 真打满（PRO+ `2000/2000`，09-17 06:35，succ 2895） | 09-17 19:52 `DELETE /credentials/2`，备份 `credentials.json.pre-del-id2.20260917-195234`（含完整凭据，可据此复活） |

### 会咬人的读数纪律

🔴 **档位至今见过四种，额度差 10 倍 —— 算容量前必须读 live cap，别按某个数硬算。**
`KIRO PRO` 1000 → `KIRO PRO+` 2000 → `KIRO PRO MAX` 5000 →
**`KIRO POWER`（`Q_DEVELOPER_STANDALONE_POWER`）10000**（09-28 id13 首见）。
⇒ §0/§B 那句「一个号 2000 credits ≈ 撑 1 天多」**只对 PRO+ 成立**。
⚠️ 四档的 entitlement **逐字相同**（PRO MAX 是 19 个那版，POWER 是 09-26 起的 20 个含
`claude-opus-5.5`，差异来自目录本身变了、不是档位）
⇒ **升档只买额度，不买模型**，§A 那条「档位闸死了」继续成立。

🔴 **`currentId` 会在无人工干预下变，别把「不会自己切到新号」当恒真。**
09-28 加 id13 时脚本复核明确打出 `currentId 仍是 10`，几十分钟后（只跑了只读回归）
再读已经是 **13**，而我**从未执行 `priority` 命令**。
且 id10 当时还剩 **1575/2000**，不满足已知的 `QuotaExceeded` 那条兜底路径。
⇒ **为什么切没有数据，不下归因**。只记事实：§D3 第 4 步那句"不会自己切"描述的是
**加号那一刻**，不是稳态保证；判在役是谁一律现读 `show`。

🔴 **`disabledReason` 会变，但它不是额度尺子。** 09-21 13:06 读到 id3/id6 是
`QuotaExceeded fail=3`，同日 17:03 再读变成 **`Manual fail=0`**。**我没动过这两个号**，
中间发生了什么**没有数据，不下归因**。判额度只认 live 读数（这四个号当时都是 `0.0/2000`），
`reason` 变了不改变"它们确实空了"这个事实。

🔴 **导出里的 `usage`/`subscription` 是导出那一刻的快照，不是当前值。**
09-21 用户发来 `alhassanrashida749` 的导出写着 `usage.current: 0 / limit: 2000`，像个满额新号 ——
但 `exportedAt` 解出来是 **09-17 08:13 UTC**，而该凭据（三个字段 sha256 与线上 id4 **逐字节相同**）
live 读出来是 **`2000/2000` 已耗尽**。
⇒ 判额度只认实时读；**判「是不是重复号」只认 `refreshToken` 的 sha256**，不看邮箱、
不看导出里的 `id`/`userId` —— 这一条现在由 `kiro-pool.py add` **自动做**（§0.1）。

### 重置日复活（`nextDateReset` 全是 `2026-10-01 00:00 UTC`）

```sh
python3 scripts/kiro-pool.py extract \
  ~/kiro-deploy/backup/credentials.json.pre-del-id2.20260917-195234 barisibetter9 --out /tmp/c.json
python3 scripts/kiro-probe.py both --creds /tmp/c.json   # 判活（走生产同一条出口）
python3 scripts/kiro-pool.py add /tmp/c.json && rm -f /tmp/c.json
```

⚠️ `/tmp/c.json` 里是 live refreshToken，**探完立刻删**。
`disabled:true` 的条目**照样会被探**（探针只在标题打 `[disabled]`，不跳过），不用改副本。
⚠️ 还在池里的 id3~id7 到点自动复活，**不需要 extract/add**，只要它们没被 DELETE。

## §E 回归压测

```bash
scripts/kiro-lane-regress.sh              # 10 轮，与基线比 in_tok；红则 exit 1
scripts/kiro-lane-regress.sh --rounds 1   # 快跑（加号后自查够用）
scripts/kiro-lane-regress.sh --only kiro-gpt5.6-sol --rounds 3   # 复打某条报红的道
scripts/kiro-lane-regress.sh --save-baseline   # 确认上游确实改了前导，才更新基线
```

🔴 **09-29 修掉「量了一个生产根本没挂的产物」**：脚本第 2 步原来从 `cm/litellm-config`
现取车道名，但 **litellm-proxy 挂的是派生的 `litellm-cwfb-stable-<hash>`**（`subPath`，不热更）。
09-29 实测两者**已漂 161 行**（09-25/09-26 那批 prometheus 指标配置只进了派生 CM），
车道名也差一条 —— 刚加完 opus-5-5，门禁却报「18 条 ≠ 预期 19」。
⇒ 现在先读 `deploy/litellm-proxy` 的 `volumes[0].configMap.name`，再从**那个** CM 取车道名。

⚠️ 这不只是 kiro 的坑：**任何"从 `cm/litellm-config` 读生产状态"的尺子都是错的**。
那份 CM 是下一次派生的输入，不是在跑的配置；两者漂了也不会报错。

🔴 **09-28 修掉两个让第 4 步静默不跑的缺陷**（改之前，一条道挂 = 降级那把尺子一次都没量）：

- **bench 有任何一条道失败就 `exit 1`**（`litellm-lane-bench.py:200`），而脚本是
  `set -e -o pipefail` ⇒ **整个脚本在第 3 步静默中止**，第 4 步 in_tok 比对压根不跑、
  `.tsv` 不生成，终端上只看到 bench 汇总表就没了 —— **看起来完全像"跑完了"**。
  09-28 sol 挂那轮就是这样，靠 `.tsv` 不存在才发现。现在退出码留到最后判。
- **「打挂的道」与「解析器坏了」原来混在一个计数里**：一条道真挂会被报成
  「只解析出 17 条 ⇒ 这把尺子自己坏了」，把人从"这条道为什么挂"带去查 awk。
  现在先摘出 `0/n` 的道单独报红并给出复打命令，再用剩下的条数校验解析器。
- 新增 `--only`：断言 18 条**通过之后**才收窄（不绕过空集假绿那道闸），
  且名字必须真在现取的 18 条里 —— 打错名会被 bench 当"不存在的模型"报 400，像道坏了。
  ⛔ `--only` 与 `--save-baseline` **互斥**：几条道的结果存成基线会把其余道抹掉，
  下次全量跑全变「🆕 不在基线里」⇒ 门禁静默失效。
  （⛔ 别用 `EXPECT_LANES=1 LANES=…` 那种写法覆盖 —— 车道名是现取的，覆盖不了，
  只会撞上条数断言。）

🔴 **`kiro-gpt5.6-sol` 上游 500 高发（09-28）**，判它的红要多轮：
第一轮三发全挂（180s 超时/`RemoteDisconnected`），第二轮同一条道 **150s 成功**、
`in_tok=430` 与 luna/terra 同族一致 ⇒ **不是静默降级，是上游侧 500**。
分层证据：直打 kiro.rs ClusterIP 时 terra 1.65s/200、sol 200s 超时（⇒ 不在 LiteLLM 层）；
kiro.rs 日志 `API 请求失败（上游瞬态错误，尝试 N/9）: 500 Internal Server Error`
`{"message":"Encountered an unexpected error…"}`，重试 9 次打满才吐 ERROR；
在役号 catalog 里 sol 4.4x **在**（⇒ 不是 entitlement）。
⚠️ 这条是 4.4x 全表最贵的道，重试 9 次穿过去的那一发也照样计费。

脚本把下面这些全固化了（09-24 前每次都要重新手搓一遍），**先看它再手搓**：

- 车道名从线上 CM 现取，**grep 不加 `^\s*- ` 前缀锚** —— CM 里的 YAML 已被 LiteLLM
  规范化（键排序、`model_name` 不再是列表首键），带锚抓到 **0 行**，bench 于是打印
  「全局 0/0 成功」：那是**空集不是全红**，极易读成"18 条道全挂了"。
  ⇒ 脚本**先断言车道数 == 18，不对就直接不跑**（`EXPECT_LANES=` 可覆盖）。
- key 环境变量名是 **`MK`**（bench 的 `--key-env` 默认值），写 `LITELLM_MASTER_KEY`
  会得到"环境变量 MK 没设"；来源是 **`secret/litellm-probe-key` 的 `PROBE_KEY`** ——
  ns 里**没有** `secret/litellm-secrets` 也没有 `LITELLM_MASTER_KEY` 这个键。
- bench 自带阴性对照（不存在的模型必须报红）、TTFT 自己掐表、轮转而非连发、
  成功判据是「文本非空」不是 200。

**🔴 验收判据是 `input_tokens` 分层，不是 200**（各家上游注入的系统前导大小不同）。
但老 SOP 里那两句判据 **09-24 实测都不成立，已改掉**：

| 作废的说法 | 实测 | 现在怎么判 |
|---|---|---|
| 「两条道 in_tok 相同 = 静默降级」 | 健康态天然就有同族相同：gpt 三道 429~430，`opus-4-5`/`sonnet-4-6`/`haiku-4-5` 都挤在 4110 附近 | 只当**参考信息**打印，不作红 |
| 「与上轮逐道**逐字**吻合」 | 同一 prompt 连跑两轮，同一条道自己就抖：`haiku-4-5` 4144→4110、`minimax-m2.1` 3742→3694（最大 **1.3%**） | 落基线 `scripts/kiro-lane-baseline.tsv`，按 **±5% 容差带**判 |

抖动成因**没查，不下归因**。容差带的两个对照都跑过：注入 1.3% 抖动不报红，
把 `opus-5` 6765 改成 4110（−39.2%，真降级的形状）必报红并 `exit 1`。

⚠️ **`kiro-auto` 不进基线比对** —— 它是**路由道**，上游自己挑落点，in_tok 天然漂
（09-24 三轮 5903 / 5878 / 5914），纳进去会让每轮都报差。只打印供参考。
⚠️ **已知盲区**：in_tok 抓得住"跨族塌陷"（6765→4110），**抓不了"同族串道"**
（相邻 Claude 变体本来就挤在 ±1.5% 内）。
⚠️ 单发长尾（`minimax-m2.1` 曾 14.27s）是抖动不是坏道，别拿 1 发的 max 定性。

2026-09-12 基线：**180/180 成功，TTFT p50 1.10s / p95 2.29s**。
最快 `kiro-qwen3-coder-next` p50 0.68s，最慢 `kiro-claude-sonnet​​-5` p50 1.65s。
报告在飞书 `Or6sdVU7GoppCnxiT2IciNf4ndh`。

## §F 回滚与备份台账

| 想回到 | 命令 / 位置 |
|---|---|
| 旧账号 + 不走代理 | `198:~cltx/kiro-deploy/backup/{credentials,config}.json.20260914-141221`（0600）⇒ `kubectl cp` 回 `/app/config/` + `rollout restart`。⚠️ 旧账号 `bradburybruns64` **仍在封停中**，回滚只用于救配置格式，救不回服务 |
| 上一版 kiro.rs | `set image deploy/kiro-rs kiro-rs=docker.io/library/kiro-rs:master-multimodel seed-config=同`（`master-5ca5703a`、v2026.3.1 镜像也都还在 242 本地） |
| PVC 冷拷贝 | 242 的 `/Data/kiro-rs-backup-{20260911-155124,20260911-184155,20260912-0030}` |
| LiteLLM 配置 | `198:~cltx/kiro-litellm-backup/litellm-{config,secrets}.20260912-021217.yaml` |
| 整个拆掉 | `kubectl delete ns kiro-rs` —— 全程只新建对象，零既有 workload 改动 |

## §G 坑清单

**🔴 模型名会静默降级，「200 且能回话」判不出模型对不对。**
v2026.2.7 的 `converter.rs:map_model` 只做子串匹配、没有 5 系分支，
`claude-opus-5` 含 "opus" 但不含 "4-5/4.6" ⇒ 落 else ⇒ 实际发 `claude-opus-4.6`。
而响应里的 `model` 字段是**原样回显请求名**，外观完全像成功 ——
两者同 prompt 返回**逐字节相同**的文本、input_tokens 都是 4171。
⇒ 判据必须是源码 map 表或跨模型 token/文本差异。
⚠️ **但"token 相同"这把尺子 09-24 收窄了**：健康态本来就有同族 in_tok 相同
（gpt 三道 429~430、`opus-4-5`/`sonnet-4-6`/`haiku-4-5` 都挤在 4110），
而且同一条道连跑两轮自己也抖 ±1.3%。⇒ 它抓得住**跨族塌陷**，抓不住**同族串道**。
判后者只能回到源码 map 表 / 文本差异。门禁形态见 §E。

### 🔴 落点尺子：唯一能证「发 X 真的落 X」的量具（09-29 定型）

`map_model` 是**子串匹配**，所以每加一个"更长的同族名"都有被短名劫走的风险，
而劫走之后 **HTTP 200 + 响应 `model` 原样回显请求名 + in_tok 同族只差几个**，
外观和成功**完全同形**。09-29 加 `claude-opus-5-5` 时逐个试过、全部不成立的伪尺子：

| 伪尺子 | 为什么不成立 |
|---|---|
| HTTP 200 + 回话正确 | 降级也是 200，响应 `model` 是回显不是落点 |
| `input_tokens` 差异 | opus-5.5 6752 vs opus-5 6748，同族只差 4 —— §G 盲区 |
| 输出上限 | 两个都 128K |
| `rateMultiplier`（2.0 vs 2.2） | 计量滞后几分钟 + 池子混合流量，算不出单发归属 |
| catalog 的 `thinking.type` schema 差异 | kiro.rs 把 thinking **注成正文文本**，不走上游结构化字段 ⇒ 不会 400 |
| 发一个**上游不存在**的长名当负对照 | `contains("opus-5-5")` 对 `claude-opus-5-5-nonexistent-xyz` 同样为真 ⇒ 它也落 5.5 分支，证不了任何事 |

✅ **真尺子 = 打开 kiro.rs 的 debug 日志读构造好的上游请求体。**
`handlers.rs:438` / `:953` 有现成的 `tracing::debug!("Kiro request body: {}")`，
打的是 **map 之后**的 body，里面就带 `conversationState.currentMessage.userInputMessage.modelId`。
`main.rs:29` 用 `EnvFilter::try_from_default_env()` ⇒ **改 env 就行，不用改代码不用重建镜像**：

```sh
# 1. 开 debug（⚠️ deploy 是 strategy: Recreate ⇒ 这一步会短暂断服）
sudo kubectl -n kiro-rs set env deploy/kiro-rs -c kiro-rs RUST_LOG=debug
sudo kubectl -n kiro-rs rollout status deploy/kiro-rs

# 2. 各发一发带**唯一 nonce** 的请求（被测名 + 老名做阳性对照）

# 3. 读日志，把 modelId 和 nonce 一起抽出来配对
sudo kubectl -n kiro-rs logs deploy/kiro-rs -c kiro-rs --since=10m \
  | grep "Kiro request body" \
  | python3 -c 'import re,sys
for ln in sys.stdin:
    print(sorted(set(re.findall(r"\"modelId\":\"([^\"]+)\"", ln))),
          sorted(set(re.findall(r"NONCE-[A-Z0-9-]+", ln))))'

# 4. 🔴 **立刻收回 info** —— debug 会把完整 system prompt 打进日志
sudo kubectl -n kiro-rs set env deploy/kiro-rs -c kiro-rs RUST_LOG=info
sudo kubectl -n kiro-rs rollout status deploy/kiro-rs
```

09-29 实测：发 `claude-opus-5-5` ⇒ 上游 `modelId: "claude-opus-5.5"` ✅；
阳性对照发 `claude-opus-5` ⇒ 仍 `"claude-opus-5"` ✅（新分支没劫走老名）。
⚠️ 抽出来的行里会混进**别人会话的 history**（一行同时出现两个 nonce 的就是），按 nonce 配对，别数行数。

### 🔴 `map_model` 是子串匹配 ⇒ 新分支必须排在短名之前

`contains("opus-5")` 对 `"claude-opus-5-5"` **同样为真**。所以 5.5 的分支放在 opus-5 之后
= 永远走不到，静默落 opus-5。09-29 加 5.5 时的正确形状（源码注释里也钉了同一句）：

```rust
} else if model_lower.contains("opus") {
    // ⚠️ 顺序敏感：`opus-5-5` / `opus-5.5` 必须排在 `opus-5` 之前
    if model_lower.contains("opus-5-5") || model_lower.contains("opus-5.5") {
        Some("claude-opus-5.5".to_string())
    } else if model_lower.contains("opus-5") {
        Some("claude-opus-5".to_string())
    } else if ...
```

⇒ 单测必须带**阳性对照**：既断言 5.5 的四种拼法都落 5.5，也断言 `claude-opus-5` /
`claude-opus-5-thinking` **仍**落 opus-5。只测新名的绿证明不了没劫走老名。

**🔴 4.6 那条道的 `input_tokens` 曾被系统性缩到 1/5**（v2026.2.7，**已修**，保留作判据参考）。
`handlers.rs:432` 写死 `CONTEXT_WINDOW_SIZE = 200_000`，而 `input_tokens` 不是真计数，
是用上游 `contextUsageEvent` 的百分比 × 这个常量算出来的；4.6 真实窗口是 1M ⇒ 除了 5。
**归因定性：这是 kiro.rs 的锅不是 Kiro 的锅** —— 同 payload 走 4 个模型，
各按真实窗口还原后全部收敛到 13.61 万 token（离散 0.04%），上游百分比是准的。

**非流式会把 `<thinking>…</thinking>` 当正文吐出来** —— 提取器只存在于 `stream.rs`。
流式正确（出 `thinking`+`text` 两个块）。

**`/v1/models` 里的 `max_tokens` 全是常数 32000，纯装饰。** kiro.rs 不设任何输出闸门。

**懒刷新：盘上快照不是存活量具。** `credentials.json` 的 `expiresAt` 可以显示早已过期
而服务完全正常；打一发真推理就会把它推后 ~1h 并回写 PVC。
`getUsageLimits` 报 403 时先打一发推理再重试，别据此判服务死了。

**无任何免认证端点**，探针只能 `tcpSocket`，**不构成在接客的证据**。

**`claude-sonnet​​-4` 上游有、kiro.rs 的 map 表没有** ⇒ 对外 18 条而不是 19 条。
要接得补一行映射再出一个镜像。

**`kubectl exec` 不带 `-i` 不转发 stdin**，heredoc 喂的 python 会被吞掉。
**litellm-proxy pod 里没有 psycopg2**，查 DB 要 exec 到 `litellm-db-0` 上跑 `psql`。

**🔴 argparse `parents=` 会把子 parser 的 default 写回 namespace，静默吃掉 `--dry-run`。**
09-17 实证：`kiro-pool.py --dry-run priority 3 9`（flag 在子命令**前**）**真把生产 prio 改成了 9**，
因为子 parser 也带这个 flag、它的 `default=False` 覆盖了主 parser 解析出的 `True`。
⇒ 共享 flag 必须 `default=argparse.SUPPRESS` + `getattr(a,"dry_run",False)`。
更一般的：**"我加了 dry-run" 不等于 dry-run 生效**，第一次用先拿一个无害目标验证它真没发写请求。

## 相关记忆

`project_198_kiro_rs_deployed_242_2026_09_11` ·
`feedback_kiro_credit_is_not_one_times_multiplier_and_metering_lags` ·
`feedback_litellm_openai_model_name_collision_hijacks_provider` ·
`feedback_allowlist_name_without_alias_is_400`
