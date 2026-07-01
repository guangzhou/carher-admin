"""conversation -> acct sticky affinity (encrypted_content_affinity 等价物)。

为什么: chatgpt /v1/responses 的 reasoning encrypted_content 是上游某个 backend 实例绑定的;
       同会话连续 turn 必须命中同 acct (= 同 OAuth identity), 否则 400 invalid encrypted_content。

策略: 内存 dict[conv_id -> (acct_name, expire_at)], TTL 默认 600s。
      conv_id 来源 (按优先级):
        1. body.metadata.conversation_id
        2. body.metadata.session_id
        3. HTTP header X-Conversation-Id / X-Session-Id

只读没污染状态; 命中即返 acct, miss 即让 picker 选并回写。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


AFFINITY_TTL_S = 600


@dataclass
class _Entry:
    acct: str
    expire_at: float


class AffinityMap:
    def __init__(self, ttl_s: int = AFFINITY_TTL_S) -> None:
        self._m: dict[str, _Entry] = {}
        self._ttl = ttl_s

    def get(self, conv_id: str | None, now: float | None = None) -> str | None:
        if not conv_id:
            return None
        now = now if now is not None else time.time()
        e = self._m.get(conv_id)
        if e is None:
            return None
        if e.expire_at < now:
            self._m.pop(conv_id, None)
            return None
        return e.acct

    def set(self, conv_id: str | None, acct: str, now: float | None = None) -> None:
        if not conv_id:
            return
        now = now if now is not None else time.time()
        self._m[conv_id] = _Entry(acct=acct, expire_at=now + self._ttl)

    def drop(self, conv_id: str | None) -> None:
        if conv_id:
            self._m.pop(conv_id, None)

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._m)}

    def gc(self, now: float | None = None) -> int:
        """主动清过期项, 返回清掉的条数。"""
        now = now if now is not None else time.time()
        dead = [k for k, v in self._m.items() if v.expire_at < now]
        for k in dead:
            self._m.pop(k, None)
        return len(dead)


def extract_conv_id(body: dict[str, Any], headers: dict[str, str] | None = None) -> str | None:
    meta = body.get("metadata") if isinstance(body, dict) else None
    if isinstance(meta, dict):
        for k in ("conversation_id", "session_id"):
            v = meta.get(k)
            if isinstance(v, str) and v:
                return v
    if headers:
        for k in ("X-Conversation-Id", "X-Session-Id", "x-conversation-id", "x-session-id"):
            v = headers.get(k)
            if v:
                return v
    return None
