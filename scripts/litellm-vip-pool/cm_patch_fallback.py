#!/usr/bin/env python3
"""Patch litellm-config ConfigMap fallbacks in-place.

Usage:
    cm_patch_fallback.py <cm.yaml> <group> <add|remove>

Reads CM yaml from disk, parses .data["config.yaml"] (inline yaml string),
adds or removes the 3 vip fallback entries for <group>, writes back to disk.

Operates only on:
    chatgpt-vip-<group>-gpt-5.5
    chatgpt-vip-<group>-gpt-5.4
    chatgpt-vip-<group>-gpt-5.3-codex
"""
import sys
import yaml

PAIRS = [
    ("gpt-5.5", ["chatgpt-gpt-5.5", "wangsu-gpt-5.5"]),
    ("gpt-5.4", ["chatgpt-gpt-5.4", "wangsu-gpt-5.4"]),
    ("gpt-5.3-codex", ["chatgpt-gpt-5.3-codex", "wangsu7-gpt-5.3-codex"]),
]


def main():
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    path, group, action = sys.argv[1], sys.argv[2], sys.argv[3]
    assert action in ("add", "remove")

    with open(path) as f:
        cm = yaml.safe_load(f)

    inner = yaml.safe_load(cm["data"]["config.yaml"])
    fallbacks = inner.setdefault("router_settings", {}).setdefault("fallbacks", [])

    vip_keys = {f"chatgpt-vip-{group}-{s}" for s, _ in PAIRS}
    # Drop any existing vip entries for this group (idempotent for both add & remove).
    fallbacks = [fb for fb in fallbacks
                 if not (isinstance(fb, dict) and set(fb.keys()) & vip_keys)]

    if action == "add":
        for s, targets in PAIRS:
            fallbacks.append({f"chatgpt-vip-{group}-{s}": targets})

    inner["router_settings"]["fallbacks"] = fallbacks
    cm["data"]["config.yaml"] = yaml.safe_dump(inner, sort_keys=False, allow_unicode=True)

    # Strip status-only keys that kubectl apply would reject.
    cm.get("metadata", {}).pop("resourceVersion", None)
    cm.get("metadata", {}).pop("uid", None)
    cm.get("metadata", {}).pop("creationTimestamp", None)
    cm.get("metadata", {}).pop("managedFields", None)

    with open(path, "w") as f:
        yaml.safe_dump(cm, f, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
