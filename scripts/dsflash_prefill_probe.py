#!/usr/bin/env python3
"""Measure DeepSeek-V4-Flash prefill TTFT against ACTUAL prompt_tokens.

Why not the naive probe: a prompt built from random letter-soup inflates the
BPE token count 2-3x, so nominal "70k words" is not 70k tokens and the derived
tok/s is meaningless. This version reads usage.prompt_tokens back from the
stream (stream_options.include_usage) and reports throughput against that.

Each size is probed twice with the SAME text:
  COLD  - unique random head defeats any prefix cache
  WARM  - immediate repeat, should hit the radix / hierarchical cache

Usage:
  python3 dsflash_prefill_probe.py [base_url] [label]
  base_url defaults to http://127.0.0.1:8767
"""
import json
import random
import string
import sys
import time
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8767").rstrip("/")
LABEL = sys.argv[2] if len(sys.argv) > 2 else "run"
URL = BASE + "/v1/chat/completions"

# Natural-ish filler so the BPE ratio resembles real openclaw traffic rather
# than letter soup. Repeated to reach the target size.
FILLER = (
    "The assistant reviewed the conversation history and summarised the key "
    "decisions, open questions, and follow-up actions for the team before "
    "continuing with the next step of the task. "
)

# Target sizes chosen from measured production percentiles:
# p50 ~72k, p90 ~146k prompt tokens.
TARGETS = [16000, 64000, 146000]


def build(target_tokens, seed):
    """Unique random head (defeats cache) + natural filler to target size."""
    rnd = random.Random(seed)
    head = " ".join(
        "".join(rnd.choice(string.ascii_lowercase) for _ in range(7))
        for _ in range(24)
    )
    # ~0.75 tokens per word for natural English; overshoot then let the server
    # report the real count.
    reps = max(1, int(target_tokens / 0.75 / len(FILLER.split())))
    return head + " " + FILLER * reps


def probe(text, tag):
    body = json.dumps(
        {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": text + "\n\nReply with exactly: OK"}],
            "temperature": 0,
            "max_tokens": 4,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"Content-Type": "application/json"}
    )
    t0 = time.time()
    ttft = None
    prompt_tokens = None
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            for line in resp:
                if not line.startswith(b"data:"):
                    continue
                if ttft is None and b'"content"' in line:
                    ttft = time.time() - t0
                if b'"usage"' in line and b'"prompt_tokens"' in line:
                    try:
                        chunk = json.loads(line[5:].strip())
                        usage = chunk.get("usage") or {}
                        prompt_tokens = usage.get("prompt_tokens")
                    except Exception:
                        pass
    except Exception as exc:
        print(f"  {tag:22s} ERROR {exc}")
        return None
    total = time.time() - t0
    if ttft is None:
        print(f"  {tag:22s} no content frame (total={total:.2f}s)")
        return None
    rate = f"{prompt_tokens / ttft:8.0f} tok/s" if prompt_tokens else "  n/a"
    ptxt = f"{prompt_tokens:>7}" if prompt_tokens else "      ?"
    print(f"  {tag:22s} prompt={ptxt} ttft={ttft:7.2f}s total={total:7.2f}s prefill={rate}")
    return ttft, prompt_tokens


def main():
    print(f"=== dsflash prefill probe [{LABEL}] -> {BASE} ===")
    for target in TARGETS:
        text = build(target, seed=target * 7919 + int(time.time()))
        cold = probe(text, f"~{target // 1000}k COLD")
        warm = probe(text, f"~{target // 1000}k WARM")
        if cold and warm and warm[0] > 0:
            print(f"  {'':22s} cache speedup {cold[0] / warm[0]:.0f}x")


if __name__ == "__main__":
    main()
