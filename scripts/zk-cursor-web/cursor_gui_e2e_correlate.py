#!/usr/bin/env python3
# cursor_gui_e2e_correlate.py — 按 nonce 对账 82 pod 日志:verdict/延迟/机制点火
#
# 配套 cursor_gui_e2e_driver.py 的 manifest.jsonl 用。
# 用法:
#   python3 cursor_gui_e2e_correlate.py <podlog> [case_prefix_filter]
#
# 拉 pod 日志的推荐一手(远程 198,注意 --tail=-1):
#   sshpass -p '...' ssh cltx@10.68.13.198 \
#     "export KUBECONFIG=/home/cltx/.kube/config; \
#      kubectl -n litellm-product logs zero-cursor-bpi-82-<hash> \
#      --tail=-1 --since=15m --timestamps" > /tmp/pod.log
#
# 输出:每个 nonce 的 verdict 序列(带相对延迟)+ 机制点火计数
#   (delta send / DIET / DIET-EXEMPT / handshake / fail-teach / empty-retry / dialect-translated)

import json, re, sys
from datetime import datetime
from collections import defaultdict, Counter

if len(sys.argv) < 2:
    print("usage: python3 cursor_gui_e2e_correlate.py <podlog> [case_prefix]")
    sys.exit(1)

logfile = sys.argv[1]
case_prefix = sys.argv[2] if len(sys.argv) > 2 else None
log = open(logfile).read().splitlines()
manifest = "/tmp/e2e_manifest.jsonl"

def ts(l):
    m = re.match(r'(\S+)', l)
    try: return datetime.fromisoformat(m.group(1).replace('Z', '+00:00')).timestamp()
    except: return None

man = [json.loads(l) for l in open(manifest)]
if case_prefix:
    man = [m for m in man if m['case'].startswith(case_prefix)]

# uniq nonce order-preserved
seen = set(); ordered = []
for m in man:
    if m['nonce'] not in seen:
        seen.add(m['nonce']); ordered.append(m['nonce'])
alln = ordered

by_case = defaultdict(lambda: {'verds': [], 'viols': [], 'delta': 0, 'diet': 0, 'exempt': 0,
                                 'hs': 0, 'ft': 0, 'er': 0, 'dt': [], 'reached': 0, 'lost': 0})

for m in man:
    n = m['nonce']; c = m['case']
    idx = [i for i, l in enumerate(log) if n in l]
    if not idx:
        by_case[c]['lost'] += 1
        continue
    by_case[c]['reached'] += 1
    i0 = idx[0]; t0 = ts(log[i0])
    others = [x for x in alln if x != n]
    for j in range(i0, min(len(log), i0 + 400)):
        L = log[j]
        if j > i0 and any(o in L for o in others): break
        if 'turn-verdict-v2]' in L:
            mm = re.search(r'turn-verdict-v2\]\s+([a-z_-]+)', L)
            v = mm.group(1) if mm else '?'
            by_case[c]['verds'].append((v, round((ts(L) or t0) - t0, 1)))
            if v == 'violation':
                raw = re.search(r'raw="⟦([a-z]+)', L)
                by_case[c]['viols'].append(raw.group(1) if raw else '?')
        if 'delta send' in L: by_case[c]['delta'] += 1
        if 'DIET' in L and 'EXEMPT' not in L: by_case[c]['diet'] += 1
        if 'EXEMPT' in L: by_case[c]['exempt'] += 1
        if 'handshake]' in L and 'implicit' in L: by_case[c]['hs'] += 1
        if 'fail-teach]' in L: by_case[c]['ft'] += 1
        if 'empty-retry]' in L: by_case[c]['er'] += 1
        dtm = re.search(r'dialect-translated ([a-z,]+)', L)
        if dtm: by_case[c]['dt'].append(dtm.group(1).rstrip(','))

print("=== by case ===")
for c in sorted(by_case):
    st = by_case[c]
    verd_ct = Counter(v for v, _ in st['verds'])
    print(f"{c}: reached={st['reached']} lost={st['lost']} viols={st['viols'] or '[]'} "
          f"verdicts={dict(verd_ct)}")
    print(f"    delta={st['delta']} DIET={st['diet']} EXEMPT={st['exempt']} "
          f"handshake={st['hs']} fail-teach={st['ft']} empty-retry={st['er']} "
          f"dialect-translated={st['dt']}")

# violation dialect first-token histogram(诊断:哪种方言在被剥空)
print("\n=== violation raw first tokens (dialect audit) ===")
viols_all = [v for c in by_case.values() for v in c['viols']]
print(Counter(viols_all) if viols_all else "(none — clean run)")
