#!/usr/bin/env python3
"""register-web-pool.py — register 225 web-only zerokey pods into LiteLLM
zerokey-pool-gpt-5.5 / 5.6-{sol,terra,luna} groups on 198 prod.

Web pods serve 5.5 (default) + 5.6 sol/terra/luna natively via PLAIN slugs
(the pod's raw.js passes gpt-5.6-{sol,terra,luna} through verbatim; NEVER -wm).

Usage:
  python3 register-web-pool.py                       # all pods, all variants
  python3 register-web-pool.py --variants 5.6-sol    # only one group
  python3 register-web-pool.py --pods 25 87 88       # only these pods
  python3 register-web-pool.py --delete              # remove zk-N-* web entries
  LITELLM_MK=sk-... python3 register-web-pool.py

Run from anywhere that can reach 198:30402 (188 or local).
"""
import json, os, sys, urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30402")
MK = os.environ.get("LITELLM_MK", "sk-pro-litellm-ce077e2b0721bb419a633e4d")

# variant -> (litellm model_name group, plain web slug sent as openai/<slug>)
VARIANTS = {
    "5.5":       ("zerokey-pool-gpt-5.5",       "gpt-5.5"),
    "5.6-sol":   ("zerokey-pool-gpt-5.6-sol",   "gpt-5.6-sol"),
    "5.6-terra": ("zerokey-pool-gpt-5.6-terra", "gpt-5.6-terra"),
    "5.6-luna":  ("zerokey-pool-gpt-5.6-luna",  "gpt-5.6-luna"),
}
DEFAULT_PODS = [25, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99]


def api(method, path, data=None):
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=body,
        headers={"Authorization": f"Bearer {MK}", "Content-Type": "application/json"},
        method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__); return
    delete = "--delete" in args
    pods = DEFAULT_PODS
    variants = list(VARIANTS)
    if "--pods" in args:
        i = args.index("--pods"); pods = [int(x) for x in args[i + 1:] if x.isdigit()]
    if "--variants" in args:
        i = args.index("--variants"); variants = [x for x in args[i + 1:] if x in VARIANTS]

    for v in variants:
        group, slug = VARIANTS[v]
        ok = 0
        for n in pods:
            mid = f"zk-{n}-gpt-{v}"
            if delete:
                try: api("POST", "/pro/model/delete", {"id": mid}); ok += 1
                except Exception as e: print(f"  {mid} del FAIL {str(e)[:60]}")
                continue
            entry = {
                "model_name": group,
                "litellm_params": {
                    "model": f"openai/{slug}",
                    "api_base": f"http://zero-{n}.litellm-product.svc.cluster.local:8200/v1",
                    "api_key": "raw", "rpm": 30,
                    "input_cost_per_token": 5e-6, "output_cost_per_token": 3e-5,
                },
                "model_info": {"id": mid, "mode": "responses"},
            }
            try: api("POST", "/pro/model/new", entry); ok += 1
            except Exception as e: print(f"  {mid} FAIL {str(e)[:60]}")
        print(f"{group}: {'deleted' if delete else 'registered'} {ok}/{len(pods)}")


if __name__ == "__main__":
    main()
