#!/usr/bin/env python3
"""s3d_probe.py — P-d 探针:大工具结果(10KB)下"结果不上屏"复现 + 可见性契约 A/B(198 上跑)

病灶(2026-08-26 真实日志):工具结果 11KB 回灌后,模型回 199 字符空话("已查看/如上")。
A/B:control=现状;treat=instructions 附加可见性契约。
判:goods(答案含 ≥3 个列表内真实文件名) | hollow(空话引用/无文件名) | other
每臂 6 轮(fixture task/ls 轮换),临时 key,钉 101(用户真实 lane)。
"""
import importlib.util
import json
import random
import time

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

VIS_CONTRACT = ("\n\nCRITICAL: The user CANNOT see tool outputs — they are invisible to the user. "
                "After a tool result comes back, your final answer MUST present the essential "
                "content itself (actual file names, actual values, actual lists — summarized or "
                "grouped is fine). NEVER refer to the output as shown above / 如上 / 已查看. "
                "If the result is long, show the most useful subset and say how many were omitted.")

random.seed(20260826)
NAMES = []
for i in range(200):
    kind = random.choice(["src", "docs", "scripts", "tests", "k8s"])
    NAMES.append(f"{kind}/file_{i:03d}.{random.choice(['py','md','sh','yaml','js'])}")
BIG_LS = "total 480\n" + "\n".join(
    f"-rw-r--r--  1 dev dev {random.randint(200,99999):6d} Aug {random.randint(1,26):2d} 0{random.randint(1,9)}:{random.randint(10,59)} {n}"
    for n in NAMES)
assert len(BIG_LS) > 9000, len(BIG_LS)


def judge(text):
    t = (text or "").strip()
    hits = sum(1 for n in NAMES if n.split("/", 1)[1] in t or n in t)
    if hits >= 3:
        return "goods", hits
    hollow_markers = ["如上", "已查看", "已列出", "已经列出", "上面", "已完成查看", "已成功", "已经查看"]
    if any(m in t for m in hollow_markers) or len(t) < 100:
        return "hollow", hits
    return "other", hits


def main():
    base, mk = s3.proxy(), s3.master_key()
    key = None
    for attempt in range(6):
        alias = f"s3d-{int(time.time())}"
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
        for arm, extra in (("control", ""), ("treat", VIS_CONTRACT)):
            for i in range(6):
                fx = ["task", "ls"][i % 2]
                d = s3.load_fix(fx, extra)
                # r1:拿工具调用(需要完整 item 才能配对回灌)
                rec1 = run_full(base, key, d)
                if not rec1["calls"]:
                    row = {"arm": arm, "n": i + 1, "fixture": fx, "verdict": "r1_no_call",
                           "lat": rec1["latency_s"]}
                    results.append(row)
                    print(f"[{arm} {i+1}] {fx} -> r1_no_call", flush=True)
                    time.sleep(3)
                    continue
                c = rec1["calls"][0]
                call_item = {k2: v for k2, v in c.items() if v is not None}
                if c["type"] == "custom_tool_call":
                    out_item = {"type": "custom_tool_call_output", "call_id": c.get("call_id"),
                                "output": BIG_LS}
                else:
                    out_item = {"type": "function_call_output", "call_id": c.get("call_id"),
                                "output": BIG_LS}
                d2 = s3.load_fix(fx, extra)
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
    with open("/home/cltx/s3d_summary.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps(out["by_arm"], ensure_ascii=False), flush=True)


def run_full(base, key, body):
    """同 s3.run_request 但保留完整 call item 字段(配对回灌需要 arguments/input)"""
    import urllib.error
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


if __name__ == "__main__":
    main()
