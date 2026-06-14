#!/usr/bin/env python3
"""Create a CarHer instance and immediately converge it to the H75 baseline.

The script intentionally treats "new Her" as a full lifecycle:
create -> H75 hardening -> generated config repair -> readiness gates.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
NS = "carher"
ADMIN_API = "https://admin.carher.net/api"
TARGET_TAG = "h75-runtime-fa244014-hermestest75-20260602"
TARGET_IMAGE = (
    "cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:"
    + TARGET_TAG
)
TARGET_PROFILE = "h75-openclaw"
INTERNAL_LITELLM = "http://litellm-proxy.carher.svc.cluster.local:4000/v1"
INTERNAL_DIFY = "http://dify-nginx.dify.svc.cluster.local"
INTERNAL_BOOTSTRAP = (
    "http://dify-bootstrap.dify.svc.cluster.local:5688/v1/bootstrap/carher-bot"
)


def log(message: str) -> None:
    print(message, flush=True)


def run(
    argv: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    cwd: pathlib.Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        argv,
        input=input_text,
        text=True,
        capture_output=True,
        cwd=str(cwd or ROOT),
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(argv)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def json_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def get_admin_api_key() -> str:
    if os.environ.get("ADMIN_API_KEY"):
        return os.environ["ADMIN_API_KEY"]
    proc = run(
        [
            "kubectl",
            "get",
            "secret",
            "carher-admin-secrets",
            "-n",
            NS,
            "-o",
            "jsonpath={.data.admin-api-key}",
        ]
    )
    encoded = proc.stdout.strip()
    if not encoded:
        raise RuntimeError("admin-api-key is empty")
    decoded = run(["base64", "-d"], input_text=encoded).stdout.strip()
    if not decoded:
        raise RuntimeError("decoded admin api key is empty")
    return decoded


def admin_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def admin_get(api_key: str, path: str) -> tuple[int, dict[str, Any]]:
    return json_request("GET", ADMIN_API + path, headers=admin_headers(api_key))


def admin_post(api_key: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return json_request("POST", ADMIN_API + path, headers=admin_headers(api_key), body=body)


def admin_put(api_key: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return json_request("PUT", ADMIN_API + path, headers=admin_headers(api_key), body=body)


def resolve_union_id(owner_name: str) -> str:
    proc = run(
        [
            "lark-cli",
            "api",
            "POST",
            "/open-apis/contact/v3/users/search",
            "--params",
            '{"user_id_type":"union_id","page_size":10}',
            "--data",
            json.dumps({"query": owner_name}, ensure_ascii=False),
        ]
    )
    payload = json.loads(proc.stdout)
    items = payload.get("data", {}).get("items", [])
    matches = [
        item
        for item in items
        if item.get("meta_data", {})
        .get("i18n_names", {})
        .get("zh_cn")
        == owner_name
    ]
    if len(matches) != 1:
        names = [
            {
                "id": item.get("id"),
                "name": item.get("meta_data", {})
                .get("i18n_names", {})
                .get("zh_cn"),
                "email": item.get("meta_data", {}).get("enterprise_mail_address"),
            }
            for item in items
        ]
        raise RuntimeError(
            f"owner_name={owner_name!r} resolved to {len(matches)} exact matches: {names}"
        )
    return matches[0]["id"]


def tenant_token(app_id: str, app_secret: str) -> str:
    status, payload = json_request(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    if status != 200 or "tenant_access_token" not in payload:
        raise RuntimeError(f"tenant token failed: status={status} payload={payload}")
    return payload["tenant_access_token"]


def resolve_owner_open_id(app_id: str, app_secret: str, owner_name: str) -> str:
    union_id = resolve_union_id(owner_name)
    token = tenant_token(app_id, app_secret)
    req = urllib.request.Request(
        f"https://open.feishu.cn/open-apis/contact/v3/users/{union_id}?user_id_type=union_id",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    try:
        return payload["data"]["user"]["open_id"]
    except KeyError as exc:
        raise RuntimeError(f"open_id resolve failed: {payload}") from exc


def create_instance(args: argparse.Namespace, api_key: str, owner_open_id: str) -> dict[str, Any]:
    status, existing = admin_get(api_key, f"/instances/{args.id}")
    if status != 404 and not args.allow_existing:
        raise RuntimeError(f"instance {args.id} already exists: status={status} payload={existing}")

    if status == 404:
        body = {
            "instances": [
                {
                    "id": args.id,
                    "name": args.name,
                    "app_id": args.app_id,
                    "app_secret": args.app_secret,
                    "owner": owner_open_id,
                    "provider": args.provider,
                    "model": args.model,
                    "prefix": args.prefix,
                    "deploy_group": f"beta-h75-{args.id}",
                }
            ]
        }
        create_status, payload = admin_post(api_key, "/instances/batch-import", body)
        if create_status >= 300:
            raise RuntimeError(f"batch-import failed: status={create_status} payload={payload}")
        result = (payload.get("results") or [{}])[0]
        if result.get("status") != "created":
            raise RuntimeError(f"unexpected create result: {payload}")
        log(
            "created id={id} cloudflare={cloudflare}".format(
                id=result.get("id"), cloudflare=result.get("cloudflare")
            )
        )
    else:
        log(f"instance {args.id} exists; continuing because --allow-existing was set")

    update_status, update_payload = admin_put(
        api_key,
        f"/instances/{args.id}",
        {"image": TARGET_TAG, "deploy_group": f"beta-h75-{args.id}"},
    )
    if update_status >= 300:
        raise RuntimeError(f"image/group update failed: status={update_status} payload={update_payload}")
    return update_payload


def annotate_profile(args: argparse.Namespace) -> None:
    run(
        [
            "kubectl",
            "annotate",
            "herinstances.carher.io",
            f"her-{args.id}",
            "-n",
            NS,
            f"carher.io/runtime-profile={TARGET_PROFILE}",
            f"carher.io/force-reconcile={int(time.time())}",
            "--overwrite",
        ]
    )
    if args.home_channel:
        run(
            [
                "kubectl",
                "annotate",
                "herinstances.carher.io",
                f"her-{args.id}",
                "-n",
                NS,
                f"carher.io/feishu-home-channel={args.home_channel}",
                "--overwrite",
            ]
        )


def ensure_hardening_configmap() -> None:
    script = ROOT / "scripts" / "h75-batch-upgrade.py"
    if not script.exists():
        raise RuntimeError(f"missing hardening script: {script}")
    yaml = run(
        [
            "kubectl",
            "-n",
            NS,
            "create",
            "configmap",
            "h75-batch-upgrade-script",
            f"--from-file=h75_batch_upgrade.py={script}",
            "--dry-run=client",
            "-o",
            "yaml",
        ]
    ).stdout
    run(["kubectl", "apply", "-f", "-"], input_text=yaml)


def run_hardening_job(args: argparse.Namespace) -> None:
    ensure_hardening_configmap()
    job = f"h75-create-{args.id}-{int(time.time())}"
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job, "labels": {"app": "h75-batch-upgrade"}},
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": {"app": "h75-batch-upgrade"}},
                "spec": {
                    "serviceAccountName": "carher-admin",
                    "restartPolicy": "Never",
                    "imagePullSecrets": [
                        {"name": "acr-secret"},
                        {"name": "acr-vpc-secret"},
                    ],
                    "containers": [
                        {
                            "name": "runner",
                            "image": "cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-admin:v20260522-000124-7d25930",
                            "command": [
                                "python3",
                                "/scripts/h75_batch_upgrade.py",
                                "--apply",
                                "--include-target-crd",
                                "--wave-size",
                                "1",
                                "--only",
                                str(args.id),
                                "--results",
                                f"/tmp/h75_create_{args.id}_results.json",
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "512Mi"},
                                "limits": {"memory": "2Gi"},
                            },
                            "volumeMounts": [{"name": "script", "mountPath": "/scripts"}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "script",
                            "configMap": {"name": "h75-batch-upgrade-script"},
                        }
                    ],
                },
            },
        },
    }
    run(["kubectl", "-n", NS, "apply", "-f", "-"], input_text=json.dumps(manifest))
    log(f"hardening job={job}")
    logs = run(["kubectl", "-n", NS, "logs", "-f", f"job/{job}"], timeout=1200)
    print(logs.stdout, end="")
    if "failed_like=0" not in logs.stdout:
        raise RuntimeError(f"hardening job did not finish cleanly: {job}")


def set_litellm_budget(args: argparse.Namespace) -> None:
    script = ROOT / "scripts" / "litellm-key-budget.py"
    run([str(script), "--apply", "--key", f"carher-{args.id}"], cwd=ROOT, timeout=300)


def current_pod(her_id: int) -> str:
    for _ in range(90):
        proc = run(
            [
                "kubectl",
                "-n",
                NS,
                "get",
                "pod",
                "-l",
                f"app=carher-user,user-id={her_id}",
                "--field-selector=status.phase=Running",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        time.sleep(2)
    raise RuntimeError(f"no running pod found for carher-{her_id}")


def kubectl_exec(pod: str, command: str, *, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return run(
        ["kubectl", "-n", NS, "exec", pod, "-c", "carher", "--", "sh", "-lc", command],
        check=check,
        timeout=timeout,
    )


def fix_dify_generated_config(her_id: int) -> None:
    pod = current_pod(her_id)
    code = f"""
import json, os, pathlib, tempfile, time
p=pathlib.Path('/data/.openclaw/workflow/dify-config.json')
if not p.exists():
    raise SystemExit(0)
data=json.loads(p.read_text())
changed=False
if data.get('dify_base_url') != '{INTERNAL_DIFY}':
    data['dify_base_url'] = '{INTERNAL_DIFY}'
    changed=True
target_lifecycle='{INTERNAL_BOOTSTRAP.rsplit('/v1/bootstrap/carher-bot', 1)[0]}/v1/lifecycle/carher-{her_id}'
if data.get('lifecycle_base_url') != target_lifecycle:
    data['lifecycle_base_url'] = target_lifecycle
    changed=True
if changed:
    backup=p.with_name(p.name + '.bak-internal-url-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    backup.write_text(p.read_text())
    fd,tmp=tempfile.mkstemp(prefix='dify-config.', dir=str(p.parent))
    with os.fdopen(fd, 'w') as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write('\\n')
    os.replace(tmp, p)
print('dify_generated_config=' + ('patched' if changed else 'ok'))
"""
    kubectl_exec(pod, "python3 - <<'PY'\n" + code + "\nPY")


def wait_openclaw_ready(her_id: int) -> dict[str, Any]:
    last = ""
    for _ in range(36):
        pod = current_pod(her_id)
        proc = kubectl_exec(
            pod,
            "printf 'engine='; cat /data/.engine/active 2>/dev/null; "
            "printf '\\nhealth='; curl -fsS -m 10 http://127.0.0.1:18789/healthz",
            check=False,
            timeout=20,
        )
        last = proc.stdout + proc.stderr
        if proc.returncode == 0 and '"status":"live"' in proc.stdout:
            return {"ok": True, "pod": pod, "output": proc.stdout.strip()}
        time.sleep(10)
    return {"ok": False, "output": last.strip()}


def deployment_hardening_summary(her_id: int) -> dict[str, Any]:
    proc = run(["kubectl", "-n", NS, "get", "deploy", f"carher-{her_id}", "-o", "json"])
    dep = json.loads(proc.stdout)
    spec = dep["spec"]["template"]["spec"]
    carher = next(c for c in spec["containers"] if c.get("name") == "carher")
    env = {e["name"]: e.get("value") for e in carher.get("env", []) if "value" in e}
    env_refs = {e["name"] for e in carher.get("env", []) if "valueFrom" in e}
    writable_names = {
        "h75-agent-skills",
        "h75-openclaw-local",
        "h75-runtime-plugins",
        "h75-openclaw-extensions",
        "h75-openclaw-skills",
        "h75-hermes-skills",
        "h75-hermes-opt-skills",
    }
    readonly = [
        [m.get("name"), m.get("mountPath")]
        for m in carher.get("volumeMounts", [])
        if m.get("name") in writable_names and m.get("readOnly") is True
    ]
    base_config = next(
        (v.get("configMap", {}).get("name") for v in spec.get("volumes", []) if v.get("name") == "base-config"),
        "",
    )
    init_names = {c.get("name") for c in spec.get("initContainers", [])}
    return {
        "image_ok": carher.get("image") == TARGET_IMAGE,
        "base_config": base_config,
        "openai_base_ok": env.get("OPENAI_BASE_URL") == INTERNAL_LITELLM,
        "dify_base_ok": env.get("CARHER_DIFY_BASE_URL") == INTERNAL_DIFY,
        "dify_bootstrap_ok": env.get("CARHER_DIFY_BOOTSTRAP_URL") == INTERNAL_BOOTSTRAP,
        "runtime_plugins_refresh": env.get("CARHER_RUNTIME_PLUGINS_REFRESH"),
        "pythonpath": env.get("PYTHONPATH"),
        "prod_key_matches_litellm": bool(env.get("CARHER_PROD_KEY"))
        and env.get("CARHER_PROD_KEY") == env.get("LITELLM_API_KEY"),
        "gateway_token_ref": "CARHER_GATEWAY_TOKEN" in env_refs,
        "anthropic_token_ref": "ANTHROPIC_AUTH_TOKEN" in env_refs,
        "dify_token_ref": "CARHER_DIFY_BOOTSTRAP_TOKEN" in env_refs,
        "copy_deps_init": "copy-hermes-feishu-deps" in init_names,
        "readonly_h75_mounts": readonly,
    }


def runtime_probes(her_id: int) -> dict[str, Any]:
    pod = current_pod(her_id)
    deps = kubectl_exec(
        pod,
        "PYTHONPATH=/data/.openclaw/local/hermes-python-packages "
        "/opt/hermes/.venv/bin/python3 -c 'import lark_oapi, aiohttp_socks; print(\"ok\")'",
        check=False,
    )
    dify = kubectl_exec(
        pod,
        "/data/.openclaw/local/bin/her-workflow-dify-creator health",
        check=False,
        timeout=60,
    )
    return {
        "pod": pod,
        "hermes_deps_ok": deps.returncode == 0 and "ok" in deps.stdout,
        "dify_health_ok": dify.returncode == 0
        and '"dify_setup_status": 200' in dify.stdout
        and '"lifecycle_status": 200' in dify.stdout,
        "dify_health": dify.stdout.strip(),
    }


def callback_status(her_id: int, prefix: str) -> str:
    url = f"https://{prefix}-u{her_id}-auth.carher.net/feishu/oauth/callback?code=test&state=test"
    proc = run(
        ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", url],
        check=False,
        timeout=20,
    )
    return proc.stdout.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--app-secret", required=True)
    parser.add_argument("--owner-name")
    parser.add_argument("--owner-open-id")
    parser.add_argument("--prefix", default="s1")
    parser.add_argument("--provider", default="litellm")
    parser.add_argument("--model", default="gpt")
    parser.add_argument("--home-channel")
    parser.add_argument("--allow-existing", action="store_true")
    parser.add_argument("--skip-budget", action="store_true")
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(2)
    args = parser.parse_args()
    if not args.owner_name and not args.owner_open_id:
        parser.error("one of --owner-name or --owner-open-id is required")
    return args


def main() -> None:
    args = parse_args()
    api_key = get_admin_api_key()
    owner_open_id = args.owner_open_id or resolve_owner_open_id(
        args.app_id, args.app_secret, args.owner_name
    )
    log(f"owner_open_id_resolved=yes her={args.id}")
    create_instance(args, api_key, owner_open_id)
    annotate_profile(args)
    run_hardening_job(args)
    if not args.skip_budget:
        set_litellm_budget(args)
    # A second generated-config pass is intentional: bootstrap can render public URLs even
    # when Deployment env is already corrected.
    time.sleep(5)
    fix_dify_generated_config(args.id)
    ready = wait_openclaw_ready(args.id)
    hardening = deployment_hardening_summary(args.id)
    probes = runtime_probes(args.id)
    status, detail = admin_get(api_key, f"/instances/{args.id}")
    callback = callback_status(args.id, args.prefix)
    result = {
        "id": args.id,
        "name": args.name,
        "admin_status": status,
        "instance": {
            k: detail.get(k)
            for k in ["id", "name", "status", "image", "deploy_group", "provider", "model", "feishu_ws", "oauth_url"]
        },
        "openclaw_ready": ready,
        "deployment_hardening": hardening,
        "runtime_probes": probes,
        "oauth_callback_http": callback,
        "feishu_group_smoke": "not_self_tested/no_home_channel"
        if not args.home_channel
        else "not_run",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    failed = []
    if not ready.get("ok"):
        failed.append("openclaw_ready")
    for key in [
        "image_ok",
        "openai_base_ok",
        "dify_base_ok",
        "dify_bootstrap_ok",
        "prod_key_matches_litellm",
        "gateway_token_ref",
        "anthropic_token_ref",
        "dify_token_ref",
        "copy_deps_init",
    ]:
        if not hardening.get(key):
            failed.append(f"deployment_hardening.{key}")
    if hardening.get("base_config") != "carher-base-config-h75":
        failed.append("deployment_hardening.base_config")
    if hardening.get("runtime_plugins_refresh") != "0":
        failed.append("deployment_hardening.runtime_plugins_refresh")
    if hardening.get("readonly_h75_mounts"):
        failed.append("deployment_hardening.readonly_h75_mounts")
    if not probes.get("hermes_deps_ok"):
        failed.append("runtime_probes.hermes_deps")
    if not probes.get("dify_health_ok"):
        failed.append("runtime_probes.dify_health")
    if failed:
        raise SystemExit("failed gates: " + ", ".join(failed))


if __name__ == "__main__":
    main()
