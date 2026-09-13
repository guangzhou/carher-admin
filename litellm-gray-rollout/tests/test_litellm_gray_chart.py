"""Contract tests for the LiteLLM 198 gray-rollout Kubernetes artifacts."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "litellm-gray-rollout" / "chart"
ROLLOUT = ROOT / "litellm-gray-rollout" / "k8s"
SCRIPTS = ROOT / "litellm-gray-rollout" / "scripts"
HELM = shutil.which("helm")
YQ = shutil.which("yq")
ACR_PREFIX = "cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/"
IDC_REGISTRY_PREFIX = "127.0.0.1:5000/"
ZERO_DIGEST = "sha256:" + ("0" * 64)
ONE_DIGEST = "sha256:" + ("1" * 64)

def create_secret_metadata(tmp_path: Path, names: list[str] | None = None) -> Path:
    """Create secret metadata file for prepare-values tests.
    
    If names is None, defaults to ["litellm-secrets"].
    """
    import json
    all_secrets = {
        "litellm-secrets": {"name": "litellm-secrets", "uid": "uid-litellm-secrets", "resource_version": "1", "data_sha256": "sha256:" + ("e" * 64)},
        "carher-env-keys": {"name": "carher-env-keys", "uid": "uid-carher-env-keys", "resource_version": "1", "data_sha256": "sha256:" + ("f" * 64)},
    }
    if names is None:
        names = ["litellm-secrets"]
    metadata = [all_secrets[name] for name in names]
    path = tmp_path / "secret-metadata.json"
    path.write_text(json.dumps(metadata))
    return path


PROFILES = {
    "prod": ("litellm-proxy", 30402, True),
    "gray": ("litellm-proxy-gray", 30405, False),
    "guarded-old": ("litellm-proxy-guarded-old", 30406, False),
}
DEFAULT_CONFIG_SHA256 = "sha256:e7180eeb637e8c0a7ec133f661cb657826b38f900ddd5ffd15043acffc28d9d2"


def test_metadata_clone_storage_is_bounded_and_pinned_off_production_node():
    dump_documents = [
        item
        for item in yaml.safe_load_all(
            (ROLLOUT / "clone-dump-restore-jobs.yaml").read_text(encoding="utf-8")
        )
        if item
    ]
    dump_pvc = next(item for item in dump_documents if item.get("kind") == "PersistentVolumeClaim")
    assert dump_pvc["spec"]["resources"]["requests"]["storage"] == "4Gi"
    assert dump_pvc["spec"]["storageClassName"] == "local-path"

    database_documents = [
        item
        for item in yaml.safe_load_all(
            (ROLLOUT / "clone-databases.yaml").read_text(encoding="utf-8")
        )
        if item
    ]
    statefulsets = [item for item in database_documents if item.get("kind") == "StatefulSet"]
    assert len(statefulsets) == 3
    for statefulset in statefulsets:
        pod_spec = statefulset["spec"]["template"]["spec"]
        assert pod_spec["nodeSelector"] == {
            "kubernetes.io/hostname": "aiyjy-litellm-standby"
        }
        assert pod_spec["tolerations"] == [{
            "key": "dedicated",
            "operator": "Equal",
            "value": "standby",
            "effect": "NoSchedule",
        }]
        assert "volumes" not in pod_spec
        claims = statefulset["spec"]["volumeClaimTemplates"]
        assert len(claims) == 1
        claim = claims[0]
        assert claim["metadata"]["name"] == "data"
        assert claim["spec"]["storageClassName"] == "local-path"
        assert claim["spec"]["resources"]["requests"]["storage"] == "4Gi"
        container = pod_spec["containers"][0]
        assert container["env"] == [{
            "name": "PGDATA",
            "value": "/var/lib/postgresql/data/pgdata",
        }]
        assert container["resources"] == {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "500m", "memory": "1Gi"},
        }

    probe = load_yaml(ROLLOUT / "clone-storage-probe-job.yaml")
    assert probe["kind"] == "Job"
    probe_spec = probe["spec"]["template"]["spec"]
    assert probe_spec["automountServiceAccountToken"] is False
    assert probe_spec["nodeSelector"] == {
        "kubernetes.io/hostname": "aiyjy-litellm-standby"
    }
    assert probe_spec["tolerations"] == [{
        "key": "dedicated",
        "operator": "Equal",
        "value": "standby",
        "effect": "NoSchedule",
    }]
    assert probe_spec["containers"][0]["volumeMounts"] == [
        {"name": "dump", "mountPath": "/dump"}
    ]
    assert probe_spec["volumes"] == [{
        "name": "dump",
        "persistentVolumeClaim": {"claimName": "litellm-clone-dump"},
    }]
    command = probe_spec["containers"][0]["args"][0]
    assert "PROBE_OK" in command
    assert "rm -f" in command
    assert "min_free_bytes=21474836480" in command
    assert "clone storage has less than 20 GiB available" in command
    assert 'available_bytes="$(df -PB1 /dump' in command

    db_probe = load_yaml(ROLLOUT / "clone-readonly-db-probe-job.yaml")
    db_spec = db_probe["spec"]["template"]["spec"]
    assert db_spec["automountServiceAccountToken"] is False
    assert db_spec["nodeSelector"] == {
        "kubernetes.io/hostname": "aiyjy-litellm-standby"
    }
    assert db_spec["tolerations"] == [{
        "key": "dedicated",
        "operator": "Equal",
        "value": "standby",
        "effect": "NoSchedule",
    }]
    db_container = db_spec["containers"][0]
    assert db_container["env"][0]["valueFrom"]["secretKeyRef"] == {
        "name": "litellm-production-readonly-dump-credentials",
        "key": "DATABASE_URL",
    }
    db_command = db_container["args"][0]
    assert "SHOW transaction_read_only" in db_command
    assert "CREATE TABLE public.litellm_clone_probe_must_not_exist" in db_command
    assert "READONLY_DB_PROBE_OK" in db_command


def value_digest(value: object) -> str:
    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


def frozen_scheduler_overrides(
    profile: str, *, extra_env: list[dict] | None = None
) -> list[str]:
    values = load_yaml(CHART / "values.yaml")
    runtime = {
        "args": values["args"],
        "command": values["command"],
        "extraEnv": values["extraEnv"] if extra_env is None else extra_env,
        "secretRefs": values["secretRefs"],
    }
    callbacks_sha = value_digest(values["callbacks"]["data"])
    runtime_sha = value_digest(runtime)
    return [
        "--set",
        "schedulerSafety.mode=disabled" if profile != "prod" else "schedulerSafety.mode=primary",
        "--set-string",
        "schedulerSafety.evidenceSha256=sha256:" + ("a" * 64),
        "--set-string",
        f"schedulerSafety.configSha256={DEFAULT_CONFIG_SHA256}",
        "--set-string",
        f"schedulerSafety.callbacksSha256={callbacks_sha}",
        "--set-string",
        f"schedulerSafety.runtimeSha256={runtime_sha}",
        "--set-string",
        "schedulerSafety.sourcePayloadSha256=sha256:" + ("b" * 64),
    ]


def load_yaml(path: Path):
    assert YQ is not None, "yq is required for Kubernetes manifest contract tests"
    result = subprocess.run(
        [YQ, "-o=json", "-I=0", ".", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    documents = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(documents) == 1, f"expected one YAML document in {path}"
    return documents[0]


def scheduler_evidence(
    config: Path,
    callbacks: Path,
    *,
    image_digest: str = "sha256:" + ("a" * 64),
    mode: str = "disabled",
    deployment: Path | None = None,
    runtime_sha256: str | None = None,
    secret_metadata: list[dict] | None = None,
) -> dict:
    callback_data = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(callbacks.iterdir())
        if path.is_file()
    }
    callback_text = json.dumps(
        callback_data, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    if runtime_sha256 is None:
        if deployment is None:
            runtime_sha256 = "sha256:" + ("d" * 64)
        else:
            workload = load_yaml(deployment)
            container = workload["spec"]["template"]["spec"]["containers"][0]
            defaults = load_yaml(CHART / "values.yaml")
            
            # Mirror prepare-values.py's canonical runtime contract exactly.
            extra_env = []
            for item in container.get("env", []):
                name = item.get("name")
                if name == "DISABLE_SCHEMA_UPDATE":
                    continue
                if set(item) == {"name", "value"}:
                    extra_env.append({"name": name, "value": item["value"]})
                    continue
                ref = item.get("valueFrom")
                ref_kind = next(iter(ref)) if isinstance(ref, dict) and len(ref) == 1 else None
                source = ref.get(ref_kind) if ref_kind else None
                if ref_kind in {"secretKeyRef", "configMapKeyRef"} and isinstance(source, dict):
                    frozen = {"name": source["name"], "key": source["key"]}
                    if isinstance(source.get("optional"), bool):
                        frozen["optional"] = source["optional"]
                    extra_env.append({"name": name, "valueFrom": {ref_kind: frozen}})

            secret_refs = [item["secretRef"]["name"] for item in container.get("envFrom", [])]
            
            runtime_payload = {
                "args": container.get("args") or defaults["args"],
                "command": container.get("command") or defaults["command"],
                "extraEnv": extra_env,
                "secretRefs": secret_refs,
            }
            if secret_metadata is not None:
                runtime_payload["secretMetadata"] = sorted(
                    secret_metadata, key=lambda item: item["name"]
                )
            runtime_sha256 = value_digest(runtime_payload)
    payload = {
        "schema_version": 1,
        "profile": "prod" if mode == "primary" else "gray",
        "mode": mode,
        "image_digest": image_digest,
        "config_sha256": "sha256:" + hashlib.sha256(config.read_bytes()).hexdigest(),
        "callbacks_sha256": "sha256:" + hashlib.sha256(callback_text.encode()).hexdigest(),
        "runtime_sha256": runtime_sha256,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "clone:scheduler-observation",
        "observations": {
            "duplicate_scheduler_runs": 0,
            "duplicate_background_jobs": 0,
            "unexpected_control_writes": 0,
        },
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    payload["source_payload_sha256"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return payload


def load_yaml_documents(path: Path) -> list[dict]:
    assert YQ is not None, "yq is required for Kubernetes manifest contract tests"
    result = subprocess.run(
        [YQ, "-o=json", "-I=0", ".", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def find_doc(documents: list[dict], kind: str, name: str) -> dict:
    matches = [
        doc
        for doc in documents
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"expected one {kind}/{name}, got {len(matches)}"
    return matches[0]


def find_namespaced_doc(documents: list[dict], kind: str, name: str, namespace: str) -> dict:
    matches = [
        doc
        for doc in documents
        if doc.get("kind") == kind
        and doc.get("metadata", {}).get("name") == name
        and doc.get("metadata", {}).get("namespace") == namespace
    ]
    assert len(matches) == 1, f"expected one {kind}/{namespace}/{name}, got {len(matches)}"
    return matches[0]


def render_profile(profile: str, *extra_args: str) -> list[dict]:
    assert HELM is not None
    extra_env = None
    for index, item in enumerate(extra_args[:-1]):
        if item == "--set-json" and extra_args[index + 1].startswith("extraEnv="):
            extra_env = json.loads(extra_args[index + 1].split("=", 1)[1])
    command = [
        HELM,
        "template",
        f"litellm-product-{profile}",
        str(CHART),
        "--namespace",
        "litellm-product",
        "--values",
        str(ROLLOUT / f"values-{profile}.yaml"),
        "--set",
        "artifactTemplate=false",
        *frozen_scheduler_overrides(profile, extra_env=extra_env),
        *extra_args,
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    parsed = subprocess.run(
        [YQ, "-o=json", "-I=0", "."],
        input=result.stdout,
        check=True,
        capture_output=True,
        text=True,
    )
    return [json.loads(line) for line in parsed.stdout.splitlines() if line.strip()]


def run_helm_with_values(tmp_path: Path, overrides: dict) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    values_path = tmp_path / "overrides.yaml"
    values_path.write_text(
        json.dumps({"artifactTemplate": False, **overrides}), encoding="utf-8"
    )
    return subprocess.run(
        [
            HELM,
            "template",
            "litellm-product-test",
            str(CHART),
            "--namespace",
            "litellm-product",
            "--values",
            str(values_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _render_gray_with_overrides(overrides: dict) -> subprocess.CompletedProcess[str]:
    """helm template the gray profile with frozen scheduler evidence plus overrides."""
    assert HELM is not None
    extra: list[str] = []
    for key, value in overrides.items():
        extra += ["--set-json", f"{key}={json.dumps(value)}"]
    return subprocess.run(
        [
            HELM,
            "template",
            "litellm-product-gray",
            str(CHART),
            "--namespace",
            "litellm-product",
            "--values",
            str(ROLLOUT / "values-gray.yaml"),
            "--set",
            "artifactTemplate=false",
            *frozen_scheduler_overrides("gray"),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _render_gray_with_ingress_from(peers: list[dict]) -> subprocess.CompletedProcess[str]:
    return _render_gray_with_overrides({"networkPolicy.ingressFrom": peers})


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
def test_networkpolicy_accepts_node_ipblock_and_rejects_mixed_or_open_peers():
    """The NodePort path is the real traffic path, so ipBlock must be expressible.

    kube-proxy presents the node address as the source of host-nginx -> NodePort
    traffic. A schema that can only express namespace/pod selectors makes the
    default-deny posture silently unroutable.
    """
    accepted = _render_gray_with_ingress_from(
        [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}
                },
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "ingress-nginx"}},
            },
            {"ipBlock": {"cidr": "10.68.13.242/32"}},
        ]
    )
    assert accepted.returncode == 0, accepted.stderr
    policy = [
        doc
        for doc in yaml.safe_load_all(accepted.stdout)
        if doc and doc.get("kind") == "NetworkPolicy"
    ]
    assert len(policy) == 1
    assert {"ipBlock": {"cidr": "10.68.13.242/32"}} in policy[0]["spec"]["ingress"][0]["from"]

    for bad_peer in (
        # ipBlock and selectors in one peer is not a valid NetworkPolicy peer.
        {
            "ipBlock": {"cidr": "10.68.13.242/32"},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "ingress-nginx"}},
        },
        {"ipBlock": {"cidr": "10.68.13.242"}},
        {"ipBlock": {"cidr": "not-a-cidr"}},
        {"ipBlock": {"cidr": "10.68.13.242/32", "unexpected": True}},
        {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "ingress-nginx"}}},
    ):
        rejected = _render_gray_with_ingress_from([bad_peer])
        assert rejected.returncode != 0, f"schema accepted {bad_peer}"
        assert "schema" in rejected.stderr.lower(), rejected.stderr


def test_prepare_values_requires_a_narrow_node_cidr_for_the_nodeport_path():
    """Freezing values without declaring the NodePort source is the bug we hit."""
    script = (ROOT / "litellm-gray-rollout" / "scripts" / "prepare-values.py").read_text(
        encoding="utf-8"
    )
    assert "--ingress-cidr" in script
    assert "required=True" in script.split("--ingress-cidr", 1)[1][:400]

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "prepare_values_under_test",
        ROOT / "litellm-gray-rollout" / "scripts" / "prepare-values.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    values = {"networkPolicy": {"enabled": True, "ingressFrom": []}}
    module.freeze_ingress_cidrs(values, ["10.68.13.242/32", "10.68.13.243/32"])
    assert values["networkPolicy"]["ingressFrom"] == [
        {"ipBlock": {"cidr": "10.68.13.242/32"}},
        {"ipBlock": {"cidr": "10.68.13.243/32"}},
    ]

    for bad in (["0.0.0.0/0"], ["10.0.0.0/8"], ["10.68.13.242"], ["nope"]):
        with pytest.raises(SystemExit):
            module.freeze_ingress_cidrs(
                {"networkPolicy": {"enabled": True, "ingressFrom": []}}, bad
            )
    with pytest.raises(SystemExit):
        module.freeze_ingress_cidrs(
            {"networkPolicy": {"enabled": True, "ingressFrom": []}},
            ["10.68.13.242/32", "10.68.13.242/32"],
        )


def test_drain_budget_is_one_derivation_across_chart_and_nginx():
    """grace >= preStop + streamDrain, and nginx waits exactly streamDrain.

    nginx promising a longer read timeout than a Terminating Pod is allowed to
    live does not buy a longer stream; it converts a clean 504 into a silent
    mid-stream SSE truncation at SIGKILL.
    """
    values = load_yaml(CHART / "values.yaml")
    pre_stop = int(values["drain"]["preStopSeconds"])
    stream_drain = int(values["drain"]["streamDrainSeconds"])
    grace = int(values["terminationGracePeriodSeconds"])

    assert grace >= pre_stop + stream_drain, (grace, pre_stop, stream_drain)
    assert values["lifecycle"]["preStop"]["exec"]["command"] == [
        "sh",
        "-c",
        f"sleep {pre_stop}",
    ]

    expected = f"proxy_read_timeout {stream_drain}s;"
    fixture = (SCRIPTS / "fixtures" / "nginx" / "nginx.conf.template").read_text(
        encoding="utf-8"
    )
    renderer = (SCRIPTS / "render-production-nginx.py").read_text(encoding="utf-8")
    assert expected in fixture
    assert expected in renderer
    for text in (fixture, renderer):
        assert len(re.findall(r"proxy_read_timeout\s+(\d+)s;", text)) == len(
            re.findall(re.escape(expected), text)
        )


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
def test_chart_rejects_a_grace_period_below_the_drain_budget(tmp_path: Path):
    rejected = _render_gray_with_overrides(
        {
            "drain": {"preStopSeconds": 30, "streamDrainSeconds": 570},
            "terminationGracePeriodSeconds": 300,
        }
    )
    assert rejected.returncode != 0
    assert "drain" in rejected.stderr.lower()

    drifted = _render_gray_with_overrides(
        {
            "drain": {"preStopSeconds": 60, "streamDrainSeconds": 500},
            "lifecycle": {"preStop": {"exec": {"command": ["sh", "-c", "sleep 30"]}}},
        }
    )
    assert drifted.returncode != 0
    assert "prestop" in drifted.stderr.lower()


def test_chart_and_rollout_artifact_set_is_complete():
    expected = {
        CHART / "Chart.yaml",
        CHART / "values.yaml",
        CHART / "values.schema.json",
        CHART / "templates" / "_helpers.tpl",
        CHART / "templates" / "configmaps.yaml",
        CHART / "templates" / "deployment.yaml",
        CHART / "templates" / "networkpolicy.yaml",
        CHART / "templates" / "service.yaml",
        ROLLOUT / "README.md",
        ROLLOUT / "values-prod.yaml",
        ROLLOUT / "values-gray.yaml",
        ROLLOUT / "values-guarded-old.yaml",
        ROLLOUT / "migration-job.yaml",
        ROLLOUT / "clone-secret-templates.yaml",
        ROLLOUT / "clone-namespace.yaml",
        ROLLOUT / "clone-networkpolicies.yaml",
        ROLLOUT / "clone-databases.yaml",
        ROLLOUT / "clone-dump-restore-jobs.yaml",
        ROLLOUT / "clone-storage-probe-job.yaml",
        ROLLOUT / "clone-readonly-db-probe-job.yaml",
        ROLLOUT / "clone-dump-artifact-cleanup-job.yaml",
        ROLLOUT / "clone-db-data-cleanup-job.yaml",
        ROLLOUT / "clone-connectivity-probes.yaml",
        ROLLOUT / "clone-version-test-jobs.yaml",
    }
    missing = sorted(str(path.relative_to(ROOT)) for path in expected if not path.is_file())
    assert not missing, f"missing rollout artifacts: {missing}"


def test_values_schema_rejects_public_or_tag_only_images_and_unknown_fields():
    schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False

    image = schema["properties"]["image"]
    assert image["additionalProperties"] is False
    assert set(image["required"]) == {"repository", "digest", "pullPolicy"}
    repository_pattern = image["properties"]["repository"]["pattern"]
    assert re.fullmatch(repository_pattern, IDC_REGISTRY_PREFIX + "litellm-carher")
    assert not re.fullmatch(repository_pattern, ACR_PREFIX + "her/litellm-proxy")
    assert not re.fullmatch(repository_pattern, "ghcr.io/berriai/litellm")
    assert image["properties"]["digest"]["pattern"] == "^sha256:[a-f0-9]{64}$"
    assert set(image["properties"]["digest"]["not"]["enum"]) == {
        ZERO_DIGEST,
        ONE_DIGEST,
    }
    assert "tag" not in image["properties"]

    assert schema["properties"]["nameOverride"]["enum"] == [
        "litellm-proxy",
        "litellm-proxy-gray",
        "litellm-proxy-guarded-old",
    ]
    assert schema["properties"]["schemaUpdateEnabled"]["const"] is False
    assert schema["properties"]["networkPolicy"]["properties"]["enabled"]["const"] is True
    assert set(schema["properties"]["selectorLabels"]["required"]) == {"app"}
    assert schema["properties"]["command"]["minItems"] == 1
    assert schema["properties"]["args"]["minItems"] == 1
    assert schema["properties"]["additionalSnapshots"]["type"] == "array"
    workload_contract = schema["properties"]["workloadContract"]
    assert workload_contract["additionalProperties"] is False
    assert workload_contract["properties"]["containers"]["items"] == {
        "const": "litellm"
    }
    extra_env = schema["properties"]["extraEnv"]["items"]
    assert set(extra_env["oneOf"][0]["required"]) == {"name", "value"}
    assert set(extra_env["oneOf"][1]["required"]) == {"name", "valueFrom"}
    assert {tuple(branch["required"]) for branch in extra_env["oneOf"]} == {
        ("name", "value"),
        ("name", "valueFrom"),
    }
    secret_ref = extra_env["properties"]["valueFrom"]["properties"]["secretKeyRef"]
    assert set(secret_ref["required"]) == {"name", "key"}
    config_map_ref = extra_env["properties"]["valueFrom"]["properties"]["configMapKeyRef"]
    assert set(config_map_ref["required"]) == {"name", "key"}
    assert len(extra_env["properties"]["valueFrom"]["oneOf"]) == 2
    assert set(extra_env["properties"]["name"]["not"]["enum"]) == {
        "DISABLE_SCHEMA_UPDATE",
    }
    background_tasks = schema["properties"]["backgroundTasks"]
    assert background_tasks["additionalProperties"] is False
    assert set(background_tasks["required"]) == {"enabled"}
    assert set(background_tasks["properties"]) == {"enabled"}
    scheduler = schema["properties"]["schedulerSafety"]
    assert set(scheduler["required"]) == {
        "mode",
        "evidenceSha256",
        "configSha256",
        "callbacksSha256",
        "runtimeSha256",
        "sourcePayloadSha256",
        "capturedAt",
        "source",
    }
    assert set(schema["properties"]["podLabels"]["propertyNames"]["not"]["enum"]) == {
        "app",
        "app.kubernetes.io/instance",
        "carher.net/litellm-production-route",
    }
    assert set(schema["properties"]["podAnnotations"]["propertyNames"]["not"]["enum"]) == {
        "litellm.carher.io/config-checksum",
        "litellm.carher.io/callbacks-checksum",
        "litellm.carher.io/scheduler-evidence-checksum",
    }
    assert schema["properties"]["callbacks"]["properties"]["data"]["propertyNames"] == {
        "pattern": "^[A-Za-z0-9._-]+$"
    }

    init_schema = schema["properties"]["initContainers"]
    allowed_names = init_schema["properties"]["approvedNames"]["items"]["enum"]
    item_names = init_schema["properties"]["items"]["items"]["properties"]["name"]["enum"]
    assert allowed_names == item_names == ["config-check"]
    assert init_schema["properties"]["items"]["items"]["required"] == ["name"]
    assert set(init_schema["properties"]["items"]["items"]["properties"]) == {"name"}


def test_chart_defaults_are_non_deployable_examples() -> None:
    defaults = load_yaml(CHART / "values.yaml")
    assert defaults["artifactTemplate"] is True
    assert defaults["allowTemplateRender"] is False


def test_profile_values_are_non_deployable_examples() -> None:
    for profile in PROFILES:
        values = load_yaml(ROLLOUT / f"values-{profile}.yaml")
        assert values["artifactTemplate"] is True
        assert values["allowTemplateRender"] is False


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
@pytest.mark.parametrize("profile", PROFILES)
def test_checked_in_profile_refuses_direct_render(profile: str) -> None:
    result = subprocess.run(
        [
            HELM,
            "template",
            f"litellm-product-{profile}",
            str(CHART),
            "--namespace",
            "litellm-product",
            "--values",
            str(ROLLOUT / f"values-{profile}.yaml"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "example only" in (result.stderr + result.stdout).lower()


def test_prepare_values_freezes_real_snapshots_and_blocks_placeholders(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("model_list:\n  - model_name: smoke\n", encoding="utf-8")
    callbacks = tmp_path / "callbacks"
    callbacks.mkdir()
    (callbacks / "smoke.py").write_text("def callback():\n    return True\n", encoding="utf-8")
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: litellm-proxy
spec:
  template:
    spec:
      terminationGracePeriodSeconds: 600
      nodeSelector: {kubernetes.io/hostname: worker-198}
      tolerations:
        - key: dedicated
          operator: Equal
          value: litellm
          effect: NoSchedule
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution: []
      containers:
        - name: litellm
          command: [/app/docker/prod_entrypoint.sh]
          args: [--config, /app/config.yaml, --port, '4000', --num_workers, '2']
          env:
            - name: CHATGPT_POOL_KEY
              valueFrom:
                secretKeyRef:
                  name: chatgpt-pool-master-key
                  key: LITELLM_MASTER_KEY
          envFrom:
            - secretRef: {name: litellm-secrets}
            - secretRef: {name: carher-env-keys}
          resources:
            requests: {cpu: 200m, memory: 1Gi}
            limits: {cpu: '2', memory: 4Gi}
          livenessProbe:
            httpGet: {path: /health/liveliness, port: 4000}
            initialDelaySeconds: 90
            periodSeconds: 15
            failureThreshold: 5
            timeoutSeconds: 15
          readinessProbe:
            httpGet: {path: /health/readiness, port: 4000}
            initialDelaySeconds: 90
            periodSeconds: 15
            failureThreshold: 5
            timeoutSeconds: 15
          lifecycle:
            preStop:
              exec:
                command: [sh, -c, sleep 15]
""",
        encoding="utf-8",
    )
    secret_names = ["litellm-secrets", "carher-env-keys"]
    secret_metadata_file = create_secret_metadata(tmp_path, names=secret_names)
    secret_metadata = json.loads(secret_metadata_file.read_text(encoding="utf-8"))
    
    scheduler = tmp_path / "scheduler.json"
    scheduler.write_text(
        json.dumps(scheduler_evidence(config, callbacks, deployment=deployment, secret_metadata=secret_metadata)),
        encoding="utf-8",
    )
    output = tmp_path / "run" / "gray-values.yaml"
    command = [
        "python3",
        str(ROOT / "litellm-gray-rollout" / "scripts" / "prepare-values.py"),
        "--ingress-cidr",
        "10.68.13.242/32",
        "--profile",
        str(ROLLOUT / "values-gray.yaml"),
        "--repository",
        IDC_REGISTRY_PREFIX + "litellm-carher",
        "--digest",
        "sha256:" + ("a" * 64),
        "--config",
        str(config),
        "--callbacks-dir",
        str(callbacks),
        "--deployment",
        str(deployment),
        "--scheduler-evidence",
        str(scheduler),
        "--secret-metadata",
        str(secret_metadata_file),
        "--output",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    values = load_yaml(output)
    assert values["artifactTemplate"] is False
    assert values["allowTemplateRender"] is False
    assert values["image"]["digest"] == "sha256:" + ("a" * 64)
    assert values["callbacks"]["data"] == {"smoke.py": "def callback():\n    return True\n"}
    assert values["extraEnv"][0]["valueFrom"]["secretKeyRef"]["name"] == "chatgpt-pool-master-key"
    assert values["schedulerSafety"]["mode"] == "disabled"
    # The NodePort path must survive the default-deny posture: kube-proxy shows
    # the node address as source, so a selector-only policy would black-hole it.
    assert {"ipBlock": {"cidr": "10.68.13.242/32"}} in values["networkPolicy"]["ingressFrom"]
    assert values["networkPolicy"]["enabled"] is True
    assert values["lifecycle"] == {
        "preStop": {"exec": {"command": ["sh", "-c", "sleep 15"]}}
    }
    assert values["nodeSelector"] == {"kubernetes.io/hostname": "worker-198"}
    assert values["tolerations"] == [
        {
            "key": "dedicated",
            "operator": "Equal",
            "value": "litellm",
            "effect": "NoSchedule",
        }
    ]
    assert values["affinity"] == {
        "podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}
    }
    assert output.stat().st_mode & 0o777 == 0o600

    rejected = subprocess.run(
        [*command[: command.index("--digest") + 1], ZERO_DIGEST, *command[command.index("--digest") + 2 :]],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "placeholder" in rejected.stderr.lower()


def test_prepare_values_preserves_config_map_key_ref_environment(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    callbacks = tmp_path / "callbacks"
    callbacks.mkdir()
    (callbacks / "smoke.py").write_text("def callback():\n    return True\n", encoding="utf-8")
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata: {name: litellm-proxy}
spec:
  template:
    spec:
      terminationGracePeriodSeconds: 600
      containers:
        - name: litellm
          env:
            - name: FEATURE_FLAGS
              valueFrom:
                configMapKeyRef:
                  name: litellm-runtime-flags
                  key: flags.json
                  optional: true
          envFrom:
            - secretRef: {name: litellm-secrets}
          resources:
            requests: {cpu: 200m, memory: 1Gi}
            limits: {cpu: '2', memory: 4Gi}
          livenessProbe:
            httpGet: {path: /health/liveliness, port: 4000}
            initialDelaySeconds: 90
            periodSeconds: 15
            failureThreshold: 5
            timeoutSeconds: 15
          readinessProbe:
            httpGet: {path: /health/readiness, port: 4000}
            initialDelaySeconds: 90
            periodSeconds: 15
            failureThreshold: 5
            timeoutSeconds: 15
          lifecycle:
            preStop:
              exec:
                command: [sh, -c, sleep 15]
""",
        encoding="utf-8",
    )
    secret_metadata_file = create_secret_metadata(tmp_path, names=["litellm-secrets"])
    secret_metadata = json.loads(secret_metadata_file.read_text(encoding="utf-8"))
    scheduler = tmp_path / "scheduler.json"
    scheduler.write_text(
        json.dumps(scheduler_evidence(config, callbacks, deployment=deployment, secret_metadata=secret_metadata)),
        encoding="utf-8",
    )
    output = tmp_path / "gray.yaml"
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "litellm-gray-rollout" / "scripts" / "prepare-values.py"),
            "--ingress-cidr",
            "10.68.13.242/32",
            "--profile",
            str(ROLLOUT / "values-gray.yaml"),
            "--repository",
            IDC_REGISTRY_PREFIX + "litellm-carher",
            "--digest",
            "sha256:" + ("a" * 64),
            "--config",
            str(config),
            "--callbacks-dir",
            str(callbacks),
            "--deployment",
            str(deployment),
            "--scheduler-evidence",
            str(scheduler),
            "--secret-metadata",
            str(secret_metadata_file),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    values = load_yaml(output)
    assert values["extraEnv"] == [
        {
            "name": "FEATURE_FLAGS",
            "valueFrom": {
                "configMapKeyRef": {
                    "name": "litellm-runtime-flags",
                    "key": "flags.json",
                    "optional": True,
                }
            },
        }
    ]


@pytest.mark.parametrize(
    ("pod_spec_patch", "expected_error"),
    [
        (
            """
      containers:
        - name: litellm
          envFrom: [{secretRef: {name: litellm-secrets}}]
          resources: {requests: {cpu: 200m, memory: 1Gi}, limits: {cpu: '2', memory: 4Gi}}
          livenessProbe: &probe {httpGet: {path: /health/liveliness, port: 4000}, initialDelaySeconds: 90, periodSeconds: 15, failureThreshold: 5, timeoutSeconds: 15}
          readinessProbe: {httpGet: {path: /health/readiness, port: 4000}, initialDelaySeconds: 90, periodSeconds: 15, failureThreshold: 5, timeoutSeconds: 15}
          lifecycle: {preStop: {exec: {command: [sh, -c, sleep 15]}}}
        - name: metrics-sidecar
          image: example.invalid/metrics:latest
""",
            "exactly one container",
        ),
        (
            """
      containers:
        - name: litellm
          envFrom: [{secretRef: {name: litellm-secrets}}]
          resources: {requests: {cpu: 200m, memory: 1Gi}, limits: {cpu: '2', memory: 4Gi}}
          livenessProbe: {httpGet: {path: /health/liveliness, port: 4000}, initialDelaySeconds: 90, periodSeconds: 15, failureThreshold: 5, timeoutSeconds: 15}
          readinessProbe: {httpGet: {path: /health/readiness, port: 4000}, initialDelaySeconds: 90, periodSeconds: 15, failureThreshold: 5, timeoutSeconds: 15}
          lifecycle: {preStop: {exec: {command: [sh, -c, sleep 15]}}}
      initContainers:
        - name: prisma-migrate
          image: example.invalid/litellm:latest
""",
            "initcontainers",
        ),
        (
            """
      containers: {name: litellm}
""",
            "containers must be a list",
        ),
        (
            """
      containers:
        - name: litellm
          envFrom: [{secretRef: {name: litellm-secrets}}]
          resources: {requests: {cpu: 200m, memory: 1Gi}, limits: {cpu: '2', memory: 4Gi}}
          livenessProbe: {httpGet: {path: /health/liveliness, port: 4000}, initialDelaySeconds: 90, periodSeconds: 15, failureThreshold: 5, timeoutSeconds: 15}
          readinessProbe: {httpGet: {path: /health/readiness, port: 4000}, initialDelaySeconds: 90, periodSeconds: 15, failureThreshold: 5, timeoutSeconds: 15}
          lifecycle: {preStop: {exec: {command: [sh, -c, sleep 15]}}}
        - name: metrics-sidecar
          image: example.invalid/metrics:latest
        - name: metrics-sidecar
          image: example.invalid/metrics:latest
""",
            "duplicate container name",
        ),
    ],
)
def test_prepare_values_rejects_unknown_live_workload_shape(
    tmp_path: Path, pod_spec_patch: str, expected_error: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    callbacks = tmp_path / "callbacks"
    callbacks.mkdir()
    (callbacks / "smoke.py").write_text("def callback():\n    return True\n", encoding="utf-8")
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: litellm-proxy}\n"
        "spec:\n  template:\n    spec:\n      terminationGracePeriodSeconds: 600\n"
        + pod_spec_patch,
        encoding="utf-8",
    )
    scheduler = tmp_path / "scheduler.json"
    scheduler.write_text(
        json.dumps(
            scheduler_evidence(
                config, callbacks, runtime_sha256="sha256:" + ("d" * 64)
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "litellm-gray-rollout" / "scripts" / "prepare-values.py"),
            "--ingress-cidr",
            "10.68.13.242/32",
            "--profile",
            str(ROLLOUT / "values-gray.yaml"),
            "--repository",
            IDC_REGISTRY_PREFIX + "litellm-carher",
            "--digest",
            "sha256:" + ("a" * 64),
            "--config",
            str(config),
            "--callbacks-dir",
            str(callbacks),
            "--deployment",
            str(deployment),
            "--scheduler-evidence",
            str(scheduler),
        "--secret-metadata",
        str(create_secret_metadata(tmp_path)),
            "--output",
            str(tmp_path / "gray.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert expected_error in result.stderr.lower()


def test_prepare_values_rejects_unguarded_non_prod_scheduler(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    callbacks = tmp_path / "callbacks"
    callbacks.mkdir()
    (callbacks / "smoke.py").write_text("def callback():\n    return True\n", encoding="utf-8")
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata: {name: litellm-proxy}
spec:
  template:
    spec:
      terminationGracePeriodSeconds: 600
      containers:
        - name: litellm
          envFrom:
            - secretRef: {name: litellm-secrets}
          resources:
            requests: {cpu: 200m, memory: 1Gi}
            limits: {cpu: '2', memory: 4Gi}
          livenessProbe:
            httpGet: {path: /health/liveliness, port: 4000}
            initialDelaySeconds: 90
            periodSeconds: 15
            failureThreshold: 5
            timeoutSeconds: 15
          readinessProbe:
            httpGet: {path: /health/readiness, port: 4000}
            initialDelaySeconds: 90
            periodSeconds: 15
            failureThreshold: 5
            timeoutSeconds: 15
          lifecycle:
            preStop:
              exec:
                command: [sh, -c, sleep 15]
""",
        encoding="utf-8",
    )
    secret_metadata_file = create_secret_metadata(tmp_path, names=["litellm-secrets"])
    secret_metadata = json.loads(secret_metadata_file.read_text(encoding="utf-8"))
    command = [
        "python3",
        str(ROOT / "litellm-gray-rollout" / "scripts" / "prepare-values.py"),
        "--ingress-cidr",
        "10.68.13.242/32",
        "--profile",
        str(ROLLOUT / "values-gray.yaml"),
        "--repository",
        IDC_REGISTRY_PREFIX + "litellm-carher",
        "--digest",
        "sha256:" + ("a" * 64),
        "--config",
        str(config),
        "--callbacks-dir",
        str(callbacks),
        "--deployment",
        str(deployment),
        "--secret-metadata",
        str(secret_metadata_file),
        "--output",
        str(tmp_path / "gray.yaml"),
    ]
    rejected = subprocess.run(command, capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert "scheduler" in rejected.stderr.lower()

    scheduler = tmp_path / "scheduler.json"
    scheduler.write_text(
        json.dumps(scheduler_evidence(config, callbacks, deployment=deployment, secret_metadata=secret_metadata)),
        encoding="utf-8",
    )
    accepted = subprocess.run(
        [*command, "--scheduler-evidence", str(scheduler)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr

    unsafe_scheduler = scheduler_evidence(config, callbacks, deployment=deployment, secret_metadata=secret_metadata)
    unsafe_scheduler["mode"] = "idempotent"
    unsigned = {key: value for key, value in unsafe_scheduler.items() if key != "source_payload_sha256"}
    unsafe_scheduler["source_payload_sha256"] = "sha256:" + hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    scheduler.write_text(json.dumps(unsafe_scheduler), encoding="utf-8")
    rejected_idempotent = subprocess.run(
        [
            *command[: command.index("--output") + 1],
            str(tmp_path / "gray-idempotent.yaml"),
            "--scheduler-evidence",
            str(scheduler),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected_idempotent.returncode != 0
    assert "disabled" in rejected_idempotent.stderr.lower()


def test_prepare_values_binds_scheduler_evidence_to_image_digest(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    callbacks = tmp_path / "callbacks"
    callbacks.mkdir()
    (callbacks / "smoke.py").write_text("def callback():\n    return True\n", encoding="utf-8")
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata: {name: litellm-proxy}
spec:
  template:
    spec:
      terminationGracePeriodSeconds: 600
      containers:
        - name: litellm
          envFrom: [{secretRef: {name: litellm-secrets}}]
          resources: {requests: {cpu: 200m, memory: 1Gi}, limits: {cpu: '2', memory: 4Gi}}
          livenessProbe: &probe {httpGet: {path: /health/liveliness, port: 4000}, initialDelaySeconds: 90, periodSeconds: 15, failureThreshold: 5, timeoutSeconds: 15}
          readinessProbe: *probe
          lifecycle: {preStop: {exec: {command: [sh, -c, sleep 15]}}}
""",
        encoding="utf-8",
    )
    secret_metadata_file = create_secret_metadata(tmp_path, names=["litellm-secrets"])
    secret_metadata = json.loads(secret_metadata_file.read_text(encoding="utf-8"))
    
    scheduler = tmp_path / "scheduler.json"
    scheduler.write_text(
        json.dumps(
            scheduler_evidence(
                config,
                callbacks,
                image_digest="sha256:" + ("b" * 64),
                deployment=deployment,
                secret_metadata=secret_metadata,
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "litellm-gray-rollout" / "scripts" / "prepare-values.py"),
            "--ingress-cidr",
            "10.68.13.242/32",
            "--profile",
            str(ROLLOUT / "values-gray.yaml"),
            "--repository",
            IDC_REGISTRY_PREFIX + "litellm-carher",
            "--digest",
            "sha256:" + ("a" * 64),
            "--config",
            str(config),
            "--callbacks-dir",
            str(callbacks),
            "--deployment",
            str(deployment),
            "--scheduler-evidence",
            str(scheduler),
            "--secret-metadata",
            str(secret_metadata_file),
            "--output",
            str(tmp_path / "gray.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "scheduler evidence" in result.stderr.lower()

    scheduler.write_text(
        json.dumps(
            scheduler_evidence(
                config,
                callbacks,
                image_digest="sha256:" + ("a" * 64),
                deployment=deployment,
                runtime_sha256="sha256:" + ("0" * 64),
            )
        ),
        encoding="utf-8",
    )
    runtime_result = subprocess.run(
        [
            *result.args[:-2],
            "--output",
            str(tmp_path / "runtime-mismatch.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert runtime_result.returncode != 0
    assert "scheduler evidence" in runtime_result.stderr.lower()


def test_prepare_migration_run_replaces_fail_closed_job_commands(tmp_path: Path) -> None:
    ledger = tmp_path / "migration-ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "statements": [
                    {
                        "id": "ddl-001",
                        "sql": 'ALTER TABLE "LiteLLM_ProxyModelTable" ADD COLUMN "gray_gate_probe" TEXT',
                        "online_safe": True,
                        "lock_mode": "ACCESS EXCLUSIVE",
                        "table_rewrite": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "run"
    target_image = IDC_REGISTRY_PREFIX + "litellm-carher@sha256:" + ("a" * 64)
    stable_image = IDC_REGISTRY_PREFIX + "litellm-carher@sha256:" + ("b" * 64)
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "litellm-gray-rollout" / "scripts" / "prepare-migration-run.py"),
            "--migration-template",
            str(ROLLOUT / "migration-job.yaml"),
            "--version-template",
            str(ROLLOUT / "clone-version-test-jobs.yaml"),
            "--target-image",
            target_image,
            "--stable-image",
            stable_image,
            "--migration-ledger",
            str(ledger),
            "--run-id",
            "test-run-1",
            "--generation",
            "gen-1",
            "--output-dir",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rendered = "\n".join(path.read_text() for path in output.glob("*.yaml"))
    assert "exit 64" not in rendered
    assert "replace with" not in rendered.lower()
    assert target_image in rendered and stable_image in rendered
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.glob("*.yaml"))


@pytest.mark.parametrize("profile", PROFILES)
def test_profile_values_pin_identity_port_and_safe_image(profile: str):
    name, node_port, production_route = PROFILES[profile]
    values = load_yaml(ROLLOUT / f"values-{profile}.yaml")

    assert values["nameOverride"] == name
    assert values["service"]["nodePort"] == node_port
    assert values["productionRouteEnabled"] is production_route
    assert values["schemaUpdateEnabled"] is False
    assert values["selectorLabels"] == {"app": name}
    assert values["image"]["repository"].startswith(IDC_REGISTRY_PREFIX)
    assert values["image"]["digest"] not in {ZERO_DIGEST, ONE_DIGEST}
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", values["image"]["digest"])
    assert "tag" not in values["image"]
    assert values["initContainers"] == {"approvedNames": [], "items": []}
    if profile == "gray":
        assert values["nodeSelector"] == {"kubernetes.io/hostname": "aiyjy-litellm-standby"}
        assert values["tolerations"] == [{
            "key": "dedicated",
            "operator": "Equal",
            "value": "standby",
            "effect": "NoSchedule",
        }]
    assert values["workloadContract"] == {
        "containers": ["litellm"],
        "managedInitContainers": [],
        "optionalInitContainers": ["config-check"],
    }
    assert values["backgroundTasks"]["enabled"] is (profile == "prod")


def test_migration_job_is_suspended_fail_closed_and_never_retries():
    documents = load_yaml_documents(ROLLOUT / "migration-job.yaml")
    job = find_doc(documents, "Job", "litellm-clone-schema-migration-template")
    spec = job["spec"]
    pod_spec = spec["template"]["spec"]
    container = pod_spec["containers"][0]

    assert spec["suspend"] is True
    assert spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] == 900
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["automountServiceAccountToken"] is False
    assert job["metadata"]["namespace"] == "litellm-clone"
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert pod_spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["image"].startswith(IDC_REGISTRY_PREFIX)
    assert re.search(r"@sha256:[a-f0-9]{64}$", container["image"])
    env_items = {item["name"]: item for item in container["env"]}
    env = {name: item.get("value") for name, item in env_items.items()}
    assert env["CLONE_DB_HOST"] == "litellm-clone-a"
    assert env_items["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "litellm-clone-a-credentials",
        "key": "DATABASE_URL",
    }
    assert "lock_timeout=5s" in env["PGOPTIONS"]
    assert "statement_timeout=15min" in env["PGOPTIONS"]
    assert "approved migration ledger" in "\n".join(container["args"]).lower()
    assert_restricted_pod_spec(pod_spec)

    production = find_doc(
        documents, "Job", "litellm-production-schema-migration-template"
    )
    production_container = production["spec"]["template"]["spec"]["containers"][0]
    production_env = {item["name"]: item for item in production_container["env"]}
    assert production["metadata"]["namespace"] == "litellm-product"
    assert production["spec"]["suspend"] is True
    assert production_env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "litellm-production-migration-credentials",
        "key": "DATABASE_URL",
    }
    assert "CLONE_DB_HOST" not in production_env
    assert_restricted_pod_spec(production["spec"]["template"]["spec"])


def test_secret_templates_are_placeholder_only_and_immutable():
    documents = load_yaml_documents(ROLLOUT / "clone-secret-templates.yaml")
    expected_names = {
        "litellm-clone-test-master-key",
        "litellm-production-readonly-dump-credentials",
        "litellm-production-migration-credentials",
        "litellm-clone-a-credentials",
        "litellm-clone-b-credentials",
        "litellm-clone-c-credentials",
    }
    assert {doc["metadata"]["name"] for doc in documents} == expected_names
    for secret in documents:
        assert secret["kind"] == "Secret"
        assert secret["immutable"] is True
        assert secret["metadata"]["namespace"] in {"litellm-clone", "litellm-product"}
        assert secret["stringData"]
        if secret["metadata"]["name"] == "litellm-clone-test-master-key":
            # This secret uses a dedicated placeholder for the clone-only master key
            expected_placeholder = "REPLACE_WITH_DEDICATED_CLONE_ONLY_SK_KEY"
            assert all(value == expected_placeholder for value in secret["stringData"].values())
        else:
            assert all(value == "REPLACE_IN_ROOT_ONLY_RUN_DIRECTORY" for value in secret["stringData"].values())


def test_clone_namespace_and_network_policy_are_default_deny_with_scoped_db_access():
    namespace = load_yaml(ROLLOUT / "clone-namespace.yaml")
    assert namespace["kind"] == "Namespace"
    assert namespace["metadata"]["name"] == "litellm-clone"

    policies = load_yaml_documents(ROLLOUT / "clone-networkpolicies.yaml")
    default_deny = find_doc(policies, "NetworkPolicy", "default-deny-all")
    assert default_deny["spec"]["podSelector"] == {}
    assert set(default_deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert default_deny["spec"].get("ingress", []) == []
    assert default_deny["spec"].get("egress", []) == []

    allow_db = find_doc(policies, "NetworkPolicy", "allow-qualified-clients-to-clone-db")
    expressions = allow_db["spec"]["ingress"][0]["from"][0]["podSelector"][
        "matchExpressions"
    ]
    role_rule = next(rule for rule in expressions if rule["key"] == "litellm.carher.io/role")
    assert set(role_rule["values"]) == {"migration", "restore", "version-test", "network-probe"}
    assert allow_db["spec"]["ingress"][0]["ports"] == [
        {"port": 5432, "protocol": "TCP"}
    ]

    allow_dns = find_doc(policies, "NetworkPolicy", "allow-qualified-clients-to-dns")
    dns_roles = allow_dns["spec"]["podSelector"]["matchExpressions"][0]
    assert dns_roles["key"] == "litellm.carher.io/role"
    assert set(dns_roles["values"]) == {
        "dump",
        "migration",
        "restore",
        "version-test",
        "network-probe",
    }
    assert set(allow_dns["spec"]["egress"][0]["ports"][0].values()) == {53, "UDP"}
    assert set(allow_dns["spec"]["egress"][0]["ports"][1].values()) == {53, "TCP"}

    allow_db_egress = find_doc(
        policies, "NetworkPolicy", "allow-qualified-clients-to-clone-db"
    )["spec"]
    assert "Egress" not in allow_db_egress["policyTypes"]
    client_egress = find_doc(
        policies, "NetworkPolicy", "allow-qualified-clients-egress-to-clone-db"
    )
    client_roles = client_egress["spec"]["podSelector"]["matchExpressions"][0]
    assert set(client_roles["values"]) == {
        "migration",
        "restore",
        "version-test",
        "network-probe",
    }
    production_dump = find_doc(
        policies, "NetworkPolicy", "allow-production-dump-readonly-egress"
    )
    assert production_dump["spec"]["podSelector"] == {
        "matchLabels": {"litellm.carher.io/role": "dump"}
    }
    dump_target = production_dump["spec"]["egress"][0]
    assert dump_target["to"] == [
        {
            "namespaceSelector": {
                "matchLabels": {
                    "kubernetes.io/metadata.name": "litellm-product",
                }
            },
            "podSelector": {"matchLabels": {"app": "litellm-db"}},
        }
    ]
    assert dump_target["ports"] == [{"port": 5432, "protocol": "TCP"}]

    redis_ingress = find_doc(
        policies, "NetworkPolicy", "allow-version-tests-to-clone-redis"
    )
    assert redis_ingress["spec"]["podSelector"] == {
        "matchLabels": {"litellm.carher.io/component": "clone-redis"}
    }
    assert redis_ingress["spec"]["ingress"][0]["from"] == [{
        "podSelector": {
            "matchLabels": {"litellm.carher.io/role": "version-test"}
        }
    }]
    assert redis_ingress["spec"]["ingress"][0]["ports"] == [
        {"port": 6379, "protocol": "TCP"}
    ]

    redis_egress = find_doc(
        policies, "NetworkPolicy", "allow-version-tests-egress-to-clone-redis"
    )
    assert redis_egress["spec"]["podSelector"] == {
        "matchLabels": {"litellm.carher.io/role": "version-test"}
    }
    assert redis_egress["spec"]["egress"][0]["to"] == [{
        "podSelector": {
            "matchLabels": {"litellm.carher.io/component": "clone-redis"}
        }
    }]
    assert redis_egress["spec"]["egress"][0]["ports"] == [
        {"port": 6379, "protocol": "TCP"}
    ]


def test_clone_dump_restore_jobs_freeze_pre_and_post_migration_sources():
    documents = load_yaml_documents(ROLLOUT / "clone-dump-restore-jobs.yaml")
    dump_job = find_doc(documents, "Job", "litellm-production-metadata-dump-template")
    dump_spec = dump_job["spec"]["template"]["spec"]
    dump_container = dump_spec["containers"][0]
    assert dump_job["metadata"]["namespace"] == "litellm-clone"
    assert dump_job["spec"]["suspend"] is True
    assert dump_job["spec"]["backoffLimit"] == 0
    assert dump_spec["restartPolicy"] == "Never"
    assert dump_spec["volumes"] == [
        {"name": "dump", "persistentVolumeClaim": {"claimName": "litellm-clone-dump"}}
    ]
    assert dump_container["volumeMounts"] == [{"name": "dump", "mountPath": "/dump"}]
    dump_env = {item["name"]: item for item in dump_container["env"]}
    assert dump_env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == "litellm-production-readonly-dump-credentials"
    dump_command = "\n".join(dump_container["args"])
    assert "pg_dump" in dump_command and "-Fc" in dump_command
    assert "--schema=public" in dump_command
    assert "pg_restore --list" in dump_command
    assert "/dump/litellm-metadata.dump" in dump_command
    assert "sha256sum litellm-metadata.dump" in dump_command
    assert_restricted_pod_spec(dump_spec)

    dump_pvc = find_doc(documents, "PersistentVolumeClaim", "litellm-clone-dump")
    assert dump_pvc["metadata"]["namespace"] == "litellm-clone"

    post_job = find_doc(documents, "Job", "litellm-clone-a-post-migration-dump-template")
    post_spec = post_job["spec"]["template"]["spec"]
    post_container = post_spec["containers"][0]
    post_env = {item["name"]: item for item in post_container["env"]}
    post_command = "\n".join(post_container["args"])
    assert post_job["metadata"]["namespace"] == "litellm-clone"
    assert post_env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == "litellm-clone-a-credentials"
    assert "/dump/litellm-migrated.dump" in post_command
    assert "pg_isready" in post_command and "seq 1 60" in post_command
    assert "pg_dump" in post_command and "pg_restore --list" in post_command
    assert "pg_export_snapshot()" in post_command
    assert "--snapshot=\"$snapshot\"" in post_command
    assert "/dump/migrated-baseline-counts.tsv" in post_command
    assert "--schema=public" in post_command
    assert_restricted_pod_spec(post_spec)

    expected_restores = {
        "litellm-clone-a-restore-template": (
            "litellm-clone-a-credentials",
            "/dump/litellm-metadata.dump",
        ),
        "litellm-clone-b-restore-template": (
            "litellm-clone-b-credentials",
            "/dump/litellm-migrated.dump",
        ),
        "litellm-clone-c-restore-template": (
            "litellm-clone-c-credentials",
            "/dump/litellm-migrated.dump",
        ),
    }
    for name, (secret_name, dump_path) in expected_restores.items():
        job = find_doc(documents, "Job", name)
        pod_spec = job["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        env = {item["name"]: item for item in container["env"]}
        assert job["metadata"]["namespace"] == "litellm-clone"
        assert job["spec"]["suspend"] is True
        assert job["spec"]["backoffLimit"] == 0
        assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == secret_name
        command = "\n".join(container["args"])
        assert "pg_isready" in command
        assert "seq 1 60" in command
        assert "pg_restore" in command
        assert "DROP SCHEMA IF EXISTS public CASCADE" in command
        assert "CREATE SCHEMA public" in command
        assert 'log_rows="$(psql' in command
        assert 'test "$log_rows" = 0' in command
        assert "DO $$" not in command
        assert dump_path in command
        if name in {"litellm-clone-b-restore-template", "litellm-clone-c-restore-template"}:
            assert "migrated-baseline-counts.tsv" in command
            assert "cmp -s /dump/baseline-counts.tsv" not in command
        assert_restricted_pod_spec(pod_spec)


def test_clone_version_jobs_are_pinned_to_standby_node():
    documents = load_yaml_documents(ROLLOUT / "clone-version-test-jobs.yaml")
    jobs = [item for item in documents if item.get("kind") == "Job"]
    assert len(jobs) == 4
    for job in jobs:
        pod_spec = job["spec"]["template"]["spec"]
        assert pod_spec["nodeSelector"] == {
            "kubernetes.io/hostname": "aiyjy-litellm-standby"
        }
        assert pod_spec["tolerations"] == [{
            "key": "dedicated",
            "operator": "Equal",
            "value": "standby",
            "effect": "NoSchedule",
        }]


def assert_restricted_pod_spec(pod_spec: dict) -> None:
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert pod_spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    for container in pod_spec["containers"]:
        security = container["securityContext"]
        assert security["allowPrivilegeEscalation"] is False
        assert security["capabilities"] == {"drop": ["ALL"]}


def test_clone_databases_define_independent_a_b_c_targets_without_credentials():
    documents = load_yaml_documents(ROLLOUT / "clone-databases.yaml")
    names = {"litellm-clone-a", "litellm-clone-b", "litellm-clone-c"}
    for name in names:
        service = find_doc(documents, "Service", name)
        stateful_set = find_doc(documents, "StatefulSet", name)
        container = stateful_set["spec"]["template"]["spec"]["containers"][0]
        assert service["spec"]["type"] == "ClusterIP"
        assert service["spec"]["ports"][0]["port"] == 5432
        assert container["image"].startswith(IDC_REGISTRY_PREFIX)
        assert re.search(r"@sha256:[a-f0-9]{64}$", container["image"])
        assert container["envFrom"] == [{"secretRef": {"name": f"{name}-credentials"}}]
        assert container["startupProbe"]["exec"]["command"][0] == "pg_isready"
        assert container["readinessProbe"]["exec"]["command"][0] == "pg_isready"
        assert_restricted_pod_spec(stateful_set["spec"]["template"]["spec"])

    assert not [doc for doc in documents if doc.get("kind") == "Secret"]


def test_clone_redis_is_ephemeral_pinned_and_immutable_digest_only():
    documents = load_yaml_documents(ROLLOUT / "clone-redis.yaml")
    service = find_doc(documents, "Service", "litellm-clone-redis")
    deployment = find_doc(documents, "Deployment", "litellm-clone-redis")
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [
        {"name": "redis", "port": 6379, "targetPort": "redis"}
    ]
    assert pod_spec["nodeSelector"] == {
        "kubernetes.io/hostname": "aiyjy-litellm-standby"
    }
    assert pod_spec["tolerations"] == [{
        "key": "dedicated",
        "operator": "Equal",
        "value": "standby",
        "effect": "NoSchedule",
    }]
    assert "volumes" not in pod_spec
    assert container["image"].startswith(IDC_REGISTRY_PREFIX)
    assert re.search(r"@sha256:[a-f0-9]{64}$", container["image"])
    assert "--appendonly" in container["args"]
    assert "no" in container["args"]
    assert_restricted_pod_spec(pod_spec)


def test_clone_connectivity_probes_include_allowed_and_unlabelled_attacker():
    documents = load_yaml_documents(ROLLOUT / "clone-connectivity-probes.yaml")
    allowed = find_doc(documents, "Pod", "clone-network-allowed-probe")
    attacker = find_doc(documents, "Pod", "clone-network-attacker-probe")

    assert allowed["metadata"]["labels"]["litellm.carher.io/role"] == "network-probe"
    assert "litellm.carher.io/role" not in attacker["metadata"].get("labels", {})
    assert allowed["spec"]["restartPolicy"] == attacker["spec"]["restartPolicy"] == "Never"
    for pod in (allowed, attacker):
        assert_restricted_pod_spec(pod["spec"])
        image = pod["spec"]["containers"][0]["image"]
        assert image.startswith(IDC_REGISTRY_PREFIX)
        assert re.search(r"@sha256:[a-f0-9]{64}$", image)
    assert "getent hosts litellm-clone-a" in "\n".join(allowed["spec"]["containers"][0]["args"])
    assert "getent hosts litellm-clone-a" in "\n".join(attacker["spec"]["containers"][0]["args"])
    for probe in (allowed, attacker):
        assert probe["spec"]["nodeSelector"] == {
            "kubernetes.io/hostname": "aiyjy-litellm-standby"
        }
        assert probe["spec"]["tolerations"] == [{
            "key": "dedicated",
            "operator": "Equal",
            "value": "standby",
            "effect": "NoSchedule",
        }]
    attacker_command = "\n".join(attacker["spec"]["containers"][0]["args"])
    allowed_command = "\n".join(allowed["spec"]["containers"][0]["args"])
    assert "seq 1 30" in allowed_command
    assert 'test "$attempt" -lt 30' in allowed_command
    assert "unlabelled pod unexpectedly resolved clone DNS" not in attacker_command
    assert "default-deny is not enforced" in attacker_command


def test_rollout_manifests_pin_workload_and_container_sets_fail_closed():
    expected = {
        "migration-job.yaml": {
            ("Job", "litellm-clone-schema-migration-template"): ([], ["migrate"]),
            ("Job", "litellm-production-schema-migration-template"): ([], ["migrate"]),
        },
        "clone-databases.yaml": {
            ("StatefulSet", "litellm-clone-a"): ([], ["postgres"]),
            ("StatefulSet", "litellm-clone-b"): ([], ["postgres"]),
            ("StatefulSet", "litellm-clone-c"): ([], ["postgres"]),
        },
        "clone-dump-restore-jobs.yaml": {
            ("Job", "litellm-production-metadata-dump-template"): ([], ["dump"]),
            ("Job", "litellm-clone-a-restore-template"): ([], ["restore"]),
            ("Job", "litellm-clone-a-post-migration-dump-template"): ([], ["dump"]),
            ("Job", "litellm-clone-b-restore-template"): ([], ["restore"]),
            ("Job", "litellm-clone-c-restore-template"): ([], ["restore"]),
        },
        "clone-storage-probe-job.yaml": {
            ("Job", "litellm-clone-dump-storage-probe"): ([], ["storage-probe"]),
        },
        "clone-readonly-db-probe-job.yaml": {
            ("Job", "litellm-production-readonly-db-probe"): ([], ["readonly-db-probe"]),
        },
        "clone-dump-artifact-cleanup-job.yaml": {
            ("Job", "litellm-clone-dump-artifact-cleanup"): ([], ["cleanup"]),
        },
        "clone-db-data-cleanup-job.yaml": {
            ("Job", "litellm-clone-a-data-cleanup"): ([], ["cleanup"]),
        },
        "clone-version-test-jobs.yaml": {
            ("Job", "litellm-new-version-test"): ([], ["test"]),
            ("Job", "litellm-old-version-test"): ([], ["test"]),
            ("Job", "litellm-concurrent-new-test"): ([], ["test"]),
            ("Job", "litellm-concurrent-old-test"): ([], ["test"]),
        },
    }
    for filename, workload_contract in expected.items():
        documents = load_yaml_documents(ROLLOUT / filename)
        actual = {
            (doc["kind"], doc["metadata"]["name"]): (
                [item["name"] for item in doc["spec"]["template"]["spec"].get("initContainers", [])],
                [item["name"] for item in doc["spec"]["template"]["spec"]["containers"]],
            )
            for doc in documents
            if doc.get("kind") in {"Deployment", "StatefulSet", "DaemonSet", "Job"}
        }
        assert actual == workload_contract


def test_clone_version_jobs_cover_new_old_and_concurrent_compatibility():
    documents = load_yaml_documents(ROLLOUT / "clone-version-test-jobs.yaml")
    expected_targets = {
        "litellm-new-version-test": "litellm-clone-a",
        "litellm-old-version-test": "litellm-clone-b",
        "litellm-concurrent-new-test": "litellm-clone-c",
        "litellm-concurrent-old-test": "litellm-clone-c",
    }
    target_secrets: dict[str, str] = {}
    for name, target in expected_targets.items():
        job = find_doc(documents, "Job", name)
        assert job["spec"]["suspend"] is True
        assert job["spec"]["backoffLimit"] == 0
        assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
        assert_restricted_pod_spec(job["spec"]["template"]["spec"])
        labels = job["spec"]["template"]["metadata"]["labels"]
        assert labels["litellm.carher.io/role"] == "version-test"
        env_items = {
            item["name"]: item
            for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        env = {key: item.get("value") for key, item in env_items.items()}
        assert env["CLONE_DB_HOST"] == target
        target_secrets[name] = env_items["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"]

    assert target_secrets["litellm-new-version-test"] == "litellm-clone-a-credentials"
    assert target_secrets["litellm-old-version-test"] == "litellm-clone-b-credentials"
    assert target_secrets["litellm-concurrent-new-test"] == "litellm-clone-c-credentials"
    assert target_secrets["litellm-concurrent-old-test"] == "litellm-clone-c-credentials"
    assert len(
        {
            target_secrets["litellm-new-version-test"],
            target_secrets["litellm-old-version-test"],
            target_secrets["litellm-concurrent-new-test"],
        }
    ) == 3


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
@pytest.mark.parametrize("profile", PROFILES)
def test_helm_lint_accepts_each_frozen_profile(profile: str):
    command = [
            HELM,
            "lint",
            str(CHART),
            "--values",
            str(ROLLOUT / f"values-{profile}.yaml"),
            "--set",
            "artifactTemplate=false",
            *frozen_scheduler_overrides(profile),
        ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
@pytest.mark.parametrize("profile", PROFILES)
def test_rendered_profiles_have_isolated_selectors_snapshots_and_schema_fuse(profile: str):
    name, node_port, production_route = PROFILES[profile]
    documents = render_profile(profile)
    deployment = find_doc(documents, "Deployment", name)
    service = find_doc(documents, "Service", f"{name}-nodeport")
    network_policy = find_doc(documents, "NetworkPolicy", f"{name}-ingress")
    assert deployment["metadata"]["namespace"] == "litellm-product"
    assert service["metadata"]["namespace"] == "litellm-product"
    pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
    selector = deployment["spec"]["selector"]["matchLabels"]

    assert all(pod_labels[key] == value for key, value in selector.items())
    assert selector == {"app": name}
    assert service["spec"]["selector"] == selector
    assert service["spec"]["ports"][0]["nodePort"] == node_port
    assert network_policy["spec"]["podSelector"] == {"matchLabels": {"app": name}}
    assert network_policy["spec"]["policyTypes"] == ["Ingress"]
    assert network_policy["spec"]["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}},
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "ingress-nginx"}},
                }
            ],
            "ports": [{"port": 4000, "protocol": "TCP"}],
        }
    ]
    # The checked-in template carries selectors only. The host-nginx -> NodePort
    # path arrives with the node address as source, so prepare-values.py welds
    # the node ipBlock in at freeze time; see the --ingress-cidr tests below.
    assert ("carher.net/litellm-production-route" in pod_labels) is production_route

    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "litellm"
    assert container["image"].startswith(IDC_REGISTRY_PREFIX)
    rendered_digest = container["image"].rsplit("@", 1)[1]
    assert rendered_digest not in {ZERO_DIGEST, ONE_DIGEST}
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", rendered_digest)
    assert container["command"] == ["/app/docker/prod_entrypoint.sh"]
    assert container["args"][-2:] == ["--num_workers", "2"]
    assert container["envFrom"] == [
        {"secretRef": {"name": "litellm-secrets"}},
        {"secretRef": {"name": "carher-env-keys"}},
    ]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["DISABLE_SCHEMA_UPDATE"] == "True"

    config_maps = [doc for doc in documents if doc.get("kind") == "ConfigMap"]
    assert len(config_maps) == 2
    assert all(config_map["immutable"] is True for config_map in config_maps)
    assert all(re.search(r"-[a-f0-9]{12}$", config_map["metadata"]["name"]) for config_map in config_maps)
    snapshot_names = {config_map["metadata"]["name"] for config_map in config_maps}
    volume_names = {
        volume["configMap"]["name"]
        for volume in deployment["spec"]["template"]["spec"]["volumes"]
        if "configMap" in volume
    }
    assert volume_names == snapshot_names
    callback_mounts = {
        mount["mountPath"]: mount["subPath"]
        for mount in container["volumeMounts"]
        if mount["name"] == "callbacks"
    }
    assert callback_mounts == {"/app/README.txt": "README.txt"}


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
def test_chart_renders_secret_key_ref_environment_without_exposing_value():
    documents = render_profile(
        "gray",
        "--set-json",
        'extraEnv=[{"name":"CHATGPT_POOL_KEY","valueFrom":{"secretKeyRef":{"name":"chatgpt-pool-master-key","key":"LITELLM_MASTER_KEY"}}}]',
    )
    deployment = find_doc(documents, "Deployment", "litellm-proxy-gray")
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    pool = next(item for item in env if item["name"] == "CHATGPT_POOL_KEY")
    assert pool == {
        "name": "CHATGPT_POOL_KEY",
        "valueFrom": {
            "secretKeyRef": {
                "name": "chatgpt-pool-master-key",
                "key": "LITELLM_MASTER_KEY",
            }
        },
    }


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
def test_chart_renders_config_map_key_ref_and_preserves_other_environment():
    documents = render_profile(
        "gray",
        "--set-json",
        'extraEnv=[{"name":"FEATURE_MODE","value":"gray"},{"name":"CALLBACK_CONFIG","valueFrom":{"configMapKeyRef":{"name":"litellm-runtime-flags","key":"callbacks.yaml","optional":true}}}]',
    )
    deployment = find_doc(documents, "Deployment", "litellm-proxy-gray")
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {item["name"] for item in env} == {
        "DISABLE_SCHEMA_UPDATE",
        "FEATURE_MODE",
        "CALLBACK_CONFIG",
    }
    callback = next(item for item in env if item["name"] == "CALLBACK_CONFIG")
    assert callback == {
        "name": "CALLBACK_CONFIG",
        "valueFrom": {
            "configMapKeyRef": {
                "name": "litellm-runtime-flags",
                "key": "callbacks.yaml",
                "optional": True,
            }
        },
    }


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
@pytest.mark.parametrize(
    "unsafe_env",
    [
        {"name": "AMBIGUOUS", "value": "x", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
        {"name": "MULTI_REF", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}, "configMapKeyRef": {"name": "c", "key": "k"}}},
        {"name": "FIELD_REF", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
    ],
)
def test_chart_rejects_ambiguous_unsupported_or_reserved_environment(
    tmp_path: Path, unsafe_env: dict
):
    result = run_helm_with_values(tmp_path, {"extraEnv": [unsafe_env]})
    assert result.returncode != 0


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
@pytest.mark.parametrize("profile", PROFILES)
def test_scheduler_role_is_bound_to_observed_evidence_not_an_unverified_env_switch(
    profile: str,
):
    documents = render_profile(profile)
    name = PROFILES[profile][0]
    deployment = find_doc(documents, "Deployment", name)
    pod_spec = deployment["spec"]["template"]["spec"]
    env_names = {item["name"] for item in pod_spec["containers"][0]["env"]}
    assert env_names == {"DISABLE_SCHEMA_UPDATE"}
    assert "initContainers" not in pod_spec
    annotations = deployment["spec"]["template"]["metadata"]["annotations"]
    assert re.fullmatch(
        r"sha256:[a-f0-9]{64}",
        annotations["litellm.carher.io/scheduler-evidence-checksum"],
    )
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert labels["litellm.carher.io/background-tasks-enabled"] == str(
        profile == "prod"
    ).lower()


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
@pytest.mark.parametrize(
    "profile,enabled",
    [("prod", False), ("gray", True), ("guarded-old", True)],
)
def test_chart_rejects_scheduler_role_mismatch(tmp_path: Path, profile: str, enabled: bool):
    defaults = load_yaml(CHART / "values.yaml")
    values = {**defaults, **load_yaml(ROLLOUT / f"values-{profile}.yaml")}
    values["artifactTemplate"] = False
    values["backgroundTasks"]["enabled"] = enabled
    config_text = "model_list: []\ngeneral_settings: {}\n"
    values["config"] = {"data": {"config.yaml": config_text}}
    values["schedulerSafety"] = {
        "mode": "primary" if profile == "prod" else "disabled",
        "evidenceSha256": "sha256:" + ("a" * 64),
        "configSha256": "sha256:" + hashlib.sha256(config_text.encode()).hexdigest(),
        "callbacksSha256": value_digest(values["callbacks"]["data"]),
        "runtimeSha256": value_digest(
            {
                "args": values["args"],
                "command": values["command"],
                "extraEnv": values["extraEnv"],
                "secretRefs": values["secretRefs"],
            }
        ),
        "sourcePayloadSha256": "sha256:" + ("b" * 64),
        "capturedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "clone:scheduler-observation",
    }
    result = run_helm_with_values(tmp_path, values)
    assert result.returncode != 0
    assert "backgroundtasks.enabled" in (result.stdout + result.stderr).lower()


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
@pytest.mark.parametrize(
    "override",
    [
        {"image": {"repository": "ghcr.io/berriai/litellm"}},
        {"image": {"repository": ACR_PREFIX + "her/litellm-proxy"}},
        {"image": {"digest": "v1.95.0"}},
        {"service": {"nodePort": 30406}},
        {"schemaUpdateEnabled": True},
        {"podLabels": {"carher.net/litellm-production-route": "true"}},
        {"podAnnotations": {"litellm.carher.io/config-checksum": "forged"}},
        {"workloadContract": {"containers": ["litellm", "hidden-sidecar"]}},
        {
            "initContainers": {
                "approvedNames": ["prisma-migrate"],
                "items": [
                    {
                        "name": "prisma-migrate",
                        "image": {
                            "repository": IDC_REGISTRY_PREFIX + "litellm-carher",
                            "digest": ZERO_DIGEST,
                            "pullPolicy": "IfNotPresent",
                        },
                    }
                ],
            }
        },
    ],
)
def test_helm_rejects_unsafe_or_mismatched_values(tmp_path: Path, override: dict):
    result = run_helm_with_values(tmp_path, override)
    assert result.returncode != 0, result.stdout


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
def test_config_snapshot_name_changes_when_content_changes():
    original = render_profile("gray")
    changed_config = "model_list: [] # changed"
    changed_sha = "sha256:" + hashlib.sha256(changed_config.encode()).hexdigest()
    changed = render_profile(
        "gray",
        "--set-string",
        f"config.data.config\\.yaml={changed_config}",
        "--set-string",
        f"schedulerSafety.configSha256={changed_sha}",
    )
    original_names = {
        doc["metadata"]["name"]
        for doc in original
        if doc.get("kind") == "ConfigMap" and "-config-" in doc["metadata"]["name"]
    }
    changed_names = {
        doc["metadata"]["name"]
        for doc in changed
        if doc.get("kind") == "ConfigMap" and "-config-" in doc["metadata"]["name"]
    }
    assert original_names != changed_names


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
def test_config_snapshot_names_are_release_scoped():
    first = render_profile("gray")
    second = subprocess.run(
        [
            HELM,
            "template",
            "another-gray-release",
            str(CHART),
            "--namespace",
            "litellm-product",
            "--values",
            str(ROLLOUT / "values-gray.yaml"),
            "--set",
            "artifactTemplate=false",
            *frozen_scheduler_overrides("gray"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    second_json = subprocess.run(
        [YQ, "-o=json", "-I=0", "."],
        input=second.stdout,
        check=True,
        capture_output=True,
        text=True,
    )
    second_docs = [json.loads(line) for line in second_json.stdout.splitlines() if line.strip()]
    first_names = {
        doc["metadata"]["name"]
        for doc in first
        if doc.get("kind") == "ConfigMap"
    }
    second_names = {
        doc["metadata"]["name"]
        for doc in second_docs
        if doc.get("kind") == "ConfigMap"
    }
    assert first_names.isdisjoint(second_names)
    assert all(name.startswith("litellm-product-gray-") for name in first_names)
    assert all(name.startswith("another-gray-release-") for name in second_names)


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
def test_only_fixed_config_check_init_container_can_be_enabled():
    documents = render_profile(
        "gray",
        "--set-json",
        'initContainers.approvedNames=["config-check"]',
        "--set-json",
        'initContainers.items=[{"name":"config-check"}]',
    )
    deployment = find_doc(documents, "Deployment", "litellm-proxy-gray")
    init_containers = deployment["spec"]["template"]["spec"]["initContainers"]
    assert {item["name"] for item in init_containers} == {"config-check"}
    config_check = next(item for item in init_containers if item["name"] == "config-check")
    assert config_check["name"] == "config-check"
    assert config_check["image"] == deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    assert "prisma" not in " ".join(config_check["command"] + config_check["args"]).lower()
    assert "migrate" not in " ".join(config_check["command"] + config_check["args"]).lower()
