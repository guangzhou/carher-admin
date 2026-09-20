#!/usr/bin/env python3
"""Deploy the isolated Cursor zerokey canary and grant it to one virtual key.

The canary is a new Deployment that reads the existing acct-122 session PVC but
has its own host port, ConfigMap overlay, LiteLLM model groups, and key allowlist.
It does not modify the existing zerokey protocol deployment or any global model
alias. Run with --apply only after reviewing the dry-run output.

Example on k8s-work-226:
  python3 zerokey-cursor-protocol-rollout.py --apply

The script uses the LiteLLM pod's own master key for admin API calls. It never
prints that credential or the target virtual-key token.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Any


NS = "carher"
CONTEXT = "203299974580141085-c215e116fb0a7414287f4be1c31bb4ebc"
LITELLM_DEPLOY = "litellm-proxy"
TARGET_ALIAS = "cursor-liuguoxian03"
SOURCE_DEPLOY = "zerokey-serve-122"
DEPLOY = "zerokey-cursor-122"
PATCH_CM = "zerokey-cursor-protocol-patch"
PORT = 8322
ACCOUNT = "122"
ZK_USER = "acct122"
MODELS = {
    "cursor-gpt-5.6-sol": "gpt-5.6-sol",
    "cursor-gpt-5.6-terra": "gpt-5.6-terra",
    "cursor-gpt-5.6-luna": "gpt-5.6-luna",
    "cursor-gpt-5.5": "gpt-5-5",
    "cursor-gpt-5.4": "gpt-5-4-thinking",
    "cursor-gpt-5.3-codex": "gpt-5-3",
}

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCH_ROOT = pathlib.Path(
    os.environ.get(
        "ZK_CURSOR_PATCH_ROOT",
        str(ROOT / "scripts" / "chatgpt-onboard" / "zerokey-codex" / "zerokey-patch"),
    )
)
PATCH_FILES = {
    "zerokey-serve-codex.js": PATCH_ROOT / "zerokey-serve-codex.js",
    "chatgpt.js": PATCH_ROOT / "routes" / "chatgpt.js",
    "raw.js": PATCH_ROOT / "routes" / "raw.js",
    "web-tools.js": PATCH_ROOT / "routes" / "web-tools.js",
    "cursor.js": PATCH_ROOT / "routes" / "cursor.js",
}


class RolloutError(RuntimeError):
    pass


def run(args: list[str], *, input_text: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(args, input=input_text, capture_output=True, text=True)
    if check and proc.returncode:
        raise RolloutError(
            f"command failed ({proc.returncode}): {' '.join(args[:5])}\n"
            f"{(proc.stderr or proc.stdout).strip()[:600]}"
        )
    return proc.stdout


def kc(*args: str, input_text: str | None = None, check: bool = True) -> str:
    return run(["kubectl", "--context", CONTEXT, "-n", NS, *args], input_text=input_text, check=check)


def load_patch_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for name, path in PATCH_FILES.items():
        if not path.is_file():
            raise RolloutError(f"required patch file is missing: {path}")
        files[name] = path.read_text(encoding="utf-8")
    return files


def source_spec() -> dict[str, Any]:
    return json.loads(kc("get", "deploy", SOURCE_DEPLOY, "-o", "json"))


def source_runtime(source: dict[str, Any]) -> tuple[str, str, str]:
    spec = source["spec"]["template"]["spec"]
    container = spec["containers"][0]
    image = str(container["image"])
    state = next(
        (v for v in spec.get("volumes") or [] if v.get("name") == "state"),
        None,
    )
    claim = ((state or {}).get("persistentVolumeClaim") or {}).get("claimName")
    node = str(spec.get("nodeName") or "")
    if not image or not claim or not node:
        raise RolloutError("source zerokey deployment is missing image, state PVC, or nodeName")
    return image, str(claim), node


def deployment_manifest(image: str, claim: str, node: str) -> dict[str, Any]:
    mounts = [
        {"name": "state", "mountPath": "/state", "readOnly": True},
        {"name": "patch", "mountPath": "/app/zerokey-serve-codex.js", "subPath": "zerokey-serve-codex.js", "readOnly": True},
        {"name": "patch", "mountPath": "/app/routes/chatgpt.js", "subPath": "chatgpt.js", "readOnly": True},
        {"name": "patch", "mountPath": "/app/routes/raw.js", "subPath": "raw.js", "readOnly": True},
        {"name": "patch", "mountPath": "/app/routes/web-tools.js", "subPath": "web-tools.js", "readOnly": True},
        {"name": "patch", "mountPath": "/app/routes/cursor.js", "subPath": "cursor.js", "readOnly": True},
    ]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": DEPLOY, "namespace": NS, "labels": {"app": DEPLOY, "pool": "zerokey-cursor", "account": ACCOUNT}},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": DEPLOY}},
            "template": {
                "metadata": {"labels": {"app": DEPLOY, "pool": "zerokey-cursor", "account": ACCOUNT}},
                "spec": {
                    "hostNetwork": True,
                    "dnsPolicy": "ClusterFirstWithHostNet",
                    "nodeName": node,
                    "imagePullSecrets": [{"name": "acr-vpc-secret"}],
                    "containers": [{
                        "name": "serve",
                        "image": image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sh", "-c", "mkdir -p /app/temp && cp /state/users.json /app/temp/users.json && exec node zerokey-serve-codex.js"],
                        "env": [
                            {"name": "PORT", "value": str(PORT)},
                            {"name": "ZK_USER", "value": ZK_USER},
                            {"name": "ZK_DEFAULT_MODEL", "value": "gpt-5-5"},
                        ],
                        "ports": [{"containerPort": PORT, "hostPort": PORT}],
                        "readinessProbe": {"httpGet": {"path": "/health", "port": PORT}, "initialDelaySeconds": 5, "periodSeconds": 10},
                        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
                        "volumeMounts": mounts,
                    }],
                    "volumes": [
                        {"name": "state", "persistentVolumeClaim": {"claimName": claim}},
                        {"name": "patch", "configMap": {"name": PATCH_CM}},
                    ],
                },
            },
        },
    }


def apply_manifest(manifest: dict[str, Any], dry_run: bool) -> None:
    body = json.dumps(manifest)
    if dry_run:
        print(f"[dry-run] would apply Deployment/{DEPLOY}")
        return
    kc("apply", "-f", "-", input_text=body)
    kc("rollout", "status", f"deploy/{DEPLOY}", "--timeout=180s")


def apply_patch_cm(files: dict[str, str], dry_run: bool) -> None:
    before = json.loads(kc("get", "cm", PATCH_CM, "-o", "json", check=False) or "{}")
    old = dict(before.get("data") or {})
    changed = [name for name, body in files.items() if old.get(name) != body]
    print("[patch] " + ", ".join(
        f"{name}={'changed' if name in changed else 'unchanged'}:{hashlib.sha256(body.encode()).hexdigest()[:12]}"
        for name, body in files.items()
    ))
    if dry_run:
        return
    manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": PATCH_CM, "namespace": NS}, "data": files}
    kc("apply", "-f", "-", input_text=json.dumps(manifest))
    after = json.loads(kc("get", "cm", PATCH_CM, "-o", "json")).get("data") or {}
    if after != files:
        raise RolloutError("patch ConfigMap verification failed")


def proxy_api(path: str, body: Any = None, method: str = "GET") -> Any:
    code = """
import json, os, sys, urllib.error, urllib.request
payload = json.load(sys.stdin)
data = json.dumps(payload['body']).encode() if payload['body'] is not None else None
request = urllib.request.Request(
    'http://127.0.0.1:4000' + payload['path'], data=data,
    headers={'Authorization': 'Bearer ' + os.environ['LITELLM_MASTER_KEY'], 'Content-Type': 'application/json'},
    method=payload['method'])
try:
    with urllib.request.urlopen(request, timeout=120) as response:
        print(json.dumps({'status': response.status, 'body': json.load(response)}))
except urllib.error.HTTPError as exc:
    print(json.dumps({'status': exc.code, 'error': exc.read().decode(errors='replace')[:500]}))
"""
    raw = kc("exec", "-i", f"deploy/{LITELLM_DEPLOY}", "-c", "litellm", "--", "python3", "-c", code,
             input_text=json.dumps({"path": path, "body": body, "method": method}))
    result = json.loads(raw)
    if result.get("status") not in range(200, 300):
        raise RolloutError(f"LiteLLM {method} {path} failed HTTP {result.get('status')}: {result.get('error')}")
    return result["body"]


def model_entries() -> list[dict[str, Any]]:
    source = source_spec()
    _, _, node = source_runtime(source)
    host_ip = node.removeprefix("ap-southeast-1.")
    base = f"http://{host_ip}:{PORT}/v1"
    return [
        {
            "model_name": public,
            "litellm_params": {
                "model": f"openai/{public}",
                "api_base": base,
                "api_key": "cursor",
                "rpm": 8,
                "input_cost_per_token": 5e-6,
                "output_cost_per_token": 3e-5,
            },
            "model_info": {"id": f"zerokey-cursor-122-{public}", "mode": "chat"},
        }
        for public in MODELS
    ]


def register_models(dry_run: bool) -> None:
    current = proxy_api("/v1/model/info").get("data") or []
    by_id = {str((row.get("model_info") or {}).get("id")): row for row in current}
    for entry in model_entries():
        model_id = entry["model_info"]["id"]
        existing = by_id.get(model_id)
        if existing:
            lp = existing.get("litellm_params") or {}
            expected = entry["litellm_params"]
            if existing.get("model_name") != entry["model_name"] or any(lp.get(k) != v for k, v in expected.items()):
                raise RolloutError(f"existing model id {model_id} has a different configuration; refusing to overwrite")
            print(f"[models] {entry['model_name']} already registered")
            continue
        print(f"[models] register {entry['model_name']} -> {entry['litellm_params']['api_base']}")
        if not dry_run:
            proxy_api("/pro/model/new", entry, "POST")


def find_target_key() -> dict[str, Any]:
    first = proxy_api("/key/list?return_full_object=true")
    rows = list(first.get("keys") or [])
    for page in range(2, int(first.get("total_pages") or 1) + 1):
        rows.extend(proxy_api(f"/key/list?return_full_object=true&page={page}").get("keys") or [])
    matches = [row for row in rows if row.get("key_alias") == TARGET_ALIAS]
    if len(matches) != 1:
        raise RolloutError(f"expected exactly one target key {TARGET_ALIAS!r}, got {len(matches)}")
    return matches[0]


def grant_target_key(dry_run: bool) -> None:
    target = find_target_key()
    before = list(target.get("models") or [])
    after = before + [model for model in MODELS if model not in before]
    redacted = {
        "key_alias": target.get("key_alias"),
        "blocked": target.get("blocked"),
        "models": before,
        "aliases": target.get("aliases") or {},
        "token_sha256": hashlib.sha256(str(target.get("token", "")).encode()).hexdigest()[:16],
    }
    print("[key] before=" + json.dumps(redacted, ensure_ascii=False, sort_keys=True))
    print(f"[key] models {len(before)} -> {len(after)}; blocked remains {target.get('blocked')}")
    if dry_run:
        return
    token = target.get("token")
    if not token:
        raise RolloutError("target key response did not include an update token")
    proxy_api("/key/update", {"key": token, "models": after}, "POST")
    verified = find_target_key()
    missing = [model for model in MODELS if model not in (verified.get("models") or [])]
    if missing or verified.get("blocked") != target.get("blocked"):
        raise RolloutError(f"key update verification failed: missing={missing}, blocked={verified.get('blocked')}")
    print("[key] update verified: six Cursor models granted; blocked state preserved")


def verify_canary(files: dict[str, str], dry_run: bool) -> None:
    if dry_run:
        return
    pod = kc("get", "pods", "-l", f"app={DEPLOY}", "-o", "jsonpath={.items[0].metadata.name}").strip()
    if not pod:
        raise RolloutError("canary deployment has no pod after successful rollout")
    checks = [
        ("/app/zerokey-serve-codex.js", files["zerokey-serve-codex.js"]),
        ("/app/routes/chatgpt.js", files["chatgpt.js"]),
        ("/app/routes/raw.js", files["raw.js"]),
        ("/app/routes/web-tools.js", files["web-tools.js"]),
        ("/app/routes/cursor.js", files["cursor.js"]),
    ]
    for path, body in checks:
        got = kc("exec", pod, "--", "sha256sum", path).split()[0]
        want = hashlib.sha256(body.encode()).hexdigest()
        if got != want:
            raise RolloutError(f"canary file hash mismatch for {path}: {got[:12]} != {want[:12]}")
    kc("exec", pod, "--", "sh", "-c", "node --check /app/zerokey-serve-codex.js && node --check /app/routes/chatgpt.js && node --check /app/routes/raw.js && node --check /app/routes/cursor.js")
    print(f"[verify] {pod}: four mounted files match source and parse successfully")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="perform the rollout; otherwise print the planned changes")
    args = parser.parse_args()
    files = load_patch_files()
    source = source_spec()
    image, claim, node = source_runtime(source)
    print(f"[source] {SOURCE_DEPLOY}: image={image}, pvc={claim}, node={node}, canary-port={PORT}")
    if args.apply:
        duplicate = kc("get", "pods", "-l", f"app={SOURCE_DEPLOY}", "-o", "jsonpath={.items[0].metadata.uid}").strip()
        if not duplicate:
            raise RolloutError(f"source deployment {SOURCE_DEPLOY} has no running pod; refusing to share its session PVC")
    apply_patch_cm(files, not args.apply)
    apply_manifest(deployment_manifest(image, claim, node), not args.apply)
    verify_canary(files, not args.apply)
    register_models(not args.apply)
    grant_target_key(not args.apply)
    print("[done]" if args.apply else "[dry-run] no changes applied")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RolloutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
