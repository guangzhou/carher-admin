---
name: kiro-rs-ops
description: >-
  kiro.rs（198 集群 242 节点，Anthropic API → AWS Kiro/CodeWhisperer 代理）的全套运维：
  判某模型这个账号有没有（`ListAvailableModels` 是唯一权威判据）、查还剩多少 credit、
  加/改模型（**必须自建镜像，升 tag 是死路**）、加号/摘号（`scripts/kiro-pool.py`，零重启）、
  逐号判死活（`scripts/kiro-probe.py`）、接进 198 LiteLLM 21 条 `kiro-*` 道
  （含绕 LiteLLM 重名劫持的别名前缀）、回归压测、回滚。
  改镜像/升上游走三个脚本 `kiro-rs-build.sh`（188 打本仓补丁+测试对照）→ `kiro-rs-ship.sh`（送 242，`--switch` 切生产）
  → `kiro-rs-e2e.sh`（隔离容器真上游 13 例），补丁真源 `k8s/kiro-rs/patches/`。
  Use when 用户说 "kiro 还剩多少额度/credit"、"kiro 支持 xx 模型吗"、"kiro 打不通"、
  "给 kiro 加个模型 / 把 kiro 上的新模型开给大家"、"给 kiro 加个账号 / 把某个号摘掉"、
  "admin 上看不了余额 / 权限不足 403"、"kiro-* 那些道怎么样了"、
  "升级 kiro.rs / 官方有新版吗 / 新版有什么提升"、"30890 那个 kiro admin 界面 / 改界面 / 排序"、
  或者要给生产 key 授权 `kiro-*` 名字。
---

# kiro.rs 运维

## §0 拓扑速查

| 项 | 值 |
|---|---|
| ns / Service | `kiro-rs`，ClusterIP **`10.43.109.5:8990`**（仅集群内，无 NodePort/ingress） |
| 节点 | **`aiyjy-litellm-242`**，nodeSelector 硬钉 + toleration `dedicated=new-node:NoSchedule` |
| 镜像 | **`kiro-rs:v2026.10.1-carher-sort2`**（10-16 起 = 上游 v2026.10.1 + 本仓补丁 0001~0005，见 §C）—— 自建，**只在 242 本地 containerd** |
| 账号 | 🔴 **一律现读，别信写死的数** —— 一条命令出全表（id / 邮箱 / `authMethod` / 档位 / live 余额 / 在役哪个 / 盘上一致否 / 零重启否）：<br>`python3 scripts/kiro-pool.py show --with-quota`<br>这张表 09-17~09-24 因为手维护漂过两次，**已不再在这里复述池子状态**。不可推导的部分（每个号的加入经过、备份路径、踩过的坑）在 §D4。<br>只留两条常驻事实：**档位已见六种**（FREE **50** / PRO 1000 / **STUDENT 1000** / PRO+ 2000 / PRO MAX 5000 / POWER 10000，跨度 **200 倍** ⇒ **算容量前必读 live cap**，别按某个数硬算；每新加一个号都可能再冒一种）；**`authMethod` 混编且无规律**（idc / social 在 id 轴上交替出现，10-08 加的 21 个里 16 idc + 5 social；**别按 id 区间推 authMethod**，现读 `show` 那一列，两条刷新路行为相反见 §D3 第 0 步）。**盘上 `email` 可能是 null**（social 那种导出形状，id46~52 就是）⇒ 任何按 email 关联账号的脚本都要有第二把判据。重置为**每月 1 日 00:00 UTC** |
| 档位↔模型 | 🔴 **「升档只买额度不买模型」已被推翻（10-01）** —— 那句话在 PRO+/MAX/POWER/STUDENT 四档之间成立（条数一致），但 **FREE 档只有 9 个模型**，且是付费档的**真子集**。<br>⚠️ **付费档的条数本身会变**：10-01 是 20，10-02 上游新出 `claude-sonnet-5.5`（1.3x，1M/128K）后变 **21** —— **别把条数写成判据，条数不对先查上游是不是出新模型**（见 [[project_kiro_sonnet55_appeared_upstream_no_lane_2026_10_02]]，198 侧至今 0 条道）。<br>FREE 能用：`auto` `claude-haiku-4.5` `claude-sonnet-4` `claude-sonnet-4.5` `deepseek-3.2` `glm-5` `minimax-m2.1` `minimax-m2.5` `qwen3-coder-next`<br>**掉档即失去的 11 个**：`claude-opus-4.5/4.6/4.7/4.8/5/5.5`、`claude-sonnet-4.6`、`claude-sonnet-5`、`gpt-5.6-sol/terra/luna` —— **全部 opus、sonnet-5/4.6 和三条 gpt 都在里面**，即 20 条道里**最贵也最常用的那批**。<br>⇒ 判「某个号能不能跑 opus」**必须看它当下的档位**，不能靠「它以前能跑」。订阅过期/掉 FREE 与「模型没了」是同一件事的两面。 |
| 出口 | **全部经 `10.68.13.243:8120`**（Astrill Los Angeles → `104.129.16.172`，09-17 19:36 从 8119 Seattle 改指），见 §D2。⚠️**全局唯一，无 per-account 路由**；⚠️ 8118/8119/8120 **三条都与 `chatgpt-acct-*` 共用**（分别 8/7/6 个对象），**没有独占 IP 可选** |
| 凭据 | PVC 内 `/app/config/credentials.json`（list = **真的多号池**，不是只用 `[0]`，见 §D3） |
| 对外 | 198 LiteLLM ns `litellm-product` 的 **21 条 `kiro-*` 道**（09-29 加 opus-5-5，10-04 加 sonnet-5-5，10-16 加 kimi-k3）|
| manifest | `198:~cltx/kiro-deploy/kiro-rs.yaml` |
| Admin UI | **`http://10.68.13.198:30890/admin`**（Service `kiro-rs-admin` NodePort 30890 → 8990，前端嵌在二进制里，改界面 = 改 `admin-ui/` + 重建镜像）|
| 登录 | `ssh cltx@10.68.13.198`，kubectl 要 `sudo` |

⚠️ **「Astrill 账号 2026-09-24 到期」这句已过期未更新** —— 10-01 实测（当时 19 条）
19/19 走这条出口成功 ⇒ **它还活着**（续过费、或当时那个日期本身就记错了，没有数据，不下归因）。
下面这段讲的是**到期后的症状形状**，仍然有用；但别再把它当"马上要断"的待办。
到期后 `proxyUrl` 连不通 ⇒ **21 条道会再次全挂**，
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

### 🔴 §0.2 不在公司内网时：两条路都要搭，一条是 ssh 一条是出口（10-08 实测）

这些脚本有**两条独立的内网依赖**，只搭一条会卡在第二条上，而且第二条的报错
（`ConnectionRefusedError` 深在 urllib 栈里）长得完全不像"网络不通"：

| 依赖 | 谁用 | 不通时的样子 |
|---|---|---|
| `cltx@10.68.13.198:22` | 全部脚本（内部自己 ssh） | `ssh: connect to host … port 22: Connection refused` |
| 出口代理 `10.68.13.243:8120` | **只有 `kiro-probe.py`**（它直打 AWS，且恒从线上 `config.json` 读出口） | 一长串 urllib traceback 末尾 `URLError: [Errno 61] Connection refused` |

```sh
export KIRO_SSH_JUMP=pubSrv                       # ① ssh 走 ProxyJump（三个 py + regress.sh 都认）
ssh -f -N -o ExitOnForwardFailure=yes -L 18120:10.68.13.243:8120 pubSrv
export KIRO_PROXY_HOSTPORT=127.0.0.1:18120        # ② 只给 kiro-probe.py 改出口**接入点**
# 用完：pkill -f "18120:10.68.13.243:8120"
```

- 两个变量**不设时逐字节保持原来的直连行为**，内网里不用管。
- `KIRO_PROXY_HOSTPORT` **只换接入点，不换代理**：口令仍从线上 `config.json` 现读、
  落地 IP 仍是 Astrill LA ⇒ 量的还是线上那条路。改写时脚本会打一行 ⚠️ 告诉你换了。
  ⛔ **别改成 `--no-proxy`** —— 那是直连 AWS，与线上**不同路**，拿它的读数判号等于量了另一条路。
- 🔴 **判「本机到底通不通 198」别用 ping**：10-08 实测 ICMP 到 198/188 **通**
  （延迟 130ms，还会经 `192.168.3.x` 这种明显不是 IDC 的跳），TCP 22 却一律 refused，
  而堡垒机 `10.68.13.189` 连 ICMP 都不通。⇒ **ping 通不代表在内网**，判据只认 `nc -z <host> 22`。
- 先找一台能通的跳板：`ssh pubSrv 'nc -z -w4 10.68.13.198 22'`（10-08 `pubSrv` 通、
  `priSrv` 不通）。`scripts/jms` 那条堡垒机路要求能到 `10.68.13.189:2222`，不在内网时同样不通。

## §0.1 十三个脚本：读用 `kiro-probe.py`，写用 `kiro-pool.py`，回归用 `kiro-lane-regress.sh`，改镜像用 `kiro-rs-build/ship/e2e`，改 UI 先 `kiro-admin-ui/verify.sh`

本仓 `scripts/` 下，都在**本机**跑（内部自己 ssh 198），不需要先登录：

| 脚本 | 性质 | 干什么 |
|---|---|---|
| `kiro-pool.py` | **写**（admin API，零重启） | `show [--with-quota] [--tier]` / `add <导出json>` / **`add-batch <多个json>`**（10-08 加，批量加号走它，别手搓 for）/ `remove <id>` / `priority <id> <n>` / **`priority-batch --ids 1,2,3 <n>`** 或 **`priority-batch --by-tier STUDENT <n>`**（10-17 加，批量改优先级一次备份一次复核）/ `extract <备份> <who>`。`--tier` 只查 enabled 号的档位（比 `--with-quota` 快） |
| `kiro-probe.py` | **纯只读**（直打 AWS） | `catalog`（`ListAvailableModels` 逐号）/ `quota` / `both` / `has <模型…>`（退出码即判据）。凭据与出口都从 live pod 现取，401/403 自己走 refresh（**按 `authMethod` 分流 idc / social**）。social 号必带 `--out-rotated FILE`，见 §D3 第 0 步。**批量验号用 `--summary-json`**，见下条红线 |
| `kiro-batch-prepare.py` | 只读线上 + 写本地文件 | **批量加号的筛子**（10-02 入仓）：拿整库导出跟线上逐号比三把判据，只把**新增**的号拍平成 per-account 文件（0600）并过 6 道门。`--dry-run` 只比对不写。门禁 `backend/tests/test_kiro_batch_prepare.py` 30 条。见 §D3「一次加多个号」/ §D4.1 |
| `kiro-lane-regress.sh` | 只读压测 | 21 条道（10-16 现读）回归一条龙：车道名现取 + **条数断言** + bench + in_tok 容差带比对。见 §E |
| `kiro-lane-add.py` | 生成 CM（**不写集群**） | 加一条 `kiro-*` 道：`<src> <dst> --name … --template <照抄哪条道> --litellm-model … --upstream-id … --rate-multiplier …`。字段级替换 ⇒ 缓存价/窗口/`mode` 从模板**继承**而不是重打（漏 `mode` 会让 `/测模型` 报「⚠️ 未测」，与模型真坏同形）。自己拒的四种情形见 §D1 |
| `litellm-198-key-behavior-regress.py` | 只读 + 一把短命 key | 改完 key 白名单/alias 的**行为层**回归：克隆真 key 形状起临时 key → 打新名/老名/**白名单外的名** → 删。见 §D1 |
| `kiro-lane-limits-price-fix.py` | 生成 CM（**不写集群**） | 批量修 19 条道的 `max_input/output_tokens` 与 opus-5.5 价。窗口从 `--catalog` 读（**不硬编**），三道门：旧值断言 / 结构门（除目标字段外逐字段相等）/ 只增不删行数门。见 §D0.1 |
| `kiro-lane-limits-verify.py` | 纯只读 | 逐**在役**副本验窗口/上限/价（按当前 RS 的 `pod-template-hash` 过滤，排掉 Terminating）。判据是 `/v1/model/info` 逐行，**不是** `/model_group/info`（那个 cap 是组内最大值不是闸门）|
| `kiro-rs-build.sh` | 188 上构建（**不碰生产**） | 干净上游 → 打本仓 `k8s/kiro-rs/patches/` → cargo test 上游/补丁对照（失败集合相同 + 通过数更多 + 无漏 `#[test]`）→ 链式复现 → 镜像 + tarball sha。`--skip-build` 只验补丁。见 §C |
| `kiro-rs-ship.sh` | 送镜像；**`--switch` 才写生产** | 188→本机→198（2MB 分块）→242 containerd，四处对 sha。`--switch` 备份 deploy + set image 两容器 + 打印回滚 + 比切前切后池子。见 §C |
| `kiro-rs-e2e.sh` | 隔离容器（不碰生产） | 198 docker 起新镜像，单号 + 假 refreshToken，打真上游 11 例（含 sonnet-5 对照、tool_result 夹图片、web_search）。用例在 `kiro_rs_e2e_cases.py`。跑旧 tag 即阴性对照。见 §C |
| `kiro-admin-ui/verify.sh` | 只读线上（一次 GET /credentials）+ 本机 | **改 Admin UI 先跑它**（10-18 加）：干净上游 admin-ui + 全部 UI 补丁 → tsc/vite → mock 回放 live 池子（写请求一律 403 并计数）→ Playwright 逐条件对条数。见 §C「改 Admin UI」 |
| `kiro-enable-overage.py` | **写**（Kiro Management API） | 为 social 号批量开启 overage（超额计费）。`<id…>` 按 id 指定 / `all-new` 选未消耗的 social 号 / `--check` 只读验证不写。从 PVC 读凭据 → refresh → `setUserPreference` → `getUsageLimits` 验证。开完自动轮询到 ENABLED（`--wait N`，默认 45s，10-15 实测 ~30s 才翻）；退出码全 ENABLED=0。cap/rate 读 `usageBreakdownList[0]`（10-15 修：之前读错位置恒 None）|

🔴 **`kiro-lane-add.py` 与 `litellm-198-key-behavior-regress.py` 是 09-29 从 198 盘上
两个一次性脚本（`~cltx/add-kiro-opus55-lane.py`、`~cltx/o55-keytest.py`）泛化进仓的。**
在那之前这两步在 SOP 里只有散落的命令片段，脚本本体**只在 198 的磁盘上**
—— 换台机器、或那台机器重装，这轮的手艺就没了（`feedback_sop_referenced_scripts_were_never_in_git`）。
泛化的验收方式：拿生产上**改前那份 CM 当夹具重放**，产物与生产改后那份**逐字节相同**
（sha `6c0c13749315`，正好等于派生 CM 名字的后缀 —— 派生 CM 名 = `sha256(config.yaml)[:12]`）。

**日常这一条就够**：`python3 scripts/kiro-pool.py show --with-quota`
—— 池状态 + 盘上一致性 + pod 零重启 + **每个号的 live 档位/余额**，一屏出全。

🔴 **`--json` 不能用来批量解析，`--summary-json` 才行（10-02 加的）。**
`both --json` 吐的是 catalog 与 quota **两个原始响应拼在一起**，前面还有一行
`===== id=… =====` 人类表头 ⇒ **整个 stdout 不是合法 JSON**（实测 `json.loads` 直接抛）。
10-02 验 20 个号时我因此退回去 grep 人类表格，三条 grep 全写错格式
（表格行**没有前导空白**、档位在 `订阅` 那一行），**20/20 全打出
`tier=[] model_lines=0 err=0`，看着像整批凭据都废了** —— 实际 20 个全是好的。
`--summary-json` 吐**一个合法 JSON 数组**，每号带
`tier/modelCount/remaining/exhausted/rotated` + **`ok`/`error`**：
一个号抛异常**不中断整批**，但失败**留在数据里**（`ok:false` + 原因），
不会变成"安静地少一行" —— 那正是「全 0 却零报错」这种假象的来源。
⇒ 批量时别在 shell 里 grep 人类输出。见 [[feedback_all_metrics_zero_at_once_is_the_ruler_not_the_fleet]]。

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

🔴 **`add` 10-08 起自己判落地并在失败时非零退出**（`assert_landed()`）——
在那之前**服务端拒了它照样 exit 0**，因为 admin API 拒绝回的是 200 + `{"error":…}`
而 `curl` 对此 rc=0。⇒ 现在才可以用退出码判成败；但判**池子最终状态**仍只认 `show`。

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
⇒ **档位闸彻底死了，别再提"升档买新模型"。**（09 月记的「`kimi` / `k3` 同样不在目录里」已过期 ⇒）
🔴 **10-16 更正：`kimi-k3` 已出现**（付费档 21→22）：10-16 启用的 6 个号（id142~147，5 social + 1 idc，全 PRO+）逐号 `AI_EDITOR` catalog **6/6 都有**；禁用的 133 个没查。
10-16 当天已上道 `kiro-kimi-k3`（补丁 0005，见 §D-2）。
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

### 现役镜像与补丁（10-16 起：上游 v2026.10.1 + 本仓 5 个补丁）

| 项 | 值 |
|---|---|
| 现役镜像 | **`kiro-rs:v2026.10.1-carher-filter`**（10-18 上线，0006 Admin 查询栏；回滚 tag `v2026.10.1-carher-sort2`，备份 `198:~/kiro-rs-deploy.pre-v2026.10.1-carher-filter.json`）|
| 上游基线 | `hank9999/kiro.rs` master `22f36e69`（v2026.10.1，10-08）|
| 补丁真源 | **本仓 `k8s/kiro-rs/patches/000N-*.patch`**（按文件名序打；`*.5ca5703.patch` 是旧基线存档，不打）。每个补丁做什么、验证记录 ⇒ 同目录 `README.md` |
| 构建树（188）| `/Data/kiro-build/kiro-up/`（= 上游 + 补丁，**脚本每次从干净上游重建**）· 干净上游 `kiro-up-pristine/` · 旧基线 `kiro.rs/` `kiro-ws/` `kiro-ui/` 只读留档 |
| 回滚 | `v2026.10.1-carher-sort2`，备份 `198:~/kiro-rs-deploy.pre-v2026.10.1-carher-filter.json`；完整链见 §F 和 `k8s/kiro-rs/patches/README.md` |

| 补丁 | 一句话 |
|---|---|
| 0001 | 9 个非 Claude 模型透传 + `kiro-` 别名前缀 + sonnet-5.5 映射（opus-5.5 上游已自带，10-15 重做时去掉）|
| 0002 | 服务端 `web_search` 搜索回合，见下节 |
| 0003 | 402 `OVERAGE_REQUEST_LIMIT_EXCEEDED` 也禁号换号 |
| 0004 | Admin UI（`:30890/admin`）列表：启用（在用置顶）→ 禁用，组内按 `lastUsedAt` 倒序（只改前端，调度仍按 priority）|
| 0005 | `kimi-k3` 透传（10-16，见 §D-2）|
| 0006 | Admin UI 查询栏（关键字/状态+禁用原因/登录方式/优先级/域名/只看失败）+ 卡片标题栏防溢出；只改前端，过滤在排序后分页前 |

上游 v2026.10.1 自带、对我们有用的只有一条：**`tool_result` 里的图片不再被丢**
（10-15 同号同请求 A/B：旧版两次都答「只收到文字 screenshot captured」，新版答对绿/蓝）。
fable-5.1 也加了，但**不是每个号都有**（#138 PRO+ 目录里没有 ⇒ 400 Invalid model），198 侧也还没有道。

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

`-thinking` 后缀在 master 里**只是名字别名**（`converter.rs:100` 直接 strip），
真 thinking 由请求体的 `thinking` 字段（`enabled`/`adaptive`）驱动。

### 服务端 web_search 搜索回合（补丁 0002）

**客户端线型、各后端谁真搜、回归命令统一放在 [[litellm-198-web-search]]**，这里只记 kiro 侧：
日志关键字 `进入搜索回合`（新路径）/ `路由到 WebSearch 处理`（Claude Code 子请求老路径）/ `MCP API 调用失败`。

🔴 **开了 overage 的号用满后上游回 `402 OVERAGE_REQUEST_LIMIT_EXCEEDED`**（不是 `MONTHLY_REQUEST_COUNT`），
且**不会继续计超额**（`currentOveragesWithPrecision` 恒 0）。0003 之前 kiro 卡在死号上不换（10-15 #128 卡 87 分钟）。
判号死没死：admin `/credentials/{id}/balance` 的 `remaining`，或 `getUsageLimits` 的 `currentOveragesWithPrecision`。

### 改 kiro / 升上游 = 三个脚本（10-15 固化，别再手搓）

```bash
# 0. 查官方有没有新版（三处都要查，见上「查官方有没有新版」）；新补丁写成 k8s/kiro-rs/patches/000N-*.patch

# 1. 188 上：干净上游 → 打补丁 → cargo test 上游/补丁对照 → 链式复现 → 构建 → tarball + sha
scripts/kiro-rs-build.sh <上游sha|tag> <镜像tag>            # 先加 --skip-build 只验补丁+测试
#    判据：失败集合与干净上游相同 + 补丁后通过数更多 + 无「test_xxx is never used」
#    （10-15 实测 210 → 235；漏 #[test] 的阴性对照 rc=1 已验）

# 2. 送进 242（不碰生产）。🔴 带 --keep-tarball，否则第 3 步结束就删了 198 上的 tarball，e2e 找不到
scripts/kiro-rs-ship.sh <镜像tag> <sha256> --keep-tarball

# 3. 隔离 E2E：198 docker 起新镜像，单号 + 假 refreshToken，打真上游 13 例（10-16 加 kimi-k3 两例）
scripts/kiro-rs-e2e.sh <镜像tag> --tarball /home/cltx/kiro-ship/img.tar.gz && ssh cltx@10.68.13.198 'rm -rf ~/kiro-ship'
#    阴性对照：同脚本跑旧 tag，「tool_result 夹图片」和 models 条数应 STOP（10-15 实测旧版 9/11）
#    加了透传模型 ⇒ 先改 `kiro_rs_e2e_cases.py` 的 EXPECTED_MODELS（10-16 33→34）再跑，否则 models-list 必 STOP

# 4. 切生产（Recreate 停机 ~10s；自动备份 deploy、打印回滚命令、比切前切后池子）
scripts/kiro-rs-ship.sh <镜像tag> <sha256> --switch
#    改了 admin-ui ⇒ 加 --expect-ui '<补丁新增的字面串>'：线上 bundle 不含它就 STOP。
#    🔴 「Admin UI 200 + 有 bundle」旧镜像也满足 = 假绿；字面串要先在干净上游 admin-ui/src 里 grep 一遍确认 0 命中
#    （10-16：`lastUsedAt` 旧组件就有，会假绿；用的是 0004 的 `(\.\d{3})\d+/`）。

# 5. 经 LiteLLM 回归全部 kiro-* 道（in_tok 基线比对抓静默降级）
scripts/kiro-lane-regress.sh --rounds 2
```

🔴 **补丁打不上 = 上游改了同一处**（10-15 升级时 0001 三处冲突，因为上游自己加了 opus-5.5）。
按新上游重做那个补丁：只保留上游没有的部分，脚本化移植（每处替换断言命中 1 次），
先例 `k8s/kiro-rs/patches/port_0001_to_v2026.10.1.py`。重做后用 `kiro-rs-build.sh --skip-build` 验。

🔴 **从补丁里截取新增行时 `#[test]` 会掉**：它在 `fn test_xxx` 的上一行，按 fn 行切区间就漏了。
cargo **不报错只是不跑**，只出一行 `warning: function test_xxx is never used`。10-15 漏了 2 个，
`kiro-rs-build.sh` 现在见这句就 STOP。

脚本把下面这些坑都固化了（出问题时读这里）：

- 188 无 rust 工具链 ⇒ `rust:1.92-alpine` 容器里跑，`apk add musl-dev perl make`；
  rust-embed 编译期要 `admin-ui/dist/` 存在（测试前塞空 index.html，构建前删掉）；
  上游/补丁两棵树 **target 目录分开**。`cargo fmt --check` 上游自己就不干净，不当门禁。
- 🔴 本机 → 198 整包 scp 会断（10-15 两次落地 0 字节，sha=`e3b0c4…`），198 也 ssh 不进 188
  ⇒ 本机中转、2MB 分块逐块 `ssh cat`、188/本机/198/242 四处对 sha。
- 198 上 `docker` 要 `sudo -n`；`docker load` 要先 `gunzip`（直接喂报 `media type text/html`）。
- ssh 远端变量值带空格必须自己加引号（`ssh host "VAR='a b' bash -s"`），否则被拆词成命令。
- 188 上 `docker images` 会被清，tarball 留在 `/Data/kiro-build/kiro-rs-*.tar.gz`。
- 🔴 `kubectl exec … | grep -qx` 在 `pipefail` 下会被 SIGPIPE 打成假 STOP（10-16 镜像明明在 242，
  第二次 ship 报「images ls 里没有」）⇒ 远端输出先落变量再 grep。`kiro-rs-ship.sh` 已改；新写的脚本照这样写。
- 🔴 **`"$VAR」"` / `"$VAR（"` 在本机 bash 3.2 + UTF-8 下把全角字符首字节吞进变量名** ⇒ `set -u` 报 `VAR�: unbound variable`
  （没 `set -u` 则静默展开成空）。10-18 `ship --expect-ui` 判据已过，却在打印 ✅ 那行崩 rc≠0；198 的 bash 5.1 不复现。
  紧贴中文的变量一律 `${VAR}`；改完跑 `scripts/lint-bash-cjk-vars.py`（10-18 全仓扫出并修了 26 处 / 20 个脚本）。
- 🔴 透传新模型 = 补丁里**三处**一起加：`PASSTHROUGH_MODELS`（窗口取 catalog `maxInputTokens`）、
  `/v1/models` 一条（`max_tokens` 取 `maxOutputTokens`）、单测（新名 + `kiro-` 前缀 + 一个相近名的阴性对照）。
  先例 `0005-kimi-k3-passthrough.patch`（在 188 构建树上 cp → 断言替换 → `diff -ruN --label` 生成）。

### 改 Admin UI（`:30890/admin`）= 只动前端的那一类补丁

现役 UI 补丁：**0004 排序**（启用→禁用、组内 `lastUsedAt` 倒序）+ **0006 查询栏**（10-18：关键字/状态+禁用原因/登录方式/优先级/域名/只看失败 + 卡片标题栏防溢出）。规则与依据见 `patches/README.md`。

**一条龙（10-18 定型，按序跑）：**

```bash
# 0. 上游源码在补丁基线上（verify.sh 会核 HEAD == README 里的 master sha）
scripts/kiro-rs-src-sync.sh
# 1. 改代码：在 .work/ 里拷干净上游 admin-ui → 打已有 UI 补丁 → 用断言替换脚本改（每处命中 1 次）→ 对「上游+前序补丁」diff 出新补丁
#    diff -u --label a/admin-ui/<路径> --label b/admin-ui/<路径>（新文件左边用 /dev/null）
# 2. 本机验：干净上游 + 全部 admin-ui 补丁 → tsc/vite → mock 回放 live 池子 → Playwright（写请求 >0 即 STOP）
scripts/kiro-admin-ui/verify.sh            # --keep 保留工作树；截图在 .work/kiro-admin-ui-verify/
#    阴性对照：10-18 把 0006 挪走再跑 ⇒ STOP（查询栏元素找不到），证明测试真在测新功能
# 3. 188 构建 + 送 242（不切）→ 4. 切生产带 --expect-ui → 5. lane regress（见上「三个脚本」）
scripts/kiro-rs-build.sh 22f36e69 v2026.10.1-carher-<新名>
scripts/kiro-rs-ship.sh v2026.10.1-carher-<新名> <sha256>
scripts/kiro-rs-ship.sh v2026.10.1-carher-<新名> <sha256> --switch --expect-ui '<补丁新增中文文案>'
scripts/kiro-lane-regress.sh --rounds 2
```

- 文件：`admin-ui/src/components/dashboard.tsx`（分页前 `sortedCredentials` → `filteredCredentials`）、`credential-filters.tsx`（0006 新增，纯函数 `filterCredentials` + 组件）、卡片 `credential-card.tsx`、字段类型 `types/api.ts`。
- **「不影响主功能」的边界**：只过滤/排序显示用的那份列表；批量操作按勾选 ID、「清除已禁用」按全量 `data.credentials`，都不能改成用过滤后的列表（否则筛选状态下点「清除已禁用」会只清一部分，或反过来误清）。调度永远只看后端 priority。
- 下拉选项与计数从 live 数据动态算（禁用原因、域名都会变），别写死枚举。
- 改排序前先拿 live 数据跑同一个比较函数看第 1 页，用户要的是「第一页看到什么」。
  想按「什么时候用完」排 ⇒ 直接用 `lastUsedAt`：`QuotaExceeded` / `InvalidRefreshToken` 禁号时后端写成禁号时刻；`Manual` 不改它。
- `--expect-ui` 选**补丁新增的中文文案**最稳（vite 不改字符串、上游里 `grep -rF` 0 命中容易核）。0004 用 `(\.\d{3})\d+/`，0006 用 `没有符合条件的凭据`。
- `kiro-rs-e2e.sh` 13 例全是 API 行为，只改 UI 可跳过（汇报写明）。cargo test 对照仍要过（rust-embed 把 UI 编进二进制）。
- 镜像 tag 每次起新名（`-sort2`、`-filter`），别复用现役 tag：242 containerd 同名会被覆盖，回滚链就断。
- 🔴 本机 `pnpm build` 内存紧时 tsc 被 SIGKILL（rc=137，**没有任何报错文字**，像「编译失败」）⇒ `node node_modules/typescript/bin/tsc -b` 直跑（verify.sh 已这样）。
- 🔴 Playwright 定位 `[aria-live=polite]` 会撞 sonner 的通知区（也是 aria-live）⇒ 带标签名 `span[aria-live=polite]`。
- 🔴 `curl http://…:30890/admin/`（带尾斜杠）= **404 空 body**，只有 `/admin` 200。手动验 bundle 时用 `/admin`，否则会把「没拿到 bundle」误读成新 UI 没上。

**坑：**
- 🔴 **`ssh 242` 这条路不存在** —— `cltx` 登不上 242（publickey/password 全拒，
  本机直连、188 跳板、198 跳板 10-04 三条都实测被拒）。§D2 早就记了这件事，
  而这段流水线以前写的是 `ssh 242 'gunzip … import'`，**两处矛盾了一个月**。
  进 242 的 containerd 只有「特权 pod 挂 socket」这条路（`kiro-rs-ship.sh` 第 3 步，importer pod 用现役镜像免拉取）。
- 🔴 **`scp` 打印 `lost connection` 却 `rc=0`** —— 10-04 从 188 往 242 推时实测，
  退出码是 0 但文件根本没过去。**又一个「返回码不是判据」**，必须去目的地核文件与 sha。
- **`k3s ctr images import` 不吃 gzip**。直接喂 `.tar.gz` 报 `archive/tar: invalid tar header`，
  而且**不给你看清退出码**。必须 `gunzip -c` 管道进去。
- 242 上 **`crictl` 不在 PATH**，`k3s crictl` 缺 config，只能用
  `k3s ctr -a /run/k3s/containerd/containerd.sock`。
- `strategy: Recreate` ⇒ **先杀后起**，镜像没预拉成功就是纯停机。先 import 并复核 `images ls`，再 set image（`kiro-rs-ship.sh` 不带 `--switch` 就只做到这步）。
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

### §D-1 上游出新模型 → 开给全员：七步清单（opus-5.5 09-29、sonnet-5.5 10-04 都走这条）

| # | 做什么 | 判据（不是 200） | 断服？ |
|---|---|---|---|
| 1 | `kiro-probe.py has <上游id>` 看几个号有 entitlement | 并集里有；FREE 档没有是正常的 | 否 |
| 2 | 直打 kiro.rs 发新名 | 🔴 **200 不算**：先读 `map_model()` 源码看有没有分支，没有就是被短名劫持 | 否 |
| 3 | 改源码（分支排在短名**前**）+ 单测带老名阳性对照 → 188 构建 | 失败集合 == 改前基线 | 否 |
| 4 | 镜像送进 242（特权 pod，§C）→ `set image` 两个容器 + 同一次开 `RUST_LOG=debug` | 池子 `total/available/currentId` 与切前一致 | **是，~10s** |
| 5 | 落点：nonce 配对读上游 `modelId`，新名 + 老名阳性对照，**直打和经 LiteLLM 各一次** → 立刻收回 `info` | `modelId` 逐字对 | 收 debug 再断 ~10s |
| 6 | LiteLLM 加道（§D0）：**价要官网现查、窗口与 `max_output_tokens` 按 catalog 改**（模板三样都会带错）；兜底 `fallback-set-rows.py`（新组多行一次写）；`EXPECT_LANES`+1、基线追加一行 | 4/4 副本 `wc -c`；`kiro-lane-limits-verify.py` 绿 | 否（滚动）|
| 7 | 开 key：按**上一个同族模型的 alias 覆盖面**导名单（skill `litellm-198-key-allowlist`「新模型开给和 X 一样的那批人」）→ 刷飞书表 | 回库数：带新名 key 数 == 带老名 key 数 | 否 |

⚠️ 第 4、5 步能合成一次重启（切镜像时顺手开 debug），收 debug 那次省不掉。

**10-16 kimi-k3 补进来的五条（每条都实际卡过）：**
- 第 6 步 `kiro-lane-add.py --template` 会把模板的**价 + 窗口 + 输出上限**一起抄过来（kimi 抄 glm-5 ⇒ 200K/64K/$1/$3.2），
  生成后**必改**：窗口/输出按 catalog，价按厂商官网（Kimi 文档 `platform.kimi.ai/docs/pricing/chat.md` 可经 198 curl）；
  改完用 yaml 比对断言「model_list +1 行、其余行/顶层键逐字相同」再 cutover。
- 🔴 **kiro 上游拒「没有 `description` 的工具」**：`400 Invalid tool use format`（kimi-k3/glm-5/sonnet-5 同形）。
  探针造工具必带 description，否则会把好道判死（10-16 第一次直打就这么 502 了两发）。
- 兜底腿不是现成组时先 `/model/new`（**不传 `model_info.mode`**），带 `disable_fallbacks:true` + 函数工具真打过再进链。
- 兜底链演练（死主腿临时组 + 临时 key 挂 per-key fallbacks，见 skill `litellm-198-fallback-chain` §10.3）：
  两跳都要演，量墙钟；kiro 道加 `model_group_retry_policy {DefaultRetries:1}`（同 §10.4）。
- 第 7 步「同族覆盖面」若不适用（用户点名「所有 cursor 除 deepseek-only」），名单从 DB 现导，`--only-file` + `--include-blocked`，
  回读查三个数：命中名单数 / 被排除那批仍为 0 / 范围外 0 误伤。

CM `litellm-config` 的 `model_list` **纯加法**（当时 74→92，added=18 / removed=0）。
Secret `litellm-secrets` 加 `KIRO_RS_API_KEY` 用 **merge patch**，别整体替换。
⛔ litellm-proxy 变更**禁 apply**，只用 `set image`/`patch`。

### §D-2 kimi-k3 上线实录（10-16，七步第三次走通）

| 层 | 现状 | 回滚 |
|---|---|---|
| kiro.rs | 补丁 0005 透传 `kimi-k3`（2.4x / 1M / 输出 131072，带 IMAGE）| `set image … v2026.10.1-carher` |
| LiteLLM 道 | `kiro-kimi-k3`（`anthropic/kiro-kimi-k3`，价按 Kimi 官方 $3/$15/读缓存 $0.3/写缓存 $3）| deploy 卷名 patch 回 `litellm-product-proxy-config-43ba11c9aac3` |
| 官方组 | `kimi-official-k3` = DB 行，`custom_openai/k3` @ `api.kimi.com/coding/v1`（🔴 用户给的 `sk-kimi-` 是 **Kimi Code** key，moonshot.cn/.ai 都 401；**5h 100 次**，与 `sa-kimi-k3` 是两个号）| `/model/delete` id `05ef5591-…` |
| 兜底 | 全局两行 `kiro-kimi-k3 → kimi-official-k3 → ag-gemini-3.8-flash`、`kimi-official-k3 → ag-gemini-3.8-flash`；`kiro-kimi-k3` retry 1 | `198:~/rs-bak/fallbacks.pre-kimi-k3-20261016.json` / `router_settings.pre-kimi-retry1-20261016.json` |
| key | 551 把 cursor（除 163 把 deepseek-only，含 46 blocked）`kimi-k3` + alias → `kiro-kimi-k3`；liuguoxian 4 把原 alias `openrouter-kimi-k3`（402 死）被覆盖 | `--restore ~/kimi-k3-full-20261016T115446.json` |

演练（死主腿临时组+临时 key）：主腿死 → kimi 官方 4~5s；主腿+官方都死 → gemini 30s（官方腿默认重试 5 次，未改）。
🔴 **kiro 上游拒「没有 `description` 的工具」**：`400 Invalid tool use format`，kimi-k3/glm-5/sonnet-5 三个模型同形，
不是 kimi 特有；探针造工具一律带 description，否则会把 kiro 判死。

### §D0 加一条 `kiro-*` 道（脚本 `scripts/kiro-lane-add.py`，09-29 泛化入仓）

🔴 **改的不是 `cm/litellm-config`** —— pod 挂的是**派生 CM**，走 `subPath`，**永不热更**（§E、
`feedback_198_lane_pods_mount_derived_cwfb_cm_not_litellm_config`）。
patch 母 CM 是零效果，而且这两份 09-29 实测已漂 161 行。

🔴 **派生 CM 的命名体系 10-04 变过一次，所以名字一律现读，别按形状猜：**

| 时期 | 名字 | 名字 = 内容 sha256 前 12 位？ |
|---|---|---|
| ~10-01 | `litellm-cwfb-stable-<hash>` | ✅ 成立（实测 `e17757c7b00a` 对得上）|
| **10-04 LiteLLM 升级后** | **`litellm-product-proxy-config-<hash>`** | 🔴 **不成立**（内容 sha 是 `556e2e32d2f9`，实名 `29b11b9fce9a`）|

⇒ 两条纪律：
1. **CM 名只从 deploy 现读**（下面第 1 步那条命令），`kiro-lane-add.py` /
   `kiro-lane-limits-price-fix.py` 的新名现在都**从 `--old-cm` 的前缀派生**，不写死。
2. 🔴 **「名字对上 = 内容对上」那条自校验只对旧体系成立** ——
   新体系下判内容只认在役副本内 `wc -c /app/config.yaml` 或自己算 sha，
   **别拿 CM 名当内容判据**。门禁 `test_fix_script_derives_cm_name_from_old_cm`。

```sh
# 1. 读 deploy 真挂的那个派生 CM（不是母 CM）
MOUNTED=$(sudo kubectl -n litellm-product get deploy litellm-proxy \
  -o jsonpath='{.spec.template.spec.volumes[0].configMap.name}')
sudo kubectl -n litellm-product get cm $MOUNTED \
  -o jsonpath='{.data.config\.yaml}' > /tmp/cm-cur.yaml

# 2. 生成新内容（纯本地，不碰集群）。--template 指照抄哪条道
python3 scripts/kiro-lane-add.py /tmp/cm-cur.yaml /tmp/cm-new.yaml \
  --name kiro-claude-opus-5-5 --template kiro-claude-opus-5 \
  --litellm-model claude-opus-5-5 --upstream-id claude-opus-5.5 \
  --rate-multiplier 2.0
# 它打印 NEW=<与 MOUNTED 同前缀>-<sha256(内容)[:12]>
# ⚠️ 10-04 起**别把这个名字当自校验**：新体系的生产 CM 名不等于内容 sha（见上表）。
# 判内容对不对只认在役副本内 `wc -c /app/config.yaml`。

# 3. 建新 CM → 带 test op 的守卫式 patch → rollout（命令由脚本原样打出）
```

⚠️ **`--template` 是为了继承而不是省事**：缓存价、窗口、`mode` 都从模板那条道抄。
手写漏掉 `mode` ⇒ `/测模型` 报「⚠️ 未测」，**与模型真的坏了同形**。

脚本拒跑的四种情形（都会让 dst 一个字节都不写）：
dst 已存在且不是本轮产物（09-29 真踩过 —— 拿上一轮的 `/tmp/lane-new.yaml` 去 diff，
看着"改动已在"实际什么都没生成）／道名已在（幂等）／模板道找不到或不唯一／
模板里没有 `model: anthropic/<id>` 行。

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

其余 16 条走裸 id。

⚠️ **「全部 `input/output_cost_per_token: 0`」已作废**（那是 09-12 刚接进来时的状态）。
09-23 起全部 kiro 道**都有真价**（`scripts/litellm-198-kiro-pricing.py` 按上游第一方目录价标定），
10-01 实测生产 **19/19 价 > 0、0 条为 0**。判价一律现读 CM，别信这类"当时的快照"。

### §D0.1 窗口 / 输出上限 / 价格：三件在 10-01 修掉的偏差（改之前先读这节）

加道之后**必须单独核这三样**，`--template` 一个都不保证：

| 项 | 真源 | 10-01 查出的偏差 |
|---|---|---|
| `max_input_tokens` | **kiro 自己的 catalog**（见下） | gpt 三条填了 **272000**（codex 口径）而 kiro 给 **1000000** ⇒ 白扔 73% 窗口 |
| `max_output_tokens` | 同上 | **19 条全缺**（整份 CM 89 条道只有 4 条有过这个字段）|
| 四个价格字段 | 上游**第一方**目录价 | `kiro-claude-opus-5-5` 照抄了模板 opus-5 的 $5/$25 ⇒ **贵标 25%**（官方 $4/$20/$0.2/$5）|

🔴 **窗口的真源是 kiro 自己的 `ListAvailableModels`，不是 `litellm-198-model-ceilings.json`。**
那张表是 **codex 口径**；**同一个模型名经不同上游，窗口本来就不同**
（`gpt-5.6-sol`：codex 872k / kiro **1M**）。拿 codex 的数去填 kiro 道，
就是 272000 这个错值换个方向再犯一次。

🔴 **`--template` 只保证形状，不保证价。** opus-5.5 是 Anthropic **降价**的一代，
照抄 opus-5 的模板就把旧价带了过来，而每一格可观察量都正常。
⇒ 加道后把四个价与官方第一方目录逐格比一遍（`model_prices_and_context_window.json`
的第一方条目 + 厂商官网页），**别只看道通不通**。

🔴 **计费读 `litellm_params`**：只写 `model_info` 会 200 且读得回，但照旧按老价记。
价格两块都写；窗口/输出上限只在 `model_info`。

```sh
# 1. 取真源（catalog 的 tokenLimits；origin 必须 AI_EDITOR/KIRO_CLI，IDE(11)/CLI(3) 是子集会假红）
python3 scripts/kiro-probe.py catalog --json > /tmp/kiro-cat.json
# 2. 改（纯本地，三道门：旧值断言 / 结构门 / 只增不删行数门）
python3 scripts/kiro-lane-limits-price-fix.py /tmp/cm-cur.yaml /tmp/cm-new.yaml \
    --catalog /tmp/kiro-cat.json --old-cm "$MOUNTED"
# 3. cutover 用它打印的命令（带 test op 守卫），然后逐**在役**副本验
python3 scripts/kiro-lane-limits-verify.py --catalog /tmp/kiro-cat.json --expect-bytes <新字节数>
```

⚠️ **`kiro-lane-limits-verify.py` 新写出来时先喂三个阴性对照再信它的绿**：
字节数故意写错 / 道数写错 / catalog 下毒一格（10-01 三个都报红，且第三个只报被下毒那一格）。

🔴 **kiro 车道 0 行在 `LiteLLM_ProxyModelTable`**（纯 CM 承载）⇒ 两个后果：
① `litellm-198-max-input-tokens-audit.sh 'kiro-rs/%'` 直接 `AUDIT ERROR` 退出，
**那把尺子对 kiro 全盲**；② **没有 DB 自收敛那条 90 秒捷径**，改 kiro 道必须 rollout。
⇒ 口径表已加 20 条 `kiro-` 前缀条目（key 写**组名**如 `kiro-auto`，不是 `model_info.id`
`kiro-rs/auto` —— 审计只拿 slug 与 model_name 匹配）。门禁
`backend/tests/test_kiro_lane_limits_fix.py`，含 codex 反向阳性对照。

⚠️ **非第一方的四条不下对错判语**：glm-5 / minimax-m2.x / qwen3-coder-next 在 litellm
价格表里**没有第一方条目**；deepseek-3.2 的 $0.40 与表里 `deepseek-chat` 的 $0.42 差 5%，
但 `deepseek-chat` 不是它的权威源。`auto` 是**约定价**（按 sonnet-5，09-23 用户裁决）。

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

✅ 行为层三向对照（临时 key，用完即删）——**09-29 起固化成脚本**：

```sh
# 值层（比快照）：scripts/litellm-key-drift-verify.py，判 now == old ∪ {新增名}
# 行为层（证真能打）：
LITELLM_MASTER_KEY=$MK python3 scripts/litellm-198-key-behavior-regress.py \
  --clone-from cursor-liuguoxian-std \
  --new claude-opus-5-5 --old claude-opus-5 --outside claude-opus-4-8
```

`claude-opus-5-5` → 200 走 alias；`claude-opus-5` → 200 仍落同名真实组（**没被新名劫走**）；
`claude-opus-4-8`（故意不进白名单）→ **403 `key_model_access_denied`**（证明闸门真在拦）。
⛔ 少了第三发就答不了"白名单到底有没有在生效"，只有前两发的绿是**恒绿**
—— 所以**脚本把"阴性对照没返 403"判成整轮无效**，不是一条普通失败：
闸门没在挡的时候，"通过"与"不通过"这两种世界的读数逐字相同。

🔴 三条只能用脚本、手搓 curl 必踩的：
1. **不许用 master key 打** —— 它不过 per-key 白名单闸门，打什么都 200 ⇒ 必然假绿。
   真 key 明文取不回来（库里只有 hash），所以唯一的路是克隆形状起临时 key。
2. **每发带唯一 nonce** —— 响应缓存开着，逐字节相同的请求到不了上游，那种 200 量的是缓存。
3. **判据是"回了非空内容"不是 200**（空内容 200 在 198 上是真实故障面）；
   而**响应体的 `model` 字段是请求名的逐字回显**，一个字都判不了落点
   —— 判落点看 SpendLogs 的 `model` 列，或上游侧 `RUST_LOG=debug`（§G）。
   脚本结尾固定把这句打出来，免得把行为层的绿读成"落点也对"。

脚本自己拒跑的四种情形（都是量不出东西的局，报红比出数有用）：
没 `LITELLM_MASTER_KEY`（⛔ 不许兜底默认值）／没给 `--outside`（这轮无分辨力）／
`--outside` 的名就在白名单里（对照是坏的）／参考 key 是 `models=[]` 全放行（克隆它 = 闸门全开）。
另外**建不出临时 key 时退 2 而不是 1** —— "什么都没量到"与"回归不过"是两件事。

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
| 3 | 真实推理 | 20 条道**文本非空**（不是 200），且各道 `prompt_tokens` 互不相同 |

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

⚠️ 批量压这 20 条时 `curl -m 120` 会偶发切断慢道（opus-4-6 中过一次，
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
python3 scripts/kiro-pool.py show --with-quota                 # 0. 先看在役号的 prio 是几（下一步要对齐）
python3 scripts/kiro-probe.py both --creds /tmp/newcred.json   # 1. 只读验号，不碰线上
python3 scripts/kiro-pool.py --dry-run add /tmp/newcred.json --priority <对齐在役>  # 2. 看请求形状
python3 scripts/kiro-pool.py add /tmp/newcred.json --priority <对齐在役>  # 3. 真加（自动备份+去重+前后复核）
python3 scripts/kiro-pool.py show --with-quota                 # 4. 看在役号还剩多少，再决定切不切
scripts/kiro-lane-regress.sh --rounds 3                        # 5. 全部 kiro 道回归（§E，10-16 是 21 条）
```

### 一次加多个号（10-01 实测 9 个；10-02 实测 20 个；10-08 实测 24 个；**10-09 `add-batch` 实测 9/9 一次过，~15 分钟**）

🔴 **一份新导出里的「死号」可能根本没重登** —— 10-09 那轮 69 个里 12 个新增，
3 个仍是 10-08 那三个死号，且 **refreshToken sha 与 `clientId` 逐字节相同、
`status` 还是 `invalid`** ⇒ 是旧快照原样带出来的，不是「重登了又死」。
⇒ **判「这批重登没」别看导出文件日期**，比两份导出里同一邮箱的 `refreshToken` sha +
`status` 字段；相同就直接跳过，别再浪费一次 probe 和一次 `add`。

**没有批量 API** —— 但 10-08 起**有批量入口 `add-batch`**（它内部仍是逐个 `add`，
每个自带备份 + 去重 + 前后复核）。⛔ **别再手搓 `for f in …; do add "$f"; done`**：
那段 for 挡不住下面三件事，10-08 全踩了（见本节末「🔴 rc=0 不是落地」）。

```sh
# 0. 在役号的 prio 是几（下一步 --priority 要对齐，别吃默认值 0）
python3 scripts/kiro-pool.py show --with-quota

# 1. 🔴 筛子：整库导出 → 只留新增 → 拍平过 6 道门（10-02 起有脚本，别再手搓 heredoc）
python3 scripts/kiro-batch-prepare.py ~/Downloads/kiro-accounts-NN.json --dry-run   # 先看几个是新的
python3 scripts/kiro-batch-prepare.py ~/Downloads/kiro-accounts-NN.json --out /tmp/kb

# 2. 逐个只读验号（同时证明不是封停号、读出档位与模型数）。
#    🔴 批量用 --summary-json，别 grep 人类表格（§0.1 那条红线）
for f in /tmp/kb/creds/*.json; do
  am=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['authMethod'])" "$f")
  [ "$am" = social ] && rot=(--out-rotated "${f%.json}.rotated.json") || rot=()
  python3 scripts/kiro-probe.py both --creds "$f" "${rot[@]}" --summary-json > "${f%.json}.probe.json"
done
# 判据：ok=true + modelCount 是付费档那个数。⚠️「全 0 且零报错」先查尺子
# 🔴 remaining=0 **不是**「不能加」—— 见下面「三类号」那张表，别用它当 assert
python3 - <<'PY'
import json,glob
rows=[json.load(open(p))[0] for p in glob.glob('/tmp/kb/creds/*.probe.json')]
assert rows, "空集假绿：一个 probe 结果都没读到"
dead=[r for r in rows if not r['ok']]                      # 真坏：refresh 就死
live=[r for r in rows if r['ok'] and (r.get('remaining') or 0)>0]
empty=[r for r in rows if r['ok'] and not (r.get('remaining') or 0)]
print(f"有量 {len(live)} / 耗尽但活 {len(empty)} / 不可用 {len(dead)}  共 {len(rows)}")
print(f"有量的合计剩余 {sum(r['remaining'] for r in live):.0f}")
for r in dead: print("  🔴", r.get('email'), r.get('error'))
PY
# ⇒ 拿这三个数去问用户加哪些（耗尽但活的号 11-01 00:00 UTC 自动重置复活）

# 3. 挑一个 --dry-run 看形状，然后 **add-batch** 真加（--priority 对齐在役）
python3 scripts/kiro-pool.py --dry-run add /tmp/kb/creds/<随便一个>.json --priority <对齐在役>
#    social 号要喂轮转后那份 ⇒ 先把 .rotated.json 顶掉原件，再整目录丢给 add-batch
for f in /tmp/kb/creds/*.rotated.json; do [ -f "$f" ] && mv "$f" "${f%.rotated.json}.json"; done
python3 scripts/kiro-pool.py add-batch /tmp/kb/creds/*.json --priority <对齐在役>
#    ↑ 一个失败不中断；收尾一张「加成/跳过(重复)/失败」表；**有失败就非零退出**

python3 scripts/kiro-pool.py show --with-quota && scripts/kiro-lane-regress.sh --rounds 1
rm -rf /tmp/kb        # 里面是 live refreshToken
```

**10-16 现行收尾（社交/IdC 混批，5 号 ~10 分钟）**：工作目录放仓内 `work-kiro-add-<日期>/`（已 gitignore，别放 /tmp），
add 完接 `kiro-enable-overage.py <social ids…> --wait 90`（IdC 跳过）→
`kiro_import_social_rt.py --pool-id <全部新 id>`（10-16 起 IdC 也收）→ 回归 → `rm -rf` 工作目录 → §D4 台账补一行。
🔴 **导出 `apiRegion: eu-central-1` 是现在的常态**（10-16 两批 6/6 都是）。`kiro-batch-prepare.py` 已自动丢弃/改写，
单号 `add` 有闸门拦；**手搓拍平时自己改成 us-east-1**，否则 Admin 余额 403、真推理也打 403 的 region。

🔴 **`add` 的 rc=0 不等于落地 —— 10-08 的坑，批量时最致命。**
admin API 拒绝时回的是 **HTTP 200 + body `{"error":{…}}`**，`curl` 对此 rc=0
⇒ 整个 `add` 安静地 exit 0，**看着像加成功了**。10-08 加 24 个号时 3 个
refreshToken 已死，服务端逐个拒，循环里**24 行全是 rc=0**；
当时唯一能看出来的线索是「比对了盘上 N 个号」那个 **N 停涨**（46/46、51/51、53/53），
而最终池子是 66 不是 45+24=69。
⇒ 已修进代码：`assert_landed()` 两道门（条数门 + 身份门，身份门读**盘上**那份因为
API 的行里没有 `machineId`、social 号 `email` 可能是 null），拒了就非零退出，
门禁 `backend/tests/test_kiro_pool_add_landing.py` 13 条。
**判加号成败一律看最终 `show` 的条数，别看返回码、别看输出像不像成功。**

⚠️ **手搓 for 还有两个坑，`add-batch` 一并修了**：
`dup_check` 命中会 `sys.exit` ⇒ 手搓循环**停在第一个重复上**（10-02 就写过这条警告，
但没给替代入口）；每个 `add` 输出上百行池子表格 ⇒ 成败那一行**被刷走**。
`add-batch` 把判重当「跳过」继续走，收尾出表。
（判重靠**异常类型** `DupFound` 分流，不按消息文本 —— `dup_check` 有三条不同的消息，
按文本匹配会漏 `machineId` 那条，把重复号记成失败。）

### 导出里的号分三类，别只分「能加 / 不能加」（10-08 定型）

| 类 | `probe --summary-json` 形状 | 能加吗 | 怎么处置 |
|---|---|---|---|
| 有量 | `ok=true` + `remaining>0` | ✅ | 直接加，立刻出力 |
| **耗尽但活** | `ok=true` + `remaining=0` + `modelCount=21` | ✅ **能加** | 加进去此刻不出力，**11-01 00:00 UTC 自动重置复活**。10-08 用户拍板「两类都加」|
| 不可用 | `ok=false` + `invalid_grant` | ❌ | 服务端也拒，**只能重新登录**（skill `kiro-account-manager-ops`）|

🔴 **`remaining<=0` 当 assert 会把「耗尽但活」误杀成废号。** 10-02 那版 SOP 里
`bad=[r for r if not ok or remaining<=0]` + `assert not bad` ⇒ 10-08 那批 24 个里
13 个 remaining=0，这个 assert 会**在验号这一步就整批停住**，而它们全是好号。
判「号是活的」只认 `ok=true` + `modelCount` 够数（catalog 打通了），**不是余额**
—— 这是 §0 那条「余额尺子判不出死活」的另一面（它既会把封停号读成可用，
也会把耗尽的好号读成废号）。

🔴 **IdC `400` 至少三种含义，不看响应体分不开**（10-08 为此手搓脚本读 body 才定位）：

| 响应体 | 含义 | 下一步 |
|---|---|---|
| `invalid_grant` / `Resource not found` | refreshToken 永久失效 | 重新登录，刷新救不回 |
| `Invalid profileArn.` | `getUsageLimits` **该带没带** | 号是好的，配额读不到而已，见 [[feedback_kiro_usage_limits_now_demands_profilearn]] |
| 其它 | 参数真拼错 | 读 body |

`kiro-probe.py` 10-08 起把 idc 分支的 `HTTPError` 接住并**打出响应体 + 归类**
（原来直接吐 20 行 urllib traceback，末尾只有 `HTTP Error 400: Bad Request`，
**body 被丢掉**）。social 分支早就这么做了，两边现在对称。
⇒ **分辨法**：单独跑 `kiro-probe.py catalog --creds <file>`，照样 400 的才是 token 死；
`catalog` 通而只有 quota 挂的，是 profileArn 那条，号是好的。

🔴 **阳性对照是第 0 步，不是事后补充。** 10-08 那 3 个 400 我先怀疑端点/调用形状，
拿**同一调用形状**喂一个已知好号（`cut.ansari`）立刻拿到 230 字节 accessToken
⇒ 一句话定位到「是这 3 个号」。没有这个对照，就会顺着「我 ARN 拼错了 / 端点变了」
查下去（[[feedback_synthetic_red_is_as_untrusted_as_synthetic_green]]）。

✅ **"同一个号的重新导出"10-01 已由 `dup_check()` 自动拦下，三把判据：**
`refreshToken` sha256 / **`machineId`** / `email`，任一命中就 `exit` 不发请求。

⚠️ **但 `dup_check` 是单号的最后一道保险，不是批量入口的筛子** ——
它命中就 `sys.exit`，20 个号的 for 循环会**停在第一个重复上**。
批量必须先跑 `kiro-batch-prepare.py` 把新增名单筛出来（第 1 步），
这也是 10-02 那轮 32 个号里挑出 20 个的做法。

背景：只比 refreshToken 的 sha **拦不住**这种 —— 重新注册 OAuth client 会把
`clientId` + `refreshToken` 全换掉，sha 自然对不上。10-01 那批 10 个里就有 1 个是池里
id14 的重复导出（`machineId` 逐字相同、只有 client 凭据换了），当时**靠邮箱那条 ⚠️
人工比出来的，脚本放行了**。额度按**账号**算 ⇒ 并存只会白占一个 id 且共用同一个 credits 池。

- ⛔ **别用 `userId` 做判据**：导出里有，但**服务端不落盘**（线上那 16 个键里没有它）
  ⇒ 拿它跟池子比是拿一个池里不存在的字段比，**恒不命中**。
  （10-01 第一版 SOP 就写错成 `userId`，根本执行不了，已改。）
- ✅ `machineId` 落盘且实测 **15/15 互不相同**，在这个池子里有身份区分度。
- 真要**替换**旧的那条：`remove <旧id>` 再 add，别两条并存。
- 验收这道门用了阴性 + 阳性双对照：重放 10-01 那份漏过去的重复导出 ⇒ 拦住；
  造一个全新号形状 ⇒ 放行。**只验"能拦"不验"不误杀"的话，一道全拒的门长得一样。**

🔴 **`--priority` 默认 0，而在役号可能是 1 ⇒ 不传就会抢役。** 数字小 = 优先
（`min_by_key`），默认 0 比在役的 1 小，新号立刻变 `currentId`。
10-01 加 id14（**STUDENT 只有 1000 credits，全池最小档**）时差点吃这个默认值：
抢役 ⇒ 约一天烧光、同时让 id11/12/13 的 1.7 万 credits 闲置、一天后又触发一次切换。
⇒ **第 0 步先 `show` 看在役号的 prio，把 `--priority` 对齐着传**，让新号与它平级
（平级取先出现的那条 ⇒ 老号仍在役）。
⚠️ **但"加完是 prio=N"只描述写入那一拍，不是稳态**：10-01 我把 10 个号全写 `--priority 1`，
两小时后现读变成了 0/1/2/3 四层，而我没执行过 `priority` 命令
（[[feedback_kiro_pool_priority_and_disabled_change_without_us]]）。
**dry-run 能看出这一点**（它复核时会打 `currentId`）
—— 这是 dry-run 真正挡下来的东西，不是"看个形状"。

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
   绿的样子：`AI_EDITOR` 读出**付费档 21 个（10-02；10-01 还是 20）/ FREE 档 9 个**模型 + 档位/额度读出来
   （这一步同时证明**不是封停号**）。🔴 **别把条数当固定判据** —— 09 月记的是 19、
   10-01 付费档已是 20、FREE 档只有 9（见 §0 档位↔模型）。**条数变了先查档位，不是先怀疑探针。**
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
  默认 `overageStatus: DISABLED`。

### 开启 overage（10-14 实测，API 方式，无需登录 kiro.dev）

```sh
cd /tmp   # 🔴 避免 operator/__init__.py shadow
python3 scripts/kiro-enable-overage.py --check 128 129 130   # 先只读看当前状态
python3 scripts/kiro-enable-overage.py 128 129 130           # 真开
python3 scripts/kiro-enable-overage.py all-new               # 或：所有未消耗的 social 号
```

API = `POST https://management.us-east-1.kiro.dev/setUserPreference`，body：
```json
{"overageConfiguration": {"overageStatus": "ENABLED"}, "profileArn": "arn:aws:codewhisperer:us-east-1:699475941385:profile/EHGA3GRVQMUK"}
```
UA 与 `getUsageLimits` 相同（`kiro_client.rs:395` `with_kiro_runtime_management_headers`）。

**三个坑（10-14 全踩了）：**
1. 🔴 **200 不代表立刻生效**：`setUserPreference` 返回 200 后立刻 `getUsageLimits` 验证，
   `overageStatus` 仍读出 `DISABLED`。**有传播延迟**：10-14 等几秒 12/13 翻，10-15 id141
   6s 后仍 DISABLED、**~30s 才翻**。⇒ 脚本已改成开完轮询（`--wait`，默认 45s），别拿首读判失败。
2. ~~gmail 号有账号级限制~~ **10-15 证伪**：id136（`nerbonnedrivepennino@gmail.com`）现读
   `ENABLED`、cap 10000。当时的「缺 `overageCap` 字段」是**脚本读错了位置**——cap/rate 在
   `usageBreakdownList[0]`，不在 `overageConfiguration`（那里只有 `overageStatus`），所以全员都是 None。
   当时它没翻多半就是传播延迟没等够。⇒ 未翻的号先过 1 分钟重 `--check` + 对照已开过的号。
   非 icloud 的 social 号可能受上游限制。
3. 🔴 **只对 social 号可行**：脚本走 `/refreshToken`（social 刷新路径）。
   IdC 号的 token 走 AWS OIDC `/token`，且 social 那个 `profileArn` 不认 IdC token（403）。

**⚠️ 开了 overage 后该号用完 credits 按 $0.04/credit 继续计费，上限 10000 credits = $400。
账单决定。**

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
| 14~25 | 10-01~10-02 那两批 | — | 见 `[[project_kiro_pool_batch_add_nine_student_2026_10_01]]` | 全 `KIRO STUDENT 1000`；id14 首见 STUDENT 档，id25 是第 5 个 social 号 |
| 53 | `primmer.calcium-9o@icloud.com` | 10-08 09:26 | `credentials.json.20261008-092632` | `provider:"Github"` 第三种形状，脚本直通零新坑。**加进来时是全池唯一有量的号**：`KIRO PRO+ 2000/2000` 满额、21 个模型，而在役 id44 只剩 155.9 且在以 ≈17 credits/min 掉（四分钟后 86.67）⇒ 按 §D3 第 4 步仍**不切**，让 `QuotaExceeded` 那条兜底自己接。零重启、45/45 落盘、回归 40/40 绿。⚠️ social 第 **4** 个未轮转样本（前后 sha256 相同，len=230）—— 四个了仍不翻案，`--out-rotated` 照带。🔴 这轮不在公司内网，走的是 §0.2 那两条隧道 |
| **26~45** | **20 个 `*@binus.ac.id`** | **10-02 22:5x** | 逐号 `credentials.json.<ts>`（`add` 每次自动备份，20 份） | **单轮最大批次**，源 `~/Downloads/kiro-accounts-32-2026-10-02.json` 的 32 个里**去重后剩 20 个**。全 `KIRO STUDENT 1000`、**21 个模型**（含 `claude-opus-5.5` + 上游 10-02 新出的 `claude-sonnet-5.5`），合计 ≈**19240 credits**。12 个 social（Github）+ 8 个 idc（BuilderId）。`--priority 2` 对齐在役 id17。回归 **19/19 绿**、`in_tok` 全在基线 ±5% 内、pod `restarts=0`、`currentId` 全程未动。详见下「§D4.1」 |
| 46~52 | 7 个 `*@binus.ac.id` | 10-0x | — | 盘上 `email` 是 **null**（social 导出那种形状）⇒ 判「新增那条是不是我的」时 email 那把判据对它们**恒不命中**，身份门必须能只靠 `machineId` 认（`assert_landed()` 为此读盘上而不是 API）|
| **54~74** | **21 个 `*@binus.ac.id`** | **10-08 14:2x~14:4x** | 逐号 `credentials.json.<ts>`（21 份） | 源 `~/Downloads/kiro-accounts-60-2026-10-08.json` 的 **60 个里 36 个已在池**（判重命中，含 4 个只中 `rt_sha+machineId` 的 social 漂移）、24 个新增、其中 **3 个加不进去**（refreshToken 死）。全 `KIRO STUDENT 1000` + 21 个模型；加进去时 11 个有量（≈10812 credits）、10 个耗尽但活。池 `45→66`、`available 2→18`，零重启，回归 **20/20 绿**。🔴 **加号过程中在役自己从 id53 换到 id58**：id53 的 PRO+ 从 150 掉到 0 ⇒ `QuotaExceeded` + `min_by_key` 重选，又一次兑现「别提前切 priority」那条兜底。详见下「§D4.2」 |
| **75~83** | **9 个 `*@binus.ac.id`** | **10-09 18:3x** | 逐号 `credentials.json.<ts>`（9 份） | 源 `~/Downloads/kiro-accounts-69-2026-10-09.json` 的 **69 个里 57 个已在池**、12 个新增、其中 3 个仍是死号。**第一轮用 `add-batch`**：9/9 一次过、收尾表直接给结论（不用再数「盘上 N 个号」）。全 `KIRO STUDENT 1000` + 21 个模型，满额 ≈**9000 credits**。池 `66→75`，零重启，在役仍 id70（182 余额，按 §D3 第 4 步不切）。回归 20/20 绿。<br>🔴 **那 3 个死号（`aldi.iasha`/`christian.oey`/`denis.darmawan`）的 token 与 10-08 那份导出逐字节相同、`status` 仍是 `invalid` ⇒ 没重登过，是旧快照原样带出来的**。判「这批到底重登没」别看导出日期，比 **refreshToken sha + `status` 字段**。<br>⚠️ 本轮消耗速率：10-08 加的 11 个有量号，**一天后只剩 2 个**（id70 243、id74 满额）⇒ 一轮加 9~21 个号撑不过两三天，见 §0 那条 credit 墙。 |
| 97 | `pedro-henrique@googlegroups.com` | 10-12 16:23 | `credentials.json.20261012-162331` | `provider:"Github"` 第三种形状（只有 `email`+`refreshToken`+`provider`），**单号 `add` 脚本直通零新坑**。`KIRO PRO+ 2000/2000` 满额、21 个模型（含 `claude-opus-5.5`/`claude-sonnet-5.5`）。池 `88→89`、`available 4→5`，零重启，在役仍 id93（prio 对齐 2，按 §D3 第 4 步**不切**）。回归 **20/20 绿**、`in_tok` 19 条全在基线 ±5% 内。⚠️ social 第 **5** 个未轮转样本（`--out-rotated` 写出来的那份 `rotated:false`、前后 sha256 相同、len=230）—— 五个了仍不翻案，照带。🔴 **加进来时它是全池唯一满额的号**：池里 89 条只有 5 条 available，id94~96 全 `succ=0`，其余 80+ 条都 `disabled=True reason=QuotaExceeded` |
| **98~99** | `zahwa.zefira@binus.ac.id` / `sherley.suciati@binus.ac.id` | 10-12 16:35 | 逐号 `credentials.json.<ts>`（2 份） | 源 `~/Downloads/kiro-accounts-79-2026-10-12.json`：**79 个里 74 个判重命中、筛子报"新增 5 个"，但其中 3 个是已知死号**（`christian.oey`/`denis.darmawan`/`aldi.iasha`，rt sha + `clientId` 与 10-11 那份逐字节相同、`status` 仍 `invalid` ⇒ 没重登，按 §D3 那条直接跳过不浪费 probe/add）⇒ **真新的只有 2 个**，都 `status=active`、`KIRO STUDENT 1000` 近满额（999.99 / 999.97）、21 个模型、idc。`add-batch` **2/2 一次过**，池 `89→91`、`available 5→6`，零重启，回归 **20/20 绿**、`in_tok` 19 条全在基线 ±5% 内。<br>🔴 **这份导出里 8 个号的 `email` 从 `*@binus.ac.id` 改成了 `*@outlook.com`**（`remynfhqusai` 等，池里 id89~96）—— rt sha + `machineId` 与 10-11 那份逐字相同，只是 account-manager 里换了邮箱标签。判重**只中 `rt_sha+machineId` 不中 `email`** 正好拦住；⚠️ 反过来说**按 email 关联账号的任何脚本都会把它们当新号**，这是 §0 那条「盘上 email 不可靠、必须有第二把判据」的又一次兑现。<br>🔴 **加号过程中在役自己从 id93 换到 id94**（我没执行 `priority`），又一次 `QuotaExceeded` + `min_by_key` 兜底。 |
| **128~140** | **13 个 social/Github PRO+ 2000**（12 个 `*@icloud.com` + 1 个 `*@gmail.com`） | **10-14** | 无单独备份（`add-batch` 内含） | 全部从 kiro-account-manager 导出，`authMethod=social`，**只需 `refreshToken` + `email`**（脚本自动派生 `machineId`）。12 个用 `add-batch --priority 2` 一次过 + 1 个 `add --priority 2`。全 `KIRO PRO+ 2000/2000` 满额、21 个模型。池 `119→132`，零重启。<br>🔴 **本轮首次用脚本批量开启 overage**（`kiro-enable-overage.py`）：13 个目标，12/13 返回 ENABLED，1 个（id=136 `nerbonnedrivepennino@gmail.com`）200 OK 但始终 DISABLED 且缺 `overageCap` 字段 —— gmail 号的账号级限制，非脚本 bug。<br>🔴 **setUserPreference 200 不代表立即生效**：第一轮验证全报 DISABLED，等数秒重验 12 个翻成 ENABLED（传播延迟）。 |
| 141 | `crier.vapid.1d@icloud.com` | 10-15 17:34 | `credentials.json.20261015-173423` | `provider:"Github"` 第三种形状，单号 `add --priority 2` 直通零新坑；`KIRO PRO+ 2000/2000` 满额 21 模型。池 132→133，在役仍 id137 不切。overage 用 `kiro-enable-overage.py 141` 开：首验 DISABLED、`--check` 6s 后仍 DISABLED，**~30s 后才翻 ENABLED**（传播延迟比 10-14 记的「数秒」长）。同步写进 AcctMgr accounts.json（111 条，现已固化为 `kiro-account-manager-ops/scripts/kiro_import_social_rt.py --pool-id`）。social 第 6 个未轮转样本。回归 20/20 绿 |
| 142~146 | `cobble-gases0z` / `inviter-dishes0g` / `mooch.pumas13` / `motor_bans.0p` / `sparse_husks_9j`（均 @icloud.com） | 10-15 23:36 | 每号加前一份，末份 `credentials.json.20261015-233652` | 5 个 `provider:"Github"` 导出，`kiro-batch-prepare.py` 判 5 新 0 重 → 逐个 `kiro-probe.py both --out-rotated` 全 `KIRO PRO+ 2000` 满额 21 模型 → `add-batch --priority 2` 5/5。池 133→138、available 3→8，在役仍 id139 不切。overage：轮询 45s 内只 1/5 翻 ENABLED，~1 分钟后 `--check` 5/5 ENABLED（传播延迟再次 >45s）。AcctMgr 111→116。回归 20/20 绿 |
| 147 | `caroladamsd64@gmail.com` | 10-16 10:32 | `credentials.json.20261016-103248` | **IdC/BuilderId 导出**（带 `clientId`/`clientSecret`，`apiRegion: eu-central-1`），`add --priority 2` 直通。`KIRO PRO+ 2000/2000`，🆕 **22 个模型 = 21 + `kimi-k3`**（§A「kimi 不在目录」已被翻）。rt 未轮转。⛔ 不开 overage（IdC token 不认 profileArn）。AcctMgr 写入走 `kiro_common.build_idc_entry`（`kiro_import_social_rt.py` 只吃 social），116→117。池 138→139，在役仍 id141。回归 20/20 绿（`gpt5.6-sol` 单发 116s 长尾）。🔴 **Admin 看余额 403「User is not authorized」= 导出带 `apiRegion: eu-central-1`**：kiro.rs 凭据级 apiRegion 优先 ⇒ 打 `q.eu-central-1`，同 token 在那里 getUsageLimits/ListAvailableModels 全 403、`q.us-east-1` 全 200。10-16 10:53 remove+add（apiRegion 改 us-east-1，id 仍 147，备份 `…20261016-105253`）后 `/balance` = PRO+ 2000/2000。`kiro-pool.py add` 已加闸门拒非 us-east-1（`--allow-api-region` 放行，门禁 `test_kiro_pool_api_region_gate.py` 含阴性对照）|
| 148~152 | `69-skycaps-pretzel` / `canoes.chewers22` / `coiner.layover.9c` / `refresh.tundra_1i`（@icloud.com，social）+ `i79843397@gmail.com`（151，IdC）| 10-16 12:15 | 末份 `credentials.json.20261016-121532` | 🔴 **5 份导出全带 `apiRegion: eu-central-1`**（social 还多带 `profileArn`）⇒ 拍平后先改 us-east-1 再验号（闸门会拦，这批是主动改的）。全 PRO+ 2000 满额 22 模型含 kimi-k3、rt 未轮转。`add-batch --priority 1`（在役 id147 已是 prio 1）5/5，池 139→144、available 6→11。overage 4 个 social `--wait 90` 一轮 4/4 ENABLED。AcctMgr 117→122（social 走 `kiro_import_social_rt.py`，151 走 `build_idc_entry`）。回归 21/21 绿 |
| 153~157 | `mattes08.whippet` / `mosque_flutes9o` / `stabler.son-5s` / `twang49purdah` / `yaw-swagger38`（均 @icloud.com，social） | 10-16 17:3x | `add-batch` 逐号自动备份 | 5 份 Github 导出全带 `apiRegion: eu-central-1`，`kiro-batch-prepare.py` 自动丢弃（判 5 新 0 重）。全 PRO+ 2000 满额 22 模型含 kimi-k3、rt 未轮转。`add-batch --priority 1`（在役 id150 prio 1）5/5，池 144→149、available 7→12，在役仍 id150。overage `--wait 90` 一轮 5/5 ENABLED（cap 10000）。AcctMgr 122→127。回归 21/21 绿。7 步 SOP 第四次直通零新坑 |
| 158 | `moses.yamamichi@binus.ac.id` | 10-17 09:0x | `add-batch` 自动备份 | 用户只给了**明文凭据**（AWS 密码 + 邮箱密码 + TOTP），先走 `kiro_batch.sh` BuilderId 登录（~35s 一次过），再把 AcctMgr 那条（email/rt/clientId/clientSecret/region/machineId）当导出喂 `kiro-batch-prepare.py`。`KIRO STUDENT 1000/1000`、21 模型（无 kimi-k3）。`add-batch --priority 1` 1/1，池 149→150、available 8→9，在役 id154 不切。IdC 不开 overage。回归 21/21 绿。🔴 **第一次登录写进 accounts.json 后 ~1 分钟被正在运行的 KiroAccountManager.app 用内存旧列表盖回 127**（脚本报 SAVED、壳报 127→127）⇒ 先 `osascript -e 'quit app "KiroAccountManager"'` 再重登，AcctMgr 127→128 |
| **159~163** | `cornelius.halim` / `nur.hatim` / `cheryl.renaby` / `richelle.carallo` / `evan.hendrawan`（均 @binus.ac.id，IdC/BuilderId） | **10-17 11:xx** | `add-batch` 自动备份 | **本轮新流程：先改密码 + 轮转 2FA 再加号**。5 个号走 `batch_pwd_2fa.sh`（`change_builderid_pwd_2fa.py` + `aws_mfa_rotate.py`），3/5 密码+2FA 全程成功、2/5 密码改好但 2FA 失败（`richelle`/`evan` 在 AWS 设备类型页早跳 ⇒ 注册了**内置身份验证器 passkey** 而非 TOTP）。🔴 **原 TOTP seed 仍可登录**——passkey 是额外注册，没替换 TOTP。第一轮 3 好号直接加（id159~161，池 150→153）；第二轮用原 TOTP 登录 richelle/evan，probe 全 STUDENT 1000 21 模型含 kimi-k3，add-batch 2/2（id162~163，池 153→155）。全 5 个 IdC 不开 overage。飞书多维表（`IhSNbXxl5azo9xsKxyhcO0lEnnO`）5 条均已标「已添加」。🔴 **`aws_mfa_rotate.py` 设备类型页等待逻辑 bug**：Privacy/Site terms 页脚按钮被当成业务按钮导致 `break` 太早 ⇒ 默认注册 passkey。已修补 lines 368-374（过滤非业务按钮 + 等 radio 元素）。richelle/evan 的 2FA 仍是原始 seed（未轮转），如需轮转须先在浏览器删 passkey |
| **164~166** | `grace.elisabeth` / `gustino.varianto` / `fendry.lie`（均 @binus.ac.id，IdC/BuilderId） | **10-17 15:xx** | `add-batch` 自动备份 | 3 个号逐个走改密码 → 加号全流程。`change_builderid_pwd_2fa.py` Step 4/5 合并修复后密码全部改成功。2FA 轮转 **3/3 失败**（AWS Security tab 显示 0 MFA 设备，注册页不渲染 radio — 非脚本 bug 而是 AWS Builder ID 页面改版）⇒ **全部用原 TOTP seed 登录成功入池**。kiro_batch.sh → probe 全 STUDENT 1000 → add 3/3（id164~166，池 155→158）。飞书多维表 3 条均已标「已添加」。🔴 **2FA 均未轮转**（原始 seed 仍在用），待找到能过 AWS CAPTCHA 的方式手动处理。🔴 **10-17 发现 AWS Builder ID 登录页改版**：旧 Google SSO 中转消失，直接 Builder ID 页面 + 「继续」按钮 + 密码页 CAPTCHA（"Security check"），`credentials.signin.aws` DNS 不再解析。所有自动化登录脚本需适配。 |

| **188~192** | `deni.aprizal` / `jovin.antono` / `maximillian.matthew` / `mohammed.akbar` / `redanka.karim`（均 @binus.ac.id，IdC/BuilderId） | **10-19 12:5x~13:0x** | `add-batch` 自动备份 | 用户给明文凭据 5 个号，走 10-17 那条「先改密码+轮转 2FA 再加号」全流程，**本轮 5/5 两项全成**（10-17 那批 2FA 3/3 失败的 AWS 改版问题，在 `aws_mfa_rotate.py` 的 `DELETE_OLD=1` delete-before-register + 页脚按钮过滤修好后不再出现）。`batch_pwd_2fa.sh` 本轮改成从 creds ndjson 读账号（原先账号硬编码在脚本里，而脚本在 git 跟踪区）。链路：`batch_pwd_2fa.sh`(5/5) → `kiro_batch.sh` 用新密码+新 seed 登录(5/5，AcctMgr 156→161) → AcctMgr 条目当导出喂 `kiro-batch-prepare.py`(判重 0 命中) → probe 全 `KIRO STUDENT` 21 模型 → `add-batch --priority 0` 5/5，池 178→183、available 25→30，在役 id186 不切。IdC 不开 overage。飞书表 108→113 条新建（表里原先一条都没有）。回归 21/21 绿。🔴 **`kiro-batch-prepare.py --out` 的 glob 会同时匹配 `*.json` 和 `*.rotated.json`**，`add-batch split/creds/*.json` 直接把每个号加两遍（dry-run 报「加成 10」才看出来）⇒ IdC 号 probe 报 `rotated:false`，加号前必须 `grep -v rotated` |

| **193** | `septian.aji@binus.ac.id`（IdC/BuilderId） | **10-19 13:2x~13:3x** | `add` 自动备份 | 同 id188~192 那条链，单号跑。用户**只给了 Kiro 密码 + MFA seed，没给邮箱密码** ⇒ `BINUS_PW` 为空也全程跑通（删旧 MFA 后重登确实不要 MFA，邮箱验证码兜底没被用上）。密码+2FA 1/1，登录 1/1（CAPTCHA 错一次自动重试），`KIRO STUDENT` 21 模型，`add --priority 0` 池 183→184，在役 id187 不切。飞书 113→114 新建。回归 21/21 绿。🔴 **「21 模型」别再顺手写成「无 kimi-k3」**：现读 catalog，kimi-k3 **在**（2.4x / 1M / 131K），缺的是 `fable-5.1`；10-17 批次那句「21 模型（无 kimi-k3）」是 kimi-k3 上线（10-16）前的旧话，本轮照抄说错了，id187/188/192 现查全是「含 kimi-k3、无 fable-5.1」，飞书 6 条备注已改正 |

### §D4.2 🔴 10-08 这批的三件事：rc=0 不是落地 / 余额不是死活尺子 / 400 有三义

24 个新增号的**结局是三类不是两类**，而 10-02 那版 SOP 的 assert 只认两类：

| 结局 | 个数 | 判据 |
|---|---|---|
| 加成功 | 21 | 池子条数真涨了，且涨的那条 email/machineId 与提交的一致 |
| 加不进去 | 3 | 服务端回 200 + `{"error":"…IdC Token 刷新失败: 400 …invalid_grant…"}` |
| （其中耗尽但活） | 10 | `ok=true` + `modelCount=21` + `remaining=0` ⇒ **能加**，11-01 自动复活 |

三条已固化进代码/文档，别再手工记：

1. **`add` 的 rc=0 不是落地** ⇒ `assert_landed()`（条数门 + 身份门）+ `add-batch` 收尾表，
   门禁 `backend/tests/test_kiro_pool_add_landing.py` 13 条（七道门逐条删都实测变红，
   阳性对照「真加成了放行」「全绿不非零退出」保持绿）。
2. **`remaining<=0` 不是「不能加」** ⇒ 验号脚本改成三分类，不再 `assert remaining>0`。
3. **IdC 400 至少三义** ⇒ `kiro-probe.py` idc 分支接住 `HTTPError` 打响应体并归类。

还有一条**没有写进代码、只能靠纪律**的：
🔴 **阳性对照是第 0 步。** 那 3 个 400 我先怀疑端点/调用形状变了，是拿**同一调用形状**
喂一个已知好号（`cut.ansari`，立刻拿到 230 字节 accessToken）才一句话定位到
「是这 3 个号」。这与 [[feedback_kiro_usage_limits_now_demands_profilearn]] 那轮
（拿 3 个有 usageData 的老号打同样形状、全是同一个 400）是同一个手法，
**两轮都是靠它才没顺着「我拼错了」查下去。**

### §D4.1 🔴 一份导出里混着「已在池里的号」—— 判重的两种形状都在这批里出现了

10-02 那份 32 个号的导出，**12 个已经在池里**（id14~25 那两批就是从同一个来源导出的），
只有 20 个是新的。⇒ **用户给的导出文件不是「新号清单」，是账号管理器的整库快照**，
拿到手第一件事是**跟线上逐号比**，别按文件名里的数字当新增数。

🔴 **三把判据各拦一种漂移组合，一把都不能省** —— 这批把三种形状全实证了：

| 形状 | 本批样本 | 命中的判据 |
|---|---|---|
| **原样重复导出** | 10 个（如 `alvin.abadi` ↔ id24）| rt sha / machineId / email **三把全中** |
| **换过 OAuth client** | `ferril.wananda` ↔ id14 | machineId + email（**rt sha 不中** —— 重新注册 client 会把 `clientId`+`refreshToken` 全换掉）|
| **social 刷新过** | `shyntha.respati` ↔ id25 | **只有 email 中**（rt 与 machineId 都漂了）|

🔴 **最后一行是「邮箱那把门」第一次成为唯一有效判据。**
10-01 补 `machineId` 的理由是「rt 换了但 machineId 不变」，而 social 号的 machineId
派生式 `sha256("KotlinNativeAPI/<rt>")` **跟着 rt 一起漂**（§D3 第 0 步）
⇒ **那条理由对 social 不成立**，只剩 email 能拦。
⚠️ 10-02 23:05 那份 36 个号的导出里，`aurora.salsabila`（我当天刚加成 id31）
也已经变成 `machineId+email` 命中、rt 不中了 —— **social 号的 rt 几小时内就会漂**。

✅ **这一整套 10-02 已落成 `scripts/kiro-batch-prepare.py` + 30 条门禁**
（`backend/tests/test_kiro_batch_prepare.py`）：比对三把判据 + 拍平 6 道门。
**阴性对照实测**：删掉 email 那把门 ⇒ 3 条红（含那条「只有 email 命中」的）；
删掉 machineId ⇒ 3 条红；拿掉不可见字符检查 ⇒ 3 条红；拿掉空集断言 ⇒ 1 条红。
**阳性对照**：全新号必须放行（少了这条，一道**全拒**的门与好门读数逐字相同）。
⚠️ 每条 `only_*` 用例刻意让**其余两把判据都不命中** ——
10-01 第一版测试把邮箱也写成相同的，于是删掉 machineId 那道门它照样通过，
**分不清谁拦的对照等于没有对照**。

⚠️ **拍平后要为 social 丢掉 `region`/`clientId`/`clientSecret` 三个字段**（见 §D3 第 0 步
第三种形状）：这批 12 个 social 号导出里 `region` 都是 `null`，但带 `profileArn`；
线上 id25 落盘也是 `region=None` ⇒ 不传、让服务端回落 `config.json` 全局值。

⚠️ **social 号必须拿 `--out-rotated` 那份去 `add`**：本批 16 个带 `--out-rotated` 的
探测**前后 sha256 全部相同**（又是 16 个未轮转样本，累计 20 个）——
**仍然不翻案**，代码按会轮转处理，`add` 照喂轮转后那份。

🔴 **收尾那条「尺子坏了长得像 20 个号全死」**：第一轮我用
`grep -E '^\s+(claude|gpt|…)'` 数模型行、`grep -oE 'KIRO [A-Z+ ]+'` 取档位，
**20 个号全部打出 `tier=[] model_lines=0 err=0`** —— 看着像整批凭据都废了。
真因是**探针输出的表格行没有前导空白、档位写在 `订阅` 那一行**，三条 grep 全都对不上。
⚠️ **连"报错行"那条 grep 也是 0** ⇒ 「全 0」本身就该先怀疑尺子：
**三个互相独立的量全部归零，比"20 个号同时死"更可能是一个共同的解析 bug**。
✅ **根因已修**（不只是"下次小心"）：`kiro-probe.py --summary-json` 吐合法 JSON 数组，
带 `ok`/`error` ⇒ 不用再 grep 人类输出，失败也不会变成"安静地少一行"。
见 §0.1 那条红线 + [[feedback_all_metrics_zero_at_once_is_the_ruler_not_the_fleet]]。

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
⇒ 判额度只认实时读；判「是不是重复号」**不看导出里的 `usage`/`id`/`userId`**（`userId` 服务端
压根不落盘），认 **`refreshToken` sha256 + `machineId` + `email` 三把** ——
10-01 起由 `kiro-pool.py add` 的 `dup_check()` **自动做**，任一命中就 `exit` 不发请求。
（⚠️ 这里以前写的是"只认 refreshToken sha256、**不看邮箱**" —— 那样会漏掉
"同一个号重新注册 OAuth client 后的重新导出"，10-01 真漏过一个，见 §D3 批量那节。）

### 重置日复活（`nextDateReset` = **每月 1 日 00:00 UTC**，别写死某一天）

🔴 **重置日会同时发生两件事，别只盯额度那一件（10-01 实测）：**
1. 额度归零复活 —— id9/id10 的 `2000/2000 耗尽` 变成 `0/2000`。
2. **订阅到期的号会掉成 `KIRO FREE`（cap 从 2000/5000/10000 变 50）并因此失去 11 个模型**
   —— 10-01 一天之内 id9/id11/id13 三个号掉档（PRO+→FREE、PRO MAX→FREE、POWER→FREE）。
   ⇒ **重置日后必须重读一次 `show --with-quota` 看档位，不是只看余额回没回来。**

🔴 **掉档的号与付费号混在优先级里 = 一颗定时炸弹（10-01 发现，未处置）。**
`filter(!disabled).min_by_key(priority)` 只看 `priority` 与 `disabled`，**不看档位** ⇒
FREE 档的号被选中时，**11 条 opus/gpt 道当场全挂**（那些模型 FREE 档没有），
而症状是模型级报错、不是「号死了」，很难一眼归因。

⚠️ **这个风险的具体排布会自己变，处置前必须现读。** 10-01 当天两次读到的就不一样：
- ~11:00 UTC：id12(PRO MAX) / id13(**FREE**) / id14(STUDENT) **三者同 prio=1**；
- ~14:00 UTC：id12→prio **0**，id13(**FREE**)→prio **1**，10 个 STUDENT→prio **2**
  ⇒ **FREE 的号优先级反而高于全部付费号**，比前一次更危险。

🔴 **「额度复活」≠「kiro.rs 会自动重新启用被 402 禁用的号」（10-13 源码精读，skill `kiro-rs-src` §4）。**
本节说的复活是 **AWS 侧**额度归零（`nextDateReset`）；kiro.rs **没有任何日期感知的自动复活逻辑**
（grep `QuotaExceeded` 全仓只有枚举/禁用/显示三处）。`disabled_reason=QuotaExceeded` 的号要重新可用，
靠 admin `POST /credentials/{id}/reset`（`InvalidConfig` 除外）或重建该凭据。
且 402 禁用是**内存态**：`kiro_stats.json` 不存 disabled，但要小心池里任何一次 token 刷新成功
都会触发 `persist_credentials` 把当时的 disabled 顺手写进 credentials.json（约 1h 内）⇒
此后重启也不清。「remaining=0 但 catalog ok」的号（本节主场景）**没有被 402 禁用**，
重置日后无需任何操作自然恢复 —— 两种「耗尽」别混。

而这期间**我没执行过任何 `priority` 命令** —— 见
[[feedback_kiro_pool_priority_and_disabled_change_without_us]]（`disabled` 也会自己变回 False）。
⇒ **先 `show --with-quota` 现读「档位 + disabled + prio」三列**，再决定给谁降权；
照着旧快照下 `priority` 命令可能打错对象。处置方式：`priority <id> 9` 降权或 `remove <id>`。

⚠️ **掉成 FREE 的号 `currentUsage` 可以远超 cap 且 kiro.rs 不一定禁用它**：
10-01 id13 是 `494.93/50.0`（超 10 倍）却仍 `disabled=False`，能被 `min_by_key` 选中去服务；
而 id11 同样超额（723.63/50）已被禁用。**两个号同样超额、禁用状态不同，为什么没有数据，不下归因。**
⇒ 判「这个号会不会被选去服务」只认 `show` 的 `disabled` 列现读，
别从「它超额了所以一定被禁」推 —— 那条推理 10-01 就是错的。

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
现取车道名，但 **litellm-proxy 挂的是派生 CM**（`subPath`，不热更）——
名字现读别按形状猜，10-04 起是 `litellm-product-proxy-config-<hash>`（见 §D0 那张表）。
09-29 实测两者**已漂 161 行**（09-25/09-26 那批 prometheus 指标配置只进了派生 CM），
车道名也差一条 —— 刚加完 opus-5-5，门禁却报「18 条 ≠ 预期 19」。
⇒ 现在先读 `deploy/litellm-proxy` 的 `volumes[0].configMap.name`，再从**那个** CM 取车道名。

⚠️ 这不只是 kiro 的坑：**任何"从 `cm/litellm-config` 读生产状态"的尺子都是错的**。
那份 CM 是下一次派生的输入，不是在跑的配置；两者漂了也不会报错。

🔴 **10-09 修掉第三个同族缺陷：`paste -sd'|'` 在 macOS 上把 DEAD 正则拼成空串。**
完整因果链（**只在真有道打挂那一轮触发** ⇒ 平时全绿看不出来，而它恰好在最需要这把
尺子的那轮把尺子弄坏）：

```
paste -sd'|'            ← BSD(macOS)：-d 吃掉分隔符后没有文件参数 ⇒ 报 usage:
  ⇒ DEAD_RE 是空串
  ⇒ grep -vE "" "$CUR"  ← 空正则匹配每一行，-v 于是滤光**全部 20 行**
  ⇒ $CUR 变 0 字节
  ⇒ 报「打通的道有 19 条，却只解析出 0 条 in_tok ⇒ 这把尺子自己坏了」
```

症状又一次把人带去查解析器，真因是一个 `paste` 参数。两处都修了：
加显式 `-` 读 stdin（GNU 下行为不变）、并把那行的 `|| true` 去掉改成空正则就 `exit 1`
（**原来失败被咽掉，所以才表现成「尺子坏了」而不是「脚本报错」**）。
⚠️ 第 79 行那个 `paste -sd,` 在 **ssh 字符串里、跑在 198 的 GNU 环境**，是合法的，别一起改。
门禁 `backend/tests/test_kiro_lane_regress_dead_filter.py` 7 条
（含一条阴性对照锁住「空正则 + `grep -v` = 全滤光」这个前提本身）。

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
| 上一版 kiro.rs | `set image deploy/kiro-rs kiro-rs=docker.io/library/kiro-rs:<tag> seed-config=同`。10-15 往回的链（都在 242 本地，每次切换前的 deploy 备份在 198 `~/kiro-rs-deploy.pre-<新tag>.json`）：<br>`v2026.10.1-carher-sort2`（现役）→ `v2026.10.1-carher-kimi` → `v2026.10.1-carher` → `master-ui-sort` → `master-overage-fix` → `master-websearch-loop` → `master-sonnet55` → `master-opus55` → `master-kiroalias` → `master-multimodel` → `master-5ca5703a` |
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

`project_kiro_rs_upgrade_v2026_10_1_2026_10_15` ·
`project_198_kiro_rs_deployed_242_2026_09_11` ·
`feedback_kiro_credit_is_not_one_times_multiplier_and_metering_lags` ·
`feedback_litellm_openai_model_name_collision_hijacks_provider` ·
`feedback_allowlist_name_without_alias_is_400`
