#!/usr/bin/env python3
"""Bind a runner result to the Kubernetes Job/Pod that actually produced it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(
    r"^(?:docker-pullable|containerd|cri-o)://"
    r"127\.0\.0\.1:5000/"
    r"[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$"
)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def fail(message: str) -> NoReturn:
    raise SystemExit(f"collect-job-attestation: {message}")


def digest(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def load(path: Path, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            fail(f"{label} must be a regular non-symlink file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read {label}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must contain one JSON object")
    return value


def secure_write_new(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        fail("output already exists")
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        os.chmod(path, 0o600, follow_symlinks=False)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True, help="kubectl get job -o json")
    parser.add_argument("--pod", type=Path, required=True, help="kubectl get pod -o json")
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--runner-config-name", required=True)
    parser.add_argument("--runner-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    job = load(args.job, "Job snapshot")
    pod = load(args.pod, "Pod snapshot")
    result = load(args.result, "runner result")
    result_sha = result.get("result_sha256")
    if not isinstance(result_sha, str) or result_sha != digest({k: v for k, v in result.items() if k != "result_sha256"}):
        fail("runner result checksum mismatch")
    binding = result.get("binding")
    if not isinstance(binding, dict) or not CHECKSUM_RE.fullmatch(str(binding.get("db_target_sha256", ""))):
        fail("runner result binding is incomplete")
    try:
        job_name = job["metadata"]["name"]
        job_uid = job["metadata"]["uid"]
        pod_uid = pod["metadata"]["uid"]
        owner_uids = {item["uid"] for item in pod["metadata"].get("ownerReferences", []) if item.get("kind") == "Job"}
        statuses = {item["name"]: item for item in pod["status"]["containerStatuses"]}
        status = statuses[args.container]
        image_id = status["imageID"]
    except (KeyError, TypeError):
        fail("Job or Pod snapshot is incomplete")
    if not all(isinstance(value, str) and ID_RE.fullmatch(value) for value in (job_name, job_uid, pod_uid)):
        fail("Job or Pod identity is invalid")
    if job_uid not in owner_uids or binding.get("pod_uid") != pod_uid:
        fail("Pod ownership or result Pod UID mismatch")
    if not IMAGE_ID_RE.fullmatch(str(image_id)):
        fail("container imageID is not an immutable 198 K3s local-registry image")
    image_digest = "sha256:" + image_id.rsplit("@sha256:", 1)[1]
    if image_digest != binding.get("image_digest"):
        fail("container imageID does not match runner result")
    if not CHECKSUM_RE.fullmatch(args.runner_sha256) or args.runner_sha256 != binding.get("runner_sha256"):
        fail("runner checksum does not match runner result")
    if not ID_RE.fullmatch(args.runner_config_name):
        fail("runner ConfigMap name is invalid")
    payload = {
        "tool": "collect-job-attestation",
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "job_name": job_name,
        "job_uid": job_uid,
        "pod_uid": pod_uid,
        "container": args.container,
        "container_image_id": image_id,
        "runner_config_name": args.runner_config_name,
        "runner_sha256": args.runner_sha256,
        "db_identity": binding.get("db_identity"),
        "db_target_sha256": binding.get("db_target_sha256"),
        "run_id": binding.get("run_id"),
        "generation": binding.get("generation"),
        "result_sha256": result_sha,
    }
    payload["payload_sha256"] = digest(payload)
    secure_write_new(args.output, payload)
    print(json.dumps({"tool": "collect-job-attestation", "status": "PASS", "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
