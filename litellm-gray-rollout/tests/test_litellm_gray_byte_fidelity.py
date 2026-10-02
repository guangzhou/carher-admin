"""Regression tests for the six defects the 2026-09-13 prod rehearsal exposed.

Every one of these was live while `tests/test_litellm_gray_chart.py` passed 276
times. The reason is uniform and worth stating once: **the fixtures there are
all pure ASCII, all LF, and none of them renders prepare-values' own output
through Helm.** Those three properties are exactly the blind spots. A green
suite proved the shape of the fixtures, not the shape of the artefact.

So these tests deliberately use the properties production actually has:
non-ASCII bytes (30 of prod's 33 callbacks), CRLF line endings (prod's
`litellm-passthrough-streaming-handler-patch`), and a full
prepare-values -> helm template -> compare-the-bytes round trip.

See docs/prepare-values-prod-rehearsal-2026-09-13.md §6 and §7.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "litellm-gray-rollout" / "chart"
ROLLOUT = ROOT / "litellm-gray-rollout" / "k8s"
SCRIPTS = ROOT / "litellm-gray-rollout" / "scripts"
PREPARE_VALUES = SCRIPTS / "prepare-values.py"
COLLECT_EVIDENCE = SCRIPTS / "collect-scheduler-evidence.py"
CHECK_POD_SPEC_SHAPE = SCRIPTS / "check-pod-spec-shape.py"
HELM = shutil.which("helm")
IDC_REGISTRY_PREFIX = "127.0.0.1:5000/"
DIGEST = "sha256:" + ("a" * 64)


def _load_prepare_values():
    import importlib.util

    spec = importlib.util.spec_from_file_location("prepare_values", PREPARE_VALUES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREPARE_VALUES_MODULE = _load_prepare_values()

# Both properties are measured, not invented. `模型` and the emoji stand in for
# the Chinese comments and status glyphs that are actually in prod's callbacks;
# what matters is only that the bytes are outside ASCII.
NON_ASCII_CALLBACK = "# 模型路由回调 ✅\ndef callback():\n    return '值'\n"
CRLF_CALLBACK = "def callback():\r\n    return True\r\n"

DEPLOYMENT_YAML = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: litellm-proxy
spec:
  template:
    spec:
      terminationGracePeriodSeconds: 600
      containers:
        - name: litellm
          command: [/app/docker/prod_entrypoint.sh]
          args: [--config, /app/config.yaml, --port, '4000']
          envFrom:
            - secretRef: {name: litellm-secrets}
          livenessProbe:
            httpGet: {path: /health/liveliness, port: 4000}
            initialDelaySeconds: 180
            periodSeconds: 30
            failureThreshold: 10
            timeoutSeconds: 8
          readinessProbe:
            httpGet: {path: /health/readiness, port: 4000}
            initialDelaySeconds: 60
            periodSeconds: 10
            failureThreshold: 12
            timeoutSeconds: 5
          lifecycle:
            preStop:
              exec:
                command: [sh, -c, sleep 30]
"""


def _freeze(
    tmp_path: Path, callbacks: dict[str, str], *, mode: str = "disabled"
) -> tuple[Path, dict]:
    """Run collect-scheduler-evidence + prepare-values on the given callbacks.

    Returns the frozen values path and the parsed prepare-values report. Going
    through the evidence collector rather than hand-building the JSON is the
    point: until 2026-09-13 that file had no producer at all, so nothing ever
    exercised the two encoders agreeing with each other.
    """
    config = tmp_path / "config.yaml"
    config.write_text("model_list:\n  - model_name: 冒烟\n", encoding="utf-8")
    callbacks_dir = tmp_path / "callbacks"
    callbacks_dir.mkdir()
    for name, text in callbacks.items():
        # Bytes, not text mode: writing CRLF through text mode on a platform
        # that translates newlines would defeat the CRLF test silently.
        (callbacks_dir / name).write_bytes(text.encode("utf-8"))
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(DEPLOYMENT_YAML, encoding="utf-8")
    secret_metadata = tmp_path / "secret-meta.json"
    secret_metadata.write_text(
        json.dumps(
            [
                {
                    "name": "litellm-secrets",
                    "uid": "0f7c1d2e-1111-2222-3333-444455556666",
                    "resource_version": "123456",
                    "data_sha256": "sha256:" + ("e" * 64),
                }
            ]
        ),
        encoding="utf-8",
    )

    passthrough = [
        "--profile", str(ROLLOUT / "values-gray.yaml"),
        "--repository", IDC_REGISTRY_PREFIX + "litellm-carher",
        "--digest", DIGEST,
        "--config", str(config),
        "--callbacks-dir", str(callbacks_dir),
        "--deployment", str(deployment),
        "--secret-metadata", str(secret_metadata),
        "--ingress-cidr", "10.42.0.0/32",
    ]

    evidence = tmp_path / "scheduler.json"
    collected = subprocess.run(
        [
            sys.executable, str(COLLECT_EVIDENCE),
            "--mode", mode,
            "--source", "clone:byte-fidelity-fixture",
            "--duplicate-scheduler-runs", "0",
            "--duplicate-background-jobs", "0",
            "--unexpected-control-writes", "0",
            "--evidence-output", str(evidence),
            *passthrough,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stderr

    output = tmp_path / "run" / "values.yaml"
    prepared = subprocess.run(
        [
            sys.executable, str(PREPARE_VALUES),
            *passthrough,
            "--scheduler-role", mode,
            "--scheduler-evidence", str(evidence),
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    return output, json.loads(prepared.stdout)


def _render(values: Path) -> list[dict]:
    result = subprocess.run(
        [
            HELM, "template", "litellm-product-gray", str(CHART),
            "--namespace", "litellm-product",
            "--values", str(values),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _callbacks_configmap(documents: list[dict]) -> dict:
    for doc in documents:
        if doc.get("kind") == "ConfigMap" and "-callbacks-" in doc["metadata"]["name"]:
            return doc
    raise AssertionError("no callbacks ConfigMap in the render")


pytestmark = pytest.mark.skipif(HELM is None, reason="helm is required")


def test_non_ascii_and_crlf_callbacks_survive_freeze_and_render(tmp_path: Path):
    """Freeze -> helm template -> compare the bytes, with real-world content.

    Two defects converge here. The digest encoding must be `toRawJson`'s, i.e.
    `ensure_ascii=False`: with Python's default every non-ASCII byte becomes
    `\\uXXXX`, so `callbacksSha256` can never equal what the chart recomputes
    and `helm template` fails unconditionally. Measured 2026-09-13, 30 of
    prod's 33 callbacks are non-ASCII, so the real values file could not render
    at all -- while the ASCII-only fixtures elsewhere never noticed.
    """
    values, report = _freeze(
        tmp_path,
        {
            "unicode_cb.py": NON_ASCII_CALLBACK,
            "crlf_cb.py": CRLF_CALLBACK,
            "plain_cb.py": "def callback():\n    return True\n",
        },
    )
    assert report["status"] == "PASS"

    frozen = yaml.safe_load(values.read_text(encoding="utf-8"))["callbacks"]["data"]

    # CRLF is content. `Path.read_text()` opens in text mode, so universal
    # newline translation silently rewrites every CRLF to LF -- in the one tool
    # whose entire job is to freeze production bytes exactly. Prod's
    # `litellm-passthrough-streaming-handler-patch` is genuinely CRLF (14016 B;
    # 13728 B once normalised), and it overlays a site-packages module. Python
    # tolerates either ending, so nothing crashes: the file that runs simply
    # stops being the file that was audited.
    assert frozen["crlf_cb.py"] == CRLF_CALLBACK
    assert frozen["unicode_cb.py"] == NON_ASCII_CALLBACK

    documents = _render(values)
    rendered = _callbacks_configmap(documents)["data"]
    # `None` entries are the tombstones that delete chart-default placeholder
    # keys (see the shadowing test below); they are instructions, not content.
    content = {key: text for key, text in frozen.items() if text is not None}
    assert content, "fixture froze nothing"
    # Byte-for-byte, after a full round trip through Helm's YAML emitter. The
    # previous `{{ $key }}: |-` + `{{ $value | nindent 4 }}` idiom prepended a
    # newline and dropped the trailing one -- same length, every file mangled.
    assert rendered == content
    assert rendered["crlf_cb.py"] == CRLF_CALLBACK
    assert rendered["unicode_cb.py"] == NON_ASCII_CALLBACK


def test_chart_default_content_keys_do_not_ride_into_a_frozen_render(tmp_path: Path):
    """Helm merges user values *over* chart defaults, additively for maps.

    A key that exists only in `chart/values.yaml` survives into the render even
    though the frozen values file never mentions it. Measured 2026-09-13: a
    values file carrying prod's 33 callbacks rendered 34 keys, the extra one
    being the placeholder `callbacks.data["README.txt"]` -- and the chart
    mounts every callbacks key, so it would have been mounted into production.
    """
    defaults = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    default_keys = set(defaults["callbacks"]["data"])
    assert default_keys, "fixture assumes the chart ships at least one placeholder"

    values, report = _freeze(tmp_path, {"only_cb.py": "def callback():\n    return 1\n"})
    assert set(report["shadowed_chart_keys"]) == {f"callbacks.{k}" for k in default_keys}
    # The report counts what was frozen, not what was deleted.
    assert report["callbacks"] == 1

    rendered = _callbacks_configmap(_render(values))["data"]
    assert set(rendered) == {"only_cb.py"}


def test_live_configmap_capture_is_parsed_as_a_list_and_empty_is_an_error(tmp_path: Path):
    """`kubectl get configmap -o yaml` emits one `kind: List`, not a stream.

    The loader walked straight past every object and reported zero *with no
    error*, so the content-digest feature was dead on arrival against its own
    documented capture command. The cost of that silence is not a crash: it is
    48 "yes, same bytes" approvals handed to the operator, which is the exact
    rubber stamp the digesting exists to prevent.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_pod_spec_shape_under_test", CHECK_POD_SPEC_SHAPE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    capture = tmp_path / "cms.yaml"
    capture.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {
                            "name": "litellm-callbacks",
                            # The loader demands the fields only the apiserver
                            # writes, so a *rendered* ConfigMap cannot be passed
                            # off as the live capture -- that would fabricate
                            # content matches and turn a genuine content swap
                            # into a proven-inert difference.
                            "namespace": "litellm-product",
                            "uid": "11111111-2222-3333-4444-555555555555",
                            "resourceVersion": "98765",
                            "creationTimestamp": "2026-09-13T00:00:00Z",
                        },
                        "data": {"a.py": "x\n"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    found, errors = module.load_configmaps(capture, "litellm-product", expect_live=True)
    assert errors == []
    assert found["litellm-callbacks"]["a.py"] == "x\n"

    empty = tmp_path / "empty.yaml"
    empty.write_text("apiVersion: v1\nkind: List\nitems: []\n", encoding="utf-8")
    found, errors = module.load_configmaps(empty, "litellm-product", expect_live=True)
    assert found == {}
    # A capture that parses to nothing is a broken capture, not a cluster with
    # no ConfigMaps. Staying quiet here only costs extra approvals, which is why
    # it would have survived review indefinitely while the gate did nothing.
    assert "LIVE_CONFIGMAPS_CAPTURE_IS_EMPTY" in errors


def test_scheduler_evidence_refuses_to_invent_observations(tmp_path: Path):
    """The three counts are cluster facts; a zero you did not measure reads green."""
    result = subprocess.run(
        [
            sys.executable, str(COLLECT_EVIDENCE),
            "--mode", "disabled",
            "--source", "clone:x",
            "--evidence-output", str(tmp_path / "e.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "duplicate-scheduler-runs" in result.stderr


def test_scheduler_evidence_never_overwrites(tmp_path: Path):
    existing = tmp_path / "scheduler.json"
    existing.write_text("{}", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(COLLECT_EVIDENCE),
            "--mode", "disabled",
            "--source", "clone:x",
            "--duplicate-scheduler-runs", "0",
            "--duplicate-background-jobs", "0",
            "--unexpected-control-writes", "0",
            "--evidence-output", str(existing),
            "--profile", str(ROLLOUT / "values-gray.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert existing.read_text(encoding="utf-8") == "{}"


def load_prepare_values():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "prepare_values_under_test", Path(__file__).resolve().parents[1] / "scripts" / "prepare-values.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_typing_annotation_pair_is_not_an_inline_credential():
    # Upstream streaming_handler.py (1.100.1 and 1.103.2) declares this TypedDict
    # field; it blocked the site-packages overlay snapshot on 2026-10-02.
    pv = load_prepare_values()
    assert pv.find_inline_credential("    token: NotRequired[Mapping[str, str]]\n") is None
    assert pv.find_inline_credential("secret: Optional[dict[str, str]]\n") is None


def test_annotation_exemption_does_not_hide_real_credentials():
    pv = load_prepare_values()
    # Negative controls: the exemption must not swallow anything value-shaped.
    assert pv.find_inline_credential("token: abcdefgh12345\n") is not None
    assert pv.find_inline_credential("api_key: NotRequiredxyz123\n") is not None
    assert pv.find_inline_credential("password: Mapping-not-a-type\n") is not None
    assert pv.find_inline_credential('headers = {"Authorization": "Bearer abcdefgh12345678"}') is not None
    assert pv.find_inline_credential("key = 'sk-abcdefgh12345678'") is not None


# --- background-task handover (2026-10-02) ---------------------------------
# gray used to be hard-wired to "scheduler disabled". That made "gray becomes
# the next prod" impossible without hand-editing the live lane: 2055 keys carry
# a daily budget_duration, and a promoted gray with the jobs off would never
# reset them. The handover re-freezes gray as primary; these tests pin both the
# new path and the guards that keep exactly one lane primary.


def test_gray_frozen_as_primary_runs_every_background_task(tmp_path: Path):
    values_path, report = _freeze(tmp_path, {"smoke.py": "x = 1\n"}, mode="primary")
    values = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    assert report["background_tasks"] == {
        "enabled": True,
        "role": "primary",
        "pinned_env": [],
        "reset_budget_overlaid": False,
    }
    assert values["backgroundTasks"] == {"enabled": True}
    assert values["schedulerSafety"]["mode"] == "primary"
    config = yaml.safe_load(values["config"]["data"]["config.yaml"])
    assert "disable_reset_budget" not in (config.get("general_settings") or {})
    env = {entry["name"]: entry.get("value") for entry in values["extraEnv"]}
    for name, off in PREPARE_VALUES_MODULE.BACKGROUND_TASK_SUPPRESSORS.items():
        assert env.get(name) != off, name
    if HELM is not None:
        documents = _render(values_path)
        deployment = next(d for d in documents if d.get("kind") == "Deployment")
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert labels["litellm.carher.io/background-tasks-enabled"] == "true"
        # Taking over the scheduler is not taking over the route; the route
        # label stays prod-only and moves by the routing transaction.
        assert "carher.net/litellm-production-route" not in labels


def test_gray_default_role_is_still_disabled(tmp_path: Path):
    # Negative control for the test above: without the explicit flag nothing
    # changed, so a normal gray run still mutes every job.
    values_path, report = _freeze(tmp_path, {"smoke.py": "x = 1\n"})
    assert report["background_tasks"]["enabled"] is False
    assert report["background_tasks"]["reset_budget_overlaid"] is True
    assert sorted(report["background_tasks"]["pinned_env"]) == sorted(
        PREPARE_VALUES_MODULE.BACKGROUND_TASK_SUPPRESSORS
    )


@pytest.mark.parametrize("profile", ["values-guarded-old.yaml"])
def test_guarded_old_can_never_be_frozen_as_primary(tmp_path: Path, profile: str):
    result = subprocess.run(
        [
            sys.executable, str(PREPARE_VALUES),
            "--profile", str(ROLLOUT / profile),
            "--scheduler-role", "primary",
            "--repository", IDC_REGISTRY_PREFIX + "litellm-carher",
            "--digest", DIGEST,
            "--config", str(tmp_path / "missing.yaml"),
            "--callbacks-dir", str(tmp_path),
            "--deployment", str(tmp_path / "missing.yaml"),
            "--secret-metadata", str(tmp_path / "missing.json"),
            "--ingress-cidr", "10.42.0.0/32",
            "--emit-bindings",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "cannot be frozen with scheduler role 'primary'" in result.stderr + result.stdout


def test_collector_refuses_a_second_role_flag(tmp_path: Path):
    result = subprocess.run(
        [
            sys.executable, str(COLLECT_EVIDENCE),
            "--mode", "disabled",
            "--source", "clone:x",
            "--duplicate-scheduler-runs", "0",
            "--duplicate-background-jobs", "0",
            "--unexpected-control-writes", "0",
            "--evidence-output", str(tmp_path / "e.json"),
            "--scheduler-role", "primary",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "pass the role once" in result.stderr
