#!/usr/bin/env python3
"""
Probe Anthropic Claude Code upstream quota for every Claude Max OAuth account.

Reads tokens from $ANTHROPIC_AUTH_DIR/acct-*/.env (default /Data/anthropic-auth),
sends a tiny Haiku probe to api.anthropic.com/v1/messages?beta=true using the
Claude-Code identification preamble, parses the `anthropic-ratelimit-unified-*`
headers, prints a colored table.

This script is meant to run ON 188 (where the .env files live). From a dev
machine, use the bash wrapper `cc-max-upstream-status.sh` which pipes it via
`jms ssh JSZX-AI-03 python3`.

Options:
  --watch <seconds>    Refresh every N seconds (Ctrl-C to stop)
  --auth-dir <path>    Override token directory (default /Data/anthropic-auth)
  --json               Output JSON instead of table

Cost: each invocation sends 1 Haiku request per account (max_tokens=5, ~10 tok
each). Negligible quota impact, but don't poll faster than every 30s.
"""
import argparse, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
from glob import glob

URL = "https://api.anthropic.com/v1/messages?beta=true"
CC_VERSION = "2.1.148.0b7"
HEADERS = {
    "anthropic-beta": ("interleaved-thinking-2025-05-14,"
                       "context-management-2025-06-27,"
                       "prompt-caching-scope-2026-01-05,"
                       "claude-code-20250219"),
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
        {"type": "text",
         "text": f"x-anthropic-billing-header: cc_version={CC_VERSION}; "
                 f"cc_entrypoint=sdk-cli; cch=probe;"},
        {"type": "text",
         "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."},
    ],
}).encode()


def probe(token):
    req = urllib.request.Request(
        URL, data=BODY_BYTES,
        headers={**HEADERS, "Authorization": f"Bearer {token}"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {})
    except Exception as e:
        return 0, {"_error": str(e)}


def load_accounts(auth_dir):
    accts = []
    for env_file in sorted(glob(os.path.join(auth_dir, "acct-*/.env"))):
        label = os.path.basename(os.path.dirname(env_file))
        try:
            with open(env_file) as f:
                for line in f:
                    if line.startswith("ANTHROPIC_OAUTH_TOKEN="):
                        accts.append((label, line.split("=", 1)[1].strip()))
                        break
        except Exception as e:
            print(f"  warn: cannot read {env_file}: {e}", file=sys.stderr)
    return accts


def fmt_eta(ts_str):
    if not ts_str:
        return "?"
    delta = int(ts_str) - time.time()
    if delta < 0:
        return "expired"
    if delta < 3600:
        return f"{int(delta/60)}m"
    if delta < 86400:
        return f"{delta/3600:.1f}h"
    return f"{delta/86400:.1f}d"


def fmt_dt(ts_str):
    if not ts_str:
        return "?"
    return (datetime.fromtimestamp(int(ts_str), tz=timezone.utc)
            .astimezone().strftime("%m-%d %H:%M"))


def util_indicator(pct):
    """Return a 1-char severity marker."""
    if pct < 0.5:
        return "."
    if pct < 0.8:
        return "!"
    return "X"


def bar(pct, width=20):
    if pct < 0 or pct > 1.5:
        return "[??????]"
    fill = min(width, int(pct * width))
    return "[" + "#" * fill + "-" * (width - fill) + f"] {pct*100:5.1f}%"


def report_table(rows):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\nCC Max upstream quota @ {ts}")
    print("=" * 96)
    hdr = f"{'account':<10} {'5h':<28} {'7d':<28} {'fallback':<8} {'5h reset':<12} {'7d reset'}"
    print(hdr)
    print("-" * 96)
    for r in rows:
        if r.get("error"):
            print(f"{r['label']:<10} ERROR: {r['error']}")
            continue
        marker = util_indicator(max(r["h5"], r["d7"]))
        print(f"{r['label']:<10} "
              f"{bar(r['h5'])} {bar(r['d7'])} "
              f"{r['fb']:<8} "
              f"{fmt_eta(r['r5'])+' ('+fmt_dt(r['r5'])+')':<12} "
              f"{fmt_eta(r['r7'])+' ('+fmt_dt(r['r7'])+')'}")
    print()
    over_50 = [r for r in rows if not r.get("error") and r["h5"] > 0.5]
    over_80 = [r for r in rows if not r.get("error") and r["h5"] > 0.8]
    if over_80:
        print(f"WARN: {len(over_80)} account(s) >80% on 5h — at risk: "
              + ", ".join(r["label"] for r in over_80))
    elif over_50:
        print(f"INFO: {len(over_50)} account(s) in fallback (>50%): "
              + ", ".join(r["label"] for r in over_50))
    print()


def collect(accts):
    rows = []
    for label, tok in accts:
        status, h = probe(tok)
        if "_error" in h:
            rows.append({"label": label, "error": h["_error"]})
            continue
        if status != 200:
            rows.append({"label": label, "error": f"HTTP {status}"})
            continue
        try:
            rows.append({
                "label": label,
                "h5": float(h.get("anthropic-ratelimit-unified-5h-utilization", -1)),
                "d7": float(h.get("anthropic-ratelimit-unified-7d-utilization", -1)),
                "fb": "ON" if h.get("anthropic-ratelimit-unified-fallback") == "available" else "-",
                "r5": h.get("anthropic-ratelimit-unified-5h-reset", ""),
                "r7": h.get("anthropic-ratelimit-unified-7d-reset", ""),
                "org": h.get("anthropic-organization-id", ""),
            })
        except (ValueError, TypeError) as e:
            rows.append({"label": label, "error": f"parse: {e}"})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth-dir", default=os.environ.get("ANTHROPIC_AUTH_DIR", "/Data/anthropic-auth"))
    ap.add_argument("--watch", type=int, default=0, metavar="SEC",
                    help="refresh every N seconds (Ctrl-C to stop)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    args = ap.parse_args()

    accts = load_accounts(args.auth_dir)
    if not accts:
        sys.exit(f"no tokens found in {args.auth_dir}/acct-*/.env")

    while True:
        rows = collect(accts)
        if args.json:
            print(json.dumps({"ts": time.time(), "accounts": rows},
                             ensure_ascii=False))
        else:
            if args.watch:
                print("\033c", end="")  # clear screen
            report_table(rows)
        if args.watch <= 0:
            break
        try:
            time.sleep(max(30, args.watch))
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
