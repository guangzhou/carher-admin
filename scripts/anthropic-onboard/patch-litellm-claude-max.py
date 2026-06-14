#!/usr/bin/env python3
"""
Add or refresh `claude-max-{opus,sonnet,haiku}` model entries in a LiteLLM
ConfigMap (carher namespace).

Usage:
  python3 patch-litellm-claude-max.py prod    # → litellm-config
  python3 patch-litellm-claude-max.py canary  # → litellm-config-canary
  python3 patch-litellm-claude-max.py dev     # → litellm-config-dev (if exists)

Idempotent: removes any existing claude-max-* entries first, then inserts the
3 fresh ones pointing at http://172.16.0.86:3456/v1.

After apply, run:
  kubectl rollout restart deployment/litellm-proxy[-canary] -n carher
  kubectl rollout status  deployment/litellm-proxy[-canary] -n carher
"""
import subprocess, yaml, json, sys

PROXY_API_BASE = "http://172.16.0.86:3456/v1"

ENV_TO_CM = {
    "prod":   "litellm-config",
    "canary": "litellm-config-canary",
    "dev":    "litellm-config-dev",
}

ENTRIES = [
    {
        "model_name": "claude-max-opus",
        "litellm_params": {
            "model": "openai/claude-opus-4-7",
            "api_base": PROXY_API_BASE,
            "api_key": "no-auth",
            "input_cost_per_token": 0.000005,
            "output_cost_per_token": 0.000025,
        },
        "model_info": {"mode": "chat"},
    },
    {
        "model_name": "claude-max-sonnet",
        "litellm_params": {
            "model": "openai/claude-sonnet-4-6",
            "api_base": PROXY_API_BASE,
            "api_key": "no-auth",
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
        },
        "model_info": {"mode": "chat"},
    },
    {
        "model_name": "claude-max-haiku",
        "litellm_params": {
            "model": "openai/claude-haiku-4-5",
            "api_base": PROXY_API_BASE,
            "api_key": "no-auth",
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000005,
        },
        "model_info": {"mode": "chat"},
    },
]


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ENV_TO_CM:
        sys.exit(f"usage: {sys.argv[0]} {{prod|canary|dev}}")
    env = sys.argv[1]
    cm_name = ENV_TO_CM[env]

    out = subprocess.check_output(
        ["kubectl", "get", "cm", cm_name, "-n", "carher", "-o", "json"])
    cm = json.loads(out)
    cfg = yaml.safe_load(cm["data"]["config.yaml"])

    cfg["model_list"] = [m for m in cfg["model_list"]
                         if not m["model_name"].startswith("claude-max-")]
    cfg["model_list"].extend(ENTRIES)

    for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
        cm["metadata"].pop(k, None)
    if "annotations" in cm["metadata"]:
        cm["metadata"]["annotations"].pop(
            "kubectl.kubernetes.io/last-applied-configuration", None)
    cm["data"]["config.yaml"] = yaml.dump(
        cfg, sort_keys=False, allow_unicode=True, width=10000)

    sys.stdout.write(json.dumps(cm))
    print(f"# patched ConfigMap → {cm_name}", file=sys.stderr)
    print("# pipe stdout into:  kubectl apply -f -", file=sys.stderr)


if __name__ == "__main__":
    main()
