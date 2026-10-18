#!/usr/bin/env python3
"""两条车道并排（默认 prod / gray）：top 组 × 3 协议（chat 流式 / responses 流式 / messages 流式），每发唯一 nonce。
判据：拼回来的文本里含 nonce 的算 OK（模型拒绝复述就看算术题）。master key 走环境变量 MK。
阴性对照：不存在的模型名必须 4xx。用法：MK=... sweep.py <prod_ip> <gray_ip> <groups_file>"""
import json, os, random, string, sys, time, urllib.request, urllib.error, concurrent.futures as cf
MK = os.environ["MK"]; PROD, GRAY, GF = sys.argv[1:4]
groups = [l.split("|")[0] for l in open(GF) if "|" in l and not l.startswith("SET")]
SKIP_EMBED = {"BAAI/bge-m3"}
def post(ip, path, body, timeout=180):
    req = urllib.request.Request(f"http://{ip}:4000{path}", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + MK, "Content-Type": "application/json", "anthropic-version": "2023-06-01"})
    t = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout); raw = r.read().decode(errors="replace"); code = r.status
    except urllib.error.HTTPError as e: raw = e.read().decode(errors="replace"); code = e.code
    except Exception as e: return 0, repr(e)[:120], time.time() - t
    return code, raw, time.time() - t
def text_of(raw):
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"): line = line[5:].strip()
        if not line or line == "[DONE]": continue
        try: o = json.loads(line)
        except Exception: continue
        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k in ("content", "text", "delta", "output_text") and isinstance(v, str): out.append(v)
                    else: walk(v)
            elif isinstance(x, list):
                for y in x: walk(y)
        walk(o)
    return "".join(out)
def one(ip, g, proto):
    a, b = random.randint(101, 999), random.randint(101, 999); nonce = "N" + "".join(random.choices(string.ascii_uppercase, k=8))
    prompt = f"[{nonce}] Compute {a}*{b}. Reply with only the number."
    want = str(a * b)
    if proto == "chat":
        c, raw, dt = post(ip, "/v1/chat/completions", {"model": g, "stream": True, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]})
    elif proto == "responses":
        c, raw, dt = post(ip, "/v1/responses", {"model": g, "stream": True, "max_output_tokens": 400, "input": prompt})
    else:
        c, raw, dt = post(ip, "/v1/messages", {"model": g, "stream": True, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]})
    txt = text_of(raw) if c == 200 else ""
    ok = c == 200 and want in txt.replace(",", "")
    err = "" if ok else (raw[:140].replace("\n", " ") if c != 200 else ("EMPTY" if not txt.strip() else "WRONG:" + txt[:60].replace("\n", " ")))
    return ok, c, round(dt, 1), err
def lane(ip, g, proto, tries=3):
    # 单边红重跑：最多 3 次，任一次 OK 即 OK（和手册「单边红先重跑 3 次再判」一致）
    last = None
    for _ in range(tries):
        last = one(ip, g, proto)
        if last[0]: return last
    return last
print("negative control:", "prod", post(PROD, "/v1/chat/completions", {"model": "no-such-model-xyz", "messages": [{"role": "user", "content": "x"}]})[0],
      "gray", post(GRAY, "/v1/chat/completions", {"model": "no-such-model-xyz", "messages": [{"role": "user", "content": "x"}]})[0], flush=True)
jobs = [(g, p) for g in groups if g not in SKIP_EMBED for p in ("chat", "responses", "messages")]
res = {}
with cf.ThreadPoolExecutor(8) as ex:
    fut = {ex.submit(lambda gp, ip: lane(ip, *gp), gp, ip): (gp, side) for gp in jobs for side, ip in (("prod", PROD), ("gray", GRAY))}
    for f in cf.as_completed(fut):
        (g, p), side = fut[f]; res[(g, p, side)] = f.result()
okp = okg = 0; mism = []
for g, p in jobs:
    a, b = res[(g, p, "prod")], res[(g, p, "gray")]
    okp += a[0]; okg += b[0]
    if a[0] != b[0] or (not a[0] and a[1] != b[1]): mism.append((g, p, a, b))
    print(f"{g:40s} {p:9s} prod={'OK' if a[0] else 'X '} {a[1]} {a[2]:>5}s  gray={'OK' if b[0] else 'X '} {b[1]} {b[2]:>5}s  {'' if a[0] and b[0] else ('P:'+a[3][:60]+' | G:'+b[3][:60])}")
print(f"\nTOTAL prod {okp}/{len(jobs)}  gray {okg}/{len(jobs)}  one-sided={len(mism)}")
for m in mism: print("  ONE-SIDED", m[0], m[1], "prod", m[2][:2], m[2][3][:100], "| gray", m[3][:2], m[3][3][:100])
