#!/usr/bin/env python3
"""Repair and converge H75 Her runtime URLs after operator/profile changes.

Default mode is read-only audit. Use --apply to patch Deployment templates and
--rollout to roll pods in controlled waves with maxSurge=0/maxUnavailable=1.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


NAMESPACE = "carher"
SELECTOR = "app=carher-user"
INTERNAL_DIFY = "http://dify-nginx.dify.svc.cluster.local"
INTERNAL_BOOTSTRAP = (
    "http://dify-bootstrap.dify.svc.cluster.local:5688/v1/bootstrap/carher-bot"
)
INTERNAL_LITELLM = "http://litellm-proxy.carher.svc.cluster.local:4000/v1"
REDIS_URL = "redis://carher-redis.carher.svc:6379"
POSTSTART_SCRIPT = "/opt/data/.hermes/h75-runtime-poststart.sh"
TITLE_PATCH_MARKER = "CARHER_TITLE_FAILURE_SILENT_PATCH"
PUBLIC_SIGNATURES = (
    "https://dify-k8s.carher.net",
    "https://litellm.carher.net/v1",
)
SENSITIVE_DEPLOYMENTS = {"carher-2"}
USE_API = False
KUBECTL_BIN = os.environ.get("KUBECTL_BIN", "kubectl")
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

REQUIRED_ENV = {
    "REDIS_URL": REDIS_URL,
    "OPENAI_BASE_URL": INTERNAL_LITELLM,
    "CARHER_HERMES_CONFIG_TEMPLATE": "/opt/data/.hermes/config-litellm.yaml",
    "CARHER_DIFY_BASE_URL": INTERNAL_DIFY,
    "CARHER_DIFY_BOOTSTRAP_URL": INTERNAL_BOOTSTRAP,
    "CARHER_DIFY_CODEX_BASE_URL": INTERNAL_LITELLM,
    "FEISHU_GROUP_POLICY": "open",
    "FEISHU_ALLOW_ALL_USERS": "true",
    "CARHER_RUNTIME_PLUGINS_REFRESH": "0",
}


@dataclass
class DeploymentState:
    name: str
    h75: bool
    paused: bool
    replicas: int
    ready: int
    updated: int
    unavailable: int
    strategy: dict[str, Any]
    bad_env: list[str]
    missing_env: list[str]
    public_url_in_template: bool
    surge_rs: bool
    pod_config_checked: bool = False
    pod_config_bad: bool = False
    hermes_config_checked: bool = False
    hermes_config_bad: bool = False
    title_patch_checked: bool = False
    title_patch_bad: bool = False

    @property
    def needs_template_patch(self) -> bool:
        return bool(self.bad_env or self.missing_env or self.public_url_in_template)

    @property
    def needs_rollout(self) -> bool:
        return (
            self.needs_template_patch
            or self.paused
            or self.ready < self.replicas
            or self.updated < self.replicas
            or self.unavailable > 0
            or self.surge_rs
            or self.pod_config_bad
            or self.hermes_config_bad
            or self.title_patch_bad
        )


def run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit {proc.returncode}"
        raise RuntimeError(f"{' '.join(cmd)} failed: {detail}")
    return proc


def compact_output(value: str | None, *, limit: int = 800) -> str:
    text = (value or "").strip().replace("\t", " ").replace("\r", "")
    text = ",".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        return "no_output"
    if len(text) > limit:
        return text[:limit] + "...truncated"
    return text


def has_kubectl() -> bool:
    if os.path.exists(KUBECTL_BIN) and os.access(KUBECTL_BIN, os.X_OK):
        return True
    return run(["sh", "-lc", f"command -v {KUBECTL_BIN} >/dev/null 2>&1"], check=False).returncode == 0


def api_base() -> str:
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    return f"https://{host}:{port}"


def api_headers(method: str, patch_kind: str = "merge") -> dict[str, str]:
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    with open(token_path, "r", encoding="utf-8") as handle:
        token = handle.read().strip()
    if method == "PATCH" and patch_kind == "strategic":
        content_type = "application/strategic-merge-patch+json"
    elif method == "PATCH":
        content_type = "application/merge-patch+json"
    else:
        content_type = "application/json"
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": content_type,
    }


def api_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    patch_kind: str = "merge",
    check: bool = True,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        api_base() + path,
        data=data,
        headers=api_headers(method, patch_kind),
        method=method,
    )
    try:
        import ssl

        ctx = ssl.create_default_context(
            cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        )
    except Exception:
        ctx = None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        if not check:
            return {"error": text, "code": exc.code}
        raise RuntimeError(f"{method} {path} -> {exc.code}: {text}") from exc


def kubectl(args: list[str], *, namespace: str, check: bool = True) -> str:
    cmd = [KUBECTL_BIN, "-n", namespace, *args]
    return run(cmd, check=check).stdout or ""


def load_json(args: list[str], *, namespace: str) -> Any:
    return json.loads(kubectl([*args, "-o", "json"], namespace=namespace))


def deployment_selector(namespace: str, deployment: str) -> str:
    deploy = load_json(["get", "deployment", deployment], namespace=namespace)
    labels = deploy.get("spec", {}).get("selector", {}).get("matchLabels") or {}
    if not labels:
        return f"app=carher-user"
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def pod_is_ready_running(pod: dict[str, Any]) -> bool:
    metadata = pod.get("metadata", {})
    status = pod.get("status", {})
    if metadata.get("deletionTimestamp"):
        return False
    if status.get("phase") != "Running":
        return False
    return any(
        cond.get("type") == "Ready" and cond.get("status") == "True"
        for cond in status.get("conditions") or []
    )


def ready_running_pods_for_deployment(namespace: str, deployment: str) -> list[str]:
    selectors = [
        deployment_selector(namespace, deployment),
        f"app=carher-user,instance={deployment}",
        f"app=carher-user,app.kubernetes.io/instance={deployment}",
    ]
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for selector in selectors:
        pods = load_json(
            [
                "get",
                "pod",
                "-l",
                selector,
                "--field-selector=status.phase=Running",
            ],
            namespace=namespace,
        )
        for pod in pods.get("items", []):
            name = pod.get("metadata", {}).get("name", "")
            if not name.startswith(f"{deployment}-") or name in seen:
                continue
            if not pod_is_ready_running(pod):
                continue
            seen.add(name)
            created = pod.get("metadata", {}).get("creationTimestamp", "")
            candidates.append((created, name))
        if candidates:
            break
    candidates.sort(reverse=True)
    return [name for _, name in candidates]


def carher_container(deploy: dict[str, Any]) -> dict[str, Any]:
    containers = deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        if container.get("name") == "carher":
            return container
    return {}


def env_map(container: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in container.get("env") or []:
        if "value" in item:
            env[item.get("name", "")] = item["value"]
    return env


def is_h75_deployment(deploy: dict[str, Any]) -> bool:
    text = json.dumps(deploy.get("spec", {}).get("template", {}), ensure_ascii=False)
    annotations = deploy.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations") or {}
    container = carher_container(deploy)
    image = container.get("image", "")
    return (
        "h75" in image
        or annotations.get("carher.io/runtime-profile") == "h75-openclaw"
        or "carher-base-config-h75" in text
    )


def int_status(deploy: dict[str, Any], key: str) -> int:
    return int(deploy.get("status", {}).get(key) or 0)


def deployment_state(deploy: dict[str, Any], surge_names: set[str]) -> DeploymentState:
    name = deploy["metadata"]["name"]
    container = carher_container(deploy)
    env = env_map(container)
    bad_env = [key for key, value in REQUIRED_ENV.items() if env.get(key) not in (None, value)]
    missing_env = [key for key in REQUIRED_ENV if key not in env]
    template_text = json.dumps(deploy.get("spec", {}).get("template", {}), ensure_ascii=False)
    spec = deploy.get("spec", {})
    status = deploy.get("status", {})
    return DeploymentState(
        name=name,
        h75=is_h75_deployment(deploy),
        paused=bool(spec.get("paused", False)),
        replicas=int(spec.get("replicas") or 0),
        ready=int(status.get("readyReplicas") or 0),
        updated=int(status.get("updatedReplicas") or 0),
        unavailable=int(status.get("unavailableReplicas") or 0),
        strategy=spec.get("strategy") or {},
        bad_env=bad_env,
        missing_env=missing_env,
        public_url_in_template=any(sig in template_text for sig in PUBLIC_SIGNATURES),
        surge_rs=name in surge_names,
    )


def list_deployments(namespace: str, selector: str) -> list[dict[str, Any]]:
    if USE_API:
        path = f"/apis/apps/v1/namespaces/{namespace}/deployments?labelSelector={selector}"
        data = api_request("GET", path)
        return sorted(data.get("items", []), key=lambda item: item["metadata"]["name"])
    data = load_json(["get", "deploy", "-l", selector], namespace=namespace)
    return sorted(data.get("items", []), key=lambda item: item["metadata"]["name"])


def surge_deployments(namespace: str, selector: str) -> set[str]:
    if USE_API:
        path = f"/apis/apps/v1/namespaces/{namespace}/replicasets?labelSelector={selector}"
        data = api_request("GET", path, check=False)
        if data.get("code"):
            print(f"warn\treplicaset_scan_skipped\t{data.get('code')}")
            return set()
    else:
        try:
            data = load_json(["get", "rs", "-l", selector], namespace=namespace)
        except RuntimeError as exc:
            print(f"warn\treplicaset_scan_skipped\t{str(exc)[:200]}")
            return set()
    surge = set()
    for rs in data.get("items", []):
        owner_refs = rs.get("metadata", {}).get("ownerReferences") or []
        deploy_name = next((ref.get("name") for ref in owner_refs if ref.get("kind") == "Deployment"), "")
        spec_replicas = int(rs.get("spec", {}).get("replicas") or 0)
        ready = int(rs.get("status", {}).get("readyReplicas") or 0)
        if deploy_name and spec_replicas > ready:
            surge.add(deploy_name)
    return surge


def target_filter(states: list[DeploymentState], args: argparse.Namespace) -> list[DeploymentState]:
    requested = {f"carher-{item}" if item.isdigit() else item for item in args.targets}
    excluded = set(args.exclude)
    if not args.include_sensitive:
        excluded |= SENSITIVE_DEPLOYMENTS
    out = []
    for state in states:
        if not state.h75:
            continue
        if requested and state.name not in requested:
            continue
        if state.name in excluded:
            continue
        if args.only_needs_fix and not state.needs_rollout:
            continue
        out.append(state)
    return out


def print_summary(states: list[DeploymentState]) -> None:
    total = len(states)
    bad_templates = sum(1 for s in states if s.needs_template_patch)
    needs_rollout = sum(1 for s in states if s.needs_rollout)
    paused = sum(1 for s in states if s.paused)
    surge = sum(1 for s in states if s.surge_rs)
    unavailable = sum(1 for s in states if s.unavailable > 0 or s.ready < s.replicas)
    pod_config_bad = sum(1 for s in states if s.pod_config_bad)
    hermes_config_bad = sum(1 for s in states if s.hermes_config_bad)
    title_patch_bad = sum(1 for s in states if s.title_patch_bad)
    print(
        "summary\t"
        f"h75={total}\tbad_templates={bad_templates}\tneeds_rollout={needs_rollout}\t"
        f"paused={paused}\tsurge_rs={surge}\tunavailable={unavailable}\t"
        f"pod_config_bad={pod_config_bad}\thermes_config_bad={hermes_config_bad}\t"
        f"title_patch_bad={title_patch_bad}"
    )


def print_table(states: list[DeploymentState]) -> None:
    print(
        "deployment\th75\tbad_env\tmissing_env\tpublic_url\tpaused\t"
        "replicas\tready\tupdated\tunavailable\tsurge_rs\t"
        "pod_config_checked\tpod_config_bad\thermes_config_checked\t"
        "hermes_config_bad\ttitle_patch_checked\ttitle_patch_bad\tneeds_rollout"
    )
    for state in states:
        if not state.h75:
            continue
        print(
            f"{state.name}\t{state.h75}\t{','.join(state.bad_env) or '-'}\t"
            f"{','.join(state.missing_env) or '-'}\t{state.public_url_in_template}\t"
            f"{state.paused}\t{state.replicas}\t{state.ready}\t{state.updated}\t"
            f"{state.unavailable}\t{state.surge_rs}\t{state.pod_config_checked}\t"
            f"{state.pod_config_bad}\t{state.hermes_config_checked}\t"
            f"{state.hermes_config_bad}\t{state.title_patch_checked}\t"
            f"{state.title_patch_bad}\t{state.needs_rollout}"
        )


def patch_template_env(namespace: str, deployment: str) -> None:
    if USE_API:
        existing = api_request(
            "GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"
        )
        container = carher_container(existing)
        env_by_name = {item.get("name"): dict(item) for item in container.get("env") or []}
        for key, value in REQUIRED_ENV.items():
            env_by_name[key] = {"name": key, "value": value}
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "carher",
                                "env": list(env_by_name.values()),
                            }
                        ]
                    }
                }
            }
        }
        api_request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}",
            body=patch,
            patch_kind="strategic",
        )
        return
    env_args = [f"{key}={value}" for key, value in REQUIRED_ENV.items()]
    kubectl(["set", "env", f"deployment/{deployment}", *env_args], namespace=namespace)


def patch_poststart(namespace: str, deployment: str) -> None:
    if USE_API:
        existing = api_request(
            "GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"
        )
    else:
        existing = load_json(["get", "deploy", deployment], namespace=namespace)
    container = carher_container(existing)
    lifecycle = dict(container.get("lifecycle") or {})
    lifecycle["postStart"] = {
        "exec": {
            "command": [
                "sh",
                "-lc",
                f"if [ -x {POSTSTART_SCRIPT} ]; then {POSTSTART_SCRIPT} || true; fi",
            ]
        }
    }
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "carher",
                            "lifecycle": lifecycle,
                        }
                    ]
                }
            }
        }
    }
    if USE_API:
        api_request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}",
            body=patch,
            patch_kind="strategic",
        )
    else:
        kubectl(["patch", "deploy", deployment, "--type=strategic", "-p", json.dumps(patch)], namespace=namespace)


def patch_strategy(namespace: str, deployment: str, max_surge: str, max_unavailable: str) -> None:
    max_surge_value: int | str = int(max_surge) if max_surge.isdigit() else max_surge
    max_unavailable_value: int | str = (
        int(max_unavailable) if max_unavailable.isdigit() else max_unavailable
    )
    patch = {
        "spec": {
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {
                    "maxSurge": max_surge_value,
                    "maxUnavailable": max_unavailable_value,
                },
            }
        }
    }
    if USE_API:
        api_request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}",
            body=patch,
        )
    else:
        kubectl(["patch", "deploy", deployment, "--type=merge", "-p", json.dumps(patch)], namespace=namespace)


def unpause(namespace: str, deployment: str) -> None:
    if USE_API:
        patch = {"spec": {"paused": False}}
        api_request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}",
            body=patch,
        )
        return
    kubectl(["rollout", "resume", f"deployment/{deployment}"], namespace=namespace, check=False)
    patch = {"spec": {"paused": False}}
    kubectl(["patch", "deploy", deployment, "--type=merge", "-p", json.dumps(patch)], namespace=namespace)


def rollout_restart(namespace: str, deployment: str) -> None:
    if USE_API:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now,
                        }
                    }
                }
            }
        }
        api_request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}",
            body=patch,
        )
        return
    kubectl(["rollout", "restart", f"deployment/{deployment}"], namespace=namespace)


def rollout_status(namespace: str, deployment: str, timeout: int) -> bool:
    if USE_API:
        started = time.time()
        while time.time() - started < timeout:
            data = api_request(
                "GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"
            )
            spec = data.get("spec", {})
            status = data.get("status", {})
            generation = int(data.get("metadata", {}).get("generation") or 0)
            observed = int(status.get("observedGeneration") or 0)
            replicas = int(spec.get("replicas") or 0)
            updated = int(status.get("updatedReplicas") or 0)
            ready = int(status.get("readyReplicas") or 0)
            unavailable = int(status.get("unavailableReplicas") or 0)
            if observed >= generation and updated >= replicas and ready >= replicas and unavailable == 0:
                return True
            time.sleep(5)
        print(f"rollout_failed\t{deployment}\ttimeout={timeout}s")
        return False
    proc = run(
        [
            KUBECTL_BIN,
            "-n",
            namespace,
            "rollout",
            "status",
            f"deployment/{deployment}",
            f"--timeout={timeout}s",
        ],
        check=False,
    )
    if proc.returncode == 0:
        return True
    print(f"rollout_failed\t{deployment}\t{(proc.stderr or proc.stdout or '').strip()}")
    return False


def patch_generated_config(namespace: str, deployment: str) -> bool:
    if USE_API:
        print(f"error\tpod_config_patch_requires_kubectl_exec\t{deployment}")
        return False
    pod_names = wait_running_pods_for_deployment(namespace, deployment)
    if not pod_names:
        print(f"warn\tpod_config_patch_no_ready_running_pod\t{deployment}")
        return False
    for name in pod_names:
        script = (
            "python3 - <<'PY'\n"
            "import json, os, pathlib, urllib.request\n"
            "p=pathlib.Path('/data/.openclaw/workflow/dify-config.json')\n"
            "if not p.exists():\n"
            "    raise SystemExit(0)\n"
            "data=json.loads(p.read_text())\n"
            f"deployment={deployment!r}\n"
            "def lifecycle_status(config):\n"
            "    try:\n"
            "        base=str(config.get('lifecycle_base_url') or '').rstrip('/')\n"
            "        token=str(config.get('lifecycle_token') or '')\n"
            "        workspace=str(config.get('workspace_id') or '')\n"
            "        if not base or not token or not workspace:\n"
            "            return 0\n"
            "        req=urllib.request.Request(\n"
            "            base + '/health',\n"
            "            headers={'Authorization': 'Bearer '+token, 'X-CarHer-Dify-Workspace': workspace, 'X-CarHer-Bot-Id': deployment, 'Accept': 'application/json'},\n"
            "            method='GET',\n"
            "        )\n"
            "        with urllib.request.urlopen(req, timeout=20) as resp:\n"
            "            return resp.status\n"
            "    except urllib.error.HTTPError as exc:\n"
            "        return exc.code\n"
            "    except Exception:\n"
            "        return 0\n"
            "needs_bootstrap = (\n"
            "    data.get('bot_id') != deployment\n"
            "    or not data.get('workspace_id')\n"
            "    or not data.get('api_key')\n"
            "    or not data.get('lifecycle_token')\n"
            "    or lifecycle_status(data) != 200\n"
            ")\n"
            "if needs_bootstrap:\n"
            "    bootstrap_url=os.environ.get('CARHER_DIFY_BOOTSTRAP_URL','')\n"
            "    bootstrap_token=os.environ.get('CARHER_DIFY_BOOTSTRAP_TOKEN','')\n"
            "    if not bootstrap_url or not bootstrap_token:\n"
            "        print('missing_bootstrap_env')\n"
            "        raise SystemExit(1)\n"
            "    body=json.dumps({'bot_id': deployment}).encode('utf-8')\n"
            "    req=urllib.request.Request(\n"
            "        bootstrap_url,\n"
            "        data=body,\n"
            "        headers={'Authorization': 'Bearer '+bootstrap_token, 'Content-Type': 'application/json', 'Accept': 'application/json'},\n"
            "        method='POST',\n"
            "    )\n"
            "    with urllib.request.urlopen(req, timeout=90) as resp:\n"
            "        boot=json.loads(resp.read().decode('utf-8'))\n"
            "    for key in ['api_key','workspace_id','lifecycle_token','bot_id','codex_model']:\n"
            "        if boot.get(key):\n"
            "            data[key]=boot[key]\n"
            "    if boot.get('mcp_registry_path') and not data.get('mcp_registry'):\n"
            "        data['mcp_registry']=boot['mcp_registry_path']\n"
            f"data['dify_base_url']={INTERNAL_DIFY!r}\n"
            "data['bot_id']=deployment\n"
            f"data['lifecycle_base_url']='http://dify-bootstrap.dify.svc.cluster.local:5688/v1/lifecycle/{deployment}'\n"
            f"data['codex_base_url']={INTERNAL_LITELLM!r}\n"
            "p.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\\n')\n"
            "PY"
        )
        result = run(
            [KUBECTL_BIN, "-n", namespace, "exec", name, "-c", "carher", "--", "sh", "-lc", script],
            check=False,
        )
        if result.returncode != 0:
            detail = compact_output((result.stderr or "") + "\n" + (result.stdout or ""))
            print(f"error\tpod_config_patch_failed\t{deployment}\t{name}\t{detail}")
            return False
        print(f"pod_config_patch\t{deployment}\t{name}\tok")
    return True


def running_pods_for_deployment(namespace: str, deployment: str) -> list[str]:
    return ready_running_pods_for_deployment(namespace, deployment)


def wait_running_pods_for_deployment(namespace: str, deployment: str, timeout: int = 120) -> list[str]:
    started = time.time()
    while time.time() - started < timeout:
        pod_names = running_pods_for_deployment(namespace, deployment)
        if pod_names:
            return pod_names
        time.sleep(5)
    return []


def check_generated_config(namespace: str, deployment: str) -> bool | None:
    if USE_API:
        print(f"warn\tpod_config_check_requires_kubectl_exec\t{deployment}")
        return None
    pod_names = running_pods_for_deployment(namespace, deployment)
    if not pod_names:
        print(f"warn\tpod_config_check_no_running_pod\t{deployment}")
        return None
    script = (
        "python3 - <<'PY'\n"
        "import json, pathlib, sys, urllib.error, urllib.request\n"
        "p=pathlib.Path('/data/.openclaw/workflow/dify-config.json')\n"
        "if not p.exists():\n"
        "    print('missing_config')\n"
        "    raise SystemExit(1)\n"
        "data=json.loads(p.read_text())\n"
        "bad=[]\n"
        f"expected={{'dify_base_url':{INTERNAL_DIFY!r},'codex_base_url':{INTERNAL_LITELLM!r}}}\n"
        "for key, value in expected.items():\n"
        "    if data.get(key) != value:\n"
        "        bad.append(key)\n"
        f"if data.get('bot_id') != {deployment!r}:\n"
        "    bad.append('bot_id')\n"
        "lifecycle=data.get('lifecycle_base_url','')\n"
        f"if lifecycle != 'http://dify-bootstrap.dify.svc.cluster.local:5688/v1/lifecycle/{deployment}':\n"
        "    bad.append('lifecycle_base_url')\n"
        "if data.get('lifecycle_token') and data.get('workspace_id') and lifecycle:\n"
        "    try:\n"
        "        req=urllib.request.Request(\n"
        "            lifecycle.rstrip('/') + '/health',\n"
        f"            headers={{'Authorization': 'Bearer '+str(data.get('lifecycle_token')), 'X-CarHer-Dify-Workspace': str(data.get('workspace_id')), 'X-CarHer-Bot-Id': {deployment!r}, 'Accept': 'application/json'}},\n"
        "            method='GET',\n"
        "        )\n"
        "        with urllib.request.urlopen(req, timeout=20) as resp:\n"
        "            status=resp.status\n"
        "    except urllib.error.HTTPError as exc:\n"
        "        status=exc.code\n"
        "    except Exception:\n"
        "        status=0\n"
        "    if status != 200:\n"
        "        bad.append('lifecycle_health')\n"
        "else:\n"
        "    bad.append('lifecycle_health')\n"
        "print('ok' if not bad else ','.join(bad))\n"
        "raise SystemExit(1 if bad else 0)\n"
        "PY"
    )
    bad = False
    for pod_name in pod_names:
        result = run(
            [KUBECTL_BIN, "-n", namespace, "exec", pod_name, "-c", "carher", "--", "sh", "-lc", script],
            check=False,
        )
        output = (result.stdout or result.stderr or "").strip()
        print(f"pod_config\t{deployment}\t{pod_name}\t{output or 'no_output'}")
        if result.returncode != 0:
            bad = True
    return bad


def patch_hermes_config(namespace: str, deployment: str) -> bool:
    if USE_API:
        print(f"error\thermes_config_patch_requires_kubectl_exec\t{deployment}")
        return False
    pod_names = wait_running_pods_for_deployment(namespace, deployment)
    if not pod_names:
        print(f"warn\thermes_config_patch_no_running_pod\t{deployment}")
        return False
    script = (
        "python3 - <<'PY'\n"
        "import datetime as dt, os, pathlib, shutil, sys\n"
        "preferred = pathlib.Path('/opt/data/.hermes/config-litellm.yaml')\n"
        "env_src = pathlib.Path(os.environ.get('CARHER_HERMES_CONFIG_TEMPLATE') or str(preferred))\n"
        "candidates = [preferred]\n"
        "if env_src != preferred:\n"
        "    candidates.append(env_src)\n"
        "dst = pathlib.Path('/opt/data/.hermes/config.yaml')\n"
        "def valid_litellm(text):\n"
        "    stale = ('cc.auto-link.com.cn/pro/v1', 'litellm.carher.net/v1', 'provider: \"chatgpt-pro\"', 'provider: chatgpt-pro')\n"
        "    return (\n"
        "        ('provider: litellm' in text or 'provider: \"litellm\"' in text)\n"
        "        and ('\\n  litellm:' in text or '\\nlitellm:' in text)\n"
        "        and ('transport: chat_completions' in text or 'transport: \"chat_completions\"' in text)\n"
        "        and not any(item in text for item in stale)\n"
        "    )\n"
        "src = None\n"
        "template_text = ''\n"
        "for candidate in candidates:\n"
        "    if not candidate.exists():\n"
        "        continue\n"
        "    text = candidate.read_text(encoding='utf-8', errors='replace')\n"
        "    if valid_litellm(text):\n"
        "        src = candidate\n"
        "        template_text = text\n"
        "        break\n"
        "if src is None:\n"
        "    print('missing_valid_litellm_template:' + ','.join(str(item) for item in candidates))\n"
        "    raise SystemExit(1)\n"
        "dst.parent.mkdir(parents=True, exist_ok=True)\n"
        "if dst.exists():\n"
        "    stamp = dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')\n"
        "    shutil.copyfile(dst, dst.with_name(dst.name + f'.bak-h75-runtime-repair-{stamp}'))\n"
        "dst.write_text(template_text, encoding='utf-8')\n"
        "text = dst.read_text(encoding='utf-8', errors='replace')\n"
        "bad = []\n"
        "if 'provider: litellm' not in text and 'provider: \"litellm\"' not in text:\n"
        "    bad.append('missing_model_provider')\n"
        "if '\\n  litellm:' not in text and '\\nlitellm:' not in text:\n"
        "    bad.append('missing_litellm_provider')\n"
        "if 'transport: chat_completions' not in text and 'transport: \"chat_completions\"' not in text:\n"
        "    bad.append('missing_chat_completions')\n"
        "stale = ('cc.auto-link.com.cn/pro/v1', 'litellm.carher.net/v1', 'provider: \"chatgpt-pro\"', 'provider: chatgpt-pro')\n"
        "if any(item in text for item in stale):\n"
        "    bad.append('stale_public_or_chatgpt_pro')\n"
        "print('ok' if not bad else ','.join(bad))\n"
        "raise SystemExit(1 if bad else 0)\n"
        "PY"
    )
    for pod_name in pod_names:
        result = run(
            [KUBECTL_BIN, "-n", namespace, "exec", pod_name, "-c", "carher", "--", "sh", "-lc", script],
            check=False,
        )
        if result.returncode != 0:
            detail = compact_output((result.stderr or "") + "\n" + (result.stdout or ""))
            print(f"error\thermes_config_patch_failed\t{deployment}\t{pod_name}\t{detail}")
            return False
        print(f"hermes_config_patch\t{deployment}\t{pod_name}\tok")
    return True


def check_hermes_config(namespace: str, deployment: str) -> bool | None:
    if USE_API:
        print(f"warn\thermes_config_check_requires_kubectl_exec\t{deployment}")
        return None
    pod_names = running_pods_for_deployment(namespace, deployment)
    if not pod_names:
        print(f"warn\thermes_config_check_no_running_pod\t{deployment}")
        return None
    script = (
        "f=/opt/data/.hermes/config.yaml\n"
        "[ -f \"$f\" ] || { echo missing_config; exit 1; }\n"
        "bad=0\n"
        "grep -q 'provider: litellm' \"$f\" || { echo missing_model_provider; bad=1; }\n"
        "grep -q '^  litellm:' \"$f\" || { echo missing_litellm_provider; bad=1; }\n"
        "grep -q 'transport: chat_completions' \"$f\" || { echo missing_chat_completions; bad=1; }\n"
        "if grep -Eq 'cc.auto-link.com.cn/pro/v1|litellm.carher.net/v1|provider: \"chatgpt-pro\"|provider: chatgpt-pro' \"$f\"; then echo stale_public_or_chatgpt_pro; bad=1; fi\n"
        "[ \"$bad\" = 0 ] && echo ok\n"
        "exit \"$bad\"\n"
    )
    bad = False
    for pod_name in pod_names:
        result = run(
            [KUBECTL_BIN, "-n", namespace, "exec", pod_name, "-c", "carher", "--", "sh", "-lc", script],
            check=False,
        )
        output = (result.stdout or result.stderr or "").strip().replace("\n", ",")
        print(f"hermes_config\t{deployment}\t{pod_name}\t{output or 'no_output'}")
        if result.returncode != 0:
            bad = True
    return bad


def patch_title_failure_visibility(namespace: str, deployment: str) -> bool:
    if USE_API:
        print(f"error\ttitle_patch_requires_kubectl_exec\t{deployment}")
        return False
    patch_poststart(namespace, deployment)
    pod_names = wait_running_pods_for_deployment(namespace, deployment)
    if not pod_names:
        print(f"warn\ttitle_patch_no_running_pod\t{deployment}")
        return False
    script = (
        "set -eu\n"
        "mkdir -p /opt/data/.hermes\n"
        f"cat > {POSTSTART_SCRIPT} <<'SH'\n"
        "#!/bin/sh\n"
        "set -eu\n"
        "python3 - <<'PY'\n"
        "import pathlib, py_compile, sys\n"
        f"marker = {TITLE_PATCH_MARKER!r}\n"
        "target = pathlib.Path('/opt/hermes/source/agent/title_generator.py')\n"
        "if not target.exists():\n"
        "    raise SystemExit(0)\n"
        "src = target.read_text(encoding='utf-8')\n"
        "if marker not in src:\n"
        "    old = '''        if failure_callback is not None:\n"
        "            try:\n"
        "                failure_callback(\"title generation\", e)\n"
        "            except Exception:\n"
        "                logger.debug(\"Title generation failure_callback raised\", exc_info=True)\n"
        "'''\n"
        "    new = '''        # CARHER_TITLE_FAILURE_SILENT_PATCH\n"
        "        # Title generation is auxiliary for Feishu chat delivery. Keep\n"
        "        # failures in logs; do not emit a user-visible warning card after\n"
        "        # an otherwise successful reply.\n"
        "'''\n"
        "    if old not in src:\n"
        "        raise SystemExit('title_generator callback anchor not found')\n"
        "    target.write_text(src.replace(old, new, 1), encoding='utf-8')\n"
        "py_compile.compile(str(target), doraise=True)\n"
        "PY\n"
        "SH\n"
        f"chmod +x {POSTSTART_SCRIPT}\n"
        f"{POSTSTART_SCRIPT}\n"
    )
    for pod_name in pod_names:
        result = run(
            [KUBECTL_BIN, "-n", namespace, "exec", pod_name, "-c", "carher", "--", "sh", "-lc", script],
            check=False,
        )
        if result.returncode != 0:
            detail = compact_output((result.stderr or "") + "\n" + (result.stdout or ""))
            print(f"error\ttitle_patch_failed\t{deployment}\t{pod_name}\t{detail}")
            return False
        print(f"title_patch_apply\t{deployment}\t{pod_name}\tok")
    return True


def check_title_failure_visibility(namespace: str, deployment: str) -> bool | None:
    if USE_API:
        print(f"warn\ttitle_patch_check_requires_kubectl_exec\t{deployment}")
        return None
    pod_names = running_pods_for_deployment(namespace, deployment)
    if not pod_names:
        print(f"warn\ttitle_patch_check_no_running_pod\t{deployment}")
        return None
    script = (
        "bad=0\n"
        f"[ -x {POSTSTART_SCRIPT} ] || {{ echo missing_poststart_script; bad=1; }}\n"
        f"grep -q {TITLE_PATCH_MARKER!r} /opt/hermes/source/agent/title_generator.py || "
        "{ echo missing_title_patch; bad=1; }\n"
        "python3 -m py_compile /opt/hermes/source/agent/title_generator.py >/dev/null 2>&1 || "
        "{ echo title_generator_py_compile_failed; bad=1; }\n"
        "[ \"$bad\" = 0 ] && echo ok\n"
        "exit \"$bad\"\n"
    )
    bad = False
    for pod_name in pod_names:
        result = run(
            [KUBECTL_BIN, "-n", namespace, "exec", pod_name, "-c", "carher", "--", "sh", "-lc", script],
            check=False,
        )
        output = (result.stdout or result.stderr or "").strip().replace("\n", ",")
        print(f"title_patch\t{deployment}\t{pod_name}\t{output or 'no_output'}")
        if result.returncode != 0:
            bad = True
    return bad


def chunks(items: list[DeploymentState], size: int) -> list[list[DeploymentState]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def repair(args: argparse.Namespace, targets: list[DeploymentState]) -> int:
    failures: list[str] = []
    defer_pod_patch = args.rollout and not args.no_restart
    for index, wave in enumerate(chunks(targets, args.wave_size), start=1):
        names = [state.name for state in wave]
        print(f"wave_start\t{index}\tcount={len(names)}\t{','.join(names)}")
        for name in names:
            patch_strategy(args.namespace, name, "0", "1")
            if args.apply:
                patch_template_env(args.namespace, name)
                if not defer_pod_patch:
                    if not args.skip_pod_config and not patch_generated_config(args.namespace, name):
                        failures.append(name)
                        continue
                    if not args.skip_hermes_config and not patch_hermes_config(args.namespace, name):
                        failures.append(name)
                        continue
                    if not args.skip_title_patch and not patch_title_failure_visibility(args.namespace, name):
                        failures.append(name)
                        continue
            unpause(args.namespace, name)
            if args.rollout and not args.no_restart:
                rollout_restart(args.namespace, name)
        if args.rollout:
            for name in names:
                if not rollout_status(args.namespace, name, args.timeout):
                    failures.append(name)
                    print(f"rollout_result\t{name}\tfailed")
                elif not args.skip_pod_config and not patch_generated_config(args.namespace, name):
                    failures.append(name)
                elif not args.skip_hermes_config and not patch_hermes_config(args.namespace, name):
                    failures.append(name)
                elif not args.skip_title_patch and not patch_title_failure_visibility(args.namespace, name):
                    failures.append(name)
                else:
                    print(f"rollout_result\t{name}\tok")
        if args.restore_strategy:
            for name in names:
                patch_strategy(args.namespace, name, "1", "0")
        print(f"wave_done\t{index}\tfailures={','.join(failures) or '-'}")
        if failures and args.stop_on_failure:
            break
        if index < len(chunks(targets, args.wave_size)) and args.sleep_between_waves > 0:
            time.sleep(args.sleep_between_waves)
    if failures:
        print(f"failed\t{','.join(failures)}")
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit and repair H75 Deployment runtime URLs and rollout convergence.",
    )
    parser.add_argument("--namespace", default=NAMESPACE)
    parser.add_argument("--selector", default=SELECTOR)
    parser.add_argument("--targets", nargs="*", default=[], help="Deployment names or numeric Her ids.")
    parser.add_argument("--exclude", nargs="*", default=[])
    parser.add_argument("--include-sensitive", action="store_true", help="Allow sensitive targets such as carher-2.")
    parser.add_argument("--only-needs-fix", action="store_true", default=True)
    parser.add_argument("--all-h75", action="store_true", help="Include all H75 deployments, even if audit is clean.")
    parser.add_argument("--apply", action="store_true", help="Patch Deployment template env to internal service URLs.")
    parser.add_argument("--rollout", action="store_true", help="Resume/restart deployments in waves and wait for rollout.")
    parser.add_argument("--no-restart", action="store_true", help="Do not force rollout restart after patching env.")
    parser.add_argument("--restore-strategy", action="store_true", help="Restore maxSurge=1/maxUnavailable=0 after each wave.")
    parser.add_argument("--wave-size", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--sleep-between-waves", type=int, default=0)
    parser.add_argument("--skip-pod-config", action="store_true")
    parser.add_argument("--skip-hermes-config", action="store_true")
    parser.add_argument("--skip-title-patch", action="store_true")
    parser.add_argument(
        "--check-pod-config",
        action="store_true",
        help="Exec into running pods and audit workflow/dify-config.json.",
    )
    parser.add_argument(
        "--check-hermes-config",
        action="store_true",
        help="Exec into running pods and audit /opt/data/.hermes/config.yaml.",
    )
    parser.add_argument(
        "--check-title-patch",
        action="store_true",
        help="Exec into running pods and audit title-failure visibility patch.",
    )
    parser.add_argument(
        "--fail-on-pod-config-drift",
        action="store_true",
        help="Return non-zero if checked pod config still points at public or wrong URLs.",
    )
    parser.add_argument(
        "--fail-on-hermes-config-drift",
        action="store_true",
        help="Return non-zero if checked Hermes config is not ACK LiteLLM chat-completions.",
    )
    parser.add_argument(
        "--fail-on-title-patch-drift",
        action="store_true",
        help="Return non-zero if title generation failures can still emit user-visible cards.",
    )
    parser.add_argument("--stop-on-failure", action="store_true", default=True)
    parser.add_argument("--json", action="store_true", help="Emit full audit JSON.")
    parser.add_argument(
        "--api-mode",
        action="store_true",
        help="Use in-cluster Kubernetes API instead of kubectl.",
    )
    return parser.parse_args()


def main() -> int:
    global USE_API
    args = parse_args()
    USE_API = args.api_mode or not has_kubectl()
    print(f"mode\t{'api' if USE_API else 'kubectl'}")
    if args.all_h75:
        args.only_needs_fix = False
    deployments = list_deployments(args.namespace, args.selector)
    surge_names = surge_deployments(args.namespace, args.selector)
    states = [deployment_state(dep, surge_names) for dep in deployments]
    if args.check_pod_config:
        requested_for_check = {f"carher-{item}" if item.isdigit() else item for item in args.targets}
        for state in states:
            if not state.h75:
                continue
            if requested_for_check and state.name not in requested_for_check:
                continue
            checked = check_generated_config(args.namespace, state.name)
            if checked is not None:
                state.pod_config_checked = True
                state.pod_config_bad = checked
    if args.check_hermes_config:
        requested_for_check = {f"carher-{item}" if item.isdigit() else item for item in args.targets}
        for state in states:
            if not state.h75:
                continue
            if requested_for_check and state.name not in requested_for_check:
                continue
            checked = check_hermes_config(args.namespace, state.name)
            if checked is not None:
                state.hermes_config_checked = True
                state.hermes_config_bad = checked
    if args.check_title_patch:
        requested_for_check = {f"carher-{item}" if item.isdigit() else item for item in args.targets}
        for state in states:
            if not state.h75:
                continue
            if requested_for_check and state.name not in requested_for_check:
                continue
            checked = check_title_failure_visibility(args.namespace, state.name)
            if checked is not None:
                state.title_patch_checked = True
                state.title_patch_bad = checked
    h75_states = [state for state in states if state.h75]
    print_summary(h75_states)
    if args.json:
        print(json.dumps([state.__dict__ for state in h75_states], ensure_ascii=False, indent=2))
    else:
        print_table(h75_states)
    targets = target_filter(states, args)
    print(f"selected\t{len(targets)}\t{','.join(state.name for state in targets) or '-'}")
    if not args.apply and not args.rollout:
        return 0
    if not targets:
        return 0
    if args.wave_size < 1:
        raise RuntimeError("--wave-size must be >= 1")
    result = repair(args, targets)
    if result != 0:
        return result
    if args.fail_on_pod_config_drift:
        failed = []
        for state in targets:
            checked = check_generated_config(args.namespace, state.name)
            if checked is True or checked is None:
                failed.append(state.name)
        if failed:
            print(f"failed_pod_config\t{','.join(failed)}")
            return 1
    if args.fail_on_hermes_config_drift:
        failed = []
        for state in targets:
            checked = check_hermes_config(args.namespace, state.name)
            if checked is True or checked is None:
                failed.append(state.name)
        if failed:
            print(f"failed_hermes_config\t{','.join(failed)}")
            return 1
    if args.fail_on_title_patch_drift:
        failed = []
        for state in targets:
            checked = check_title_failure_visibility(args.namespace, state.name)
            if checked is True or checked is None:
                failed.append(state.name)
        if failed:
            print(f"failed_title_patch\t{','.join(failed)}")
            return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"fatal\t{exc}", file=sys.stderr)
        raise SystemExit(1)
