#!/usr/bin/env python3
"""分析 latency-triage.sh collect 抓下来的叶子 ws_incr 日志。

回答三个问题（全部同窗口、全部来自被测系统自报，零额度成本）：

  1. 会话被甩了几次号 —— 同一个 pck 被多少个不同 acct 服务过。
     这是「黏性丢没丢」的唯一硬判据，比读 WA 日志的 HIT/MISS 更直接。
  2. 丢缓存到底贵多少 —— 按 total_input_items 配平后，
     incremental(有缓存, 帧 ~10KB) vs full_ws(丢缓存, 帧 ~500KB) 的耗时差。
     **配平是必须的**：不配平会把"深会话本来就慢"记到"丢缓存"头上。
  3. 为什么丢缓存 —— full_reason 分布（no_session / ws_closed / props_change /
     prefix_break / shorter_input）。

⚠️ 命中率的分母是坑：走 HTTP 兜底的那几类（lock_busy_full / first_frame_error /
   handshake_401）在 ws_transport 里是 `return None`，**不会 emit mode= 行**，
   所以本脚本算出的 full_ws 占比是「上了 WS 的请求里丢缓存的比例」，
   真实的上下文复用率比它更差。要拿完整分母得同时数 ws_incr_fallback 行。

用法: python3 ws-log-analyze.py /tmp/wspck-XXXX.txt
      日志行形如: acct=136 ws_incr mode=full_ws pck=6e0d9cf5 src=... full_reason=no_session
                  delta_items=5 total_input_items=5 frame_bytes=35085 elapsed_ms=17747
"""
import collections
import re
import sys


def load(path):
    rows = []
    for ln in open(path):
        d = dict(re.findall(r"(\w+)=([^\s]+)", ln))
        if "mode" not in d or "elapsed_ms" not in d:
            continue
        try:
            d["_it"] = int(d["total_input_items"])
            d["_ms"] = int(d["elapsed_ms"])
            d["_fb"] = int(d.get("frame_bytes", 0))
        except (KeyError, ValueError):
            continue
        rows.append(d)
    return rows


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))]


def spray(rows):
    print("=" * 74)
    print("① 会话被甩号情况（一个 pck = 一个对话）")
    byp = collections.defaultdict(set)
    for r in rows:
        byp[r["pck"]].add(r.get("acct", "?"))
    dist = collections.Counter(len(v) for v in byp.values())
    multi = {k for k, v in byp.items() if len(v) > 1}
    nmulti = sum(1 for r in rows if r["pck"] in multi)
    print("  distinct_pck=%d  最多被 %d 个账号服务过" % (len(byp), max(dist)))
    print("  被 N 个账号服务过的分布:", dict(sorted(dist.items())))
    print("  跨号 pck 占比        : %d/%d = %.1f%%" % (len(multi), len(byp), 100 * len(multi) / len(byp)))
    print("  落在跨号 pck 上的请求: %d/%d = %.1f%%   ← 黏性好坏看这一行" % (nmulti, len(rows), 100 * nmulti / len(rows)))
    print("  基线: 2026-09-09 上游劣化期实测 96.4%（一个会话最多被 22 个号服务过）")


def ab(rows):
    print("=" * 74)
    print("② 按上下文长度配平的同窗口 A/B —— incremental(有缓存) vs full_ws(丢缓存)")
    print("   %-12s %26s %26s %14s" % ("items桶", "incremental", "full_ws", "p50差"))
    print("   %-12s %26s %26s" % ("", "n / p50s / p90s / 帧KB", "n / p50s / p90s / 帧KB"))
    for lo, hi in [(1, 20), (21, 60), (61, 120), (121, 240), (241, 600), (601, 10 ** 9)]:
        out = []
        for m in ("incremental", "full_ws"):
            r = [x for x in rows if x["mode"] == m and lo <= x["_it"] <= hi]
            if len(r) < 8:                      # 样本太少不下结论
                out.append(None)
                continue
            ms = [x["_ms"] for x in r]
            out.append((len(r), pct(ms, .5) / 1000, pct(ms, .9) / 1000,
                        pct([x["_fb"] for x in r], .5) / 1024))
        f = lambda o: "n/a" if not o else "%5d / %5.1f / %5.1f / %7.0f" % o
        diff = "" if not (out[0] and out[1]) else \
            "%+.1fs (%.2fx)" % (out[1][1] - out[0][1], out[1][1] / out[0][1])
        print("   %-12s %26s %26s %14s" % ("%d-%d" % (lo, min(hi, 99999)), f(out[0]), f(out[1]), diff))
    print("   基线: 09-09 实测差 1.1~1.4x(+1.6~4.5s)。**丢缓存不是慢的主因**——")
    print("         有缓存的 incremental 自己 p50 也有 12~13s，那才是上游给的地板。")

    fw = sorted((x for x in rows if x["mode"] == "full_ws"), key=lambda x: x["_fb"])
    if len(fw) >= 25:
        print("\n   full_ws 内部按帧大小五等分（排除 mode 混淆，看延迟对字节数敏不敏感）:")
        n = len(fw) // 5
        for i in range(5):
            s = fw[i * n:(i + 1) * n]
            ms = [x["_ms"] for x in s]
            print("     帧 %6.0fKB~%6.0fKB  n=%d  p50=%.1fs p90=%.1fs"
                  % (s[0]["_fb"] / 1024, s[-1]["_fb"] / 1024, len(s),
                     pct(ms, .5) / 1000, pct(ms, .9) / 1000))
        print("     基线: 帧涨 40 倍 p50 只从 14.0 走到 16.2s ⇒ p50 对字节数近乎不敏感，")
        print("           贵的是 p90 和出网流量。")


def reasons(rows):
    print("=" * 74)
    print("③ 为什么丢缓存（full_reason，只在 mode=full_ws 行上有意义）")
    c = collections.Counter(re.sub(r"[:@]\S+", "", r.get("full_reason", "-"))
                            for r in rows if r["mode"] == "full_ws")
    tot = sum(c.values()) or 1
    for k, v in c.most_common(10):
        print("   %-16s %6d  %5.1f%%" % (k, v, 100 * v / tot))
    print("   读法: no_session/ws_closed 占大头 ⇒ 会话被甩号或 WS 断了，配 ① 看；")
    print("         prefix_break/shorter_input 占大头 ⇒ 才是补丁的前缀比对逻辑有问题；")
    print("         evict_lru>0 ⇒ LRU 容量小了（CHATGPT_WS_MAX_SESSIONS）。")

    fb = sum(x["_fb"] for x in rows if x["mode"] == "full_ws")
    ib = sum(x["_fb"] for x in rows if x["mode"] == "incremental")
    print("\n   出网代价: full_ws %.2fGB vs incremental %.2fGB（本窗口）" % (fb / 1e9, ib / 1e9))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    rows = load(sys.argv[1])
    if not rows:
        sys.exit("FATAL: 一行都没解析到。确认文件是 collect 抓的、且带 acct= 前缀。")
    print("总行数 %d   mode 分布 %s" % (rows and len(rows), dict(collections.Counter(r["mode"] for r in rows))))
    print("⚠️ 这个分母不含走 HTTP 兜底的请求（它们 return None，不 emit mode= 行）。")
    spray(rows)
    ab(rows)
    reasons(rows)


if __name__ == "__main__":
    main()
