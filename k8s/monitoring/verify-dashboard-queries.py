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

# 看板变量的代入值。$model 代入 .* = 全选，与 allValue 一致。
SUBST = {
    "$model": ".*", "$win": "30m",
    # Grafana 内置宏。不代入会让查询以 HTTP 400 失败，看起来像「看板坏了」，
    # 实际是尺子少了一步。2026-09-25 实测假红 8 条。
    "$__rate_interval": "1m", "$__interval": "1m",
    "$__range": "30m", "$__range_s": "1800", "$__range_ms": "1800000",
}


# `litellm_*` 这种 token 既可能是指标名，也可能是 **label 名**
# （`by (litellm_model_name)` / `{litellm_model_name="x"}` / label_replace 的字符串参数）。
# 不剥掉这些位置，就会把 label 名当成「不存在的指标」报红 —— 2026-09-25 实测假红 1 条，
# 而那条查询实际返回 223 series。红的对象自己出数了，就该先疑提取器。
_LABEL_POS = re.compile(
    r"""(?:\b(?:by|on|without|ignoring|group_left|group_right)\s*\([^()]*\))"""
    r"""|(?:\{[^{}]*\})"""          # label selector
    r"""|(?:"[^"]*")""",              # 字符串字面量（label_replace 的参数在这里）
    re.X,
)


def metric_refs(expr):
    """表达式里真正作为**指标名**出现的 litellm_* / up。"""
    stripped = _LABEL_POS.sub(" ", expr)
    return set(re.findall(r"\b(litellm_[a-z0-9_]+|up)\b", stripped))


def fetch(prom, path, params):
    url = f"{prom}{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=40) as r:
        return json.load(r)


def expand(expr):
    for k, v in SUBST.items():
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

    rows, bad = [], 0
    for p in walk(dash["panels"]):
        for t in p.get("targets", []) or []:
            ref = t.get("refId", "?")   # 有些 target 没有 refId，早期手写的看板里有
            raw = t.get("expr")
            if not raw:
                continue
            expr = expand(raw)
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
                rows.append(("OK", p.get("title"), ref, f"{n} series", ""))

    w = max((len(r[1] or "") for r in rows), default=20)
    for st, title, ref, detail, note in rows:
        print(f"{st:<6} {title or '':<{w}} {ref}  {detail:<12} {note}")

    from collections import Counter
    c = Counter(r[0] for r in rows)
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
    if bad:
        print(f"\nFAIL: {bad} 条查询指向不存在的指标或执行失败")
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
