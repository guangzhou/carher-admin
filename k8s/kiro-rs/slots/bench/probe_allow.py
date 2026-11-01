"""Step 1 of benching the 5 self-built exits: find which URLs their squid allowlist permits.

Run on 198 as cltx:  sudo python3 probe_allow.py
Reads the REAL proxy passwords out of the slots PVC config.json (the Admin API masks them as ******)
and never prints them. Prints one line per (slot, candidate URL): status + seconds.

🔴 Do not pass credentials on a curl command line and do not use curl -v here: the Proxy-Authorization
   header is base64 of the password and would land in the transcript.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

POD_NS = ["sudo", "k3s", "kubectl", "-n", "kiro-rs"]
CFG = "/app/config/config.json"

CANDIDATES = [
    "https://q.us-east-1.amazonaws.com/",          # kiro api (the latency ruler)
    "https://prod.us-east-1.auth.desktop.kiro.dev/",
    "https://api.ipify.org",
    "https://ipinfo.io/ip",
    "https://speed.cloudflare.com/__down?bytes=10485760",
    "https://cachefly.cachefly.net/10mb.test",
    "https://codeload.github.com/git/git/tar.gz/refs/tags/v2.47.0",
    "https://registry.npmjs.org/typescript/-/typescript-5.6.3.tgz",
    "https://files.pythonhosted.org/packages/source/p/pip/pip-24.2.tar.gz",
]


def pod():
    out = subprocess.run(POD_NS + ["get", "pod", "-l", "app=kiro-rs-slots",
                                   "-o", "jsonpath={.items[0].metadata.name}"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def slots():
    p = pod()
    out = subprocess.run(POD_NS + ["exec", p, "-c", "kiro-rs", "--", "cat", CFG],
                         capture_output=True, text=True, check=True)
    cfg = json.loads(out.stdout)
    return [s for s in cfg.get("slots") or [] if s.get("bench") is None and s.get("proxyUrl")]


def proxy_url(s):
    u = s["proxyUrl"]
    if s.get("proxyUsername"):
        host = u.split("//", 1)[1]
        u = "http://%s:%s@%s" % (s["proxyUsername"], s["proxyPassword"], host)
    return u


def probe(px, url, read_bytes=4096, timeout=30):
    o = urllib.request.build_opener(urllib.request.ProxyHandler({"https": px, "http": px}))
    t = time.time()
    try:
        r = o.open(url, timeout=timeout)
        r.read(read_bytes)
        return str(r.status), round(time.time() - t, 2)
    except urllib.error.HTTPError as e:
        return str(e.code), round(time.time() - t, 2)
    except Exception as e:
        return "ERR:" + type(e).__name__, round(time.time() - t, 2)


def main():
    ss = slots()
    print("# slots without bench: %s" % ", ".join(s["name"] for s in ss))
    for s in ss:
        px = proxy_url(s)
        for url in CANDIDATES:
            st, dt = probe(px, url)
            print("%-22s %-62s %-18s %ss" % (s["name"], url[:60], st, dt), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
