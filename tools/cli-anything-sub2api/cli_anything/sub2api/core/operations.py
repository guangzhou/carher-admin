"""High-level endpoint maps and safe file helpers for Sub2API operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from cli_anything.sub2api.core.client import Sub2APIError


def build_grok_oauth_credentials(token_info: dict[str, Any]) -> dict[str, Any]:
    """Build persisted Grok OAuth fields while excluding transient secrets."""
    allowed = (
        "access_token", "refresh_token", "token_type", "expires_at", "client_id",
        "scope", "email", "sub", "team_id", "subscription_tier", "entitlement_status", "id_token",
    )
    return {key: token_info[key] for key in allowed if token_info.get(key) not in (None, "")}


def build_grok_extra(token_info: dict[str, Any]) -> dict[str, Any]:
    """Build non-secret Grok account metadata from an OAuth response."""
    return {key: token_info[key] for key in ("email", "subscription_tier", "entitlement_status") if token_info.get(key) not in (None, "")}


CRUD_RESOURCES = {
    "users": "/admin/users",
    "groups": "/admin/groups",
    "proxies": "/admin/proxies",
    "channels": "/admin/channels",
}

SENSITIVE_PATH_SUFFIXES = (
    "/admin/accounts/data",
    "/admin/proxies/data",
    "/admin/settings/admin-api-key",
)


def account_list_query(
    *,
    page: int,
    page_size: int,
    search: str | None = None,
    platform: str | None = None,
    account_type: str | None = None,
    status: str | None = None,
    group: str | None = None,
    privacy_mode: str | None = None,
    sort_by: str = "name",
    sort_order: str = "asc",
    lite: bool = True,
) -> dict[str, Any]:
    """Create the query accepted by the native accounts list endpoint."""
    return {
        "page": page,
        "page_size": page_size,
        "search": search,
        "platform": platform,
        "type": account_type,
        "status": status,
        "group": group,
        "privacy_mode": privacy_mode,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "lite": 1 if lite else None,
    }


def parse_json_data(data: str | None, file_path: Path | None, *, required: bool = True) -> Any:
    """Read request JSON from exactly one source, with actionable validation errors."""
    if data and file_path:
        raise Sub2APIError("Use only one of --data or --file")
    if data is None and file_path is None:
        if required:
            raise Sub2APIError("This command requires --data JSON or --file JSON_FILE")
        return None
    try:
        raw = data if data is not None else file_path.read_text(encoding="utf-8")
        return json.loads(raw)
    except FileNotFoundError as exc:
        raise Sub2APIError(f"JSON file not found: {file_path}") from exc
    except json.JSONDecodeError as exc:
        source = "--data" if data is not None else str(file_path)
        raise Sub2APIError(f"Invalid JSON in {source}: {exc.msg}") from exc


def parse_ids(value: str) -> list[int]:
    """Parse a comma-separated list of positive database IDs."""
    try:
        ids = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise Sub2APIError("--ids must be a comma-separated list of numeric IDs") from exc
    if not ids or any(item <= 0 for item in ids):
        raise Sub2APIError("--ids must contain one or more positive IDs")
    return ids


def parse_params(values: Iterable[str]) -> list[tuple[str, str]]:
    """Parse repeatable key=value query parameters for the raw API escape hatch."""
    parsed: list[tuple[str, str]] = []
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key:
            raise Sub2APIError("Each --param must have key=value form")
        parsed.append((key, item))
    return parsed


def is_sensitive_export_path(path: str) -> bool:
    """Identify known endpoints that can return credentials or admin secrets."""
    normalized = path.rstrip("/")
    return any(normalized.endswith(suffix) for suffix in SENSITIVE_PATH_SUFFIXES)


def write_json_file(path: Path, data: Any) -> dict[str, Any]:
    """Write an explicit operator-selected export file without echoing secret data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    path.write_text(encoded, encoding="utf-8")
    return {"file": str(path.resolve()), "bytes": len(encoded.encode("utf-8"))}
