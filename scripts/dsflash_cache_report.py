#!/usr/bin/env python3
"""Measure prefix-cache effectiveness for deepseek-v4-flash from sglang logs.

Run ON the GPU host (h100 / 192.168.3.205):
    python3 dsflash_cache_report.py 6h

Reads the sglang scheduler's "Prefill batch" lines out of the container log and
reports, per REQUEST, how much of the prompt was served from cache vs
recomputed. That share is the thing that decides user-visible latency: a fully
cached 146k-token prompt returns in ~0.5s, a cold one takes ~15s.

⚠ REQUEST BOUNDARY DETECTION — do not simplify this away.
A single chunked prefill emits one log line PER CHUNK. With
--chunked-prefill-size 8192 a 146k-token prompt is ~18 lines. Counting lines as
requests inflates the request count and skews every percentile toward large
prompts (this bug produced "89.5% cold / p50 37k" on 2026-08-07 before it was
caught; the corrected numbers were 24.4% cold / p50 72k). A chunk line is a
continuation when its #pending-token equals the previous line's pending minus
this line's #new-token; only non-continuations start a request.

⚠ Probe traffic contaminates this. dsflash_prefill_probe.py deliberately sends
cache-defeating prompts, so run this when no probe is in flight, or discount the
handful of oversized COLD entries it adds.
"""
import collections
import re
import subprocess
import sys

CONTAINER = "deepseek-v4-dspark"

# Measured prefill throughput. 2026-08-07: ~8.6k tok/s at
# --chunked-prefill-size 1024, ~9.6-13.0k tok/s at 8192 (higher on longer
# prompts). Used only to turn recompute sizes into an implied TTFT.
PREFILL_TOK_S = 11000

LINE_RE = re.compile(
    r"Prefill batch, #new-seq: (\d+), #new-token: (\d+), #cached-token: (\d+)"
    r".*?#running-req: (\d+), #queue-req: (\d+)"
    r"(?:, #pending-token: (\d+))?"
)


def pct(values, p):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def main():
    window = sys.argv[1] if len(sys.argv) > 1 else "24h"
    proc = subprocess.run(
        ["docker", "logs", "--since", window, CONTAINER],
        capture_output=True,
        text=True,
        errors="replace",
    )
    # sglang logs to stderr
    lines = proc.stderr.splitlines()

    requests = []
    prev_pending = None
    total_lines = queue_nonzero = max_queue = max_running = 0

    for line in lines:
        match = LINE_RE.search(line)
        if not match:
            continue
        total_lines += 1
        _new_seq, new_tok, cached_tok, running, queued, pending = (
            int(x) if x else 0 for x in match.groups()
        )
        max_queue = max(max_queue, queued)
        max_running = max(max_running, running)
        if queued > 0:
            queue_nonzero += 1

        is_continuation = (
            prev_pending is not None
            and prev_pending > 0
            and pending == prev_pending - new_tok
        )
        if not is_continuation:
            # new_tok + pending = the full amount this request must compute
            requests.append((cached_tok, new_tok + pending))
        prev_pending = pending

    print(f"window={window}  prefill-batch lines={total_lines}  "
          f"distinct requests={len(requests)}")
    print(f"batches with queue>0={queue_nonzero}  max #queue-req={max_queue}  "
          f"max #running-req={max_running}")

    buckets = collections.Counter()
    sizes = []
    cold_recompute = []
    for cached_tok, compute_tok in requests:
        total = cached_tok + compute_tok
        if total < 1000:
            continue
        sizes.append(total)
        hit = cached_tok / total
        if hit >= 0.9:
            buckets["cache>=90%"] += 1
        elif hit >= 0.5:
            buckets["cache 50-90%"] += 1
        elif hit >= 0.1:
            buckets["cache 10-50%"] += 1
        else:
            buckets["<10% COLD"] += 1
            cold_recompute.append(compute_tok)

    n = sum(buckets.values())
    print(f"\nrequests with prompt >=1k tokens: {n}")
    for key in ("cache>=90%", "cache 50-90%", "cache 10-50%", "<10% COLD"):
        share = 100 * buckets[key] / n if n else 0
        print(f"  {key:14s} {buckets[key]:5d}  {share:5.1f}%")

    print(f"\nprompt tokens:       p50={pct(sizes, .5)}  p90={pct(sizes, .9)}  "
          f"p99={pct(sizes, .99)}  max={max(sizes) if sizes else 0}")
    print(f"COLD recompute tok:  p50={pct(cold_recompute, .5)}  "
          f"p90={pct(cold_recompute, .9)}  p99={pct(cold_recompute, .99)}  "
          f"n={len(cold_recompute)}")
    print(f"  -> implied TTFT @{PREFILL_TOK_S} tok/s: "
          f"p50={pct(cold_recompute, .5) / PREFILL_TOK_S:.1f}s  "
          f"p90={pct(cold_recompute, .9) / PREFILL_TOK_S:.1f}s  "
          f"p99={pct(cold_recompute, .99) / PREFILL_TOK_S:.1f}s")
    print("\nBaseline for comparison (2026-08-07, 24h BEFORE tuning):")
    print("  cache>=90% 64.9% | 50-90% 5.9% | 10-50% 4.8% | <10% COLD 24.4%")
    print("  prompt p50=71936 p90=146176 p99=197016")


if __name__ == "__main__":
    main()
