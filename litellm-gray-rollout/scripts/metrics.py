#!/usr/bin/env python3
"""Evaluate offline gray-rollout metrics without changing routing state."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TOOL = "metrics"
EVIDENCE_VERSION = 1
MAX_EVIDENCE_AGE = timedelta(minutes=10)
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_SPEND_LAG_SECONDS = 60
CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
CONFIG_CHECKSUM_RE = re.compile(r"^(?:test-mode|[0-9a-f]{64})$")
EVIDENCE_KEYS = {"schema_version", "captured_at", "payload_sha256", "source", "run_id", "generation", "config_checksum"}
METRIC_KEYS = {
    "evidence", "phase", "rollout_percent", "records", "hard_errors", "baseline",
    "backend_health", "spend_reconciliation",
}
MIN_SAMPLE = 100
THRESHOLDS = {
    "five_xx_delta": 0.01,
    "p95_ratio": 1.3,
    "p99_ratio": 1.5,
}
HARD_ERROR_CODES = {
    "prisma_error": "PRISMA_ERROR",
    "callback_import_error": "CALLBACK_IMPORT_ERROR",
    "callback_behavior_error": "CALLBACK_BEHAVIOR_ERROR",
    "redis_error": "REDIS_ERROR",
    "redis_deserialize_error": "REDIS_DESERIALIZE_ERROR",
    "redis_key_collision": "REDIS_KEY_COLLISION",
    "redis_eviction_anomaly": "REDIS_EVICTION_ANOMALY",
    "gray_pod_restart": "GRAY_POD_RESTART",
    "gray_spend_batch_error": "GRAY_SPEND_BATCH_ERROR",
    "spend_batch_error": "GRAY_SPEND_BATCH_ERROR",
}
ROLLBACK_PHASES = {"normal_gray", "convergence_ready"}
OFFLINE_PHASES = {"prod_offline_upgrading", "prod_verified"}
KNOWN_PHASES = {
    "preflight", "bridge_preparing", "bridge_verified", "normal_gray", "convergence_ready",
    "prod_offline_upgrading", "prod_verified", "committed", "rolled_back",
    "aborting_to_bridge", "aborted",
}
POOL_ALIASES = {
    "stable": "stable",
    "prod": "stable",
    "product": "stable",
    "canary": "canary",
    "gray": "canary",
    "grey": "canary",
    "guarded-old": "guarded-old",
    "guarded_old": "guarded-old",
}


def emit(payload: dict[str, Any], code: int) -> int:
    payload = dict(payload)
    payload.setdefault("schema_version", EVIDENCE_VERSION)
    payload["payload_sha256"] = digest({key: value for key, value in payload.items() if key != "payload_sha256"})
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
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


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


def error_result(errors: list[str], groups: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], int]:
    return {
        "tool": TOOL,
        "status": "ERROR",
        "dispatcher_recommendation": {
            "action": "alert_only",
            "reason_codes": sorted(set(errors)),
            "hard_trigger": False,
        },
        "groups": groups or [],
        "errors": sorted(set(errors)),
    }, 2


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def normalize_record(record: dict[str, Any]) -> tuple[str, str, int, float] | None:
    pool = record.get("pool_label", record.get("pool"))
    uri_class = record.get("uri_class")
    status_value = record.get("status", record.get("status_code"))
    latency_value = record.get("response_time", record.get("upstream_response_time", record.get("rt")))
    try:
        status = int(status_value)
    except (TypeError, ValueError):
        return None
    if status < 100 or status > 599:
        return None
    latency = number(latency_value)
    if not isinstance(pool, str) or not pool or not isinstance(uri_class, str) or not uri_class or latency is None or latency < 0:
        return None
    canonical_pool = POOL_ALIASES.get(pool.strip().lower())
    if canonical_pool is None:
        return None
    return canonical_pool, uri_class, status, latency


def summarize(records: list[Any]) -> tuple[list[dict[str, Any]], int]:
    buckets: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"statuses": [], "latencies": []})
    invalid = 0
    for record in records:
        if not isinstance(record, dict):
            invalid += 1
            continue
        normalized = normalize_record(record)
        if normalized is None:
            invalid += 1
            continue
        pool, uri_class, status, latency = normalized
        buckets[(pool, uri_class)]["statuses"].append(status)
        buckets[(pool, uri_class)]["latencies"].append(latency)

    result: list[dict[str, Any]] = []
    for (pool, uri_class), values in sorted(buckets.items()):
        statuses = values["statuses"]
        latencies = values["latencies"]
        five_xx = sum(1 for status in statuses if status >= 500)
        result.append(
            {
                "pool_label": pool,
                "uri_class": uri_class,
                "count": len(statuses),
                "five_xx_count": five_xx,
                "five_xx_rate": rounded(five_xx / len(statuses)),
                "p95": rounded(percentile(latencies, 0.95)),
                "p99": rounded(percentile(latencies, 0.99)),
            }
        )
    return result, invalid


def by_class(groups: list[dict[str, Any]], pool: str) -> dict[str, dict[str, Any]]:
    return {item["uri_class"]: item for item in groups if item["pool_label"] == pool}


def ratio(numerator: Any, denominator: Any) -> float | None:
    left, right = number(numerator), number(denominator)
    if left is None or right is None:
        return None
    if right == 0:
        return None if left == 0 else math.inf
    return left / right


def baseline_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    baseline = payload.get("baseline", [])
    if isinstance(baseline, dict):
        if "uri_class" in baseline:
            baseline = [baseline]
        else:
            baseline = [dict(value, uri_class=key) if isinstance(value, dict) else {} for key, value in baseline.items()]
    if not isinstance(baseline, list):
        return {}
    return {
        str(item["uri_class"]): item
        for item in baseline
        if isinstance(item, dict) and isinstance(item.get("uri_class"), str)
    }


def compare(gray: dict[str, dict[str, Any]], reference: dict[str, dict[str, Any]], reference_name: str) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    comparisons: list[dict[str, Any]] = []
    triggers: set[str] = set()
    alerts: set[str] = set()
    for uri_class, gray_group in sorted(gray.items()):
        ref = reference.get(uri_class)
        if not ref:
            alerts.add("REFERENCE_SAMPLE_MISSING")
            comparisons.append({"uri_class": uri_class, "reference": reference_name, "qualified": False, "reason": "missing_reference"})
            continue
        gray_count = int(gray_group.get("count", 0))
        ref_count = int(ref.get("count", 0))
        if gray_count < MIN_SAMPLE:
            alerts.add("INSUFFICIENT_GRAY_SAMPLE")
            comparisons.append({"uri_class": uri_class, "reference": reference_name, "qualified": False, "reason": "insufficient_gray_sample"})
            continue
        if ref_count < MIN_SAMPLE:
            alerts.add("INSUFFICIENT_REFERENCE_SAMPLE")
            comparisons.append({"uri_class": uri_class, "reference": reference_name, "qualified": False, "reason": "insufficient_reference_sample"})
            continue
        five_delta = float(gray_group["five_xx_rate"]) - float(ref.get("five_xx_rate", 0))
        p95_ratio = ratio(gray_group.get("p95"), ref.get("p95"))
        p99_ratio = ratio(gray_group.get("p99"), ref.get("p99"))
        item = {
            "uri_class": uri_class,
            "reference": reference_name,
            "qualified": True,
            "gray_count": gray_count,
            "reference_count": ref_count,
            "five_xx_delta": rounded(five_delta),
            "p95_ratio": rounded(p95_ratio),
            "p99_ratio": rounded(p99_ratio),
        }
        comparisons.append(item)
        if five_delta > THRESHOLDS["five_xx_delta"]:
            triggers.add("FIVE_XX_DELTA")
        if p95_ratio is not None and p95_ratio > THRESHOLDS["p95_ratio"]:
            triggers.add("P95_RATIO")
        if p99_ratio is not None and p99_ratio > THRESHOLDS["p99_ratio"]:
            triggers.add("P99_RATIO")
    return comparisons, triggers, alerts


def hard_errors(payload: dict[str, Any]) -> set[str] | None:
    source = payload.get("hard_errors", {})
    if not isinstance(source, dict):
        return None
    result: set[str] = set()
    for key, value in source.items():
        if str(key) not in HARD_ERROR_CODES:
            return None
        amount = number(value)
        if amount is None or amount < 0:
            return None
        if amount > 0:
            result.add(HARD_ERROR_CODES[str(key)])
    return result


def opaque_ids(value: Any) -> set[str] | None:
    if not isinstance(value, list):
        return None
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 256:
            return None
        result.add(item)
    return result


def spend_reconciliation(payload: dict[str, Any]) -> tuple[dict[str, Any], set[str], list[str]]:
    source = payload.get("spend_reconciliation")
    if source is None:
        return {"status": "NOT_PROVIDED"}, set(), []
    if not isinstance(source, dict) or set(source) - {
        "expected_request_ids", "terminal_request_ids", "failed_request_ids", "observed_lag_seconds"
    }:
        return {"status": "INVALID"}, set(), ["SPEND_RECONCILIATION_INVALID"]
    expected = opaque_ids(source.get("expected_request_ids"))
    terminal = opaque_ids(source.get("terminal_request_ids"))
    failed = opaque_ids(source.get("failed_request_ids"))
    lag = number(source.get("observed_lag_seconds"))
    if expected is None or terminal is None or failed is None or not expected or lag is None or lag < 0:
        return {"status": "INVALID"}, set(), ["SPEND_RECONCILIATION_INVALID"]
    if terminal & failed or not (terminal | failed).issubset(expected):
        return {"status": "INVALID"}, set(), ["SPEND_RECONCILIATION_INVALID"]
    missing = expected - terminal - failed
    triggers: set[str] = set()
    if failed or missing:
        triggers.add("SPEND_RECONCILIATION_FAILED")
    if lag > MAX_SPEND_LAG_SECONDS:
        triggers.add("SPEND_RECONCILIATION_LAG")
    summary = {
        "status": "PASS" if not triggers else "FAIL",
        "expected_count": len(expected),
        "terminal_count": len(terminal),
        "failed_count": len(failed),
        "missing_request_ids": sorted(missing),
        "failed_request_ids": sorted(failed),
        "observed_lag_seconds": rounded(lag),
        "request_ids_digest": digest(sorted(expected)),
    }
    return summary, triggers, []


def run(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    envelope_errors = evidence_errors(payload)
    if envelope_errors:
        return error_result(envelope_errors)
    if set(payload) - METRIC_KEYS:
        return error_result(["EVIDENCE_SCHEMA_UNKNOWN_FIELD"])
    if not {"phase", "rollout_percent", "records", "hard_errors"}.issubset(payload):
        return error_result(["EVIDENCE_SECTION_MISSING"])
    records = payload.get("records")
    if not isinstance(records, list):
        return error_result(["INVALID_RECORDS"])
    phase = payload.get("phase")
    rollout = number(payload.get("rollout_percent"))
    validation_errors: list[str] = []
    if not isinstance(phase, str) or phase not in KNOWN_PHASES:
        validation_errors.append("INVALID_PHASE")
    if rollout is None or rollout < 0 or rollout > 100 or not rollout.is_integer():
        validation_errors.append("INVALID_SPLIT")
    if validation_errors:
        return error_result(validation_errors)
    rollout = int(rollout)
    # A hard error must still dispatch to the safe bridge/rollback path even
    # when a comparison baseline is unavailable.  Baseline absence blocks a
    # clean health decision, but must never mask an already observed fault.
    hard_error_probe = hard_errors(payload)
    if hard_error_probe is None:
        return error_result(["INVALID_HARD_ERRORS"])
    if rollout >= 100 and not payload.get("baseline") and not hard_error_probe:
        return error_result(["BASELINE_EVIDENCE_MISSING"])

    groups, invalid_count = summarize(records)
    if invalid_count:
        return error_result(["INVALID_RECORD"], groups)
    errors: list[str] = []
    gray = by_class(groups, "canary")
    if rollout >= 100:
        mode = "baseline"
        reference = baseline_map(payload)
        comparisons, threshold_triggers, alerts = compare(gray, reference, "baseline")
    else:
        mode = "stable"
        reference = by_class(groups, "stable")
        comparisons, threshold_triggers, alerts = compare(gray, reference, "stable")
    if rollout > 0 and not gray:
        alerts.add("CANARY_SAMPLE_MISSING")

    hard = hard_error_probe
    spend_summary, spend_triggers, spend_errors = spend_reconciliation(payload)
    if rollout > 0 and spend_summary.get("status") == "NOT_PROVIDED":
        spend_errors.append("SPEND_RECONCILIATION_MISSING")
    if spend_errors:
        return error_result(spend_errors, groups)
    triggers = hard | threshold_triggers | spend_triggers
    reason_codes = sorted(triggers)
    if triggers:
        if phase in ROLLBACK_PHASES:
            health = payload.get("backend_health", {})
            if isinstance(health, dict) and health.get("prod") is True:
                action = "rollback"
            else:
                action = "alert_only"
                reason_codes = sorted(triggers | {"PROD_NOT_HEALTHY_FOR_ROLLBACK"})
        elif phase in OFFLINE_PHASES:
            health = payload.get("backend_health", {})
            if (
                isinstance(health, dict)
                and health.get("gray") is False
                and health.get("bridge") is True
            ):
                action = "abort_to_bridge"
            else:
                action = "alert_only"
                if not isinstance(health, dict) or health.get("gray") is not False:
                    reason_codes = sorted(triggers | {"GRAY_FAILURE_NOT_CONFIRMED"})
                if not isinstance(health, dict) or health.get("bridge") is not True:
                    reason_codes = sorted(set(reason_codes) | {"BRIDGE_NOT_HEALTHY_FOR_ABORT"})
        else:
            action = "alert_only"
        hard_trigger = True
        status = "FAIL"
        code = 1
    elif alerts:
        action = "alert_only"
        reason_codes = sorted(alerts)
        hard_trigger = False
        status = "PASS"
        code = 0
    elif phase in OFFLINE_PHASES:
        health = payload.get("backend_health", {})
        if isinstance(health, dict) and health.get("gray") is True and health.get("prod") is False:
            action = "hold_gray"
            reason_codes = ["PROD_OFFLINE_GRAY_HEALTHY"]
        else:
            action = "none"
        hard_trigger = False
        status = "PASS"
        code = 0
    else:
        action = "none"
        hard_trigger = False
        status = "PASS"
        code = 0

    result = {
        "tool": TOOL,
        "status": status,
        "run_id": payload["evidence"].get("run_id"),
        "generation": payload["evidence"].get("generation"),
        "config_checksum": payload["evidence"].get("config_checksum"),
        "captured_at": payload["evidence"]["captured_at"],
        "phase": phase,
        "rollout_percent": rollout,
        "comparison_mode": mode,
        "thresholds": {**THRESHOLDS, "minimum_sample": MIN_SAMPLE},
        "dispatcher_recommendation": {
            "action": action,
            "reason_codes": reason_codes,
            "hard_trigger": hard_trigger,
        },
        "groups": groups,
        "comparisons": comparisons,
        "spend_reconciliation": spend_summary,
        "backend_health": payload.get("backend_health", {}),
        "errors": sorted(set(errors)),
    }
    return result, code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", "-i", default="-", help="JSON evidence file, or - for stdin")
    args = parser.parse_args(argv)
    payload = load(args.input)
    if payload is None:
        return emit({"tool": TOOL, "status": "ERROR", "dispatcher_recommendation": {"action": "alert_only", "reason_codes": ["INVALID_JSON"], "hard_trigger": False}, "groups": [], "errors": ["INVALID_JSON"]}, 2)
    result, code = run(payload)
    return emit(result, code)


if __name__ == "__main__":
    raise SystemExit(main())
