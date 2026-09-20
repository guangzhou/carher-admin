#!/usr/bin/env python3
"""把 k8s/monitoring/ 同步回 198 集群 —— diff 优先，门禁贴在不可逆那一步正前方。

为什么要有这个脚本
------------------
README 里那串 patch 命令是对的，但它有三个地方靠人记住，而人会忘：

  1. **第 0 步必须 diff**。集群比 repo 新时直接 patch = 静默覆盖别人的改动，
     而且 patch 成功、sha 对得上，事后完全看不出来（09-20 实测过一次反方向）。
  2. **禁 `kubectl apply`**。一个 CM 里有多个 key，apply 会按整份文件覆盖，
     把同 CM 里别人的 key 一起抹掉 —— 包括 `zz-delete-probe.yaml` 那条
     `deleteRules` 墓碑，抹了会让 `zz-positive-control` 探针复活。
  3. **改 CM ≠ 生效**，判据还分对象：Prometheus 热加载、Grafana 必须
     `rollout restart`（文件式 provisioning 只在启动时读）、探针要重启进程。

门禁贴在哪
----------
贴在 `patch` 正前方，不套在整条流水线外。套在外面的后果见
`feedback_gate_the_irreversible_step_not_the_whole_sequence`：要么带着没准备好的
问题去问人，要么被逼 `--force` 把检查一起跳过。所以：
diff / 读取 / 校验全部免确认随便跑，**只有 patch 这一下要 `--yes`**。

判「生效」用的路径不许猜
------------------------
容器内 sha256 是唯一可信判据，但**挂载路径必须从 Deployment 的
`volumes`/`volumeMounts` 反查**，不许在这里写死一个猜的路径 ——
指向不存在路径的检查永不触发，形状跟「根本没检查」一模一样
（`topic_ruler_failure_shapes`）。查不到就如实说查不到，不假装绿。

用法
----
  # 全量 diff（只读，什么都不改；有漂移 exit 1）
  ./litellm-198-monitoring-sync.py diff

  # 单个文件 diff
  ./litellm-198-monitoring-sync.py diff --file probe.py

  # 推一个 key（不带 --yes 只演练到 patch 前一步就停，并打印将要做什么）
  ./litellm-198-monitoring-sync.py push --file probe.py
  ./litellm-198-monitoring-sync.py push --file probe.py --yes

  # 推完验生效（容器内 sha + 对象特有判据）
  ./litellm-198-monitoring-sync.py verify --file probe.py

⚠️ 改 `probe.py` 的节奏参数或 `litellm-stability.yaml` 的窗口/阈值之前，
先跑 `k8s/monitoring/observability-preflight.py --probe-script probe.py --static-only`
（CADENCE 腿）。本脚本在 push 前会自动替你跑一遍，不过不许 patch。
"""
import argparse
import base64
import difflib
import hashlib
import json
import os
import subprocess
import sys

SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20",
       "cltx@10.68.13.198"]
KUBECTL = "sudo -n kubectl"
REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "k8s", "monitoring")

# 文件 → (ns, ConfigMap, key, 生效动作)。与 k8s/monitoring/README.md 的映射表同源；
# 改一边必须改另一边。生效动作三类：
#   grafana = rollout restart deploy/grafana（provisioning 只在启动时加载）
#   prom    = POST /-/reload（热加载，不必重启）
#   probe   = rollout restart deploy/litellm-probe（进程要重读脚本）
MAP = {
    "litellm-stability.yaml":  ("monitoring", "grafana-alerting", "grafana"),
    "zz-delete-probe.yaml":    ("monitoring", "grafana-alerting", "grafana"),
    "feishu-contactpoint.yaml": ("monitoring", "grafana-alerting", "grafana"),
    "prometheus.yml":          ("monitoring", "prometheus-config", "prom"),
    "model-stability.json":    ("monitoring", "grafana-dashboard-litellm", "grafana"),
    "litellm-proxy.json":      ("monitoring", "grafana-dashboard-litellm", "grafana"),
    # ⚠️ 不是 grafana：它的消费者是 deploy/alert-to-feishu（那个停在 replicas:0 的）。
    # 写成 grafana 会去重启一个跟它无关的进程，然后报「已生效」—— 假绿。
    "alert2feishu.py":         ("monitoring", "alert2feishu-script", "feishu"),
    "probe.py":               ("litellm-product", "litellm-probe-script", "probe"),
}
# 生效动作 → 重启哪个 Deployment（prom 是热加载，没有）
RESTART_TARGET = {"grafana": ("monitoring", "deploy/grafana"),
                  "feishu": ("monitoring", "deploy/alert-to-feishu"),
                  "probe": ("litellm-product", "deploy/litellm-probe")}


def sh(cmd, check=True, stdin=None):
    r = subprocess.run(SSH + [cmd], capture_output=True, text=True, input=stdin)
    if check and r.returncode != 0:
        raise SystemExit("远端命令失败 (%d): %s\n%s" % (r.returncode, cmd, r.stderr[:800]))
    return r.stdout


def repo_text(fname):
    with open(os.path.join(REPO_DIR, fname)) as fh:
        return fh.read()


def cluster_text(fname):
    """读集群现值。key 名里的点要转义，否则 jsonpath 会当成路径分隔符。"""
    ns, cm, _ = MAP[fname]
    esc = fname.replace(".", r"\.")
    # base64 过一层：json/yaml 内容里有引号和换行，直接取原文会被 shell 和
    # subprocess 的文本解码各扭一次，diff 出一堆假差异。
    out = sh("%s -n %s get cm %s -o jsonpath='{.data.%s}' | base64 -w0"
             % (KUBECTL, ns, cm, esc), check=False)
    if not out.strip():
        return None
    return base64.b64decode(out.strip()).decode("utf-8", "replace")


def do_diff(files):
    """只读。返回有漂移的文件名列表。

    ⚠️ 「没有 diff」和「读不到集群那份」必须分开报。读不到时返回 MISSING 而不是
    静默算成一致 —— 后者会把「这个 key 在集群里根本不存在」读成「已经同步好了」。
    """
    drifted, missing = [], []
    for f in files:
        mine, theirs = repo_text(f), cluster_text(f)
        if theirs is None:
            missing.append(f)
            print("  ? %-24s 集群里读不到这个 key（CM 不存在 / key 名不对）" % f)
            continue
        if mine == theirs:
            print("  = %-24s 一致 (sha %s)" % (f, hashlib.sha256(mine.encode()).hexdigest()[:12]))
            continue
        drifted.append(f)
        d = list(difflib.unified_diff(theirs.splitlines(), mine.splitlines(),
                                      fromfile="cluster/" + f, tofile="repo/" + f,
                                      lineterm="", n=2))
        print("  ≠ %-24s 有漂移，%d 行差异：" % (f, len(d)))
        for line in d[:60]:
            print("      " + line)
        if len(d) > 60:
            print("      … 还有 %d 行，用 --file %s 单看" % (len(d) - 60, f))
    return drifted, missing


def preflight(fname):
    """push 前的静态门禁。CADENCE 腿会抓「改了间隔没改窗口/阈值」。

    只对两个文件有意义（probe.py 与规则文件互为对账双方），但**两者任一被推都要跑** ——
    改 probe.py 的节奏和改 yaml 的阈值是同一个耦合的两端。
    """
    if fname not in ("probe.py", "litellm-stability.yaml"):
        return True
    pf = os.path.join(REPO_DIR, "observability-preflight.py")
    r = subprocess.run([sys.executable, pf,
                        "--rules", os.path.join(REPO_DIR, "litellm-stability.yaml"),
                        "--probe-script", os.path.join(REPO_DIR, "probe.py"),
                        "--static-only"], capture_output=True, text=True)
    sys.stdout.write(r.stdout[-2000:])
    if r.returncode != 0:
        print("\n⛔ preflight 不过 —— 不 patch。先修尺子，别推上去。")
        return False
    return True


def do_push(fname, confirmed):
    ns, cm, _ = MAP[fname]
    mine = repo_text(fname)
    theirs = cluster_text(fname)

    # 第 0 步：diff。集群比 repo 新时直接 patch 会静默吞掉别人的改动。
    if theirs is None:
        print("! 集群里读不到 %s/%s 的 key %s —— 这是新建，不是更新。确认这是你要的。"
              % (ns, cm, fname))
    elif theirs == mine:
        print("= %s 与集群一致，无需 patch。" % fname)
        return 0
    else:
        d = list(difflib.unified_diff(theirs.splitlines(), mine.splitlines(),
                                      fromfile="cluster", tofile="repo", lineterm="", n=2))
        print("将要用 repo 版本覆盖集群版本，差异 %d 行：" % len(d))
        for line in d[:80]:
            print("  " + line)
        print("\n⚠️ 集群那份里如果有别人的改动，这一下会把它抹掉，而且 patch 会成功、"
              "sha 会对得上、事后看不出来。上面的 diff 就是你唯一的审查机会。")

    if not preflight(fname):
        return 1

    if not confirmed:
        print("\n== PREPARED, NOT PATCHED ==")
        print("已做完 diff 和 preflight，**没有改集群任何东西**。")
        print("要真推：重跑并加 --yes")
        return 2

    # patch 单个 key。禁 apply —— 会连带覆盖同 CM 里别的 key。
    payload = json.dumps({"data": {fname: mine}})
    sh("%s -n %s patch cm %s --type merge --patch-file /dev/stdin"
       % (KUBECTL, ns, cm), stdin=payload)
    print("✓ 已 patch %s/%s 的 key %s" % (ns, cm, fname))
    print("   repo sha256 = %s" % hashlib.sha256(mine.encode()).hexdigest())
    print("\n⚠️ patch 完 ≠ 生效。下一步跑 verify（它会告诉你还要不要 rollout restart）：")
    print("   ./litellm-198-monitoring-sync.py verify --file %s" % fname)
    return 0


def _mount_paths(ns, workload, cm):
    """从 Deployment 反查这个 CM 被挂到容器里的哪些路径。

    **不许写死猜的路径。** 指向不存在路径的检查永不触发，而它的输出形状跟
    「检查过了，没问题」一模一样。查不到就返回空，由调用方如实说查不到。
    """
    out = sh("%s -n %s get %s -o json" % (KUBECTL, ns, workload), check=False)
    if not out.strip():
        return []
    spec = json.loads(out)["spec"]["template"]["spec"]
    vol_names = {v["name"] for v in spec.get("volumes", [])
                 if (v.get("configMap") or {}).get("name") == cm}
    paths = []
    for c in spec.get("containers", []):
        for vm in c.get("volumeMounts", []):
            if vm["name"] in vol_names:
                paths.append((c["name"], vm["mountPath"], vm.get("subPath")))
    return paths


def do_verify(fname):
    ns, cm, action = MAP[fname]
    mine = repo_text(fname)
    want = hashlib.sha256(mine.encode()).hexdigest()
    print("repo sha256 = %s" % want)

    # 1) CM 侧
    theirs = cluster_text(fname)
    if theirs is None:
        print("✗ 集群 CM 里读不到这个 key")
        return 1
    got_cm = hashlib.sha256(theirs.encode()).hexdigest()
    print("%s CM 内容 sha256 = %s" % ("✓" if got_cm == want else "✗", got_cm))

    # 2) 容器内 sha —— 唯一可信的「投影到位了」判据（kubelet 投影要 20~60s）
    tgt = RESTART_TARGET.get(action)
    if not tgt:
        print("· %s 无需重启（Prometheus 热加载）" % fname)
    else:
        wns, workload = tgt
        paths = _mount_paths(wns, workload, cm)
        if not paths:
            print("? 从 %s/%s 反查不到 CM %s 的挂载路径 —— **没验到容器内**，"
                  "不当绿。去人工看 volumeMounts。" % (wns, workload, cm))
        for cname, mp, sub in paths:
            full = mp if sub else os.path.join(mp, fname)
            got = sh("%s -n %s exec %s -c %s -- sha256sum %s 2>/dev/null || echo MISSING"
                     % (KUBECTL, wns, workload, cname, full), check=False).split()
            got = got[0] if got else "MISSING"
            ok = "✓" if got == want else "✗"
            print("%s 容器 %s:%s sha256 = %s" % (ok, cname, full, got[:64]))

    # 3) 对象特有判据
    if action == "grafana":
        print("\n⚠️ Grafana 文件式 provisioning **只在启动时加载** ⇒ 必须："
              "\n   %s -n monitoring rollout restart deploy/grafana"
              "\n   然后判据 = GET /api/v1/provisioning/alert-rules 读回的阈值是新值，"
              "\n   不是 CM 内容、也不是容器内 sha（sha 对了进程也可能还读着旧值）。" % KUBECTL)
    elif action == "probe":
        print("\n⚠️ 探针进程启动时读脚本 ⇒ 必须："
              "\n   %s -n litellm-product rollout restart deploy/litellm-probe"
              "\n   判据 = pod 日志第一行「探针启动 ... 轮间隔=A~Bs(随机) 串行」参数是新值，"
              "\n   且 restartCount 没在涨。节奏类改动还要等一整轮，拿"
              "\n   litellm_probe_round_duration_seconds 复核单轮耗时的预期值。" % KUBECTL)
    elif action == "feishu":
        print("\n⚠️ 消费者是 deploy/alert-to-feishu，**不是 grafana**。而且它目前"
              " replicas=0 ⇒ rollout restart 对 0 副本是 no-op，**照样返 0 也照样"
              "`Available:True`**，别读成「已生效」。"
              "\n   判据 = 先确认 `.spec.replicas` > 0 且有 Running pod，再核容器内 sha。"
              "\n   只改脚本不拉起副本 = 这次改动一行代码都没跑。")
    elif action == "prom":
        print("\n· Prometheus 热重载即可："
              "\n   %s -n monitoring exec <prom-pod> -c prometheus -- "
              "wget -qO- --post-data='' http://127.0.0.1:9090/-/reload"
              "\n   判据 = /api/v1/status/config 读到新内容。" % KUBECTL)
    return 0


def main():
    ap = argparse.ArgumentParser(description="k8s/monitoring/ ↔ 198 集群同步（diff 优先）")
    ap.add_argument("action", choices=["diff", "push", "verify", "map"])
    ap.add_argument("--file", help="只处理这一个文件；不给则全部（push 必须指定）")
    ap.add_argument("--yes", action="store_true",
                    help="push 时真的 patch。不给只演练到 patch 前一步")
    a = ap.parse_args()

    if a.action == "map":
        for f, (ns, cm, act) in sorted(MAP.items()):
            print("%-24s → %s/%s  生效=%s" % (f, ns, cm, act))
        return 0

    if a.file and a.file not in MAP:
        raise SystemExit("不认识的文件 %s；跑 `map` 看清单" % a.file)
    files = [a.file] if a.file else sorted(MAP)

    if a.action == "diff":
        drifted, missing = do_diff(files)
        print("\n%d 个文件：%d 漂移，%d 读不到" % (len(files), len(drifted), len(missing)))
        # 漂移和读不到都返回非 0：两者都需要人看，不许当成"检查过了没问题"
        return 1 if (drifted or missing) else 0

    if a.action == "push":
        if not a.file:
            raise SystemExit("push 必须 --file 指定单个文件 —— 批量推等于把这一轮"
                             "没审过的漂移一起带上去")
        return do_push(a.file, a.yes)

    if not a.file:
        raise SystemExit("verify 必须 --file")
    return do_verify(a.file)


if __name__ == "__main__":
    sys.exit(main())
