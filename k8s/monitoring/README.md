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
| `verify-dashboard-queries.py` | 拿真 Prometheus 跑一遍看板里每条 query，出不了数、或用了看板未定义的变量就退 1 |

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

⚠️ **它还有第三个坏法，2026-09-25 已补：代入本身会把病藏起来。** 尺子无条件把 `$win`
换成 `30m` 再去打 Prometheus，于是一块引用了**看板根本没定义的变量**的面板在尺子这里满分绿，
在 Grafana 里却把字面量 `$win` 发出去直接报错。当天我差点这样上一块死面板
（`model-stability.json` 的 `templating` 是 `null`，而我照 `model-perf.json` 的习惯写了 `[$win]`）。
现在非 `$__` 开头的变量必须在 `templating.list` 里有定义，否则报 `UNDEF` 退 1；
`$1`..`$9` 是 `label_replace` 的捕获组，不算变量。这个检查配了双向对照：
合成一份把 `$win` 塞回去的副本必须 `UNDEF`，真定义了 `$win` 的 `model-perf.json` 必须放过。

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

## 已修：冷却次数指标曾被我们自己的 `include_labels` 打死 17 天

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

**同族审计（做过，别再重做一遍）**：这份 config 配了 12 个指标，逐个数 Prometheus 里的序列数，
**11 个都有活序列，唯一零序列的就是 `cooled_down`** ⇒ 病是孤立的，一次滚动就够，不用捆别的。
（⛔ 别用"静态扫 `.labels(` 调用点数位置参数"来做这个审计 —— 实测它把**当前正常工作的**
`litellm_deployment_state` 判成"注册 4 传 1"。一屏红先疑提取器。）

**修法**：把它从共用组里拆出来单独一组。不能直接往 `deployment_state` 组加
`exception_status` —— `litellm_deployment_state` 那个 gauge 只吃 4 个标签，加了会把**当前正常工作的**
指标一起打坏。

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

### 2026-09-25 已切生产，怎么切的 / 怎么滚回去

生产 config 挂的是派生 CM `litellm-cwfb-stable-<hash>`、`subPath` 永不热更，所以这是**生产切换**，
不是监控侧改动。hash 约定 = `config.yaml` 内容的 `sha256[:12]`，**CM 名字自证内容**。

| | |
|---|---|
| 旧 CM（回滚点，**原地未动**） | `litellm-cwfb-stable-aa08e8cdcd1e` |
| 新 CM | `litellm-cwfb-stable-a26e1e6160bb` |
| 动的对象 | `deploy/litellm-proxy` 的 `config` 卷名（strategic merge 按 `name: config` 精确合并，不碰其余 7 个卷）|

回滚 = 把卷名 patch 回旧 CM，旧 CM 一直在。

切换纪律，按顺序：

1. **文本级手术改**，不重序列化整个 61KB YAML（否则 diff 糊成一片、审不了）。
2. **语义 diff 断言**：摘掉 `prometheus_metrics_config` 后两边必须完全相等，且指标集合不变（12 个）
   —— 这是防「推文件上生产 = 把这文件里所有未提交改动一起发」。
3. **在 pod 内起独立进程跑双向对照**：旧 config 下 `increment` 必抛 `ValueError` 且零样本，
   新 config 下两个调用都 OK 且计数器真出样本，同时 `deployment_state` **仍是 4 个标签**（没被连坐）。
4. **门禁贴在换卷名正前方**：新 CM 存在 + 内容 sha 自证 + 旧 CM 在 + 切换前就绪端点 == 4，
   任一不过就 `PREPARED, NOT CUT OVER`。
5. 滚动期间每 15s 记一次就绪端点数。`maxSurge=1 / maxUnavailable=0` ⇒ **全程恒 4，没有降载**，
   4 个新 pod 重启次数 0。

验收三层，不许只做前两层：

- **配置** — CM 内容 sha == `a26e1e6160bb`。
- **装载** — 4/4 pod 容器内 `sha256sum /app/config.yaml` 都等于新值。
- **兜住** — 真实流量下计数器出了第一条序列：`exception_status="401"`，落点
  `chatgpt-acct-85-gpt-6-luna`。**不是靠探针，是真事件。**
  同时核了 `litellm_deployment_state` 的标签集仍无 `exception_status`。

⚠️ **切换后 `state==2` 一度是 0 个落点。那时计数器为空是预期，不是失败** ——
判它死活必须先看 state 那块是不是也为 0，否则就会把「没发生」读成「又坏了」。
⚠️ 生产代理重启会让所有计数器归零，所以**切换后短期内序列数比切换前少是正常的**，
看趋势只用 `increase`/`rate`，别读绝对值、别把它读成回归。

**顺带发现（只记录，没动手）**：第一条冷却是 `chatgpt-acct-85-gpt-6-luna` 返 401。
acct-85 是 free 档，当初上 `gpt-6-sol` 时是被**明确排除**的，但它在 luna 组里且在返 401。
这碰的是生产路由，不在本次范围内。

看板侧现在两块面板、**两个不同口径，别混读**：
`当前被判完全故障 / 冷却的落点数` 读 `state == 2`（当前有几个在里面），
`冷却发生次数 (按 HTTP 状态)` 读 `increase(cooled_down_total[$__range])`（发生了几次）。

### 🔴 未解：`failure_responses_total` 记不到 401，3 块面板 + 1 条告警对 401 全盲

2026-09-26 发现，**这不是 `include_labels` 那个病**（已证伪：它 340 条序列、`exception_status`
标签健在、8 个状态码都有），是这把计数器**确实一条 401 都没记过**。

数据三方对照：

| 尺子 | 401 |
|---|---|
| `litellm_deployment_cooled_down_total` | 有，9 条序列 |
| SpendLogs `error_information` 近 6h | 有，45 行 `AuthenticationError/401` |
| `litellm_deployment_failure_responses_total`（全集群 340 条序列）| **零** |

穷举确认：整个 Prometheus 里带 `exception_status="401"` 的指标**只有 `cooled_down_total` 一个**。
而 `failure_responses_total` 的 303/340 条序列带 `model_id` ⇒ 它**也是按腿计的**，
所以「一个按腿计一个按请求计」解释不了这个差。**机制未解，不编。**
下一步是读 401 路径上这个 counter 到底 inc 没 inc，**不是再换一把尺子**。

受影响的东西（建在它上面 ⇒ 看不见任何 401）：

- `model-stability.json` 三块：`重试/兜底救回来的次数`、`上游腿失败速率`、`上游腿失败占比`
- 告警 `llm-stab-leg-failrate`「单条上游腿失败率 > 20%」（repo 与集群各 1 处，**无漂移**）

⚠️ **这条告警正好漏掉它被设计来抓的那个案例**：`chatgpt-acct-175-gpt-5.6-sol` 在
以约 **10 次/小时**的速率反复 401 冷却、烧重试预算，而这条规则永远不会为它响
—— 它的分子恒 0。判这类腿只能用 `cooled_down_total`。

⚠️ 顺带纠正一个我自己说过的错：**「冷却是一次突发、已停止增长」是错的**。
那是一次时点读数（`increase[15m]` 恰好为 0）就下的结论，复核时 175-sol 仍在
约 10 次/小时地涨。**判「还在不在烧」必须比两个时点或看速率，单次 `increase` 为 0 不是结论。**

### ⚠️ 遗留：这份生产 `config.yaml` 在 repo 里没有家

本文件开篇那个病（「唯一的家是 ConfigMap，谁误删就没了，改动也没有 diff 可审」），
`litellm-cwfb-stable-<hash>` 那份 61KB 生产 config **正是如此** —— `git ls-files` 里没有它。
查过了，**阻碍不是凭据**：92 个 `api_key` 行里 88 个是 `os.environ/` 引用、4 个是 `dummy-*` 占位，
零真实密钥，所以技术上是可以入库的。阻碍是同步纪律：它每次改都换 hash CM，
入库就等于多一条必须维护的 repo↔集群同步义务，而这类义务一旦漂移就是静默的。
**这件事得单独拍板，本轮只记录，没有擅自入库。**


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
