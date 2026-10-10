#!/usr/bin/env python3
"""Set a gated key's model-family daily limit on 198 prod.

The script is intended to run on the 198 host, where the LiteLLM service and
master key are available through Kubernetes. It updates only
metadata.budget_family_overrides[family] through /key/update and preserves the
rest of the key metadata.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import subprocess
import sys
import urllib.error
import urllib.request

NS = "litellm-product"


def kubectl(*args: str) -> str:
    return subprocess.check_output(
        ["sudo", "-n", "kubectl", "-n", NS, *args],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def api(base: str, master: str, path: str, *, method: str = "GET", body=None):
    headers = {"Authorization": "Bearer " + master}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        return exc.code, raw[:500]


def get_runtime() -> tuple[str, str]:
    secret = kubectl("get", "secret", "litellm-secrets", "-o", "jsonpath={.data.LITELLM_MASTER_KEY}")
    master = base64.b64decode(secret).decode()
    cluster_ip = kubectl("get", "svc", "litellm-proxy", "-o", "jsonpath={.spec.clusterIP}")
    return f"http://{cluster_ip}:4000", master


def metadata_text(row: dict) -> list[str]:
    meta = row.get("metadata") or {}
    return [str(meta.get(field) or "") for field in ("display_name", "owner_name", "email")]


def matches(row: dict, args: argparse.Namespace) -> bool:
    alias = str(row.get("key_alias") or "")
    if not alias.lower().startswith(args.key_prefix.casefold()):
        return False
    if args.alias:
        return alias in args.alias
    if args.all:
        return True
    if args.owner is not None:
        needle = args.owner.casefold()
        return any(value.casefold() == needle for value in metadata_text(row))
    needle = args.contains.casefold()
    return needle in alias.casefold() or needle in json.dumps(
        row.get("metadata") or {}, ensure_ascii=False
    ).casefold()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--alias", action="append", help="Exact Cursor key alias; repeatable")
    selector.add_argument("--owner", help="Exact metadata display_name/owner_name/email")
    selector.add_argument("--contains", help="Case-insensitive substring in alias or metadata")
    selector.add_argument("--all", action="store_true", help="Target every key with the selected prefix")
    parser.add_argument("--key-prefix", default="cursor-", help="Alias prefix to target (default: cursor-)")
    parser.add_argument("--family", choices=("claude", "other"), default="claude")
    parser.add_argument("--limit", type=float, required=True, help="Daily family limit in USD")
    parser.add_argument("--max-budget", type=float, help="Optional key total budget in USD")
    parser.add_argument("--apply", action="store_true", help="Apply changes; default is dry-run")
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.max_budget is not None and args.max_budget <= 0:
        parser.error("--max-budget must be greater than zero")

    base, master = get_runtime()
    status, payload = api(base, master, "/spend/keys?limit=5000")
    if status != 200 or not isinstance(payload, list):
        print(f"failed to list keys: HTTP {status} {payload}", file=sys.stderr)
        return 1
    targets = [row for row in payload if matches(row, args)]
    if not targets:
        print("no keys matched the selector", file=sys.stderr)
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    label = "Cursor" if args.key_prefix.casefold() == "cursor-" else "Key"
    total_text = f" max_budget=${args.max_budget:g}" if args.max_budget is not None else ""
    print(f"=== {label} family limit {mode} · {len(targets)} key(s) · {args.family}=${args.limit:g}/day{total_text} ===")
    failed = []
    for row in sorted(targets, key=lambda item: str(item.get("key_alias") or "")):
        meta = row.get("metadata") or {}
        family = meta.get("budget_family_overrides") or {}
        old = family.get(args.family, "<default>")
        print(
            f"· {row.get('key_alias')}  old_{args.family}={old}  "
            f"max_budget={row.get('max_budget')} spend={row.get('spend')}"
        )

        if not args.apply:
            continue
        new_meta = copy.deepcopy(meta)
        overrides = copy.deepcopy(new_meta.get("budget_family_overrides") or {})
        overrides[args.family] = args.limit
        new_meta["budget_family_overrides"] = overrides
        payload = {"key": row["token"], "metadata": new_meta}
        if args.max_budget is not None:
            payload["max_budget"] = args.max_budget
        st, body = api(
            base,
            master,
            "/key/update",
            method="POST",
            body=payload,
        )
        if st != 200:
            print(f"  update {row.get('key_alias')} -> {st} {body}", file=sys.stderr)
            failed.append((row.get("key_alias"), st, body))
            continue
        print(f"  update {row.get('key_alias')} -> 200 OK")

    if failed:
        print(f"update failures: {len(failed)}/{len(targets)}", file=sys.stderr)

    if not args.apply:
        print("VERDICT: DRY-RUN. 加 --apply 执行。")
        return 0

    status, payload = api(base, master, "/spend/keys?limit=5000")
    if status != 200 or not isinstance(payload, list):
        print(f"failed to read back keys: HTTP {status} {payload}", file=sys.stderr)
        return 1
    readback = [row for row in payload if matches(row, args)]
    wrong = []
    for row in readback:
        got = ((row.get("metadata") or {}).get("budget_family_overrides") or {}).get(args.family)
        total_ok = args.max_budget is None or float(row.get("max_budget") or 0) == args.max_budget
        if float(got or 0) != args.limit or not total_ok:
            wrong.append((row.get("key_alias"), got, row.get("max_budget")))
    if failed or len(readback) != len(targets) or wrong:
        print(f"readback mismatch: matched={len(readback)} wrong={wrong}", file=sys.stderr)
        return 1
    print("VERDICT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
