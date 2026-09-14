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
    r"Bearer[ \t]+(?P<bearer>[A-Za-z0-9._~+/-]{8,}=*)"
    r"|(?P<sk>sk-[A-Za-z0-9._~-]{8,})"
    r"|(?P<dsn>postgres(?:ql)?://[^\s'\"]+)"
    r"|(?:master_key|api_key|password|token|secret|cookie|database_url|dsn)"
    r"[ \t]*:[ \t]*(?!os\.environ/)(?![-+0-9][0-9.eE+-]*(?:$|[\s,}\]\)]))"
    r"(?P<pair>[^\s{}\[\],]{8,})",
    re.I,
)
# A value that literally spells out that it is not a value. Measured against
# today's prod snapshots (2026-09-13): the tightened pattern above still fires
# on `cookie: __oailb=<JWT>` (a redaction template in error_sanitize.py) and on
# four `api_key: dummy-local` entries in config.yaml pointing at no-auth local
# endpoints. Neither can be "fixed" -- they are prod's actual bytes -- so
# without this the gate has no disposition path at all and the operator would
# reach for an override flag, which is worse than the exemption.
PLACEHOLDER_VALUE_RE = re.compile(
    r"<[^<>]*>|^(?:dummy|placeholder|changeme|redacted|example|unset|none)[-_.a-z0-9]*$",
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
# The three scheduled jobs a non-prod release can actually be stopped from
# running *through its Pod spec*. Measured 2026-09-14 by reading
# `ProxyStartupEvent.initialize_scheduled_background_jobs` in both the live
# image (v1.90.2, digest 7286aa2d) and the target image (v1.95.0, digest
# 50e647bd) -- the job set is identical in the two versions:
#
#   PROXY_BATCH_POLLING_ENABLED               default "true"  => check_batch_cost_job
#                                                             +  check_responses_cost_job
#   LITELLM_KEY_ROTATION_ENABLED              default "false" => key_rotation_job
#   LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_ENABLED  default "false"
#                                                             => expired UI session cleanup
#
# The last two are already off on prod today, but they are off *by default*,
# which is not the same as off *by contract*: both read a plain env var, and
# both `secretRef`s in the Pod spec are shared with prod, so a Secret edit
# during the window would switch them on in every release at once. Pinning
# them makes the gray release's answer independent of that.
BACKGROUND_TASK_SUPPRESSORS = {
    "LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_ENABLED": "false",
    "LITELLM_KEY_ROTATION_ENABLED": "false",
    "PROXY_BATCH_POLLING_ENABLED": "false",
}
# The fourth global-mutating job, `reset_budget_job`, has no env var at all --
# its only guard is `general_settings.get("disable_reset_budget", False)`, read
# out of config.yaml. So it is suppressed by a one-key overlay on the frozen
# config instead of by an env entry; see disable_reset_budget_in_config().
RESET_BUDGET_KEY = "disable_reset_budget"
GENERAL_SETTINGS_ANCHOR_RE = re.compile(r"^general_settings:[ \t]*$", re.M)
MAX_SCHEDULER_EVIDENCE_AGE = timedelta(minutes=15)
CHART_DEFAULTS = Path(__file__).resolve().parents[1] / "chart" / "values.yaml"


def fail(message: str) -> NoReturn:
    raise SystemExit(f"prepare-values: {message}")


def raw_json(value: object) -> str:
    """Reproduce Helm's `toRawJson` byte-for-byte.

    Any digest this script writes into `schedulerSafety` is re-derived by the
    chart at render time and compared for equality, so the two encoders have to
    agree exactly. Go's `json.Marshal` sorts map keys, emits raw UTF-8, adds no
    trailing newline, and `toRawJson` (unlike `toJson`) does not HTML-escape
    `<`, `>` or `&`.

    The `ensure_ascii` default is the trap. Measured 2026-09-13 against the real
    prod callback snapshot: 30 of the 33 callbacks contain non-ASCII bytes, so
    `ensure_ascii=True` escapes them to `\\uXXXX` and the digest can never equal
    the chart's -- `helm template` fails unconditionally on the real artefact.
    Every fixture in the test suite is pure ASCII, which is exactly why 276
    tests passed while the production values file could not render.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def raw_json_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(raw_json(value).encode("utf-8")).hexdigest()


PLAIN_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")


def is_plain_filesystem_path(value: str) -> bool:
    """True for an absolute POSIX path that cannot itself be a credential.

    `SENSITIVE_NAME_RE` matches on the variable NAME, which is a proxy, not the
    thing being protected. Prod's `CHATGPT_TOKEN_DIR` trips it on `_TOKEN_`
    while holding an absolute directory path — rejecting it is a false positive
    that would have blocked the run on the day (measured 2026-09-13).

    The exemption is deliberately narrow: leading `/`, and only path-safe
    characters, so there is no room for a `Bearer …`, an `sk-…`, a DSN, or a
    URL. The value is still checked against `INLINE_SECRET_RE` first, so this
    can only ever widen the NAME heuristic, never the value check.
    """
    return bool(PLAIN_PATH_RE.fullmatch(value))


def find_inline_credential(text: str) -> str | None:
    """First match that is shaped like a credential and is not a placeholder.

    The old pattern was written for `config.yaml`-shaped key/value config and
    then applied to 537 KB of Python callbacks and a pricing-heavy config.
    Measured against today's prod snapshots (2026-09-13) it fired 388 times,
    every single one a false positive: `token: str` annotations,
    `input_cost_per_token: 0.0000012`, and `f"Bearer {token}"` where `\\S+`
    matched the closing quote. A gate that reds 388 times on the real artefact
    is not a gate -- it trains the operator to route around it.

    Three tightenings, each aimed at one of those shapes: the value must be at
    least 8 characters (`str`, `int`, `0.0` cannot be credentials), it must not
    be numeric, and `name:value` no longer spans a newline, so a Python
    parameter list broken after the colon stops matching the next line. The
    high-precision alternatives (`sk-`, a Postgres DSN) are untouched.
    Re-measured after the change: 388 -> 5.
    """
    for match in INLINE_SECRET_RE.finditer(text):
        value = (
            match.group("bearer")
            or match.group("sk")
            or match.group("dsn")
            or match.group("pair")
        )
        if PLACEHOLDER_VALUE_RE.search(value):
            continue
        return match.group(0)
    return None


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
    """Read a snapshot without letting Python rewrite it.

    `Path.read_text()` opens in text mode, so universal-newline translation
    turns every CRLF into LF. That is a silent content mutation in the one tool
    whose job is to freeze production bytes exactly.

    Measured 2026-09-13: prod's live `litellm-passthrough-streaming-handler-patch`
    holds `streaming_handler.py` with CRLF endings (14016 bytes). Read through
    `read_text()` it came back as 13728 bytes of LF, and the frozen values would
    have rewritten a file that overlays a site-packages module. Python tolerates
    either ending, so nothing would have crashed -- it would just no longer be
    the file that is running.
    """
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        fail(f"{label} snapshot is not valid UTF-8; the chart can only carry text")
    if not text.strip() or FILL_RE.search(text):
        fail(f"{label} snapshot is empty or contains a placeholder")
    return text


def reject_inline_credentials(text: str, label: str) -> None:
    if find_inline_credential(text) is not None:
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


def freeze_runtime_shape(
    values: dict, deployment: dict, grace_override: int | None = None
) -> tuple[list[str], dict]:
    pod_spec, container = main_container(deployment)
    extra_env = []
    for item in container.get("env", []):
        name = item.get("name")
        if name == "DISABLE_SCHEMA_UPDATE":
            continue
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
            fail("deployment contains an invalid environment variable name")
        if set(item) == {"name"}:
            # A bare `{name: X}` is legal Kubernetes and means the empty string.
            # Prod carries two of these (UA_ROUTE_KEY_ALIASES,
            # UA_ROUTE_KEY_PREFIXES). Rejecting them as "unsupported value
            # source" was a gate gap, not a real finding: the empty string
            # cannot be a credential, and dropping the variable instead would
            # silently change the container's environment.
            item = {"name": name, "value": ""}
        if set(item) == {"name", "value"} and isinstance(item.get("value"), str):
            if find_inline_credential(item["value"]) is not None:
                fail(f"environment variable {name} contains an inline credential")
            if SENSITIVE_NAME_RE.search(name) and not is_plain_filesystem_path(item["value"]):
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
    # Grace is the one live field the chart can outright refuse. The chart
    # requires grace >= drain.preStopSeconds + drain.streamDrainSeconds (600 on
    # 198), and prod runs 30 with no preStop at all. Copying 30 through produced
    # a values file that every `helm template` rejects -- fail-closed, but at the
    # wrong moment: at the console during the window, where the only way past it
    # was a `--set` that the frozen artefact does not record.
    #
    # The decision itself was already taken and is not re-litigated here
    # (docs/prepare-values-prod-rehearsal-2026-09-13.md §4: adopting the chart
    # moves the rolling-update truncation floor from >=0.6537% of streams to
    # 0.0940%). What was missing was a way to state it in the artefact. So the
    # operator must name the number, the tool checks it against the chart's own
    # drain budget, and the override lands in the report -- never silently.
    live_grace = pod_spec.get("terminationGracePeriodSeconds")
    drain = values.get("drain") or {}
    drain_budget = int(drain.get("preStopSeconds", 0)) + int(drain.get("streamDrainSeconds", 0))
    if grace_override is None:
        values["terminationGracePeriodSeconds"] = (
            live_grace if live_grace is not None else values.get("terminationGracePeriodSeconds")
        )
    else:
        if grace_override < drain_budget:
            fail(
                f"--termination-grace-seconds {grace_override} is below the chart's "
                f"drain budget {drain_budget}; the chart would reject it at render time"
            )
        values["terminationGracePeriodSeconds"] = grace_override
    applied_grace = values.get("terminationGracePeriodSeconds")
    if applied_grace is not None and int(applied_grace) < drain_budget:
        fail(
            f"live terminationGracePeriodSeconds={applied_grace} is below the chart's "
            f"drain budget {drain_budget}; pass --termination-grace-seconds to state "
            "the adopted value explicitly instead of overriding it at the console"
        )
    grace_report = {"live": live_grace, "applied": applied_grace, "drain_budget": drain_budget}
    probe = container.get("readinessProbe", {})
    liveness = container.get("livenessProbe", {})
    fields = ("initialDelaySeconds", "periodSeconds", "failureThreshold", "timeoutSeconds")
    # readiness and liveness are two independent schedules, not one. Prod runs
    # readiness 60/10/12/5 and liveness 180/30/10/8 (measured 2026-09-13);
    # forcing them equal would have been a real change to a live probe, with no
    # safety argument behind it — k8s separates them on purpose.
    for label, source in (("readiness", probe), ("liveness", liveness)):
        missing = [key for key in fields if not isinstance(source.get(key), int)]
        if missing:
            fail(f"deployment {label} probe is missing timing fields: {', '.join(missing)}")
    values["probes"] = {
        "readiness": {key: probe[key] for key in fields},
        "liveness": {key: liveness[key] for key in fields},
    }
    return secret_refs, grace_report


CONFIG_MOUNT_PATH = "/app/config.yaml"
CONFIG_SUB_PATH = "config.yaml"
SNAPSHOT_KEY_RE = re.compile(r"[A-Za-z0-9._-]+")
SNAPSHOT_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")


def read_snapshot_dir(directory: Path, label: str) -> dict[str, str]:
    if not directory.is_dir():
        fail(f"{label} snapshot directory does not exist: {directory}")
    data: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and SNAPSHOT_KEY_RE.fullmatch(path.name):
            text = read_text(path, f"{label} {path.name}")
            reject_inline_credentials(text, f"{label} {path.name}")
            data[path.name] = text
    if not data:
        fail(f"{label} snapshot directory has no usable files")
    return data


def freeze_mount_shape(
    values: dict,
    deployment: dict,
    snapshot_dirs: dict[str, Path],
    callbacks_volume: str,
) -> dict:
    """Freeze the container's mount shape from the live Deployment.

    The mount list is where prod's capability actually lives -- 40 volumeMounts
    on 2026-09-13, 38 of them single-file `subPath` overlays, 4 of those landing
    on top of upstream library files under site-packages. None of the other
    gates can see it: the deletion-set gate compares objects, `rollout status`
    and the probes all read green while every runtime patch quietly fails to be
    mounted at all.

    Content still comes from the audited snapshot directories, never from the
    live cluster, so nothing unreviewed can be frozen into the values. Only the
    *shape* -- which key lands on which path, and which volume carries it -- is
    read off the live object, because transcribing 40 rows by hand is itself a
    silent-failure mechanism.

    Everything the chart cannot express is a hard failure here rather than a
    dropped mount: an unmanaged volume source, a volume with no audited
    snapshot, a `subPath` that is not a key in its data. `readOnly` is not
    frozen -- the chart always mounts read-only -- but every mount that widens
    is reported, so it reaches the 2c approval instead of disappearing.
    """
    pod_spec, container = main_container(deployment)
    mounts = container.get("volumeMounts") or []
    if not isinstance(mounts, list) or not all(isinstance(item, dict) for item in mounts):
        fail("deployment volumeMounts must be a list of objects")
    if not mounts:
        if snapshot_dirs:
            fail("--snapshot was supplied but the deployment snapshot mounts nothing")
        return {"mounts": 0, "snapshots": 0, "readonly_widened": []}

    volume_sources: dict[str, str | None] = {}
    for volume in pod_spec.get("volumes") or []:
        if not isinstance(volume, dict) or not isinstance(volume.get("name"), str):
            fail("deployment volumes must be named objects")
        kinds = sorted(key for key in volume if key != "name")
        if len(kinds) != 1:
            fail(f"volume {volume['name']} must declare exactly one source")
        source = volume[kinds[0]]
        volume_sources[volume["name"]] = (
            source.get("name") if kinds[0] == "configMap" and isinstance(source, dict) else None
        )
        if volume_sources[volume["name"]] is None:
            volume_sources[volume["name"]] = f"!{kinds[0]}"

    grouped: dict[str, list[tuple[str, str | None, bool]]] = {}
    seen_paths: dict[str, str] = {}
    for mount in mounts:
        name = mount.get("name")
        mount_path = mount.get("mountPath")
        if not isinstance(name, str) or name not in volume_sources:
            fail(f"volumeMount references an undeclared volume: {name!r}")
        if not isinstance(mount_path, str) or not mount_path.startswith("/"):
            fail(f"volumeMount on volume {name} has no absolute mountPath")
        if mount_path in seen_paths:
            fail(f"two volumeMounts share mountPath {mount_path}; the kubelet picks one silently")
        seen_paths[mount_path] = name
        sub_path = mount.get("subPath")
        if sub_path is not None and (
            not isinstance(sub_path, str) or not SNAPSHOT_KEY_RE.fullmatch(sub_path)
        ):
            fail(f"volumeMount {mount_path} has an unsupported subPath")
        grouped.setdefault(name, []).append((mount_path, sub_path, bool(mount.get("readOnly"))))

    config_volume = seen_paths.get(CONFIG_MOUNT_PATH)
    if config_volume is None:
        fail(f"deployment snapshot does not mount the LiteLLM config at {CONFIG_MOUNT_PATH}")
    config_mounts = grouped.pop(config_volume)
    if len(config_mounts) != 1 or config_mounts[0][1] != CONFIG_SUB_PATH:
        fail(
            f"volume {config_volume} must carry exactly one mount: "
            f"{CONFIG_MOUNT_PATH} subPath {CONFIG_SUB_PATH}"
        )

    widened = [
        mount_path
        for entries in grouped.values()
        for mount_path, _, read_only in entries
        if not read_only
    ]

    callbacks_data = values["callbacks"]["data"]
    mount_paths: dict[str, str] = {}
    mounted_callbacks: set[str] = set()
    for mount_path, sub_path, _ in grouped.pop(callbacks_volume, []):
        if sub_path is None:
            fail(
                f"volume {callbacks_volume} mounts {mount_path} as a whole directory; "
                "the chart mounts callbacks one file at a time"
            )
        mounted_callbacks.add(sub_path)
        if sub_path not in callbacks_data:
            fail(f"callbacks volume mounts {sub_path}, which the audited callbacks directory lacks")
        if mount_path != f"/app/{sub_path}":
            mount_paths[sub_path] = mount_path
    # An audited callback that prod does not mount is the silent shape in the
    # other direction: it renders into the chart's ConfigMap and lands on
    # /app/<key>, which is a mount prod does not have today.
    if mounted_callbacks and mounted_callbacks != set(callbacks_data):
        missing = sorted(set(callbacks_data) - mounted_callbacks)
        fail(
            "audited callbacks the deployment does not mount: " + ", ".join(missing)
        )

    snapshots = []
    for name in sorted(grouped):
        entries = grouped[name]
        if not SNAPSHOT_NAME_RE.fullmatch(name):
            fail(f"volume {name} is not a usable snapshot name")
        source = volume_sources[name]
        if source.startswith("!"):
            fail(
                f"volume {name} is backed by {source[1:]}, which the chart cannot express; "
                "adopting the chart would drop it"
            )
        if len(entries) != 1:
            fail(
                f"volume {name} has {len(entries)} mounts; the chart expresses one mountPath "
                "per snapshot, so split it into that many snapshot entries"
            )
        directory = snapshot_dirs.pop(name, None)
        if directory is None:
            fail(f"volume {name} is mounted but has no --snapshot {name}=<dir> content snapshot")
        data = read_snapshot_dir(directory, f"snapshot {name}")
        mount_path, sub_path, _ = entries[0]
        if sub_path is not None and sub_path not in data:
            fail(
                f"volume {name} mounts subPath {sub_path}, which its snapshot directory lacks; "
                "the kubelet would mount an empty path over the target"
            )
        entry = {"name": name, "mountPath": mount_path, "data": data}
        if sub_path is not None:
            entry["subPath"] = sub_path
        snapshots.append(entry)
    if snapshot_dirs:
        fail(
            "--snapshot named volumes the deployment does not mount: "
            + ", ".join(sorted(snapshot_dirs))
        )

    if mount_paths:
        values["callbacks"]["mountPaths"] = dict(sorted(mount_paths.items()))
    values["additionalSnapshots"] = snapshots
    return {
        "mounts": len(mounts),
        "snapshots": len(snapshots),
        "readonly_widened": sorted(widened),
    }


def shadow_chart_default_keys(values: dict) -> list[str]:
    """Delete chart-default content keys this run did not freeze.

    Helm merges a user values file *over* the chart defaults, and for maps that
    merge is **additive**: a key present only in `chart/values.yaml` survives
    into the render even though the frozen values file never mentions it.

    Measured 2026-09-13 against the real prod snapshot: a values file carrying
    prod's 33 callbacks rendered a callbacks ConfigMap with **34** keys, because
    `chart/values.yaml` ships a placeholder `callbacks.data["README.txt"]`. The
    chart mounts every callbacks key, so that placeholder would have been
    mounted into the production container. Writing `README.txt: null` in the
    user file deletes it (measured: 33 keys, README absent).

    Today the stray key also breaks `callbacksSha256`, so it happens to fail
    loudly -- but that is a side effect of a checksum that exists for another
    reason. Any chart default the checksums do not cover rides in silently, so
    the deletion is done here rather than left to the checksum.

    Returns the deleted `section.key` names for the run report.
    """
    defaults = load_yaml(CHART_DEFAULTS)
    shadowed: list[str] = []
    for section in ("config", "callbacks"):
        default_data = (defaults.get(section) or {}).get("data")
        frozen_data = (values.get(section) or {}).get("data")
        if not isinstance(default_data, dict) or not isinstance(frozen_data, dict):
            continue
        for key in sorted(default_data):
            if key not in frozen_data:
                frozen_data[key] = None
                shadowed.append(f"{section}.{key}")
    return shadowed


def suppress_background_tasks(values: dict, *, profile: str) -> list[str]:
    """Make `backgroundTasks.enabled: false` a fact about the Pod spec.

    It was a label. Measured 2026-09-14: rendering the gray profile produced a
    container whose entire env was `DISABLE_SCHEMA_UPDATE=True` plus prod's
    frozen env, and whose only trace of the setting was the Pod label
    `litellm.carher.io/background-tasks-enabled: "false"`. Nothing in that Pod
    spec stops APScheduler; the sole guard on the whole job set is
    `prisma_client is not None`, and gray shares prod's DATABASE_URL. So the
    gray replicas would have run `reset_budget`, `check_batch_cost` and
    `check_responses_cost` against the production database -- at a different
    LiteLLM version than the release that owns them -- while the chart, the
    label and the runbook all said background tasks were off.

    That is the gate-property-in-prose shape: the sentence "gray does not run
    background tasks" existed only in the sentence.

    Returns the names this call pinned, for the run report. prod is asserted,
    never rewritten: if the live prod Pod already pins a suppressor to "false"
    then prod is not the release running that job, and the premise that prod
    owns the scheduler is wrong -- that is a red, not something to paper over.
    """
    extra_env = values.get("extraEnv")
    if not isinstance(extra_env, list):
        fail("extraEnv must be frozen before background tasks can be pinned")
    existing = {
        entry.get("name"): entry
        for entry in extra_env
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    if profile == "prod":
        for name, off in sorted(BACKGROUND_TASK_SUPPRESSORS.items()):
            entry = existing.get(name)
            if entry is not None and str(entry.get("value", "")).lower() == off:
                fail(
                    f"prod pins {name}={off}; prod cannot both own the scheduler "
                    "and have that job disabled"
                )
        return []
    pinned: list[str] = []
    for name, off in sorted(BACKGROUND_TASK_SUPPRESSORS.items()):
        entry = existing.get(name)
        if entry is not None and str(entry.get("value", "")).lower() == off:
            continue
        if entry is not None:
            # Prod set it to something else, or sourced it from a Secret. Drop
            # that entry and pin the literal; leaving both would render two env
            # entries with the same name, where the kubelet keeps the last one
            # silently.
            extra_env.remove(entry)
        extra_env.append({"name": name, "value": off})
        pinned.append(name)
    # Appended, never re-sorted. The rest of extraEnv is prod's live order, and
    # gate 2c diffs this Pod spec against prod's -- a wholesale reorder would
    # turn one honest three-entry diff into a whole-block diff that no approval
    # list can usefully name.
    return pinned


def disable_reset_budget_in_config(config_text: str, *, profile: str) -> tuple[str, bool]:
    """Overlay `general_settings.disable_reset_budget: true` for non-prod releases.

    `reset_budget_job` is the one global-mutating scheduled job with no env
    var: `initialize_scheduled_background_jobs` guards it with
    `general_settings.get("disable_reset_budget", False) is False` and nothing
    else. Measured on prod's live `litellm-config` ConfigMap 2026-09-14: the
    key is absent, so the job is scheduled -- today on all four prod replicas,
    and, without this overlay, on the gray replicas too.

    The edit is a single inserted line rather than a YAML round-trip. Re-dumping
    a 500-model config would rewrite every line of the frozen snapshot, and the
    whole point of freezing it is that the diff against prod is readable. The
    anchor must occur exactly once at column 0; a non-unique anchor is a red,
    not a "take the first one".

    Returns (text, changed). prod is checked, not edited: prod carrying the key
    would mean nobody resets budgets once gray is also muted.
    """
    parsed = yaml.safe_load(config_text)
    if not isinstance(parsed, dict):
        fail("config.yaml must be a mapping")
    general = parsed.get("general_settings")
    if general is not None and not isinstance(general, dict):
        fail("config.yaml general_settings must be a mapping")
    already = general.get(RESET_BUDGET_KEY) if general else None
    if profile == "prod":
        if already is True:
            fail(
                f"prod config sets general_settings.{RESET_BUDGET_KEY}; prod owns "
                "reset_budget_job and cannot have it disabled"
            )
        return config_text, False
    if already is True:
        return config_text, False
    if already is not None:
        fail(
            f"config.yaml sets general_settings.{RESET_BUDGET_KEY}={already!r}; "
            "refusing to overwrite an explicit value"
        )
    anchors = GENERAL_SETTINGS_ANCHOR_RE.findall(config_text)
    if general is None and not anchors:
        # No block to overlay: append one. Only reachable on a config that has
        # no general_settings at all, which prod's does not.
        suffix = "" if config_text.endswith("\n") or not config_text else "\n"
        overlaid = f"{config_text}{suffix}general_settings:\n  {RESET_BUDGET_KEY}: true\n"

    else:
        if len(anchors) != 1:
            fail(
                "config.yaml must contain exactly one top-level `general_settings:` "
                f"line to overlay, found {len(anchors)}"
            )
        match = GENERAL_SETTINGS_ANCHOR_RE.search(config_text)
        assert match is not None
        insert_at = match.end() + 1
        indent = "  "
        for line in config_text[insert_at:].splitlines():
            if not line.strip():
                continue
            leading = line[: len(line) - len(line.lstrip(" "))]
            if leading:
                indent = leading
            break
        overlaid = (
            config_text[:insert_at]
            + f"{indent}{RESET_BUDGET_KEY}: true\n"
            + config_text[insert_at:]
        )
    # The insertion is textual, so prove semantically that it changed exactly
    # one key and nothing else. A wrong indent would silently land the key in a
    # nested mapping, or end the block -- both parse fine and both read green.
    after = yaml.safe_load(overlaid)
    expected = json.loads(json.dumps(parsed, default=str))
    expected.setdefault("general_settings", {})
    if expected["general_settings"] is None:
        expected["general_settings"] = {}
    expected["general_settings"][RESET_BUDGET_KEY] = True
    if json.loads(json.dumps(after, default=str)) != expected:
        fail("reset_budget overlay changed more than one key; refusing to freeze")
    return overlaid, True


def scheduler_safety(
    path: Path | None,
    *,
    profile: str,
    config_text: str,
    callbacks: dict[str, str],
    image_digest: str,
    runtime_sha: str,
    secret_metadata_sha: str,
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
    callbacks_sha = raw_json_sha256(callbacks)
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
    # One encoder for every JSON digest in this file. Two of them are re-derived
    # by the chart and must match Helm byte-for-byte; the rest are free, but a
    # per-call-site encoding choice is exactly how the callbacks digest ended up
    # unable to ever match. No choice, no drift.
    source_payload_sha = raw_json_sha256(source_payload)
    if payload.get("source_payload_sha256") != source_payload_sha:
        fail("scheduler evidence source payload checksum mismatch")
    return {
        "mode": mode,
        "evidenceSha256": raw_json_sha256(payload),
        "configSha256": config_sha,
        "callbacksSha256": callbacks_sha,
        "runtimeSha256": runtime_sha,
        # The chart recomputes runtimeSha256 from {args,command,extraEnv,
        # secretRefs} and refuses to render on mismatch, so that digest cannot
        # carry anything the chart does not have. The Secret snapshot (uid +
        # resourceVersion + per-Secret data digest) is exactly such a thing, and
        # it used to be folded into runtimeSha256 -- which is why the real
        # values file could never render. It gets its own recorded field here
        # instead: the chart cannot verify it, but dropping it outright would
        # have made `--secret-metadata` decorative and unbound the frozen values
        # from the Secret revision they were audited against.
        "secretMetadataSha256": secret_metadata_sha,
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
    parser.add_argument(
        "--snapshot",
        action="append",
        default=[],
        metavar="VOLUME=DIR",
        help=(
            "audited content for one ConfigMap-backed volume the deployment "
            "mounts besides config and callbacks (prod 2026-09-13: hooks, "
            "chatgpt-noauth, deepcopy-patch and the three site-packages "
            "overlays). Every such volume needs one, or the run fails rather "
            "than adopting a chart that silently drops the mount"
        ),
    )
    parser.add_argument(
        "--callbacks-volume",
        default="callbacks",
        help="name of the live volume carrying the audited callbacks directory",
    )
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--scheduler-evidence", type=Path)
    parser.add_argument("--secret-metadata", type=Path, required=True)
    parser.add_argument(
        "--ingress-cidr",
        action="append",
        required=True,
        metavar="CIDR",
        help=(
            "SNAT source the pod actually sees for NodePort traffic; repeat "
            "once per FORWARDING PATH, not per node (198 measured 2026-09-13: "
            "10.42.0.0/32 flannel.1 cross-node + 10.42.0.1/32 cni0 same-node; "
            "the node business IP never appears). /24 or narrower"
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--termination-grace-seconds",
        type=int,
        help=(
            "adopt this terminationGracePeriodSeconds instead of the live "
            "Deployment's. Needed because prod runs 30 with no preStop while the "
            "chart requires grace >= drain.preStopSeconds + drain.streamDrainSeconds "
            "(600 on 198); without it the frozen values file cannot render at all. "
            "The number is stated here, not at the console with --set, so it lands "
            "in the artefact and in this tool's report"
        ),
    )
    parser.add_argument(
        "--emit-bindings",
        action="store_true",
        help=(
            "compute the artefact digests that scheduler evidence must carry "
            "and print them instead of writing values. Needed because those "
            "digests are not hand-computable: the runtime one is Helm's own "
            "toRawJson over four post-merge values, and the callbacks one "
            "covers 33 files. Writes nothing and does not touch --output"
        ),
    )
    args = parser.parse_args()
    # --output stays optional in the parser only so --emit-bindings can omit
    # it; every path that writes a values file still demands it.
    if args.output is None and not args.emit_bindings:
        parser.error("the following arguments are required: --output")

    if not IDC_REGISTRY_RE.fullmatch(args.repository):
        fail("repository must use the verified 198 K3s local registry alias")
    if not DIGEST_RE.fullmatch(args.digest) or args.digest in PLACEHOLDER_DIGESTS:
        fail("digest must be a non-placeholder immutable sha256")
    if not args.callbacks_dir.is_dir():
        fail("callbacks directory does not exist")
    snapshot_dirs: dict[str, Path] = {}
    for item in args.snapshot:
        volume, separator, directory = str(item).partition("=")
        if not separator or not volume or not directory:
            fail(f"--snapshot must be VOLUME=DIR, got {item!r}")
        if volume in snapshot_dirs:
            fail(f"--snapshot given twice for volume {volume}")
        snapshot_dirs[volume] = Path(directory)

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
    # Before values["config"] and config_sha, or the frozen snapshot and the
    # digest the chart re-derives would describe two different files.
    config_text, reset_budget_overlaid = disable_reset_budget_in_config(
        config_text, profile=profile_name
    )
    values["config"] = {"data": {"config.yaml": config_text}}
    # Copy, don't alias: shadow_chart_default_keys() adds null keys to
    # values["callbacks"]["data"], and sharing the object with `callbacks`
    # would poison both the run report's count and the digest.
    values["callbacks"] = {"data": dict(callbacks)}
    deployment = load_deployment(args.deployment)
    secret_refs, grace_report = freeze_runtime_shape(
        values, deployment, args.termination_grace_seconds
    )
    # After freeze_runtime_shape (which sets extraEnv wholesale from the live
    # Pod) and before runtime_sha, which the chart re-derives from extraEnv.
    pinned_env = suppress_background_tasks(values, profile=profile_name)
    mount_report = freeze_mount_shape(
        values, deployment, snapshot_dirs, args.callbacks_volume
    )
    secret_metadata = load_secret_metadata(args.secret_metadata, secret_refs)
    # Exactly the four keys `litellm-proxy.runtimeChecksum` digests, in Helm's
    # own encoding. Anything extra here renders the chart unbuildable; see the
    # secretMetadataSha256 comment in scheduler_safety().
    runtime_sha = raw_json_sha256(
        {
            "args": values["args"],
            "command": values["command"],
            "extraEnv": values["extraEnv"],
            "secretRefs": values["secretRefs"],
        }
    )
    config_sha = "sha256:" + hashlib.sha256(config_text.encode("utf-8")).hexdigest()
    if args.emit_bindings:
        print(
            json.dumps(
                {
                    "tool": "prepare-values",
                    "mode": "emit-bindings",
                    "profile": profile_name,
                    "image_digest": args.digest,
                    "config_sha256": config_sha,
                    "callbacks_sha256": raw_json_sha256(callbacks),
                    "runtime_sha256": runtime_sha,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0

    values["schedulerSafety"] = scheduler_safety(
        args.scheduler_evidence,
        profile=profile_name,
        config_text=config_text,
        callbacks=callbacks,
        image_digest=args.digest,
        runtime_sha=runtime_sha,
        secret_metadata_sha=raw_json_sha256(secret_metadata),
    )

    shadowed = shadow_chart_default_keys(values)

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
        "mounts": mount_report["mounts"],
        "snapshots": mount_report["snapshots"],
        # Mounts that prod declares read-write and the chart mounts read-only.
        # Measured 2026-09-13 inside the live pod: all 40 mounts already show
        # `ro` in /proc/mounts regardless of the spec flag, so this is a spec
        # field change with no observed behaviour change -- but it is still a
        # change, so it is reported here and must be signed off at gate 2c
        # rather than quietly normalised away.
        "readonly_widened": mount_report["readonly_widened"],
        # Chart-default content keys this run explicitly deleted. Non-empty is
        # normal, not a warning: chart/values.yaml ships placeholder content and
        # a frozen run must not inherit any of it.
        "shadowed_chart_keys": shadowed,
        # Live vs adopted grace, always reported so a 30 -> 600 adoption can
        # never happen without a line in the run record saying so.
        "termination_grace": grace_report,
        # What `backgroundTasks.enabled: false` actually did to this Pod spec.
        # Empty lists on a non-prod profile mean the frozen input already
        # carried the suppression; they must never be read as "nothing to
        # suppress" -- that was the old, silent behaviour.
        "background_tasks": {
            "enabled": profile_name == "prod",
            "pinned_env": pinned_env,
            "reset_budget_overlaid": reset_budget_overlaid,
        },
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
