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
    "backend_health", "spend_reconciliation", "sustain_state",
    "latency_records", "latency_window_minutes",
}
MIN_SAMPLE = 100
# Sample floor for the 5xx stop-loss leg, separate from MIN_SAMPLE.  MIN_SAMPLE
# used to gate the whole class with a `continue`, which took the stop-loss leg
# down with the latency legs -- and the latency legs already have their own
# floors, so for them MIN_SAMPLE was redundant while for 5xx it was actively
# wrong.
#
# The floor is 200 because that is where a single window's rate stops being
# dominated by counting noise, NOT because it controls false positives -- it
# barely does.  Measured on the negative control (stable split in half against
# itself, 1223 five-minute buckets over 2026-09-14..18, threshold 0.01, real
# inference classes only): a single window fires on 10.94% of windows at floor
# 200 and 10.42% at floor 100 where the true delta is zero by construction.
# What actually suppresses that is SUSTAIN_WINDOWS: requiring the same breach in
# two consecutive windows takes it to 0.00% while still catching 54.7% of an
# injected +2pp.  Do not raise this floor hoping to buy precision; at 300 only 6
# windows in four days qualify at all and at 500 none do.
#
# Note for anyone re-running this: `other` (health/probe traffic) is ~90% of
# armed windows and almost never fires, so including it dilutes the false
# positive rate by more than 10x.  Calibrate on real inference classes only.
#
# The 5xx leg stays on the five-minute window, per its stop-loss role: a wider
# window averages a fault down against healthy minutes before anyone sees it.
# Below the floor the leg reports FIVE_XX_SAMPLE_BELOW_FLOOR and goes dark
# honestly rather than pretending to be armed.  At the current 15% split that is
# most of the time -- see docs section 6.1.1 for the coverage table and what it
# means for arming the dispatcher.
MIN_FIVE_XX_SAMPLE = 200
# A 101 response is a websocket upgrade: its `rt` is how long the connection
# stayed open (observed median 74s, max 64281s = 17.8h), not how long a request
# took to serve.  Mixing that into a latency percentile measures "how long did
# someone keep a socket open", so p95 landed inside the 101 band whenever they
# were 5% or more of a class.  They are still counted -- count, five_xx_rate and
# the sample floors all include them -- but they never enter p95/p99.
LATENCY_EXCLUDED_STATUSES = frozenset({101})
# Sample floors for the latency legs, separate from MIN_SAMPLE.  A p99 over 57
# samples is the single slowest request wearing a percentile's name; the
# `responses` class has a median of 57 per five minutes.  Measured against a
# negative control (stable split in half against itself, same version, same
# people, same instant -- every trigger is by construction false), the floors
# below are what took that false-positive rate from 15.1% to ~0%.
MIN_P95_SAMPLE = 200
MIN_P99_SAMPLE = 500
# Consecutive qualifying windows a class must stay bad before a threshold
# breach becomes a trigger.  Adjacent stable windows move p95 by 2.42x and p99
# by 3.01x at the median with nothing changed at all, so a single window over
# the line carries almost no information.  Requiring two in a row costs one
# window of detection latency and removes the remaining false positives.
SUSTAIN_WINDOWS = 2
# Shortest latency window that can carry the floors above.  At five minutes the
# `responses` class has a median of 57 latency samples, so MIN_P99_SAMPLE can
# never be met and the p99 leg would be permanently dark -- a gate that cannot
# fire is worse than a noisy one, because it looks armed.  The collector supplies
# `latency_records` over a wider window for the percentiles only; `records` stays
# at the five-minute live window so the 5xx stop-loss ruler is untouched.
MIN_LATENCY_WINDOW_MINUTES = 30
THRESHOLDS = {
    "five_xx_delta": 0.01,
    "p95_ratio": 1.3,
    "p99_ratio": 1.5,
}
# The statistical legs, and only those, are subject to the sustain gate.
THRESHOLD_TRIGGER_CODES = frozenset({"FIVE_XX_DELTA", "P95_RATIO", "P99_RATIO"})
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


# Keys the evidence checksum deliberately does not cover.  "evidence" is the
# envelope carrying the checksum itself.  "sustain_state" is control input the
# wrapper splices in: collect-metrics.py signs the payload before those counts
# exist (only gray-monitor-cycle.sh reads the state file beside the ledger), so
# covering it made every real cycle fail EVIDENCE_CHECKSUM_MISMATCH while the
# tests, which build the payload with the counts already present, stayed green.
# Leaving it out is safe in the direction that matters: sustain counts can only
# promote a breach to a trigger sooner, never suppress one, so a tampered value
# cannot hide a regression -- and metrics.py clamps and filters the contents
# anyway (see sustain_state()).
UNSIGNED_PAYLOAD_KEYS = {"evidence", "sustain_state"}


def payload_digest(payload: dict[str, Any]) -> str:
    return digest(
        {key: value for key, value in payload.items() if key not in UNSIGNED_PAYLOAD_KEYS}
    )


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
    """Aggregate records per (pool, uri_class).

    `latencies` deliberately holds fewer entries than `statuses`: websocket
    upgrades are counted but excluded from the percentiles (see
    LATENCY_EXCLUDED_STATUSES).  `latency_count` is emitted so a reader can see
    how many samples each percentile actually rests on, and so the latency
    sample floors gate on that number rather than on the class total.
    """
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
        if status not in LATENCY_EXCLUDED_STATUSES:
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
                "latency_count": len(latencies),
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


def latency_count(group: dict[str, Any]) -> int:
    """Samples backing this group's percentiles.

    Falls back to `count` for a group that predates `latency_count` -- a frozen
    baseline captured by an older collect-metrics.py has no such key, and
    treating it as zero would silently disable the latency legs against it.
    """
    raw = group.get("latency_count", group.get("count", 0))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


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
        # Each leg gates on its own floor.  This used to `continue` on MIN_SAMPLE
        # and take the whole class down -- including the 5xx stop-loss leg, which
        # is the one thing that must stay armed.  A class below every floor is
        # still reported, with the alerts saying which legs were dark, so a PASS
        # is never mistaken for "this class was checked".
        five_xx_qualified = (
            gray_count >= MIN_FIVE_XX_SAMPLE and ref_count >= MIN_FIVE_XX_SAMPLE
        )
        if gray_count < MIN_SAMPLE:
            alerts.add("INSUFFICIENT_GRAY_SAMPLE")
        if ref_count < MIN_SAMPLE:
            alerts.add("INSUFFICIENT_REFERENCE_SAMPLE")
        if not five_xx_qualified:
            alerts.add("FIVE_XX_SAMPLE_BELOW_FLOOR")
        five_delta = float(gray_group["five_xx_rate"]) - float(ref.get("five_xx_rate", 0))
        p95_ratio = ratio(gray_group.get("p95"), ref.get("p95"))
        p99_ratio = ratio(gray_group.get("p99"), ref.get("p99"))
        gray_latency = latency_count(gray_group)
        ref_latency = latency_count(ref)
        # Each latency leg gates on its own floor.  A ratio computed below the
        # floor is still reported -- it is the honest reading of a thin sample --
        # but it cannot breach, and `*_qualified` says which legs were live so a
        # PASS is never mistaken for "latency was checked".
        p95_qualified = gray_latency >= MIN_P95_SAMPLE and ref_latency >= MIN_P95_SAMPLE
        p99_qualified = gray_latency >= MIN_P99_SAMPLE and ref_latency >= MIN_P99_SAMPLE
        item = {
            "uri_class": uri_class,
            "reference": reference_name,
            # `qualified` means "at least one leg could judge this class".  It is
            # not a claim that every leg was live; `*_qualified` say which were.
            "qualified": five_xx_qualified or p95_qualified or p99_qualified,
            "gray_count": gray_count,
            "reference_count": ref_count,
            "gray_latency_count": gray_latency,
            "reference_latency_count": ref_latency,
            "five_xx_qualified": five_xx_qualified,
            "p95_qualified": p95_qualified,
            "p99_qualified": p99_qualified,
            "five_xx_delta": rounded(five_delta),
            "p95_ratio": rounded(p95_ratio),
            "p99_ratio": rounded(p99_ratio),
        }
        comparisons.append(item)
        breaches: set[str] = set()
        if five_xx_qualified and five_delta > THRESHOLDS["five_xx_delta"]:
            breaches.add("FIVE_XX_DELTA")
        if p95_qualified and p95_ratio is not None and p95_ratio > THRESHOLDS["p95_ratio"]:
            breaches.add("P95_RATIO")
        if p99_qualified and p99_ratio is not None and p99_ratio > THRESHOLDS["p99_ratio"]:
            breaches.add("P99_RATIO")
        if not p95_qualified and not p99_qualified:
            alerts.add("LATENCY_SAMPLE_BELOW_FLOOR")
        item["breaches"] = sorted(breaches)
        triggers |= breaches
    return comparisons, triggers, alerts


def with_wide_latency(
    live: dict[str, dict[str, Any]], wide: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Take counts and 5xx from the live window, percentiles from the wide one.

    The two legs answer different questions and need different window lengths.
    `five_xx_rate` is a stop-loss ruler: it has to stay on the five-minute window
    or a fault is averaged down by twenty-five minutes of health before anyone
    sees it.  p95/p99 are sampled statistics that need enough samples to mean
    anything, and five minutes of `responses` traffic does not have them.

    A class present live but absent from the wide window keeps its live
    percentiles rather than losing them: the wide window is a superset in
    practice, so this only fires on a malformed input, and the fail-safe
    direction is to keep measuring.
    """
    merged: dict[str, dict[str, Any]] = {}
    for uri_class, group in live.items():
        source = wide.get(uri_class)
        if source is None:
            merged[uri_class] = group
            continue
        merged[uri_class] = {
            **group,
            "p95": source.get("p95"),
            "p99": source.get("p99"),
            "latency_count": source.get("latency_count", 0),
        }
    return merged


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
    """Judge spend-write integrity: did every request we issued get accounted for?

    `pending_request_ids` exists because of a measurement, not a preference.  Over
    6 hours / 25851 production rows on 198 (2026-09-18), LiteLLM's spend write is
    bimodal: 98.4% of rows land within 30s of endTime, and the rest land in a
    backlog sweep 5-100 minutes later (max 6069s), arriving in batches that share
    an identical `created_at` and spanning unrelated model groups -- gpt-5.6-terra,
    kiro-claude-opus-5, sa-grok-4.6.  So "row not there yet" is flush cadence, not
    a fault, and a collector that scored it as `missing` would put a ~5%-per-cycle
    false rollback on a leg that deliberately bypasses the sustain gate.  The
    collector carries those ids forward instead and only reports one as missing
    once it is past the full observed flush tail; `missing` therefore still means
    "this spend write is lost", which is the fault worth a one-way door.

    For the same reason `observed_lag_seconds` is an ALERT, not a trigger.  It
    measures how long LiteLLM's batcher took, which is a property of the batcher
    and not of the gray version -- no threshold on it can separate the two, and a
    threshold sitting inside a 6069s tail is a ruler that reads the noise floor.
    """
    source = payload.get("spend_reconciliation")
    if source is None:
        return {"status": "NOT_PROVIDED"}, set(), [], set()
    if not isinstance(source, dict) or set(source) - {
        "expected_request_ids", "terminal_request_ids", "failed_request_ids",
        "pending_request_ids", "observed_lag_seconds",
    }:
        return {"status": "INVALID"}, set(), ["SPEND_RECONCILIATION_INVALID"], set()
    expected = opaque_ids(source.get("expected_request_ids"))
    terminal = opaque_ids(source.get("terminal_request_ids"))
    failed = opaque_ids(source.get("failed_request_ids"))
    # Absent is allowed: a collector that cannot distinguish pending from lost
    # must say so by omitting the key rather than by claiming an empty set.
    pending_raw = source.get("pending_request_ids")
    pending = set() if pending_raw is None else opaque_ids(pending_raw)
    lag = number(source.get("observed_lag_seconds"))
    if (
        expected is None or terminal is None or failed is None or pending is None
        or not expected or lag is None or lag < 0
    ):
        return {"status": "INVALID"}, set(), ["SPEND_RECONCILIATION_INVALID"], set()
    if terminal & failed or not (terminal | failed | pending).issubset(expected):
        return {"status": "INVALID"}, set(), ["SPEND_RECONCILIATION_INVALID"], set()
    # A pending id that is also already terminal or failed is a contradiction:
    # the collector observed the row and still called it unobserved.
    if pending & (terminal | failed):
        return {"status": "INVALID"}, set(), ["SPEND_RECONCILIATION_INVALID"], set()
    missing = expected - terminal - failed - pending
    triggers: set[str] = set()
    alerts: set[str] = set()
    if failed or missing:
        triggers.add("SPEND_RECONCILIATION_FAILED")
    if lag > MAX_SPEND_LAG_SECONDS:
        alerts.add("SPEND_RECONCILIATION_LAG")
    summary = {
        "status": "PASS" if not triggers else "FAIL",
        "expected_count": len(expected),
        "terminal_count": len(terminal),
        "failed_count": len(failed),
        "pending_count": len(pending),
        "missing_request_ids": sorted(missing),
        "failed_request_ids": sorted(failed),
        "pending_request_ids": sorted(pending),
        "observed_lag_seconds": rounded(lag),
        "request_ids_digest": digest(sorted(expected)),
    }
    return summary, triggers, [], alerts


def sustain_state(payload: dict[str, Any]) -> dict[str, int]:
    """Consecutive-breach counts carried in from the previous cycle.

    Absent or malformed state reads as empty, which makes the first cycle after
    a restart require SUSTAIN_WINDOWS fresh breaches.  That direction is
    deliberate: losing the ledger must not let one noisy window move traffic.
    """
    raw = payload.get("sustain_state")
    if not isinstance(raw, dict):
        return {}
    state: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in THRESHOLD_TRIGGER_CODES:
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            state[key] = min(count, SUSTAIN_WINDOWS)
    return state


def apply_sustain(
    breaches: set[str], previous: dict[str, int]
) -> tuple[set[str], dict[str, Any]]:
    """Promote a breach to a trigger only once it has held SUSTAIN_WINDOWS times.

    A code that does not breach this window resets to zero rather than decaying:
    a class that is bad, fine, bad, fine is noise, and letting those alternating
    windows accumulate would rebuild the very false positive this removes.
    """
    counts: dict[str, int] = {}
    promoted: set[str] = set()
    for code in sorted(breaches):
        counts[code] = min(previous.get(code, 0) + 1, SUSTAIN_WINDOWS)
        if counts[code] >= SUSTAIN_WINDOWS:
            promoted.add(code)
    summary = {
        "required_windows": SUSTAIN_WINDOWS,
        "counts": counts,
        "pending": sorted(code for code in counts if code not in promoted),
        "promoted": sorted(promoted),
    }
    return promoted, summary


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

    # Wide latency window.  Optional, but if either half is supplied both must
    # be, and the declared length must actually reach MIN_LATENCY_WINDOW_MINUTES:
    # a caller that passes thirty minutes of records while claiming five, or five
    # while claiming thirty, would put a number on the evidence that the samples
    # do not support.  Refuse rather than compare across mismatched windows.
    latency_records = payload.get("latency_records")
    latency_minutes = payload.get("latency_window_minutes")
    latency_groups: list[dict[str, Any]] = []
    latency_window_minutes: int | None = None
    if latency_records is not None or latency_minutes is not None:
        if not isinstance(latency_records, list) or not isinstance(latency_minutes, int) or isinstance(latency_minutes, bool):
            return error_result(["INVALID_LATENCY_WINDOW"], groups)
        if latency_minutes < MIN_LATENCY_WINDOW_MINUTES:
            return error_result(["LATENCY_WINDOW_TOO_SHORT"], groups)
        latency_groups, latency_invalid = summarize(latency_records)
        if latency_invalid:
            return error_result(["INVALID_LATENCY_RECORD"], groups)
        latency_window_minutes = latency_minutes

    errors: list[str] = []
    gray = by_class(groups, "canary")
    if latency_window_minutes is not None:
        gray = with_wide_latency(gray, by_class(latency_groups, "canary"))
    if rollout >= 100:
        mode = "baseline"
        reference = baseline_map(payload)
        comparisons, threshold_triggers, alerts = compare(gray, reference, "baseline")
    else:
        mode = "stable"
        reference = by_class(groups, "stable")
        if latency_window_minutes is not None:
            reference = with_wide_latency(reference, by_class(latency_groups, "stable"))
        comparisons, threshold_triggers, alerts = compare(gray, reference, "stable")
    if rollout > 0 and not gray:
        alerts.add("CANARY_SAMPLE_MISSING")

    hard = hard_error_probe
    spend_summary, spend_triggers, spend_errors, spend_alerts = spend_reconciliation(payload)
    if rollout > 0 and spend_summary.get("status") == "NOT_PROVIDED":
        spend_errors.append("SPEND_RECONCILIATION_MISSING")
    if spend_errors:
        return error_result(spend_errors, groups)
    alerts |= spend_alerts
    # Sustain gate.  Only the statistical legs are held back: a threshold breach
    # in a single window is mostly noise (adjacent stable windows move p95 2.42x
    # at the median with nothing changed), so it must repeat before it can move
    # traffic.  Hard errors and spend mismatches are NOT held back -- those are
    # observed faults, not sampled ratios, and one is already one too many.
    sustain = sustain_state(payload)
    sustained, sustain_summary = apply_sustain(threshold_triggers, sustain)
    triggers = hard | sustained | spend_triggers
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
    else:
        # An alert carries information; it must never suppress a safer action.
        # `hold_gray` is checked first for that reason: when prod is offline and
        # gray is healthy, holding gray is the correct recommendation whether or
        # not some class also happens to be below a sample floor.  Ordering these
        # the other way round let an informational alert silently downgrade the
        # recommendation to `alert_only`.
        health = payload.get("backend_health", {})
        if (
            phase in OFFLINE_PHASES
            and isinstance(health, dict)
            and health.get("gray") is True
            and health.get("prod") is False
        ):
            action = "hold_gray"
            reason_codes = sorted({"PROD_OFFLINE_GRAY_HEALTHY"} | alerts)
        elif alerts:
            action = "alert_only"
            reason_codes = sorted(alerts)
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
        "thresholds": {
            **THRESHOLDS,
            "minimum_sample": MIN_SAMPLE,
            "minimum_p95_sample": MIN_P95_SAMPLE,
            "minimum_p99_sample": MIN_P99_SAMPLE,
            "sustain_windows": SUSTAIN_WINDOWS,
            "latency_excluded_statuses": sorted(LATENCY_EXCLUDED_STATUSES),
            "minimum_latency_window_minutes": MIN_LATENCY_WINDOW_MINUTES,
            # None means the percentiles came from the five-minute live window and
            # will mostly sit below the floors; a reader must not have to infer
            # that from the absence of a key.
            "latency_window_minutes": latency_window_minutes,
        },
        # Carried into the next cycle's input so a breach can be seen to repeat.
        "sustain": sustain_summary,
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
