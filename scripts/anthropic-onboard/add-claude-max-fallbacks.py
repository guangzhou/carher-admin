#!/usr/bin/env python3
"""Add claude-max-* → anthropic.* fallback entries to 198 prod LiteLLM."""
import subprocess, yaml, json, sys

out = subprocess.check_output(
    ["kubectl", "get", "cm", "litellm-config", "-n", "litellm-product", "-o", "json"])
cm = json.loads(out)
cfg = yaml.safe_load(cm["data"]["config.yaml"])

router = cfg.setdefault("router_settings", {})
fallbacks = router.setdefault("fallbacks", [])

# Desired: claude-max-X → anthropic.claude-X (网宿 backend)
desired = {
    "claude-max-opus":   "anthropic.claude-opus-4-7",
    "claude-max-sonnet": "anthropic.claude-sonnet-4-6",
    "claude-max-haiku":  "anthropic.claude-haiku-4-5",
}

# Remove any existing entries for claude-max-* keys (idempotent)
fallbacks = [f for f in fallbacks
             if not (isinstance(f, dict) and any(k.startswith("claude-max-") for k in f))]

# Append new entries
for src, dst in desired.items():
    fallbacks.append({src: [dst]})

router["fallbacks"] = fallbacks

# Strip server-managed fields
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
    cm["metadata"].pop(k, None)
if "annotations" in cm["metadata"]:
    cm["metadata"]["annotations"].pop(
        "kubectl.kubernetes.io/last-applied-configuration", None)
cm["data"]["config.yaml"] = yaml.dump(cfg, sort_keys=False, allow_unicode=True, width=10000)

sys.stdout.write(json.dumps(cm))
print(f"# added {len(desired)} fallback entries:", file=sys.stderr)
for src, dst in desired.items():
    print(f"#   {src} → {dst}", file=sys.stderr)
