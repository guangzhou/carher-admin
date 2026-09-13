#!/usr/bin/env python3
"""Freeze deployable Helm values from an audited profile and snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NoReturn

import yaml


IDC_REGISTRY_RE = re.compile(r"^127\.0\.0\.1:5000/[a-z0-9._/-]+$")
CIDR_RE = re.compile(
    r"^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"/(3[0-2]|[12]?\d)$"
)
MIN_NODE_CIDR_PREFIX = 24
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
PLACEHOLDER_DIGESTS = {"sha256:" + char * 64 for char in "0123"}
FILL_RE = re.compile(r"(?:FILL-|REPLACE_|Replace this|example only)", re.I)
SENSITIVE_NAME_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|MASTER_?KEY|TOKEN|SECRET|PASSWORD|COOKIE|DATABASE_URL|DSN)(?:$|_)",
    re.I,
)
INLINE_SECRET_RE = re.compile(
    r"(?:Bearer\s+\S+|sk-[A-Za-z0-9._~-]{8,}|postgres(?:ql)?://[^\s'\"]+|"
    r"(?:master_key|api_key|password|token|secret|cookie|database_url|dsn)\s*:\s*(?!os\.environ/)[^\s{}\[\],]+)",
    re.I,
)
SCHEDULER_EVIDENCE_KEYS = {
    "schema_version",
    "profile",
    "mode",
    "image_digest",
    "config_sha256",
    "callbacks_sha256",
    "runtime_sha256",
    "captured_at",
    "source",
    "source_payload_sha256",
    "observations",
}
SCHEDULER_OBSERVATION_KEYS = {
    "duplicate_scheduler_runs",
    "duplicate_background_jobs",
    "unexpected_control_writes",
}
SECRET_METADATA_KEYS = {"name", "uid", "resource_version", "data_sha256"}
MAX_SCHEDULER_EVIDENCE_AGE = timedelta(minutes=15)
CHART_DEFAULTS = Path(__file__).resolve().parents[1] / "chart" / "values.yaml"


def fail(message: str) -> NoReturn:
    raise SystemExit(f"prepare-values: {message}")


def load_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        fail(f"{path} must contain one YAML object")
    return value


def merge_values(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_values(result[key], value)
        else:
            result[key] = value
    return result


def read_text(path: Path, label: str) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.strip() or FILL_RE.search(text):
        fail(f"{label} snapshot is empty or contains a placeholder")
    return text


def reject_inline_credentials(text: str, label: str) -> None:
    if INLINE_SECRET_RE.search(text):
        fail(f"{label} contains an inline credential")


def secure_write_new(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir():
        fail("unsafe output directory")
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        fail("output already exists")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    linked = False
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600, follow_symlinks=False)
        os.link(temporary, path, follow_symlinks=False)
        linked = True
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        if linked:
            try:
                path.unlink()
            except OSError:
                pass
        fail("cannot safely create output")
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def load_deployment(path: Path) -> dict:
    documents = [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]
    deployments = [item for item in documents if item.get("kind") == "Deployment"]
    if len(deployments) != 1:
        fail("deployment snapshot must contain exactly one Deployment")
    return deployments[0]


def main_container(deployment: dict) -> tuple[dict, dict]:
    try:
        pod_spec = deployment["spec"]["template"]["spec"]
    except (KeyError, TypeError):
        fail("deployment snapshot has an invalid pod spec")
    containers = pod_spec.get("containers")
    if not isinstance(containers, list):
        fail("deployment containers must be a list")
    if not all(isinstance(item, dict) for item in containers):
        fail("deployment containers must contain objects")
    names = [item.get("name") for item in containers]
    if len(names) != len(set(names)):
        fail("deployment contains a duplicate container name")
    if len(containers) != 1 or names != ["litellm"]:
        fail("deployment snapshot must contain exactly one container named litellm")
    init_containers = pod_spec.get("initContainers", [])
    if not isinstance(init_containers, list):
        fail("deployment initContainers must be a list")
    if init_containers:
        fail("deployment initContainers are not supported by the frozen chart")
    container = containers[0]
    return pod_spec, container


def load_secret_metadata(path: Path, expected_names: list[str]) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read Secret metadata snapshot: {exc}")
    if not isinstance(payload, list) or len(payload) != len(expected_names):
        fail("Secret metadata snapshot is incomplete")
    result: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != SECRET_METADATA_KEYS:
            fail("Secret metadata snapshot entry is invalid")
        if (
            not isinstance(item.get("name"), str)
            or not isinstance(item.get("uid"), str) or not item["uid"]
            or not isinstance(item.get("resource_version"), str) or not item["resource_version"]
            or not DIGEST_RE.fullmatch(str(item.get("data_sha256", "")))
        ):
            fail("Secret metadata snapshot entry is invalid")
        result.append({key: str(item[key]) for key in sorted(SECRET_METADATA_KEYS)})
    if sorted(item["name"] for item in result) != sorted(expected_names):
        fail("Secret metadata snapshot does not match deployment secretRefs")
    return sorted(result, key=lambda item: item["name"])


def freeze_runtime_shape(values: dict, deployment: dict) -> list[str]:
    pod_spec, container = main_container(deployment)
    extra_env = []
    for item in container.get("env", []):
        name = item.get("name")
        if name == "DISABLE_SCHEMA_UPDATE":
            continue
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
            fail("deployment contains an invalid environment variable name")
        if set(item) == {"name", "value"} and isinstance(item.get("value"), str):
            if SENSITIVE_NAME_RE.search(name) or INLINE_SECRET_RE.search(item["value"]):
                fail(f"environment variable {name} contains an inline credential")
            extra_env.append({"name": name, "value": item["value"]})
            continue
        ref = item.get("valueFrom")
        if isinstance(ref, dict) and len(ref) == 1:
            ref_kind = next(iter(ref))
            source = ref.get(ref_kind)
            if ref_kind in {"secretKeyRef", "configMapKeyRef"} and isinstance(source, dict):
                if isinstance(source.get("name"), str) and isinstance(source.get("key"), str):
                    frozen = {"name": source["name"], "key": source["key"]}
                    if isinstance(source.get("optional"), bool):
                        frozen["optional"] = source["optional"]
                    extra_env.append({"name": name, "valueFrom": {ref_kind: frozen}})
                    continue
        fail(f"environment variable {name} uses an unsupported value source")
    secret_refs = []
    for item in container.get("envFrom", []):
        ref = item.get("secretRef") if isinstance(item, dict) else None
        name = ref.get("name") if isinstance(ref, dict) else None
        if not isinstance(name, str) or not name:
            fail("only named secretRef entries are allowed in envFrom")
        secret_refs.append(name)
    if not secret_refs or len(secret_refs) != len(set(secret_refs)):
        fail("deployment envFrom Secret names must be non-empty and unique")

    values["command"] = container.get("command") or values.get("command")
    values["args"] = container.get("args") or values.get("args")
    lifecycle = container.get("lifecycle")
    if not isinstance(lifecycle, dict) or not lifecycle:
        fail("deployment must define a lifecycle preStop contract")
    values["lifecycle"] = lifecycle
    reject_inline_credentials(
        json.dumps(
            {
                "command": values["command"],
                "args": values["args"],
                "lifecycle": values["lifecycle"],
            }
        ),
        "deployment command",
    )
    values["extraEnv"] = extra_env
    values["secretRefs"] = secret_refs
    values["resources"] = container.get("resources") or values.get("resources")
    for field in ("nodeSelector", "tolerations", "affinity"):
        value = pod_spec.get(field)
        if value is not None:
            values[field] = value
    values["terminationGracePeriodSeconds"] = pod_spec.get(
        "terminationGracePeriodSeconds", values.get("terminationGracePeriodSeconds")
    )
    probe = container.get("readinessProbe", {})
    liveness = container.get("livenessProbe", {})
    fields = ("initialDelaySeconds", "periodSeconds", "failureThreshold", "timeoutSeconds")
    if any(probe.get(key) != liveness.get(key) for key in fields):
        fail("readiness and liveness timing must match the chart probe contract")
    values["probes"] = {key: probe.get(key) for key in fields}
    return secret_refs


def scheduler_safety(
    path: Path | None,
    *,
    profile: str,
    config_text: str,
    callbacks: dict[str, str],
    image_digest: str,
    runtime_sha: str,
) -> dict:
    if path is None:
        if profile != "prod":
            fail("gray and guarded-old profiles require scheduler evidence")
        fail("prod profile requires scheduler evidence binding the primary role")
    payload = load_yaml(path) if path.suffix.lower() in {".yaml", ".yml"} else json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != SCHEDULER_EVIDENCE_KEYS:
        fail("scheduler evidence must be an object")
    expected_mode = "primary" if profile == "prod" else None
    mode = payload.get("mode")
    captured_at = payload.get("captured_at")
    source = payload.get("source")
    observations = payload.get("observations")
    try:
        captured = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
    except ValueError:
        captured = None
    callbacks_text = json.dumps(
        callbacks, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    callbacks_sha = "sha256:" + hashlib.sha256(callbacks_text.encode()).hexdigest()
    if (
        payload.get("schema_version") != 1
        or payload.get("profile") != profile
        or payload.get("image_digest") != image_digest
        or payload.get("callbacks_sha256") != callbacks_sha
        or payload.get("runtime_sha256") != runtime_sha
        or captured is None
        or captured.tzinfo is None
        or not isinstance(source, str)
        or not re.fullmatch(r"(?:clone|direct):[A-Za-z0-9._:-]+", source)
        or not isinstance(observations, dict)
        or set(observations) != SCHEDULER_OBSERVATION_KEYS
        or any(
            not isinstance(observations.get(key), int)
            or isinstance(observations.get(key), bool)
            or observations[key] != 0
            for key in SCHEDULER_OBSERVATION_KEYS
        )
    ):
        fail("scheduler evidence profile/status mismatch")
    captured = captured.astimezone(timezone.utc)
    age = datetime.now(timezone.utc) - captured
    if age < -timedelta(minutes=5) or age > MAX_SCHEDULER_EVIDENCE_AGE:
        fail("scheduler evidence is stale")
    if expected_mode and mode != expected_mode:
        fail("prod scheduler evidence must declare primary mode")
    if not expected_mode and mode != "disabled":
        fail("non-prod scheduler evidence must declare disabled mode")
    config_sha = "sha256:" + hashlib.sha256(config_text.encode()).hexdigest()
    if payload.get("config_sha256") != config_sha:
        fail("scheduler evidence is not bound to the frozen config snapshot")
    source_payload = {
        key: value for key, value in payload.items() if key != "source_payload_sha256"
    }
    source_payload_sha = "sha256:" + hashlib.sha256(
        json.dumps(
            source_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    if payload.get("source_payload_sha256") != source_payload_sha:
        fail("scheduler evidence source payload checksum mismatch")
    evidence_text = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return {
        "mode": mode,
        "evidenceSha256": "sha256:" + hashlib.sha256(evidence_text.encode()).hexdigest(),
        "configSha256": config_sha,
        "callbacksSha256": callbacks_sha,
        "runtimeSha256": runtime_sha,
        "sourcePayloadSha256": source_payload_sha,
        "capturedAt": captured.isoformat().replace("+00:00", "Z"),
        "source": source,
    }


def freeze_ingress_cidrs(values: dict, cidrs: list[str]) -> None:
    """Weld the NodePort reachability contract into the frozen values.

    The host nginx reaches the proxy through the NodePort, so kube-proxy
    presents the node address as the source. A selector-only ingressFrom
    therefore blocks the real traffic path; refuse to freeze without at
    least one node CIDR, and refuse ranges wide enough to mean "anyone".
    """
    seen: list[str] = []
    for cidr in cidrs:
        if not CIDR_RE.fullmatch(cidr):
            fail(f"ingress cidr {cidr!r} is not a dotted-quad IPv4 CIDR")
        prefix = int(cidr.split("/", 1)[1])
        if prefix < MIN_NODE_CIDR_PREFIX:
            fail(
                f"ingress cidr {cidr} is wider than /{MIN_NODE_CIDR_PREFIX}; "
                "list the node addresses explicitly"
            )
        if cidr in seen:
            fail(f"ingress cidr {cidr} is listed twice")
        seen.append(cidr)

    existing = values.get("networkPolicy", {}).get("ingressFrom", [])
    if any("ipBlock" in item for item in existing if isinstance(item, dict)):
        fail("profile already pins an ipBlock; node addresses belong on the command line")
    values["networkPolicy"] = {
        "enabled": True,
        "ingressFrom": list(existing) + [{"ipBlock": {"cidr": cidr}} for cidr in seen],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--callbacks-dir", type=Path, required=True)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--scheduler-evidence", type=Path)
    parser.add_argument("--secret-metadata", type=Path, required=True)
    parser.add_argument(
        "--ingress-cidr",
        action="append",
        required=True,
        metavar="CIDR",
        help=(
            "node address allowed to reach the proxy through the NodePort; "
            "repeat once per node, /24 or narrower"
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not IDC_REGISTRY_RE.fullmatch(args.repository):
        fail("repository must use the verified 198 K3s local registry alias")
    if not DIGEST_RE.fullmatch(args.digest) or args.digest in PLACEHOLDER_DIGESTS:
        fail("digest must be a non-placeholder immutable sha256")
    if not args.callbacks_dir.is_dir():
        fail("callbacks directory does not exist")

    profile_values = load_yaml(args.profile)
    if profile_values.get("artifactTemplate") is not True:
        fail("profile must be a checked-in artifact template")
    values = merge_values(load_yaml(CHART_DEFAULTS), profile_values)
    freeze_ingress_cidrs(values, args.ingress_cidr)

    callbacks = {}
    for path in sorted(args.callbacks_dir.iterdir()):
        if path.is_file() and re.fullmatch(r"[A-Za-z0-9._-]+", path.name):
            callback_text = read_text(path, f"callback {path.name}")
            reject_inline_credentials(callback_text, f"callback {path.name}")
            callbacks[path.name] = callback_text
    if not callbacks:
        fail("at least one callback snapshot is required")

    config_text = read_text(args.config, "config")
    reject_inline_credentials(config_text, "config")
    profile_name = str(values.get("nameOverride", "")).removeprefix("litellm-proxy-")
    if values.get("nameOverride") == "litellm-proxy":
        profile_name = "prod"
    values["artifactTemplate"] = False
    values["allowTemplateRender"] = False
    values["image"] = {
        "repository": args.repository,
        "digest": args.digest,
        "pullPolicy": values.get("image", {}).get("pullPolicy", "IfNotPresent"),
    }
    values["config"] = {"data": {"config.yaml": config_text}}
    values["callbacks"] = {"data": callbacks}
    secret_refs = freeze_runtime_shape(values, load_deployment(args.deployment))
    secret_metadata = load_secret_metadata(args.secret_metadata, secret_refs)
    runtime_payload = {
        "args": values["args"],
        "command": values["command"],
        "extraEnv": values["extraEnv"],
        "secretRefs": values["secretRefs"],
        "secretMetadata": secret_metadata,
    }
    runtime_sha = "sha256:" + hashlib.sha256(
        json.dumps(
            runtime_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    values["schedulerSafety"] = scheduler_safety(
        args.scheduler_evidence,
        profile=profile_name,
        config_text=config_text,
        callbacks=callbacks,
        image_digest=args.digest,
        runtime_sha=runtime_sha,
    )

    rendered = yaml.safe_dump(values, sort_keys=False, allow_unicode=False)
    if FILL_RE.search(rendered):
        fail("rendered values still contain a placeholder")

    secure_write_new(args.output, rendered)
    report = {
        "tool": "prepare-values",
        "status": "PASS",
        "output": str(args.output),
        "sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        "callbacks": len(callbacks),
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
