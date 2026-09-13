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
)
SECRET_RE = re.compile(
    r"(?:authorization|x-api-key)\s*[:=]|\bbearer\s+\S+|\bsk-[A-Za-z0-9._~-]{8,}",
    re.I,
)
MAX_LOG_AGE = timedelta(minutes=5)
MAX_CLOCK_SKEW = timedelta(minutes=1)
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


def parse_access_log(path: Path) -> tuple[list[dict[str, Any]], datetime]:
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
    records = [record for timestamp, record in parsed if timedelta(0) <= now - timestamp <= MAX_LOG_AGE]
    if not records:
        fail("access log window is stale")
    return records, newest


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def summarize_pool(
    records: list[dict[str, Any]], pool: str, *, min_samples: int
) -> list[dict[str, Any]]:
    """Aggregate one pool per uri_class into metrics.py's comparison shape."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record["pool_label"] == pool:
            buckets.setdefault(record["uri_class"], []).append(record)
    if not buckets:
        fail(f"access log window contains no {pool} records to baseline")

    thin = sorted(name for name, items in buckets.items() if len(items) < min_samples)
    if thin:
        fail(
            "baseline window is too thin for "
            + ",".join(thin)
            + f" (need >={min_samples} samples each); capture a longer window"
        )

    groups: list[dict[str, Any]] = []
    for uri_class in sorted(buckets):
        items = buckets[uri_class]
        latencies = [float(item["response_time"]) for item in items]
        five_xx = sum(1 for item in items if int(item["status"]) >= 500)
        groups.append(
            {
                "pool_label": pool,
                "uri_class": uri_class,
                "count": len(items),
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

    records, access_captured_at = parse_access_log(args.access_log)

    if args.emit_baseline:
        if args.baseline is not None:
            fail("--emit-baseline produces a baseline; it does not consume one")
        if args.rollout_percent != 0:
            fail("baseline must be captured before any gray traffic (--rollout-percent 0)")
        if args.baseline_min_samples < 1:
            fail("baseline-min-samples must be positive")
        window_end = access_captured_at
        window_start = window_end - MAX_LOG_AGE
        groups = [
            {
                **group,
                "run_id": args.run_id,
                "generation": args.generation,
                "window_started_at": window_start.isoformat().replace("+00:00", "Z"),
                "window_ended_at": window_end.isoformat().replace("+00:00", "Z"),
            }
            for group in summarize_pool(
                records, "stable", min_samples=args.baseline_min_samples
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
    baseline = load_baseline(args.baseline, args.run_id)
    captured_times = [
        item
        for item in (access_captured_at, hard_errors_at, backend_health_at, spend_at)
        if item is not None
    ]
    captured_at = min(captured_times)
    payload: dict[str, Any] = {
        "phase": args.phase,
        "rollout_percent": args.rollout_percent,
        "records": records,
        "hard_errors": hard_errors,
    }
    if args.backend_health is not None:
        payload["backend_health"] = backend_health
    if args.spend_reconciliation is not None:
        payload["spend_reconciliation"] = spend_reconciliation
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
