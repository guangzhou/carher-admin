#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 verify-dashboard-queries.py 的结论，把一份看板里查不到数的 panel 删掉。

为什么不直接导入官方看板：官方 `dashboard_all_metrics` 有 97 个 panel / 156 条查询，
2026-09-26 在我们这套上实测 **DEAD=104 / EMPTY=48 / OK=4**。原样导进 Grafana，
用户看到的是一页 95% 空白 —— 而**空白 panel 和「此刻没有故障」长得一模一样**，
于是这页看板从上线第一天就不可读，还会把真的故障藏在一堆假空白里。

DEAD 的两个不同来源（脚本会分开标注，但都删）：
  - 我们跑的镜像根本没有这个指标（官方文档比线上版本新）—— 改配置也救不回
  - 镜像有、但没进 `prometheus_metrics_config` 白名单 —— 这类应该先补配置再裁

⚠️ EMPTY 默认**保留**：指标存在、此刻无数据，很可能就是"真的没故障"
   （fallback 为 0、cooldown 为 0）。要连 EMPTY 一起删得显式 `--drop-empty`。

用法：
    prune-dashboard.py in.json --prom http://10.68.13.198:30900 \
        -o out.json --title "LiteLLM 官方全指标（已裁）"

退出码：0 = 裁完；2 = 量具自己坏了（与"这份看板全是死的"区分开）。
"""
import argparse, json, sys, importlib.util, pathlib

_here = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "vdq", _here / "verify-dashboard-queries.py")
vdq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vdq)


def classify(target_expr, prom, names, defined, lbl_cache, subst):
    """单条查询的档位，判据与 verify-dashboard-queries.py 保持同一套。"""
    undef = vdq.undefined_vars(target_expr, defined)
    if undef:
        return "UNDEF", "看板未定义变量: " + ",".join(sorted(undef))
    expr = vdq.expand(target_expr, subst)
    try:
        r = vdq.fetch(prom, "/api/v1/query", {"query": expr})
    except Exception as e:                              # noqa: BLE001
        return "ERROR", repr(e)[:70]
    if r.get("status") != "success":
        return "ERROR", str(r.get("error"))[:70]
    refs = vdq.metric_refs(expr)
    missing = sorted(m for m in refs if m not in names)
    if missing:
        return "DEAD", "指标不存在: " + ",".join(missing)
    n = len(r["data"]["result"])
    if n == 0:
        return "EMPTY", "指标存在但此刻无数据"
    gl = vdq.grouping_labels(expr)
    if gl:
        avail = set()
        for m in refs:
            avail |= vdq.family_labels(prom, m, lbl_cache)
        lost = sorted(gl - avail) if avail else []
        if lost:
            return "DEGEN", "该指标族不带这些分组标签: " + ",".join(lost)
    return "OK", f"{n} series"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dashboard")
    ap.add_argument("--prom", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--title")
    ap.add_argument("--uid")
    ap.add_argument("--drop-empty", action="store_true",
                    help="连 EMPTY 的 panel 也删（默认保留，因为可能是真的没故障）")
    ap.add_argument("--drop-degen", action="store_true", default=True,
                    help="删掉分组标签被裁掉、已塌成无名序列的 panel（默认开）")
    args = ap.parse_args()

    dash = json.load(open(args.dashboard))
    names = set(vdq.fetch(args.prom, "/api/v1/label/__name__/values", {})["data"])
    tpl = dash.get("templating") or {}
    defined = {v["name"] for v in (tpl.get("list") or [])}
    subst = vdq.substitutions(tpl.get("list"))
    lbl_cache = {}

    drop_states = {"DEAD", "UNDEF", "ERROR"}
    if args.drop_degen:
        drop_states.add("DEGEN")
    if args.drop_empty:
        drop_states.add("EMPTY")

    stats = {}
    kept_titles, dropped_titles = [], []

    def keep_panel(p):
        """panel 的所有查询都被判死 ⇒ 删掉整块；部分死 ⇒ 只删那条 target。"""
        tgts = p.get("targets") or []
        if not tgts:
            return True                      # text / row 这类没查询的块，保留
        good, verdicts = [], []
        for t in tgts:
            raw = t.get("expr")
            if not raw:
                good.append(t)
                continue
            st, note = classify(raw, args.prom, names, defined, lbl_cache, subst)
            stats[st] = stats.get(st, 0) + 1
            verdicts.append((st, note))
            if st not in drop_states:
                good.append(t)
        if not good:
            dropped_titles.append((p.get("title"), verdicts[0] if verdicts else None))
            return False
        p["targets"] = good
        kept_titles.append(p.get("title"))
        return True

    def prune(panels):
        out = []
        for p in panels:
            if p.get("type") == "row":
                sub = prune(p.get("panels") or [])
                # 子 panel 全被删掉的 row 自己也没意义了（留着就是一个空标题）
                if sub or not (p.get("panels") or []):
                    p["panels"] = sub
                    out.append(p)
                continue
            if keep_panel(p):
                out.append(p)
        return out

    dash["panels"] = prune(dash.get("panels") or [])
    if args.title:
        dash["title"] = args.title
    if args.uid:
        dash["uid"] = args.uid
    dash.pop("id", None)                    # 导入时让 Grafana 自己分配

    def count(ps):
        n = 0
        for p in ps:
            if p.get("type") == "row":
                n += count(p.get("panels") or [])
            else:
                n += 1
        return n

    json.dump(dash, open(args.out, "w"), ensure_ascii=False, indent=1)
    print("查询分档: " + "  ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    print(f"保留 panel {count(dash['panels'])} 个 / 删掉 {len(dropped_titles)} 个")
    for t, v in dropped_titles[:40]:
        print(f"  ✗ {t}  ({v[0] if v else '-'}: {v[1] if v else ''})")
    if len(dropped_titles) > 40:
        print(f"  ... 另外 {len(dropped_titles)-40} 个")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:                                   # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("\nBROKEN RULER: 裁板工具自己崩了，这不是看板的结论", file=sys.stderr)
        sys.exit(2)
