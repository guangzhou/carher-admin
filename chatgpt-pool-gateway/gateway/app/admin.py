"""/admin/* CRUD: 加/删/列账号 + 人工 enable/disable + 触发 probe。

T-OPS 主轴: 加 acct = 2 步
  1. kubectl create secret generic chatgpt-auth-N --from-file=auth.json
  2. curl -H "Authorization: Bearer $MK" -X POST gateway/admin/acct/add \
       -d '{"name":"acct-N","auth_path":"/data/auth/acct-N/auth.json","priority":50}'
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .config import AUTH_FILES_DIR, INTERNAL_API_KEY
from .registry import Registry
from .state import AccountState, transition

log = logging.getLogger("gateway.admin")
router = APIRouter(prefix="/admin", tags=["admin"])


def _check_auth(authorization: str | None = Header(None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer")
    if authorization[7:] != INTERNAL_API_KEY:
        raise HTTPException(403, "bad token")


class AcctAddBody(BaseModel):
    name: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    auth_path: str | None = None  # 默认 {AUTH_FILES_DIR}/{name}/auth.json
    priority: int = 100


@router.post("/acct/add", dependencies=[Depends(_check_auth)])
async def acct_add(req: Request, body: AcctAddBody) -> dict[str, Any]:
    reg: Registry = req.app.state.registry
    auth_path = body.auth_path or str(Path(AUTH_FILES_DIR) / body.name / "auth.json")
    if not os.path.exists(auth_path):
        raise HTTPException(400, f"auth_path not found: {auth_path}")
    try:
        status = reg.add(body.name, auth_path=auth_path, priority=body.priority)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "name": status.name, "state": status.state.value, "auth_path": auth_path}


@router.post("/acct/remove", dependencies=[Depends(_check_auth)])
async def acct_remove(req: Request, body: dict[str, Any]) -> dict[str, Any]:
    name = body.get("name")
    if not name:
        raise HTTPException(400, "name required")
    reg: Registry = req.app.state.registry
    reg.remove(name)
    return {"ok": True, "name": name}


@router.get("/acct/list", dependencies=[Depends(_check_auth)])
async def acct_list(req: Request) -> dict[str, Any]:
    reg: Registry = req.app.state.registry
    return {
        "accounts": [
            {
                "name": s.name,
                "state": s.state.value,
                "priority": s.priority,
                "primary_used_pct": s.primary_used_pct,
                "secondary_used_pct": s.secondary_used_pct,
                "primary_reset_at": s.primary_reset_at,
                "secondary_reset_at": s.secondary_reset_at,
                "last_probe_at": s.last_probe_at,
                "last_state_change_at": s.last_state_change_at,
                "last_state_reason": s.last_state_reason,
                "consecutive_401": s.consecutive_401,
                "cooldown_until": s.cooldown_until,
            }
            for s in reg.all()
        ]
    }


@router.post("/acct/disable", dependencies=[Depends(_check_auth)])
async def acct_disable(req: Request, body: dict[str, Any]) -> dict[str, Any]:
    name = body.get("name")
    reason = body.get("reason", "manual")
    reg: Registry = req.app.state.registry
    status = reg.get(name)
    if status is None:
        raise HTTPException(404, "not found")
    from_state = status.state
    if not transition(status, AccountState.DISABLED, f"admin:{reason}"):
        raise HTTPException(409, f"illegal transition from {from_state.value}")
    reg.persist(status, from_state=from_state)
    return {"ok": True, "name": name, "state": status.state.value}


@router.post("/acct/enable", dependencies=[Depends(_check_auth)])
async def acct_enable(req: Request, body: dict[str, Any]) -> dict[str, Any]:
    name = body.get("name")
    reg: Registry = req.app.state.registry
    status = reg.get(name)
    if status is None:
        raise HTTPException(404, "not found")
    from_state = status.state
    # DISABLED -> COOLING, 等下次 probe 回 HEALTHY
    if not transition(status, AccountState.COOLING, "admin:enable"):
        raise HTTPException(409, f"illegal transition from {from_state.value}")
    reg.persist(status, from_state=from_state)
    return {"ok": True, "name": name, "state": status.state.value}


@router.post("/acct/probe", dependencies=[Depends(_check_auth)])
async def acct_probe(req: Request, body: dict[str, Any]) -> dict[str, Any]:
    """手动触发一次 probe (运维) — 不动 60s tick loop。"""
    from .probe import probe_once
    name = body.get("name")
    reg: Registry = req.app.state.registry
    if reg.get(name) is None:
        raise HTTPException(404, "not found")
    data = await probe_once(reg, name, req.app.state.cf_session_factory)
    status = reg.get(name)
    return {
        "ok": data is not None,
        "name": name,
        "state": status.state.value,
        "primary_used_pct": status.primary_used_pct,
        "secondary_used_pct": status.secondary_used_pct,
    }
