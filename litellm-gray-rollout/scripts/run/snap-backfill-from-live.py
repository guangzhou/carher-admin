#!/usr/bin/env python3
"""把 live 车道上的带外改动回填进升级 run 的车道快照（198 root），让「快照 → helm」== live。只改快照，不碰集群。

    snap-backfill-from-live.py check|apply <lane> [--run-dir D] [--file X.py ...] [--config] [--env NAME ...]
    snap-backfill-from-live.py restore <lane> <backup_dir> [--run-dir D]

  lane       gray | prod | guarded-old（目录约定同 render-lane-check.sh）
  --file     callbacks CM 里的文件，快照那份换成 live 那份（live 没有 ⇒ 从快照删掉）
  --config   config.yaml 换成 live 那份；去掉 prepare-values 会自己注入的行（disable_reset_budget）
  --env      Deployment 源（<inputs>/*-deployment.yaml）里这条 env 对齐 live：live 没有就删、值不同就改、快照没有就加
             **逐行编辑**，不整份重写 yaml（别的会话的文件，格式和其余内容逐字节不动）；改完回读断言结构 == 预期

为什么要它（10-20 起反复踩）：lane-callback-add / lane-callback-replace / set env 都是 helm 带外改动。
不回填 ⇒ 下一次谁对这条车道走 helm（tune / swap / 整版本升级），就会把这些改动静默冲掉。
10-29 up1064 快照落后 live 三处：别的会话改的 budget_notice / kiro_slots_identity / config（kiro→aws-legacy），
以及 sec_redact 的代码和 SEC_REDACT_SHADOW。

纪律：
  * 只回填你点名的东西；不点名 = 不动。先 render-lane-check.sh 看 DIFF 清单，逐条判来源再点名
  * 回填别的会话的改动前，先证明三车道 live 彼此一致（--file/--config 时自动比 prod/gray，不一致就停：对方可能做到一半）
  * apply 前整份备份到 <inputs>/backfill-<ts>-live/；restore 原样拷回
  * 验收 = 之后 render-lane-check.sh <lane> 打印 RENDER == LIVE
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

import yaml

NS = "litellm-product"
ENV = dict(os.environ, KUBECONFIG=os.environ.get("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"))
LANES = {
    "prod": ("litellm-proxy", "prod-target", "prod-deployment.yaml"),
    "gray": ("litellm-proxy-gray", "gray-inputs", "gray-deployment.yaml"),
    "guarded-old": ("litellm-proxy-guarded-old", "go-inputs", "prod-deployment.yaml"),
}
# prepare-values 按车道 profile 自己注入的 config 行：快照里不该有（有了反而重复）
INJECTED_CONFIG_LINE = re.compile(r"^\s*disable_reset_budget\s*:")


def kj(*a):
    out = subprocess.run(["kubectl", "-n", NS, "get", *a, "-o", "json"], capture_output=True, text=True, env=ENV)
    if out.returncode:
        sys.exit(f"kubectl get {' '.join(a)}: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()[:12]


def live_lane(dep):
    d = kj("deploy", dep)
    spec = d["spec"]["template"]["spec"]
    vols = {v["name"]: (v.get("configMap") or {}).get("name") for v in spec["volumes"]}
    return {
        "callbacks": kj("cm", vols["callbacks"])["data"],
        "config": kj("cm", vols["config"])["data"]["config.yaml"],
        "env": {e["name"]: e.get("value") for e in spec["containers"][0].get("env", []) if "valueFrom" not in e},
    }


def strip_injected(cfg: str) -> str:
    return "".join(l for l in cfg.splitlines(True) if not INJECTED_CONFIG_LINE.match(l))


def strip_lane_specific(cfg: str, files) -> str:
    """跨车道比较用：去掉注入行和「本来就只在某条车道」的 callback 条目。"""
    drop = {f"- {os.path.splitext(f)[0]}." for f in files}
    return "".join(l for l in strip_injected(cfg).splitlines(True) if not any(l.strip().startswith(p) for p in drop))


def container_env_list(doc):
    return doc["spec"]["template"]["spec"]["containers"][0].setdefault("env", [])


def edit_env_lines(text: str, name: str, value):
    """在 Deployment yaml 文本里对一条 env 做最小编辑。value=None ⇒ 删。返回新文本。"""
    lines = text.splitlines(True)
    idx = [i for i, l in enumerate(lines) if re.match(rf"^\s*- name: ['\"]?{re.escape(name)}['\"]?\s*$", l)]
    if len(idx) > 1:
        sys.exit(f"env {name} appears {len(idx)} times in deployment source; edit by hand")
    if idx:
        i = idx[0]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not re.match(r"^\s*value:", nxt):
            sys.exit(f"env {name}: line after '- name' is not 'value:' ({nxt.strip()!r}); edit by hand")
        if value is None:
            return "".join(lines[:i] + lines[i + 2:])
        indent = re.match(r"^(\s*)", nxt).group(1)
        lines[i + 1] = f"{indent}value: {json.dumps(str(value))}\n"
        return "".join(lines)
    if value is None:
        return text
    # 追加到 container env 列表最后一项之后（找最后一个 env 条目的缩进）
    env_hdr = [i for i, l in enumerate(lines) if re.match(r"^\s*env:\s*$", l)]
    if len(env_hdr) != 1:
        sys.exit(f"cannot locate a single 'env:' block to add {name}; edit by hand")
    h = env_hdr[0]
    item = re.compile(r"^(\s*)- name: ")
    first = next((j for j in range(h + 1, len(lines)) if item.match(lines[j])), None)
    if first is None:
        sys.exit(f"'env:' block is empty; edit by hand to add {name}")
    ind = item.match(lines[first]).group(1)
    j = first
    while j + 1 < len(lines) and (lines[j + 1].startswith(ind + " ") or lines[j + 1].startswith(ind + "- ")):
        j += 1
    ins = [f"{ind}- name: {name}\n", f"{ind}  value: {json.dumps(str(value))}\n"]
    return "".join(lines[: j + 1] + ins + lines[j + 1:])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["check", "apply", "restore"])
    ap.add_argument("lane", choices=list(LANES))
    ap.add_argument("backup", nargs="?")
    ap.add_argument("--run-dir")
    ap.add_argument("--file", action="append", default=[])
    ap.add_argument("--config", action="store_true")
    ap.add_argument("--env", action="append", default=[])
    a = ap.parse_args()
    R = a.run_dir or open("/root/l3/.gray-run-latest").read().strip()
    dep, inputs, dep_yaml = LANES[a.lane]
    I = f"{R}/{inputs}"
    snap = f"{I}/snap"
    src = f"{I}/{dep_yaml}"

    if a.cmd == "restore":
        bk = a.backup or sys.exit("restore needs <backup_dir>")
        for sub in ("callbacks", "config"):
            if os.path.isdir(f"{bk}/{sub}"):
                shutil.rmtree(f"{snap}/{sub}")
                shutil.copytree(f"{bk}/{sub}", f"{snap}/{sub}")
        if os.path.exists(f"{bk}/{dep_yaml}"):
            shutil.copy2(f"{bk}/{dep_yaml}", src)
        print("restored", I, "from", bk)
        return
    if not (a.file or a.config or a.env):
        sys.exit("nothing named: pass --file / --config / --env (run render-lane-check.sh first to see the DIFF list)")

    live = live_lane(dep)
    if a.file or a.config:
        # 回填前：prod / gray 两条 live 车道这些东西必须一致（别的会话可能只改了一半）
        other = "gray" if a.lane != "gray" else "prod"
        lo = live_lane(LANES[other][0])
        for f in a.file:
            if live["callbacks"].get(f) != lo["callbacks"].get(f):
                print(f"!! {f}: {a.lane}={sha(live['callbacks'].get(f, ''))} {other}={sha(lo['callbacks'].get(f, ''))} "
                      f"—— 两条车道不一致。若这是只装在 {a.lane} 的件（如只在 gray 的新 callback）可继续，否则先查清", file=sys.stderr)
        lane_only = [f for f in live["callbacks"] if f not in lo["callbacks"]] + [f for f in lo["callbacks"] if f not in live["callbacks"]]
        if a.config and strip_lane_specific(live["config"], lane_only) != strip_lane_specific(lo["config"], lane_only):
            sys.exit(f"STOP: config.yaml differs between {a.lane} and {other} beyond lane-only callbacks/injected lines")

    changes = []
    for f in a.file:
        p = f"{snap}/callbacks/{f}"
        cur = open(p).read() if os.path.exists(p) else None
        want = live["callbacks"].get(f)
        if cur != want:
            changes.append(("file", f, sha(cur or ""), sha(want or "") if want is not None else "DELETE"))
    want_cfg = strip_injected(live["config"]) if a.config else None
    if a.config and open(f"{snap}/config/config.yaml").read() != want_cfg:
        changes.append(("config", "config.yaml", sha(open(f"{snap}/config/config.yaml").read()), sha(want_cfg)))
    text = open(src).read()
    new_text = text
    for name in a.env:
        new_text = edit_env_lines(new_text, name, live["env"].get(name))
    if new_text != text:
        # 回读断言：结构上 == 原对象只改了这几条 env
        exp = list(yaml.safe_load_all(text))
        for doc in exp:
            if doc and doc.get("kind") == "Deployment":
                env = container_env_list(doc)
                for name in a.env:
                    v = live["env"].get(name)
                    env[:] = [e for e in env if e.get("name") != name] if v is None else env
                    if v is not None:
                        hit = [e for e in env if e.get("name") == name]
                        if hit:
                            hit[0]["value"] = str(v)
                        else:
                            env.append({"name": name, "value": str(v)})
        got = list(yaml.safe_load_all(new_text))
        if got != exp:
            sys.exit("STOP: line edit of env did not produce the expected structure; edit by hand")
        changes.append(("env", ",".join(a.env), src, "edited"))
    for c in changes:
        print(f"{a.lane}: {c[0]} {c[1]}: {c[2]} -> {c[3]}")
    if not changes:
        print(f"{a.lane}: snapshot already matches live for everything named")
        return
    if a.cmd != "apply":
        return
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bk = f"{I}/backfill-{ts}-live"
    os.makedirs(bk)
    shutil.copytree(f"{snap}/callbacks", f"{bk}/callbacks")
    shutil.copytree(f"{snap}/config", f"{bk}/config")
    shutil.copy2(src, f"{bk}/{dep_yaml}")
    for kind, f, *_ in changes:
        if kind == "file":
            p = f"{snap}/callbacks/{f}"
            want = live["callbacks"].get(f)
            if want is None:
                os.remove(p)
            else:
                open(p, "w").write(want)
        elif kind == "config":
            open(f"{snap}/config/config.yaml", "w").write(want_cfg)
        elif kind == "env":
            open(src, "w").write(new_text)
    print(f"applied; backup {bk}")
    print(f"next: bash render-lane-check.sh {a.lane} {R}    # must print RENDER == LIVE")


if __name__ == "__main__":
    main()
