#!/usr/bin/env python3
"""Negative control for the gray gate: replay real traffic and count how often it goes red.

WHAT THIS ANSWERS

A gate leg that fires on a healthy system is worse than no leg, because it
trains everyone to ignore the one window where it was right.  The only way to
know a leg's idle false-positive rate is to run it against traffic that is known
NOT to contain the fault it looks for, and count.  That is what this does:

  * Split the real nginx access log into consecutive 5-minute windows.
  * Label ONE pool `canary` and the SAME pool `stable`, so the two cohorts are
    the same requests -- there is no version difference to find.  Any red is a
    false positive by construction.
  * Feed each window to the real metrics.py through the real sustain-state
    carry, so depth gating is exercised rather than bypassed.
  * Print, per leg, how many windows went red.

⛔ This is a NEGATIVE control.  Everything green proves only that the legs are
quiet; it does NOT prove they can fire.  A synthetic green is as untrusted as a
synthetic red.  `--inject` runs the positive control on the same windows (see
below) and both must be run before believing either.

⛔ The baseline MUST be the frozen production file (`/root/l3/base/baseline-*.json`).
An earlier replay invented `five_xx_rate: 0.001` and made FIVE_XX_DELTA go red on
a window whose real rate was 1.3% -- which was read as a defect in the new leg
for a while.  There is no default and no synthesis here: no `--baseline`, no run.

WHAT IT DOES NOT TOUCH

Read-only.  It reads an access log and a baseline, writes nothing outside
`--work-dir` (created 0700, contents 0600), and never calls kubectl, nginx, or
any gray-route script.  There is nothing to roll back.

WINDOWS AND THE CLOCK

metrics.py refuses evidence older than MAX_EVIDENCE_AGE (10 min), so a replay of
historical windows cannot present the real timestamps in the envelope: every
window would be rejected as EVIDENCE_STALE and the run would "pass" with zero
windows judged.  Each window's envelope is therefore stamped `now`, while the
per-source `observed_at` values keep their RELATIVE spacing from the window.
That is exactly what the data-liveness leg reads (it anchors on the newest
observation, not on wall clock), so relabelling the absolute time does not
change that leg's verdict.  The count of judged windows is printed so a run that
silently judged nothing cannot be read as a clean one.

WHICH LEGS THIS ACTUALLY CONTROLS

⚠️ Three of the four legs get both controls here; DATA_SOURCE_SILENT gets only
the negative one, and its zero must not be read as measured:

  USER_FAILURE_RATE       negative: replay as-is.  positive: --inject.
  FIVE_XX_ABSOLUTE        negative: replay as-is.  positive: --inject >= 0.10.
  READY_CONTAINERS_SHORT  negative: --ready == --expected-ready.
                          positive: --ready one below it (fires on window 1, no
                          sustain, which is the point of that leg).
  DATA_SOURCE_SILENT      negative only.  Every window here is built with a
                          healthy lag spread, so this leg reads 0 because it was
                          never shown a stale source -- NOT because it was
                          exercised and stayed quiet.  There is deliberately no
                          --stale-source flag: a lag this harness synthesises is
                          a fixture, and the fixture that belongs to that leg
                          lives in the unit tests next to the anchoring rule it
                          has to get right (the anchor is the NEWEST observation,
                          not evidence.captured_at, which is a min() and would
                          hand a dead source age 0).

2026-09-20 run, 185 windows / 15.4h of real stable traffic:
  negative            hard_triggers 0    (was 3 of 6 before the leg-4 fix)
  --inject 0.03       hard_triggers 180  USER_FAILURE_RATE only
  --inject 0.20       hard_triggers 183  both 5xx legs, 2 windows short of 185
                      because the sustain gate is filling to depth 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
METRICS = HERE / "metrics.py"

# Must match collect-metrics.py's LOG_LINE_RE in the fields it reads.  Kept
# separate rather than imported because that module is a CLI that reads a live
# log and refuses a stale window -- exactly the two behaviours a replay needs to
# not have.  The fields are asserted against the collector's regex by
# test_replay_harness_reads_the_same_log_fields_as_the_collector.
LOG_LINE_RE = re.compile(
    r"^ts=(?P<ts>\S+)\s+\S+\s+(?P<status>[1-5][0-9]{2})\s+"
    r"(?P<request_time>[0-9]+(?:\.[0-9]+)?)\s+"
    r".*?\bpool=(?P<pool>stable|canary|guarded-old)\b.*?"
    r"\brt=(?P<rt>.*?)\s+"
    r"\buri_class=(?P<uri_class>[A-Za-z0-9._:-]+)\b"
)
SECRET_RE = re.compile(
    r"(?:authorization|x-api-key)\s*[:=]|\bbearer\s+\S+|\bsk-[A-Za-z0-9._~-]{8,}",
    re.I,
)
# `other` is health checks and probes: ~90% of stable's rows, none of it a user,
# and none of it touching a provider.  Mixing it in diluted an earlier burst
# measurement from 89.47% to 64.44%.  Excluded here for the same reason it is
# excluded in metrics.py, so the replay measures the same population the gate does.
EXCLUDED_CLASSES = {"other"}
WINDOW = timedelta(minutes=5)

# Legs under test.  Anything reported outside this set is printed separately
# rather than silently folded in -- a new reason code appearing in a replay is
# information, and a harness that only counts codes it already knows about would
# hide it.
LEGS = (
    "FIVE_XX_ABSOLUTE",
    "READY_CONTAINERS_SHORT",
    "DATA_SOURCE_SILENT",
    "USER_FAILURE_RATE",
    "FIVE_XX_DELTA",
)


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"replay-gate-negative-control: {message}", file=sys.stderr)
    raise SystemExit(2)


def parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def upstream_response_time(rt: str, request_time: str) -> float:
    """Sum the upstream hops, falling back to nginx's own request_time.

    Same rule as the collector: `rt` can be `-`, a single number, or a
    comma/colon-separated chain when nginx retried upstreams.
    """
    total = 0.0
    seen = False
    for piece in re.split(r"[,:]", rt):
        piece = piece.strip()
        if not piece or piece == "-":
            continue
        try:
            total += float(piece)
            seen = True
        except ValueError:
            continue
    if seen:
        return total
    try:
        return float(request_time)
    except ValueError:
        return 0.0


def read_log(path: Path, source_pool: str) -> list[tuple[datetime, dict[str, Any]]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if SECRET_RE.search(raw):
        fail(
            f"{path} contains secret-bearing data -- refusing to read it into a "
            "replay artifact"
        )
    rows: list[tuple[datetime, dict[str, Any]]] = []
    skipped = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        match = LOG_LINE_RE.search(line)
        if match is None:
            skipped += 1
            continue
        moment = parse_timestamp(match.group("ts"))
        if moment is None:
            skipped += 1
            continue
        if match.group("pool") != source_pool:
            continue
        uri_class = match.group("uri_class")
        if uri_class in EXCLUDED_CLASSES:
            continue
        rows.append(
            (
                moment,
                {
                    "uri_class": uri_class,
                    "status": int(match.group("status")),
                    "response_time": upstream_response_time(
                        match.group("rt"), match.group("request_time")
                    ),
                },
            )
        )
    # Printed, not swallowed.  A replay whose extractor silently stopped matching
    # would report zero reds on zero windows and look like the cleanest run yet --
    # this is the same failure shape as a path regex that truncates long prefixes
    # and manufactures a screen of false reds, read from the other side.
    if skipped:
        print(
            f"note: {skipped} line(s) did not match the log format and were skipped",
            file=sys.stderr,
        )
    if not rows:
        fail(
            f"no usable rows for pool={source_pool} in {path} -- check the pool label "
            "and that the log format still matches LOG_LINE_RE"
        )
    rows.sort(key=lambda item: item[0])
    return rows


def windows(
    rows: list[tuple[datetime, dict[str, Any]]], span: timedelta
) -> Iterator[tuple[datetime, list[dict[str, Any]]]]:
    """Consecutive fixed-width windows aligned on the first row.

    Fixed-width rather than "last N rows": the gate reads a time window, and a
    count-based slice would stretch a quiet period into a window that never
    existed on the wire.  Empty windows are skipped rather than emitted -- a
    window with no traffic has nothing to judge, and the gate treats it as
    CANARY_SAMPLE_MISSING, which is a different question from the one here.
    """
    if not rows:
        return
    # Single pass over sorted rows.  A re-scan per window is O(windows x rows),
    # which on a full day of this log is minutes of nothing -- and a harness slow
    # enough to be run on a narrow slice is a harness whose result was measured on
    # a narrower population than the one being cleared.
    index = 0
    start = rows[0][0]
    total = len(rows)
    while index < total:
        stop = start + span
        batch: list[dict[str, Any]] = []
        while index < total and rows[index][0] < stop:
            batch.append(rows[index][1])
            index += 1
        if batch:
            yield start, batch
        start = stop
        # Jump the clock forward over a gap in traffic rather than stepping
        # through it one empty window at a time.
        if index < total and rows[index][0] >= start + span:
            start = rows[index][0]


def inject(batch: list[dict[str, Any]], pp: float) -> list[dict[str, Any]]:
    """Positive control: turn `pp` of this window's canary rows into 502s.

    Applied to the canary cohort only, deterministically by position rather than
    randomly -- a random positive control that goes green once is indistinguishable
    from a leg that cannot fire, and re-running it with a different seed to "check"
    is how a synthetic green gets believed.
    """
    if pp <= 0:
        return batch
    every = max(1, round(1 / pp))
    return [
        dict(record, status=502) if position % every == 0 else record
        for position, record in enumerate(batch)
    ]


def digest(payload: dict[str, Any]) -> str:
    """Checksum over the same key set the collector signs.

    `evidence` and `sustain_state` are excluded because the first contains this
    value and the second is written by the previous cycle -- matching
    metrics.py's UNSIGNED_PAYLOAD_KEYS.  Getting this wrong does not produce a
    wrong verdict, it produces EVIDENCE_CHECKSUM_MISMATCH on every window, i.e. a
    run that judges nothing.
    """
    body = {
        key: value
        for key, value in payload.items()
        if key not in {"evidence", "sustain_state"}
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def build_payload(
    *,
    canary: list[dict[str, Any]],
    stable: list[dict[str, Any]],
    baseline: Any,
    ready: int,
    expected_ready: int,
    source_lag: dict[str, float],
    sustain_state: dict[str, int],
    run_id: str,
    generation: str,
    config_checksum: str,
    phase: str,
    split: int,
) -> dict[str, Any]:
    records = [dict(record, pool_label="canary") for record in canary]
    records += [dict(record, pool_label="stable") for record in stable]
    # Stamped `now`, not at the window's real time: metrics.py rejects evidence
    # older than MAX_EVIDENCE_AGE, so historical timestamps here would make every
    # window EVIDENCE_STALE and the run would report zero reds having judged zero
    # windows.  The liveness leg is unaffected because it anchors on the newest
    # per-source observation and reads only the SPACING between them, which
    # `source_lag` preserves.
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "phase": phase,
        "rollout_percent": split,
        "records": records,
        "hard_errors": {},
        "readiness": {
            "lane": "gray",
            "ready_containers": ready,
            "expected_containers": expected_ready,
            "observed_at": stamp(now - timedelta(seconds=source_lag.get("readiness", 0))),
        },
        "data_sources": [
            {"name": name, "observed_at": stamp(now - timedelta(seconds=lag))}
            for name, lag in source_lag.items()
        ],
    }
    # Above split 0 metrics.py refuses a cycle with no spend section at all
    # (SPEND_RECONCILIATION_MISSING), so a replay cannot omit it -- the first run
    # of this harness errored on every window and reported 0 reds, which is the
    # reassuring-looking nothing this script now exits non-zero for.
    #
    # ⚠️ This section is SYNTHETIC and deliberately clean: one expected id,
    # observed terminal, zero lag.  It is scaffolding to get the window judged,
    # NOT a measurement -- the spend leg is NOT under test here and this run says
    # nothing about its false-positive rate.  Its real input is
    # collect-spend-reconciliation.py against live LiteLLM writes, and its
    # calibration is the bimodal-flush measurement in metrics.py.  A reader who
    # takes this clean section as the spend leg being cleared has read a synthetic
    # green.
    # Both lanes healthy.  Required, not cosmetic: without it a promoted trigger
    # in `normal_gray` is downgraded to alert_only + PROD_NOT_HEALTHY_FOR_ROLLBACK
    # rather than becoming `rollback`, so a leg that fired on every window would
    # still show `traffic_moving_actions: 0`.  The first run of this harness did
    # exactly that -- the counter meant to measure false rollbacks was reading a
    # missing health section instead of a quiet gate.
    #
    # ⚠️ Synthetic and clean, like the spend section: scaffolding so the dispatch
    # path is the real one, not a measurement of backend health.
    payload["backend_health"] = {"gray": True, "prod": True, "bridge": True}
    payload["spend_reconciliation"] = {
        "expected_request_ids": ["replay-scaffold"],
        "terminal_request_ids": ["replay-scaffold"],
        "failed_request_ids": [],
        "pending_request_ids": [],
        "observed_lag_seconds": 0,
    }
    if baseline is not None:
        payload["baseline"] = baseline
    if sustain_state:
        payload["sustain_state"] = sustain_state
    payload["evidence"] = {
        "schema_version": 1,
        "captured_at": stamp(now),
        "payload_sha256": digest(payload),
        "source": "collect-metrics.py",
        "run_id": run_id,
        "generation": generation,
        "config_checksum": config_checksum,
    }
    return payload


def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(METRICS), "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if not result.stdout.strip():
        fail(f"metrics.py produced no output: {result.stderr.strip()[:400]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        fail(f"metrics.py output was not JSON: {result.stdout[:400]}")


def load_baseline(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8")
    if SECRET_RE.search(raw):
        fail(f"{path} contains secret-bearing data")
    document = json.loads(raw)
    # The frozen file is an envelope; metrics.py wants the class list inside it.
    # Accepted in either shape because the on-disk envelope has changed once, and
    # a harness that silently read `{}` out of the newer shape would compare every
    # window against nothing and call it clean.
    for key in ("baseline", "groups", "classes"):
        if isinstance(document, dict) and isinstance(document.get(key), (list, dict)):
            return document[key]
    if isinstance(document, (list, dict)):
        return document
    fail(f"{path} is not a baseline document")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay real nginx windows through the real metrics.py and count how "
            "often each gate leg goes red on traffic containing no version fault."
        )
    )
    parser.add_argument("--access-log", type=Path, required=True)
    parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help=(
            "the FROZEN production baseline (/root/l3/base/baseline-*.json). "
            "Required and never synthesised: an invented five_xx_rate made an "
            "earlier replay red on a healthy window and was nearly read as a "
            "defect in the leg under test."
        ),
    )
    parser.add_argument(
        "--source-pool",
        default="stable",
        choices=("stable", "canary", "guarded-old"),
        help=(
            "which pool's real rows to replay. Both cohorts are built from this one "
            "pool, which is what makes the run a negative control: the canary and "
            "the stable side are literally the same requests, so any red is false "
            "by construction (default: stable)"
        ),
    )
    parser.add_argument(
        "--expected-ready",
        type=int,
        required=True,
        help=(
            "ready containers the lane is supposed to have, from the run sheet. No "
            "default on purpose -- a literal here would be the same hard-coded "
            "fleet size that sat wrong in llm-stab-scrape-down for weeks"
        ),
    )
    parser.add_argument(
        "--ready",
        type=int,
        default=None,
        help="ready containers to report (default: --expected-ready, i.e. healthy)",
    )
    parser.add_argument(
        "--inject",
        type=float,
        default=0.0,
        metavar="PP",
        help=(
            "POSITIVE control: make this fraction of canary rows 502 (e.g. 0.20). "
            "A negative control alone cannot tell a quiet leg from a dead one, so "
            "run both"
        ),
    )
    parser.add_argument("--split", type=int, default=100)
    parser.add_argument("--phase", default="normal_gray")
    parser.add_argument("--run-id", default="replay-negative-control")
    parser.add_argument("--generation", default="replay")
    parser.add_argument("--config-checksum", default="test-mode")
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=int(WINDOW.total_seconds() // 60),
        help="window width (default 5, matching the live stop-loss window)",
    )
    parser.add_argument(
        "--max-windows",
        type=int,
        default=0,
        help="0 = all. Use only to smoke-test the harness; a partial run is not a control",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="write the per-window verdicts here (0600) for later inspection",
    )
    args = parser.parse_args()

    if args.window_minutes < 1:
        fail("--window-minutes must be >= 1")
    if args.expected_ready < 1:
        fail("--expected-ready must be >= 1")
    ready = args.expected_ready if args.ready is None else args.ready

    baseline = load_baseline(args.baseline)
    rows = read_log(args.access_log, args.source_pool)
    span = timedelta(minutes=args.window_minutes)

    # Both sides of the comparison get the same per-source lag, all inside the
    # floor.  The liveness leg is being measured for its idle false-positive rate
    # here, so it must be given the shape a healthy cycle has -- sources observed
    # a few seconds apart -- rather than a synthetic zero spread, which would be a
    # green measured on a window that never occurs in production.
    source_lag = {
        "access_log": 0.0,
        "hard_errors": 4.0,
        "backend_health": 9.0,
        "spend_reconciliation": 21.0,
        "readiness": 12.0,
    }

    red_windows: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    other_codes: Counter[str] = Counter()
    sustain_state: dict[str, int] = {}
    verdicts: list[dict[str, Any]] = []
    judged = 0
    errored = 0

    for start, batch in windows(rows, span):
        if args.max_windows and judged >= args.max_windows:
            break
        canary = inject(batch, args.inject)
        payload = build_payload(
            canary=canary,
            stable=batch,
            baseline=baseline,
            ready=ready,
            expected_ready=args.expected_ready,
            source_lag=source_lag,
            sustain_state=sustain_state,
            run_id=args.run_id,
            generation=args.generation,
            config_checksum=args.config_checksum,
            phase=args.phase,
            split=args.split,
        )
        output = evaluate(payload)
        judged += 1
        if output.get("status") == "ERROR":
            errored += 1
            for code in output.get("errors", []):
                other_codes[code] += 1
            continue
        # Carried forward exactly as the live loop does, so depth gating is real.
        # Reset it between windows and FIVE_XX_ABSOLUTE could never reach 4.
        sustain_state = dict(output.get("sustain", {}).get("counts", {}))
        recommendation = output.get("dispatcher_recommendation", {})
        actions[recommendation.get("action", "?")] += 1
        codes = set(recommendation.get("reason_codes", []))
        for code in codes:
            if code in LEGS:
                red_windows[code] += 1
            else:
                other_codes[code] += 1
        verdicts.append(
            {
                "window_start": stamp(start),
                "requests": len(batch),
                "action": recommendation.get("action"),
                "hard_trigger": bool(recommendation.get("hard_trigger")),
                "reason_codes": sorted(codes),
                "sustain": sustain_state,
                "user_failure_rate": output.get("user_facing_failures", {}).get(
                    "failure_rate"
                ),
                # Reported beside it because the two are different populations and
                # the whole point of the 2026-09-20 fix was that only the first
                # moves traffic.  A run where `client_four_xx_rate` is high and
                # `user_failure_rate` is 0 is the shape this harness was built to
                # produce; before the fix both were one number and the leg fired.
                "client_four_xx_rate": output.get("user_facing_failures", {}).get(
                    "client_four_xx_rate"
                ),
            }
        )

    rollbacks = sum(
        count for action, count in actions.items() if action in {"rollback", "abort_to_bridge"}
    )
    # A promoted trigger that could not dispatch still counts as a false positive.
    # `hard_trigger` is the leg's own verdict; the action is what the dispatcher
    # could do with it, and those come apart whenever a lane is unhealthy
    # (PROD_NOT_HEALTHY_FOR_ROLLBACK / GRAY_FAILURE_NOT_CONFIRMED).  Counting only
    # actions would let a gate that fires on every window report zero.
    hard_triggers = sum(1 for verdict in verdicts if verdict["hard_trigger"])
    report = {
        "tool": "replay-gate-negative-control",
        "control": "positive" if args.inject > 0 else "negative",
        "source_pool": args.source_pool,
        "inject_fraction": args.inject,
        "windows_judged": judged,
        "windows_errored": errored,
        # The denominator for every count below.  Kept separate from
        # windows_judged so a run where most windows errored cannot be read as a
        # clean sweep of them all.
        "windows_with_verdict": len(verdicts),
        "traffic_moving_actions": rollbacks,
        "hard_triggers": hard_triggers,
        "leg_red_windows": {leg: red_windows.get(leg, 0) for leg in LEGS},
        "actions": dict(sorted(actions.items())),
        "other_reason_codes": dict(sorted(other_codes.items())),
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    if args.json_out is not None:
        handle = os.open(str(args.json_out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(verdicts, stream, indent=2, sort_keys=True)

    # A run that judged nothing is the failure this exits non-zero for.  Zero reds
    # out of zero VERDICTS is the most reassuring-looking output this script can
    # produce and it means the harness is broken, not the gate quiet.  The first
    # run of this script errored on all three windows for a missing spend section
    # and printed a perfect row of zeros; `judged` counted those windows, so
    # checking `judged` alone was not enough -- the bar is windows that actually
    # reached a verdict.
    if judged == 0:
        fail("judged 0 windows -- nothing was measured")
    if not verdicts:
        fail(
            f"all {judged} window(s) errored before any leg was evaluated "
            f"({dict(other_codes)}) -- nothing was measured"
        )
    if errored:
        # Not fatal (a genuinely bad window in the middle of a long replay is
        # information), but it must not be silent: the denominator of every rate
        # printed above is `len(verdicts)`, not `judged`.
        print(
            f"warning: {errored} of {judged} window(s) errored and were not judged",
            file=sys.stderr,
        )
    if args.inject > 0:
        # Positive control: silence here means a leg cannot fire at all.
        return 0 if hard_triggers else 1
    # Judged on hard_triggers, not on dispatched actions -- see above.
    return 0 if hard_triggers == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
