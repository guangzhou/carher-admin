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
    "readiness", "data_sources",
}
# Sample floor for the 5xx delta leg.  There used to be a MIN_SAMPLE = 100 here
# that gated the whole class, and it is gone: every leg below carries its own
# floor, so a single shared number could only be redundant (for the latency legs,
# which have 200/500) or actively harmful (for the 5xx stop-loss, which it dragged
# dark with them).  It also produced two alerts -- INSUFFICIENT_GRAY_SAMPLE and
# INSUFFICIENT_REFERENCE_SAMPLE -- that fired in essentially every real window and
# told a reader nothing the per-leg `*_qualified` flags do not say precisely.
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
# The absolute 5xx stop-loss leg.  This exists because the delta leg above cannot
# arm at production volume: measured on 65 five-minute windows of canary
# inference traffic at split=100 (2026-09-19, 12h), the per-class counts are
# chat median 65 / max 149, responses 44 / 100, messages 12 / 36, and even all
# inference classes pooled peak at 185.  Zero windows in twelve hours reached
# MIN_FIVE_XX_SAMPLE on the gray side, so `five_xx_qualified` was false in every
# window at every split.  The reference side was never the problem -- the frozen
# baseline carries 5156 / 2208 / 200.
#
# Lowering MIN_FIVE_XX_SAMPLE does not fix it.  A 100-request window holds ~2
# requests' worth of a 2pp shift, so nothing can resolve that effect there:
# measured against a leave-one-out negative control on the same version, floor 50
# arms 51.6% of windows but fires falsely on 3.8% of them (a false rollback every
# ~1.5h) while still catching only 11% of an injected +2pp.  A one-sided Fisher
# exact on the raw counts was also tried: false positives 0.00%, detection 0.4%.
# The information is not in the window.
#
# What IS resolvable at n=100 is a broken build, which does not produce 2% errors.
# So this leg drops the reference comparison and trips on an absolute rate that
# same-version history does not sustain.  Floor 30 because it arms 92.6% of
# windows (stable) / 79.8% (canary) versus 3.16% for the delta leg, and a rate
# needs a handful of requests to exist at all.
MIN_ABS_FIVE_XX_SAMPLE = 30
# 0.10 sits above the p90 of same-version five-minute windows (0.043 stable,
# 0.035 canary) and below the p99 (0.206 / 0.288).  It is deliberately NOT above
# the historical max: same-version windows reach 0.62 during upstream outages, so
# any rate a broken build crosses, a burst crosses too.  DURATION separates them,
# not the rate -- see ABS_SUSTAIN_WINDOWS.
ABS_FIVE_XX_RATE = 0.10
# Three events, so a 30-request window cannot trip on a single failure.
MIN_ABS_FIVE_XX_EVENTS = 3
# Concurrent non-gray inference traffic must clear this before its rate is worth
# reporting.  It NO LONGER VETOES anything -- see absolute_five_xx() for the
# measurement that demoted it (blind in >=72% of gray-armed windows, 0% readable
# in the last two hours before cutover completed).  The floor is kept because a
# rate computed over 4 requests is not a reading either.
SHARED_FATE_MIN_SAMPLE = 30
# Classes that reach an upstream model provider.  `other` is health and probe
# traffic: it never leaves the proxy, it is ~90% of stable's requests, and
# pooling it into the shared-fate cohort dilutes a real burst from 89.5%
# suppression down to 64.4%.  Measured; that is why this set is explicit.
INFERENCE_CLASSES = frozenset({"chat", "messages", "responses", "embedding"})
# A 101 response is a websocket upgrade: its `rt` is how long the connection
# stayed open (observed median 74s, max 64281s = 17.8h), not how long a request
# took to serve.  Mixing that into a latency percentile measures "how long did
# someone keep a socket open", so p95 landed inside the 101 band whenever they
# were 5% or more of a class.  They are still counted -- count, five_xx_rate and
# the sample floors all include them -- but they never enter p95/p99.
LATENCY_EXCLUDED_STATUSES = frozenset({101})
# Sample floors for the latency readings.  A p99 over 57 samples is the single
# slowest request wearing a percentile's name; the `responses` class has a median
# of 57 per five minutes.  Measured against a negative control (stable split in
# half against itself, same version, same people, same instant -- every trigger is
# by construction false), these floors are what took that false-positive rate from
# 15.1% to ~0%.
#
# They now gate a *reading*, not a trigger: see LATENCY_OBSERVED_ONLY.
MIN_P95_SAMPLE = 200
MIN_P99_SAMPLE = 500
# p95/p99 ratios are reported and never trip the dispatcher.
#
# Not a preference -- measured on 148 real monitor cycles of the 2026-09-14 run at
# split=100 with five_xx_count identically 0 throughout.  The median ratio was
# BELOW 1.0 on every class (chat 0.527, responses 0.564, messages 0.664: canary was
# faster than the reference), and the legs still breached 6 and 11 times.  One
# 30-minute stretch of `responses` read
# 1.369 1.799 1.746 1.034 0.727 0.725 0.725 0.751 1.127 1.428 1.648 1.656
# -- across the 1.3 line four times, swinging 2.5x, with zero 5xx.
#
# The cause is the denominator, and it cannot be fixed by tuning: the frozen
# baseline holds 2043-5156 samples per class while a live canary window holds
# 189-317, so the ratio divides two distributions of ~8x different depth.  A
# percentile ratio across unequal depths measures sample variance, not
# regression -- the same reason a raw canary-vs-stable p50 comparison is a broken
# ruler.  `messages` was worse than noisy: p95_qualified was False in all 80 of
# its windows, so that leg was dark while reporting itself armed.
#
# Making them usable would require equal-depth cohorts (same sid set, or a window
# wide enough on both sides), which is a different piece of work.  Until then the
# honest form is: keep the numbers -- they are a real measurement of something --
# and refuse to move production traffic on them.
LATENCY_OBSERVED_ONLY = True
# Consecutive qualifying windows a class must stay bad before a threshold
# breach becomes a trigger.  Adjacent stable windows move p95 by 2.42x and p99
# by 3.01x at the median with nothing changed at all, so a single window over
# the line carries almost no information.  Requiring two in a row costs one
# window of detection latency and removes the remaining false positives.
SUSTAIN_WINDOWS = 2
# The absolute 5xx leg needs a deeper gate than the comparison legs, because it
# has no reference to cancel provider weather out and its remaining false
# positives are all upstream bursts.  Depth is the lever that works: measured on
# same-version canary history (1310 windows / 4.55 days), at rate 0.10 the false
# fires per day go 2.07 (depth 2) -> 0.89 (3) -> 0.30 (4), while detection of a
# +20pp regression only falls 94.2% -> 90.9% -> 88.6%.  Four windows is 20
# minutes of a sustained break before traffic moves; a burst that clears inside
# that is exactly what we do not want to act on.
ABS_SUSTAIN_WINDOWS = 4
# Shortest latency window that can carry the floors above.  At five minutes the
# `responses` class has a median of 57 latency samples, so MIN_P99_SAMPLE can
# never be met and the p99 leg would be permanently dark -- a gate that cannot
# fire is worse than a noisy one, because it looks armed.  The collector supplies
# `latency_records` over a wider window for the percentiles only; `records` stays
# at the five-minute live window so the 5xx stop-loss ruler is untouched.
MIN_LATENCY_WINDOW_MINUTES = 30
# --- Leg 2: ready container count -------------------------------------------
#
# Counted in READY CONTAINERS, never in replicas.  `replicas` is a spec field:
# a Deployment scaled to 0 still reports `Available: True`, and on the acct pool
# 165 deployments with replicas>0 had only 54 actually serving.  It is the same
# broken ruler in both places, and it fails in the direction that hides an
# outage.
#
# The floor is not a constant here because it cannot be: the correct number is
# whatever the run sheet froze for this lane, and hard-coding one would repeat
# the `llm-stab-scrape-down` mistake of a literal 5 that silently stops matching
# after a scale change.  The collector supplies both the observed count and the
# expected one; this module only checks the arithmetic and refuses to guess when
# either is absent.
READINESS_KEYS = {"lane", "ready_containers", "expected_containers", "observed_at"}
# --- Leg 3: data liveness ----------------------------------------------------
#
# Any source silent longer than this reads red.  300s == the frozen scheduler
# interval, so one missed cycle is already over the line: this leg exists because
# a monitor whose inputs stopped is indistinguishable, in every downstream
# payload, from a monitor watching a healthy system.  Every leg above reports
# "clean" on an empty window.
#
# Coupled to the scheduler interval by arithmetic, exactly like
# check-monitor-continuity.py's --cycle-interval-seconds.  A run at a different
# cadence must pass its own value; the default matches the frozen 300s and
# test_data_liveness_floor_matches_the_frozen_cycle_interval pins it.
MAX_SOURCE_SILENCE_SECONDS = 300
DATA_SOURCE_KEYS = {"name", "observed_at"}
# --- Leg 4: user-facing failure count ---------------------------------------
#
# Failures of REAL keys, counted per request.  Not ERROR log lines: those count a
# retry layer's attempts, not a user's outcome, and the same user-visible failure
# appears 1-N times depending on how many upstream tries it took.  The ruler is
# the access log's own status against an enrolled sid, which is one row per thing
# a user actually saw.
#
# One is one too many only above this floor for the same reason the absolute 5xx
# leg needs 3 events: a single failure in a thin window is a coin flip, and this
# leg bypasses nothing -- it goes straight to the dispatcher.
MIN_USER_FAILURE_EVENTS = 3
# Rate rather than a bare count, because a bare count scales with traffic: 5
# failures in 60 requests and 5 in 6000 are different facts.
#
# 🔴 What this rate is taken OVER was wrong until 2026-09-20, and the negative
# control caught it: replaying real stable traffic against itself (identical
# cohorts, so every red is false by construction) promoted USER_FAILURE_RATE to
# `rollback` on 3 of 6 consecutive windows.  The threshold was not the defect --
# the population was.  Counting every status >= 400, 48 of 185 healthy windows
# (25.9%) sit above 0.02, because most 4xx on this proxy is steady-state client
# behaviour, present at this rate on traffic with no version fault at all:
#
#   66393 stable inference rows / 15.4h / 185 windows of >= 30 samples
#   client-side 4xx  499x508 400x326 405x296 403x107 401x70 402x1
#     -> p50 0.0043  p90 0.0330  max 0.1222   above 0.02 in 39/185 (21.1%)
#   proxy-emitted    503x371 500x124 429x92 413x4
#     -> p50 0.0000  p90 0.0065  max 0.2283   above 0.02 in  7/185 ( 3.8%)
#
# 499 is nginx's own code for the client hanging up before a reply; 405 (296, all
# on `responses`) is a client calling a method the route does not serve; 400 is a
# malformed body.  None of those is something the build under test did, and none
# of them is what 「用户面失败」 means.  So the trigger counts only what the proxy
# itself emitted, and the client codes stay in the payload as a reading with their
# own alert line -- visible, never moving traffic.
#
# The line stays at 0.02 rather than moving up: on the correct population it is
# already above p90 by 3x, and raising it would blunt the one leg that sees a
# 429/401 storm.
MAX_USER_FAILURE_RATE = 0.02
# 4xx the proxy emits about its OWN state, folded in with 5xx: 429 is this proxy
# refusing the request (rate limit or budget) and 413 is it rejecting the body.
# A build that starts 429-ing everyone is invisible to a 5xx ruler and entirely
# visible to the person hitting it -- that case is why this leg exists next to the
# absolute 5xx stop-loss.  Every other 4xx describes the CALLER, not the build.
PROXY_EMITTED_CLIENT_STATUSES = frozenset({413, 429})
# Client-side 4xx gets an alert, never a trigger.  Measured above: 0.03 would page
# 35.9 times a day on healthy traffic, 0.05 15.6 times.  0.10 fires on 1 of 185
# windows (1.56/day) and is still well under the 0.1222 ceiling this population
# actually reached, so it flags a genuine change in caller behaviour -- a client
# fleet suddenly sending bad bodies is worth knowing about, just not worth
# rolling back a proxy for.
MAX_USER_CLIENT_ERROR_RATE = 0.10
# Only the 5xx legs have thresholds now: p95_ratio/p99_ratio used to live here at
# 1.3/1.5 and they are gone with the triggers they fed (LATENCY_OBSERVED_ONLY).
# Leaving a threshold behind for a leg that cannot fire is how a dark leg keeps
# looking armed -- the ratios are still in the payload as readings, with no line
# for anyone to read them against.
THRESHOLDS = {
    "five_xx_delta": 0.01,
}
# The statistical legs, and only those, are subject to the sustain gate.
#
# USER_FAILURE_RATE is in here and READY_CONTAINERS_SHORT / DATA_SOURCE_SILENT are
# not, and that split is the point.  A failure rate is a sampled ratio and one bad
# window of it is mostly noise.  A container that is not ready and a data source
# that has stopped are OBSERVED FACTS read from an exact counter -- holding them
# for repeats would mean watching a known outage for 20 minutes before acting, and
# for the liveness leg it is worse than that: the thing it detects is the monitor
# having stopped, so the next window that would confirm it may never arrive.
THRESHOLD_TRIGGER_CODES = frozenset(
    {"FIVE_XX_DELTA", "FIVE_XX_ABSOLUTE", "USER_FAILURE_RATE"}
)
# Per-code sustain depth.  Anything absent uses SUSTAIN_WINDOWS.
#
# USER_FAILURE_RATE needs 3, not 2, and that is measured on the corrected
# population rather than argued: over the same 185 healthy windows, the runs of
# consecutive proxy-emitted breaches above 0.02 are [1, 1, 1, 2, 2] -- max 2.
# Depth 2 therefore still fires twice in 15.4h (3.13 false rollbacks/day); depth 3
# fires zero times.  The cost is 5 extra minutes before a real regression moves
# traffic, and the absolute 5xx stop-loss sits underneath at depth 4 for anything
# severe enough to be worth less delay than that.
#
# ⛔ This is a per-code depth for the same reason FIVE_XX_ABSOLUTE's is: raising
# SUSTAIN_WINDOWS globally to 3 would blunt FIVE_XX_DELTA, whose own calibration
# was done at 2.
USER_FAILURE_SUSTAIN_WINDOWS = 3
SUSTAIN_DEPTH = {
    "FIVE_XX_ABSOLUTE": ABS_SUSTAIN_WINDOWS,
    "USER_FAILURE_RATE": USER_FAILURE_SUSTAIN_WINDOWS,
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
        # 4xx is for the user-facing leg only and is deliberately NOT folded into
        # five_xx_count or five_xx_rate: the stop-loss legs and the frozen baseline
        # are both calibrated on 5xx, and widening that number here would silently
        # re-base every threshold measured against it.
        four_xx = sum(1 for status in statuses if 400 <= status < 500)
        # ...and split in two, because only one half describes the build.  The
        # proxy emits 429/413 about its own state; every other 4xx describes the
        # caller (499 hung up, 405 wrong method, 400 bad body).  Folding them into
        # one number is what made the user-facing leg fire on 25.9% of healthy
        # windows -- see MAX_USER_FAILURE_RATE.  `four_xx_count` stays as the total
        # so an existing reader is not silently re-based.
        proxy_four_xx = sum(
            1 for status in statuses if status in PROXY_EMITTED_CLIENT_STATUSES
        )
        result.append(
            {
                "pool_label": pool,
                "uri_class": uri_class,
                "count": len(statuses),
                "latency_count": len(latencies),
                "five_xx_count": five_xx,
                "four_xx_count": four_xx,
                "proxy_four_xx_count": proxy_four_xx,
                "client_four_xx_count": four_xx - proxy_four_xx,
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
        # Each leg gates on its own floor.  This used to `continue` on a shared
        # MIN_SAMPLE and take the whole class down -- including the 5xx stop-loss
        # leg, which is the one thing that must stay armed.  A class below every
        # floor is still reported, with the alerts saying which legs were dark, so
        # a PASS is never mistaken for "this class was checked".
        five_xx_qualified = (
            gray_count >= MIN_FIVE_XX_SAMPLE and ref_count >= MIN_FIVE_XX_SAMPLE
        )
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
            # `qualified` means "the one leg that can judge this class could judge
            # it".  The latency readings below do not enter it: they never trigger,
            # so counting them here would let a class with 900 latency samples and
            # 40 requests report itself as checked when nothing was checked.
            "qualified": five_xx_qualified,
            "gray_count": gray_count,
            "reference_count": ref_count,
            "gray_latency_count": gray_latency,
            "reference_latency_count": ref_latency,
            "five_xx_qualified": five_xx_qualified,
            # Kept as floor flags for the reading, not as gate state.  Below the
            # floor the ratio is a percentile over too few samples and a reader
            # should discount it; above the floor it is still only a reading.
            "p95_above_floor": p95_qualified,
            "p99_above_floor": p99_qualified,
            # Says out loud what the flags above no longer imply.  Without this a
            # reader who remembers the old payload would take `p95_above_floor:
            # true` to mean the leg is armed.
            "latency_observed_only": LATENCY_OBSERVED_ONLY,
            "five_xx_delta": rounded(five_delta),
            "p95_ratio": rounded(p95_ratio),
            "p99_ratio": rounded(p99_ratio),
        }
        comparisons.append(item)
        breaches: set[str] = set()
        if five_xx_qualified and five_delta > THRESHOLDS["five_xx_delta"]:
            breaches.add("FIVE_XX_DELTA")
        item["breaches"] = sorted(breaches)
        triggers |= breaches
    return comparisons, triggers, alerts


def pool_inference_totals(groups: list[dict[str, Any]], pool: str) -> tuple[int, int]:
    """Requests and 5xx across one pool's inference classes, pooled.

    Pooled rather than per-class because that is the only grouping with a usable
    denominator: per-class, the busiest five-minute window in twelve hours held
    149 requests, while pooled it holds ~100 at the median.  `other` is excluded
    because it never reaches a model provider, so its errors say nothing about the
    build under test and its volume (~90% of stable) would bury a real one.
    """
    count = five_xx = 0
    for item in groups:
        if item["pool_label"] != pool or item["uri_class"] not in INFERENCE_CLASSES:
            continue
        count += int(item.get("count", 0))
        five_xx += int(item.get("five_xx_count", 0))
    return count, five_xx


def absolute_five_xx(
    groups: list[dict[str, Any]], gray_pool: str, guard_pool: str
) -> tuple[dict[str, Any], set[str], set[str]]:
    """Stop-loss on the gray cohort's own 5xx rate.

    This is the leg that is actually armed at production volume; the delta leg
    beside it cannot be, and says so with FIVE_XX_SAMPLE_BELOW_FLOOR.  It answers
    a narrower question on purpose -- "is the gray cohort failing outright" rather
    than "is it slightly worse than the reference" -- because that is the question
    a five-minute window of ~100 requests can answer.

    The shared-fate cohort is REPORTED but no longer vetoes.  It was introduced to
    tell a provider outage apart from a bad build, and on paper it does; measured,
    it cannot see.  At split=100 the stable pool retains only health checks (which
    are excluded from the cohort by design) plus a handful of force-prod keys, so
    stable inference clears SHARED_FATE_MIN_SAMPLE in 27.8% of gray-armed windows
    over six hours and 0% over the last two, with a per-window median of 0
    requests.  Widening the guard window does not rescue it: 5/15/30/60 minutes
    give 27.8% / 30.6% / 34.7% / 43.1%.

    A veto blind in >=72% of windows is worse than no veto, because it is not
    inert -- it is a branch that changes the verdict on the minority of windows
    where it happens to be readable, which makes the leg's behaviour depend on
    whether a few force-prod keys were busy.  Depth is what actually separates a
    burst from a regression and it is measured: over 1318 windows / 4.58 days the
    consecutive-breach runs are p50 1, p90 3, max 4, and exactly one run in the
    whole span reached ABS_SUSTAIN_WINDOWS -- so with the veto assumed blind
    throughout, false promotions are 0.22/day.  The veto was the bonus; the depth
    was always the mechanism.

    The cohort's numbers stay in the summary, and UPSTREAM_FIVE_XX_SHARED is still
    raised as an ALERT when both cohorts are equally bad: that is real information
    for whoever is reading, and the users are getting errors either way.  It just
    no longer suppresses the stop-loss.
    """
    gray_count, gray_five_xx = pool_inference_totals(groups, gray_pool)
    guard_count, guard_five_xx = pool_inference_totals(groups, guard_pool)
    gray_rate = (gray_five_xx / gray_count) if gray_count else None
    guard_rate = (guard_five_xx / guard_count) if guard_count else None
    guard_readable = guard_count >= SHARED_FATE_MIN_SAMPLE
    summary: dict[str, Any] = {
        "gray_pool": gray_pool,
        "gray_count": gray_count,
        "gray_five_xx_count": gray_five_xx,
        "gray_five_xx_rate": rounded(gray_rate),
        "threshold": ABS_FIVE_XX_RATE,
        "minimum_sample": MIN_ABS_FIVE_XX_SAMPLE,
        "minimum_events": MIN_ABS_FIVE_XX_EVENTS,
        "shared_fate_pool": guard_pool,
        "shared_fate_count": guard_count,
        "shared_fate_five_xx_rate": rounded(guard_rate),
        "shared_fate_readable": guard_readable,
        # Renamed from `shared_fate_vetoed`, which a reader of the old payload would
        # take to mean "a veto exists and did not fire this window".  It no longer
        # exists at all; this field says only whether both cohorts were bad.
        "shared_fate_observed_only": True,
        "shared_fate_concurrent_breach": False,
    }
    breaches: set[str] = set()
    alerts: set[str] = set()
    if gray_count < MIN_ABS_FIVE_XX_SAMPLE:
        summary["qualified"] = False
        # No gray inference traffic at all is a different fact from a thin window,
        # and CANARY_SAMPLE_MISSING already reports it.  Adding a floor alert here
        # would put two codes on one cause.
        if gray_count:
            alerts.add("ABS_FIVE_XX_SAMPLE_BELOW_FLOOR")
        return summary, breaches, alerts
    summary["qualified"] = True
    if gray_five_xx >= MIN_ABS_FIVE_XX_EVENTS and gray_rate > ABS_FIVE_XX_RATE:
        # The breach stands regardless of what the guard cohort looks like.  What
        # the cohort adds is an attribution HINT for the human reading the alert,
        # and it is reported as one.
        breaches.add("FIVE_XX_ABSOLUTE")
        if not guard_readable:
            alerts.add("SHARED_FATE_COHORT_BLIND")
        elif guard_rate > ABS_FIVE_XX_RATE:
            # Both cohorts are equally bad and only one runs the gray build, so
            # this most likely is provider weather rather than a regression.  It
            # does not suppress the rollback: the users are getting errors either
            # way, and "probably upstream" is a judgement for a human with more
            # context than this window has.
            summary["shared_fate_concurrent_breach"] = True
            alerts.add("UPSTREAM_FIVE_XX_SHARED")
    return summary, breaches, alerts


def readiness(payload: dict[str, Any]) -> tuple[dict[str, Any], set[str], list[str]]:
    """Leg 2: are the containers that should be serving actually ready?

    Ready containers, never `replicas`.  `replicas` is a spec number and it lies
    in the dangerous direction: a Deployment scaled to zero still reports
    `Available: True`, so a lane with nothing running reads healthy.

    The expected count comes from the collector rather than from a constant here.
    A literal in this file would be the `llm-stab-scrape-down` failure again --
    hard-coded 5, silently wrong after any scale change, and wrong in a way that
    never goes red.  Absence of the section is NOT_PROVIDED, not zero: a missing
    reading and a lane with no ready containers are opposite facts, and only one
    of them is an outage.
    """
    source = payload.get("readiness")
    if source is None:
        return {"status": "NOT_PROVIDED"}, set(), []
    if not isinstance(source, dict) or set(source) - READINESS_KEYS:
        return {"status": "INVALID"}, set(), ["READINESS_INVALID"]
    ready = number(source.get("ready_containers"))
    expected = number(source.get("expected_containers"))
    lane = source.get("lane")
    if (
        ready is None or expected is None
        or ready < 0 or expected < 1
        or not float(ready).is_integer() or not float(expected).is_integer()
        or not isinstance(lane, str) or not lane
    ):
        return {"status": "INVALID"}, set(), ["READINESS_INVALID"]
    ready, expected = int(ready), int(expected)
    summary: dict[str, Any] = {
        "status": "PASS",
        "lane": lane,
        "ready_containers": ready,
        "expected_containers": expected,
        # Said out loud so nobody has to trust that this leg read the right field.
        "ruler": "ready_containers",
    }
    triggers: set[str] = set()
    if ready < expected:
        triggers.add("READY_CONTAINERS_SHORT")
        summary["status"] = "FAIL"
    return summary, triggers, []


def data_liveness(payload: dict[str, Any]) -> tuple[dict[str, Any], set[str], list[str]]:
    """Leg 3: has any input to this cycle gone silent?

    Every other leg in this module reports "clean" on an empty window, which means
    a monitor whose inputs died produces a payload indistinguishable from a
    monitor watching a healthy system.  That shape is why the 2026-09-14 run was
    green throughout while three of its five ramp steps had an unarmed stop-loss.

    Ages are measured from the NEWEST observation in the cycle, not from
    `evidence.captured_at`.  captured_at is the MINIMUM of the source timestamps
    by construction in the collector, so anchoring on it gives the oldest source
    an age of 0 -- the one that has actually gone quiet reads as the freshest, and
    every other source reads as being in the future.  The first smoke run of this
    leg did exactly that.

    Anchoring on the newest observation makes this leg RELATIVE on purpose: it
    answers "is one input lagging while the others are alive", which is the
    failure nothing else here can see.  Whole-payload staleness -- everything
    stopped together -- is already `evidence_errors`'s job via MAX_EVIDENCE_AGE
    against wall clock, and the collector's own MAX_LOG_AGE refuses a stale access
    log before this module runs.  Splitting it this way is also what lets a
    re-run over an archived cycle judge that cycle instead of calling every
    historical file dead.

    There is deliberately no clock-skew code here.  With a max anchor no age can
    be negative, so such a branch could never fire, and a leg that cannot fire
    while looking armed is the exact shape this whole trim exists to delete.
    """
    source = payload.get("data_sources")
    if source is None:
        return {"status": "NOT_PROVIDED"}, set(), []
    if not isinstance(source, list) or not source:
        return {"status": "INVALID"}, set(), ["DATA_SOURCES_INVALID"]
    observations: list[tuple[str, datetime]] = []
    seen: set[str] = set()
    for item in source:
        if not isinstance(item, dict) or set(item) - DATA_SOURCE_KEYS:
            return {"status": "INVALID"}, set(), ["DATA_SOURCES_INVALID"]
        name = item.get("name")
        observed = parse_timestamp(item.get("observed_at"))
        if not isinstance(name, str) or not name or observed is None or name in seen:
            return {"status": "INVALID"}, set(), ["DATA_SOURCES_INVALID"]
        seen.add(name)
        observations.append((name, observed))
    anchor = max(observed for _, observed in observations)
    triggers: set[str] = set()
    entries: list[dict[str, Any]] = []
    for name, observed in observations:
        age = (anchor - observed).total_seconds()
        entry = {
            "name": name,
            "age_seconds": rounded(age),
            "silent": age > MAX_SOURCE_SILENCE_SECONDS,
        }
        if entry["silent"]:
            triggers.add("DATA_SOURCE_SILENT")
        entries.append(entry)
    summary = {
        "status": "FAIL" if triggers else "PASS",
        "floor_seconds": MAX_SOURCE_SILENCE_SECONDS,
        # Named so a reader knows the ages are relative to this cycle's freshest
        # input rather than to wall clock.
        "anchor": "newest_observation",
        "anchor_at": anchor.isoformat().replace("+00:00", "Z"),
        "sources": sorted(entries, key=lambda item: item["name"]),
        "silent_sources": sorted(
            entry["name"] for entry in entries if entry["silent"]
        ),
    }
    return summary, triggers, []


def user_facing_failures(
    groups: list[dict[str, Any]], pool: str
) -> tuple[dict[str, Any], set[str], set[str]]:
    """Leg 4: how many requests did real users see THIS PROXY fail?

    Counted from the access log's own status codes on the gray pool's inference
    classes -- one row per thing a user actually saw.  NOT from ERROR log lines:
    those count a retry layer's upstream attempts, so one user-visible failure
    appears 1-N times depending on how many tries it took, and the count moves
    when retry policy changes with nothing wrong.

    🔴 The trigger counts 5xx plus PROXY_EMITTED_CLIENT_STATUSES (429/413) and
    NOT the rest of the 4xx.  That is not a softening of the leg, it is the fix
    for a false positive the negative control caught: on identical cohorts this
    leg promoted to `rollback` on 3 of 6 windows, because 499 (client hung up),
    405 (wrong method) and 400 (bad body) are steady-state caller behaviour on
    this proxy -- 21.1% of healthy windows are above 0.02 on those codes alone,
    against 3.8% on the codes the proxy itself emits.  See MAX_USER_FAILURE_RATE
    for the full distribution.

    The client codes are still counted, reported, and given their own alert at
    MAX_USER_CLIENT_ERROR_RATE.  A client fleet that starts sending bad bodies is
    worth knowing about; it is not a reason to roll a proxy back, and treating it
    as one meant the whole leg could not be trusted.

    Distinct from the absolute 5xx leg despite reading the same rows.  That leg is
    a stop-loss with a deliberately high bar (rate 0.10, four consecutive windows)
    tuned so provider weather does not move traffic.  This one is the users'-eye
    view at a lower bar (0.02, three windows), and it sees the one failure mode a
    pure-5xx ruler cannot: a build that starts 429-ing or 413-ing everybody is a
    total outage to the person hitting it and zero 5xx to the monitor.

    `other` stays excluded, same as everywhere else: health and probe traffic is
    ~90% of the rows and none of it is a user.
    """
    count = server_errors = proxy_four_xx = client_four_xx = 0
    for item in groups:
        if item["pool_label"] != pool or item["uri_class"] not in INFERENCE_CLASSES:
            continue
        count += int(item.get("count", 0))
        server_errors += int(item.get("five_xx_count", 0))
        proxy_four_xx += int(item.get("proxy_four_xx_count", 0))
        client_four_xx += int(item.get("client_four_xx_count", 0))
    failures = server_errors + proxy_four_xx
    rate = (failures / count) if count else None
    client_rate = (client_four_xx / count) if count else None
    summary: dict[str, Any] = {
        "pool": pool,
        "request_count": count,
        # `failure_count` / `failure_rate` are what the trigger reads, and their
        # meaning CHANGED on 2026-09-20: they no longer include caller-side 4xx.
        # A reader comparing them against an archived payload is comparing two
        # different populations -- the old number is `failure_count` +
        # `client_four_xx_count`.
        "failure_count": failures,
        "failure_rate": rounded(rate),
        "five_xx_count": server_errors,
        "proxy_four_xx_count": proxy_four_xx,
        "client_four_xx_count": client_four_xx,
        "client_four_xx_rate": rounded(client_rate),
        "threshold": MAX_USER_FAILURE_RATE,
        "client_error_threshold": MAX_USER_CLIENT_ERROR_RATE,
        "minimum_events": MIN_USER_FAILURE_EVENTS,
        "proxy_emitted_client_statuses": sorted(PROXY_EMITTED_CLIENT_STATUSES),
        # Both counts come from summarize() over the same rows, so there is no
        # "collector too old to report 4xx" case to guard against -- the groups this
        # leg reads are built in this module.  A flag for it would be a check that
        # can never go red, which is the shape this whole trim exists to remove.
        "ruler": "access_log_status",
    }
    triggers: set[str] = set()
    alerts: set[str] = set()
    if count < MIN_ABS_FIVE_XX_SAMPLE:
        summary["qualified"] = False
        if count:
            alerts.add("USER_FAILURE_SAMPLE_BELOW_FLOOR")
        return summary, triggers, alerts
    summary["qualified"] = True
    if failures >= MIN_USER_FAILURE_EVENTS and rate > MAX_USER_FAILURE_RATE:
        triggers.add("USER_FAILURE_RATE")
    # Alert only, and gated on the same event floor so a thin window cannot raise
    # it on one hung-up client.  It never enters `triggers`, so it never reaches
    # the sustain gate or the dispatcher.
    if (
        client_four_xx >= MIN_USER_FAILURE_EVENTS
        and client_rate > MAX_USER_CLIENT_ERROR_RATE
    ):
        alerts.add("CLIENT_ERROR_RATE_HIGH")
    return summary, triggers, alerts


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
            state[key] = min(count, depth_for(key))
    return state


def depth_for(code: str) -> int:
    """Consecutive windows this code must hold before it can move traffic."""
    return SUSTAIN_DEPTH.get(code, SUSTAIN_WINDOWS)


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
        depth = depth_for(code)
        counts[code] = min(previous.get(code, 0) + 1, depth)
        if counts[code] >= depth:
            promoted.add(code)
    summary = {
        "required_windows": SUSTAIN_WINDOWS,
        "required_windows_by_code": {code: depth_for(code) for code in sorted(THRESHOLD_TRIGGER_CODES)},
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
    # Absolute 5xx stop-loss.  Runs in both comparison modes and at every split
    # above zero, because it does not depend on a reference cohort -- which is the
    # point: the delta leg is dark at production volume, and after cutover the
    # stable pool retains only health checks plus a handful of force-prod keys.
    # The guard cohort is stable's inference traffic, thin but real, and the leg
    # reports when it could not be read rather than standing down.
    abs_summary, abs_breaches, abs_alerts = absolute_five_xx(groups, "canary", "stable")
    if rollout > 0:
        threshold_triggers |= abs_breaches
        alerts |= abs_alerts
    else:
        abs_summary["qualified"] = False
        abs_summary["skipped_reason"] = "split_zero"

    # Leg 4, the users'-eye view.  Same gating as the stop-loss above -- at split 0
    # there is no gray traffic to judge and a zero-request window must not read as
    # a clean one.
    user_summary, user_triggers, user_alerts = user_facing_failures(groups, "canary")
    if rollout > 0:
        threshold_triggers |= user_triggers
        alerts |= user_alerts
    else:
        user_summary["qualified"] = False
        user_summary["skipped_reason"] = "split_zero"

    # Legs 2 and 3 are observed facts, not sampled ratios, so they bypass the
    # sustain gate entirely (see THRESHOLD_TRIGGER_CODES) and they run at every
    # split including zero: a lane with no ready containers and a monitor whose
    # inputs stopped are faults regardless of how much traffic is pointed at it.
    ready_summary, ready_triggers, ready_errors = readiness(payload)
    liveness_summary, liveness_triggers, liveness_errors = data_liveness(payload)
    # Required once any traffic is split, same rule as spend reconciliation.  The
    # collector always emits both, so absence means a hand-assembled payload -- and
    # accepting it would move production traffic with the two legs that detect "the
    # lane is not running" and "the monitor stopped" switched off, which is the
    # precise condition they exist to make impossible.
    if rollout > 0:
        if ready_summary.get("status") == "NOT_PROVIDED":
            ready_errors.append("READINESS_MISSING")
        if liveness_summary.get("status") == "NOT_PROVIDED":
            liveness_errors.append("DATA_SOURCES_MISSING")
    if ready_errors or liveness_errors:
        return error_result(ready_errors + liveness_errors, groups)
    observed_faults = ready_triggers | liveness_triggers

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
    triggers = hard | sustained | spend_triggers | observed_faults
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
            "minimum_five_xx_delta_sample": MIN_FIVE_XX_SAMPLE,
            # Floors for the latency READINGS, named so nobody reads them as gate
            # state.  `latency_observed_only` says the same thing in one field for
            # anyone diffing this block against the pre-trim payload.
            "p95_reading_floor": MIN_P95_SAMPLE,
            "p99_reading_floor": MIN_P99_SAMPLE,
            "latency_observed_only": LATENCY_OBSERVED_ONLY,
            "sustain_windows": SUSTAIN_WINDOWS,
            "absolute_five_xx_rate": ABS_FIVE_XX_RATE,
            "minimum_absolute_five_xx_sample": MIN_ABS_FIVE_XX_SAMPLE,
            "minimum_absolute_five_xx_events": MIN_ABS_FIVE_XX_EVENTS,
            "absolute_sustain_windows": ABS_SUSTAIN_WINDOWS,
            # Kept in the block so the depth and the demoted veto are read
            # together: the depth is now the whole mechanism.
            "shared_fate_minimum_sample": SHARED_FATE_MIN_SAMPLE,
            "shared_fate_observed_only": True,
            "user_failure_rate": MAX_USER_FAILURE_RATE,
            "minimum_user_failure_events": MIN_USER_FAILURE_EVENTS,
            # The rate above is taken over 5xx + these codes only.  Emitted so a
            # reader can tell which population a `failure_rate` in the same payload
            # was measured on -- before 2026-09-20 it was every 4xx, and the two
            # numbers are not comparable.
            "user_failure_statuses": ["5xx"] + sorted(PROXY_EMITTED_CLIENT_STATUSES),
            "user_failure_sustain_windows": USER_FAILURE_SUSTAIN_WINDOWS,
            # Alert-only line for caller-side 4xx (499/405/400/401/403).
            "client_error_rate": MAX_USER_CLIENT_ERROR_RATE,
            "maximum_source_silence_seconds": MAX_SOURCE_SILENCE_SECONDS,
            "latency_excluded_statuses": sorted(LATENCY_EXCLUDED_STATUSES),
            "minimum_latency_window_minutes": MIN_LATENCY_WINDOW_MINUTES,
            # None means the percentiles came from the five-minute live window and
            # will mostly sit below the floors; a reader must not have to infer
            # that from the absence of a key.
            "latency_window_minutes": latency_window_minutes,
        },
        # Carried into the next cycle's input so a breach can be seen to repeat.
        "sustain": sustain_summary,
        # Always present, whether or not anything triggered.  `reason_codes` below
        # carries alerts ONLY when nothing triggered, so before this field existed
        # every alert vanished from the payload at exactly the moment a human
        # started reading it.  That was tolerable while the shared-fate cohort could
        # veto -- suppression was visible in the action itself -- and stopped being
        # tolerable when it was demoted to an attribution hint: the hint is now the
        # only place the payload says "both cohorts are equally bad, this is
        # probably upstream", and it was being dropped from every rollback.
        "alerts": sorted(alerts),
        "dispatcher_recommendation": {
            "action": action,
            "reason_codes": reason_codes,
            "hard_trigger": hard_trigger,
        },
        "groups": groups,
        "comparisons": comparisons,
        "absolute_five_xx": abs_summary,
        "user_facing_failures": user_summary,
        "readiness": ready_summary,
        "data_liveness": liveness_summary,
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
