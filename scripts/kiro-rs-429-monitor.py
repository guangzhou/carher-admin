#!/usr/bin/env python3
"""kiro.rs 上游 429 监控：按小时出「请求数 / 首发 429 / 最终 429 / 429 reason / window 换号」。

为什么读节点日志而不是 kubectl logs
  kubectl logs 只剩当前文件（约 10 分钟～1 小时）；kubelet 在 242 上还留着轮转件（含 .gz），
  经 `kubectl get --raw /api/v1/nodes/<node>/proxy/logs/pods/<dir>/kiro-rs/` 能全拉回来。
  （pod 重建后旧 pod 的目录也还在，一起读）

用法
  scripts/kiro-rs-429-monitor.py                 # 节点上能读到的全部
  scripts/kiro-rs-429-monitor.py --since 2026-10-23T00  # 只看这个 UTC 小时之后
"""
import argparse, collections, gzip, re, subprocess, sys

PROD = "cltx@10.68.13.198"
NODE = "aiyjy-litellm-242"
BASE = f"/api/v1/nodes/{NODE}/proxy/logs/pods/"


def raw(path: str) -> bytes:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", PROD, f"sudo -n kubectl get --raw '{path}'"],
                       capture_output=True, timeout=300)
    if r.returncode != 0:
        sys.exit(f"STOP: 读 {path} 失败: {r.stderr.decode()[:200]}")
    return r.stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="UTC 小时前缀，如 2026-10-23T00")
    a = ap.parse_args()

    dirs = sorted(set(re.findall(r'href="(kiro-rs_kiro-rs-[^"/]+)/?"', raw(BASE).decode())))
    if not dirs:
        sys.exit("STOP: 节点上没有 kiro-rs pod 日志目录")
    lines = []
    for d in dirs:
        files = re.findall(r'href="([^"]+)"', raw(f"{BASE}{d}/kiro-rs/").decode())
        for f in files:
            b = raw(f"{BASE}{d}/kiro-rs/{f}")
            if f.endswith(".gz"):
                b = gzip.decompress(b)
            lines += b.decode("utf-8", "ignore").splitlines()

    ansi = re.compile(r"\x1b\[[0-9;]*m")
    h = collections.defaultdict(collections.Counter)
    reasons = collections.defaultdict(collections.Counter)
    seen = set()
    for raw_line in lines:
        l = ansi.sub("", raw_line)
        m = re.search(r" F (\d{4}-\d\d-\d\dT\d\d):\d\d:\d\d\.\d+Z", l)
        if not m or m.group(1) < a.since:
            continue
        if l in seen:  # 轮转边界可能重复
            continue
        seen.add(l)
        hr = m.group(1)
        c = h[hr]
        if "Received POST /v1/messages" in l:
            c["req"] += 1
        elif "Too Many" in l or "429" in l and "冷却并立刻换号" in l:
            if "冷却并立刻换号" in l:
                c["switch"] += 1
                if "尝试 1/" in l:
                    c["first429"] += 1
            elif "ERROR" in l:
                c["final429"] += 1
            elif "WARN" in l:
                c["retry429"] += 1
                if "尝试 1/" in l:
                    c["first429"] += 1
            r = re.search(r'"reason":("?[A-Za-z_]+"?|null)', l)
            if r:
                reasons[hr][r.group(1).strip('"')] += 1
        elif "额度已用尽" in l and "已被禁用" in l:
            c["quota_disabled"] += 1
        elif "suspended" in l.lower():
            c["suspended"] += 1

    print("UTC小时        请求   首发429  首发429%  同号重试429  window换号  最终429  额度禁用  suspended  429 reason")
    for hr in sorted(h):
        c = h[hr]
        pct = f"{100 * c['first429'] / c['req']:.0f}%" if c["req"] else "-"
        rs = ",".join(f"{k}:{v}" for k, v in reasons[hr].most_common())
        print(f"{hr}  {c['req']:6} {c['first429']:8} {pct:>8} {c['retry429']:11} {c['switch']:10} {c['final429']:8} "
              f"{c['quota_disabled']:8} {c['suspended']:10}  {rs}")


if __name__ == "__main__":
    main()
