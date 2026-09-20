#!/usr/bin/env python3
"""Turn measured upstream-seconds into the fail-closed `split_capacity` gate.

gray-split-update.sh has demanded this gate at every step >= 50% since the run
book was written, and until 2026-09-20 nothing in this repository produced it.
The evidence file was hand-written, which is the exact shape section 6.1.4 of the
operator manual exists to remove: a gate that validates the SHAPE of a claim and
never its TRUTH.  A human typing `"status": "PASS"` into a JSON file proves that
a human typed it.

WHY UPSTREAM-SECONDS AND NOT REQUEST COUNT

Because the two disagree, measured on this system.  On the 2026-09-18 window the
canary lane carried 21.4% of the REQUESTS and 44.9% of the WORK -- the class mix
differs between the pools, and a `responses` call costs many times a `chat` call.
Anything that divides by request count is therefore answering a question nobody
asked: "can the lane take 50% of the lines in the log?"  The lane does not serve
lines, it serves seconds of upstream time, and seconds are what its workers run
out of.  Every number below is a seconds-per-second figure, which is also a
concurrency: the mean number of upstream requests in flight (Little's law), which
is the quantity a worker pool actually caps.

WHAT THIS PROVES, AND WHAT IT CANNOT

It measures, per uri_class, what a request on THIS build costs in upstream
seconds; projects total demand to the target split; divides by the READY
container count (never replicas -- a Deployment scaled to 0 still reports
`Available: True`, and on the acct pool 165 deployments with replicas>0 had only
54 serving); and compares that against a per-container ceiling.

The ceiling itself is a run-sheet input with no default, on purpose.  This tool
cannot measure the point at which a container stops coping -- nothing short of
driving one there can -- and a default would be a hard-coded fleet constant that
stops matching after any change and never goes red when it does (the
`llm-stab-scrape-down` failure, where a literal 5 sat in a rule for weeks).  So
the tool does the part a human cannot do by hand -- the per-class cost, the
request-to-seconds conversion, the projection, the per-container division -- and
refuses to run without the one number that must come from the run sheet.

It does check that the supplied ceiling is not fiction: a ceiling BELOW what a
lane is already demonstrably sustaining per container is wrong about the lane,
not a verdict about it, and that fails closed (CEILING_BELOW_DEMONSTRATED).

It never talks to the cluster and never mutates routing state.
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
from collections import defaultdict
from pathlib import Path
from typing import Any, NoReturn


TOOL = "check-split-capacity"
GATE = "split_capacity"
SCHEMA_VERSION = 1
STATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
CONFIG_CHECKSUM_RE = re.compile(r"^(?:test-mode|[0-9a-f]{64})$")
MAX_CLOCK_SKEW = dt.timedelta(minutes=1)
SOURCE_KEYS = {"schema_version", "source", "captured_at", "payload_sha256", "data"}
READINESS_KEYS = {"lane", "ready_containers", "expected_containers", "observed_at"}

# Only inference classes are costed.  Keep in lockstep with metrics.py's
# INFERENCE_CLASSES: `health` and `images` are in the log but are not the work a
# worker pool runs out of, and including them would dilute the per-request cost
# of the classes that are.
INFERENCE_CLASSES = frozenset({"chat", "messages", "responses", "embedding"})

# How long a window must be before it may be called a capacity measurement.  A
# capacity claim is about a sustained rate, and a one-minute window on this
# traffic is a burst reading: the `responses` class medians 57 requests in five
# minutes, so a shorter window prices it off single digits.  Keep at or above
# collect-metrics.py's MAX_LOG_AGE (5 min) -- the same window the stop-loss reads.
MIN_WINDOW_SECONDS = 300
# ...and a ceiling, for the opposite reason.  Averaging demand across six hours
# hides the peak the lane must actually survive, and a capacity gate that passes
# on a daily mean is the 「配置吻合症状」 shape: true on average, false when it
# matters.  6h == collect-metrics.py's MAX_BASELINE_WINDOW.
MAX_WINDOW_SECONDS = 6 * 3600

# Samples a uri_class needs on the GRAY pool before its per-request cost is a
# reading rather than an anecdote.  30 is the same floor metrics.py uses for its
# rate legs: below it a single slow request moves the class mean by more than the
# margin this gate is deciding on.
MIN_CLASS_SAMPLES = 30

# How old the ready-container reading may be.  300 == collect-metrics.py's
# MAX_LOG_AGE and metrics.py's MAX_SOURCE_SILENCE_SECONDS and the monitor loop's
# cycle interval.  🔴 This number is arithmetically coupled to that cadence: raise
# GRAY_CYCLE_INTERVAL_SECONDS without raising this and every capacity check starts
# failing on a perfectly healthy loop, because the freshest reading the loop can
# produce is already older than this allows.  Keep them equal.
MAX_READINESS_AGE_SECONDS = 300


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


def read_regular(path: Path, label: str) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot read {label}: {exc}")
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        fail(f"{label} must not be group/world accessible")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail(f"cannot read {label}: {exc}")


def load_collector() -> Any:
    """Import collect-metrics.py and borrow its access-log parser.

    ⛔ Deliberately NOT a second copy of the regex.  The replay harness already
    carries one, and the failure mode it has to be tested against is a silent
    one: if nginx's log_format changes and only one copy is updated, the tool that
    was not updated matches nothing and reports a clean zero -- which for THIS
    tool means zero demand, i.e. infinite headroom, i.e. a green capacity gate on
    a lane nobody measured.  Sharing the parser removes that failure mode instead
    of guarding it.
    """
    path = Path(__file__).resolve().parent / "collect-metrics.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location("_collect_metrics_for_capacity", path)
    if spec is None or spec.loader is None:
        fail("cannot load collect-metrics.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - import failure is environmental
        fail(f"cannot load collect-metrics.py: {exc}")
    for attribute in ("LOG_LINE_RE", "upstream_response_time"):
        if not hasattr(module, attribute):
            fail(f"collect-metrics.py has no {attribute}; the shared parser moved")
    return module


def read_access_log(
    path: Path, window: dt.timedelta
) -> tuple[dict[tuple[str, str], dict[str, float]], dt.datetime, dt.datetime]:
    """Bucket the window's requests into per-(pool, class) count and upstream seconds.

    Upstream seconds, not request_time: the lane's cost is what it spent waiting on
    the model, which is what `rt` carries.  upstream_response_time() sums the hops
    of a retried request, because two upstream attempts occupy the worker twice.
    """
    collector = load_collector()
    raw = read_regular(path, "access log")
    now = dt.datetime.now(dt.timezone.utc)
    buckets: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"count": 0.0, "upstream_seconds": 0.0}
    )
    newest: dt.datetime | None = None
    oldest: dt.datetime | None = None
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        match = collector.LOG_LINE_RE.search(line)
        if match is None:
            fail(f"invalid access log line {number}")
        stamp = parse_timestamp(match.group("captured_at"))
        if stamp is None:
            fail(f"invalid access log timestamp on line {number}")
        if stamp - now > MAX_CLOCK_SKEW:
            fail("access log window is in the future")
        age = now - stamp
        if age < dt.timedelta(0) or age > window:
            continue
        seconds = collector.upstream_response_time(
            match.group("upstream_times"), match.group("request_time")
        )
        key = (match.group("pool"), match.group("uri_class"))
        buckets[key]["count"] += 1
        buckets[key]["upstream_seconds"] += float(seconds)
        newest = stamp if newest is None or stamp > newest else newest
        oldest = stamp if oldest is None or stamp < oldest else oldest
    if not buckets or newest is None or oldest is None:
        fail("access log contains no requests inside the window")
    return buckets, oldest, newest


def read_readiness(path: Path) -> dict[str, Any]:
    """Load the ready-container envelope gray-monitor-loop.sh already writes.

    Same document metrics.py's readiness leg reads, for the same reason: ready
    CONTAINERS counted from `.status.containerStatuses[*].ready`, never replicas
    and never a Deployment's `Available` condition.  Both of those are spec-side
    numbers that read healthy for a lane with nothing running.
    """
    raw = read_regular(path, "readiness")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        fail("readiness evidence is invalid JSON")
    if (
        not isinstance(envelope, dict)
        or set(envelope) != SOURCE_KEYS
        or envelope.get("schema_version") != 1
        or not isinstance(envelope.get("data"), dict)
    ):
        fail("readiness evidence has invalid structure")
    if envelope.get("payload_sha256") != digest(envelope["data"]):
        fail("readiness evidence checksum is invalid")
    captured_at = parse_timestamp(str(envelope.get("captured_at", "")))
    if captured_at is None:
        fail("readiness evidence timestamp is invalid")
    if captured_at - dt.datetime.now(dt.timezone.utc) > MAX_CLOCK_SKEW:
        fail("readiness evidence timestamp is in the future")
    data = envelope["data"]
    if set(data) != READINESS_KEYS:
        fail("readiness document has unexpected fields")
    for key in ("ready_containers", "expected_containers"):
        value = data[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            fail(f"readiness {key} must be a non-negative integer")
    observed_at = parse_timestamp(str(data["observed_at"]))
    if observed_at is None:
        fail("readiness observed_at is invalid")
    return {
        "ready_containers": int(data["ready_containers"]),
        "expected_containers": int(data["expected_containers"]),
        "observed_at": observed_at,
        "captured_at": captured_at,
    }


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    now = dt.datetime.now(dt.timezone.utc)
    window = dt.timedelta(seconds=args.window_seconds)
    buckets, oldest, newest = read_access_log(args.access_log, window)
    readiness = read_readiness(args.readiness)

    # The denominator is the span the requests actually cover, not the span that
    # was asked for.  A window that was requested as 30 minutes but holds 4
    # minutes of traffic is a 4-minute measurement, and dividing by 30 would
    # report a seventh of the real arrival rate -- the single easiest way to make
    # this gate green.  Using the observed span also errs high (it misses one
    # inter-arrival gap at each end), which is the safe direction here.
    span_seconds = (newest - oldest).total_seconds()

    errors: list[str] = []
    if span_seconds < MIN_WINDOW_SECONDS:
        errors.append("WINDOW_TOO_SHORT")
    if span_seconds > MAX_WINDOW_SECONDS:
        errors.append("WINDOW_TOO_LONG")
    readiness_age = (now - readiness["observed_at"]).total_seconds()
    if readiness_age > MAX_READINESS_AGE_SECONDS:
        errors.append("READINESS_STALE")
    ready = readiness["ready_containers"]
    if ready == 0:
        errors.append("ZERO_READY_CONTAINERS")
    if ready < readiness["expected_containers"]:
        # A degraded lane is not a lane to ramp onto, and this is not merely
        # conservative bookkeeping: dividing by the smaller count does raise the
        # per-container number, but the projection would still be describing a
        # fleet that does not exist yet.  Same code metrics.py uses, same reason.
        errors.append("READY_CONTAINERS_SHORT")

    # Per-class demand across EVERY pool, because the split routes on the caller's
    # key and not on the class: at 50% each class hands the gray lane half of
    # whatever it is carrying in total.  `guarded-old` is included, which
    # overstates demand slightly when the bridge is up -- the safe direction.
    demand: dict[str, dict[str, float]] = defaultdict(
        lambda: {"count": 0.0, "upstream_seconds": 0.0}
    )
    per_pool: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"count": 0.0, "upstream_seconds": 0.0})
    )
    for (pool, uri_class), values in buckets.items():
        if uri_class not in INFERENCE_CLASSES:
            continue
        demand[uri_class]["count"] += values["count"]
        demand[uri_class]["upstream_seconds"] += values["upstream_seconds"]
        bucket = per_pool[pool][uri_class]
        bucket["count"] += values["count"]
        bucket["upstream_seconds"] += values["upstream_seconds"]

    if not demand:
        errors.append("NO_INFERENCE_TRAFFIC")

    gray = per_pool.get("canary", {})
    stable = per_pool.get("stable", {})
    imputed = frozenset(args.impute_class_cost_from_stable)
    classes: list[dict[str, Any]] = []
    unpriced: list[str] = []
    thin: list[str] = []
    projected_concurrency = 0.0
    for uri_class in sorted(demand):
        total = demand[uri_class]
        gray_item = gray.get(uri_class, {"count": 0.0, "upstream_seconds": 0.0})
        stable_item = stable.get(uri_class, {"count": 0.0, "upstream_seconds": 0.0})
        gray_cost = (
            gray_item["upstream_seconds"] / gray_item["count"]
            if gray_item["count"]
            else None
        )
        stable_cost = (
            stable_item["upstream_seconds"] / stable_item["count"]
            if stable_item["count"]
            else None
        )
        enough = gray_item["count"] >= MIN_CLASS_SAMPLES
        source = "gray"
        cost = gray_cost
        if not enough:
            thin.append(uri_class)
            if uri_class in imputed and stable_cost is not None:
                # Imputation can only ever RAISE the cost.  A class priced off the
                # old build is an assumption about the new one; letting that
                # assumption make the projection cheaper would turn an exemption
                # into a discount.
                source = "stable_imputed"
                cost = stable_cost if gray_cost is None else max(gray_cost, stable_cost)
            else:
                source = "unpriced"
                cost = None
                unpriced.append(uri_class)
        rate = total["count"] / span_seconds
        share = args.target_split / 100.0
        class_concurrency = None if cost is None else rate * share * cost
        if class_concurrency is not None:
            projected_concurrency += class_concurrency
        classes.append(
            {
                "uri_class": uri_class,
                "total_requests": int(total["count"]),
                "total_upstream_seconds": rounded(total["upstream_seconds"]),
                "requests_per_second": rounded(rate),
                "gray_requests": int(gray_item["count"]),
                "gray_seconds_per_request": rounded(gray_cost),
                "stable_seconds_per_request": rounded(stable_cost),
                "cost_source": source,
                "seconds_per_request": rounded(cost),
                "projected_concurrency": rounded(class_concurrency),
            }
        )

    if unpriced:
        # Fail closed, and say which classes.  A class with real demand and no
        # measured cost on THIS build cannot be projected, and treating it as free
        # is how a capacity gate reports infinite headroom for a lane nobody
        # measured.  The escape hatch is named on the command line
        # (--impute-class-cost-from-stable) and recorded in the evidence, the same
        # shape collect-metrics.py's --baseline-exempt-class uses.
        errors.append("UNPRICED_CLASS")

    per_container = (projected_concurrency / ready) if ready else None
    ceiling = args.per_container_concurrency
    usable = ceiling * (1.0 - args.headroom_fraction)
    utilisation = (per_container / ceiling) if per_container is not None else None

    # What the lane is ALREADY sustaining per container, right now, at the current
    # split.  Not part of the verdict's arithmetic -- it is the sanity check on the
    # ceiling itself.  A ceiling below a number the lane has demonstrably been
    # carrying is a wrong input, not a verdict about the lane, and it must not be
    # allowed to produce a red that reads like a real capacity finding.
    gray_seconds_now = sum(item["upstream_seconds"] for item in gray.values())
    demonstrated = (gray_seconds_now / span_seconds / ready) if ready else None
    if demonstrated is not None and demonstrated > ceiling:
        errors.append("CEILING_BELOW_DEMONSTRATED")

    if per_container is not None and per_container > usable:
        errors.append("INSUFFICIENT_CAPACITY")

    # The largest split this measurement supports, as a percentage, reported
    # whether the gate passes or not so the run sheet has a number to aim at
    # instead of bisecting by trial ramps.  `projected_concurrency` is the demand
    # at `target_split`, so dividing by that share recovers the 100% demand.
    #
    # ⚠️ If any class is unpriced this number is OPTIMISTIC -- the missing class
    # contributes nothing to the denominator.  It is emitted anyway because the
    # verdict is already FAIL in that case, and a suppressed number invites the
    # reader to compute a worse one by hand.
    max_split = None
    if projected_concurrency > 0 and ready:
        demand_at_full = projected_concurrency / (args.target_split / 100.0)
        max_split = rounded(min(100.0, 100.0 * usable * ready / demand_at_full))

    result: dict[str, Any] = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "gate": GATE,
        "status": "FAIL" if errors else "PASS",
        "run_id": args.run_id,
        "generation": args.generation,
        "config_checksum": args.config_checksum,
        "captured_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "access_log": str(args.access_log),
        "readiness": str(args.readiness),
        "ruler": "upstream_seconds_per_second",
        "window_started_at": oldest.isoformat().replace("+00:00", "Z"),
        "window_ended_at": newest.isoformat().replace("+00:00", "Z"),
        "window_seconds": rounded(span_seconds),
        "requested_window_seconds": args.window_seconds,
        "target_split": args.target_split,
        "ready_containers": ready,
        "expected_containers": readiness["expected_containers"],
        "readiness_age_seconds": int(readiness_age),
        "classes": classes,
        "thin_classes": sorted(thin),
        "unpriced_classes": sorted(unpriced),
        "imputed_classes": sorted(imputed),
        "projected_concurrency": rounded(projected_concurrency),
        "projected_concurrency_per_container": rounded(per_container),
        "demonstrated_concurrency_per_container": rounded(demonstrated),
        "per_container_ceiling": ceiling,
        "headroom_fraction": args.headroom_fraction,
        "usable_per_container": rounded(usable),
        "utilisation": rounded(utilisation),
        "max_supportable_split": max_split,
        "errors": errors,
        # ⚠️ Written because a gate that only reports PASS/FAIL invites the reader
        # to believe more than was measured.  Each line is something this tool
        # ASSUMED rather than observed, and a PASS is only as good as these.
        "residual_risk": residual_risk(args, thin, imputed, span_seconds),
    }
    result["result_sha256"] = digest(
        {key: value for key, value in result.items() if key != "captured_at"}
    )
    return result, 1 if errors else 0


def residual_risk(
    args: argparse.Namespace,
    thin: list[str],
    imputed: frozenset[str],
    span_seconds: float,
) -> list[str]:
    notes = [
        f"per-container ceiling {args.per_container_concurrency} is a run-sheet input, "
        "not a measurement by this tool: nothing here drove a container to the point "
        "where it stops coping, so the verdict inherits whatever that number's own "
        "evidence is worth",
        f"headroom {args.headroom_fraction:.2f} is a margin against burstiness that is "
        "NOT measured here -- the projection is a MEAN concurrency over "
        f"{int(span_seconds)}s, and a lane sized at its mean queues on every peak",
        "demand is projected from the CURRENT class mix; a caller fleet that changes "
        "what it sends changes the answer without changing this evidence",
    ]
    if imputed:
        notes.append(
            "classes priced off the OLD build (--impute-class-cost-from-stable): "
            + ", ".join(sorted(imputed))
            + " -- if the new build is slower on them, this projection is low"
        )
    unmeasured = sorted(set(thin) - set(imputed))
    if unmeasured:
        notes.append(
            "classes with fewer than "
            f"{MIN_CLASS_SAMPLES} gray samples: " + ", ".join(unmeasured)
        )
    return notes


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
    parser.add_argument("--access-log", type=Path, required=True)
    parser.add_argument(
        "--readiness",
        type=Path,
        required=True,
        help=(
            "the ready-container envelope gray-monitor-loop.sh writes to "
            "raw/readiness.json; ready CONTAINERS, never replicas"
        ),
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--config-checksum", required=True)
    parser.add_argument(
        "--target-split",
        type=int,
        required=True,
        help="the split this evidence is for (the step about to be taken, e.g. 50)",
    )
    parser.add_argument(
        "--per-container-concurrency",
        type=float,
        required=True,
        help=(
            "upstream-seconds per second one ready container can sustain, i.e. its "
            "mean in-flight request ceiling. Run-sheet value, no default: this tool "
            "cannot measure it, and a default would be a hard-coded fleet constant "
            "that never goes red when it stops matching"
        ),
    )
    parser.add_argument(
        "--headroom-fraction",
        type=float,
        default=0.30,
        help=(
            "fraction of the ceiling left unused as burst margin (default 0.30). The "
            "projection is a MEAN over the window; traffic is not flat, and a lane "
            "sized at its mean queues on every peak"
        ),
    )
    parser.add_argument(
        "--window-seconds",
        type=int,
        default=1800,
        help=(
            f"how far back to read the access log (default 1800, min {MIN_WINDOW_SECONDS}, "
            f"max {MAX_WINDOW_SECONDS}). The verdict divides by the span the requests "
            "actually cover, not by this number"
        ),
    )
    parser.add_argument(
        "--impute-class-cost-from-stable",
        action="append",
        default=[],
        metavar="URI_CLASS",
        help=(
            "price this class off the STABLE pool when the gray pool has fewer than "
            f"{MIN_CLASS_SAMPLES} samples. For classes that cannot reach the floor at "
            "any window length (`messages` was dark in 80/80 windows). Recorded in the "
            "evidence, and can only raise the projected cost, never lower it"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if not STATE_ID_RE.fullmatch(args.run_id) or not STATE_ID_RE.fullmatch(args.generation):
        fail("run-id or generation is invalid")
    if not CONFIG_CHECKSUM_RE.fullmatch(args.config_checksum):
        fail("config-checksum is invalid")
    if not 1 <= args.target_split <= 100:
        fail("target-split must be between 1 and 100")
    if not args.per_container_concurrency > 0:
        fail("per-container-concurrency must be positive")
    if not 0.0 <= args.headroom_fraction < 1.0:
        fail("headroom-fraction must be in [0, 1)")
    if args.window_seconds < MIN_WINDOW_SECONDS:
        fail(f"window-seconds must be at least {MIN_WINDOW_SECONDS}")
    if args.window_seconds > MAX_WINDOW_SECONDS:
        fail(f"window-seconds must not exceed {MAX_WINDOW_SECONDS}")
    unknown = sorted(set(args.impute_class_cost_from_stable) - INFERENCE_CLASSES)
    if unknown:
        # A typo here would silently leave the class unpriced -- which fails
        # closed, but with an error naming the class rather than the flag, and the
        # operator would re-supply the same typo.
        fail("impute-class-cost-from-stable names non-inference classes: " + ",".join(unknown))

    result, code = run(args)
    if args.output is not None:
        secure_write(args.output, result)
    json.dump(result, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
