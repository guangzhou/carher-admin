#!/usr/bin/env python3
"""给 198 某条车道的 litellm Deployment **替换**一个已挂载的 callback 文件（以 live 为底，不走 helm）。

在 198 上以 root 跑::

    python3 litellm-198-lane-callback-replace.py replace  <deploy> <file.py>
    python3 litellm-198-lane-callback-replace.py rollback <deploy> <backup.json>

与 litellm-198-lane-callback-add.py 成对：add 负责新增，本脚本只换内容。
为什么不用 lane-callback-swap.sh：车道一旦被 add 工具带外 patch 过（live 引用的 CM ≠ helm
manifest），swap 的第 4 步会拒绝——它从冻结快照重渲染，会把带外改动静默回退。

每步断言：
1. callbacks CM：复制 live CM 全部 key，只把 <file> 换成新内容 ⇒ 新 CM
   （断言 key 集合不变、只有这一个 key 的内容变了、新内容 sha == 本地文件 sha）
2. Deployment 一次 JSON patch：只把 callbacks volume 的 CM 名换掉（RollingUpdate maxUnavailable=0，零中断）
3. rollout status；之后用 verify 子命令或 add 工具的 verify 核每个 pod 的 sha

改前整份 Deployment 存 /root/cb-add-backup/<deploy>.<ts>.json，rollback 把 volumes 原样还原（旧 CM 不删）。
"""
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


def volume_index(spec, name):
    for i, v in enumerate(spec["volumes"]):
        if v["name"] == name:
            return i
    sys.exit(f"volume {name} not found")


def replace(deploy, path):
    fname = os.path.basename(path)
    content = open(path, encoding="utf-8").read()
    want = hashlib.sha256(content.encode()).hexdigest()[:12]
    d = get_json("deploy", deploy)
    spec = d["spec"]["template"]["spec"]
    assert d["spec"]["strategy"]["type"] == "RollingUpdate"
    assert d["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] in (0, "0"), "maxUnavailable must be 0"
    c = spec["containers"][0]
    assert any(m.get("subPath") == fname for m in c["volumeMounts"]), f"{fname} not mounted on {deploy}"

    vi = volume_index(spec, "callbacks")
    old_cm = spec["volumes"][vi]["configMap"]["name"]
    data = get_json("cm", old_cm)["data"]
    assert fname in data, f"{fname} not in {old_cm}"
    if hashlib.sha256(data[fname].encode()).hexdigest()[:12] == want:
        print(f"{deploy}: {fname} already {want}, nothing to do")
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    bk = f"{BACKUP_DIR}/{deploy}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    json.dump(d, open(bk, "w"))
    print(f"backup: {bk}")

    new_data = dict(data)
    new_data[fname] = content
    assert set(new_data) == set(data)
    assert [k for k in data if data[k] != new_data[k]] == [fname]
    base = old_cm.rsplit("-", 1)[0] if len(old_cm.rsplit("-", 1)[-1]) == 12 else old_cm
    new_cm = f"{base}-{sha12(new_data)}"
    obj = {"apiVersion": "v1", "kind": "ConfigMap",
           "metadata": {"name": new_cm, "namespace": NS,
                        "labels": {"carher.io/cb-replace-from": old_cm[:63]}},
           "data": new_data}
    kc("create", "-f", "-", stdin=json.dumps(obj))
    got = hashlib.sha256(get_json("cm", new_cm)["data"][fname].encode()).hexdigest()[:12]
    assert got == want, (got, want)
    print(f"  created CM {new_cm} ({len(new_data)} keys, {fname}={want})")

    ops = [{"op": "test", "path": f"/spec/template/spec/volumes/{vi}/configMap/name", "value": old_cm},
           {"op": "replace", "path": f"/spec/template/spec/volumes/{vi}/configMap/name", "value": new_cm}]
    kc("patch", "deploy", deploy, "--type", "json", "-p", json.dumps(ops))
    print(f"patch: callbacks {old_cm} -> {new_cm}")
    print(kc("rollout", "status", f"deploy/{deploy}", "--timeout=1500s").strip().splitlines()[-1])
    print(f"ROLLBACK: python3 {sys.argv[0]} rollback {deploy} {bk}")


def rollback(deploy, bk):
    old = json.load(open(bk))
    ops = [{"op": "replace", "path": "/spec/template/spec/volumes",
            "value": old["spec"]["template"]["spec"]["volumes"]}]
    kc("patch", "deploy", deploy, "--type", "json", "-p", json.dumps(ops))
    print(kc("rollout", "status", f"deploy/{deploy}", "--timeout=1500s").strip().splitlines()[-1])


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["replace"] and len(a) == 3:
        replace(a[1], a[2])
    elif a[:1] == ["rollback"] and len(a) == 3:
        rollback(a[1], a[2])
    else:
        sys.exit(__doc__)
