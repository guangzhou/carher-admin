#!/usr/bin/env python3
"""Validate an offline LiteLLM migration evidence bundle.

This validator never runs DDL.  It reconciles an already captured schema diff,
DDL ledger, and clone A/B/C results into a deterministic gate decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TOOL = "check-migration"
EVIDENCE_VERSION = 1
MAX_EVIDENCE_AGE = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=5)
CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
CONFIG_CHECKSUM_RE = re.compile(r"^(?:test-mode|[0-9a-f]{64})$")
EVIDENCE_KEYS = {"schema_version", "captured_at", "payload_sha256", "source", "run_id", "generation", "config_checksum"}
MIGRATION_KEYS = {
    "evidence", "schema", "ddl_ledger", "ledger", "compatibility", "clones",
    "thresholds", "binding",
}
QUALIFICATION_PROBES = {
    "A": {"proxy_startup", "key_api", "proxy_model_api", "auth", "spend_logs"},
    "B": {"proxy_startup", "key_api", "proxy_model_api", "auth", "spend_logs"},
    "C": {"concurrent_budget", "concurrent_spend_logs", "concurrent_proxy_model"},
}
LEDGER_ENTRY_KEYS = {
    "id", "statement", "ddl", "sql", "status", "online_safe", "observed_in_schema",
    "duration_ms", "lock_wait_ms", "lock_mode", "table_rewrite", "started_at", "completed_at",
}
LEDGER_KEYS = {
    "partial_state", "entries", "lock_timeout_ms", "statement_timeout_ms",
    "migration_duration_ms", "workload_p95_ratio", "network_policy", "snapshot_counts",
}
THRESHOLD_KEYS = {"max_migration_duration_ms", "max_workload_p95_ratio", "max_lock_wait_ms"}
ALLOWED_LOCK_MODES = {"ACCESS EXCLUSIVE"}
DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DROP", re.compile(r"\bDROP\s+(?:TABLE|COLUMN|TYPE|INDEX|CONSTRAINT|SCHEMA)\b", re.I)),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\b", re.I)),
    ("ALTER_TYPE", re.compile(r"\bALTER\s+COLUMN\b[^;]*\bTYPE\b", re.I | re.S)),
    ("SET_NOT_NULL", re.compile(r"\bALTER\s+COLUMN\b[^;]*\bSET\s+NOT\s+NULL\b", re.I | re.S)),
    ("RENAME", re.compile(r"\bRENAME\s+(?:COLUMN|TABLE|TO)\b", re.I)),
)


def emit(payload: dict[str, Any], code: int) -> int:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return code


def load(path: str) -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def payload_digest(payload: dict[str, Any]) -> str:
    return digest({key: value for key, value in payload.items() if key != "evidence"})


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def evidence_errors(payload: dict[str, Any]) -> list[str]:
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        return ["EVIDENCE_MISSING" if evidence is None else "EVIDENCE_INVALID"]
    if set(evidence) - EVIDENCE_KEYS or evidence.get("schema_version") != EVIDENCE_VERSION:
        return ["EVIDENCE_INVALID"]
    captured_at = parse_timestamp(evidence.get("captured_at"))
    checksum = evidence.get("payload_sha256")
    if (
        captured_at is None
        or not isinstance(checksum, str)
        or not CHECKSUM_RE.fullmatch(checksum)
        or not isinstance(evidence.get("run_id"), str)
        or not STATE_ID_RE.fullmatch(evidence["run_id"])
        or not isinstance(evidence.get("generation"), str)
        or not STATE_ID_RE.fullmatch(evidence["generation"])
        or not isinstance(evidence.get("config_checksum"), str)
        or not CONFIG_CHECKSUM_RE.fullmatch(evidence["config_checksum"])
    ):
        return ["EVIDENCE_INVALID"]
    now = datetime.now(timezone.utc)
    if captured_at - now > MAX_CLOCK_SKEW or now - captured_at > MAX_EVIDENCE_AGE:
        return ["EVIDENCE_STALE"]
    if checksum != payload_digest(payload):
        return ["EVIDENCE_CHECKSUM_MISMATCH"]
    return []


def finite_non_negative(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number >= 0 and number != float("inf") and number != float("-inf") and number == number


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            statement = item.get("statement", item.get("ddl", item.get("sql")))
            if statement is not None:
                result.append(str(statement))
    return result


def destructive_kinds(statements: list[str]) -> list[str]:
    result = {
        name
        for name, pattern in DESTRUCTIVE_PATTERNS
        for statement in statements
        if pattern.search(statement)
    }
    for statement in statements:
        normalized = re.sub(r"\s+", " ", statement).upper()
        if re.search(r"\bADD\s+(?:COLUMN\s+)?", normalized) and "NOT NULL" in normalized and "DEFAULT" not in normalized:
            result.add("NOT_NULL_NO_DEFAULT")
    return sorted(result)


def status_pass(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        value = value.get("status", value.get("result", value.get("pass")))
    return str(value).upper() in {"PASS", "PASSED", "OK", "TRUE"}


def compatibility_result(value: Any, clone: str) -> tuple[bool, str | None]:
    if not isinstance(value, dict) or value.get("status") != "PASS":
        return False, None
    checksum = value.get("result_sha256")
    checks = value.get("checks")
    if (
        not isinstance(checksum, str)
        or not CHECKSUM_RE.fullmatch(checksum)
        or not isinstance(checks, list)
        or set(checks) != QUALIFICATION_PROBES[clone]
        or len(checks) != len(set(checks))
    ):
        return False, None
    return True, checksum


def run(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    evidence_failure = evidence_errors(payload)
    if evidence_failure:
        return {"tool": TOOL, "status": "ERROR", "reason_codes": evidence_failure, "errors": evidence_failure}, 2
    if set(payload) - MIGRATION_KEYS:
        errors = ["EVIDENCE_SCHEMA_UNKNOWN_FIELD"]
        return {"tool": TOOL, "status": "ERROR", "reason_codes": errors, "errors": errors}, 2
    if "schema" not in payload or not ({"ddl_ledger", "ledger"} & set(payload)) or not ({"compatibility", "clones"} & set(payload)):
        errors = ["EVIDENCE_SECTION_MISSING"]
        return {"tool": TOOL, "status": "ERROR", "reason_codes": errors, "errors": errors}, 2
    schema = payload.get("schema", {})
    ledger = payload.get("ddl_ledger", payload.get("ledger", {}))
    compatibility = payload.get("compatibility", payload.get("clones", {}))
    binding = payload.get("binding")
    thresholds = payload.get("thresholds")
    if not isinstance(schema, dict) or not isinstance(ledger, dict) or not isinstance(compatibility, dict):
        result = {"tool": TOOL, "status": "ERROR", "reason_codes": ["INVALID_STRUCTURE"], "errors": ["INVALID_STRUCTURE"]}
        return result, 2
    if not isinstance(thresholds, dict) or set(thresholds) != THRESHOLD_KEYS:
        reasons = ["MIGRATION_THRESHOLDS_MISSING" if thresholds is None else "MIGRATION_THRESHOLDS_INVALID"]
        return {"tool": TOOL, "status": "ERROR", "reason_codes": reasons, "errors": reasons}, 2
    if any(not finite_non_negative(thresholds.get(name)) or float(thresholds[name]) <= 0 for name in THRESHOLD_KEYS):
        reasons = ["MIGRATION_THRESHOLDS_INVALID"]
        return {"tool": TOOL, "status": "ERROR", "reason_codes": reasons, "errors": reasons}, 2

    if not {"before_checksum", "after_checksum", "expected_after_checksum"}.issubset(schema) or not ({"changes", "diff"} & set(schema)):
        reasons = ["SCHEMA_EVIDENCE_INVALID"]
        return {"tool": TOOL, "status": "ERROR", "reason_codes": reasons, "errors": reasons}, 2
    changes = string_list(schema.get("changes", schema.get("diff", [])))
    reasons: set[str] = set()
    if set(ledger) - LEDGER_KEYS:
        reasons.add("DDL_LEDGER_INVALID")
    entries = ledger.get("entries", [])
    if not isinstance(entries, list):
        entries = []
    ledger_statements = string_list(entries)
    statements = changes + ledger_statements
    destructive = destructive_kinds(statements)
    if destructive:
        reasons.add("DESTRUCTIVE_DDL")

    online_gate_fields = {
        "lock_timeout_ms", "statement_timeout_ms", "migration_duration_ms",
        "workload_p95_ratio", "network_policy", "snapshot_counts",
    }
    if not online_gate_fields.issubset(ledger):
        reasons.add("ONLINE_GATE_EVIDENCE_MISSING")
    else:
        for name in (
            "lock_timeout_ms", "statement_timeout_ms", "migration_duration_ms",
            "workload_p95_ratio",
        ):
            if not finite_non_negative(ledger.get(name)):
                reasons.add("ONLINE_GATE_EVIDENCE_INVALID")
        if finite_non_negative(ledger.get("migration_duration_ms")):
            if float(ledger["migration_duration_ms"]) > float(thresholds["max_migration_duration_ms"]):
                reasons.add("MIGRATION_DURATION_THRESHOLD_EXCEEDED")
        if finite_non_negative(ledger.get("workload_p95_ratio")):
            if float(ledger["workload_p95_ratio"]) > float(thresholds["max_workload_p95_ratio"]):
                reasons.add("WORKLOAD_P95_THRESHOLD_EXCEEDED")
        network = ledger.get("network_policy")
        if not isinstance(network, dict) or set(network) != {"allowed_probe", "denied_probe"}:
            reasons.add("ONLINE_GATE_EVIDENCE_INVALID")
        elif not all(status_pass(network.get(name)) for name in ("allowed_probe", "denied_probe")):
            reasons.add("NETWORK_POLICY_NOT_ENFORCED")
        counts = ledger.get("snapshot_counts")
        if not isinstance(counts, dict) or set(counts) != {"expected", "restored"}:
            reasons.add("ONLINE_GATE_EVIDENCE_INVALID")
        else:
            expected_counts, restored_counts = counts.get("expected"), counts.get("restored")
            if not isinstance(expected_counts, dict) or not expected_counts or not isinstance(restored_counts, dict):
                reasons.add("ONLINE_GATE_EVIDENCE_INVALID")
            elif expected_counts != restored_counts:
                reasons.add("SNAPSHOT_COUNT_MISMATCH")
            elif any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in expected_counts.values()):
                reasons.add("ONLINE_GATE_EVIDENCE_INVALID")

    partial_state = str(ledger.get("partial_state", "unknown")).lower()
    if partial_state != "none":
        reasons.add("PARTIAL_DDL_DETECTED")
        if partial_state in {"", "unknown", "partial", "failed"}:
            reasons.add("PARTIAL_DDL_UNKNOWN")

    completed_ids: list[str] = []
    observed_ids: list[str] = []
    unsafe_ids: list[str] = []
    incomplete_ids: list[str] = []
    invalid_duration_ids: list[str] = []
    invalid_lock_ids: list[str] = []
    invalid_rewrite_ids: list[str] = []
    duplicate_ids: list[str] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            reasons.add("DDL_LEDGER_INVALID")
            continue
        if set(item) - LEDGER_ENTRY_KEYS:
            reasons.add("DDL_LEDGER_INVALID")
        raw_identifier = item.get("id")
        identifier = str(raw_identifier) if raw_identifier is not None else ""
        if not identifier or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", identifier):
            reasons.add("DDL_LEDGER_INVALID_ID")
            identifier = f"invalid-{index + 1:03d}"
        elif identifier in seen_ids:
            duplicate_ids.append(identifier)
            reasons.add("DDL_LEDGER_DUPLICATE_ID")
        seen_ids.add(identifier)
        if not finite_non_negative(item.get("duration_ms")):
            invalid_duration_ids.append(identifier)
            reasons.add("DDL_LEDGER_INVALID_DURATION")
        if not finite_non_negative(item.get("lock_wait_ms")):
            invalid_lock_ids.append(identifier)
            reasons.add("DDL_LEDGER_INVALID_LOCK_EVIDENCE")
        elif float(item["lock_wait_ms"]) > float(thresholds["max_lock_wait_ms"]):
            reasons.add("DDL_LOCK_WAIT_THRESHOLD_EXCEEDED")
        lock_mode = item.get("lock_mode")
        if not isinstance(lock_mode, str) or not lock_mode.strip():
            invalid_lock_ids.append(identifier)
            reasons.add("DDL_LEDGER_INVALID_LOCK_EVIDENCE")
        elif lock_mode.strip().upper() not in ALLOWED_LOCK_MODES:
            reasons.add("DDL_UNAPPROVED_LOCK_MODE")
        if not isinstance(item.get("table_rewrite"), bool):
            invalid_rewrite_ids.append(identifier)
            reasons.add("DDL_LEDGER_INVALID_REWRITE_EVIDENCE")
        elif item.get("table_rewrite") is True:
            reasons.add("DDL_TABLE_REWRITE_DETECTED")
        if str(item.get("status", "")).lower() == "completed":
            completed_ids.append(identifier)
        else:
            incomplete_ids.append(identifier)
        if item.get("observed_in_schema") is True:
            observed_ids.append(identifier)
        if item.get("online_safe") is not True:
            unsafe_ids.append(identifier)
    if incomplete_ids:
        reasons.add("DDL_LEDGER_INCOMPLETE")
    if unsafe_ids:
        reasons.add("ONLINE_DDL_UNSAFE")

    before = str(schema.get("before_checksum", ""))
    after = str(schema.get("after_checksum", ""))
    expected_after = str(schema.get("expected_after_checksum", ""))
    if not before or not after or not expected_after:
        reasons.add("SCHEMA_CHECKSUM_MISSING")
    elif not all(CHECKSUM_RE.fullmatch(value) for value in (before, after, expected_after)):
        reasons.add("SCHEMA_CHECKSUM_INVALID")
    elif after != expected_after:
        reasons.add("SCHEMA_CHECKSUM_MISMATCH")

    if changes:
        path = "A"
        if not entries:
            reasons.add("DDL_LEDGER_MISSING")
        if (
            len(completed_ids) != len(entries)
            or len(observed_ids) != len(entries)
            or sorted(changes) != sorted(ledger_statements)
        ):
            reasons.add("DDL_SCHEMA_RECONCILIATION_FAILED")
    else:
        path = "B"
        if entries:
            reasons.add("PATH_B_HAS_DDL")
        if before and after and before != after:
            reasons.add("EMPTY_DIFF_CHECKSUM_CHANGED")

    compatibility_summary: dict[str, str] = {}
    compatibility_checksums: dict[str, str] = {}
    for clone in ("A", "B", "C"):
        if clone not in compatibility:
            reasons.add("COMPATIBILITY_RESULT_MISSING")
            compatibility_summary[clone] = "MISSING"
        elif binding is None and status_pass(compatibility[clone]):
            compatibility_summary[clone] = "PASS"
        else:
            valid, result_checksum = compatibility_result(compatibility[clone], clone)
            if valid and result_checksum is not None:
                compatibility_summary[clone] = "PASS"
                compatibility_checksums[clone] = result_checksum
            else:
                reasons.add("COMPATIBILITY_RESULT_FAILED")
                compatibility_summary[clone] = "FAIL"

    binding_summary: dict[str, str] | None = None
    if binding is not None:
        if (
            not isinstance(binding, dict)
            or set(binding) != {
                "ledger_sha256", "target_image", "stable_image", "attestations_sha256",
                "runner_sha256", "db_targets_sha256",
            }
            or not CHECKSUM_RE.fullmatch(str(binding.get("ledger_sha256", "")))
            or not CHECKSUM_RE.fullmatch(str(binding.get("attestations_sha256", "")))
            or not CHECKSUM_RE.fullmatch(str(binding.get("runner_sha256", "")))
            or not isinstance(binding.get("db_targets_sha256"), dict)
            or set(binding["db_targets_sha256"]) != {
                "migration", "new", "old", "concurrent-new", "concurrent-old"
            }
            or not all(CHECKSUM_RE.fullmatch(str(value)) for value in binding["db_targets_sha256"].values())
            or not all(
                isinstance(binding.get(name), str)
                and "@sha256:" in binding[name]
                and CHECKSUM_RE.fullmatch("sha256:" + binding[name].rsplit("@sha256:", 1)[1])
                for name in ("target_image", "stable_image")
            )
        ):
            reasons.add("QUALIFICATION_BINDING_INVALID")
        else:
            binding_summary = {
                "ledger_sha256": binding["ledger_sha256"],
                "target_image": binding["target_image"],
                "stable_image": binding["stable_image"],
                "attestations_sha256": binding["attestations_sha256"],
                "runner_sha256": binding["runner_sha256"],
                "db_targets_sha256": binding["db_targets_sha256"],
            }

    reason_codes = sorted(reasons)
    status = "PASS" if not reason_codes else "FAIL"
    result = {
        "tool": TOOL,
        "status": status,
        "path": path,
        "reason_codes": reason_codes,
        "errors": reason_codes,
        "schema": {
            "change_count": len(changes),
            "changes_digest": digest(changes),
            "checksum_match": bool(expected_after and after == expected_after),
            "destructive_kinds": destructive,
        },
        "ddl_ledger": {
            "entry_count": len(entries),
            "partial_state": partial_state,
            "completed_ids": sorted(completed_ids),
            "observed_ids": sorted(observed_ids),
            "unsafe_ids": sorted(unsafe_ids),
            "incomplete_ids": sorted(incomplete_ids),
            "duplicate_ids": sorted(set(duplicate_ids)),
            "invalid_duration_ids": sorted(invalid_duration_ids),
            "invalid_lock_ids": sorted(invalid_lock_ids),
            "invalid_rewrite_ids": sorted(invalid_rewrite_ids),
            "ledger_digest": digest(entries),
        },
        "compatibility": compatibility_summary,
        "compatibility_result_sha256": compatibility_checksums,
    }
    if binding_summary is not None:
        result["qualification"] = {
            "schema_version": 1,
            "status": status,
            "run_id": payload["evidence"]["run_id"],
            "generation": payload["evidence"]["generation"],
            "config_checksum": payload["evidence"]["config_checksum"],
            "source_payload_sha256": payload["evidence"]["payload_sha256"],
            "captured_at": payload["evidence"]["captured_at"],
            "path": path,
            **binding_summary,
            "schema_before_checksum": before,
            "schema_after_checksum": after,
            "compatibility": compatibility_summary,
            "compatibility_result_sha256": compatibility_checksums,
            "online_gate": {
                "network_policy": ledger.get("network_policy"),
                "snapshot_counts": ledger.get("snapshot_counts"),
                "partial_state": partial_state,
                "migration_duration_ms": ledger.get("migration_duration_ms"),
                "workload_p95_ratio": ledger.get("workload_p95_ratio"),
            },
        }
        result["qualification"]["qualification_sha256"] = digest(result["qualification"])
    return result, 0 if status == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", "-i", default="-", help="JSON evidence file, or - for stdin")
    args = parser.parse_args(argv)
    payload = load(args.input)
    if payload is None:
        return emit({"tool": TOOL, "status": "ERROR", "reason_codes": ["INVALID_JSON"], "errors": ["INVALID_JSON"]}, 2)
    result, code = run(payload)
    return emit(result, code)


if __name__ == "__main__":
    raise SystemExit(main())
