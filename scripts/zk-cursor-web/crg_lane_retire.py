#!/usr/bin/env python3
"""crg_lane_retire.py —— 把某几条 lane 的腿从池别名里摘掉（cr-g-* / cursor-g-*）。

**为什么要摘 81/83/85**（2026-09-02 定的，判据是服务端实读，不是推断）：
这三条 lane 背后的账号是 **free 档**（`/backend-api/accounts/check/v4-2023-04-27` 实读
plan=free、53 features；模型目录 `/backend-api/models` 只有 10 个 slug，
没有 thinking / pro / instant / 任何 -wm）。它们**物理上答不出**菜单里的大多数名字 ——
不是"偶尔慢"、不是"账号不稳"，是这些模型在那个账号下根本不存在。
同事现在用的 `cursor-g-*` 池五条腿里有三条是它们，命中率 3/5。

一条硬门，是这个脚本存在的主要理由：
    **绝不把任何一个 model_name 摘成 0 条腿。**
`/model/delete` 一次删一行，删到最后一行时别名就变成一个"存在但没有 deployment"的空壳
——请求会拿到 `No deployments available`，比留着坏腿更糟（坏腿至少会 failover）。
所以先在内存里算一遍摘完之后每个名字还剩几条腿，任何一个算出来是 0 就整批停手。

用法：
    python3 crg_lane_retire.py --lanes 81,83,85                    # dry-run，列出要删的行
    python3 crg_lane_retire.py --lanes 81,83,85 --pools cursor-g   # 只摘同事在用的那个池
    python3 crg_lane_retire.py --lanes 81,83,85 --apply

删完必须做的两件事（脚本会提醒，不代做——它们各自有独立判据）：
  1. `kubectl rollout restart deploy/litellm-proxy` + `rollout status`
     （动过 LiteLLM_ProxyModelTable，四个副本的内存路由表要重建；只查一个副本是假绿）
  2. WA 亲和 flush，**按 model_group 精确 flush，禁 FLUSHALL**：
     `scripts/litellm-wa-flush-affinity.py --model-group <别名> --apply`
     不 flush 的话，已经被钉在被删 deployment 上的 key 要等 pin 过期才换腿。

回滚：从**活着的**同族腿整份拷贝重建（`crg_pool_register.py`）。
⚠️ 备份文件里只有 `model_name|id` 两列，**不能拿它直接重建** —— `litellm_params` 不在里面，
且 `/model/info` 对 `api_key` 脱敏。重建的拷贝源永远是一条活腿。
"""
import json
import os
import subprocess
import sys
import time

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=25", "cltx@10.68.13.198"]
DEFAULT_POOLS = ["cr-g", "cursor-g"]

PROG = r'''
import json, os, sys, urllib.request, urllib.error
CFG = json.loads(os.environ["RETIRE_CFG"])
MK = os.environ["LITELLM_MASTER_KEY"]
BASE = "http://localhost:4000"


def call(path, payload=None):
    h = {"Authorization": "Bearer " + MK, "Content-Type": "application/json"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


_, body = call("/model/info")
rows = json.loads(body)["data"]

# 分组：每个 model_name 现有哪些腿（id -> lane）
groups = {}
for r in rows:
    n = r.get("model_name") or ""
    if not any(n.startswith(p + "-") for p in CFG["pools"]):
        continue
    i = (r.get("model_info") or {}).get("id") or ""
    lane = ""
    for p in CFG["pools"]:
        tag = "zerokey-%s-" % p
        if i.startswith(tag):
            lane = i[len(tag):].split("-")[0]
            break
    groups.setdefault(n, []).append((i, lane))

doomed, keep_counts = [], {}
for n, legs in sorted(groups.items()):
    kill = [i for i, lane in legs if lane in CFG["lanes"]]
    left = len(legs) - len(kill)
    # **直连名 vs 池别名**：`cursor-g-81-5.6-sol` 这种把 lane 号编在名字里的是运维直连调试名，
    # 一个名字只有那一条腿。lane 下线时它整个消失才是对的，摘到 0 不是事故。
    # 池别名（`cursor-g-5.6-sol` / `cr-g-5.6-pro`）摘到 0 才是事故 —— 那是同事在用的名字。
    direct = any(("-%s-" % lane) in n or n.endswith("-%s" % lane) for lane in CFG["lanes"])
    keep_counts[n] = (len(legs), len(kill), left, direct)
    doomed += [(n, i) for i in kill]

print(json.dumps({"survey": keep_counts}))
print(json.dumps({"doomed": doomed}))

zeroed = [n for n, (t, k, l, direct) in keep_counts.items() if k and l == 0 and not direct]
if zeroed:
    # 摘成 0 腿 = 别名变空壳，请求拿 "No deployments available"，比留着坏腿更糟
    print(json.dumps({"abort": zeroed}))
    sys.exit(0)
if not CFG["apply"]:
    print(json.dumps({"dryrun": len(doomed)}))
    sys.exit(0)

ok, fail = 0, []
for n, i in doomed:
    st, b = call("/model/delete", {"id": i})
    if st >= 300:
        fail.append([i, st, b[:160]])
    else:
        ok += 1
print(json.dumps({"deleted": ok, "failed": fail}))
'''


def main():
    argv = sys.argv[1:]

    def opt(n, dv=None):
        if n in argv:
            i = argv.index(n)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return dv

    lanes = [x.strip() for x in (opt("--lanes") or "").split(",") if x.strip()]
    pools = [x.strip() for x in (opt("--pools") or ",".join(DEFAULT_POOLS)).split(",") if x.strip()]
    apply = "--apply" in argv
    if not lanes:
        print("要摘哪几条腿？--lanes 81,83,85")
        return 2

    cfg = {"lanes": lanes, "pools": pools, "apply": apply}
    print("摘腿: lane %s   池: %s   模式: %s\n"
          % (",".join(lanes), ",".join(pools), "APPLY" if apply else "dry-run"))

    r = subprocess.run(
        SSH + ["sudo -n kubectl -n %s exec -i deploy/litellm-proxy -- env RETIRE_CFG=%s python3 -"
               % (NS, json.dumps(json.dumps(cfg)))],
        input=PROG, capture_output=True, text=True, timeout=900)
    survey, doomed, res = {}, [], {}
    for ln in r.stdout.splitlines():
        if not ln.startswith("{"):
            continue
        d = json.loads(ln)
        survey = d.get("survey", survey)
        doomed = d.get("doomed", doomed)
        if "abort" in d:
            print("❌ 这些别名会被摘成 0 腿，整批停手：%s" % ", ".join(d["abort"]))
            print("   （0 腿 = 请求拿 No deployments available，比留着坏腿更糟）")
            return 2
        if "deleted" in d or "failed" in d:
            res = d
    if not survey:
        print("❌ 没拿到调查结果:\n%s\n%s" % (r.stdout[-600:], r.stderr[-600:]))
        return 2

    print("== 逐别名：现有腿 → 摘 → 剩 ==")
    for n in sorted(survey):
        t, k, l, direct = survey[n]
        if not k:
            continue
        tag = "  (直连名，随 lane 一起下线)" if direct else ("  ⬅ 只剩 1 条" if l == 1 else "")
        print("  %-26s %d → 摘 %d → 剩 %d%s" % (n, t, k, l, tag))
    npool = sum(1 for n in survey if survey[n][1] and not survey[n][3])
    ndir = sum(1 for n in survey if survey[n][1] and survey[n][3])
    print("\n共命中 %d 行（池别名 %d 个名字 / 直连名 %d 个名字）" % (len(doomed), npool, ndir))

    if not apply:
        # 备份留档：这是"哪些行是我删的"的唯一判据
        ts = time.strftime("%Y%m%d-%H%M%S")
        p = "/tmp/crg-retire-%s.txt" % ts
        with open(p, "w") as f:
            for n, i in doomed:
                f.write("%s|%s\n" % (n, i))
        print("待删清单已存 %s（%d 行）" % (p, len(doomed)))
        print("\n(dry-run。加 --apply 执行)")
        return 0

    print("\n删除结果: %d 行 OK" % res.get("deleted", 0))
    for i, st, b in res.get("failed", []):
        print("  ❌ %s => %s %s" % (i, st, b))
    print("\n⚠️ 还没完，两件事各有独立判据：")
    print("  1. kubectl rollout restart deploy/litellm-proxy + rollout status（四副本路由表重建）")
    print("  2. WA 亲和 flush（按 model_group 精确，禁 FLUSHALL）:")
    print("     scripts/litellm-wa-flush-affinity.py --model-group <别名> --apply")
    return 0 if not res.get("failed") else 1


if __name__ == "__main__":
    sys.exit(main())
