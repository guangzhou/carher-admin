"""Pydantic models for CarHer Admin API.

All request/response models are defined here for OpenAPI schema generation.
Cursor and other AI agents consume these via /openapi.json.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ──────────────────────────────────────
# Instance models
# ──────────────────────────────────────

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
    provider: str = "openrouter"
    sync_status: str = ""
    deploy_group: str = "stable"
    image_tag: str = ""
    has_memory: bool | None = None


class HerAddRequest(BaseModel):
    id: int | None = Field(None, description="Instance ID (auto-assigned if omitted)")
    name: str = Field(..., description="User display name")
    model: str = Field("gpt", description="Model: gpt / sonnet / opus")
    app_id: str = Field(..., description="Feishu App ID (cli_xxx)")
    app_secret: str = Field(..., description="Feishu App Secret")
    prefix: str = Field("s1", description="Server prefix (s1/s2/s3)")
    owner: str = Field("", description="Feishu open_id(s), pipe-separated")
    provider: str = Field("openrouter", description="AI provider: openrouter / anthropic")
    deploy_group: str = Field("stable", description="Deploy group name")


class HerBatchImport(BaseModel):
    instances: list[HerAddRequest]


class HerUpdateRequest(BaseModel):
    name: str | None = Field(None, description="Update display name")
    model: str | None = Field(None, description="Update model: gpt / sonnet / opus")
    app_id: str | None = Field(None, description="Update Feishu App ID (cli_xxx)")
    app_secret: str | None = Field(None, description="Update Feishu App Secret (stored in K8s Secret)")
    owner: str | None = Field(None, description="Update owner open_id(s)")
    provider: str | None = Field(None, description="Update provider: openrouter / anthropic")
    prefix: str | None = Field(None, description="Update server prefix (s1/s2/s3)")
    bot_open_id: str | None = Field(None, description="Update bot open_id")
    image: str | None = Field(None, description="Update image tag (e.g. v20260329)")
    deploy_group: str | None = Field(None, description="Update deploy group")


class HerBatchAction(BaseModel):
    ids: list[int] = Field(..., description="List of instance IDs")
    action: str = Field(..., description="Action: stop / start / restart / delete / update")
    params: HerUpdateRequest | None = None


# ──────────────────────────────────────
# Cluster / Health models
# ──────────────────────────────────────

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


# ──────────────────────────────────────
# Deploy group models
# ──────────────────────────────────────

class DeployGroupCreate(BaseModel):
    name: str = Field(..., description="Group name (alphanumeric, - or _)")
    priority: int = Field(100, description="Deploy order: lower = deployed first")
    description: str = Field("", description="Group description")


class DeployGroupUpdate(BaseModel):
    priority: int | None = None
    description: str | None = None


class SetDeployGroupRequest(BaseModel):
    group: str = Field(..., description="Target deploy group name")


class BatchSetDeployGroupRequest(BaseModel):
    ids: list[int] = Field(..., description="Instance IDs to move")
    group: str = Field(..., description="Target deploy group name")


# ──────────────────────────────────────
# Deploy pipeline models
# ──────────────────────────────────────

class DeployRequest(BaseModel):
    image_tag: str = Field(..., description="Docker image tag to deploy")
    mode: str = Field("normal", description="Deploy mode: normal / fast / canary-only / group:<name>")
    force: bool = Field(False, description="Force deploy even if same tag was already deployed")


class DeployWebhookRequest(BaseModel):
    image_tag: str = Field(..., description="Docker image tag")
    secret: str = Field(..., description="Webhook authentication secret")
    mode: str = Field("normal", description="Deploy mode")


# ──────────────────────────────────────
# Search / Filter models
# ──────────────────────────────────────

class InstanceSearchParams(BaseModel):
    status: str | None = Field(None, description="Filter: Running / Stopped / Failed / Paused")
    model: str | None = Field(None, description="Filter: gpt / sonnet / opus")
    deploy_group: str | None = Field(None, description="Filter: group name")
    owner: str | None = Field(None, description="Filter: owner contains this open_id")
    name: str | None = Field(None, description="Filter: name contains this text")
    feishu_ws: str | None = Field(None, description="Filter: Connected / Disconnected")


# ──────────────────────────────────────
# AI Agent models
# ──────────────────────────────────────

class AgentRequest(BaseModel):
    message: str = Field(..., description="Natural language command (Chinese or English)")
    context: dict | None = Field(None, description="Optional context (e.g. instance_id)")
    dry_run: bool = Field(False, description="If true, only explain what would happen without executing")


class AgentResponse(BaseModel):
    answer: str = Field(..., description="Agent's response text")
    actions_taken: list[dict] = Field(default_factory=list, description="List of API calls executed")
    suggestions: list[str] = Field(default_factory=list, description="Follow-up suggestions")
