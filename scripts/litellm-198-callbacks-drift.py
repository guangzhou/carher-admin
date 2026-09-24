#!/usr/bin/env python3
"""对账 198 各 proxy lane 容器里真正跑着的 callbacks 与 repo `k8s/litellm-callbacks/`。

为什么要有这个脚本
------------------
2026-09-21 修 Codex `查余额` 反复重连（mock usage 缺 `reasoning_tokens`）时发现：
callbacks 这一摊有**三份互不相等的副本**，而且哪一份都不是权威：

  1. repo `k8s/litellm-callbacks/*.py`
  2. 每条 lane 自己的 ConfigMap（gray / 空转 prod / guarded-old 各一份，名字都不同）
  3. helm release 里存着的 values（`helm get values`，是某次 run 的冻结快照）

当天实测：repo 与 gray 活 CM 之间，8 个文件内容不同、8 个文件只有集群里有
（从没进过 git）。也就是说「照 repo 重建一条 lane」= 静默回退一堆已经在线上
跑了很久的改动，而且 patch 会成功、rollout 会绿、事后完全看不出来。

所以这个脚本不判「谁对」——它只让**不一致本身不再是静默的**。
方向要人看 diff 定（memory: 禁 blanket cp，一侧是严格超集才可覆盖）。

CM 名字不许写死
---------------
每条 lane 的 callbacks CM 名带内容 hash，改一次就换一个名
（gray 当天就经历了 `...-2c79069fcb8b` → `...-f981341fb130`）。
写死名字的检查在改名后永远查的是旧对象，形状跟「查过了，没问题」一模一样。
所以这里一律从 Deployment 的 `volumes` 反查当前名。

用法
----
  ./litellm-198-callbacks-drift.py                 # 全部 lane
  ./litellm-198-callbacks-drift.py --lane gray     # 单条
  ./litellm-198-callbacks-drift.py --file budget_notice.py   # 只看一个文件
  ./litellm-198-callbacks-drift.py --diff --file budget_notice.py  # 打印 diff

退出码：0 = 全对齐；1 = 有漂移 / 有只存在于一侧的文件。
**只读，不改集群任何东西。**
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
NS = "litellm-product"
REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "k8s", "litellm-callbacks")
# lane → Deployment。volume 名统一是 callbacks，CM 名从 Deployment 反查。
# 🔴 2026-09-24：`prod` 这条**按路由标签反查**，不写死名字 —— 09-24 之前
# 带标签的是 `litellm-proxy-gray`，09-24 起是 `litellm-proxy`。写死名字会让
# 漂移体检去比一条 0 副本的车道，结果「没漂移」而实际一个在服务的 pod 都没看。
LANES = {
    "guarded-old": "litellm-proxy-guarded-old",  # 回滚 lane
}
PROD_POD_SELECTOR = "carher.net/litellm-production-route=enabled"
IDLE_CANDIDATES = ["litellm-proxy", "litellm-proxy-gray"]
VOLUME = "callbacks"


def sh(cmd, check=True):
    r = subprocess.run(SSH + [cmd], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit("远端失败(%d): %s\n%s" % (r.returncode, cmd, r.stderr[:800]))
    return r.stdout


def h12(s):
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def lane_cm_name(deploy):
    """从 Deployment 反查 callbacks volume 当前指向哪个 CM。查不到就如实报，不猜。"""
    out = sh("%s -n %s get deploy %s -o json" % (KUBECTL, NS, deploy), check=False)
    if not out.strip():
        return None
    for v in json.loads(out)["spec"]["template"]["spec"].get("volumes", []):
        if v["name"] == VOLUME:
            return (v.get("configMap") or {}).get("name")
    return None


def cm_data(name):
    out = sh("%s -n %s get cm %s -o json | base64 -w0" % (KUBECTL, NS, name), check=False)
    if not out.strip():
        return None
    return json.loads(base64.b64decode(out.strip()).decode("utf-8", "replace"))["data"]


def repo_data():
    return {f: open(os.path.join(REPO_DIR, f)).read()
            for f in sorted(os.listdir(REPO_DIR)) if f.endswith(".py")}


def compare(lane, deploy, repo, only_file, show_diff):
    cm = lane_cm_name(deploy)
    if cm is None:
        print("? %-12s 反查不到 %s 的 callbacks volume —— **没对上账**，不当绿" % (lane, deploy))
        return 1
    data = cm_data(cm)
    if data is None:
        print("? %-12s CM %s 读不到" % (lane, cm))
        return 1
    print("\n== lane %s（deploy %s）→ CM %s ==" % (lane, deploy, cm))

    keys = sorted(set(repo) | set(data))
    if only_file:
        keys = [only_file]
    same = drift = missing = 0
    for k in keys:
        a, b = repo.get(k), data.get(k)
        if a is None:
            missing += 1
            print("  ← %-42s 只有集群有（从没进 git，repo 重建会丢）" % k)
            continue
        if b is None:
            missing += 1
            print("  → %-42s 只有 repo 有（这条 lane 没装）" % k)
            continue
        if a == b:
            same += 1
            continue
        drift += 1
        print("  ≠ %-42s repo=%s  live=%s" % (k, h12(a), h12(b)))
        if show_diff:
            for line in list(difflib.unified_diff(
                    b.splitlines(), a.splitlines(),
                    fromfile="live/" + k, tofile="repo/" + k, lineterm="", n=2))[:120]:
                print("      " + line)
    print("  小计：%d 一致，%d 内容不同，%d 只存在于一侧" % (same, drift, missing))
    return 1 if (drift or missing) else 0


def prod_deploy():
    """反查带路由标签的那条车道，fail-closed。

    ⛔ 不用 `get deploy -l <路由标签>`：标签在 Pod 上不在 Deployment 上，
    那条查询返空集，而空集长得跟「没问题」一样。
    """
    out = sh("%s -n %s get po -l %s -o json" % (KUBECTL, NS, PROD_POD_SELECTOR), check=False)
    names = set()
    for p in (json.loads(out).get("items", []) if out.strip() else []):
        if p.get("status", {}).get("phase") != "Running":
            continue
        for ref in p["metadata"].get("ownerReferences", []):
            if ref["kind"] != "ReplicaSet":
                continue
            rs = sh("%s -n %s get rs %s -o json" % (KUBECTL, NS, ref["name"]), check=False)
            if not rs.strip():
                continue
            for r2 in json.loads(rs)["metadata"].get("ownerReferences", []):
                if r2["kind"] == "Deployment":
                    names.add(r2["name"])
    if not names:
        raise SystemExit("反查不到生产车道：没有 Running pod 带 %s —— 判据失效，不当绿。"
                         % PROD_POD_SELECTOR)
    if len(names) > 1:
        raise SystemExit("生产车道反查到多条：%s —— 切换窗口里比对会只看一半，等收敛。"
                         % sorted(names))
    return names.pop()


def live_lanes():
    """把「谁在服务 / 谁在空转」按实测填出来，名字是结果不是前提。"""
    prod = prod_deploy()
    lanes = {"prod": prod}
    for d in IDLE_CANDIDATES:
        if d != prod:
            lanes["idle-" + d] = d
    lanes.update(LANES)
    return lanes


def main():
    ap = argparse.ArgumentParser(description="repo k8s/litellm-callbacks/ ↔ 198 各 lane 活 CM 对账（只读）")
    ap.add_argument("--lane", help="只看一条 lane（名字见不带参数时的输出）")
    ap.add_argument("--file", help="只看一个文件名，如 budget_notice.py")
    ap.add_argument("--diff", action="store_true", help="打印 unified diff")
    a = ap.parse_args()

    repo = repo_data()
    all_lanes = live_lanes()
    print("车道实测：" + "  ".join("%s=%s" % (k, v) for k, v in all_lanes.items()))
    if a.lane:
        if a.lane not in all_lanes:
            raise SystemExit("没有这条 lane：%s（现有 %s）" % (a.lane, sorted(all_lanes)))
        lanes = {a.lane: all_lanes[a.lane]}
    else:
        lanes = all_lanes
    rc = 0
    for lane, deploy in lanes.items():
        rc |= compare(lane, deploy, repo, a.file, a.diff)
    if rc:
        print("\n⛔ 有不一致。**方向要你自己看 diff 定**：线上那份可能是别人没提交的新改动，"
              "\n   也可能是漂移。禁 blanket cp —— 只有一侧是严格超集才谈覆盖。")
    else:
        print("\n✓ 全部对齐。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
