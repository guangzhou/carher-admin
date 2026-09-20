#!/usr/bin/env python3
"""GPT 变慢的分层归因量具：同一时间窗里跑 A/B 两条腿，拆「上游慢」vs「我们慢」。

腿 A(leaf-direct) = pool key 打 svc/chatgpt-acct-<n>:4000/v1/responses
    **绕开母 router / 排队 / 池子 / WA / 重试 / cooldown**，量的是「叶子 + 上游」。
腿 B(via-mother)  = master key 打 127.0.0.1:30402/v1/responses, model=<group>
    量的是全链。

判据：
    A ≈ B  ⇒ 慢在上游（我们无处可逃，别改路由）
    A ≪ B  ⇒ 慢在母 router 层（排队 / retry / cooldown 重挑）
    **A 自己就很慢 ⇒ 直接坐实上游**，这是最有力的一腿：
    20 token 的 prompt、只要 16 token 输出，绕开一切，还要十几秒，赖不到我们头上。

⚠️ 纪律（每条都是踩出来的）：
  1. A 腿必须用 pool key（secret chatgpt-pool-master-key），母 router 的 key 会 400
     "No connected db"。
  2. 必须打 /v1/responses（生产路径），不是 /v1/chat/completions。
  3. 每发唯一 nonce，绕响应缓存。input 必须是 list + input_text。
  4. A/B 交错在一个线程池里发，才吃得到同一个时间窗。**永不跨窗口相减。**
  5. **n<12 不下结论**：上游是双峰的，实测同一批叶子同一秒能给出 1.2s 和 17.4s。
     n=3 时方差完全盖过效应，我在 09-09 就被 n=3 骗过一次。
  6. 只发推理请求，READ-ONLY，但**会花掉被测账号的额度**，别乱开大 --pad-tokens。

在 198 上跑（脚本要先 scp 上去，因为要 sudo kubectl 读 secret）：
  python3 leaf-direct-probe.py --accts 150,139,167,152,154,172,138,153,145,147,156,136 \
      --n-leaf 12 --n-mother 0
想验「大上下文是不是更慢」：同时起两个进程，一个 --pad-tokens 0 一个 --pad-tokens 100000，
同一秒启动 = 同窗口，不用改脚本。
"""
import argparse
import base64
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

NS = "litellm-product"
KUBECTL = ["sudo", "k3s", "kubectl"]


def sh(argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode:
        sys.exit("FATAL " + r.stderr)
    return r.stdout


def secret(name, field):
    v = sh(KUBECTL + ["-n", NS, "get", "secret", name, "-o", "jsonpath={.data.%s}" % field])
    return base64.b64decode(v).decode().strip()


def timed_post(url, key, body, timeout=180):
    """返回 (ttft_s, total_s, verdict, detail)。"""
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    t0 = time.time()
    ttft = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as x:
            first = x.read(1)          # 首字节 = 上游开口说话的时刻
            if first:
                ttft = time.time() - t0
            x.read()                   # 读到底才拿得到 total
        return ttft, time.time() - t0, "OK", ""
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf8", "replace")
        except Exception:
            pass
        return ttft, time.time() - t0, "HTTP%s" % e.code, " ".join(raw.split())[:200]
    except Exception as e:
        return ttft, time.time() - t0, type(e).__name__, str(e)[:200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="gpt-5.6-sol", help="逗号分隔可给多个组做对照")
    ap.add_argument("--accts", default="", help="逗号分隔;空=自动取前 N 个 svc")
    ap.add_argument("--n-leaf", type=int, default=12)
    ap.add_argument("--n-mother", type=int, default=6)
    ap.add_argument("--model-hint", default="5.6-sol")
    ap.add_argument("--pad-tokens", type=int, default=0,
                    help="给 prompt 塞 padding 近似生产的大上下文(1 token≈4 char)。会花额度。")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    pool_key = secret("chatgpt-pool-master-key", "LITELLM_MASTER_KEY")
    master_key = secret("litellm-secrets", "LITELLM_MASTER_KEY")
    if not pool_key or not master_key:
        sys.exit("FATAL: key 空")

    svc_ip = {}
    for s in json.loads(sh(KUBECTL + ["-n", NS, "get", "svc", "-o", "json"]))["items"]:
        m = re.fullmatch(r"chatgpt-acct-(\d+)", s["metadata"]["name"])
        if m:
            svc_ip[int(m.group(1))] = s["spec"]["clusterIP"]

    accts = [int(x) for x in a.accts.split(",") if x.strip()] if a.accts else sorted(svc_ip)[:a.n_leaf]
    accts = [n for n in accts if n in svc_ip]
    if not accts:
        sys.exit("FATAL: 一个 acct svc 都没解析到")

    pad = (("lorem ipsum dolor sit amet consectetur " * ((a.pad_tokens * 4) // 39 + 1))[:a.pad_tokens * 4]
           if a.pad_tokens else "")

    def mk_body(model, nonce, stream=True):
        text = "reply with the single word ok. probe id %s" % nonce
        if pad:
            text = pad + "\n\n" + text
        return {"model": model,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
                "max_output_tokens": 16, "stream": stream}

    def leaf_models(n):
        r = urllib.request.Request("http://%s:4000/v1/models" % svc_ip[n],
                                   headers={"Authorization": "Bearer " + pool_key})
        with urllib.request.urlopen(r, timeout=20) as x:
            return [m["id"] for m in json.loads(x.read())["data"]]

    def job_leaf(n):
        try:
            models = leaf_models(n)
        except Exception as e:
            return ("A-leaf", "acct-%d" % n, None, None, "MODELS", str(e)[:120])
        pick = next((m for m in models if a.model_hint in m), None)
        if not pick:
            return ("A-leaf", "acct-%d" % n, None, None, "NOMODEL", ",".join(models[:6]))
        nonce = "%d-%.6f" % (n, time.time())
        ttft, tot, v, d = timed_post("http://%s:4000/v1/responses" % svc_ip[n],
                                     pool_key, mk_body(pick, nonce))
        return ("A-leaf", "acct-%d" % n, ttft, tot, v, d)

    groups = [g.strip() for g in a.group.split(",") if g.strip()]

    def job_mother(spec):
        i, g = spec
        nonce = "m%d-%.6f" % (i, time.time())
        ttft, tot, v, d = timed_post("http://127.0.0.1:30402/v1/responses",
                                     master_key, mk_body(g, nonce))
        return ("B:" + g, g, ttft, tot, v, d)

    jobs = []
    for i in range(max(len(accts), a.n_mother)):
        if i < len(accts):
            jobs.append(("A", accts[i]))
        if i < a.n_mother:
            for g in groups:
                jobs.append(("B", (i, g)))

    print("# window start %s UTC  group=%s pad_tokens=%d"
          % (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), a.group, a.pad_tokens))
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        rows = list(ex.map(lambda j: job_leaf(j[1]) if j[0] == "A" else job_mother(j[1]), jobs))

    hdr = "%-24s %-14s %8s %8s %-9s %s"
    print(hdr % ("LEG", "TARGET", "TTFT_s", "TOTAL_s", "VERDICT", "DETAIL"))
    for leg, tgt, ttft, tot, v, d in rows:
        print(hdr % (leg, tgt, "-" if ttft is None else "%.1f" % ttft,
                     "-" if tot is None else "%.1f" % tot, v, d[:90]))

    print()
    for leg in ["A-leaf"] + ["B:" + g for g in groups]:
        ok = [r for r in rows if r[0] == leg and r[4] == "OK" and r[3] is not None]
        if not ok:
            print("%-24s n=0 (全非 OK，不许拿它当读数)" % leg)
            continue
        tt = sorted(r[3] for r in ok)
        slow = sum(1 for x in tt if x >= 5)
        print("%-24s n=%-3d total med=%.1fs min=%.1fs max=%.1fs  ≥5s 的占 %d/%d"
              % (leg, len(ok), statistics.median(tt), tt[0], tt[-1], slow, len(tt)))

    print("\n判据: A≈B ⇒ 上游慢; A≪B ⇒ 母 router 层慢; A 自己就慢 ⇒ 上游，铁证。")
    print("      **n<12 不下结论**——上游是双峰的，小样本必被方差骗。")
    print("      基线(2026-09-09 上游劣化期, pad=0, n=12): med 5.7s / max 17.4s，6 快 6 慢。")
    print("      基线(同日更早 08:29, n=6): med 2.1s。健康时应当是 1~3s。")


if __name__ == "__main__":
    main()
