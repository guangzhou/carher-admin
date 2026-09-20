#!/usr/bin/env python3
"""Patch and audit GPT-6 Astra cost fields on a DB-managed LiteLLM proxy.

Replaces the throwaway ``/tmp/patch_astra_cache_*.py`` helpers used on
2026-09-05. Keep this in the repo: the first run of the throwaway version wrote
cache prices into ``model_info`` only, got HTTP 200 on all 26 rows, read back
fine from ``/model/info`` -- and still billed at the default rate, because
LiteLLM's cost calculator resolves cost fields from ``litellm_params``.

So this script always writes **both** blocks, and ``--verify`` counts
``cache_ok``/``bad`` per row instead of trusting the write's status code. After
``--apply`` the proxy deployment still needs a ``rollout restart``: an in-DB
patch only reaches the replicas that reload. Re-run ``--verify`` afterwards.

Aliyun is ConfigMap-authoritative (its init container wipes DB config rows), so
there ``--apply`` is meaningless -- edit the ConfigMaps via
``scripts/aliyun-add-gpt6-astra.py`` and use this script's ``--verify`` against
each proxy to confirm what the running replica actually loaded.

Usage:
  LITELLM_BASE=http://127.0.0.1:30402 LITELLM_MASTER_KEY=sk-... \\
      ./scripts/litellm-astra-cache-price-patch.py --verify
  ... --apply            # then: kubectl rollout restart deploy/litellm-proxy
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402").rstrip("/")
# Keep pure helpers importable by offline tests; CLI execution needs the key.
MASTER = os.environ.get("LITELLM_MASTER_KEY", "")

MODEL_NAME = "chatgpt-gpt-6-astra"

# Official Astra rates. Unset tiers (above_200k / above_512k / above_1hr) are
# deliberately absent rather than 0.0: a literal zero would bill as free.
PRICES = {
    "input_cost_per_token": 1e-5,
    "output_cost_per_token": 5e-5,
    "cache_read_input_token_cost": 1e-6,
    "cache_creation_input_token_cost": 1.25e-5,
    "cache_read_input_token_cost_above_272k_tokens": 2e-6,
    "cache_read_input_token_cost_above_272k_tokens_priority": 4e-6,
}
# Only these bill. model_info gets the same values for readability/tooling.
BILLING_BLOCK = "litellm_params"


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {MASTER}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def astra_rows(info: dict) -> list[dict]:
    return [
        row
        for row in (info.get("data") or [])
        if isinstance(row, dict) and row.get("model_name") == MODEL_NAME
    ]


def row_status(row: dict) -> tuple[bool, list[str]]:
    """A row is ok only when every price is right in the billing block."""
    params = row.get(BILLING_BLOCK) or {}
    bad = [field for field, want in PRICES.items() if params.get(field) != want]
    return (not bad, bad)


def plan_row(row: dict) -> dict | None:
    """Merge prices into both blocks; None when the row is already correct."""
    ok, _ = row_status(row)
    info = row.get("model_info") or {}
    info_ok = all(info.get(field) == want for field, want in PRICES.items())
    if ok and info_ok:
        return None
    params = dict(row.get(BILLING_BLOCK) or {})
    params.update(PRICES)
    merged_info = dict(info)
    merged_info.update(PRICES)
    return {BILLING_BLOCK: params, "model_info": merged_info}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="PATCH rows needing prices")
    parser.add_argument("--verify", action="store_true", help="audit only, no writes")
    args = parser.parse_args()
    if not (args.apply or args.verify):
        parser.error("pass --verify (audit) or --apply (write)")
    if not MASTER:
        print("LITELLM_MASTER_KEY is required", file=sys.stderr)
        return 2

    rows = astra_rows(api("GET", "/model/info"))
    if not rows:
        print(f"no {MODEL_NAME} rows at {BASE}; nothing to audit", file=sys.stderr)
        return 1

    ok = sum(1 for row in rows if row_status(row)[0])
    print(f"base={BASE} rows={len(rows)} cache_ok={ok} bad={len(rows) - ok}")
    for row in rows:
        good, bad = row_status(row)
        if not good:
            mid = (row.get("model_info") or {}).get("id")
            print(f"  bad {mid}: {','.join(bad)}")

    if not args.apply:
        if ok != len(rows):
            print("verify failed: fix with --apply, then rollout restart the proxy", file=sys.stderr)
            return 4
        return 0

    success = failures = 0
    for row in rows:
        plan = plan_row(row)
        if plan is None:
            continue
        mid = (row.get("model_info") or {}).get("id")
        try:
            api("POST", f"/model/{mid}/update", plan)
            success += 1
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {mid}: {exc}", file=sys.stderr)
            if failures >= 2:
                print("aborting after two write failures", file=sys.stderr)
                return 3
    print(f"patched_ok={success} patched_fail={failures}")
    print("NOT DONE YET: kubectl rollout restart deploy/litellm-proxy, then re-run --verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
