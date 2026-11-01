"""litellm-198-gray-change-guard.py verify-replace 离线测试（不连 198）。

假 kubectl：GUARD_KUBECTL 指向一个小脚本，按 `get <kind> <name>` 从 $FAKE_KUBE_DIR/<kind>-<name>.json 读。
每条用例都是「snapshot（改前集群）→ 换成改后集群 → verify-replace」走真 CLI，判 exit code + 输出。
阳性对照：报告 42 那次 pusher 换代的真形状（只换 callbacks 卷名 + 只变一个 key）必须绿；
每种越界（多变一个 key、改 mount、改 env、config 变、CM 没换名、点错文件、本地 sha 不符）必须红。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import subprocess
import sys

import pytest

GUARD = pathlib.Path(__file__).resolve().parents[1] / "litellm-198-gray-change-guard.py"
DEP = "litellm-proxy"

FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
i = a.index("get")
kind, name = a[i + 1], a[i + 2]
p = os.path.join(os.environ["FAKE_KUBE_DIR"], f"{kind}-{name}.json")
if not os.path.exists(p):
    sys.stderr.write(f"NotFound {kind}/{name}\\n"); sys.exit(1)
sys.stdout.write(open(p).read())
"""

CFG = "litellm_settings:\n  callbacks:\n  - error_sanitize.error_sanitize\n  - observability_attempt_pusher.pusher\n" \
      "general_settings:\n  master_key: os.environ/LITELLM_MASTER_KEY\n"
FILES = {
    "error_sanitize.py": "# es v1\n",
    "observability_attempt_pusher.py": "# pusher old\n",
    "video_id_alias_auth.py": "# vaa\n",
}


def mount(f):
    return {"mountPath": f"/app/{f}", "name": "callbacks", "readOnly": True, "subPath": f}


def cluster(cb_cm="litellm-stable-callbacks-8373e2ce2912", files=None, cfg=CFG, env=None, mounts=None):
    files = dict(FILES if files is None else files)
    deploy = {
        "metadata": {"name": DEP, "generation": 987, "resourceVersion": "100"},
        "spec": {
            "replicas": 4,
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
            "template": {
                "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "2026-10-16T09:08:37+08:00"}},
                "spec": {
                    "containers": [{
                        "name": "litellm",
                        "image": "127.0.0.1:5000/litellm-carher@sha256:" + "a" * 64,
                        "env": env if env is not None else [{"name": "OBSERVABILITY_INGEST_URL", "value": "http://x"}],
                        "envFrom": [{"secretRef": {"name": "litellm-secrets-r2"}}],
                        "volumeMounts": mounts if mounts is not None else
                        [{"mountPath": "/app/config.yaml", "name": "config", "subPath": "config.yaml"}]
                        + [mount(f) for f in sorted(FILES)],
                    }],
                    "volumes": [
                        {"name": "config", "configMap": {"name": "litellm-product-proxy-config-b9160a8f69da"}},
                        {"name": "callbacks", "configMap": {"name": cb_cm}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    }
    return {
        f"deploy-{DEP}": deploy,
        f"cm-{cb_cm}": {"metadata": {"name": cb_cm}, "data": files},
        "cm-litellm-product-proxy-config-b9160a8f69da": {"metadata": {}, "data": {"config.yaml": cfg}},
    }


def after_replace(fname="observability_attempt_pusher.py", content="# pusher new\n", new_cm="litellm-stable-callbacks-7f867bbc457e"):
    """lane-callback-replace.py 的真形状：新 CM = 旧 CM 只换一个 key；deploy 只改 callbacks 卷名；generation 涨。"""
    files = dict(FILES, **{fname: content})
    c = cluster(cb_cm=new_cm, files=files)
    c[f"deploy-{DEP}"]["metadata"].update(generation=988, resourceVersion="200")
    return c


@pytest.fixture
def env(tmp_path):
    kc = tmp_path / "kubectl"
    kc.write_text(FAKE_KUBECTL)
    kc.chmod(0o755)
    kube = tmp_path / "kube"
    kube.mkdir()
    e = dict(os.environ, GUARD_KUBECTL=str(kc), FAKE_KUBE_DIR=str(kube))
    return tmp_path, kube, e


def load(kube, objs):
    for p in kube.glob("*.json"):
        p.unlink()
    for k, v in objs.items():
        (kube / f"{k}.json").write_text(json.dumps(v))


def guard(e, *args):
    r = subprocess.run([sys.executable, str(GUARD), *args], capture_output=True, text=True, env=e)
    return r.returncode, r.stdout + r.stderr


def snap_then(env, after, *files, strip_spec=False):
    tmp, kube, e = env
    snap = tmp / "snap.json"
    load(kube, cluster())
    code, out = guard(e, "snapshot", str(snap), DEP)
    assert code == 0, out
    if strip_spec:  # 模拟 10-30 之前的旧快照
        d = json.loads(snap.read_text()); d.pop("spec"); snap.write_text(json.dumps(d))
    load(kube, after)
    args = ["verify-replace", str(snap), DEP]
    for f in files:
        args += ["--file", f]
    return guard(e, *args)


# ---- 阳性对照：报告 42 的真实换代必须绿 ----

def test_pusher_replace_passes(env):
    code, out = snap_then(env, after_replace(), "observability_attempt_pusher.py")
    assert code == 0, out
    assert "verify-replace OK" in out and "observability_attempt_pusher.py" in out


def test_any_file_name_not_just_sec_redact(env):
    code, out = snap_then(env, after_replace("error_sanitize.py", "# es v2\n"), "error_sanitize.py")
    assert code == 0, out


def test_local_file_sha_matches(env, tmp_path):
    local = tmp_path / "observability_attempt_pusher.py"
    local.write_text("# pusher new\n")
    code, out = snap_then(env, after_replace(), str(local))
    assert code == 0, out
    assert "local sha matched" in out


def test_verify_pre_still_ok_on_unchanged_cluster(env):
    tmp, kube, e = env
    load(kube, cluster())
    snap = tmp / "s.json"
    assert guard(e, "snapshot", str(snap), DEP)[0] == 0
    code, out = guard(e, "verify-pre", str(snap), DEP)
    assert code == 0, out


# ---- 阴性：每种越界都必须红 ----

def test_requires_file_argument(env):
    code, out = snap_then(env, after_replace())
    assert code != 0 and "--file" in out


def test_wrong_file_declared(env):
    code, out = snap_then(env, after_replace(), "error_sanitize.py")
    assert code == 1 and "changed files" in out


def test_extra_key_changed(env):
    after = after_replace()
    after["cm-litellm-stable-callbacks-7f867bbc457e"]["data"]["video_id_alias_auth.py"] = "# vaa changed\n"
    code, out = snap_then(env, after, "observability_attempt_pusher.py")
    assert code == 1 and "changed files" in out


def test_key_added_or_removed(env):
    after = after_replace()
    del after["cm-litellm-stable-callbacks-7f867bbc457e"]["data"]["video_id_alias_auth.py"]
    code, out = snap_then(env, after, "observability_attempt_pusher.py")
    assert code == 1 and "callback file set changed" in out


def test_cm_not_rotated(env):
    # 原地改 CM（名字不变）：subPath 挂载的 pod 永远看不到新内容
    after = after_replace(new_cm="litellm-stable-callbacks-8373e2ce2912")
    code, out = snap_then(env, after, "observability_attempt_pusher.py")
    assert code == 1 and "did not rotate" in out


def test_mount_dropped(env):
    after = after_replace()
    vm = after[f"deploy-{DEP}"]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    vm[:] = [m for m in vm if m.get("subPath") != "video_id_alias_auth.py"]
    code, out = snap_then(env, after, "observability_attempt_pusher.py")
    assert code == 1 and "mounts changed" in out


def test_env_changed(env):
    after = after_replace()
    after[f"deploy-{DEP}"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "http://y"
    code, out = snap_then(env, after, "observability_attempt_pusher.py")
    assert code == 1 and "env changed" in out


def test_config_line_changed(env):
    after = after_replace()
    after["cm-litellm-product-proxy-config-b9160a8f69da"]["data"]["config.yaml"] = CFG.replace(
        "  - error_sanitize.error_sanitize\n", "")
    code, out = snap_then(env, after, "observability_attempt_pusher.py")
    assert code == 1 and "callbacks_list changed" in out


def test_spec_field_outside_known_keys(env):
    # 字段级检查看不到的地方（strategy / 注解）也要被逐路径比对抓到
    after = after_replace()
    s = after[f"deploy-{DEP}"]["spec"]
    s["strategy"]["rollingUpdate"]["maxUnavailable"] = 1
    s["template"]["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"] = "2026-11-01T00:00:00Z"
    code, out = snap_then(env, after, "observability_attempt_pusher.py")
    assert code == 1
    assert "spec.strategy.rollingUpdate.maxUnavailable" in out and "restartedAt" in out


def test_local_file_sha_mismatch(env, tmp_path):
    local = tmp_path / "observability_attempt_pusher.py"
    local.write_text("# some other build\n")
    code, out = snap_then(env, after_replace(), str(local))
    assert code == 1 and "!= local file" in out


def test_new_file_is_not_a_replace(env):
    after = after_replace()
    after["cm-litellm-stable-callbacks-7f867bbc457e"]["data"]["brand_new.py"] = "# new\n"
    code, out = snap_then(env, after, "brand_new.py")
    assert code == 1 and "lane-callback-add" in out


def test_old_snapshot_without_spec_still_checks_fields(env):
    code, out = snap_then(env, after_replace(), "observability_attempt_pusher.py", strip_spec=True)
    assert code == 0 and "no 'spec'" in out
    after = after_replace()
    after[f"deploy-{DEP}"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "http://y"
    code, out = snap_then(env, after, "observability_attempt_pusher.py", strip_spec=True)
    assert code == 1 and "env changed" in out


# ---- 纯函数 ----

def _load_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_spec_diffs_paths():
    g = _load_module()
    a = cluster()[f"deploy-{DEP}"]["spec"]
    b = copy.deepcopy(a)
    b["template"]["spec"]["volumes"][1]["configMap"]["name"] = "x"
    d = g.spec_diffs(a, b)
    assert [p for p, *_ in d] == ["spec.template.spec.volumes[1].configMap.name"]
    assert g.callbacks_volume_name_path(a, b) == d[0][0]


def test_split_file_args():
    g = _load_module()
    assert g.split_file_args(["verify-replace", "s.json", "--file", "a.py", "dep", "--file=b.py"]) == \
        (["a.py", "b.py"], ["verify-replace", "s.json", "dep"])
    h = hashlib.sha256(b"x").hexdigest()
    assert len(h) == 64
