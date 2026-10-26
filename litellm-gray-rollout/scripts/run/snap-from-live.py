#!/usr/bin/env python3
"""从 live 车道取快照，产出 freeze.sh / prepare-values.py 要的 inputs 目录（198 root 跑，只读集群）。

    snap-from-live.py --out $D/gray-inputs --prev-shape <上一轮冻结的 prod-deployment.yaml> \
        --overlays-dir /root/l3/rev1106/ov-1064/out --carry-gray-extras
    snap-from-live.py --out $D/prod-target --prev-shape <...> --overlays-dir <...>          # prod 目标
    snap-from-live.py --out $D/go-inputs   --prev-shape <...>                              # 保险丝 = 上一版覆盖件

产出：<out>/snap/{config,callbacks,hooks,chatgpt-noauth,deepcopy-patch,streaming-handler-patch,
      passthrough-streaming-handler-patch,anthropic-logging-patch}/ + <out>/prod-deployment.yaml + secret-metadata.json

为什么这么取（10-26 v1.106.4 踩出来的）：
- config / callbacks / hooks 等一律取 **live prod 挂载的 CM**（SOP §3.1：以 live prod 为底；10-17 取 live gray 少 3 个模型）。
- 三个整文件覆盖件给 --overlays-dir 时换成新版重做的那套（§5：不能取 live prod 的旧版，rev9 就这么错过）；
  不给 = 照抄 live prod（保险丝跑上一版时就该这样）。
- Deployment 形状取 **上一轮冻结的那份**（--prev-shape），不取 live：live 的卷名是 helm 渲染的 `snapshot-*`，
  prepare-values 认不出（报 `volume snapshot-… has no --snapshot`）。再按快照补新 callback 的 mount、去掉悬空 mount
  （subPath 不在 callbacks CM 里，例 `websearch_you_com.py`，否则 prepare-values 报 `mounts … which the audited directory lacks`）。
- --carry-gray-extras：live gray 有而 prod 没有的 callback（例 sec_redact 影子）连同文件、列表条目（**照 live gray 的位置**，
  放末尾会被形状门禁报 ORDER）、mount、gray 独有 env 一起带上。prepare-values 自己会给 gray 加 disable_reset_budget 等抑制项。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys

import yaml

NS = "litellm-product"
ENV = dict(os.environ, KUBECONFIG=os.environ.get("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"))
# live 卷名 → 快照子目录（prepare-values 的 --snapshot 名）
VOLUMES = {"config": "config", "callbacks": "callbacks", "snapshot-hooks": "hooks",
           "snapshot-chatgpt-noauth": "chatgpt-noauth", "snapshot-deepcopy-patch": "deepcopy-patch",
           "snapshot-streaming-handler-patch": "streaming-handler-patch",
           "snapshot-passthrough-streaming-handler-patch": "passthrough-streaming-handler-patch",
           "snapshot-anthropic-logging-patch": "anthropic-logging-patch"}
# --overlays-dir 里的文件名 → (快照子目录, 快照里的文件名)
OVERLAYS = {"core_streaming_handler.py": ("streaming-handler-patch", "streaming_handler.py"),
            "passthrough_streaming_handler.py": ("passthrough-streaming-handler-patch", "streaming_handler.py"),
            "anthropic_passthrough_logging_handler.py": ("anthropic-logging-patch", "anthropic_passthrough_logging_handler.py")}
GRAY_ONLY_ENV_SKIP = {"PROXY_BATCH_POLLING_ENABLED"}  # prepare-values 按 profile 覆盖，别从 live gray 抄


def kubectl_json(*args: str) -> dict:
    out = subprocess.run(["kubectl", "-n", NS, *args, "-o", "json"], capture_output=True, text=True, env=ENV, check=True)
    return json.loads(out.stdout)


def mounted_cms(deploy: str) -> tuple[dict, dict]:
    d = kubectl_json("get", "deploy", deploy)
    spec = d["spec"]["template"]["spec"]
    mounted = {m["name"] for c in spec["containers"] for m in c.get("volumeMounts", [])}
    return {v["name"]: v["configMap"]["name"] for v in spec["volumes"] if "configMap" in v and v["name"] in mounted}, d


def cm_data(name: str) -> dict[str, str]:
    return kubectl_json("get", "cm", name)["data"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--prev-shape", required=True, help="上一轮冻结的 Deployment yaml（卷名为 config/callbacks/hooks…）")
    ap.add_argument("--secret-metadata", default="/root/l3/up1051-20261016T161310Z/gray-inputs/secret-metadata.json")
    ap.add_argument("--overlays-dir")
    ap.add_argument("--carry-gray-extras", action="store_true")
    ap.add_argument("--source-deploy", default="litellm-proxy")
    a = ap.parse_args()

    snap = os.path.join(a.out, "snap")
    os.makedirs(snap, exist_ok=True)
    os.chmod(a.out, 0o700)
    vols, _ = mounted_cms(a.source_deploy)
    missing = sorted(set(VOLUMES) - set(vols))
    if missing:
        print(f"STOP: {a.source_deploy} 没挂这些卷: {missing}", file=sys.stderr)
        return 1
    for vol, sub in VOLUMES.items():
        p = os.path.join(snap, sub)
        shutil.rmtree(p, ignore_errors=True)
        os.makedirs(p)
        data = cm_data(vols[vol])
        for fn, body in data.items():
            with open(os.path.join(p, fn), "w", encoding="utf-8") as fh:
                fh.write(body)
        print(f"{sub:36s} <- {vols[vol]} ({len(data)})")
    if a.overlays_dir:
        for src, (sub, fn) in OVERLAYS.items():
            p = os.path.join(snap, sub)
            shutil.rmtree(p)
            os.makedirs(p)
            shutil.copy(os.path.join(a.overlays_dir, src), os.path.join(p, fn))
            print(f"{sub:36s} <- overlay {src}")

    shape = yaml.safe_load(open(a.prev_shape, encoding="utf-8"))
    c = shape["spec"]["template"]["spec"]["containers"][0]

    if a.carry_gray_extras:
        gvols, gdep = mounted_cms("litellm-proxy-gray")
        gcb, pcb_dir = cm_data(gvols["callbacks"]), os.path.join(snap, "callbacks")
        extras = sorted(set(gcb) - set(os.listdir(pcb_dir)))
        gcfg = yaml.safe_load(cm_data(gvols["config"])["config.yaml"])["litellm_settings"]["callbacks"]
        cfg_path = os.path.join(snap, "config", "config.yaml")
        text = open(cfg_path, encoding="utf-8").read()
        pcfg = yaml.safe_load(text)["litellm_settings"]["callbacks"]
        for fn in extras:
            with open(os.path.join(pcb_dir, fn), "w", encoding="utf-8") as fh:
                fh.write(gcb[fn])
        for entry in [e for e in gcfg if e not in pcfg]:
            idx = gcfg.index(entry)
            before = next((gcfg[i] for i in range(idx - 1, -1, -1) if gcfg[i] in pcfg), None)
            anchor = f"  - {before}\n" if before else "  callbacks:\n"
            if text.count(anchor) != 1:
                print(f"STOP: 列表锚点 {anchor.strip()!r} 命中 {text.count(anchor)} 次", file=sys.stderr)
                return 1
            text = text.replace(anchor, anchor + f"  - {entry}\n", 1)
            pcfg.insert(pcfg.index(before) + 1 if before else 0, entry)
        assert [e for e in gcfg if e in pcfg] == [e for e in pcfg if e in gcfg], "列表顺序与 live gray 不一致"
        open(cfg_path, "w", encoding="utf-8").write(text)
        gc = gdep["spec"]["template"]["spec"]["containers"][0]
        # mount 由下面「按快照补 mount」统一补（模板 = 已有 callbacks mount），这里不重复加
        have = {e["name"] for e in c.get("env", [])}
        for e in gc.get("env", []):
            if e["name"] not in have and e["name"] not in GRAY_ONLY_ENV_SKIP:
                c["env"].append(e)
                print(f"carry gray env {e['name']}")
        print(f"carry gray callbacks: {extras}")

    cb = set(os.listdir(os.path.join(snap, "callbacks")))
    tmpl = next(m for m in c["volumeMounts"] if m["name"] == "callbacks" and m.get("subPath"))
    have = {m["subPath"] for m in c["volumeMounts"] if m["name"] == "callbacks" and m.get("subPath")}
    for fn in sorted(cb - have):
        m = copy.deepcopy(tmpl)
        m["mountPath"], m["subPath"] = "/app/" + fn, fn
        c["volumeMounts"].append(m)
    dangling = sorted(have - cb)
    c["volumeMounts"] = [m for m in c["volumeMounts"]
                         if not (m["name"] == "callbacks" and m.get("subPath") and m["subPath"] not in cb)]
    with open(os.path.join(a.out, "prod-deployment.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(shape, fh, sort_keys=False)
    shutil.copy(a.secret_metadata, os.path.join(a.out, "secret-metadata.json"))
    print(f"deployment: +mount {sorted(cb - have)}  -dangling {dangling}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
