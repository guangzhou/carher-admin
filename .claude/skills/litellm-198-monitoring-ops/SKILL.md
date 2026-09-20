---
name: litellm-198-monitoring-ops
description: >-
  198 LiteLLM 的监控栈运维：Prometheus 抓取、Grafana 告警规则与看板、飞书投递、
  probe 探针，以及灰度切流前的可观测性自检（第 −1 天门禁）。真身在
  `k8s/monitoring/`（11 个文件），线上形态是 ConfigMap。Use when 用户提到
  "监控" / "告警" / "Prometheus" / "Grafana" / "告警没收到" / "告警规则" /
  "noDataState" / "看板" / "probe 探针" / "飞书告警" / "切流前先看监控" /
  "可观测性"，或要改 `litellm-stability.yaml` / `prometheus.yml` /
  `alert2feishu.py` / `probe.py`。涵盖同步回集群的正确姿势、生效判据、
  以及一整族「绿灯是假的」坏尺子。
---

# 198 LiteLLM 监控栈运维

**这个 skill 解决的问题**：监控的绿灯默认不可信。改监控比改业务更容易"改完看起来
对、其实一次都没评估过"，因为坏掉的量具和健康的系统长得一模一样。

先读 `k8s/monitoring/README.md`（102 行，文件↔ConfigMap 映射表在那里，本 skill 不复制）。

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

# 全量：加活性检查
k8s/monitoring/observability-preflight.py \
  --rules k8s/monitoring/litellm-stability.yaml \
  --prom-config k8s/monitoring/prometheus.yml \
  --prom-url http://10.68.13.242:30300/api/datasources/proxy/1
```

四条腿，任何一条不过 exit 1：

| 腿 | 判什么 | 为什么脚本测得/测不得 |
|---|---|---|
| **LIVE** | 每个 scrape target 最近有数据（`up==1` 且 sample 新鲜） | 自动。**对账对象是 `prometheus.yml` 里声明的 job 列表**，不是现存的 series |
| **RED-ABLE** | 每条规则能被合成红触发 | **MANUAL** —— 读配置说"看起来对"不算，得真发一发 |
| **DELIVER** | 告警真能送到人手上 | 半自动：判 `readyReplicas`，计数器要人看 |
| **FAIL-RED** | 数据断了必须变红 | 自动，走解析后的 YAML 逐条判 |

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

## 6. 两个不能碰的东西

- **`zz-delete-probe.yaml` 是墓碑，别删。** Grafana 文件式 provisioning 不会因为
  规则从文件里消失就删它，必须显式 `deleteRules`。删了 = 09-13 那个阳性对照探针
  （uid `zz-positive-control`）复活。
- **凭据一个都不在这 11 个文件里**，全走 env（`PROBE_KEY` / `ADMIN_KEY` /
  `FEISHU_WEBHOOK` / `FEISHU_SIGN_SECRET` / Prometheus 的 `credentials_file`）。
  **保持这个性质** —— 往这些文件里写任何 `sk-` 开头的东西都是错的。

## 7. 当前基线（2026-09-19 实测）

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

## 8. 待办（本 skill 范围内）

- `llm-stab-scrape-down` 的完整性阈值目前硬编码 5，它自己的 annotation 就建议
  改成动态对比 `kube_deployment_status_replicas`。
- 拿到 webhook 后跑 `wire-feishu-alerts.sh`，然后把 DELIVER 那条腿从 MANUAL 转自动。

相关：[[litellm-gray-key-rollout]]（切流本体，preflight 是它的第 −1 天门禁）、
[[litellm-ops]]（proxy 侧运维）、[[her-oom-alert-triage]]（her 实例侧告警分诊）
