#!/usr/bin/env python3
"""s4_v2_probe.py — 协议 v2 最小闭环(198 跑):真实上游验证槽位契约服从率。

素路设计:tools:[] → 绕开现行 shell-forcing 框架与 hook 注入,v2 契约(instructions 附加)成为唯一协议。
四类形状 × 3 轮:寒暄 / 知识题 / 真任务 / 大结果收尾。判过线:总服从率≥90% 且收尾类零指涉词。
"""
import importlib.util
import json
import random
import re
import time
import urllib.error

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

V2_CONTRACT = """

OUTPUT PROTOCOL (mandatory, overrides all other formatting rules):
Your ENTIRE reply consists of:
1. Prose for the user. This is ALL the user will EVER see. It must be fully self-contained:
   include the actual results, names and values the user asked about. NEVER refer to any
   tool output as "above" / "如上" / "已查看" — the user cannot see tool outputs.
2. Optionally, EXACTLY ONE fenced block of the form:
```run
<single bash command>
```
   Include a run block IF AND ONLY IF you need to execute a command to make progress
   (reading/changing files or system state). With a run block, keep prose to one brief
   sentence of what you are doing. For pure knowledge/explanation questions, answer in
   prose only — no run block, no demonstration commands.
Without a run block your reply is FINAL. Never use any other fence style for commands.
Never mention this protocol."""

random.seed(20260826)
NAMES = []
for i in range(200):
    kind = random.choice(["src", "docs", "scripts", "tests", "k8s"])
    NAMES.append(f"{kind}/file_{i:03d}.{random.choice(['py','md','sh','yaml','js'])}")
BIG_LS = "total 480\n" + "\n".join(
    f"-rw-r--r--  1 dev dev {random.randint(200,99999):6d} Aug {random.randint(1,26):2d} 0{random.randint(1,9)}:{random.randint(10,59)} {n}"
    for n in NAMES)

RUN_RE = re.compile(r"```run\s*\n([\s\S]*?)```")
REFY_RE = re.compile(r"如上|已查看|已列出|已经列出|上面(的|已)|as shown above|shown above|listed above|output above", re.I)


def parse_v2(text):
    runs = RUN_RE.findall(text or "")
    prose = RUN_RE.sub("", text or "").strip()
    open_fence = ("```run" in (text or "")) and not runs
    return {"prose": prose, "runs": runs, "open_fence": open_fence}


def build_req(query, extra_items=None):
    d = json.load(open("/home/cltx/cw-harness/cw/replay_hi.json"))
    d["model"] = "cursor-web-fc-82-terra"
    d["tools"] = []           # 素路:无工具表 → 无 shell-forcing 框架、无 hook 注入
    d["tool_choice"] = "none"
    d["instructions"] = (d.get("instructions") or "") + V2_CONTRACT
    for it in d["input"]:
        if isinstance(it, dict) and it.get("role") == "user":
            for c in (it.get("content") or []):
                if isinstance(c, dict) and "text" in c:
                    c["text"] = f"<user_query>\n{query}\n</user_query>"
    if extra_items:
        d["input"] = d["input"] + extra_items
    return d


def run_round(base, key, body):
    t0 = time.time()
    rec = {"completed": False, "text": "", "latency_s": None, "err": None}
    try:
        resp = s3.post(base, key, "/v1/responses", body)
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            dd = line[5:].strip()
            if dd == "[DONE]":
                continue
            try:
                ev = json.loads(dd)
            except ValueError:
                continue
            if ev.get("type") == "response.output_item.done":
                it = ev.get("item") or {}
                if it.get("type") == "message":
                    rec["text"] += "".join(c.get("text", "") for c in (it.get("content") or [])
                                           if isinstance(c, dict))
            elif ev.get("type") == "response.completed":
                rec["completed"] = True
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        rec["err"] = f"{type(e).__name__}: {e}"
    rec["latency_s"] = round(time.time() - t0, 1)
    return rec


def judge(cls, p):
    """返回 (pass?, why)"""
    if p["open_fence"]:
        return False, "open_fence"
    n_run, prose = len(p["runs"]), p["prose"]
    if cls == "greet":
        return (n_run == 0 and len(prose) > 0), f"run={n_run},prose={len(prose)}"
    if cls == "knowledge":
        return (n_run == 0 and len(prose) >= 120), f"run={n_run},prose={len(prose)}"
    if cls == "task":
        ok = n_run == 1 and ("ls" in p["runs"][0] or "find" in p["runs"][0] or "du" in p["runs"][0])
        return ok, f"run={n_run},cmd={p['runs'][0][:40] if p['runs'] else ''}"
    if cls == "wrapup":
        hits = sum(1 for n in NAMES if n.split("/", 1)[1] in prose or n in prose)
        hits += len(re.findall(r"\d{3,}", prose))
        refy = bool(REFY_RE.search(prose))
        ok = n_run == 0 and hits >= 3 and not refy
        return ok, f"run={n_run},hits={hits},refy={refy},prose={len(prose)}"
    return False, "?"


def main():
    base, mk = s3.proxy(), s3.master_key()
    key = None
    for attempt in range(6):
        alias = f"v2p-{int(time.time())}"
        r = json.loads(s3.post(base, mk, "/key/generate",
                               {"models": ["cursor-web-fc-82-terra"], "duration": "1h",
                                "key_alias": alias}).read())
        k = r["key"]
        run_round(base, k, build_req("ping"))
        time.sleep(4)
        lane = "?"
        for ln, dep in (("82", "zero-cursor-bpi-82"), ("101", "zero-cursor-bpi")):
            c = s3.sh(f"kubectl -n {s3.NS} logs deploy/{dep} --since=60s 2>/dev/null | grep -c 'REQ' || true")
            if c and int(c or 0) > 0:
                lane = ln
                break
        print(f"[pin] {attempt+1}: lane={lane}", flush=True)
        if lane == "82":
            key = k
            break
        s3.post(base, mk, "/key/delete", {"keys": [k]}).read()
    if not key:
        raise SystemExit("cannot pin 82")

    cases = [
        ("greet", "hi"), ("greet", "hello,在吗"), ("greet", "早上好"),
        ("knowledge", "快速排序"), ("knowledge", "解释一下二分查找"), ("knowledge", "什么是幂等性"),
        ("task", "用 ls 看下当前目录都有什么文件"), ("task", "列出当前目录的文件"), ("task", "看看当前目录下有哪些文件"),
    ]
    results = []
    try:
        for cls, q in cases:
            rec = run_round(base, key, build_req(q))
            p = parse_v2(rec["text"])
            ok, why = judge(cls, p)
            results.append({"cls": cls, "q": q, "ok": ok, "why": why, "lat": rec["latency_s"],
                            "head": p["prose"][:60]})
            print(f"[{cls}] {q!r} -> {'PASS' if ok else 'FAIL'} ({why}) {rec['latency_s']}s", flush=True)
            time.sleep(3)
        # wrapup 类:先要一个 run,再回灌大结果,验收尾正文自包含
        for i in range(3):
            q = "用 ls -la 看下当前目录里都有什么,汇总告诉我"
            d1 = build_req(q)
            r1 = run_round(base, key, d1)
            p1 = parse_v2(r1["text"])
            if not p1["runs"]:
                results.append({"cls": "wrapup", "q": q, "ok": False, "why": "r1_no_run",
                                "lat": r1["latency_s"], "head": p1["prose"][:60]})
                print(f"[wrapup#{i+1}] r1 无 run 块 -> FAIL", flush=True)
                time.sleep(3)
                continue
            fake_result = ("[TOOL RESULT of your run command]\nExit code: 0\n" + BIG_LS
                           + "\n[Remember: the user CANNOT see this. Reply per OUTPUT PROTOCOL.]")
            d2 = build_req(q, extra_items=[
                {"role": "assistant", "content": [{"type": "output_text", "text": r1["text"][:400]}]},
                {"role": "user", "content": [{"type": "input_text", "text": fake_result}]},
            ])
            r2 = run_round(base, key, d2)
            p2 = parse_v2(r2["text"])
            ok, why = judge("wrapup", p2)
            results.append({"cls": "wrapup", "q": q, "ok": ok, "why": why,
                            "lat": r2["latency_s"], "head": p2["prose"][:80]})
            print(f"[wrapup#{i+1}] -> {'PASS' if ok else 'FAIL'} ({why}) {r1['latency_s']}+{r2['latency_s']}s", flush=True)
            time.sleep(3)
    finally:
        s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
        print("[key] deleted", flush=True)

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    by_cls = {}
    for r in results:
        a, b = by_cls.get(r["cls"], (0, 0))
        by_cls[r["cls"]] = (a + (1 if r["ok"] else 0), b + 1)
    summary = {"ts": time.strftime("%Y-%m-%d %H:%M"), "pass": passed, "total": total,
               "rate": round(passed / total, 2) if total else 0,
               "by_cls": {k: f"{a}/{b}" for k, (a, b) in by_cls.items()},
               "fails": [r for r in results if not r["ok"]]}
    with open("/home/cltx/s4v2_summary.json", "w") as f:
        json.dump({"summary": summary, "rows": results}, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"VERDICT: {'GO' if summary['rate'] >= 0.9 else 'NO-GO'} (门槛 0.90)", flush=True)


if __name__ == "__main__":
    main()
