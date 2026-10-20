#!/usr/bin/env python3
"""现量 198 某条 lane 此刻到底有几层 LiteLLM 补丁、分别在哪、谁盖了谁。

为什么要有这个脚本
------------------
2026-10-20 想回答「线上 litellm 被我们改了哪些地方」，发现答案散在四个互不相交的地方，
而且**任何单一一处都读不出全貌**：

  1. callbacks CM 里的 .py（37 个）—— 但 CM 名带内容 hash，`litellm-callbacks` 那个
     base CM **不是线上跑的那份**（live prod 比它多 5 个、少 1 个）
  2. `sitecustomize.py` —— 也在 callbacks CM 里，但**不在 callbacks 列表里**，
     靠 site-packages 自动加载，4 个 patch 全程不出现在任何 callbacks 配置里
  3. 整文件覆盖件（overlay）—— 独立 subPath mount，盖在 site-packages 上
  4. 烘焙进镜像的补丁 —— 不在任何 CM 里，只能把容器里的 site-packages 和上游 tag 逐字节比

所以这个脚本的判据只有一条：**容器里的 site-packages 和上游 tag 逐文件 md5 比**。
这把尺子同时抓到 3 和 4（它们都落在 site-packages 上），再从 Deployment 的 mount 反查
哪些差异是 overlay、剩下的就是烘焙的。1 和 2 则按容器内真实 import 现量。

两个会制造假绿的坑（都踩过）
--------------------------
* **CM 名不许写死**：每条 lane 的 CM 名带内容 hash，改一次换一个名（memory:
  `feedback_derived_cm_name_is_not_content_sha_after_upgrade`）。一律从 Deployment
  的 volumes 反查当前名。
* **import 成功 ≠ 补丁装上了**：猴补丁的源码指纹守卫拒装时，文件 sha 照样对、
  import 照样成功，但上游方法根本没被替换（memory:
  `feedback_guard_must_warn_not_refuse_when_its_ruler_breaks`）。所以 `--probe-chain`
  是去读 `__closure__` 看替换链真的叠了几层，不是只看 import 成不成功。

用法（在能 ssh 198 的机器上跑；读操作，不碰生产）
-----------------------------------------------
  ./litellm-198-patch-layer-map.py --tag v1.105.1                  # 默认 lane=prod
  ./litellm-198-patch-layer-map.py --lane gray --tag v1.105.1
  ./litellm-198-patch-layer-map.py --tag v1.105.1 --probe-chain     # 另外实测替换链叠了几层
  ./litellm-198-patch-layer-map.py --tag v1.105.1 --json

`--tag` 要是 198 `/root/litellm-fork` 里已 fetch 的 tag；线上版本用 `--tag` 之前先现量
（`importlib.metadata.version("litellm")`，脚本会自己核，不一致直接报错不出数）。
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys

HOST = "cltx@10.68.13.198"
NS = "litellm-product"
FORK = "/root/litellm-fork"
LANES = {
    "prod": "litellm-proxy",
    "gray": "litellm-proxy-gray",
    "guarded-old": "litellm-proxy-guarded-old",
}
# 容器里 litellm 包的位置，随 base 镜像的 python 版本变，所以现量而不写死。
PKG_PROBE = (
    "python -c 'import litellm,os,importlib.metadata as m;"
    'print(os.path.dirname(litellm.__file__));print(m.version("litellm"))\''
)


def sh(cmd: str, timeout: int = 180) -> str:
    """在 198 上以 sudo -n 跑一段 bash，返回 stdout；非零退出抛异常。"""
    full = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", HOST, f"sudo -n bash -c {shlex.quote(cmd)}"]
    r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"198 命令失败 rc={r.returncode}: {cmd[:120]}\n{r.stderr[-600:]}")
    return r.stdout


KUBE = "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; "


def lane_pod(deploy: str) -> str:
    """三条 lane 的 selector 实测都是 app=<deploy名>（10-20 核），但仍从 Deployment 反查，不写死。"""
    sel = sh(KUBE + f"kubectl get deploy -n {NS} {deploy} -o jsonpath='{{.spec.selector.matchLabels}}'").strip()
    labels = ",".join(f"{k}={v}" for k, v in json.loads(sel).items()) if sel else f"app={deploy}"
    out = sh(KUBE + f"kubectl get pod -n {NS} -l {shlex.quote(labels)} --field-selector=status.phase=Running "
             "-o jsonpath='{.items[0].metadata.name}'").strip()
    if not out:
        raise RuntimeError(f"lane {deploy} 没有 Running pod（selector {labels}）")
    return out


def mounted_cms(deploy: str) -> dict[str, str]:
    """volume 名 -> ConfigMap 名。CM 名带内容 hash，必须这样反查，不许写死。"""
    out = sh(KUBE + f"kubectl get deploy -n {NS} {deploy} "
             "-o jsonpath='{range .spec.template.spec.volumes[*]}{.name}|{.configMap.name}{\"\\n\"}{end}'")
    cms = {}
    for line in out.splitlines():
        name, _, cm = line.partition("|")
        if cm.strip():
            cms[name.strip()] = cm.strip()
    return cms


def overlay_mounts(deploy: str, pkg_dir: str) -> dict[str, str]:
    """落在 litellm 包里的整文件覆盖件：site-packages 相对路径 -> volume 名。

    判据是 mountPath 落在 litellm 包目录下且带 subPath（单文件挂载）。callbacks 那些
    挂在 /app/*.py，不在包里，所以不会被算成 overlay。
    """
    out = sh(KUBE + f"kubectl get deploy -n {NS} {deploy} "
             "-o jsonpath='{range .spec.template.spec.containers[0].volumeMounts[*]}"
             "{.name}|{.mountPath}|{.subPath}{\"\\n\"}{end}'")
    site = pkg_dir.rsplit("/litellm", 1)[0] + "/"
    res = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        vol, path, sub = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if sub and path.startswith(site + "litellm"):
            res[path[len(site):]] = vol
    return res


def live_vs_upstream(pod: str, pkg_dir: str, tag: str) -> dict[str, list[str]]:
    """容器里的 litellm 包 vs 上游 tag，逐文件 md5。这是抓 overlay + 烘焙补丁的唯一判据。

    md5 在容器里算、在 198 上比，不传文件回本地（155MB）。
    """
    site = pkg_dir.rsplit("/litellm", 1)[0]
    script = f"""
set -euo pipefail
{KUBE.strip()}
kubectl exec -n {NS} {pod} -- sh -c 'cd {shlex.quote(site)} && find litellm -name "*.py" -print0 | xargs -0 md5sum' \
  > /tmp/plm-live.txt
rm -rf /tmp/plm-up && mkdir -p /tmp/plm-up
git -C {FORK} archive {shlex.quote(tag)} litellm | tar -x -C /tmp/plm-up
cd /tmp/plm-up && find litellm -name "*.py" -print0 | xargs -0 md5sum > /tmp/plm-up.txt
python3 - <<'PY'
import json
def load(p):
    d = {{}}
    for line in open(p):
        line = line.rstrip("\\n")
        if not line:
            continue
        h, _, f = line.partition(" ")
        d[f.strip()] = h.strip()
    return d
live, up = load("/tmp/plm-live.txt"), load("/tmp/plm-up.txt")
print(json.dumps({{
    "differs": sorted(f for f in set(live) & set(up) if live[f] != up[f]),
    "only_live": sorted(set(live) - set(up)),
    "only_upstream": sorted(set(up) - set(live)),
    "counted": len(live),
}}))
PY
"""
    return json.loads(sh(script, timeout=420).strip().splitlines()[-1])


def active_callbacks(pod: str) -> list[str]:
    """config.yaml 里 litellm_settings.callbacks（容器内那份，不是任何 CM 的推测）。"""
    out = sh(KUBE + f"kubectl exec -n {NS} {pod} -- python -c "
             "'import yaml;d=yaml.safe_load(open(\"/app/config.yaml\"));"
             "print(\"\\n\".join((d.get(\"litellm_settings\") or {}).get(\"callbacks\") or []))'")
    return [l.strip() for l in out.splitlines() if l.strip()]


def probe_imports(pod: str) -> list[tuple[str, str, str]]:
    """逐个 callback 在容器里真实 import，报 OK / NO-ATTR / IMPORTERR。

    ⚠ OK 只证明模块能 import 到、实例名存在，**不证明猴补丁装上了**。
    要证明替换真发生，用 --probe-chain。
    """
    code = (
        "import importlib,yaml\n"
        "d=yaml.safe_load(open('/app/config.yaml'))\n"
        "for c in (d.get('litellm_settings') or {}).get('callbacks') or []:\n"
        "    if '.' not in c:\n"
        "        print('BUILTIN|'+c+'|'); continue\n"
        "    mod,attr=c.rsplit('.',1)\n"
        "    try:\n"
        "        m=importlib.import_module(mod)\n"
        "        print(('OK' if hasattr(m,attr) else 'NO-ATTR')+'|'+c+'|')\n"
        "    except Exception as e:\n"
        "        print('IMPORTERR|'+c+'|'+type(e).__name__+': '+str(e)[:120])\n"
    )
    out = sh(KUBE + f"kubectl exec -n {NS} {pod} -- python -c {shlex.quote(code)}")
    rows = []
    for line in out.splitlines():
        if line.count("|") >= 2 and line.split("|")[0] in ("OK", "NO-ATTR", "IMPORTERR", "BUILTIN"):
            st, name, detail = line.split("|", 2)
            rows.append((st, name, detail))
    return rows


# 已知被多个补丁接管的热点符号。三个 callback 都 patch BaseResponsesAPIStreamingIterator
# ._process_chunk，各自带幂等哨兵并闭包住前一个 _orig ⇒ 应该是**叠起来**而不是互相覆盖。
# 10-20 实测链条：deepseek_id_prefix → streaming_output_backfill → codex_compaction_v2 → vanilla。
CHAIN_PROBE = (
    "litellm.responses.streaming_iterator",
    "BaseResponsesAPIStreamingIterator",
    "_process_chunk",
)


def probe_chain(pod: str) -> dict:
    """实测 _process_chunk 的替换链真的叠了几层（读 __closure__，不信 import 成功）。"""
    mod, cls, attr = CHAIN_PROBE
    code = f"""
import importlib, json, yaml
from {mod} import {cls} as B
vanilla = '%s.%s' % (B.{attr}.__module__, B.{attr}.__qualname__)
d = yaml.safe_load(open('/app/config.yaml'))
steps = []
for c in (d.get('litellm_settings') or {{}}).get('callbacks') or []:
    if '.' not in c:
        continue
    m = c.rsplit('.', 1)[0]
    before = B.{attr}
    importlib.import_module(m)
    if B.{attr} is not before:
        f = B.{attr}
        steps.append({{'callback': m, 'now_owned_by': '%s.%s' % (f.__module__, f.__qualname__)}})
chain, cur, seen = [], B.{attr}, set()
for _ in range(8):
    nxt = [x.cell_contents for x in (cur.__closure__ or ())
           if callable(x.cell_contents) and id(x.cell_contents) not in seen]
    nxt = [x for x in nxt if getattr(x, '__name__', '').startswith(('_patched', '{attr}'))]
    if not nxt:
        break
    cur = nxt[0]; seen.add(id(cur))
    chain.append('%s.%s' % (cur.__module__, cur.__qualname__))
print('@@' + json.dumps({{'symbol': '{cls}.{attr}', 'vanilla': vanilla,
                          'install_steps': steps, 'chain_below_outermost': chain}}))
"""
    out = sh(KUBE + f"kubectl exec -n {NS} {pod} -- python -c {shlex.quote(code)}")
    for line in out.splitlines():
        if line.startswith("@@"):
            return json.loads(line[2:])
    raise RuntimeError("chain 探针没出数（容器里 import 失败？）\n" + out[-800:])


def collect(lane: str, tag: str, want_chain: bool) -> dict:
    deploy = LANES[lane]
    pod = lane_pod(deploy)
    probe = sh(KUBE + f"kubectl exec -n {NS} {pod} -- {PKG_PROBE}").split()
    pkg_dir, live_ver = probe[0], probe[1]
    if tag.lstrip("v") != live_ver:
        raise RuntimeError(
            f"--tag {tag} 与线上实测版本 {live_ver} 不一致（lane={lane}）。\n"
            "   记忆里的「当前线上 vX」会静默过时，版本必须现量后再传 --tag。"
        )

    cms = mounted_cms(deploy)
    overlays = overlay_mounts(deploy, pkg_dir)
    md5 = live_vs_upstream(pod, pkg_dir, tag)
    cbs = active_callbacks(pod)
    imports = probe_imports(pod)

    baked = [f for f in md5["differs"] if f not in overlays]
    return {
        "lane": lane,
        "deploy": deploy,
        "pod": pod,
        "live_version": live_ver,
        "upstream_tag": tag,
        "pkg_dir": pkg_dir,
        "files_compared": md5["counted"],
        "configmaps": cms,
        "layers": {
            "callbacks": {
                "active": cbs,
                "imports": [{"status": s, "name": n, "detail": d} for s, n, d in imports],
                "bad": [n for s, n, _ in imports if s in ("NO-ATTR", "IMPORTERR")],
            },
            "overlay_files": overlays,
            "baked_files": baked,
            "differs": md5["differs"],
            "only_live": md5["only_live"],
            "only_upstream": md5["only_upstream"],
        },
        "chain": probe_chain(pod) if want_chain else None,
    }


def render(d: dict) -> None:
    print(f"lane={d['lane']}  deploy={d['deploy']}  pod={d['pod']}")
    print(f"线上 litellm={d['live_version']}（对照上游 {d['upstream_tag']}，逐文件比了 {d['files_compared']} 个 .py）")
    print(f"包目录 {d['pkg_dir']}")

    print("\n挂载的 ConfigMap（名字带内容 hash，每次都反查）:")
    for vol, cm in sorted(d["configmaps"].items()):
        print(f"  {vol:46s} -> {cm}")

    L = d["layers"]
    cb = L["callbacks"]
    print(f"\n[第1层] callbacks：config.yaml 里 {len(cb['active'])} 个，容器内 import 实测"
          f" {sum(1 for r in cb['imports'] if r['status'] == 'OK')} OK / {len(cb['bad'])} 坏")
    for r in cb["imports"]:
        if r["status"] in ("NO-ATTR", "IMPORTERR"):
            print(f"  🔴 {r['status']:10s} {r['name']}  {r['detail']}")
    if not cb["bad"]:
        print("     （无 import 失败。⚠ import OK 不等于猴补丁装上了，用 --probe-chain 看替换链）")

    print(f"\n[第2层] 整文件覆盖件 overlay：{len(L['overlay_files'])} 个（独立 subPath 盖在包里）")
    for f, vol in sorted(L["overlay_files"].items()):
        mark = "偏离上游" if f in L["differs"] else "⚠ 与上游逐字节相同（覆盖件没起作用？）"
        print(f"  {f}\n       vol={vol}  {mark}")

    print(f"\n[第3层] 烘焙进镜像：{len(L['baked_files'])} 个文件偏离上游（不在任何 CM 里）")
    for f in L["baked_files"]:
        print(f"  {f}")

    if L["only_live"]:
        print(f"\n🔴 容器里多出上游没有的文件 {len(L['only_live'])} 个：{L['only_live']}")
    if L["only_upstream"]:
        print(f"\n上游有、容器里没有 {len(L['only_upstream'])} 个（多为测试件被镜像裁掉）：")
        for f in L["only_upstream"]:
            print(f"  {f}")

    if d["chain"]:
        c = d["chain"]
        print(f"\n[替换链实测] {c['symbol']}")
        print(f"  vanilla: {c['vanilla']}")
        for s in c["install_steps"]:
            print(f"  import {s['callback']:30s} -> {s['now_owned_by']}")
        print("  最外层往下的闭包链:")
        for i, f in enumerate(c["chain_below_outermost"], 1):
            print(f"    {i}. {f}")
        n = len(c["chain_below_outermost"])
        print(f"  ⇒ {n + 1} 层叠在一起（链底是 vanilla ⇒ 没有互相覆盖）"
              if c["chain_below_outermost"] and "litellm." in c["chain_below_outermost"][-1]
              else f"  🔴 链底不是 vanilla（{n} 层）⇒ 有补丁被盖掉了，逐个查哨兵")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lane", choices=sorted(LANES), default="prod")
    ap.add_argument("--tag", required=True, help="上游对照 tag，必须已在 198 /root/litellm-fork fetch 过")
    ap.add_argument("--probe-chain", action="store_true", help="另外实测 _process_chunk 替换链叠了几层")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    d = collect(a.lane, a.tag, a.probe_chain)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        render(d)
    return 1 if (d["layers"]["callbacks"]["bad"] or d["layers"]["only_live"]) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.TimeoutExpired as e:
        print(f"🔴 198 命令超时：{e}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(f"🔴 {e}", file=sys.stderr)
        sys.exit(2)
