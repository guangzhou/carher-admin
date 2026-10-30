"""Gemini-FastAPI 1731445 carher patch p4: per-account status endpoint for the 198:8796 console.

GET /carher/status (same api_key as /v1/*) -> {"clients": [{id, running, account_status, cooldown_until,
    proxy_host, requests, ok, fail, last_used, last_error, usage}], "started": ts}
- proxy_host: host:port only, never the credential part of the proxy URL.
- requests/ok/fail/last_used/last_error: counted in-process per client id (reset on restart).
  Counted in _send_with_internal_fallback, the single place every chat/responses call sends through.
- usage: gemini_webapi's own usage_info snapshot (remaining credits / usage % / reset per window), no extra call.

Applied after p1..p3.2. Idempotent (refuses to apply twice).
"""
SRC = "/root/gemini-web-build/src/app"

# ---- pool.py: counters --------------------------------------------------------
p = f"{SRC}/services/pool.py"
s = open(p).read()
assert "carher p4" not in s, "already patched"
old = "        # carher p2: client id -> unix ts until which it is skipped\n        self._cooldown: dict[str, float] = {}\n"
assert s.count(old) == 1
s = s.replace(old, old + "        # carher p4: client id -> {requests, ok, fail, last_used, last_error}\n        self._stats: dict[str, dict] = {}\n        self._started = time.time()\n", 1)
old = "    # carher p2 ---------------------------------------------------------------\n"
assert s.count(old) == 1
s = s.replace(old, '''    # carher p4 ---------------------------------------------------------------
    def record(self, client_id: str, ok: bool, error: str | None = None) -> None:
        st = self._stats.setdefault(client_id, {"requests": 0, "ok": 0, "fail": 0, "last_used": None, "last_error": None})
        st["requests"] += 1
        st["ok" if ok else "fail"] += 1
        st["last_used"] = time.time()
        if not ok:
            st["last_error"] = (error or "")[:200]

    def carher_status(self) -> dict:
        from urllib.parse import urlsplit

        now = time.time()
        out = []
        for c in self._clients:
            px = getattr(c, "proxy", None) or ""
            try:
                u = urlsplit(px) if px else None
                host = ("%s:%s" % (u.hostname, u.port)) if u and u.hostname else ""
            except Exception:
                host = "?"
            try:
                usage = dict((c.quotas or {}).get("usage_info") or {})
            except Exception:
                usage = {}
            st = self._stats.get(c.id) or {}
            cd = self._cooldown.get(c.id, 0)
            out.append({
                "id": c.id,
                "running": c.running(),
                "account_status": str(getattr(getattr(c, "account_status", None), "name", getattr(c, "account_status", "")) or ""),
                "cooldown_until": cd if cd > now else None,
                "proxy_host": host,
                "requests": st.get("requests", 0), "ok": st.get("ok", 0), "fail": st.get("fail", 0),
                "last_used": st.get("last_used"), "last_error": st.get("last_error"),
                "usage": usage,
            })
        return {"clients": out, "started": self._started, "now": now}

''' + old, 1)
open(p, "w").write(s)

# ---- chat.py: count every send ------------------------------------------------
p = f"{SRC}/server/chat.py"
s = open(p).read()
assert "carher p4" not in s, "already patched"
old = """            if stream:
                # carher p2: pull the first chunk here so quota errors surface while we can still switch.
                output = await _carher_prime_stream(output)
            return output, session, client
        except Exception as exc:
"""
assert s.count(old) == 1
s = s.replace(old, """            if stream:
                # carher p2: pull the first chunk here so quota errors surface while we can still switch.
                output = await _carher_prime_stream(output)
            pool.record(client.id, True)  # carher p4
            return output, session, client
        except Exception as exc:
            pool.record(client.id, False, "%s: %s" % (type(exc).__name__, exc))  # carher p4
""", 1)
open(p, "w").write(s)

# ---- health.py: the endpoint --------------------------------------------------
p = f"{SRC}/server/health.py"
s = open(p).read()
assert "carher p4" not in s, "already patched"
old = 'router = APIRouter()\n'
assert s.count(old) == 1
s = s.replace(old, old + '''

# carher p4: per-account status for the 198:8796 console (same api_key as /v1/*).
from fastapi import Depends  # noqa: E402
from .middleware import verify_api_key  # noqa: E402


@router.get("/carher/status")
async def carher_status(api_key: str = Depends(verify_api_key)):
    return GeminiClientPool().carher_status()
''', 1)
open(p, "w").write(s)
print("p4 applied")
