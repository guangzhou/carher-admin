#!/usr/bin/env python3
"""Build a redacted, state-bound input document for metrics.py."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn


STATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
CONFIG_CHECKSUM_RE = re.compile(r"^(?:test-mode|[0-9a-f]{64})$")
LOG_LINE_RE = re.compile(
    r"^ts=(?P<captured_at>\S+)\s+\S+\s+(?P<status>[1-5][0-9]{2})\s+"
    r"(?P<request_time>[0-9]+(?:\.[0-9]+)?)\s+"
    r".*?\bpool=(?P<pool>stable|canary|guarded-old)\b.*?"
    r"\brt=(?P<upstream_times>.*?)\s+"
    r"\buri_class=(?P<uri_class>[A-Za-z0-9._:-]+)\b"
    # nginx emits `sid=$key_sid` at the end of this log_format
    # (render-production-nginx.py, the `litellm_gray` format).  Matched so the
    # trailing field cannot break the line match, and deliberately NOT captured
    # into the records:
    #
    #   * No leg reads it.  It was going to be the enrolment filter for the
    #     user-facing failure leg, and it cannot be one: `-` is nginx's
    #     empty-value default for a key missing from key-sid.map, and the canary
    #     pool carried 14614 of them on the 2026-09-14 run.  A filter that drops
    #     unknowns would have discarded most of the traffic it was meant to count.
    #     `pool=canary` is the ruler instead -- reaching that pool requires a
    #     resolved canonical key, since an unauthenticated request gets an empty
    #     $bucket_key and is forced to stable.
    #   * It is a per-request key fingerprint (sha256 head of the virtual key), and
    #     this payload is signed, shipped and kept.  Carrying which keys ran which
    #     requests through it, for a field nothing reads, widens the artifact for
    #     no measurement.
    #
    # Non-capturing and optional: parse_access_log calls fail() on any non-match,
    # so a required group would reject every line from a lane whose format predates
    # the field -- turning one missing field into a refusal to monitor at all.
    r"(?:\s+sid=[A-Za-z0-9._:-]+)?"
)
SECRET_RE = re.compile(
    r"(?:authorization|x-api-key)\s*[:=]|\bbearer\s+\S+|\bsk-[A-Za-z0-9._~-]{8,}",
    re.I,
)
MAX_LOG_AGE = timedelta(minutes=5)
MAX_CLOCK_SKEW = timedelta(minutes=1)
# The baseline window may be widened past MAX_LOG_AGE (see
# --baseline-window-minutes) but not without bound: it must stay inside
# MAX_BASELINE_AGE so a baseline still describes this change window, and a
# window long enough to average across a whole day would hide the very
# regressions the comparison exists to catch.
MAX_BASELINE_WINDOW = timedelta(hours=6)
# Latency percentiles need more samples than five minutes of traffic provides
# (the `responses` class medians 57), so they are computed over their own wider
# window.  MAX_LOG_AGE still governs `records`, which is what the 5xx stop-loss
# reads: averaging a fault across thirty minutes of health is exactly what a
# stop-loss must not do.  Keep the floor in lockstep with metrics.py's
# MIN_LATENCY_WINDOW_MINUTES, and the ceiling inside MAX_BASELINE_WINDOW so a
# live percentile window can never be longer than the baseline it is compared to.
LATENCY_WINDOW = timedelta(minutes=30)
MAX_LATENCY_WINDOW = MAX_BASELINE_WINDOW
# Keep in lockstep with metrics.py's LATENCY_EXCLUDED_STATUSES: a 101 is a
# websocket upgrade whose `rt` is connection lifetime, not service time.
LATENCY_EXCLUDED_STATUSES = frozenset({101})
SOURCE_KEYS = {"schema_version", "source", "captured_at", "payload_sha256", "data"}
# A baseline is deliberately older than the live window, but "older" is not
# "unbounded": it must come from this run, from this tool, and from inside the
# change window.  Otherwise a hand-written file is indistinguishable from a
# measured reference, and every 100%-rollout comparison is against fiction.
BASELINE_SOURCE = "collect-metrics.py --emit-baseline"
MAX_BASELINE_AGE = timedelta(hours=24)
BASELINE_ITEM_KEYS = {
    "uri_class",
    "pool_label",
    "count",
    # Samples behind p95/p99 after websocket upgrades are excluded.  Named
    # explicitly because this set is compared for equality: a baseline written
    # by an older build lacks the key and is rejected rather than silently
    # compared against percentiles built from a different population.
    "latency_count",
    "five_xx_count",
    "five_xx_rate",
    "p95",
    "p99",
    "run_id",
    "generation",
    "window_started_at",
    "window_ended_at",
}
DEFAULT_BASELINE_MIN_SAMPLES = 100


def fail(message: str) -> NoReturn:
    raise SystemExit(f"collect-metrics: {message}")


def digest(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def read_regular(path: Path, label: str) -> str:
    try:
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            fail(f"{label} must be a regular non-symlink file")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail(f"cannot read {label}: {exc}")


def load_source(
    path: Path | None,
    label: str,
    expected: type,
    default: Any,
    *,
    require_fresh: bool = True,
) -> tuple[Any, datetime | None]:
    if path is None:
        return default, None
    raw = read_regular(path, label)
    if SECRET_RE.search(raw):
        fail(f"{label} contains secret-bearing data")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        fail(f"{label} is invalid JSON")
    if (
        not isinstance(envelope, dict)
        or set(envelope) != SOURCE_KEYS
        or envelope.get("schema_version") != 1
        or not isinstance(envelope.get("source"), str)
        or not envelope["source"].strip()
        or not isinstance(envelope.get("data"), expected)
    ):
        fail(f"{label} evidence has invalid structure")
    captured_at = parse_timestamp(str(envelope.get("captured_at", "")))
    checksum = envelope.get("payload_sha256")
    if captured_at is None or checksum != digest(envelope["data"]):
        fail(f"{label} evidence checksum or timestamp is invalid")
    now = datetime.now(timezone.utc)
    if captured_at - now > MAX_CLOCK_SKEW:
        fail(f"{label} evidence timestamp is in the future")
    if require_fresh and now - captured_at > MAX_LOG_AGE:
        fail(f"{label} evidence is stale")
    return envelope["data"], captured_at


def upstream_response_time(value: str, request_time: str) -> float:
    values = [float(item) for item in re.findall(r"[0-9]+(?:\.[0-9]+)?", value)]
    if not values:
        return float(request_time)
    return round(sum(values), 6)


def parse_timestamp(value: str) -> datetime | None:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def parse_access_log(
    path: Path,
    *,
    window: timedelta = MAX_LOG_AGE,
    latency_window: timedelta | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, datetime]:
    """Read the access log, keeping records inside `window` of now.

    `window` defaults to MAX_LOG_AGE so the live stop-loss path is unchanged.
    Only --emit-baseline widens it: a baseline needs enough samples per
    uri_class to be a reference at all, and the sparse classes (messages,
    image) never reach that floor in five minutes of real traffic.  Widening
    the live window instead would blunt the stop-loss ruler itself.

    `latency_window`, when given, returns a second and wider slice of the SAME
    read for the percentile legs.  It is one read on purpose: reading the file
    twice would let the log grow between them, and the wide slice would then no
    longer be a superset of the live one -- so a class could show more 5xx than
    requests, and the merged group in metrics.py would be incoherent.
    """
    raw = read_regular(path, "access log")
    if SECRET_RE.search(raw):
        fail("access log contains secret-bearing data")
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        match = LOG_LINE_RE.search(line)
        if match is None:
            fail(f"invalid access log line {line_number}")
        timestamp = parse_timestamp(match.group("captured_at"))
        if timestamp is None:
            fail(f"invalid access log timestamp on line {line_number}")
        parsed.append(
            (
                timestamp,
                {
                "pool_label": match.group("pool"),
                "uri_class": match.group("uri_class"),
                "status": int(match.group("status")),
                "response_time": upstream_response_time(
                    match.group("upstream_times"), match.group("request_time")
                ),
                },
            )
        )
    if not parsed:
        fail("access log contains no metric records")
    now = datetime.now(timezone.utc)
    newest = max(timestamp for timestamp, _ in parsed)
    if newest - now > MAX_CLOCK_SKEW:
        fail("access log window is in the future")
    records = [record for timestamp, record in parsed if timedelta(0) <= now - timestamp <= window]
    if not records:
        fail("access log window is stale")
    latency_records = None
    if latency_window is not None:
        if latency_window < window:
            fail("latency window cannot be narrower than the live window")
        latency_records = [
            record
            for timestamp, record in parsed
            if timedelta(0) <= now - timestamp <= latency_window
        ]
    return records, latency_records, newest


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def summarize_pool(
    records: list[dict[str, Any]],
    pool: str,
    *,
    min_samples: int,
    exempt_classes: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Aggregate one pool per uri_class into metrics.py's comparison shape.

    A class listed in `exempt_classes` may enter the baseline below
    `min_samples`.  This exists for classes that cannot reach the floor at any
    window length -- `image` sees about one request a day -- where the honest
    options are an explicit, recorded exemption or no baseline at all.  It is
    not a way to admit a class that is merely inconvenient: the exemption is
    named on the command line and the resulting `count` shows how thin it is.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record["pool_label"] == pool:
            buckets.setdefault(record["uri_class"], []).append(record)
    if not buckets:
        fail(f"access log window contains no {pool} records to baseline")

    thin = sorted(
        name
        for name, items in buckets.items()
        if len(items) < min_samples and name not in exempt_classes
    )
    if thin:
        counts = ", ".join(f"{name}={len(buckets[name])}" for name in thin)
        fail(
            "baseline window is too thin for "
            + ",".join(thin)
            + f" (need >={min_samples} samples each; have {counts}); capture a "
            "longer window with --baseline-window-minutes, or exempt a class "
            "that cannot reach the floor with --baseline-exempt-class"
        )

    groups: list[dict[str, Any]] = []
    for uri_class in sorted(buckets):
        items = buckets[uri_class]
        # Websocket upgrades are counted but kept out of the percentiles: their
        # `rt` is connection lifetime, not service time.  Must match metrics.py's
        # LATENCY_EXCLUDED_STATUSES, or a baseline and the live window would be
        # measuring two different quantities and every ratio against it is void.
        latencies = [
            float(item["response_time"])
            for item in items
            if int(item["status"]) not in LATENCY_EXCLUDED_STATUSES
        ]
        five_xx = sum(1 for item in items if int(item["status"]) >= 500)
        if not latencies:
            # percentile() would return 0.0 here, which a comparison would read
            # as "instant", not "unmeasured", and a ratio against 0 is not a
            # reading at all.  Refuse instead of freezing a fake floor.
            fail(
                f"uri_class {uri_class} has {len(items)} records but none carry a "
                "latency sample (all websocket upgrades); it cannot be a latency "
                "baseline -- exempt it with --baseline-exempt-class or widen the "
                "window until it sees real requests"
            )
        groups.append(
            {
                "pool_label": pool,
                "uri_class": uri_class,
                "count": len(items),
                "latency_count": len(latencies),
                "five_xx_count": five_xx,
                "five_xx_rate": round(five_xx / len(items), 6),
                "p95": round(percentile(latencies, 0.95), 6),
                "p99": round(percentile(latencies, 0.99), 6),
            }
        )
    return groups


def load_baseline(path: Path | None, run_id: str) -> Any:
    """Load a baseline emitted by this tool for this run, inside the window."""
    if path is None:
        return None
    raw = read_regular(path, "baseline")
    if SECRET_RE.search(raw):
        fail("baseline contains secret-bearing data")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        fail("baseline is invalid JSON")
    if (
        not isinstance(envelope, dict)
        or set(envelope) != SOURCE_KEYS
        or envelope.get("schema_version") != 1
        or not isinstance(envelope.get("data"), list)
        or not envelope["data"]
    ):
        fail("baseline evidence has invalid structure")
    if envelope.get("source") != BASELINE_SOURCE:
        fail(f"baseline must be produced by `{BASELINE_SOURCE}`")
    captured_at = parse_timestamp(str(envelope.get("captured_at", "")))
    if captured_at is None or envelope.get("payload_sha256") != digest(envelope["data"]):
        fail("baseline evidence checksum or timestamp is invalid")
    now = datetime.now(timezone.utc)
    if captured_at - now > MAX_CLOCK_SKEW:
        fail("baseline evidence timestamp is in the future")
    if now - captured_at > MAX_BASELINE_AGE:
        fail("baseline evidence is older than the change window")
    for item in envelope["data"]:
        if not isinstance(item, dict) or set(item) != BASELINE_ITEM_KEYS:
            fail("baseline entry has invalid structure")
        if item["run_id"] != run_id:
            fail("baseline was captured for a different run id")
    return envelope["data"]


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-log", type=Path, required=True)
    parser.add_argument("--hard-errors", type=Path)
    parser.add_argument("--backend-health", type=Path)
    parser.add_argument("--spend-reconciliation", type=Path)
    parser.add_argument(
        "--readiness",
        type=Path,
        help=(
            "ready-container reading for the gray lane (metrics.py leg 2). The "
            "document carries ready_containers AND expected_containers: the "
            "expected number belongs to the run sheet, not to a constant in "
            "metrics.py, because a hard-coded one stops matching after any scale "
            "change and never goes red when it does."
        ),
    )
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--emit-baseline",
        action="store_true",
        help=(
            "freeze the current stable-pool window as the comparison baseline "
            "instead of building a metrics input; requires --rollout-percent 0"
        ),
    )
    parser.add_argument(
        "--baseline-min-samples",
        type=int,
        default=DEFAULT_BASELINE_MIN_SAMPLES,
        help="minimum samples per uri_class the baseline window must contain",
    )
    parser.add_argument(
        "--baseline-window-minutes",
        type=int,
        default=None,
        help=(
            "widen ONLY the --emit-baseline window to this many minutes "
            f"(default {int(MAX_LOG_AGE.total_seconds() // 60)}, max "
            f"{int(MAX_BASELINE_WINDOW.total_seconds() // 60)}). The live "
            "metrics window is never affected; it stays at MAX_LOG_AGE so the "
            "stop-loss ruler keeps its reaction time."
        ),
    )
    parser.add_argument(
        "--latency-window-minutes",
        type=int,
        default=int(LATENCY_WINDOW.total_seconds() // 60),
        help=(
            "window for the p95/p99 legs only "
            f"(default {int(LATENCY_WINDOW.total_seconds() // 60)}, max "
            f"{int(MAX_LATENCY_WINDOW.total_seconds() // 60)}). The 5xx "
            "stop-loss keeps reading the "
            f"{int(MAX_LOG_AGE.total_seconds() // 60)}-minute live window. "
            "Below metrics.py's floor the percentile legs go dark, so this "
            "cannot be narrowed to disable them quietly."
        ),
    )
    parser.add_argument(
        "--baseline-exempt-class",
        action="append",
        default=[],
        metavar="URI_CLASS",
        help=(
            "allow this uri_class into the baseline below --baseline-min-samples. "
            "Each exemption is recorded in the baseline envelope, because a class "
            "admitted below the floor has a weaker reference than the others and "
            "the reader must be able to see which ones."
        ),
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--config-checksum", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--rollout-percent", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not STATE_ID_RE.fullmatch(args.run_id) or not STATE_ID_RE.fullmatch(args.generation):
        fail("run-id or generation is invalid")
    if not CONFIG_CHECKSUM_RE.fullmatch(args.config_checksum):
        fail("config-checksum is invalid")
    if not 0 <= args.rollout_percent <= 100:
        fail("rollout-percent must be between 0 and 100")

    baseline_window = MAX_LOG_AGE
    if args.baseline_window_minutes is not None:
        if not args.emit_baseline:
            fail("--baseline-window-minutes only applies to --emit-baseline")
        if args.baseline_window_minutes < 1:
            fail("baseline-window-minutes must be positive")
        baseline_window = timedelta(minutes=args.baseline_window_minutes)
        if baseline_window > MAX_BASELINE_WINDOW:
            fail(
                "baseline-window-minutes exceeds "
                f"{int(MAX_BASELINE_WINDOW.total_seconds() // 60)}"
            )
        if baseline_window < MAX_LOG_AGE:
            fail(
                "baseline-window-minutes cannot be narrower than the live window "
                f"({int(MAX_LOG_AGE.total_seconds() // 60)} minutes)"
            )
    if args.baseline_exempt_class and not args.emit_baseline:
        fail("--baseline-exempt-class only applies to --emit-baseline")

    latency_window = timedelta(minutes=args.latency_window_minutes)
    if latency_window < MAX_LOG_AGE:
        fail(
            "latency-window-minutes cannot be narrower than the live window "
            f"({int(MAX_LOG_AGE.total_seconds() // 60)} minutes)"
        )
    if latency_window > MAX_LATENCY_WINDOW:
        fail(
            "latency-window-minutes exceeds "
            f"{int(MAX_LATENCY_WINDOW.total_seconds() // 60)}"
        )

    records, latency_records, access_captured_at = parse_access_log(
        args.access_log,
        window=baseline_window if args.emit_baseline else MAX_LOG_AGE,
        # --emit-baseline already reads one wide window and computes percentiles
        # over all of it; a second wider slice there would mean nothing.
        latency_window=None if args.emit_baseline else latency_window,
    )

    if args.emit_baseline:
        if args.baseline is not None:
            fail("--emit-baseline produces a baseline; it does not consume one")
        if args.rollout_percent != 0:
            fail("baseline must be captured before any gray traffic (--rollout-percent 0)")
        if args.baseline_min_samples < 1:
            fail("baseline-min-samples must be positive")
        window_end = access_captured_at
        window_start = window_end - baseline_window
        groups = [
            {
                **group,
                "run_id": args.run_id,
                "generation": args.generation,
                "window_started_at": window_start.isoformat().replace("+00:00", "Z"),
                "window_ended_at": window_end.isoformat().replace("+00:00", "Z"),
            }
            for group in summarize_pool(
                records,
                "stable",
                min_samples=args.baseline_min_samples,
                exempt_classes=frozenset(args.baseline_exempt_class),
            )
        ]
        envelope = {
            "schema_version": 1,
            "source": BASELINE_SOURCE,
            "captured_at": window_end.isoformat().replace("+00:00", "Z"),
            "payload_sha256": digest(groups),
            "data": groups,
        }
        secure_write_new(args.output, envelope)
        print(
            json.dumps(
                {
                    "tool": "collect-metrics",
                    "mode": "emit-baseline",
                    "status": "PASS",
                    "output": str(args.output),
                    "uri_classes": [group["uri_class"] for group in groups],
                    "payload_sha256": envelope["payload_sha256"],
                    "window_minutes": int(baseline_window.total_seconds() // 60),
                    "min_samples": args.baseline_min_samples,
                    # Named so the reader can see which classes hold a weaker
                    # reference than the floor would otherwise guarantee.
                    "exempted_classes": sorted(set(args.baseline_exempt_class)),
                    "thin_classes": sorted(
                        group["uri_class"]
                        for group in groups
                        if group["count"] < args.baseline_min_samples
                    ),
                },
                sort_keys=True,
            )
        )
        return 0

    hard_errors, hard_errors_at = load_source(
        args.hard_errors, "hard errors", dict, {}
    )
    backend_health, backend_health_at = load_source(
        args.backend_health, "backend health", dict, {}
    )
    spend_reconciliation, spend_at = load_source(
        args.spend_reconciliation, "spend reconciliation", dict, None
    )
    readiness, readiness_at = load_source(args.readiness, "readiness", dict, {})
    baseline = load_baseline(args.baseline, args.run_id)
    captured_times = [
        item
        for item in (
            access_captured_at, hard_errors_at, backend_health_at, spend_at,
            readiness_at,
        )
        if item is not None
    ]
    captured_at = min(captured_times)
    # Data-liveness input (metrics.py leg 3).  Built here rather than read from
    # another file because the collector already holds every source's own
    # timestamp -- asking an operator to supply them again would let the two
    # disagree, and the liveness leg would then be judging the second copy.
    #
    # A source that was not requested on the command line is ABSENT from this list,
    # not listed with a stale timestamp: "we did not ask for backend health" and
    # "backend health stopped answering" are different facts and only one is a
    # fault.  Each entry's name matches the flag that supplied it.
    data_sources = [
        {"name": name, "observed_at": moment.isoformat().replace("+00:00", "Z")}
        for name, moment in (
            ("access_log", access_captured_at),
            ("hard_errors", hard_errors_at if args.hard_errors else None),
            ("backend_health", backend_health_at if args.backend_health else None),
            ("spend_reconciliation", spend_at if args.spend_reconciliation else None),
            ("readiness", readiness_at if args.readiness else None),
        )
        if moment is not None
    ]
    payload: dict[str, Any] = {
        "phase": args.phase,
        "rollout_percent": args.rollout_percent,
        "records": records,
        "hard_errors": hard_errors,
    }
    if latency_records is not None:
        # Both keys or neither: metrics.py rejects a half-supplied pair rather
        # than guessing how many minutes the extra records cover.
        payload["latency_records"] = latency_records
        payload["latency_window_minutes"] = int(latency_window.total_seconds() // 60)
    if args.backend_health is not None:
        payload["backend_health"] = backend_health
    if args.spend_reconciliation is not None:
        payload["spend_reconciliation"] = spend_reconciliation
    if args.readiness is not None:
        payload["readiness"] = readiness
    payload["data_sources"] = data_sources
    if baseline is not None:
        payload["baseline"] = baseline

    payload["evidence"] = {
        "schema_version": 1,
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "payload_sha256": digest(payload),
        "source": "collect-metrics.py",
        "run_id": args.run_id,
        "generation": args.generation,
        "config_checksum": args.config_checksum,
    }
    secure_write_new(args.output, payload)
    print(json.dumps({"tool": "collect-metrics", "status": "PASS", "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
