#!/usr/bin/env python3
"""Execute an approved additive DDL ledger and persist partial-state evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from importlib.util import module_from_spec, spec_from_file_location

from prisma import Prisma


import re

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTRACT_SPEC = spec_from_file_location(
    "litellm_gray_migration_contract", Path(__file__).with_name("migration-contract.py")
)
if _CONTRACT_SPEC is None or _CONTRACT_SPEC.loader is None:
    raise SystemExit("migration-ledger-runner: cannot load migration contract")
_CONTRACT = module_from_spec(_CONTRACT_SPEC)
_CONTRACT_SPEC.loader.exec_module(_CONTRACT)
validate_add_column = _CONTRACT.validate_add_column


def fail(message: str) -> NoReturn:
    raise SystemExit(f"migration-ledger-runner: {message}")


def canonical_digest(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def database_target_digest(connection: Prisma) -> str:
    rows = await connection.query_raw(
        "SELECT current_database() AS database_name, current_user AS database_user, "
        "COALESCE(inet_server_addr()::text, 'local') AS server_addr, "
        "COALESCE(inet_server_port(), 0)::integer AS server_port"
    )
    if len(rows) != 1:
        fail("cannot fingerprint migration database target")
    return canonical_digest(rows[0])


def execution_binding() -> dict[str, str]:
    required = (
        "GRAY_RUN_ID",
        "GRAY_GENERATION",
        "GRAY_RUNNER_SHA256",
        "GRAY_IMAGE_DIGEST",
        "GRAY_DB_IDENTITY",
        "POD_UID",
    )
    values = {name: os.environ.get(name, "") for name in required}
    if any(not value for value in values.values()):
        fail("execution binding environment is incomplete")
    if not ID_RE.fullmatch(values["GRAY_RUN_ID"]) or not ID_RE.fullmatch(values["GRAY_GENERATION"]):
        fail("execution binding run identity is invalid")
    if not CHECKSUM_RE.fullmatch(values["GRAY_RUNNER_SHA256"]):
        fail("execution binding runner checksum is invalid")
    if not CHECKSUM_RE.fullmatch(values["GRAY_IMAGE_DIGEST"]):
        fail("execution binding image digest is invalid")
    if not ID_RE.fullmatch(values["GRAY_DB_IDENTITY"]) or not ID_RE.fullmatch(values["POD_UID"]):
        fail("execution binding database or pod identity is invalid")
    return {
        "run_id": values["GRAY_RUN_ID"],
        "generation": values["GRAY_GENERATION"],
        "runner_sha256": values["GRAY_RUNNER_SHA256"],
        "image_digest": values["GRAY_IMAGE_DIGEST"],
        "db_identity": values["GRAY_DB_IDENTITY"],
        "pod_uid": values["POD_UID"],
    }


def validate_ledger(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read ledger: {exc}")
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "statements"}:
        fail("ledger must contain only schema_version and statements")
    if payload.get("schema_version") != 1:
        fail("unsupported ledger schema_version")
    statements = payload.get("statements")
    if not isinstance(statements, list) or not statements:
        fail("ledger must contain at least one statement")
    seen: set[str] = set()
    required = {"id", "sql", "online_safe", "lock_mode", "table_rewrite"}
    for entry in statements:
        if not isinstance(entry, dict) or set(entry) != required:
            fail("each ledger statement must use the approved schema")
        identifier = entry.get("id")
        sql = entry.get("sql")
        if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier) or identifier in seen:
            fail("ledger statement id is invalid or duplicated")
        seen.add(identifier)
        if not isinstance(sql, str) or not sql.strip() or "\x00" in sql:
            fail(f"ledger statement {identifier} has invalid SQL")
        try:
            validate_add_column(sql)
        except ValueError as exc:
            fail(f"ledger statement {identifier} is outside the approved ADD COLUMN subset: {exc}")
        if entry.get("online_safe") is not True or entry.get("table_rewrite") is not False:
            fail(f"ledger statement {identifier} is not approved as online safe")
        if str(entry.get("lock_mode", "")).upper() != "ACCESS EXCLUSIVE":
            fail(f"ledger statement {identifier} has an unapproved lock declaration")
    digest = canonical_digest(payload)
    if not CHECKSUM_RE.fullmatch(expected_sha256) or digest != expected_sha256:
        fail("ledger checksum mismatch")
    return payload, digest


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        fail("result path must not be a symlink")
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


async def run(
    ledger: dict[str, Any], ledger_sha256: str, result_path: Path, binding: dict[str, str]
) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        fail("DATABASE_URL is required")
    result: dict[str, Any] = {
        "tool": "migration-ledger-runner",
        "schema_version": 1,
        "status": "RUNNING",
        "partial_state": "none",
        "ledger_sha256": ledger_sha256,
        "started_at": now(),
        "binding": binding,
        "entries": [],
    }
    atomic_write(result_path, result)
    os.environ["DATABASE_URL"] = database_url
    connection = Prisma()
    await connection.connect()
    try:
        result["binding"]["db_target_sha256"] = await database_target_digest(connection)
        atomic_write(result_path, result)
        for statement in ledger["statements"]:
            entry = {
                "id": statement["id"],
                "statement": statement["sql"].strip().removesuffix(";"),
                "status": "started",
                "online_safe": True,
                "lock_mode": statement["lock_mode"],
                "table_rewrite": False,
                "started_at": now(),
            }
            result["entries"].append(entry)
            result["partial_state"] = "running"
            atomic_write(result_path, result)
            started = time.monotonic()
            try:
                async with connection.tx() as transaction:
                    await transaction.execute_raw("SET LOCAL lock_timeout = '5s'")
                    await transaction.execute_raw("SET LOCAL statement_timeout = '15min'")
                    await transaction.execute_raw(entry["statement"])
            except Exception as exc:
                entry.update(
                    status="failed",
                    completed_at=now(),
                    duration_ms=round((time.monotonic() - started) * 1000, 3),
                    error_type=type(exc).__name__,
                )
                result.update(status="FAIL", partial_state="partial", completed_at=now())
                atomic_write(result_path, result)
                raise
            entry.update(
                status="completed",
                completed_at=now(),
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )
            atomic_write(result_path, result)
        result.update(status="PASS", partial_state="none", completed_at=now())
        result["result_sha256"] = canonical_digest(result)
        atomic_write(result_path, result)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    finally:
        await connection.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--ledger-sha256", required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    ledger, digest = validate_ledger(args.ledger, args.ledger_sha256)
    asyncio.run(run(ledger, digest, args.result, execution_binding()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
