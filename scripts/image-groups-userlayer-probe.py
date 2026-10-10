#!/usr/bin/env python3
"""用户层出图探针：克隆真 key 的白名单起临时 key，打组名 + 阴性对照。
判绿只认 b64 或 url 非空（见 feedback_image_probe_must_accept_url_not_only_b64）。
master key 绕过白名单，用它验等于没验用户那条路。"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

B = "http://10.68.13.198:30402/pro"
MK = os.environ["LITELLM_MASTER_KEY"]
PROMPT = "a small red cube on a white background"


def call(path, data=None, method="POST", key=None, timeout=300):
    req = urllib.request.Request(
        B + path,
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + (key or MK),
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400].decode(errors="replace")
    except Exception as e:
        return 0, "%s %s" % (type(e).__name__, e)


models = [m for m in sys.argv[1].split(",") if m]
targets = sys.argv[2].split(",")
print("allowlist n=%d  gpt-image-2=%s  image-2=%s"
      % (len(models), "gpt-image-2" in models, "image-2" in models))

s, k = call("/key/generate", {"models": models,
                              "key_alias": "tmp-imgprobe-%d" % time.time(),
                              "duration": "20m"})
if s != 200:
    raise SystemExit("key/generate failed %s %s" % (s, k))
tk = k["key"]
print("tmp key created")
try:
    for mdl in targets:
        t0 = time.time()
        s, r = call("/v1/images/generations",
                    {"model": mdl, "prompt": PROMPT, "n": 1}, key=tk)
        dt = time.time() - t0
        if s == 200 and isinstance(r, dict):
            d0 = (r.get("data") or [{}])[0]
            nb = len(d0.get("b64_json") or "")
            url = d0.get("url") or ""
            ok = nb > 1000 or bool(url)
            print("  %-28s %s 200 %5.1fs b64=%dB url=%s"
                  % (mdl, "OK " if ok else "BAD", dt, nb, "yes" if url else "no"))
        else:
            print("  %-28s FAIL H%s %5.1fs %s" % (mdl, s, dt, str(r)[:200]))
finally:
    call("/key/delete", {"keys": [tk]})
    print("tmp key deleted")
