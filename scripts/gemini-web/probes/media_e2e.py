"""LiteLLM end-to-end for the gemini-media capabilities, run on 198.

Temp key = cursor-liuguoxian-std's models/aliases (+ the media names under test) and
allowed_passthrough_routes=/gemini-deepresearch, 40 min / $2, deleted in finally.

    MK=$(sudo kubectl -n litellm-product get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d) \
      python3 media_e2e.py image music video research

Each item burns real account quota (video has its own small daily cap) — run only what you need.
Pass criteria: image -> data[0] has url/b64; music -> audio/* body starting with ID3/fLaC/OggS;
video -> create 200 + poll reaches completed (quota_exceeded = path OK, quota empty);
research -> 200 with a report; plus negatives: private group name 403, bad key 401.
"""
import json
import os
import sys
import time
import urllib.request
import uuid

B = "http://127.0.0.1:30402"
MK = os.environ["MK"]
WHAT = set(sys.argv[1:] or ["image"])
MEDIA = {"gemini-image": "web-gemini-image", "gemini-music": "web-gemini-music", "gemini-video": "web-gemini-video"}


def req(method, path, key, body=None, multipart=None, timeout=900, raw=False):
    headers = {"Authorization": "Bearer " + key}
    data = None
    if multipart:
        bd = uuid.uuid4().hex
        data = ("".join(f'--{bd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in multipart.items())
                + f"--{bd}--\r\n").encode()
        headers["Content-Type"] = "multipart/form-data; boundary=" + bd
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(B + path, data=data, headers=headers, method=method)
    t = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            out = f.read()
            return f.status, (out if raw else json.loads(out)), round(time.time() - t, 1), f.headers.get("content-type")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300], round(time.time() - t, 1), None


src = json.load(urllib.request.urlopen(urllib.request.Request(
    B + "/key/list?return_full_object=true&size=1&key_alias=cursor-liuguoxian-std", headers={"Authorization": "Bearer " + MK})))["keys"][0]
# web-gemini-video must be in models too: GET /v1/videos/{id} re-resolves the private group name and checks it.
models = sorted(set(src["models"]) | set(MEDIA) | {"web-gemini-video"})
st, k, _, _ = req("POST", "/key/generate", MK, {
    "key_alias": "tmp-media-e2e-%d" % time.time(), "models": models, "aliases": dict(src.get("aliases") or {}, **MEDIA),
    "duration": "40m", "max_budget": 2.0, "metadata": {"allowed_passthrough_routes": ["/gemini-deepresearch"]}})
tk = k["key"]
try:
    if "image" in WHAT:
        st, r, dt, _ = req("POST", "/v1/images/generations", tk, {"model": "gemini-image", "prompt": "a red bicycle on a beach, photo", "n": 1})
        d = r.get("data", []) if isinstance(r, dict) else []
        print("image", st, dt, "items=%d" % len(d), list(d[0].keys()) if d else r, flush=True)
        st, *_ = req("POST", "/v1/images/generations", tk, {"model": "web-gemini-image", "prompt": "x"})
        print("image private-name (expect 403)", st, flush=True)
    if "music" in WHAT:
        st, r, dt, ct = req("POST", "/v1/audio/speech", tk, {"model": "gemini-music", "voice": "alloy",
                            "input": "An upbeat acoustic guitar tune about a sunny morning walk"}, raw=True, timeout=600)
        print("music", st, dt, ct, len(r) if isinstance(r, bytes) else r, r[:4] if isinstance(r, bytes) else "", flush=True)
    if "video" in WHAT:
        st, r, dt, _ = req("POST", "/v1/videos", tk, multipart={"model": "gemini-video", "prompt": "a paper boat on a calm lake", "seconds": "8"})
        print("video create", st, dt, (r.get("id", "")[:24], r.get("status")) if isinstance(r, dict) else r, flush=True)
        if isinstance(r, dict) and r.get("id"):
            s = None
            for _ in range(60):
                time.sleep(15)
                st, r2, _, _ = req("GET", f"/v1/videos/{r['id']}", tk)
                s = r2.get("status") if isinstance(r2, dict) else r2
                if s not in ("in_progress", "queued"):
                    print("video status", st, s, r2.get("error") if isinstance(r2, dict) else "", flush=True)
                    break
            if s == "completed":
                st, c, _, ct = req("GET", f"/v1/videos/{r['id']}/content", tk, raw=True)
                print("video content", st, ct, len(c) if isinstance(c, bytes) else c, flush=True)
    if "research" in WHAT:
        st, r, dt, _ = req("POST", "/gemini-deepresearch", tk, {"model": "gemini-3.1-pro", "query": "In one paragraph: what is the Model Context Protocol?"})
        print("deepresearch", st, dt, len(json.dumps(r)) if isinstance(r, dict) else r, flush=True)
        st, *_ = req("POST", "/gemini-deepresearch", "sk-not-a-real-key", {"query": "x"}, timeout=30)
        print("deepresearch bad key (expect 401)", st, flush=True)
finally:
    req("POST", "/key/delete", MK, {"keys": [tk]})
    print("tmp key deleted")
