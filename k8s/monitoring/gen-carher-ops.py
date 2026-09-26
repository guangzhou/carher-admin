# -*- coding: utf-8 -*-
"""生成 carher-ops.json —— CarHer 专用运维页（198）。

和官方那两份裁过的看板的分工：
  - 官方看板 = 按指标铺开，用来「这个指标现在什么样」，97 个 panel 里能用的 35 个。
  - 这一页   = 按**一次事故的排查顺序**铺开：先看用户面（QPS/错误率/TTFT），
               再看是不是被兜底救回来了（fallback/cooldown），最后看是不是我们自己
               的机器/Redis/DB 拖的（第四段）。

为什么是生成器不是手写 JSON：每个用户面 panel 都要带同一段「剔除探针流量」子句
+ 同一组 `$model` / `$provider` 选择器，手写一定会漏一个，而漏掉的那个 panel
长得和正常的一模一样。

🔴 `$provider` 不是所有 panel 都能用 —— fallback 那两个指标结构上就没有
`api_provider` 这一维。凡是用不上的，代码里显式走 SEL_M（只带 model），
并且在「盲区」那段写清楚，不做静默降级。
"""
import json
import urllib.parse
import urllib.request

PROM = "http://10.68.13.198:30900"

# ── 探针流量剔除：与 model-stability.json / model-perf.json 逐字一致，别另写一版 ──
EXCL = ('unless on(requested_model) label_replace('
        'max by (model_id)(litellm_probe_target_info), '
        '"requested_model", "$1", "model_id", "(.*)")')

# 带 model + provider 两维的指标用这个
SEL = '{requested_model=~"$model",api_provider=~"$provider"}'
# 只有 model 维、没有 api_provider 的指标用这个（fallback 系列）
SEL_M = '{requested_model=~"$model"}'


def prom_has_label(metric, label):
    """这个指标族到底带不带这个标签 —— 问 Prometheus，不猜。

    生成期就问，是为了**不生成一个 by() 空转的 panel**：那种 panel 会出数、
    会画出一根线，但那根线没有图例，「按 provider 拆」这个诉求一条也没实现。
    """
    url = (f"{PROM}/api/v1/labels?"
           + urllib.parse.urlencode({"match[]": metric}))
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return label in set(json.load(r).get("data") or [])
    except Exception:                                   # noqa: BLE001
        return False


FAIL_HAS_PROVIDER = prom_has_label(
    "litellm_proxy_failed_requests_metric_total", "api_provider")
SEL_FAIL = SEL if FAIL_HAS_PROVIDER else SEL_M
print(f"  litellm_proxy_failed_requests_metric_total 带 api_provider: "
      f"{FAIL_HAS_PROVIDER} → 失败类 panel 选择器 = "
      f"{'model+provider' if FAIL_HAS_PROVIDER else 'model only'}")


def q(expr, legend=None, ref="A", instant=False, fmt=None):
    t = {"refId": ref, "expr": expr, "editorMode": "code", "range": not instant}
    if legend:
        t["legendFormat"] = legend
    if instant:
        t.update({"instant": True, "range": False})
    if fmt:
        t["format"] = fmt
    return t


_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def gp(x, y, w, h):
    return {"h": h, "w": w, "x": x, "y": y}


def stat(title, expr, gpos, unit="none", dec=2, desc="", thresholds=None):
    steps = thresholds or [{"color": "text", "value": None}]
    return {
        "id": nid(), "type": "stat", "title": title, "description": desc,
        "datasource": None, "gridPos": gpos, "targets": [q(expr, instant=True)],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                      "values": False},
                    "textMode": "auto", "colorMode": "value",
                    "graphMode": "none", "justifyMode": "auto"},
        "fieldConfig": {"defaults": {"unit": unit, "decimals": dec,
                                     "color": {"mode": "thresholds"},
                                     "thresholds": {"mode": "absolute",
                                                    "steps": steps}},
                        "overrides": []},
    }


def ts(title, targets, gpos, unit="s", desc="", dec=2,
       legend_calcs=("mean", "max", "lastNotNull")):
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

# ═════════════════ 一、用户面总览 ═════════════════
P.append(row("一、用户面（窗口 = $win，已剔除探针流量）", 0))
y = 1

REQ = (f'sum(sum by (requested_model)'
       f'(rate(litellm_proxy_total_requests_metric_total{SEL}[$win])) {EXCL})')
FAIL = (f'sum(sum by (requested_model)'
        f'(rate(litellm_proxy_failed_requests_metric_total{SEL_FAIL}[$win])) {EXCL})')

P.append(stat("QPS（请求/秒）", REQ, gp(0, y, 4, 4), unit="reqps", dec=2,
              desc="窗口内平均请求速率。探针流量已剔除。"))
P.append(stat("用户真实失败率",
              f'100 * ({FAIL}) / clamp_min({REQ}, 0.0001)',
              gp(4, y, 4, 4), unit="percent", dec=2,
              desc="重试和兜底全挂了才算一次失败 —— 这是用户真正看到的失败率，"
                   "不是上游失败率。上游在坏但兜底救回来了，这里是 0。",
              thresholds=[{"color": "green", "value": None},
                          {"color": "orange", "value": 1},
                          {"color": "red", "value": 5}]))
for pct, lab, x, th in (("0.50", "p50", 8, 5), ("0.95", "p95", 12, 10),
                        ("0.99", "p99", 16, 20)):
    P.append(stat(
        f"TTFT {lab}",
        f'histogram_quantile({pct}, sum by (le)(sum by (le,requested_model)'
        f'(rate(litellm_llm_api_time_to_first_token_metric_bucket{SEL}[$win])) {EXCL}))',
        gp(x, y, 4, 4), unit="s",
        desc="首字时间。⚠️ 只统计**流式**请求，且起点是「上游调用开始」，"
             "不含 LiteLLM 侧鉴权/排队 —— 与用户浏览器感受到的首字节不是一回事。",
        thresholds=[{"color": "green", "value": None},
                    {"color": "orange", "value": th},
                    {"color": "red", "value": th * 3}]))
P.append(stat("在抓的 proxy 副本", 'count(up{job="litellm-proxy"} == 1)',
              gp(20, y, 4, 4), unit="none", dec=0,
              desc="应为 4。低于 4 时本页**所有**面板的分母都偏小，先修这个再读别的。",
              thresholds=[{"color": "red", "value": None},
                          {"color": "green", "value": 4}]))

# ═════════════════ 二、TTFT ═════════════════
y += 4
P.append(row("二、TTFT 首字时间（仅流式请求）", y))
y += 1
P.append(ts("TTFT 分位（当前筛选聚合）",
            [q(f'histogram_quantile({p_}, sum by (le)(sum by (le,requested_model)'
               f'(rate(litellm_llm_api_time_to_first_token_metric_bucket{SEL}[$win])) {EXCL}))',
               lab, ref)
             for p_, lab, ref in (("0.50", "p50", "A"), ("0.95", "p95", "B"),
                                  ("0.99", "p99", "C"))],
            gp(0, y, 12, 8),
            desc="p50 平、p99 翘 = 长尾；三条一起翘 = 上游整体变慢。"))
P.append(ts("按模型 TTFT p95（top 15）",
            [q(f'topk(15, histogram_quantile(0.95, (sum by (le, requested_model)'
               f'(rate(litellm_llm_api_time_to_first_token_metric_bucket{SEL}[$win])) {EXCL})))',
               "{{requested_model}}")],
            gp(12, y, 12, 8),
            desc="只画 top 15，避免几百条线糊成一片。"))
y += 8
P.append(ts("按 Provider TTFT p95",
            [q(f'histogram_quantile(0.95, sum by (le, api_provider)'
               f'(rate(litellm_llm_api_time_to_first_token_metric_bucket{SEL}[$win])))',
               "{{api_provider}}")],
            gp(0, y, 12, 7),
            desc="哪家上游首字慢。⚠️ 这条**没有**剔除探针流量：unless 要在 "
                 "requested_model 维上做，而这里已经聚到 provider 维了。"))
P.append(ts("端到端 vs 上游 vs LiteLLM 侧排队 p95",
            [q(f'histogram_quantile(0.95, sum by (le)(sum by (le,requested_model)'
               f'(rate(litellm_request_total_latency_metric_bucket{SEL}[$win])) {EXCL}))',
               "端到端 p95", "A"),
             q(f'histogram_quantile(0.95, sum by (le)(sum by (le,requested_model)'
               f'(rate(litellm_llm_api_latency_metric_bucket{SEL}[$win])) {EXCL}))',
               "上游 API p95", "B"),
             q(f'histogram_quantile(0.95, sum by (le)(sum by (le,requested_model)'
               f'(rate(litellm_request_queue_time_seconds_bucket{SEL}[$win])) {EXCL}))',
               "LiteLLM 侧排队 p95", "C")],
            gp(12, y, 12, 7),
            desc="🔴 这三条一起看才回答得了「TTFT 很好但用户说卡」：TTFT 的起点是"
                 "**上游调用开始**，排队那一段完全在它之外。"
                 "「排队 p95」翘起来 = 卡在我们这侧，与上游快慢无关。"))

# ═════════════════ 三、吞吐与错误 ═════════════════
y += 7
P.append(row("三、吞吐与错误", y))
y += 1
P.append(ts("按模型 QPS（top 15）",
            [q(f'topk(15, (sum by (requested_model)'
               f'(rate(litellm_proxy_total_requests_metric_total{SEL}[$win])) {EXCL}))',
               "{{requested_model}}")],
            gp(0, y, 12, 7), unit="reqps"))
P.append(ts("按模型 用户真实失败率（%，只画出过错的）",
            [q(f'topk(15, 100 * (sum by (requested_model)'
               f'(rate(litellm_proxy_failed_requests_metric_total{SEL_FAIL}[$win])) {EXCL})'
               f' / clamp_min((sum by (requested_model)'
               f'(rate(litellm_proxy_total_requests_metric_total{SEL}[$win])) {EXCL}), 0.0001) > 0)',
               "{{requested_model}}")],
            gp(12, y, 12, 7), unit="percent",
            desc="`> 0` 过滤掉一直健康的模型。分母 clamp_min 防 0 除。"))
y += 7
P.append(ts("失败按错误码拆",
            [q(f'sum by (exception_status)'
               f'(rate(litellm_proxy_failed_requests_metric_total{SEL_FAIL}[$win])) > 0',
               "{{exception_status}}")],
            gp(0, y, 8, 7), unit="reqps",
            desc="403=没权限 / 429=限流 / 5xx=上游挂 / 499=客户端断开。"
                 "⚠️ 没剔除探针流量（这一维上没有 requested_model 可以 unless），"
                 "只看形状变化，别当 SLA 分子。"))
P.append(ts("失败按异常类型拆",
            [q(f'sum by (exception_class)'
               f'(rate(litellm_proxy_failed_requests_metric_total{SEL_FAIL}[$win])) > 0',
               "{{exception_class}}")],
            gp(8, y, 8, 7), unit="reqps"))
P.append(ts("按 Provider QPS",
            [q(f'sum by (api_provider)'
               f'(rate(litellm_proxy_total_requests_metric_total{SEL}[$win]))',
               "{{api_provider}}")],
            gp(16, y, 8, 7), unit="reqps",
            desc="流量落在哪几家上游。⚠️ 未剔除探针流量（已聚到 provider 维）。"))

# ═════════════════ 四、Token 吞吐 ═════════════════
y += 7
P.append(row("四、Token 吞吐", y))
y += 1
P.append(ts("Token/秒：输入 vs 输出",
            [q(f'sum(sum by (requested_model)'
               f'(rate(litellm_input_tokens_metric_total{SEL}[$win])) {EXCL})',
               "输入 token/s", "A"),
             q(f'sum(sum by (requested_model)'
               f'(rate(litellm_output_tokens_metric_total{SEL}[$win])) {EXCL})',
               "输出 token/s", "B"),
             q(f'sum(sum by (requested_model)'
               f'(rate(litellm_output_reasoning_tokens_metric_total{SEL}[$win])) {EXCL})',
               "其中推理 token/s", "C")],
            gp(0, y, 12, 7), unit="short", dec=1,
            desc="输入远大于输出是长上下文场景的正常形态。"
                 "「推理 token」是输出的子集，不要重复计入。"))
P.append(ts("按模型 总 token/秒（top 15）",
            [q(f'topk(15, (sum by (requested_model)'
               f'(rate(litellm_total_tokens_metric_total{SEL}[$win])) {EXCL}))',
               "{{requested_model}}")],
            gp(12, y, 12, 7), unit="short", dec=1))
y += 7
P.append(ts("缓存命中的输入 token/秒（省下来的钱）",
            [q(f'sum by (requested_model)'
               f'(rate(litellm_input_cached_tokens_metric_total{SEL}[$win])) > 0',
               "{{requested_model}}")],
            gp(0, y, 12, 7), unit="short", dec=1,
            desc="prompt caching 实际命中的量。为空 = 当前筛选下没有缓存命中。"))
P.append(ts("按 Provider 花费（$/小时）",
            [q(f'sum by (api_provider)'
               f'(rate(litellm_spend_metric_total{SEL}[$win])) * 3600',
               "{{api_provider}}")],
            gp(12, y, 12, 7), unit="currencyUSD", dec=3,
            desc="⚠️ 只统计 LiteLLM 算得出价格的模型；自建/未配价的上游这里是 0，"
                 "不代表免费。"))

# ═════════════════ 五、兜底与冷却 ═════════════════
y += 7
P.append(row("五、兜底（fallback）与冷却（cooldown）", y))
y += 1
P.append(ts("兜底：救回来了 vs 兜底也失败",
            [q(f'sum(rate(litellm_deployment_successful_fallbacks_total{SEL_M}[$win]))',
               "救回来了", "A"),
             q(f'sum(rate(litellm_deployment_failed_fallbacks_total{SEL_M}[$win]))',
               "兜底也失败", "B")],
            gp(0, y, 8, 7), unit="reqps", dec=3,
            desc="「救回来了」高 = 上游在坏但用户没感觉，在烧重试预算 —— "
                 "这是**最容易被忽略的故障形态**，用户面失败率仍然是绿的。"
                 "🔴 这两个指标结构上没有 api_provider，**Provider 筛选对本面板无效**。"))
P.append(ts("兜底落到哪个模型上",
            [q(f'sum by (fallback_model)'
               f'(rate(litellm_deployment_successful_fallbacks_total{SEL_M}[$win])) > 0',
               "{{fallback_model}}")],
            gp(8, y, 8, 7), unit="reqps", dec=3,
            desc="承接兜底流量的模型。某个名字突然变高 = 它正在给别人当垫背，"
                 "自己的配额会被烧掉。"))
P.append(ts("冷却：被摘出轮转的上游腿",
            [q('sum by (litellm_model_name, api_provider, exception_status)'
               '(rate(litellm_deployment_cooled_down_total[$win])) > 0',
               "{{litellm_model_name}} / {{api_provider}} / {{exception_status}}")],
            gp(16, y, 8, 7), unit="reqps", dec=3,
            desc="LiteLLM 把某条腿判死并摘出轮转的速率。"
                 "🔴 这个指标**不带 requested_model**，所以模型筛选对本面板无效。"))

# ═════════════════ 六、基础设施 ═════════════════
y += 7
P.append(row("六、基础设施（是不是我们自己的机器拖的）", y))
y += 1
CTR = '{namespace="litellm-product",container="litellm",pod=~"litellm-proxy-.*"}'
P.append(ts("proxy 每副本 CPU（核）",
            [q(f'sum by (pod)(rate(container_cpu_usage_seconds_total{CTR}[$win]))',
               "{{pod}}")],
            gp(0, y, 8, 7), unit="none", dec=2,
            desc="与下面的 limit 对照。贴着 limit 跑 = 在被限流，"
                 "会直接表现为上面那个「我们这侧开销」变大。"))
P.append(ts("proxy 每副本内存（工作集）",
            [q(f'sum by (pod)(container_memory_working_set_bytes{CTR})', "{{pod}}")],
            gp(8, y, 8, 7), unit="bytes", dec=1,
            desc="工作集持续爬升不回落 = 泄漏；接近 limit 会 OOMKilled，"
                 "表现为下面「重启次数」跳变。"))
P.append(ts("重启次数（累计）",
            [q('sum by (pod)(kube_pod_container_status_restarts_total'
               '{namespace="litellm-product",container="litellm"})', "{{pod}}")],
            gp(16, y, 8, 7), unit="none", dec=0,
            desc="🔴 这是**累计量**不是速率 —— 滚动更新会让新 pod 从 0 开始，"
                 "曲线下跌是换了 pod，不是修好了。"))
y += 7
P.append(ts("Redis 延迟 p95 / p99",
            [q('histogram_quantile(0.95, sum by (le)(rate(litellm_redis_latency_bucket[$win])))',
               "p95", "A"),
             q('histogram_quantile(0.99, sum by (le)(rate(litellm_redis_latency_bucket[$win])))',
               "p99", "B")],
            gp(0, y, 6, 7), desc="Redis 慢会直接顶到用户面延迟上。"))
P.append(ts("Postgres 延迟 p95 / p99",
            [q('histogram_quantile(0.95, sum by (le)(rate(litellm_postgres_latency_bucket[$win])))',
               "p95", "A"),
             q('histogram_quantile(0.99, sum by (le)(rate(litellm_postgres_latency_bucket[$win])))',
               "p99", "B")],
            gp(6, y, 6, 7),
            desc="花费落库走这里。慢 ≠ 用户受影响（写是异步的），"
                 "但持续慢会让 SpendLogs 落后。"))
P.append(ts("LiteLLM 自身开销：鉴权 / 路由 / 调用前",
            [q('histogram_quantile(0.95, sum by (le)(rate(litellm_auth_latency_bucket[$win])))',
               "鉴权 p95", "A"),
             q('histogram_quantile(0.95, sum by (le)(rate(litellm_router_latency_bucket[$win])))',
               "路由 p95", "B"),
             q('histogram_quantile(0.95, sum by (le)(rate(litellm_proxy_pre_call_latency_bucket[$win])))',
               "调用前 p95", "C")],
            gp(12, y, 6, 7),
            desc="「端到端 减 上游」那段差值具体花在哪。"))
P.append(ts("在途请求数（每副本）",
            [q('sum by (pod)(litellm_in_flight_requests)', "{{pod}}")],
            gp(18, y, 6, 7), unit="none", dec=0,
            desc="副本之间差很多 = 负载不均。这个指标曾被 Prometheus 侧的第二层"
                 "白名单静默丢了 17 天，2026-09-26 才接回来。"))
y += 7
P.append({
    "id": nid(), "type": "table",
    "title": "⚠️ 当前不健康的上游部署（1=部分故障 2=完全故障）",
    "description": "litellm_deployment_state 带 pid 标签（每 worker 一份），"
                   "用 max 收敛到部署级。空表 = 当前没有故障腿。",
    "datasource": None, "gridPos": gp(0, y, 12, 7),
    "targets": [q('max by (litellm_model_name, model_id, api_provider, api_base)'
                  '(litellm_deployment_state) > 0', instant=True, fmt="table")],
    "transformations": [{"id": "organize",
                         "options": {"excludeByName": {"Time": True}}}],
    "fieldConfig": {"defaults": {}, "overrides": []},
    "options": {"showHeader": True},
})
P.append({
    "id": nid(), "type": "table", "title": "Pod 状态（litellm-product 命名空间）",
    "description": "Ready=1 才在接流量。副本数与上面「在抓的 proxy 副本」对不上，"
                   "说明采集侧有问题而不是服务侧。",
    "datasource": None, "gridPos": gp(12, y, 12, 7),
    "targets": [q('kube_pod_status_ready{namespace="litellm-product",condition="true"}',
                  instant=True, fmt="table")],
    "transformations": [{"id": "organize",
                         "options": {"excludeByName": {"Time": True, "job": True,
                                                       "instance": True,
                                                       "condition": True,
                                                       "__name__": True}}}],
    "fieldConfig": {"defaults": {}, "overrides": []},
    "options": {"showHeader": True},
})

# ═════════════════ 七、盲区 ═════════════════
y += 7
P.append(row("七、这一页测不到什么（先读这个，再读上面的数）", y))
y += 1
P.append(text("盲区清单", f"""
**写在页面上而不是交接文档里，是因为读看板的人不会去翻交接文档。**

1. **TTFT 只覆盖流式请求。** 非流式请求根本不产生
   `litellm_llm_api_time_to_first_token_metric`，所以 TTFT 面板的样本数**永远**
   小于 QPS 面板。两者对不上是预期，不是采集丢数。

2. **TTFT 的起点是「上游调用开始」，不是「用户按回车」。** LiteLLM 侧的鉴权、路由、
   排队不在里面。第二段最右那块把**端到端 / 上游 / LiteLLM 侧排队**三条放在一起，
   就是为了回答「TTFT 很好但用户说卡」：那种情况下「排队 p95」是翘的、TTFT 是平的。
   （`litellm_request_queue_time_seconds` 2026-09-26 实测 p95 ≈ 1.4s，有数。）
   → 所以官方 TTFT 指标、日志里的 TTFT、用户浏览器真正感受到的首字节，**是三件事**。

3. **`$provider` 筛选对两类面板无效**，代码里是显式降级不是静默的：
   - **fallback 那两个指标结构上没有 `api_provider`**（只有 requested_model /
     fallback_model / exception_status）—— 永远无法按 provider 拆。
   - `litellm_deployment_cooled_down_total` **没有 `requested_model`**，
     所以反过来 **`$model` 筛选对「冷却」面板无效**。
   - 本次生成时实测 `litellm_proxy_failed_requests_metric_total` 带 api_provider =
     **{FAIL_HAS_PROVIDER}**，失败类面板据此选的是
     `{'model+provider' if FAIL_HAS_PROVIDER else '只按 model'}`。
   - ⚠️ `api_provider` 有一个字面取值 **`"None"`**（LiteLLM 判不出上游时填的，
     不是缺失）。按具体 provider 筛会把这批漏掉 —— 2026-09-26 实测它在请求侧
     占比 0.0%，但在**失败**序列里出现过，查失败时记得把 `None` 也选上。

4. **聚合到 provider 维的面板没有剔除探针流量。** 剔除子句要在 `requested_model`
   维上 `unless`，聚到 provider 之后那一维已经没了。这些面板绝对值偏高，
   只适合看形状变化。

5. **花费面板只覆盖 LiteLLM 算得出价格的模型。** 自建/未配价的上游显示 0，
   **不代表免费**。

6. **「重启次数」是累计量。** 滚动更新时新 pod 从 0 开始，曲线下跌 = 换了 pod，
   不是问题修好了。判趋势要看跳变时刻，不看绝对高度。

7. **副本数 < 4 时本页所有面板的分母都偏小。** 第一段最右边那个卡片是前置条件，
   它不绿的时候，别读别的数。

8. **指标白名单只有一份**（LiteLLM 侧 `prometheus_metrics_config`）。
   Prometheus 侧已于 2026-09-26 改成 `litellm_.*` 全收 —— 之前两份白名单要手动同步，
   不同步的症状是**静默少指标**、没有任何报错，`litellm_in_flight_requests`
   就这样被丢了 17 天。**加指标只改 LiteLLM 侧配置。**
""", gp(0, y, 24, 12)))

dash = {
    "uid": "carher-litellm-ops-198",
    "title": "CarHer · LiteLLM 运维总览 (198)",
    "description": "按排查顺序组织：用户面 → TTFT → 吞吐/错误 → Token → 兜底/冷却 → 基础设施。"
                   "统计窗口用 $win 切换，与 Grafana 时间范围解耦。",
    "tags": ["litellm", "198", "carher", "ops"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "refresh": "1m",
    "time": {"from": "now-6h", "to": "now"},
    "timepicker": {"refresh_intervals": ["30s", "1m", "5m", "15m"]},
    "templating": {"list": [
        {
            "name": "model", "label": "模型", "type": "query", "datasource": None,
            # 只列近 6h 有流量的模型。全量 label_values 会回几百个早已停用的名字。
            "query": {"qryType": 1,
                      "query": 'query_result(topk(300, sum by (requested_model)'
                               '(increase(litellm_proxy_total_requests_metric_total[6h])) > 0))',
                      "refId": "var-model"},
            "regex": '/requested_model="([^"]+)"/',
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
            "refresh": 2, "sort": 1, "hide": 0,
        },
        {
            "name": "provider", "label": "Provider", "type": "query",
            "datasource": None,
            "query": {"qryType": 1,
                      "query": 'label_values(litellm_proxy_total_requests_metric_total, api_provider)',
                      "refId": "var-provider"},
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
            "refresh": 2, "sort": 1, "hide": 0,
        },
        {
            "name": "win", "label": "统计窗口", "type": "custom",
            # 用户明确要的是 30 分钟 / 1 小时 / 6 小时；另外两档是排查时用的短窗。
            "query": "5m,10m,30m,1h,6h",
            "options": [{"selected": v == "30m", "text": v, "value": v}
                        for v in ("5m", "10m", "30m", "1h", "6h")],
            "current": {"selected": True, "text": "30m", "value": "30m"},
            "multi": False, "includeAll": False, "hide": 0,
        },
    ]},
    "panels": P,
}

out = "/Users/Liuguoxian/codes/carher-admin/k8s/monitoring/carher-ops.json"
with open(out, "w") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("wrote", out, "panels:", len(P))
