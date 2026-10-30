"""Gemini-FastAPI 1731445 carher patch p5：把「这个号到底登上了没有」和「还剩多少额度」变成可读的。

为什么要这个（10-29 查清）：
  gemini_webapi 2.1.1 的 `account_status` 只有「GetUserStatus 回的那个码」，
  而**没有 cookie 的 guest 会话读数和 cookie 过期的号完全一样**（都是 UNAUTHENTICATED + 只有
  gemini-flash-lite 一个模型 + 纯聊天还能回话）。真正能一眼分真假的判据是 **SNlM0e（access_token）**：
  认证会话有，guest 没有。p4 的 /carher/status 只报 account_status ⇒ 76 个号全红但看不出严重性。

p5 在 /carher/status 每个号上加：
  authed        bool   有没有 SNlM0e ⇒ **false = 这个号实际在拿免费 guest 身份服务**
  cookie_source str    Cache / Base Cookies / Browser / Guest（库自己记的，哪组 cookie 产生了这个会话）
  models        list   这个号实时能用的模型名（guest 只有 gemini-flash-lite；Pro 有 argon/pro/flash）
  pro           bool   模型表里有没有非 flash-lite 的模型 = 订阅档位真的生效
  cookie_age_d  float  配置里这份 cookie 存了多少天（config.yaml 的 ts；没有就 None）
并新增 `POST /carher/quota`（同 api_key，body {"ids": [...]}，不给 ids = 全部）：
  对每个号现打一次 GetUsageInfo + CheckQuota，回 {id: {usage, quotas, authed, error}}。
  🔴 这两个 RPC 在库里都被 `_check_account_status()` 挡着 ⇒ 没登上的号恒回空，
     所以 p5 顺带把「空额度」和「没登上」分开报，别让管理台把 guest 显示成「额度满」。

额度读数是 Google 自己的 compute-usage 口径（remaining_credits / usage_percentage / reset_time，
外加 ai_credits_remaining）。**不发生成请求，不花额度**。

接在 p1..p4 之后。幂等（装过就 assert 失败，不会装两遍）。
"""
SRC = "/root/gemini-web-build/src/app"

# ---- pool.py: 每号的认证实况 + 按需刷额度 ---------------------------------------
p = f"{SRC}/services/pool.py"
s = open(p).read()
assert "carher p5" not in s, "already patched"
old = '''            try:
                usage = dict((c.quotas or {}).get("usage_info") or {})
            except Exception:
                usage = {}
'''
assert s.count(old) == 1, "p4 usage anchor missing or ambiguous"
s = s.replace(old, '''            try:
                usage = dict((c.quotas or {}).get("usage_info") or {})
            except Exception:
                usage = {}
            # carher p5: SNlM0e 才是「真登上了」的判据 —— 没 cookie 的 guest 会话和 cookie 过期的号
            # 在 account_status 上完全同形（都 UNAUTHENTICATED），只有 access_token 能分开。
            authed = bool(getattr(c, "access_token", None))
            try:
                models = sorted(m.model_name for m in (c._model_registry or {}).values())
            except Exception:
                models = []
''', 1)

old = '''                "last_used": st.get("last_used"), "last_error": st.get("last_error"),
                "usage": usage,
            })'''
assert s.count(old) == 1, "p4 status dict anchor missing"
s = s.replace(old, '''                "last_used": st.get("last_used"), "last_error": st.get("last_error"),
                "usage": usage,
                # carher p5
                "authed": authed,
                "cookie_source": getattr(c, "_cookie_source", None),
                "models": models,
                "pro": any(m != "gemini-flash-lite" for m in models),
            })''', 1)

old = "    def carher_status(self) -> dict:\n"
assert s.count(old) == 1
s = s.replace(old, '''    async def carher_quota(self, ids: list[str] | None = None) -> dict:
        """carher p5: 现打 GetUsageInfo + CheckQuota 读额度（只读，不发生成请求）。

        没登上的号（authed=False）库里这两个 RPC 直接返回空 ⇒ 这里显式报 need_login，
        别让管理台把 guest 的空额度显示成「额度充足」。
        """
        want = set(ids or [])
        out: dict[str, dict] = {}
        for c in self._clients:
            if want and c.id not in want:
                continue
            row: dict = {"authed": bool(getattr(c, "access_token", None)), "at": time.time()}
            if not c.running():
                row["error"] = "没在跑"
                out[c.id] = row
                continue
            if not row["authed"]:
                row["error"] = "need_login"
                out[c.id] = row
                continue
            try:
                await c._fetch_usage_info()
                await c._fetch_quota()
                row["usage"] = dict(getattr(c, "usage_info", None) or {})
                row["quotas"] = {k: v for k, v in (c.quotas or {}).items() if k != "usage_info"}
            except Exception as exc:
                row["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            out[c.id] = row
        return out

''' + old, 1)
open(p, "w").write(s)

# ---- health.py: 额度端点 --------------------------------------------------------
p = f"{SRC}/server/health.py"
s = open(p).read()
assert "carher p5" not in s, "already patched"
old = '''@router.get("/carher/status")
async def carher_status(api_key: str = Depends(verify_api_key)):
    return GeminiClientPool().carher_status()
'''
assert s.count(old) == 1, "p4 endpoint anchor missing"
s = s.replace(old, old + '''

# carher p5: 按需读额度（只读 RPC，不花额度）。body {"ids": [...]}；不给 ids = 全部号。
from fastapi import Body  # noqa: E402


@router.post("/carher/quota")
async def carher_quota(body: dict = Body(default={}), api_key: str = Depends(verify_api_key)):
    ids = body.get("ids") if isinstance(body, dict) else None
    ids = [str(x) for x in ids] if isinstance(ids, list) else None
    return await GeminiClientPool().carher_quota(ids)
''', 1)
open(p, "w").write(s)
print("p5 applied")
