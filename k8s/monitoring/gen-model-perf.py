# -*- coding: utf-8 -*-
"""生成 model-perf.json —— LiteLLM 模型表现看板（198）。

为什么是生成器而不是手写 JSON：24 列网格 + 每个 panel 都要带同一段
「剔除探针流量」子句，手写一定会漏。漏掉的那个 panel 会静默多算探针流量，
长得和正常 panel 一模一样。
"""
import json

# 探针流量剔除：与 model-stability.json 逐字一致（那份已在线运行，别另写一版）。
# 语义 = 把「名字本身就是探针目标」的 requested_model 整条剔掉。
# 已实测：近 30m 有 TTFT 样本的 11 个模型一个都没被它剔掉。
EXCL = ('unless on(requested_model) label_replace('
        'max by (model_id)(litellm_probe_target_info), '
        '"requested_model", "$1", "model_id", "(.*)")')

SEL = '{requested_model=~"$model"}'

def q(expr, legend=None, ref="A", instant=False, fmt=None):
    t = {"refId": ref, "expr": expr, "editorMode": "code", "range": not instant}
    if legend: t["legendFormat"] = legend
    if instant: t.update({"instant": True, "range": False})
    if fmt: t["format"] = fmt
    return t

_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

def gp(x, y, w, h):
    return {"h": h, "w": w, "x": x, "y": y}

def stat(title, expr, gpos, unit="none", dec=2, desc="", thresholds=None, color="thresholds"):
    steps = thresholds or [{"color": "text", "value": None}]
    return {
        "id": nid(), "type": "stat", "title": title, "description": desc,
        "datasource": None, "gridPos": gpos,
        "targets": [q(expr, instant=True)],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "textMode": "auto", "colorMode": "value", "graphMode": "none",
                    "justifyMode": "auto"},
        "fieldConfig": {"defaults": {"unit": unit, "decimals": dec,
                                     "color": {"mode": color},
                                     "thresholds": {"mode": "absolute", "steps": steps}},
                        "overrides": []},
    }

def ts(title, targets, gpos, unit="s", desc="", dec=2, legend_calcs=("mean", "max", "lastNotNull")):
    return {
        "id": nid(), "type": "timeseries", "title": title, "description": desc,
        "datasource": None, "gridPos": gpos, "targets": targets,
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "showLegend": True, "calcs": list(legend_calcs)},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {"unit": unit, "decimals": dec,
                                     "custom": {"lineWidth": 1, "fillOpacity": 6,
                                                "showPoints": "never",
                                                "spanNulls": False}},
                        "overrides": []},
    }

def row(title, y):
    return {"id": nid(), "type": "row", "title": title, "collapsed": False,
            "gridPos": gp(0, y, 24, 1), "panels": []}

def text(title, content, gpos):
    return {"id": nid(), "type": "text", "title": title, "datasource": None,
            "gridPos": gpos, "options": {"mode": "markdown", "content": content}}

P = []

# ───────────────────────── 一、总览 ─────────────────────────
P.append(row("一、总览（统计窗口 = $win，已剔除探针流量）", 0))
y = 1
P.append(stat("请求数（窗口内）",
              f'sum(sum by (requested_model)(increase(litellm_proxy_total_requests_metric_total{SEL}[$win])) {EXCL})',
              gp(0, y, 4, 4), unit="short", dec=0,
              desc="窗口内客户端请求总数。探针流量已剔除。"))
P.append(stat("用户真实失败率",
              f'100 * sum(sum by (requested_model)(increase(litellm_proxy_failed_requests_metric_total{SEL}[$win])) {EXCL})'
              f' / clamp_min(sum(sum by (requested_model)(increase(litellm_proxy_total_requests_metric_total{SEL}[$win])) {EXCL}), 1)',
              gp(4, y, 4, 4), unit="percent", dec=2,
              desc="重试和兜底全挂了才算失败 —— 这是用户真正看到的失败率，不是上游失败率。",
              thresholds=[{"color": "green", "value": None},
                          {"color": "orange", "value": 1},
                          {"color": "red", "value": 5}]))
P.append(stat("TTFT p95",
              f'histogram_quantile(0.95, sum by (le)(sum by (le,requested_model)'
              f'(rate(litellm_llm_api_time_to_first_token_metric_bucket{SEL}[$win])) {EXCL}))',
              gp(8, y, 4, 4), unit="s",
              desc="首字时间 p95。⚠️ 只统计流式请求，且起点是「上游调用开始」，不含 LiteLLM 侧排队。",
              thresholds=[{"color": "green", "value": None},
                          {"color": "orange", "value": 10},
                          {"color": "red", "value": 30}]))
P.append(stat("端到端 p95",
              f'histogram_quantile(0.95, sum by (le)(sum by (le,requested_model)'
              f'(rate(litellm_request_total_latency_metric_bucket{SEL}[$win])) {EXCL}))',
              gp(12, y, 4, 4), unit="s",
              desc="请求从进 LiteLLM 到出 LiteLLM 的完整耗时 p95。",
              thresholds=[{"color": "green", "value": None},
                          {"color": "orange", "value": 30},
                          {"color": "red", "value": 60}]))
P.append(stat("上游 API p95",
              f'histogram_quantile(0.95, sum by (le)(sum by (le,requested_model)'
              f'(rate(litellm_llm_api_latency_metric_bucket{SEL}[$win])) {EXCL}))',
              gp(16, y, 4, 4), unit="s",
              desc="只算上游那一段。端到端 减 这条 ≈ 我们这侧的开销。"))
P.append(stat("在抓的 proxy 副本",
              'count(up{job="litellm-proxy"} == 1)',
              gp(20, y, 4, 4), unit="none", dec=0,
              desc="应为 4。低于 4 说明采集不全 —— 此时所有面板的分母都是偏小的，先修这个再读别的。",
              thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 4}]))

# ───────────────────────── 二、TTFT ─────────────────────────
y += 4
P.append(row("二、TTFT 首字时间（仅流式请求）", y))
y += 1
P.append(ts("TTFT 分位（全选模型聚合）",
            [q(f'histogram_quantile({p}, sum by (le)(sum by (le,requested_model)'
               f'(rate(litellm_llm_api_time_to_first_token_metric_bucket{SEL}[$win])) {EXCL}))',
               lab, ref)
             for p, lab, ref in (("0.50", "p50", "A"), ("0.95", "p95", "B"), ("0.99", "p99", "C"))],
            gp(0, y, 12, 8),
            desc="三条分位放一起看。p50 平、p99 翘 = 长尾问题；三条一起翘 = 上游整体变慢。"))
P.append(ts("按模型 TTFT p95（top 15）",
            [q(f'topk(15, histogram_quantile(0.95, (sum by (le, requested_model)'
               f'(rate(litellm_llm_api_time_to_first_token_metric_bucket{SEL}[$win])) {EXCL})))',
               "{{requested_model}}")],
            gp(12, y, 12, 8),
            desc="哪个模型首字慢。只画 top 15，避免几百条线糊成一片。"))

# ───────────────────────── 三、吞吐与错误 ─────────────────────────
y += 8
P.append(row("三、吞吐与错误", y))
y += 1
P.append(ts("按模型 请求速率（req/分钟，top 15）",
            [q(f'topk(15, (sum by (requested_model)'
               f'(rate(litellm_proxy_total_requests_metric_total{SEL}[$win])) {EXCL}) * 60)',
               "{{requested_model}}")],
            gp(0, y, 12, 7), unit="reqpm", dec=1))
P.append(ts("按模型 用户真实失败率（%，top 15）",
            [q(f'topk(15, 100 * (sum by (requested_model)'
               f'(rate(litellm_proxy_failed_requests_metric_total{SEL}[$win])) {EXCL})'
               f' / clamp_min((sum by (requested_model)'
               f'(rate(litellm_proxy_total_requests_metric_total{SEL}[$win])) {EXCL}), 0.0001) > 0)',
               "{{requested_model}}")],
            gp(12, y, 12, 7), unit="percent",
            desc="分母 clamp_min 防 0 除。> 0 过滤掉一直健康的模型，只留真出过错的。"))
y += 7
P.append(ts("失败按错误码拆（次/分钟）",
            [q(f'sum by (exception_status)'
               f'(rate(litellm_proxy_failed_requests_metric_total{SEL}[$win])) * 60 > 0',
               "{{exception_status}}")],
            gp(0, y, 12, 7), unit="short", dec=2,
            desc="403=没权限 / 429=限流 / 5xx=上游挂 / 499=客户端断开。"
                 "注意这条**没有**剔除探针流量 —— exception_status 这一维上没有 requested_model 可以 unless。"))
P.append(ts("兜底（fallback）：救回 vs 也失败（次/分钟）",
            [q('sum(rate(litellm_deployment_successful_fallbacks_total[$win])) * 60', "救回来了", "A"),
             q('sum(rate(litellm_deployment_failed_fallbacks_total[$win])) * 60', "兜底也失败", "B")],
            gp(12, y, 12, 7), unit="short", dec=2,
            desc="「救回来了」高 = 上游在坏但用户没感觉，属于在烧重试预算。"))

# ───────────────────────── 四、上游腿 ─────────────────────────
y += 7
P.append(row("四、上游部署（哪条腿在坏）", y))
y += 1
P.append(ts("按模型 上游 API p95（秒，top 15）",
            [q(f'topk(15, histogram_quantile(0.95, (sum by (le, requested_model)'
               f'(rate(litellm_llm_api_latency_metric_bucket{SEL}[$win])) {EXCL})))',
               "{{requested_model}}")],
            gp(0, y, 12, 7)))
P.append({
    "id": nid(), "type": "table", "title": "⚠️ 当前不健康的上游部署（1=部分故障 2=完全故障）",
    "description": "litellm_deployment_state 带 pid 标签（每 worker 一份），用 max 收敛到部署级。",
    "datasource": None, "gridPos": gp(12, y, 12, 7),
    "targets": [q('max by (litellm_model_name, model_id, api_provider, api_base)'
                  '(litellm_deployment_state) > 0', instant=True, fmt="table")],
    "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True}}}],
    "fieldConfig": {"defaults": {}, "overrides": []},
    "options": {"showHeader": True},
})

# ───────────────────────── 五、这块看板测不到什么 ─────────────────────────
y += 7
P.append(row("五、盲区（先读这个，再读上面的数）", y))
y += 1
P.append(text("这块看板测不到什么", """
**写在这里而不是交接文档里，是因为读看板的人不会去翻交接文档。**

1. **TTFT 只覆盖流式请求。** 非流式请求根本不产生这个指标，所以 TTFT 面板的样本数
   总是小于请求数面板。两者对不上是**预期**，不是采集丢数。

2. **TTFT 的起点是「上游调用开始」，不是「用户按回车」。** LiteLLM 自己的鉴权、
   排队、worker 抢占都不在里面。当前 `/metrics` **没有** emit
   `litellm_request_queue_time_seconds`，所以这段差值现在量不到。
   → 「TTFT 很好但用户说卡」这种情况，本看板查不出来。

3. **没有 Token 吞吐（TPS）。** 当前 `/metrics` 没有 emit
   `litellm_input_tokens_metric` / `litellm_output_tokens_metric` / `litellm_spend_metric`。
   要 TPS 和费用得先在 LiteLLM 侧放开这几个指标。

4. **没有 cooldown 面板。** `litellm_deployment_cooled_down_total` 这个指标
   proxy 从来没 emit 过。（「LiteLLM 模型稳定性」那份看板里有一个 cooldown 面板，
   查的就是它，**因此那个面板一直是空的** —— 空不代表没发生 cooldown。）

5. **`api_provider` / `model_id` 这两维在请求侧量不到。** 请求类指标的 label 被裁到只剩
   `requested_model`（为控基数，我们有几百个模型名）。所以**同一个模型名下多条上游腿
   谁快谁慢，本看板分不开** —— 一条慢腿会被平均值盖住。要分腿只能看「上游部署」那张表
   或走探针面板。

6. **「失败按错误码拆」那个面板没有剔除探针流量**（那一维上没有 `requested_model`
   可以 `unless`），所以它的绝对值偏高，只适合看**形状变化**，不适合当 SLA 分子。

7. **副本数 < 4 时，上面所有面板的分母都偏小。** 先看「在抓的 proxy 副本」那个卡片。
""", gp(0, y, 24, 9)))

dash = {
    "uid": "litellm-model-perf-198",
    "title": "LiteLLM 模型表现 (198)",
    "description": "按模型看 TTFT / 延迟 / 失败率 / 吞吐。统计窗口用 $win 切换，与 Grafana 时间范围解耦。",
    "tags": ["litellm", "198", "model-performance"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "refresh": "1m",
    "time": {"from": "now-1h", "to": "now"},
    "timepicker": {"refresh_intervals": ["30s", "1m", "5m", "15m"]},
    "templating": {"list": [
        {
            "name": "model", "label": "模型", "type": "query", "datasource": None,
            "query": {"qryType": 1, "query": 'query_result(topk(300, sum by (requested_model)'
                                             '(increase(litellm_proxy_total_requests_metric_total[6h])) > 0))',
                      "refId": "var-model"},
            # 只列近 6h 有流量的模型。全量 label_values 会回几百个早已停用的名字。
            "regex": '/requested_model="([^"]+)"/',
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
            "refresh": 2, "sort": 1, "hide": 0,
        },
        {
            "name": "win", "label": "统计窗口", "type": "custom",
            "query": "5m,10m,30m,1h,6h",
            "options": [{"selected": v == "30m", "text": v, "value": v}
                        for v in ("5m", "10m", "30m", "1h", "6h")],
            "current": {"selected": True, "text": "30m", "value": "30m"},
            "multi": False, "includeAll": False, "hide": 0,
        },
    ]},
    "panels": P,
}

out = "/Users/Liuguoxian/codes/carher-admin/k8s/monitoring/model-perf.json"
with open(out, "w") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("wrote", out, "panels:", len(P))
