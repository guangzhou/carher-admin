#!/usr/bin/env python3
"""zerokey-patch-deploy.py — 把 zerokey 补丁代码发到 CM 并重启在用的号。

**必须在 198 上以 root 执行**（需要 kubectl）::

    scp scripts/zerokey-patch-deploy.py \\
        scripts/chatgpt-onboard/zerokey-codex/zerokey-patch/routes/bpi-codex.js \\
        scripts/chatgpt-onboard/zerokey-codex/zerokey-patch/routes/responses.js \\
        cltx@10.68.13.198:/tmp/
    ssh cltx@10.68.13.198 'sudo python3 /tmp/zerokey-patch-deploy.py /tmp/bpi-codex.js'
    ssh cltx@10.68.13.198 'sudo python3 /tmp/zerokey-patch-deploy.py --dry-run /tmp/*.js'

它替代此前两个躺在 198 `/tmp` 里的临时脚本（`zkcm.py` + `restart18.sh`），
那两个各自带一个**会骗人**的毛病，都是本文件要修掉的：

1. `zkcm.py` 的断言是对的（确实逐字节比对），但**打印是写死的**——
   不管改哪个文件都输出「除 responses.js 外 9 个逐字节未变」。2026-08-08 只改
   了 `bpi-codex.js`，它照样这么说，字面意思等于"你的改动没进去"，害我去查哈希
   才敢确认。日志说的必须是**实际发生的事**，不是当初写脚本时那次发生的事。
2. `restart18.sh` 把账号清单 `83…151` **写死**，且无条件打印「18 台重启完成」、
   所有错误吞进 `/dev/null`。这是快照类故障的同一个模子：号一轮换，新号还跑着
   旧代码，而脚本照样报成功。这里改成**从路由表现查**（同
   [zerokey-agent-contract-rollout.py] 的判据），并逐台报真实状态。

为什么最后要进 pod 里 grep
--------------------------
CM 更新成功 ≠ pod 加载到了新代码：`zk-image-patch` 是全队共用 CM，但"把哪些文件
cp 进 /app"是**逐个 Deployment 的启动参数**。少配一行，那台 pod 就永远是旧的
（`responses.js` 的 require 做了容错，不会崩，所以**完全静默**）。所以收尾必须
抽查 pod 内文件哈希，而不是看 rollout 成功就完事。
"""
import hashlib
import json
import os
import subprocess
import sys

NS = os.environ.get("LITELLM_NS", "litellm-product")
CM = os.environ.get("ZK_PATCH_CM", "zk-image-patch")


def k(*a):
    r = subprocess.run(["kubectl", "-n", NS, *a], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"kubectl {' '.join(a[:3])} FAILED:\n{r.stderr[:600]}")
    return r.stdout


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:8]


def registered_accounts():
    """只取 LiteLLM 路由里真在用的号。

    集群里有 68 个 zero-N Deployment，路由里只注册了其中一部分。对全部 68 个做
    rollout restart 会把**早就死掉、只靠内存里旧 session 还显示 Running** 的号
    打回原形（2026-08-07 实测 4 台起不来，全是 Sentinel 401 token_expired）。
    """
    sql = ("select distinct substring(model_id from '-([0-9]+)-') "
           "from \"LiteLLM_ProxyModelTable\" where model_id like '%zk-%'")
    out = k("exec", "litellm-db-0", "--", "psql", "-U", "litellm", "-d", "litellm",
            "-A", "-t", "-c", sql)
    return sorted({int(x) for x in out.split() if x.isdigit()})


def patch_cm(files: dict, dry: bool) -> list:
    """写 CM，返回**实际内容发生变化**的 key 列表。"""
    before = dict(json.loads(k("get", "cm", CM, "-o", "json"))["data"])
    changed = [n for n, body in files.items() if before.get(n) != body]
    added = [n for n in files if n not in before]
    print(f"[cm] {CM} 现有 {len(before)} 个 key")
    for n, body in files.items():
        state = "新增" if n in added else ("改变" if n in changed else "未变")
        print(f"[cm]   {n:<18} {state}  {md5(body)}  {len(body)} bytes")
    if dry:
        print("[cm] --dry-run，不写入")
        return changed
    if not changed:
        print("[cm] 内容与线上一致，跳过写入")
        return []

    k("patch", "cm", CM, "--type", "merge",
      "--patch", json.dumps({"data": files}, ensure_ascii=False))
    after = json.loads(k("get", "cm", CM, "-o", "json"))["data"]

    # 逐字节断言：目标 key 必须等于我给的内容，其余 key 一个字都不许动
    for n, body in files.items():
        assert after.get(n) == body, f"写入后 {n} 与本地不一致"
    untouched = [n for n in before if n not in files]
    for n in untouched:
        assert after[n] == before[n], f"动到了别的 key: {n}"
    assert set(after) == set(before) | set(files), "key 集合意外变化"
    # 报实际情况，不报写脚本那天的情况
    print(f"[cm] OK 共 {len(after)} 个 key；本次改动 {changed or '无'}；"
          f"其余 {len(untouched)} 个逐字节未变")
    return changed


def restart(accts: list, dry: bool) -> list:
    print(f"[restart] 路由里在用的号 {len(accts)} 个: {accts}")
    if dry:
        return []
    for n in accts:
        k("rollout", "restart", f"deploy/zero-{n}")
    bad = []
    for n in accts:
        r = subprocess.run(["kubectl", "-n", NS, "rollout", "status",
                            f"deploy/zero-{n}", "--timeout=180s"],
                           capture_output=True, text=True)
        if r.returncode:
            bad.append(n)
            print(f"[restart] zero-{n} 未就绪: {(r.stderr or r.stdout).strip()[:120]}")
    print(f"[restart] 就绪 {len(accts) - len(bad)}/{len(accts)}"
          + (f"，失败 {bad}" if bad else ""))
    return bad


def verify_in_pods(files: dict, accts: list, sample: int = 3) -> bool:
    """抽查 pod 内文件是否真是新的 —— CM 对了不代表 pod 加载到了（见模块头）。"""
    ok = True
    names = k("get", "pods", "--no-headers", "-o", "custom-columns=:metadata.name")
    for n in accts[:sample]:
        pod = next((p for p in names.split() if p.startswith(f"zero-{n}-")), None)
        if not pod:
            print(f"[verify] zero-{n} 没有 pod"); ok = False; continue
        for fn, body in files.items():
            r = subprocess.run(["kubectl", "-n", NS, "exec", pod, "--",
                                "md5sum", f"/app/routes/{fn}"],
                               capture_output=True, text=True)
            got = (r.stdout or "").split(" ")[0][:8]
            want = hashlib.md5(body.encode()).hexdigest()[:8]
            hit = got == want
            ok = ok and hit
            print(f"[verify] {pod} {fn}: pod={got or '读不到'} 本地={want}"
                  f" {'OK' if hit else '✗ 该 Deployment 的启动参数少了 cp 这个文件'}")
    return ok


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = "--dry-run" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    files = {os.path.basename(p): open(p, encoding="utf-8").read() for p in args}

    changed = patch_cm(files, dry)
    accts = registered_accounts()
    if not changed and not dry:
        print("[done] 内容未变，不重启（重启是有代价的：会打断在跑的会话）")
        raise SystemExit(0)
    bad = restart(accts, dry)
    if dry:
        raise SystemExit(0)
    good = verify_in_pods(files, [a for a in accts if a not in bad])
    print("[done]" if good and not bad else "[!!] 有未就绪或未生效的号，见上")
    raise SystemExit(0 if good and not bad else 1)
