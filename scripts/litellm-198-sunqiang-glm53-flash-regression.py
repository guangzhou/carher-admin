#!/usr/bin/env python3
"""Regression probe for sunqiang's 198 LiteLLM glm-5.3-flash access.

The management API exposes key hashes (not the original secret), so the
default run verifies the live deployment and sunqiang allowlists with the
198 master key.  Pass ``--probe-key`` (or ``LITELLM_PROBE_KEY``) when the
actual Cursor/Claude key is available to run an end-to-end request too.

Examples::

    export LITELLM_MASTER_KEY='sk-...'
    python3 scripts/litellm-198-sunqiang-glm53-flash-regression.py

    LITELLM_PROBE_KEY='sk-...' python3 \
      scripts/litellm-198-sunqiang-glm53-flash-regression.py \
      --protocol openai

The script is read-only with respect to key configuration.  Probe requests
may create normal LiteLLM spend-log entries.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE = "http://10.68.13.198:30402"
CANONICAL_MODEL = "zai-coding-glm-5.3-flash"
SHORT_MODEL = "glm-5.3-flash"
DEFAULT_ALIASES = ("cursor-sunqiang", "claude-code-sunqiang")


def api_json(base: str, path: str, bearer: str) -> Any:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        headers={"Authorization": f"Bearer {bearer}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_keys(rows: list[dict[str, Any]], aliases: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if any(str(row.get("key_alias") or "").startswith(prefix) for prefix in aliases)
    ]


def classify(status: int, body: str) -> str:
    if status == 200:
        return "PASS"
    if status == 400 and "Invalid model name" in body:
        return "FAIL_INVALID_MODEL_ALIAS"
    if status == 401 and ("Authentication Failed" in body or "auth_error" in body):
        return "FAIL_PROVIDER_AUTH"
    if status == 401:
        return "FAIL_KEY_AUTH"
    if status == 429 and "Usage limit reached" in body:
        return "FAIL_PROVIDER_QUOTA_5H"
    if status == 429:
        return "FAIL_PROVIDER_RATE_LIMIT"
    if status == 500:
        return "FAIL_PROVIDER_OR_ROUTER_500"
    return f"FAIL_HTTP_{status}"


def probe(
    base: str,
    token: str,
    model: str,
    protocol: str,
    timeout: float,
) -> tuple[int, float, str, str]:
    if protocol == "openai":
        path = "/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
            # A regression probe must expose the first provider error instead
            # of waiting through LiteLLM retries and turning 429 into timeout.
            "max_retries": 0,
        }
        headers = {"Authorization": f"Bearer {token}"}
    else:
        path = "/v1/messages"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
            "max_retries": 0,
        }
        headers = {"x-api-key": token, "anthropic-version": "2023-06-01"}
    data = json.dumps(payload).encode("utf-8")
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2048).decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read(2048).decode("utf-8", errors="replace")
        status = exc.code
    except Exception as exc:  # network/DNS/timeout
        return 0, time.monotonic() - started, "FAIL_TRANSPORT", str(exc)
    return status, time.monotonic() - started, classify(status, raw), raw.replace("\n", " ")[:240]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("LITELLM_BASE_URL", DEFAULT_BASE))
    parser.add_argument("--master-key", default=os.getenv("LITELLM_MASTER_KEY"))
    parser.add_argument("--probe-key", default=os.getenv("LITELLM_PROBE_KEY"))
    parser.add_argument("--protocol", choices=("openai", "anthropic"), default="openai")
    parser.add_argument(
        "--alias",
        action="append",
        dest="aliases",
        help="key alias or prefix to inspect (repeatable; default: both sunqiang keys)",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    if not args.master_key:
        parser.error("LITELLM_MASTER_KEY or --master-key is required")
    aliases = tuple(args.aliases or DEFAULT_ALIASES)

    print(f"=== 198 LiteLLM glm-5.3-flash regression ===")
    print(f"base={args.base_url} protocol={args.protocol}")

    try:
        rows = api_json(args.base_url, "/spend/keys?limit=600", args.master_key)
        model_info = api_json(args.base_url, "/model/info", args.master_key)
    except Exception as exc:
        print(f"FATAL management API: {exc}", file=sys.stderr)
        return 2

    matches = find_keys(rows if isinstance(rows, list) else [], aliases)
    print(f"sunqiang keys matched={len(matches)}")
    if not matches:
        return 1

    deployments = {
        item.get("model_name"): item
        for item in (model_info.get("data", []) if isinstance(model_info, dict) else [])
        if isinstance(item, dict)
    }
    live = deployments.get(CANONICAL_MODEL)
    if live:
        params = live.get("litellm_params", {})
        info = live.get("model_info", {})
        print(
            "deployment PASS: "
            f"model_name={CANONICAL_MODEL} model={params.get('model')} "
            f"api_base={params.get('api_base')} id={info.get('id')}"
        )
    else:
        print(f"deployment FAIL: {CANONICAL_MODEL} missing from /model/info")

    failed = int(live is None)
    # The master key bypasses per-key allowlists and exercises the actual
    # provider route.  A 401 here means the configured z.ai credential is
    # rejected upstream; it is distinct from a missing user allowlist.
    status, elapsed, verdict, detail = probe(
        args.base_url, args.master_key, CANONICAL_MODEL, "openai", args.timeout
    )
    print(
        f"provider probe {CANONICAL_MODEL}: {verdict} HTTP={status or 'n/a'} "
        f"elapsed={elapsed:.2f}s detail={detail}"
    )
    failed += int(verdict != "PASS")

    for row in matches:
        alias = str(row.get("key_alias") or "")
        models = row.get("models") or []
        aliases_map = row.get("aliases") or {}
        allowlisted = CANONICAL_MODEL in models
        mapped = aliases_map.get(SHORT_MODEL)
        print(
            f"key {alias}: token_present={'yes' if row.get('token') else 'no'} "
            f"allowlist[{CANONICAL_MODEL}]={'yes' if allowlisted else 'NO'} "
            f"alias[{SHORT_MODEL}]={mapped or '(none)'}"
        )
        failed += int(not allowlisted or mapped != CANONICAL_MODEL)

    if not args.probe_key:
        print("probe SKIP: original sunqiang key is not recoverable from 198 management API")
        print("         set LITELLM_PROBE_KEY to run the end-to-end request")
        return 1 if failed or not live else 0

    for model in (CANONICAL_MODEL, SHORT_MODEL):
        status, elapsed, verdict, detail = probe(
            args.base_url, args.probe_key, model, args.protocol, args.timeout
        )
        print(f"probe {model}: {verdict} HTTP={status or 'n/a'} elapsed={elapsed:.2f}s detail={detail}")
        failed += int(verdict != "PASS")
    return 1 if failed or not live else 0


if __name__ == "__main__":
    sys.exit(main())
