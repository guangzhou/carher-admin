#!/usr/bin/env python3
"""Roll out an isolated Cursor zerokey canary in the 225 K3s cluster."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
from typing import Any

NS = "litellm-product"
CONTEXT = ""
LITELLM_DEPLOY = "litellm-proxy"
SOURCE_DEPLOY = "zero-101"
CANARY_DEPLOY = "zero-cursor-101"
CANARY_SERVICE = "zero-cursor-101"
PATCH_CM = "zk-cursor-protocol-patch"
CANARY_PORT = 8200
TARGET_ALIAS = "cursor-liuguoxian03"
MODELS = {
    "cursor-gpt-5.6-sol": "gpt-5.6-sol",
    "cursor-gpt-5.6-terra": "gpt-5.6-terra",
    "cursor-gpt-5.6-luna": "gpt-5.6-luna",
    "cursor-gpt-5.5": "gpt-5-5",
    "cursor-gpt-5.4": "gpt-5-4-thinking",
    "cursor-gpt-5.3-codex": "gpt-5-3",
}
ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCH_ROOT = pathlib.Path(__import__("os").environ.get("ZK_CURSOR_PATCH_ROOT", str(ROOT / "scripts/chatgpt-onboard/zerokey-codex/zerokey-patch")))
PATCH_FILES = {
    "zerokey-serve-codex.js": PATCH_ROOT / "zerokey-serve-codex.js",
    "chatgpt.js": PATCH_ROOT / "routes/chatgpt.js",
    "raw.js": PATCH_ROOT / "routes/raw.js",
    "web-tools.js": PATCH_ROOT / "routes/web-tools.js",
    "cursor.js": PATCH_ROOT / "routes/cursor.js",
}
# The current 225 launcher imports these runtime patches. Copy their live
# ConfigMap versions into the isolated canary rather than altering production.
LIVE_PATCH_FILES = ("images.js", "api.js", "constants.js", "responses.js")


def load_live_patch_files() -> dict[str, str]:
    raw = json.loads(kc("get", "cm", "zk-image-patch", "-o", "json"))
    data = raw.get("data") or {}
    missing = [name for name in LIVE_PATCH_FILES if name not in data]
    if missing:
        raise RolloutError(f"live patch ConfigMap is missing required files: {missing}")
    return {name: data[name] for name in LIVE_PATCH_FILES}


class RolloutError(RuntimeError):
    pass


def run(args: list[str], input_text: str | None = None, check: bool = True) -> str:
    p = subprocess.run(args, input=input_text, capture_output=True, text=True)
    if check and p.returncode:
        raise RolloutError(f"command failed: {' '.join(args[:5])}: {(p.stderr or p.stdout).strip()[:600]}")
    return p.stdout


def kc(*args: str, input_text: str | None = None, check: bool = True) -> str:
    cmd = ["kubectl"]
    if CONTEXT:
        cmd += ["--context", CONTEXT]
    return run(cmd + ["-n", NS, *args], input_text, check)


def load_files() -> dict[str, str]:
    out = load_live_patch_files()
    for name, path in PATCH_FILES.items():
        if not path.is_file():
            raise RolloutError(f"missing patch file: {path}")
        out[name] = path.read_text(encoding="utf-8")
    return out


def source_spec() -> dict[str, Any]:
    return json.loads(kc("get", "deploy", SOURCE_DEPLOY, "-o", "json"))


def source_runtime(source: dict[str, Any]) -> tuple[str, str, str, str]:
    pod = source["spec"]["template"]["spec"]
    container = pod["containers"][0]
    image = str(container["image"])
    node = str(pod.get("nodeName") or "")
    env = {str(x.get("name")): str(x.get("value")) for x in container.get("env") or []}
    user = env.get("ZK_USER", "acct101")
    session = next((v for v in pod.get("volumes") or [] if v.get("name") == "session-data"), None)
    session_path = ((session or {}).get("hostPath") or {}).get("path")
    if not image or not node or not session_path:
        raise RolloutError("source deployment lacks image, nodeName, or session hostPath")
    return image, node, str(session_path), user


def deployment(image: str, node: str, session_path: str, user: str) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": CANARY_DEPLOY, "namespace": NS, "labels": {"app": CANARY_DEPLOY, "pool": "zerokey-cursor", "account": "101"}},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": CANARY_DEPLOY}},
            "template": {
                "metadata": {"labels": {"app": CANARY_DEPLOY, "pool": "zerokey-cursor", "account": "101"}},
                "spec": {
                    "nodeName": node,
                    "dnsPolicy": "None",
                    "dnsConfig": {"nameservers": ["1.1.1.1", "8.8.8.8"]},
                    "containers": [{
                        "name": "zerokey",
                        "image": image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sh", "-c"],
                        "args": ["mkdir -p /app/temp /app/routes /app/core/chatgpt /app/config && cp /seed/users.json /app/temp/users.json && cp /patch/zerokey-serve-codex.js /app/zerokey-serve-codex.js && cp /patch/chatgpt.js /app/routes/chatgpt.js && cp /patch/raw.js /app/routes/raw.js && cp /patch/web-tools.js /app/routes/web-tools.js && cp /patch/cursor.js /app/routes/cursor.js && cp /patch/images.js /app/routes/images.js && cp /patch/responses.js /app/routes/responses.js && cp /patch/api.js /app/core/chatgpt/api.js && cp /patch/constants.js /app/config/constants.js && exec node /app/zerokey-serve-codex.js"],
                        "env": [{"name": "PORT", "value": str(CANARY_PORT)}, {"name": "ZK_USER", "value": user}, {"name": "ZK_DEFAULT_MODEL", "value": "gpt-5-5"}, {"name": "CODEX_TOKEN_DIR", "value": "/codex-tokens"}],
                        "ports": [{"containerPort": CANARY_PORT}],
                        "readinessProbe": {"httpGet": {"path": "/health", "port": CANARY_PORT}, "initialDelaySeconds": 5, "periodSeconds": 10},
                        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
                        "volumeMounts": [{"name": "seed", "mountPath": "/seed", "readOnly": True}, {"name": "temp", "mountPath": "/app/temp"}, {"name": "patch", "mountPath": "/patch", "readOnly": True}, {"name": "codex-tokens", "mountPath": "/codex-tokens", "readOnly": True}],
                    }],
                    "volumes": [{"name": "seed", "hostPath": {"path": session_path, "type": "Directory"}}, {"name": "temp", "emptyDir": {}}, {"name": "patch", "configMap": {"name": PATCH_CM}}, {"name": "codex-tokens", "hostPath": {"path": "/Data/codex-tokens", "type": "Directory"}}],
                },
            },
        },
    }


def service() -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": CANARY_SERVICE, "namespace": NS, "labels": {"pool": "zerokey-cursor"}}, "spec": {"selector": {"app": CANARY_DEPLOY}, "ports": [{"name": "http", "port": CANARY_PORT, "targetPort": CANARY_PORT}]}}


def apply_patch(files: dict[str, str], dry: bool) -> None:
    old = json.loads(kc("get", "cm", PATCH_CM, "-o", "json", check=False) or "{}").get("data") or {}
    print("[patch] " + ", ".join(f"{k}={'changed' if old.get(k) != v else 'unchanged'}:{hashlib.sha256(v.encode()).hexdigest()[:12]}" for k, v in files.items()))
    if dry:
        return
    body = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": PATCH_CM, "namespace": NS}, "data": files}
    kc("apply", "-f", "-", input_text=json.dumps(body))


def apply_canary(manifest: dict[str, Any], dry: bool) -> None:
    if dry:
        print(f"[dry-run] would apply Deployment/{CANARY_DEPLOY} and Service/{CANARY_SERVICE}")
        return
    kc("apply", "-f", "-", input_text=json.dumps(manifest))
    kc("apply", "-f", "-", input_text=json.dumps(service()))
    kc("rollout", "status", f"deploy/{CANARY_DEPLOY}", "--timeout=180s")


def proxy_api(path: str, body: Any = None, method: str = "GET") -> Any:
    code = """
import json, os, urllib.request, urllib.error
p=json.load(__import__('sys').stdin)
data=json.dumps(p['body']).encode() if p['body'] is not None else None
r=urllib.request.Request('http://127.0.0.1:4000'+p['path'],data=data,headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],'Content-Type':'application/json'},method=p['method'])
try:
  with urllib.request.urlopen(r,timeout=120) as x: print(json.dumps({'status':x.status,'body':json.load(x)}))
except urllib.error.HTTPError as e: print(json.dumps({'status':e.code,'error':e.read().decode(errors='replace')[:500]}))
"""
    raw = kc("exec", "-i", f"deploy/{LITELLM_DEPLOY}", "-c", "litellm", "--", "python3", "-c", code, input_text=json.dumps({"path": path, "body": body, "method": method}))
    result = json.loads(raw)
    if result.get("status") not in range(200, 300):
        raise RolloutError(f"LiteLLM {method} {path} failed: HTTP {result.get('status')}: {result.get('error')}")
    return result["body"]


def entries() -> list[dict[str, Any]]:
    base = f"http://{CANARY_SERVICE}.{NS}.svc.cluster.local:{CANARY_PORT}/v1"
    return [{"model_name": name, "litellm_params": {"model": f"openai/{name}", "api_base": base, "api_key": "cursor", "rpm": 8, "input_cost_per_token": 5e-6, "output_cost_per_token": 3e-5}, "model_info": {"id": f"zerokey-cursor-101-{name}", "mode": "chat"}} for name in MODELS]


def register_models(dry: bool) -> None:
    current = proxy_api("/v1/model/info").get("data") or []
    by_id = {str((x.get("model_info") or {}).get("id")): x for x in current}
    for entry in entries():
        mid = entry["model_info"]["id"]
        old = by_id.get(mid)
        if old:
            if old.get("model_name") != entry["model_name"] or any((old.get("litellm_params") or {}).get(k) != v for k, v in entry["litellm_params"].items()):
                raise RolloutError(f"existing model {mid} differs; refusing overwrite")
            print(f"[models] {entry['model_name']} already registered")
        else:
            print(f"[models] register {entry['model_name']}")
            if not dry:
                proxy_api("/pro/model/new", entry, "POST")


def find_key() -> dict[str, Any]:
    # The key appears on the first admin page. Avoid a 140-page scan here: it
    # times out before a write while the API already returns the update token.
    rows = proxy_api("/key/list?return_full_object=true").get("keys") or []
    matches = [x for x in rows if x.get("key_alias") == TARGET_ALIAS]
    if len(matches) != 1:
        raise RolloutError(f"expected exactly one {TARGET_ALIAS} on the current admin key page, got {len(matches)}")
    return matches[0]


def grant_key(dry: bool) -> None:
    key = find_key()
    before = list(key.get("models") or [])
    after = before + [x for x in MODELS if x not in before]
    print(f"[key] {TARGET_ALIAS}: models {len(before)} -> {len(after)}; blocked state preserved ({key.get('blocked')})")
    if dry:
        return
    token = key.get("token")
    if not token:
        raise RolloutError("target key has no update token")
    proxy_api("/key/update", {"key": token, "models": after}, "POST")
    verified = find_key()
    if any(x not in (verified.get("models") or []) for x in MODELS) or verified.get("blocked") != key.get("blocked"):
        raise RolloutError("key verification failed")
    print("[key] verified")


def verify_canary(files: dict[str, str], dry: bool) -> None:
    if dry:
        return
    pod = kc("get", "pods", "-l", f"app={CANARY_DEPLOY}", "-o", "jsonpath={.items[0].metadata.name}").strip()
    if not pod:
        raise RolloutError("canary pod missing")
    paths = {
        "zerokey-serve-codex.js": "/app/zerokey-serve-codex.js",
        "api.js": "/app/core/chatgpt/api.js",
        "constants.js": "/app/config/constants.js",
    }
    for name, body in files.items():
        path = paths.get(name, f"/app/routes/{name}")
        got = kc("exec", pod, "--", "sha256sum", path).split()[0]
        want = hashlib.sha256(body.encode()).hexdigest()
        if got != want:
            raise RolloutError(f"hash mismatch {name}")
    kc("exec", pod, "--", "node", "--check", "/app/routes/cursor.js")
    print(f"[verify] {pod}: mounted files and cursor.js syntax verified")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    dry = not args.apply
    if dry:
        # Dry-run still needs the live ConfigMap for its file inventory, but
        # loading happens only after kubectl access is available.
        pass
    files = load_files()
    source = source_spec()
    image, node, session_path, user = source_runtime(source)
    running = kc("get", "pods", "-l", f"app={SOURCE_DEPLOY}", "-o", "jsonpath={.items[0].status.phase}").strip()
    if running != "Running":
        raise RolloutError(f"source {SOURCE_DEPLOY} is not Running: {running}")
    print(f"[source] {SOURCE_DEPLOY}: image={image}, node={node}, seed={session_path}, user={user}")
    apply_patch(files, dry)
    apply_canary(deployment(image, node, session_path, user), dry)
    verify_canary(files, dry)
    register_models(dry)
    grant_key(dry)
    print("[done]" if args.apply else "[dry-run] no changes applied")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RolloutError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
