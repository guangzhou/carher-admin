---
name: litellm-198-monitoring-ops
description: >-
  198 LiteLLM 的监控栈运维：Prometheus 抓取、Grafana 告警规则与看板、飞书投递、
  probe 探针，以及灰度切流前的可观测性自检（第 −1 天门禁）。真身在
  `k8s/monitoring/`（12 个文件），线上形态是 ConfigMap。Use when 用户提到
  "监控" / "告警" / "Prometheus" / "Grafana" / "告警没收到" / "告警规则" /
  "noDataState" / "看板" / "probe 探针" / "探针频率" / "探针打哪个模型" /
  "飞书告警" / "切流前先看监控" / "可观测性"，或要改
  `litellm-stability.yaml` / `prometheus.yml` / `alert2feishu.py` / `probe.py`
  （含改探针节奏 `PROBE_INTERVAL_MIN/MAX`、`PROBE_GAP_MIN/MAX` —— 它们和三道尺子
  算术耦合），或提到"探针被风控/太频繁/像扫描器"。涵盖同步回集群的正确姿势
  （`scripts/litellm-198-monitoring-sync.py`，diff-first）、生效判据、
  探针挑落点的打分规则与反指纹四件套，以及一整族「绿灯是假的」坏尺子。
---

# 198 LiteLLM 监控栈运维

**这个 skill 解决的问题**：监控的绿灯默认不可信。改监控比改业务更容易"改完看起来
对、其实一次都没评估过"，因为坏掉的量具和健康的系统长得一模一样。

先读 `k8s/monitoring/README.md`（文件↔ConfigMap 映射表在那里，本 skill 不复制）。

## 0. 三条铁律（先记这三条，其余都是推论）

1. **绿色必须先证明量具活着。** 09-13→09-19 探针停了 6 天，6 条探针告警全绿。
2. **改 CM ≠ 生效。** Grafana 文件式 provisioning 只在启动时加载；判据是容器内
   `sha256sum` + `/api/v1/provisioning/alert-rules` 读回的值，不是 CM 内容。
3. **禁 `kubectl apply`。** 一个 CM 里有多个 key，apply 会连带覆盖别的 key。
   只 patch 单个 key（README 有现成命令）。

## 1. 切流前置门禁：`observability-preflight.py`

灰度升级的**第 −1 天**跑它，不过不许开始切流。只读，不碰任何线上对象。

```bash
# 静态两项，本地就能跑（适合 CI / 提交前）
k8s/monitoring/observability-preflight.py --rules k8s/monitoring/litellm-stability.yaml --static-only

# 全量：加活性检查 + 间隔自洽（改过 PROBE_INTERVAL 后必带 --probe-script）
k8s/monitoring/observability-preflight.py \
  --rules k8s/monitoring/litellm-stability.yaml \
  --prom-config k8s/monitoring/prometheus.yml \
  --probe-script k8s/monitoring/probe.py \
  --prom-url http://10.68.13.242:30300/api/datasources/proxy/1
```

五条腿，任何一条不过 exit 1：

| 腿 | 判什么 | 为什么脚本测得/测不得 |
|---|---|---|
| **LIVE** | 每个 scrape target 最近有数据（`up==1` 且 sample 新鲜） | 自动。**对账对象是 `prometheus.yml` 里声明的 job 列表**，不是现存的 series |
| **RED-ABLE** | 每条规则能被合成红触发 | **MANUAL** —— 读配置说"看起来对"不算，得真发一发 |
| **DELIVER** | 告警真能送到人手上 | 半自动：判 `readyReplicas`，计数器要人看 |
| **FAIL-RED** | 数据断了必须变红 | 自动，走解析后的 YAML 逐条判 |
| **CADENCE** | 采样间隔与读它的窗口/阈值算术自洽 | 自动，需 `--probe-script`。见第 6 段 |

**它为什么存在**：09-13 灰度方案落地到 09-20 共 38 个提交，**11 个是在修门禁和监控
自己**（尺子坏了），不是在修被测对象。最贵的单点是 Prometheus 六天没抓过生产，
而门禁一直在读它出的数 —— 那六天每一次绿灯都是空数据渲染的，后面所有返工的根都在这。

## 2. `noDataState` / `execErrState` 怎么判（这里最容易一刀切错）

- **业务规则**（失败率、p95、fallback）→ `noDataState: OK`。
  `increase(失败数[15m])` 没数据**本就该**是"没有失败"。
- **看门狗规则**（"探针停了"、"采集不全"）→ **必须 `Alerting`**。
- `execErrState` **一律 `Error`**，不分业务/看门狗：表达式执行失败是量具故障，
  不是"业务正常"。09-19 修掉 `llm-stab-latency` 唯一一处 `OK`。

⛔ **不要把 `noDataState: Alerting` 一刀切铺满。** 09-20 实测：12 条规则一刀切
造出 **10 条假红**。正确的不变量是**覆盖**，不是一致：

> 每条 fail-open 业务规则所读的**指标族**，都必须有某个 fail-closed 看门狗在看
> 它的来源 job。无主的指标族 = 红。

现有两条看门狗：`llm-stab-scrape-down`（`sum(up{job="litellm-proxy"})`）、
`llm-probe-stale`（`time() - litellm_probe_last_round_timestamp_seconds`）。
脚本里 `FAMILY_OWNER` 就是这张归属表，加新指标族要同步加一行。

## 3. 同族坏尺子（一族，不是四个独立的坑）

| 坏尺子 | 为什么骗人 | 真判据 |
|---|---|---|
| `noDataState: OK` 配在看门狗上 | 数据源一死，它下面规则集体变绿 | 看门狗 `Alerting` |
| **`eq` 阈值** | Grafana 只有 `gt`/`lt`/`within_range`/`outside_range`；写 `eq` ⇒ 规则永久 `failed to parse expression`，**从写下那天起一次都没评估过**，症状只是个不起眼的 Error | `within_range(n-0.5, n+0.5)`。09-19 修掉 4 条这样的死规则 |
| **声明了却零 target 的 job** | 压根没有 `up` series ⇒ **永远不会 down**，缺席不告警 | 对账 `prometheus.yml` 的 job 声明，不是现存 series |
| **`Available: True`** | `spec.replicas: 0` 时 0 满足 0，照样 True（"has minimum availability"） | `readyReplicas >= 1` |
| `replicas > 0` | 起得来 ≠ 在服务 | 同上 + 一个真流量计数器 |
| **飞书 HTTP 200** | 飞书会 200 + body `code != 0` 地假装成功 | `alert2feishu_sent_total` 计数器 |
| **文档里写着的那条命令** | 带 `#!` 但没 `+x` ⇒ 照 SOP 敲就是 permission denied，而"按名单校验模式位"的测试看不见名单外的文件（09-20 有 5 个，含当天新写的门禁生产者） | 扫目录认 shebang，不吃名单 |
| **`require_gate_evidence <name>` 在脚本里** | 门禁在强制执行，但可能**全仓没有任何东西生产那份证据**（`split_capacity` 就是：形状全校验、真相零校验，文件手写） | 搜这个名字，必须找到**写**这份证据的代码；找不到就是人签，见灰度手册 6.2.3 |

投递侧三层尺子是叠着坏的：HTTP 200 → `replicas>0` → `Available:True`。
只有 `readyReplicas>=1` **加** 活的投递计数器才算判断。

## 4. 告警送不到人手上（当前唯一未闭合项）

**缺的唯一东西是飞书群机器人 webhook URL**（只有群管理员能拿到，脚本没法代跑）。
拿到后一键：`k8s/monitoring/wire-feishu-alerts.sh`。

三处断点有**依赖顺序，不能颠倒**：

1. `deploy/alert-to-feishu` `replicas=0`，`generation=1` —— generation 是 1 说明
   spec 从未被改过：不是被谁关掉的，是当年建到这步就停住了。
2. 依赖的 Secret `feishu-alert-webhook` 不存在（`alert2feishu.py` 读
   `os.environ["FEISHU_WEBHOOK"]`，缺了 KeyError 起不来）⇒ 解释了第 1 条。
3. Grafana 根路由 receiver 仍是 `grafana-default-email`，联系点建好了没人指过来
   —— 注释当年就写明"只加不切，等真能发出去了再切"。**当初停在这步是对的。**

没有 webhook 就先切策略 = 把全部告警打进一个 CrashLoop 的服务，比现在更糟。

端到端已自检过（假 webhook 指 `.invalid`，绝不外发）：副本起得来、合成告警解析成功、
投递如实失败，`received=1` / `sent=0` / `delivery_failures{reason="exception"}=1`。
**除 webhook 值本身，整条链路已验证可用。** 自检完立刻缩回 0 并删掉假 Secret ——
不留一个指向 `.invalid` 的 Secret，否则将来有人起副本会以为已经配好了。

两个调用形状（脚本里已绕开）：grafana pod 里的 `wget` 是 BusyBox **没有 `--method`**，
切策略要用 `curl`；根路由不是文件式 provisioning 管的，API 改得动、不用 restart。

## 5. 生效判据（按对象分两种，别混）

- **Prometheus**：热重载，不必重启。
  `exec <prom-pod> -c prometheus -- wget -qO- --post-data='' http://127.0.0.1:9090/-/reload`
  判据 = `/api/v1/status/config` 读到新内容 + `up{job=...}` 实例数变成预期值。
- **Grafana 告警/看板**：**必须 `rollout restart deploy/grafana`**，改 CM 不生效。
  判据 = `/api/v1/provisioning/alert-rules` 读出的 `noDataState`/阈值是新值。
  只改 CM 就宣布修好 = 假绿，09-19 实测：进程读到的仍是旧值。

两者都要先等 kubelet 把新内容投影进容器（约 20~60s），判据 = **容器内 `sha256sum`**。

**改之前先 diff 集群现值 vs repo HEAD。** 09-20 实测集群里的 `litellm-stability.yaml`
比 repo 少一处改动（`llm-stab-latency` 的 `execErrState` 集群是 `OK`、repo 是 `Error`）。
这次方向是对的（patch 把 repo 版本带上去），但**反方向就是拿旧文件覆盖掉别人的改动**，
而且 patch 成功、sha 对得上，事后完全看不出来。所以 patch 前必须比一次，并说清
"这次顺带带上去了什么"。

## 6. 探针间隔与读它的尺子是耦合的（改一个必须改三处）

探针节奏不是自由参数。`litellm-stability.yaml` 里有三道尺子按它标定：

| 尺子 | 算术 | 只改间隔不改它会怎样 |
|---|---|---|
| `llm-probe-stale` 的 `gt` 阈值 | **间隔上界** + 单轮耗时 ≈ 正常最大间隔，阈值取 ≈2 轮 | 阈值偏小 ⇒ **每轮假报**；偏大 ⇒ 探针死了要漏 4 轮才响 |
| `min_over_time(litellm_probe_state[W])` ×3（down/auth/misconfig） | `W ≥ 2×间隔上界 + 单轮` —— W 编码的是"连续两轮" | W 不够 ⇒ "连续两轮"**静默退化成一轮**，一次抖动报 critical |
| 那三条的 `relativeTimeRange.from` | 要盖住 W | 图上取不到窗口内的点 |

**间隔是区间，尺子按 MAX 标定，不按均值。** 按均值标 ⇒ 抽到最长那一轮才假报，
**间歇性假报比每轮假报更难查**（每轮都报，一眼就知道是尺子坏了；偶尔报，会被当成真事故追）。

当前值（间隔 `PROBE_INTERVAL_MIN..MAX` = 1800~2700s 随机、串行单轮预算 1200s）：
stale `gt 7800`、窗口 `[115m]`、`from: 7200`。

沿革：间隔 900s 固定 ⇒ `gt 2400` / `[35m]` / `from: 1800`；
1800s 固定、并发 8、单轮 ~92s ⇒ `gt 4200` / `[65m]` / `from: 3900`。

**单轮预算随串行化从 180s 涨到 1200s**，算法是 `目标数 × PROBE_GAP_MAX`（82 × 10s）。
所以除了间隔，**改目标数或改发间停顿上界同样会让 stale 阈值失准** —— preflight 里
`PROBE_TARGETS` 就是为此存在的，端点数涨了要跟着改。

**这条耦合关系是机器校验的**，不只是注释：preflight 的 CADENCE 腿
（`--probe-script probe.py`）读 `probe.py` 里的 `PROBE_INTERVAL_MIN/MAX` 与
`PROBE_GAP_MAX` 默认值，跟规则文件里的窗口和阈值对算术。五个形状都立过阳性对照：
间隔改小 / 窗口改小 / 阈值太松 / 区间反了（MIN>MAX）/ 提取器脱节（变量改名 ⇒
报"先修提取器"，不报"间隔没问题"）。

## 6.5 探针不许长得像扫描器（四件事一起做，缺一件就留指纹）

上游看到的不是"我们的监控"，是一个账号上的请求流。**固定节奏 + 固定并发 +
固定文本 = 不需要理解意图就能命中的机器指纹。** 所以 `probe.py` 里四件事同时做：

| 做法 | 参数 | 缺了它会怎样 |
|---|---|---|
| 轮间隔随机 30~45 分钟 | `PROBE_INTERVAL_MIN/MAX` | 每账号每天固定在同样的分钟数上被打 48 次 |
| 串行，不并发 | 原 `PROBE_CONCURRENCY=8` **已删** | 82 发挤在 92s 内成一个短促脉冲 |
| 轮内顺序每轮重洗 + 发间随机停 2~10s | `PROBE_GAP_MIN/MAX` | 并发虽是 1，同一账号每轮仍固定落在第 N 分钟 |
| prompt 从 10 条里随机抽 | `PING_PROMPTS` | 一串一模一样的 `"ping"` |

两条不许越的线：

1. **不许靠抽样降密度。** 每轮仍然全量探完 82 个端点。少探一部分会让
   「连续两轮探不通」读到陈旧值 —— 某端点这轮没被选中，指标停在上一轮的绿上，
   告警看到的是旧数据。那是拿假绿换来的安静。
2. **prompt 随机化只动 `messages` 那句话，body 其余形状一个字段都不能动** ——
   尤其不许补 `reasoning_effort`。真实客户端不发它，它必须由模型行的
   `litellm_params` 提供；探针一补，"配置漏了档位"这类红就被构造性屏蔽掉
   （2026-09-01 `cursor-g-82-sol` 实测踩过：用户全线 500，我的探针全绿）。
   换文本之所以安全，是因为**判据只有状态码** —— 生产挂着 `error_sanitize`，
   body 里的错误文本早被抹成 `API 异常 (req: <id>)`，本来就不可判。

## 7. 探针挑哪个落点当代表（id 名字会骗人）

探针按端点收敛：1598 个 deployment → 116 个端点 → 82 个可探目标，每个端点挑**一个**
落点当代表。打分在 `build_targets()` 的 `score()`，键是
`(neg, unsupported, tier, 有无"/", 名字)`：

- `NEG_HINTS` —— 名字一看就不是 chat（image/veo/embed/rerank…），选中必然假红。
- `UNSUPPORTED_UPSTREAM_MODELS` —— **上游明确不接受这个模型**。匹配的是
  `litellm_params.model`，**不是 id 后缀**。09-20 实测：
  `chatgpt-acct-NNN-gpt-5.3-codex` 的上游真名是 `openai/chatgpt-gpt-5.3-codex-spark`，
  只看 id 名字会漏掉。
- `TIER_HINTS` —— 档位从低到高，别去烧强模型那个额度桶。

三条纪律：

1. **降级不是删除。** 不支持的落点仍留在候选里（万一上游哪天支持了，轮换还能用），
   只是永远不当首选。
2. **匹配串必须带池子前缀**（`chatgpt-`）。同名落点挂在别的端点上照样是好的 ——
   `wangsu7-gpt-5.3-codex` 实测 200，`chatgpt-acct-*-gpt-5.3-codex` 全 400。
3. **改完必须拿线上真拓扑干跑一遍打分器**，再谈部署。09-20 第一版补丁只调了
   `TIER_HINTS` 顺序（把 5.3 提到 5.4 前），干跑显示 5.4 从 53 个端点归零 ——
   看着是修好了，实际 39 个 ChatGPT 账号整体搬到了 `gpt-5.3-codex`，**同一个 400
   换了个名字**。没有那次干跑，这个补丁会当成"已修复"发上去。

**为什么这不是小事**：`probe_one()` 轮换能兜住结果（首选 400 就换下一个候选，并把
能用的提到候选表头），所以指标上**看不出任何异常**。但那个提升只活在内存里的
`targets` 列表，`TOPO_REFRESH`（3600s）一到就清空 —— 于是每个端点每小时白烧一发
注定失败的请求，永远。实测修前 39 发/小时，修后 **0**。轮换是容错，不是修复。

## 8. 两个不能碰的东西

- **`zz-delete-probe.yaml` 是墓碑，别删。** Grafana 文件式 provisioning 不会因为
  规则从文件里消失就删它，必须显式 `deleteRules`。删了 = 09-13 那个阳性对照探针
  （uid `zz-positive-control`）复活。
- **凭据一个都不在这 11 个文件里**，全走 env（`PROBE_KEY` / `ADMIN_KEY` /
  `FEISHU_WEBHOOK` / `FEISHU_SIGN_SECRET` / Prometheus 的 `credentials_file`）。
  **保持这个性质** —— 往这些文件里写任何 `sk-` 开头的东西都是错的。

## 9. 当前基线（2026-09-20 实测）

**探针**：`deploy/litellm-probe`（ns `litellm-product`）generation 9，1 副本，
`topology_failures_total=0`、`known_deployments=1598` / `known_endpoints=116`，
34 个只剩图像/embedding 的端点探不了（计入 `litellm_probe_unprobeable_endpoints`）。
目标按 kind 分布：chatgpt账号池 39、zerokey账号 15、外部网关 11、zerokey-cursor 10、
cursor网关 3、copilot2api 2、sub2api 1、官方API 1。

节奏（**09-20 当天第二次改，反风控**，repo 已改，集群待同步）：间隔 1800~2700s 随机、
串行、发间停 2~10s ⇒ 单轮约 14 分钟（最坏约 20 分钟；原 8 并发是 92s）。
周期 = 间隔 + 单轮 ⇒ **每账号每天约 22~33 发**
（沿革：15 分钟一轮 96 发 → 30 分钟固定 48 发 → 现在）。
⚠️ **算每天几发别拿间隔当分母** —— 串行化之后单轮耗时不再可忽略，漏掉它会把密度
算高 1/3（我第一版就这么错的：写成 32~48）。
⚠️ 单轮耗时和每天发数都是按 82 目标 × 停顿均值 + 单发实测 3~5s 算的**预期值，
不是实测** —— 集群同步后拿 `litellm_probe_round_duration_seconds` 复核。

**Grafana**：12 条 provisioned 规则全部 `health=ok`、无 `lastError`。
`llm-probe-stale` 与 `llm-stab-scrape-down` 是 `noData=Alerting`，其余 `OK`；
**全部 `execErr=Error`**。

**Prometheus**（2026-09-19 实测，未变）：
17/17 target up，无 stale >5min，7 个活 job 全满编：`litellm-proxy` 5/5、
`litellm-probe` 1/1、`kubelet` 3/3、`kubelet-cadvisor` 3/3、`node-exporter` 3/3、
`kube-state-metrics` 1/1、`prometheus` 1/1。第 8 个声明的 job `alert2feishu`
**零 target**（见第 4 节）。Prometheus `monitoring/prometheus` ClusterIP
`10.43.32.210:9090`；Grafana `10.43.217.110:3000`，NodePort `10.68.13.242:30300`
uid `litellm-198`。

⚠️ Grafana admin 口令是 `deploy/grafana` 上的明文 env `GF_SECURITY_ADMIN_PASSWORD`
—— 只在容器内用，**永不打印、永不放命令行**。
`deploy/alert-to-feishu` 的 `FEISHU_WEBHOOK`/`FEISHU_SIGN_SECRET` 同理，
打日志前 `sed -E 's#(hook/)[A-Za-z0-9-]+#\1<REDACTED>#g'`。

## 10. 待办（本 skill 范围内）

- `llm-stab-scrape-down` 的完整性阈值目前硬编码 5，它自己的 annotation 就建议
  改成动态对比 `kube_deployment_status_replicas`。
- 拿到 webhook 后跑 `wire-feishu-alerts.sh`，然后把 DELIVER 那条腿从 MANUAL 转自动。
- `UNSUPPORTED_UPSTREAM_MODELS` 现在是手写常量。更彻底的做法是让探针把首选 400 的
  落点**落到一个指标上**（而不是只靠轮换悄悄绕过），这样"白烧"本身就能被告警看见，
  不必依赖人去干跑打分器才发现。

相关：[[litellm-gray-key-rollout]]（切流本体，preflight 是它的第 −1 天门禁）、
[[litellm-ops]]（proxy 侧运维）、[[her-oom-alert-triage]]（her 实例侧告警分诊）
