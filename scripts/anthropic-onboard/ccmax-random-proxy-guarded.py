#!/usr/bin/env python3
"""
Guarded CC Max random proxy.

This is a drop-in-compatible /v1/messages forwarder for the Malaysia CC Max
random pool, but it reads active upstreams from a JSON file rendered by
ccmax-pool-guard.py instead of a static UPSTREAMS env var.

The active upstream file must not contain OAuth tokens. Per-account proxy keys
are read from the referenced per-account proxy .env file.
"""

from __future__ import annotations

import http.client
import hashlib
import json
import os
import random
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PORT = int(os.environ.get("PORT", "3466"))
BIND = os.environ.get("BIND", "127.0.0.1")
API_KEYS = set(filter(None, os.environ.get("API_KEYS", "").split(",")))
ACTIVE_UPSTREAMS_FILE = Path(os.environ.get(
    "ACTIVE_UPSTREAMS_FILE",
    "/Data/ccmax-pool-guard/active-upstreams.json",
))
SSE_READ_MODE = os.environ.get("SSE_READ_MODE", "line").strip().lower()
THINKING_DISPLAY_MODE = os.environ.get("THINKING_DISPLAY_MODE", "preserve").strip().lower()
UPSTREAM_RELOAD_SECONDS = float(os.environ.get("UPSTREAM_RELOAD_SECONDS", "2"))
RNG = random.SystemRandom()


@dataclass(frozen=True)
class Upstream:
    label: str
    acct: str
    host: str
    port: int
    key: str
    rpm_limit: int
    concurrency_limit: int


class UpstreamRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.loaded_at = 0.0
        self.content_hash = ""
        self.upstreams: list[Upstream] = []
        self.last_error = ""

    def maybe_reload(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.loaded_at < UPSTREAM_RELOAD_SECONDS:
            return
        with self.lock:
            if not force and now - self.loaded_at < UPSTREAM_RELOAD_SECONDS:
                return
            self.loaded_at = now
            try:
                raw = self.path.read_text()
                digest = hashlib.sha256(raw.encode()).hexdigest()
                if not force and digest == self.content_hash:
                    return
                data = json.loads(raw)
                self.upstreams = [self._parse_item(item) for item in data.get("upstreams", [])]
                self.content_hash = digest
                self.last_error = ""
            except FileNotFoundError:
                self.upstreams = []
                self.last_error = f"{self.path} not found"
            except Exception as exc:
                self.upstreams = []
                self.last_error = f"reload failed: {exc}"

    def _parse_item(self, item: dict[str, Any]) -> Upstream:
        url = urlparse(item["url"])
        key = item.get("api_key") or read_proxy_key(Path(item["api_key_file"]))
        label = item.get("label") or item.get("acct")
        return Upstream(
            label=label,
            acct=item.get("acct", label),
            host=url.hostname or "127.0.0.1",
            port=url.port or 80,
            key=key,
            rpm_limit=int(item.get("rpm_limit", 0) or 0),
            concurrency_limit=int(item.get("concurrency_limit", 0) or 0),
        )

    def snapshot(self) -> tuple[list[Upstream], str]:
        self.maybe_reload()
        with self.lock:
            return list(self.upstreams), self.last_error


class RateLimiter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.request_ts: dict[str, deque[float]] = defaultdict(deque)
        self.inflight: dict[str, int] = defaultdict(int)
        self.counts: dict[str, int] = defaultdict(int)

    def try_acquire(self, upstreams: list[Upstream]) -> tuple[Upstream | None, str]:
        now = time.time()
        candidates: list[Upstream] = []
        limited_reasons: list[str] = []
        with self.lock:
            for item in upstreams:
                q = self.request_ts[item.label]
                while q and now - q[0] >= 60:
                    q.popleft()
                rpm_ok = item.rpm_limit <= 0 or len(q) < item.rpm_limit
                conc_ok = item.concurrency_limit <= 0 or self.inflight[item.label] < item.concurrency_limit
                if rpm_ok and conc_ok:
                    candidates.append(item)
                else:
                    reason = []
                    if not rpm_ok:
                        reason.append(f"rpm {len(q)}/{item.rpm_limit}")
                    if not conc_ok:
                        reason.append(f"inflight {self.inflight[item.label]}/{item.concurrency_limit}")
                    limited_reasons.append(f"{item.label}: {'; '.join(reason)}")
            if not candidates:
                return None, "; ".join(limited_reasons) or "no candidates"
            min_inflight = min(self.inflight[item.label] for item in candidates)
            tied = [item for item in candidates if self.inflight[item.label] == min_inflight]
            picked = RNG.choice(tied)
            self.request_ts[picked.label].append(now)
            self.inflight[picked.label] += 1
            self.counts[picked.label] += 1
            return picked, ""

    def release(self, label: str) -> None:
        with self.lock:
            self.inflight[label] = max(0, self.inflight[label] - 1)

    def status(self) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            windows = {}
            for label, q in self.request_ts.items():
                while q and now - q[0] >= 60:
                    q.popleft()
                windows[label] = len(q)
            return {
                "counts": dict(self.counts),
                "inflight": dict(self.inflight),
                "rpm_window": windows,
            }


def read_proxy_key(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("API_KEYS="):
            value = line.split("=", 1)[1].strip()
            if "," in value:
                return value.split(",", 1)[0].strip()
            return value
    raise ValueError(f"API_KEYS missing in {path}")


def maybe_patch_thinking(body: bytes) -> bytes:
    if THINKING_DISPLAY_MODE != "summarized":
        return body
    try:
        req = json.loads(body)
    except Exception:
        return body
    thinking = req.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
        thinking.setdefault("display", "summarized")
        return json.dumps(req, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return body


def read_decoded_sse_line(resp: http.client.HTTPResponse) -> bytes:
    out = bytearray()
    while True:
        b = resp.read(1)
        if not b:
            break
        out += b
        if b == b"\n":
            break
    return bytes(out)


REGISTRY = UpstreamRegistry(ACTIVE_UPSTREAMS_FILE)
LIMITER = RateLimiter()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)

    def _json(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        if not API_KEYS:
            return True
        h = self.headers.get("authorization", "")
        if h.startswith("Bearer ") and h[7:] in API_KEYS:
            return True
        xak = self.headers.get("x-api-key", "")
        return bool(xak and xak in API_KEYS)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            upstreams, err = REGISTRY.snapshot()
            status = LIMITER.status()
            code = 200 if upstreams else 503
            return self._json(code, {
                "ok": bool(upstreams),
                "mode": "guarded-random-forward",
                "active_upstreams_file": str(ACTIVE_UPSTREAMS_FILE),
                "upstreams": [u.label for u in upstreams],
                "reload_error": err,
                **status,
            })
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._check_auth():
            return self._json(401, {
                "type": "error",
                "error": {"type": "authentication_error", "message": "unauthorized"},
            })
        if urlparse(self.path).path != "/v1/messages":
            return self._json(404, {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "only /v1/messages supported"},
            })
        try:
            n = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(n)
        except Exception as exc:
            return self._json(400, {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": f"bad request body: {exc}"},
            })

        upstreams, reload_err = REGISTRY.snapshot()
        if not upstreams:
            return self._json(503, {
                "type": "error",
                "error": {
                    "type": "ccmax_pool_empty",
                    "message": reload_err or "no active CC Max upstreams",
                },
            })
        upstream, limited_reason = LIMITER.try_acquire(upstreams)
        if upstream is None:
            return self._json(503, {
                "type": "error",
                "error": {
                    "type": "ccmax_rate_limited",
                    "message": limited_reason or "all CC Max upstreams are rate limited",
                },
            })

        body = maybe_patch_thinking(body)
        print(f"  -> {upstream.label} bytes={len(body)}", flush=True)
        conn: http.client.HTTPConnection | None = None
        try:
            conn = http.client.HTTPConnection(upstream.host, upstream.port, timeout=610)
            conn.request(
                "POST",
                "/v1/messages",
                body=body,
                headers={"content-type": "application/json", "x-api-key": upstream.key},
            )
            resp = conn.getresponse()
        except Exception as exc:
            LIMITER.release(upstream.label)
            return self._json(502, {
                "type": "error",
                "error": {"type": "api_error", "message": f"upstream connect failed: {exc}"},
            })

        self.send_response(resp.status)
        ct = resp.getheader("content-type", "application/json")
        self.send_header("content-type", ct)
        self.send_header("x-ccmax-random-route", upstream.label)
        for h, v in resp.getheaders():
            hl = h.lower()
            if hl.startswith("anthropic-") or hl == "request-id":
                self.send_header(h, v)
        if "text/event-stream" in ct:
            self.send_header("cache-control", "no-cache")
            self.send_header("x-accel-buffering", "no")
            self.send_header("connection", "keep-alive")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    if SSE_READ_MODE == "chunk":
                        chunk = resp.read(4096)
                    else:
                        chunk = read_decoded_sse_line(resp)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                if conn:
                    conn.close()
                LIMITER.release(upstream.label)
        else:
            data = resp.read()
            self.send_header("content-length", str(len(data)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(data)
            if conn:
                conn.close()
            LIMITER.release(upstream.label)


def main() -> None:
    REGISTRY.maybe_reload(force=True)
    print(f"Guarded CC Max random proxy on {BIND}:{PORT}", flush=True)
    print(f"  active_upstreams_file: {ACTIVE_UPSTREAMS_FILE}", flush=True)
    print(f"  API_KEYS: {'enabled' if API_KEYS else 'disabled'}", flush=True)
    print(f"  sse_read_mode: {SSE_READ_MODE}", flush=True)
    print(f"  thinking_display_mode: {THINKING_DISPLAY_MODE}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
