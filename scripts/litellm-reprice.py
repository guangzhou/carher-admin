#!/usr/bin/env python3
"""
litellm-reprice: spec-driven repricing of k8s/litellm-proxy.yaml.

Handles two ops:
  - multiply: scale existing cost fields by a factor (in-place)
  - add:      insert new cost fields after an anchor line under matching model

Designed around two hard-learned constraints:
  - Multiple model_names share the same cost VALUE (e.g. Opus input 0.000005
    == Haiku output 0.000005), so naive sed/replace_all is forbidden. We track
    model_name context line-by-line.
  - LiteLLM cost calc reads four fields: input_cost_per_token,
    output_cost_per_token, cache_read_input_token_cost,
    cache_creation_input_token_cost. Forgetting cache_* breaks the
    cache-discount ratio after a price change.

Spec format (YAML):

  rules:
    - desc: "Claude *2"
      match: "(?i)(claude|opus|sonnet|haiku)"   # regex on model_name
      op: multiply
      factor: 2.0
      fields:                                    # optional, default all 4
        - input_cost_per_token
        - output_cost_per_token
        - cache_read_input_token_cost
        - cache_creation_input_token_cost
    - desc: "chatgpt-gpt-5.5 = OpenAI list / 5"
      match: "^chatgpt-gpt-5\\.5$"
      op: add
      insert_after: api_key
      values:
        input_cost_per_token: "0.000001"
        output_cost_per_token: "0.000006"

Usage:
  python scripts/litellm-reprice.py SPEC.yaml [--target k8s/litellm-proxy.yaml]
                                              [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML missing. install: pip3 install pyyaml")

MODEL_NAME_RE = re.compile(r"^(\s*)-\s*model_name:\s*(\S+)\s*$")
FIELD_LINE_RE = re.compile(r"^(\s*)(\w+):\s*([0-9.eE+-]+)\s*$")
ALL_COST_FIELDS = {
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
}


def fmt_float(x: float) -> str:
    s = f"{x:.10f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return s


def compile_rules(rules: list[dict]) -> list[dict]:
    out = []
    for r in rules:
        if "match" not in r:
            sys.exit(f"rule missing 'match': {r}")
        op = r.get("op")
        if op not in ("multiply", "add"):
            sys.exit(f"rule op must be multiply|add, got {op!r}: {r}")
        compiled = {
            "desc": r.get("desc", r["match"]),
            "re": re.compile(r["match"]),
            "op": op,
        }
        if op == "multiply":
            if "factor" not in r:
                sys.exit(f"multiply rule missing 'factor': {r}")
            compiled["factor"] = float(r["factor"])
            compiled["fields"] = set(r.get("fields", list(ALL_COST_FIELDS)))
        else:  # add
            if "values" not in r:
                sys.exit(f"add rule missing 'values': {r}")
            compiled["values"] = {k: str(v) for k, v in r["values"].items()}
            compiled["anchor"] = r.get("insert_after", "api_key")
        out.append(compiled)
    return out


def apply(target_path: Path, rules: list[dict], dry_run: bool) -> dict:
    lines = target_path.read_text().splitlines(keepends=True)
    out: list[str] = []
    current_model: str | None = None
    stats = {"multiplied": 0, "added": 0, "hits": {}}
    anchor_pat_cache: dict[str, re.Pattern] = {}

    for line in lines:
        m = MODEL_NAME_RE.match(line)
        if m:
            current_model = m.group(2)

        # multiply: replace value of matching cost field
        emitted = False
        if current_model:
            fm = FIELD_LINE_RE.match(line)
            if fm and fm.group(2) in ALL_COST_FIELDS:
                field = fm.group(2)
                indent, val = fm.group(1), fm.group(3)
                for r in rules:
                    if r["op"] != "multiply":
                        continue
                    if field not in r["fields"]:
                        continue
                    if r["re"].search(current_model):
                        new = float(val) * r["factor"]
                        out.append(f"{indent}{field}: {fmt_float(new)}\n")
                        stats["multiplied"] += 1
                        stats["hits"][r["desc"]] = stats["hits"].get(r["desc"], 0) + 1
                        emitted = True
                        break

        if not emitted:
            out.append(line)

        # add: insert new fields after anchor line under matching model
        if current_model:
            for r in rules:
                if r["op"] != "add":
                    continue
                if not r["re"].search(current_model):
                    continue
                anchor = r["anchor"]
                anchor_re = anchor_pat_cache.setdefault(
                    anchor, re.compile(rf"^(\s*){re.escape(anchor)}:\s*\S")
                )
                if anchor_re.match(line):
                    indent = re.match(r"(\s*)", line).group(1)
                    for k, v in r["values"].items():
                        out.append(f"{indent}{k}: {v}\n")
                        stats["added"] += 1
                    stats["hits"][r["desc"]] = stats["hits"].get(r["desc"], 0) + 1

    if not dry_run:
        target_path.write_text("".join(out))

    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("spec", help="path to YAML spec file")
    ap.add_argument(
        "--target",
        default="k8s/litellm-proxy.yaml",
        help="LiteLLM proxy ConfigMap yaml (default: k8s/litellm-proxy.yaml)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print summary only, don't modify target",
    )
    args = ap.parse_args()

    spec_path = Path(args.spec)
    target_path = Path(args.target)
    if not spec_path.exists():
        sys.exit(f"spec not found: {spec_path}")
    if not target_path.exists():
        sys.exit(f"target not found: {target_path}")

    spec = yaml.safe_load(spec_path.read_text()) or {}
    rules = compile_rules(spec.get("rules", []))
    if not rules:
        sys.exit("spec has no rules")

    stats = apply(target_path, rules, dry_run=args.dry_run)

    tag = "[dry-run] would" if args.dry_run else ""
    print(f"{tag} multiply {stats['multiplied']} cost lines, add {stats['added']} new lines".strip())
    print("rule hits:")
    for desc, cnt in stats["hits"].items():
        print(f"  {desc}: {cnt}")

    if not args.dry_run:
        print(f"\nREVIEW diff before apply:  git diff {target_path}")
        print("THEN deploy:               scripts/litellm-reprice-deploy.sh")


if __name__ == "__main__":
    main()
