#!/usr/bin/env python3
"""Repoint the two wangsu-backed glm-5.2 deployments on 198 prod at 智谱官方 (api.z.ai),
and register OpenRouter as their fallback.

Reads a live `kubectl get cm litellm-config -o json` dump, edits
data["config.yaml"] structurally (YAML round-trip is byte-identical on this CM,
verified), and writes the patched CM JSON back out for `kubectl replace`.

Every edit asserts the current value first — if 198 has drifted from what we
surveyed, this aborts instead of silently writing something else.

Usage:
    python3 litellm-198-glm52-to-zhipu.py <live-cm.json> <out-cm.json> [expected_fallbacks]

`expected_fallbacks` defaults to the live-CM baseline (47). The on-disk manifest
/root/litellm-product-manifests/30-cm-litellm-config.yaml has independently
drifted to 48, so pass it explicitly when patching that file.
"""
import json
import sys

import yaml

ZAI_KEY = "os.environ/ZAI_API_KEY"
CACHE_READ = 2.6e-07  # 智谱官方 GLM-5.2 cached input $0.26/M — docs.z.ai/guides/overview/pricing

# model_name -> (expected current litellm_params subset, new litellm_params subset)
REPOINT = {
    "claude-glm-5.2": (
        {
            "api_base": "https://aigateway.edgecloudapp.com/v2/gws/8abh1x4g/compat",
            "api_key": "os.environ/WANGSU_API_KEY",
            "model": "custom_openai/glm-5.2",
        },
        {
            "api_base": "https://api.z.ai/api/paas/v4",
            "api_key": ZAI_KEY,
            "model": "openai/glm-5.2",
        },
    ),
    "zai-claude-glm-5.2": (
        {
            "api_base": "https://aigateway.edgecloudapp.com/v2/gws/rpwikyxw/anthropic",
            "api_key": "os.environ/WANGSU_GLM52_ANTHROPIC_KEY",
            # the `anthropic.` prefix is a wangsu gateway requirement, not a z.ai one
            "model": "anthropic/anthropic.glm-5.2",
        },
        {
            "api_base": "https://api.z.ai/api/anthropic",
            "api_key": ZAI_KEY,
            "model": "anthropic/glm-5.2",
        },
    ),
}

# groups that get cache_read_input_token_cost written into both places
CACHE_COST_GROUPS = ["claude-glm-5.2", "zai-claude-glm-5.2", "zai-glm-5.2"]

NEW_FALLBACKS = [
    {"zai-glm-5.2": ["openrouter-glm-5.2"]},
    {"claude-glm-5.2": ["openrouter-glm-5.2"]},
    {"zai-claude-glm-5.2": ["openrouter-glm-5.2"]},
]

BASELINE_FALLBACKS = 47
BASELINE_ALIASES = 10


def find(model_list, name):
    hits = [m for m in model_list if m.get("model_name") == name]
    if len(hits) != 1:
        sys.exit(f"FATAL: expected exactly 1 entry named {name!r}, found {len(hits)}")
    return hits[0]


def main():
    live_path, out_path = sys.argv[1], sys.argv[2]
    expected_fallbacks = int(sys.argv[3]) if len(sys.argv) > 3 else BASELINE_FALLBACKS
    cm = json.load(open(live_path))
    src = cm["data"]["config.yaml"]
    cfg = yaml.safe_load(src)

    # the round-trip must be lossless before we touch anything, otherwise the
    # diff we review is not the diff we ship
    if yaml.safe_dump(cfg, sort_keys=True, default_flow_style=False,
                      allow_unicode=True, width=10 ** 9) != src:
        sys.exit("FATAL: YAML round-trip is not byte-identical; refusing to rewrite")

    ml = cfg["model_list"]
    rs = cfg["router_settings"]

    if len(rs["fallbacks"]) != expected_fallbacks:
        sys.exit(f"FATAL: fallbacks={len(rs['fallbacks'])}, expected {expected_fallbacks}")
    if len(rs["model_group_alias"]) != BASELINE_ALIASES:
        sys.exit(f"FATAL: model_group_alias={len(rs['model_group_alias'])}, expected {BASELINE_ALIASES}")

    for name, (expect, new) in REPOINT.items():
        lp = find(ml, name)["litellm_params"]
        for k, v in expect.items():
            if lp.get(k) != v:
                sys.exit(f"FATAL: {name}.{k} is {lp.get(k)!r}, expected {v!r} — 198 has drifted")
        lp.update(new)
        print(f"  repointed {name} -> {new['model']} @ {new['api_base']}")

    for name in CACHE_COST_GROUPS:
        entry = find(ml, name)
        # cost fields are written twice on purpose: LiteLLM skips register_model()
        # when model_info.id collides with a bundled price, so litellm_params alone
        # silently yields $0 spend forever
        for block in ("litellm_params", "model_info"):
            entry[block]["cache_read_input_token_cost"] = CACHE_READ
        print(f"  cache_read_input_token_cost={CACHE_READ} on {name} (both blocks)")

    existing = {json.dumps(f, sort_keys=True) for f in rs["fallbacks"]}
    for fb in NEW_FALLBACKS:
        if json.dumps(fb, sort_keys=True) in existing:
            print(f"  fallback already present, skipping: {fb}")
            continue
        rs["fallbacks"].append(fb)
        print(f"  + fallback {fb}")

    assert len(rs["fallbacks"]) == expected_fallbacks + len(NEW_FALLBACKS)
    assert len(rs["model_group_alias"]) == BASELINE_ALIASES, "model_group_alias must not change"

    out = yaml.safe_dump(cfg, sort_keys=True, default_flow_style=False,
                         allow_unicode=True, width=10 ** 9)
    if yaml.safe_load(out) != cfg:
        sys.exit("FATAL: re-parse of the patched YAML does not match the in-memory config")

    cm["data"]["config.yaml"] = out
    json.dump(cm, open(out_path, "w"), ensure_ascii=False)
    print(f"\nwrote {out_path}  (config.yaml {len(src)} -> {len(out)} bytes)")


if __name__ == "__main__":
    main()
