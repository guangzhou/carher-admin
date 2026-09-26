#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐条真打看板里的每个查询，判它到底出不出数。

为什么需要这个：Grafana 导入成功 / 看板打得开 / panel 渲染出坐标轴，
这三件事**都不需要查询返回数据**。一个查错指标名的 panel 和一个
「此刻确实没有故障」的 panel 长得一模一样。2026-09-25 实测：线上
「LiteLLM 模型稳定性」看板里有 1 个 panel 查的指标 proxy 从来没 emit 过，
它已经空跑了 17 天没人发现。

用法：
    verify-dashboard-queries.py k8s/monitoring/model-perf.json \
        --prom http://10.68.13.198:30900

退出码：0 = 每条都出数；1 = 有查询返回空或报错。

⚠️ 空结果有两种，脚本分不开，必须人看：
   - 查询写错（指标名/label 不存在）  → 必须修
   - 此刻确实没有故障（比如 fallback 为 0）→ 正常
   所以输出里对每条空结果都标了「该指标族在 Prometheus 里存不存在」，
   存在但为空 = 大概率是第二种。
"""
import argparse, json, re, sys, urllib.parse, urllib.request

# Grafana 内置宏。不代入会让查询以 HTTP 400 失败，看起来像「看板坏了」，
# 实际是尺子少了一步。2026-09-25 实测假红 8 条。
MACROS = {
    "$__rate_interval": "1m", "$__interval": "1m",
    "$__range": "30m", "$__range_s": "1800", "$__range_ms": "1800000",
}

# 看板自定义变量的兜底代入值（按变量名）。没列到的走 allValue / `.*`。
# 🔴 2026-09-26：这里原来是一张**硬编码表** `{"$model": ".*", "$win": "30m"}`，
# 于是任何新加的看板变量都不会被代入，`api_provider=~"$provider"` 原样发给
# Prometheus、匹配不到任何序列 → 判成 EMPTY「指标存在但此刻无数据，可能是真的没故障」。
# 当天实测：`carher-ops.json` 39 个 panel 里 24 条这样被判成假 EMPTY，
# 而其中包括 QPS 这种**绝不可能没数据**的面板。
# ⇒ 代入不许跟变量名绑定；改成从看板 `templating` 里读出**所有**已定义变量，
#   统一按 allValue（没有就 `.*`）代入。新加变量不用再改尺子。
FALLBACK = {"win": "30m"}


def substitutions(tpl_list):
    """看板里每个已定义变量的代入值。"""
    s = dict(MACROS)
    for v in tpl_list or []:
        name = v.get("name")
        if not name:
            continue
        if name in FALLBACK:
            s["$" + name] = FALLBACK[name]
        elif v.get("allValue"):
            s["$" + name] = v["allValue"]
        elif v.get("type") == "custom" and v.get("query"):
            s["$" + name] = str(v["query"]).split(",")[0].strip()
        else:
            s["$" + name] = ".*"
    return s

# `$1`..`$9` 是 label_replace 的捕获组，不是看板变量，别当变量查。
_VAR = re.compile(r"\$(__)?([A-Za-z_]\w*)")


def undefined_vars(expr, defined):
    """用了但看板没定义的变量。

    这是 2026-09-25 补的：**代入本身会把这个病藏起来**。尺子无条件把 `$win`
    换成 `30m` 再去打 Prometheus，于是一块引用了看板根本没定义的变量的面板
    在尺子这里是满分绿，在 Grafana 里却把字面量 `$win` 发出去直接报错。
    当天我自己就差点这样上一块死面板（`model-stability.json` 的
    `templating` 是 null，而我照另一份看板的习惯写了 `[$win]`）。
    """
    out = set()
    for m in _VAR.finditer(expr):
        if m.group(1):          # $__ 开头 = Grafana 内置宏，看板不必定义
            continue
        name = m.group(2)
        if name not in defined:
            out.add("$" + name)
    return out


# `litellm_*` 这种 token 既可能是指标名，也可能是 **label 名**
# （`by (litellm_model_name)` / `{litellm_model_name="x"}` / label_replace 的字符串参数）。
# 不剥掉这些位置，就会把 label 名当成「不存在的指标」报红 —— 2026-09-25 实测假红 1 条，
# 而那条查询实际返回 223 series。红的对象自己出数了，就该先疑提取器。
_LABEL_POS = re.compile(
    r"""(?:\b(?:by|on|without|ignoring|group_left|group_right)\s*\([^()]*\))"""
    r"""|(?:\{[^{}]*\})"""          # label selector
    r"""|(?:\[[^\]]*\])"""          # range / offset：`[5m]` 里的 `m` 会被当标识符
    r"""|(?:"[^"]*")""",              # 字符串字面量（label_replace 的参数在这里）
    re.X,
)

# PromQL 的函数 / 聚合算子 / 关键字。剥掉标签位之后剩下的标识符，
# 不在这张表里的才算指标名。
_PROMQL_WORDS = {
    # 聚合算子
    "sum", "min", "max", "avg", "group", "stddev", "stdvar", "count",
    "count_values", "bottomk", "topk", "quantile", "limitk", "limit_ratio",
    # 修饰符 / 关键字（PromQL 里大小写不敏感）
    "by", "without", "on", "ignoring", "group_left", "group_right",
    "offset", "bool", "and", "or", "unless", "start", "end", "inf", "nan",
    # 函数
    "abs", "absent", "absent_over_time", "ceil", "changes", "clamp",
    "clamp_max", "clamp_min", "day_of_month", "day_of_week", "day_of_year",
    "days_in_month", "delta", "deriv", "exp", "floor", "histogram_quantile",
    "histogram_count", "histogram_sum", "histogram_fraction", "histogram_avg",
    "histogram_stddev", "histogram_stdvar", "holt_winters", "info",
    "double_exponential_smoothing", "hour", "idelta", "increase", "irate",
    "label_join", "label_replace", "ln", "log2", "log10", "minute", "month",
    "predict_linear", "rate", "resets", "round", "scalar", "sgn", "sort",
    "sort_desc", "sort_by_label", "sort_by_label_desc", "sqrt", "time",
    "timestamp", "vector", "year", "atan2", "avg_over_time", "min_over_time",
    "max_over_time", "sum_over_time", "count_over_time", "quantile_over_time",
    "stddev_over_time", "stdvar_over_time", "last_over_time",
    "present_over_time", "mad_over_time", "ts_of_max_over_time",
    "ts_of_min_over_time", "ts_of_last_over_time",
}

_IDENT = re.compile(r"\b([A-Za-z_:][A-Za-z0-9_:]*)\b")


def metric_refs(expr):
    """表达式里真正作为**指标名**出现的 token。

    🔴 2026-09-26 修：这里原来写死 `litellm_[a-z0-9_]+|up`，**只认我们自己的前缀**。
    后果:官方 `dashboard_genai_otel` 全部 10 个 panel 查的是 `gen_ai_*`
    (OTel 语义约定),我们一个都没有 —— 但它们从来没被提取成指标名,
    `missing` 恒空,于是 10/10 死查询全部落进 EMPTY 那句
    「指标存在但此刻无数据,可能是真的没故障」,整份看板**退 0 = PASS**。
    一份 100% 死掉的看板和一份「此刻恰好安静」的看板在尺子这里同形。
    ⇒ 指标名提取不许跟前缀绑定;判据只能是「这个标识符在
      `/api/v1/label/__name__/values` 里存不存在」。
    """
    stripped = _LABEL_POS.sub(" ", expr)
    return {
        m.group(1) for m in _IDENT.finditer(stripped)
        if m.group(1).lower() not in _PROMQL_WORDS
    }


# `by (...)` / `sum by (...)` 里的分组标签。
_BY = re.compile(r"\b(?:by|without)\s*\(([^()]*)\)")


def grouping_labels(expr):
    out = set()
    for m in _BY.finditer(expr):
        for x in m.group(1).split(","):
            x = x.strip()
            # `le` 会被 histogram_quantile 吃掉，不会出现在结果里，不能据此判红。
            if x and x != "le":
                out.add(x)
    return out


_LBL_CACHE = {}


def family_labels(prom, metric, cache):
    """某个指标族实际带的标签名集合（问 Prometheus，不猜）。"""
    if metric not in cache:
        try:
            d = fetch(prom, "/api/v1/labels", {"match[]": metric})
            cache[metric] = set(d.get("data") or [])
        except Exception:                               # noqa: BLE001
            cache[metric] = set()
    return cache[metric]


def fetch(prom, path, params):
    url = f"{prom}{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=40) as r:
        return json.load(r)


def expand(expr, subst=None):
    # 长变量名优先，否则 `$model` 会先把 `$model_id` 的前缀吃掉
    for k, v in sorted((subst or MACROS).items(), key=lambda kv: -len(kv[0])):
        expr = expr.replace(k, v)
    return expr


def walk(panels):
    """row 类型的 panel 可能把子 panel 收在自己的 panels 里（collapsed=true）。"""
    for p in panels:
        yield p
        for c in p.get("panels", []) or []:
            yield c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dashboard")
    ap.add_argument("--prom", required=True)
    args = ap.parse_args()

    dash = json.load(open(args.dashboard))
    names = set(fetch(args.prom, "/api/v1/label/__name__/values", {})["data"])
    tpl = dash.get("templating") or {}
    defined = {v["name"] for v in (tpl.get("list") or [])}
    subst = substitutions(tpl.get("list"))

    rows, bad = [], 0
    for p in walk(dash["panels"]):
        for t in p.get("targets", []) or []:
            ref = t.get("refId", "?")   # 有些 target 没有 refId，早期手写的看板里有
            raw = t.get("expr")
            if not raw:
                continue
            # 先判变量定义，再判查询出不出数 —— 代入会把未定义变量藏起来
            undef = undefined_vars(raw, defined)
            if undef:
                rows.append(("UNDEF", p.get("title"), ref, "-",
                             "看板未定义变量: " + ",".join(sorted(undef))))
                bad += 1
                continue
            expr = expand(raw, subst)
            try:
                r = fetch(args.prom, "/api/v1/query", {"query": expr})
            except Exception as e:                      # noqa: BLE001
                rows.append(("ERROR", p.get("title"), ref, repr(e)[:70], ""))
                bad += 1
                continue
            if r.get("status") != "success":
                rows.append(("ERROR", p.get("title"), ref,
                             str(r.get("error"))[:70], ""))
                bad += 1
                continue
            n = len(r["data"]["result"])
            refs = metric_refs(expr)
            missing = sorted(m for m in refs if m not in names)
            if missing:
                rows.append(("DEAD", p.get("title"), ref,
                             f"{n} series", "指标不存在: " + ",".join(missing)))
                bad += 1
            elif n == 0:
                rows.append(("EMPTY", p.get("title"), ref, "0 series",
                             "指标存在但此刻无数据 —— 可能是真的没故障，人工确认"))
            else:
                # 🔴 2026-09-26 加:出数 ≠ 出的是这个 panel 要的东西。
                # `sum(rate(litellm_proxy_total_requests_metric_total[5m])) by (api_key_alias)`
                # 回 1 series 且 metric 是 `{}` —— 因为 `api_key_alias` 被 litellm 侧
                # include_labels 裁掉了。旧尺子读成「OK 1 series」,而 panel 上是一根
                # 没有图例的线,「按 key 拆分」这个诉求一条也没实现。
                #
                # ⚠️ 判据不能是「结果序列里有没有这个标签」—— 那样会被**嵌套聚合**打成假红:
                # `sum(topk(15, sum by (requested_model)(...)))` 外层故意把标签聚掉,
                # 结果无标签是对的。2026-09-26 第一版这么写,把我们两份已上线的好看板
                # 打红 27 条。
                # ⇒ 改成问 Prometheus「这个指标族到底带不带这个标签」,
                #   与表达式结构无关。
                gl = grouping_labels(expr)
                if gl:
                    avail = set()
                    for m in refs:
                        avail |= family_labels(args.prom, m, _LBL_CACHE)
                    lost = sorted(gl - avail) if avail else []
                else:
                    lost = []
                if lost:
                    rows.append(("DEGEN", p.get("title"), ref, f"{n} series",
                                 "该指标族不带这些分组标签,by() 是空转: "
                                 + ",".join(lost)))
                    bad += 1
                else:
                    rows.append(("OK", p.get("title"), ref, f"{n} series", ""))

    w = max((len(r[1] or "") for r in rows), default=20)
    for st, title, ref, detail, note in rows:
        print(f"{st:<6} {title or '':<{w}} {ref}  {detail:<12} {note}")

    from collections import Counter
    c = Counter(r[0] for r in rows)
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
    if bad:
        print(f"\nFAIL: {bad} 条查询指向不存在的指标、分组标签已被裁掉、"
              f"用了看板未定义的变量，或执行失败")
        return 1
    if c.get("EMPTY"):
        print(f"\n注意: {c['EMPTY']} 条查询指标存在但当前无数据，需人工判是否合理")
    print("\nPASS")
    return 0


if __name__ == "__main__":
    # 退出码分三档，别让「量具自己崩了」和「查询是死的」同形 ——
    # 2026-09-25 它就因为一个没有 refId 的 target 抛 KeyError 退了 1，
    # 看上去和「这份看板有坏查询」一模一样。
    #   0 = 全部出数    1 = 有查询指向不存在的指标/执行失败    2 = 量具自己坏了
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:                                   # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("\nBROKEN RULER: 量具自己崩了，这不是看板的结论", file=sys.stderr)
        sys.exit(2)
