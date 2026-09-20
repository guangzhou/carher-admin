#!/usr/bin/env python3
"""Pod-side: dump every deployment behind the two new groups, decrypted, plus
resolve the two model ids that appeared in x-litellm-model-id. Reduces inside the
pod because /model/info is ~10MB and gets truncated crossing the jms hop.
"""
import json, os, urllib.request

BASE = "http://127.0.0.1:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
GROUPS = {"cursor-fc-opus-5", "cursor-fc-fable-5.1",
          "claude-opus-5", "claude-fable-5.1"}
HDR_IDS = {"1519252f8874", "30260603bbf6"}

r = urllib.request.Request(BASE + "/model/info",
    headers={"Authorization": "Bearer " + MK})
data = json.load(urllib.request.urlopen(r, timeout=115))["data"]

print("=== deployments in the watched groups ===")
for x in data:
    g = x.get("model_name")
    if g not in GROUPS:
        continue
    lp = x.get("litellm_params") or {}
    mid = str((x.get("model_info") or {}).get("id"))
    print(f"  group={g:22} id={mid:34} model={lp.get('model')!r} api_base={lp.get('api_base')!r}")

print("\n=== resolve the ids seen in x-litellm-model-id ===")
for x in data:
    mid = str((x.get("model_info") or {}).get("id"))
    if mid in HDR_IDS or any(mid.startswith(h) for h in HDR_IDS):
        lp = x.get("litellm_params") or {}
        print(f"  id={mid} group={x.get('model_name')!r} model={lp.get('model')!r} api_base={lp.get('api_base')!r}")

print("\n=== router settings: fallbacks touching these groups ===")
r = urllib.request.Request(BASE + "/config/list?config_type=general_settings",
    headers={"Authorization": "Bearer " + MK})
try:
    print("  (general_settings)", json.dumps(json.load(urllib.request.urlopen(r, timeout=60)))[:400])
except Exception as e:
    print("  general_settings unavailable:", e)
