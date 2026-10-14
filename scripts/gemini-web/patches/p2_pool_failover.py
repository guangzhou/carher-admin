"""Gemini-FastAPI 1731445 carher patch p2: multi-account failover.

1. pool.acquire(): skip clients whose quota snapshot says remaining=0 and reset is in the future,
   and clients we marked cooling after a usage-limit / block / timeout error.
2. _send_with_internal_fallback(): on UsageLimitExceeded / TemporarilyBlocked / Timeout from a fresh
   session, mark the client cooling and retry once per remaining client on a new session.

Applied on top of patch_namedmodel.py. Idempotent (refuses to apply twice).
"""
SRC = "/root/gemini-web-build/src/app"

# ---- pool.py ----------------------------------------------------------------
p = f"{SRC}/services/pool.py"
s = open(p).read()
assert "carher p2" not in s, "already patched"

s = s.replace("import asyncio\nfrom collections import deque\n",
              "import asyncio\nimport time\nfrom collections import deque\n", 1)

old_init_tail = "            self._restart_locks[c.id] = asyncio.Lock()\n"
assert s.count(old_init_tail) == 1
s = s.replace(old_init_tail, old_init_tail + "        # carher p2: client id -> unix ts until which it is skipped\n        self._cooldown: dict[str, float] = {}\n", 1)

old_rr = """        for _ in range(len(self._round_robin)):
            client = self._round_robin[0]
            self._round_robin.rotate(-1)
            if await self._ensure_client_ready(client):
                return client

        raise RuntimeError("No Gemini clients are currently available")
"""
new_rr = """        exclude = exclude or set()
        for _ in range(len(self._round_robin)):
            client = self._round_robin[0]
            self._round_robin.rotate(-1)
            if client.id in exclude or not self.has_quota(client):
                continue
            if await self._ensure_client_ready(client):
                return client

        raise RuntimeError("No Gemini clients are currently available (all exhausted or cooling down)")

    # carher p2 ---------------------------------------------------------------
    def mark_cooldown(self, client_id: str, seconds: float) -> None:
        self._cooldown[client_id] = time.time() + seconds
        logger.warning(f"[carher p2] client {client_id} cooling down for {int(seconds)}s")

    def has_quota(self, client: GeminiClientWrapper) -> bool:
        now = time.time()
        if self._cooldown.get(client.id, 0) > now:
            return False
        try:
            quotas = client.quotas() or {}
        except Exception:
            return True
        for q in quotas.values():
            if not isinstance(q, dict) or "remaining" not in q:
                continue
            reset = q.get("reset_time") or 0
            if q.get("remaining") == 0 and (q.get("total") or 0) > 0 and reset > now:
                return False
        return True
"""
assert s.count(old_rr) == 1
s = s.replace(old_rr, new_rr, 1)
s = s.replace("    async def acquire(self, client_id: str | None = None) -> GeminiClientWrapper:",
              "    async def acquire(self, client_id: str | None = None, exclude: set[str] | None = None) -> GeminiClientWrapper:", 1)
open(p, "w").write(s)

# ---- chat.py ------------------------------------------------------------------
p = f"{SRC}/server/chat.py"
s = open(p).read()
old_try = """    try:
        output = await _send_with_split(
            session,
            current_input,
            files=files,
            stream=stream,
            temporary=temporary,
        )
        return output, session, client
    except Exception as exc:
        should_fallback = ("""
new_try = """    tried: set[str] = set()
    while True:
        try:
            output = await _send_with_split(
                session,
                current_input,
                files=files,
                stream=stream,
                temporary=temporary,
            )
            if stream:
                # carher p2: pull the first chunk here so quota errors surface while we can still switch.
                output = await _carher_prime_stream(output)
            return output, session, client
        except Exception as exc:
            if reused_session or not _carher_is_account_error(exc):
                break_exc = exc
                break
            tried.add(client.id)
            pool.mark_cooldown(client.id, _carher_cooldown_seconds(exc))
            try:
                client = await pool.acquire(exclude=tried)
            except Exception:
                raise exc
            logger.warning(f"[carher p2] {type(exc).__name__} -> retrying on client {client.id}")
            session = client.start_chat(model=model)
    exc = break_exc
    try:
        raise exc
    except Exception as exc:
        should_fallback = ("""
assert s.count(old_try) == 1, "send block anchor"
s = s.replace(old_try, new_try, 1)

helpers = '''

# carher p2 helpers -------------------------------------------------------------
def _carher_is_account_error(exc: Exception) -> bool:
    from gemini_webapi.exceptions import TemporarilyBlockedError, UsageLimitExceededError
    from gemini_webapi.exceptions import TimeoutError as GeminiTimeoutError

    return isinstance(exc, (UsageLimitExceededError, TemporarilyBlockedError, GeminiTimeoutError))


def _carher_cooldown_seconds(exc: Exception) -> float:
    from gemini_webapi.exceptions import UsageLimitExceededError

    return 1800.0 if isinstance(exc, UsageLimitExceededError) else 300.0


async def _carher_prime_stream(agen):
    first = await agen.__anext__()

    async def _chain():
        yield first
        async for item in agen:
            yield item

    return _chain()


async def _send_with_internal_fallback('''
assert s.count("\nasync def _send_with_internal_fallback(") == 1
s = s.replace("\nasync def _send_with_internal_fallback(", helpers, 1)
open(p, "w").write(s)
print("patched pool.py + chat.py")
