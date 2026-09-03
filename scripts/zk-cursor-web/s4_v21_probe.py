#!/usr/bin/env python3
"""s4_v21_probe.py — v2.1 最小闭环:命令槽方言改 ⟦cmd¦run=...⟧(模型在 acct82 已内化的方言),
双 lane 复测(82=顺水推舟,101=方言可教性)。判过线同 v2.0:≥90%。"""
import importlib.util
import json
import re
import time

spec = importlib.util.spec_from_file_location("p", "/home/cltx/s4_v2_probe.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

CONTRACT = p.V2_CONTRACT.replace(
    "2. Optionally, EXACTLY ONE fenced block of the form:\n```run\n<single bash command>\n```",
    "2. Optionally, EXACTLY ONE command block of the form: ⟦cmd¦run=<single bash command>⟧"
).replace(
    "Without a run block your reply is FINAL. Never use any other fence style for commands.",
    "Without a command block your reply is FINAL. Never use any other syntax for commands.")
RUN2 = re.compile("⟦cmd¦run=([\\s\\S]*?)⟧")


def parse2(text):
    runs = RUN2.findall(text or "")
    prose = RUN2.sub("", text or "").strip()
    return {"prose": prose, "runs": runs,
            "open_fence": ("⟦cmd" in (text or "")) and not runs}


def req(query, model, extra_items=None):
    d = p.build_req(query, extra_items=extra_items)
    d["model"] = model
    d["instructions"] = d["instructions"].replace(p.V2_CONTRACT, CONTRACT)
    return d


def main():
    base, mk = p.s3.proxy(), p.s3.master_key()
    results = []
    for lane, model in (("82", "cursor-web-fc-82-terra"), ("101", "cursor-web-fc-terra")):
        r = json.loads(p.s3.post(base, mk, "/key/generate",
                                 {"models": [model], "duration": "1h",
                                  "key_alias": f"v21-{lane}-{int(time.time())}"}).read())
        key = r["key"]
        try:
            for cls, q in (("greet", "hi"), ("knowledge", "快速排序"),
                           ("task", "用 ls 看下当前目录都有什么文件"),
                           ("task", "列出当前目录的文件")):
                rec = p.run_round(base, key, req(q, model))
                pr = parse2(rec["text"])
                ok, why = p.judge(cls, pr)
                results.append({"lane": lane, "cls": cls, "ok": ok, "why": why})
                verdict = "PASS" if ok else "FAIL"
                print(f"[{lane}/{cls}] {q!r} -> {verdict} ({why}) {rec['latency_s']}s", flush=True)
                time.sleep(3)
            for i in range(2):
                q = "用 ls -la 看下当前目录里都有什么,汇总告诉我"
                r1 = p.run_round(base, key, req(q, model))
                p1 = parse2(r1["text"])
                if not p1["runs"]:
                    results.append({"lane": lane, "cls": "wrapup", "ok": False, "why": "r1_no_run"})
                    head = p1["prose"][:50]
                    print(f"[{lane}/wrapup#{i+1}] r1 no cmd block -> FAIL head={head!r}", flush=True)
                    time.sleep(3)
                    continue
                fake = ("[TOOL RESULT of your command]\nExit code: 0\n" + p.BIG_LS
                        + "\n[Remember: the user CANNOT see this. Reply per OUTPUT PROTOCOL.]")
                d2 = req(q, model, extra_items=[
                    {"role": "assistant", "content": [{"type": "output_text", "text": r1["text"][:400]}]},
                    {"role": "user", "content": [{"type": "input_text", "text": fake}]}])
                r2 = p.run_round(base, key, d2)
                p2 = parse2(r2["text"])
                ok, why = p.judge("wrapup", p2)
                results.append({"lane": lane, "cls": "wrapup", "ok": ok, "why": why})
                verdict = "PASS" if ok else "FAIL"
                print(f"[{lane}/wrapup#{i+1}] -> {verdict} ({why}) {r1['latency_s']}+{r2['latency_s']}s", flush=True)
                time.sleep(3)
        finally:
            p.s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
    tot = len(results)
    ps = sum(1 for r in results if r["ok"])
    by = {}
    for r in results:
        k = f"{r['lane']}/{r['cls']}"
        a, b = by.get(k, (0, 0))
        by[k] = (a + (1 if r["ok"] else 0), b + 1)
    summary = {"pass": ps, "total": tot, "rate": round(ps / tot, 2),
               "by": {k: f"{a}/{b}" for k, (a, b) in by.items()},
               "fails": [r for r in results if not r["ok"]]}
    with open("/home/cltx/s4v21_summary.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    print("VERDICT:", "GO" if ps / tot >= 0.9 else "NO-GO", flush=True)


if __name__ == "__main__":
    main()
