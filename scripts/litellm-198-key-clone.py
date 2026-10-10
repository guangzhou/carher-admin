#!/usr/bin/env python3
"""Clone routing fields from one exact 198 LiteLLM key to another.

Only ``models``, ``aliases`` and ``router_settings`` are copied. Identity,
budget, metadata, spend and blocked state remain owned by the target key.
The command is dry-run by default and verifies the target after an apply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402").rstrip("/")
MASTER = os.environ.get("LITELLM_MASTER_KEY", "")
COPY_FIELDS = ("models", "aliases", "router_settings")


def api(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Authorization", f"Bearer {MASTER}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def key_rows() -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for page in range(1, 1000):
        data = api("GET", f"/spend/keys?limit=500&page={page}")
        batch = data.get("data") if isinstance(data, dict) else data
        if not batch:
            break
        if isinstance(batch, dict):
            batch = batch.get("keys") or []
        for row in batch:
            alias = row.get("key_alias")
            if alias and row.get("token"):
                rows[alias] = row
        if len(batch) < 500:
            break
    return rows


def info(token: str) -> dict[str, Any]:
    data = api("GET", f"/key/info?key={token}")
    return data.get("info") or {}


def signature(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def copy_state(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": target["token"],
        **{field: source["info"].get(field) or ({} if field != "models" else [])
           for field in COPY_FIELDS},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Clone routing fields between exact 198 LiteLLM keys")
    parser.add_argument("--source", required=True, help="exact source key_alias")
    parser.add_argument("--target", required=True, help="exact target key_alias")
    parser.add_argument("--apply", action="store_true", help="write target; default is dry-run")
    parser.add_argument("--backup", type=Path, help="write target routing fields before apply")
    args = parser.parse_args()

    if not MASTER:
        print("LITELLM_MASTER_KEY is not set", file=sys.stderr)
        return 2
    if args.source == args.target:
        print("source and target must differ", file=sys.stderr)
        return 2
    if args.apply and not args.backup:
        print("--apply requires --backup", file=sys.stderr)
        return 2

    rows = key_rows()
    missing = [alias for alias in (args.source, args.target) if alias not in rows]
    if missing:
        print(f"missing exact key alias: {', '.join(missing)}", file=sys.stderr)
        return 2

    source = {"row": rows[args.source], "info": info(rows[args.source]["token"])}
    target = {"row": rows[args.target], "info": info(rows[args.target]["token"])}
    wanted = {field: source["info"].get(field) for field in COPY_FIELDS}
    current = {field: target["info"].get(field) for field in COPY_FIELDS}
    print(f"source={args.source} target={args.target}")
    print(f"source_signature={signature(wanted)} target_signature={signature(current)}")
    print("source_counts=" + json.dumps({
        "models": len(wanted["models"] or []),
        "aliases": len(wanted["aliases"] or {}),
        "fallbacks": len((wanted["router_settings"] or {}).get("fallbacks") or []),
    }, ensure_ascii=False))

    if not args.apply:
        print("dry-run: no writes")
        return 0

    assert args.backup is not None
    args.backup.write_text(json.dumps({
        "target": args.target,
        "token": rows[args.target]["token"],
        "fields": current,
    }, ensure_ascii=False, indent=2) + "\n")
    api("POST", "/key/update", copy_state(source, target))
    after = info(rows[args.target]["token"])
    readback = {field: after.get(field) for field in COPY_FIELDS}
    if readback != wanted:
        print(f"readback mismatch: source={signature(wanted)} target={signature(readback)}", file=sys.stderr)
        return 3
    print(f"IDENTICAL signature={signature(readback)} backup={args.backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
