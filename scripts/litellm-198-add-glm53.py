#!/usr/bin/env python3
"""Additively clone the five z.ai-direct glm-5.2 deployments on 198 prod into
glm-5.3 deployments.

Reads a live `kubectl get cm litellm-config -o json` dump, appends five new
`model_list` entries (each a deep copy of the matching glm-5.2 entry with the
substring `glm-5.2` rewritten to `glm-5.3` in every string field), and writes
the patched CM JSON back out for `kubectl replace`.

glm-5.3 confirmed served by z.ai paas/v4 (probe 2026-08-14: bogus model -> 400
code 1214 modelCode does not exist; glm-5.3 -> 429 code 1113 balance == glm-5.2).
No official glm-5.3 pricing published yet, so the clones keep glm-5.2 pricing as
a placeholder ($1.4/$4.4/M, cached $0.26) — revisit when z.ai publishes it.

This is PURELY ADDITIVE: no existing entry, fallback, alias, or allowlist is
touched. We splice the five new entries into the raw config.yaml text at the end
of the model_list block instead of re-dumping the whole document, because this
CM has hand-edited entries whose keys are not alphabetically ordered — a full
round-trip would cosmetically reorder ~20 unrelated lines. Text splice keeps the
review diff to exactly the five appended blocks and nothing else.

Usage:
    python3 litellm-198-add-glm53.py <live-cm.json> <out-cm.json>
"""
import copy
import json
import sys

import yaml

# glm-5.2 model_name -> expected `model` (source-of-truth guard before cloning)
SOURCES = {
    "zai-glm-5.2": "openai/glm-5.2",
    "zai-coding-glm-5.2": "openai/glm-5.2",
    "zai-claude-glm-5.2": "anthropic/glm-5.2",
    "zai-max-glm-5.2": "anthropic/glm-5.2",
    "claude-glm-5.2": "openai/glm-5.2",
}


def rewrite(obj):
    """Recursively rewrite the substring glm-5.2 -> glm-5.3 in every string."""
    if isinstance(obj, str):
        return obj.replace("glm-5.2", "glm-5.3")
    if isinstance(obj, dict):
        return {k: rewrite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rewrite(v) for v in obj]
    return obj


def find(model_list, name):
    hits = [m for m in model_list if m.get("model_name") == name]
    if len(hits) != 1:
        sys.exit(f"FATAL: expected exactly 1 entry named {name!r}, found {len(hits)}")
    return hits[0]


def main():
    live_path, out_path = sys.argv[1], sys.argv[2]
    cm = json.load(open(live_path))
    src = cm["data"]["config.yaml"]
    cfg = yaml.safe_load(src)

    ml = cfg["model_list"]
    before = len(ml)
    existing_names = {m.get("model_name") for m in ml}
    existing_ids = {m.get("model_info", {}).get("id") for m in ml}

    new_entries = []
    for name, expect_model in SOURCES.items():
        entry = find(ml, name)
        got = entry["litellm_params"].get("model")
        if got != expect_model:
            sys.exit(f"FATAL: {name}.model is {got!r}, expected {expect_model!r} — 198 has drifted")
        clone = rewrite(copy.deepcopy(entry))
        new_name = clone["model_name"]
        new_id = clone.get("model_info", {}).get("id")
        if new_name in existing_names:
            sys.exit(f"FATAL: {new_name!r} already exists — refusing to duplicate")
        if new_id in existing_ids:
            sys.exit(f"FATAL: model_info.id {new_id!r} already exists — refusing to duplicate")
        existing_names.add(new_name)
        existing_ids.add(new_id)
        new_entries.append(clone)
        print(f"  + {new_name}  ({clone['litellm_params']['model']} @ "
              f"{clone['litellm_params'].get('api_base')}  id={new_id})")

    # dump only the five new entries, formatted exactly like existing model_list
    # items (PyYAML puts the `- ` at column 0), then strip the wrapper key
    fragment = yaml.safe_dump({"model_list": new_entries}, sort_keys=True,
                              default_flow_style=False, allow_unicode=True, width=10 ** 9)
    assert fragment.startswith("model_list:\n"), "unexpected fragment shape"
    fragment = fragment[len("model_list:\n"):]

    # locate the model_list block in the raw text and find where it ends: the
    # first column-0 top-level key line after `model_list:`
    lines = src.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln == "model_list:"), None)
    if start is None:
        sys.exit("FATAL: no `model_list:` line found in config.yaml")
    end = None
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln and ln[0] not in " -":  # next top-level key at column 0
            end = i
            break
    if end is None:
        sys.exit("FATAL: could not find the end of the model_list block")

    # sanity: the block we identified must contain exactly `before` entries
    block_entries = sum(1 for ln in lines[start + 1:end] if ln.startswith("- "))
    if block_entries != before:
        sys.exit(f"FATAL: model_list text block has {block_entries} entries, parser saw {before}")

    frag_lines = fragment.rstrip("\n").split("\n")
    patched = lines[:end] + frag_lines + lines[end:]
    new_src = "\n".join(patched)

    # verify the spliced text parses and has exactly the five new entries
    new_cfg = yaml.safe_load(new_src)
    if len(new_cfg["model_list"]) != before + 5:
        sys.exit(f"FATAL: spliced model_list has {len(new_cfg['model_list'])}, expected {before + 5}")
    new_names = {m.get("model_name") for m in new_cfg["model_list"]}
    added = new_names - {m.get("model_name") for m in ml}
    if added != {c["model_name"] for c in new_entries}:
        sys.exit(f"FATAL: spliced entries mismatch: {added}")
    # everything except model_list must be byte-identical
    for k in cfg:
        if k == "model_list":
            continue
        if yaml.safe_dump(cfg[k], sort_keys=True) != yaml.safe_dump(new_cfg[k], sort_keys=True):
            sys.exit(f"FATAL: top-level key {k!r} changed — splice touched more than model_list")

    cm["data"]["config.yaml"] = new_src
    json.dump(cm, open(out_path, "w"), ensure_ascii=False, indent=2)
    print(f"wrote {out_path}: model_list {before} -> {len(new_cfg['model_list'])} (text splice)")


if __name__ == "__main__":
    main()
