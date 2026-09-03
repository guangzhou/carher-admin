#!/usr/bin/env python3
"""lane_direct_probe.py —— **定点**打某一条池腿，不靠 key 亲和抽签。

解决的问题：池别名 `cr-g-*` 底下挂 4 条腿，选哪条由 weighted_affinity 按 **key** 决定
（Cursor 不发 session 头）。想问"lane 85 这条腿到底行不行"，靠多开 key 撞运气既慢又抽不全
——09-02 打 12 发都没抽到 81 一次。这里给每条腿建一个**临时直连名**
`crgtmp-<变体>-<lane>`，一个名字只有一条腿，打谁就是谁。

三条设计约束：
  · **整个流程在同一个 proxy 副本内闭环**（建行 → 建 key → 打 → 删）。`/model/new` 只让
    执行它的那个副本立刻看见，别的副本要等刷新；在 pod 内打 localhost:4000 就绕开了这件事，
    **不用为一次诊断去 rollout restart 生产 proxy**。
  · **仍然走临时 scoped key**，不用 master key —— master key 绕过 per-key 鉴权和 openai/ 的
    api_key gate，漏了占位符也照样 200（假绿①②）。
  · **临时行必删**。`finally` 里删 key + 删行；崩了也删。名字用 `crgtmp-` 前缀，
    与 `cr-g-%` 不匹配，不会污染 crg_row_audit.py 的 72 行判据。

诚实边界：这条路打的是 pod 内 `localhost:4000`，**没走 198 的 nginx 前门**。litellm 的
chat→responses 桥仍然在链路里（桥在 litellm 内部），所以线型是真的；但它**不能替代**
第 6 步 B 段那个走 `cc.auto-link.com.cn` 的验收，只用来定位"哪条腿坏"。

用法：
    python3 lane_direct_probe.py                              # cr-g-5.6-pro，四腿各 2 发
    python3 lane_direct_probe.py --model cr-g-5.6 --repeat 3
    python3 lane_direct_probe.py --lanes 85 --repeat 1
"""
import json
import subprocess
import sys

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]
SVC = "http://zero-cursor-bpi-%s.litellm-product.svc.cluster.local:8201/v1"

# 在 proxy pod 内跑的整个流程。master key 只从 pod 自己的 env 取，不过 argv。
PROG = r'''
import json, os, sys, time, urllib.request, urllib.error

CFG = json.loads(os.environ["PROBE_CFG"])
MK = os.environ["LITELLM_MASTER_KEY"]
BASE = "http://localhost:4000"
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file from the workspace",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}}]


def call(path, payload=None, key=None, timeout=200):
    h = {"Authorization": "Bearer " + (key or MK), "Content-Type": "application/json"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def harvest(raw):
    t = ""
    for ln in raw.splitlines():
        if not ln.startswith("data:"):
            continue
        p = ln[5:].strip()
        if not p or p == "[DONE]":
            continue
        try:
            c = (json.loads(p).get("choices") or [{}])[0]
            d = c.get("delta") or {}
            if isinstance(d.get("content"), str):
                t += d["content"]
            elif isinstance((c.get("message") or {}).get("content"), str):
                t += c["message"]["content"]
        except Exception:
            pass
    return t


# 1) 找拷贝源：池别名的任意一条腿（/model/info 是解密视图）
_, body = call("/model/info")
rows = [r for r in json.loads(body)["data"] if r.get("model_name") == CFG["model"]]
if not rows:
    print(json.dumps({"fatal": "找不到 %s" % CFG["model"]})); sys.exit(0)
src = rows[0]
sp = dict(src.get("litellm_params") or {})
mode = (src.get("model_info") or {}).get("mode") or "chat"
print(json.dumps({"src": {"model": sp.get("model"),
                          "reasoning_effort": sp.get("reasoning_effort"), "mode": mode}}))

made, key_alias, key = [], "crgtmp-%d" % int(time.time()), None
try:
    # 2) 每条腿一个临时直连名
    for lane in CFG["lanes"]:
        nm = "crgtmp-%s-%s" % (CFG["variant"], lane)
        lp = dict(sp)
        lp["api_base"] = CFG["svc"] % lane
        lp["api_key"] = "sk-zerokey-web-noop"      # /model/info 脱敏掉了，必须补
        lp.pop("input_cost_per_token", None); lp.pop("output_cost_per_token", None)
        st, b = call("/model/new", {"model_name": nm, "litellm_params": lp,
                                    "model_info": {"id": nm, "mode": mode}})
        if st >= 300:
            print(json.dumps({"warn": "建 %s 失败 %s %s" % (nm, st, b[:160])})); continue
        made.append(nm)
    # 3) 临时 scoped key（不用 master key，见 docstring）
    st, b = call("/key/generate", {"models": made, "key_alias": key_alias, "duration": "20m"})
    key = json.loads(b)["key"]

    # 4) 逐腿打
    for lane in CFG["lanes"]:
        nm = "crgtmp-%s-%s" % (CFG["variant"], lane)
        if nm not in made:
            continue
        for i in range(CFG["repeat"]):
            nonce = "LDP-%d-%s-%d" % (int(time.time()), lane, i)
            t0 = time.time()
            st, raw = call("/v1/chat/completions",
                           {"model": nm, "stream": True, "tools": TOOLS,
                            "messages": [{"role": "user",
                                          "content": "请原样输出这一行，不要加别的字：" + nonce}]},
                           key=key)
            txt = harvest(raw)
            print(json.dumps({"lane": lane, "i": i, "http": st,
                              "ok": nonce in txt, "dt": round(time.time() - t0, 1),
                              "text": txt[:120]}, ensure_ascii=False))
finally:
    if key:
        call("/key/delete", {"key_aliases": [key_alias]})
    for nm in made:
        call("/model/delete", {"id": nm})
    print(json.dumps({"cleanup": {"key": key_alias, "rows": made}}))
'''


def main():
    argv = sys.argv[1:]

    def opt(name, default=None):
        if name in argv:
            i = argv.index(name)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return default

    model = opt("--model", "cr-g-5.6-pro")
    repeat = int(opt("--repeat", "2"))
    lanes = (opt("--lanes") or "81,83,84,85").split(",")
    cfg = {"model": model, "variant": model[len("cr-g-"):], "lanes": lanes,
           "repeat": repeat, "svc": SVC}

    print("定点探针: %s  腿=%s  每腿 %d 发（临时直连行 crgtmp-*，跑完即删）\n"
          % (model, ",".join(lanes), repeat))
    r = subprocess.run(
        SSH + ["sudo -n kubectl -n %s exec -i deploy/litellm-proxy -- env PROBE_CFG=%s python3 -"
               % (NS, json.dumps(json.dumps(cfg)))],
        input=PROG, capture_output=True, text=True, timeout=1800)
    per, fatal = {}, None
    for ln in r.stdout.splitlines():
        if not ln.startswith("{"):
            continue
        d = json.loads(ln)
        if "fatal" in d:
            fatal = d["fatal"]
        elif "src" in d:
            print("拷贝源: model=%s reasoning_effort=%s mode=%s"
                  % (d["src"]["model"], d["src"]["reasoning_effort"], d["src"]["mode"]))
        elif "warn" in d:
            print("⚠️ " + d["warn"])
        elif "cleanup" in d:
            print("\n临时资产已删: key=%s, 行=%s"
                  % (d["cleanup"]["key"], ",".join(d["cleanup"]["rows"]) or "(无)"))
        elif "lane" in d:
            per.setdefault(d["lane"], []).append(d)
            print("  lane %-4s #%d http=%-4s %5.1fs %s %s"
                  % (d["lane"], d["i"], d["http"], d["dt"],
                     "命中 ✅" if d["ok"] else "未命中 ❌",
                     "" if d["ok"] else repr(d["text"])[:90]))
    if fatal:
        print("❌ " + fatal)
        return 2
    if not per:
        print("❌ 一发都没跑起来:\n%s\n%s" % (r.stdout[-500:], r.stderr[-500:]))
        return 2
    print("\n== 逐腿小结 ==")
    for lane in sorted(per):
        n = len(per[lane]); k = sum(1 for d in per[lane] if d["ok"])
        print("  lane %-4s %d/%d 绿%s" % (lane, k, n, "" if k == n else "   ⬅ 这条腿有问题"))
    return 0 if all(d["ok"] for v in per.values() for d in v) else 1


if __name__ == "__main__":
    sys.exit(main())
