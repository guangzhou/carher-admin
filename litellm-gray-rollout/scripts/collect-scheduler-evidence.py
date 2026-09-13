#!/usr/bin/env python3
"""Emit the scheduler-safety evidence file that prepare-values.py consumes.

Why this exists: until 2026-09-13 the evidence file had no producer in this
repo. `prepare-values.py --scheduler-evidence` demanded a JSON document whose
`callbacks_sha256` covers 33 files, whose `runtime_sha256` is Helm's own
`toRawJson` over four post-merge values, and whose `source_payload_sha256`
digests the rest of the document. None of that is hand-computable, so in
practice the operator would have had to copy digests out of an error message
until the gate went quiet -- which is the rubber-stamp shape this whole run is
built to avoid.

What the digests are and are not: they **bind** the evidence to one specific
artefact so it cannot be reused across a rebuild. They assert nothing about the
cluster. Deriving them mechanically (by calling `prepare-values.py
--emit-bindings` on the same inputs) is therefore correct, not circular.

What the evidence actually asserts is `observations` -- the three counts that
say no second scheduler, no duplicated background job and no unexpected control
plane write was seen in the window. Those come from the cluster, so this script
refuses to invent them: every count must be passed explicitly, and `--source`
must record how they were measured. A zero you did not measure is worse than no
evidence at all, because it reads as a clean green.

The 15-minute freshness window in prepare-values.py means this runs inside the
change window, immediately before the values file is frozen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

SCRIPT_DIR = Path(__file__).resolve().parent
PREPARE_VALUES = SCRIPT_DIR / "prepare-values.py"
SOURCE_RE = re.compile(r"(?:clone|direct):[A-Za-z0-9._:-]+")
OBSERVATION_KEYS = (
    "duplicate_scheduler_runs",
    "duplicate_background_jobs",
    "unexpected_control_writes",
)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"collect-scheduler-evidence: {message}")


def raw_json(value: object) -> str:
    """Same encoder as prepare-values.raw_json; see its docstring."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def collect_bindings(passthrough: list[str]) -> dict:
    """Ask prepare-values.py for the digests, rather than recomputing them here.

    A second implementation of the same digests is a second thing to drift. The
    whole point of the binding is that it equals what prepare-values will
    compute a minute later, so the only safe producer is prepare-values itself.
    """
    if not PREPARE_VALUES.is_file():
        fail(f"{PREPARE_VALUES} not found")
    proc = subprocess.run(
        [sys.executable, str(PREPARE_VALUES), "--emit-bindings", *passthrough],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Surface the real reason. prepare-values fails closed on inline
        # credentials, unmountable volumes and missing snapshots; swallowing
        # that here would turn a hard red into "evidence collection failed".
        sys.stderr.write(proc.stderr)
        fail(f"prepare-values --emit-bindings exited {proc.returncode}")
    try:
        bindings = json.loads(proc.stdout)
    except json.JSONDecodeError:
        fail("prepare-values --emit-bindings did not print JSON")
    if bindings.get("mode") != "emit-bindings":
        fail("prepare-values did not run in emit-bindings mode")
    return bindings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        help=(
            "how the observations were measured, as clone:<id> or direct:<id>. "
            "direct: means the counts came off the live production control "
            "plane; clone: means they came off a qualification clone"
        ),
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["primary", "disabled"],
        help=(
            "primary for the prod release (it owns the scheduler); disabled "
            "for gray and guarded-old, which must not run it a second time"
        ),
    )
    for key in OBSERVATION_KEYS:
        parser.add_argument(
            f"--{key.replace('_', '-')}",
            type=int,
            required=True,
            metavar="N",
            help=f"measured count of {key.replace('_', ' ')} in the window",
        )
    # Deliberately not --output: everything this parser does not recognise is
    # forwarded verbatim to prepare-values.py, and prepare-values has an
    # --output of its own. Sharing the name would let argparse silently eat the
    # wrong one.
    parser.add_argument("--evidence-output", type=Path, required=True, dest="output")
    args, passthrough = parser.parse_known_args()

    observations = {}
    for key in OBSERVATION_KEYS:
        count = getattr(args, key)
        if count < 0:
            fail(f"{key} cannot be negative")
        observations[key] = count

    if not SOURCE_RE.fullmatch(args.source):
        fail("--source must match clone:<id> or direct:<id>")

    bindings = collect_bindings(passthrough)
    profile = bindings.get("profile")
    if args.mode == "primary" and profile != "prod":
        fail(f"primary mode is only valid for the prod profile, got {profile!r}")
    if args.mode == "disabled" and profile == "prod":
        fail("the prod profile must declare primary mode")

    payload = {
        "schema_version": 1,
        "profile": profile,
        "mode": args.mode,
        "image_digest": bindings["image_digest"],
        "config_sha256": bindings["config_sha256"],
        "callbacks_sha256": bindings["callbacks_sha256"],
        "runtime_sha256": bindings["runtime_sha256"],
        "captured_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "source": args.source,
        "observations": observations,
    }
    payload["source_payload_sha256"] = (
        "sha256:" + hashlib.sha256(raw_json(payload).encode("utf-8")).hexdigest()
    )

    if args.output.exists():
        # Never overwrite: a stale evidence file that silently became a fresh
        # one is the exact failure this gate is supposed to catch.
        fail(f"{args.output} already exists; evidence files are never reused")
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)

    nonzero = [key for key, count in observations.items() if count]
    print(
        json.dumps(
            {
                "tool": "collect-scheduler-evidence",
                # Non-zero observations are not this script's to adjudicate --
                # it records what was measured. prepare-values.py fails closed
                # on any non-zero count, which is where the stop belongs.
                "status": "PASS" if not nonzero else "RECORDED_NONZERO",
                "output": str(args.output),
                "profile": profile,
                "mode": args.mode,
                "nonzero_observations": sorted(nonzero),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
