#!/usr/bin/env python3
"""量 function_call_output 的大小，并判它与 sources[].document 是不是同一份内容。

这决定 leg2 能不能截到真正的大输出：
  - 大内容在 function_call_output ⇒ convert_output_to_messages 产出 role=tool ⇒ leg2 生效
  - 大内容只在 sources[].document ⇒ 走 RAG 注入进 system/user ⇒ leg2 完全截不到

只读（mode=ro）。
"""
import json
import sqlite3
import sys
from collections import Counter

DB = "file:/app/backend/data/webui.db?mode=ro"


def item_text_len(it):
    """function_call_output 的正文可能在 output / output[].text / content 里。"""
    total = 0
    o = it.get("output")
    if isinstance(o, str):
        total += len(o)
    elif isinstance(o, list):
        for p in o:
            if isinstance(p, dict):
                for k in ("text", "output_text"):
                    if isinstance(p.get(k), str):
                        total += len(p[k])
            elif isinstance(p, str):
                total += len(p)
    c = it.get("content")
    if isinstance(c, str):
        total += len(c)
    elif isinstance(c, list):
        for p in c:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                total += len(p["text"])
    return total


def pct(v, p):
    if not v:
        return None
    return v[min(len(v) - 1, int(len(v) * p / 100))]


def main():
    conn = sqlite3.connect(DB, uri=True)
    cur = conn.cursor()

    fco = []
    fco_names = Counter()
    big_names = Counter()      # >30000 的是哪个工具
    doc_lens = []
    overlap = 0                # fco 正文出现在某个 source document 里
    checked_overlap = 0

    cur.execute("SELECT chat FROM chat ORDER BY updated_at DESC LIMIT 4000")
    for (blob,) in cur:
        try:
            d = json.loads(blob)
        except Exception:
            continue

        buckets = []
        if isinstance(d.get("messages"), list):
            buckets.append(d["messages"])
        h = d.get("history")
        if isinstance(h, dict) and isinstance(h.get("messages"), dict):
            buckets.append(list(h["messages"].values()))

        for msgs in buckets:
            for m in msgs:
                if not isinstance(m, dict):
                    continue

                docs = []
                if isinstance(m.get("sources"), list):
                    for s in m["sources"]:
                        if isinstance(s, dict) and isinstance(s.get("document"), list):
                            for dd in s["document"]:
                                if isinstance(dd, str):
                                    docs.append(dd)
                                    doc_lens.append(len(dd))

                for key in ("output", "output_items"):
                    ov = m.get(key)
                    if not isinstance(ov, list):
                        continue
                    for it in ov:
                        if not isinstance(it, dict):
                            continue
                        if it.get("type") != "function_call_output":
                            continue
                        n = item_text_len(it)
                        fco.append(n)
                        nm = it.get("name") or "(no name)"
                        fco_names[nm] += 1
                        if n > 30000:
                            big_names[nm] += 1
                        # 重叠判定：拿 fco 正文的一段指纹去 docs 里找
                        if n > 2000 and docs:
                            o = it.get("output")
                            probe = o if isinstance(o, str) else None
                            if probe:
                                checked_overlap += 1
                                frag = probe[500:800]
                                if frag and any(frag in dd for dd in docs):
                                    overlap += 1

    fco.sort()
    doc_lens.sort()
    print(f"=== function_call_output: {len(fco)} 条 ===")
    if fco:
        print(f"  max={fco[-1]} p50={pct(fco,50)} p95={pct(fco,95)} p99={pct(fco,99)}")
        for t in (2000, 10000, 30000, 100000):
            n = sum(1 for v in fco if v > t)
            print(f"  > {t}: {n} 条 ({100.0*n/len(fco):.2f}%)")
    print()
    print(f"=== sources[].document: {len(doc_lens)} 条 ===")
    if doc_lens:
        print(f"  max={doc_lens[-1]} p95={pct(doc_lens,95)}")
        print(f"  > 30000: {sum(1 for v in doc_lens if v > 30000)} 条")
    print()
    print(f"=== 重叠判定：抽查 {checked_overlap} 条 fco，其正文出现在同消息 sources 里的 {overlap} 条 ===")
    print()
    print("=== 超过 30000 的是哪些工具 ===")
    for nm, n in big_names.most_common(10):
        print(f"  {nm}: {n} 条 (该工具总共 {fco_names[nm]} 条)")
    if not big_names:
        print("  (无)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
