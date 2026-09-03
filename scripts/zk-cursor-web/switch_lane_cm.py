#!/usr/bin/env python3
"""switch_lane_cm.py —— 把一条 lane 的 patch CM 换成另一份，逐条做、做完一条验完一条。

判据链（缺一环都不算过）：
  1. 改前整份 deploy json 备份到 198 /Data/backups，**回读 sha256 相等**才继续
     （「备份写了 ≠ 备份存在」：/Data/backups 属 root，不走 sudo 会静默失败）。
  2. patch 前断言 volumes[3].name == "patch"（形状变了就停手，不硬编下标闷头改）。
  3. rollout status 等就绪。
  4. **容器内 sha256sum /app/routes/responses.js == 目标 CM 里 responses.js 的 sha256**
     —— 这一条才是「新代码真的到岗了」，不是「我记得重启过」。
  5. serve_check：pod 内直打 8201 一发，出字才算数。

⚠️ 动之前先跑 lane_coldstart_probe.py：deploy strategy 是 Recreate，必然重启，
   而 seed 登录态死了的 lane 一重启就 CrashLoop（2026-09-02：85 就是这样，
   Running 能服务但冷启动握手 401）。

用法：python3 switch_lane_cm.py 84 zk-cursor-bpi-patch-pool [--apply]
"""
import hashlib
import json
import subprocess
import sys
import time

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]
sys.path.insert(0, __file__.rsplit("/", 1)[0])


def k(argstr, stdin=None, timeout=300):
    r = subprocess.run(SSH + ["sudo -n kubectl -n %s %s" % (NS, argstr)],
                       input=stdin, capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr, r.returncode


def backup(text, path):
    r = subprocess.run(SSH + ["sudo -n tee %s >/dev/null && sudo -n sha256sum %s" % (path, path)],
                       input=text, capture_output=True, text=True)
    got = r.stdout.strip().split()[0] if r.stdout.strip() else ""
    return got == hashlib.sha256(text.encode()).hexdigest(), got


def main():
    lane, target = sys.argv[1], sys.argv[2]
    apply = "--apply" in sys.argv
    dep = "zero-cursor-bpi-%s" % lane
    ts = time.strftime("%Y%m%d-%H%M%S")

    out, err, rc = k("get deploy %s -o json" % dep)
    if rc != 0:
        print("读 deploy 失败:", err[:200]); return 2
    d = json.loads(out)
    vols = d["spec"]["template"]["spec"]["volumes"]
    if vols[3]["name"] != "patch":
        print("❌ volumes[3] 不是 patch（是 %s），形状变了，停手" % vols[3]["name"]); return 2
    cur = vols[3]["configMap"]["name"]
    print("lane %s: %s -> %s" % (lane, cur, target))
    if cur == target:
        print("已经是目标 CM，无需 patch（仍会跑判据）")

    cmo, _, rc = k("get cm %s -o json" % target)
    if rc != 0:
        print("目标 CM 读不到"); return 2
    want_sha = hashlib.sha256(json.loads(cmo)["data"]["responses.js"].encode()).hexdigest()
    print("目标 responses.js sha256 = %s" % want_sha[:16])

    if not apply:
        print("\n(dry-run。加 --apply 执行)")
        return 0

    if cur != target:
        path = "/Data/backups/zk-bpi-deploy-%s-%s-pre-poolcm.json" % (lane, ts)
        ok, got = backup(out, path)
        print("备份 deploy -> %s  %s" % (path, "✅ 落盘且相等" if ok else "❌ 失败 " + got[:16]))
        if not ok:
            print("停手：备份没成功不许往下走"); return 2

        patch = json.dumps([{"op": "replace",
                             "path": "/spec/template/spec/volumes/3/configMap/name",
                             "value": target}])
        o, e, rc = k("patch deploy %s --type json -p '%s'" % (dep, patch))
        print((o or e).strip()[:200])
        if rc != 0:
            return 2
        o, e, rc = k("rollout status deploy/%s --timeout=240s" % dep, timeout=300)
        print((o or e).strip()[:200])
        if rc != 0:
            print("❌ rollout 没就绪 —— 多半是冷启动握手失败（seed 登录态死了）。"
                  "回滚: kubectl -n %s patch deploy %s --type json -p "
                  "'[{\"op\":\"replace\",\"path\":\"/spec/template/spec/volumes/3/configMap/name\","
                  "\"value\":\"%s\"}]'" % (NS, dep, cur))
            return 1

    # 判据 4：容器内字节
    o, _, _ = k("get pod -l app=%s --field-selector=status.phase=Running -o json" % dep)
    pods = json.loads(o).get("items") or []
    if not pods:
        print("❌ 没有 Running pod"); return 1
    pod = pods[0]["metadata"]["name"]
    o, e, rc = k("exec %s -- sha256sum /app/routes/responses.js" % pod)
    got_sha = o.strip().split()[0] if o.strip() else ""
    ok4 = got_sha == want_sha
    print("判据4 容器内 responses.js = %s  %s" % (got_sha[:16], "✅ 与目标 CM 相等" if ok4 else "❌ 不相等"))
    if not ok4:
        return 1

    # 判据 5：出字
    import lane_coldstart_probe as P
    ok5, why = P.serve_check(pod, "lane %s 换 CM 后" % lane)
    print("判据5 %s" % ("✅ " + why if ok5 else "❌ " + why))
    return 0 if ok5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
