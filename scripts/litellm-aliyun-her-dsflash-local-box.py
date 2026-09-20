#!/usr/bin/env python3
"""阿里云 litellm(ns carher)her deepseek-v4-flash → local-gpu 盒子:alias 治理 + 判别式验收探针。

本脚本**在 litellm pod 内执行**(读 env LITELLM_MASTER_KEY,打 localhost:4000)。
Mac 侧投递(pod 无 curl/base64 参数不稳,统一 python3 解码):

    B64=$(base64 < scripts/litellm-aliyun-her-dsflash-local-box.py | tr -d '\n')
    POD=$(jms ssh k8s-work-226 "kubectl -n carher get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}'")
    echo "$B64" | jms ssh k8s-work-226 \
      "base64 -d > /tmp/dsbox.py && kubectl -n carher cp /tmp/dsbox.py $POD:/tmp/dsbox.py \
       && kubectl -n carher exec $POD -- python3 /tmp/dsbox.py <subcommand>"

子命令(alias 治理,对象=全部 carher-* key):
    summary            只读:总数/已映射/缺口统计
    backup             只读:输出缺口 key 的完整现状 JSON(改前必存档)
    dry                只读:逐 key 打印将做的变更
    apply [--only=carher-N]   写入:补 alias deepseek-v4-flash=>local-deepseek-v4-flash
                       + union 裸名进 allowlist;连续 2 次失败即停
    verify             只读:重取全量,报告仍未映射的 key(期望 0)

子命令(切上游后的验收):
    probe              判别式探针:临时 key(用完即删)发 4 发请求
                       —— 常规 / 20k输入+mt=384000(clamp判别) / 20k输入+mt=65536(对照)
                       / 官方组直调 384000(不许被误钳)。
                       判据看 x-litellm-model-id:
                         local-gpu/deepseek-v4-flash          = 落盒子(clamp 生效)
                         deepseek-official/...-fallback       = 回落官方(clamp 没生效或超限)
                       ⚠ 小输入 + mt=384000 是假证明:20+384000 < 393216 盒子本来就收。
                       判别条件必须 输入+384000 > 393216 > 输入+65536。

背景与全记录:memory project_aliyun_her_dsflash_to_local_gpu_box_2026_08_19。
相关红线:
  - her key 只在阿里云 litellm 有效(198 的 carher-* 不被使用)
  - 阿里云配置正统在 CM(init 容器 wipe-db-config-rows 会清 DB 行),
    改组/fallback 用 CM + rollout,不用 /model/new、/config/update
  - litellm_params.max_tokens 压不过客户端值,钳制靠 local_gpu_max_tokens_clamp callback
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
TGT = "local-deepseek-v4-flash"
KEY_PREFIX = "carher-"
BOX_MODEL_ID = "local-gpu/deepseek-v4-flash"


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


def fetch_keys():
    """全量取 carher-* key 及其现状(models/aliases)。"""
    _, _, body = http("GET", "/spend/keys?limit=100000")
    rows = [r for r in json.loads(body) if str(r.get("key_alias") or "").startswith(KEY_PREFIX)]
    rows.sort(key=lambda r: str(r.get("key_alias")))
    out = []
    for r in rows:
        tok = r.get("token")
        _, _, kb = http("GET", "/key/info?key=" + urllib.parse.quote(tok, safe=""))
        info = json.loads(kb).get("info", {}) if kb else {}
        out.append({
            "alias": r.get("key_alias"),
            "token": tok,
            "models": list(info.get("models") or []),
            "aliases": dict(info.get("aliases") or {}),
        })
    return out


def gap_of(keys):
    return [k for k in keys if k["aliases"].get(SRC) != TGT]


def cmd_summary():
    keys = fetch_keys()
    gap = gap_of(keys)
    print("carher-* total=%d | mapped(%s=>%s)=%d | gap=%d" % (
        len(keys), SRC, TGT, len(keys) - len(gap), len(gap)))
    for g in gap:
        print("  GAP %-14s bare_in_allowlist=%s" % (g["alias"], SRC in g["models"]))


def cmd_backup():
    print(json.dumps(gap_of(fetch_keys()), ensure_ascii=False))


def cmd_dry():
    gap = gap_of(fetch_keys())
    print("gap=%d" % len(gap))
    for g in gap:
        print("  %-14s +alias %s=>%s | union_bare=%s" % (g["alias"], SRC, TGT, SRC not in g["models"]))


def cmd_apply(only=None):
    gap = gap_of(fetch_keys())
    targets = [g for g in gap if only is None or g["alias"] == only]
    ok = fail = consec = 0
    for g in targets:
        aliases = dict(g["aliases"])
        aliases[SRC] = TGT
        body = {"key": g["token"], "aliases": aliases}
        if SRC not in g["models"] and g["models"]:  # models==[] 不收窄无限制 key
            body["models"] = g["models"] + [SRC]
        st, _, resp = http("POST", "/key/update", body)
        if st == 200:
            ok += 1
            consec = 0
            print("OK   %s" % g["alias"])
        else:
            fail += 1
            consec += 1
            print("FAIL %s HTTP %s %s" % (g["alias"], st, resp[:150]))
            if consec >= 2:
                print("STOP: 2 consecutive failures")
                break
    print("apply ok=%d fail=%d of %d" % (ok, fail, len(targets)))
    sys.exit(0 if fail == 0 else 1)


def cmd_verify():
    gap = gap_of(fetch_keys())
    for g in gap:
        print("STILL-UNMAPPED %s" % g["alias"])
    print("verify: unmapped=%d (expect 0)" % len(gap))
    sys.exit(0 if not gap else 1)


def cmd_probe():
    alias = "tmp-dsbox-probe"
    st, _, body = http("POST", "/key/generate", {
        "key_alias": alias,
        "models": [SRC, TGT, "official-deepseek-v4-flash"],
        "aliases": {SRC: TGT},
        "metadata": {"purpose": "dsflash-box-probe"},
    })
    assert st == 200, body[:200]
    tk = json.loads(body)["key"]
    tools = [{"type": "function", "function": {
        "name": "noop", "description": "noop",
        "parameters": {"type": "object", "properties": {}}}}]
    # 判别式输入:~20k token,使 输入+384000 > 393216(盒子上限) > 输入+65536
    big = ("alpha beta gamma delta epsilon zeta eta theta " * 2500).strip()

    def chat(model, content, mt):
        st, hd, _ = http("POST", "/v1/chat/completions", {
            "model": model, "stream": True, "tools": tools, "max_tokens": mt,
            "messages": [{"role": "user", "content": content}],
        }, key=tk)
        return st, hd.get("x-litellm-model-id")

    failures = []
    cases = [
        ("normal (mt=2048)", SRC, "reply exactly: OK", 2048, BOX_MODEL_ID),
        ("clamp-discriminator (20k in, mt=384000)", SRC,
         big + "\n\nIgnore the words above. reply exactly: OK", 384000, BOX_MODEL_ID),
        ("control (20k in, mt=65536)", SRC,
         big + "\n\nIgnore the words above. reply exactly: OK", 65536, BOX_MODEL_ID),
        ("official passthrough (mt=384000, must NOT clamp)",
         "official-deepseek-v4-flash", "reply exactly: OK", 384000, None),
    ]
    for name, model, content, mt, expect_id in cases:
        st, mid = chat(model, content, mt)
        verdict = "PASS" if st == 200 and (expect_id is None or mid == expect_id) else "FAIL"
        if verdict == "FAIL":
            failures.append(name)
        print("%s  %-48s HTTP %s | model-id=%s" % (verdict, name, st, mid))
    http("POST", "/key/delete", {"key_aliases": [alias]})
    print("temp key deleted | result: %s" % ("ALL PASS" if not failures else "FAILED: %s" % failures))
    sys.exit(0 if not failures else 1)


def main():
    if not MASTER:
        sys.exit("LITELLM_MASTER_KEY not in env — run inside the litellm pod")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    only = None
    for a in sys.argv[2:]:
        if a.startswith("--only="):
            only = a.split("=", 1)[1]
    if cmd == "summary":
        cmd_summary()
    elif cmd == "backup":
        cmd_backup()
    elif cmd == "dry":
        cmd_dry()
    elif cmd == "apply":
        cmd_apply(only)
    elif cmd == "verify":
        cmd_verify()
    elif cmd == "probe":
        cmd_probe()
    else:
        sys.exit("unknown subcommand: %s (summary|backup|dry|apply|verify|probe)" % cmd)


if __name__ == "__main__":
    main()
