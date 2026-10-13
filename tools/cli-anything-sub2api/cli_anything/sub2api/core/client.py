"""Thin standard-library client for the real Sub2API HTTP management API."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from cli_anything.sub2api.core.profile import DEFAULT_PROFILE_PATH, load_profile


class Sub2APIError(RuntimeError):
    """An API failure that is safe to display without exposing credentials."""


@dataclass(frozen=True)
class ConnectionConfig:
    """Connection settings resolved from flags, environment, then local profile."""

    base_url: str
    admin_api_key: str | None = None
    jwt: str | None = None
    timeout: float = 30.0
    profile_path: Path = DEFAULT_PROFILE_PATH

    @classmethod
    def from_sources(
        cls,
        *,
        base_url: str | None = None,
        target: str | None = None,
        admin_api_key: str | None = None,
        jwt: str | None = None,
        timeout: float = 30.0,
        profile_path: Path = DEFAULT_PROFILE_PATH,
        environ: Mapping[str, str] | None = None,
    ) -> "ConnectionConfig":
        env = os.environ if environ is None else environ
        profile = load_profile(profile_path)
        selected_target = (target or env.get("SUB2API_TARGET") or profile.get("target") or "").strip().lower()
        target_urls = {
            "198": env.get("SUB2API_198_BASE_URL") or "http://10.68.13.198:31880",
            "198-dev": env.get("SUB2API_198_BASE_URL") or "http://10.68.13.198:31880",
        }
        configured_url = (
            base_url
            or env.get("SUB2API_BASE_URL")
            or (target_urls.get(selected_target) if selected_target else None)
            or profile.get("base_url")
            or ""
        )
        return cls(
            base_url=str(configured_url).rstrip("/"),
            admin_api_key=admin_api_key or env.get("SUB2API_ADMIN_API_KEY") or None,
            jwt=jwt or env.get("SUB2API_JWT") or None,
            timeout=timeout,
            profile_path=profile_path,
        )

    def validate(self, *, require_auth: bool = True) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise Sub2APIError(
                "Missing or invalid Sub2API base URL. Set SUB2API_BASE_URL, pass --base-url, "
                "or run 'profile set <url>'."
            )
        if require_auth and not (self.admin_api_key or self.jwt):
            raise Sub2APIError(
                "Missing administrator credentials. Set SUB2API_ADMIN_API_KEY or SUB2API_JWT, "
                "or pass --admin-api-key/--jwt."
            )

    @property
    def auth_scheme(self) -> str | None:
        if self.admin_api_key:
            return "x-api-key"
        if self.jwt:
            return "bearer-jwt"
        return None

    def auth_headers(self) -> dict[str, str]:
        self.validate(require_auth=True)
        if self.admin_api_key:
            return {"x-api-key": self.admin_api_key}
        return {"Authorization": f"Bearer {self.jwt}"}


def normalize_path(path: str, *, admin_only: bool = False) -> str:
    """Normalize supported relative API paths without accepting arbitrary URLs."""
    if not path:
        raise Sub2APIError("API path must not be empty")
    if urlsplit(path).scheme or path.startswith("//"):
        raise Sub2APIError("API path must be relative to the configured Sub2API base URL")
    normalized = "/" + path.lstrip("/")
    if normalized.startswith("/admin/"):
        normalized = "/api/v1" + normalized
    elif normalized.startswith("/api/v1/admin/"):
        pass
    elif admin_only:
        raise Sub2APIError("Admin requests must start with /admin/ or /api/v1/admin/")
    return normalized


def encode_query(params: Mapping[str, Any] | Iterable[tuple[str, Any]] | None) -> str:
    """Encode a query map, omitting absent values and retaining repeated values."""
    if not params:
        return ""
    pairs = params.items() if isinstance(params, Mapping) else params
    encoded: list[tuple[str, str]] = []
    for key, value in pairs:
        if value is None or value == "" or value is False:
            continue
        if isinstance(value, (list, tuple)):
            encoded.extend((str(key), str(item)) for item in value if item is not None and item != "")
        else:
            encoded.append((str(key), str(value).lower() if isinstance(value, bool) else str(value)))
    return urlencode(encoded, doseq=True)


def unwrap_response(payload: Any, *, endpoint: str, status: int) -> Any:
    """Unwrap Sub2API's response envelope and reject explicit API error codes."""
    if not isinstance(payload, dict) or "code" not in payload:
        return payload
    code = payload.get("code")
    if code not in (0, "0", None):
        message = str(payload.get("message") or payload.get("error") or "unknown API error")
        raise Sub2APIError(f"API {endpoint} returned code {code}: {message}")
    return payload.get("data")


class Sub2APIClient:
    """Call the real deployed Sub2API HTTP API without reimplementing its logic."""

    def __init__(self, config: ConnectionConfig):
        self.config = config

    def _url(self, path: str, query: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None) -> str:
        self.config.validate(require_auth=False)
        parsed = urlsplit(self.config.base_url)
        query_string = encode_query(query)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + path, query_string, ""))

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        query: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
        admin_only: bool = True,
        require_auth: bool = True,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Send a JSON request and return the native unwrapped API response body."""
        normalized_path = normalize_path(path, admin_only=admin_only)
        self.config.validate(require_auth=require_auth)
        headers = {"Accept": "application/json"}
        if require_auth:
            headers.update(self.config.auth_headers())
        if extra_headers:
            headers.update(extra_headers)
        data = None
        if payload is not None:
            try:
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise Sub2APIError("Request payload must be JSON serializable") from exc
            headers["Content-Type"] = "application/json"
        request = Request(self._url(normalized_path, query), data=data, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.config.timeout) as response:
                status = response.status
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise self._http_error(normalized_path, exc.code, raw) from None
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise Sub2APIError(f"Cannot reach Sub2API at {self.config.base_url}: {reason}") from None
        except TimeoutError as exc:
            raise Sub2APIError(f"Sub2API request timed out after {self.config.timeout:g}s") from exc

        try:
            decoded = json.loads(raw) if raw else None
        except json.JSONDecodeError as exc:
            raise Sub2APIError(f"API {normalized_path} returned invalid JSON (HTTP {status})") from exc
        return unwrap_response(decoded, endpoint=normalized_path, status=status)

    def _http_error(self, endpoint: str, status: int, raw: str) -> Sub2APIError:
        detail = f"HTTP {status}"
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                detail = str(payload.get("message") or payload.get("code") or detail)
        except json.JSONDecodeError:
            if raw.strip():
                detail = raw.strip()[:300]
        return Sub2APIError(f"API {endpoint} failed: {detail}")

    def admin(self, method: str, path: str, **kwargs: Any) -> Any:
        """Call an administrator endpoint, accepting either documented path form."""
        return self.request(method, path, admin_only=True, require_auth=True, **kwargs)

    def health(self) -> Any:
        """Call the service's native unauthenticated health endpoint."""
        return self.request("GET", "/health", admin_only=False, require_auth=False)
