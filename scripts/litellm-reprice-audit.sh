#!/usr/bin/env bash
# litellm-reprice-audit: list current per-model cost in k8s/litellm-proxy.yaml.
# Use BEFORE writing a reprice spec to get a baseline; use AFTER to confirm.
#
# Output columns: model_name, in/M (USD), out/M (USD), cache_read/M, cache_create/M
#
# Usage: scripts/litellm-reprice-audit.sh [yaml-path]
set -euo pipefail
TARGET="${1:-k8s/litellm-proxy.yaml}"

if [ ! -f "$TARGET" ]; then
  echo "not found: $TARGET" >&2
  exit 1
fi

python3 - "$TARGET" << 'PY'
import re, sys
from collections import defaultdict

path = sys.argv[1]
lines = open(path).read().splitlines()

MODEL_RE = re.compile(r"^\s*-\s*model_name:\s*(\S+)\s*$")
COST_RE  = re.compile(r"^\s*(input_cost_per_token|output_cost_per_token|cache_read_input_token_cost|cache_creation_input_token_cost):\s*([0-9.eE+-]+)\s*$")

# Each model_name may appear multiple times (multi-deployment). We aggregate
# distinct (model_name, cost-tuple) so that price-inconsistent deployments
# under the same model_name are visible.
seen = defaultdict(set)
current = None
acc = {}

def flush():
    global acc
    if current and acc:
        # canonical tuple of all 4 cost fields
        t = (
            acc.get("input_cost_per_token", ""),
            acc.get("output_cost_per_token", ""),
            acc.get("cache_read_input_token_cost", ""),
            acc.get("cache_creation_input_token_cost", ""),
        )
        seen[current].add(t)
    acc = {}

for line in lines:
    m = MODEL_RE.match(line)
    if m:
        flush()
        current = m.group(1)
        continue
    cm = COST_RE.match(line)
    if cm and current:
        acc[cm.group(1)] = cm.group(2)

flush()

def to_per_M(v):
    if not v:
        return "-"
    return f"{float(v)*1_000_000:g}"

print(f"{'model_name':<48} {'in/M':>10} {'out/M':>10} {'cache_R/M':>12} {'cache_C/M':>12}")
print("-" * 96)

CLAUDE_HINT = re.compile(r"(?i)claude|opus|sonnet|haiku")
CHATGPT_HINT = re.compile(r"^chatgpt-")

for name in sorted(seen):
    tags = []
    if CLAUDE_HINT.search(name): tags.append("[claude]")
    if CHATGPT_HINT.match(name): tags.append("[chatgpt]")
    tag_str = " ".join(tags)
    for t in sorted(seen[name]):
        ic, oc, crc, ccc = t
        label = f"{name} {tag_str}".strip()
        print(f"{label:<48} {to_per_M(ic):>10} {to_per_M(oc):>10} {to_per_M(crc):>12} {to_per_M(ccc):>12}")

# Find chatgpt deployments WITHOUT cost (silent fallback to LiteLLM built-in)
print()
print("=== chatgpt-* deployments with NO explicit cost (fall back to built-in model_prices.json) ===")
missing = []
for name in seen:
    if CHATGPT_HINT.match(name):
        for t in seen[name]:
            if not any(t):
                missing.append(name)
                break
print("\n".join(missing) if missing else "(none — all chatgpt-* have explicit cost)")
PY
