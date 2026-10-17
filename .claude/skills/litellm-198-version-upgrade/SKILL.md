---
name: litellm-198-version-upgrade
description: >-
  198 LiteLLM（ns litellm-product）整版本升级的总 runbook：看新版变更 → 灰度 → 收敛转正 → 转正后换 callback。
  串起现成工具（gray-rollout 状态机 / prepare-values / 删除集+形状门禁 / worker-kill 分诊 / callbacks-drift /
  lane-callback-swap），并把 2026-10-02~04 v1.100.1→v1.103.2 升级里踩过的每个坑写成检查点。
  Use when 用户说「升级 litellm / 看看新版本有什么变更 / 新版本能不能上 / 灰度新版本 / 把新版设为正式 /
  旧版缩容 / 线上 callback 要换 / 三条车道补丁不一致」。
---

# 198 LiteLLM 版本升级

**一句话**：先看清新版改了什么 → 用现成状态机灰度 → §7 原地升级 `litellm-proxy` 转正 → 之后换 callback 一律走
`lane-callback-swap.sh`。每一步都有门禁，门禁红了先查门禁说的是不是真的，不许绕过。

相关 skill（细节在那边，这里不重复）：
[[litellm-gray-key-rollout]] 选人/切 key · [[litellm-198-worker-kill-502]] worker 被心跳杀 ·
`litellm-gray-rollout/docs/litellm-198-gray-operator-manual.md`（§5.5 检验项 R1~R10、§7 收敛、§7.0 后台任务交接）

## 0. 看新版本（只读，不碰集群）

```bash
# release 原文（WebFetch 会被小模型摘要，关键条目必须回原文核）
curl -s https://api.github.com/repos/BerriAI/litellm/releases/tags/vX.Y.Z | python3 -c "import json,sys;print(json.load(sys.stdin)['body'])" > /tmp/rel.md
# 同时查 PyPI 是否 yanked、同日有没有补丁版（10-03 1.104.0 同日还发了 1.103.3）
# migration 差集：对比两个版本 wheel 依赖的 litellm-proxy-extras 里 migrations/ 目录
```

必查清单（v1.104.0 那次就是按这张表查出两条要改我们补丁的）：

| 查什么 | 为什么 |
|---|---|
| migration 差集，每条 SQL 是不是 `ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` | 带 `CREATE INDEX`（非 CONCURRENTLY）碰 `LiteLLM_SpendLogs`（265GB）= 锁写；1.103 就是这样，官方到 1.103.3 才承认 |
| 我们 callbacks monkey-patch 的符号在新版还在不在 | 在 ≠ 还在调用链上（见 §5 第 3 条） |
| 错误码 / 默认值变化（例：1.104 `BudgetExceededError` 429→422） | `budget_notice.py`/`error_sanitize.py` 按 429 设计 |
| 启动拒绝条件（例：1.104 master key 为空/`sk-1234` 拒启动） | 在 pod 内只回布尔核一下，不打印 key |
| 「配置开关在旧版到底生没生效」 | `log_raw_request_response` 在 1.100.1 是空操作、1.103 生效后把 worker 卡死 |
| 鉴权语义变化（例：1.104 #43049 key aliases 收窄白名单） | 198 的 key 是「models 写对外名 + aliases 指内部组」，1.105.1 上 1323/2116 把会 403；只有**克隆真 key** 并排回归抓得到，master key 测不出 |
| 稳定分支的修复是否进了新大版本（1.103.3 的 #44341 不在 1.105.1） | 不在就回移成烘焙补丁，带阴性对照（旧版复现 bug）+ 阳性对照 |

烘焙补丁配方按版本放 `litellm-gray-rollout/patches/v<ver>/`（Dockerfile 每个补丁一个 grep 门禁 + md5 join 只许补丁文件不同）。
1.105.1 那套：capacity / sse-lifecycle（重定锚点）/ stream-alias-pricing（#44341）/ key-alias-auth-compat（#43049），见该目录 README。

## 1. 灰度（现成状态机，不重造）

顺序：`gray-run-init.sh` → `gray-workload-pin.sh` → `gray_entry` → `gray-phase.sh set normal_gray` →
指名试点（`gray-key-route.sh force-gray`）→ `gray-split-update.sh 1/5/10/50/100`。
每档前：`check-monitor-continuity.py` 出 continuity 证据；≥50% 再要 `check-split-capacity.py` + guarded-old 全容量 Ready。

🔴 已踩过、必须提前处理的：

- **上一个 run 的心跳账本**：`monitor-heartbeat.jsonl` 是 append-only，新 run 第一次 continuity 必报
  `FOREIGN_RUN_ID`。持 `.monitor-cycle.lock` 备份后只留本 run 的行。
- **monitor loop 截日志**：日志 > 40MB 时 `tail -c` 切在行中间，每轮 FAIL（已修 312b0a6）。
- **split > 0 后 monitor 必须配 spend 探针 key**（只开一个便宜模型、小预算、短有效期；用完删 key、shred 文件）。
  🔴 **启用前先手跑一次 `collect-spend-reconciliation.py --probes 3` 看到 `terminal=3 http=[200]`**：10-17 探针模型
  上游 402（OpenRouter 余额），1% 档第一个周期就硬触发全局回退（spend 触发不走 sustain）。
- **新 run 的 gray 冻结输入以 live prod 为底**（config/callbacks 从 live prod CM 取，`--deployment` 用上一轮冻结时那份
  Deployment 再补上带外加的 callback mount）。10-17 第一版取了 live gray 的 config，少 3 个模型，并排回归才发现。
- **nginx base template 用上一轮模板 + live 带外块拼出来，要求「重渲染 = live 逐字节」**才能用（10-16 live 多了
  `/ccdl/`、`/muse-media/` 等 30 行，`build-base-template.py` 对已渲染的 live 拒绝）。
- 每个 run 的 baseline 绑 run_id，开新 run 要重做；夜间流量薄时用 `--baseline-window-minutes 360 --baseline-exempt-class image`。
- **SRC 目录脚本 775 会被 monitor-cycle 拒**（owner/mode unsafe）⇒ `chmod go-w`。
- **试点要挑重度用户**（近 24h 单次输入最大），不要按编号取——编号 1/2 是 buyitian，按编号必中。
- **要放到最后的 key** 用 `protect-prod`，否则放比例时会被哈希分到新版。
- **试点失败率不能直接比**：10 把 key 时一把 key 的上游 403 就能拉到 20%。按 key×model 拆，对比旧版同 key 同 model 的历史。
- **split 是按 key 哈希，不是按请求**：凌晨 5%/10% 档新版 25 分钟只有十几个请求，监控只报「样本不足」是正常的。

### 1.1 操作层脚本 `litellm-gray-rollout/scripts/run/`（10-17 起入库）

1.105.1 那轮在 198 现写的 8 个脚本已整理入库（参数化了 run 目录 / 版本 / 签字人）：
`freeze.sh`（冻结三车道 values）· `sign.sh`（签 gate，签字人从 `$D/signer.env` 读）· `step.sh`（一档：驻留 → 判据 → ≥50 跑容量）·
`ramp.sh` · `chain.sh`（在 198 上 `nohup setsid` 跑，SSH 断了不影响）· `converge.sh` / `commit.sh`（§7）· `obs.sh` ·
`protect-noisy-keys.sh`（新 run 放比例前把 stability-probe / carher-hangzhou 钉旧版）。
必需 env：`GRAY_PER_CONTAINER_CONCURRENCY`（10-17 实测生产单 pod 30~49 并发 / ~1 核，用 50；默认 8 会误判容量不足）。
静态门禁 `tests/test_run_scripts.py` 钉住了 10-17 的四个坑（split 没变不报错、读到上轮 capacity 证据、R10 算进退出中的 pod、自动回退后链还在跑）。

## 2. 每档的判定（§5.5 R1~R10）

最少：worker 被杀 = 0（`scripts/litellm-198-worker-kill-triage.py --lane gray --since 30m --probe`，卡顿 < 5s）；
新旧并排回归一致（24 组×3 协议 / 三类 key / 预算软拦截 / kiro 19 / her 逐 pod / codex 压缩）；
有效 router 设置三车道同 hash（10-17 = 129/5/24）。**单边绿不算过**，一律新旧并排；单边红先重跑 3 次再判。
🔴 **加一项：真 key 形状鉴权并排**——从 VerificationToken 取全部不同的 (models, aliases) 形状，各克隆一把短命 key，
对白名单里每个名字 + 一个白名单外名字，prod / gray 各打一发，比「403 与否」（10-17：33 形状 × 600 名，0 单边才放）。
triage 脚本走密码；198 现行 ssh key + `sudo -n`，从本机跑时用 `.work/litellm-upgrade-1016/tools/triage_keyauth.py` 包一层。

## 3. 收敛转正（§7，原地升级 `litellm-proxy`，不搞车道轮换）

1. 四份 convergence 证据 → `gray-convergence-prepare.sh`（整个 /pro/ 切到 gray）→ 2 分钟后确认 30402 = 0 → `prod_zero` → `prod_offline_upgrading`
2. **冻结 prod values**：`--deployment` 用**升级前**的 live Deployment；快照目录的 3 个整文件覆盖件必须是
   **新版本那一套**（`overlays-<ver>/`，= gray），不能从 live prod CM 取（那是旧版的）。
3. 门禁：`check-release-deletion-set.py`（不许删 ClusterIP `litellm-proxy`）+ `check-pod-spec-shape.py`（只批准 image / config）
4. 备份 live 对象 → `helm upgrade litellm-product-proxy` → rollout
5. **验收「转正字节 = 灰度字节」**（R10）：逐 pod 对 3 个覆盖件 + `register_pricing.py` 取 sha，和 gray pod 比。
   rollout 绿 / health 200 / 回归全绿都看不出覆盖件版本错了（rev9 就是这样）。
6. pin prod release → `prod_verified` → `gray-convergence-commit.sh`
7. 收敛后：🔴 **guarded-old 要换成「上一版 + 升级前生产 config」**（10-17 发现它还停在两代前的 1.100.1、少 3 个模型、
   没验过迁移后的库能读写 ⇒ 保险丝是坏的）。做法：`go-inputs/snap` 取升级前 live prod 的 CM，`freeze.sh guarded-old` 用上一版 digest，
   上线后并排回归。clone 兼容腿的「old」就是上一版，迁移后库的读写已验。gray 缩 2 副本热备。

chart 必须能渲染出 live 的样子（8e14e5f 补的四处，任一缺都会让 rollout 全绿、生产断）：
ClusterIP `litellm-proxy`、路由标签值 `enabled`、Service 按路由标签选 pod、`topologySpreadConstraints`。

## 4. 转正后换 callback（三条车道都是 helm 冻结出来的）

```bash
# 先对账：git ↔ 三条车道
python3 scripts/litellm-198-callbacks-drift.py            # 退出码 1 = 有漂移；方向看 --diff 再定
# 换（198 上 root；FILES_DIR 里放 git 那份）
$SRC/scripts/lane-callback-swap.sh --lane gray        --run-dir $D --files-dir $D/swsrc <file>...
$SRC/scripts/lane-callback-swap.sh --lane guarded-old --run-dir $D --files-dir $D/swsrc <file>...
$SRC/scripts/lane-callback-swap.sh --lane prod        --run-dir $D --files-dir $D/swsrc <file>...
```

顺序：**零流量的 gray 先上、实打验证，再 guarded-old，再 prod**；三条都换完再跑一次 drift。
2026-10-04 基线：三车道各 34 个与 git 逐字节一致；drift 仍报的 2 个「只有 repo 有」是**预期的**——
`streaming_output_backfill_logonly.py`（阿里云专用）、`inject_prompt_cache_key.py`（198 未启用），别装上去。
脚本只许「callbacks CM 换代 + 点名的 key 变」，冻结后断言 values 里的 sha 已变（防止 rev10 那种静默空操作），
最后逐 pod 看 sha 和 ImportError。

### 4.1 新增 callback（还没挂载的）：`scripts/litellm-198-lane-callback-add.py`

`lane-callback-swap.sh` 只换已有 key；`litellm-callback-install.py` 是 helm 化之前的老路（写可变 CM，别用）。
新增走这个脚本（198 root，以 **live** 为底、只做加法、每步断言）：

```bash
W=/root/cb-add-work   # 放脚本 + 要装的 .py（逐字节 = git）
python3 $W/litellm-198-lane-callback-add.py install  litellm-proxy-gray $W/<mod>.py <mod>.<instance>   # 默认插在 responses_aclose 行后
python3 $W/litellm-198-lane-callback-add.py verify   litellm-proxy-gray <mod>.py <日志tag>              # 逐 pod: sha / installed≥1 / 拒装=0 / ImportError=0
python3 $W/litellm-198-lane-callback-add.py rollback litellm-proxy-gray /root/cb-add-backup/<deploy>.<ts>.json
```

顺序同上：gray（零流量）→ 实打验证 → prod → guarded-old。

🔴 **它是 helm 带外改动**：装完以后 helm 冻结快照落后于 live。`lane-callback-swap.sh` 已加守卫
（live Deployment 引用的 CM 名 ≠ helm manifest ⇒ STOP），**下一次 helm upgrade 之前必须先把 live 的
callbacks / config 回填进快照目录和 `--deployment` 源**，否则会静默回退。
2026-10-10 现状：三车道都带外加了 `responses_tool_args_done_backfill.py`；prod callbacks 另有当天
手工换的 `deepseek_responses_adapt.py`（e291064）。

🔴 漂移两个方向都踩过：
- **git 新于线上**（修复入库没上线）：`sitecustomize` `/app` 路径、`pricing_overlay` 出图价、`chatgpt_responses_normalize`
- **线上独有**（直接改线上没回填）：10-03 一次入库 14 个。动之前先把线上版原样入库作基线，再改。

## 5. 排查原则（这轮每一条都差点归因错）

1. **收敛后才出现的单 key 报错 ≠ 升级引起**：先查那个 key 当时做了什么（压缩 / 换模型），再新旧版单变量重放。
   huangminqi 的 `invalid_encrypted_content` 是我们自己 compaction 写明文 + 名单漏 gpt-6，新旧版同形。
2. **本地注释说的「实测」也要复测**：bridge 注释称「patch `__anext__` 零命中」，探针 17 次命中，说法不成立。
3. **被 patch 的函数还在不在调用链上**，要用探针证明，不能读码推：`deepseek_id_prefix` 的 SSE patch 函数在
   /v1/responses 上根本不被调用，但真正的病是上游 deepseek 对 custom `apply_patch` 回 function_call。
4. **大表查询必须先加 `set statement_timeout`**，并且按单个 key / 窄时间窗查；10 天全表扫在生产库上跑了 15 分钟。
5. **pod 日志会被冲掉**：跑了两天的 pod 里 grep 不到启动行 ≠ 没启动那个功能；判额度重置看 `budget_reset_at`。
6. **「客户端升级后才报错」先查上游行为有没有变，再查客户端**（10-09 her-13 并行工具空参数）：
   升级前后 SpendLogs 里客户端回传的 `messages[].tool_calls[].arguments` 就是它当时**实际收到**的东西，
   按 call_id（去掉 `_` 再比）对上游 `response.output[]` 逐个配对，一次查询判清「以前就空被吞」还是「以前就是满的」。
   那次是后者：真因是 ChatGPT 上游对并行调用**随机**不发 delta，与 her 升级同期，纯属巧合。

## 6. 回退

- 灰度期：`gray-split-update.sh 0` + 试点逐把 `force-prod`（降级永远不设门禁）
- committed 后：`gray-post-commit-rollback.sh prepare → prod-restored → finish`（guarded-old 必须 Ready）
- 单个 callback：把 `swap-<ts>/*.before` 放回 FILES_DIR 再跑 `lane-callback-swap.sh`，或 `helm rollback` 上一 revision
