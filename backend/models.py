"""Pydantic models for CarHer Admin API."""

from __future__ import annotations

from pydantic import BaseModel


class HerInstance(BaseModel):
    id: int
    name: str = ""
    model: str = ""
    model_short: str = ""
    status: str = "Unknown"
    pod_ip: str = ""
    node: str = ""
    age: str = ""
    restarts: int = 0
    app_id: str = ""
    oauth_url: str = ""
    owner: str = ""
    has_memory: bool | None = None


class HerAddRequest(BaseModel):
    id: int | None = None
    name: str
    model: str = "gpt"
    app_id: str
    app_secret: str
    prefix: str = "s1"
    owner: str = ""
    provider: str = "openrouter"


class HerBatchImport(BaseModel):
    instances: list[HerAddRequest]


class HerUpdateRequest(BaseModel):
    model: str | None = None
    owner: str | None = None
    image: str | None = None


class HerBatchAction(BaseModel):
    ids: list[int]
    action: str  # stop | start | restart | delete | update
    params: HerUpdateRequest | None = None


class ClusterStatus(BaseModel):
    total_pods: int = 0
    running: int = 0
    stopped: int = 0
    tunnel_status: str = "unknown"
    nodes: list[dict] = []


class HealthItem(BaseModel):
    id: int
    name: str = ""
    feishu_ws: bool = False
    memory_db: bool = False
    model_ok: bool = False
    status: str = ""
