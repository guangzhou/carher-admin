"""Bench the 5 self-built slot exits with the iGuge ruler: 连 Kiro = median of N HTTPS requests to the
Kiro API (404 = reachable); 下载 = median of 4 x 10MB downloads where the exit's allowlist permits one.

Run on 198 as cltx:  sudo python3 bench_self5.py > self5.tsv

Reads the real proxy passwords out of the slots PVC config.json (the Admin API masks them) and NEVER
prints them — no credential on a command line, no curl -v (the Proxy-Authorization header is the
password in base64).

🔴 The 4 aliyun squid exits allow ONLY the Kiro endpoints + api.ipify.org, so no 10MB source is
   reachable through them (probe_allow.py proved it: every candidate ERR:URLError). Their 下载 comes
   from the aliyun EIP bandwidth cap instead and must be labelled as a declared cap, not a measurement.
   Only jp-172.235.204.67 (via 243, not squid) can actually be downloaded through.
"""
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

K = ["sudo", "k3s", "kubectl", "-n", "kiro-rs"]
CFG = "/app/config/config.json"
KIRO_URL = "https://q.us-east-1.amazonaws.com/"
IP_URL = "https://api.ipify.org"
DOWN_URL = "https://cachefly.cachefly.net/10mb.test"
N_KIRO = 18
N_DOWN = 4

# aliyun EIP bandwidth caps (official console, screenshot 10-31) — declared ceiling, not measured
EIP_CAP = {"47.84.112.136": 40.0, "47.84.101.218": 20.0, "47.84.85.100": 5.0, "47.236.200.98": 1.0}


def slots_cfg():
    p = subprocess.run(K + ["get", "pod", "-l", "app=kiro-rs-slots",
                            "-o", "jsonpath={.items[0].metadata.name}"],
                       capture_output=True, text=True, check=True).stdout.strip()
    raw = subprocess.run(K + ["exec", p, "-c", "kiro-rs", "--", "cat", CFG],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(raw)


def proxy_url(s):
    u = s["proxyUrl"]
    if s.get("proxyUsername"):
        return "http://%s:%s@%s" % (s["proxyUsername"], s["proxyPassword"], u.split("//", 1)[1])
    return u


def opener(px):
    return urllib.request.build_opener(urllib.request.ProxyHandler({"https": px, "http": px}))


def hit(px, url, read=2000, timeout=40):
    o = opener(px)
    t = time.time()
    try:
        r = o.open(url, timeout=timeout)
        n = len(r.read(read)) if read else len(r.read())
        return r.status, n, time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, 0, time.time() - t
    except Exception as e:
        return "ERR:" + type(e).__name__, 0, time.time() - t


def main():
    cfg = slots_cfg()
    targets = [s for s in (cfg.get("slots") or []) if s.get("bench") is None and s.get("proxyUrl")]
    print("# slot\texit_ip\tkiro_median_s\tn_ok\tdown_mbps\tdown_source", flush=True)
    for s in targets:
        px = proxy_url(s)
        name = s["name"]
        ip = name.rsplit("-", 1)[-1]
        st, _, _ = hit(px, IP_URL)
        if st != 200:
            print("%s\t%s\tSTOP\t0\t\tipify=%s" % (name, ip, st), flush=True)
            continue
        real_ip = opener(px).open(IP_URL, timeout=30).read().decode().strip()
        secs = []
        for _ in range(N_KIRO):
            code, _, dt = hit(px, KIRO_URL)
            if code == 404:
                secs.append(dt)
        if not secs:
            print("%s\t%s\tSTOP\t0\t\tkiro unreachable" % (name, real_ip), flush=True)
            continue
        kmed = statistics.median(secs)
        if ip in EIP_CAP:
            down, src = EIP_CAP[ip], "aliyun-EIP-cap"
        else:
            mb = []
            for _ in range(N_DOWN):
                code, n, dt = hit(px, DOWN_URL, read=0, timeout=120)
                if code == 200 and n > 0 and dt > 0:
                    mb.append(n * 8 / dt / 1e6)
            down, src = (round(statistics.median(mb), 1), "measured-%dx10MB" % len(mb)) if mb else ("", "download blocked")
        print("%s\t%s\t%.2f\t%d\t%s\t%s" % (name, real_ip, kmed, len(secs), down, src), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
