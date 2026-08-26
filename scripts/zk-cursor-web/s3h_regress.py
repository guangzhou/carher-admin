#!/usr/bin/env python3
"""s3h_regress.py — tr-vis + th-pulse 上线后的全量回归(198 上跑)

覆盖:两 lane(直连名确定性路由)× {chat(hi) / sort(信封冻结形状) / 工具链 ls(r1+大结果r2)}
每轮验四件:①completed 恰好 1 次 ②思考 item 开了必收口 ③交付物不空心(r2: goods|digest)
④脉冲时序(首脉冲早于首正文,若有脉冲)。产出 /home/cltx/s3h_summary.json
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

LANES = {"101": "cursor-web-fc-terra", "82": "cursor-web-fc-82-terra"}


def stream_round(base, key, body):
    t0 = time.time()
    r = {"completed": 0, "text": "", "calls": [], "pulses": 0, "rs_added": 0, "rs_done": 0,
         "first_pulse_t": None, "first_text_t": None, "latency_s": None, "err": None}
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
            now = round(time.time() - t0, 1)
            if t == "response.reasoning_summary_text.delta":
                r["pulses"] += 1
                if r["first_pulse_t"] is None:
                    r["first_pulse_t"] = now
            elif t == "response.output_text.delta":
                if r["first_text_t"] is None:
                    r["first_text_t"] = now
            elif t == "response.output_item.added":
                if (ev.get("item") or {}).get("type") == "reasoning":
                    r["rs_added"] += 1
            elif t == "response.output_item.done":
                it = ev.get("item") or {}
                if it.get("type") == "reasoning":
                    r["rs_done"] += 1
                elif it.get("type") == "message":
                    r["text"] += "".join(c.get("text", "") for c in (it.get("content") or [])
                                         if isinstance(c, dict))
                elif it.get("type") in ("function_call", "custom_tool_call"):
                    r["calls"].append({k: it.get(k) for k in
                                       ("type", "id", "call_id", "name", "arguments",
                                        "input", "status")})
            elif t == "response.completed":
                r["completed"] += 1
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        r["err"] = f"{type(e).__name__}: {e}"
    r["latency_s"] = round(time.time() - t0, 1)
    return r


def sanity(r, need_deliverable):
    fails = []
    if r["err"]:
        fails.append(f"transport:{r['err']}")
    if r["completed"] != 1:
        fails.append(f"completed×{r['completed']}")
    if r["rs_added"] != r["rs_done"]:
        fails.append(f"rs_unclosed({r['rs_added']}/{r['rs_done']})")
    if r["first_pulse_t"] is not None and r["first_text_t"] is not None \
            and r["first_pulse_t"] > r["first_text_t"]:
        fails.append("pulse_after_text")
    if need_deliverable == "text" and not (r["text"].strip() or r["calls"]):
        fails.append("empty_deliverable")
    if need_deliverable == "goods":
        t = r["text"]
        hits = sum(1 for n in NAMES if n.split("/", 1)[1] in t or n in t)
        if "网关代呈" in t:
            pass  # digest_served = 合格
        elif hits < 3:
            fails.append(f"hollow(hits={hits},len={len(t.strip())})")
    return fails


def main():
    base, mk = s3.proxy(), s3.master_key()
    report = {}
    for lane, model in LANES.items():
        alias = f"s3h-{lane}-{int(time.time())}"
        kr = json.loads(s3.post(base, mk, "/key/generate",
                                {"models": [model], "duration": "1h",
                                 "key_alias": alias}).read())
        key = kr["key"]
        rows = []
        try:
            for fx, label, need in (("hi", "chat", "text"), ("sort", "sort", "text")):
                d = s3.load_fix(fx, "")
                d["model"] = model
                r = stream_round(base, key, d)
                fails = sanity(r, need)
                rows.append({"round": label, "ok": not fails, "fails": fails,
                             "pulses": r["pulses"], "fp": r["first_pulse_t"],
                             "ft": r["first_text_t"], "lat": r["latency_s"],
                             "text_len": len(r["text"].strip()), "calls": len(r["calls"])})
                print(f"[{lane}/{label}] {'OK' if not fails else 'FAIL ' + ','.join(fails)} "
                      f"pulses={r['pulses']} fp={r['first_pulse_t']} ft={r['first_text_t']} "
                      f"{r['latency_s']}s", flush=True)
                time.sleep(3)
            # 工具链
            d = s3.load_fix("ls", "")
            d["model"] = model
            r1 = stream_round(base, key, d)
            f1 = sanity(r1, "text")
            if not r1["calls"]:
                f1.append("r1_no_call")
            rows.append({"round": "tool.r1", "ok": not f1, "fails": f1,
                         "pulses": r1["pulses"], "lat": r1["latency_s"]})
            print(f"[{lane}/tool.r1] {'OK' if not f1 else 'FAIL ' + ','.join(f1)} "
                  f"pulses={r1['pulses']} {r1['latency_s']}s", flush=True)
            if r1["calls"]:
                c = r1["calls"][0]
                call_item = {k: v for k, v in c.items() if v is not None}
                out_type = ("custom_tool_call_output" if c["type"] == "custom_tool_call"
                            else "function_call_output")
                d2 = s3.load_fix("ls", "")
                d2["model"] = model
                d2["input"] = d["input"] + [call_item,
                                            {"type": out_type, "call_id": c.get("call_id"),
                                             "output": BIG_LS}]
                r2 = stream_round(base, key, d2)
                f2 = sanity(r2, "goods")
                rows.append({"round": "tool.r2", "ok": not f2, "fails": f2,
                             "pulses": r2["pulses"], "lat": r2["latency_s"],
                             "text_len": len(r2["text"].strip())})
                print(f"[{lane}/tool.r2] {'OK' if not f2 else 'FAIL ' + ','.join(f2)} "
                      f"pulses={r2['pulses']} len={len(r2['text'].strip())} "
                      f"{r2['latency_s']}s", flush=True)
        finally:
            s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
        report[lane] = rows
        time.sleep(2)

    all_ok = all(row["ok"] for rows in report.values() for row in rows)
    summary = {"ts": time.strftime("%Y-%m-%d %H:%M"), "verdict": "PASS" if all_ok else "FAIL",
               "lanes": report}
    with open("/home/cltx/s3h_summary.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"VERDICT: {summary['verdict']}", flush=True)


if __name__ == "__main__":
    main()
