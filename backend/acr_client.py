"""ACR client for syncing CarHer image tags via Docker Registry v2 API.

Auth flow (ACR Enterprise Edition):
  1. GET /v2/ → 401 with WWW-Authenticate Bearer realm + service
  2. GET {realm}?service={service}&scope=repository:{repo}:pull  (basic auth)
  3. Use returned bearer token for subsequent API calls
  4. GET /v2/{repo}/tags/list → tag list
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from base64 import b64encode
import json

logger = logging.getLogger("carher-admin")

REPO_NAMESPACE = "her"
REPO_NAME = "carher"
REPO_PATH = f"{REPO_NAMESPACE}/{REPO_NAME}"


class ACRConfigError(ValueError):
    """Raised when ACR settings are incomplete."""


@dataclass(frozen=True)
class ACRSettings:
    registry: str
    username: str
    password: str


@dataclass(frozen=True)
class ACRTag:
    tag: str
    digest: str
    image_id: str
    image_size: int
    image_update_ms: int
    updated_at: str


def build_settings(*, registry: str, username: str, password: str) -> ACRSettings:
    missing = [
        name for name, value in (
            ("acr_registry", registry),
            ("acr_username", username),
            ("acr_password", password),
        ) if not value
    ]
    if missing:
        raise ACRConfigError(f"Missing ACR settings: {', '.join(missing)}")
    return ACRSettings(registry=registry, username=username, password=password)


def list_carher_tags(settings: ACRSettings) -> list[ACRTag]:
    """List all tags for her/carher via Docker Registry v2 API."""
    token = _get_bearer_token(settings)
    url = f"https://{settings.registry}/v2/{REPO_PATH}/tags/list"
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (URLError, HTTPError) as e:
        raise ACRConfigError(f"Failed to list tags: {e}") from e

    tag_names = data.get("tags") or []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return [
        ACRTag(tag=t, digest="", image_id="", image_size=0, image_update_ms=0, updated_at=now_str)
        for t in tag_names
        if isinstance(t, str) and t.strip()
    ]


def _get_bearer_token(settings: ACRSettings) -> str:
    """Obtain a bearer token via the ACR Docker auth endpoint."""
    realm, service = _discover_auth(settings.registry)
    params = urlencode({"service": service, "scope": f"repository:{REPO_PATH}:pull"})
    url = f"{realm}?{params}"
    creds = b64encode(f"{settings.username}:{settings.password}".encode()).decode()
    req = Request(url, headers={"Authorization": f"Basic {creds}"})
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (URLError, HTTPError) as e:
        raise ACRConfigError(f"ACR auth failed: {e}") from e
    token = data.get("token") or data.get("access_token") or ""
    if not token:
        raise ACRConfigError("ACR auth returned empty token")
    return token


_WWW_AUTH_RE = re.compile(r'Bearer\s+realm="([^"]+)",\s*service="([^"]+)"')


def _discover_auth(registry: str) -> tuple[str, str]:
    """GET /v2/ → parse WWW-Authenticate header for realm and service."""
    url = f"https://{registry}/v2/"
    try:
        req = Request(url)
        with urlopen(req, timeout=10) as resp:
            return "", ""
    except HTTPError as e:
        if e.code != 401:
            raise ACRConfigError(f"Unexpected /v2/ response: HTTP {e.code}") from e
        www_auth = e.headers.get("WWW-Authenticate", "")
    except URLError as e:
        raise ACRConfigError(f"Cannot reach registry {registry}: {e}") from e

    m = _WWW_AUTH_RE.search(www_auth)
    if not m:
        raise ACRConfigError(f"Cannot parse WWW-Authenticate: {www_auth!r}")
    return m.group(1), m.group(2)
