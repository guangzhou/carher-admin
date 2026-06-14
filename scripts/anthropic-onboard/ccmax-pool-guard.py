#!/usr/bin/env python3
"""
CC Max upstream quota guard.

Reads configured acct OAuth tokens from /Data/anthropic-auth, probes Anthropic
quota headers, keeps a small local state file, and renders active-upstreams.json
for ccmax-random-proxy-guarded.py.

This script never writes OAuth tokens or proxy API keys to its output files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "/Data/ccmax-pool-guard/config.json"
DEFAULT_STATE = "/Data/ccmax-pool-guard/state.json"
DEFAULT_ACTIVE = "/Data/ccmax-pool-guard/active-upstreams.json"
DEFAULT_EVENTS = "/Data/ccmax-pool-guard/events.jsonl"
DEFAULT_AUTH_DIR = "/Data/anthropic-auth"

URL = "https://api.anthropic.com/v1/messages?beta=true"
CC_VERSION = "2.1.148.0b7"
HEADERS = {
    "anthropic-beta": (
        "interleaved-thinking-2025-05-14,"
        "context-management-2025-06-27,"
        "prompt-caching-scope-2026-01-05,"
        "claude-code-20250219"
    ),
    "anthropic-dangerous-direct-browser-access": "true",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
    "x-app": "cli",
    "user-agent": f"claude-cli/{CC_VERSION.split('.0b')[0]} (external, sdk-cli)",
}
BODY_BYTES = json.dumps({
    "model": "claude-haiku-4-5",
    "max_tokens": 5,
    "messages": [{"role": "user", "content": "hi"}],
    "system": [
        {
            "type": "text",
            "text": (
                f"x-anthropic-billing-header: cc_version={CC_VERSION}; "
                "cc_entrypoint=sdk-cli; cch=pool-guard;"
            ),
        },
        {
            "type": "text",
            "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
        },
    ],
}).encode()


@dataclass
class ProbeResult:
    acct: str
    status: int
    h5: float | None = None
    d7: float | None = None
    fallback: str = "-"
    h5_reset_at: int | None = None
    d7_reset_at: int | None = None
    org: str = ""
    error: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid json {path}: {exc}") from exc


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=str(path.parent), delete=False) as f:
        f.write(text)
        tmp = Path(f.name)
    os.chmod(tmp, mode)
    tmp.replace(path)


def append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_oauth_token(auth_dir: Path, acct: str) -> str:
    env_file = auth_dir / acct / ".env"
    for line in env_file.read_text().splitlines():
        if line.startswith("ANTHROPIC_OAUTH_TOKEN="):
            token = line.split("=", 1)[1].strip()
            if token:
                return token
    raise ValueError(f"ANTHROPIC_OAUTH_TOKEN missing in {env_file}")


def probe(acct: str, token: str, timeout: int) -> ProbeResult:
    req = urllib.request.Request(
        URL,
        data=BODY_BYTES,
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        headers = dict(resp.headers)
        status = resp.status
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers or {})
        status = exc.code
    except Exception as exc:
        return ProbeResult(acct=acct, status=0, error=str(exc))

    if status != 200:
        return ProbeResult(acct=acct, status=status, error=f"HTTP {status}")

    try:
        return ProbeResult(
            acct=acct,
            status=status,
            h5=float(headers.get("anthropic-ratelimit-unified-5h-utilization", -1)),
            d7=float(headers.get("anthropic-ratelimit-unified-7d-utilization", -1)),
            fallback="ON" if headers.get("anthropic-ratelimit-unified-fallback") == "available" else "-",
            h5_reset_at=parse_ts(headers.get("anthropic-ratelimit-unified-5h-reset")),
            d7_reset_at=parse_ts(headers.get("anthropic-ratelimit-unified-7d-reset")),
            org=headers.get("anthropic-organization-id", ""),
        )
    except (TypeError, ValueError) as exc:
        return ProbeResult(acct=acct, status=status, error=f"parse headers: {exc}")


def threshold_config(config: dict[str, Any]) -> dict[str, float | int]:
    raw = config.get("thresholds") or {}
    return {
        "h5_drain": float(raw.get("h5_drain", 0.70)),
        "h5_fast_drain": float(raw.get("h5_fast_drain", 0.75)),
        "h5_recover": float(raw.get("h5_recover", 0.40)),
        "d7_drain": float(raw.get("d7_drain", 0.90)),
        "d7_fast_drain": float(raw.get("d7_fast_drain", 0.95)),
        "d7_recover": float(raw.get("d7_recover", 0.80)),
        "min_drain_seconds": int(raw.get("min_drain_seconds", 1800)),
    }


def compute_state(
    acct: str,
    item: dict[str, Any],
    prev: dict[str, Any],
    result: ProbeResult,
    thresholds: dict[str, float | int],
    now_ts: int,
) -> dict[str, Any]:
    old = prev.get(acct) or {}
    old_state = old.get("state", "UNKNOWN")
    state = old_state if old_state != "UNKNOWN" else "ACTIVE"
    reason = old.get("drained_reason")
    cooldown_until = int(old.get("cooldown_until") or 0)
    consecutive_over = int(old.get("consecutive_over_limit") or 0)
    consecutive_healthy = int(old.get("consecutive_healthy") or 0)

    if not item.get("enabled", True):
        state = "DISABLED"
        reason = "config_disabled"
        cooldown_until = 0
    elif result.status in (401, 403):
        state = "HARD_DOWN"
        reason = f"auth_http_{result.status}"
        cooldown_until = 0
        consecutive_over = 0
        consecutive_healthy = 0
    elif result.status == 429:
        state = "DRAINED"
        reason = "upstream_429"
        cooldown_until = max(now_ts + int(thresholds["min_drain_seconds"]), result.h5_reset_at or 0)
        consecutive_over = 0
        consecutive_healthy = 0
    elif result.status != 200:
        state = "DRAINED"
        reason = result.error or f"http_{result.status}"
        cooldown_until = now_ts + int(thresholds["min_drain_seconds"])
        consecutive_over = 0
        consecutive_healthy = 0
    else:
        h5 = result.h5 if result.h5 is not None else -1
        d7 = result.d7 if result.d7 is not None else -1
        over = h5 >= float(thresholds["h5_drain"]) or d7 >= float(thresholds["d7_drain"])
        fast_over = h5 >= float(thresholds["h5_fast_drain"]) or d7 >= float(thresholds["d7_fast_drain"])
        if over:
            consecutive_over += 1
            consecutive_healthy = 0
        else:
            consecutive_over = 0
            consecutive_healthy += 1

        if fast_over or consecutive_over >= 2:
            state = "DRAINED"
            if h5 >= float(thresholds["h5_drain"]):
                reason = "h5_threshold"
                reset_at = result.h5_reset_at or now_ts + int(thresholds["min_drain_seconds"])
            else:
                reason = "d7_threshold"
                reset_at = result.d7_reset_at or now_ts + int(thresholds["min_drain_seconds"])
            cooldown_until = max(now_ts + int(thresholds["min_drain_seconds"]), reset_at)
        elif old_state == "DRAINED":
            reset_passed = now_ts >= cooldown_until
            recover_ok = h5 < float(thresholds["h5_recover"]) and d7 < float(thresholds["d7_recover"])
            if reset_passed and recover_ok and consecutive_healthy >= 2:
                state = "ACTIVE"
                reason = None
                cooldown_until = 0
            else:
                state = "DRAINED"
        elif h5 >= 0.65 or d7 >= 0.85:
            state = "HOT"
            reason = None
            cooldown_until = 0
        elif h5 >= 0.50 or d7 >= 0.70:
            state = "WATCH"
            reason = None
            cooldown_until = 0
        else:
            state = "ACTIVE"
            reason = None
            cooldown_until = 0

    return {
        "acct": acct,
        "enabled": bool(item.get("enabled", True)),
        "state": state,
        "previous_state": old_state,
        "status": result.status,
        "h5": result.h5,
        "d7": result.d7,
        "fallback": result.fallback,
        "h5_reset_at": result.h5_reset_at,
        "d7_reset_at": result.d7_reset_at,
        "drained_reason": reason,
        "cooldown_until": cooldown_until or None,
        "consecutive_over_limit": consecutive_over,
        "consecutive_healthy": consecutive_healthy,
        "last_error": result.error,
        "updated_at": now_iso(),
    }


def active_upstream_item(acct: str, item: dict[str, Any]) -> dict[str, Any]:
    proxy_env = item.get("proxy_env_file")
    if not proxy_env:
        proxy_env = f"/Data/claude-max-proxy-{acct}/.env"
    return {
        "acct": acct,
        "label": item.get("label", acct.replace("-", "")),
        "url": item["proxy_url"],
        "api_key_file": proxy_env,
        "rpm_limit": int(item.get("rpm_limit", 0) or 0),
        "concurrency_limit": int(item.get("concurrency_limit", 0) or 0),
    }


def summarize(states: dict[str, Any], active: list[dict[str, Any]]) -> str:
    parts = []
    for acct, st in sorted(states.items()):
        h5 = st.get("h5")
        d7 = st.get("d7")
        h5_s = "?" if h5 is None else f"{h5 * 100:.1f}%"
        d7_s = "?" if d7 is None else f"{d7 * 100:.1f}%"
        parts.append(f"{acct}:{st.get('state')} h5={h5_s} d7={d7_s}")
    return f"active={[x['acct'] for x in active]} " + " | ".join(parts)


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    state_path = Path(args.state)
    active_path = Path(args.active)
    events_path = Path(args.events)
    auth_dir = Path(args.auth_dir)

    config = load_json(config_path, {})
    if not config:
        raise SystemExit(f"config missing or empty: {config_path}")
    thresholds = threshold_config(config)
    prev = load_json(state_path, {"accounts": {}}).get("accounts", {})
    accounts = config.get("accounts") or {}
    now_ts = int(time.time())

    new_states: dict[str, Any] = {}
    active: list[dict[str, Any]] = []
    exit_code = 0

    for acct, item in sorted(accounts.items()):
        if not item.get("enabled", True):
            result = ProbeResult(acct=acct, status=0, error="config disabled")
        else:
            try:
                token = read_oauth_token(auth_dir, acct)
                result = probe(acct, token, args.timeout)
            except Exception as exc:
                result = ProbeResult(acct=acct, status=0, error=str(exc))
        state = compute_state(acct, item, prev, result, thresholds, now_ts)
        new_states[acct] = state
        if state["state"] in ("ACTIVE", "WATCH", "HOT"):
            active.append(active_upstream_item(acct, item))
        if state["state"] != state["previous_state"]:
            append_event(events_path, {
                "ts": now_iso(),
                "acct": acct,
                "old_state": state["previous_state"],
                "new_state": state["state"],
                "reason": state.get("drained_reason"),
                "h5": state.get("h5"),
                "d7": state.get("d7"),
                "cooldown_until": state.get("cooldown_until"),
            })
        if state["state"] in ("HARD_DOWN",):
            exit_code = 2

    state_doc = {"updated_at": now_iso(), "accounts": new_states}
    active_doc = {"updated_at": now_iso(), "upstreams": active}
    if args.dry_run:
        print(json.dumps({"state": state_doc, "active": active_doc}, ensure_ascii=False, indent=2))
    else:
        atomic_write(state_path, json.dumps(state_doc, ensure_ascii=False, indent=2) + "\n")
        atomic_write(active_path, json.dumps(active_doc, ensure_ascii=False, indent=2) + "\n")
        print(summarize(new_states, active))
    return exit_code


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--active", default=DEFAULT_ACTIVE)
    ap.add_argument("--events", default=DEFAULT_EVENTS)
    ap.add_argument("--auth-dir", default=os.environ.get("ANTHROPIC_AUTH_DIR", DEFAULT_AUTH_DIR))
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--dry-run", action="store_true")
    raise_code = run(ap.parse_args())
    sys.exit(raise_code)


if __name__ == "__main__":
    main()
