#!/usr/bin/env python3
"""把**已经在线**的一个 callback 文件更新到 198 各 lane（ns `litellm-product`）。

跟 [[litellm-198-router-patch]] 的 `install` 不是一回事
----------------------------------------------------
那个脚本装的是**新**补丁：加 CM data + 加独立 subPath volumeMount + 改
config.yaml callbacks 列表，三步。本脚本只干「这个文件已经装好了、挂好了、
在 callbacks 列表里了，我只想换掉它的内容」——**一步都不能多**，多加
volumeMount 或改 config 反而会引入新故障面。

两条 lane 的换法**不一样**，混用会出事
-------------------------------------
* **生产车道**（Svc `litellm-proxy-nodeport` 真正指向的那条，靠路由标签
  `carher.net/litellm-production-route=enabled` 反查，**不写死名字**；
  2026-09-24 前是 `litellm-proxy-gray`，09-24 起是 `litellm-proxy`）
  挂的是**内容哈希命名的不可变 CM**（新一代 `litellm-stable-callbacks-<hash>`，
  历史上是 `litellm-product-gray-callbacks-<hash>`，旧名留着当回滚点）。
  换代 = 复制当前 CM → 替换那一个 key → 按新内容重新哈希命名 → `create` →
  strategic-patch Deployment 的 volume 指向新名 → 滚动更新。
* `idle-prod` / acct 池挂的是**可变的共享 CM** `litellm-callbacks`。
  它同时被 13 个 `chatgpt-acct-*` Deployment 挂着，其中有在服务的号。
  🔴 **只 merge-patch data，永远不要 rollout restart 它们** ——
  重启在服务的 acct 号可能永久打死它且回退救不回
  （memory: feedback_restarting_a_serving_acct_can_kill_it_permanently）。
  data 改了但不重启 = 这些 pod 下次自然重启时才生效，这是**有意为之**。

三条出厂即开的门禁（都可以 `--force` 越过，但会把越过这件事打在屏幕上）
--------------------------------------------------------------------
1. **`git diff HEAD -- <file>` 必须是空的。**
   2026-09-23 实测：我把工作区整份文件 scp 上生产，里面**带着一处本轮没打算
   发的未提交改动**（25 行），它跟着上了生产、还被写进了我那条 commit。
   「我只改了 X」这句话的判据不是我的记忆，是 `git diff HEAD`。
2. **线上旧版必须能在 git 里找到对应版本**（默认比 `HEAD~1`/`HEAD`）。
   对不上说明线上那份是别人没提交的改动，推上去 = 静默回退它
   （memory: 禁 blanket cp）。先跑 `litellm-198-callbacks-drift.py --diff`。
3. **`kubectl apply` 全程禁用**（manifest 陈旧，apply 会回退 image + 内嵌 CM）。
   本脚本只用 `create` / `patch` / `rollout status`。

验收（`verify` 干的事，逐副本不抽样）
------------------------------------
* 每个在服务的 pod 内 `sha256sum /app/<file>` == 本地文件 sha
* pod 日志里 `ImportError|SyntaxError|ModuleNotFoundError` == 0
* ⚠️ 滚动更新期间旧 pod 会 `connection reset by peer`，在途流式请求会断成
  「event-stream 开了、0 个 event」。**那是重启窗口，不是新代码的 bug。**
  判「是不是这次改动带入的」要分两层：① 改动的代码路径在门控上可不可达
  ② 报错时刻落不落在 rollout 窗口里。见 skill codex-remote-compaction-triage。

用法
----
    ./litellm-198-callback-update.py plan   deepseek_responses_adapt.py
    ./litellm-198-callback-update.py apply  deepseek_responses_adapt.py
    ./litellm-198-callback-update.py verify deepseek_responses_adapt.py

`plan` 只读：打印 git 对账 + 各 lane 当前 sha + 将要做的动作，不碰集群。
`apply` 先把每个要改的 CM 备份到 198 `/tmp/bak_<cm>_<时间戳>.yaml` 并打印路径。

回滚
----
    ssh cltx@10.68.13.198 'sudo kubectl -n litellm-product patch deploy \
        <plan 打印出来的那条生产车道> --type=json -p "[{\"op\":\"replace\",
        \"path\":\"/spec/template/spec/volumes/<i>/configMap/name\",
        \"value\":\"<旧CM名>\"}]"'
旧 CM 是不可变的、没被删，改回名字即回滚（`plan` 会把当前名字打出来存档）。
共享 CM 用 `kubectl replace -f <备份yaml>` 还原 data。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time

SSH = ["ssh", "-n", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20",
       "cltx@10.68.13.198"]
KUBECTL = "sudo -n kubectl"
NS = "litellm-product"
REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "k8s", "litellm-callbacks")
VOLUME = "callbacks"

# 2026-09-24：新一代 CM 一律用中性前缀。生产车道上仍可能挂着历史的
# `litellm-product-gray-callbacks-<hash>`（旧名不动、不可变、留作回滚），
# 本脚本只决定**新建的那一代**叫什么 —— 旧名是从 Deployment 反查来的。
GRAY_CM_PREFIX = "litellm-stable-callbacks-"
SHARED_CM = "litellm-callbacks"
# 生产车道判据：Svc 的 selector，不是 `-l app=litellm-proxy`
# （memory: feedback_prod_lane_label_is_on_pods_not_deployments）
PROD_POD_SELECTOR = "carher.net/litellm-production-route=enabled"

# 🔴 2026-09-24：生产车道的 Deployment 名字**不再写死**。
# 历史上这里是 `litellm-proxy-gray`，09-24 把路由标签搬到 `litellm-proxy` 之后，
# 写死的名字会让本脚本 patch 一个 0 副本的 Deployment —— patch 成功、退出码 0、
# 但改的东西一个在服务的 pod 都没吃到（静默打空）。
# 判据只认「谁的 pod 带路由标签」，名字是它的结果不是它的前提。
_PROD_DEPLOY_CACHE = None


def sh(cmd, check=True, stdin_data=None):
    """在 198 上跑一条命令。大载荷必须走 `stdin_data`，不许拼进 argv。

    🔴 2026-09-25：`apply budget_notice.py` 整个炸在这里。旧写法是把整份 CM
    （875 KB base64）当成 `echo <b64>` 的**命令行参数**塞给 ssh，直接撞
    ARG_MAX，症状不是干净的报错而是
    `Read from remote host: Connection reset by peer` + `Broken pipe`，
    ssh 退 255。CM 越长越容易踩到，而 callbacks CM 只会越来越大。

    幸运的是这一步在三步动作的**第一步**，失败时 deploy 还没被 patch、
    共享 CM 还没被改，集群零改动（当天实测：新 CM NotFound、deploy 仍指旧
    CM、4 个 pod restartCount=0）。但这是运气，不是设计 —— 所以改成 stdin。
    """
    # ⚠️ SSH 里带着 `-n`（stdin 接 /dev/null，防循环里 ssh 吞掉外层输入）。
    # 要喂 stdin 就必须把 `-n` 摘掉，否则载荷被静默丢弃 —— `kubectl create -f -`
    # 会收到空输入，报的错跟"内容不对"长得一样，查起来很贵。
    argv = [a for a in SSH if a != "-n"] if stdin_data is not None else list(SSH)
    r = subprocess.run(argv + [cmd], input=stdin_data,
                       capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit("远端失败(%d): %s\n%s" % (r.returncode, cmd, r.stderr[:800]))
    return r.stdout


def local(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def h12(s: str) -> str:
    return sha(s)[:12]


def prod_deploy() -> str:
    """反查「真正在服务的那条车道」的 Deployment 名，fail-closed。

    路径：带路由标签的 Running pod → ownerRef(ReplicaSet) → ownerRef(Deployment)。
    ⛔ 不用 `get deploy -l <路由标签>`：标签在 **Pod** 上不在 Deployment 上，
    那条查询会返空集，而空集在 shell 里长得跟「没问题」一模一样。
    """
    global _PROD_DEPLOY_CACHE
    if _PROD_DEPLOY_CACHE:
        return _PROD_DEPLOY_CACHE
    out = sh("%s -n %s get po -l %s -o json" % (KUBECTL, NS, PROD_POD_SELECTOR))
    names = set()
    for p in json.loads(out).get("items", []):
        if p.get("status", {}).get("phase") != "Running":
            continue
        for ref in p["metadata"].get("ownerReferences", []):
            if ref["kind"] != "ReplicaSet":
                continue
            rs = json.loads(sh("%s -n %s get rs %s -o json" % (KUBECTL, NS, ref["name"])))
            for r2 in rs["metadata"].get("ownerReferences", []):
                if r2["kind"] == "Deployment":
                    names.add(r2["name"])
    if not names:
        raise SystemExit(
            "反查不到生产车道：没有 Running pod 带 %s。\n"
            "这不是「没问题」，是判据失效 —— 先确认路由标签在谁身上再跑本脚本。"
            % PROD_POD_SELECTOR)
    if len(names) > 1:
        raise SystemExit(
            "生产车道反查到 %d 个 Deployment：%s\n"
            "切换窗口里两条车道同时带标签是预期的，但这时换 callback 会只改一半 —— "
            "等切换收敛到单条再跑。" % (len(names), sorted(names)))
    _PROD_DEPLOY_CACHE = names.pop()
    return _PROD_DEPLOY_CACHE


def gray_cm_name() -> str:
    out = sh("%s -n %s get deploy %s -o json" % (KUBECTL, NS, prod_deploy()))
    for v in json.loads(out)["spec"]["template"]["spec"].get("volumes", []):
        if v["name"] == VOLUME:
            return (v.get("configMap") or {}).get("name")
    raise SystemExit("反查不到 %s 的 %s volume —— 没对上账，不当绿" % (prod_deploy(), VOLUME))


def gray_volume_index() -> int:
    """永远反查下标，别写死 —— 别人加了 volume 就错位了。"""
    out = sh("%s -n %s get deploy %s -o json" % (KUBECTL, NS, prod_deploy()))
    vols = json.loads(out)["spec"]["template"]["spec"].get("volumes", [])
    for i, v in enumerate(vols):
        if v["name"] == VOLUME:
            return i
    raise SystemExit("反查不到 volume 下标")


def cm_data(name: str) -> dict:
    out = sh("%s -n %s get cm %s -o json | base64 -w0" % (KUBECTL, NS, name))
    return json.loads(base64.b64decode(out.strip()).decode("utf-8", "replace"))["data"]


def git_preflight(fname: str, force: bool) -> None:
    """门禁 1：工作区那份文件相对 HEAD 必须干净。"""
    rel = "k8s/litellm-callbacks/" + fname
    r = local("git diff HEAD --stat -- %s" % rel)
    dirty = r.stdout.strip()
    if not dirty:
        print("✓ 门禁1 `git diff HEAD -- %s` 为空 —— 要推的就是 HEAD 那份" % rel)
        return
    print("⛔ 门禁1 不过：工作区这份文件相对 HEAD 有未提交改动\n" + dirty)
    print(local("git diff HEAD -- %s" % rel).stdout[:4000])
    print("\n推上去 = 把上面这些也一起发到生产。先 commit（或 checkout 掉）再来。")
    if not force:
        raise SystemExit(1)
    print("⚠️ --force：**明知带着未提交改动**仍然继续。这件事会出现在本轮记录里。")


def live_vs_git(fname: str, live: str) -> None:
    """门禁 2：线上旧版能不能在 git 历史里对上。对不上=线上有人没提交的改动。"""
    rel = "k8s/litellm-callbacks/" + fname
    for ref in ("HEAD", "HEAD~1", "HEAD~2"):
        r = local("git show %s:%s" % (ref, rel))
        if r.returncode == 0 and r.stdout == live:
            print("✓ 门禁2 线上旧版 == git %s（推上去不会静默回退别人的改动）" % ref)
            return
    print("⚠️ 门禁2 线上旧版在 HEAD/HEAD~1/HEAD~2 里都对不上 —— "
          "线上那份可能是别人没提交的改动。\n"
          "   先跑 `scripts/litellm-198-callbacks-drift.py --diff --file %s` 看 diff 定方向，"
          "禁 blanket cp。" % fname)


def pods():
    out = sh("%s -n %s get po -l %s -o json" % (KUBECTL, NS, PROD_POD_SELECTOR))
    return [p["metadata"]["name"] for p in json.loads(out).get("items", [])
            if p.get("status", {}).get("phase") == "Running"]


def cmd_plan(fname: str, force: bool) -> int:
    path = os.path.join(REPO_DIR, fname)
    want = open(path, encoding="utf-8").read()
    git_preflight(fname, force)

    cur_cm = gray_cm_name()
    data = cm_data(cur_cm)
    if fname not in data:
        raise SystemExit("⛔ %s 不在 CM %s 里 —— 这是**新装**，走 litellm-198-router-patch，"
                         "别用本脚本（还缺 volumeMount + config.yaml 两步）" % (fname, cur_cm))
    live = data[fname]
    live_vs_git(fname, live)

    new_cm = GRAY_CM_PREFIX + h12("".join(
        "%s\0%s\0" % (k, want if k == fname else v) for k, v in sorted(data.items())))
    print("\n== 计划 ==")
    print("  文件        %s" % fname)
    print("  本地 sha    %s" % h12(want))
    print("  gray 线上   %s  (CM %s, %d 个 key)" % (h12(live), cur_cm, len(data)))
    try:
        shared = cm_data(SHARED_CM).get(fname)
        print("  共享 CM     %s  (%s)" % (h12(shared) if shared else "缺这个 key", SHARED_CM))
    except SystemExit:
        print("  共享 CM     读不到")
    if live == want:
        print("\n✓ gray 已经是这份内容，无事可做。")
        return 0
    print("\n  将要：① 建新 CM %s（只换 %s 这一个 key）" % (new_cm, fname))
    print("        ② patch %s 的 volumes[%d] 指向新 CM → 滚动更新 4 副本"
          % (prod_deploy(), gray_volume_index()))
    print("        ③ merge-patch 共享 CM %s 的 data（**不重启任何 acct pod**）" % SHARED_CM)
    print("\n  回滚：把 volumes[%d].configMap.name 改回 %s（旧 CM 不可变、不删）"
          % (gray_volume_index(), cur_cm))
    return 0


def cmd_apply(fname: str, force: bool) -> int:
    path = os.path.join(REPO_DIR, fname)
    want = open(path, encoding="utf-8").read()
    git_preflight(fname, force)

    cur_cm = gray_cm_name()
    data = cm_data(cur_cm)
    if fname not in data:
        raise SystemExit("⛔ %s 不在 CM %s 里 —— 这是新装，走 litellm-198-router-patch" % (fname, cur_cm))
    live_vs_git(fname, data[fname])
    if data[fname] == want:
        print("✓ gray 已是这份内容，跳过 ①②")
    ts = time.strftime("%Y%m%d%H%M%S")

    # --- 备份（先做，路径打出来存档）---
    for cm in (cur_cm, SHARED_CM):
        bak = "/tmp/bak_%s_%s.yaml" % (cm, ts)
        sh("%s -n %s get cm %s -o yaml > %s" % (KUBECTL, NS, cm, bak))
        print("备份 %s → 198:%s" % (cm, bak))

    if data[fname] != want:
        # --- ① 新 CM ---
        newdata = dict(data)
        newdata[fname] = want
        new_cm = GRAY_CM_PREFIX + h12("".join(
            "%s\0%s\0" % (k, v) for k, v in sorted(newdata.items())))
        payload = json.dumps({"apiVersion": "v1", "kind": "ConfigMap",
                              "metadata": {"name": new_cm, "namespace": NS},
                              "data": newdata}, ensure_ascii=False)
        b64 = base64.b64encode(payload.encode()).decode()
        # `create` 不是 `apply`：apply 要把整份塞进 last-applied 注解，
        # 33 个 key 会撞 262144 字节上限直接失败。
        # base64 走 stdin，不进 argv —— 875 KB 的载荷会撞 ARG_MAX（见 sh()）。
        sh("base64 -d | %s -n %s create -f -" % (KUBECTL, NS), stdin_data=b64)
        print("① 建好 CM %s（%d 个 key，只有 %s 变了）" % (new_cm, len(newdata), fname))

        # --- ② 切 volume ---
        i = gray_volume_index()
        patch = json.dumps([{"op": "replace",
                             "path": "/spec/template/spec/volumes/%d/configMap/name" % i,
                             "value": new_cm}])
        sh("%s -n %s patch deploy %s --type=json -p '%s'" % (KUBECTL, NS, prod_deploy(), patch))
        print("② %s volumes[%d] → %s，等滚动更新…" % (prod_deploy(), i, new_cm))
        print(sh("%s -n %s rollout status deploy %s --timeout=300s" % (KUBECTL, NS, prod_deploy())))

    # --- ③ 共享 CM：只改 data，不重启 ---
    pj = json.dumps({"data": {fname: want}}, ensure_ascii=False)
    b64 = base64.b64encode(pj.encode()).decode()
    # 同样走 stdin：这份只含单个文件，比 ① 小一个量级，但没理由留第二个 ARG_MAX 雷。
    sh("base64 -d > /tmp/_cbpatch.json && %s -n %s patch cm %s "
       "--type=merge --patch-file /tmp/_cbpatch.json" % (KUBECTL, NS, SHARED_CM),
       stdin_data=b64)
    print("③ 共享 CM %s data 已更新 —— **没有重启任何 pod**（acct 池在服务的号不能重启）"
          % SHARED_CM)
    print("   这些 pod 会在下次自然重启时才用上新版本，这是有意为之。")
    return cmd_verify(fname)


def cmd_verify(fname: str) -> int:
    want_sha = sha(open(os.path.join(REPO_DIR, fname), encoding="utf-8").read())
    rc = 0
    ps = pods()
    if not ps:
        print("⛔ 生产车道一个 Running pod 都没查到 —— 没对上账，不当绿")
        return 1
    print("\n== 逐副本验收（%d 个）==" % len(ps))
    for p in ps:
        got = sh("%s -n %s exec %s -- sha256sum /app/%s" % (KUBECTL, NS, p, fname),
                 check=False).split()
        ok = bool(got) and got[0] == want_sha
        # 🔴 这个 grep 必须按**被发布的那个文件**收窄。
        #    原先它 grep 整份日志里任何 import 错误，于是恒抓到既存的
        #    `[sitecustomize] responses_aclose import failed`（改动**前**的旧 pod
        #    同样 4 条）⇒ 每次发布都报 `import错误=4` 的假红，把整条验收拦住。
        #    一屏红里对照对象也红 ⇒ 先疑量具。2026-09-25 修。
        #    ⚠️ 别改成「减掉一个基线数」—— 那会在真出错时把错误数也一起减掉。
        #    正解是只认提到本文件名（或其模块名）的那些行。
        modname = fname[:-3] if fname.endswith(".py") else fname
        bad = sh("%s -n %s logs %s --since=10m 2>/dev/null | "
                 "grep -E 'ImportError|SyntaxError|ModuleNotFoundError' | "
                 "grep -cE '%s' || true"
                 % (KUBECTL, NS, p, modname), check=False).strip() or "0"
        print("  %s sha=%s  import错误=%s  %s"
              % ("✓" if ok and bad == "0" else "✗", (got[0][:12] if got else "读不到"),
                 bad, p))
        if not ok or bad != "0":
            rc = 1
    print("\n共享 CM %s 里这个文件 sha=%s（%s）"
          % (SHARED_CM, h12(cm_data(SHARED_CM).get(fname, "")),
             "已同步" if sha(cm_data(SHARED_CM).get(fname, "")) == want_sha else "⚠️ 未同步"))
    if rc:
        print("\n⛔ 有副本没对上 —— 回滚见文件头。")
    else:
        print("\n✓ 逐副本对齐。⚠️ 滚动窗口内的在途流式请求会断（0 event），"
              "那是重启窗口不是新代码；别拿那几分钟的报错给本次改动定罪。")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        description="更新 198 上一个**已在线**的 callback 文件（gray 哈希 CM 换代 + 共享 CM data 同步）")
    ap.add_argument("action", choices=["plan", "apply", "verify"])
    ap.add_argument("file", help="文件名，如 deepseek_responses_adapt.py")
    ap.add_argument("--force", action="store_true",
                    help="越过 git 门禁（会明确打印越过了什么）")
    a = ap.parse_args()
    if a.action == "plan":
        return cmd_plan(a.file, a.force)
    if a.action == "apply":
        return cmd_apply(a.file, a.force)
    return cmd_verify(a.file)


if __name__ == "__main__":
    sys.exit(main())
