#!/usr/bin/env python3
"""阿里云 litellm(ns carher)her deepseek-v4-flash 默认映射 → 官网(official-deepseek-v4-flash)。

与 litellm-aliyun-her-dsflash-local-box.py 互为反向：那把把全 fleet 指向自建 GPU 盒
(local-deepseek-v4-flash / 36.151.241.10)，这把把 **每把 carher-* key 的 per-key alias**
改指 `official-deepseek-v4-flash`(api.deepseek.com)。

**只动 key,不动 CM**：CM 里的 local 组、两条 local→official fallback、
local_gpu_max_tokens_clamp callback 全部原样保留(没有 key 再指它=零流量),
回滚只需把 alias 改回去,不需要 rollout。

⚠ 纪律：
  - `/key/update` 的 `aliases`/`models` 是**整字段替换**,必须 caller-side merge；
    本脚本只动 `deepseek-v4-flash` 这一个 alias 条目,其余 alias / models / 预算 /
    身份字段逐项断言未变(apply 后立即回读比对,不一致即停)。
  - alias 目标组**不需要**进 key 的 models 白名单(白名单查的是改写前的请求名)——
    这一条由 probe 的 her-shaped 临时 key 现场证明,不靠记忆。
  - her key 只在阿里云 litellm 有效,198 上的 carher-* 不被使用。

本脚本**在 litellm pod 内执行**(读 env LITELLM_MASTER_KEY,打 localhost:4000)。
Mac 侧投递：

    B64=$(base64 < scripts/litellm-aliyun-her-dsflash-official.py | tr -d '\n')
    POD=$(jms ssh k8s-work-226 "kubectl -n carher get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}'")
    echo "$B64" | jms ssh k8s-work-226 \
      "base64 -d > /tmp/dsoff.py && kubectl -n carher cp /tmp/dsoff.py $POD:/tmp/dsoff.py \
       && kubectl -n carher exec $POD -- python3 /tmp/dsoff.py <subcommand>"

子命令：
    summary                只读:全量 carher-* 的 deepseek-v4-flash alias 分布
    backup                 只读:**全部** carher-* key 的 models/aliases 快照 JSON(改前必存)
    dry                    只读:逐 key 打印将做的变更
    probe [--via=alias|direct]
                           判别式探针:临时 key(her 形状:models 只含裸名,alias 指目标组)
                           发 4 发,判据看 x-litellm-model-id:
                             deepseek-official/deepseek-v4-flash-fallback = 落官网 ✅
                             local-gpu/deepseek-v4-flash                  = 还在盒子 ❌
    apply [--only=carher-N] 写入:alias deepseek-v4-flash => official-deepseek-v4-flash
                           每把写后立即回读断言(只该条变、其余零变);连续 2 次失败即停
    verify                 只读:重取全量,报告未映射的 key(期望 0)
    restore --file=PATH    从 backup JSON 恢复 aliases(+models)

判据（每一步都要有数据,别拿"应该"下结论）：
    改前 probe: SRC 落 local-gpu/...            ← 阳性对照,证明量具能分辨两个落点
    改后 probe: SRC 落 deepseek-official/...    ← 目标态
    verify:     unmapped=0 且 untouched-diff=0
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://localhost:4000"
MASTER = os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("MASTER_KEY") or ""
SRC = "deepseek-v4-flash"
TGT = "official-deepseek-v4-flash"
OLD_TGT = "local-deepseek-v4-flash"
KEY_PREFIX = "carher-"
OFFICIAL_MODEL_ID = "deepseek-official/deepseek-v4-flash-fallback"
BOX_MODEL_ID = "local-gpu/deepseek-v4-flash"

# 改 apply 时逐字段断言"没被顺手改掉"的字段集
UNTOUCHED_FIELDS = ("models", "max_budget", "budget_duration", "tpm_limit", "rpm_limit",
                    "user_id", "team_id", "metadata", "blocked", "expires")


def http(method, path, body=None, key=None, timeout=180):
    headers = {"Authorization": "Bearer " + (key or MASTER), "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers), e.read().decode()
        except Exception:
            return e.code, {}, ""


def key_info(token):
    _, _, kb = http("GET", "/key/info?key=" + urllib.parse.quote(token, safe=""))
    return json.loads(kb).get("info", {}) if kb else {}


def snap(info):
    """取出用于"未被顺手改掉"比对的字段快照。"""
    out = {}
    for f in UNTOUCHED_FIELDS:
        v = info.get(f)
        out[f] = sorted(v) if isinstance(v, list) else v
    return out


def fetch_keys():
    """全量取 carher-* key 及其现状(models/aliases + untouched 快照)。"""
    _, _, body = http("GET", "/spend/keys?limit=100000")
    rows = [r for r in json.loads(body) if str(r.get("key_alias") or "").startswith(KEY_PREFIX)]
    rows.sort(key=lambda r: str(r.get("key_alias")))
    out = []
    for r in rows:
        tok = r.get("token")
        info = key_info(tok)
        out.append({
            "alias": r.get("key_alias"),
            "token": tok,
            "models": list(info.get("models") or []),
            "aliases": dict(info.get("aliases") or {}),
            "snap": snap(info),
        })
    return out


def gap_of(keys):
    return [k for k in keys if k["aliases"].get(SRC) != TGT]


def cmd_summary():
    keys = fetch_keys()
    dist = {}
    for k in keys:
        dist[k["aliases"].get(SRC, "<NO-ALIAS>")] = dist.get(k["aliases"].get(SRC, "<NO-ALIAS>"), 0) + 1
    print("carher-* total=%d" % len(keys))
    for a, n in sorted(dist.items(), key=lambda x: -x[1]):
        print("  %-32s %d" % (a, n))
    gap = gap_of(keys)
    print("gap(!= %s)=%d" % (TGT, len(gap)))


def cmd_backup():
    print(json.dumps(fetch_keys(), ensure_ascii=False))


def cmd_dry():
    gap = gap_of(fetch_keys())
    print("gap=%d" % len(gap))
    for g in gap:
        print("  %-16s %s: %s -> %s" % (g["alias"], SRC, g["aliases"].get(SRC, "<NO-ALIAS>"), TGT))


def cmd_apply(only=None):
    gap = gap_of(fetch_keys())
    targets = [g for g in gap if only is None or g["alias"] == only]
    print("targets=%d" % len(targets))
    ok = fail = consec = 0
    for g in targets:
        aliases = dict(g["aliases"])
        aliases[SRC] = TGT
        st, _, resp = http("POST", "/key/update", {"key": g["token"], "aliases": aliases})
        if st != 200:
            fail += 1
            consec += 1
            print("FAIL %s HTTP %s %s" % (g["alias"], st, resp[:150]))
            if consec >= 2:
                print("STOP: 2 consecutive failures")
                break
            continue
        # 立即回读:该条已改 + 其余 alias 一字不差 + untouched 字段零变
        after = key_info(g["token"])
        a_after = dict(after.get("aliases") or {})
        bad = []
        if a_after.get(SRC) != TGT:
            bad.append("alias not applied: %r" % a_after.get(SRC))
        if {k: v for k, v in a_after.items() if k != SRC} != {k: v for k, v in aliases.items() if k != SRC}:
            bad.append("other aliases drifted")
        if snap(after) != g["snap"]:
            bad.append("untouched fields drifted: %s" % [
                f for f in UNTOUCHED_FIELDS if snap(after)[f] != g["snap"][f]])
        if bad:
            fail += 1
            consec += 1
            print("FAIL %s readback: %s" % (g["alias"], "; ".join(bad)))
            if consec >= 2:
                print("STOP: 2 consecutive readback failures")
                break
            continue
        ok += 1
        consec = 0
        print("OK   %s" % g["alias"])
    print("apply ok=%d fail=%d of %d" % (ok, fail, len(targets)))
    sys.exit(0 if fail == 0 else 1)


def cmd_verify(backup_file=None):
    keys = fetch_keys()
    gap = gap_of(keys)
    for g in gap:
        print("STILL-UNMAPPED %-16s %s" % (g["alias"], g["aliases"].get(SRC, "<NO-ALIAS>")))
    drift = 0
    if backup_file:
        before = {b["alias"]: b for b in json.load(open(backup_file))}
        for k in keys:
            b = before.get(k["alias"])
            if not b:
                print("NEW-KEY %s (not in backup)" % k["alias"])
                continue
            if k["snap"] != b["snap"]:
                drift += 1
                print("DRIFT %s untouched fields changed" % k["alias"])
            if {x: y for x, y in k["aliases"].items() if x != SRC} != \
               {x: y for x, y in b["aliases"].items() if x != SRC}:
                drift += 1
                print("DRIFT %s other aliases changed" % k["alias"])
    print("verify: total=%d unmapped=%d (expect 0) untouched-drift=%d (expect 0)" %
          (len(keys), len(gap), drift))
    sys.exit(0 if not gap and not drift else 1)


def cmd_restore(path):
    rows = json.load(open(path))
    ok = fail = 0
    for r in rows:
        st, _, resp = http("POST", "/key/update",
                           {"key": r["token"], "aliases": r["aliases"], "models": r["models"]})
        if st == 200:
            ok += 1
        else:
            fail += 1
            print("FAIL %s HTTP %s %s" % (r["alias"], st, resp[:150]))
    print("restore ok=%d fail=%d" % (ok, fail))
    sys.exit(0 if fail == 0 else 1)


def cmd_probe(via=TGT):
    """her 形状临时 key:models 只含裸名(不含目标组),alias 指 via。"""
    alias = "tmp-dsoff-probe"
    http("POST", "/key/delete", {"key_aliases": [alias]})
    st, _, body = http("POST", "/key/generate", {
        "key_alias": alias,
        "models": [SRC, "deepseek-v4-pro"],
        "aliases": {SRC: via},
        "metadata": {"purpose": "dsflash-official-probe"},
    })
    assert st == 200, body[:300]
    tk = json.loads(body)["key"]
    tools = [{"type": "function", "function": {
        "name": "noop", "description": "noop",
        "parameters": {"type": "object", "properties": {}}}}]
    big = ("alpha beta gamma delta epsilon zeta eta theta " * 2500).strip()  # ~20k token

    def chat(model, content, mt, stream):
        st, hd, rb = http("POST", "/v1/chat/completions", {
            "model": model, "stream": stream, "tools": tools, "max_tokens": mt,
            "messages": [{"role": "user", "content": content}],
        }, key=tk)
        return st, hd.get("x-litellm-model-id"), rb[:200]

    expect = OFFICIAL_MODEL_ID if via == TGT else BOX_MODEL_ID
    cases = [
        ("normal stream (mt=2048)", "reply exactly: OK", 2048, True),
        ("non-stream (mt=2048)", "reply exactly: OK", 2048, False),
        ("20k in + mt=384000 (her catalog 形状)",
         big + "\n\nIgnore the words above. reply exactly: OK", 384000, True),
        ("20k in + mt=65536 (对照)",
         big + "\n\nIgnore the words above. reply exactly: OK", 65536, True),
    ]
    failures = []
    print("probe via alias %s => %s | expect model-id=%s" % (SRC, via, expect))
    for name, content, mt, stream in cases:
        st, mid, rb = chat(SRC, content, mt, stream)
        good = st == 200 and mid == expect
        if not good:
            failures.append(name)
        print("%s  %-42s HTTP %s | model-id=%s %s" %
              ("PASS" if good else "FAIL", name, st, mid, "" if good else "| " + rb))
    http("POST", "/key/delete", {"key_aliases": [alias]})
    print("temp key deleted | result: %s" % ("ALL PASS" if not failures else "FAILED: %s" % failures))
    sys.exit(0 if not failures else 1)


def main():
    if not MASTER:
        sys.exit("LITELLM_MASTER_KEY not in env — run inside the litellm pod")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    only = path = None
    via = TGT
    for a in sys.argv[2:]:
        if a.startswith("--only="):
            only = a.split("=", 1)[1]
        elif a.startswith("--file="):
            path = a.split("=", 1)[1]
        elif a.startswith("--via="):
            v = a.split("=", 1)[1]
            via = OLD_TGT if v in ("old", "box", "local") else (TGT if v in ("new", "official") else v)
    if cmd == "summary":
        cmd_summary()
    elif cmd == "backup":
        cmd_backup()
    elif cmd == "dry":
        cmd_dry()
    elif cmd == "apply":
        cmd_apply(only)
    elif cmd == "verify":
        cmd_verify(path)
    elif cmd == "restore":
        if not path:
            sys.exit("restore needs --file=PATH")
        cmd_restore(path)
    elif cmd == "probe":
        cmd_probe(via)
    else:
        sys.exit("unknown subcommand: %s (summary|backup|dry|probe|apply|verify|restore)" % cmd)


if __name__ == "__main__":
    main()
