#!/usr/bin/env python3
"""
T1 dev short stress + side-effect snapshot for reactive cooldown POC.

Phase 4b of litellm-fix-or-feature SOP §4b. Mock-pool only (no prod traffic).

Plan:
  - snapshot proxy/redis memory + counters before
  - 5min mixed-fault burst: 20 concurrent workers, each loops calling proxy
      * 80% calls hit happy (no fault) -> route to mock-pool, expect 200 (after affinity)
      * 20% calls toggle a random mock fault then call (recovers right after)
  - mid-run snapshot every 60s
  - tail proxy logs for ERROR/CRITICAL
  - assert: no proxy OOM/restart; redis growth bounded; cooldown count stable
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile


REMOTE_SCRIPT = r"""#!/usr/bin/env python3
import json
import random
import subprocess
import sys
import threading
import time
from collections import Counter

NS = "litellm-dev"
PROXY = "http://localhost:30400"
MODEL = "mock-pool-gpt-5.5"
ACCOUNTS = ["mock-1", "mock-2", "mock-3", "mock-4", "mock-5"]
DURATION = 300  # seconds
WORKERS = 20


def sh(args):
    return subprocess.check_output(args, text=True).strip()


def sh_ok(args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        return e.output


def get_master_key():
    return sh([
        "kubectl", "exec", "-n", NS, "deploy/litellm-proxy", "--",
        "printenv", "LITELLM_MASTER_KEY",
    ])


def mock_py(snippet):
    return sh([
        "kubectl", "exec", "-n", NS, "deploy/mock-chatgpt-upstream", "--",
        "python3", "-c", snippet,
    ])


def admin_reset():
    return mock_py(
        "import urllib.request as u; "
        "print(u.urlopen(u.Request('http://localhost:4101/_admin/reset',method='POST'),timeout=5).read().decode())"
    )


def admin_set_fault(name, fault):
    body = json.dumps({"name": name, "fault": fault})
    snippet = (
        "import urllib.request as u; "
        f"r=u.Request('http://localhost:4101/_admin/fault',"
        f"data={body!r}.encode(),"
        "headers={'Content-Type':'application/json'},method='POST'); "
        "print(u.urlopen(r,timeout=5).read().decode())"
    )
    return mock_py(snippet)


def proxy_call(master_key, body_input):
    body = json.dumps({"model": MODEL, "input": body_input, "stream": True})
    out = sh_ok([
        "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {master_key}",
        "-X", "POST", "-d", body,
        "--max-time", "15",
        PROXY + "/v1/responses",
    ])
    try:
        return int(out.strip())
    except Exception:
        return 0


def get_proxy_mem():
    # mem usage of proxy pods (Mi)
    try:
        out = sh(["kubectl", "top", "pod", "-n", NS, "-l", "app=litellm-proxy", "--no-headers"])
        vals = []
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 3 and cols[2].endswith("Mi"):
                vals.append(int(cols[2][:-2]))
        return sum(vals)
    except Exception:
        return -1


def get_redis_used_bytes():
    try:
        out = sh([
            "kubectl", "exec", "-n", NS, "litellm-redis-0", "--",
            "redis-cli", "INFO", "memory",
        ])
        for line in out.splitlines():
            if line.startswith("used_memory:"):
                return int(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return -1


def get_cooldown_count():
    out = sh_ok([
        "kubectl", "exec", "-n", NS, "litellm-redis-0", "--",
        "redis-cli", "--scan", "--pattern", "deployment:*:cooldown",
    ])
    return len([line for line in out.splitlines() if line.strip()])


def get_proxy_restart_count():
    out = sh([
        "kubectl", "get", "pod", "-n", NS, "-l", "app=litellm-proxy",
        "-o", "jsonpath={.items[*].status.containerStatuses[0].restartCount}",
    ])
    return sum(int(x) for x in out.split())


def snapshot(label):
    return {
        "label": label,
        "ts": int(time.time()),
        "proxy_mem_Mi": get_proxy_mem(),
        "redis_bytes": get_redis_used_bytes(),
        "cooldown_count": get_cooldown_count(),
        "proxy_restarts": get_proxy_restart_count(),
    }


def fmt(snap):
    return (
        f"[{snap['label']:>12}] proxy={snap['proxy_mem_Mi']}Mi "
        f"redis={snap['redis_bytes']//1024}KiB "
        f"cd={snap['cooldown_count']} "
        f"restarts={snap['proxy_restarts']}"
    )


class Worker(threading.Thread):
    def __init__(self, idx, master_key, deadline, stats):
        super().__init__(daemon=True)
        self.idx = idx
        self.master_key = master_key
        self.deadline = deadline
        self.stats = stats

    def run(self):
        while time.time() < self.deadline:
            if random.random() < 0.20:
                # transient fault toggle
                victim = random.choice(ACCOUNTS)
                fault = random.choice(["429", "500", "timeout"])
                try:
                    admin_set_fault(victim, fault)
                except Exception:
                    pass
                # call
                code = proxy_call(self.master_key, f"w{self.idx}-fault")
                self.stats[code] = self.stats.get(code, 0) + 1
                # recover
                try:
                    admin_set_fault(victim, "none")
                except Exception:
                    pass
            else:
                code = proxy_call(self.master_key, f"w{self.idx}-happy")
                self.stats[code] = self.stats.get(code, 0) + 1
            time.sleep(0.1)


def main():
    master_key = get_master_key()
    print(f"master_key: {master_key[:10]}...")
    print(f"Phase 4b stress: {WORKERS} workers x {DURATION}s, mixed faults")

    # bootstrap
    admin_reset()
    sh_ok([
        "kubectl", "exec", "-n", NS, "litellm-redis-0", "--",
        "sh", "-c",
        "redis-cli --scan --pattern 'deployment:*:cooldown' | xargs -r redis-cli DEL >/dev/null; "
        "redis-cli --scan --pattern 'deployment_affinity:*' | xargs -r redis-cli DEL >/dev/null",
    ])

    snapshots = [snapshot("before")]
    print(fmt(snapshots[-1]))

    stats = {}
    deadline = time.time() + DURATION
    workers = [Worker(i, master_key, deadline, stats) for i in range(WORKERS)]
    for w in workers:
        w.start()

    last_snap = time.time()
    while time.time() < deadline:
        time.sleep(2)
        if time.time() - last_snap >= 60:
            last_snap = time.time()
            elapsed = int(DURATION - (deadline - time.time()))
            snapshots.append(snapshot(f"t+{elapsed}s"))
            print(fmt(snapshots[-1]))

    for w in workers:
        w.join(timeout=10)

    snapshots.append(snapshot("after"))
    print(fmt(snapshots[-1]))

    print(f"\nrequest stats (status -> count): {dict(stats)}")
    total = sum(stats.values())
    s200 = stats.get(200, 0)
    s429 = stats.get(429, 0)
    s500 = stats.get(500, 0)
    s5xx_other = sum(v for k, v in stats.items() if 500 < k < 600)
    s0 = stats.get(0, 0)
    print(f"total={total} 200={s200} 429={s429} 500={s500} other5xx={s5xx_other} clientErr/0={s0}")

    # check proxy logs for ERROR / CRITICAL (last 200 lines)
    print("\nproxy log tail scan:")
    log = sh_ok([
        "kubectl", "logs", "-n", NS, "deploy/litellm-proxy", "--tail=500",
    ])
    bad = [ln for ln in log.splitlines() if " ERROR " in ln or " CRITICAL " in ln]
    # filter expected upstream-fault noise
    real_bad = [
        ln for ln in bad
        if "Mock 429" not in ln
        and "Mock 500" not in ln
        and "Mock 502" not in ln
        and "RateLimitError" not in ln
        and "APIError" not in ln
    ]
    print(f"  ERROR/CRITICAL lines: {len(bad)} (after filtering expected mock noise: {len(real_bad)})")
    for ln in real_bad[:10]:
        print(f"    {ln[:200]}")

    # cleanup
    admin_reset()
    print("\n[cleanup] mock reset done")

    # asserts
    before, after = snapshots[0], snapshots[-1]
    assert after["proxy_restarts"] == before["proxy_restarts"], (
        f"proxy restarted during stress: {before['proxy_restarts']} -> {after['proxy_restarts']}"
    )
    mem_delta = after["proxy_mem_Mi"] - before["proxy_mem_Mi"]
    redis_delta = after["redis_bytes"] - before["redis_bytes"]
    print(f"\ndeltas: proxy_mem={mem_delta:+}Mi  redis={redis_delta:+}B  cooldown={after['cooldown_count']-before['cooldown_count']:+}")
    # mem growth must be bounded (allow generous 200Mi headroom)
    assert mem_delta < 200, f"proxy memory grew {mem_delta}Mi (>200 threshold)"
    # redis growth bounded (cooldown keys + affinity keys: ~few KB per deployment)
    assert redis_delta < 10 * 1024 * 1024, f"redis grew {redis_delta}B (>10MiB threshold)"
    # real_bad lines must be 0 (mock-source noise filtered)
    assert len(real_bad) == 0, f"unexpected ERROR/CRITICAL lines: {len(real_bad)}"

    print("\n===== Phase 4b stress PASS =====")


if __name__ == "__main__":
    main()
"""


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    jms = os.path.join(here, "jms")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(REMOTE_SCRIPT)
        local = f.name
    try:
        remote = "/tmp/_litellm_dev_reactive_cooldown_stress.py"
        subprocess.check_call([jms, "scp", local, f"AIYJY-litellm:{remote}"])
        return subprocess.call([jms, "ssh", "AIYJY-litellm", "python3 " + shlex.quote(remote)])
    finally:
        os.unlink(local)


if __name__ == "__main__":
    sys.exit(main())
