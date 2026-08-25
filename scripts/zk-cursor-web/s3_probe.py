#!/usr/bin/env python3
"""s3_probe.py — S3 双探针:模型对协议两个承重点的服从率(198 上跑)

P-a 趟内单一产物(设计原则1承重点):tool 类 fixture + 输出契约「整个回复=一个 ```json 信封 或 单词 DONE」
    判:pure_envelope(calls>0 且 text=0)| pure_done(text=="DONE")| mixed(calls>0 且 text>0)| prose_miss
P-b 终止记号(设计原则5承重点):chat 类 fixture + 「最后一行必须是 [END-OF-ANSWER]」
    判:end_ok(text 以记号收尾)| end_miss
每探针 20 轮,临时 key 钉 82,产出 /home/cltx/s3_rounds.jsonl + /home/cltx/s3_summary.json
"""
import base64
import json
import subprocess
import time
import urllib.request
import urllib.error

NS = "litellm-product"
MODEL = "cursor-g-5.6-sol"
FIXDIR = "/home/cltx/cw-harness/cw"
ROUNDS_LOG = "/home/cltx/s3_rounds.jsonl"
SUMMARY = "/home/cltx/s3_summary.json"
PA_FIX = ["task", "ls"]
PB_FIX = ["sort", "weather", "hello", "hi"]
PA_CONTRACT = ("\n\nCRITICAL OUTPUT CONTRACT (overrides all other formatting rules): "
               "Your ENTIRE reply must be EXACTLY ONE fenced ```json code block of the form "
               '{"tool_calls":[{"name":"...","arguments":{...}}]} and NOTHING else — '
               "no prose before or after. ONLY if the task requires no tool at all, reply with "
               "the single word DONE instead. Never mix prose with the block.")
PB_CONTRACT = ("\n\nCRITICAL OUTPUT CONTRACT: The very last line of your reply must be exactly "
               "[END-OF-ANSWER] on its own line. Always append it after your final answer.")
END_MARK = "[END-OF-ANSWER]"


def sh(cmd, timeout=60):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def master_key():
    data = json.loads(sh(f"kubectl -n {NS} get secret litellm-secrets -o json"))["data"]
    for k, v in data.items():
        if "MASTER_KEY" in k:
            return base64.b64decode(v).decode()
    raise SystemExit("no master key")


def proxy():
    ip = sh(f"kubectl -n {NS} get svc litellm-proxy -o jsonpath='{{.spec.clusterIP}}'")
    return f"http://{ip}:4000"


def post(base, key, path, body, timeout=240):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def run_request(base, key, body):
    t0 = time.time()
    rec = {"completed": False, "failed": False, "http_error": None,
           "text": "", "calls": [], "latency_s": None}
    try:
        resp = post(base, key, "/v1/responses", body)
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                continue
            try:
                ev = json.loads(d)
            except ValueError:
                continue
            t = ev.get("type", "")
            if t == "response.output_item.done":
                it = ev.get("item") or {}
                if it.get("type") == "message":
                    rec["text"] += "".join(c.get("text", "") for c in (it.get("content") or [])
                                           if isinstance(c, dict))
                elif it.get("type") in ("function_call", "custom_tool_call"):
                    rec["calls"].append({"name": it.get("name")})
            elif t == "response.completed":
                rec["completed"] = True
            elif t in ("response.failed", "error"):
                rec["failed"] = True
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        rec["http_error"] = f"{type(e).__name__}: {e}"
    rec["latency_s"] = round(time.time() - t0, 1)
    return rec


def load_fix(name, extra_instructions):
    with open(f"{FIXDIR}/replay_{name}.json") as f:
        d = json.load(f)
    d["model"] = MODEL
    d["instructions"] = (d.get("instructions") or "") + extra_instructions
    return d


def pa_verdict(rec):
    txt = (rec["text"] or "").strip()
    if rec["failed"] or rec["http_error"]:
        return "transport_fail"
    if not rec["completed"]:
        return "no_completed"
    if rec["calls"] and not txt:
        return "pure_envelope"
    if txt == "DONE":
        return "pure_done"
    if rec["calls"] and txt:
        return "mixed"
    return "prose_miss"


def pb_verdict(rec):
    txt = (rec["text"] or "").rstrip()
    if rec["failed"] or rec["http_error"]:
        return "transport_fail"
    if not rec["completed"]:
        return "no_completed"
    return "end_ok" if txt.endswith(END_MARK) else "end_miss"


def main():
    print("[start] S3 probes", flush=True)
    base, mk = proxy(), master_key()
    log = open(ROUNDS_LOG, "a")

    key, alias, lane = None, None, "?"
    for attempt in range(5):
        alias_try = f"s3-probe-{int(time.time())}"
        r = json.loads(post(base, mk, "/key/generate",
                            {"models": [MODEL], "duration": "3h", "key_alias": alias_try}).read())
        k = r["key"]
        run_request(base, k, load_fix("hi", ""))
        time.sleep(4)
        lane_now = "?"
        for ln, dep in (("82", "zero-cursor-bpi-82"), ("101", "zero-cursor-bpi")):
            c = sh(f"kubectl -n {NS} logs deploy/{dep} --since=60s 2>/dev/null | grep -c 'REQ' || true")
            if c and int(c or 0) > 0:
                lane_now = ln
                break
        print(f"[pin] attempt {attempt+1}: {alias_try} lane={lane_now}", flush=True)
        if lane_now == "82":
            key, alias, lane = k, alias_try, lane_now
            break
        post(base, mk, "/key/delete", {"keys": [k]}).read()
    if not key:
        raise SystemExit("cannot pin lane 82 after 5 attempts")

    results = []
    try:
        for i in range(20):
            fx = PA_FIX[i % len(PA_FIX)]
            rec = run_request(base, key, load_fix(fx, PA_CONTRACT))
            v = pa_verdict(rec)
            row = {"probe": "P-a", "n": i + 1, "fixture": fx, "verdict": v,
                   "latency_s": rec["latency_s"], "calls": len(rec["calls"]),
                   "text_head": rec["text"][:70], "text_len": len(rec["text"].strip()),
                   "ts": time.strftime("%H:%M:%S")}
            results.append(row)
            log.write(json.dumps(row, ensure_ascii=False) + "\n")
            log.flush()
            print(f"[P-a {i+1:02d}] {fx} -> {v} {rec['latency_s']}s", flush=True)
            time.sleep(3)
        for i in range(20):
            fx = PB_FIX[i % len(PB_FIX)]
            rec = run_request(base, key, load_fix(fx, PB_CONTRACT))
            v = pb_verdict(rec)
            row = {"probe": "P-b", "n": i + 1, "fixture": fx, "verdict": v,
                   "latency_s": rec["latency_s"], "calls": len(rec["calls"]),
                   "text_tail": rec["text"].rstrip()[-50:], "text_len": len(rec["text"].strip()),
                   "ts": time.strftime("%H:%M:%S")}
            results.append(row)
            log.write(json.dumps(row, ensure_ascii=False) + "\n")
            log.flush()
            print(f"[P-b {i+1:02d}] {fx} -> {v} {rec['latency_s']}s", flush=True)
            time.sleep(3)
    finally:
        try:
            post(base, mk, "/key/delete", {"keys": [key]}).read()
            print(f"[key] {alias} deleted", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[key] DELETE FAILED manual cleanup: {alias}: {e}", flush=True)
        log.close()

    agg = {}
    for r in results:
        k2 = (r["probe"], r["verdict"])
        agg[k2] = agg.get(k2, 0) + 1
    summary = {"ts": time.strftime("%Y-%m-%d %H:%M"), "model": MODEL, "lane": lane,
               "by_probe_verdict": {f"{a}/{b}": c for (a, b), c in sorted(agg.items())},
               "violations": [r for r in results if r["verdict"] not in
                              ("pure_envelope", "pure_done", "end_ok")]}
    with open(SUMMARY, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary["by_probe_verdict"], ensure_ascii=False), flush=True)
    print(f"[out] {SUMMARY}", flush=True)


if __name__ == "__main__":
    main()
