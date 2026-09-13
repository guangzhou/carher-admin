#!/usr/bin/env python3
"""Assemble clone migration outputs into a check-migration.py evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn


CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
CONFIG_CHECKSUM_RE = re.compile(r"^(?:test-mode|[0-9a-f]{64})$")
IMAGE_RE = re.compile(
    r"^127\.0\.0\.1:5000/"
    r"[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$"
)
ISOLATED_CHECKS = {"proxy_startup", "key_api", "proxy_model_api", "auth", "spend_logs"}
CONCURRENT_CHECKS = {"concurrent_budget", "concurrent_spend_logs", "concurrent_proxy_model"}
MAX_RESULT_AGE = timedelta(minutes=30)
MAX_CLOCK_SKEW = timedelta(minutes=5)
ATTESTATION_KEYS = {
    "tool", "schema_version", "captured_at", "job_name", "job_uid", "pod_uid",
    "container", "container_image_id", "runner_config_name", "runner_sha256",
    "db_identity", "db_target_sha256", "run_id", "generation", "result_sha256",
    "payload_sha256",
}


def fail(message: str) -> NoReturn:
    raise SystemExit(f"collect-migration-evidence: {message}")


def digest(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(read_bytes(path, path.name)).hexdigest()


def read_bytes(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            fail(f"{label} must be a regular non-symlink file")
        return path.read_bytes()
    except OSError as exc:
        fail(f"cannot read {label}: {exc}")


def load_json(path: Path, label: str, expected: type) -> Any:
    raw = read_bytes(path, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        fail(f"{label} is invalid JSON")
    if not isinstance(value, expected):
        fail(f"{label} has invalid structure")
    return value


def validate_result_digest(payload: dict[str, Any], label: str) -> None:
    checksum = payload.get("result_sha256")
    canonical = {key: value for key, value in payload.items() if key != "result_sha256"}
    if not isinstance(checksum, str) or checksum != digest(canonical):
        fail(f"{label} result checksum mismatch")


def parse_timestamp(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        fail(f"{label} timestamp is invalid")
    if parsed.tzinfo is None:
        fail(f"{label} timestamp lacks timezone")
    parsed = parsed.astimezone(timezone.utc)
    age = datetime.now(timezone.utc) - parsed
    if age < -MAX_CLOCK_SKEW or age > MAX_RESULT_AGE:
        fail(f"{label} is stale")
    return parsed


def job_attestation(
    path: Path,
    *,
    label: str,
    result: dict[str, Any],
    run_id: str,
    generation: str,
    image: str,
    db_identity: str,
) -> dict[str, Any]:
    attestation = load_json(path, f"{label} attestation", dict)
    if set(attestation) != ATTESTATION_KEYS:
        fail(f"{label} attestation contract is invalid")
    checksum = attestation.get("payload_sha256")
    if checksum != digest({key: value for key, value in attestation.items() if key != "payload_sha256"}):
        fail(f"{label} attestation checksum mismatch")
    parse_timestamp(attestation.get("captured_at"), f"{label} attestation")
    result_binding = result.get("binding")
    expected_image = "sha256:" + image.rsplit("@sha256:", 1)[1]
    if (
        not isinstance(result_binding, dict)
        or result_binding.get("run_id") != run_id
        or result_binding.get("generation") != generation
        or result_binding.get("image_digest") != expected_image
        or result_binding.get("db_identity") != db_identity
        or attestation.get("tool") != "collect-job-attestation"
        or attestation.get("schema_version") != 1
        or attestation.get("run_id") != run_id
        or attestation.get("generation") != generation
        or attestation.get("db_identity") != db_identity
        or attestation.get("db_target_sha256") != result_binding.get("db_target_sha256")
        or attestation.get("pod_uid") != result_binding.get("pod_uid")
        or attestation.get("runner_sha256") != result_binding.get("runner_sha256")
        or attestation.get("result_sha256") != result.get("result_sha256")
        or not str(attestation.get("container_image_id", "")).endswith("@" + expected_image)
    ):
        fail(f"{label} result is not bound to the expected Job, image, database, and run")
    return attestation


def compatibility_result(path: Path, mode: str, checks: set[str]) -> dict[str, Any]:
    payload = load_json(path, mode, dict)
    validate_result_digest(payload, mode)
    if (
        payload.get("tool") != "compatibility-runner"
        or payload.get("schema_version") != 2
        or payload.get("mode") != mode
        or payload.get("status") != "PASS"
        or payload.get("rolled_back") is not True
        or not isinstance(payload.get("checks"), list)
        or set(payload["checks"]) != checks
        or len(payload["checks"]) != len(checks)
    ):
        fail(f"{mode} compatibility result is incomplete")
    parse_timestamp(payload.get("completed_at"), f"{mode} result")
    return payload


def secure_write_new(path: Path, payload: dict[str, Any]) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or not parent.is_dir():
        fail("output directory is unsafe")
    parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        fail("output already exists")
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        fail(f"cannot safely create output: {exc}")
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def require_args(args: argparse.Namespace, names: tuple[str, ...]) -> None:
    missing = [name.replace("_", "-") for name in names if getattr(args, name) is None]
    if missing:
        fail("missing required inputs: " + ", ".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-before", type=Path)
    parser.add_argument("--schema-after", type=Path)
    parser.add_argument("--expected-schema-after", type=Path)
    parser.add_argument("--schema-changes", type=Path)
    parser.add_argument("--migration-result", type=Path, required=True)
    parser.add_argument("--migration-attestation", type=Path)
    parser.add_argument("--online-gate", type=Path)
    parser.add_argument("--new-result", type=Path)
    parser.add_argument("--old-result", type=Path)
    parser.add_argument("--concurrent-new-result", type=Path)
    parser.add_argument("--concurrent-old-result", type=Path)
    parser.add_argument("--new-attestation", type=Path)
    parser.add_argument("--old-attestation", type=Path)
    parser.add_argument("--concurrent-new-attestation", type=Path)
    parser.add_argument("--concurrent-old-attestation", type=Path)
    parser.add_argument("--thresholds", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--target-image")
    parser.add_argument("--stable-image")
    parser.add_argument("--run-id")
    parser.add_argument("--generation")
    parser.add_argument("--config-checksum")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    migration = load_json(args.migration_result, "migration result", dict)
    if migration.get("partial_state") != "none" or migration.get("status") != "PASS":
        fail("migration result is partial or incomplete")
    require_args(
        args,
        (
            "schema_before", "schema_after", "expected_schema_after", "schema_changes",
            "migration_attestation", "online_gate", "new_result", "old_result",
            "concurrent_new_result", "concurrent_old_result", "new_attestation",
            "old_attestation", "concurrent_new_attestation", "concurrent_old_attestation",
            "thresholds", "ledger", "target_image",
            "stable_image", "run_id", "generation", "config_checksum", "output",
        ),
    )
    assert args.schema_before and args.schema_after and args.expected_schema_after
    assert args.schema_changes and args.migration_attestation and args.online_gate and args.new_result and args.old_result
    assert args.concurrent_new_result and args.concurrent_old_result and args.thresholds
    assert args.new_attestation and args.old_attestation
    assert args.concurrent_new_attestation and args.concurrent_old_attestation
    assert args.ledger and args.target_image and args.stable_image
    assert args.run_id and args.generation and args.config_checksum and args.output

    if not STATE_ID_RE.fullmatch(args.run_id) or not STATE_ID_RE.fullmatch(args.generation):
        fail("run-id or generation is invalid")
    if not CONFIG_CHECKSUM_RE.fullmatch(args.config_checksum):
        fail("config-checksum is invalid")
    if not IMAGE_RE.fullmatch(args.target_image) or not IMAGE_RE.fullmatch(args.stable_image):
        fail("target and stable images must use immutable 198 K3s local-registry digests")

    ledger = load_json(args.ledger, "migration ledger", dict)
    ledger_sha256 = digest(ledger)
    if migration.get("tool") != "migration-ledger-runner" or migration.get("schema_version") != 1:
        fail("migration result contract is invalid")
    validate_result_digest(migration, "migration")
    parse_timestamp(migration.get("completed_at"), "migration result")
    if migration.get("ledger_sha256") != ledger_sha256:
        fail("migration result ledger checksum mismatch")
    entries = migration.get("entries")
    if not isinstance(entries, list) or not entries:
        fail("migration result entries are incomplete")

    online = load_json(args.online_gate, "online gate", dict)
    observations = online.get("entries")
    if not isinstance(observations, dict) or set(observations) != {
        str(item.get("id")) for item in entries
    }:
        fail("online gate entry observations are incomplete")
    merged_entries: list[dict[str, Any]] = []
    for item in entries:
        identifier = item.get("id")
        observation = observations.get(identifier)
        if not isinstance(observation, dict) or set(observation) != {
            "lock_wait_ms", "observed_in_schema", "lock_mode", "table_rewrite"
        }:
            fail(f"online gate observation is incomplete for {identifier}")
        merged = dict(item)
        merged["lock_wait_ms"] = observation["lock_wait_ms"]
        merged["observed_in_schema"] = observation["observed_in_schema"]
        merged["lock_mode"] = observation["lock_mode"]
        merged["table_rewrite"] = observation["table_rewrite"]
        merged_entries.append(merged)

    required_online = {
        "lock_timeout_ms", "statement_timeout_ms", "migration_duration_ms",
        "workload_p95_ratio", "network_policy", "snapshot_counts", "entries",
    }
    if set(online) != required_online:
        fail("online gate summary has unknown or missing fields")
    thresholds = load_json(args.thresholds, "thresholds", dict)
    changes = load_json(args.schema_changes, "schema changes", list)
    if not all(isinstance(item, str) and item.strip() for item in changes):
        fail("schema changes must be non-empty SQL strings")

    new = compatibility_result(args.new_result, "new", ISOLATED_CHECKS)
    old = compatibility_result(args.old_result, "old", ISOLATED_CHECKS)
    concurrent_new = compatibility_result(
        args.concurrent_new_result, "concurrent-new", CONCURRENT_CHECKS
    )
    concurrent_old = compatibility_result(
        args.concurrent_old_result, "concurrent-old", CONCURRENT_CHECKS
    )
    attestations = {
        "migration": job_attestation(
            args.migration_attestation,
            label="migration",
            result=migration,
            run_id=args.run_id,
            generation=args.generation,
            image=args.target_image,
            db_identity="clone-a",
        ),
        "new": job_attestation(
            args.new_attestation, label="new", result=new, run_id=args.run_id,
            generation=args.generation, image=args.target_image, db_identity="clone-a",
        ),
        "old": job_attestation(
            args.old_attestation, label="old", result=old, run_id=args.run_id,
            generation=args.generation, image=args.stable_image, db_identity="clone-b",
        ),
        "concurrent-new": job_attestation(
            args.concurrent_new_attestation, label="concurrent-new", result=concurrent_new,
            run_id=args.run_id, generation=args.generation, image=args.target_image,
            db_identity="clone-c",
        ),
        "concurrent-old": job_attestation(
            args.concurrent_old_attestation, label="concurrent-old", result=concurrent_old,
            run_id=args.run_id, generation=args.generation, image=args.stable_image,
            db_identity="clone-c",
        ),
    }
    compatibility = {
        "A": {"status": "PASS", "checks": sorted(ISOLATED_CHECKS), "result_sha256": new["result_sha256"]},
        "B": {"status": "PASS", "checks": sorted(ISOLATED_CHECKS), "result_sha256": old["result_sha256"]},
        "C": {
            "status": "PASS",
            "checks": sorted(CONCURRENT_CHECKS),
            "result_sha256": digest(
                {
                    "concurrent-new": concurrent_new["result_sha256"],
                    "concurrent-old": concurrent_old["result_sha256"],
                }
            ),
        },
    }
    payload: dict[str, Any] = {
        "schema": {
            "before_checksum": file_digest(args.schema_before),
            "after_checksum": file_digest(args.schema_after),
            "expected_after_checksum": file_digest(args.expected_schema_after),
            "changes": changes,
        },
        "ddl_ledger": {
            "partial_state": "none",
            "entries": merged_entries,
            **{key: value for key, value in online.items() if key != "entries"},
        },
        "thresholds": thresholds,
        "compatibility": compatibility,
        "binding": {
            "ledger_sha256": ledger_sha256,
            "target_image": args.target_image,
            "stable_image": args.stable_image,
            "attestations_sha256": digest(
                {name: value["payload_sha256"] for name, value in sorted(attestations.items())}
            ),
            "runner_sha256": migration["binding"]["runner_sha256"],
            "db_targets_sha256": {
                name: value["db_target_sha256"] for name, value in sorted(attestations.items())
            },
        },
    }
    payload["evidence"] = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload_sha256": digest(payload),
        "source": "collect-migration-evidence.py",
        "run_id": args.run_id,
        "generation": args.generation,
        "config_checksum": args.config_checksum,
    }
    secure_write_new(args.output, payload)
    print(
        json.dumps(
            {"tool": "collect-migration-evidence", "status": "PASS", "output": str(args.output)},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
