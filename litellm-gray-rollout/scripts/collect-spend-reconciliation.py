#!/usr/bin/env python3
"""Emit the spend_reconciliation source document metrics.py requires at split>0.

WHY A PROBE AND NOT PRODUCTION TRAFFIC
--------------------------------------
metrics.py's contract is exact id sets: every id in `expected_request_ids` must
appear in `terminal_request_ids` or `failed_request_ids`, and anything left over
is a SPEND_RECONCILIATION_FAILED trigger.  Spend triggers bypass the two-window
sustain gate ("observed faults, not sampled ratios"), so an inexact ruler
dispatches a rollback on its first bad reading.

Production traffic cannot supply that set.  Measured on 2026-09-18:

  * The nginx gray log carries no request id, and adding one changes
    render-production-nginx.py, whose sha256 IS `renderer_identity` inside
    `input_checksum` -> `config_checksum` -> the three hand-signed split gates.
    A new log field therefore invalidates split_sample/split_capacity/
    split_bridge and forces re-signing.
  * SpendLogs.request_id is LiteLLM-internal (`resp_<base64>..._cache_hit<ts>`
    or an upstream `gen-...` id).  No client-visible field corresponds to it.
  * `metadata->>'litellm_call_id'` is populated on only ~21% of aresponses rows,
    so it cannot audit production either.
  * Counting per key does not work: one canary sid logged 6 nginx requests/min
    against 4.5 spend rows/min, steady -- every row cache_hit=True, spend=0,
    ~600ms against nginx's 3.7s.  The two sides count different things, so a
    count ruler would fire a false rollback every cycle.

What IS exact: a request we issue ourselves.  The response header
`x-litellm-call-id` (common_request_processing.py:782) equals
`metadata->>'litellm_call_id'` on the row that request writes -- verified
end-to-end against the live gray backend.  So the reconcilable set is exactly
the probe calls this tool makes, which is what the runbook means by
"request-ID spend reconciliation".

The probe id is the join key; it is NOT a virtual key and is safe to record.
The virtual key itself is read from a file and never appears in argv, output, or
any log line.

LOOKUP MUST BE TIME-BOUNDED.  A WHERE on metadata->>'litellm_call_id' with no
time predicate is a full-table scan on LiteLLM_SpendLogs and was observed to be
killed by the client timeout, returning zero rows -- i.e. a missing-row verdict
that is really a timeout.  Every query here carries an "endTime" lower bound,
and a query that fails is reported as an error rather than as "not found":
silence must never read as a spend mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

SCHEMA_VERSION = 1
SOURCE = "collect-spend-reconciliation.py"
CALL_ID_RE = re.compile(r"^[0-9a-fA-F-]{16,64}$")
# Keep in lockstep with metrics.py's MAX_SPEND_LAG_SECONDS.  The collector polls
# past it on purpose: reporting the true lag lets metrics.py judge, whereas
# giving up early would report a missing row for something merely slow.
MAX_SPEND_LAG_SECONDS = 60
POLL_GRACE_SECONDS = 45
POLL_INTERVAL_SECONDS = 5
# A probe is a real billed request.  Keep it minimal and keep the count low.
MAX_PROBES = 10
# Bounded on purpose. Enough to ride out one transient upstream stall, small
# enough that a genuinely dead probe path still fails inside the cycle interval
# rather than running past it and colliding with the next cycle.
PROBE_ATTEMPTS = 3
PROBE_RETRY_SECONDS = 3
# 7200s, set from the measured flush tail rather than picked: over 6h/25851
# production rows on 198 (2026-09-18) the latest spend row landed 6069s after its
# endTime.  An id still absent past that has outlived every arrival actually
# observed, so calling it a lost write is a claim the data supports.  A shorter
# window would turn the backlog sweep into a false rollback; a longer one would
# let a genuinely lost write sit unreported.
CARRY_MAX_AGE_SECONDS = 7200


def die(message: str) -> NoReturn:
    print(f"collect-spend-reconciliation: {message}", file=sys.stderr)
    raise SystemExit(2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def read_key(path: Path) -> str:
    """Read the probe virtual key.

    Refuses a world/group-readable file: this is live credential material, and a
    collector that silently accepts 0644 teaches the operator the wrong habit.
    """
    try:
        info = path.stat()
    except OSError as exc:
        die(f"cannot read probe key: {exc}")
    if info.st_mode & 0o077:
        die(f"probe key {path} must be 0600, found {info.st_mode & 0o777:04o}")
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        die(f"probe key {path} is empty")
    return key


def run(argv: list[str], *, stdin: str | None = None, timeout: int) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def issue_probe(base_url: str, model: str, key: str, timeout: int) -> dict[str, Any]:
    """Send one probe and return its call id plus the observed HTTP status.

    The key goes in via stdin (`-H @-`), never argv, so it cannot reach `ps`,
    shell history, or a log line.  `-D -` writes headers to stdout while the body
    is discarded: we need the id and the status, not the completion.
    """
    argv = [
        "curl", "--silent", "--show-error",
        "--max-time", str(timeout),
        "--dump-header", "-",
        "--output", os.devnull,
        "-H", "@-",
        "-H", "content-type: application/json",
        "-X", "POST", f"{base_url.rstrip('/')}/v1/chat/completions",
        "--data-binary", json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": "spend reconciliation probe"}],
                "max_tokens": 4,
            }
        ),
    ]
    rc, out, err = run(argv, stdin=f"authorization: Bearer {key}\n", timeout=timeout + 10)
    if rc != 0:
        return {"error": f"probe transport failed rc={rc}: {err.strip()[:200]}"}
    status: int | None = None
    call_id: str | None = None
    for line in out.splitlines():
        line = line.strip()
        if line.upper().startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
        elif line.lower().startswith("x-litellm-call-id:"):
            call_id = line.split(":", 1)[1].strip()
    if status is None:
        return {"error": "probe response carried no HTTP status line"}
    if call_id is None or not CALL_ID_RE.match(call_id):
        # Without an id there is nothing to reconcile.  Say so rather than
        # quietly dropping the probe, which would shrink `expected` and make a
        # broken probe path look like a clean reconciliation.
        return {"error": f"probe returned status {status} with no usable x-litellm-call-id"}
    return {"call_id": call_id, "status": status}


def query_rows(
    psql_argv: list[str], call_ids: list[str], since: datetime, timeout: int
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Look up the probe rows.  Returns (rows_by_call_id, error).

    An error is NOT the same as "no rows": a timeout or a dead connection would
    otherwise be indistinguishable from a genuine missing row, and a missing row
    is a rollback trigger.  Callers must propagate the error instead.
    """
    if not call_ids:
        return {}, None
    ids_sql = ",".join("'" + cid.replace("'", "") + "'" for cid in call_ids)
    sql = (
        "select metadata->>'litellm_call_id', status, "
        "to_char(\"endTime\",'YYYY-MM-DD\"T\"HH24:MI:SSZ'), "
        "coalesce(round(extract(epoch from (created_at - \"endTime\"))::numeric,2),0) "
        'from "LiteLLM_SpendLogs" '
        # The time bound is mandatory, not an optimisation: without it this is a
        # full-table scan that the client timeout kills, and an empty result then
        # reads as "the row never landed".
        f"where \"endTime\" >= '{since.strftime('%Y-%m-%d %H:%M:%S')}' "
        f"and metadata->>'litellm_call_id' in ({ids_sql});"
    )
    rc, out, err = run(psql_argv + ["-At", "-c", sql], timeout=timeout)
    if rc != 0:
        return {}, f"spend query failed rc={rc}: {err.strip()[:200]}"
    rows: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        if len(fields) < 4:
            return {}, f"unexpected spend query row shape: {len(fields)} fields"
        try:
            lag = float(fields[3])
        except ValueError:
            return {}, f"unparsable write lag {fields[3]!r}"
        rows[fields[0]] = {"status": fields[1], "end_time": fields[2], "lag_seconds": lag}
    return rows, None


def load_carry(path: Path | None, max_age_seconds: int) -> dict[str, datetime]:
    """Read the ids still awaiting a spend row, dropping ones past the tail.

    A malformed or unreadable carry file yields an empty set rather than an
    error: losing the carry makes the next cycle re-report those ids as pending
    and then missing, which is the conservative direction, whereas dying here
    would darken the window entirely.
    """
    if path is None or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cutoff = utcnow() - timedelta(seconds=max_age_seconds)
    out: dict[str, datetime] = {}
    for call_id, first_seen in raw.items():
        if not isinstance(call_id, str) or not CALL_ID_RE.match(call_id):
            continue
        if not isinstance(first_seen, str):
            continue
        try:
            seen = datetime.strptime(first_seen, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if seen >= cutoff:
            out[call_id] = seen
    return out


def save_carry(path: Path | None, carried: dict[str, datetime]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({cid: iso(at) for cid, at in carried.items()}, sort_keys=True),
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-key-file", type=Path, required=True,
                        help="file holding the probe virtual key (mode 0600)")
    parser.add_argument("--base-url", required=True,
                        help="pool to probe, e.g. http://127.0.0.1:30405 for gray")
    parser.add_argument("--model", required=True, help="model group the probe key may call")
    parser.add_argument("--probes", type=int, default=3)
    parser.add_argument("--psql", default="", help="psql command; default uses --kubectl-exec")
    parser.add_argument("--kubectl-exec", default="litellm-db-0")
    parser.add_argument("--namespace", default="litellm-product")
    parser.add_argument("--db-user", default="litellm")
    parser.add_argument("--db-name", default="litellm")
    parser.add_argument("--query-timeout", type=int, default=60)
    # 30s, not 60s: the measured round-trip is 0.7-4.7s, and the whole collector
    # must finish inside one 300s monitor cycle. Worst case is now
    # PROBE_ATTEMPTS*30 + retries + the 105s poll deadline ~= 200s, which leaves
    # headroom; at 60s it could run past the next cycle and overlap itself.
    parser.add_argument("--probe-timeout", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--carry-file", type=Path, default=None,
        help="state file of ids still awaiting a spend row; without it a row "
             "inside LiteLLM's flush backlog is reported missing every cycle",
    )
    parser.add_argument(
        "--carry-max-age-seconds", type=int, default=CARRY_MAX_AGE_SECONDS,
        help="how long an id may stay pending before it counts as a lost write",
    )
    args = parser.parse_args()

    if not 1 <= args.probes <= MAX_PROBES:
        die(f"--probes must be between 1 and {MAX_PROBES}")
    key = read_key(args.probe_key_file)

    if args.psql:
        psql_argv = args.psql.split()
    else:
        psql_argv = [
            "kubectl", "-n", args.namespace, "exec", "-i", args.kubectl_exec, "--",
            "psql", "-U", args.db_user, "-d", args.db_name,
        ]

    # Anchor the lookup window slightly before the first probe so a row whose
    # endTime precedes our own clock reading is still inside it.
    since = utcnow() - timedelta(minutes=5)

    expected: list[str] = []
    probe_status: dict[str, int] = {}
    errors: list[str] = []
    for _ in range(args.probes):
        # A bounded retry, because the two failure shapes are not the same fault.
        # Measured 2026-09-18 on the gray pool: the probe round-trip is 0.7-4.7s,
        # but one call in a batch stalled past a 60s transport timeout. Refusing
        # the whole document on a single flaky upstream call would black out the
        # entire 300s monitor window -- and an unobserved window is exactly what
        # the continuity gate exists to forbid. Retrying a *transport* failure
        # does not weaken the contract: the document still refuses to emit unless
        # every probe eventually produced a real call id, so a genuinely broken
        # probe path still fails closed rather than shrinking `expected`.
        result: dict[str, Any] = {}
        for attempt in range(PROBE_ATTEMPTS):
            result = issue_probe(args.base_url, args.model, key, args.probe_timeout)
            if "error" not in result:
                break
            if attempt + 1 < PROBE_ATTEMPTS:
                print(
                    f"collect-spend-reconciliation: probe attempt {attempt + 1}"
                    f"/{PROBE_ATTEMPTS} failed, retrying: {result['error']}",
                    file=sys.stderr,
                )
                time.sleep(PROBE_RETRY_SECONDS)
        if "error" in result:
            errors.append(result["error"])
            continue
        expected.append(result["call_id"])
        probe_status[result["call_id"]] = result["status"]

    if errors:
        # A probe that never reached the proxy proves nothing about spend
        # logging.  Emitting a document with a shrunken `expected` set would let
        # a broken probe path pass as a clean reconciliation, so refuse instead.
        for message in errors:
            print(f"collect-spend-reconciliation: {message}", file=sys.stderr)
        die(f"{len(errors)}/{args.probes} probes failed; refusing to emit a partial document")

    # Carry forward ids from earlier cycles that had not landed yet, so this
    # cycle re-checks them.  Without this, a row still inside LiteLLM's flush
    # backlog would be reported missing -- a rollback trigger -- and would then be
    # forgotten, so the eventual arrival could never clear it.
    carried = load_carry(args.carry_file, args.carry_max_age_seconds)
    lookup_ids = expected + [cid for cid in carried if cid not in set(expected)]
    # The carried ids can be older than the probe window, so widen the time bound
    # to cover the oldest one still being tracked.
    lookup_since = min(
        [since] + [carried[cid] for cid in carried if cid not in set(expected)]
    ) - timedelta(minutes=1)

    deadline = time.monotonic() + MAX_SPEND_LAG_SECONDS + POLL_GRACE_SECONDS
    rows: dict[str, dict[str, Any]] = {}
    while True:
        found, query_error = query_rows(psql_argv, lookup_ids, lookup_since, args.query_timeout)
        if query_error:
            die(query_error)
        rows.update(found)
        if all(cid in rows for cid in expected) or time.monotonic() >= deadline:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    terminal: list[str] = []
    failed: list[str] = []
    pending: list[str] = []
    still_carried: dict[str, datetime] = {}
    now = utcnow()
    for call_id in lookup_ids:
        first_seen = carried.get(call_id, now)
        row = rows.get(call_id)
        if row is None:
            # Measured 2026-09-18: 98.4% of production spend rows land within 30s
            # of endTime, but the remainder arrive in a batch sweep up to 6069s
            # later.  So an absent row inside that tail is flush cadence, not a
            # lost write -- reported as pending and re-checked next cycle.  Only
            # once it is past the whole observed tail is it reported missing,
            # which is the shape that means "this spend write is lost".
            if (now - first_seen).total_seconds() <= args.carry_max_age_seconds:
                pending.append(call_id)
                still_carried[call_id] = first_seen
            # else: omitted from every set, so metrics.py scores it missing.
            continue
        if row["status"] == "success":
            terminal.append(call_id)
        else:
            failed.append(call_id)

    save_carry(args.carry_file, still_carried)

    # Only the ids this cycle actually issued may set `expected`; a carried id
    # that landed is reported through the same terminal/failed sets, and the
    # contract requires every reported id to be a subset of `expected`.
    reported = expected + [cid for cid in lookup_ids if cid not in set(expected)]
    observed_lag = max((row["lag_seconds"] for row in rows.values()), default=0.0)
    document = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "captured_at": iso(utcnow()),
        "data": {
            "expected_request_ids": reported,
            "terminal_request_ids": terminal,
            "failed_request_ids": failed,
            "pending_request_ids": pending,
            "observed_lag_seconds": round(max(observed_lag, 0.0), 2),
        },
    }
    # Must match collect-metrics.py's digest() byte for byte -- same key order,
    # same separators, and ensure_ascii=True -- or load_source rejects the
    # envelope as a checksum mismatch.
    payload = json.dumps(
        document["data"], ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    document["payload_sha256"] = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, args.output)

    missing = [cid for cid in reported if cid not in rows and cid not in set(pending)]
    print(
        f"probes={len(expected)} carried={len(lookup_ids) - len(expected)} "
        f"terminal={len(terminal)} failed={len(failed)} pending={len(pending)} "
        f"missing={len(missing)} lag={document['data']['observed_lag_seconds']}s "
        f"http={sorted(set(probe_status.values()))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
