#!/usr/bin/env python3
"""muse 视频真并发验收：同时交 N 个文生视频，看它们是不是**同时**开跑。在 198 上跑。

    python3 video_concurrency.py [N]        # 默认 10

判据不是「都成功」而是「最早开跑 ≈ 最晚开跑」—— 串行时第 k 个要等前 k-1 个做完
（10-29 单号实测 5 个串行：90/221/301/371/491s）。
🔴 真并发上限 = 活号数 x MUSE2API_PER_ACCOUNT_SLOTS，且 MUSE2API_CONCURRENCY 要 >= 它；
   三个数对不对用 `pool_status.py` 看。每个任务最长等 600s（MUSE2API_VIDEO_TIMEOUT）。
🔴 这会真的烧额度（每号每周有限），别当健康检查反复跑。
"""
import sys
import json, time, subprocess, urllib.request
key = subprocess.check_output("sudo -n k3s kubectl -n muse2api get secret muse2api-env -o jsonpath={.data.MUSE2API_KEY} | base64 -d", shell=True).decode().strip()
B = "http://127.0.0.1:30610"; H = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
def req(m, p, body=None, timeout=60):
    r = urllib.request.Request(B + p, data=json.dumps(body).encode() if body else None, headers=H, method=m)
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
prompts = ["a red kite over green hills", "rain on a window at night", "a cat stretching in sunlight",
           "fireworks over a river", "a windmill at dusk", "a paper boat on a lake",
           "a balloon rising at sunrise", "snow on a quiet street", "a candle in a dark room",
           "waves on a sandy beach"]
prompts = [prompts[i % len(prompts)] + (" #%d" % (i // len(prompts) + 1) if i >= len(prompts) else "") for i in range(N)]
t0 = time.time(); tasks = [req("POST", "/v1/videos", {"model": "muse-video", "prompt": p})["id"] for p in prompts]
print("同时提交 %d 个，用时 %.1fs" % (len(tasks), time.time() - t0), flush=True)
started, done = {}, {}
while len(done) < len(tasks) and time.time() - t0 < 1500:
    time.sleep(10)
    for i, t in enumerate(tasks):
        if t in done: continue
        try: s = req("GET", "/v1/videos/" + t)
        except Exception: continue
        st = s.get("status")
        if st not in ("queued", "pending") and i not in started: started[i] = round(time.time() - t0)
        if st in ("completed", "failed", "error", "cancelled"):
            done[t] = st
            print("视频 %2d %s 第%ds完成（第%ss开跑）" % (i + 1, st, round(time.time() - t0), started.get(i)), flush=True)
ok = sum(1 for v in done.values() if v == "completed")
print("总用时 %ds  成功 %d/%d  最早开跑 %ss 最晚开跑 %ss" % (
    round(time.time() - t0), ok, len(tasks), min(started.values()) if started else "-", max(started.values()) if started else "-"), flush=True)
