"""lane-callback-swap.sh 离线测试：真脚本 + 假 kubectl / helm / $SRC/scripts，不连 198。

钉住 11-01 报告 39/42 的两个坑：
  1. gray 车道的 --deployment 源必须是 gray-deployment.yaml（旧版指着过时的 prod-deployment.yaml）。
     源文件的 callback 挂载 != live ⇒ 动快照之前 STOP。
  2. 冻结 / 门禁 / helm 任一步失败，快照里的文件和 values 都要是原样（旧版先改快照，失败不还原）。

假集群状态在 $STATE：deploy.json、cm-<name>.json、manifest.yaml（helm get manifest）、pod-<file>（pod 里 /app 的内容）。
假 helm upgrade 会把 values 里的 callbacks 写进 manifest / deploy / pod，模拟真的 rollout。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

SWAP = Path(__file__).resolve().parents[1] / "scripts" / "lane-callback-swap.sh"
NS = "litellm-product"
DEP = "litellm-proxy-gray"
REL = "litellm-product-gray"
FILES = {"sec_redact.py": "# sec v1\n", "observability_attempt_pusher.py": "# pusher old\n", "error_sanitize.py": "# es\n"}
OLD_CB = "litellm-product-gray-callbacks-6e42bc936ce6"
CFG_CM = "litellm-product-gray-config-c46496eba7a2"

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import hashlib, json, os, sys
S = os.environ["FAKE_STATE"]
a = [x for x in sys.argv[1:]]
if a[:1] == ["-n"]:
    a = a[2:]
def rd(n): return open(os.path.join(S, n)).read()
dep = json.loads(rd("deploy.json"))
if a[0] == "get" and a[1] == "deploy":
    if "json" in a:
        print(json.dumps(dep)); sys.exit()
    jp = next(x for x in a if x.startswith("jsonpath="))
    if "image" in jp: print(dep["spec"]["template"]["spec"]["containers"][0]["image"], end="")
    elif "replicas" in jp: print(dep["spec"]["replicas"], end="")
    sys.exit()
if a[0] == "get" and a[1] == "cm":
    print(rd(f"cm-{a[2]}.json")); sys.exit()
if a[0] == "get" and a[1] == "po":
    pods = [f"{dep['metadata']['name']}-p{i}" for i in range(dep["spec"]["replicas"])]
    if "--no-headers" in a: print("\n".join(f"{p} 1/1 Running 0 1m" for p in pods))
    else: print(" ".join(pods), end="")
    sys.exit()
if a[0] == "rollout":
    print('deployment "x" successfully rolled out'); sys.exit()
if a[0] == "logs":
    sys.exit()
if a[0] == "exec":
    f = a[-1].rsplit("/", 1)[-1]
    print(hashlib.sha256(rd(f"pod-{f}").encode()).hexdigest() + "  /app/" + f); sys.exit()
sys.stderr.write(f"fake kubectl: unhandled {a}\n"); sys.exit(2)
'''

FAKE_HELM = r'''#!/usr/bin/env python3
import hashlib, json, os, sys, yaml
S = os.environ["FAKE_STATE"]
a = sys.argv[1:]
log = open(os.path.join(S, "helm.log"), "a"); log.write(" ".join(a[:2]) + "\n"); log.close()
def render(values_path, rel):
    v = yaml.safe_load(open(values_path))
    data = v["callbacks"]["data"]
    h = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
    cb = f"{rel}-callbacks-{h}"
    base = json.load(open(os.path.join(S, "deploy.json")))
    base["spec"]["replicas"] = v["replicas"]
    for vol in base["spec"]["template"]["spec"]["volumes"]:
        if vol["name"] == "callbacks": vol["configMap"]["name"] = cb
    docs = [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": base["metadata"]["name"], "namespace": "litellm-product"},
             "spec": base["spec"]},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": cb, "namespace": "litellm-product"}, "data": data},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": os.environ["FAKE_CFG_CM"], "namespace": "litellm-product"},
             "data": {"config.yaml": "x: 1\n"}}]
    return docs, cb, data, base
if a[:2] == ["get", "manifest"]:
    print(open(os.path.join(S, "manifest.yaml")).read()); sys.exit()
vals = a[a.index("--values") + 1]
if a[0] == "template":
    docs, *_ = render(vals, a[1]); print(yaml.safe_dump_all(docs, sort_keys=False)); sys.exit()
if a[0] == "upgrade":
    if os.path.exists(os.path.join(S, "helm-upgrade-fail")):
        sys.stderr.write("Error: UPGRADE FAILED: simulated\n"); sys.exit(1)
    docs, cb, data, base = render(vals, a[1])
    open(os.path.join(S, "manifest.yaml"), "w").write(yaml.safe_dump_all(docs, sort_keys=False))
    json.dump(base, open(os.path.join(S, "deploy.json"), "w"))
    json.dump({"metadata": {"name": cb}, "data": data}, open(os.path.join(S, f"cm-{cb}.json"), "w"))
    for f, t in data.items(): open(os.path.join(S, f"pod-{f}"), "w").write(t)
    print("REVISION: 33\nSTATUS: deployed"); sys.exit()
sys.stderr.write(f"fake helm: unhandled {a}\n"); sys.exit(2)
'''

FAKE_SCHED = r'''#!/usr/bin/env python3
import os, sys
if os.path.exists(os.path.join(os.environ["FAKE_STATE"], "freeze-fail")):
    sys.stderr.write("collect-scheduler-evidence: prepare-values --emit-bindings exited 1\n"); sys.exit(1)
a = sys.argv[1:]
open(a[a.index("--evidence-output") + 1], "w").write("{}")
'''

FAKE_PREPARE = r'''#!/usr/bin/env python3
import os, sys, yaml
a = sys.argv[1:]
cbdir = a[a.index("--callbacks-dir") + 1]
prof = a[a.index("--profile") + 1]
data = {n: open(os.path.join(cbdir, n)).read() for n in sorted(os.listdir(cbdir))}
mode = "primary" if prof.endswith("values-prod.yaml") else "disabled"
yaml.safe_dump({"replicas": 9, "schedulerSafety": {"mode": mode}, "callbacks": {"data": data}},
               open(a[a.index("--output") + 1], "w"), sort_keys=False)
print("prepared")
'''

FAKE_DELSET = r'''#!/usr/bin/env python3
import json, sys
a = sys.argv[1:]
json.dump({"status": "PASS", "errors": []}, open(a[a.index("--output") + 1], "w"))
'''


def _x(p: Path, body: str):
    p.write_text(body)
    p.chmod(0o755)


def mount(f):
    return {"mountPath": f"/app/{f}", "name": "callbacks", "readOnly": True, "subPath": f}


def deploy_obj(cb_files, cb_cm=OLD_CB):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": DEP, "namespace": NS},
        "spec": {"replicas": 2, "template": {"metadata": {"annotations": {}}, "spec": {
            "containers": [{"name": "litellm", "image": "127.0.0.1:5000/litellm-carher@sha256:" + "b" * 64,
                            "volumeMounts": [{"mountPath": "/app/config.yaml", "name": "config", "subPath": "config.yaml"}]
                            + [mount(f) for f in sorted(cb_files)]}],
            "volumes": [{"name": "config", "configMap": {"name": CFG_CM}},
                        {"name": "callbacks", "configMap": {"name": cb_cm}}]}}},
    }


@pytest.fixture
def lab(tmp_path):
    bin_ = tmp_path / "bin"; bin_.mkdir()
    _x(bin_ / "kubectl", FAKE_KUBECTL)
    _x(bin_ / "helm", FAKE_HELM)
    src = tmp_path / "src"; (src / "scripts").mkdir(parents=True)
    _x(src / "scripts" / "collect-scheduler-evidence.py", FAKE_SCHED)
    _x(src / "scripts" / "prepare-values.py", FAKE_PREPARE)
    _x(src / "scripts" / "check-release-deletion-set.py", FAKE_DELSET)

    state = tmp_path / "state"; state.mkdir()
    dep = deploy_obj(FILES)
    (state / "deploy.json").write_text(json.dumps(dep))
    (state / f"cm-{OLD_CB}.json").write_text(json.dumps({"metadata": {"name": OLD_CB}, "data": FILES}))
    for f, t in FILES.items():
        (state / f"pod-{f}").write_text(t)
    manifest = [dep, {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": OLD_CB, "namespace": NS}, "data": FILES},
                {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": CFG_CM, "namespace": NS}, "data": {"config.yaml": "x: 1\n"}}]
    (state / "manifest.yaml").write_text(yaml.safe_dump_all(manifest, sort_keys=False))

    run = tmp_path / "run"
    I = run / "gray-inputs"
    (I / "snap" / "callbacks").mkdir(parents=True)
    (I / "snap" / "config").mkdir()
    for f, t in FILES.items():
        (I / "snap" / "callbacks" / f).write_text(t)
    (I / "snap" / "config" / "config.yaml").write_text("x: 1\n")
    (I / "gray-deployment.yaml").write_text(yaml.safe_dump(deploy_obj(FILES)))
    # 10-29 那份过时的源：没有 sec_redact 的 mount（报告 39 失败的根因）
    (I / "prod-deployment.yaml").write_text(yaml.safe_dump(deploy_obj({k: v for k, v in FILES.items() if k != "sec_redact.py"})))
    (run / "gray").mkdir()
    (run / "gray" / "gray-values.yaml").write_text(yaml.safe_dump({"replicas": 2, "callbacks": {"data": FILES}}))
    (run / "chart3").mkdir(); (run / "chart3" / "chart.tgz").write_text("")
    (run / "go-inputs").mkdir(); (run / "go-inputs" / "secret-metadata.json").write_text("{}")
    (run / "target.env").write_text(f"SRC={src}\n")

    newdir = tmp_path / "new"; newdir.mkdir()
    (newdir / "observability_attempt_pusher.py").write_text("# pusher new\n")

    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", FAKE_STATE=str(state), FAKE_CFG_CM=CFG_CM,
               KUBECONFIG=str(tmp_path / "kubeconfig"))
    return {"run": run, "I": I, "state": state, "new": newdir, "env": env}


def swap(lab, script=SWAP):
    r = subprocess.run(["bash", str(script), "--lane", "gray", "--run-dir", str(lab["run"]), "--files-dir", str(lab["new"]),
                        "observability_attempt_pusher.py"], capture_output=True, text=True, env=lab["env"])
    return r.returncode, r.stdout + r.stderr


def snap_file(lab):
    return (lab["I"] / "snap" / "callbacks" / "observability_attempt_pusher.py").read_text()


def values_text(lab):
    return (lab["run"] / "gray" / "gray-values.yaml").read_text()


def helm_calls(lab):
    p = lab["state"] / "helm.log"
    return p.read_text().splitlines() if p.exists() else []


def test_gray_happy_path_updates_live_and_snapshot(lab):
    code, out = swap(lab)
    assert code == 0, out
    assert "deploy source ok" in out and "gray-deployment.yaml" in out
    assert "VERIFIED gray" in out
    assert snap_file(lab) == "# pusher new\n"                       # 成功 ⇒ 快照 == live
    assert (lab["state"] / "pod-observability_attempt_pusher.py").read_text() == "# pusher new\n"
    assert "upgrade litellm-product-gray" in helm_calls(lab)
    assert yaml.safe_load(values_text(lab))["replicas"] == 2       # 副本数沿用车道现值，不被冻结器的 9 覆盖


def test_stale_deploy_source_stops_before_touching_snapshot(lab):
    # 有人把 gray-deployment.yaml 弄旧了（= 旧版脚本读的那份）：没有 sec_redact mount
    shutil.copy(lab["I"] / "prod-deployment.yaml", lab["I"] / "gray-deployment.yaml")
    before_vals = values_text(lab)
    code, out = swap(lab)
    assert code == 1
    assert "STOP" in out and "sec_redact.py" in out
    assert snap_file(lab) == "# pusher old\n"
    assert values_text(lab) == before_vals
    assert not any(c.startswith("upgrade") for c in helm_calls(lab))


def test_freeze_failure_leaves_snapshot_untouched(lab):
    # 报告 39 的现场：冻结那步 exit 1。旧版此时快照里已经是新文件
    (lab["state"] / "freeze-fail").write_text("")
    before_vals = values_text(lab)
    code, out = swap(lab)
    assert code != 0
    assert snap_file(lab) == "# pusher old\n"
    assert values_text(lab) == before_vals
    assert not any(c.startswith("upgrade") for c in helm_calls(lab))


def test_helm_upgrade_failure_restores_snapshot_and_values(lab):
    (lab["state"] / "helm-upgrade-fail").write_text("")
    before_vals = values_text(lab)
    code, out = swap(lab)
    assert code != 0
    assert "RESTORED snapshot + values" in out
    assert snap_file(lab) == "# pusher old\n"
    assert values_text(lab) == before_vals
    assert (lab["state"] / "pod-observability_attempt_pusher.py").read_text() == "# pusher old\n"


def test_out_of_band_live_stops(lab):
    # live 引用的 callbacks CM ≠ helm manifest（有人用 lane-callback-replace 带外换过）⇒ 第 4 步 STOP
    dep = json.loads((lab["state"] / "deploy.json").read_text())
    dep["spec"]["template"]["spec"]["volumes"][1]["configMap"]["name"] = "litellm-product-gray-callbacks-8300c6929b07"
    (lab["state"] / "deploy.json").write_text(json.dumps(dep))
    code, out = swap(lab)
    assert code == 1 and "STOP" in out
    assert snap_file(lab) == "# pusher old\n"
    assert not any(c.startswith("upgrade") for c in helm_calls(lab))


def test_lane_table_matches_render_lane_check():
    """swap / render-lane-check / snap-backfill 三处对「车道的 deployment 源」必须是同一个文件。"""
    swap_src = SWAP.read_text(encoding="utf-8")
    rlc = (SWAP.parent / "run" / "render-lane-check.sh").read_text(encoding="utf-8")
    assert "DEPLOY_SRC=$I/gray-deployment.yaml" in swap_src and "SRCDEP=$I/gray-deployment.yaml" in rlc
    assert "DEPLOY_SRC=$I/prod-deployment.yaml;      MODE=primary" in swap_src and "SRCDEP=$I/prod-deployment.yaml; MODE=primary" in rlc
    assert not [l for l in swap_src.splitlines() if "DEPLOY_SRC=" in l and "prod-live-deployment.yaml" in l]
    bf = (SWAP.parent / "run" / "snap-backfill-from-live.py").read_text(encoding="utf-8")
    assert '"gray": ("litellm-proxy-gray", "gray-inputs", "gray-deployment.yaml")' in bf
    code = [l for l in swap_src.splitlines() if not l.lstrip().startswith("#")]
    assert not [l for l in code if l.lstrip().startswith("declare -A")]   # 本机 bash 3.2 也要能跑本测试


def _has_bash4():
    r = subprocess.run(["bash", "-c", "declare -A x 2>/dev/null && echo ok"], capture_output=True, text=True)
    return r.stdout.strip() == "ok"


@pytest.mark.skipif(not _has_bash4(), reason="旧版用 declare -A，需要 bash>=4 才能做阴性对照（198 上 bash 5.1 可跑）")
def test_negative_control_old_script_leaves_snapshot_dirty(lab, tmp_path):
    old = Path(os.environ.get("OLD_SWAP", ""))
    if not old.is_file():
        pytest.skip("set OLD_SWAP=<旧版 lane-callback-swap.sh> 做阴性对照")
    # 旧版 gray 读 prod-deployment.yaml；给它一份「会让冻结失败」的环境
    (lab["state"] / "freeze-fail").write_text("")
    code, _ = swap(lab, script=old)
    assert code != 0
    assert snap_file(lab) == "# pusher new\n"   # 旧 bug：失败了快照却已被改
