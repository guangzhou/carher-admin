"""Gemini-FastAPI 1731445 carher patch p7：/health 不再阻塞重 init + 选号跳过要点同意条款的号。

两件事，都是 10-30 端到端测试撞出来的：

1) /health 把整个服务摘出流量（10-30 17:29~17:46 生产 4 个 web-gemini 组全不通）
   - readinessProbe：`/health`，timeoutSeconds=5，periodSeconds=15，failureThreshold=3。
   - 上游 `/health` 每次都 `await pool.init()`，它**串行**对每个 `running()=False` 的号跑
     `client.init(timeout=120…)`。一个号停了、init 慢，`/health` 就超过 5s ⇒ 3 次失败 ⇒ NotReady ⇒
     endpoints 清空 ⇒ 池里另外 43 个好号也一起被摘掉。现场：Ready=False@09:29:04Z、restarts=0、
     `/carher/status` 0.0s 回而 `/health` 等 150s 不回、当时唯一 running=False 的号 = levienjore500。
   - 改法：/health 只报状态、不 init。停掉的号本来就由 `acquire()` → `_ensure_client_ready()`
     在派到它时按号加锁重启（上游已有），健康检查不必、也不该替它做。
     「至少有一个号在跑」才算 ok —— 和 `pool.init()` 原本的「一个都起不来才失败」语义一致；
     上游原来 `ok=all(...)` 会让一个坏号把整池判死（但它以前从没真报过 ok=False，因为同样被 init 卡住）。

2) 选号会派到 TOS_PENDING / TOS_OUT_OF_DATE 的号（1040/1042）
   - 这些号是**真登录**的（有 access_token），p6 的 `_authed()` 放行 ⇒ 一派到就
     `Unknown API error code: 1040`。10-30 测试 42 次派号里 6 次落在 TOS 号上，全部失败。
   - 改法：`acquire()` 轮询时额外跳过 account_status ∈ {TOS_PENDING, TOS_OUT_OF_DATE}。
     同 p6 的兜底：全池都是这种号时退回原行为，不把池子变空。
   - 人在网页上点完同意后，下次 `client.init()` 会把 account_status 刷成 AVAILABLE，自动回到轮询。
   - account_status 枚举值来自 `gemini_webapi.constants.AccountStatus`（1040 TOS_PENDING、1042 TOS_OUT_OF_DATE）。

应用在 p1..p6 之后。每处替换都断言恰好匹配一次，重复应用会失败。
"""
SRC = "/root/gemini-web-build/src/app"

# ---------------------------------------------------------------- 1) /health
p = f"{SRC}/server/health.py"
s = open(p).read()
assert "carher p7" not in s, "already patched"

old = """    try:
        await pool.init()
    except Exception as e:
        logger.error(f"Failed to initialize Gemini clients: {e}")
        return HealthCheckResponse(ok=False, error=str(e))

    client_status = pool.status()

    if not all(client_status.values()):
        logger.warning("One or more Gemini clients not running")
"""
assert s.count(old) == 1, "health.py anchor drifted"
new = """    # carher p7: 不在健康检查里 init。上游这里串行 init 每个停掉的号（每个最长 120s），
    # 而 readinessProbe 只给 5s ⇒ 一个坏号就把整个 pod 摘出流量（10-30 生产事故）。
    # 停掉的号由 acquire() → _ensure_client_ready() 在派到时按号重启。
    client_status = pool.status()

    if not any(client_status.values()):
        logger.error("No Gemini clients running")
        return HealthCheckResponse(ok=False, error="no Gemini clients running", clients=client_status)
    if not all(client_status.values()):
        logger.warning("One or more Gemini clients not running")
"""
s = s.replace(old, new, 1)

old2 = "    return HealthCheckResponse(ok=all(client_status.values()), storage=stat, clients=client_status)\n"
assert s.count(old2) == 1, "health.py return anchor drifted"
s = s.replace(old2, "    return HealthCheckResponse(ok=any(client_status.values()), storage=stat, clients=client_status)\n", 1)
open(p, "w").write(s)

# ---------------------------------------------------------------- 2) skip TOS in acquire
p = f"{SRC}/services/pool.py"
s = open(p).read()
assert "carher p6" in s, "p7 must be applied after p6"
assert "carher p7" not in s, "already patched"

old = """        prefer_authed = any(self._authed(c) for c in self._clients)
        for _ in range(len(self._round_robin)):
            client = self._round_robin[0]
            self._round_robin.rotate(-1)
            if client.id in exclude or not self.has_quota(client):
                continue
            if prefer_authed and not self._authed(client):
                continue
"""
assert s.count(old) == 1, "pool.py p6 acquire anchor drifted"
new = """        prefer_authed = any(self._authed(c) for c in self._clients)
        # carher p7: 真登录但要点同意条款（1040/1042）的号一派就失败 ⇒ 有别的号时跳过。
        prefer_no_tos = any(self._authed(c) and not self._tos_blocked(c) for c in self._clients)
        for _ in range(len(self._round_robin)):
            client = self._round_robin[0]
            self._round_robin.rotate(-1)
            if client.id in exclude or not self.has_quota(client):
                continue
            if prefer_authed and not self._authed(client):
                continue
            if prefer_no_tos and self._tos_blocked(client):
                continue
"""
s = s.replace(old, new, 1)

anchor = "    # carher p6 ---------------------------------------------------------------\n"
assert s.count(anchor) == 1, "pool.py p6 helper anchor drifted"
s = s.replace(anchor, """    # carher p7 ---------------------------------------------------------------
    _TOS_CODES = (1040, 1042)  # gemini_webapi AccountStatus.TOS_PENDING / TOS_OUT_OF_DATE

    @classmethod
    def _tos_blocked(cls, client) -> bool:
        \"\"\"这个号是不是卡在「要点同意条款」上。读不到状态时当作没卡（不误伤）。\"\"\"
        try:
            st = getattr(client, "account_status", None)
            if st is None:
                return False
            code = getattr(st, "value", st)
            return int(code) in cls._TOS_CODES
        except Exception:
            return False

""" + anchor, 1)
open(p, "w").write(s)
print("p7 applied: health.py (no init in /health, ok=any) + pool.py (skip TOS 1040/1042)")
