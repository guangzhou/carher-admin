"""chatgpt-acct-probe — 阿里云 carher ns 内部的探针 service.

acct-admin (188) 通过 cloudflared 公网 pull 这个 service 拿全 pool 状态:
  GET /probe          → 5min 内部缓存
  GET /probe?live=1   → 强制现场探一次
  GET /healthz        → 仅返回缓存元数据 (无 ssh, 用于 K8s probe)

数据来源跟 scripts/chatgpt_acct_quota_aliyun_view.py 一致:
  1. K8s API list pod (labelSelector=pool=chatgpt-acct)
  2. kubectl exec pod -- cat /chatgpt-auth/auth.json
  3. kubectl exec pod -- python3 -c <USAGE_PROBE>
  4. kubectl exec litellm-db-0 -- psql LiteLLM_SpendLogs

跑在 carher ns 内, 用 in-cluster SA + ServiceAccount token.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from kubernetes import client, config
from kubernetes.stream import stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aliyun-probe")

NS = os.environ.get("PROBE_NS", "carher")
POD_LABEL = os.environ.get("PROBE_POD_LABEL", "pool=chatgpt-acct")
DB_POD = os.environ.get("PROBE_DB_POD", "litellm-db-0")
DB_USER = os.environ.get("PROBE_DB_USER", "litellm")
DB_NAME = os.environ.get("PROBE_DB_NAME", "litellm")
DB_PWD = os.environ.get("PROBE_DB_PWD", "")
AUTH_PATH = "/chatgpt-auth/auth.json"

# bearer token to authenticate the caller (acct-admin)
EXPECTED_BEARER = os.environ.get("PROBE_BEARER", "")

# refresh cadence + bounds
CACHE_TTL_SEC = int(os.environ.get("PROBE_CACHE_TTL", "300"))  # 5min
LIVE_THROTTLE_SEC = int(os.environ.get("PROBE_LIVE_THROTTLE", "30"))
EXEC_TIMEOUT_SEC = int(os.environ.get("PROBE_EXEC_TIMEOUT", "25"))
WORKERS = int(os.environ.get("PROBE_WORKERS", "4"))

USAGE_PROBE_CODE = r'''
import json, urllib.request, urllib.error, sys
try:
    with open("/chatgpt-auth/auth.json") as f:
        a = json.load(f)
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/usage",
        headers={
            "Authorization": "Bearer " + a["access_token"],
            "ChatGPT-Account-ID": a.get("account_id", ""),
            "Originator": "codex_cli_rs",
            "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as e:
    body = e.read().decode(errors="ignore")[:400]
    sys.stdout.write(json.dumps({"_err": e.code, "_body": body}))
except Exception as e:
    sys.stdout.write(json.dumps({"_err": -1, "_body": str(e)[:200]}))
'''


# ---------- k8s client ----------

config.load_incluster_config()
_core_v1 = client.CoreV1Api()


def _exec_pod(pod: str, command: list[str], timeout: int = EXEC_TIMEOUT_SEC) -> str:
    """kubectl exec equivalent. Returns stdout (stderr discarded).

    Wraps the command so stdout is base64 on the wire — the kubernetes stream
    helper otherwise mangles non-ASCII / quote-heavy text (we observed JSON
    coming back rendered as Python repr with single quotes).
    """
    import shlex
    quoted = " ".join(shlex.quote(c) for c in command)
    wrapped = ["sh", "-c", f"({quoted}) | base64 -w0 2>/dev/null || ({quoted}) | base64 | tr -d '\\n'"]
    resp = stream(
        _core_v1.connect_get_namespaced_pod_exec,
        pod, NS,
        command=wrapped,
        stderr=False, stdin=False, stdout=True, tty=False,
        _request_timeout=timeout,
    )
    try:
        return base64.b64decode((resp or "").strip()).decode("utf-8", errors="replace")
    except Exception:
        return resp or ""


# ---------- shared helpers (mirror chatgpt_acct_quota_aliyun_view.py) ----------

def claims_from_id_token(auth: dict[str, Any]) -> dict[str, Any]:
    token = auth.get("id_token") or ""
    if token.count(".") < 2:
        return {}
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment))
    except Exception:
        return {}


def email_from_auth(auth: dict[str, Any]) -> str:
    return claims_from_id_token(auth).get("email") or ""


def subscription_info(auth: dict[str, Any]) -> tuple[str, float | None]:
    claims = claims_from_id_token(auth)
    oai = claims.get("https://api.openai.com/auth") or {}
    plan = oai.get("chatgpt_plan_type") or ""
    until_raw = oai.get("chatgpt_subscription_active_until")
    until_ts: float | None = None
    if until_raw:
        try:
            until_ts = datetime.fromisoformat(
                str(until_raw).replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            until_ts = None
    return plan, until_ts


def list_pods() -> list[dict[str, Any]]:
    resp = _core_v1.list_namespaced_pod(NS, label_selector=POD_LABEL)
    out = []
    for it in resp.items:
        labels = it.metadata.labels or {}
        app = labels.get("app", "")
        if not app.startswith("chatgpt-acct-"):
            continue
        cs = (it.status.container_statuses or [None])[0]
        out.append({
            "acct": app.replace("chatgpt-", "", 1),
            "pod": it.metadata.name,
            "phase": it.status.phase,
            "ready": bool(cs.ready) if cs else False,
            "restarts": int(cs.restart_count) if cs else 0,
            "started_at": it.metadata.creation_timestamp.isoformat() if it.metadata.creation_timestamp else None,
        })
    return out


def probe_auth(pod: str) -> dict[str, Any]:
    info = {
        "email": "", "expires_at": None, "plan": "", "sub_until": None,
        "p5h": None, "p7d": None, "p_reset": None, "w_reset": None,
        "codex_5h": None, "codex_7d": None,
        "probe_err": None,
    }
    try:
        raw = _exec_pod(pod, ["cat", AUTH_PATH])
        auth = json.loads(raw)
        plan, sub_until = subscription_info(auth)
        info.update({
            "email": email_from_auth(auth),
            "expires_at": auth.get("expires_at"),
            "plan": plan,
            "sub_until": sub_until,
        })
    except Exception as e:
        log.warning("probe_auth %s cat auth failed: %s", pod, e)
        return info

    try:
        raw = _exec_pod(pod, ["python3", "-c", USAGE_PROBE_CODE])
        usage = json.loads(raw)
        if usage.get("_err") is not None:
            body = (usage.get("_body") or "")
            if "token_invalidated" in body or usage["_err"] == 401:
                info["probe_err"] = "token_invalidated"
            else:
                info["probe_err"] = f"HTTP {usage['_err']}"
        else:
            rl = usage.get("rate_limit") or {}
            pw = rl.get("primary_window") or {}
            sw = rl.get("secondary_window") or {}
            info["p5h"] = pw.get("used_percent")
            info["p7d"] = sw.get("used_percent")
            info["p_reset"] = pw.get("reset_at")
            info["w_reset"] = sw.get("reset_at")
            for extra in usage.get("additional_rate_limits") or []:
                if "codex" in (extra.get("limit_name") or "").lower():
                    erl = extra.get("rate_limit") or {}
                    info["codex_5h"] = (erl.get("primary_window") or {}).get("used_percent")
                    info["codex_7d"] = (erl.get("secondary_window") or {}).get("used_percent")
                    break
    except Exception as e:
        log.warning("probe_auth %s usage failed: %s", pod, e)
    return info


def gather_auth(pods: list[dict[str, Any]]) -> None:
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(probe_auth, p["pod"]): p for p in pods}
        for fut in futs:
            p = futs[fut]
            try:
                p.update(fut.result(timeout=EXEC_TIMEOUT_SEC + 5))
            except Exception as e:
                log.warning("gather_auth %s timeout/err: %s", p["pod"], e)


def spend_window(hours: int) -> dict[str, dict[str, float]]:
    if not DB_PWD:
        return {}
    sql = (
        "SELECT split_part(model_id, '/', 1) AS acct, "
        "COUNT(*) AS n, ROUND(SUM(spend)::numeric, 2) AS spend "
        "FROM \\\"LiteLLM_SpendLogs\\\" "
        f"WHERE model_id LIKE 'chatgpt-acct-%/%' "
        f"AND \\\"startTime\\\" > NOW() - INTERVAL '{hours} hours' "
        "GROUP BY acct;"
    )
    try:
        raw = _exec_pod(
            DB_POD,
            ["bash", "-c",
             f"PGPASSWORD={DB_PWD} psql -U {DB_USER} -d {DB_NAME} -A -F'|' -t -c \"{sql}\""],
        )
    except Exception as e:
        log.warning("spend_window(%dh) failed: %s", hours, e)
        return {}
    out: dict[str, dict[str, float]] = {}
    for line in raw.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        acct = parts[0].replace("chatgpt-", "", 1)
        try:
            out[acct] = {"calls": int(parts[1]), "spend": float(parts[2])}
        except ValueError:
            continue
    return out


def acct_sort_key(acct: str) -> int:
    try:
        return int(acct.split("-", 1)[1])
    except Exception:
        return 10**9


def gather_all() -> dict[str, Any]:
    t0 = time.time()
    pods = list_pods()
    gather_auth(pods)
    s5 = spend_window(5)
    s24 = spend_window(24)
    rows = []
    for p in sorted(pods, key=lambda x: acct_sort_key(x["acct"])):
        a = p["acct"]
        rows.append({
            **p,
            "pool": "aliyun",
            "spend_5h": s5.get(a, {"calls": 0, "spend": 0.0}),
            "spend_24h": s24.get(a, {"calls": 0, "spend": 0.0}),
        })
    return {
        "rows": rows,
        "gathered_at": t0,
        "duration_sec": round(time.time() - t0, 2),
    }


# ---------- cache + background refresher ----------

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"data": None, "at": 0.0, "err": None, "last_live_at": 0.0}


def _refresh_into_cache() -> dict[str, Any]:
    """Synchronous refresh. Writes cache. Returns data."""
    try:
        data = gather_all()
        with _cache_lock:
            _cache["data"] = data
            _cache["at"] = time.time()
            _cache["err"] = None
        return data
    except Exception as e:
        log.exception("refresh failed")
        with _cache_lock:
            _cache["err"] = str(e)[:300]
        raise


def _bg_refresher() -> None:
    """Background loop: refresh every CACHE_TTL_SEC."""
    while True:
        try:
            _refresh_into_cache()
            log.info("bg refresh ok (cached %d rows)", len(_cache["data"]["rows"]) if _cache["data"] else 0)
        except Exception as e:
            log.warning("bg refresh err: %s", e)
        time.sleep(CACHE_TTL_SEC)


# ---------- FastAPI ----------

app = FastAPI(title="chatgpt-acct-probe", version="0.1.0")


def _check_bearer(authorization: str | None) -> None:
    if not EXPECTED_BEARER:
        # not configured → fail closed
        raise HTTPException(status_code=503, detail="bearer not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    if authorization.split(" ", 1)[1].strip() != EXPECTED_BEARER:
        raise HTTPException(status_code=401, detail="invalid bearer")


@app.on_event("startup")
async def _startup():
    t = threading.Thread(target=_bg_refresher, daemon=True, name="bg-refresher")
    t.start()


@app.get("/healthz")
def healthz():
    with _cache_lock:
        return {
            "status": "ok" if _cache["data"] is not None else "warming",
            "cached_at": _cache["at"] or None,
            "cached_rows": len(_cache["data"]["rows"]) if _cache["data"] else 0,
            "last_error": _cache["err"],
            "ttl_sec": CACHE_TTL_SEC,
        }


@app.get("/probe")
def probe(
    live: int = Query(0, description="1 to force a fresh probe (subject to throttle)"),
    authorization: str | None = Header(default=None),
):
    _check_bearer(authorization)
    now = time.time()
    if live:
        with _cache_lock:
            since_last_live = now - _cache["last_live_at"]
            if since_last_live < LIVE_THROTTLE_SEC:
                wait = round(LIVE_THROTTLE_SEC - since_last_live, 1)
                raise HTTPException(
                    status_code=429,
                    detail=f"live refresh throttled, retry in {wait}s",
                )
            _cache["last_live_at"] = now
        data = _refresh_into_cache()
        return {**data, "served_from": "live"}
    with _cache_lock:
        data = _cache["data"]
        cached_at = _cache["at"]
        err = _cache["err"]
    if data is None:
        raise HTTPException(status_code=503, detail=f"cache warming up; last_err={err}")
    return {
        **data,
        "served_from": "cache",
        "cache_age_sec": round(now - cached_at, 1) if cached_at else None,
    }
