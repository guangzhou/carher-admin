# monitoring 真身

这 8 个文件在 2026-09-19 之前**只存在于集群 ConfigMap 里**，repo 一份副本都没有。
和 `zk_session_reaper.py` 是同一个病：唯一的家是 ConfigMap，谁误删就没了，改动也没有 diff 可审。
（`model-perf.json` 是 2026-09-25 新加的，从第一天起 repo 就是真身。）

现在 repo 是真身，但**集群仍会漂移**（别人也会 patch），所以下面「改完怎么同步」
第一步是 diff，不是 patch。

## 文件 → ConfigMap 映射

| 文件 | namespace | ConfigMap | key |
|------|-----------|-----------|-----|
| `litellm-stability.yaml` | monitoring | `grafana-alerting` | `litellm-stability.yaml` |
| `zz-delete-probe.yaml` | monitoring | `grafana-alerting` | `zz-delete-probe.yaml` |
| `feishu-contactpoint.yaml` | monitoring | `grafana-alerting` | `feishu-contactpoint.yaml` |
| `prometheus.yml` | monitoring | `prometheus-config` | `prometheus.yml` |
| `model-stability.json` | monitoring | `grafana-dashboard-litellm` | `model-stability.json` |
| `litellm-proxy.json` | monitoring | `grafana-dashboard-litellm` | `litellm-proxy.json` |
| `model-perf.json` | monitoring | `grafana-dashboard-litellm` | `model-perf.json` |
| `alert2feishu.py` | monitoring | `alert2feishu-script` | `alert2feishu.py` |
| `probe.py` | litellm-product | `litellm-probe-script` | `probe.py` |

只在 repo 里、集群不需要的工具（不要去集群找它们）：

| 文件 | 干什么 |
|------|--------|
| `gen-model-perf.py` | `model-perf.json` 的**真身**。改看板改这个脚本再生成，别手改 JSON |
| `verify-dashboard-queries.py` | 拿真 Prometheus 跑一遍看板里每条 query，出不了数就退 1 |

`verify-dashboard-queries.py` 是给看板配的量具 —— 没有它，一块查了不存在指标的面板
会永远空着且零报错（`model-stability.json` 的「被临时拉黑(cooldown)的次数」就是这样
空了十几天）。用法：

```bash
python3 k8s/monitoring/verify-dashboard-queries.py \
  k8s/monitoring/model-perf.json --prom http://10.68.13.198:30900
```

⚠️ 它只判「这条 query 出不出数」，判不了「出的数对不对」。而且**必须配阳性对照**
（拿一份已知在服务的看板跑一遍）：它自己坏过两次都是假红 —— 把 `by (...)` 里的
**标签名**当成指标名，以及没替换 Grafana 的 `$__range` 宏导致 HTTP 400。
一屏红先疑它，别先疑看板。

凭据一个都不在这些文件里，全走 env（`PROBE_KEY` / `ADMIN_KEY` / `FEISHU_WEBHOOK` /
`FEISHU_SIGN_SECRET` / Prometheus 的 `credentials_file`）。**保持这个性质**，
往这些文件里写任何 `sk-` 开头的东西都是错的。

## 改完怎么同步回集群

**第 0 步：diff 集群现值 vs 你手上这份。**

```bash
kubectl -n monitoring get cm grafana-alerting \
  -o jsonpath='{.data.litellm-stability\.yaml}' > /tmp/cluster.yaml
diff /tmp/cluster.yaml k8s/monitoring/litellm-stability.yaml
```

集群比 repo 新 ⇒ 直接 patch 会**静默覆盖掉别人的改动**，而且 patch 成功、sha 对得上，
事后看不出来。2026-09-20 实测过一次反方向（集群少一处 `execErrState: OK→Error`），
patch 把 repo 版本带上去了 —— 方向对，但必须在交付里写明"顺带带上去了什么"。

**禁 `kubectl apply`**（会连带覆盖同一个 CM 里别的 key）。只 patch 单个 key：

```bash
kubectl -n monitoring create cm grafana-alerting \
  --from-file=litellm-stability.yaml=k8s/monitoring/litellm-stability.yaml \
  --dry-run=client -o json \
 | python3 -c 'import json,sys;n=json.load(sys.stdin);print(json.dumps({"data":{"litellm-stability.yaml":n["data"]["litellm-stability.yaml"]}}))' \
 > /tmp/patch.json
kubectl -n monitoring patch cm grafana-alerting --type merge --patch-file /tmp/patch.json
```

生效判据，按对象分两种：

- **Prometheus**：热重载即可，不必重启。
  `kubectl -n monitoring exec <prom-pod> -c prometheus -- wget -qO- --post-data='' http://127.0.0.1:9090/-/reload`
  判据 = `/api/v1/status/config` 里读到新内容 + `up{job=...}` 实例数变成预期值。
- **Grafana 告警/看板**：文件式 provisioning **只在启动时加载**，改 CM 不会生效，
  必须 `kubectl -n monitoring rollout restart deploy/grafana`。
  判据 = `/api/v1/provisioning/alert-rules` 读出的 `noDataState`/阈值是新值。
  只改 CM 就宣布修好 = 假绿，2026-09-19 已实测：进程读到的仍是旧值。

两者都要先等 kubelet 把新内容投影进容器（约 20~60s），判据 = **容器内 `sha256sum`**，
不是 CM 的内容。

## 改 `probe.py` 的探针节奏 ⇒ 必须同时改 `litellm-stability.yaml`

三道尺子按间隔标定：`llm-probe-stale` 的 `gt` 阈值（≈2 轮）、三条
`min_over_time(litellm_probe_state[W])` 的 W（编码"连续两轮"，需 `≥2×间隔上界+单轮`）、
以及那三条的 `relativeTimeRange.from`（要盖住 W）。只改间隔不改它们不会报错，
只会开始说谎：stale 每轮假报，或"连续两轮"静默退化成"一轮"。

间隔是**区间**（`PROBE_INTERVAL_MIN..MAX`，30~45 分钟随机抽，防上游风控指纹），
**尺子按 MAX 标定** —— 按均值标会让抽到最长那一轮间歇性假报，比每轮假报更难查。
单轮耗时按 `目标数 × PROBE_GAP_MAX` 算（串行且发间随机停顿），所以
**改目标数或改停顿上界同样会让阈值失准**。

当前：间隔 1800~2700s、单轮预算 1200s ⇒ `gt 7800` / `[115m]` / `from: 7200`。
（沿革：900s 固定 ⇒ `2400`/`[35m]`/`1800`；1800s 固定并发 8 ⇒ `4200`/`[65m]`/`3900`。）

⚠️ **2026-09-25 实测：上面这套「当前」在 repo 里，没在集群里。** 线上跑的还是
`PROBE_INTERVAL=1800` 固定 + 并发 8（`PROBE_CONCURRENCY` 无覆盖 ⇒ 默认 8），
集群告警仍是 `4200`/`[65m]`/`3900`，集群看板文案仍写「每 30 分钟」。
`b790f26` 把三件事一起改了但**从未部署**。

好消息是两侧各自自洽（集群三件套按 1800 标定、repo 三件套按 2700 标定），没有交叉错配，
所以现在的告警没在说谎。坏消息是 repo↔集群任何方向的单点同步都会**打破这个自洽**：
只推看板文案 = 描述一个线上不存在的行为；只推告警 = 阈值按 2700 标而探针 1800 ⇒ 看门狗钝化一倍。
这三样要么一起上，要么一起不动。

这条耦合关系有机器校验，改完跑它（**不过不许同步上去**）：

```bash
./observability-preflight.py --rules litellm-stability.yaml --probe-script probe.py --static-only
```

## 两个不能踩的坑

**`zz-delete-probe.yaml` 是墓碑，别删。** Grafana 的文件式 provisioning 不会因为规则从
文件里消失就删掉它，必须显式 `deleteRules`。删了这个文件 = 2026-09-13 那个阳性对照
探针（uid `zz-positive-control`）会复活。

**Grafana 阈值函数只有 `gt` / `lt` / `within_range` / `outside_range`。**
写 `eq` 会让整条规则永久 `failed to parse expression`，而且症状只是一个不起眼的
Error 状态 —— 规则从写下来那天起一次都没评估过。判等要用
`within_range(n-0.5, n+0.5)`。2026-09-19 修掉了 4 条这样的死规则。

## `noDataState` 怎么选

- **业务规则**（失败率、p95、fallback）→ `OK`。模型没流量时无数据，不该喊。
- **看门狗规则**（「探针自己停了」「指标采集不全」）→ **必须 `Alerting`**。
  它们的职责就是「数据没了要喊」，配成 `OK` 等于自己把自己关掉：
  探针一死，它依赖的指标跟着消失 → 判成无数据 → 读成正常。
  2026-09-13 到 09-19，探针停了 6 天，6 条探针告警全绿，就是这个机制。

绿色必须先证明量具活着。

## 已备未切：冷却次数指标被我们自己的 `include_labels` 打死了

`litellm_deployment_cooled_down_total` 从建看板起 17 天恒空、零报错。**不是没发生过冷却。**

- 同期 42 个落点进过 `litellm_deployment_state == 2`，而设这个 state 的
  `set_deployment_complete_outage()` 就在 `increment_deployment_cooled_down()` 的**前一行**
  （`litellm/router_utils/cooldown_callbacks.py`，同一个 `if` 块）。前一行成功、后一行没留下任何序列。
- 生产 config 的 `prometheus_metrics_config` 把 `litellm_deployment_cooled_down` 和
  `litellm_deployment_state` 放在同一组 `deployment_state`，`include_labels` 只有 4 个
  （`litellm_model_name, model_id, api_base, api_provider`）。
- 上游 `increment_deployment_cooled_down()` 按 **5 个位置参数**调 `.labels()`（多一个
  `exception_status`）⇒ 每次冷却抛 `ValueError: Incorrect label count`，计数器永远拿不到子序列。

在 pod 内用**同一份 config** 起独立进程实测复现过：注册 labelnames 4 个、调用传 5 个、
`increment` 抛 `ValueError`，而前一行 4 标签的 `set_deployment_complete_outage` 不抛。

⚠️ 这里有个反向的坑：`/metrics` 只暴露**有子序列**的指标（实测 12 个 HELP、零样本指标一个都没有），
所以「指标不在 Prometheus 里」和「从没 inc 过」在这条路上同形 —— 不能拿「查不到」直接判指标不存在。
分辨方法是在容器内起独立进程复现注册，或看紧邻代码路径有没有留下痕迹（这里是 state=2）。

**修法（已实测通过，未上生产）**：把它从共用组里拆出来单独一组。
不能直接往 `deployment_state` 组加 `exception_status` —— `litellm_deployment_state` 那个 gauge
只吃 4 个标签，加了会把**当前正常工作的**指标一起打坏。

```yaml
  - group: deployment_state
    metrics:
    - litellm_deployment_state
    include_labels: [litellm_model_name, model_id, api_base, api_provider]
  - group: deployment_cooldown          # 新增
    metrics:
    - litellm_deployment_cooled_down
    include_labels: [litellm_model_name, model_id, api_base, api_provider, exception_status]
```

**为什么没顺手改**：生产 config 挂的是派生 CM `litellm-cwfb-stable-<hash>`，`subPath` 永不热更 ⇒
要新建 hash CM + 换卷名 + 滚动生产代理。这是生产切换，不是监控侧改动，得单独拍板。

在那之前，看板 `当前被判完全故障 / 冷却的落点数` 读的是 `litellm_deployment_state == 2`
（口径是**当前有几个落点在里面**，不是**被拉黑了几次**），面板描述里写明了这个差别。

## 待填：告警现在评估正确了，但送不到任何人手上

`wire-feishu-alerts.sh` 是这件事的一键脚本，**缺的唯一东西是飞书群机器人的
webhook URL** —— 那只有群管理员能拿到，所以脚本没法代跑。

2026-09-19 实测三处全断，有依赖顺序：

1. `deploy/alert-to-feishu` `spec.replicas=0`，`generation=1`。generation 是 1
   说明它的 spec 从未被改过 —— 不是被谁关掉的，是当年建到这一步就停住了。
2. 它依赖的 Secret `feishu-alert-webhook` **不存在**。`alert2feishu.py:45` 是
   `os.environ["FEISHU_WEBHOOK"]`，缺了就 KeyError 起不来。这解释了上一条：
   webhook 拿不到，副本就没法起，于是停在 0。
3. Grafana 根路由 receiver 是 `grafana-default-email`，
   联系点「feishu-群机器人」建好了但没人指过来 ——
   `feishu-contactpoint.yaml` 的注释当年就写明了「只加不切，等真能发出去了再切策略」。
   当初停在这一步是对的，不是遗漏。

顺序不能颠倒：没有 webhook 就先切策略，等于把全部告警打进一个 CrashLoop 的
服务，比现在（至少 Grafana UI 里还看得到）更糟。

已做过的端到端自检（用假 webhook 指向 `.invalid`，绝不外发）：副本起得来、
合成告警解析成功、投递如实失败，计数器 `received=1` / `sent=0` /
`delivery_failures{reason="exception"}=1`。**除 webhook 值本身，整条链路已验证可用。**
自检完立刻缩回 0 并删掉假 Secret —— 不留一个指向 `.invalid` 的 Secret，
否则将来有人起副本会以为已经配好了。

两个调用形状上的坑，脚本里已经绕开：

- **grafana pod 里的 `wget` 是 BusyBox，没有 `--method`**，发不了 PUT。切策略要用
  `curl`（pod 里有）。根路由不是文件式 provisioning 管的，所以 API 改得动，
  也不需要 `rollout restart`。
- **判投递成功不能看 HTTP 码**：飞书会 200 + body `code != 0` 地假装成功。
  判据是 `alert2feishu_sent_total` 这个计数器。
