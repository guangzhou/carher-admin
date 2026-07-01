#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/litellm-key-image-route.sh <canary|deploy|status|cleanup-canary> [options]

Options:
  --asset NAME          JumpServer asset, default: AIYJY-litellm
  --namespace NAME      K8s namespace, default: litellm-product
  --key-alias ALIAS     LiteLLM virtual key alias to gate on
  --image-model MODEL   Vision-capable model group, default: chatgpt-gpt-5.5
  --text-models CSV     Client/local model names that may be image-rerouted

Examples:
  scripts/litellm-key-image-route.sh canary --key-alias cursor-baiyu-thga
  scripts/litellm-key-image-route.sh deploy --key-alias cursor-baiyu-thga
  scripts/litellm-key-image-route.sh status --key-alias cursor-baiyu-thga
  scripts/litellm-key-image-route.sh cleanup-canary

This script writes no virtual keys. Use a real key only in separate curl tests.
USAGE
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

MODE="$1"
shift

ASSET="AIYJY-litellm"
NAMESPACE="litellm-product"
KEY_ALIAS=""
IMAGE_MODEL="chatgpt-gpt-5.5"
TEXT_MODELS="gpt-5.5,chatgpt-gpt-5.5,local-deepseek-v4-flash-responses,local-deepseek-v4-flash"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --asset) ASSET="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --key-alias) KEY_ALIAS="$2"; shift 2 ;;
    --image-model) IMAGE_MODEL="$2"; shift 2 ;;
    --text-models) TEXT_MODELS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

case "$MODE" in
  canary|deploy|status|cleanup-canary) ;;
  *) echo "Unknown mode: $MODE" >&2; usage; exit 2 ;;
esac

if [[ "$MODE" != "cleanup-canary" && -z "$KEY_ALIAS" ]]; then
  echo "--key-alias is required for $MODE" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JMS="${SCRIPT_DIR}/jms"
if [[ ! -x "$JMS" ]]; then
  JMS="jms"
fi

"$JMS" ssh "$ASSET" \
  "MODE=$(printf '%q' "$MODE") NS=$(printf '%q' "$NAMESPACE") KEY_ALIAS=$(printf '%q' "$KEY_ALIAS") IMAGE_MODEL=$(printf '%q' "$IMAGE_MODEL") TEXT_MODELS=$(printf '%q' "$TEXT_MODELS") bash -s" <<'REMOTE'
set -euo pipefail

DEPLOY=litellm-proxy
CANARY=litellm-proxy-key-image-route-canary
CONFIG=litellm-config
CALLBACKS=litellm-callbacks
HOOK_NAME=baiyu_image_route.py
CALLBACK_NAME=baiyu_image_route.baiyu_image_route
MANIFEST_DIR=/root/litellm-product-manifests

if [[ "$MODE" == "cleanup-canary" ]]; then
  kubectl delete deploy "$CANARY" svc "$CANARY" cm "${CONFIG}-key-image-route-canary" cm "${CALLBACKS}-key-image-route-canary" -n "$NS" --ignore-not-found
  exit 0
fi

python3 - <<'PY'
import os
import pathlib
import subprocess
import sys
import yaml

mode = os.environ["MODE"]
ns = os.environ["NS"]
key_alias = os.environ["KEY_ALIAS"]
image_model = os.environ["IMAGE_MODEL"]
text_models = [m.strip() for m in os.environ["TEXT_MODELS"].split(",") if m.strip()]

deploy_name = "litellm-proxy"
canary_name = "litellm-proxy-key-image-route-canary"
config_name = "litellm-config"
callbacks_name = "litellm-callbacks"
hook_name = "baiyu_image_route.py"
callback_name = "baiyu_image_route.baiyu_image_route"
manifest_dir = pathlib.Path("/root/litellm-product-manifests")


def kubectl_get(kind, name):
    raw = subprocess.check_output(["kubectl", "get", kind, name, "-n", ns, "-o", "yaml"])
    return yaml.safe_load(raw)


def clean_meta(obj, name=None):
    meta = obj.setdefault("metadata", {})
    if name is not None:
        meta["name"] = name
    meta["namespace"] = ns
    for key in ("uid", "resourceVersion", "creationTimestamp", "managedFields", "annotations", "generation"):
        meta.pop(key, None)


def hook_source():
    return f'''from __future__ import annotations

from typing import Any
from litellm.integrations.custom_logger import CustomLogger
import litellm

_TARGET_ALIAS = {key_alias!r}
_IMAGE_MODEL = {image_model!r}
_TEXT_MODELS = {set(text_models)!r}


def _get_key_alias(user_api_key_dict: Any) -> str:
    if isinstance(user_api_key_dict, dict):
        return str(user_api_key_dict.get("key_alias") or user_api_key_dict.get("alias") or "")
    return str(getattr(user_api_key_dict, "key_alias", "") or getattr(user_api_key_dict, "alias", "") or "")


def _has_image(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("type") in {{"input_image", "image_url"}}:
            return True
        if "image_url" in value or "input_image" in value:
            return True
        return any(_has_image(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_image(v) for v in value)
    if isinstance(value, str):
        lower = value.lower()
        if "data:image" in lower:
            return True
        if lower.startswith(("http://", "https://")) and any(ext in lower for ext in (".png", ".jpg", ".jpeg", ".webp")):
            return True
    return False


class BaiyuImageRoute(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            if not isinstance(data, dict):
                return data
            if _get_key_alias(user_api_key_dict) != _TARGET_ALIAS:
                return data
            if str(data.get("model") or "") not in _TEXT_MODELS:
                return data
            if not _has_image(data.get("input", data.get("messages", data))):
                return data
            old = data.get("model")
            data["model"] = _IMAGE_MODEL
            try:
                litellm.print_verbose(f"[baiyu_image_route] routed image request {{old!r}} -> {{_IMAGE_MODEL!r}} for {{_TARGET_ALIAS}}")
            except Exception:
                pass
        except Exception as exc:
            try:
                litellm.print_verbose(f"[baiyu_image_route] ERROR: {{exc!r}}")
            except Exception:
                pass
        return data


baiyu_image_route = BaiyuImageRoute()
'''


def patch_config(cm, name=None):
    cm = dict(cm)
    clean_meta(cm, name)
    conf = yaml.safe_load(cm["data"]["config.yaml"])
    callbacks = conf.setdefault("litellm_settings", {}).setdefault("callbacks", [])
    if callback_name not in callbacks:
        callbacks.append(callback_name)
    cm["data"]["config.yaml"] = yaml.safe_dump(conf, allow_unicode=True, sort_keys=False)
    return cm


def patch_callbacks(cm, name=None):
    cm = dict(cm)
    clean_meta(cm, name)
    cm.setdefault("data", {})[hook_name] = hook_source()
    return cm


def add_mount(deploy, name=None, replicas=None, config_ref=None, callbacks_ref=None):
    deploy = dict(deploy)
    clean_meta(deploy, name)
    if replicas is not None:
        deploy["spec"]["replicas"] = replicas
    if name is not None:
        labels = {"app": name}
        deploy["metadata"]["labels"] = labels
        deploy["spec"]["selector"]["matchLabels"] = labels
        deploy["spec"]["template"].setdefault("metadata", {})["labels"] = labels
    deploy["spec"]["template"].setdefault("metadata", {}).pop("creationTimestamp", None)
    spec = deploy["spec"]["template"]["spec"]
    for vol in spec.get("volumes", []):
        if config_ref and vol.get("name") == "config":
            vol["configMap"]["name"] = config_ref
        if callbacks_ref and vol.get("name") == "callbacks":
            vol["configMap"]["name"] = callbacks_ref
    container = spec["containers"][0]
    mounts = container.setdefault("volumeMounts", [])
    if not any(m.get("subPath") == hook_name for m in mounts):
        mounts.append({"mountPath": f"/app/{hook_name}", "name": "callbacks", "readOnly": True, "subPath": hook_name})
    return deploy


def write_yaml(path, obj):
    pathlib.Path(path).write_text(yaml.safe_dump(obj, allow_unicode=True, sort_keys=False))


def sync_manifests(config_cm, callbacks_cm):
    config_path = manifest_dir / "30-cm-litellm-config.yaml"
    callbacks_path = manifest_dir / "30-cm-litellm-callbacks.yaml"
    proxy_path = manifest_dir / "40-proxy.yaml"

    manifest_config = yaml.safe_load(config_path.read_text())
    manifest_config["data"]["config.yaml"] = config_cm["data"]["config.yaml"]
    write_yaml(config_path, manifest_config)

    manifest_callbacks = yaml.safe_load(callbacks_path.read_text())
    manifest_callbacks.setdefault("data", {})[hook_name] = callbacks_cm["data"][hook_name]
    write_yaml(callbacks_path, manifest_callbacks)

    docs = list(yaml.safe_load_all(proxy_path.read_text()))
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == deploy_name:
            add_mount(doc)
    proxy_path.write_text("---\n".join(yaml.safe_dump(d, allow_unicode=True, sort_keys=False) for d in docs if d is not None))


work = pathlib.Path("/tmp/key-image-route")
work.mkdir(parents=True, exist_ok=True)

if mode == "status":
    config = kubectl_get("cm", config_name)
    callbacks = kubectl_get("cm", callbacks_name)
    deploy = kubectl_get("deploy", deploy_name)
    mounted = any(m.get("subPath") == hook_name for m in deploy["spec"]["template"]["spec"]["containers"][0].get("volumeMounts", []))
    print("callback_registered=", callback_name in yaml.safe_load(config["data"]["config.yaml"]).get("litellm_settings", {}).get("callbacks", []))
    print("hook_code_present=", hook_name in callbacks.get("data", {}))
    print("deployment_mount_present=", mounted)
    print("target_key_alias=", key_alias)
    print("image_model=", image_model)
    sys.exit(0)

config_live = kubectl_get("cm", config_name)
callbacks_live = kubectl_get("cm", callbacks_name)
deploy_live = kubectl_get("deploy", deploy_name)

if mode == "canary":
    config_canary = patch_config(config_live, f"{config_name}-key-image-route-canary")
    callbacks_canary = patch_callbacks(callbacks_live, f"{callbacks_name}-key-image-route-canary")
    deploy_canary = add_mount(
        deploy_live,
        name=canary_name,
        replicas=1,
        config_ref=f"{config_name}-key-image-route-canary",
        callbacks_ref=f"{callbacks_name}-key-image-route-canary",
    )
    svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": canary_name, "namespace": ns, "labels": {"app": canary_name}},
        "spec": {"type": "ClusterIP", "ports": [{"name": "http", "port": 4000, "targetPort": 4000}], "selector": {"app": canary_name}},
    }
    out = work / "canary.yaml"
    out.write_text("---\n".join(yaml.safe_dump(o, allow_unicode=True, sort_keys=False) for o in [config_canary, callbacks_canary, svc, deploy_canary]))
    print(out)
    sys.exit(0)

if mode == "deploy":
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = manifest_dir / "backups" / f"key-image-route-{ts}"
    bdir.mkdir(parents=True, exist_ok=True)
    write_yaml(bdir / "litellm-config.live.yaml", config_live)
    write_yaml(bdir / "litellm-callbacks.live.yaml", callbacks_live)
    write_yaml(bdir / "litellm-proxy.live.yaml", deploy_live)
    for name in ("30-cm-litellm-config.yaml", "30-cm-litellm-callbacks.yaml", "40-proxy.yaml"):
        src = manifest_dir / name
        if src.exists():
            (bdir / f"{name}.before").write_text(src.read_text())

    config_patched = patch_config(config_live)
    callbacks_patched = patch_callbacks(callbacks_live)
    deploy_patched = add_mount(deploy_live)
    write_yaml(work / "litellm-config.patched.yaml", config_patched)
    write_yaml(work / "litellm-callbacks.patched.yaml", callbacks_patched)
    write_yaml(work / "litellm-proxy.patched.yaml", deploy_patched)
    sync_manifests(config_patched, callbacks_patched)
    print("backup_dir=", bdir)
    sys.exit(0)

raise SystemExit(f"unsupported mode: {mode}")
PY

if [[ "$MODE" == "canary" ]]; then
  kubectl apply -f /tmp/key-image-route/canary.yaml
  kubectl -n "$NS" rollout status deploy "$CANARY" --timeout=240s
  kubectl get pod -n "$NS" -l app="$CANARY" -o wide
elif [[ "$MODE" == "deploy" ]]; then
  kubectl apply -f /tmp/key-image-route/litellm-callbacks.patched.yaml
  kubectl apply -f /tmp/key-image-route/litellm-config.patched.yaml
  kubectl apply -f /tmp/key-image-route/litellm-proxy.patched.yaml
  kubectl -n "$NS" rollout status deploy "$DEPLOY" --timeout=600s
  kubectl get deploy "$DEPLOY" -n "$NS" -o jsonpath='{.status.readyReplicas}/{.spec.replicas} ready generation={.metadata.generation} observed={.status.observedGeneration}{"\n"}'
fi
REMOTE
