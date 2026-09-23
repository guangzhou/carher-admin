#!/usr/bin/env python3
"""扫 198 acct 池：某个 codex slug 此刻在哪些号上真能调，产出建组用的 targets 文件。

为什么需要这把尺子（都是 2026-09-23 实测踩出来的）：

- **目录声明不是事实。** `/backend-api/codex/models` 里 `visibility: list` +
  `supported_in_api: true` 的 slug（`gpt-6-sol`）直打照样 404，且**同一天内翻了三次面**
  （10:24 三个号 200 → 10:40 全 404 → 15:15 全池 0 个 404）。
  ⇒ 「哪几个号有」是**时刻快照，不是账号属性**，禁缓存到下一轮。
- **没有阳性对照，acct 的 400 会被读成「模型还没放开」。** acct-85 在目标 slug 上返 400,
  在阳性对照 `gpt-5.6-sol` 上**同样** 400 ⇒ 是这个号（free 档）不是这个模型。
  少了对照那一列，它会把整轮结论带偏。
- **没有阴性对照，整张表的绿都不可信。** 编造名必须 400；它要是也"通"了，
  说明尺子没有分辨力，读数作废。
- **`200 空流` 是瞬态节流，不是不可用。** HTTP 200 但空 SSE + `usage=None`,
  阳性对照同频率中招，复打即回显 ⇒ 必须复打一次再定性。

⛔ 本脚本**只读，绝不重启任何 acct pod**。重启在服务的号可能永久打死它且回退救不回
   （acct-237 前车之鉴）。slug 不在 pod 的 `/app/config.yaml` 里是 CM 那一步没做完，
   去做 CM + 受控 rollout，不要让这把尺子顺手帮你重启。

用法：
    # 默认 dry 扫描，打印分布，不写文件
    python3 chatgpt-pool-codex-slug-survey.py --slug gpt-6-sol
    # 产出 targets（只含目标 slug 实测可用的号）
    python3 chatgpt-pool-codex-slug-survey.py --slug gpt-6-sol --out ~/sol-targets.txt

退出码：0 = 尺子有效且有可用号；3 = 尺子失效（对照不成立）；4 = 尺子有效但 0 个可用号。
"""
import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import time

NS_DEFAULT = "litellm-product"

# 在 acct pod 内跑的探针：用该号自己的 auth.json 直打 codex 后端，绕开 litellm 聚合层、
# 绕开 alias、绕开 fallback。唯一 nonce 回显才算活。
PROBE_SRC = r'''
import json, urllib.request, urllib.error, sys, time
d = json.load(open("/chatgpt-auth/auth.json"))
h = {"Authorization": "Bearer " + d["access_token"], "Content-Type": "application/json",
     "User-Agent": "codex_cli_rs/1.0.0", "chatgpt-account-id": d.get("account_id") or "",
     "OpenAI-Beta": "responses=experimental",
     "session_id": "11111111-2222-3333-4444-555555555555",
     "Accept": "text/event-stream"}
model, nonce = sys.argv[1], sys.argv[2]
body = {"model": model, "instructions": "You are a terse assistant.",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text",
                                "text": "Reply with exactly this token and nothing else: " + nonce}]}],
        "stream": True, "store": False,
        # 🔴 推理模型必须给足预算：小 max_tokens 会被 reasoning 吃光，
        #    返 200 + 空 content，形状和"不可用"一模一样。这里不设上限。
        "reasoning": {"effort": "low", "summary": "auto"}}
req = urllib.request.Request("https://chatgpt.com/backend-api/codex/responses",
                             data=json.dumps(body).encode(), headers=h)
t = time.time()
try:
    r = urllib.request.urlopen(req, timeout=120)
except urllib.error.HTTPError as e:
    print("RESULT\t%s\tHTTP%d\t\t%s" % (model, e.code, e.read()[:200].decode(errors="replace").replace("\t", " ").replace("\n", " ")))
    raise SystemExit(0)
except Exception as e:
    print("RESULT\t%s\tEXC\t\t%s" % (model, str(e)[:160].replace("\t", " ")))
    raise SystemExit(0)
txt, usage = [], None
for raw in r:
    line = raw.decode(errors="replace").strip()
    if not line.startswith("data:"):
        continue
    p = line[5:].strip()
    if p == "[DONE]":
        break
    try:
        ev = json.loads(p)
    except Exception:
        continue
    if ev.get("type") == "response.output_text.delta":
        txt.append(ev.get("delta", ""))
    if ev.get("type") == "response.completed":
        usage = ev["response"].get("usage")
out = "".join(txt)
print("RESULT\t%s\t200\t%.1f\t%s|usage=%s" % (model, time.time() - t, out[:60].replace("\t", " "), bool(usage)))
'''

# 🔴 只认行首锚点。kubectl 的 websocket 警告会混进 stdout，2026-09-23 就把一条警告
#    读成了「编造名返 200」。用行首 RESULT\t 做锚点，别用 in / 子串。
RESULT_RE = re.compile(r"^RESULT\t([^\t]*)\t([^\t]*)\t([^\t]*)\t(.*)$")


def sh(cmd, timeout=180):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def kubectl(args, ns, sudo=True):
    return ("sudo -n kubectl " if sudo else "kubectl ") + "-n %s %s" % (ns, args)


def running_accts(ns, sudo):
    """只取 Running 且 Ready 的 acct pod。replicas>0 ≠ 在服务，不能拿 deploy 数当池子大小。"""
    rc, out, err = sh(kubectl(
        "get pods -l pool=chatgpt-acct "
        "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\t\"}{.status.phase}{\"\\t\"}"
        "{.status.containerStatuses[0].ready}{\"\\n\"}{end}'", ns, sudo))
    if rc != 0:
        sys.exit("列 acct pod 失败: %s" % err[:300])
    pods = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        name, phase, ready = parts[0], parts[1], parts[2]
        if phase == "Running" and ready == "true":
            m = re.match(r"^chatgpt-acct-(\d+)-", name)
            if m:
                pods.append((m.group(1), name))
    return sorted(set(pods), key=lambda x: int(x[0]))


def classify(status, body):
    """把一次直打的结果归成一个桶。两种 4xx 语义不同，禁合并。"""
    if status == "200":
        # 有回显 = 活；空流 = 瞬态节流，调用方要复打
        return "OK" if body.split("|")[0].strip() else "EMPTY200"
    if status == "HTTP404":
        return "404"          # model_not_found —— 付费号拿不到这个名
    if status == "HTTP400":
        return "400"          # 「not supported when using Codex with a ChatGPT account」
    if status == "HTTP401":
        return "401"          # token 死，号的问题
    if status == "HTTP429":
        return "429"
    return "OTHER"


def probe_pod(acct, pod, ns, sudo, models, nonce, remote_path):
    rc, _, err = sh(kubectl("cp %s %s:%s" % (remote_path, pod, remote_path), ns, sudo), timeout=90)
    if rc != 0:
        return acct, {m: ("CPFAIL", err[:80]) for m in models}
    res = {}
    for m in models:
        rc, out, err = sh(kubectl("exec %s -- python3 %s %s %s" % (pod, remote_path, m, nonce),
                                  ns, sudo), timeout=200)
        got = None
        for line in out.splitlines():
            mm = RESULT_RE.match(line)          # 行首锚点，不用子串
            if mm and mm.group(1) == m:
                got = (classify(mm.group(2), mm.group(4)), mm.group(4)[:60])
        res[m] = got or ("NOPARSE", (err or out)[:80].replace("\n", " "))
    return acct, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True, help="要测的目标 slug，如 gpt-6-sol")
    ap.add_argument("--control", default="gpt-5.6-sol",
                    help="阳性对照：一个此刻确定能用的 slug（默认 gpt-5.6-sol）")
    ap.add_argument("--negative", default=None,
                    help="阴性对照：编造名，必须 400（默认 <slug>-nope-<ts>）")
    ap.add_argument("--ns", default=NS_DEFAULT)
    ap.add_argument("--nonce", default=None, help="回显暗号；默认按时刻生成（禁复用旧 nonce）")
    ap.add_argument("--out", default=None, help="把可用的号写进这个文件（建组脚本的 --targets）")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--no-sudo", action="store_true")
    ap.add_argument("--remote-path", default="/tmp/codex-slug-probe.py",
                    help="探针在 pod 内的落点")
    ap.add_argument("--local-path", default=None, help="本机探针路径（默认写到 remote-path 同名）")
    ap.add_argument("--min-control-ok", type=float, default=0.5,
                    help="阳性对照至少这个比例要 OK/EMPTY200，否则判尺子失效")
    args = ap.parse_args()

    sudo = not args.no_sudo
    # 🔴 nonce 必须每轮新生成。复用上一轮的 nonce 会让尺子分不出本轮和上轮，
    #    2026-09-23 就因为 sed 没替换到 nonce 常量，探针拿旧 nonce 跑了一轮。
    nonce = args.nonce or "SV%d" % (int(time.time()) % 10 ** 8)
    negative = args.negative or "%s-nope-%d" % (args.slug, int(time.time()) % 10000)
    models = [args.slug, args.control, negative]

    local = args.local_path or args.remote_path
    with open(local, "w") as f:
        f.write(PROBE_SRC)

    pods = running_accts(args.ns, sudo)
    print("== survey slug=%s control=%s negative=%s nonce=%s" % (args.slug, args.control, negative, nonce))
    print("== running+ready acct pods: %d" % len(pods))
    if not pods:
        sys.exit("没有 Running+Ready 的 acct pod —— 先查池子，别往下建组")

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = [ex.submit(probe_pod, a, p, args.ns, sudo, models, nonce, args.remote_path)
                for a, p in pods]
        for fu in concurrent.futures.as_completed(futs):
            acct, res = fu.result()
            results[acct] = res

    # 200 空流复打一次（瞬态节流；不复打会把它算成不可用）
    retry = [a for a, r in results.items() if r.get(args.slug, ("",))[0] == "EMPTY200"]
    if retry:
        print("== EMPTY200 复打 %d 个（瞬态节流，不复打会误判）" % len(retry))
        pod_of = dict(pods)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = [ex.submit(probe_pod, a, pod_of[a], args.ns, sudo, [args.slug],
                              nonce + "R", args.remote_path) for a in retry]
            for fu in concurrent.futures.as_completed(futs):
                acct, res = fu.result()
                results[acct][args.slug] = res[args.slug]

    def dist(m):
        d = {}
        for r in results.values():
            bucket = r.get(m, ("MISSING", ""))[0]
            d[bucket] = d.get(bucket, 0) + 1
        return d

    print("\n%-28s %s" % ("model", "分布"))
    for m in models:
        tag = {args.slug: "(目标)", args.control: "(阳性对照)", negative: "(阴性对照)"}[m]
        print("%-28s %-12s %s" % (m, tag, json.dumps(dist(m), sort_keys=True)))

    # ---- 门禁：尺子自证有分辨力，放在建组之前 ----
    neg = dist(negative)
    n = len(results)
    neg_rejected = neg.get("400", 0) + neg.get("404", 0)
    if neg_rejected < n:
        print("\n🔴 尺子失效：阴性对照 %s 有 %d/%d 个号没被拒（编造名居然通了）⇒ 本轮读数作废"
              % (negative, n - neg_rejected, n))
        return 3
    ctl = dist(args.control)
    ctl_ok = ctl.get("OK", 0) + ctl.get("EMPTY200", 0)
    if ctl_ok < args.min_control_ok * n:
        print("\n🔴 尺子失效：阳性对照 %s 只有 %d/%d 可用 ⇒ 坏的是池子/尺子，不是 %s。"
              "别据此判目标 slug 不可用。" % (args.control, ctl_ok, n, args.slug))
        return 3

    ok = sorted([a for a, r in results.items() if r[args.slug][0] == "OK"], key=int)
    # 目标失败但阳性对照成功 = 真的是这个模型在这个号上没有
    model_gap = sorted([a for a, r in results.items()
                        if r[args.slug][0] != "OK" and r[args.control][0] in ("OK", "EMPTY200")], key=int)
    # 两个都失败 = 号的问题（free 档 / token 死），不是模型的问题
    acct_bad = sorted([a for a, r in results.items()
                       if r[args.slug][0] != "OK" and r[args.control][0] not in ("OK", "EMPTY200")], key=int)

    print("\n== 三桶（这一分类就是 acct-85 那一课）")
    print("可用            %3d  %s" % (len(ok), ",".join(ok)))
    print("模型缺口        %3d  %s  <- 目标不行、对照行 ⇒ 是这个 slug 在这些号上没有"
          % (len(model_gap), ",".join(model_gap)))
    print("号本身的问题    %3d  %s  <- 两个都不行 ⇒ 是号（free 档/token 死），别记成模型问题"
          % (len(acct_bad), ",".join(acct_bad)))
    print("\n⏱  结论只对 %s 这一刻成立。可用性是时间函数不是账号属性，禁缓存到下一轮。"
          % time.strftime("%Y-%m-%d %H:%M:%S"))

    if not ok:
        print("\n🔴 0 个可用 ⇒ 不要建组。建了就是对用户稳定返 404。")
        return 4
    if args.out:
        with open(args.out, "w") as f:
            f.write("\n".join(ok) + "\n")
        print("\ntargets -> %s (%d 个)" % (args.out, len(ok)))
    else:
        print("\n（没给 --out，没写文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
