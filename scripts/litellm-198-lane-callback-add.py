#!/usr/bin/env python3
"""给 198 某条车道的 litellm Deployment **新增**一个 callback（以 live 为底，不走 helm 快照）。

在 198 上以 root 跑::

    python3 litellm-198-lane-callback-add.py install  <deploy> <file.py> <module.instance> [--after <module.instance>]
    python3 litellm-198-lane-callback-add.py verify   <deploy> <file.py> <tag>
    python3 litellm-198-lane-callback-add.py rollback <deploy> <backup.json>

为什么不用 lane-callback-swap.sh：它从 helm 冻结快照重渲染，而 2026-10-10 10:00 prod 的
callbacks CM 被手工换过（deepseek_responses_adapt.py），快照里还是旧版 ⇒ 走 helm 会静默回退那次修复。
本脚本只做加法，每步断言：

1. callbacks CM：复制 live CM 全部 key + 新 key ⇒ 新 CM（断言旧 key 逐字节未变、只多一个 key）
2. config CM：config.yaml 在锚点行后定点插一行（断言长度差 == 插入行、行级 diff 只有这一行）
3. Deployment 一次 JSON patch：两个 volume 换 CM 名 + 追加一个 subPath volumeMount
   （RollingUpdate maxUnavailable=0，零中断；不删 pod、不 apply）
4. rollout status

改前整份 Deployment 存 /root/cb-add-backup/<deploy>.<ts>.json，rollback 用它把 volumes /
volumeMounts 原样还原（旧 CM 不删，仍在集群里）。
"""
import difflib
import hashlib
import json
import os
import subprocess
import sys
import time

NS = "litellm-product"
BACKUP_DIR = "/root/cb-add-backup"


def kc(*args, stdin=None, check=True):
    r = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True, input=stdin)
    if check and r.returncode != 0:
        sys.exit(f"kubectl {' '.join(args[:3])} failed: {r.stderr.strip()[:400]}")
    return r.stdout


def get_json(*args):
    return json.loads(kc("get", *args, "-o", "json"))


def sha12(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def new_cm_name(old, data):
    # 旧名去掉末尾 -<hash>，再接新内容 hash（名字只是标签，内容以 data 为准）
    base = old.rsplit("-", 1)[0] if len(old.rsplit("-", 1)[-1]) == 12 else old
    return f"{base}-{sha12(data)}"


def create_cm(name, data, src):
    obj = {"apiVersion": "v1", "kind": "ConfigMap",
           "metadata": {"name": name, "namespace": NS,
                        "labels": {"carher.io/cb-add-from": src[:63]}},
           "data": data}
    exists = subprocess.run(["kubectl", "-n", NS, "get", "cm", name], capture_output=True).returncode == 0
    if exists:
        live = get_json("cm", name)["data"]
        assert live == data, f"CM {name} exists with different content"
        print(f"  CM {name} already exists with identical content, reuse")
        return
    kc("create", "-f", "-", stdin=json.dumps(obj))
    assert get_json("cm", name)["data"] == data, f"CM {name} readback mismatch"
    print(f"  created CM {name} ({len(data)} keys)")


def volume_index(spec, name):
    idx = [i for i, v in enumerate(spec["volumes"]) if v["name"] == name]
    assert len(idx) == 1, (name, idx)
    return idx[0]


def install(deploy, path, entry, after, env=()):
    fname = os.path.basename(path)
    content = open(path, encoding="utf-8").read()
    d = get_json("deploy", deploy)
    spec = d["spec"]["template"]["spec"]
    c = spec["containers"][0]
    assert c["name"] == "litellm", c["name"]
    assert d["spec"]["strategy"]["type"] == "RollingUpdate"
    assert d["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] in (0, "0"), "maxUnavailable must be 0"
    assert not any(m.get("subPath") == fname for m in c["volumeMounts"]), f"{fname} already mounted"

    os.makedirs(BACKUP_DIR, exist_ok=True)
    bk = f"{BACKUP_DIR}/{deploy}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    json.dump(d, open(bk, "w"))
    print(f"backup: {bk}")

    # 1) callbacks CM
    vi_cb = volume_index(spec, "callbacks")
    old_cb = spec["volumes"][vi_cb]["configMap"]["name"]
    cb = get_json("cm", old_cb)["data"]
    assert fname not in cb, f"{fname} already in {old_cb}"
    new_cb_data = dict(cb)
    new_cb_data[fname] = content
    assert {k: new_cb_data[k] for k in cb} == cb and set(new_cb_data) - set(cb) == {fname}
    new_cb = new_cm_name(old_cb, new_cb_data)
    create_cm(new_cb, new_cb_data, old_cb)

    # 2) config CM（config.yaml 定点插一行）
    cfg_mount = [m for m in c["volumeMounts"] if m["mountPath"] == "/app/config.yaml"]
    assert len(cfg_mount) == 1, cfg_mount
    vi_cfg = volume_index(spec, cfg_mount[0]["name"])
    old_cfg = spec["volumes"][vi_cfg]["configMap"]["name"]
    cfg = get_json("cm", old_cfg)["data"]
    key = cfg_mount[0]["subPath"]
    text = cfg[key]
    line = f"  - {entry}\n"
    anchor = f"  - {after}\n"
    assert line not in text, "callback line already present"
    assert text.count(anchor) == 1, f"anchor {after!r} occurs {text.count(anchor)} times"
    new_text = text.replace(anchor, anchor + line, 1)
    assert len(new_text) - len(text) == len(line)
    diff = [l for l in difflib.unified_diff(text.splitlines(), new_text.splitlines(), lineterm="", n=0)
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    assert diff == ["+" + line.rstrip("\n")], diff
    new_cfg_data = dict(cfg)
    new_cfg_data[key] = new_text
    new_cfg = new_cm_name(old_cfg, new_cfg_data)
    create_cm(new_cfg, new_cfg_data, old_cfg)

    # 3) 一次 patch
    ops = [
        {"op": "test", "path": f"/spec/template/spec/volumes/{vi_cb}/configMap/name", "value": old_cb},
        {"op": "replace", "path": f"/spec/template/spec/volumes/{vi_cb}/configMap/name", "value": new_cb},
        {"op": "test", "path": f"/spec/template/spec/volumes/{vi_cfg}/configMap/name", "value": old_cfg},
        {"op": "replace", "path": f"/spec/template/spec/volumes/{vi_cfg}/configMap/name", "value": new_cfg},
        {"op": "add", "path": "/spec/template/spec/containers/0/volumeMounts/-",
         "value": {"name": "callbacks", "mountPath": f"/app/{fname}", "subPath": fname, "readOnly": True}},
    ]
    # 可选：同一个 patch 里加 env（同一次滚动生效，不多滚一轮）。已存在的同名 env 拒绝，不覆盖。
    have = {e["name"] for e in c.get("env", [])}
    for kv in env:
        k, v = kv.split("=", 1)
        assert k not in have, f"env {k} already set on {deploy}"
        ops.append({"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": k, "value": v}})
    print(f"patch: callbacks {old_cb} -> {new_cb}; config {old_cfg} -> {new_cfg}; +mount /app/{fname}; +env {list(env)}")
    kc("patch", "deploy", deploy, "--type", "json", "-p", json.dumps(ops))
    print(kc("rollout", "status", f"deploy/{deploy}", "--timeout=1500s").strip().splitlines()[-1])
    print(f"ROLLBACK: python3 {sys.argv[0]} rollback {deploy} {bk}")


def verify(deploy, fname, tag):
    want = None
    d = get_json("deploy", deploy)
    cbname = d["spec"]["template"]["spec"]["volumes"][volume_index(d["spec"]["template"]["spec"], "callbacks")]["configMap"]["name"]
    want = hashlib.sha256(get_json("cm", cbname)["data"][fname].encode()).hexdigest()[:12]
    sel = ",".join(f"{k}={v}" for k, v in d["spec"]["selector"]["matchLabels"].items())
    pods = [p for p in get_json("pod", "-l", sel)["items"] if p["status"].get("phase") == "Running"]
    bad = 0
    for p in pods:
        n = p["metadata"]["name"]
        logs = kc("logs", n, "-c", "litellm", check=False)
        got = kc("exec", n, "-c", "litellm", "--", "sha256sum", f"/app/{fname}", check=False)[:12]
        installed = logs.count(f"{tag}: installed")
        failed = logs.count(f"{tag}: install failed") + logs.count(f"{tag}: chunk_parser not found") + logs.count(f"{tag}: disabled")
        warn = logs.count(f"{tag}: source does not show") + logs.count(f"{tag}: cannot read")
        imp = sum(logs.count(s) for s in ("ImportError", "ModuleNotFoundError", "SyntaxError"))
        ready = all(cs.get("ready") for cs in p["status"].get("containerStatuses", []))
        ok = got == want and installed >= 1 and failed == 0 and imp == 0 and ready
        bad += not ok
        print(f"  {n} ready={ready} sha={'ok' if got == want else 'BAD ' + got} installed={installed} "
              f"refused={failed} guard_warn={warn} import_err={imp} {'OK' if ok else 'FAIL'}")
    print(("VERIFIED" if not bad and pods else "VERIFY FAILED") + f" {deploy}: {len(pods)} pods")
    sys.exit(1 if bad or not pods else 0)


def rollback(deploy, bk):
    old = json.load(open(bk))
    os_ = old["spec"]["template"]["spec"]
    ops = [{"op": "replace", "path": "/spec/template/spec/volumes", "value": os_["volumes"]},
           {"op": "replace", "path": "/spec/template/spec/containers/0/volumeMounts",
            "value": os_["containers"][0]["volumeMounts"]},
           {"op": "replace", "path": "/spec/template/spec/containers/0/env",
            "value": os_["containers"][0].get("env", [])}]
    kc("patch", "deploy", deploy, "--type", "json", "-p", json.dumps(ops))
    print(kc("rollout", "status", f"deploy/{deploy}", "--timeout=1500s").strip().splitlines()[-1])


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["install"] and len(a) >= 4:
        after = a[a.index("--after") + 1] if "--after" in a else "responses_aclose.responses_aclose"
        env = [a[i + 1] for i, x in enumerate(a) if x == "--env"]
        install(a[1], a[2], a[3], after, env)
    elif a[:1] == ["verify"] and len(a) == 4:
        verify(a[1], a[2], a[3])
    elif a[:1] == ["rollback"] and len(a) == 3:
        rollback(a[1], a[2])
    else:
        sys.exit(__doc__)
