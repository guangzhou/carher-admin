# monitoring 真身

这 8 个文件在 2026-09-19 之前**只存在于集群 ConfigMap 里**，repo 一份副本都没有。
和 `zk_session_reaper.py` 是同一个病：唯一的家是 ConfigMap，谁误删就没了，改动也没有 diff 可审。

## 文件 → ConfigMap 映射

| 文件 | namespace | ConfigMap | key |
|------|-----------|-----------|-----|
| `litellm-stability.yaml` | monitoring | `grafana-alerting` | `litellm-stability.yaml` |
| `zz-delete-probe.yaml` | monitoring | `grafana-alerting` | `zz-delete-probe.yaml` |
| `feishu-contactpoint.yaml` | monitoring | `grafana-alerting` | `feishu-contactpoint.yaml` |
| `prometheus.yml` | monitoring | `prometheus-config` | `prometheus.yml` |
| `model-stability.json` | monitoring | `grafana-dashboard-litellm` | `model-stability.json` |
| `litellm-proxy.json` | monitoring | `grafana-dashboard-litellm` | `litellm-proxy.json` |
| `alert2feishu.py` | monitoring | `alert2feishu-script` | `alert2feishu.py` |
| `probe.py` | litellm-product | `litellm-probe-script` | `probe.py` |

凭据一个都不在这些文件里，全走 env（`PROBE_KEY` / `ADMIN_KEY` / `FEISHU_WEBHOOK` /
`FEISHU_SIGN_SECRET` / Prometheus 的 `credentials_file`）。**保持这个性质**，
往这些文件里写任何 `sk-` 开头的东西都是错的。

## 改完怎么同步回集群

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
