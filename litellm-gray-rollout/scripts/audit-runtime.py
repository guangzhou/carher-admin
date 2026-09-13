#!/usr/bin/env python3
"""Audit offline runtime evidence for the LiteLLM gray rollout.

The command deliberately accepts evidence rather than contacting a cluster.  It
is therefore safe to run in CI or on a copied, redacted evidence bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


TOOL = "audit-runtime"
EVIDENCE_VERSION = 1
MAX_EVIDENCE_AGE = timedelta(minutes=15)
MAX_CLOCK_SKEW = timedelta(minutes=5)
CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
CONFIG_CHECKSUM_RE = re.compile(r"^(?:test-mode|[0-9a-f]{64})$")
EVIDENCE_KEYS = {"schema_version", "captured_at", "payload_sha256", "source", "run_id", "generation", "config_checksum"}
RUNTIME_KEYS = {
    "evidence", "expected_inventory", "references", "bypasses", "callbacks", "callback_imports", "api_surfaces",
    "api_surface", "acct", "redis", "scheduler", "mutation_visibility", "spendlogs", "sources",
}
REQUIRED_RUNTIME_SECTIONS = {"expected_inventory", "references", "callbacks", "api_surfaces", "acct", "redis", "scheduler", "mutation_visibility"}
SOURCE_SECTIONS = REQUIRED_RUNTIME_SECTIONS
SOURCE_KEYS = {"section", "source", "captured_at", "freshness_seconds", "payload_sha256"}
INVENTORY_KEYS = {"callbacks", "api_surfaces", "acct_fields", "mutation_operations"}
CALLBACK_KEYS = {
    "name", "import_ok", "behavior_ok", "probe_sha256", "image_digest",
    "config_sha256", "captured_at",
}
SMOKE_KEYS = {"status", "request_id", "captured_at", "response_sha256"}
MUTATION_KEYS = {
    "operation", "writer", "reader", "request_id", "written_at", "observed_at",
    "latency_ms", "sla_ms", "result_sha256", "status",
}
SCHEDULER_KEYS = {
    "mode",
    "profile",
    "image_digest",
    "config_sha256",
    "callbacks_sha256",
    "captured_at",
    "source",
    "source_payload_sha256",
    "observations",
}
SCHEDULER_OBSERVATION_KEYS = {
    "duplicate_scheduler_runs",
    "duplicate_background_jobs",
    "unexpected_control_writes",
}
CONTROL_WRITE_RE = re.compile(
    r"^/(?:model/(?:new|delete|update|edit)|key/(?:new|update|delete|generate)|"
    r"team/(?:new|update|delete)|budget/(?:new|update|delete)|quota/(?:new|update|delete)|"
    r"user/(?:new|update|delete|edit)|organization/(?:new|update|delete|edit)|"
    r"customer/(?:new|update|delete|edit))"
    r"(?:/|$)",
    re.I,
)
CONTROL_READ_RE = re.compile(
    r"^/(?:model/(?:info|list|shadow|aliases?)|key(?:/|$)|team(?:/|$)|budget(?:/|$)|"
    r"user(?:/|$)|organization(?:/|$)|customer(?:/|$)|health(?:/|$)|model/info)(?:/|$)",
    re.I,
)
INFERENCE_RE = re.compile(
    r"^/(?:v1/|chat/completions|responses|messages|embeddings|images/|audio/|"
    r"rerank|batch|count_tokens|health/readiness)",
    re.I,
)
def _emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return code


def _load_json(path: str) -> tuple[dict[str, Any] | None, int]:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, 2
    if not isinstance(parsed, dict):
        return None, 2
    return parsed, 0


def _digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def _payload_digest(payload: dict[str, Any]) -> str:
    return _digest({key: value for key, value in payload.items() if key != "evidence"})


def _parse_timestamp(value: Any) -> datetime | None:
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


def _evidence_errors(payload: dict[str, Any]) -> list[str]:
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        return ["EVIDENCE_MISSING" if evidence is None else "EVIDENCE_INVALID"]
    if set(evidence) - EVIDENCE_KEYS:
        return ["EVIDENCE_INVALID"]
    if evidence.get("schema_version") != EVIDENCE_VERSION:
        return ["EVIDENCE_INVALID"]
    captured_at = _parse_timestamp(evidence.get("captured_at"))
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
    if checksum != _payload_digest(payload):
        return ["EVIDENCE_CHECKSUM_MISMATCH"]
    return []


def _as_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, dict):
        value = value.keys()
    if isinstance(value, (str, bytes)):
        value = [value]
    if not isinstance(value, Iterable):
        return set()
    return {str(item) for item in value if str(item)}


def _explanation_set(value: Any) -> set[str]:
    """Read either {item: reason}, [item], or [{item: ...}] explanations."""
    if isinstance(value, dict):
        return {str(k) for k, v in value.items() if v}
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            if isinstance(item, str):
                result.add(item)
            elif isinstance(item, dict):
                key = item.get("item", item.get("id", item.get("name")))
                if key is not None and item.get("reason", item.get("explanation", True)):
                    result.add(str(key))
        return result
    return set()


def _extract_path(reference: dict[str, Any]) -> tuple[str, str]:
    method = str(reference.get("method", "")).upper() or "UNKNOWN"
    explicit = reference.get("uri") or reference.get("path") or reference.get("url")
    candidate = str(explicit or reference.get("text") or reference.get("command") or "")
    if explicit:
        match = re.search(r"(?:https?://[^\s'\"]+)?(?::30402)?(/[^\s'\";,)]*)", candidate)
    else:
        # Commands often contain an absolute script path before the URL. Anchor
        # extraction to :30402 so /root/task.py cannot be mistaken for the API.
        match = re.search(r":30402(/[^\s'\";,)]*)", candidate)
    path = match.group(1) if match else ""
    if not path.startswith("/"):
        path = "/" + path
    path = path.split("?", 1)[0].split("#", 1)[0]
    if method == "UNKNOWN":
        method_match = re.search(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD)\b", candidate, re.I)
        if method_match:
            method = method_match.group(1).upper()
    return method, path


def _classify(method: str, path: str) -> str:
    if CONTROL_WRITE_RE.search(path) or (
        method in {"POST", "PUT", "PATCH", "DELETE"}
        and re.match(r"^/(?:model|key|team|budget|quota|user|organization|customer)(?:/|$)", path, re.I)
    ):
        return "control_write"
    if CONTROL_READ_RE.search(path):
        return "control_read"
    if INFERENCE_RE.search(path):
        return "inference_probe"
    # A direct, unrecognised GET is safer to treat as a read; mutation methods
    # are classified as probes only after the explicit control-write check.
    return "control_read" if method in {"GET", "HEAD", "UNKNOWN"} else "inference_probe"


def _surface(path: str) -> str:
    match = re.match(r"^/(model|key|team|budget|quota|user|organization|customer)(?:/|$)", path, re.I)
    return match.group(1).lower() if match else "inference"


def _references(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    references = payload.get("references", payload.get("bypasses", []))
    if isinstance(references, dict):
        references = [references]
    if not isinstance(references, list):
        return [], ["BYPASS_EVIDENCE_INVALID"]
    if not references:
        return [], ["BYPASS_EVIDENCE_EMPTY"]
    result = []
    errors: list[str] = []
    for index, item in enumerate(references):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            errors.append("BYPASS_REFERENCE_INVALID")
            continue
        if "direct_30402" in item and not isinstance(item.get("direct_30402"), bool):
            errors.append("BYPASS_REFERENCE_INVALID")
            continue
        method, path = _extract_path(item)
        # Only direct product-port references are in scope.  Explicitly marked
        # entries are accepted for evidence exporters that omit the URL.
        raw = " ".join(str(item.get(key, "")) for key in ("text", "url", "path", "uri", "command"))
        if "30402" not in raw and not item.get("direct_30402", False):
            errors.append("BYPASS_REFERENCE_INVALID")
            continue
        if not path or method == "UNKNOWN":
            errors.append("BYPASS_REFERENCE_INVALID")
            continue
        category = _classify(method, path)
        result.append(
            {
                "index": index,
                "category": category,
                "method": method,
                "path": path or "/",
                "surface": _surface(path),
                "source_digest": _digest(item.get("source", index)),
            }
        )
    return result, errors


def _expected_inventory(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    inventory = payload.get("expected_inventory")
    if not isinstance(inventory, dict) or set(inventory) != INVENTORY_KEYS:
        return {}, ["EXPECTED_INVENTORY_INVALID"]
    callbacks = inventory.get("callbacks")
    surfaces = inventory.get("api_surfaces")
    acct_fields = inventory.get("acct_fields")
    operations = inventory.get("mutation_operations")
    if (
        not isinstance(callbacks, list) or not callbacks
        or not isinstance(surfaces, list) or not surfaces
        or not isinstance(acct_fields, list) or not acct_fields
        or not isinstance(operations, dict)
        or set(operations) != {"stable_to_gray", "gray_to_stable"}
        or any(not isinstance(value, list) or not value for value in operations.values())
        or any(not isinstance(item, str) or not item for group in (callbacks, surfaces, acct_fields) for item in group)
        or any(not isinstance(item, str) or not item for values in operations.values() for item in values)
        or any(len(group) != len(set(group)) for group in (callbacks, surfaces, acct_fields))
    ):
        return {}, ["EXPECTED_INVENTORY_INVALID"]
    return inventory, []


def _callback_summary(payload: dict[str, Any], expected: set[str]) -> tuple[dict[str, Any], list[str]]:
    callbacks = payload.get("callbacks", payload.get("callback_imports", []))
    if isinstance(callbacks, dict):
        callbacks = [callbacks]
    if not isinstance(callbacks, list) or not callbacks:
        return {"count": 0, "names_digest": _digest([]), "import_errors": 0}, ["CALLBACK_EVIDENCE_EMPTY"]
    errors: list[str] = []
    names: list[str] = []
    for item in callbacks:
        if not isinstance(item, dict) or set(item) != CALLBACK_KEYS:
            return {"count": 0, "names_digest": _digest([]), "import_errors": 0}, ["CALLBACK_EVIDENCE_INVALID"]
        name = item.get("name")
        captured_at = _parse_timestamp(item.get("captured_at"))
        if (
            not isinstance(name, str) or not name
            or item.get("import_ok") is not True
            or item.get("behavior_ok") is not True
            or not isinstance(item.get("probe_sha256"), str) or not CHECKSUM_RE.fullmatch(item["probe_sha256"])
            or not isinstance(item.get("image_digest"), str) or not IMAGE_DIGEST_RE.fullmatch(item["image_digest"])
            or not isinstance(item.get("config_sha256"), str) or not CHECKSUM_RE.fullmatch(item["config_sha256"])
            or captured_at is None
        ):
            errors.append("CALLBACK_EVIDENCE_INVALID")
        else:
            age = datetime.now(timezone.utc) - captured_at
            if age < -MAX_CLOCK_SKEW or age > MAX_EVIDENCE_AGE:
                errors.append("CALLBACK_EVIDENCE_STALE")
        names.append(str(name or ""))
    if len(names) != len(set(names)) or set(names) != expected:
        errors.append("CALLBACK_INVENTORY_MISMATCH")
    return {"count": len(names), "names_digest": _digest(sorted(names)), "import_errors": len([x for x in errors if x == "CALLBACK_IMPORT_ERROR"])}, errors


def _api_summary(payload: dict[str, Any], frozen_expected: set[str]) -> tuple[dict[str, Any], list[str]]:
    source = payload.get("api_surfaces", payload.get("api_surface", {}))
    if isinstance(source, dict):
        expected = _as_set(source.get("expected", source.get("configured", [])))
        observed = _as_set(source.get("observed", source.get("enabled", [])))
        smoke = source.get("smoke", {}) if isinstance(source.get("smoke", {}), dict) else {}
        explanations = _explanation_set(source.get("explanations", {}))
    else:
        expected, observed, smoke, explanations = set(), set(), {}, set()
    missing = expected - observed
    unexpected = observed - expected
    unexplained = sorted((missing | unexpected) - explanations)
    errors: list[str] = []
    if not expected or not observed or not smoke:
        errors.append("API_SURFACE_EVIDENCE_INVALID")
    if expected != frozen_expected:
        errors.append("API_SURFACE_INVENTORY_MISMATCH")
    if unexplained:
        errors.append("API_SURFACE_DRIFT")
    smoke_keys = set(smoke)
    missing_smoke = expected - smoke_keys
    unexpected_smoke = smoke_keys - expected
    if missing_smoke:
        errors.append("API_SMOKE_COVERAGE_MISSING")
    if unexpected_smoke:
        errors.append("API_SMOKE_EVIDENCE_INVALID")
    for value in smoke.values():
        if not isinstance(value, dict) or set(value) != SMOKE_KEYS:
            errors.append("API_SMOKE_EVIDENCE_INVALID")
            continue
        captured_at = _parse_timestamp(value.get("captured_at"))
        if (
            value.get("status") != "PASS"
            or not isinstance(value.get("request_id"), str) or not value["request_id"]
            or not isinstance(value.get("response_sha256"), str) or not CHECKSUM_RE.fullmatch(value["response_sha256"])
            or captured_at is None
        ):
            errors.append("API_SMOKE_ERROR")
        elif datetime.now(timezone.utc) - captured_at > MAX_EVIDENCE_AGE:
            errors.append("API_SMOKE_STALE")
    return {
        "expected_count": len(expected),
        "observed_count": len(observed),
        "missing": sorted(missing),
        "unexpected": sorted(unexpected),
        "missing_smoke": sorted(missing_smoke),
        "unexpected_smoke": sorted(unexpected_smoke),
        "unexplained_differences": unexplained,
    }, errors


def _source_errors(payload: dict[str, Any]) -> list[str]:
    sources = payload.get("sources")
    if not isinstance(sources, list) or len(sources) != len(SOURCE_SECTIONS):
        return ["RUNTIME_SOURCE_EVIDENCE_INVALID"]
    errors: list[str] = []
    seen: set[str] = set()
    collected_at = _parse_timestamp(payload.get("evidence", {}).get("captured_at"))
    if collected_at is None:
        return ["RUNTIME_SOURCE_EVIDENCE_INVALID"]
    for item in sources:
        if not isinstance(item, dict) or set(item) != SOURCE_KEYS:
            errors.append("RUNTIME_SOURCE_EVIDENCE_INVALID")
            continue
        section = item.get("section")
        source = item.get("source")
        captured_at = _parse_timestamp(item.get("captured_at"))
        freshness = item.get("freshness_seconds")
        checksum = item.get("payload_sha256")
        if (
            not isinstance(section, str)
            or section not in SOURCE_SECTIONS
            or section in seen
            or not isinstance(source, str)
            or not source.strip()
            or captured_at is None
            or not isinstance(freshness, (int, float))
            or isinstance(freshness, bool)
            or freshness < 0
            or not isinstance(checksum, str)
            or not CHECKSUM_RE.fullmatch(checksum)
        ):
            errors.append("RUNTIME_SOURCE_EVIDENCE_INVALID")
            continue
        seen.add(section)
        actual_freshness = (collected_at - captured_at).total_seconds()
        if actual_freshness < -MAX_CLOCK_SKEW.total_seconds() or actual_freshness > MAX_EVIDENCE_AGE.total_seconds():
            errors.append("RUNTIME_SOURCE_STALE")
        if abs(max(0.0, actual_freshness) - float(freshness)) > 5:
            errors.append("RUNTIME_SOURCE_FRESHNESS_MISMATCH")
        if checksum != _digest(payload.get(section)):
            errors.append("RUNTIME_SOURCE_CHECKSUM_MISMATCH")
    if seen != SOURCE_SECTIONS:
        errors.append("RUNTIME_SOURCE_EVIDENCE_INVALID")
    return errors


def _acct_summary(payload: dict[str, Any], expected_fields: set[str]) -> tuple[dict[str, Any], list[str]]:
    acct = payload.get("acct", {})
    if not isinstance(acct, dict) or not acct:
        return {"counts": {}, "unexplained_differences": [], "set_digests": {}}, ["ACCT_EVIDENCE_EMPTY"]
    required = {"deployments", "services", "active_ready", "quota_take", "registered", "recent_requests"}
    if expected_fields != required:
        return {"counts": {}, "unexplained_differences": [], "set_digests": {}}, ["ACCT_INVENTORY_MISMATCH"]
    if not required.issubset(acct):
        return {"counts": {}, "unexplained_differences": [], "set_digests": {}}, ["ACCT_EVIDENCE_INVALID"]
    names = {
        "deployments": _as_set(acct.get("deployments")),
        "services": _as_set(acct.get("services")),
        "active_ready": _as_set(acct.get("active_ready", acct.get("active"))),
        "quota_take": _as_set(acct.get("quota_take", acct.get("take"))),
        "registered": _as_set(acct.get("registered")),
        "recent_requests": _as_set(acct.get("recent_requests")),
    }
    explanations = _explanation_set(acct.get("explanations", {}))
    differences: list[dict[str, Any]] = []

    def add(kind: str, values: set[str]) -> None:
        for value in sorted(values):
            if value not in explanations:
                differences.append({"kind": kind, "id": value})

    add("deployment_without_service", names["deployments"] - names["services"])
    add("service_without_deployment", names["services"] - names["deployments"])
    add("active_unregistered", names["active_ready"] - names["registered"])
    add("registered_not_ready", names["registered"] - names["active_ready"])
    add("take_not_ready", names["quota_take"] - names["active_ready"])
    add("request_unregistered", names["recent_requests"] - names["registered"])
    errors = ["ACCT_SET_DRIFT"] if differences else []
    summary = {
        "counts": {key: len(value) for key, value in sorted(names.items())},
        "unexplained_differences": differences,
        "set_digests": {key: _digest(sorted(value)) for key, value in sorted(names.items())},
    }
    return summary, errors


def _run(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    evidence_errors = _evidence_errors(payload)
    if evidence_errors:
        result = {"tool": TOOL, "status": "ERROR", "reason_codes": evidence_errors, "errors": evidence_errors}
        return result, 2
    unknown = set(payload) - RUNTIME_KEYS
    if unknown:
        errors = ["EVIDENCE_SCHEMA_UNKNOWN_FIELD"]
        return {"tool": TOOL, "status": "ERROR", "reason_codes": errors, "errors": errors}, 2
    missing = REQUIRED_RUNTIME_SECTIONS - set(payload)
    if missing:
        errors = ["EVIDENCE_SECTION_MISSING"]
        return {"tool": TOOL, "status": "ERROR", "reason_codes": errors, "errors": errors, "missing_sections": sorted(missing)}, 2
    inventory, inventory_errors = _expected_inventory(payload)
    references, reference_errors = _references(payload)
    counts = {name: 0 for name in ("control_read", "control_write", "inference_probe")}
    for reference in references:
        counts[reference["category"]] += 1
    callback_summary, callback_errors = _callback_summary(payload, _as_set(inventory.get("callbacks")))
    api_summary, api_errors = _api_summary(payload, _as_set(inventory.get("api_surfaces")))
    acct_summary, acct_errors = _acct_summary(payload, _as_set(inventory.get("acct_fields")))
    errors = inventory_errors + reference_errors + callback_errors + api_errors + acct_errors + _source_errors(payload)

    redis = payload.get("redis", {})
    if not isinstance(redis, dict) or not isinstance(redis.get("compatible"), bool):
        errors.append("REDIS_EVIDENCE_INVALID")
    elif redis["compatible"] is False:
        errors.append("REDIS_INCOMPATIBLE")
    scheduler = payload.get("scheduler", {})
    if (
        not isinstance(scheduler, dict)
        or set(scheduler) != SCHEDULER_KEYS
        or scheduler.get("mode") not in {"primary", "disabled"}
        or scheduler.get("profile") not in {"prod", "gray", "guarded-old"}
        or not isinstance(scheduler.get("image_digest"), str)
        or not IMAGE_DIGEST_RE.fullmatch(scheduler["image_digest"])
        or not isinstance(scheduler.get("config_sha256"), str)
        or not CHECKSUM_RE.fullmatch(scheduler["config_sha256"])
        or not isinstance(scheduler.get("callbacks_sha256"), str)
        or not CHECKSUM_RE.fullmatch(scheduler["callbacks_sha256"])
        or not isinstance(scheduler.get("source_payload_sha256"), str)
        or not CHECKSUM_RE.fullmatch(scheduler["source_payload_sha256"])
        or _parse_timestamp(scheduler.get("captured_at")) is None
        or not isinstance(scheduler.get("source"), str)
        or not re.fullmatch(r"(?:clone|direct):[A-Za-z0-9._:-]+", scheduler["source"])
        or not isinstance(scheduler.get("observations"), dict)
        or set(scheduler["observations"]) != SCHEDULER_OBSERVATION_KEYS
        or any(
            not isinstance(scheduler["observations"].get(key), int)
            or isinstance(scheduler["observations"].get(key), bool)
            or scheduler["observations"][key] < 0
            for key in SCHEDULER_OBSERVATION_KEYS
        )
        or (scheduler.get("profile") == "prod") != (scheduler.get("mode") == "primary")
    ):
        errors.append("SCHEDULER_EVIDENCE_INVALID")
    else:
        captured = _parse_timestamp(scheduler["captured_at"])
        source_payload = {
            key: value for key, value in scheduler.items() if key != "source_payload_sha256"
        }
        if scheduler["source_payload_sha256"] != _digest(source_payload):
            errors.append("SCHEDULER_EVIDENCE_INVALID")
        elif captured is None or captured - datetime.now(timezone.utc) > MAX_CLOCK_SKEW or datetime.now(timezone.utc) - captured > MAX_EVIDENCE_AGE:
            errors.append("SCHEDULER_EVIDENCE_STALE")
        elif any(scheduler["observations"].values()):
            errors.append("SCHEDULER_UNSAFE")

    mutation = payload.get("mutation_visibility", {})
    if (
        not isinstance(mutation, dict)
        or set(mutation) != {"stable_to_gray", "gray_to_stable", "freeze"}
        or not isinstance(mutation.get("freeze"), bool)
    ):
        errors.append("MUTATION_EVIDENCE_INVALID")
        mutation = {"stable_to_gray": {}, "gray_to_stable": {}, "freeze": False}
    mutation_fail = False
    for direction in ("stable_to_gray", "gray_to_stable"):
        item = mutation.get(direction)
        allowed_operations = _as_set(inventory.get("mutation_operations", {}).get(direction))
        if not isinstance(item, dict) or set(item) != MUTATION_KEYS:
            errors.append("MUTATION_EVIDENCE_INVALID")
            mutation_fail = True
            continue
        written = _parse_timestamp(item.get("written_at"))
        observed = _parse_timestamp(item.get("observed_at"))
        latency = item.get("latency_ms")
        sla = item.get("sla_ms")
        if (
            item.get("operation") not in allowed_operations
            or item.get("status") != "PASS"
            or not all(isinstance(item.get(field), str) and item[field] for field in ("writer", "reader", "request_id"))
            or not isinstance(item.get("result_sha256"), str) or not CHECKSUM_RE.fullmatch(item["result_sha256"])
            or written is None or observed is None or observed < written
            or not isinstance(latency, (int, float)) or isinstance(latency, bool) or latency < 0
            or not isinstance(sla, (int, float)) or isinstance(sla, bool) or sla <= 0
            or latency > sla
            or abs((observed - written).total_seconds() * 1000 - float(latency)) > 5000
        ):
            errors.append("MUTATION_VISIBILITY_ERROR")
            mutation_fail = True
    freeze = bool(mutation.get("freeze", False))
    if mutation_fail and freeze:
        decision = "freeze"
    elif mutation_fail:
        decision = "block"
        errors.append("MUTATION_VISIBILITY_ERROR")
    else:
        decision = "allow"

    # De-duplicate while preserving deterministic lexical order.
    reason_codes = sorted(set(errors))
    status = "PASS" if not reason_codes else "FAIL"
    result = {
        "tool": TOOL,
        "status": status,
        "reason_codes": reason_codes,
        "errors": reason_codes,
        "bypass_summary": {
            "total": len(references),
            "counts": counts,
            "references": references,
        },
        "callbacks": callback_summary,
        "api_surfaces": api_summary,
        "acct": acct_summary,
        "mutation_visibility": {"decision": decision, "freeze": freeze},
    }
    return result, 0 if status == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", "-i", default="-", help="JSON evidence file, or - for stdin")
    args = parser.parse_args(argv)
    payload, code = _load_json(args.input)
    if payload is None:
        return _emit({"tool": TOOL, "status": "ERROR", "reason_codes": ["INVALID_JSON"], "errors": ["INVALID_JSON"]}, code)
    result, code = _run(payload)
    return _emit(result, code)


if __name__ == "__main__":
    raise SystemExit(main())
