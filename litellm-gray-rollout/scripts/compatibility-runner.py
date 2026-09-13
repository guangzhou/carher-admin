#!/usr/bin/env python3
"""Start the image's real LiteLLM proxy and exercise clone compatibility over HTTP."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from prisma import Prisma


CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BARRIER_NAMESPACE = 1981930
BARRIER_KEY = 2
# Second rendezvous: both legs must have OBSERVED the overlap before either one
# deletes its ProxyModel row, otherwise the faster leg's cleanup can make the
# slower leg's overlap check fail for a reason that has nothing to do with the
# version under test.
BARRIER_DONE_KEY = 3
ISOLATED_CHECKS = {"proxy_startup", "key_api", "proxy_model_api", "auth", "spend_logs"}
CONCURRENT_CHECKS = {"concurrent_budget", "concurrent_spend_logs", "concurrent_proxy_model"}


def fail(message: str) -> NoReturn:
    raise SystemExit(f"compatibility-runner: {message}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        fail("result path must not be a symlink")
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def execution_binding() -> dict[str, str]:
    required = (
        "GRAY_RUN_ID", "GRAY_GENERATION", "GRAY_RUNNER_SHA256", "GRAY_IMAGE_DIGEST",
        "GRAY_DB_IDENTITY", "POD_UID",
    )
    values = {name: os.environ.get(name, "") for name in required}
    if any(not value for value in values.values()):
        fail("execution binding environment is incomplete")
    if not ID_RE.fullmatch(values["GRAY_RUN_ID"]) or not ID_RE.fullmatch(values["GRAY_GENERATION"]):
        fail("execution binding run identity is invalid")
    if not CHECKSUM_RE.fullmatch(values["GRAY_RUNNER_SHA256"]) or not CHECKSUM_RE.fullmatch(values["GRAY_IMAGE_DIGEST"]):
        fail("execution binding checksum is invalid")
    if not ID_RE.fullmatch(values["GRAY_DB_IDENTITY"]) or not ID_RE.fullmatch(values["POD_UID"]):
        fail("execution binding database or Pod identity is invalid")
    return {
        "run_id": values["GRAY_RUN_ID"],
        "generation": values["GRAY_GENERATION"],
        "runner_sha256": values["GRAY_RUNNER_SHA256"],
        "image_digest": values["GRAY_IMAGE_DIGEST"],
        "db_identity": values["GRAY_DB_IDENTITY"],
        "pod_uid": values["POD_UID"],
    }


async def db_digest(connection: Prisma) -> str:
    rows = await connection.query_raw(
        "SELECT current_database() AS database_name, current_user AS database_user, "
        "COALESCE(inet_server_addr()::text, 'local') AS server_addr, "
        "COALESCE(inet_server_port(), 0)::integer AS server_port"
    )
    if len(rows) != 1:
        fail("cannot fingerprint compatibility database target")
    return digest(rows[0])


def request(
    method: str, path: str, *, token: str, body: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    req = urllib.request.Request(f"http://127.0.0.1:4000{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            payload = json.loads(raw) if raw else {}
            return payload, {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        # The proxy puts the actionable reason in the error body. Dropping it (the
        # old behaviour) turns every 4xx/5xx into an unactionable status line and
        # forces a second run just to learn what went wrong.
        try:
            detail = exc.read().decode("utf-8", "replace")[:2000]
        except Exception:  # pragma: no cover - body already consumed/closed
            detail = "<error body unavailable>"
        fail(f"{method} {path} failed: {exc} body={detail}")
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        fail(f"{method} {path} failed: {exc}")


def proxy_log_tail(log_path: Path, limit: int = 12000) -> str:
    """Last `limit` bytes of the proxy log, decoded leniently.

    The proxy prints its startup diagnosis (engine resolution, DB handshake,
    config errors) in the FIRST lines and its request errors in the last ones.
    Piping it into the runner and quoting only a slice on failure destroyed the
    half that mattered, so the full log now lives in a file on the evidence
    mount and this only produces the human-readable tail.
    """
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            text = handle.read().decode("utf-8", "replace")
        return f"[{size} bytes total, tail of {min(size, limit)}]\n{text}"
    except OSError as exc:
        return f"<proxy log unavailable: {exc}>"


def start_proxy(config: Path, log_path: Path) -> subprocess.Popen[bytes]:
    entrypoint = Path("/app/docker/prod_entrypoint.sh")
    if not entrypoint.is_file():
        fail("real LiteLLM proxy entrypoint is missing from the image")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # A pipe with no reader deadlocks the proxy once it fills the 64 KiB buffer,
    # which looked exactly like "readiness timed out". Redirect to a real file.
    handle = open(log_path, "wb")
    try:
        process = subprocess.Popen(
            [str(entrypoint), "--config", str(config), "--port", "4000", "--num_workers", "1"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        handle.close()
    deadline = time.monotonic() + 180
    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    while time.monotonic() < deadline:
        if process.poll() is not None:
            fail(
                f"LiteLLM proxy exited during startup with code {process.returncode}; "
                f"full log at {log_path}\n{proxy_log_tail(log_path)}"
            )
        try:
            request("GET", "/health/readiness", token=master_key)
            return process
        except SystemExit:
            time.sleep(1)
    fail(f"LiteLLM proxy readiness timed out; full log at {log_path}\n{proxy_log_tail(log_path)}")


async def wait_for_peer(connection: Prisma, key: int = BARRIER_KEY, label: str = "start") -> None:
    await connection.query_raw(
        "SELECT pg_advisory_lock_shared($1::integer, $2::integer)", BARRIER_NAMESPACE, key
    )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        rows = await connection.query_raw(
            "SELECT count(DISTINCT pid)::bigint AS peers FROM pg_locks "
            "WHERE locktype='advisory' AND classid=$1::oid AND objid=$2::oid "
            "AND mode='ShareLock' AND granted",
            BARRIER_NAMESPACE,
            key,
        )
        if rows and int(rows[0]["peers"] or 0) >= 2:
            return
        await asyncio.sleep(0.2)
    fail(f"concurrent compatibility peer did not reach the {label} barrier")


def admin_token() -> str:
    value = os.environ.get("LITELLM_MASTER_KEY", "")
    if not re.fullmatch(r"sk-[A-Za-z0-9._~-]{8,512}", value):
        fail("LITELLM_MASTER_KEY is missing or invalid")
    return value


async def application_probe(mode: str, connection: Prisma) -> list[str]:
    admin = admin_token()
    marker = f"gray-{os.environ['GRAY_RUN_ID']}-{mode}-{secrets.token_hex(4)}"
    model_id = f"{marker}-model"
    model_name = f"{marker}-name"
    generated, _ = request(
        "POST", "/key/generate", token=admin,
        body={"key_alias": marker, "models": ["gray-gate-mock"], "max_budget": 10},
    )
    key = generated.get("key") or generated.get("token")
    if not isinstance(key, str) or not key.startswith("sk-"):
        fail("/key/generate did not return a LiteLLM key")
    try:
        request("POST", "/key/update", token=admin, body={"key": key, "max_budget": 11})
        request(
            "POST", "/model/new", token=admin,
            body={
                "model_name": model_name,
                "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "os.environ/OPENAI_API_KEY"},
                "model_info": {"id": model_id},
            },
        )
        info, _ = request("GET", "/model/info", token=admin)
        if model_id not in json.dumps(info, sort_keys=True):
            fail("new ProxyModel is not visible through /model/info")
        # Take the lower bound from the database's own clock so the concurrency
        # window below cannot be widened or narrowed by pod/DB clock skew.
        clock = await connection.query_raw(
            "SELECT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS.MS') AS t"
        )
        if not clock:
            fail("cannot read the database clock")
        probe_started = str(clock[0]["t"])
        response, headers = request(
            "POST", "/v1/chat/completions", token=key,
            body={
                # No mock_response here on purpose: the proxy strips client-supplied
                # mock fields unless the key allows them, so it would quietly become
                # a real upstream call. The mock lives in the deployment config.
                "model": "gray-gate-mock",
                "messages": [{"role": "user", "content": "gray compatibility probe"}],
            },
        )
        if "gray compatibility ok" not in json.dumps(response):
            fail("authenticated mock inference did not return the expected response")
        # SpendLogs.request_id is the RESPONSE BODY id (get_spend_logs_id falls back
        # to litellm_call_id only when the response carries none). The x-litellm-call-id
        # header is a different value, so polling on it never matched a row.
        request_id = response.get("id") or headers.get("x-litellm-call-id") or headers.get("x-request-id")
        if not request_id:
            fail("inference response lacks a request identifier")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = await connection.query_raw(
                'SELECT request_id FROM "LiteLLM_SpendLogs" WHERE request_id = $1', request_id
            )
            if rows:
                break
            await asyncio.sleep(0.5)
        else:
            fail("inference did not create a SpendLogs terminal row")
        if mode.startswith("concurrent-"):
            peer_prefix = f"gray-{os.environ['GRAY_RUN_ID']}-concurrent-"
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                models = await connection.query_raw(
                    'SELECT count(*)::bigint AS count FROM "LiteLLM_ProxyModelTable" WHERE model_id LIKE $1',
                    peer_prefix + "%",
                )
                # model holds the LANDING model (openai/gpt-4o-mini); the requested
                # name lives in model_group. Filtering on `model` matched nothing.
                logs = await connection.query_raw(
                    'SELECT count(DISTINCT request_id)::bigint AS count FROM "LiteLLM_SpendLogs" '
                    "WHERE model_group = 'gray-gate-mock' AND \"startTime\" >= $1::timestamp",
                    probe_started,
                )
                if models and int(models[0]["count"] or 0) >= 2 and logs and int(logs[0]["count"] or 0) >= 2:
                    break
                await asyncio.sleep(0.5)
            else:
                fail("concurrent ProxyModel/SpendLogs writes did not overlap visibly")
            await wait_for_peer(connection, BARRIER_DONE_KEY, "overlap-observed")
            return sorted(CONCURRENT_CHECKS)
        return sorted(ISOLATED_CHECKS)
    finally:
        # A failed leg used to leave its ProxyModel row and key behind, so the
        # next leg started against a dirtier clone than the one it was meant to
        # qualify. Clean up on both paths, never masking the original error.
        for endpoint, payload in (("/model/delete", {"id": model_id}), ("/key/delete", {"keys": [key]})):
            try:
                request("POST", endpoint, token=admin, body=payload)
            except BaseException:
                pass


async def run(mode: str, config: Path, result_path: Path) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        fail("DATABASE_URL is required")
    binding = execution_binding()
    result: dict[str, Any] = {
        "tool": "compatibility-runner", "schema_version": 2, "mode": mode,
        "status": "RUNNING", "started_at": now(), "binding": binding, "checks": [],
        "rolled_back": False,
    }
    atomic_write(result_path, result)
    # No PRISMA_QUERY_ENGINE_BINARY override here. The image keeps its query
    # engines under root-only /root/.cache, and the Job runs as uid 0 exactly as
    # production does, so Prisma resolves them on its own. The former override
    # pointed at an engine exported by an init container that searched a path
    # which does not exist in this image.
    connection = Prisma()
    await connection.connect()
    binding["db_target_sha256"] = await db_digest(connection)
    atomic_write(result_path, result)
    log_path = result_path.parent / f"proxy-{mode}.log"
    result["proxy_log"] = str(log_path)
    proxy = start_proxy(config, log_path)
    barrier = mode.startswith("concurrent-")
    try:
        if barrier:
            await wait_for_peer(connection)
        result["checks"] = await application_probe(mode, connection)
        result.update(status="PASS", completed_at=now(), rolled_back=True)
        result["result_sha256"] = digest(result)
        atomic_write(result_path, result)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    except BaseException as exc:
        # Without the proxy's own log a failed leg only says which HTTP call
        # broke, never why. Carry the tail into the result so one run is enough.
        result.update(status="FAIL", completed_at=now(), failure=f"{type(exc).__name__}: {exc}")
        tail = proxy_log_tail(log_path)
        result["proxy_log_tail"] = tail
        atomic_write(result_path, result)
        # The Job's Pod is gone by the time anyone reads the evidence mount, so
        # the tail has to reach `kubectl logs` too, not only the result file.
        print(f"--- proxy log ({log_path}) ---\n{tail}", file=sys.stderr, flush=True)
        raise
    finally:
        if barrier:
            for key in (BARRIER_KEY, BARRIER_DONE_KEY):
                try:
                    await connection.query_raw(
                        "SELECT pg_advisory_unlock_shared($1::integer, $2::integer)",
                        BARRIER_NAMESPACE, key,
                    )
                except Exception:
                    pass
        await connection.disconnect()
        if proxy.poll() is None:
            os.killpg(proxy.pid, signal.SIGTERM)
        try:
            proxy.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proxy.pid, signal.SIGKILL)
            proxy.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("new", "old", "concurrent-new", "concurrent-old"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.mode, args.config, args.result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
