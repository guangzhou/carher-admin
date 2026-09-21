---
name: kiro-rs-ops
description: >-
  kiro.rs（198 集群 242 节点，Anthropic API → AWS Kiro/CodeWhisperer 代理）的全套运维：
  判某模型这个账号有没有（`ListAvailableModels` 是唯一权威判据）、查还剩多少 credit、
  加/改模型（**必须自建镜像，升 tag 是死路**）、加号/摘号（`scripts/kiro-pool.py`，零重启）、
  逐号判死活（`scripts/kiro-probe.py`）、接进 198 LiteLLM 18 条 `kiro-*` 道
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
| 账号 | **8 个，都 KIRO PRO+ / 2000 credits / `us-east-1` / priority 0**：id 3 `rmiglio582`、id 4 `alhassanrashida749`、id 5 `kumistephen070`、id 6 `adafiasenyo444` —— 🔴 **这 4 个 09-19~09-21 已全部耗尽（`0.0/2000` live 复核）并 disabled**；id 7 `edemamoahalbert`（**在役 `currentId=7`**，09-21 17:05 live 剩 `1513.15`）；备用满额三个：id 8 `princessrit15`、id 9 `tetteyfrederick506`、id 10 `msajjadousavi`（都 09-21 加，`2000/2000`，**故意不切 currentId**）。下次重置 **2026-10-01 00:00 UTC**。台账见 §D4 |
| 出口 | **全部经 `10.68.13.243:8120`**（Astrill Los Angeles → `104.129.16.172`，09-17 19:36 从 8119 Seattle 改指），见 §D2。⚠️**全局唯一，无 per-account 路由**；⚠️ 8118/8119/8120 **三条都与 `chatgpt-acct-*` 共用**（分别 8/7/6 个对象），**没有独占 IP 可选** |
| 凭据 | PVC 内 `/app/config/credentials.json`（list = **真的多号池**，不是只用 `[0]`，见 §D3） |
| 对外 | 198 LiteLLM ns `litellm-product` 的 **18 条 `kiro-*` 道** |
| manifest | `198:~cltx/kiro-deploy/kiro-rs.yaml` |
| 登录 | `ssh cltx@10.68.13.198`，kubectl 要 `sudo` |

🔴 **Astrill 账号 2026-09-24 到期。** 到期后 `proxyUrl` 连不通 ⇒ **18 条道会再次全挂**，
且症状是**超时不是 403**（跟被封停长得不一样，别误判成又被封了）。
续费或把 `proxyUrl` 改指别的境外出口，二选一。

🔴 **比 Astrill 更近的一堵墙：credit —— 09-17 已经撞上一次并兑现。**
09-17 00:27 北京实测 `712.67 / 2000`（号 09-16 06:18Z 起服务 ⇒ ≈68 credits/h），
据此预测"约 09-17 19:30 北京打满" —— **实际 09-17 06:35 就打满了**（`successCount` 2895），
即真实速率比外推更快。⇒ **一个 PRO+ 2000 credits ≈ 撑 1 天多，撑不到月底是确定的**，
要么持续加号（§D3，零重启）、要么开 overage、要么把量大的道从高倍率往低倍率挪。
复核一行：`python3 scripts/kiro-probe.py quota`（**逐号打印，别只看池子总数**）。

⚠️ 上一个账号 `bradburybruns64@gmail.com` 2026-09-12 被 AWS 封停（403 suspended），
**触发原因至今未查清**。申诉入口 `https://app.kiro.dev/account/usage?support_form`。

🔴 **判一个号"还能不能用"必须打 `ListAvailableModels`，不能只看 credit 余额。**
09-17 逐号 live 复核 `bradburybruns64`：`getUsageLimits` 答得好好的、
**KIRO FREE `16.34/50` 还剩 33.66**，但同一凭据打 `ListAvailableModels` 直接 **403
`Your User ID (c488d458-…) temporarily is suspended`**。
⇒ **余额尺子会把封停号读成可用号**。清号/排障时先过 catalog 这道，再看余额。

⚠️ **镜像只在 242 本地**。Deployment 靠 nodeSelector 钉在 242 才成立，
换节点/重装节点必须重新 import，否则起不来。构建产物留在 `188:/Data/kiro-build/`。

## §0.1 两个脚本：读用 `kiro-probe.py`，写用 `kiro-pool.py`

本仓 `scripts/` 下，都在**本机**跑（内部自己 ssh 198），不需要先登录：

| 脚本 | 性质 | 干什么 |
|---|---|---|
| `kiro-probe.py` | **纯只读** | `catalog`（`ListAvailableModels` 逐号）/ `quota`（credit 余额）/ `both` / `has <模型…>`（退出码即判据）。凭据与出口都从 live pod 现取，401/403 自己走 OIDC refresh |
| `kiro-pool.py` | **写**（admin API，零重启） | `show` / `add <导出json>` / `remove <id>` / `priority <id> <n>` / `extract <备份> <who>` |

`kiro-pool.py` 把下面这些反复踩的坑固化进去了，**手搓 curl 之前先看它能不能用**：
adminApiKey 走 `curl -K` 不进 argv；body 走临时文件不做 shell 转义；
写前自动备份到 `198:~/kiro-deploy/backup/`（0600 并打印回滚提示）；
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
python3 scripts/kiro-probe.py quota
```

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
`5ca5703a`（2026-07-27），**没出过任何镜像**。
（早前"仓库无 v2026.3.x tag"的说法是错的 —— `tags/list` 不带 `?n=` 只返回首页。）

### 源码在哪、改什么

源码 `188:/Data/kiro-build/kiro.rs/`。已经打的补丁：

| 文件 | 改了什么 |
|---|---|
| `src/anthropic/converter.rs` | `PASSTHROUGH_MODELS` 表：9 个非 Claude id → 真实上下文窗口，命中即原样透传 |
| 同上 | `strip_kiro_alias_prefix()`：剥可选的 `kiro-` / `kiro/` 前缀再查表 —— **绕 LiteLLM 重名劫持的逃生舱**，见 §D |
| `src/anthropic/handlers.rs` | 9 个透传条目前置进 `/v1/models` ⇒ 现列 27 个 |

`-thinking` 后缀在 master 里**只是名字别名**（`converter.rs:100` 直接 strip），
真 thinking 由请求体的 `thinking` 字段（`enabled`/`adaptive`）驱动。

### 构建 → 落地流水线（每一步都有坑）

```bash
# 1. 188 上构建（188 有 docker 28.2.2，/Data 余量够；Dockerfile 三段式，约 4 分钟，产物 17.3MB）
ssh <188> 'cd /Data/kiro-build/kiro.rs && docker build -t kiro-rs:master-<tag> .'

# 2. 回归测试（188 无 rust 工具链，跑在容器里挂源码 + cargo registry 缓存）
#    判据不是"全绿"，是"失败集合与干净 master 基线逐条相同"
#    基线：干净 master 200 passed / 8 failed；打完补丁 206 passed / 同样 8 failed

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
| 3 | 真实推理 | 18 条道**文本非空**（不是 200），且各道 `prompt_tokens` 互不相同 |

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

⚠️ 批量压这 18 条时 `curl -m 120` 会偶发切断慢道（opus-4-6 中过一次，
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
python3 scripts/kiro-pool.py add /tmp/newcred.json             # 3. 真加（自动备份+前后复核）
python3 scripts/kiro-pool.py priority <老号id> 9               # 4. 逼 currentId 切过来
```

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
     **比 sha256，别比长度**。09-17 就是靠比线上 id3/id4 的 `clientSecret`/`refreshToken`/
     `clientId` 三者哈希，才确认新号不是抄串。
     ⛔ 拿"历史消息里另一个号的 secret"做对照时先确认**集合非空** ——
     第一次比出 `相同: False (比对了 0 个)`，那是**空集假绿**不是通过。
1. **只读验号**：`kiro-probe.py both --creds <拍平后的文件>`。探针遇到 401/403 自己走
   OIDC refresh，**用户给的 `accessToken` 过期不影响**。
   绿的样子：`AI_EDITOR` **19 个模型** + 档位/额度读出来（这一步同时证明**不是封停号**）。
   ⚠️ 导出 json 里的 `expiresAt` 是**毫秒 epoch**，线上格式是 **ISO** ——
   `kiro-pool.py add` 索性**不传这个字段**，让服务端自己 OIDC 换，别手动转。
   ⚠️ `clientSecret` 是 ~4.8KB 的三段 JWT，**用 heredoc 单独写文件再注入**，
   跟别的字段挤在一个 heredoc 里容易被截断（09-17 就丢过一次，脚本当场拦下了）。
2. **备份**由 `add` 自动做（`198:~/kiro-deploy/backup/credentials.json.$TS`，0600，打印回滚提示）。
3. **POST /api/admin/credentials**（camelCase 字段：`refreshToken` `authMethod:"idc"`
   `clientId` `clientSecret` `priority` `region` `authRegion` `apiRegion` `machineId` `email`；
   缺 `refreshToken`/`clientId`/`clientSecret`/`machineId` 任一，脚本当场拒绝而不是发出去）。
   服务端自己走 OIDC 换 token、按 `max(id)+1` 分配 id、**并 `persist_credentials()` 落 PVC**
   （`token_manager.rs:1654`）⇒ **加完就能扛重启，不需要再 `kubectl cp`**。
   ⛔ 手搓时 body 一律走文件（`curl --data @file` / `wget --post-file`），
   **绝不嵌套 ssh + `\x27` 转义**（会被 shell 搞坏回 400）。
4. 🔴 **`currentId` 不会自己切到新号 —— 但"切不切"要看老号还剩多少，别条件反射地切。**
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

## §D4 历史账号台账（09-17 19:52 逐号 live 判定后清空）

| 账号 | 档位 / live credit | 判定 | 处置 |
|---|---|---|---|
| `bradburybruns64@gmail.com` | KIRO **FREE** `16.34/50`（还剩 33.66） | ❌ **AWS 封停**：catalog 403 `User ID c488d458-… temporarily is suspended`（09-12 起） | 09-14 换号时移出；余额还在但打不动，只能走 support_form 申诉 |
| `beltmike66@gmail.com` | KIRO PRO **`1000.0/1000.0`** | ✅ 真打满 | 09-16 摘除，备份 `backup/credentials.json.20260916-141659` |
| `barisibetter9@gmail.com` (id2) | KIRO PRO+ **`2000.0/2000.0`** | ✅ 真打满（09-17 06:35，`successCount` 2895） | **09-17 19:52 `DELETE /credentials/2`**，备份 `backup/credentials.json.pre-del-id2.20260917-195234`（0600，含完整凭据） |
| `rmiglio582@gmail.com` (id3) | KIRO PRO+ **`2000/2000`** | ❌ 已打满（succ 3091，`reason=QuotaExceeded` 自动 disabled） | 留在池里等 10-01 重置 |
| `alhassanrashida749@gmail.com` (id4) | KIRO PRO+ **`2000/2000`**（09-21 live 复核） | ❌ 已打满（succ 2648） | 09-17 21:19 加，priority 0，**故意不切 currentId**（id3 还剩 1902，见 §D3 第 4 步）。备份 `backup/credentials.json.20260917-211915` |
| `kumistephen070@gmail.com` (id5) | KIRO PRO+ **`2000/2000`** | ❌ 已打满（succ 2693） | 09-17 21:35 加，priority 0，同样不切 currentId。导出是**扁平无 `machineId`** 那种形状（`provider: BuilderId`），`machineId` 按 `sha256("KotlinNativeAPI/<rt>")` 固化。备份 `backup/credentials.json.20260917-213531` |
| `adafiasenyo444@gmail.com` (id6) | KIRO PRO+ **`2000/2000`** | ❌ 已打满（succ 2839） | 09-17 22:10 加，与 id5 同一形状同一套路（扁平·`BuilderId`·补 `machineId`），**全程照 §D3 第 0 步走无新坑**。备份 `backup/credentials.json.20260917-221052` |
| `edemamoahalbert@gmail.com` (id7) | KIRO PRO+ `108.03/2000`（09-21 13:08 live） | 🟢 **在服役 `currentId=7`** | 09-21 10:30 加，priority 0。**其余 4 个全 disabled ⇒ 服务端自己选中它，没执行 `priority` 命令**。扁平·`BuilderId` 形状，`machineId` 按 `sha256("KotlinNativeAPI/<rt>")` 固化。备份 `backup/credentials.json.20260921-103044`。加完 18/18 回归全绿（TTFT p50 1.17 / p95 1.64，`input_tokens` 分层正常） |
| `princessrit15@gmail.com` (id8) | KIRO PRO+ `0.0/2000`（09-21 17:05 live 仍满额） | 🟡 **备用，`disabled:false` 但非 current** | 09-21 13:06 加，priority 0。扁平·`BuilderId`·无 `machineId` 那种形状，照 §D3 第 0 步走**无新坑**。🔴 **故意不切 currentId** —— id7 当时还剩 1891.97，切过去等于把两个号摊薄成半个；等 id7 打满后 kiro.rs 自己 `QuotaExceeded` + `min_by_key` 选中它。备份 `backup/credentials.json.20260921-130626`。加完 18/18 回归全绿（TTFT p50 1.25 / p95 1.96，`input_tokens` 分层正常：opus-5 6765 / sonnet-5 6482 / opus-4-6 4171 / gpt 道 429-430，与上轮逐道吻合 ⇒ 无降级），pod `restarts=0` `startTime` 未变 ⇒ 零重启 |
| `tetteyfrederick506@gmail.com` (id9) | KIRO PRO+ `0.0/2000`（09-21 17:03 live 验号：AI_EDITOR 19 个模型 + 满额） | 🟡 **备用，非 current** | 09-21 17:03 加，priority 0。同一形状同一套路，无新坑。备份 `backup/credentials.json.20260921-170332` |
| `msajjadousavi@gmail.com` (id10) | KIRO PRO+ `0.0/2000`（09-21 17:03 live 验号：AI_EDITOR 19 个模型 + 满额） | 🟡 **备用，非 current** | 09-21 17:03 加，priority 0。备份 `backup/credentials.json.20260921-170350`。id9+id10 加完合并跑一次 18/18 回归全绿（TTFT p50 1.50 / p95 3.51；`minimax-m2.1` 单发 14.27s 是长尾抖动不是坏道，§D3「别拿 1 发的 max 定性」）；`input_tokens` 逐道与上轮吻合 ⇒ 无降级。池 `total=8 available=4`，盘上 8 条一致，pod `restarts=0` |

🔴 **`disabledReason` 会变，但它不是额度尺子。** 09-21 13:06 读到 id3/id6 是
`QuotaExceeded fail=3`，同日 17:03 再读变成 **`Manual fail=0`**（id4/id5 仍是 `QuotaExceeded`）。
**我没动过这两个号**，中间发生了什么没有数据，不下归因。
判额度只认 `kiro-probe.py quota` 的 live 读数 —— 这四个号当时都是 `0.0/2000`，
`reason` 字段变了不改变"它们确实空了"这个事实。

🔴 **account-manager 导出里的 `usage` / `subscription` 是导出那一刻的快照，不是当前值。**
09-21 用户发来 `alhassanrashida749` 的导出，里面写着 `usage.current: 0 / limit: 2000`，
看上去像个满额新号 —— 但 `exportedAt`/`lastUpdated` 解出来是 **09-17 08:13 UTC**，
而该凭据（`refreshToken`/`clientId`/`clientSecret` 三个 sha256 与线上 id4 **逐字节相同**）
live 读出来是 **`2000/2000` 已耗尽**。
⇒ **判额度只认 `kiro-probe.py quota` 的实时读，导出里的数字一律当作历史快照。**
⇒ **判「是不是重复号」只认三个字段的 sha256，不看邮箱也不看导出里的 `id`/`userId`。**

**这些号的 `nextDateReset` 全是 `2026-10-01 00:00 UTC`。** ⇒ 到点后 4 个打满的号（id3~id6）都能复活，
两条命令（`extract` 会打印下一步的探针命令）：

```sh
python3 scripts/kiro-pool.py extract \
  ~/kiro-deploy/backup/credentials.json.pre-del-id2.20260917-195234 barisibetter9 --out /tmp/c.json
python3 scripts/kiro-probe.py both --creds /tmp/c.json   # 判活（走生产同一条出口）
python3 scripts/kiro-pool.py add /tmp/c.json && rm -f /tmp/c.json
```

`bradburybruns64` 例外 —— 重置只补 credit，**补不了封停**。

⚠️ `/tmp/c.json` 里是 live refreshToken，**探完立刻删**。
`disabled:true` 的条目**照样会被探**（探针只在标题打 `[disabled]`，不跳过），不用改副本。

## §E 回归压测

```bash
scp scripts/litellm-lane-bench.py cltx@10.68.13.198:/tmp/
ssh cltx@10.68.13.198 'set -e
# 18 条道的名字从线上 CM 现取，别抄死名单（加道/改名都会漂）
MODELS=$(sudo kubectl -n litellm-product get cm litellm-config -o jsonpath="{.data.config\.yaml}" \
  | grep -oE "model_name: kiro-[a-zA-Z0-9._-]+" | sed "s/model_name: //" | sort | paste -sd,)
MK=$(sudo kubectl -n litellm-product get secret litellm-probe-key \
    -o jsonpath="{.data.PROBE_KEY}" | base64 -d) \
  python3 /tmp/litellm-lane-bench.py --models "$MODELS" --rounds 10 --out /tmp/raw.jsonl'
```

⛔ **grep 别加 `^\s*- ` 前缀锚**：CM 里的 YAML 已被 LiteLLM 规范化（键排序、`model_name`
不再是列表首键），`grep -oE '^\s+- model_name: kiro-…'` 抓到 **0 行**然后 bench 打印
「全局 0/0 成功」—— 那是**空集不是全红**，很容易读成"18 条道全挂了"。
上面那条不带锚的写法 09-17 21:2x 实测 18 行。**车道数先打出来再跑**。

⚠️ 两个已踩过的名字坑：key 的环境变量名是 **`MK`**（脚本 `--key-env` 默认值，
写成 `LITELLM_MASTER_KEY` 会得到"环境变量 MK 没设"）；
key 的来源是 **`secret/litellm-probe-key` 的 `PROBE_KEY`** ——
ns 里**没有** `secret/litellm-secrets` 也没有 `LITELLM_MASTER_KEY` 这个键。

脚本自带阴性对照（不存在的模型必须报红）、TTFT 自己掐表、轮转而非连发、
成功判据是「文本非空」不是 200，有失败就非零退出。判据纪律写在脚本 docstring 里。

**🔴 验收判据是 `input_tokens` 差异，不是 200。** 各家上游注入的系统前导大小不同，
同一句 `hi`：GPT 道 ≈1,580 / qwen·glm·minimax ≈3,700 / Claude 4.5 代 ≈4,1xx /
opus-4-7 ≈6,0xx / sonnet-5·opus-4-8 ≈6,5xx / opus-5 ≈6,8xx。
**两条道 token 逐字节相同 = 静默降级**（见 §G）。

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
