#!/usr/bin/env python3
"""Turn a PostgreSQL statement log into the three scheduler `observations` counts.

`prepare-values.py` will not emit a gray values file unless
`duplicate_scheduler_runs`, `duplicate_background_jobs` and
`unexpected_control_writes` are all 0. This tool is the only sanctioned way to
produce those three numbers, and the reason it exists is stated in
`docs/scheduler-suppression-evidence-2026-09-14.md`:

    没量过的 0 比没有证据更糟，它读起来是干净的绿。

So the whole design here is about making a 0 *expensive*. A run that cannot
prove its ruler was working, that cannot prove the gray leg's scheduler was
actually alive, and that cannot prove there was something for a scheduler to
write, is rejected outright rather than allowed to report three zeros.

Input is the raw container log of the clone's postgres, captured while it ran
with:

    log_statement       = 'all'
    log_line_prefix     = '%m [%p] app=%a host=%h db=%d '

`'all'`, not the `'mod'` the plan originally wrote down: `'mod'` logs only
DML/DDL, and two of the scheduled jobs whose liveness is load-bearing here
(`add_deployment_job`, `get_credentials_job`) are read-only. Under `'mod'` a
dead gray proxy and a suppressed gray proxy look identical.

Attribution is by `%h` (client pod IP) alone. Measured on clone C 2026-09-14:
Prisma sets no `application_name`, so every line reads `app=[unknown]` and `%a`
carries no information at all.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, NoReturn

TOOL = "measure-scheduler-observations"
SCHEMA_VERSION = 1

# A postgres log entry under the prefix above. Continuation lines of a multi-line
# statement carry no prefix at all and are folded into the entry above them.
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) UTC "
    r"\[(?P<pid>\d+)\] app=(?P<app>\S*) host=(?P<host>\S*) db=(?P<db>\S*) "
    r"(?P<level>[A-Z]+):  (?P<payload>.*)$"
)
# Simple query protocol logs `statement: <sql>`; Prisma uses the extended
# protocol, which logs `execute sN: <sql>`. Both carry the full SQL text.
PAYLOAD_RE = re.compile(r"^(?:statement|execute [^:]+): (?P<sql>.*)$", re.DOTALL)

# The proxy is still draining its own startup writes for a couple of seconds
# after the runner reports the hold as started. Measured on clone C 2026-09-14:
# the old leg's hold began at 01:44:09.929 and it issued an application write at
# 01:44:12.144, 2.2s inside its own hold. 60s is that gap with two orders of
# magnitude of slack, and it costs nothing: the shortest job cycle under test
# reschedules every 597-605s.
DEFAULT_SETTLE_SECONDS = 60

# Scheduled jobs that MUTATE shared state. These are what the suppression is for
# and the only ones the three counters count. Each pattern was written against
# real captured statement text, not against the LiteLLM source.
CONTROL_JOB_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # reset_budget_job. Captured verbatim on clone C 2026-09-14 01:54:00.149:
    #   UPDATE "public"."LiteLLM_VerificationToken" SET "spend" = $1,
    #   "budget_reset_at" = $2, "updated_at" = $3 WHERE (... = $4 AND 1=1) RETURNING ...
    # The assignment-shape is what separates it from the spend INCREMENT below,
    # which is an application write and reads `"spend" = ("public"....)`.
    (
        "reset_budget_job",
        re.compile(
            r'UPDATE "public"\."LiteLLM_(?:VerificationToken|UserTable|TeamTable)" '
            r'SET "spend" = \$\d+, "budget_reset_at" = \$\d+'
        ),
    ),
    (
        "key_rotation_job",
        re.compile(r'UPDATE "public"\."LiteLLM_VerificationToken" SET [^\n]*"key_rotation_at"'),
    ),
    (
        "expired_session_cleanup_job",
        re.compile(r'DELETE FROM "public"\."LiteLLM_VerificationToken"'),
    ),
    (
        "batch_cost_job",
        re.compile(r'UPDATE "public"\."LiteLLM_ManagedObjectTable"'),
    ),
    (
        "spend_update_job",
        re.compile(r'INSERT INTO "public"\."LiteLLM_(?:SpendLogs|DailyTagSpend)"'),
    ),
)

# Read-only scheduled pollers. Deliberately NOT counted -- they run on every
# release by design and suppressing them was never the goal. They are parsed
# because they are the liveness proof for the gray leg's scheduler: without
# them, "gray wrote nothing" cannot be told apart from "gray was not running".
READONLY_JOB_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("add_deployment_job", re.compile(r'SELECT [^\n]*"public"\."LiteLLM_ProxyModelTable"')),
    ("get_credentials_job", re.compile(r'SELECT [^\n]*"public"\."LiteLLM_CredentialsTable"')),
)

WRITE_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|MERGE)\b", re.IGNORECASE)
# Two executions of the same job on the same leg closer together than this are
# one run: a single reset_budget pass issues several statements back to back.
RUN_COALESCE_SECONDS = 30.0
# Two runs of the same job on DIFFERENT legs within one job period are the same
# scheduled tick arriving twice. reset_budget_job reschedules every 597-605s.
DUPLICATE_WINDOW_SECONDS = 605.0


def fail(message: str) -> NoReturn:
    raise SystemExit(f"{TOOL}: {message}")


def parse_pg_timestamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f")


def read_leg(path: Path, role: str) -> dict[str, Any]:
    """Load one leg's runner result and take the window bounds from IT, not from us.

    `hold.started_at` / `hold.ended_at` / `hold.client_addr` were all read out of
    the database's own clock and the database's own view of the connection, by
    the process being measured. Recomputing any of them here would introduce a
    skew between the bounds and the log lines they bound.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"cannot read {role} result {path}: {exc}")
    if payload.get("tool") != "compatibility-runner":
        fail(f"{role} result {path} is not a compatibility-runner result")
    hold = payload.get("hold")
    if not isinstance(hold, dict):
        fail(
            f"{role} result {path} has no hold section -- it was run without "
            "--hold-seconds and cannot carry scheduler evidence"
        )
    addr = str(hold.get("client_addr", "")).split("/")[0]
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", addr):
        fail(f"{role} result {path} has no usable client_addr for %h attribution")
    return {
        "role": role,
        "mode": payload.get("mode"),
        "addr": addr,
        "started_at": parse_pg_timestamp(str(hold["started_at"])),
        "ended_at": parse_pg_timestamp(str(hold["ended_at"])),
        "hold_seconds": hold.get("seconds"),
        "image_digest": (payload.get("binding") or {}).get("image_digest"),
        "scheduler_knobs": payload.get("scheduler_knobs") or {},
        "result_path": str(path),
    }


def suppressed(knobs: dict[str, Any]) -> bool:
    """Whether this leg attested that it ran with the suppression applied.

    Read off the leg's OWN self-attestation rather than off the rendered Job, so
    the analysis can never disagree with what the process actually loaded.
    """
    env = knobs.get("env") or {}
    pinned = all(str(env.get(name)).lower() == "false" for name in sorted(env)) and bool(env)
    return bool(knobs.get("disable_reset_budget")) and pinned


def iter_entries(path: Path) -> Iterator[tuple[datetime, str, str, str]]:
    """Yield (ts, host, level, payload) per log ENTRY, folding continuation lines."""
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        fail(f"cannot read log {path}: {exc}")
    current: list[Any] | None = None
    with handle:
        for raw in handle:
            line = raw.rstrip("\n")
            match = LINE_RE.match(line)
            if match:
                if current is not None:
                    yield (current[0], current[1], current[2], "\n".join(current[3]))
                current = [
                    parse_pg_timestamp(match.group("ts")),
                    match.group("host"),
                    match.group("level"),
                    [match.group("payload")],
                ]
            elif current is not None:
                current[3].append(line)
    if current is not None:
        yield (current[0], current[1], current[2], "\n".join(current[3]))


def classify(sql: str) -> tuple[str | None, str | None]:
    for name, pattern in CONTROL_JOB_PATTERNS:
        if pattern.search(sql):
            return name, None
    for name, pattern in READONLY_JOB_PATTERNS:
        if pattern.search(sql):
            return None, name
    return None, None


def coalesce(stamps: list[datetime]) -> list[datetime]:
    runs: list[datetime] = []
    for stamp in sorted(stamps):
        if not runs or (stamp - runs[-1]).total_seconds() > RUN_COALESCE_SECONDS:
            runs.append(stamp)
    return runs


def measure(log: Path, gray: dict[str, Any], prod: dict[str, Any], settle: int) -> dict[str, Any]:
    start = max(gray["started_at"], prod["started_at"]) + timedelta(seconds=settle)
    end = min(gray["ended_at"], prod["ended_at"])
    if end <= start:
        fail("the two legs' holds do not overlap once the settle offset is applied")
    if gray["addr"] == prod["addr"]:
        fail(f"both legs report the same client address {gray['addr']}; %h cannot attribute")

    by_addr = {gray["addr"]: "gray", prod["addr"]: "prod"}
    control: dict[str, dict[str, list[datetime]]] = {}
    readonly: dict[str, dict[str, list[datetime]]] = {}
    writes: dict[str, list[dict[str, str]]] = {"gray": [], "prod": []}
    seen_lines = {"gray": 0, "prod": 0}

    for stamp, host, level, payload in iter_entries(log):
        role = by_addr.get(host)
        if role is None or level != "LOG" or not (start <= stamp <= end):
            continue
        seen_lines[role] += 1
        body = PAYLOAD_RE.match(payload)
        if body is None:
            continue
        sql = body.group("sql")
        control_job, readonly_job = classify(sql)
        if control_job:
            control.setdefault(control_job, {}).setdefault(role, []).append(stamp)
        if readonly_job:
            readonly.setdefault(readonly_job, {}).setdefault(role, []).append(stamp)
        if WRITE_RE.match(sql):
            writes[role].append(
                {
                    "at": stamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    "job": control_job or "unclassified",
                    "sql": " ".join(sql.split())[:200],
                }
            )

    duplicate_runs = 0
    duplicate_jobs: list[str] = []
    per_job: dict[str, Any] = {}
    for job, legs in sorted(control.items()):
        gray_runs = coalesce(legs.get("gray", []))
        prod_runs = coalesce(legs.get("prod", []))
        overlapping = [
            g
            for g in gray_runs
            if any(abs((g - p).total_seconds()) <= DUPLICATE_WINDOW_SECONDS for p in prod_runs)
        ]
        if overlapping:
            duplicate_jobs.append(job)
        duplicate_runs += len(overlapping)
        per_job[job] = {
            "gray_runs": len(gray_runs),
            "prod_runs": len(prod_runs),
            "gray_runs_duplicating_prod": len(overlapping),
        }

    readonly_summary = {
        job: {role: len(coalesce(stamps)) for role, stamps in sorted(legs.items())}
        for job, legs in sorted(readonly.items())
    }
    gray_readonly = sum(v.get("gray", 0) for v in readonly_summary.values())
    prod_control_runs = sum(entry["prod_runs"] for entry in per_job.values())

    return {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "window": {
            "start": start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "end": end.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "seconds": round((end - start).total_seconds(), 3),
            "settle_seconds": settle,
        },
        "legs": {
            role: {
                "mode": leg["mode"],
                "client_addr": leg["addr"],
                "image_digest": leg["image_digest"],
                "suppressed": suppressed(leg["scheduler_knobs"]),
                "scheduler_knobs": leg["scheduler_knobs"],
                "log_lines_in_window": seen_lines[role],
            }
            for role, leg in (("gray", gray), ("prod", prod))
        },
        "observations": {
            "duplicate_scheduler_runs": duplicate_runs,
            "duplicate_background_jobs": len(duplicate_jobs),
            "unexpected_control_writes": len(writes["gray"]),
        },
        "duplicate_background_job_names": sorted(duplicate_jobs),
        "control_jobs": per_job,
        "readonly_pollers": readonly_summary,
        "gray_writes": writes["gray"][:50],
        "prod_writes": writes["prod"][:50],
        "liveness": {
            "ruler_saw_gray_lines": seen_lines["gray"],
            "ruler_saw_prod_lines": seen_lines["prod"],
            "gray_scheduler_readonly_runs": gray_readonly,
            "prod_control_job_runs": prod_control_runs,
        },
    }


def check_liveness(report: dict[str, Any]) -> list[str]:
    """Everything that has to be true before any of the three counts means anything."""
    live = report["liveness"]
    problems: list[str] = []
    if live["ruler_saw_gray_lines"] == 0:
        problems.append(
            "the ruler captured no lines at all from the gray leg's address -- "
            "every count below is a blind 0, not a measurement"
        )
    if live["ruler_saw_prod_lines"] == 0:
        problems.append("the ruler captured no lines at all from the prod leg's address")
    if live["gray_scheduler_readonly_runs"] == 0:
        problems.append(
            "the gray leg ran no read-only scheduled poller in the window -- "
            "'gray wrote nothing' cannot be told apart from 'gray was not running'"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True, help="captured postgres container log")
    parser.add_argument("--gray-result", type=Path, required=True)
    parser.add_argument("--prod-result", type=Path, required=True)
    parser.add_argument("--settle-seconds", type=int, default=DEFAULT_SETTLE_SECONDS)
    parser.add_argument(
        "--expect",
        choices=("duplicates", "none"),
        required=True,
        help=(
            "duplicates = positive control, the run FAILS unless the ruler saw the "
            "gray leg duplicating prod; none = the real measurement"
        ),
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    if args.settle_seconds < 0:
        fail("--settle-seconds must not be negative")
    gray = read_leg(args.gray_result, "gray")
    prod = read_leg(args.prod_result, "prod")
    report = measure(args.log, gray, prod, args.settle_seconds)

    problems = check_liveness(report)
    observations = report["observations"]
    legs = report["legs"]

    if args.expect == "duplicates":
        # Positive control. Its whole job is to prove the ruler can see the thing
        # it will later be asked to report as absent, so a clean 0 here is a
        # FAILURE -- it means the ruler is blind and the real run's 0 would be
        # worthless.
        if legs["gray"]["suppressed"]:
            problems.append(
                "--expect duplicates but the gray leg attests it ran suppressed; "
                "a positive control has to run with the suppression OFF"
            )
        for name in ("duplicate_scheduler_runs", "duplicate_background_jobs"):
            if observations[name] == 0:
                problems.append(
                    f"positive control saw {name}=0: the ruler cannot see duplication, "
                    "so it cannot testify to its absence either"
                )
        if observations["unexpected_control_writes"] == 0:
            problems.append(
                "positive control saw unexpected_control_writes=0: the ruler cannot "
                "see a control write, so a 0 in the real run would be blind"
            )
    else:
        if not legs["gray"]["suppressed"]:
            problems.append(
                "--expect none but the gray leg does not attest full suppression; "
                "this run measures the wrong thing"
            )
        if legs["prod"]["suppressed"]:
            problems.append(
                "the prod leg attests suppression too -- with no leg owning the "
                "scheduler, zero duplication is trivially true and proves nothing"
            )
        if report["liveness"]["prod_control_job_runs"] == 0:
            problems.append(
                "the prod leg ran no control job in the window, so there was nothing "
                "for the gray leg to duplicate; gray's 0 is untested"
            )
        for name, value in sorted(observations.items()):
            if value:
                problems.append(f"{name}={value}: suppression did not hold")

    report["status"] = "FAIL" if problems else "PASS"
    report["problems"] = problems
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
