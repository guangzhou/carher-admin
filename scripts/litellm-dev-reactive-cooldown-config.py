#!/usr/bin/env python3
"""
Configure litellm-dev (198) for reactive cooldown POC.

Adds an isolated `mock-pool-gpt-5.5` model_group backed by mock-chatgpt-upstream
(5 mock accounts), and tunes router_settings for reactive cooldown:
  allowed_fails: 1
  cooldown_time: 3600
  + (kept) optional_pre_call_checks deployment_affinity / responses_api / prompt_caching

Does NOT touch existing chatgpt-pool-gpt-5.5 entries (those point to PROD
chatgpt-acct-24 svc and cannot be used as test target).

Defaults to --dry-run. Run from repo:
  python3 scripts/litellm-dev-reactive-cooldown-config.py
  python3 scripts/litellm-dev-reactive-cooldown-config.py --apply
  python3 scripts/litellm-dev-reactive-cooldown-config.py --restore <remote-backup-path>
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile


REMOTE_SCRIPT = r"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml


POOL_NAME = "mock-pool-gpt-5.5"
# Mock registers /v1/responses; openai/ provider posts <api_base>/responses,
# so api_base must end with /v1 for the joined URL to hit /v1/responses.
MOCK_SVC = "http://mock-chatgpt-upstream.litellm-dev.svc.cluster.local:4101/v1"
MOCK_ADMIN_LIST = "http://mock-chatgpt-upstream.litellm-dev.svc.cluster.local:4101/_admin/accounts"

# Reactive cooldown desired router settings
RC_ROUTER = {
    "allowed_fails": 1,
    "cooldown_time": 3600,
}


def sh(args, *, input_text=None):
    return subprocess.check_output(args, input=input_text, text=True).strip()


def load_cm(namespace, cm_name):
    return json.loads(sh(["kubectl", "get", "cm", "-n", namespace, cm_name, "-o", "json"]))


def config_from_cm(cm):
    return yaml.safe_load(cm["data"]["config.yaml"])


def dump_config(data):
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def psql(namespace, sql):
    return sh([
        "kubectl", "exec", "-n", namespace, "litellm-db-0", "--",
        "psql", "-U", "litellm", "-d", "litellm", "-t", "-A", "-c", sql,
    ])


def load_db_router_settings(namespace):
    try:
        raw = psql(
            namespace,
            'SELECT COALESCE(param_value, \'{}\'::jsonb)::text FROM "LiteLLM_Config" '
            "WHERE param_name = 'router_settings' LIMIT 1;",
        )
    except Exception:
        return None
    return json.loads(raw) if raw else {}


def write_db_router_settings(namespace, router):
    payload = json.dumps(router, ensure_ascii=False, sort_keys=True)
    sql = "\n".join([
        'UPDATE "LiteLLM_Config"',
        f"SET param_value = {sql_literal(payload)}::jsonb",
        "WHERE param_name = 'router_settings';",
        'INSERT INTO "LiteLLM_Config" (param_name, param_value)',
        f"SELECT 'router_settings', {sql_literal(payload)}::jsonb",
        "WHERE NOT EXISTS (",
        '  SELECT 1 FROM "LiteLLM_Config" WHERE param_name = \'router_settings\'',
        ");",
    ])
    psql(namespace, sql)


def fetch_mock_accounts():
    # 198 host can't resolve cluster DNS; exec into the mock pod and call localhost
    raw = sh([
        "kubectl", "exec", "-n", "litellm-dev", "deploy/mock-chatgpt-upstream", "--",
        "python3", "-c",
        "import urllib.request,sys;"
        "sys.stdout.write(urllib.request.urlopen('http://localhost:4101/_admin/accounts',timeout=5).read().decode())",
    ])
    return json.loads(raw)


def find_models(data, model_name):
    return [m for m in data.get("model_list", []) if m.get("model_name") == model_name]


def ensure_mock_pool(data, changes, warnings):
    accounts = fetch_mock_accounts()
    if not accounts:
        warnings.append("mock-chatgpt-upstream returned 0 accounts; cannot register pool")
        return
    existing = {
        (m.get("model_info") or {}).get("id"): m
        for m in find_models(data, POOL_NAME)
    }
    model_list = data.setdefault("model_list", [])
    for name, acct in sorted(accounts.items()):
        deployment_id = f"mock-pool/{name}"
        entry = {
            "model_name": POOL_NAME,
            "litellm_params": {
                "model": "openai/chatgpt-gpt-5.5",
                "api_base": MOCK_SVC,
                "api_key": acct["access_token"],
            },
            "model_info": {
                "id": deployment_id,
                "input_cost_per_token": 0.0,
                "output_cost_per_token": 0.0,
                "mode": "responses",
            },
        }
        if deployment_id in existing:
            existing[deployment_id]["litellm_params"]["api_key"] = acct["access_token"]
            existing[deployment_id]["litellm_params"]["api_base"] = MOCK_SVC
            existing[deployment_id]["litellm_params"]["model"] = "openai/chatgpt-gpt-5.5"
            mi = existing[deployment_id].setdefault("model_info", {})
            mi["mode"] = "responses"
            mi["id"] = deployment_id
            changes.append(f"~ deployment refresh: {deployment_id} (model/mode/api_key)")
        else:
            model_list.append(entry)
            changes.append(f"+ deployment: {deployment_id} (model_name={POOL_NAME})")


def ensure_router_settings(router, changes):
    for k, v in RC_ROUTER.items():
        old = router.get(k)
        if old != v:
            router[k] = v
            changes.append(f"~ router_settings.{k}: {v} (was {old!r})")


def backup_paths(manifest):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return (
        f"{manifest}.bak-rc-{stamp}",
        f"/root/litellm-dev/litellm-config.cm.bak-rc-{stamp}.json",
        f"/root/litellm-dev/litellm-config.db-router_settings.bak-rc-{stamp}.json",
    )


def write_and_apply(cm, data, manifest, namespace, router_settings, restart):
    if data is not None:
        cm["data"]["config.yaml"] = dump_config(data)
        Path(manifest).write_text(json.dumps(cm, indent=2, ensure_ascii=False), encoding="utf-8")
        sh(["kubectl", "apply", "-f", manifest, "-n", namespace])
    if router_settings is not None:
        write_db_router_settings(namespace, router_settings)
    if restart:
        sh(["kubectl", "rollout", "restart", "deployment/litellm-proxy", "-n", namespace])
        sh(["kubectl", "rollout", "status", "deployment/litellm-proxy", "-n", namespace, "--timeout=180s"])


def restore_backup(args):
    backup = Path(args.restore)
    if not backup.exists():
        print(f"ERROR: backup not found: {backup}", file=sys.stderr)
        return 1
    current_cm = load_cm(args.namespace, args.configmap)
    text = backup.read_text(encoding="utf-8")
    restored_data = None
    restored_router = None
    try:
        obj = json.loads(text)
        if "data" in obj and "config.yaml" in obj["data"]:
            restored_data = yaml.safe_load(obj["data"]["config.yaml"])
        elif "fallbacks" in obj or "model_group_alias" in obj or "allowed_fails" in obj:
            restored_router = obj
        else:
            restored_data = obj
    except json.JSONDecodeError:
        restored_data = yaml.safe_load(text)
    print(f"restore source -> {backup}")
    write_and_apply(current_cm, restored_data, args.manifest, args.namespace, restored_router, not args.no_restart)
    print("restore done")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="litellm-dev")
    parser.add_argument("--configmap", default="litellm-config")
    parser.add_argument("--manifest", default="/root/litellm-dev/30-cm-litellm-config.yaml")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore")
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()

    if args.restore:
        return restore_backup(args)

    cm = load_cm(args.namespace, args.configmap)
    data = config_from_cm(cm)
    changes = []
    db_changes = []
    warnings = []

    ensure_mock_pool(data, changes, warnings)
    cm_router = data.setdefault("router_settings", {})
    ensure_router_settings(cm_router, changes)

    db_router = load_db_router_settings(args.namespace)
    if db_router is None:
        warnings.append("LiteLLM_Config.router_settings not readable; DB will not be patched")
        desired_db_router = None
    else:
        desired_db_router = copy.deepcopy(db_router)
        ensure_router_settings(desired_db_router, db_changes)

    print("litellm-dev reactive cooldown config")
    print(f"namespace : {args.namespace}")
    print(f"configmap : {args.configmap}")
    print(f"manifest  : {args.manifest}")
    print(f"mode      : {'apply' if args.apply else 'dry-run'}")
    print(f"mock-svc  : {MOCK_SVC}")

    if changes:
        print("\nchanges:")
        for c in changes:
            print(f"  {c}")
    else:
        print("\nchanges: none")
    if db_changes:
        print("\ndb router_settings changes:")
        for c in db_changes:
            print(f"  {c}")
    else:
        print("\ndb router_settings changes: none")
    if warnings:
        print("\nwarnings:")
        for w in warnings:
            print(f"  WARN: {w}")

    if not args.apply:
        print("\ndry-run only; pass --apply to write and restart")
        return 0
    if not changes and not db_changes:
        print("\nno changes to apply")
        return 0

    Path("/root/litellm-dev").mkdir(parents=True, exist_ok=True)
    manifest_backup, cm_backup, db_router_backup = backup_paths(args.manifest)
    Path(manifest_backup).write_text(dump_config(config_from_cm(cm)), encoding="utf-8")
    Path(cm_backup).write_text(json.dumps(cm, indent=2, ensure_ascii=False), encoding="utf-8")
    if db_router is not None:
        Path(db_router_backup).write_text(json.dumps(db_router, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(f"\nbackup manifest -> {manifest_backup}")
    print(f"backup configmap -> {cm_backup}")
    if db_router is not None:
        print(f"backup db router_settings -> {db_router_backup}")

    write_and_apply(
        cm,
        data if changes else None,
        args.manifest,
        args.namespace,
        desired_db_router if db_changes else None,
        restart=not args.no_restart,
    )
    print("apply done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--restore", help="remote backup path to restore")
    p.add_argument("--no-restart", action="store_true")
    p.add_argument("--namespace", default="litellm-dev")
    p.add_argument("--configmap", default="litellm-config")
    p.add_argument("--manifest", default="/root/litellm-dev/30-cm-litellm-config.yaml")
    p.add_argument("--jms", default=None)
    return p.parse_args()


def main():
    args = parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    jms = args.jms or os.path.join(here, "jms")

    remote_args = [
        "--namespace", args.namespace,
        "--configmap", args.configmap,
        "--manifest", args.manifest,
    ]
    if args.apply:
        remote_args.append("--apply")
    if args.restore:
        remote_args.extend(["--restore", args.restore])
    if args.no_restart:
        remote_args.append("--no-restart")

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(REMOTE_SCRIPT)
        local_path = f.name

    try:
        remote_path = "/tmp/_litellm_dev_reactive_cooldown_config.py"
        subprocess.check_call([jms, "scp", local_path, f"AIYJY-litellm:{remote_path}"])
        cmd = "python3 " + shlex.quote(remote_path) + " " + shlex.join(remote_args)
        return subprocess.call([jms, "ssh", "AIYJY-litellm", cmd])
    finally:
        os.unlink(local_path)


if __name__ == "__main__":
    sys.exit(main())
