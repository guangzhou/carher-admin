#!/usr/bin/env python3
"""改 198 某条车道前后的「不覆盖别人变更」守卫。198 上 sudo 跑。

  snapshot <out.json> [deploy]       记录车道现状：Deployment generation/resourceVersion、CM 名、每个 callback
                                     文件 sha、config.yaml 的 callbacks 列表、env、mounts、image
  verify-pre <snap.json> [deploy]    动手前再读一次，和快照逐项相等（期间有人动 ⇒ exit 1）
  verify-post <snap.json> [deploy]   装完：只许「多 sec_redact.py + 列表多一行 + 多一个 mount + CM 换名」
  verify-replace <snap.json> [deploy] 换内容：只许 sec_redact.py 内容变
  verify-env <snap.json> [deploy] NAME=VALUE...  只许追加这些 env（别的全等）

deploy 默认 litellm-proxy-gray。只读，不改任何对象。
"""
import hashlib
import json
import os
import subprocess
import sys

import yaml

NS = "litellm-product"
ENV = {"KUBECONFIG": "/etc/rancher/k3s/k3s.yaml", "PATH": "/usr/local/bin:/usr/bin:/bin"}
NEW_FILE = "sec_redact.py"
NEW_ENTRY = "sec_redact.sec_redact"


def kj(*a):
    return json.loads(subprocess.run(["kubectl", "-n", NS, "get", *a, "-o", "json"], capture_output=True,
                                     text=True, check=True, env=ENV).stdout)


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def state(deploy):
    d = kj("deploy", deploy)
    spec = d["spec"]["template"]["spec"]
    c = spec["containers"][0]
    vols = {v["name"]: v.get("configMap", {}).get("name") for v in spec["volumes"]}
    cfg_mount = next(m for m in c["volumeMounts"] if m["mountPath"] == "/app/config.yaml")
    cb_data = kj("cm", vols["callbacks"])["data"]
    cfg_data = kj("cm", vols[cfg_mount["name"]])["data"]
    cfg_text = cfg_data[cfg_mount["subPath"]]
    cfg = yaml.safe_load(cfg_text)
    return {
        "deploy": deploy,
        "generation": d["metadata"]["generation"],
        "resourceVersion": d["metadata"]["resourceVersion"],
        "image": c["image"],
        "replicas": d["spec"]["replicas"],
        "env": c.get("env", []),
        "envFrom": c.get("envFrom", []),
        "mounts": c["volumeMounts"],
        "volumes_cm": vols,
        "other_volumes": [v for v in spec["volumes"] if "configMap" not in v],
        "callback_files": {k: sha(v) for k, v in cb_data.items()},
        "config_other_keys": {k: sha(v) for k, v in cfg_data.items() if k != cfg_mount["subPath"]},
        "callbacks_list": cfg["litellm_settings"]["callbacks"],
        "config_rest_sha": sha(json.dumps({**cfg, "litellm_settings": {k: v for k, v in cfg["litellm_settings"].items()
                                                                       if k != "callbacks"}}, sort_keys=True,
                                          default=str)),
        "config_text_len": len(cfg_text),
    }


def diff(a, b, ignore=()):
    return {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if k not in ignore and a.get(k) != b.get(k)}


def fail(tag, bad):
    print(f"{tag} FAILED:\n  " + "\n  ".join(bad))
    sys.exit(1)


def main():
    cmd, path = sys.argv[1], sys.argv[2]
    deploy = sys.argv[3] if len(sys.argv) > 3 and "=" not in sys.argv[3] else "litellm-proxy-gray"
    now = state(deploy)
    if cmd == "snapshot":
        json.dump(now, open(path, "w"), indent=1)
        os.chmod(path, 0o600)
        print(f"snapshot {deploy}: gen={now['generation']} rv={now['resourceVersion']} files={len(now['callback_files'])} "
              f"callbacks={len(now['callbacks_list'])} cb_cm={now['volumes_cm']['callbacks']}")
        return
    snap = json.load(open(path))
    assert snap.get("deploy", "litellm-proxy-gray") == deploy, (snap.get("deploy"), deploy)
    fixed = ("image", "envFrom", "replicas", "other_volumes")
    if cmd == "verify-pre":
        d = diff(snap, now)
        if d:
            print("CHANGED SINCE SNAPSHOT — do not proceed:", json.dumps(d, ensure_ascii=False)[:2000])
            sys.exit(1)
        print(f"verify-pre OK: {deploy} unchanged since snapshot")
        return
    if cmd == "verify-post":
        bad = []
        extra = set(now["callback_files"]) - set(snap["callback_files"])
        missing = set(snap["callback_files"]) - set(now["callback_files"])
        changed = [k for k in snap["callback_files"] if now["callback_files"].get(k) != snap["callback_files"][k]]
        if extra != {NEW_FILE} or missing or changed:
            bad.append(f"callback files: extra={extra} missing={missing} changed={changed}")
        if now["callbacks_list"] != snap["callbacks_list"] + [NEW_ENTRY] and not (
                NEW_ENTRY in now["callbacks_list"]
                and [x for x in now["callbacks_list"] if x != NEW_ENTRY] == snap["callbacks_list"]
                and now["callbacks_list"].count(NEW_ENTRY) == 1):
            bad.append(f"callbacks list: before={len(snap['callbacks_list'])} after={len(now['callbacks_list'])}")
        if now["config_rest_sha"] != snap["config_rest_sha"]:
            bad.append("config.yaml (non-callback part) changed")
        if now["config_other_keys"] != snap["config_other_keys"]:
            bad.append("config CM other keys changed")
        add = [m for m in now["mounts"] if m not in snap["mounts"]]
        rm = [m for m in snap["mounts"] if m not in now["mounts"]]
        if rm or len(add) != 1 or add[0].get("mountPath") != f"/app/{NEW_FILE}":
            bad.append(f"mounts: added={add} removed={rm}")
        for k in fixed + ("env",):
            if now[k] != snap[k]:
                bad.append(f"{k} changed")
        vd = diff(snap["volumes_cm"], now["volumes_cm"], ignore=("callbacks", "config"))
        if vd:
            bad.append(f"other CM volumes changed: {vd}")
        if bad:
            fail("verify-post", bad)
        print(f"verify-post OK {deploy}: only +{NEW_FILE}, +1 callback line, +1 mount; "
              f"{len(snap['callback_files'])} existing files / {len(snap['callbacks_list'])} callbacks / "
              f"config rest / image / env unchanged")
        return
    if cmd == "verify-replace":
        bad = []
        cf_a, cf_b = snap["callback_files"], now["callback_files"]
        if set(cf_a) != set(cf_b):
            bad.append(f"callback file set changed: +{set(cf_b)-set(cf_a)} -{set(cf_a)-set(cf_b)}")
        changed = [k for k in cf_a if cf_b.get(k) != cf_a[k]]
        if changed != [NEW_FILE]:
            bad.append(f"changed files = {changed} (expected only {NEW_FILE})")
        for k in ("callbacks_list", "config_rest_sha", "config_other_keys", "mounts", "env") + fixed:
            if now[k] != snap[k]:
                bad.append(f"{k} changed")
        vd = diff(snap["volumes_cm"], now["volumes_cm"], ignore=("callbacks",))
        if vd:
            bad.append(f"CM volumes other than callbacks changed: {vd}")
        if bad:
            fail("verify-replace", bad)
        print(f"verify-replace OK {deploy}: only {NEW_FILE} content changed; {len(cf_a)-1} other files, "
              f"{len(snap['callbacks_list'])} callbacks, config, mounts, image, env unchanged")
        return
    if cmd == "verify-env":
        want = dict(a.split("=", 1) for a in sys.argv[3:] if "=" in a)
        bad = []
        before = {e["name"]: e for e in snap["env"]}
        after = {e["name"]: e for e in now["env"]}
        for k, v in want.items():
            if after.get(k, {}).get("value") != v:
                bad.append(f"env {k} != {v!r} (got {after.get(k)})")
        for k in set(before) | set(after):
            if k in want:
                continue
            if before.get(k) != after.get(k):
                bad.append(f"env {k} changed: {before.get(k)} -> {after.get(k)}")
        for k in ("callback_files", "callbacks_list", "config_rest_sha", "config_other_keys", "mounts", "volumes_cm") + fixed:
            if now[k] != snap[k]:
                bad.append(f"{k} changed")
        if bad:
            fail("verify-env", bad)
        print(f"verify-env OK {deploy}: only env {sorted(want)} set; everything else unchanged")
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
