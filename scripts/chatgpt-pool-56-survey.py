#!/usr/bin/env python3
"""Survey & backfill GPT-5.6 DB entries for 198 chatgpt-acct pool.

Connects to 198 litellm-product DB via kubectl exec, compares the set of accts
that have gpt-5.5 entries (the "active pool") against those that have gpt-5.6
entries.  Reports missing accts and optionally registers them via /model/new.

Topology:
    runs on your Mac → scripts/jms ssh 198 → kubectl exec litellm-proxy

Usage:
    python3 scripts/chatgpt-pool-56-survey.py                # survey only
    python3 scripts/chatgpt-pool-56-survey.py --fix          # register missing
    python3 scripts/chatgpt-pool-56-survey.py --fix --yes    # skip prompt
    python3 scripts/chatgpt-pool-56-survey.py --clean-bulk   # delete dirty bulk entries
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
JMS = os.path.join(HERE, "jms")
NS = "litellm-product"
VARIANTS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
BASELINE_MODEL = "gpt-5.5"


def jms_ssh(cmd: str, timeout: int = 60) -> str:
    r = subprocess.run(
        [JMS, "ssh", "198", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        print(f"FATAL: jms ssh failed:\n{r.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    return r.stdout


def query_db(sql_body: str) -> str:
    """Run a python snippet inside litellm-proxy pod that queries the DB."""
    escaped = sql_body.replace("'", "'\\''")
    return jms_ssh(
        f"kubectl exec -n {NS} deploy/litellm-proxy -- python3 -c '{escaped}'",
        timeout=90,
    )


def get_accts_with_model(short: str) -> dict[int, list[str]]:
    """Return {acct_num: [model_id, ...]} for entries matching the short model name."""
    script = textwrap.dedent(f"""\
        import json, asyncio
        from litellm.proxy.utils import PrismaClient
        async def main():
            pc = PrismaClient(
                database_url="postgresql://litellm:dbpassword9999@litellm-db.{NS}.svc:5432/litellm",
                proxy_logging_obj=None)
            await pc.connect()
            rows = await pc.db.litellm_proxymodeltable.find_many(
                where={{"model_name": "chatgpt-{short}"}})
            out = []
            for r in rows:
                out.append({{"model_id": r.model_id, "model_name": r.model_name}})
            print(json.dumps(out))
        asyncio.run(main())
    """)
    raw = query_db(script)
    for line in raw.strip().splitlines():
        line = line.strip()
        if line.startswith("["):
            entries = json.loads(line)
            result: dict[int, list[str]] = {}
            for e in entries:
                mid = e["model_id"]
                m = re.match(r"^chatgpt-acct-(\d+)-", mid)
                if m:
                    n = int(m.group(1))
                    result.setdefault(n, []).append(mid)
            return result
    return {}


def get_bulk_entries() -> list[dict]:
    """Find dirty bulk entries (model_id contains spaces)."""
    script = textwrap.dedent(f"""\
        import json, asyncio
        from litellm.proxy.utils import PrismaClient
        async def main():
            pc = PrismaClient(
                database_url="postgresql://litellm:dbpassword9999@litellm-db.{NS}.svc:5432/litellm",
                proxy_logging_obj=None)
            await pc.connect()
            rows = await pc.db.litellm_proxymodeltable.find_many(
                where={{"model_name": {{"startswith": "chatgpt-gpt-5.6"}}}})
            out = []
            for r in rows:
                if " " in r.model_id:
                    out.append({{"model_id": r.model_id, "model_name": r.model_name}})
            print(json.dumps(out))
        asyncio.run(main())
    """)
    raw = query_db(script)
    for line in raw.strip().splitlines():
        line = line.strip()
        if line.startswith("["):
            return json.loads(line)
    return []


def get_svc_ip(acct_n: int) -> str | None:
    raw = jms_ssh(
        f"kubectl get svc chatgpt-acct-{acct_n} -n {NS} "
        f'-o jsonpath="{{.spec.clusterIP}}:{{.spec.ports[0].port}}"'
    ).strip()
    if ":" in raw:
        return f"http://{raw}"
    return None


def get_master_key() -> str:
    raw = jms_ssh(
        f"kubectl get secret litellm-secrets -n {NS} "
        f'-o jsonpath="{{.data.LITELLM_MASTER_KEY}}" | base64 -d'
    ).strip()
    if not raw:
        print("FATAL: cannot read LITELLM_MASTER_KEY", file=sys.stderr)
        sys.exit(2)
    return raw


def register_model(mk: str, acct_n: int, variant: str) -> tuple[int, str]:
    api_base = get_svc_ip(acct_n)
    if not api_base:
        return 0, f"no svc for acct-{acct_n}"
    model_id = f"chatgpt-acct-{acct_n}-{variant}"
    body = json.dumps({
        "model_name": f"chatgpt-{variant}",
        "litellm_params": {
            "model": f"chatgpt/{variant}",
            "api_base": api_base,
            "api_key": "sk-1234",
        },
        "model_info": {
            "id": model_id,
            "mode": "responses",
            "db_model": False,
        },
    })
    escaped_body = body.replace("'", "'\\''")
    raw = jms_ssh(
        f"curl -s -w '\\n%{{http_code}}' -X POST "
        f"http://10.43.149.225:4000/model/new "
        f"-H 'Authorization: Bearer {mk}' "
        f"-H 'Content-Type: application/json' "
        f"-d '{escaped_body}'",
        timeout=30,
    )
    lines = raw.strip().splitlines()
    http_code = int(lines[-1]) if lines else 0
    resp_body = "\n".join(lines[:-1]) if len(lines) > 1 else ""
    return http_code, resp_body[:200]


def delete_model(mk: str, model_id: str) -> tuple[int, str]:
    body = json.dumps({"id": model_id})
    escaped_body = body.replace("'", "'\\''")
    raw = jms_ssh(
        f"curl -s -w '\\n%{{http_code}}' -X POST "
        f"http://10.43.149.225:4000/model/delete "
        f"-H 'Authorization: Bearer {mk}' "
        f"-H 'Content-Type: application/json' "
        f"-d '{escaped_body}'",
        timeout=30,
    )
    lines = raw.strip().splitlines()
    http_code = int(lines[-1]) if lines else 0
    resp_body = "\n".join(lines[:-1]) if len(lines) > 1 else ""
    return http_code, resp_body[:200]


def main():
    args = sys.argv[1:]
    do_fix = "--fix" in args
    assume_yes = "--yes" in args
    clean_bulk = "--clean-bulk" in args

    print("Querying DB for baseline (gpt-5.5) entries...")
    baseline = get_accts_with_model(BASELINE_MODEL)
    baseline_accts = sorted(baseline.keys())
    print(f"  {len(baseline_accts)} accts have gpt-5.5: {baseline_accts}")

    variant_accts: dict[str, set[int]] = {}
    for v in VARIANTS:
        print(f"Querying DB for {v} entries...")
        entries = get_accts_with_model(v)
        variant_accts[v] = set(entries.keys())
        print(f"  {len(variant_accts[v])} accts have {v}")

    print()
    print(f"{'acct':<12}", end="")
    for v in VARIANTS:
        print(f"{v:<18}", end="")
    print()
    print("-" * (12 + 18 * len(VARIANTS)))

    missing_map: dict[int, list[str]] = {}
    for n in baseline_accts:
        cols = []
        miss = []
        for v in VARIANTS:
            if n in variant_accts[v]:
                cols.append("OK")
            else:
                cols.append("MISSING")
                miss.append(v)
        line = f"acct-{n:<7}"
        for c in cols:
            marker = "MISSING !" if c == "MISSING" else "OK"
            line += f"{marker:<18}"
        if miss:
            missing_map[n] = miss
        print(line)

    print()
    if not missing_map:
        print("All baseline accts have complete 5.6 entries.")
    else:
        total_missing = sum(len(v) for v in missing_map.values())
        print(f"Missing entries: {total_missing} across {len(missing_map)} accts:")
        for n, miss in sorted(missing_map.items()):
            print(f"  acct-{n}: {miss}")

    # Check bulk entries
    print()
    print("Checking for dirty bulk entries (model_id with spaces)...")
    bulk = get_bulk_entries()
    if bulk:
        print(f"  Found {len(bulk)} dirty bulk entries:")
        for b in bulk:
            mid = b["model_id"]
            display = mid[:60] + "..." if len(mid) > 60 else mid
            print(f"    {b['model_name']}: {display}")
    else:
        print("  No dirty bulk entries found.")

    if clean_bulk and bulk:
        mk = get_master_key()
        if not assume_yes:
            ans = input(f"\nDelete {len(bulk)} dirty bulk entries? [y/N]: ").strip().lower()
            if ans != "y":
                print("Skipped bulk cleanup.")
                bulk = []
        if bulk:
            for b in bulk:
                code, resp = delete_model(mk, b["model_id"])
                status = "OK" if code == 200 else f"HTTP {code}"
                print(f"  delete {b['model_name']}: {status}")

    if do_fix and missing_map:
        mk = get_master_key()
        if not assume_yes:
            total = sum(len(v) for v in missing_map.values())
            ans = input(
                f"\nRegister {total} entries across {len(missing_map)} accts? [y/N]: "
            ).strip().lower()
            if ans != "y":
                print("Aborted.")
                return
        ok = fail = 0
        for n, miss in sorted(missing_map.items()):
            for v in miss:
                code, resp = register_model(mk, n, v)
                if code == 200 or (code == 500 and "already" in resp.lower()):
                    ok += 1
                    print(f"  acct-{n} {v}: OK")
                else:
                    fail += 1
                    print(f"  acct-{n} {v}: FAIL HTTP {code} {resp[:80]}")
        print(f"\nDone: ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
