"""Fixture builders for the `split_capacity` gate, shared by two test modules.

⛔ Deliberately ONE copy of the access-log line format and ONE copy of the
readiness envelope.  The contract tests (test_litellm_gray_audit.py) and the gate
enforcement tests (test_litellm_gray_routing.py) both need production-shaped
inputs, and two copies would drift: the module that was not updated would keep
writing lines the parser no longer matches, the tool would see zero demand, and
zero demand in a capacity gate means infinite headroom -- a green verdict on a
lane nobody measured.  Same reasoning as check-split-capacity.py's load_collector().
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def digest(value: object) -> str:
    """Byte-for-byte the digest check-split-capacity.py computes.

    Written out rather than imported so a change to the tool's canonicalisation
    breaks the tests instead of travelling silently into the evidence format.
    """
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def access_log(
    directory: Path,
    *,
    span_seconds: int = 1800,
    gray_chat_per_second: int = 1,
    stable_chat_per_second: int = 1,
    gray_chat_seconds: float = 0.5,
    stable_chat_seconds: float = 0.5,
    gray_responses_total: int = 0,
    stable_responses_every: int = 0,
    responses_seconds: float = 4.0,
    name: str = "gray.log",
) -> Path:
    """Write an access log in the production `litellm_gray` log_format.

    Rates are per second so a test can state demand directly instead of deriving
    it from a line count, and the two pools are separate knobs because the whole
    point of this gate is that their class mix differs.
    """
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    for offset in range(span_seconds, 0, -1):
        stamp = (now - timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")
        for _ in range(gray_chat_per_second):
            lines.append(
                f"ts={stamp} 10.0.0.1 200 {gray_chat_seconds} pool=canary "
                f"upstream=127.0.0.1:30405 rt={gray_chat_seconds} uri_class=chat sid=-"
            )
        for _ in range(stable_chat_per_second):
            lines.append(
                f"ts={stamp} 10.0.0.2 200 {stable_chat_seconds} pool=stable "
                f"upstream=127.0.0.1:30402 rt={stable_chat_seconds} uri_class=chat sid=-"
            )
        if stable_responses_every and offset % stable_responses_every == 0:
            lines.append(
                f"ts={stamp} 10.0.0.2 200 {responses_seconds} pool=stable "
                f"upstream=127.0.0.1:30402 rt={responses_seconds} uri_class=responses sid=-"
            )
    for index in range(gray_responses_total):
        stamp = (now - timedelta(seconds=100 + index)).isoformat().replace("+00:00", "Z")
        lines.append(
            f"ts={stamp} 10.0.0.1 200 {responses_seconds} pool=canary "
            f"upstream=127.0.0.1:30405 rt={responses_seconds} uri_class=responses sid=-"
        )
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def readiness_envelope(
    directory: Path,
    *,
    ready: int = 3,
    expected: int = 3,
    age_seconds: int = 30,
    name: str = "readiness.json",
) -> Path:
    """Reproduce the envelope gray-monitor-loop.sh writes to raw/readiness.json.

    Checksum over the canonical `data` alone, which is what envelope.py does --
    computing it any other way here would let the tool's integrity check pass on a
    document production never produces.
    """
    observed = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    stamp = observed.isoformat(timespec="seconds").replace("+00:00", "Z")
    data = {
        "lane": "gray",
        "ready_containers": ready,
        "expected_containers": expected,
        "observed_at": stamp,
    }
    envelope = {
        "schema_version": 1,
        "source": "kubectl get pods -l app=litellm-gray .status.containerStatuses[*].ready",
        "captured_at": stamp,
        "data": data,
        "payload_sha256": digest(data),
    }
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path
