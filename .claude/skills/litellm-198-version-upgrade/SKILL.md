---
name: litellm-198-version-upgrade
description: >-
  198 LiteLLM（ns litellm-product）整版本升级的总 runbook：看新版变更 → 烘焙补丁 → DB 迁移 → 灰度（驾驶台或命令行）→
  收敛转正 → 修保险丝 → 转正后换 callback。串起现成工具（gray-rollout 状态机 / scripts/run/ 操作层 / 升级驾驶台 /
  7 条并排回归 / prepare-values / 删除集+形状门禁 / worker-kill 分诊 / lane-callback-swap），每一步都写了
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

## 4. 回归（每档判定 + 转正前 + 转正后）：7 条用例，一律新旧并排

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
- `litellm-callback-install.py` 是 helm 化之前的老路（写可变 CM），别用。
- 漂移两个方向都踩过（git 新于线上 / 线上独有，10-03 一次入库 14 个）：动之前先把线上版原样入库作基线。

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
