"""Shared local HTTP backend used by the E2E suite."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest


ADMIN_KEY = "test-admin-key"


@dataclass
class BackendLog:
    requests: list[dict[str, Any]] = field(default_factory=list)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class FakeSub2APIBackend:
    """A small HTTP service exercising Sub2API's native envelope and admin routes."""

    def __init__(self) -> None:
        self.log = BackendLog()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_factory())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def _handler_factory(self):
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _handle(self) -> None:
                parsed = urlsplit(self.path)
                content_length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(content_length) if content_length else b""
                try:
                    body = json.loads(raw_body) if raw_body else None
                except json.JSONDecodeError:
                    body = {"raw": raw_body.decode("utf-8", errors="replace")}
                record = {
                    "method": self.command,
                    "path": parsed.path,
                    "query": {key: values[0] if len(values) == 1 else values for key, values in parse_qs(parsed.query).items()},
                    "headers": {
                        "x-api-key": self.headers.get("x-api-key"),
                        "authorization": self.headers.get("Authorization"),
                        "idempotency-key": self.headers.get("Idempotency-Key"),
                    },
                    "body": body,
                }
                backend.log.requests.append(record)

                if parsed.path == "/health":
                    return _json_response(self, 200, {"status": "ok"})

                if parsed.path == "/api/v1/admin/system/version":
                    if self.headers.get("x-api-key") != ADMIN_KEY:
                        return _json_response(self, 401, {"code": 401, "message": "INVALID_ADMIN_KEY"})
                    return _json_response(self, 200, {"code": 0, "message": "success", "data": {"version": "1.2.3-test"}})

                if parsed.path == "/api/v1/admin/accounts" and self.command == "GET":
                    if self.headers.get("x-api-key") != ADMIN_KEY:
                        return _json_response(self, 401, {"code": 401, "message": "INVALID_ADMIN_KEY"})
                    return _json_response(
                        self,
                        200,
                        {"code": 0, "message": "success", "data": {"items": [{"id": 1, "name": "upstream@example.com", "status": "active"}], "total": 1, "page": 1, "page_size": 200, "pages": 1}},
                    )

                if parsed.path == "/api/v1/admin/accounts/1" and self.command == "PUT":
                    if self.headers.get("x-api-key") != ADMIN_KEY:
                        return _json_response(self, 401, {"code": 401, "message": "INVALID_ADMIN_KEY"})
                    return _json_response(self, 200, {"code": 0, "message": "success", "data": {"id": 1, **body}})

                if parsed.path == "/api/v1/admin/grok/oauth/capabilities" and self.command == "GET":
                    if self.headers.get("x-api-key") != ADMIN_KEY:
                        return _json_response(self, 401, {"code": 401, "message": "INVALID_ADMIN_KEY"})
                    return _json_response(self, 200, {"code": 0, "message": "success", "data": {"password_auth_enabled": False}})

                if parsed.path == "/api/v1/admin/accounts/data" and self.command == "GET":
                    if self.headers.get("x-api-key") != ADMIN_KEY:
                        return _json_response(self, 401, {"code": 401, "message": "INVALID_ADMIN_KEY"})
                    return _json_response(
                        self,
                        200,
                        {"code": 0, "message": "success", "data": [{"id": 1, "name": "upstream@example.com", "access_token": "secret-token"}]},
                    )

                if parsed.path == "/api/v1/admin/users/7" and self.command == "GET":
                    if self.headers.get("x-api-key") != ADMIN_KEY:
                        return _json_response(self, 401, {"code": 401, "message": "INVALID_ADMIN_KEY"})
                    return _json_response(self, 200, {"code": 0, "message": "success", "data": {"id": 7, "email": "user@example.com"}})

                if parsed.path == "/api/v1/admin/users/7/balance" and self.command == "POST":
                    if self.headers.get("x-api-key") != ADMIN_KEY:
                        return _json_response(self, 401, {"code": 401, "message": "INVALID_ADMIN_KEY"})
                    return _json_response(self, 200, {"code": 0, "message": "success", "data": {"id": 7, "balance": body["balance"]}})

                if parsed.path == "/api/v1/admin/audit-logs" and self.command == "GET":
                    if self.headers.get("x-api-key") != ADMIN_KEY:
                        return _json_response(self, 401, {"code": 401, "message": "INVALID_ADMIN_KEY"})
                    return _json_response(
                        self,
                        200,
                        {"code": 0, "message": "success", "data": {"items": [{"id": 9, "action": "auth.login", "path": "/api/v1/auth/login"}], "total": 1}},
                    )

                return _json_response(self, 404, {"code": 404, "message": "not found"})

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_PATCH = _handle
            do_DELETE = _handle

        return Handler


@pytest.fixture()
def backend():
    fake = FakeSub2APIBackend()
    fake.start()
    yield fake
    fake.stop()
