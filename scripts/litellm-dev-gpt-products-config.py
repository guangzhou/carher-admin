#!/usr/bin/env python3
"""
Configure 198 litellm-dev GPT product model aliases and router fallbacks.

litellm-dev has store_model_in_db=true, so this script patches both:
  - litellm-dev/litellm-config ConfigMap for restart/reapply persistence
  - LiteLLM_Config.router_settings for the runtime router state

Default mode is --dry-run. Run from this repo:

  python3 scripts/litellm-dev-gpt-products-config.py
  python3 scripts/litellm-dev-gpt-products-config.py --apply
  python3 scripts/litellm-dev-gpt-products-config.py --restore /root/litellm-dev/...

The actual work runs on AIYJY-litellm through scripts/jms.
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
from pathlib import Path

import yaml


PRODUCT_MODELS = [
    "gpt-5.5",
    "chatgpt-gpt-5.5",
    "gpt-5.4",
    "chatgpt-gpt-5.4",
    "gpt-5.2",
    "gpt-5.3-codex",
    "chatgpt-gpt-5.3-codex",
    "gpt-5.4-mini",
    "chatgpt-gpt-5.3-codex-spark",
]

DESIRED_ALIASES = {
    "gpt-5.5": "chatgpt-pool-gpt-5.5",
    "chatgpt-gpt-5.5": "chatgpt-pool-gpt-5.5",
    "gpt-5.4": "chatgpt-gpt-5.4",
    "gpt-5.2": "chatgpt-gpt-5.4",
    "gpt-5.3-codex": "chatgpt-gpt-5.3-codex",
    "gpt-5.4-mini": "chatgpt-gpt-5.3-codex-spark",
}

DESIRED_FALLBACKS = {
    "gpt-5.5": ["wangsu-gpt-5.5"],
    "chatgpt-gpt-5.5": ["wangsu-gpt-5.5"],
    "chatgpt-pool-gpt-5.5": ["wangsu-gpt-5.5"],
    "gpt-5.4": ["wangsu-gpt-5.4"],
    "chatgpt-gpt-5.4": ["wangsu-gpt-5.4"],
    "gpt-5.2": ["wangsu-gpt-5.4"],
    "gpt-5.3-codex": ["wangsu7-gpt-5.3-codex"],
    "chatgpt-gpt-5.3-codex": ["wangsu7-gpt-5.3-codex"],
}

NO_FALLBACK_MODELS = {
    "gpt-5.4-mini",
    "chatgpt-gpt-5.3-codex-spark",
}

DIRECT_INTERNAL_ALIAS_PREFIXES = ("wangsu", "openrouter")


def sh(args: list[str], *, input_text: str | None = None) -> str:
    return subprocess.check_output(args, input=input_text, text=True).strip()


def load_cm(namespace: str, cm_name: str) -> dict:
    raw = sh(["kubectl", "get", "cm", "-n", namespace, cm_name, "-o", "json"])
    return json.loads(raw)


def config_from_cm(cm: dict) -> dict:
    return yaml.safe_load(cm["data"]["config.yaml"])


def dump_config(data: dict) -> str:
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def model_names(data: dict) -> set[str]:
    return {m.get("model_name") for m in data.get("model_list", [])}


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def psql(namespace: str, sql: str) -> str:
    return sh([
        "kubectl",
        "exec",
        "-n",
        namespace,
        "litellm-db-0",
        "--",
        "psql",
        "-U",
        "litellm",
        "-d",
        "litellm",
        "-t",
        "-A",
        "-c",
        sql,
    ])


def load_db_router_settings(namespace: str) -> dict | None:
    try:
        raw = psql(
            namespace,
            'SELECT COALESCE(param_value, \'{}\'::jsonb)::text FROM "LiteLLM_Config" '
            "WHERE param_name = 'router_settings' LIMIT 1;",
        )
    except Exception:
        return None
    return json.loads(raw) if raw else {}


def write_db_router_settings(namespace: str, router: dict) -> None:
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


def find_model(data: dict, name: str) -> dict | None:
    for entry in data.get("model_list", []):
        if entry.get("model_name") == name:
            return entry
    return None


def ensure_chatgpt_pool(data: dict, changes: list[str]) -> None:
    if find_model(data, "chatgpt-pool-gpt-5.5") is not None:
        return

    source = find_model(data, "chatgpt-gpt-5.5")
    if source is None:
        raise SystemExit("ERROR: chatgpt-gpt-5.5 is missing; cannot create pool entry")

    entry = copy.deepcopy(source)
    entry["model_name"] = "chatgpt-pool-gpt-5.5"
    info = entry.setdefault("model_info", {})
    info["id"] = "chatgpt-pool/gpt-5.5/account-2"
    info["mode"] = "responses"
    data.setdefault("model_list", []).append(entry)
    changes.append("+ model_list: chatgpt-pool-gpt-5.5 copied from chatgpt-gpt-5.5")


def ensure_wangsu7_codex(data: dict, changes: list[str], warnings: list[str], prod_cm: str) -> None:
    if find_model(data, "wangsu7-gpt-5.3-codex") is not None:
        return

    try:
        prod = config_from_cm(load_cm("litellm-product", prod_cm))
    except Exception as exc:
        warnings.append(f"cannot load litellm-product/{prod_cm}: {exc}")
        return

    source = find_model(prod, "wangsu7-gpt-5.3-codex")
    if source is None:
        warnings.append("wangsu7-gpt-5.3-codex not found in product config; codex fallback will be BLOCKED")
        return

    data.setdefault("model_list", []).append(copy.deepcopy(source))
    changes.append("+ model_list: wangsu7-gpt-5.3-codex copied from product config")


def ensure_aliases(data: dict, changes: list[str]) -> None:
    known = model_names(data)
    router = data.setdefault("router_settings", {})
    aliases = router.setdefault("model_group_alias", {})

    for src, dst in DESIRED_ALIASES.items():
        if dst.startswith(DIRECT_INTERNAL_ALIAS_PREFIXES):
            raise SystemExit(f"ERROR: refusing direct internal alias {src} -> {dst}")
        if dst not in known:
            raise SystemExit(f"ERROR: alias target {dst!r} is not in model_list")
        old = aliases.get(src)
        if old != dst:
            aliases[src] = dst
            changes.append(f"~ alias: {src} -> {dst} (was {old or '<missing>'})")


def set_fallback(fallbacks: list[dict], source: str, targets: list[str]) -> str | None:
    first_seen = False
    old_value = None
    for fb in list(fallbacks):
        if source not in fb:
            continue
        if not first_seen:
            first_seen = True
            old_value = fb.get(source)
            if fb.get(source) != targets:
                fb[source] = targets
        else:
            del fb[source]
            if not fb:
                fallbacks.remove(fb)

    if not first_seen:
        fallbacks.append({source: targets})
        return "<missing>"
    if old_value != targets:
        return json.dumps(old_value, ensure_ascii=True)
    return None


def remove_fallback(fallbacks: list[dict], source: str) -> bool:
    removed = False
    for fb in list(fallbacks):
        if source in fb:
            del fb[source]
            removed = True
        if not fb:
            fallbacks.remove(fb)
    return removed


def ensure_fallbacks(data: dict, changes: list[str], warnings: list[str]) -> None:
    known = model_names(data)
    router = data.setdefault("router_settings", {})
    fallbacks = router.setdefault("fallbacks", [])

    for source in NO_FALLBACK_MODELS:
        if remove_fallback(fallbacks, source):
            changes.append(f"- fallback: removed {source}")

    for source, targets in DESIRED_FALLBACKS.items():
        missing = [target for target in targets if target not in known]
        if missing:
            warnings.append(
                f"skip fallback {source} -> {targets}: missing target(s) {missing}; verification should mark BLOCKED"
            )
            continue
        old = set_fallback(fallbacks, source, targets)
        if old is not None:
            changes.append(f"~ fallback: {source} -> {targets} (was {old})")


def backup_paths(manifest: str) -> tuple[str, str, str]:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest_backup = f"{manifest}.bak-gpt-products-{stamp}"
    cm_backup = f"/root/litellm-dev/litellm-config.cm.bak-gpt-products-{stamp}.json"
    db_router_backup = f"/root/litellm-dev/litellm-config.db-router_settings.bak-gpt-products-{stamp}.json"
    return manifest_backup, cm_backup, db_router_backup


def write_and_apply(
    cm: dict,
    data: dict | None,
    manifest: str,
    namespace: str,
    *,
    router_settings: dict | None,
    restart: bool,
) -> None:
    if data is not None:
        cm["data"]["config.yaml"] = dump_config(data)
        Path(manifest).write_text(json.dumps(cm, indent=2, ensure_ascii=False), encoding="utf-8")
        sh(["kubectl", "apply", "-f", manifest, "-n", namespace])
    if router_settings is not None:
        write_db_router_settings(namespace, router_settings)
    if restart:
        sh(["kubectl", "rollout", "restart", "deployment/litellm-proxy", "-n", namespace])
        sh(["kubectl", "rollout", "status", "deployment/litellm-proxy", "-n", namespace, "--timeout=180s"])


def restore_backup(args: argparse.Namespace) -> int:
    backup = Path(args.restore)
    if not backup.exists():
        print(f"ERROR: backup not found on remote host: {backup}", file=sys.stderr)
        return 1

    current_cm = load_cm(args.namespace, args.configmap)
    text = backup.read_text(encoding="utf-8")
    restored_data = None
    restored_router = None
    try:
        obj = json.loads(text)
        if "data" in obj and "config.yaml" in obj["data"]:
            restored_data = yaml.safe_load(obj["data"]["config.yaml"])
        elif "fallbacks" in obj or "model_group_alias" in obj:
            restored_router = obj
        else:
            restored_data = obj
    except json.JSONDecodeError:
        restored_data = yaml.safe_load(text)

    print(f"restore source -> {backup}")
    write_and_apply(
        current_cm,
        restored_data,
        args.manifest,
        args.namespace,
        router_settings=restored_router,
        restart=not args.no_restart,
    )
    print("restore done")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="litellm-dev")
    parser.add_argument("--configmap", default="litellm-config")
    parser.add_argument("--product-configmap", default="litellm-config")
    parser.add_argument("--manifest", default="/root/litellm-dev/30-cm-litellm-config.yaml")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore")
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()

    if args.restore:
        return restore_backup(args)

    cm = load_cm(args.namespace, args.configmap)
    data = config_from_cm(cm)
    changes: list[str] = []
    db_changes: list[str] = []
    warnings: list[str] = []

    ensure_chatgpt_pool(data, changes)
    ensure_wangsu7_codex(data, changes, warnings, args.product_configmap)
    ensure_aliases(data, changes)
    ensure_fallbacks(data, changes, warnings)

    db_router = load_db_router_settings(args.namespace)
    if db_router is None:
        warnings.append("LiteLLM_Config.router_settings not readable; runtime DB router settings will not be patched")
        desired_db_router = None
    else:
        desired_db_data = {
            "model_list": data.get("model_list", []),
            "router_settings": copy.deepcopy(db_router),
        }
        ensure_aliases(desired_db_data, db_changes)
        ensure_fallbacks(desired_db_data, db_changes, warnings)
        desired_db_router = desired_db_data["router_settings"]

    print("litellm-dev GPT product config")
    print(f"namespace : {args.namespace}")
    print(f"configmap : {args.configmap}")
    print(f"manifest  : {args.manifest}")
    print(f"mode      : {'apply' if args.apply else 'dry-run'}")

    if changes:
        print("\nchanges:")
        for change in changes:
            print(f"  {change}")
    else:
        print("\nchanges: none")

    if db_changes:
        print("\ndb router_settings changes:")
        for change in db_changes:
            print(f"  {change}")
    else:
        print("\ndb router_settings changes: none")

    if warnings:
        print("\nwarnings:")
        for warning in warnings:
            print(f"  WARN: {warning}")

    if not args.apply:
        print("\ndry-run only; pass --apply to write and restart")
        return 0

    if not changes and not db_changes:
        print("\nno changes to apply")
        return 0

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
        router_settings=desired_db_router if db_changes else None,
        restart=not args.no_restart,
    )
    print("apply done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write config and restart litellm-dev")
    mode.add_argument("--restore", help="remote backup path to restore")
    parser.add_argument("--no-restart", action="store_true", help="apply without rollout restart")
    parser.add_argument("--namespace", default="litellm-dev")
    parser.add_argument("--configmap", default="litellm-config")
    parser.add_argument("--manifest", default="/root/litellm-dev/30-cm-litellm-config.yaml")
    parser.add_argument("--jms", default=None, help="path to jms wrapper")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    jms = args.jms or os.path.join(here, "jms")

    remote_args = [
        "--namespace",
        args.namespace,
        "--configmap",
        args.configmap,
        "--manifest",
        args.manifest,
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
        remote_path = "/tmp/_litellm_dev_gpt_products_config.py"
        subprocess.check_call([jms, "scp", local_path, f"AIYJY-litellm:{remote_path}"])
        command = "python3 " + shlex.quote(remote_path) + " " + shlex.join(remote_args)
        return subprocess.call([jms, "ssh", "AIYJY-litellm", command])
    finally:
        os.unlink(local_path)


if __name__ == "__main__":
    sys.exit(main())
