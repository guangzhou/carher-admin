#!/usr/bin/env python3
"""Build a checksum-bound runtime evidence bundle from exported snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DIRECT_RE = re.compile(r"(?:https?://)?[^\s'\"]*:30402(?P<path>/[^\s'\"]*)")
SECRET_RE = re.compile(r"(?:sk-[A-Za-z0-9._~-]{8,}|Bearer\s+\S+)", re.I)
CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_SOURCE_AGE = timedelta(minutes=15)
MAX_CLOCK_SKEW = timedelta(minutes=5)
SOURCE_KEYS = {"schema_version", "source", "captured_at", "payload_sha256", "data"}


def digest(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def load_source(path: Path, expected: type, *, section: str) -> tuple[Any, dict[str, Any]]:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SystemExit(f"collect-runtime: invalid source envelope for {section}")
    if not isinstance(envelope, dict) or set(envelope) != SOURCE_KEYS or envelope.get("schema_version") != 1:
        raise SystemExit(f"collect-runtime: invalid source envelope for {section}")
    data = envelope.get("data")
    captured_at = parse_timestamp(envelope.get("captured_at"))
    source = envelope.get("source")
    checksum = envelope.get("payload_sha256")
    now = datetime.now(timezone.utc)
    if (
        not isinstance(data, expected)
        or not isinstance(source, str)
        or not source.strip()
        or captured_at is None
        or not isinstance(checksum, str)
        or not CHECKSUM_RE.fullmatch(checksum)
        or checksum != digest(data)
    ):
        raise SystemExit(f"collect-runtime: invalid source envelope for {section}")
    freshness = (now - captured_at).total_seconds()
    if captured_at - now > MAX_CLOCK_SKEW or freshness > MAX_SOURCE_AGE.total_seconds():
        raise SystemExit(f"collect-runtime: stale source evidence for {section}")
    return data, {
        "section": section,
        "source": source,
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "freshness_seconds": max(0, round(freshness, 3)),
        "payload_sha256": checksum,
    }


def direct_references(lines: list[str]) -> list[dict[str, Any]]:
    references = []
    for line_number, line in enumerate(lines, 1):
        if "30402" not in line:
            continue
        if SECRET_RE.search(line):
            raise SystemExit("collect-runtime: bypass inventory contains a credential")
        method = re.search(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD)\b", line, re.I)
        target = DIRECT_RE.search(line)
        if method is None or target is None:
            raise SystemExit(f"collect-runtime: unstructured 30402 line {line_number}")
        references.append(
            {
                "method": method.group(1).upper(),
                "path": target.group("path").split("?", 1)[0],
                "source": f"inventory-line-{line_number}",
                "direct_30402": True,
            }
        )
    if not references:
        raise SystemExit("collect-runtime: bypass inventory is empty")
    return references


def secure_write_new(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise SystemExit("collect-runtime: unsafe output directory")
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        raise SystemExit("collect-runtime: output already exists")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
    except OSError:
        raise SystemExit("collect-runtime: cannot safely create output")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600, follow_symlinks=False)
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--config-checksum", required=True)
    parser.add_argument("--bypass-inventory", type=Path, required=True)
    parser.add_argument("--expected-inventory", type=Path, required=True)
    parser.add_argument("--callbacks", type=Path, required=True)
    parser.add_argument("--api-surfaces", type=Path, required=True)
    parser.add_argument("--acct", type=Path, required=True)
    parser.add_argument("--redis", type=Path, required=True)
    parser.add_argument("--scheduler", type=Path, required=True)
    parser.add_argument("--mutation-visibility", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    expected_inventory, expected_source = load_source(
        args.expected_inventory, dict, section="expected_inventory"
    )
    inventory, inventory_source = load_source(args.bypass_inventory, list, section="references")
    callbacks, callbacks_source = load_source(args.callbacks, list, section="callbacks")
    api_surfaces, api_source = load_source(args.api_surfaces, dict, section="api_surfaces")
    acct, acct_source = load_source(args.acct, dict, section="acct")
    redis, redis_source = load_source(args.redis, dict, section="redis")
    scheduler, scheduler_source = load_source(args.scheduler, dict, section="scheduler")
    mutation, mutation_source = load_source(
        args.mutation_visibility, dict, section="mutation_visibility"
    )
    references = direct_references(inventory)
    inventory_source["payload_sha256"] = digest(references)
    payload: dict[str, Any] = {
        "expected_inventory": expected_inventory,
        "references": references,
        "callbacks": callbacks,
        "api_surfaces": api_surfaces,
        "acct": acct,
        "redis": redis,
        "scheduler": scheduler,
        "mutation_visibility": mutation,
        "sources": [
            expected_source,
            inventory_source,
            callbacks_source,
            api_source,
            acct_source,
            redis_source,
            scheduler_source,
            mutation_source,
        ],
    }
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload_checksum = digest(payload)
    payload["evidence"] = {
        "schema_version": 1,
        "captured_at": captured_at,
        "payload_sha256": payload_checksum,
        "source": "collect-runtime.py",
        "run_id": args.run_id,
        "generation": args.generation,
        "config_checksum": args.config_checksum,
    }
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    secure_write_new(args.output, rendered)
    print(json.dumps({"tool": "collect-runtime", "status": "PASS", "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
