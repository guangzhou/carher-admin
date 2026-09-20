#!/usr/bin/env python3
"""Add GPT-6 Astra to live Aliyun ChatGPT acct and Her key config.

Run on k8s-work-226 with Aliyun kubeconfig. ConfigMap changes are dry-run by
 default; --apply backs up and updates both prod/canary model configs. Her
 per-instance ConfigMaps and keys are handled separately by the Her alignment
 script so existing defaults and aliases remain intact.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any

import yaml

MODEL = "gpt-6-astra"
MODEL_NAME = "chatgpt-gpt-6-astra"
# Real measured input window, not the official 1,050,000 total: that figure is
# total context, and the acct upstream reserves 128,000 for output. Aliyun's
# live config was governed to 922,000 (= 1,050,000 - 128,000) on 2026-09-05 by
# skill litellm-context-window-govern. Do not "restore" it to 1,050,000.
MAX_INPUT_TOKENS = 922000
MAX_OUTPUT_TOKENS = 128000
INPUT_COST = 1e-5
OUTPUT_COST = 5e-5
CACHE_READ_COST = 1e-6
CACHE_CREATE_COST = 1.25e-5
CACHE_READ_COST_ABOVE_272K = 2e-6
CACHE_READ_COST_ABOVE_272K_PRIORITY = 4e-6
CACHE_READ_COST_ABOVE_200K = None
CACHE_READ_COST_ABOVE_512K = None
CACHE_CREATE_COST_ABOVE_1HR = None


def cache_prices() -> dict[str, float | None]:
    """Astra cache rates. ``None`` = tier deliberately unset.

    Unset tiers must be *omitted* from the emitted config, never written as
    ``0.0``: a literal zero bills that tier as free.
    """
    return {
        "cache_read_input_token_cost": CACHE_READ_COST,
        "cache_creation_input_token_cost": CACHE_CREATE_COST,
        "cache_read_input_token_cost_above_200k_tokens": CACHE_READ_COST_ABOVE_200K,
        "cache_read_input_token_cost_above_272k_tokens": CACHE_READ_COST_ABOVE_272K,
        "cache_read_input_token_cost_above_272k_tokens_priority": CACHE_READ_COST_ABOVE_272K_PRIORITY,
        "cache_read_input_token_cost_above_512k_tokens": CACHE_READ_COST_ABOVE_512K,
        "cache_creation_input_token_cost_above_1hr": CACHE_CREATE_COST_ABOVE_1HR,
    }


def price_fields() -> dict[str, float]:
    """Cost fields for ``litellm_params`` -- the only block LiteLLM bills from."""
    fields = {"input_cost_per_token": INPUT_COST, "output_cost_per_token": OUTPUT_COST, **cache_prices()}
    return {k: v for k, v in fields.items() if v is not None}
CONFIGS = ("litellm-config", "litellm-config-canary")


def add_entries(config: dict[str, Any], accounts: list[str]) -> tuple[dict[str, Any], int]:
    out = copy.deepcopy(config)
    models = out.setdefault("model_list", [])
    existing = {(str((x.get("model_info") or {}).get("id")), x.get("model_name")) for x in models if isinstance(x, dict)}
    added = 0
    # Aliyun serves the bare user-facing name alongside the internal pool name
    # (same shape as the live gpt-5.6-sol rows); 198 registers only the
    # prefixed name and resolves the short name through per-key aliases.
    for exposed in (MODEL_NAME, MODEL):
        for n in accounts:
            mid = f"chatgpt-acct-{n}/{exposed}"
            if (mid, exposed) in existing:
                continue
            models.append({
                "model_name": exposed,
                "litellm_params": {
                    "model": f"openai/{MODEL_NAME}",
                    "api_base": f"http://chatgpt-acct-{n}.carher.svc:4000",
                    "api_key": "os.environ/CHATGPT_POOL_KEY",
                    **price_fields(),
                },
                # Cost fields are double-written (litellm_params + model_info) to
                # match live config, but only the litellm_params copy bills.
                "model_info": {
                    "mode": "responses",
                    "id": mid,
                    "base_model": MODEL,
                    "max_input_tokens": MAX_INPUT_TOKENS,
                    "max_output_tokens": MAX_OUTPUT_TOKENS,
                    **{k: v for k, v in cache_prices().items() if v is not None},
                },
            })
            added += 1
    return out, added


def validate(config: dict[str, Any], accounts: list[str]) -> list[str]:
    errors = []
    for exposed in (MODEL_NAME, MODEL):
        rows = [x for x in config.get("model_list", []) if isinstance(x, dict) and x.get("model_name") == exposed]
        by_id = {(x.get("model_info") or {}).get("id"): x for x in rows}
        for n in accounts:
            mid = f"chatgpt-acct-{n}/{exposed}"
            row = by_id.get(mid)
            if not row:
                errors.append(f"missing {mid}")
                continue
            p, i = row.get("litellm_params") or {}, row.get("model_info") or {}
            if p.get("model") != f"openai/{MODEL_NAME}" or p.get("api_base") != f"http://chatgpt-acct-{n}.carher.svc:4000":
                errors.append(f"route mismatch {mid}")
            if i.get("mode") != "responses" or i.get("base_model") != MODEL:
                errors.append(f"metadata mismatch {mid}")
            # Cost fields only bill from litellm_params; model_info alone is silently ignored.
            for field, want in price_fields().items():
                if want is not None and p.get(field) != want:
                    errors.append(f"price mismatch {mid}:{field}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("accounts", nargs="+", help="verified numeric account IDs")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--input-dir", default="/tmp/aliyun-gpt6-configs")
    parser.add_argument("--output-dir", default="/tmp/aliyun-gpt6-configs-patched")
    args = parser.parse_args()
    accounts = [str(int(x.removeprefix("acct-"))) for x in args.accounts]
    in_dir, out_dir = Path(args.input_dir), Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in CONFIGS:
        src = in_dir / f"{name}.yaml"
        if not src.exists():
            raise SystemExit(f"missing snapshot {src}")
        current = yaml.safe_load(src.read_text()) or {}
        desired, added = add_entries(current, accounts)
        errors = validate(desired, accounts)
        if errors:
            raise SystemExit("; ".join(errors))
        (out_dir / f"{name}.yaml").write_text(yaml.safe_dump(desired, sort_keys=False, allow_unicode=True))
        rows = [x for x in desired.get("model_list", []) if isinstance(x, dict) and x.get("model_name") in (MODEL_NAME, MODEL)]
        print(f"{name}: added={added} astra_rows={len(rows)} (internal+bare)")
    print("dry-run only; apply snapshots with the existing Aliyun ConfigMap rollout procedure" if not args.apply else "prepared patched snapshots; apply explicitly after review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
