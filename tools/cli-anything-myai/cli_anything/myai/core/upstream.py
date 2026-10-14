"""Upstream LiteLLM probes executed inside the usage-sync-worker pod.

The pod already holds LITELLM_* (backend ``primary``) and HER_LITELLM_* (backend
``her``) credentials, so nothing secret crosses to this machine: we send a fixed
script on stdin and read back JSON. Only GET endpoints are called.
"""

from __future__ import annotations

import json
from datetime import date, timedelta


BACKEND_ENV = {
    "primary": ("LITELLM_BASE_URL", "LITELLM_ADMIN_KEY"),
    "her": ("HER_LITELLM_BASE_URL", "HER_LITELLM_ADMIN_KEY"),
}

# Runs in the pod (python 3.12 + httpx from requirements.txt). Params arrive as
# the first line of stdin-script via the PARAMS constant substituted below.
_DAILY_SCRIPT = r'''
import json, os, sys
import httpx
PARAMS = json.loads(__PARAMS__)
out = {"backends": {}}
for name, (url_env, key_env) in PARAMS["backends"].items():
    base, key = os.environ.get(url_env, ""), os.environ.get(key_env, "")
    entry = {"baseUrl": base, "configured": bool(base and key), "days": []}
    out["backends"][name] = entry
    if not entry["configured"]:
        continue
    client = httpx.Client(base_url=base, headers={"Authorization": "Bearer " + key}, timeout=120)
    for day in PARAMS["days"]:
        row = {"date": day}
        try:
            r = client.get("/user/daily/activity/aggregated", params={"start_date": day, "end_date": day})
            row["status"] = r.status_code
            md = (r.json() or {}).get("metadata") or {} if r.status_code == 200 else {}
            row["requests"] = md.get("total_api_requests")
            row["spend"] = round(float(md.get("total_spend") or 0), 4)
        except Exception as exc:
            row["error"] = exc.__class__.__name__
        entry["days"].append(row)
print(json.dumps(out))
'''


# Global per-key rollup (no user_id): which keys carry spend, and whether
# their owner is a LiteLLM_UserTable user. Keys with an empty or unknown owner
# are invisible to the dashboard's per-user fallback unless key rollup is on.
_KEYS_SCRIPT = r'''
import json, os
import httpx
PARAMS = json.loads(__PARAMS__)
base, key = os.environ.get("LITELLM_BASE_URL", ""), os.environ.get("LITELLM_ADMIN_KEY", "")
c = httpx.Client(base_url=base, headers={"Authorization": "Bearer " + key}, timeout=180)
users, page = set(), 1
while page <= 100:
    j = c.get("/user/list", params={"page": page, "page_size": 100, "sort_by": "user_id", "sort_order": "asc"}).json()
    users.update(str(u.get("user_id") or "") for u in j.get("users") or [])
    if page >= int(j.get("total_pages") or 1):
        break
    page += 1
keys, totals, page, pages = {}, {"requests": 0, "spend": 0.0}, 1, 1
while page <= pages and page <= 50:
    j = c.get("/user/daily/activity", params={"start_date": PARAMS["start"], "end_date": PARAMS["end"], "page": page, "page_size": 1000}).json()
    md = j.get("metadata") or {}
    pages = int(md.get("total_pages") or 1)
    totals["requests"] += int(md.get("total_api_requests") or 0)
    totals["spend"] += float(md.get("total_spend") or 0)
    for day in j.get("results") or []:
        for sha, v in ((day.get("breakdown") or {}).get("api_keys") or {}).items():
            m, meta = v.get("metrics") or {}, v.get("metadata") or {}
            e = keys.setdefault(sha, {"alias": meta.get("key_alias") or "", "owner": meta.get("user_id") or "", "exists": meta.get("key_exists", True), "requests": 0, "spend": 0.0})
            e["requests"] += int(m.get("api_requests") or 0)
            e["spend"] += float(m.get("spend") or 0)
    page += 1
out = []
for sha, e in keys.items():
    cls = "no_owner" if not e["owner"] else ("known" if e["owner"] in users else "owner_not_in_usertable")
    if not e["exists"]:
        cls = "deleted_key"
    out.append({"keyId": sha[:12], "alias": e["alias"], "owner": e["owner"], "class": cls, "requests": e["requests"], "spend": round(e["spend"], 2)})
out.sort(key=lambda r: -r["spend"])
print(json.dumps({"start": PARAMS["start"], "end": PARAMS["end"], "users": len(users), "totals": {k: round(v, 2) for k, v in totals.items()}, "keys": out}))
'''


def keys_script(start: str, end: str) -> str:
    return _KEYS_SCRIPT.replace("__PARAMS__", repr(json.dumps({"start": start, "end": end})))


def summarize_keys(data: dict, top: int = 15) -> dict:
    by_class: dict[str, dict] = {}
    for row in data.get("keys") or []:
        bucket = by_class.setdefault(row["class"], {"keys": 0, "requests": 0, "spend": 0.0})
        bucket["keys"] += 1
        bucket["requests"] += row["requests"]
        bucket["spend"] += row["spend"]
    for bucket in by_class.values():
        bucket["spend"] = round(bucket["spend"], 2)
    gap = [r for r in data.get("keys") or [] if r["class"] != "known" and (r["requests"] or r["spend"])]
    return {
        "window": f"{data.get('start')}..{data.get('end')}",
        "upstreamTotals": data.get("totals"),
        "userTableSize": data.get("users"),
        "byClass": by_class,
        "invisibleToPerUserSync": gap[:top],
    }


def day_list(days: int, end: date | None = None) -> list[str]:
    end = end or date.today()
    days = max(1, min(90, int(days)))
    return [(end - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def daily_activity_script(backends: list[str], days: list[str]) -> str:
    unknown = [name for name in backends if name not in BACKEND_ENV]
    if unknown:
        raise ValueError(f"未知 backend: {unknown}(可选 {sorted(BACKEND_ENV)})")
    params = {"backends": {name: BACKEND_ENV[name] for name in backends}, "days": days}
    return _DAILY_SCRIPT.replace("__PARAMS__", repr(json.dumps(params)))


def parse_script_output(stdout: str) -> dict:
    """The script prints exactly one JSON line last; ignore any warnings before it."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError(f"上游探针没有返回 JSON: {stdout[-300:]!r}")
