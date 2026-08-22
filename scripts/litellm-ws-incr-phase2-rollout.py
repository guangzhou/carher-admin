#!/usr/bin/env python3
"""
acct 多账户链路 WS 增量传输 —— Phase 2 全池灰度编排（在 198 上跑，需 kubectl）。

前提（Phase 1 过闸后才许执行）：acct-82 canary soak ≥24h 干净：
  命中率≥60% + midstream_break≤0.5% + SpendLogs 客户端失败无增量。

两段式灰度（可回滚/可验证/可灰度）：
  Stage A（行为中性铺镜像）：按波次把活跃 acct pod 显式 set image 到 ws-incr tag。
    网关默认 OFF → 字节级同 stock；验证 = rollout 完成 + 无 ws_incr 日志（不该有）。
    ⚠ 不用"重指 acct-stable 别名"：实测 pullPolicy=IfNotPresent，retag 后节点仍用
    缓存旧镜像，静默不生效。显式 tag 还让回滚按 deploy 粒度精确。
  Stage B（按波开闸）：set env CHATGPT_WS_INCREMENTAL=1（+LOG=1）。
    验证 = ws_incr mode= 行出现、midstream_break 不超阈、pod 无重启。

回滚：
  --rollback-env  ：波内 env 置 0（gate off = 字节级 stock，秒级，镜像留着）
  --rollback-image：从 backup JSON 恢复原 image（deploy 粒度）

默认 dry-run 只打印计划；--apply 才执行。acct-82（canary）自动排除。
backup 落 /root/ws-incr-rollout/backup-<ts>.json，改前必须已写盘。

用法：
  python3 litellm-ws-incr-phase2-rollout.py --stage inventory
  python3 litellm-ws-incr-phase2-rollout.py --stage a --wave 1 [--apply]
  python3 litellm-ws-incr-phase2-rollout.py --stage b --wave 1 [--apply]
  python3 litellm-ws-incr-phase2-rollout.py --rollback-env  --wave 1 --apply
  python3 litellm-ws-incr-phase2-rollout.py --rollback-image --backup <file> --apply
"""
import argparse
import json
import os
import subprocess
import sys
import time

NS = "litellm-product"
WS_IMG = "127.0.0.1:5000/litellm-carher:vanilla-v1.90.2.cache-session-fix-v2.ws-incr-prod2-20260823"
# 仅迁移与 ws-incr 镜像同 base 的 pod（digest 已核对 acct-stable == 该 tag）：
ELIGIBLE_IMAGES = {
    "127.0.0.1:5000/litellm-carher:acct-stable",
    "127.0.0.1:5000/litellm-carher:vanilla-v1.90.2.cache-session-fix-v2-20260817-103630",
}
CANARY = "chatgpt-acct-82"          # 已在 Phase 1，排除
BACKUP_DIR = "/root/ws-incr-rollout"
# 波次定义：first 小步试水，后续放量。按 deploy 名排序后切片。
WAVES = [3, 8, None]                 # wave1=3 台, wave2=8 台, wave3=其余全部


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"CMD FAIL: {cmd}\n{r.stderr[:400]}")
    return r.stdout.strip()


def active_eligible():
    """活跃(replicas=1)且 base 适格的 acct deploy 列表（排除 canary），稳定排序。"""
    out = sh(
        f"kubectl -n {NS} get deploy -o jsonpath="
        "'{range .items[*]}{.metadata.name} {.spec.template.spec.containers[0].image} "
        "{.spec.replicas}{\"\\n\"}{end}'"
    )
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        name, img, reps = parts
        if not name.startswith("chatgpt-acct") or name == CANARY:
            continue
        if reps != "1":
            continue
        rows.append((name, img))
    eligible = [(n, i) for n, i in rows if i in ELIGIBLE_IMAGES]
    skipped = [(n, i) for n, i in rows if i not in ELIGIBLE_IMAGES and not i.endswith(WS_IMG.split(":")[-1])]
    done = [(n, i) for n, i in rows if i == WS_IMG]
    return sorted(eligible), sorted(skipped), sorted(done)


def wave_slice(names, wave):
    """wave 从 1 计。按 WAVES 尺寸切片。"""
    start = 0
    for i, size in enumerate(WAVES, 1):
        end = len(names) if size is None else start + size
        if i == wave:
            return names[start:end]
        start = end
    return []


def backup(deploys):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    data = []
    for name in deploys:
        img = sh(f"kubectl -n {NS} get deploy/{name} -o jsonpath='{{.spec.template.spec.containers[0].image}}'")
        env = sh(f"kubectl -n {NS} get deploy/{name} -o json")
        envs = {
            e.get("name"): e.get("value")
            for e in json.loads(env)["spec"]["template"]["spec"]["containers"][0].get("env", [])
            if e.get("name", "").startswith("CHATGPT_WS_")
        }
        data.append({"deploy": name, "image": img, "ws_env": envs})
    path = os.path.join(BACKUP_DIR, f"backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
    print(f"BACKUP -> {path}")
    return path


def verify_pod(name, expect_ws_lines):
    """rollout 完成 + pod Running 无重启；Stage A 期望零 ws_incr 行，Stage B 期望出现。"""
    sh(f"kubectl -n {NS} rollout status deploy/{name} --timeout=180s")
    pod = sh(f"kubectl -n {NS} get pod -l app={name} -o jsonpath='{{.items[0].metadata.name}}'")
    restarts = sh(f"kubectl -n {NS} get pod {pod} -o jsonpath='{{.status.containerStatuses[0].restartCount}}'")
    if restarts != "0":
        raise SystemExit(f"VERIFY FAIL {name}: restarts={restarts}")
    breaks = sh(f"kubectl -n {NS} logs {pod} -c litellm --tail=-1 2>/dev/null | grep -c ws_incr_midstream_break || true", check=False)
    print(f"  verify {name}: pod={pod} restarts=0 midstream_break={breaks or 0} "
          f"(ws lines expected={'yes' if expect_ws_lines else 'no'})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["inventory", "a", "b"])
    ap.add_argument("--wave", type=int, default=1)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback-env", action="store_true")
    ap.add_argument("--rollback-image", action="store_true")
    ap.add_argument("--backup", help="backup json for --rollback-image")
    args = ap.parse_args()

    eligible, skipped, done = active_eligible()
    names = [n for n, _ in eligible]

    if args.rollback_image:
        if not args.backup:
            raise SystemExit("--rollback-image needs --backup <file>")
        with open(args.backup) as f:
            for row in json.load(f):
                cmd = f"kubectl -n {NS} set image deploy/{row['deploy']} litellm={row['image']}"
                print(("APPLY " if args.apply else "DRY   ") + cmd)
                if args.apply:
                    sh(cmd)
                    verify_pod(row["deploy"], expect_ws_lines=False)
        return

    if args.rollback_env:
        targets = wave_slice([n for n, _ in done], args.wave)
        for name in targets:
            cmd = f"kubectl -n {NS} set env deploy/{name} CHATGPT_WS_INCREMENTAL=0"
            print(("APPLY " if args.apply else "DRY   ") + cmd)
            if args.apply:
                sh(cmd)
                verify_pod(name, expect_ws_lines=False)
        return

    if args.stage == "inventory" or args.stage is None:
        print(f"eligible={len(eligible)} done={len(done)} skipped_base_mismatch={len(skipped)}")
        for n, i in eligible:
            print(f"  ELIGIBLE {n} {i.split(':')[-1]}")
        for n, i in done:
            print(f"  DONE     {n}")
        for n, i in skipped:
            print(f"  SKIP     {n} {i.split(':')[-1]}  <-- base 不符，人工决策")
        for i, size in enumerate(WAVES, 1):
            w = wave_slice(names, i)
            print(f"  wave{i}: {len(w)} -> {', '.join(w[:6])}{' ...' if len(w) > 6 else ''}")
        return

    if args.stage == "a":
        targets = wave_slice(names, args.wave)
    else:
        # Stage B 目标 = 已铺镜像(done)里的本波。空波就报错，绝不静默扩大范围。
        targets = wave_slice([n for n, _ in done], args.wave)
    if not targets:
        raise SystemExit(f"wave {args.wave} 为空（stage {args.stage}）")

    print(f"stage={args.stage} wave={args.wave} targets={len(targets)}: {', '.join(targets)}")
    if args.apply:
        backup(targets)
    for name in targets:
        if args.stage == "a":
            cmd = f"kubectl -n {NS} set image deploy/{name} litellm={WS_IMG}"
        else:
            cmd = f"kubectl -n {NS} set env deploy/{name} CHATGPT_WS_INCREMENTAL=1 CHATGPT_WS_INCREMENTAL_LOG=1"
        print(("APPLY " if args.apply else "DRY   ") + cmd)
        if args.apply:
            sh(cmd)
            verify_pod(name, expect_ws_lines=(args.stage == "b"))
    if args.apply:
        print(f"wave {args.wave} stage {args.stage} 完成。观察后再进下一波；"
              f"回滚：--rollback-env --wave {args.wave} --apply（秒级）")


if __name__ == "__main__":
    main()
