#!/usr/bin/env python3
"""Register zerokey upstream models in 198 LiteLLM Pro (litellm-product).

Idempotent: adds/updates zerokey-* entries with use_chat_completions_api: true
(required for Codex 2026+ wire_api=responses). Does not touch non-zerokey models.

Usage (on 198, after jms ssh AIYJY-litellm):
  python3 litellm-register-zerokey.py            # dry-run
  python3 litellm-register-zerokey.py --apply    # replace cm + rollout restart
  python3 litellm-register-zerokey.py --apply --sync-manifest
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

NS = "litellm-product"
CM = "litellm-config"
MANIFEST = os.path.expanduser("~/litellm-product-manifests/30-cm-litellm-config.yaml")

# (litellm model_name, web slug, api_base)
ACCOUNTS = [
    (
        "",
        "8123",
        [
            ("zerokey-gpt-5.5", "gpt-5-5"),
            ("zerokey-gpt-5.5-thinking", "gpt-5-5-thinking"),
            ("zerokey-gpt-5.5-pro", "gpt-5-5-pro"),
            ("zerokey-o3", "o3"),
        ],
    ),
    (
        "timothy",
        "8124",
        [
            ("zerokey-timothy-gpt-5.5", "gpt-5-5"),
            ("zerokey-timothy-gpt-5.5-thinking", "gpt-5-5-thinking"),
            ("zerokey-timothy-gpt-5.5-pro", "gpt-5-5-pro"),
            ("zerokey-timothy-o3", "o3"),
        ],
    ),
]


def block(name: str, slug: str, port: str) -> str:
    return (
        f"- model_name: {name}\n"
        f"  litellm_params:\n"
        f"    model: openai/{slug}\n"
        f"    api_base: http://10.68.13.188:{port}/v1\n"
        f"    api_key: raw\n"
        f"    use_chat_completions_api: true\n"
        f"    input_cost_per_token: 0\n"
        f"    output_cost_per_token: 0"
    )


def desired_blocks() -> list[str]:
    out = []
    for _acct, port, models in ACCOUNTS:
        for name, slug in models:
            out.append(block(name, slug, port))
    return out


def splice_cfg(cfg: str, blocks: list[str]) -> str:
    lines = cfg.split("\n")
    # drop existing zerokey-* entries
    kept: list[str] = []
    skip = False
    for line in lines:
        if line.startswith("- model_name:") and "zerokey" in line:
            skip = True
            continue
        if skip:
            if line.startswith("- model_name:") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*:", line):
                skip = False
            else:
                continue
        if not skip:
            kept.append(line)
    # insert before router_settings:
    end = len(kept)
    for j, line in enumerate(kept):
        if re.match(r"^router_settings:", line):
            end = j
            break
    new_lines = kept[:end] + blocks + kept[end:]
    return "\n".join(new_lines)


def kubectl_json(args: list[str]) -> dict:
    raw = subprocess.check_output(["kubectl", *args])
    return json.loads(raw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sync-manifest", action="store_true", help="also patch manifest yaml on disk")
    args = ap.parse_args()

    blocks = desired_blocks()
    cm = kubectl_json(["get", "cm", CM, "-n", NS, "-o", "json"])
    old_cfg = cm["data"]["config.yaml"]
    new_cfg = splice_cfg(old_cfg, blocks)

    if old_cfg == new_cfg:
        print("zerokey model_list already up to date")
    else:
        print(f"will update {len(blocks)} zerokey entries (use_chat_completions_api: true)")
        if not args.apply:
            print("DRY-RUN — pass --apply to replace cm")
        else:
            ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            bdir = os.path.expanduser("~/zerokey-litellm-backups")
            os.makedirs(bdir, exist_ok=True)
            open(f"{bdir}/litellm-config-{ts}.json", "w").write(
                json.dumps(kubectl_json(["get", "cm", CM, "-n", NS, "-o", "json"]))
            )
            cm["data"]["config.yaml"] = new_cfg
            path = "/tmp/litellm-config-zerokey-new.json"
            open(path, "w").write(json.dumps(cm))
            subprocess.run(["kubectl", "replace", "-f", path], check=True)
            print("configmap replaced; rolling restart…")
            subprocess.run(
                ["kubectl", "rollout", "restart", f"deployment/litellm-proxy", "-n", NS],
                check=True,
            )

    if args.sync_manifest and os.path.isfile(MANIFEST):
        import yaml

        with open(MANIFEST) as f:
            doc = yaml.safe_load(f)
        inner = yaml.safe_load(doc["data"]["config.yaml"])
        inner_yaml = splice_cfg(yaml.dump(inner, sort_keys=False), blocks)
        doc["data"]["config.yaml"] = inner_yaml
        bak = MANIFEST + f".bak-zerokey-{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
        subprocess.run(["cp", MANIFEST, bak], check=True)
        with open(MANIFEST, "w") as f:
            yaml.dump(doc, f, sort_keys=False, allow_unicode=True)
        print(f"manifest synced: {MANIFEST} (backup {bak})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
