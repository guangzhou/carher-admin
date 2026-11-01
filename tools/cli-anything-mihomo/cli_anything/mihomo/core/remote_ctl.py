"""Runs ON 198 (python3.10, stdlib only), shipped over ssh as `python3 -c`.

stdin: {"dir": "<instance dir>", "ctl": 19090, "jobs": [...], "workers": 16}
job kinds:
  {"kind": "api", "method": "GET", "path": "/proxies/%F0%9F...", "body": {...}|null, "timeout": 30}
  {"kind": "fetch", "port": 17890, "url": "https://...", "timeout": 20, "ua": "...", "max": 65536}
stdout: JSON list, one result per job in the same order.
The controller secret is read from <dir>/secret here and never leaves 198.
"""
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request

spec = json.load(sys.stdin)
with open(spec["dir"] + "/secret") as fh:
    SECRET = fh.read().strip()
BASE = "http://127.0.0.1:%d" % int(spec["ctl"])
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(job):
    data = None if job.get("body") is None else json.dumps(job["body"]).encode()
    req = urllib.request.Request(BASE + job["path"], data=data, method=job.get("method", "GET"),
                                 headers={"Authorization": "Bearer " + SECRET, "Content-Type": "application/json"})
    try:
        with NOPROXY.open(req, timeout=job.get("timeout", 30)) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception as e:  # connection refused, timeout
        return {"status": 0, "error": "%s: %s" % (type(e).__name__, e)}
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = raw.decode("utf-8", "replace")[:2000]
    return {"status": status, "body": body}


def fetch(job):
    p = "http://127.0.0.1:%d" % int(job["port"])
    op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
    req = urllib.request.Request(job["url"], headers={"User-Agent": job.get("ua") or "Mozilla/5.0"})
    t0 = time.time()
    try:
        with op.open(req, timeout=job.get("timeout", 20)) as r:
            raw, status = r.read(int(job.get("max", 65536))), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(int(job.get("max", 65536))), e.code
    except Exception as e:
        return {"status": 0, "error": "%s: %s" % (type(e).__name__, e), "ms": int(1000 * (time.time() - t0))}
    return {"status": status, "ms": int(1000 * (time.time() - t0)), "text": raw.decode("utf-8", "replace")}



def one(job):
    return api(job) if job["kind"] == "api" else fetch(job)


with concurrent.futures.ThreadPoolExecutor(max_workers=int(spec.get("workers", 16))) as ex:
    out = list(ex.map(one, spec["jobs"]))
json.dump(out, sys.stdout, ensure_ascii=False)
