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
#
# Kept in sync with the models measured on GET /backend-api/models (2026-07-27),
# see docs/zerokey-bridge/web-capability-inventory.md. Registering the pod-side
# catalogue alone is NOT enough: config/constants.js only feeds the pod's own
# /v1/models (advertising) -- a model is only reachable by a client once it has a
# LiteLLM model group here. That gap is exactly why gpt-5.3 was unusable despite
# the pod catalogue listing it.
#
# ⚠️ ALWAYS the PLAIN slug, NEVER the `-wm` variant. `-wm` (with-memory) triggers a
# conduit stream_handoff: the first response carries only a resume token and the
# body streams from an internal conduit our stateless replay cannot follow.
# Measured direct: gpt-5.6-sol = 17004 bytes with content, gpt-5.6-sol-wm = 973
# bytes with none. Via the pod both spellings happen to return content, so testing
# only through the pod will NOT catch this.
VARIANTS = {
    # 137k context -- cheap/fast tier, used for triage and light chat
    "5.3":         ("zerokey-pool-gpt-5.3",         "gpt-5.3"),
    "5.3-instant": ("zerokey-pool-gpt-5.3-instant", "gpt-5.3-instant"),
    "5.3-mini":    ("zerokey-pool-gpt-5.3-mini",    "gpt-5.3-mini"),
    "5.4":         ("zerokey-pool-gpt-5.4",         "gpt-5.4"),
    "5.4-t-mini":  ("zerokey-pool-gpt-5.4-t-mini",  "gpt-5.4-t-mini"),
    "5.5":         ("zerokey-pool-gpt-5.5",         "gpt-5.5"),
    "5.5-instant": ("zerokey-pool-gpt-5.5-instant", "gpt-5.5-instant"),
    "5.5-mini":    ("zerokey-pool-gpt-5.5-mini",    "gpt-5.5-mini"),
    # 410k context
    "5.5-thinking": ("zerokey-pool-gpt-5.5-thinking", "gpt-5.5-thinking"),
    "5.5-pro":      ("zerokey-pool-gpt-5.5-pro",      "gpt-5.5-pro"),
    "5.6-pro":      ("zerokey-pool-gpt-5.6-pro",      "gpt-5.6-pro"),
    # 262k context
    "5.6-thinking": ("zerokey-pool-gpt-5.6-thinking", "gpt-5.6-thinking"),
    "5.6-sol":      ("zerokey-pool-gpt-5.6-sol",      "gpt-5.6-sol"),
    "5.6-terra":    ("zerokey-pool-gpt-5.6-terra",    "gpt-5.6-terra"),
    "5.6-luna":     ("zerokey-pool-gpt-5.6-luna",     "gpt-5.6-luna"),
    # reasoning
    "o3":     ("zerokey-pool-o3",     "o3"),
    "o3-pro": ("zerokey-pool-o3-pro", "o3-pro"),
}
# 52 个在线 web pod(与 zk-image-patch 挂载者一致)。原列表停在 111,
# 漏掉了 112-121 / 127-131 / 49 / 81-86 —— 新增的模型组要覆盖全池,
# 否则新组只有部分 pod,容量远低于既有组。
DEFAULT_PODS = [25, 49, 81, 82, 83, 84, 85, 86,
                87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99,
                100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111,
                112, 113, 114, 115, 116, 117, 118, 119, 120, 121,
                127, 128, 129, 130, 131]


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
