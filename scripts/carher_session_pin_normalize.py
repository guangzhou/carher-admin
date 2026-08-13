#!/usr/bin/env python3
"""
carher_session_pin_normalize.py

批量检查/修复 carher her 实例里 sessions.json 的 stale 模型 pin。

背景
----
每个 her 的运行时把每个会话(main / 群 / DM / cron / subagent)最近使用的模型
pin 在 pod 内 /data/.openclaw/agents/main/sessions/sessions.json 的 `model` 字段里。
历史遗留会 pin 到已下线的模型名(chatgpt-gpt-5.5 / openai/gpt-5.4 / claude-opus-4-6 …)。
当这些会话被再次触发:
  - 若 fallback 有下一跳 -> 自愈(成功模型写回 pin)
  - 若 next=none      -> surface_error reason=auth,用户看到报错且不自愈
所以要主动把 stale pin 归一到当前 litellm 白名单里的等价别名。

安全性(已实测确认,2026-08-13)
------------------------------
sessions.json 是活进程每 ~15s 改写的文件。实测:在磁盘上编辑一个 **休眠** 会话的
model 字段后,跨多个进程 flush 周期(mtime 前进多次)编辑均存活 —— 进程改写会把磁盘
内容并入内存,不会用旧内存副本回冲。因此就地编辑是安全的。改动幂等:已是白名单值的
pin 会被跳过,可反复运行到收敛。

归一策略(class-aware)
----------------------
按模型 "类" 归到当前别名,绝不跨类(opus 会话不会被改成 gpt):
  opus*         -> litellm/claude-opus-4-8
  sonnet/haiku* -> litellm/claude-sonnet-5
  gemini*       -> litellm/gemini-3.5-flash
  glm*          -> litellm/glm-5
  deepseek*pro  -> litellm/deepseek-v4-pro
  deepseek*     -> litellm/deepseek-v4-flash
  *sol*         -> litellm/gpt-5.6-sol
  *luna*        -> litellm/gpt-5.6-luna
  *5.5*         -> litellm/gpt-5.5
  其余 gpt/未知 -> litellm/gpt-5.6-terra   (默认)

用法
----
  python3 scripts/carher_session_pin_normalize.py              # 只读检查(默认)
  python3 scripts/carher_session_pin_normalize.py --apply      # 修复(带备份+收敛循环)
  python3 scripts/carher_session_pin_normalize.py --apply --max-passes 8
  python3 scripts/carher_session_pin_normalize.py --verify     # 修完只读复查
  python3 scripts/carher_session_pin_normalize.py --uids 71,333 --apply   # 只作用于指定实例

备份:每次写入前 shutil.copy 到同目录 sessions.json.pinfix.<UTC时间戳>,可回滚。
在能直连集群(如构建机 226)上运行;需要 kubectl 指向 carher context。
"""
import argparse
import concurrent.futures
import re
import subprocess
import sys

CTX = "203299974580141085-c215e116fb0a7414287f4be1c31bb4ebc"
NS = "carher"
SESS_PATH = "/data/.openclaw/agents/main/sessions/sessions.json"

# 当前 litellm 白名单里的合法 pin 值(不带前缀的 10 模型别名)
GOOD = {
    "litellm/gpt-5.6-terra", "litellm/gpt-5.5", "litellm/gpt-5.6-sol", "litellm/gpt-5.6-luna",
    "litellm/claude-opus-4-8", "litellm/claude-sonnet-5", "litellm/gemini-3.5-flash",
    "litellm/glm-5", "litellm/deepseek-v4-flash", "litellm/deepseek-v4-pro",
}


def kx(args, timeout=60):
    try:
        return subprocess.run(
            ["kubectl", "--context", CTX, "-n", NS] + args,
            capture_output=True, text=True, timeout=timeout,
        ).stdout
    except Exception:
        return ""


def list_pods(uids=None):
    pods = []
    for line in kx(["get", "pod", "--no-headers"]).splitlines():
        if not re.match(r"^carher-\d+-", line) or "Running" not in line:
            continue
        name = line.split()[0]
        uid = name.split("-")[1]
        if uids is None or uid in uids:
            pods.append(name)
    return pods


# ---- 在 pod 内执行的程序(check / apply 共用,APPLY 占位符切换行为) ----
POD_PROG = r'''
import json, os, sys, time, shutil
from collections import Counter
GOOD = set(%r)
APPLY = %s
P = "%s"

def norm(m):
    s = m.lower()
    if "opus" in s: return "litellm/claude-opus-4-8"
    if "sonnet" in s or "haiku" in s: return "litellm/claude-sonnet-5"
    if "gemini" in s: return "litellm/gemini-3.5-flash"
    if "glm" in s: return "litellm/glm-5"
    if "deepseek" in s: return "litellm/deepseek-v4-pro" if "pro" in s else "litellm/deepseek-v4-flash"
    if "sol" in s: return "litellm/gpt-5.6-sol"
    if "luna" in s: return "litellm/gpt-5.6-luna"
    if "5.5" in s or "5-5" in s: return "litellm/gpt-5.5"
    return "litellm/gpt-5.6-terra"

try:
    d = json.load(open(P))
except Exception:
    print("NOFILE"); sys.exit()

changes = []
def walk(o):
    if isinstance(o, dict):
        m = o.get("model")
        if isinstance(m, str) and m and m not in GOOD:
            nw = norm(m); changes.append((m, nw))
            if APPLY: o["model"] = nw
        for v in o.values(): walk(v)
    elif isinstance(o, list):
        for v in o: walk(v)
walk(d)

if not changes:
    print("CLEAN"); sys.exit()

if APPLY:
    bak = P + ".pinfix." + time.strftime("%%Y%%m%%dT%%H%%M%%SZ", time.gmtime())
    shutil.copy(P, bak)
    json.dump(d, open(P, "w"))
    print("APPLIED %%d bak=%%s" %% (len(changes), os.path.basename(bak)))
else:
    c = Counter("%%s=>%%s" %% (a, b) for a, b in changes)
    print("DRY %%d | %%s" %% (len(changes),
          "; ".join("%%s x%%d" %% (k, v) for k, v in c.most_common())))
'''


def build_prog(apply):
    return POD_PROG % (tuple(GOOD), "True" if apply else "False", SESS_PATH)


def run_on_pod(pod, prog, timeout=60):
    out = kx(["exec", pod, "-c", "carher", "--", "python3", "-c", prog], timeout=timeout)
    return pod, out.strip()


def sweep(pods, apply, workers=20, timeout=60):
    """返回 {pod: out}. 只对成功返回的 pod 有 key;超时/空的 pod 不在 dict 里。"""
    prog = build_prog(apply)
    res = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for pod, out in ex.map(lambda p: run_on_pod(p, prog, timeout), pods):
            res[pod] = out
    return res


def cross_class_flags(out):
    """检出 non-gpt 源被错误映射到 gpt 目标(不该发生)。"""
    flags = []
    body = out.split("|", 1)[-1] if "|" in out else ""
    for seg in body.split(";"):
        m = re.match(r"\s*(\S+)=>(\S+)", seg)
        if not m:
            continue
        src, dst = m.group(1).lower(), m.group(2)
        if any(k in src for k in ("opus", "sonnet", "haiku", "gemini", "glm", "deepseek")) and "gpt" in dst:
            flags.append(seg.strip())
    return flags


def cmd_check(pods):
    res = sweep(pods, apply=False)
    dirty, timed_out = [], []
    total_changes = 0
    for pod in pods:
        out = res.get(pod, "")
        uid = pod.split("-")[1]
        if out in ("", ):
            timed_out.append(uid); continue
        if out in ("CLEAN", "NOFILE"):
            continue
        dirty.append((uid, out))
        m = re.match(r"DRY (\d+)", out)
        if m:
            total_changes += int(m.group(1))
        cc = cross_class_flags(out)
        marker = "  !!CROSS-CLASS: " + ", ".join(cc) if cc else ""
        print("carher-%-5s %s%s" % (uid, out, marker))
    print("=== scanned=%d  dirty=%d  stale_pins=%d  timed_out=%d %s" %
          (len(pods), len(dirty), total_changes, len(timed_out),
           ("(" + ",".join(timed_out) + ")") if timed_out else ""))
    return dirty


def cmd_apply(pods, max_passes):
    """收敛循环:反复 apply 直到某一轮无 pod 被改动(或达 max_passes)。"""
    remaining = list(pods)
    grand_total = 0
    for p in range(1, max_passes + 1):
        res = sweep(remaining, apply=True)
        applied = []
        for pod in remaining:
            out = res.get(pod, "")
            if out.startswith("APPLIED"):
                uid = pod.split("-")[1]
                n = int(re.match(r"APPLIED (\d+)", out).group(1))
                grand_total += n
                applied.append(pod)
                print("  [pass %d] carher-%-5s %s" % (p, uid, out))
        print("=== pass %d: pods_touched=%d" % (p, len(applied)))
        if not applied:
            print("=== converged after pass %d" % p)
            break
    else:
        print("=== reached max-passes=%d (may need one more run)" % max_passes)
    print("=== TOTAL pins normalized: %d" % grand_total)


def main():
    ap = argparse.ArgumentParser(description="Normalize stale model pins in carher sessions.json")
    ap.add_argument("--apply", action="store_true", help="write fixes (default: read-only check)")
    ap.add_argument("--verify", action="store_true", help="read-only re-check (alias of default check)")
    ap.add_argument("--uids", default="", help="comma-separated instance ids, e.g. 71,333 (default: all)")
    ap.add_argument("--max-passes", type=int, default=8, help="convergence passes for --apply")
    args = ap.parse_args()

    uids = set(u.strip() for u in args.uids.split(",") if u.strip()) or None
    pods = list_pods(uids)
    if not pods:
        print("no running her pods matched"); sys.exit(1)
    print("=== target pods: %d ===" % len(pods))

    if args.apply:
        cmd_apply(pods, args.max_passes)
        print("\n=== post-apply verify ===")
        cmd_check(pods)
    else:
        cmd_check(pods)


if __name__ == "__main__":
    main()
