---
name: litellm-198-version-upgrade
description: >-
  198 LiteLLM（ns litellm-product）整版本升级的总 runbook：看新版变更 → 烘焙补丁 → DB 迁移 → 灰度（驾驶台或命令行）→
  收敛转正 → 修保险丝 → 转正后换 callback。串起现成工具（gray-rollout 状态机 / scripts/run/ 操作层 / 升级驾驶台 /
  8 条并排回归（含主组全挂兜底契约）/ prepare-values / 删除集+形状门禁 / worker-kill 分诊 / lane-callback-swap），每一步都写了
  10-02 v1.103.2、10-17 v1.105.1 两轮踩过的坑。
  Use when 用户说「升级 litellm / 看看新版本有什么变更 / 新版本能不能上 / 灰度新版本 / 把新版设为正式 /
  旧版缩容 / 驾驶台 / 回归一下 / 用例回归 / 新旧并排对比 / 线上 callback 要换 / 三条车道补丁不一致」。
---

# 198 LiteLLM 版本升级

**一句话**：看清新版改了什么 → 补丁按版本烘进镜像 → clone 上验过迁移再迁生产 → 灰度放量（驾驶台自动开顺路）→
§7 原地升级 `litellm-proxy` → 保险丝换成上一版。每一步都有门禁，门禁红了先查门禁说的是不是真的，不许绕过。

相关：[[litellm-198-patch-layer-map]] 升级前先现量线上四层补丁面 · [[cli-anything-litellm]] `src find --tag <新版>` 对比上游函数、`patches compare` 看三车道 · [[litellm-gray-key-rollout]] 选人/切 key · [[litellm-198-worker-kill-502]] worker 被心跳杀 ·
[[litellm-198-key-allowlist]] 改 key 白名单/别名 · 手册 `litellm-gray-rollout/docs/litellm-198-gray-operator-manual.md`
（§5.5 R1~R10、§7 收敛、§7.0 后台任务交接）

## 现场速查

| 东西 | 在哪 |
|---|---|
| 生产 / gray / 保险丝 | `litellm-proxy`（30402）/ `litellm-proxy-gray`（30405）/ `litellm-proxy-guarded-old`（30406） |
| 升级驾驶台 | **http://10.68.13.198:8795**（内网直连，源白名单 10.68.30/24 + 10.68.13/24；写操作口令 `sudo cat /root/l3/.console-token`） |
| 本轮 run 目录 | `cat /root/l3/.gray-run-latest`（`$D`，root 0700，含 target.env / nginx/frozen-env.sh） |
| 补丁配方 | `litellm-gray-rollout/patches/v<ver>/`（Dockerfile + 烘焙脚本 + overlays/ + README） |
| LiteLLM 源码 | 198 `/root/litellm-fork`：`git show <tag>:<path>`，**不 checkout**；fetch 走 `https://www.github.com/BerriAI/litellm.git` |
| 198 登录 | `ssh -o BatchMode=yes cltx@10.68.13.198 'sudo -n bash -s'`（ssh key，不用密码） |

## 0. 看新版本（只读）

release 原文用 `curl api.github.com/.../releases/tags/vX.Y.Z` 取（WebFetch 会被摘要）；同时查 PyPI yanked、
同日补丁版；migration 差集对比两个 tag 的 `litellm-proxy-extras/.../migrations/`。

| 必查 | 为什么（踩过的） |
|---|---|
| migration 每条是不是纯加法；有没有非 CONCURRENTLY 的 `CREATE INDEX` 碰 SpendLogs | 1.103 锁写；1.105 的 `rename_lens` 只碰 0 行新表 |
| 我们 monkey-patch 的符号还在不在、**还在不在调用链上** | 1.105 `experimental_pass_through`→`pass_through` 改名，两个 callback 静默 ImportError skip |
| 错误码 / 默认值（1.104 超预算 429→422） | config 写 `litellm_settings.budget_exceeded_status_code: 429`（`budget_notice`/`error_sanitize` 按 429 设计） |
| 启动拒绝条件（1.104 master key 空 / `sk-1234` 拒启动） | pod 内只回布尔核，不打印 key |
| 配置开关旧版到底生没生效 | `log_raw_request_response` 1.100 空操作、1.103 生效后卡死 worker |
| **鉴权语义**（1.104 #43049 key aliases 收窄白名单） | 1323/2116 把 key 会对白名单内名字 403；只有「真 key 形状鉴权并排」抓得到 |
| 稳定分支的修复进没进新大版本（1.103.3 #44341 不在 1.105.1） | 不在就回移成烘焙补丁，带阴性（旧版复现）+ 阳性对照 |
| 老补丁的指纹在新版还命中吗 | `midstream_fallback_loop` 指纹在 1.105 仍命中 ⇒ 会覆盖上游新多跳 ⇒ 必须下线 |
| 🔴 **下线任何补丁前：它提供的每一条行为，新版谁来提供** | 见下方「下线补丁 = 删行为」。10-17 下线 `midstream_fallback_loop` 只核了「上游有多跳」，它另外两条行为（同组换号 ≤3、总预算 10）没人接 ⇒ 10-20 过载风暴全打穿 |

### 下线补丁 = 删行为（10-20 复盘）

**发生了什么**：1.105.1 升级时 `midstream_fallback_loop` 下线（理由成立：指纹仍命中、会盖掉上游新多跳）。
但这个文件不只修了「中流单发」一件事，09-17 / 09-28 又往里加了**同组换号上限 3** 和**总预算 10**，用户拍过板。
1.105.1 上游的 `_maybe_run_weighted_failover` 没有同组上限，且同组换号和跨组兜底共用 `max_fallbacks`（默认 5）
⇒ 16 腿组过载时 5 次全花在换号上，跨组一跳都不走。转正后 3 天第一次 ChatGPT 大面积过载，群里一片「rate limit exceeded」。

**为什么没被拦住**（三层，缺一层都会漏）：
1. **判据只看了补丁的主要目的**：README 写「上游已有多跳 midstream（#42283/#43133/#43238）」——对，但只覆盖了文件头写的那条。
   后加的行为写在常量旁的注释里，没进任何清单。
2. **回归全是健康流量**：7 条用例都在问「请求能不能通」，没有一条逼出「主组整组挂掉」。行为丢了，7/7 照样绿。
3. **漏掉的配置只在故障时才有人用**：09-28 量过，平时 25 分钟 36 次换号里 32 次第 1 发就成、4 次到第 2 发、第 3 发及以上 0 次。
   日常指标、灰度 5xx 对比都看不出来，只有上游大面积出问题时才暴露。

**以后怎么做**：
- 下线补丁前，把它**全文**（含常量注释、`[日期]` 段）列成「行为清单」，每条写「新版谁提供 + 怎么验」，
  写不出来的就不许下线，或者先做好替代件再下线。清单写进 `patches/v<ver>/README.md`。
- 这类「只在故障时生效」的行为必须有**故障注入式回归**：§4 新增「主组全挂兜底契约」就是这一条。
- 恢复件：`k8s/litellm-callbacks/same_group_failover_cap.py` + env `ROUTER_MAX_FALLBACKS=10`（两样都要，单抬上限没用：同族组也过载，次数照样被换号吃满）。
| 🔴 文档里的开关在**当前 tag 源码**里有没有 | `git -C /root/litellm-fork grep -c <name> v<当前版本> -- litellm` ——10-20 查到 4 个只在文档里（`reasoning_effort_rounding` 1.108+、`fallback_on_unsupported_params` 1.107…），照文档配不生效 |
| live callbacks 引用的上游符号 | `scripts/litellm-callback-upstream-audit.py --old <tag> --new <tag> --cb-dir <live CM 落盘>`（198 root）：GONE = 会静默 skip，CHANGED = 被包装的函数体变了，要回归 |
| 烘焙补丁 / 覆盖件能不能打到新源码 | 补丁脚本直接对 `git archive <tag>` 的树跑；覆盖件 `patch --dry-run`（1.106 passthrough 那份因为上游改名失败） |

## 1. 镜像与补丁

- base 先镜像进本地 registry：`ghcr.io/berriai/litellm@<amd64 digest>` → `127.0.0.1:5000/litellm-carher:vanilla-v<ver>`，push 回来 digest 必须一致。
- 烘焙补丁全是**就地锚点替换**（不整文件 COPY，否则静默回退上游改动）；锚点次数 ≠ 期望 ⇒ 整体不写、非零退出；
  Dockerfile 每个补丁一个 grep 门禁。验收：两镜像 `find /app -type f | md5sum` join，只许补丁文件不同、零增删
  （文件名带空格的 migration 目录会被 join 误判，用整行 diff）。
- 整文件覆盖件（core / passthrough streaming_handler、anthropic logging handler）**每个版本按新源重做**到 `overlays/`。
- 行为探针在 `--network none` 容器里对比 旧 / vanilla 新 / 补丁新 三个镜像（SSE 生命周期、别名计价都这么验）。
- callback 套件在新旧两个镜像里逐文件跑 pytest（本机缺依赖；wheel 下载后解压进 PYTHONPATH），**两边通过数和失败集合要一样**。

## 2. DB（clone 先过、生产后迁）

`$D/dumpprod.sh`（排除两张日志表数据）→ clone dump → restore A → migrate A → post-dump → restore B/C →
兼容四腿（new/old/concurrent，**runner CM 要用带 `BARRIER_ACK_KEYS` 的那份**，旧的会复现竞态）→ 生产 migrate Job（`lock_timeout=5s`）→
比 relfilenode（零重写）和列集合（零丢失）。🔴 `kubectl exec -i` 往 pod 灌 80MB 会卡死 apiserver websocket ⇒ 校验 dump 用 `kubectl cp`。

## 3. 灰度

### 3.1 冻结 gray（`scripts/run/freeze.sh`）

- 🔴 **以 live prod 为底**：config / callbacks 取 live prod 的 CM（不是 live gray，10-17 取错少 3 个模型）。
- `--deployment` 用上一轮冻结时那份 Deployment 形状（卷名对得上），再补上带外加的 callback mount、去掉要下线的。
- 冻结后逐项和 live prod 比：只许差计划内的改动（+ gray 自带的 `disable_reset_budget`）。
- nginx base template = 上一轮模板 + live 带外块，要求**重渲染 = live 逐字节**才用。

### 3.2 放量：驾驶台 或 命令行，二选一，同一套脚本

**驾驶台**（`litellm-gray-rollout/console/`，见其 README）：机器在下面全满足时自己升一档，否则停下出决策卡等人：
驻留够（1/5/10 档 25 分钟、50/100 档 30 分钟）、本档 ≥5 个监控周期全过、新版 5xx ≤ 旧版 + 1pp（样本 <50 不判）、
worker 0 被杀、重启不变、监控循环在跑、巡检号已钉旧版；≥50 另要本 run 本 generation 的容量 PASS + 保险丝 Ready。
**开始灰度、换生产、切回生产、回退、关 run 永远要人点**；没人回答 = 保持现状；同一档自动升档失败不重试。
人在终端动手时 `flock $D/.console.lock -c '<命令>'`，驾驶台自动进「人工接管」只读。

**命令行**：`scripts/run/chain.sh`（`nohup setsid` 在 198 上跑，SSH 断了不影响）= 逐档 `step.sh <next> <驻留分钟>`。

不管哪种，开局都要：
1. `gray-run-init` → `gray-workload-pin`（shape evidence 要逐条批准）→ `sign.sh gray_entry` → `gray-phase.sh set normal_gray`
2. 🔴 **`scripts/run/protect-noisy-keys.sh` + Mac 上那条杭州管道**：`stability-probe`、`carher-hangzhou` 钉旧版
   （它们扫坏组，新旧版同样 30~40% 失败；不钉 ⇒ 50% 档 USER_FAILURE_RATE 必误回退，10-17 run b）
3. buyitian 两把 `protect-prod`（放最后）；试点 10 把 = 近 24h 单次输入最大的在职用户
4. monitor loop：baseline 每个 run 重做（夜里 `--baseline-window-minutes 360 --baseline-exempt-class image`）；
   🔴 **spend 探针模型先手跑 `collect-spend-reconciliation.py --probes 3` 见 `terminal=3 http=[200]`**（10-17 openrouter 402 ⇒ 1% 档首周期自动回退）
5. `GRAY_PER_CONTAINER_CONCURRENCY=50`（10-17 实测单 pod 30~49 并发 / ~1 核；默认 8 会误判容量不足）

其它坑：心跳账本 append-only ⇒ 新 run 先持锁只留本 run 行；SRC 目录 `chown root` + `chmod go-w`；
split 按 key 哈希（凌晨 1% 档新版可能只有个位数请求）；试点失败率要按 key×model 拆、对旧版同 key 同 model。

## 4. 回归（每档判定 + 转正前 + 转正后）：8 条用例，一律新旧并排

驾驶台「用例回归」区一键跑（选 A/B 车道，要口令 + 二次确认，约 35 分钟）；结果三态：
**通过** / **同形**（两边一样不过 = 上游或用例问题，不是版本问题）/ **不过**（单边或两边不一样 ⇒ 停）。

| 用例 | 脚本 | 过的判据 | 为什么 |
|---|---|---|---|
| 流量前 24 组 × 3 协议 | `scripts/litellm-198-lane-sweep.py` | 0 单边；阴性对照两边 4xx | 基线集 |
| 真 key 行为 | `scripts/litellm-198-key-behavior-regress.py --clone-from` | 新/老名 200+nonce，白名单外 403 | master key 不过白名单闸门 |
| 真 key 形状鉴权并排 | `scripts/litellm-198-auth-parity.py` | 0 单边 | 只有它抓到 #43049 |
| kiro 全车道 | `scripts/litellm-lane-bench.py` | 全成功、in_tok 分层一致 | 跨族塌陷 = 静默降级 |
| her 逐 pod | `scripts/litellm-198-her-name-probe.py --clone-key` | 每 pod 每名 OK、阴性被拒 | 每个 pod 各自缓存 key |
| 预算软拦截 | `scripts/litellm-198-budget-notice-smoke.sh`（`BASE_OVERRIDE`） | 8/8 | 依赖 429；10-14 起 cursor 走高配桶 |
| codex 压缩 | `scripts/codex-compaction-regression.py` | compaction 格两边恒 1 | 普通格 reasoning 有无是随机的，不算 |
| **主组全挂兜底契约** | `scripts/litellm-198-failover-contract-check.py [--lane X] [--negative]` | 每车道 `OK`：成功、主组 ≤4 次、7 跳链每跳恰 1 次 | 10-20 下线补丁丢了同组上限，健康流量回归全绿 |

**兜底契约怎么量**：在车道 pod 里按该车道 `/app/config.yaml` 的 callbacks 顺序 import 全部补丁 + 用 pod 自己的 env
建真 `Router`，假上游（不出网）让主组 16 腿开流即过载，走真 `aresponses(stream=True)`；同 pod 再用 `python3 -S`（vanilla）跑一遍。
它量的是**行为**，不管谁提供 ⇒ 下线补丁、helm 冲掉带外补丁、env 被改都会红。10-20 读数：
prod/gray/guarded-old 全 `主组=4 链=1/1/1/1/1/1`；`--negative` 现场复现事故配置（关 cap + 上限 5）= `主组=6 链=0/0/0/0/0/0` 抛错，
它要是也绿就 exit 2（夹具量不出病）；没装 sgfc 的车道（guarded-old）跳过这一项。🔴 事故配置的 env 必须在**进程启动时**给：
sitecustomize 在脚本第一行前就 import 了 litellm，`ROUTER_MAX_FALLBACKS` 是 import 时读成常量的（第一版在脚本里改 env ⇒ 假绿）。
vanilla 也绿时打 `NOTE`（上游自带了 ⇒ 补丁可评估下线），不算红。🔴 假流对象要带 `_hidden_params`，
否则 1.103.2 会多包一层、`isinstance` 失败、中流兜底不接 ⇒ 夹具自己的假红（第一版就是这样把 guarded-old 量红的）。

另外每档：worker 被杀 = 0 + 卡顿探针（本机跑 `scripts/litellm-198-worker-kill-triage-keyauth.py --lane all --since 30m --probe`）；
有效 router 设置（fallbacks/cwf/alias 条数）三车道同 hash。

## 5. 收敛转正（§7，`scripts/run/converge.sh` → 回归 → `commit.sh`）

1. 四份 convergence 证据 → prepare（/pro/ 全到 gray）→ 100 秒后 30402 = 0 → `prod_zero` → `prod_offline_upgrading`
2. prod values 用和 gray **同一套快照**（只差 `disable_reset_budget`）；`--deployment` 用升级前的 live Deployment；
   整文件覆盖件必须是**新版那一套**（= gray），不能从 live prod CM 取（那是旧版的，rev9 就这么错过）。
   `check-release-deletion-set.py`（不许删 ClusterIP `litellm-proxy`）+ `check-pod-spec-shape.py` 逐条批准
   （live prod 上「只声明未挂载」的旧卷可以删，先确认 `mounted=False`）
3. 备份 → `helm upgrade` → rollout → **R10 逐 pod 6 个文件 sha = gray**（只看非 terminating 的 pod，旧 pod 优雅退出要 10 分钟；
   rollout 绿 / health 200 / 回归全绿都看不出覆盖件版本错了）
4. pin（三个 release 一起重报）→ 直打 30402 跑回归 → `prod_verified` → commit
5. 🔴 **修保险丝**：guarded-old 换成「上一版 digest + 升级前生产 config 快照」（10-17 它还停在 1.100.1、少 3 模型、没验过新库）；
   上线后并排回归。gray 缩到 2 副本热备（只改 replicas，门禁断言只变这一项）。关 run。
6. 收尾：探针 key 删、明文 key 文件 shred、驾驶台留着。

chart 必须渲染出 live 的样子（8e14e5f 四处）：ClusterIP `litellm-proxy`、路由标签值 `enabled`、Service 按路由标签选、`topologySpreadConstraints`。

## 6. 转正后换 / 加 callback

```bash
python3 scripts/litellm-198-callbacks-drift.py            # 退出码 1 = 有漂移；方向看 --diff 再定
# 换已有（198 root；FILES_DIR 放 git 那份）——顺序 gray（零流量，实打验证）→ guarded-old → prod，完了再跑一次 drift
$SRC/scripts/lane-callback-swap.sh --lane gray --run-dir $D --files-dir $D/swsrc <file>...
# 新增还没挂载的（以 live 为底、只做加法；默认插在 responses_aclose 行后）
python3 $W/litellm-198-lane-callback-add.py install  litellm-proxy-gray $W/<mod>.py <mod>.<instance>
python3 $W/litellm-198-lane-callback-add.py verify   litellm-proxy-gray <mod>.py <日志tag>   # 逐 pod sha / installed≥1 / ImportError=0
python3 $W/litellm-198-lane-callback-add.py rollback litellm-proxy-gray /root/cb-add-backup/<deploy>.<ts>.json
```

- swap 只许「callbacks CM 换代 + 点名的 key 变」，冻结后断言 values 里 sha 已变（防 rev10 静默空操作）。
- drift 报「只有 repo 有」的 `streaming_output_backfill_logonly.py`（阿里云专用）、`inject_prompt_cache_key.py`（198 未启用）是预期的，别装。
- 🔴 add 是 helm 带外改动：swap 有守卫（live 引用的 CM 名 ≠ helm manifest ⇒ STOP），**下次 helm upgrade 前必须先把 live 回填进快照**。
  10-20 起守卫**只比真挂载的卷**：live 上「只声明未挂载」的旧卷（历次升级残留，如 prod 曾有 4 个 `litellm-stable-*`）
  逐条打印 `ignore unmounted live volume …` 后忽略，helm upgrade 会顺手去掉它们（不影响运行）。挂载卷不一致照旧 STOP。
- swap 前先 `helm history <release>` 看最新 revision 是不是自己的：同一天别的会话会用 `lane-config-tune.sh` 在同车道改配置/下线 callback（10-20 prod rev21）。
  swap 从 helm 快照起步，别人已入快照的改动不会被回退；但别拿「我上次部署的是 revN」当现状。
  `--env K=V` 可在同一个 patch 里加 env（同名已存在拒绝）；rollback 同时还原 env。
  带外装完要把 live 回填进车道快照（`$I/snap` + `--deployment` 那份 yaml），**只回填、不 helm upgrade**
  （下一次计划内的 `lane-config-tune.sh --adopt-live` / swap 顺带收编，省一次滚动）。回填只许「live − 快照 = 刚加的那条
  callbacks 条目 + 文件 + env + mount」，别的差异一律停；env 只追加新名字，**不从 live 整份抄**（gray 的
  `PROXY_BATCH_POLLING_ENABLED` 是 prepare-values 覆盖的）。验收 = `lane-config-tune.sh --adopt-live --dry-run` 门禁 PASS 且 target.yaml 里有它。
  10-20：sgfc 回填后 gray rev26、prod rev21 已收编进 helm。🔴 prod 别 `helm rollback` 到 rev20（那版没有 sgfc = 事故配置），用 tune 的 `--restore`。
  任何 helm upgrade 前后跑兜底契约检查，红了就是被冲掉了。
- `litellm-callback-install.py` 是 helm 化之前的老路（写可变 CM），别用。
- 漂移两个方向都踩过（git 新于线上 / 线上独有，10-03 一次入库 14 个）：动之前先把线上版原样入库作基线。

### 6.1 改 config 标量 / 下线 callback：`lane-config-tune.sh`（10-20 起）

```bash
# 198 root；--set 只认三段 settings 下的一行标量（JSON 值，null=删键）；--drop-callback 去列表条目 + 删快照文件 + 去 mount
flock -n $D/.console.lock bash $SRC/scripts/lane-config-tune.sh --lane gray --run-dir $D [--adopt-live] --dry-run \
  --set general_settings.user_api_key_cache_max_size=3000 --drop-callback log_truncate
# 去掉 --dry-run 才落地；回滚：同命令换成 --restore $I/tune-<ts>
```

- 门禁：config 语义 diff **恰好** = 声明项（+ 去掉的 callbacks 条目），callbacks CM 只少声明的 key，Deployment 只许卷名/checksum/被下线 mount 变。
- 一次也没声明 ⇒ 拒绝（「no-op --set 收编带外」走不通，也不是 no-op：CM 名换代 + 悬空 mount 被去掉 ⇒ 照样滚一次）。
- `--adopt-live`：live 有带外改动、且已按上面规则回填进快照时用 —— 基线换成 live 对象；live 上 subPath 不在 CM 里的悬空 mount 逐条打印忽略。
- 容器里 `/app/.venv/bin/test` 是 python 入口，会盖掉 coreutils `test` ⇒ pod 内判文件存在一律 `sh -c '[ -e … ]'`。
- 选 gray 先（零流量），回归 8 条 + 下面两把量具，再同参数上 prod；改 prod 前给同车道活跃会话发消息。
- 10-20 实例：pool 1→3（每 pod 6 连接，库 50/100）+ 鉴权缓存 3000 + 下线 log_truncate/pingfix ⇒ gray rev26 / prod rev21。

198 上的副本在 `/opt/gray-console/scripts/`，和仓库逐字节一致；改了仓库要同步过去。

| 量具 | 用途 | 判据 |
|---|---|---|
| `scripts/litellm-198-spend-row-parity.py --lane-a … --lane-b … --model …` | 每个请求都落 SpendLogs 行且 token>0（下线日志类 callback / 改连接池后必跑） | 两车道 n/n。按每车道独立短命 key 的 `api_key` 计数——`request_id` ≠ `x-litellm-call-id`，按它匹配全 0 是量具坏 |
| `scripts/litellm-198-auth-latency-ab.py --keys 1200` | N 把临时 key 打 `/v1/models`，量鉴权缓存命中前后 | key 数 > 缓存时每轮都回源（p50 不降）；装得下第 2 轮起 p50 降 |
| `scripts/litellm-198-failover-contract-check.py --local --lane X` | 主组全挂兜底契约（同组上限 + 逐跳） | 任何 helm upgrade 前后各跑一次；并发跑会撞 `/tmp/.fcc.py`，STOP 先重跑 |

🔴 回归期间**别的会话 rollout 了 B 侧车道** ⇒ B 侧 -1 / `No route to host`，单边全挂 = 量具（pod 被换掉），重跑那几条即可；
codex 压缩格假红两类已修（model_id 变 12 位 hex 哈希不再按族比；`keepalive` 心跳算噪声）。

## 7. 排查原则

1. 收敛后单 key 报错先查那个 key 当时做了什么，再**新旧版单变量重放**（huangminqi `invalid_encrypted_content` = 我们自己 compaction 名单漏 gpt-6，新旧同形）。
2. 「失败率高」先按 key 拆：10-17 两次「新版失败率高」全是巡检号 / 杭州凭据；剩下的单把 key 超额度（429 usage limit）也不是版本。
3. 本地注释说的「实测」也要复测（bridge 注释称 `__anext__` 零命中，探针 17 次）；被 patch 的函数在不在调用链上要用探针证明。
4. 「客户端升级后才报错」先查上游变没变：SpendLogs 里客户端回传的 `tool_calls[].arguments` 按 call_id 对上游 output 逐个配对（10-09 并行工具空参数 = 上游随机不发 delta，与 her 升级同期是巧合）。
5. 大表查询先 `set statement_timeout`，按 `api_key` / 窄时间窗查。
6. pod 日志会被冲掉：grep 不到启动行 ≠ 没启动；判额度重置看 `budget_reset_at`。
7. 检查工具自己也会坏：探针模型欠费、读到上一轮残留证据、默认并发值不符实测——先阳性对照工具本身。

## 8. 回退

- 灰度期：自动止损会自己 `rolled_back`；人工：驾驶台「全部退回旧版」或 `gray-global-rollback.sh`（= split 0 + 试点逐把 `force-prod`）。降级永远不设门禁。
- committed 后：`gray-post-commit-rollback.sh prepare → prod-restored → finish`（guarded-old 必须 Ready）。
- 单个 callback：`swap-<ts>/*.before` 放回再跑 `lane-callback-swap.sh`，或 `helm rollback`。
