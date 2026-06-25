#!/usr/bin/env python3
"""Audit 198 ChatGPT pool: K8s deployments vs LiteLLM router entries.

Lists every chatgpt-acct-N Pod and checks whether LiteLLM has 3 expected
entries (gpt-5.5, gpt-5.4, gpt-5.3-codex) for each. Reports drift.

A drift = Pod exists + state.json says ONLINE (not paused, not manual_offline)
but LiteLLM router missing one or more entries. This is the same class of bug
that hid acct-1 5h=0% on 2026-06-20 — `quota-rebalance.py` resume_acct only
fires when state.paused flips, so entries lost via any other path stay missing.

Since 2026-06-20 patch, quota-rebalance.py auto-heals on next probe via
router_has_entries(). This script lets you manually verify the pool any time.

Topology:
    runs on 188 (JSZX-AI-03)
    - state.json is local (/home/cltx/.chatgpt-quota/state/state.json)
    - kubectl/router live on 198 (ssh cltx@10.68.13.198 hop)
    - LiteLLM /model/info served on 10.68.13.198:30402/pro

Usage (on 188):
    python3 /tmp/chatgpt-pool-router-drift-audit.py             # human table
    python3 /tmp/chatgpt-pool-router-drift-audit.py --json      # machine-readable
    python3 /tmp/chatgpt-pool-router-drift-audit.py --fix       # call /model/new for ONLINE drift

Run via:
    jms ssh JSZX-AI-03 'python3 /tmp/chatgpt-pool-router-drift-audit.py'
"""

import json
import re
import subprocess
import sys
import urllib.request

PRD = "http://10.68.13.198:30402/pro"
MK = "sk-pro-litellm-ce077e2b0721bb419a633e4d"
POOL_KEY = "sk-chatgpt-198-d8a3f4e62b9c1057ef324918a7b6d3e0"
NS = "litellm-product"
STATE_JSON = "/home/cltx/.chatgpt-quota/state/state.json"
EXPECTED = {
    "gpt-5.5": "openai/chatgpt-gpt-5.5",
    "gpt-5.4": "openai/chatgpt-gpt-5.4",
    "gpt-5.3-codex": "openai/chatgpt-gpt-5.3-codex-spark",
}


def ssh_198(cmd: str) -> str:
    """Run shell on 198 K3s host (cltx@10.68.13.198), used for kubectl."""
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "cltx@10.68.13.198", cmd],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"FATAL: ssh 198 failed: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    return r.stdout


def list_pods() -> list[int]:
    out = ssh_198(
        f"export KUBECONFIG=$HOME/.kube/config && "
        f"kubectl -n {NS} get deploy --no-headers 2>/dev/null | "
        f"awk '{{print $1}}' | grep -E '^chatgpt-acct-[0-9]+$'"
    )
    return sorted(int(line.split("-")[-1]) for line in out.split() if line)


def deploy_replicas() -> dict[int, int]:
    """{acct_n: spec.replicas}。scale=0 的 deploy 仍出现，但 replicas==0 时
    pod 不存在；ONLINE 状态 + replicas=0 = 不可路由的 ghost（不应算 drift_online）。"""
    out = ssh_198(
        f"export KUBECONFIG=$HOME/.kube/config && "
        f"kubectl -n {NS} get deploy -o json 2>/dev/null"
    )
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return {}
    res: dict[int, int] = {}
    for it in d.get("items", []):
        name = (it.get("metadata") or {}).get("name", "")
        m = re.match(r"^chatgpt-acct-(\d+)$", name)
        if not m:
            continue
        res[int(m.group(1))] = (it.get("spec") or {}).get("replicas", 1)
    return res


def load_router() -> dict[int, set[str]]:
    req = urllib.request.Request(
        f"{PRD}/model/info", headers={"Authorization": f"Bearer {MK}"}
    )
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]
    present: dict[int, set[str]] = {}
    for x in data:
        mid = x.get("model_info", {}).get("id", "")
        m = re.match(r"^chatgpt-acct-(\d+)-(.+)$", mid)
        if m:
            present.setdefault(int(m.group(1)), set()).add(m.group(2))
    return present


def load_state() -> dict:
    """state.json lives on 188 (this host) — read local file directly."""
    try:
        with open(STATE_JSON) as f:
            return json.load(f)
    except FileNotFoundError:
        print(
            f"FATAL: {STATE_JSON} not found — script must run on 188 (JSZX-AI-03)",
            file=sys.stderr,
        )
        sys.exit(2)


def register(acct_n: int, short: str) -> tuple[int, str]:
    mid = f"chatgpt-acct-{acct_n}-{short}"
    api_base = f"http://chatgpt-acct-{acct_n}.{NS}.svc.cluster.local:4000"
    body = {
        "model_name": f"chatgpt-{short}",
        "litellm_params": {
            "model": EXPECTED[short],
            "api_base": api_base,
            "api_key": POOL_KEY,
        },
        "model_info": {"id": mid, "mode": "responses"},
    }
    req = urllib.request.Request(
        f"{PRD}/model/new",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {MK}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:200]


def main():
    args = sys.argv[1:]
    out_json = "--json" in args
    do_fix = "--fix" in args
    assume_yes = "--yes" in args

    nums = list_pods()
    replicas = deploy_replicas()
    router = load_router()
    state = load_state()
    expected = set(EXPECTED)

    rows = []
    drift_online = []
    drift_paused = []
    drift_scale0_ghost = []  # state.ONLINE/HEALTHY 但 deploy=0 + router 有 entry → 路由到死 svc
    for n in nums:
        has = router.get(n, set())
        miss = expected - has
        s = state.get(f"acct-{n}", {})
        is_paused = bool(s.get("paused"))
        manual = bool(s.get("manual_offline"))
        online = not is_paused and not manual
        scaled_down = replicas.get(n, 1) == 0
        if online and scaled_down:
            status = "SCALE0"
        else:
            status = "ONLINE" if online else ("MANUAL" if manual else "PAUSED")
        # state.ONLINE 但 deploy=0：router 里仍有 entry 就是 ghost（路由到死 svc → wangsu fallback）
        if online and scaled_down and has:
            drift_scale0_ghost.append((n, sorted(has)))
            # 不进 drift_online——pod 都没有，re-register 也修不了
        elif miss and online and not scaled_down:
            drift_online.append((n, sorted(miss)))
        elif miss and not online:
            drift_paused.append((n, sorted(miss)))
        rows.append({
            "acct": n,
            "status": status,
            "replicas": replicas.get(n, 1),
            "has": sorted(has),
            "missing": sorted(miss),
            "drift": bool((miss and online and not scaled_down) or (online and scaled_down and has)),
        })

    if out_json:
        print(json.dumps({
            "rows": rows,
            "drift_online": drift_online,
            "drift_paused": drift_paused,
            "drift_scale0_ghost": drift_scale0_ghost,
        }, indent=2))
        return

    print(f"{'acct':<10}{'status':<10}{'rep':<5}{'has':<35}{'missing':<35}")
    print("-" * 95)
    for r in rows:
        has_s = ",".join(r["has"]) if r["has"] else "(none)"
        miss_s = ",".join(r["missing"]) if r["missing"] else "-"
        marker = " ◀ DRIFT" if r["drift"] else ""
        print(f"acct-{r['acct']:<6}{r['status']:<10}{r['replicas']:<5}{has_s:<35}{miss_s:<35}{marker}")
    print()
    print(f"ONLINE drift (need fix): {len(drift_online)}")
    for n, miss in drift_online:
        print(f"  acct-{n}: missing={miss}")
    print(f"SCALE0 ghost (deploy=0 + state.ONLINE + router has entries): {len(drift_scale0_ghost)}")
    for n, hs in drift_scale0_ghost:
        print(f"  acct-{n}: ghost router entries={hs}  → DELETE + rollout restart, or scale up")
    print(f"PAUSED missing (expected — pause deleted them): {len(drift_paused)}")

    if do_fix:
        if not drift_online:
            print("\nnothing to fix.")
            return
        if not assume_yes:
            ans = input(
                f"\nregister {sum(len(m) for _,m in drift_online)} entries across "
                f"{len(drift_online)} accts? [y/N]: "
            ).strip().lower()
            if ans != "y":
                print("aborted.")
                return
        for n, miss in drift_online:
            for short in miss:
                code, body = register(n, short)
                print(f"  acct-{n} {short}: HTTP {code}")
                if code != 200 and "exists" not in body.lower():
                    print(f"    body: {body}")
        print(
            "\n⚠️  rollout restart litellm-proxy required for entries to take effect:"
        )
        print(
            f"  jms ssh JSZX-AI-03 'ssh cltx@10.68.13.198 \"export KUBECONFIG=\\$HOME/.kube/config && kubectl -n {NS} rollout restart deploy/litellm-proxy\"'"
        )


if __name__ == "__main__":
    main()
