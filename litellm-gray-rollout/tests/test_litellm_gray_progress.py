"""Contract tests for the gray-rollout progress ledger and its gate counterpart.

Two things are pinned here.

First, gray-progress.py's reading of on-disk generation directories. Every number
it prints is reconstructed from directory names, state.env and key-sid.map line
counts, so a misread does not look like a crash -- it looks like a plausible
progress bar. The initial-generation ordering case below is not hypothetical: the
first synthetic run reported ring 1/13 (preflight) for a run that was actually at
6/13 (5%), because `<run_id>-g000001` carries no timestamp in its name and the
mtime fallback sorted it last.

Second, the arithmetic coupling between three files. metrics.py's
ABS_SUSTAIN_WINDOWS is how many consecutive breaching windows the absolute 5xx
stop-loss needs; check-monitor-continuity.py's --min-cycles is how many cycles a
ramp step must contain before it may claim the window was observed; and
gray-progress.py's DEFAULT_MIN_CYCLES is what the ledger prints as required.
Raising the first without the others silently disarms the stop-loss again -- the
exact 2026-09-14 failure, where 5% / 10% / 50% each ran 2 cycles against a depth
of 4 and every gate was green. A comment cannot stop that; this test can.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "litellm-gray-rollout" / "scripts"
PROGRESS = TOOLS / "gray-progress.py"
CONTINUITY = TOOLS / "check-monitor-continuity.py"
METRICS = TOOLS / "metrics.py"

BASE = dt.datetime(2026, 9, 20, 2, 0, 0, tzinfo=dt.timezone.utc)


def _load(path: Path, name: str) -> Any:
    """Import a hyphenated CLI script as a module so constants can be asserted on."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


progress = _load(PROGRESS, "gray_progress_under_test")
continuity = _load(CONTINUITY, "gray_continuity_under_test")
metrics = _load(METRICS, "gray_metrics_under_test")


# --------------------------------------------------------------------------
# fixtures: a run laid out on disk the way the real GRAY_ROOT lays one out
# --------------------------------------------------------------------------

Step = tuple[int, str, str, int]  # minutes from BASE, phase, split, key count


def write_run(root: Path, run_id: str, steps: list[Step], cycles: list[int]) -> Path:
    """Materialise generations/ + evidence/monitor-heartbeat.jsonl.

    The first step is written as `<run_id>-g000001`, i.e. without a timestamp in
    its name, because that is what gray-run-init.sh mints; every later one gets
    the `g<UTC>-<pid>-<rand>` name that stage_from_active() mints.
    """
    generations = root / "generations"
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    for minutes, phase, split, keys in steps:
        if minutes == 0:
            name = f"{run_id}-g000001"
        else:
            stamp = (BASE + dt.timedelta(minutes=minutes)).strftime("%Y%m%dT%H%M%SZ")
            name = f"g{stamp}-1000-{minutes}"
        directory = generations / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state.env").write_text(
            f"run_id={run_id}\ngeneration={name}\nphase={phase}\nmode=0\n"
            f"split={split}\nbridge=off\nrouting_frozen=0\n"
            "input_checksum=test-mode\nconfig_checksum=test-mode\n"
        )
        (directory / "key-sid.map").write_text(
            "".join(f"sk-fixture-{i} sid{i}\n" for i in range(keys))
        )
    ledger = evidence / "monitor-heartbeat.jsonl"
    with ledger.open("w", encoding="utf-8") as handle:
        for index, minutes in enumerate(cycles):
            stamp = BASE + dt.timedelta(minutes=minutes)
            handle.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "tool": "gray-monitor-cycle",
                        "cycle_completed_at": stamp.isoformat().replace("+00:00", "Z"),
                        "run_id": run_id,
                        "generation": "g",
                        "metrics_status": "PASS",
                        "evidence": f"metrics-{index}.json",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    os.chmod(ledger, 0o600)
    return ledger


def report_for(root: Path, ledger: Path | None = None, **overrides: Any) -> dict[str, Any]:
    argv = [
        str(PROGRESS),
        "--generations", str(root / "generations"),
        "--json",
    ]
    if ledger is not None:
        argv += ["--ledger", str(ledger)]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    result = subprocess.run(
        [sys.executable, *argv], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def stage(report: dict[str, Any], key: str) -> dict[str, Any]:
    for item in report["stages"]:
        if item["key"] == key:
            return item
    raise AssertionError(f"stage {key} missing from ledger")


# A healthy run: enters normal_gray empty, enrols keys, then every ramp step
# dwells for at least min_cycles, all the way to committed.
GREEN_STEPS: list[Step] = [
    (0, "preflight", "0", 0),
    (20, "normal_gray", "0", 0),
    (30, "normal_gray", "0", 3),
    (60, "normal_gray", "1", 3),
    (85, "normal_gray", "5", 3),
    (110, "normal_gray", "10", 3),
    (135, "normal_gray", "50", 3),
    (160, "normal_gray", "100", 3),
    (200, "convergence_ready", "100", 3),
    (210, "prod_offline_upgrading", "100", 3),
    (225, "prod_verified", "100", 3),
    (230, "committed", "0", 3),
]
GREEN_CYCLES = list(range(61, 200, 5))


# --------------------------------------------------------------------------
# the coupling that must be able to go red
# --------------------------------------------------------------------------

def test_min_cycles_equals_absolute_sustain_depth() -> None:
    """The deepest stop-loss depth and the observation floor are one number.

    metrics.py decides how many consecutive breaching windows the absolute 5xx
    leg needs. If a ramp step is allowed to dwell for fewer cycles than that, the
    leg is not "quiet", it is unarmed -- and the gate is green either way, which
    is what happened at 5% / 10% / 50% on 2026-09-14. Keep the three equal.
    """
    depth = metrics.SUSTAIN_DEPTH["FIVE_XX_ABSOLUTE"]
    assert depth == metrics.ABS_SUSTAIN_WINDOWS
    assert continuity.DEFAULT_MIN_CYCLES == depth, (
        "check-monitor-continuity.py --min-cycles default drifted from "
        "metrics.py ABS_SUSTAIN_WINDOWS: ramp steps may now dwell too short "
        "for the absolute 5xx stop-loss to fire"
    )
    assert progress.DEFAULT_MIN_CYCLES == depth, (
        "gray-progress.py DEFAULT_MIN_CYCLES drifted from ABS_SUSTAIN_WINDOWS: "
        "the ledger would report an under-observed step as sufficient"
    )


def test_deepest_leg_is_deeper_than_the_comparative_default() -> None:
    """Depth is configured per code, not globally -- that is the whole lever.

    Raising SUSTAIN_WINDOWS globally to reach depth 4 would drag the comparative
    legs along and change their calibrated false-fire rate. If these ever become
    equal, the per-code configuration has been flattened by accident.
    """
    assert metrics.ABS_SUSTAIN_WINDOWS > metrics.SUSTAIN_WINDOWS


def test_data_liveness_floor_matches_the_frozen_cycle_interval() -> None:
    """The liveness floor and the scheduler interval are one number too.

    MAX_SOURCE_SILENCE_SECONDS is what makes a silent input red, and it is only
    meaningful relative to how often a cycle runs: at the frozen 300s cadence one
    missed cycle is already over the line, which is the intended sensitivity.

    Raise the interval without raising this and the leg becomes a false alarm on
    every single cycle -- each source is now legitimately up to one new interval
    old.  Lower the interval without lowering this and it silently loses
    resolution, tolerating several missed cycles before it says anything.  This is
    the same arithmetic coupling that made `PROBE_INTERVAL` 900 -> 1800 turn a
    stale-check into a per-round false red and quietly degraded a
    `min_over_time[35m]` "two consecutive rounds" rule into one round.

    A comment saying "keep these in step" does not stop the next person; a test
    that goes red does.  A run at a genuinely different cadence passes its own
    value on both sides -- what must not happen is one of them moving alone.
    """
    assert metrics.MAX_SOURCE_SILENCE_SECONDS == progress.DEFAULT_CYCLE_INTERVAL, (
        "metrics.py MAX_SOURCE_SILENCE_SECONDS drifted from the frozen cycle "
        "interval: the data-liveness leg now either fires every cycle or tolerates "
        "several missed ones"
    )
    # The floor has to be at least one full interval or it cannot be met even by a
    # perfectly healthy run: sources are read sequentially within a cycle, so their
    # timestamps legitimately differ.
    assert metrics.MAX_SOURCE_SILENCE_SECONDS >= progress.DEFAULT_CYCLE_INTERVAL


def test_monitor_loop_default_interval_matches_the_liveness_floor() -> None:
    """The shell scheduler holds the third copy of that same number.

    gray-monitor-loop.sh is what actually sets the cadence, and it defaults it in
    shell, out of reach of every Python constant.  Read here so the coupling is
    checked against the thing that schedules rather than against two constants that
    only agree with each other.
    """
    loop = (TOOLS / "gray-monitor-loop.sh").read_text(encoding="utf-8")
    match = re.search(r'INTERVAL="\$\{GRAY_CYCLE_INTERVAL_SECONDS:-(\d+)\}"', loop)
    assert match, (
        "could not find gray-monitor-loop.sh's interval default -- if the variable "
        "was renamed, update this extractor rather than deleting the check: an "
        "extractor that silently stops matching is how a coupling check turns into "
        "a green that measures nothing"
    )
    assert int(match.group(1)) == metrics.MAX_SOURCE_SILENCE_SECONDS


# --------------------------------------------------------------------------
# check-monitor-continuity.py: the INSUFFICIENT_CYCLES leg
# --------------------------------------------------------------------------

def run_continuity(
    ledger: Path, window_start: dt.datetime, **overrides: Any
) -> tuple[int, dict[str, Any]]:
    argv = [
        str(CONTINUITY),
        "--ledger", str(ledger),
        "--run-id", overrides.pop("run_id", "fixture-run"),
        "--generation", "fixture-run-g000001",
        "--config-checksum", "test-mode",
        "--window-start", window_start.isoformat().replace("+00:00", "Z"),
        "--cycle-interval-seconds", str(overrides.pop("interval", 300)),
    ]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    result = subprocess.run(
        [sys.executable, *argv], capture_output=True, text=True, check=False
    )
    assert result.stdout, result.stderr
    return result.returncode, json.loads(result.stdout)


@pytest.fixture()
def recent_ledger(tmp_path: Path) -> Path:
    """A ledger whose cycles land just before 'now', so no trailing gap fires.

    The gap legs are relative to wall-clock now; this test file is about counts,
    so the fixture is anchored to now rather than to BASE.
    """
    def build(count: int, interval_seconds: int = 300) -> tuple[Path, dt.datetime]:
        now = dt.datetime.now(dt.timezone.utc)
        stamps = [
            now - dt.timedelta(seconds=interval_seconds * (count - 1 - i))
            for i in range(count)
        ]
        ledger = tmp_path / f"heartbeat-{count}.jsonl"
        with ledger.open("w", encoding="utf-8") as handle:
            for index, stamp in enumerate(stamps):
                handle.write(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "tool": "gray-monitor-cycle",
                            "cycle_completed_at": stamp.isoformat().replace("+00:00", "Z"),
                            "run_id": "fixture-run",
                            "generation": "g",
                            "metrics_status": "PASS",
                            "evidence": f"metrics-{index}.json",
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        os.chmod(ledger, 0o600)
        # Start the window one interval before the first cycle so the leading
        # comparison against window_start is inside the allowance.
        return ledger, stamps[0] - dt.timedelta(seconds=1)

    return build  # type: ignore[return-value]


def test_short_window_with_no_gap_is_red(recent_ledger) -> None:
    """Two cycles 5 minutes apart: no gap at all, and still unobservable.

    This is the precise shape the gate used to pass. It is why a count check had
    to be added rather than tightening the gap allowance -- there is no gap here
    to tighten against.
    """
    ledger, window_start = recent_ledger(2)
    code, result = run_continuity(ledger, window_start)
    assert result["gaps"] == [], "fixture must be gap-free or it proves the wrong thing"
    assert result["cycles_in_window"] == 2
    assert result["status"] == "FAIL"
    assert result["errors"] == ["INSUFFICIENT_CYCLES"]
    assert code == 1


def test_enough_cycles_is_green(recent_ledger) -> None:
    ledger, window_start = recent_ledger(4)
    code, result = run_continuity(ledger, window_start)
    assert result["cycles_in_window"] == 4
    assert result["errors"] == []
    assert result["status"] == "PASS"
    assert code == 0


def test_empty_window_stays_no_cycles_not_insufficient(recent_ledger) -> None:
    """The two emptiness shapes must stay distinguishable in the evidence."""
    ledger, _ = recent_ledger(4)
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
    code, result = run_continuity(ledger, future)
    assert "NO_CYCLES_IN_WINDOW" in result["errors"]
    assert "INSUFFICIENT_CYCLES" not in result["errors"]
    assert code == 1


def test_min_cycles_reported_so_the_threshold_is_auditable(recent_ledger) -> None:
    """A gate that does not say what it required cannot be checked afterwards."""
    ledger, window_start = recent_ledger(4)
    _, result = run_continuity(ledger, window_start, min_cycles=3, interval=300)
    assert result["min_cycles"] == 3
    assert result["min_observation_seconds"] == 900


def test_min_cycles_zero_is_refused(recent_ledger) -> None:
    ledger, window_start = recent_ledger(4)
    result = subprocess.run(
        [
            sys.executable, str(CONTINUITY),
            "--ledger", str(ledger),
            "--run-id", "fixture-run",
            "--generation", "fixture-run-g000001",
            "--config-checksum", "test-mode",
            "--window-start", window_start.isoformat().replace("+00:00", "Z"),
            "--cycle-interval-seconds", "300",
            "--min-cycles", "0",
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "--min-cycles" in result.stderr


# --------------------------------------------------------------------------
# gray-progress.py: reading the timeline off disk
# --------------------------------------------------------------------------

def test_initial_generation_sorts_first_even_with_a_later_mtime(tmp_path: Path) -> None:
    """`<run_id>-g000001` has no timestamp in its name; mtime must not decide order.

    mtime survives `cp -p`, restores and repacking, so it can end up later than
    the second generation. When that happened the ledger reported ring 1/13
    (preflight) for a run sitting at 6/13 (5%): the whole timeline was reordered
    and every dwell time was wrong, with no error anywhere. Position is
    structural -- the directory is named g000001 -- so rank pins it, and mtime is
    only allowed to fill in the displayed clock time.
    """
    ledger = write_run(
        tmp_path, "order-run",
        [(0, "preflight", "0", 0), (30, "normal_gray", "0", 0), (60, "normal_gray", "5", 3)],
        [61, 66, 71, 76],
    )
    initial = tmp_path / "generations" / "order-run-g000001"
    future = (BASE + dt.timedelta(days=30)).timestamp()
    os.utime(initial, (future, future))

    report = report_for(tmp_path, ledger)
    assert report["stages"][0]["key"] == "preflight"
    assert stage(report, "preflight")["status"] == "已过"
    assert report["phase"] == "normal_gray"
    assert report["split"] == "5"
    assert stage(report, "split_5")["status"] == "当前"
    # The clamped start must not produce a negative first segment.
    assert stage(report, "preflight")["elapsed_seconds"] >= 0
    assert report["elapsed_total_seconds"] > 0


def test_named_keys_is_distinguished_from_gray_entry_by_the_key_list(tmp_path: Path) -> None:
    """Rings ③ and ④ share (phase, split); only key-sid.map tells them apart.

    Without the key-count dimension both fold into one segment and ④ reports
    跳过⚠ on every run, including a clean one -- a permanent false red.
    """
    ledger = write_run(tmp_path, "green-run", GREEN_STEPS, GREEN_CYCLES)
    report = report_for(tmp_path, ledger)
    entry = stage(report, "gray_entry")
    named = stage(report, "named_keys")
    assert entry["status"] == "已过"
    assert named["status"] == "已过"
    assert named["warnings"] == []
    assert entry["entered_at"] < named["entered_at"]


def test_clean_run_raises_no_warnings_at_all(tmp_path: Path) -> None:
    """The green control. A gate that cannot be quiet is not a gate."""
    ledger = write_run(tmp_path, "green-run", GREEN_STEPS, GREEN_CYCLES)
    report = report_for(tmp_path, ledger)
    noisy = {item["key"]: item["warnings"] for item in report["stages"] if item["warnings"]}
    assert noisy == {}
    assert report["terminal"] is True
    assert report["outcome"] == "committed"
    assert report["remaining_stages"] == 0
    for item in report["stages"]:
        assert item["status"] in {"已过", "当前", "跳过"}


def test_under_observed_ramp_step_is_reported_with_its_count(tmp_path: Path) -> None:
    """The 2026-09-14 shape: dwelled, looked fine, stop-loss never armed."""
    ledger = write_run(
        tmp_path, "short-run",
        [
            (0, "preflight", "0", 0),
            (20, "normal_gray", "0", 0),
            (30, "normal_gray", "0", 3),
            (60, "normal_gray", "1", 3),
            (95, "normal_gray", "5", 3),
        ],
        [61, 66, 71, 76, 81, 96, 101],
    )
    report = report_for(tmp_path, ledger)
    short = stage(report, "split_5")
    assert short["cycles_observed"] == 2
    assert short["cycles_required"] == 4
    assert any("没有武装过" in text for text in short["warnings"])
    assert stage(report, "split_1")["warnings"] == []


def test_missing_ledger_says_so_instead_of_claiming_observed(tmp_path: Path) -> None:
    """No ledger means the count is unproven, which is not the same as zero."""
    write_run(tmp_path, "noledger-run",
              [(0, "preflight", "0", 0), (30, "normal_gray", "0", 0),
               (60, "normal_gray", "1", 3)], [])
    report = report_for(tmp_path, ledger=None)
    ramp = stage(report, "split_1")
    assert ramp["cycles_observed"] is None
    assert any("无 heartbeat 台账" in text for text in ramp["warnings"])


def test_skipped_non_optional_stage_is_flagged_and_optional_one_is_not(tmp_path: Path) -> None:
    ledger = write_run(
        tmp_path, "skip-run",
        [(0, "preflight", "0", 0), (30, "normal_gray", "0", 0), (60, "normal_gray", "5", 0)],
        [61, 66, 71, 76],
    )
    report = report_for(tmp_path, ledger)
    assert stage(report, "bridge")["status"] == "跳过"
    assert stage(report, "bridge")["warnings"] == []
    assert stage(report, "named_keys")["status"] == "跳过⚠"
    assert any("不是 optional" in t for t in stage(report, "named_keys")["warnings"])


def test_rolled_back_run_reports_how_deep_it_got(tmp_path: Path) -> None:
    """A terminal phase is not a ring, but 'died at which ring' is the question."""
    ledger = write_run(
        tmp_path, "rb-run",
        [
            (0, "preflight", "0", 0),
            (20, "normal_gray", "0", 0),
            (30, "normal_gray", "0", 3),
            (60, "normal_gray", "1", 3),
            (90, "rolled_back", "0", 3),
        ],
        [62, 67, 72, 77],
    )
    report = report_for(tmp_path, ledger)
    assert report["terminal"] is True
    assert report["outcome"] == "rolled_back"
    assert report["current_stage_index"] is None
    assert report["deepest_stage_index"] == 5  # ⑤.1 放量 1%
    assert report["remaining_stages"] == 0
    assert stage(report, "split_1")["status"] == "已过"


def test_unknown_phase_refuses_to_report_progress(tmp_path: Path) -> None:
    """Snapping to the nearest ring would hide a state machine that went off-list."""
    ledger = write_run(
        tmp_path, "unk-run",
        [(0, "preflight", "0", 0), (30, "aborting_to_bridge", "100", 3)],
        [31, 36, 41, 46],
    )
    report = report_for(tmp_path, ledger)
    assert report["current_stage_index"] is None
    assert report["remaining_stages"] is None
    assert report["remaining_machine_seconds"] is None
    assert "不在台账的环节清单里" in report["eta_note"]


def test_eta_splits_machine_time_from_human_signatures(tmp_path: Path) -> None:
    """Blending waiting-for-a-human into an ETA produces a number nobody believes.

    Last round the 指名 key 试点 ring measured 84.8h, of which 79h was nobody
    doing anything. So the floor counts only arithmetic (cycles x interval) and
    reports pending human rings as a separate count.
    """
    ledger = write_run(
        tmp_path, "eta-run",
        [
            (0, "preflight", "0", 0),
            (20, "normal_gray", "0", 0),
            (30, "normal_gray", "0", 3),
            (60, "normal_gray", "1", 3),
            (80, "normal_gray", "5", 3),
        ],
        [61, 66, 71, 76, 81, 86],
    )
    report = report_for(tmp_path, ledger, cycle_interval_seconds=300, min_cycles=4)
    # Ahead of 5%: 10 / 50 / 100 at 4x300s each, plus prod_offline's measured
    # floor, plus whatever cycles 5% itself still owes.
    assert report["remaining_machine_seconds"] >= 3 * 4 * 300
    assert report["remaining_human_stages"] == 0
    assert "机器时间下限" in report["eta_note"]
    for item in report["stages"]:
        if item["kind"] == "human":
            assert item["machine_floor_seconds"] in (0, None), (
                "a ring that waits on a signature must not contribute to the floor"
            )


def test_same_state_generations_fold_and_the_count_is_kept(tmp_path: Path) -> None:
    """662 generations of one state is a fact about how keys were enrolled."""
    generations = tmp_path / "generations"
    write_run(tmp_path, "fold-run",
              [(0, "preflight", "0", 0), (30, "normal_gray", "0", 1)], [])
    for index in range(2, 8):
        stamp = (BASE + dt.timedelta(minutes=30 + index)).strftime("%Y%m%dT%H%M%SZ")
        directory = generations / f"g{stamp}-1000-{index}"
        directory.mkdir()
        (directory / "state.env").write_text(
            "run_id=fold-run\ngeneration=x\nphase=normal_gray\nmode=0\nsplit=0\n"
            "bridge=off\nrouting_frozen=0\ninput_checksum=test-mode\n"
            "config_checksum=test-mode\n"
        )
        (directory / "key-sid.map").write_text(
            "".join(f"sk-fixture-{i} sid{i}\n" for i in range(index))
        )
    report = report_for(tmp_path, ledger=None)
    named = stage(report, "named_keys")
    assert named["status"] == "当前"
    assert named["generations"] == 7


def test_other_runs_are_filtered_out(tmp_path: Path) -> None:
    """generations/ is never pruned, so it holds every past run's directories."""
    write_run(tmp_path, "old-run",
              [(0, "preflight", "0", 0), (10, "committed", "0", 3)], [])
    ledger = write_run(
        tmp_path, "new-run",
        [(200, "preflight", "0", 0), (230, "normal_gray", "0", 0),
         (260, "normal_gray", "1", 3)],
        [261, 266, 271, 276],
    )
    report = report_for(tmp_path, ledger)
    assert report["run_id"] == "new-run"
    assert stage(report, "split_1")["status"] == "当前"

    pinned = report_for(tmp_path, ledger, run_id="old-run")
    assert pinned["run_id"] == "old-run"
    assert pinned["outcome"] == "committed"


def test_progress_never_fails_the_caller(tmp_path: Path) -> None:
    """The ledger observes; reds belong to the gate. It must be safe mid-ramp."""
    ledger = write_run(
        tmp_path, "exit-run",
        [(0, "preflight", "0", 0), (30, "normal_gray", "0", 0), (60, "normal_gray", "5", 0)],
        [61, 66],
    )
    result = subprocess.run(
        [sys.executable, str(PROGRESS), "--generations", str(tmp_path / "generations"),
         "--ledger", str(ledger)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "止损腿" in result.stdout
    assert "不是 optional" in result.stdout


def test_ramp_ladder_matches_the_approved_split_values() -> None:
    """The ledger's rings and the splits gray-split-update.sh accepts are one list."""
    ladder = [
        item["split"] for item in progress.STAGES if item["key"].startswith("split_")
    ]
    assert ladder == list(progress.RAMP_LADDER)
    update = (TOOLS / "gray-split-update.sh").read_text(encoding="utf-8")
    for percent in ladder:
        assert percent in update or "0,1,5,10,50,100" in update
