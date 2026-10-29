#!/usr/bin/env python3
"""汇总 Cursor cursor.requestTraces.log 里每个 span 的耗时(span_started→span_completed),按名字给中位数/p90/总占比。
用法: python3 spans.py <requestTraces.log>... """
import re, sys, statistics
from datetime import datetime
from collections import defaultdict

rx = re.compile(r'^(\S+Z) (span_started|span_completed) name="([^"]+)" traceId=(\S+) spanId=(\S+)')
dur = defaultdict(list); start = {}; per_trace = defaultdict(dict)
for f in sys.argv[1:]:
    for line in open(f, errors="replace"):
        m = rx.match(line)
        if not m: continue
        ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
        ev, name, tr, sp = m.group(2), m.group(3), m.group(4), m.group(5)
        if ev == "span_started": start[sp] = (ts, name, tr)
        elif sp in start:
            t0, nm, tr = start.pop(sp); d = ts - t0
            dur[nm].append(d); per_trace[tr][nm] = per_trace[tr].get(nm, 0) + d
rows = sorted(dur.items(), key=lambda kv: -statistics.median(kv[1]))
print(f"{'span':70s} {'n':>5s} {'p50':>7s} {'p90':>7s} {'max':>7s}")
for nm, v in rows:
    if statistics.median(v) < 0.05 and max(v) < 1: continue
    v2 = sorted(v); p90 = v2[int(len(v2) * 0.9) - 1] if len(v2) >= 10 else v2[-1]
    print(f"{nm[:70]:70s} {len(v):5d} {statistics.median(v):7.2f} {p90:7.2f} {max(v):7.2f}")
