"""Regression contracts for `measure-scheduler-observations.py`.

Every fixture under `fixtures/scheduler-observations/` is a verbatim subsample of
a real postgres log captured on clone C on 2026-09-14, paired with the fields
`compatibility-runner` really wrote for that run. Nothing here is synthesised.
That is the point: the project rule is 合成红与合成绿同样不可信, and a fixture I
invented would only test the parser against my own idea of the log format.

The two anchor fixtures reproduce, line for line, the two verdicts the tool
returned against the full logs on the day:

* `red-pilot` -- the first unsuppressed run. Both legs alive, both schedulers
  polling, but only ONE leg ever won the `reset_budget_job` row, so the ruler saw
  no duplication. Under `--expect duplicates` that is a FAILURE, because a ruler
  that cannot see duplication cannot testify to its absence either.
* `green-unsuppressed` -- the same run repeated with the probe row re-seeded
  overdue every 2s, which defeats the first-poller-wins race. Both legs write
  inside one cycle. This is the positive control §6.3 was blocked on.
* `green-suppressed` -- the real measurement. Same ruler, same re-seeder, gray
  running with all four suppressors on. Prod takes both `reset_budget_job`
  cycles; gray writes nothing while its scheduler stays visibly alive. These are
  the three zeros `prepare-values.py` gates on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "measure-scheduler-observations.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "scheduler-observations"


def measure(base: str, *, expect: str, extra: list[str] | None = None):
    command = [
        sys.executable,
        str(TOOL),
        "--log",
        str(FIXTURES / f"{base}.log"),
        "--gray-result",
        str(FIXTURES / f"{base}-gray.json"),
        "--prod-result",
        str(FIXTURES / f"{base}-prod.json"),
        "--expect",
        expect,
        *(extra or []),
    ]
    process = subprocess.run(command, capture_output=True, text=True, check=False)
    payload = json.loads(process.stdout) if process.stdout.strip() else None
    return process, payload


def test_positive_control_fixture_reproduces_the_measured_duplication():
    """The green fixture must still yield the three numbers clone C really gave.

    2/1/2 is not a round number somebody picked; it is what the run produced:
    gray wrote at 02:23:20.294 and 02:33:17.295, prod wrote at 02:33:22.430, and
    the second gray run lands 5.1s from prod's inside one 605s cycle.
    """
    process, report = measure("green-unsuppressed", expect="duplicates")
    assert process.returncode == 0, report
    assert report["status"] == "PASS"
    assert report["observations"] == {
        "duplicate_scheduler_runs": 2,
        "duplicate_background_jobs": 1,
        "unexpected_control_writes": 2,
    }
    assert report["duplicate_background_job_names"] == ["reset_budget_job"]


def test_positive_control_proves_the_ruler_can_see_each_leg_separately():
    _, report = measure("green-unsuppressed", expect="duplicates")
    live = report["liveness"]
    assert live["ruler_saw_gray_lines"] > 0
    assert live["ruler_saw_prod_lines"] > 0
    # Both legs' read-only pollers, which is how a suppressed-but-alive gray is
    # told apart from a gray that never started.
    assert live["gray_scheduler_readonly_runs"] > 0
    assert set(report["readonly_pollers"]) == {"add_deployment_job", "get_credentials_job"}
    for legs in report["readonly_pollers"].values():
        assert legs["gray"] > 0 and legs["prod"] > 0


def test_a_live_gray_that_simply_never_duplicated_is_still_a_failed_positive_control():
    """The real trap this tool exists to catch.

    In the pilot run nothing was broken: both proxies were up, both schedulers
    polled, the suppression was OFF on both. The ruler still reported three
    zeros -- because `reset_budget_job` is first-poller-wins and only one leg
    could ever claim the single overdue row. Had that been accepted as "no
    duplication", the real suppressed run's zeros would have measured nothing.
    """
    process, report = measure("red-pilot", expect="duplicates")
    assert process.returncode == 1
    assert report["status"] == "FAIL"
    assert report["observations"] == {
        "duplicate_scheduler_runs": 0,
        "duplicate_background_jobs": 0,
        "unexpected_control_writes": 0,
    }
    # The gray leg was demonstrably alive -- this is NOT a dead-proxy failure.
    assert report["liveness"]["ruler_saw_gray_lines"] > 0
    assert report["liveness"]["gray_scheduler_readonly_runs"] > 0
    assert report["liveness"]["prod_control_job_runs"] > 0
    joined = " | ".join(report["problems"])
    assert "positive control saw duplicate_scheduler_runs=0" in joined
    assert "positive control saw duplicate_background_jobs=0" in joined
    assert "positive control saw unexpected_control_writes=0" in joined


def test_unsuppressed_legs_cannot_be_reported_as_the_real_measurement():
    """`--expect none` on a run where gray never applied the suppression.

    Both unsuppressed fixtures were captured with the suppressors OFF. Feeding
    either to the real measurement has to fail on the attestation, no matter
    what the counts say, because the run measured the wrong configuration.
    """
    for base in ("green-unsuppressed", "red-pilot"):
        process, report = measure(base, expect="none")
        assert process.returncode == 1, base
        assert any(
            "does not attest full suppression" in problem for problem in report["problems"]
        ), (base, report["problems"])


def test_the_suppressed_run_yields_the_three_zeros_the_values_gate_requires():
    """Window 3. The only sanctioned source of the three `observations`."""
    process, report = measure("green-suppressed", expect="none")
    assert process.returncode == 0, report
    assert report["status"] == "PASS"
    assert report["observations"] == {
        "duplicate_scheduler_runs": 0,
        "duplicate_background_jobs": 0,
        "unexpected_control_writes": 0,
    }
    assert report["gray_writes"] == []


def test_the_suppressed_runs_zeros_are_backed_by_all_three_liveness_legs():
    """A 0 is only worth anything if all three of these hold at once."""
    _, report = measure("green-suppressed", expect="none")
    live = report["liveness"]
    # 1. the ruler could see the gray leg at all
    assert live["ruler_saw_gray_lines"] > 0
    # 2. gray's scheduler was alive -- otherwise "wrote nothing" == "never started"
    assert live["gray_scheduler_readonly_runs"] > 0
    # 3. prod really ran the control job, so there WAS something to duplicate
    assert live["prod_control_job_runs"] > 0
    assert report["control_jobs"]["reset_budget_job"]["prod_runs"] > 0
    assert report["control_jobs"]["reset_budget_job"]["gray_runs"] == 0
    # And the pairing is right way round: exactly one leg owns the scheduler.
    assert report["legs"]["gray"]["suppressed"] is True
    assert report["legs"]["prod"]["suppressed"] is False


def test_suppressing_both_legs_makes_zero_duplication_trivially_true(tmp_path: Path):
    """The failure mode that would quietly invalidate the whole measurement.

    With no leg owning the scheduler nothing writes, so all three counts are 0
    for a reason that has nothing to do with the suppression working.
    """
    prod = json.loads((FIXTURES / "green-suppressed-prod.json").read_text())
    gray_knobs = json.loads((FIXTURES / "green-suppressed-gray.json").read_text())
    prod["scheduler_knobs"] = gray_knobs["scheduler_knobs"]
    doubled = tmp_path / "prod.json"
    doubled.write_text(json.dumps(prod), encoding="utf-8")
    process = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--log",
            str(FIXTURES / "green-suppressed.log"),
            "--gray-result",
            str(FIXTURES / "green-suppressed-gray.json"),
            "--prod-result",
            str(doubled),
            "--expect",
            "none",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 1
    report = json.loads(process.stdout)
    assert any("no leg owning the scheduler" in p for p in report["problems"]), report["problems"]


def test_the_suppressed_run_would_be_rejected_as_a_positive_control():
    """The two `--expect` modes are not interchangeable.

    Window 3's zeros are the answer; offered as a positive control they are
    exactly the blind green the tool exists to refuse.
    """
    process, report = measure("green-suppressed", expect="duplicates")
    assert process.returncode == 1
    assert any("ran suppressed" in p for p in report["problems"]), report["problems"]
    assert any("cannot see duplication" in p for p in report["problems"]), report["problems"]


def test_suppression_is_read_off_the_legs_own_attestation():
    """Not off the rendered Job -- off what the process reported it loaded."""
    _, report = measure("green-unsuppressed", expect="duplicates")
    for role in ("gray", "prod"):
        knobs = report["legs"][role]["scheduler_knobs"]
        assert report["legs"][role]["suppressed"] is False
        assert knobs["disable_reset_budget"] is False
        # Unset reads as null, never as "false": an env var that was never
        # exported must not be mistaken for one explicitly pinned off.
        assert set(knobs["env"].values()) == {None}


def test_read_only_pollers_are_never_counted_as_control_writes():
    """They run on every release by design; counting them would be a permanent red."""
    _, report = measure("green-unsuppressed", expect="duplicates")
    assert set(report["control_jobs"]) == {"reset_budget_job"}
    for write in report["gray_writes"] + report["prod_writes"]:
        assert not write["sql"].upper().startswith("SELECT")


def test_settle_offset_shifts_the_window_and_can_exclude_the_evidence():
    """The window bounds are load-bearing, not decorative.

    A settle offset long enough to swallow the whole hold must not silently
    report a clean three zeros; it has to lose the liveness that justifies them.
    """
    process, report = measure(
        "green-unsuppressed", expect="duplicates", extra=["--settle-seconds", "1400"]
    )
    assert process.returncode == 1
    assert report["liveness"]["gray_scheduler_readonly_runs"] == 0
    assert any("blind 0" in p or "was not running" in p for p in report["problems"])


def test_window_that_closes_before_it_opens_is_rejected_outright(tmp_path: Path):
    payload = json.loads((FIXTURES / "green-unsuppressed-gray.json").read_text())
    payload["hold"]["started_at"] = "2026-09-14 03:13:30.118"
    payload["hold"]["ended_at"] = "2026-09-14 03:38:30.158"
    shifted = tmp_path / "gray.json"
    shifted.write_text(json.dumps(payload), encoding="utf-8")
    process = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--log",
            str(FIXTURES / "green-unsuppressed.log"),
            "--gray-result",
            str(shifted),
            "--prod-result",
            str(FIXTURES / "green-unsuppressed-prod.json"),
            "--expect",
            "duplicates",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode != 0
    assert "do not overlap" in process.stderr


def test_two_legs_sharing_one_client_address_cannot_be_attributed(tmp_path: Path):
    """%h is the ONLY attribution channel -- `%a` is `[unknown]` under Prisma."""
    payload = json.loads((FIXTURES / "green-unsuppressed-gray.json").read_text())
    payload["hold"]["client_addr"] = "10.42.1.86/32"
    collided = tmp_path / "gray.json"
    collided.write_text(json.dumps(payload), encoding="utf-8")
    process = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--log",
            str(FIXTURES / "green-unsuppressed.log"),
            "--gray-result",
            str(collided),
            "--prod-result",
            str(FIXTURES / "green-unsuppressed-prod.json"),
            "--expect",
            "duplicates",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode != 0
    assert "same client address" in process.stderr


@pytest.mark.parametrize("missing", ["hold", "client_addr"])
def test_a_result_without_usable_window_evidence_is_refused(tmp_path: Path, missing: str):
    payload = json.loads((FIXTURES / "green-unsuppressed-gray.json").read_text())
    if missing == "hold":
        payload.pop("hold")
        expected = "no hold section"
    else:
        payload["hold"]["client_addr"] = "local"
        expected = "no usable client_addr"
    broken = tmp_path / "gray.json"
    broken.write_text(json.dumps(payload), encoding="utf-8")
    process = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--log",
            str(FIXTURES / "green-unsuppressed.log"),
            "--gray-result",
            str(broken),
            "--prod-result",
            str(FIXTURES / "green-unsuppressed-prod.json"),
            "--expect",
            "duplicates",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode != 0
    assert expected in process.stderr


def test_fixtures_are_real_captured_log_text():
    """Guard against someone later 'fixing' a test by editing the evidence.

    Every line in a fixture log either carries the exact prefix clone C was
    configured with, or is a continuation of the statement above it.
    """
    for log in sorted(FIXTURES.glob("*.log")):
        text = log.read_text(encoding="utf-8")
        assert text, log
        prefixed = [
            line
            for line in text.splitlines()
            if line and not line.startswith(("\t", " "))
        ]
        assert prefixed, log
        for line in prefixed:
            assert " UTC [" in line and " app=" in line and " host=" in line, (log, line[:120])
