"""Gemini-FastAPI 1731445 carher patch p6: 选号时跳过**没真登录**的号（guest）。

为什么（10-30 实测）：`acquire()` 只过滤 `exclude` 和 `has_quota()`，**不看有没有真登录**。
而没登上的号在 muse… 不，在 Gemini 侧只注册了 `gemini-flash-lite` 一个模型 ⇒
轮到它们时，付费模型名直接 502：
    ValueError: Unknown model name: 'gemini-3.1-pro'. Available registered models: gemini-flash-lite
直打 gemini-web 实测 20 发 `gemini-3.1-pro`：8 发这个 502；`gemini-4-argon` 8 发里 4 发同样。
（LiteLLM 的重试目前兜住了大部分，所以用户还没大面积撞上 —— 但一开给更多 key 就会爆。）

判据用 `authed`（= 有没有 `access_token`/SNlM0e，p5 已经暴露出来），**不能用 `account_status`**：
guest 和过期号在 account_status 上完全同形（见 memory feedback_plain_chat_working_is_not_proof_of_login）。

🔴 兜底：如果**全池都没登上**，不能把池子变成空（那会 500 给所有人，连 flash-lite 都不能用）。
   所以 authed 过滤只在「池里至少还有一个 authed 的号」时生效，否则退回原行为。

应用在 p1..p5 之后。幂等（重复应用会失败）。
"""
SRC = "/root/gemini-web-build/src/app"

p = f"{SRC}/services/pool.py"
s = open(p).read()
assert "carher p6" not in s, "already patched"

old = """        exclude = exclude or set()
        for _ in range(len(self._round_robin)):
            client = self._round_robin[0]
            self._round_robin.rotate(-1)
            if client.id in exclude or not self.has_quota(client):
                continue
            if await self._ensure_client_ready(client):
                return client
"""
assert s.count(old) == 1
new = """        exclude = exclude or set()
        # carher p6: 没真登录的号只注册了 gemini-flash-lite ⇒ 付费模型名在它身上直接 502。
        # 只要池里还有登上的号就不派给 guest；全池都没登上时退回原行为（别把池子变空）。
        prefer_authed = any(self._authed(c) for c in self._clients)
        for _ in range(len(self._round_robin)):
            client = self._round_robin[0]
            self._round_robin.rotate(-1)
            if client.id in exclude or not self.has_quota(client):
                continue
            if prefer_authed and not self._authed(client):
                continue
            if await self._ensure_client_ready(client):
                return client
"""
s = s.replace(old, new, 1)

anchor = "    # carher p4 ---------------------------------------------------------------\n"
assert s.count(anchor) == 1
s = s.replace(anchor, """    # carher p6 ---------------------------------------------------------------
    @staticmethod
    def _authed(client) -> bool:
        \"\"\"这个号是不是**真登录**（不是 guest）。判据只能是 access_token/SNlM0e ——
        guest 和过期号的 account_status 完全同形，用它分不开。\"\"\"
        try:
            return bool(getattr(client, "access_token", None))
        except Exception:
            return False

""" + anchor, 1)
open(p, "w").write(s)
print("p6 applied")
