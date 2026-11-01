#!/usr/bin/env python3
"""改 198 某条车道前后的「不覆盖别人变更」守卫。198 上 sudo 跑。

  snapshot <out.json> [deploy]       记录车道现状：Deployment generation/resourceVersion、整份 spec、CM 名、每个
                                     callback 文件 sha、config.yaml 的 callbacks 列表、env、mounts、image
  verify-pre <snap.json> [deploy]    动手前再读一次，和快照逐项相等（期间有人动 ⇒ exit 1）
  verify-post <snap.json> [deploy]   装完：只许「多 sec_redact.py + 列表多一行 + 多一个 mount + CM 换名」
  verify-replace <snap.json> [deploy] --file X.py [--file Y.py ...]
                                     换内容（lane-callback-replace.py 之后）。--file 必填，可以是任意已挂载的 callback：
                                     * Deployment .spec 逐路径比，只许 callbacks 卷的 configMap.name 变（且必须变）
                                     * callbacks CM 的 key 集合不变，内容变了的 key **恰好** = 点名的这几个
                                     * config CM / config.yaml / mounts / env / image 全等
                                     --file 给的是本地存在的路径 ⇒ 再断言 live 里该 key 的 sha == 这个本地文件
  verify-env <snap.json> [deploy] NAME=VALUE...  只许追加这些 env（别的全等）

deploy 默认 litellm-proxy-gray。只读，不改任何对象。
10-30 之前的快照没有 spec 字段：verify-replace 照样跑字段级检查，并提示没做逐路径比对。
离线测试：scripts/tests/test_gray_change_guard.py（GUARD_KUBECTL 指向假 kubectl）。
"""
import hashlib
import json
import os
import subprocess
import sys

import yaml

NS = "litellm-product"
ENV = {"KUBECONFIG": "/etc/rancher/k3s/k3s.yaml", "PATH": "/usr/local/bin:/usr/bin:/bin"}
KUBECTL = os.environ.get("GUARD_KUBECTL", "kubectl")  # 只给离线测试用：指向假 kubectl
KENV = ENV if "GUARD_KUBECTL" not in os.environ else dict(os.environ)  # 生产仍用固定 ENV
NEW_FILE = "sec_redact.py"
NEW_ENTRY = "sec_redact.sec_redact"


def kj(*a):
    return json.loads(subprocess.run([KUBECTL, "-n", NS, "get", *a, "-o", "json"], capture_output=True,
                                     text=True, check=True, env=KENV).stdout)


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def state(deploy, get=None):
    get = get or kj
    d = get("deploy", deploy)
    spec = d["spec"]["template"]["spec"]
    c = spec["containers"][0]
    vols = {v["name"]: v.get("configMap", {}).get("name") for v in spec["volumes"]}
    cfg_mount = next(m for m in c["volumeMounts"] if m["mountPath"] == "/app/config.yaml")
    cb_data = get("cm", vols["callbacks"])["data"]
    cfg_data = get("cm", vols[cfg_mount["name"]])["data"]
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
        "spec": d["spec"],  # 整份 Deployment .spec，verify-replace 逐路径比（同报告 42 的 specdiff.py）
    }


def diff(a, b, ignore=()):
    return {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if k not in ignore and a.get(k) != b.get(k)}


def fail(tag, bad):
    print(f"{tag} FAILED:\n  " + "\n  ".join(bad))
    sys.exit(1)


def walk(a, b, p, out):
    """逐路径列出 a→b 的差异：[(path, before, after)]。list 按下标比，长度不同单独记一条。"""
    if type(a) is not type(b):
        out.append((p, a, b))
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append((f"{p}.{k}", a.get(k, "<absent>"), b.get(k, "<absent>")))
            else:
                walk(a[k], b[k], f"{p}.{k}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append((p, f"len={len(a)}", f"len={len(b)}"))
        for i, (x, y) in enumerate(zip(a, b)):
            walk(x, y, f"{p}[{i}]", out)
    elif a != b:
        out.append((p, a, b))


def spec_diffs(a, b):
    out = []
    walk(a, b, "spec", out)
    return out


def callbacks_volume_name_path(spec_a, spec_b):
    """两边同一下标都是 callbacks 卷时，它的 configMap.name 路径；否则 None（卷被挪位/增删也算违规）。"""
    va, vb = spec_a["template"]["spec"]["volumes"], spec_b["template"]["spec"]["volumes"]
    for i, (x, y) in enumerate(zip(va, vb)):
        if x.get("name") == "callbacks" and y.get("name") == "callbacks":
            return f"spec.template.spec.volumes[{i}].configMap.name"
    return None


def check_replace(snap, now, files, local_sha=None):
    """verify-replace 的判据（纯函数，离线可测）。返回 (bad, notes)。

    files      点名的 callbacks CM key（文件名）
    local_sha  {key: 全长 sha256}，只含给了本地路径的那几个
    """
    bad, notes = [], []
    names = sorted(set(files))
    if not names:
        return ["no --file given: name the callback file(s) you replaced"], notes
    cf_a, cf_b = snap["callback_files"], now["callback_files"]
    if set(cf_a) != set(cf_b):
        bad.append(f"callback file set changed: +{sorted(set(cf_b) - set(cf_a))} -{sorted(set(cf_a) - set(cf_b))}")
    for f in names:
        if f not in cf_a:
            bad.append(f"{f} was not in the callbacks CM before (new file ⇒ lane-callback-add + verify-post, not replace)")
    changed = sorted(k for k in cf_a if k in cf_b and cf_b[k] != cf_a[k])
    if changed != names:
        bad.append(f"changed files = {changed} (declared {names})")
    for f, want in sorted((local_sha or {}).items()):
        if cf_b.get(f) != want:
            bad.append(f"{f}: live sha {(cf_b.get(f) or '')[:12]} != local file {want[:12]}")
    if now["volumes_cm"].get("callbacks") == snap["volumes_cm"].get("callbacks"):
        bad.append("callbacks CM name did not rotate (in-place CM edit: subPath-mounted pods never see it)")
    for k in ("callbacks_list", "config_rest_sha", "config_other_keys", "mounts", "env",
              "image", "envFrom", "replicas", "other_volumes"):
        if now[k] != snap[k]:
            bad.append(f"{k} changed")
    vd = diff(snap["volumes_cm"], now["volumes_cm"], ignore=("callbacks",))
    if vd:
        bad.append(f"CM volumes other than callbacks changed: {vd}")
    if "spec" in snap and "spec" in now:
        allow = callbacks_volume_name_path(snap["spec"], now["spec"])
        extra = [d for d in spec_diffs(snap["spec"], now["spec"]) if d[0] != allow]
        for p, x, y in extra[:20]:
            bad.append(f"deploy {p}: {str(x)[:100]} -> {str(y)[:100]}")
        if len(extra) > 20:
            bad.append(f"... {len(extra) - 20} more deploy spec diffs")
    else:
        notes.append("snapshot has no 'spec' (taken by an older guard): field-level checks only, no per-path spec diff")
    return bad, notes


def split_file_args(argv):
    """把 --file X / --file=X 从参数里摘出来，返回 (files, 其余位置参数)。"""
    files, rest, i = [], [], 0
    while i < len(argv):
        if argv[i] == "--file":
            if i + 1 >= len(argv):
                sys.exit("--file needs a value")
            files.append(argv[i + 1])
            i += 2
        elif argv[i].startswith("--file="):
            files.append(argv[i][len("--file="):])
            i += 1
        else:
            rest.append(argv[i])
            i += 1
    return files, rest


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    files, argv = split_file_args(sys.argv[1:])
    cmd, path = argv[0], argv[1]
    if files and cmd != "verify-replace":
        sys.exit("--file only applies to verify-replace")
    deploy = argv[2] if len(argv) > 2 and "=" not in argv[2] else "litellm-proxy-gray"
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
        d = diff(snap, now, ignore=() if "spec" in snap else ("spec",))
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
        if not files:
            sys.exit("verify-replace needs --file <callback.py> (one per replaced file; 10-31 前写死 sec_redact.py，已去掉)")
        names, local_sha = [], {}
        for f in files:
            n = os.path.basename(f)
            names.append(n)
            if os.path.isfile(f):
                local_sha[n] = hashlib.sha256(open(f, encoding="utf-8").read().encode()).hexdigest()
        bad, notes = check_replace(snap, now, names, local_sha)
        for n in notes:
            print("  note:", n)
        if bad:
            fail("verify-replace", bad)
        cf_a = snap["callback_files"]
        print(f"verify-replace OK {deploy}: only {sorted(set(names))} content changed "
              f"(cb CM {snap['volumes_cm']['callbacks']} -> {now['volumes_cm']['callbacks']}); "
              f"{len(cf_a) - len(set(names))} other files, {len(snap['callbacks_list'])} callbacks, config, mounts, "
              f"image, env unchanged; deploy spec diff = callbacks volume name only"
              + (f"; local sha matched for {sorted(local_sha)}" if local_sha else ""))
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
