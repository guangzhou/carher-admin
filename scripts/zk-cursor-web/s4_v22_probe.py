#!/usr/bin/env python3
"""s4_v22_probe.py — v2.2:补"禁用自带工具"禁令(101 的失败=模型用自己沙箱跑了命令),只重测 101。"""
import importlib.util
import json
import time

spec = importlib.util.spec_from_file_location("v21", "/home/cltx/s4_v21_probe.py")
v21 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v21)
p = v21.p

CONTRACT22 = v21.CONTRACT.replace(
    "Include a command block IF AND ONLY IF",
    "CRITICAL: you have NO working tools of your own here — do NOT use your python/analysis/"
    "container tools; anything you run there executes in an unrelated sandbox and its output is "
    "FAKE for this user. The ONLY way to execute anything on the user's machine is the "
    "⟦cmd¦run=...⟧ block. Include a command block IF AND ONLY IF"
) if "Include a command block IF AND ONLY IF" in v21.CONTRACT else (
    v21.CONTRACT + "\nCRITICAL: do NOT use your python/analysis/container tools — they run in an "
    "unrelated sandbox and their output is FAKE for this user. The ONLY way to execute anything "
    "on the user's machine is the ⟦cmd¦run=...⟧ block.")


def req(query, model, extra_items=None):
    d = p.build_req(query, extra_items=extra_items)
    d["model"] = model
    d["instructions"] = d["instructions"].replace(p.V2_CONTRACT, CONTRACT22)
    return d


def main():
    base, mk = p.s3.proxy(), p.s3.master_key()
    model = "cursor-web-fc-terra"
    results = []
    r = json.loads(p.s3.post(base, mk, "/key/generate",
                             {"models": [model], "duration": "1h",
                              "key_alias": f"v22-101-{int(time.time())}"}).read())
    key = r["key"]
    try:
        for cls, q in (("task", "用 ls 看下当前目录都有什么文件"),
                       ("task", "列出当前目录的文件"),
                       ("task", "看看当前目录下有哪些文件"),
                       ("knowledge", "快速排序")):
            rec = p.run_round(base, key, req(q, model))
            pr = v21.parse2(rec["text"])
            ok, why = p.judge(cls, pr)
            results.append({"cls": cls, "ok": ok, "why": why})
            verdict = "PASS" if ok else "FAIL"
            head = pr["prose"][:50]
            print(f"[101/{cls}] {q!r} -> {verdict} ({why}) {rec['latency_s']}s head={head!r}", flush=True)
            time.sleep(3)
        for i in range(3):
            q = "用 ls -la 看下当前目录里都有什么,汇总告诉我"
            r1 = p.run_round(base, key, req(q, model))
            p1 = v21.parse2(r1["text"])
            if not p1["runs"]:
                results.append({"cls": "wrapup", "ok": False, "why": "r1_no_run"})
                print(f"[101/wrapup#{i+1}] r1 no cmd -> FAIL head={p1['prose'][:50]!r}", flush=True)
                time.sleep(3)
                continue
            fake = ("[TOOL RESULT of your command]\nExit code: 0\n" + p.BIG_LS
                    + "\n[Remember: the user CANNOT see this. Reply per OUTPUT PROTOCOL.]")
            d2 = req(q, model, extra_items=[
                {"role": "assistant", "content": [{"type": "output_text", "text": r1["text"][:400]}]},
                {"role": "user", "content": [{"type": "input_text", "text": fake}]}])
            r2 = p.run_round(base, key, d2)
            p2 = v21.parse2(r2["text"])
            ok, why = p.judge("wrapup", p2)
            results.append({"cls": "wrapup", "ok": ok, "why": why})
            verdict = "PASS" if ok else "FAIL"
            print(f"[101/wrapup#{i+1}] -> {verdict} ({why})", flush=True)
            time.sleep(3)
    finally:
        p.s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
    tot = len(results)
    ps = sum(1 for x in results if x["ok"])
    print(json.dumps({"pass": ps, "total": tot, "rate": round(ps / tot, 2),
                      "fails": [x for x in results if not x["ok"]]}, ensure_ascii=False), flush=True)
    print("VERDICT-101:", "GO" if ps / tot >= 0.85 else "NO-GO", flush=True)


if __name__ == "__main__":
    main()
