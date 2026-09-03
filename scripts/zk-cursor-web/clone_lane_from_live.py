#!/usr/bin/env python3
"""clone_lane_from_live.py —— 从**活着的**一条 lane 克隆出新 lane（deploy + svc）。

为什么不用 clone_web_fc_lane_v2.py：那个脚本连模型行一起建，且建的是 `cursor-g-*` 名字；
本轮的模型行走 crg_pool_register.py（从 82 的 /model/info 整份拷贝）。这里**只做 K8s 那一半**。

核心纪律（记忆 feedback_new_lane_gate_must_be_relative_and_clone_from_live）：
**写死模板必陈旧**。new-pod.sh 里那份 manifest 模板带着 `tolerations: dedicated=standby`，
而现网活着的 lane 84 `tolerations=null` —— 因为 pod 直接钉 `nodeName` 会绕开调度器，
`NoSchedule` 是调度器侧的检查，根本不生效。照模板写不会坏，但会让"新 lane 和老 lane 长得
不一样"，下次比对时多一处噪音。**照抄活的，只换身份字段。**

只换四类身份字段，别的一个键都不碰：
  1. `metadata.name` / labels `app`,`account` / `spec.selector.matchLabels.app`
  2. volumes 里两条 hostPath：`zero-<N>-cache`（convcache）、`zero-<N>`（seed）
  3. env `ZK_USER=acct<N>`
  4. svc 的 name / selector

建前硬断言（形状变了就停手，不猜）：
  · volumes[1].name == 'seed' 且 hostPath 指向参照 lane 的号
  · volumes[3].name == 'patch' 且 configMap.name == 期望的那份（默认 zk-cursor-bpi-patch-pool）
    —— 挂错 CM = 这条腿悄悄跑另一份代码，正是这一轮要修的那个病
  · 目标 deploy/svc 不存在（不覆盖活的对象）

用法：
    python3 clone_lane_from_live.py --ref 84 --new 135                 # dry-run，打印 diff
    python3 clone_lane_from_live.py --ref 84 --new 135 --apply
    python3 clone_lane_from_live.py --ref 84 --new 135,136,137 --apply

回滚：`kubectl -n litellm-product delete deploy/zero-cursor-bpi-<N> svc/zero-cursor-bpi-<N>`
（新建对象，删掉即净，不影响任何既有 lane）。
"""
import json
import subprocess
import sys

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=25", "cltx@10.68.13.198"]
EXPECT_CM = "zk-cursor-bpi-patch-pool"
SEED_ROOT = "/Data/zerokey-sessions"


def kc(args, stdin=None, timeout=180):
    r = subprocess.run(SSH + ["sudo -n kubectl -n %s %s" % (NS, args)],
                       input=stdin, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def get_json(kind, name):
    rc, out, err = kc("get %s %s -o json" % (kind, name))
    if rc != 0:
        return None
    return json.loads(out)


def strip(o):
    """剥掉运行时字段，剩下的才是可以重建的 spec。"""
    m = o.get("metadata", {})
    for k in ("resourceVersion", "uid", "creationTimestamp", "generation",
              "managedFields", "selfLink", "annotations"):
        m.pop(k, None)
    o.pop("status", None)
    return o


def build(ref_deploy, ref_svc, ref, new):
    d = strip(json.loads(json.dumps(ref_deploy)))
    s = strip(json.loads(json.dumps(ref_svc)))
    old_name, new_name = "zero-cursor-bpi-%s" % ref, "zero-cursor-bpi-%s" % new

    ps = d["spec"]["template"]["spec"]
    vols = ps["volumes"]
    # —— 硬断言：形状必须是我以为的那个形状 ——
    assert vols[1]["name"] == "seed", "volumes[1] 不是 seed，形状变了，停手"
    assert vols[1]["hostPath"]["path"] == "%s/zero-%s" % (SEED_ROOT, ref), \
        "参照 lane 的 seed 路径不是 zero-%s：%s" % (ref, vols[1]["hostPath"]["path"])
    assert vols[3]["name"] == "patch", "volumes[3] 不是 patch，形状变了，停手"
    got_cm = vols[3]["configMap"]["name"]
    assert got_cm == EXPECT_CM, \
        "参照 lane 挂的是 %s，不是 %s —— 从它克隆会把错的 CM 传下去" % (got_cm, EXPECT_CM)

    d["metadata"]["name"] = new_name
    d["metadata"]["labels"] = dict(d["metadata"].get("labels") or {},
                                   app=new_name, account=str(new))
    d["spec"]["selector"]["matchLabels"]["app"] = new_name
    tm = d["spec"]["template"]["metadata"]
    tm["labels"] = dict(tm.get("labels") or {}, app=new_name, account=str(new))
    vols[0]["hostPath"]["path"] = "%s/zero-%s-cache" % (SEED_ROOT, new)
    vols[1]["hostPath"]["path"] = "%s/zero-%s" % (SEED_ROOT, new)

    env = ps["containers"][0]["env"]
    hit = [e for e in env if e["name"] == "ZK_USER"]
    assert len(hit) == 1, "ZK_USER 不是恰好一个，停手"
    hit[0]["value"] = "acct%s" % new

    s["metadata"]["name"] = new_name
    s["metadata"]["labels"] = dict(s["metadata"].get("labels") or {}, app=new_name)
    s["spec"]["selector"]["app"] = new_name
    s["spec"].pop("clusterIP", None)
    s["spec"].pop("clusterIPs", None)
    return d, s, old_name


def main():
    argv = sys.argv[1:]

    def opt(n, dv=None):
        if n in argv:
            i = argv.index(n)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return dv

    ref = opt("--ref", "84")
    news = (opt("--new") or "").split(",")
    apply = "--apply" in argv
    news = [n.strip() for n in news if n.strip()]
    if not news:
        print("要建哪几条？--new 135,136")
        return 2

    rd = get_json("deploy", "zero-cursor-bpi-%s" % ref)
    rs = get_json("svc", "zero-cursor-bpi-%s" % ref)
    if not rd or not rs:
        print("❌ 参照 lane %s 的 deploy/svc 读不到" % ref)
        return 2
    ready = (rd.get("status") or {}).get("readyReplicas") or 0
    if ready < 1:
        # 「活着」是这个脚本的前提：从一条自己都没起来的 lane 克隆，等于把它的病一起复制
        print("❌ 参照 lane %s readyReplicas=%s —— 它自己没活着，不能当拷贝源" % (ref, ready))
        return 2
    print("拷贝源: zero-cursor-bpi-%s (ready=%d)  CM=%s\n" % (ref, ready, EXPECT_CM))

    rc_all = 0
    for new in news:
        name = "zero-cursor-bpi-%s" % new
        print("==================== %s ====================" % name)
        if get_json("deploy", name):
            print("  ⚠️ 已存在，跳过（不覆盖活对象）")
            continue
        try:
            d, s, old = build(rd, rs, ref, new)
        except AssertionError as e:
            print("  ❌ 断言失败: %s" % e)
            return 2
        ps = d["spec"]["template"]["spec"]
        print("  name=%s  nodeName=%s  ZK_USER=%s" % (
            d["metadata"]["name"], ps.get("nodeName"),
            [e for e in ps["containers"][0]["env"] if e["name"] == "ZK_USER"][0]["value"]))
        print("  seed=%s  cache=%s  patchCM=%s"
              % (ps["volumes"][1]["hostPath"]["path"], ps["volumes"][0]["hostPath"]["path"],
                 ps["volumes"][3]["configMap"]["name"]))
        # 残留检查：把参照 lane 的名字漏在任何字段里，是这类克隆最常见的错
        blob = json.dumps(d) + json.dumps(s)
        if old in blob:
            print("  ❌ 产物里仍残留参照 lane 名 %r —— 有字段没换到，停手" % old)
            return 2
        if not apply:
            print("  (dry-run，加 --apply 执行)")
            continue
        for kind, obj in (("deploy", d), ("svc", s)):
            rc, out, err = kc("create -f -", stdin=json.dumps(obj))
            print("  %s create: %s" % (kind, (out or err).strip()[:160]))
            if rc != 0:
                rc_all = 1
        rc, out, err = kc("rollout status deploy/%s --timeout=180s" % name, timeout=240)
        print("  rollout: %s" % (out or err).strip()[:160])
        if rc != 0:
            rc_all = 1
    if apply:
        print("\n⚠️ 建完还不算数：每条新 lane 必须跑 lane_model_catalog.py 验模型目录"
              "（必须出现 thinking/pro/instant，19+ slug），**不带病入池**。")
    return rc_all


if __name__ == "__main__":
    sys.exit(main())
