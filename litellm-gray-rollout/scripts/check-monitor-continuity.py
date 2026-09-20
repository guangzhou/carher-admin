#!/usr/bin/env python3
"""Turn the monitor-cycle heartbeat ledger into a fail-closed ramp gate.

A ramp step is a claim about an observation window: "gray behaved for the last
N minutes at the current split". Metrics evidence files only prove the cycles
that *did* run. If the scheduler died, the host rebooted, or the collector
started failing, the window simply has no files in it -- and an unobserved
window is indistinguishable from a healthy one at ramp time. That is the empty
data column the diagnosis discipline forbids.

So this tool reads the append-only ledger written by gray-monitor-cycle.sh and
answers two questions with data about the span between --window-start and now:

  1. was there ever a gap longer than the approved cycle interval allows?
  2. were there *enough* cycles for the deepest sustain leg to be able to fire?

Question 2 exists because of what the 2026-09-14 run actually did. Measured from
the heartbeat ledger, the 5% / 10% / 50% steps ran 2 monitor cycles each, while
metrics.py requires ABS_SUSTAIN_WINDOWS = 4 consecutive breaching windows before
the absolute 5xx stop-loss fires. In three of five ramp steps that leg was
physically incapable of firing -- and this gate was green for all of them,
because it only ever compared *intervals* and never the *count*. A gap check
cannot see a window that is merely too short: two cycles 5 minutes apart have no
gap at all. A stop-loss that cannot fire is the same shape as one that fired and
found nothing, which is exactly the empty data column the discipline forbids.

It emits gate evidence in the shape require_gate_evidence() consumes, and
returns 1 on FAIL. It never talks to the cluster and never mutates routing state.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, NoReturn


TOOL = "check-monitor-continuity"
GATE = "split_monitor_continuity"
SCHEMA_VERSION = 1
STATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
CONFIG_CHECKSUM_RE = re.compile(r"^(?:test-mode|[0-9a-f]{64})$")
RECORD_KEYS = {
    "schema_version",
    "tool",
    "cycle_completed_at",
    "run_id",
    "generation",
    "metrics_status",
    "evidence",
}
MAX_CLOCK_SKEW = dt.timedelta(minutes=1)

# How many completed cycles a ramp step must accumulate before it may claim the
# window was observed. 4 == metrics.py's ABS_SUSTAIN_WINDOWS: the absolute 5xx
# stop-loss leg needs 4 consecutive breaching windows, so a step that dwells for
# fewer than 4 cycles has never armed it. Raising ABS_SUSTAIN_WINDOWS without
# raising this default silently disarms that leg again -- the coupling is
# arithmetic, and a comment is not a gate, so keep them equal.
DEFAULT_MIN_CYCLES = 4


def fail(message: str) -> NoReturn:
    raise SystemExit(f"{TOOL}: {message}")


def digest(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def parse_timestamp(value: str) -> dt.datetime | None:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def read_ledger(path: Path) -> list[dict[str, Any]]:
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot read heartbeat ledger: {exc}")
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        fail("heartbeat ledger must be a regular non-symlink file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        fail("heartbeat ledger must not be group/world accessible")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail(f"cannot read heartbeat ledger: {exc}")

    records: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            fail(f"heartbeat ledger line {number} is invalid JSON")
        if not isinstance(record, dict) or set(record) != RECORD_KEYS:
            fail(f"heartbeat ledger line {number} has an unexpected shape")
        if record["schema_version"] != SCHEMA_VERSION or record["tool"] != "gray-monitor-cycle":
            fail(f"heartbeat ledger line {number} was not written by gray-monitor-cycle.sh")
        stamp = parse_timestamp(str(record["cycle_completed_at"]))
        if stamp is None:
            fail(f"heartbeat ledger line {number} has an invalid timestamp")
        record["_at"] = stamp
        records.append(record)
    if not records:
        fail("heartbeat ledger contains no completed cycles")
    records.sort(key=lambda item: item["_at"])
    return records


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    now = dt.datetime.now(dt.timezone.utc)
    window_start = parse_timestamp(args.window_start)
    if window_start is None:
        fail("--window-start must be an ISO-8601 timestamp with a timezone")
    if window_start - now > MAX_CLOCK_SKEW:
        fail("--window-start is in the future")
    if args.cycle_interval_seconds < 1:
        fail("--cycle-interval-seconds must be positive")
    if args.max_missed_cycles < 0:
        fail("--max-missed-cycles must not be negative")
    if args.min_cycles < 1:
        fail("--min-cycles must be at least 1")

    allowed = dt.timedelta(
        seconds=args.cycle_interval_seconds * (1 + args.max_missed_cycles)
    )
    records = read_ledger(args.ledger)

    errors: list[str] = []
    foreign = sorted(
        {str(item["run_id"]) for item in records if item["run_id"] != args.run_id}
    )
    in_window = [item for item in records if item["_at"] >= window_start]
    if not in_window:
        errors.append("NO_CYCLES_IN_WINDOW")

    gaps: list[dict[str, Any]] = []
    previous = window_start
    for item in in_window:
        delta = item["_at"] - previous
        if delta > allowed:
            gaps.append(
                {
                    "from": previous.isoformat().replace("+00:00", "Z"),
                    "to": item["_at"].isoformat().replace("+00:00", "Z"),
                    "seconds": int(delta.total_seconds()),
                }
            )
        previous = item["_at"]
    trailing = now - previous
    if trailing > allowed:
        gaps.append(
            {
                "from": previous.isoformat().replace("+00:00", "Z"),
                "to": now.isoformat().replace("+00:00", "Z"),
                "seconds": int(trailing.total_seconds()),
            }
        )

    failed_cycles = [
        item["evidence"] for item in in_window if item["metrics_status"] != "PASS"
    ]
    # A window with cycles but too few of them: no gap, nothing failed, and the
    # deepest sustain leg still never had the consecutive windows it needs. The
    # empty case is already NO_CYCLES_IN_WINDOW, so this stays disjoint from it.
    if in_window and len(in_window) < args.min_cycles:
        errors.append("INSUFFICIENT_CYCLES")
    if gaps:
        errors.append("MONITORING_GAP")
    if foreign:
        errors.append("FOREIGN_RUN_ID")
    if failed_cycles:
        errors.append("FAILED_CYCLE_IN_WINDOW")

    result: dict[str, Any] = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "gate": GATE,
        "status": "FAIL" if errors else "PASS",
        "run_id": args.run_id,
        "generation": args.generation,
        "config_checksum": args.config_checksum,
        "captured_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "ledger": str(args.ledger),
        "window_started_at": window_start.isoformat().replace("+00:00", "Z"),
        "cycle_interval_seconds": args.cycle_interval_seconds,
        "max_missed_cycles": args.max_missed_cycles,
        "max_allowed_gap_seconds": int(allowed.total_seconds()),
        "cycles_in_window": len(in_window),
        "min_cycles": args.min_cycles,
        "min_observation_seconds": args.min_cycles * args.cycle_interval_seconds,
        "last_cycle_at": previous.isoformat().replace("+00:00", "Z"),
        "gaps": gaps,
        "failed_cycles": failed_cycles,
        "foreign_run_ids": foreign,
        "errors": errors,
    }
    result["result_sha256"] = digest(
        {key: value for key, value in result.items() if key != "captured_at"}
    )
    return result, 1 if errors else 0


def secure_write(path: Path, payload: dict[str, Any]) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or not parent.is_dir():
        fail("output directory is unsafe")
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    temporary = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        fail(f"cannot safely write output: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--config-checksum", required=True)
    parser.add_argument(
        "--window-start",
        required=True,
        help="start of the observation window this ramp step claims (ISO-8601 with tz)",
    )
    parser.add_argument(
        "--cycle-interval-seconds",
        type=int,
        required=True,
        help="the frozen scheduler interval; no guessing, copy it from the run sheet",
    )
    parser.add_argument(
        "--max-missed-cycles",
        type=int,
        default=1,
        help="how many consecutive cycles may be missed before the window is unobserved",
    )
    parser.add_argument(
        "--min-cycles",
        type=int,
        default=DEFAULT_MIN_CYCLES,
        help=(
            "minimum completed cycles this window must contain; must stay equal to "
            "metrics.py's ABS_SUSTAIN_WINDOWS, or the deepest stop-loss leg cannot fire"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if not STATE_ID_RE.fullmatch(args.run_id) or not STATE_ID_RE.fullmatch(args.generation):
        fail("run-id or generation is invalid")
    if not CONFIG_CHECKSUM_RE.fullmatch(args.config_checksum):
        fail("config-checksum is invalid")

    result, code = run(args)
    if args.output is not None:
        secure_write(args.output, result)
    json.dump(result, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
