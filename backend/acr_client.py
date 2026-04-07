"""Alibaba Cloud ACR client for syncing CarHer image tags.

Uses the official ACR OpenAPI:
- ListRepository: resolve the fixed `her/carher` repository to a RepoId
- ListRepoTag: list tags for that repository page by page
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from alibabacloud_cr20181201.client import Client as ACRClient
from alibabacloud_cr20181201 import models as acr_models
from alibabacloud_tea_openapi.models import Config

REPO_NAMESPACE = "her"
REPO_NAME = "carher"
PAGE_SIZE = 100


class ACRConfigError(ValueError):
    """Raised when ACR settings are incomplete."""


@dataclass(frozen=True)
class ACRSettings:
    region_id: str
    instance_id: str
    access_key_id: str
    access_key_secret: str


@dataclass(frozen=True)
class ACRTag:
    tag: str
    digest: str
    image_id: str
    image_size: int
    image_update_ms: int
    updated_at: str


def build_settings(*, region_id: str, instance_id: str, access_key_id: str, access_key_secret: str) -> ACRSettings:
    missing = [
        name for name, value in (
            ("acr_region_id", region_id),
            ("acr_instance_id", instance_id),
            ("acr_access_key_id", access_key_id),
            ("acr_access_key_secret", access_key_secret),
        ) if not value
    ]
    if missing:
        raise ACRConfigError(f"Missing ACR settings: {', '.join(missing)}")
    return ACRSettings(
        region_id=region_id,
        instance_id=instance_id,
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
    )


def list_carher_tags(settings: ACRSettings) -> list[ACRTag]:
    client = _create_client(settings)
    repo_id = _find_repo_id(client, settings.instance_id)
    tags: list[ACRTag] = []
    page_no = 1

    while True:
        request = acr_models.ListRepoTagRequest(
            instance_id=settings.instance_id,
            repo_id=repo_id,
            page_no=page_no,
            page_size=PAGE_SIZE,
        )
        payload = _response_map(client.list_repo_tag(request))
        images = _get_list(payload, "Images")
        if not images:
            break
        for image in images:
            parsed = _parse_tag(image)
            if parsed is not None:
                tags.append(parsed)

        total_count = _to_int(_get_value(payload, "TotalCount"))
        if total_count <= page_no * PAGE_SIZE:
            break
        page_no += 1

    tags.sort(key=lambda item: (item.image_update_ms, item.tag), reverse=True)
    return tags


def _create_client(settings: ACRSettings) -> ACRClient:
    config = Config(
        access_key_id=settings.access_key_id,
        access_key_secret=settings.access_key_secret,
        region_id=settings.region_id,
        endpoint=f"cr.{settings.region_id}.aliyuncs.com",
    )
    return ACRClient(config)


def _find_repo_id(client: ACRClient, instance_id: str) -> str:
    request = acr_models.ListRepositoryRequest(
        instance_id=instance_id,
        repo_namespace_name=REPO_NAMESPACE,
        repo_name=REPO_NAME,
        page_no=1,
        page_size=PAGE_SIZE,
    )
    payload = _response_map(client.list_repository(request))
    repositories = _get_list(payload, "Repositories")
    for repo in repositories:
        if _matches_carher_repo(repo):
            repo_id = str(_get_value(repo, "RepoId") or "")
            if repo_id:
                return repo_id
    raise ACRConfigError(f"ACR repository not found: {REPO_NAMESPACE}/{REPO_NAME}")


def _matches_carher_repo(repo: dict[str, Any]) -> bool:
    return (
        str(_get_value(repo, "RepoNamespaceName") or "") == REPO_NAMESPACE
        and str(_get_value(repo, "RepoName") or "") == REPO_NAME
    )


def _parse_tag(image: dict[str, Any]) -> ACRTag | None:
    tag = str(_get_value(image, "Tag") or "").strip()
    if not tag:
        return None
    image_update_ms = _to_int(_get_value(image, "ImageUpdate"))
    return ACRTag(
        tag=tag,
        digest=str(_get_value(image, "Digest") or ""),
        image_id=str(_get_value(image, "ImageId") or ""),
        image_size=_to_int(_get_value(image, "ImageSize")),
        image_update_ms=image_update_ms,
        updated_at=_iso_from_millis(image_update_ms),
    )


def _response_map(response: Any) -> dict[str, Any]:
    body = getattr(response, "body", None)
    if body is not None and hasattr(body, "to_map"):
        return body.to_map()
    if hasattr(response, "to_map"):
        return response.to_map()
    if isinstance(response, dict):
        return response
    raise TypeError(f"Unsupported ACR response type: {type(response)!r}")


def _get_value(payload: dict[str, Any], key: str) -> Any:
    for candidate in (key, key[:1].lower() + key[1:], key.upper(), key.lower()):
        if candidate in payload:
            return payload[candidate]
    return None


def _get_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = _get_value(payload, key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _iso_from_millis(value: int) -> str:
    if value <= 0:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
