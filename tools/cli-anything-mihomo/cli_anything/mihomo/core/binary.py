"""The real mihomo binary: fetch a release (GitHub via 188, digest-checked) and place it on 198.

198 cannot reach github.com release assets reliably, so the .gz is downloaded locally through
the 188 SOCKS tunnel, verified against the sha256 digest the GitHub release API publishes,
then streamed to 198 where the sha256 is checked again before gunzip.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess

from cli_anything.mihomo.core import subscription
from cli_anything.mihomo.utils import remote198

API = "https://api.github.com/repos/MetaCubeX/mihomo/releases"
ARCH = "linux-amd64"  # 198 is x86_64 (uname -m measured 2026-10-28)
BIN_DIR = f"{remote198.ROOT}/bin"


class BinaryError(RuntimeError):
    pass


def _curl_json(url: str) -> object:
    cmd = ["curl", "-sS", "-m", "30", "-x", subscription.FETCH_PROXY, url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    if r.returncode != 0:
        raise BinaryError(f"GitHub API via {subscription.FETCH_PROXY}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def release(version: str = "latest") -> dict[str, str]:
    """{"version", "asset", "url", "sha256"} for the plain linux-amd64 .gz (not -v1/-v2/-compatible)."""
    rel = _curl_json(f"{API}/latest" if version == "latest" else f"{API}/tags/{version}")
    if not isinstance(rel, dict) or "tag_name" not in rel:
        raise BinaryError(f"no release {version!r}: {str(rel)[:200]}")
    tag = rel["tag_name"]
    want = f"mihomo-{ARCH}-{tag}.gz"
    for a in rel.get("assets", []):
        if a["name"] == want:
            digest = str(a.get("digest") or "")
            if not digest.startswith("sha256:"):
                raise BinaryError(f"{want}: release API gave no sha256 digest")
            return {"version": tag, "asset": want, "url": a["browser_download_url"], "sha256": digest[7:]}
    raise BinaryError(f"{tag} has no asset {want}")


def local_gz(version: str) -> str:
    return os.path.join(subscription.BASE_DIR, f"mihomo-{ARCH}-{version}.gz")  # shared by all accounts


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(version: str = "latest") -> dict[str, str]:
    rel = release(version)
    path = local_gz(rel["version"])
    if not (os.path.exists(path) and sha256_file(path) == rel["sha256"]):
        os.makedirs(subscription.BASE_DIR, mode=0o700, exist_ok=True)
        r = subprocess.run(["curl", "-sSL", "-m", "300", "-x", subscription.FETCH_PROXY, "-o", path + ".part", rel["url"]],
                           capture_output=True, text=True, timeout=320)
        if r.returncode != 0:
            raise BinaryError(f"download {rel['asset']}: {r.stderr.strip()[:200]}")
        got = sha256_file(path + ".part")
        if got != rel["sha256"]:
            os.remove(path + ".part")
            raise BinaryError(f"{rel['asset']}: sha256 {got} != release digest {rel['sha256']}")
        os.replace(path + ".part", path)
    return {**rel, "local": path}


def remote_path(version: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise BinaryError(f"bad version {version!r}")
    return f"{BIN_DIR}/mihomo-{version}"


def install(version: str) -> dict[str, str]:
    """Put the verified binary on 198 at ~/mihomo-harness/bin/mihomo-<version>. Idempotent."""
    path = local_gz(version)
    if not os.path.exists(path):
        raise BinaryError(f"{path} missing; run `cli-anything-mihomo binary fetch --version {version}`")
    sha = sha256_file(path)
    dst = remote_path(version)
    out = remote198.run(f"test -x ~/{dst} && ~/{dst} -v | head -1 || true").strip()
    if version not in out:
        gz = f"{BIN_DIR}/.mihomo-{version}.gz"
        remote198.put_file(path, gz)
        remote198.run(
            f"cd ~/{BIN_DIR}; echo {shlex.quote(sha + '  .mihomo-' + version + '.gz')} | sha256sum -c --quiet; "
            f"gunzip -c .mihomo-{version}.gz > .mihomo-{version}.part; chmod 700 .mihomo-{version}.part; "
            f"mv .mihomo-{version}.part mihomo-{version}; rm -f .mihomo-{version}.gz")
        out = remote198.run(f"~/{dst} -v | head -1").strip()
    return {"version": version, "remote": f"~/{dst}", "gz_sha256": sha, "reports": out}


def installed() -> list[str]:
    out = remote198.run(f"ls ~/{BIN_DIR} 2>/dev/null | grep -E '^mihomo-v[0-9.]+$' || true")
    return sorted(l[len('mihomo-'):] for l in out.split())
