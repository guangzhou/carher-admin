#!/usr/bin/env python3
"""make_pool_cm.py —— 第 1 步：新建 `zk-cursor-bpi-patch-pool` = `zk-cursor-bpi-patch-82` 逐字节拷贝。

为什么要第三份 CM 而不是让池 lane 直接挂 `-82`：
  那样以后改 82 = 同时改全池，canary 就没了，违背「82 单独留出来」。
为什么不 patch 共用 CM：
  101 会变成「CM 内容新了、pod 还跑旧的」——pool_consistency 真红，且 101 一旦因任何原因
  重启就静默吃到新代码，是定时炸弹。

判据：新 CM 15 个 key；**逐 key sha256 与 -82 全等（15/15）**，特别是
  raw.js=4b067bb4…、responses.js=ba2f5e77…。不全等就删掉重来。
回滚：kubectl delete cm zk-cursor-bpi-patch-pool（此刻还没人挂它，零影响）。
"""
import hashlib
import json
import subprocess
import sys
import time

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]
SRC = "zk-cursor-bpi-patch-82"
DST = "zk-cursor-bpi-patch-pool"
SHARED = "zk-cursor-bpi-patch"


def k(argstr, stdin=None):
    r = subprocess.run(SSH + ["sudo -n kubectl -n %s %s" % (NS, argstr)],
                       input=stdin, capture_output=True, text=True)
    return r.stdout, r.stderr, r.returncode


def digests(cm):
    return {kk: hashlib.sha256(vv.encode()).hexdigest() for kk, vv in (cm.get("data") or {}).items()}


def main():
    apply = "--apply" in sys.argv
    ts = time.strftime("%Y%m%d-%H%M%S")

    out, err, rc = k("get cm %s -o json" % SRC)
    if rc != 0:
        print("读 %s 失败: %s" % (SRC, err[:200])); return 2
    src = json.loads(out)
    sh_out, _, sh_rc = k("get cm %s -o json" % SHARED)
    if sh_rc != 0:
        print("读 %s 失败" % SHARED); return 2

    dsrc = digests(src)
    print("%s: %d 个 key" % (SRC, len(dsrc)))
    for kk in ("raw.js", "responses.js"):
        print("   %-14s %s" % (kk, dsrc.get(kk, "(缺)")[:8]))

    exist, _, exist_rc = k("get cm %s -o json" % DST)
    if exist_rc == 0:
        print("\n⚠️ %s 已存在，不重复创建；直接走逐 key 校验。" % DST)
    if not apply and exist_rc != 0:
        print("\n(dry-run。加 --apply 执行)")
        return 0

    if exist_rc != 0:
        # 备份两份现有 CM（198 的 /Data/backups）
        for name in (SRC, SHARED):
            path = "/Data/backups/zk-cursor-bpi-cm-%s-%s-pre-pool.json" % (
                "82" if name == SRC else "shared", ts)
            o, e, rc2 = k("get cm %s -o json" % name)
            # /Data/backups 属 root，cltx 直接 `cat >` 会 Permission denied 且我们看不见
            # ——「备份写了 ≠ 备份存在」，所以走 sudo -n tee，并**回读 sha256 当判据**。
            r = subprocess.run(SSH + ["sudo -n tee %s >/dev/null && sudo -n sha256sum %s" % (path, path)],
                               input=o, capture_output=True, text=True)
            got = r.stdout.strip().split()[0] if r.stdout.strip() else ""
            want = hashlib.sha256(o.encode()).hexdigest()
            ok = got == want
            print("备份 %-24s -> %s %s" % (name, (got or "失败")[:16], "✅落盘且校验相等" if ok else "❌ 没落盘/不相等"))
            if not ok:
                print("   停手：备份没成功就不许往下走"); return 2

        new = {"apiVersion": "v1", "kind": "ConfigMap",
               "metadata": {"name": DST, "namespace": NS,
                            "labels": (src.get("metadata") or {}).get("labels") or {}},
               "data": src["data"]}
        o, e, rc2 = k("create -f -", stdin=json.dumps(new))
        print("\n" + (o.strip() or e.strip()[:300]))
        if rc2 != 0:
            return 2

    # 判据：逐 key sha256 全等
    out, err, rc = k("get cm %s -o json" % DST)
    ddst = digests(json.loads(out))
    same = sum(1 for kk in dsrc if ddst.get(kk) == dsrc[kk])
    print("\n判据: %s %d 个 key；与 %s 逐 key sha256 相等 %d/%d"
          % (DST, len(ddst), SRC, same, len(dsrc)))
    if len(ddst) != len(dsrc) or same != len(dsrc):
        print("❌ 不全等 —— 删掉重来: kubectl -n %s delete cm %s" % (NS, DST))
        return 1
    print("✅ 逐字节相等")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
