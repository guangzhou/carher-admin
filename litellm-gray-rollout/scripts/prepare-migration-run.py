#!/usr/bin/env python3
"""Freeze migration Jobs around repository-owned, fail-closed runners."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

import yaml

from importlib.util import module_from_spec, spec_from_file_location


IDC_IMAGE_RE = re.compile(
    r"^127\.0\.0\.1:5000/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$"
)
PLACEHOLDER_RE = re.compile(r"(?:replace with|exit\s+64|sha256:[0-3]{64})", re.I)
CHECKSUM_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTRACT_SPEC = spec_from_file_location(
    "litellm_gray_migration_contract", Path(__file__).with_name("migration-contract.py")
)
if _CONTRACT_SPEC is None or _CONTRACT_SPEC.loader is None:
    raise SystemExit("prepare-migration-run: cannot load migration contract")
_CONTRACT = module_from_spec(_CONTRACT_SPEC)
_CONTRACT_SPEC.loader.exec_module(_CONTRACT)
validate_add_column = _CONTRACT.validate_add_column
JOB_MODES = {
    "litellm-new-version-test": "new",
    "litellm-old-version-test": "old",
    "litellm-concurrent-new-test": "concurrent-new",
    "litellm-concurrent-old-test": "concurrent-old",
}
MIGRATION_JOBS = {
    "clone": "litellm-clone-schema-migration-template",
    "prod": "litellm-production-schema-migration-template",
}
RUNNER_FILES = (
    "compatibility-config.yaml",
    "compatibility-config-suppressed.yaml",
    "migration-contract.py",
    "migration-ledger-runner.py",
    "compatibility-runner.py",
)
# Where the three suppressor names come from. Importing the constant rather than
# retyping it means a fourth suppressor added to prepare-values.py cannot be
# silently missing from the leg that is supposed to be qualifying it.
_PV_SPEC = spec_from_file_location(
    "litellm_gray_prepare_values", Path(__file__).with_name("prepare-values.py")
)
if _PV_SPEC is None or _PV_SPEC.loader is None:
    raise SystemExit("prepare-migration-run: cannot load prepare-values")
_PV = module_from_spec(_PV_SPEC)
_PV_SPEC.loader.exec_module(_PV)
BACKGROUND_TASK_SUPPRESSORS = _PV.BACKGROUND_TASK_SUPPRESSORS
QUALIFICATION_KEYS = {
    "schema_version", "status", "run_id", "generation", "config_checksum",
    "source_payload_sha256", "captured_at", "path", "ledger_sha256", "target_image",
    "stable_image", "schema_before_checksum", "schema_after_checksum",
    "compatibility", "compatibility_result_sha256", "online_gate",
    "attestations_sha256", "runner_sha256", "db_targets_sha256", "qualification_sha256",
}
QUALIFICATION_PROBES = {
    "A": {"proxy_startup", "key_api", "proxy_model_api", "auth", "spend_logs"},
    "B": {"proxy_startup", "key_api", "proxy_model_api", "auth", "spend_logs"},
    "C": {"concurrent_budget", "concurrent_spend_logs", "concurrent_proxy_model"},
}


def fail(message: str) -> NoReturn:
    raise SystemExit(f"prepare-migration-run: {message}")


def load_documents(path: Path) -> list[dict[str, Any]]:
    try:
        documents = [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        fail(f"invalid YAML in {path}: {exc}")
    if not documents or not all(isinstance(item, dict) for item in documents):
        fail(f"invalid YAML in {path}")
    return documents


def canonical_digest(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def load_ledger(path: Path) -> tuple[dict[str, Any], str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read migration ledger: {exc}")
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "statements"}:
        fail("migration ledger must contain only schema_version and statements")
    if payload.get("schema_version") != 1:
        fail("migration ledger schema_version must be 1")
    statements = payload.get("statements")
    if not isinstance(statements, list) or not statements:
        fail("migration ledger must contain at least one statement")
    required = {"id", "sql", "online_safe", "lock_mode", "table_rewrite"}
    seen: set[str] = set()
    for entry in statements:
        if not isinstance(entry, dict) or set(entry) != required:
            fail("migration ledger statement schema is invalid")
        identifier = entry.get("id")
        sql = entry.get("sql")
        if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier) or identifier in seen:
            fail("migration ledger statement id is invalid or duplicated")
        seen.add(identifier)
        if not isinstance(sql, str) or not sql.strip() or "\x00" in sql:
            fail(f"migration ledger statement {identifier} has invalid SQL")
        try:
            validate_add_column(sql)
        except ValueError as exc:
            fail(f"migration ledger statement {identifier} is outside the approved ADD COLUMN subset: {exc}")
        if entry.get("online_safe") is not True or entry.get("table_rewrite") is not False:
            fail(f"migration ledger statement {identifier} is not approved online-safe")
        if str(entry.get("lock_mode", "")).upper() != "ACCESS EXCLUSIVE":
            fail(f"migration ledger statement {identifier} has an unapproved lock declaration")
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return payload, canonical_digest(payload), canonical + "\n"


def load_clone_qualification(
    path: Path | None,
    *,
    ledger_sha256: str,
    target_image: str,
    stable_image: str,
    run_id: str,
    generation: str,
    live_schema: Path,
) -> dict[str, Any]:
    if path is None:
        fail("production migration requires clone qualification evidence")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read clone qualification evidence: {exc}")
    if not isinstance(payload, dict) or payload.get("tool") != "check-migration":
        fail("clone qualification must be a check-migration.py result")
    qualification = payload.get("qualification")
    if not isinstance(qualification, dict) or set(qualification) != QUALIFICATION_KEYS:
        fail("check-migration clone qualification schema is invalid")
    expected_digest = canonical_digest(
        {key: value for key, value in qualification.items() if key != "qualification_sha256"}
    )
    checksums = qualification.get("compatibility_result_sha256")
    compatibility = qualification.get("compatibility")
    online_gate = qualification.get("online_gate")
    try:
        captured_at = datetime.fromisoformat(str(qualification.get("captured_at", "")).replace("Z", "+00:00"))
    except ValueError:
        captured_at = None
    age = datetime.now(timezone.utc) - captured_at.astimezone(timezone.utc) if captured_at and captured_at.tzinfo else None
    if (
        payload.get("status") != "PASS"
        or payload.get("path") != "A"
        or qualification.get("schema_version") != 1
        or qualification.get("status") != "PASS"
        or qualification.get("path") != "A"
        or qualification.get("run_id") != run_id
        or qualification.get("generation") != generation
        or qualification.get("ledger_sha256") != ledger_sha256
        or qualification.get("target_image") != target_image
        or qualification.get("stable_image") != stable_image
        or qualification.get("compatibility") != {"A": "PASS", "B": "PASS", "C": "PASS"}
        or not isinstance(checksums, dict)
        or set(checksums) != set(QUALIFICATION_PROBES)
        or not all(isinstance(value, str) and CHECKSUM_RE.fullmatch(value) for value in checksums.values())
        or not isinstance(online_gate, dict)
        or set(online_gate) != {
            "network_policy", "snapshot_counts", "partial_state",
            "migration_duration_ms", "workload_p95_ratio",
        }
        or online_gate.get("partial_state") != "none"
        or not isinstance(online_gate.get("network_policy"), dict)
        or online_gate["network_policy"] != {"allowed_probe": "PASS", "denied_probe": "PASS"}
        or not isinstance(online_gate.get("snapshot_counts"), dict)
        or online_gate["snapshot_counts"].get("expected") != online_gate["snapshot_counts"].get("restored")
        or qualification.get("qualification_sha256") != expected_digest
        or age is None
        or age < -timedelta(minutes=5)
        or age > timedelta(hours=24)
        or qualification.get("schema_before_checksum") != "sha256:" + hashlib.sha256(live_schema.read_bytes()).hexdigest()
        or not CHECKSUM_RE.fullmatch(str(qualification.get("attestations_sha256", "")))
        or not CHECKSUM_RE.fullmatch(str(qualification.get("runner_sha256", "")))
        or not isinstance(qualification.get("db_targets_sha256"), dict)
    ):
        fail("clone qualification is not bound to this ledger and image pair")
    return qualification


def runner_sources() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    result: dict[str, str] = {}
    for name in RUNNER_FILES:
        path = root / name
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            fail(f"cannot read fixed runner {name}: {exc}")
        if not source.strip() or PLACEHOLDER_RE.search(source):
            fail(f"fixed runner {name} is empty or contains a placeholder")
        result[name] = source
    return result


def validate_job(job: dict[str, Any]) -> dict[str, Any]:
    try:
        spec = job["spec"]
        pod_spec = spec["template"]["spec"]
        containers = pod_spec["containers"]
    except (KeyError, TypeError):
        fail("Job template pod spec is invalid")
    if job.get("kind") != "Job" or spec.get("suspend") is not True:
        fail("every rendered object must be a suspended Job")
    if spec.get("backoffLimit") != 0 or pod_spec.get("restartPolicy") != "Never":
        fail("Job retry policy is unsafe")
    if not isinstance(containers, list) or len(containers) != 1:
        fail("each Job must contain exactly one container")
    return containers[0]


def add_runner_mount(job: dict[str, Any], config_name: str) -> None:
    pod_spec = job["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    volumes = pod_spec.setdefault("volumes", [])
    volumes.extend(
        [
            # ConfigMap files stay root-owned and read-only; Python is the
            # entrypoint, so the runner only ever needs to read them.
            {"name": "gray-runners", "configMap": {"name": config_name, "defaultMode": 0o444}},
            {"name": "gray-evidence", "emptyDir": {}},
        ]
    )
    container.setdefault("volumeMounts", []).extend(
        [
            {"name": "gray-runners", "mountPath": "/opt/litellm-gray", "readOnly": True},
            {"name": "gray-evidence", "mountPath": "/evidence"},
        ]
    )


def enforce_production_identity(job: dict[str, Any]) -> None:
    """Refuse to render a Job that cannot reach the image's Prisma engine.

    The image keeps its query engines under /root/.cache with /root at 0700, so
    a non-root uid resolves no engine and the runner dies at connect(). The
    previous workaround exported one engine from an init container, but it
    searched /opt/prisma/binaries — a path that only existed in a different
    image — so it silently produced Jobs that could never pass. Production's
    litellm-proxy runs as uid 0; qualifying it under any other identity tests
    something that will not be deployed.
    """
    pod_spec = job["spec"]["template"]["spec"]
    security = pod_spec.get("securityContext") or {}
    if security.get("runAsUser") != 0 or security.get("runAsNonRoot"):
        fail(f"Job {job['metadata']['name']} must run as uid 0 to reach the image's Prisma engine")
    if pod_spec.get("initContainers"):
        fail(f"Job {job['metadata']['name']} must not carry a Prisma engine export init container")


def prepare_migration_job(
    job: dict[str, Any],
    *,
    target: str,
    image: str,
    ledger_sha256: str,
    config_name: str,
    config_checksum: str,
    run_id: str,
    generation: str,
    db_identity: str,
) -> None:
    container = validate_job(job)
    container["image"] = image
    container["command"] = ["python3"]
    container["args"] = [
        "/opt/litellm-gray/migration-ledger-runner.py",
        "--ledger",
        "/opt/litellm-gray/migration-ledger.json",
        "--ledger-sha256",
        ledger_sha256,
        "--result",
        "/evidence/migration-result.json",
    ]
    metadata = job.get("metadata", {})
    pod_metadata = job["spec"]["template"].setdefault("metadata", {})
    pod_metadata.setdefault("labels", {})["litellm.carher.io/migration-target"] = target
    container.setdefault("env", []).extend(
        execution_binding_env(
            run_id=run_id,
            generation=generation,
            config_checksum=config_checksum,
            image=image,
            db_identity=db_identity,
        )
    )
    database = next((item for item in container.get("env", []) if item.get("name") == "DATABASE_URL"), None)
    try:
        secret_name = database["valueFrom"]["secretKeyRef"]["name"]
    except (KeyError, TypeError):
        fail("migration Job lacks DATABASE_URL secretKeyRef")
    expected = (
        ("litellm-clone", "litellm-clone-a-credentials")
        if target == "clone"
        else ("litellm-product", "litellm-production-migration-credentials")
    )
    if (metadata.get("namespace"), secret_name) != expected:
        fail("migration target template is unsafe")
    add_runner_mount(job, config_name)
    enforce_production_identity(job)


def prepare_version_jobs(
    jobs: list[dict[str, Any]],
    *,
    target_image: str,
    stable_image: str,
    config_name: str,
    config_checksum: str,
    run_id: str,
    generation: str,
    scheduler_probe: str = "none",
    hold_seconds: int = 0,
) -> None:
    seen: set[str] = set()
    for job in jobs:
        name = job.get("metadata", {}).get("name")
        mode = JOB_MODES.get(name)
        if mode is None:
            fail(f"unknown compatibility Job {name}")
        if name in seen:
            fail(f"duplicate compatibility Job {name}")
        seen.add(name)
        container = validate_job(job)
        container["image"] = target_image if mode in {"new", "concurrent-new"} else stable_image
        db_identity = "clone-c" if mode.startswith("concurrent-") else ("clone-a" if mode == "new" else "clone-b")
        container.setdefault("env", []).extend(
            execution_binding_env(
                run_id=run_id,
                generation=generation,
                config_checksum=config_checksum,
                image=container["image"],
                db_identity=db_identity,
            )
        )
        concurrent = mode.startswith("concurrent-")
        # The scheduler probe models the window itself: the stable leg stands in
        # for prod, which owns the scheduler, and the target leg stands in for
        # gray, which must not run it a second time. Only the target leg is ever
        # suppressed, and only the two legs sharing clone C are held open --
        # holding the isolated legs would measure one release against nobody.
        suppressed = scheduler_probe == "suppressed" and mode == "concurrent-new"
        config_file = (
            "compatibility-config-suppressed.yaml" if suppressed else "compatibility-config.yaml"
        )
        container["command"] = ["python3"]
        container["args"] = [
            "/opt/litellm-gray/compatibility-runner.py",
            "--mode",
            mode,
            "--config",
            f"/opt/litellm-gray/{config_file}",
            "--result",
            f"/evidence/{mode}.json",
        ]
        if scheduler_probe != "none" and concurrent:
            container["args"] += ["--hold-seconds", str(hold_seconds)]
            # activeDeadlineSeconds counts the whole Pod, image pull and proxy
            # startup included. Leaving the template's 1800 would kill the Job
            # mid-hold and hand back a truncated window that still parses.
            job["spec"]["activeDeadlineSeconds"] = hold_seconds + 900
        container.setdefault("env", []).extend(
            [
                {
                    "name": "LITELLM_MASTER_KEY",
                    "valueFrom": {
                        "secretKeyRef": {"name": "litellm-clone-test-master-key", "key": "LITELLM_MASTER_KEY"}
                    },
                },
                {"name": "OPENAI_API_KEY", "value": "not-used-by-mock-response"},
                {"name": "STORE_MODEL_IN_DB", "value": "True"},
            ]
        )
        if suppressed:
            container["env"] += [
                {"name": key, "value": value}
                for key, value in sorted(BACKGROUND_TASK_SUPPRESSORS.items())
            ]
        add_runner_mount(job, config_name)
        enforce_production_identity(job)
    if seen != set(JOB_MODES):
        fail("compatibility Job set is incomplete")


def execution_binding_env(
    *, run_id: str, generation: str, config_checksum: str, image: str, db_identity: str
) -> list[dict[str, Any]]:
    image_digest = "sha256:" + image.rsplit("@sha256:", 1)[1]
    return [
        {"name": "GRAY_RUN_ID", "value": run_id},
        {"name": "GRAY_GENERATION", "value": generation},
        {"name": "GRAY_RUNNER_SHA256", "value": "sha256:" + config_checksum},
        {"name": "GRAY_IMAGE_DIGEST", "value": image_digest},
        {"name": "GRAY_DB_IDENTITY", "value": db_identity},
        {
            "name": "POD_UID",
            "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}},
        },
    ]


def secure_write_new(path: Path, rendered: str) -> None:
    if path.exists() or path.is_symlink():
        fail(f"output already exists: {path.name}")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    linked = False
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
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
        fail(f"cannot safely create {path.name}")
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-template", type=Path)
    parser.add_argument("--version-template", type=Path, required=True)
    parser.add_argument("--target-image", required=True)
    parser.add_argument("--stable-image", required=True)
    parser.add_argument("--migration-ledger", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--migration-target", choices=("clone", "prod"), default="clone")
    parser.add_argument(
        "--version-only",
        action="store_true",
        help="render clone compatibility Jobs without qualifying a migration",
    )
    parser.add_argument("--clone-qualification", type=Path)
    parser.add_argument(
        "--scheduler-probe",
        choices=("none", "suppressed", "unsuppressed"),
        default="none",
        help=(
            "hold the two clone-C legs open so a database-side ruler can watch "
            "their schedulers. 'suppressed' gives the target leg the gray "
            "background-task suppression; 'unsuppressed' is the positive "
            "control, where the ruler must see duplicates or it is blind"
        ),
    )
    parser.add_argument("--scheduler-hold-seconds", type=int, default=0)
    parser.add_argument("--live-schema", type=Path)
    parser.add_argument("--migration-command", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--test-command-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.migration_command is not None or args.test_command_dir is not None:
        fail("arbitrary shell command inputs are no longer accepted")
    for image in (args.target_image, args.stable_image):
        if not IDC_IMAGE_RE.fullmatch(image) or PLACEHOLDER_RE.search(image):
            fail("images must be real 198 K3s local-registry immutable digests")
    if not ID_RE.fullmatch(args.run_id) or not ID_RE.fullmatch(args.generation):
        fail("run-id or generation is invalid")
    if args.scheduler_probe != "none":
        if args.migration_target != "clone":
            fail("the scheduler probe only runs against the clone")
        if not 1500 <= args.scheduler_hold_seconds <= 3600:
            # Same bound the runner enforces. Checking it here too means the
            # operator learns at render time, not 25 minutes into a Job that
            # was always going to exit 2 on its argument parser.
            fail("--scheduler-hold-seconds must be between 1500 and 3600")
    elif args.scheduler_hold_seconds:
        fail("--scheduler-hold-seconds requires --scheduler-probe")

    if args.version_only:
        if args.migration_target != "clone":
            fail("version-only mode is restricted to clone compatibility Jobs")
        if any((args.migration_template, args.migration_ledger, args.clone_qualification, args.live_schema)):
            fail("version-only mode rejects migration and production qualification inputs")
        ledger_sha256 = None
        ledger_text = None
    else:
        if args.migration_template is None or args.migration_ledger is None:
            fail("migration template and ledger are required outside version-only mode")
        _, ledger_sha256, ledger_text = load_ledger(args.migration_ledger)

    qualification = None
    if not args.version_only and args.migration_target == "prod":
        if args.live_schema is None or not args.live_schema.is_file():
            fail("production migration requires a fresh normalized live schema snapshot")
        qualification = load_clone_qualification(
            args.clone_qualification,
            ledger_sha256=ledger_sha256,
            target_image=args.target_image,
            stable_image=args.stable_image,
            run_id=args.run_id,
            generation=args.generation,
            live_schema=args.live_schema,
        )
    elif not args.version_only and (args.clone_qualification is not None or args.live_schema is not None):
        fail("clone qualification/live schema evidence is only valid for the production target")

    sources = runner_sources()
    config_payload = dict(sources)
    if ledger_text is not None:
        config_payload["migration-ledger.json"] = ledger_text
    config_checksum = hashlib.sha256(
        json.dumps(config_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    config_name = f"litellm-gray-migration-runners-{config_checksum[:12]}"
    namespace = "litellm-clone" if args.migration_target == "clone" else "litellm-product"
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": config_name, "namespace": namespace},
        "immutable": True,
        "data": config_payload,
    }

    outputs: dict[str, list[dict[str, Any]]] = {"runner-configmaps.yaml": [config_map]}
    if not args.version_only:
        assert args.migration_template is not None and ledger_sha256 is not None
        migration_docs = load_documents(args.migration_template)
        migration_by_name = {
            job.get("metadata", {}).get("name"): job for job in migration_docs
        }
        if set(migration_by_name) != set(MIGRATION_JOBS.values()) or len(migration_by_name) != len(migration_docs):
            fail("migration Job template set is invalid")
        migration_job = copy.deepcopy(migration_by_name[MIGRATION_JOBS[args.migration_target]])
        prepare_migration_job(
            migration_job,
            target=args.migration_target,
            image=args.target_image,
            ledger_sha256=ledger_sha256,
            config_name=config_name,
            config_checksum=config_checksum,
            run_id=args.run_id,
            generation=args.generation,
            db_identity="clone-a" if args.migration_target == "clone" else "production",
        )
        outputs["migration-job.yaml"] = [migration_job]
    if args.migration_target == "clone":
        version_jobs = copy.deepcopy(load_documents(args.version_template))
        prepare_version_jobs(
            version_jobs,
            target_image=args.target_image,
            stable_image=args.stable_image,
            config_name=config_name,
            config_checksum=config_checksum,
            run_id=args.run_id,
            generation=args.generation,
            scheduler_probe=args.scheduler_probe,
            hold_seconds=args.scheduler_hold_seconds,
        )
        outputs["clone-version-test-jobs.yaml"] = version_jobs

    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.output_dir.is_symlink() or not args.output_dir.is_dir():
        fail("unsafe output directory")
    args.output_dir.chmod(0o700)
    for filename in outputs:
        if (args.output_dir / filename).exists() or (args.output_dir / filename).is_symlink():
            fail(f"output already exists: {filename}")

    checksums: dict[str, str] = {}
    for filename, documents in outputs.items():
        rendered = yaml.safe_dump_all(documents, sort_keys=False, allow_unicode=False)
        if PLACEHOLDER_RE.search(rendered):
            fail(f"placeholder remains in {filename}")
        secure_write_new(args.output_dir / filename, rendered)
        checksums[filename] = hashlib.sha256(rendered.encode()).hexdigest()

    report = {
        "tool": "prepare-migration-run",
        "status": "PASS",
        "migration_target": "version-only" if args.version_only else args.migration_target,
        "ledger_sha256": ledger_sha256,
        "clone_qualification_sha256": canonical_digest(qualification) if qualification else None,
        "sha256": checksums,
    }
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
