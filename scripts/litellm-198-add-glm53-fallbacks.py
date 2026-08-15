#!/usr/bin/env python3
"""Additively add glm-5.3 router fallbacks -> openrouter-glm-5.2 on 198 prod.

Mirrors the existing glm-5.2 fallbacks (zai-glm-5.2 / claude-glm-5.2 /
zai-claude-glm-5.2 -> openrouter-glm-5.2) for the glm-5.3 groups, and ALSO adds
zai-coding-glm-5.3 (the group Cursor/Xcode users are aliased to — a single z.ai
coding-plan upstream, so it must have a fallback before we expose it to 566
keys). openrouter-glm-5.2 has allow_fallbacks:false, so it is a terminal hop —
no loop.

PURELY ADDITIVE, text-splice only: we insert four fallback blocks immediately
after the last glm-5.2 fallback block and before `model_group_alias:`. Every
other byte of config.yaml is preserved (this CM has hand-edited, non-alphabetical
keys that do NOT survive a yaml re-dump round-trip).

Usage:
    python3 litellm-198-add-glm53-fallbacks.py <live-cm.json> <out-cm.json>
"""
import json
import sys

import yaml

NEW_KEYS = ["zai-coding-glm-5.3", "zai-glm-5.3", "claude-glm-5.3", "zai-claude-glm-5.3"]
ANCHOR = "  - zai-claude-glm-5.2:\n    - openrouter-glm-5.2\n"
INSERT = "".join(f"  - {k}:\n    - openrouter-glm-5.2\n" for k in NEW_KEYS)


def main():
    live_path, out_path = sys.argv[1], sys.argv[2]
    cm = json.load(open(live_path))
    src = cm["data"]["config.yaml"]
    cfg = yaml.safe_load(src)

    fb = cfg["router_settings"]["fallbacks"]
    existing = set()
    for entry in fb:
        existing.update(entry.keys())
    for k in NEW_KEYS:
        if k in existing:
            sys.exit(f"FATAL: fallback for {k!r} already exists — refusing to duplicate")
    if "openrouter-glm-5.2" not in {m.get("model_name") for m in cfg["model_list"]}:
        sys.exit("FATAL: fallback target openrouter-glm-5.2 not in model_list")

    if src.count(ANCHOR) != 1:
        sys.exit(f"FATAL: anchor block found {src.count(ANCHOR)} times, expected 1")
    new_src = src.replace(ANCHOR, ANCHOR + INSERT, 1)

    # verify: parses, fallbacks grew by exactly 4, each new key -> [openrouter-glm-5.2],
    # and nothing else changed
    new_cfg = yaml.safe_load(new_src)
    new_fb = new_cfg["router_settings"]["fallbacks"]
    if len(new_fb) != len(fb) + 4:
        sys.exit(f"FATAL: fallbacks {len(fb)} -> {len(new_fb)}, expected +4")
    fb_map = {}
    for entry in new_fb:
        fb_map.update(entry)
    for k in NEW_KEYS:
        if fb_map.get(k) != ["openrouter-glm-5.2"]:
            sys.exit(f"FATAL: {k} -> {fb_map.get(k)!r}, expected ['openrouter-glm-5.2']")
    # everything except router_settings.fallbacks must be byte-identical structure
    a = dict(cfg["router_settings"]); a.pop("fallbacks")
    b = dict(new_cfg["router_settings"]); b.pop("fallbacks")
    if yaml.safe_dump(a, sort_keys=True) != yaml.safe_dump(b, sort_keys=True):
        sys.exit("FATAL: router_settings changed beyond fallbacks")
    for key in cfg:
        if key == "router_settings":
            continue
        if yaml.safe_dump(cfg[key], sort_keys=True) != yaml.safe_dump(new_cfg[key], sort_keys=True):
            sys.exit(f"FATAL: top-level key {key!r} changed — splice touched more than fallbacks")

    cm["data"]["config.yaml"] = new_src
    json.dump(cm, open(out_path, "w"), ensure_ascii=False, indent=2)
    print(f"wrote {out_path}: fallbacks {len(fb)} -> {len(new_fb)} (+{NEW_KEYS})")


if __name__ == "__main__":
    main()
