#!/usr/bin/env python3
"""s3e_probe.py — P-e:可见性指令注入位置 A/B(198 上跑)

P-d 已证:control 6/6 空心;instructions 层契约几乎无效(1/6)。
P-e 测真实修复缝:指令贴着工具结果注入(模拟 responses.js [TOOL RESULT] 包装层改法)。
arm=wrap:结果尾部附注;arm=wrap_ins:包装附注+instructions 契约双保险。各 6 轮,钉 101。
"""
import importlib.util
import json
import time
import urllib.error

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)
import random  # noqa: E402

random.seed(20260826)
NAMES = []
for i in range(200):
    kind = random.choice(["src", "docs", "scripts", "tests", "k8s"])
    NAMES.append(f"{kind}/file_{i:03d}.{random.choice(['py','md','sh','yaml','js'])}")
BIG_LS = "total 480\n" + "\n".join(
    f"-rw-r--r--  1 dev dev {random.randint(200,99999):6d} Aug {random.randint(1,26):2d} 0{random.randint(1,9)}:{random.randint(10,59)} {n}"
    for n in NAMES)

WRAP_NOTE = ("\n\n[IMPORTANT: the user CANNOT see this tool output. In your answer you MUST "
             "present the essential contents yourself (actual names/values, grouped or "
             "summarized). Never say the output is shown above / 如上 / 已查看.]")
INS_CONTRACT = ("\n\nCRITICAL: The user CANNOT see tool outputs. After a tool result comes back, "
                "your final answer MUST present the essential content itself. NEVER refer to it "
                "as shown above / 如上 / 已查看.")


def judge(text):
    t = (text or "").strip()
    hits = sum(1 for n in NAMES if n.split("/", 1)[1] in t or n in t)
    if hits >= 3:
        return "goods", hits
    hollow_markers = ["如上", "已查看", "已列出", "已经列出", "上面", "已完成查看", "已成功", "已经查看"]
    if any(m in t for m in hollow_markers) or len(t) < 100:
        return "hollow", hits
    return "other", hits


def run_full(base, key, body):
    t0 = time.time()
    rec = {"completed": False, "failed": False, "http_error": None, "text": "", "calls": [],
           "latency_s": None}
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
            t = ev.get("type", "")
            if t == "response.output_item.done":
                it = ev.get("item") or {}
                if it.get("type") == "message":
                    rec["text"] += "".join(cc.get("text", "") for cc in (it.get("content") or [])
                                           if isinstance(cc, dict))
                elif it.get("type") in ("function_call", "custom_tool_call"):
                    rec["calls"].append({kk: it.get(kk) for kk in
                                         ("type", "id", "call_id", "name", "arguments",
                                          "input", "status")})
            elif t == "response.completed":
                rec["completed"] = True
            elif t in ("response.failed", "error"):
                rec["failed"] = True
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        rec["http_error"] = f"{type(e).__name__}: {e}"
    rec["latency_s"] = round(time.time() - t0, 1)
    return rec


def main():
    base, mk = s3.proxy(), s3.master_key()
    key = None
    for attempt in range(6):
        alias = f"s3e-{int(time.time())}"
        r = json.loads(s3.post(base, mk, "/key/generate",
                               {"models": [s3.MODEL], "duration": "2h", "key_alias": alias}).read())
        k = r["key"]
        s3.run_request(base, k, s3.load_fix("hi", ""))
        time.sleep(4)
        lane = "?"
        for ln, dep in (("101", "zero-cursor-bpi"), ("82", "zero-cursor-bpi-82")):
            c = s3.sh(f"kubectl -n {s3.NS} logs deploy/{dep} --since=60s 2>/dev/null | grep -c REQ || true")
            if c and int(c or 0) > 0:
                lane = ln
                break
        print(f"[pin] {attempt+1}: lane={lane}", flush=True)
        if lane == "101":
            key = k
            break
        s3.post(base, mk, "/key/delete", {"keys": [k]}).read()
    if not key:
        raise SystemExit("cannot pin 101")

    results = []
    try:
        for arm, ins_extra, out_note in (("wrap", "", WRAP_NOTE),
                                         ("wrap_ins", INS_CONTRACT, WRAP_NOTE)):
            for i in range(6):
                fx = ["task", "ls"][i % 2]
                d = s3.load_fix(fx, ins_extra)
                rec1 = run_full(base, key, d)
                if not rec1["calls"]:
                    results.append({"arm": arm, "n": i + 1, "fixture": fx,
                                    "verdict": "r1_no_call", "lat": rec1["latency_s"]})
                    print(f"[{arm} {i+1}] {fx} -> r1_no_call", flush=True)
                    time.sleep(3)
                    continue
                c = rec1["calls"][0]
                call_item = {k2: v for k2, v in c.items() if v is not None}
                out_type = ("custom_tool_call_output" if c["type"] == "custom_tool_call"
                            else "function_call_output")
                out_item = {"type": out_type, "call_id": c.get("call_id"),
                            "output": BIG_LS + out_note}
                d2 = s3.load_fix(fx, ins_extra)
                d2["input"] = d["input"] + [call_item, out_item]
                rec2 = run_full(base, key, d2)
                v, hits = judge(rec2["text"])
                row = {"arm": arm, "n": i + 1, "fixture": fx, "verdict": v, "name_hits": hits,
                       "text_len": len((rec2["text"] or "").strip()),
                       "text_head": (rec2["text"] or "")[:90],
                       "lat1": rec1["latency_s"], "lat2": rec2["latency_s"]}
                results.append(row)
                print(f"[{arm} {i+1}] {fx} -> {v} hits={hits} len={row['text_len']} "
                      f"lat={rec1['latency_s']}+{rec2['latency_s']}s", flush=True)
                time.sleep(3)
    finally:
        s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
        print("[key] deleted", flush=True)

    agg = {}
    for r in results:
        k2 = (r["arm"], r["verdict"])
        agg[k2] = agg.get(k2, 0) + 1
    out = {"ts": time.strftime("%Y-%m-%d %H:%M"),
           "by_arm": {f"{a}/{b}": c for (a, b), c in sorted(agg.items())}, "rows": results}
    with open("/home/cltx/s3e_summary.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps(out["by_arm"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
