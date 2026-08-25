#!/usr/bin/env python3
"""s1_baseline.py — cursor-g 网页线 S1 基线驱动器(在 198 上跑)

- 真实 Cursor 抓包 fixture 回放(/home/cltx/cw-harness/cw/replay_*.json,19 工具/8 items 原形状)
- 临时 scoped key(cursor-g-5.6-sol,用完即删);钉位探测:重生成 key 直到 WA 钉到 82 lane(最多5次)
- A 类聊天 ×10;B 类工具 ×10(round1 期待工具调用 → 伪造结果回灌 → round2 期待短答=r16 病灶形状)
- 三分法归类:complete | failed | half(0字符无调用/无completed/协议泄漏);短答(1-23字符)=合法,仅打 short 旗
- 产出:/home/cltx/s1_rounds.jsonl + /home/cltx/s1_summary.json
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
ROUNDS_LOG = "/home/cltx/s1_rounds.jsonl"
SUMMARY = "/home/cltx/s1_summary.json"
CHAT_FIX = ["hi", "hello", "sort", "weather"]
TOOL_FIX = ["ls", "ll", "task"]
FAKE_LS = "total 24\ndrwxr-xr-x  5 dev dev 4096 Aug 26 00:50 .\n-rw-r--r--  1 dev dev 1204 Aug 26 00:41 README.md\n-rw-r--r--  1 dev dev  312 Aug 25 22:10 main.py\ndrwxr-xr-x  2 dev dev 4096 Aug 24 09:12 tests"


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
    """一次 /v1/responses 流;返回 {完成/失败/文本/调用/时长}"""
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
                    rec["calls"].append({k: it.get(k) for k in
                                         ("type", "id", "call_id", "name", "arguments", "input")})
            elif t == "response.completed":
                rec["completed"] = True
            elif t in ("response.failed", "error"):
                rec["failed"] = True
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        rec["http_error"] = f"{type(e).__name__}: {e}"
    rec["latency_s"] = round(time.time() - t0, 1)
    return rec


def verdict(rec):
    txt = (rec["text"] or "").strip()
    if rec["failed"] or rec["http_error"]:
        return "failed", None
    leak = ("```json" in txt) or ("⟦" in txt)
    if not rec["completed"]:
        return "half", "no_completed"
    if leak:
        return "half", "protocol_leak"
    if not rec["calls"] and len(txt) == 0:
        return "half", "empty"
    flag = "short" if (not rec["calls"] and 0 < len(txt) < 24) else None
    return "complete", flag


def load_fix(name):
    with open(f"{FIXDIR}/replay_{name}.json") as f:
        d = json.load(f)
    d["model"] = MODEL
    return d


def pin_lane(ts_start):
    """看 90s 内哪个 bpi pod 出现新 [PROMPT] 行"""
    for lane, dep in (("82", "zero-cursor-bpi-82"), ("101", "zero-cursor-bpi")):
        out = sh(f"kubectl -n {NS} logs deploy/{dep} --since=90s --timestamps 2>/dev/null | grep -c 'REQ' || true")
        if out and int(out or 0) > 0:
            return lane
    return "?"


def make_temp_key(base, mk):
    alias = f"s1-base-{int(time.time())}"
    r = json.loads(post(base, mk, "/key/generate",
                        {"models": [MODEL], "duration": "3h", "key_alias": alias}).read())
    return r["key"], alias


def main():
    print("[start] resolving proxy + master key ...", flush=True)
    base, mk = proxy(), master_key()
    print(f"[start] proxy={base} mk=ok fixtures={FIXDIR}", flush=True)
    log = open(ROUNDS_LOG, "a")

    # ── 钉位:重生成 key 直到钉 82(最多 5 把,每把 1 发 hi 探测)──
    key, alias, lane = None, None, "?"
    for attempt in range(5):
        k, a = make_temp_key(base, mk)
        probe = run_request(base, k, load_fix("hi"))
        time.sleep(4)
        lane = pin_lane(time.time())
        print(f"[pin] attempt {attempt+1}: key={a} lane={lane} probe_latency={probe['latency_s']}s", flush=True)
        log.write(json.dumps({"phase": "pin", "attempt": attempt + 1, "alias": a,
                              "lane": lane, "probe": verdict(probe)[0]}) + "\n")
        log.flush()
        if lane == "82":
            key, alias = k, a
            break
        post(base, mk, "/key/delete", {"keys": [k]}).read()
    if not key:
        key, alias = make_temp_key(base, mk)  # 兜底:接受任意 lane,如实记录
        print(f"[pin] fallback: accept whatever lane, key={alias}", flush=True)

    results = []
    try:
        n = 0
        # A 类聊天 ×10
        for i in range(10):
            n += 1
            fx = CHAT_FIX[i % len(CHAT_FIX)]
            rec = run_request(base, key, load_fix(fx))
            v, flag = verdict(rec)
            row = {"round": n, "cls": "chat", "fixture": fx, "verdict": v, "flag": flag,
                   "latency_s": rec["latency_s"], "calls": len(rec["calls"]),
                   "text_head": rec["text"][:80], "text_len": len(rec["text"].strip()),
                   "err": rec["http_error"], "ts": time.strftime("%H:%M:%S")}
            results.append(row)
            log.write(json.dumps(row, ensure_ascii=False) + "\n")
            log.flush()
            print(f"[{n:02d}] chat/{fx} -> {v}{'/' + flag if flag else ''} {rec['latency_s']}s", flush=True)
            time.sleep(3)
        # B 类工具 ×10:round1 期待调用;回灌伪造结果;round2 期待收尾短答
        for i in range(10):
            n += 1
            fx = TOOL_FIX[i % len(TOOL_FIX)]
            d = load_fix(fx)
            r1 = run_request(base, key, d)
            v1, _ = verdict(r1)
            got_call = bool(r1["calls"])
            row = {"round": n, "cls": "tool.r1", "fixture": fx, "verdict": v1,
                   "got_call": got_call, "latency_s": r1["latency_s"],
                   "calls": len(r1["calls"]), "text_head": r1["text"][:80],
                   "text_len": len(r1["text"].strip()), "err": r1["http_error"],
                   "ts": time.strftime("%H:%M:%S")}
            results.append(row)
            log.write(json.dumps(row, ensure_ascii=False) + "\n")
            log.flush()
            print(f"[{n:02d}] tool.r1/{fx} -> {v1} call={got_call} {r1['latency_s']}s", flush=True)
            if got_call:
                c = r1["calls"][0]
                d2 = load_fix(fx)
                call_item = {k: v for k, v in c.items() if v is not None}
                if c["type"] == "custom_tool_call":
                    out_item = {"type": "custom_tool_call_output",
                                "call_id": c.get("call_id"), "output": FAKE_LS}
                else:
                    out_item = {"type": "function_call_output",
                                "call_id": c.get("call_id"), "output": FAKE_LS}
                d2["input"] = d["input"] + [call_item, out_item]
                r2 = run_request(base, key, d2)
                v2, flag2 = verdict(r2)
                row2 = {"round": n, "cls": "tool.r2", "fixture": fx, "verdict": v2, "flag": flag2,
                        "latency_s": r2["latency_s"], "calls": len(r2["calls"]),
                        "text_head": r2["text"][:80], "text_len": len(r2["text"].strip()),
                        "err": r2["http_error"], "ts": time.strftime("%H:%M:%S")}
                results.append(row2)
                log.write(json.dumps(row2, ensure_ascii=False) + "\n")
                log.flush()
                print(f"[{n:02d}] tool.r2/{fx} -> {v2}{'/' + flag2 if flag2 else ''} {r2['latency_s']}s", flush=True)
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
        k = (r["cls"], r["verdict"])
        agg[k] = agg.get(k, 0) + 1
    summary = {"ts": time.strftime("%Y-%m-%d %H:%M"), "model": MODEL, "lane": lane,
               "alias": alias, "total_rows": len(results),
               "by_class_verdict": {f"{a}/{b}": c for (a, b), c in sorted(agg.items())},
               "halves": [r for r in results if r["verdict"] != "complete"],
               "shorts": [r for r in results if r.get("flag") == "short"]}
    with open(SUMMARY, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary["by_class_verdict"], ensure_ascii=False), flush=True)
    print(f"[out] {SUMMARY}", flush=True)


if __name__ == "__main__":
    main()
