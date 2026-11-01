"""198 backend: ssh as ``cltx`` (key auth, no sudo needed for anything this harness does).

The mihomo instance runs as cltx in ``~/mihomo-harness/<name>/`` and listens on 127.0.0.1
only, so it never touches iptables, the k3s CNI, or anything another tenant of 198 can reach.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass

HOST = os.environ.get("MIHOMO_198_HOST", "cltx@10.68.13.198")
JUMP = os.environ.get("MIHOMO_SSH_JUMP", "").strip()
ROOT = "mihomo-harness"  # relative to cltx's home on 198


@dataclass(frozen=True, slots=True)
class RemoteError(Exception):
    message: str
    returncode: int | None = None
    stderr: str = ""

    def __str__(self) -> str:
        return self.message


def _ssh() -> list[str]:
    # ClearAllForwardings: a RemoteForward warning in ~/.ssh/config otherwise lands in stderr
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ClearAllForwardings=yes",
            *(["-J", JUMP] if JUMP else []), HOST]


def _clean(stderr: str) -> str:
    return "\n".join(l for l in stderr.splitlines() if "port forwarding" not in l)


def run(script: str, *, timeout: float = 120, stdin: str | bytes | None = None) -> str:
    """Run a bash script on 198. Returns stdout; raises RemoteError on non-zero exit."""
    cmd = [*_ssh(), f"bash -c {shlex.quote('set -euo pipefail; ' + script)}"]
    binary = isinstance(stdin, bytes)
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=not binary, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RemoteError(f"198 command timed out after {timeout:g}s") from exc
    out = r.stdout.decode("utf-8", "replace") if binary else r.stdout
    err = r.stderr.decode("utf-8", "replace") if binary else r.stderr
    if r.returncode != 0:
        raise RemoteError(f"198 command failed rc={r.returncode}: {script[:120]}", r.returncode, _clean(err)[-1500:])
    return out


_HOME: str | None = None


def home() -> str:
    """cltx's $HOME on 198 (mihomo's controller wants absolute paths)."""
    global _HOME
    if _HOME is None:
        _HOME = run("echo $HOME").strip()
    return _HOME


def put_file(local: str, remote_rel: str, *, mode: str = "600", timeout: float = 300) -> None:
    """Copy a local file to ~/<remote_rel> on 198 with the given mode (via stdin, no scp quirks)."""
    with open(local, "rb") as fh:
        data = fh.read()
    q = shlex.quote(remote_rel)
    run(f"umask 077; mkdir -p \"$(dirname ~/{q})\"; cat > ~/{q}.part; chmod {mode} ~/{q}.part; mv ~/{q}.part ~/{q}",
        stdin=data, timeout=timeout)
