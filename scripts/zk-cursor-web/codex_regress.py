#!/usr/bin/env python3
"""codex_regress.py — codex 线(cursor-fc-*)回归对照脚本(cursor-g 协议重写 S0/S6 双闸之一)

目的:cursor-g 网页线改 responses.js 前后,证明 codex 线(不同 CM/pod)行为逐轮不变。
对照口径 = **结构**不变(每轮 item 类型序列/是否调工具/结束状态),不比文本内容。

用法(在 198 上跑):
    python3 codex_regress.py                          # 跑基线,存 /home/cltx/backups-bpi/
    python3 codex_regress.py --compare <旧基线.json>   # 跑一遍并与旧基线比结构,不同则 exit 1

纪律:
- 临时 scoped key(10 分钟,只授一个 cursor-fc 模型),用完即删;绝不 master key 打业务请求。
- 只 2 轮(烧 codex 额度,最小化):①纯聊天暗号轮 ②单工具轮。
- 只读脚本,不改任何集群状态(除临时 key 的生成/删除)。
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request

NS = "litellm-product"
MODEL_DEFAULT = "cursor-fc-5.6-sol"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60).stdout.strip()


def master_key():
    import base64
    dep = json.loads(sh(f"kubectl -n {NS} get deploy litellm-proxy -o json"))
    c = dep["spec"]["template"]["spec"]["containers"][0]
    for e in c.get("env", []):
        if "MASTER_KEY" in e.get("name", ""):
            if e.get("value"):
                return e["value"]
            ref = e.get("valueFrom", {}).get("secretKeyRef", {})
            if ref:
                val = sh(f"kubectl -n {NS} get secret {ref['name']} -o jsonpath='{{.data.{ref['key']}}}'")
                return base64.b64decode(val).decode()
    for ef in c.get("envFrom", []):
        sec = ef.get("secretRef", {}).get("name")
        if not sec:
            continue
        data = json.loads(sh(f"kubectl -n {NS} get secret {sec} -o json")).get("data", {})
        for k, v in data.items():
            if "MASTER_KEY" in k:
                return base64.b64decode(v).decode()
    raise SystemExit("master key not found in litellm-proxy env/envFrom secrets")


def proxy_url():
    ip = sh(f"kubectl -n {NS} get svc litellm-proxy -o jsonpath='{{.spec.clusterIP}}'")
    port = sh(f"kubectl -n {NS} get svc litellm-proxy -o jsonpath='{{.spec.ports[0].port}}'")
    return f"http://{ip}:{port}"


def post(base, key, path, body, stream=False, timeout=180):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def run_round(base, key, model, label, prompt, tools=None):
    body = {"model": model, "stream": True,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}]}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    t0 = time.time()
    item_types, added_types, fn_names, status, err = [], [], [], "no_completed", None
    raw_head = []
    try:
        resp = post(base, key, "/v1/responses", body)
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line and len(raw_head) < 3:
                raw_head.append(line[:200])
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            t = ev.get("type", "")
            if t == "response.output_item.added":
                added_types.append(ev.get("item", {}).get("type", "?"))
            elif t == "response.output_item.done":
                item = ev.get("item", {})
                item_types.append(item.get("type", "?"))
                if item.get("type") in ("function_call", "custom_tool_call"):
                    fn_names.append(item.get("name", "?"))
            elif t == "response.completed":
                status = "completed"
            elif t in ("response.failed", "error"):
                status = "failed"
    except Exception as e:  # noqa: BLE001 — 对照脚本要如实记录任何失败形态
        err = f"{type(e).__name__}: {e}"
        status = "transport_error"
    rec = {"label": label, "status": status, "item_types": item_types, "added_types": added_types,
           "fn_names": fn_names, "latency_s": round(time.time() - t0, 1), "error": err}
    if status != "completed":
        rec["raw_head"] = raw_head
    return rec


def structural(r):
    return {"label": r["label"], "status": r["status"],
            "item_type_set": sorted(set(r["item_types"])),
            "added_type_set": sorted(set(r["added_types"])), "called_tool": bool(r["fn_names"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--out", default=None)
    ap.add_argument("--compare", default=None)
    args = ap.parse_args()

    base, mk = proxy_url(), master_key()
    alias = f"codex-regress-tmp-{int(time.time())}"
    kr = json.loads(post(base, mk, "/key/generate",
                         {"models": [args.model], "duration": "10m", "key_alias": alias}).read())
    tmp_key = kr["key"]
    print(f"[key] temp key {alias} generated")
    try:
        rounds = []
        # 两轮都带 tools —— 贴真实流量形状(Cursor 永远带 tools)。
        # 已实测既有怪癖:无 tools 请求该线只发 added 就断流(无 done/completed),
        # 真实流量不走该形状,回归不测它。
        _tool = [{"type": "function", "name": "get_current_time",
                  "description": "Returns current time",
                  "parameters": {"type": "object", "properties": {},
                                 "additionalProperties": False}}]
        specs = [
            ("chat", "Reply with exactly this passphrase and nothing else: CODEXREG-OK. "
                     "Do NOT call any tool.", _tool),
            ("tool", "What is the current time? You MUST call the get_current_time tool.", _tool),
        ]
        for label, prompt, tools in specs:
            r = run_round(base, tmp_key, args.model, label, prompt, tools=tools)
            # 单次重试:瞬态(no_completed/transport)不算结构结论,重试仍失败才如实入账
            if r["status"] != "completed":
                print(f"[retry] {label} round status={r['status']}, retrying once")
                r2 = run_round(base, tmp_key, args.model, label, prompt, tools=tools)
                r = r2 if r2["status"] == "completed" else r
                r["retried"] = True
            rounds.append(r)
    finally:
        try:
            post(base, mk, "/key/delete", {"keys": [tmp_key]}).read()
            print("[key] temp key deleted")
        except Exception as e:  # noqa: BLE001
            print(f"[key] DELETE FAILED, manual cleanup needed: {alias}: {e}", file=sys.stderr)

    result = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "model": args.model,
              "rounds": rounds, "structural": [structural(r) for r in rounds]}
    out = args.out or f"/home/cltx/backups-bpi/codex_regress_baseline_{time.strftime('%Y%m%d_%H%M')}.json"
    with open(out, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result["structural"], ensure_ascii=False))
    print(f"[out] {out}")

    if args.compare:
        with open(args.compare) as f:
            old = json.load(f)
        if old.get("structural") == result["structural"]:
            print("[compare] PASS — codex 线结构行为与基线一致")
        else:
            print("[compare] FAIL — 结构漂移:")
            print("  old:", json.dumps(old.get("structural"), ensure_ascii=False))
            print("  new:", json.dumps(result["structural"], ensure_ascii=False))
            sys.exit(1)


if __name__ == "__main__":
    main()
