#!/usr/bin/env python3
"""s3f_validate.py — 金丝雀 82 验收:ZK_TR_VIS=1 后,探针不带任何契约,验证结果可见性由网关保证。
判:goods(模型自己复述,含≥3真实文件名)| digest_served(模型空心但网关代呈摘要,用户仍看到货)
   | hollow_leak(既没复述也没代呈=闸没生效)| r1_no_call
"""
import importlib.util
import json
import random
import time
import urllib.error

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

random.seed(20260826)
NAMES = []
for i in range(200):
    kind = random.choice(["src", "docs", "scripts", "tests", "k8s"])
    NAMES.append(f"{kind}/file_{i:03d}.{random.choice(['py','md','sh','yaml','js'])}")
BIG_LS = "total 480\n" + "\n".join(
    f"-rw-r--r--  1 dev dev {random.randint(200,99999):6d} Aug {random.randint(1,26):2d} 0{random.randint(1,9)}:{random.randint(10,59)} {n}"
    for n in NAMES)


def judge(text):
    t = (text or "").strip()
    hits = sum(1 for n in NAMES if n.split("/", 1)[1] in t or n in t)
    if "网关代呈" in t:
        return "digest_served", hits
    if hits >= 3:
        return "goods", hits
    return "hollow_leak", hits


def run_full(base, key, body):
    t0 = time.time()
    rec = {"completed": False, "text": "", "calls": [], "latency_s": None, "http_error": None}
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
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        rec["http_error"] = f"{type(e).__name__}: {e}"
    rec["latency_s"] = round(time.time() - t0, 1)
    return rec


def main():
    base, mk = s3.proxy(), s3.master_key()
    key = None
    for attempt in range(8):
        alias = f"s3f-{int(time.time())}"
        r = json.loads(s3.post(base, mk, "/key/generate",
                               {"models": [s3.MODEL], "duration": "1h", "key_alias": alias}).read())
        k = r["key"]
        s3.run_request(base, k, s3.load_fix("hi", ""))
        time.sleep(4)
        lane = "?"
        for ln, dep in (("82", "zero-cursor-bpi-82"), ("101", "zero-cursor-bpi")):
            c = s3.sh(f"kubectl -n {s3.NS} logs deploy/{dep} --since=60s 2>/dev/null | grep -c REQ || true")
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

    results = []
    try:
        for i in range(6):
            fx = ["task", "ls"][i % 2]
            d = s3.load_fix(fx, "")
            rec1 = run_full(base, key, d)
            if not rec1["calls"]:
                results.append({"n": i + 1, "fixture": fx, "verdict": "r1_no_call"})
                print(f"[{i+1}] {fx} -> r1_no_call", flush=True)
                time.sleep(3)
                continue
            c = rec1["calls"][0]
            call_item = {k2: v for k2, v in c.items() if v is not None}
            out_type = ("custom_tool_call_output" if c["type"] == "custom_tool_call"
                        else "function_call_output")
            d2 = s3.load_fix(fx, "")
            d2["input"] = d["input"] + [call_item,
                                        {"type": out_type, "call_id": c.get("call_id"),
                                         "output": BIG_LS}]
            rec2 = run_full(base, key, d2)
            v, hits = judge(rec2["text"])
            row = {"n": i + 1, "fixture": fx, "verdict": v, "name_hits": hits,
                   "text_len": len((rec2["text"] or "").strip()),
                   "text_head": (rec2["text"] or "")[:90],
                   "lat1": rec1["latency_s"], "lat2": rec2["latency_s"]}
            results.append(row)
            print(f"[{i+1}] {fx} -> {v} hits={hits} len={row['text_len']} "
                  f"lat={rec1['latency_s']}+{rec2['latency_s']}s", flush=True)
            time.sleep(3)
    finally:
        s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
        print("[key] deleted", flush=True)

    agg = {}
    for r in results:
        agg[r["verdict"]] = agg.get(r["verdict"], 0) + 1
    print(json.dumps(agg, ensure_ascii=False), flush=True)
    with open("/home/cltx/s3f_summary.json", "w") as f:
        json.dump({"agg": agg, "rows": results}, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
