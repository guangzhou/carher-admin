#!/usr/bin/env python3
import argparse
import base64
import csv
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

NAMESPACE = "carher"
TARGET_TAG = "h75-runtime-fa244014-hermestest75-20260602"
TARGET_IMAGE = (
    "cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:"
    + TARGET_TAG
)
TARGET_PROFILE = "h75-openclaw"
REDIS_URL = "redis://carher-redis.carher.svc:6379"
INTERNAL_DIFY = "http://dify-nginx.dify.svc.cluster.local"
INTERNAL_BOOTSTRAP = (
    "http://dify-bootstrap.dify.svc.cluster.local:5688/v1/bootstrap/carher-bot"
)
INTERNAL_LITELLM = "http://litellm-proxy.carher.svc.cluster.local:4000/v1"


def log(msg):
    print(msg, flush=True)


def service_account_headers(method="GET", patch_kind="merge"):
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    with open(token_path, "r", encoding="utf-8") as f:
        token = f.read().strip()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": (
            "application/strategic-merge-patch+json"
            if method == "PATCH" and patch_kind == "strategic"
            else "application/merge-patch+json"
            if method == "PATCH"
            else "application/json"
        ),
        "Accept": "application/json",
    }


def api_base():
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    return f"https://{host}:{port}"


def api_request(method, path, body=None, patch_kind="merge"):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        api_base() + path,
        data=data,
        headers=service_account_headers(method, patch_kind=patch_kind),
        method=method,
    )
    ctx = None
    try:
        import ssl

        cafile = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        ctx = ssl.create_default_context(cafile=cafile)
    except Exception:
        ctx = None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> {e.code}: {text}") from e


class Redis:
    def __init__(self, host="carher-redis.carher.svc", port=6379):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(10)
        self.file = self.sock.makefile("rb")

    def close(self):
        self.file.close()
        self.sock.close()

    def call(self, *parts):
        payload = f"*{len(parts)}\r\n".encode()
        for part in parts:
            if isinstance(part, str):
                b = part.encode("utf-8")
            else:
                b = part
            payload += f"${len(b)}\r\n".encode() + b + b"\r\n"
        self.sock.sendall(payload)
        return self._read()

    def _read(self):
        line = self.file.readline()
        if not line:
            raise RuntimeError("redis closed")
        prefix = line[:1]
        rest = line[1:].rstrip(b"\r\n")
        if prefix == b"+":
            return rest.decode()
        if prefix == b"-":
            raise RuntimeError(rest.decode())
        if prefix == b":":
            return int(rest)
        if prefix == b"$":
            size = int(rest)
            if size == -1:
                return None
            data = self.file.read(size)
            self.file.read(2)
            return data.decode("utf-8", errors="replace")
        if prefix == b"*":
            count = int(rest)
            if count == -1:
                return None
            return [self._read() for _ in range(count)]
        raise RuntimeError(f"unknown redis reply {line!r}")


def list_hers():
    return api_request(
        "GET", f"/apis/carher.io/v1alpha1/namespaces/{NAMESPACE}/herinstances"
    )["items"]


def get_her(name):
    return api_request(
        "GET", f"/apis/carher.io/v1alpha1/namespaces/{NAMESPACE}/herinstances/{name}"
    )


def patch_her(name, patch):
    return api_request(
        "PATCH",
        f"/apis/carher.io/v1alpha1/namespaces/{NAMESPACE}/herinstances/{name}",
        patch,
    )


def get_deploy(name):
    return api_request("GET", f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}")


def patch_deploy(name, patch):
    return api_request(
        "PATCH",
        f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}",
        patch,
        patch_kind="strategic",
    )


def user_id(her):
    spec = her.get("spec", {})
    value = spec.get("userId") or spec.get("userID") or her["metadata"]["name"].split("-")[-1]
    return str(value)


def deploy_name(her):
    return f"carher-{user_id(her)}"


def deployment_hardened(her):
    try:
        dep = get_deploy(deploy_name(her))
    except Exception:
        return False
    template = dep.get("spec", {}).get("template", {})
    spec = template.get("spec", {})
    containers = spec.get("containers", [])
    carher = next((c for c in containers if c.get("name") == "carher"), None)
    reloader = next((c for c in containers if c.get("name") == "config-reloader"), None)
    if not carher or carher.get("image") != TARGET_IMAGE:
        return False
    if reloader and reloader.get("image") != TARGET_IMAGE:
        return False
    env = {e.get("name"): e.get("value") for e in carher.get("env", []) if "value" in e}
    required_env = {
        "REDIS_URL": REDIS_URL,
        "OPENAI_BASE_URL": INTERNAL_LITELLM,
        "CARHER_DIFY_BASE_URL": INTERNAL_DIFY,
        "CARHER_DIFY_BOOTSTRAP_URL": INTERNAL_BOOTSTRAP,
        "CARHER_DIFY_CODEX_BASE_URL": INTERNAL_LITELLM,
        "CARHER_RUNTIME_PLUGINS_REFRESH": "0",
        "FEISHU_ALLOW_ALL_USERS": "true",
        "FEISHU_GROUP_POLICY": "open",
        "PYTHONPATH": "/data/.openclaw/local/hermes-python-packages",
    }
    for key, value in required_env.items():
        if env.get(key) != value:
            return False
    env_names = set(env.keys()) | {
        e.get("name")
        for e in carher.get("env", [])
        if e.get("valueFrom") is not None and e.get("name")
    }
    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "CARHER_DIFY_BOOTSTRAP_TOKEN"):
        if key not in env_names:
            return False
    for key in ("CARHER_GATEWAY_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        if key not in env_names:
            return False
    if env.get("CARHER_REQUIRED_SECRET_ENVS") != "CARHER_PROD_KEY":
        return False
    if env.get("LITELLM_API_KEY") and env.get("CARHER_PROD_KEY") != env.get("LITELLM_API_KEY"):
        return False
    mount_names = {m.get("name") for m in carher.get("volumeMounts", [])}
    mounts_by_name = {m.get("name"): m for m in carher.get("volumeMounts", [])}
    for mount_name in (
        "h75-agent-skills",
        "h75-runtime-plugins",
        "h75-openclaw-extensions",
        "h75-openclaw-skills",
        "h75-hermes-skills",
        "h75-hermes-opt-skills",
    ):
        if mount_name not in mount_names:
            return False
        if mounts_by_name.get(mount_name, {}).get("readOnly") is True:
            return False
    base_config = next((v for v in spec.get("volumes", []) if v.get("name") == "base-config"), {})
    if base_config.get("configMap", {}).get("name") != "carher-base-config-h75":
        return False
    init_names = {c.get("name") for c in spec.get("initContainers", [])}
    if "copy-hermes-feishu-deps" not in init_names:
        return False
    lifecycle = carher.get("lifecycle", {})
    post_start = json.dumps(lifecycle.get("postStart", {}), ensure_ascii=False)
    if INTERNAL_LITELLM not in post_start:
        return False
    strategy = dep.get("spec", {}).get("strategy", {}).get("rollingUpdate", {})
    if str(strategy.get("maxSurge")) not in ("0", "0%"):
        return False
    if str(strategy.get("maxUnavailable")) not in ("1", "1%"):
        return False
    return True


def is_crd_target_state(her):
    meta = her.get("metadata", {})
    anns = meta.get("annotations") or {}
    spec = her.get("spec", {})
    uid = user_id(her)
    return (
        spec.get("image") == TARGET_TAG
        and spec.get("deployGroup") == f"beta-h75-{uid}"
        and anns.get("carher.io/runtime-profile") == TARGET_PROFILE
    )


def is_target_state(her):
    return is_crd_target_state(her) and deployment_hardened(her)


def parse_mode(value):
    if not value:
        return ""
    try:
        parsed = json.loads(value)
        return parsed.get("mode") or ""
    except Exception:
        return value


def ascii_group_json(mode, context):
    return json.dumps(
        {"mode": mode, "context": context, "set_by": "codex-h75-batch"},
        ensure_ascii=True,
        separators=(",", ":"),
    )


def redis_audit_for(redis, app_id):
    if not app_id:
        return {
            "tracked": [],
            "group_at": [],
            "owner_at": [],
            "modes": {},
            "candidate_home": "",
            "strategy": "no_app_id",
        }
    tracked = redis.call("SMEMBERS", f"group:tracked:{app_id}") or []
    modes = {}
    group_at = []
    owner_at = []
    for chat in sorted(tracked):
        raw = redis.call("GET", f"group:mode:{chat}:{app_id}")
        mode = parse_mode(raw)
        modes[chat] = mode
        if mode == "group-at":
            group_at.append(chat)
        if mode == "owner-at":
            owner_at.append(chat)
    candidate = group_at[0] if len(group_at) == 1 else ""
    if candidate:
        strategy = "redis_single_group_at"
    elif len(group_at) > 1:
        strategy = "redis_multi_group_at"
    elif tracked:
        strategy = "redis_tracked_no_group_at"
    else:
        strategy = "no_redis_tracked"
    return {
        "tracked": sorted(tracked),
        "group_at": sorted(group_at),
        "owner_at": sorted(owner_at),
        "modes": modes,
        "candidate_home": candidate,
        "strategy": strategy,
    }


def env_entry(name, value):
    return {"name": name, "value": value}


def env_secret_entry(name, secret_name, key):
    return {
        "name": name,
        "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}},
    }


def upsert_env(envs, name, value):
    out = []
    found = False
    for item in envs or []:
        if item.get("name") == name:
            out.append(env_entry(name, value))
            found = True
        else:
            out.append(item)
    if not found:
        out.append(env_entry(name, value))
    return out


def upsert_env_value_from(envs, name, value_from_entry):
    out = []
    found = False
    for item in envs or []:
        if item.get("name") == name:
            out.append(value_from_entry)
            found = True
        else:
            out.append(item)
    if not found:
        out.append(value_from_entry)
    return out


def harden_deployment(name, home, app_id="", secret_ref=""):
    dep = get_deploy(name)
    spec = dep["spec"]["template"]["spec"]
    containers = spec.get("containers", [])
    new_containers = []
    for c in containers:
        c = dict(c)
        if c.get("name") in ("carher", "config-reloader"):
            c["image"] = TARGET_IMAGE
        if c.get("name") == "carher":
            mounts = c.get("volumeMounts") or []
            required_mounts = [
                {"name": "h75-fastbin", "mountPath": "/carher-fastbin", "readOnly": True},
                {"name": "h75-agent-skills", "mountPath": "/data/.agents/skills", "readOnly": False},
                {"name": "h75-openclaw-local", "mountPath": "/data/.openclaw/local", "readOnly": False},
                {"name": "h75-runtime-plugins", "mountPath": "/data/.openclaw/runtime-plugins", "readOnly": False},
                {"name": "h75-openclaw-extensions", "mountPath": "/data/.openclaw/extensions", "readOnly": False},
                {"name": "h75-openclaw-skills", "mountPath": "/data/.openclaw/skills", "readOnly": False},
                {"name": "h75-hermes-skills", "mountPath": "/opt/data/.hermes/skills", "readOnly": False},
                {"name": "h75-hermes-opt-skills", "mountPath": "/opt/data/skills", "readOnly": False},
            ]
            by_mount = {m.get("name"): dict(m) for m in mounts}
            for mount in required_mounts:
                current = by_mount.get(mount["name"], {})
                current.update(mount)
                by_mount[mount["name"]] = current
            mounts = list(by_mount.values())
            c["volumeMounts"] = mounts
            resources = c.get("resources") or {}
            requests = resources.get("requests") or {}
            requests["cpu"] = "50m"
            requests.setdefault("memory", "1Gi")
            resources["requests"] = requests
            c["resources"] = resources
            envs = c.get("env") or []
            for k, v in [
                ("REDIS_URL", REDIS_URL),
                (
                    "PATH",
                    "/carher-fastbin:/opt/hermes/venv/bin:/opt/hermes/.venv/bin:/opt/node22/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                ),
                ("OPENAI_BASE_URL", INTERNAL_LITELLM),
                ("CARHER_DIFY_BASE_URL", INTERNAL_DIFY),
                ("CARHER_DIFY_BOOTSTRAP_URL", INTERNAL_BOOTSTRAP),
                ("CARHER_DIFY_CODEX_BASE_URL", INTERNAL_LITELLM),
                ("CARHER_RUNTIME_PLUGINS_REFRESH", "0"),
                ("FEISHU_ALLOW_ALL_USERS", "true"),
                ("FEISHU_GROUP_POLICY", "open"),
                ("PYTHONPATH", "/data/.openclaw/local/hermes-python-packages"),
                ("CARHER_REQUIRED_SECRET_ENVS", "CARHER_PROD_KEY"),
            ]:
                envs = upsert_env(envs, k, v)
            env_values = {e.get("name"): e.get("value") for e in envs if "value" in e}
            if env_values.get("LITELLM_API_KEY"):
                envs = upsert_env(envs, "CARHER_PROD_KEY", env_values["LITELLM_API_KEY"])
            if app_id:
                envs = upsert_env(envs, "FEISHU_APP_ID", app_id)
            if secret_ref:
                envs = upsert_env_value_from(
                    envs,
                    "FEISHU_APP_SECRET",
                    env_secret_entry("FEISHU_APP_SECRET", secret_ref, "app_secret"),
                )
            envs = upsert_env_value_from(
                envs,
                "CARHER_DIFY_BOOTSTRAP_TOKEN",
                env_secret_entry(
                    "CARHER_DIFY_BOOTSTRAP_TOKEN",
                    "carher-dify-bootstrap-token",
                    "token",
                ),
            )
            envs = upsert_env_value_from(
                envs,
                "CARHER_GATEWAY_TOKEN",
                env_secret_entry(
                    "CARHER_GATEWAY_TOKEN",
                    "carher-h75-runtime-secrets",
                    "CARHER_GATEWAY_TOKEN",
                ),
            )
            envs = upsert_env_value_from(
                envs,
                "ANTHROPIC_AUTH_TOKEN",
                env_secret_entry(
                    "ANTHROPIC_AUTH_TOKEN",
                    "carher-h75-acp-secrets",
                    "ANTHROPIC_AUTH_TOKEN",
                ),
            )
            if home:
                envs = upsert_env(envs, "FEISHU_HOME_CHANNEL", home)
            c["env"] = envs
            lifecycle = c.get("lifecycle") or {}
            lifecycle["postStart"] = {
                "exec": {
                    "command": [
                        "sh",
                        "-lc",
                        (
                            "set -eu\n"
                            f"INTERNAL='{INTERNAL_LITELLM}'\n"
                            "f=/opt/data/.hermes/config.yaml\n"
                            "sleep 45\n"
                            "[ -f \"$f\" ] || exit 0\n"
                            "cp \"$f\" \"$f.bak-poststart-delayed-internal-litellm-$(date -u +%Y%m%dT%H%M%SZ)\" || true\n"
                            "sed -i \"s#https://cc.auto-link.com.cn/pro/v1#$INTERNAL#g; s#https://litellm.carher.net/v1#$INTERNAL#g\" \"$f\"\n"
                            "grep -Eq \"cc.auto-link.com.cn/pro/v1|litellm.carher.net/v1\" \"$f\" && exit 1 || true\n"
                            "echo poststart-delayed-internal-litellm=ok"
                        ),
                    ]
                }
            }
            lifecycle.setdefault(
                "preStop", {"exec": {"command": ["sh", "-c", "sleep 15"]}}
            )
            c["lifecycle"] = lifecycle
        if c.get("name") == "config-reloader" and secret_ref:
            c["env"] = upsert_env_value_from(
                c.get("env") or [],
                "FEISHU_APP_SECRET",
                env_secret_entry("FEISHU_APP_SECRET", secret_ref, "app_secret"),
            )
        new_containers.append(c)
    init_containers = spec.get("initContainers") or []
    init_containers = [c for c in init_containers if c.get("name") != "copy-hermes-feishu-deps"]
    if not any(c.get("name") == "prepare-h75-fastbin" for c in init_containers):
        init_containers.append(
            {
                "name": "prepare-h75-fastbin",
                "image": TARGET_IMAGE,
                "command": [
                    "sh",
                    "-lc",
                    (
                        "set -eu\n"
                        "mkdir -p /carher-fastbin\n"
                        "cat > /carher-fastbin/rm <<'EOF'\n"
                        "#!/bin/sh\n"
                        "real=/bin/rm\n"
                        "if [ \"$1\" = \"-rf\" ] && [ \"$#\" -eq 2 ] && [ \"$2\" = \"/data/.openclaw/runtime-plugins\" ]; then\n"
                        "  find \"$2\" -mindepth 1 -maxdepth 1 -exec \"$real\" -rf {} + 2>/dev/null || true\n"
                        "  exit 0\n"
                        "fi\n"
                        "exec \"$real\" \"$@\"\n"
                        "EOF\n"
                        "chmod 0755 /carher-fastbin/rm\n"
                    ),
                ],
                "volumeMounts": [
                    {"name": "h75-fastbin", "mountPath": "/carher-fastbin"}
                ],
            }
        )
    if not any(c.get("name") == "patch-openclaw-config-legacy-llm" for c in init_containers):
        init_containers.append(
            {
                "name": "patch-openclaw-config-legacy-llm",
                "image": TARGET_IMAGE,
                "command": [
                    "python3",
                    "-c",
                    (
                        "import json, pathlib\n"
                        "p=pathlib.Path('/user-data/.openclaw/openclaw.json')\n"
                        "if not p.exists():\n"
                        "    raise SystemExit(0)\n"
                        "data=json.loads(p.read_text())\n"
                        "defaults=data.get('agents',{}).get('defaults')\n"
                        "if isinstance(defaults,dict) and 'llm' in defaults:\n"
                        "    defaults.pop('llm',None)\n"
                        "    p.write_text(json.dumps(data,ensure_ascii=False,indent=2))\n"
                        "    print('removed agents.defaults.llm')\n"
                    ),
                ],
                "volumeMounts": [{"name": "user-data", "mountPath": "/user-data"}],
            }
        )
    init_containers.append(
        {
            "name": "copy-hermes-feishu-deps",
            "image": TARGET_IMAGE,
            "command": [
                "sh",
                "-lc",
                (
                    "set -eu; "
                    "DST=/data/.openclaw/local/hermes-python-packages; "
                    "rm -rf \"$DST\"; mkdir -p \"$DST\"; "
                    "uv pip install --target \"$DST\" --link-mode=copy "
                    "\"lark-oapi==1.6.7\" \"aiohttp-socks==0.11.0\"; "
                    "PYTHONPATH=\"$DST\" /opt/hermes/.venv/bin/python3 -c "
                    "'import lark_oapi, aiohttp_socks; print(\"hermes-feishu-deps=ok\")'"
                ),
            ],
            "volumeMounts": [
                {"name": "h75-openclaw-local", "mountPath": "/data/.openclaw/local"}
            ],
        }
    )
    volumes = spec.get("volumes") or []
    base_config_volume = {
        "name": "base-config",
        "configMap": {"name": "carher-base-config-h75", "defaultMode": 420},
    }
    replaced_base_config = False
    for index, volume in enumerate(volumes):
        if volume.get("name") == "base-config":
            volumes[index] = base_config_volume
            replaced_base_config = True
            break
    if not replaced_base_config:
        volumes.append(base_config_volume)
    for volume_name in [
        "h75-fastbin",
        "h75-agent-skills",
        "h75-openclaw-local",
        "h75-runtime-plugins",
        "h75-openclaw-extensions",
        "h75-openclaw-skills",
        "h75-hermes-skills",
        "h75-hermes-opt-skills",
    ]:
        if not any(v.get("name") == volume_name for v in volumes):
            volumes.append({"name": volume_name, "emptyDir": {}})
    patch = {
        "spec": {
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
            },
            "template": {
                "metadata": {
                    "annotations": {
                        "carher.io/deploy-group": dep["spec"]["template"]
                        .get("metadata", {})
                        .get("annotations", {})
                        .get("carher.io/deploy-group", ""),
                        "codex.carher.io/h75-hardened-at": str(int(time.time())),
                    }
                },
                "spec": {
                    "containers": new_containers,
                    "initContainers": init_containers,
                    "volumes": volumes,
                },
            }
        }
    }
    if home:
        patch["spec"]["template"]["metadata"]["annotations"]["carher.io/feishu-home-channel"] = home
    return patch_deploy(name, patch)


def set_low_surge_strategy(name):
    return patch_deploy(
        name,
        {
            "spec": {
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
                }
            }
        },
    )


def rollout_ready(name, timeout=600):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            dep = get_deploy(name)
            spec = dep.get("spec", {})
            status = dep.get("status", {})
            desired = spec.get("replicas", 1) or 1
            replicas = status.get("replicas", 0) or 0
            updated = status.get("updatedReplicas", 0) or 0
            ready = status.get("readyReplicas", 0) or 0
            available = status.get("availableReplicas", 0) or 0
            unavailable = status.get("unavailableReplicas", 0) or 0
            observed = status.get("observedGeneration", 0) or 0
            generation = dep["metadata"].get("generation", 0)
            image_ok = True
            for c in spec.get("template", {}).get("spec", {}).get("containers", []):
                if c.get("name") in ("carher", "config-reloader") and c.get("image") != TARGET_IMAGE:
                    image_ok = False
            last = {
                "desired": desired,
                "replicas": replicas,
                "updated": updated,
                "ready": ready,
                "available": available,
                "unavailable": unavailable,
                "observed": observed,
                "generation": generation,
                "image_ok": image_ok,
            }
            if (
                observed >= generation
                and replicas == desired
                and updated == desired
                and ready == desired
                and available == desired
                and unavailable == 0
                and image_ok
            ):
                return True, last
        except Exception as e:
            last = {"error": str(e)}
        time.sleep(10)
    return False, last


def pod_for_user(uid):
    pods = api_request("GET", f"/api/v1/namespaces/{NAMESPACE}/pods")["items"]
    candidates = []
    for pod in pods:
        labels = pod.get("metadata", {}).get("labels", {})
        if labels.get("user-id") == uid:
            candidates.append(pod)
    def score(p):
        phase = p.get("status", {}).get("phase", "")
        statuses = p.get("status", {}).get("containerStatuses", [])
        ready = statuses and all(
            cs.get("ready") for cs in statuses if cs.get("name") in ("carher", "config-reloader")
        )
        images = [cs.get("image", "") for cs in statuses if cs.get("name") in ("carher", "config-reloader")]
        image_ok = images and all(img == TARGET_IMAGE for img in images)
        return (
            1 if phase == "Running" else 0,
            1 if ready else 0,
            1 if image_ok else 0,
            p.get("metadata", {}).get("creationTimestamp", ""),
        )

    candidates.sort(key=score, reverse=True)
    return candidates[0] if candidates else None


def exec_pod(pod, command, timeout=60):
    try:
        from kubernetes import client, config, stream

        config.load_incluster_config()
        api = client.CoreV1Api()
        return stream.stream(
            api.connect_get_namespaced_pod_exec,
            pod,
            NAMESPACE,
            command=command,
            container="carher",
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _request_timeout=timeout,
        )
    except Exception as e:
        return f"EXEC_ERROR: {e}"


def verify_runtime(uid):
    pod = pod_for_user(uid)
    if not pod:
        return {"pod": "", "status": "no_pod"}
    pod_name = pod["metadata"]["name"]
    phase = pod.get("status", {}).get("phase", "")
    statuses = [
        cs
        for cs in pod.get("status", {}).get("containerStatuses", [])
        if cs.get("name") in ("carher", "config-reloader")
    ]
    ready = all(
        cs.get("ready") and cs.get("image") == TARGET_IMAGE
        for cs in statuses
    )
    if not statuses:
        ready = False
    health = exec_pod(
        pod_name,
        [
            "sh",
            "-lc",
            (
                "printf 'active='; cat /data/.engine/active 2>/dev/null || true; echo; "
                "printf 'health='; curl -fsS --max-time 5 http://127.0.0.1:3000/healthz >/dev/null && echo ok || echo fail; "
                "printf 'deps='; PYTHONPATH=/data/.openclaw/local/hermes-python-packages /opt/hermes/.venv/bin/python3 -c 'import lark_oapi,aiohttp_socks; print(\"ok\")' 2>/dev/null || echo fail; "
                "printf 'dify='; /data/.openclaw/local/bin/her-workflow-dify-creator health 2>/dev/null | head -c 400 || true; echo; "
                "printf 'public_litellm='; grep -RE 'cc.auto-link.com.cn/pro/v1|litellm.carher.net/v1' /opt/data/.hermes/config.yaml /data/.openclaw/workflow/dify-config.json 2>/dev/null | wc -l"
            ),
        ],
        timeout=90,
    )
    return {"pod": pod_name, "phase": phase, "ready": ready, "probe": health}


def runtime_ok(runtime):
    if runtime.get("phase") != "Running" or not runtime.get("ready"):
        return False
    probe = runtime.get("probe") or ""
    return (
        "EXEC_ERROR" not in probe
        and "health=ok" in probe
        and "deps=ok" in probe
        and "public_litellm=0" in probe
    )


def build_manifest(limit=None, only=None, include_target_crd=False):
    redis = Redis()
    rows = []
    try:
        for her in sorted(list_hers(), key=lambda h: int(user_id(h)) if user_id(h).isdigit() else 0):
            uid = user_id(her)
            if only and uid not in only and her["metadata"]["name"] not in only:
                continue
            if is_crd_target_state(her) and not include_target_crd:
                continue
            if include_target_crd and is_target_state(her):
                continue
            spec = her.get("spec", {})
            anns = her.get("metadata", {}).get("annotations") or {}
            app_id = spec.get("appId") or spec.get("feishu", {}).get("appId") or ""
            audit = redis_audit_for(redis, app_id)
            current_home = anns.get("carher.io/feishu-home-channel", "")
            chosen_home = current_home or audit["candidate_home"]
            feishu_strategy = "home_annotation_present" if current_home else audit["strategy"]
            rows.append(
                {
                    "her": her["metadata"]["name"],
                    "uid": her["metadata"]["uid"],
                    "user_id": uid,
                    "deploy": deploy_name(her),
                    "name": spec.get("name") or spec.get("userName") or "",
                    "app_id": app_id,
                    "secret_ref": spec.get("appSecretRef") or f"carher-{uid}-secret",
                    "current_image": spec.get("image", ""),
                    "current_group": spec.get("deployGroup", ""),
                    "current_profile": anns.get("carher.io/runtime-profile", ""),
                    "current_home": current_home,
                    "target_image": TARGET_TAG,
                    "target_group": f"beta-h75-{uid}",
                    "target_profile": TARGET_PROFILE,
                    "target_home": chosen_home,
                    "redis_tracked_count": len(audit["tracked"]),
                    "redis_group_at_count": len(audit["group_at"]),
                    "redis_owner_at_count": len(audit["owner_at"]),
                    "redis_strategy": feishu_strategy,
                    "redis_sample": ",".join(audit["tracked"][:5]),
                }
            )
            if limit and len(rows) >= limit:
                break
    finally:
        redis.close()
    return rows


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def write_csv(path, rows):
    if not rows:
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def apply_rows(rows, wave_size, verify, sleep_between_waves):
    results = []
    redis = Redis()
    try:
        for wave_start in range(0, len(rows), wave_size):
            wave = rows[wave_start : wave_start + wave_size]
            log(f"WAVE start={wave_start + 1} size={len(wave)}")
            active_wave = []
            for row in wave:
                result = dict(row)
                try:
                    live = get_her(row["her"])
                    if live["metadata"]["uid"] != row["uid"]:
                        result.update({"apply": "fail", "error": "uid_mismatch"})
                        results.append(result)
                        continue
                    anns = {
                        "carher.io/runtime-profile": TARGET_PROFILE,
                        "carher.io/reconcile-poke": str(int(time.time())),
                        "carher.io/force-reconcile": str(int(time.time())),
                    }
                    if row["target_home"]:
                        anns["carher.io/feishu-home-channel"] = row["target_home"]
                        redis.call("SADD", f"group:tracked:{row['app_id']}", row["target_home"])
                        redis.call(
                            "SET",
                            f"group:mode:{row['target_home']}:{row['app_id']}",
                            ascii_group_json(
                                "group-at",
                                "h75 batch exact home or single redis group-at candidate",
                            ),
                        )
                    patch = {
                        "metadata": {"annotations": anns},
                        "spec": {
                            "image": TARGET_TAG,
                            "deployGroup": row["target_group"],
                        },
                    }
                    patch_her(row["her"], patch)
                    try:
                        set_low_surge_strategy(row["deploy"])
                    except Exception as strategy_error:
                        result["strategy_warning"] = str(strategy_error)
                    try:
                        harden_deployment(
                            row["deploy"],
                            row["target_home"],
                            row.get("app_id", ""),
                            row.get("secret_ref", ""),
                        )
                        result["harden_patch"] = "patched"
                    except Exception as harden_error:
                        result["harden_patch"] = "fail"
                        result["error"] = str(harden_error)
                    result["apply"] = "patched_her"
                    active_wave.append(row)
                except Exception as e:
                    result.update({"apply": "fail", "error": str(e)})
                results.append(result)
            for row in active_wave:
                result = next(r for r in results if r["her"] == row["her"])
                try:
                    ok, state = rollout_ready(row["deploy"], timeout=900)
                    result["rollout_after_hardening"] = "pass" if ok else "fail"
                    result["rollout_after_hardening_state"] = json.dumps(state, ensure_ascii=False)
                except Exception as e:
                    result["rollout_after_hardening"] = "fail"
                    result["error"] = str(e)
                if verify and result.get("rollout_after_hardening") == "pass":
                    try:
                        runtime = verify_runtime(row["user_id"])
                        result["runtime_verify"] = json.dumps(runtime, ensure_ascii=False)
                        result["runtime_status"] = "pass" if runtime_ok(runtime) else "fail"
                    except Exception as e:
                        result["runtime_status"] = "fail"
                        result["error"] = str(e)
                else:
                    result["runtime_verify"] = "skipped"
                    result["runtime_status"] = "skipped"
                write_json("/tmp/h75_batch_results.partial.json", results)
                write_csv("/tmp/h75_batch_results.partial.csv", results)
            log(f"WAVE done={wave_start + 1}-{wave_start + len(wave)}")
            if sleep_between_waves:
                time.sleep(sleep_between_waves)
    finally:
        redis.close()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--wave-size", type=int, default=10)
    parser.add_argument("--sleep-between-waves", type=int, default=0)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--include-target-crd", action="store_true")
    parser.add_argument("--manifest", default="/tmp/h75_batch_manifest.json")
    parser.add_argument("--results", default="/tmp/h75_batch_results.json")
    args = parser.parse_args()

    rows = build_manifest(
        limit=args.limit,
        only=set(args.only or []),
        include_target_crd=args.include_target_crd,
    )
    write_json(args.manifest, rows)
    write_csv(args.manifest.replace(".json", ".csv"), rows)
    log(f"manifest_count={len(rows)} manifest={args.manifest}")
    if args.manifest_only:
        return
    if not args.apply:
        return
    for path in ("/tmp/h75_batch_results.partial.json", "/tmp/h75_batch_results.partial.csv"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    results = apply_rows(
        rows,
        wave_size=args.wave_size,
        verify=args.verify,
        sleep_between_waves=args.sleep_between_waves,
    )
    write_json(args.results, results)
    write_csv(args.results.replace(".json", ".csv"), results)
    failed = [r for r in results if "fail" in json.dumps(r, ensure_ascii=False)]
    log(f"results={len(results)} failed_like={len(failed)} path={args.results}")
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
