#!/usr/bin/env python3
"""
Converge 198 Pro cursor keys and GPT router rules to the GPT product matrix.

Default mode is --dry-run. All remote work runs on AIYJY-litellm through
scripts/jms. Reports are written locally under reports/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


REMOTE_SCRIPT = r"""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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
    "gpt-5.4-mini": ["wangsu-gpt-5.4"],
    "chatgpt-gpt-5.3-codex-spark": ["wangsu-gpt-5.4"],
}

CODEX_COMPAT_GROUP = "chatgpt-gpt-5.3-codex"
CODEX_COMPAT_TARGET_MODEL = "openai/chatgpt-gpt-5.3-codex-spark"
CODEX_SPARK_PRODUCT = "chatgpt-gpt-5.3-codex-spark"

HIDDEN_MODELS = {
    "chatgpt-pool-gpt-5.5",
    "wangsu-gpt-5.5",
    "wangsu-gpt-5.4",
    "wangsu7-gpt-5.3-codex",
    "openrouter-gpt-5.2",
    "openrouter-gpt-5.3-codex",
    "openrouter-gpt-5.4",
    "openrouter-gpt-5.4-mini",
    "openrouter-gpt-5.5",
    "openrouter-gpt-5.5-pro",
    "gemini-3.1-pro-preview",
    "glm-5",
    "glm-5.1",
    "minimax-m2.7",
    "wangsu-gemini-3.1-pro-preview",
    "wangsu-glm-5.1",
}

MANAGED_FALLBACK_SOURCES = set(PRODUCT_MODELS) | set(DESIRED_FALLBACKS) | {
    "openrouter-gpt-5.2",
    "openrouter-gpt-5.3-codex",
    "openrouter-gpt-5.4",
    "openrouter-gpt-5.4-mini",
    "openrouter-gpt-5.5",
    "openrouter-gpt-5.5-pro",
    "wangsu-gpt-5.5",
    "wangsu-gpt-5.4",
    "wangsu7-gpt-5.5",
    "wangsu7-gpt-5.4",
    "wangsu7-gpt-5.3-codex",
}

ALIAS_REMOVE_SOURCES = MANAGED_FALLBACK_SOURCES | {"wangsu-gpt-5.5", "wangsu-gpt-5.4"}
BEGIN_MARKER = "__LITELLM_PRO_GPT_CONVERGE_RESULT_BEGIN__"
END_MARKER = "__LITELLM_PRO_GPT_CONVERGE_RESULT_END__"


def sh(args: list[str], *, input_text: str | None = None) -> str:
    return subprocess.check_output(args, input=input_text, text=True).strip()


def http_json(method: str, url: str, key: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode(errors="replace")
            return {"status": resp.status, "text": text, "json": parse_json(text)}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        return {"status": exc.code, "text": text, "json": parse_json(text)}
    except Exception as exc:
        return {"status": 0, "text": str(exc), "json": None}


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


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


def psql_json(namespace: str, sql: str) -> Any:
    out = psql(namespace, sql)
    return json.loads(out) if out else None


def master_key(namespace: str) -> str:
    raw = sh([
        "kubectl",
        "get",
        "secret",
        "litellm-secrets",
        "-n",
        namespace,
        "-o",
        "jsonpath={.data.LITELLM_MASTER_KEY}",
    ])
    return base64.b64decode(raw).decode()


def load_cm(namespace: str, configmap: str) -> dict:
    return json.loads(sh(["kubectl", "get", "cm", "-n", namespace, configmap, "-o", "json"]))


def load_config(cm: dict) -> dict:
    return yaml.safe_load(cm["data"]["config.yaml"]) or {}


def dump_config(data: dict) -> str:
    return yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def config_checksum(cm: dict) -> str:
    return hashlib.sha256(cm["data"]["config.yaml"].encode()).hexdigest()


def model_names(data: dict) -> set[str]:
    return {entry.get("model_name") for entry in data.get("model_list", []) if entry.get("model_name")}


def fallback_map(fallbacks: list[dict]) -> dict[str, list[str]]:
    mapped: dict[str, list[str]] = {}
    for item in fallbacks:
        if isinstance(item, dict):
            for source, targets in item.items():
                mapped[source] = list(targets or [])
    return mapped


def fallback_list(mapped: dict[str, list[str]]) -> list[dict]:
    return [{source: targets} for source, targets in mapped.items()]


def runtime_model_names(base: str, master: str) -> tuple[set[str], dict]:
    resp = http_json("GET", f"{base}/model/info", master, timeout=60)
    names: set[str] = set()
    if resp["status"] == 200 and isinstance(resp["json"], dict):
        for item in resp["json"].get("data") or []:
            if isinstance(item, dict) and item.get("model_name"):
                names.add(item["model_name"])
    return names, {"status": resp["status"], "count": len(names), "error": resp["text"][:500] if resp["status"] != 200 else ""}


def sanitized_model_row(item: dict) -> dict:
    row = copy.deepcopy(item)
    params = row.setdefault("litellm_params", {})
    if "api_key" in params:
        params["api_key"] = "<redacted>"
    return row


def runtime_model_rows(base: str, master: str, wanted_names: set[str]) -> tuple[list[dict], dict]:
    resp = http_json("GET", f"{base}/model/info", master, timeout=60)
    rows: list[dict] = []
    if resp["status"] == 200 and isinstance(resp["json"], dict):
        for item in resp["json"].get("data") or []:
            if isinstance(item, dict) and item.get("model_name") in wanted_names:
                rows.append(sanitized_model_row(item))
    info = {"status": resp["status"], "count": len(rows), "error": resp["text"][:500] if resp["status"] != 200 else ""}
    return rows, info


def model_row_summary(row: dict) -> dict:
    params = row.get("litellm_params") or {}
    info = row.get("model_info") or {}
    return {
        "model_name": row.get("model_name"),
        "litellm_model": params.get("model"),
        "api_base": params.get("api_base"),
        "model_id": info.get("id"),
        "mode": info.get("mode"),
    }


def db_proxy_model_rows(namespace: str, model_names_: set[str]) -> list[dict]:
    if not model_names_:
        return []
    names_sql = ",".join(sql_literal(name) for name in sorted(model_names_))
    sql = f'''
SELECT COALESCE(json_agg(row_to_json(t) ORDER BY model_id), '[]'::json)
FROM (
  SELECT
    model_id,
    model_name,
    litellm_params,
    model_info,
    created_at::text AS created_at,
    created_by,
    updated_at::text AS updated_at,
    updated_by,
    blocked
  FROM "LiteLLM_ProxyModelTable"
  WHERE model_name IN ({names_sql})
) t;
'''
    return psql_json(namespace, sql) or []


def model_patch_payload_from_runtime_row(row: dict) -> dict:
    params = {}
    info = copy.deepcopy(row.get("model_info") or {})
    if info.get("mode") is None:
        info["mode"] = "responses"
    return {
        "model_name": row.get("model_name"),
        "litellm_params": params,
        "model_info": info,
    }


def codex_compat_payload(row: dict) -> dict:
    payload = model_patch_payload_from_runtime_row(row)
    params = payload["litellm_params"]
    info = payload["model_info"]
    params["model"] = CODEX_COMPAT_TARGET_MODEL
    info["mode"] = "responses"
    return payload


def codex_compat_plan(rows: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    errors: list[str] = []
    changes: list[str] = []
    desired_by_id: dict[str, dict] = {}
    if not rows:
        errors.append(f"missing runtime model rows for {CODEX_COMPAT_GROUP}")
        return [], changes, errors

    for row in rows:
        summary = model_row_summary(row)
        if not summary.get("api_base"):
            errors.append(f"{summary.get('model_id') or CODEX_COMPAT_GROUP} missing api_base")
            continue
        if not summary.get("model_id"):
            errors.append(f"{CODEX_COMPAT_GROUP} row on {summary.get('api_base')} missing model_info.id")
            continue
        if str(summary.get("api_base") or "").startswith(("https://aigateway", "https://openrouter")):
            errors.append(f"refusing codex primary on non-ChatGPT api_base: {summary.get('api_base')}")
            continue

        desired = codex_compat_payload(row)
        desired_summary = model_row_summary(desired)
        desired_by_id[desired_summary["model_id"]] = desired
        if (
            summary.get("litellm_model") != CODEX_COMPAT_TARGET_MODEL
            or summary.get("mode") != "responses"
        ):
            changes.append(
                "codex acct deployment "
                f"{summary.get('model_id')}: model {summary.get('litellm_model')} -> {CODEX_COMPAT_TARGET_MODEL}; "
                f"mode {summary.get('mode')} -> responses; preserves existing api_key/api_base/model_id"
            )
    return list(desired_by_id.values()), changes, errors


def desired_router_config(data: dict, extra_known_models: set[str] | None = None) -> tuple[dict, list[str], list[str]]:
    desired = copy.deepcopy(data)
    router = desired.setdefault("router_settings", {})
    aliases = copy.deepcopy(router.get("model_group_alias") or {})
    fallbacks = fallback_map(router.get("fallbacks") or [])
    changes: list[str] = []
    errors: list[str] = []
    known = model_names(desired) | set(extra_known_models or set())

    required_targets = set(DESIRED_ALIASES.values())
    for targets in DESIRED_FALLBACKS.values():
        required_targets.update(targets)
    missing_targets = sorted(target for target in required_targets if target not in known)
    if missing_targets:
        errors.append(f"missing model_list targets: {missing_targets}")

    for source, target in DESIRED_ALIASES.items():
        if target.startswith(("wangsu", "openrouter")):
            errors.append(f"refusing product alias to fallback provider: {source} -> {target}")
        old = aliases.get(source)
        if old != target:
            changes.append(f"alias {source}: {old or '<missing>'} -> {target}")
        aliases[source] = target

    for source in sorted(ALIAS_REMOVE_SOURCES - set(DESIRED_ALIASES)):
        if source in aliases:
            changes.append(f"alias {source}: remove {aliases[source]}")
            aliases.pop(source, None)

    for source in sorted(MANAGED_FALLBACK_SOURCES):
        if source in fallbacks:
            changes.append(f"fallback {source}: remove {fallbacks[source]}")
            fallbacks.pop(source, None)

    for source, targets in DESIRED_FALLBACKS.items():
        old = fallbacks.get(source)
        if old != targets:
            changes.append(f"fallback {source}: {old or '<missing>'} -> {targets}")
        fallbacks[source] = list(targets)

    router["model_group_alias"] = aliases
    router["fallbacks"] = fallback_list(fallbacks)
    return desired, changes, errors


def cursor_key_rows(namespace: str) -> list[dict]:
    sql = '''
SELECT COALESCE(json_agg(row_to_json(t) ORDER BY key_alias), '[]'::json)
FROM (
  SELECT
    token,
    key_alias,
    COALESCE(models, ARRAY[]::text[]) AS models,
    COALESCE(aliases, '{}'::jsonb) AS aliases,
    metadata,
    max_budget,
    budget_duration,
    blocked
  FROM "LiteLLM_VerificationToken"
  WHERE key_alias ILIKE 'cursor-%' OR metadata->>'purpose' = 'cursor'
) t;
'''
    return psql_json(namespace, sql) or []


def key_diff(row: dict) -> dict:
    models = list(row.get("models") or [])
    aliases = row.get("aliases") or {}
    extra = sorted(set(models) - set(PRODUCT_MODELS))
    missing = sorted(set(PRODUCT_MODELS) - set(models))
    direct_aliases = {
        key: value
        for key, value in aliases.items()
        if key in PRODUCT_MODELS and str(value).startswith(("wangsu", "openrouter"))
    }
    return {
        "key_alias": row.get("key_alias"),
        "models": models,
        "aliases": aliases,
        "extra_models": extra,
        "missing_products": missing,
        "hidden_models": sorted(set(models) & HIDDEN_MODELS),
        "direct_internal_aliases": direct_aliases,
        "needs_update": bool(extra or missing or aliases),
    }


def cursor_audit(rows: list[dict]) -> dict:
    diffs = [key_diff(row) for row in rows]
    needs_update = [item for item in diffs if item["needs_update"]]
    return {
        "cursor_key_count": len(rows),
        "expected_product_models": PRODUCT_MODELS,
        "keys_exactly_9_products": sum(1 for item in diffs if not item["extra_models"] and not item["missing_products"]),
        "keys_with_extra_models": sum(1 for item in diffs if item["extra_models"]),
        "keys_with_missing_products": sum(1 for item in diffs if item["missing_products"]),
        "keys_with_hidden_models": sum(1 for item in diffs if item["hidden_models"]),
        "keys_with_aliases": sum(1 for item in diffs if item["aliases"]),
        "keys_with_direct_internal_aliases": sum(1 for item in diffs if item["direct_internal_aliases"]),
        "keys_needing_update": len(needs_update),
        "examples": needs_update[:20],
        "status": "PASS" if not needs_update else "FAIL",
    }


def validate_cursor_rows(rows: list[dict]) -> tuple[bool, dict]:
    audit = cursor_audit(rows)
    return audit["status"] == "PASS", audit


def backup_state(args: argparse.Namespace, cm: dict, rows: list[dict], proxy_model_rows: list[dict], runtime_rows: list[dict]) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(args.backup_root) / f"gpt-products-converge-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    (backup_dir / "configmap.json").write_text(json.dumps(cm, indent=2, ensure_ascii=False), encoding="utf-8")
    (backup_dir / "config.yaml").write_text(cm["data"]["config.yaml"], encoding="utf-8")
    manifest = Path(args.manifest)
    if manifest.exists():
        (backup_dir / "manifest.raw").write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    (backup_dir / "cursor_keys.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    (backup_dir / "proxy_model_rows_raw.json").write_text(json.dumps(proxy_model_rows, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    (backup_dir / "proxy_model_rows_runtime.json").write_text(json.dumps(runtime_rows, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return backup_dir


def restart_proxy(args: argparse.Namespace) -> None:
    if not args.no_restart:
        sh(["kubectl", "rollout", "restart", "deployment/litellm-proxy", "-n", args.namespace])
        sh(["kubectl", "rollout", "status", "deployment/litellm-proxy", "-n", args.namespace, "--timeout=240s"])


def apply_config(args: argparse.Namespace, desired_cm: dict) -> None:
    manifest = Path(args.manifest)
    manifest.write_text(yaml.safe_dump(desired_cm, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")
    sh(["kubectl", "apply", "-f", str(manifest), "-n", args.namespace])
    restart_proxy(args)


def update_key(base: str, master: str, token: str, models: list[str], aliases: dict) -> dict:
    return http_json("POST", f"{base}/key/update", master, {"key": token, "models": models, "aliases": aliases}, timeout=45)


def delete_key(base: str, master: str, alias: str) -> None:
    http_json("POST", f"{base}/key/delete", master, {"key_aliases": [alias]}, timeout=45)


def delete_model(base: str, master: str, model_id: str) -> dict:
    return http_json("POST", f"{base}/model/delete", master, {"id": model_id}, timeout=45)


def create_model(base: str, master: str, payload: dict) -> dict:
    return http_json("POST", f"{base}/model/new", master, payload, timeout=45)


def patch_model(base: str, master: str, model_id: str, payload: dict) -> dict:
    return http_json("PATCH", f"{base}/model/{model_id}/update", master, payload, timeout=45)


def infer_probe(base: str, master: str, model: str, marker: str) -> dict:
    body = {
        "model": model,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": f"Reply OK only. {marker}"}]}],
        "max_output_tokens": 16,
        "stream": True,
        "store": False,
    }
    return http_json("POST", f"{base}/v1/responses", master, body, timeout=90)


def api_alias_clear_probe(args: argparse.Namespace, base: str, master: str) -> dict:
    alias = "codex-pro-gpt-converge-probe-" + dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    body = {
        "key_alias": alias,
        "models": PRODUCT_MODELS + ["wangsu7-gpt-5.3-codex"],
        "aliases": {"gpt-5.3-codex": "wangsu7-gpt-5.3-codex"},
        "max_budget": 1,
        "budget_duration": "1d",
        "metadata": {"purpose": "pro-gpt-converge-probe"},
    }
    created = http_json("POST", f"{base}/key/generate", master, body, timeout=45)
    result = {"alias": alias, "create_status": created["status"], "update_status": None, "remaining": None, "status": "FAIL"}
    try:
        if created["status"] != 200:
            result["error"] = created["text"][:500]
            return result
        token = psql(args.namespace, f'SELECT token FROM "LiteLLM_VerificationToken" WHERE key_alias={sql_literal(alias)} LIMIT 1;').strip()
        updated = update_key(base, master, token, PRODUCT_MODELS, {})
        result["update_status"] = updated["status"]
        row = psql_json(
            args.namespace,
            'SELECT row_to_json(t) FROM ('
            "SELECT COALESCE(models, ARRAY[]::text[]) AS models, COALESCE(aliases, '{}'::jsonb) AS aliases "
            f'FROM "LiteLLM_VerificationToken" WHERE key_alias={sql_literal(alias)} LIMIT 1'
            ") t;",
        )
        result["row_after_update"] = row
        result["status"] = "PASS" if updated["status"] == 200 and row and row.get("models") == PRODUCT_MODELS and row.get("aliases") == {} else "FAIL"
        return result
    finally:
        delete_key(base, master, alias)
        try:
            remaining = psql(args.namespace, f'SELECT COUNT(*)::int FROM "LiteLLM_VerificationToken" WHERE key_alias={sql_literal(alias)};').strip()
            result["remaining"] = int(remaining or "0")
        except Exception as exc:
            result["remaining_error"] = str(exc)


def api_existing_spark_probe(base: str, master: str) -> dict:
    marker = "codex-pro-gpt-converge-existing-spark-probe-" + dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    resp = infer_probe(base, master, CODEX_SPARK_PRODUCT, marker)
    return {
        "model": CODEX_SPARK_PRODUCT,
        "http_status": resp["status"],
        "status": "PASS" if resp["status"] == 200 else "FAIL",
        "error": resp["text"][:500] if resp["status"] != 200 else "",
    }


def apply_codex_compat(base: str, master: str, current_rows: list[dict], desired_entries: list[dict], changes: list[str]) -> dict:
    result = {"status": "PASS", "changed": bool(changes), "patch": []}
    if not changes:
        return result

    failures = []
    for entry in desired_entries:
        model_id = (entry.get("model_info") or {}).get("id")
        resp = patch_model(base, master, str(model_id), entry)
        item = {"id": model_id, "http_status": resp["status"], "error": resp["text"][:500] if resp["status"] != 200 else ""}
        result["patch"].append(item)
        if resp["status"] != 200:
            failures.append(item)

    if failures:
        result["status"] = "FAIL"
        result["failures"] = failures
    return result


def restore_proxy_model_rows(args: argparse.Namespace, base: str, master: str, backup_rows: list[dict]) -> dict:
    result = {"status": "PASS", "changed": bool(backup_rows), "patch": []}
    if not backup_rows:
        result["status"] = "SKIPPED"
        result["reason"] = "backup has no proxy_model_rows_runtime.json entries"
        return result

    failures = []
    for row in backup_rows:
        payload = model_patch_payload_from_runtime_row(row)
        payload["litellm_params"]["model"] = (row.get("litellm_params") or {}).get("model")
        model_id = (payload.get("model_info") or {}).get("id")
        resp = patch_model(base, master, str(model_id), payload)
        item = {"id": model_id, "http_status": resp["status"], "error": resp["text"][:500] if resp["status"] != 200 else ""}
        result["patch"].append(item)
        if resp["status"] != 200:
            failures.append(item)

    if failures:
        result["status"] = "FAIL"
        result["failures"] = failures
    return result


def apply_key_updates(args: argparse.Namespace, rows: list[dict], base: str, master: str) -> list[dict]:
    failures = []
    for row in rows:
        diff = key_diff(row)
        if not diff["needs_update"]:
            continue
        resp = update_key(base, master, row["token"], PRODUCT_MODELS, {})
        if resp["status"] != 200:
            failures.append({
                "key_alias": row.get("key_alias"),
                "http_status": resp["status"],
                "error": resp["text"][:500],
            })
    return failures


def restore(args: argparse.Namespace) -> dict:
    backup = Path(args.restore)
    if not backup.is_dir():
        raise SystemExit(f"restore path must be a backup directory: {backup}")

    base = f"http://localhost:{args.nodeport}"
    master = master_key(args.namespace)
    cm = json.loads((backup / "configmap.json").read_text(encoding="utf-8"))
    rows = json.loads((backup / "cursor_keys.json").read_text(encoding="utf-8"))
    proxy_runtime_path = backup / "proxy_model_rows_runtime.json"
    proxy_runtime_rows = json.loads(proxy_runtime_path.read_text(encoding="utf-8")) if proxy_runtime_path.exists() else []
    before_cm = load_cm(args.namespace, args.configmap)
    before_rows = cursor_key_rows(args.namespace)

    apply_config(args, cm)
    failures = []
    for row in rows:
        resp = update_key(base, master, row["token"], list(row.get("models") or []), row.get("aliases") or {})
        if resp["status"] != 200:
            failures.append({
                "key_alias": row.get("key_alias"),
                "http_status": resp["status"],
                "error": resp["text"][:500],
            })

    model_restore = restore_proxy_model_rows(args, base, master, proxy_runtime_rows)
    if model_restore.get("changed") and not args.no_restart:
        restart_proxy(args)

    after_cm = load_cm(args.namespace, args.configmap)
    after_rows = cursor_key_rows(args.namespace)
    return {
        "mode": "restore",
        "status": "PASS" if not failures and model_restore.get("status") in {"PASS", "SKIPPED"} else "FAIL",
        "restore_source": str(backup),
        "config_checksum_before": config_checksum(before_cm),
        "config_checksum_after": config_checksum(after_cm),
        "cursor_audit_before": cursor_audit(before_rows),
        "cursor_audit_after": cursor_audit(after_rows),
        "model_restore": model_restore,
        "key_update_failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="litellm-product")
    parser.add_argument("--configmap", default="litellm-config")
    parser.add_argument("--manifest", default="/root/litellm-product-manifests/30-cm-litellm-config.yaml")
    parser.add_argument("--backup-root", default="/root/litellm-product-manifests/backups")
    parser.add_argument("--nodeport", default="30402")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore")
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()

    if args.restore:
        result = restore(args)
        print(BEGIN_MARKER)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        print(END_MARKER)
        return 0 if result["status"] == "PASS" else 1

    base = f"http://localhost:{args.nodeport}"
    master = master_key(args.namespace)
    runtime_names, runtime_info = runtime_model_names(base, master)
    codex_rows, codex_rows_info = runtime_model_rows(base, master, {CODEX_COMPAT_GROUP})
    spark_rows, spark_rows_info = runtime_model_rows(base, master, {"chatgpt-gpt-5.3-codex-spark", "gpt-5.4-mini"})
    desired_codex_entries, codex_changes, codex_errors = codex_compat_plan(codex_rows)
    proxy_model_rows = db_proxy_model_rows(args.namespace, {CODEX_COMPAT_GROUP})
    cm = load_cm(args.namespace, args.configmap)
    current_config = load_config(cm)
    desired_config, router_changes, router_errors = desired_router_config(current_config, runtime_names)
    desired_cm = copy.deepcopy(cm)
    desired_cm["data"]["config.yaml"] = dump_config(desired_config)
    before_rows = cursor_key_rows(args.namespace)
    before_audit = cursor_audit(before_rows)
    desired_checksum = config_checksum(desired_cm)
    current_checksum = config_checksum(cm)
    config_changed = desired_checksum != current_checksum

    result: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "status": "PASS",
        "namespace": args.namespace,
        "configmap": args.configmap,
        "manifest": args.manifest,
        "nodeport": args.nodeport,
        "runtime_model_info": runtime_info,
        "codex_runtime_info": codex_rows_info,
        "codex_spark_runtime_info": spark_rows_info,
        "started_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "config_checksum_before": current_checksum,
        "config_checksum_desired": desired_checksum,
        "config_changed": config_changed,
        "router_changes": router_changes,
        "router_errors": router_errors,
        "model_compat": {
            "group": CODEX_COMPAT_GROUP,
            "target_litellm_model": CODEX_COMPAT_TARGET_MODEL,
            "api_key_strategy": "preserve_existing_db_value",
            "current": [model_row_summary(row) for row in codex_rows],
            "desired": [model_row_summary(row) for row in desired_codex_entries],
            "changes": codex_changes,
            "errors": codex_errors,
            "raw_db_rows_backed_up": len(proxy_model_rows),
        },
        "cursor_audit_before": before_audit,
        "backup_dir": "",
        "apply": {},
    }

    if router_errors or codex_errors:
        result["status"] = "FAIL"
    if not args.apply:
        result["status"] = "PASS" if not router_errors and not codex_errors else "FAIL"
        print(BEGIN_MARKER)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        print(END_MARKER)
        return 0 if result["status"] == "PASS" else 1

    if router_errors or codex_errors:
        print(BEGIN_MARKER)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        print(END_MARKER)
        return 1

    probe = api_alias_clear_probe(args, base, master)
    result["apply"]["api_alias_clear_probe"] = probe
    if probe.get("status") != "PASS" or probe.get("remaining") != 0:
        result["status"] = "FAIL"
        result["apply"]["reason"] = "API /key/update did not prove aliases can be cleared safely"
        print(BEGIN_MARKER)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        print(END_MARKER)
        return 1

    codex_probe = api_existing_spark_probe(base, master)
    result["apply"]["existing_spark_probe"] = codex_probe
    if codex_probe.get("status") != "PASS":
        result["status"] = "FAIL"
        result["apply"]["reason"] = "existing chatgpt-gpt-5.3-codex-spark product did not prove usable"
        print(BEGIN_MARKER)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        print(END_MARKER)
        return 1

    backup_dir = backup_state(args, cm, before_rows, proxy_model_rows, codex_rows)
    result["backup_dir"] = str(backup_dir)

    model_apply = apply_codex_compat(base, master, codex_rows, desired_codex_entries, codex_changes)
    result["apply"]["model_compat"] = model_apply
    if model_apply.get("status") != "PASS":
        result["status"] = "FAIL"
        print(BEGIN_MARKER)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        print(END_MARKER)
        return 1

    if config_changed:
        apply_config(args, desired_cm)
        result["apply"]["config"] = "applied"
    else:
        result["apply"]["config"] = "unchanged"
        if model_apply.get("changed") and not args.no_restart:
            restart_proxy(args)

    failures = apply_key_updates(args, before_rows, base, master)
    result["apply"]["key_update_failures"] = failures
    after_rows = cursor_key_rows(args.namespace)
    after_ok, after_audit = validate_cursor_rows(after_rows)
    after_cm = load_cm(args.namespace, args.configmap)
    after_codex_rows, _ = runtime_model_rows(base, master, {CODEX_COMPAT_GROUP})
    result["config_checksum_after"] = config_checksum(after_cm)
    result["cursor_audit_after"] = after_audit
    result["model_compat"]["after"] = [model_row_summary(row) for row in after_codex_rows]

    if failures or not after_ok:
        result["status"] = "FAIL"

    print(BEGIN_MARKER)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print(END_MARKER)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
"""


BEGIN_MARKER = "__LITELLM_PRO_GPT_CONVERGE_RESULT_BEGIN__"
END_MARKER = "__LITELLM_PRO_GPT_CONVERGE_RESULT_END__"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write pro config and update cursor keys")
    mode.add_argument("--restore", help="remote backup directory to restore")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--jms", default=None, help="path to jms wrapper")
    parser.add_argument("--namespace", default="litellm-product")
    parser.add_argument("--configmap", default="litellm-config")
    parser.add_argument("--manifest", default="/root/litellm-product-manifests/30-cm-litellm-config.yaml")
    parser.add_argument("--backup-root", default="/root/litellm-product-manifests/backups")
    parser.add_argument("--nodeport", default="30402")
    parser.add_argument("--no-restart", action="store_true")
    return parser.parse_args()


def extract_result(stdout: str) -> dict[str, Any]:
    if BEGIN_MARKER not in stdout or END_MARKER not in stdout:
        raise RuntimeError("remote output did not contain convergence result markers")
    start = stdout.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = stdout.index(END_MARKER, start)
    return json.loads(stdout[start:end].strip())


def write_reports(result: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(result.get("started_at") or dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z")
    stamp = stamp.replace(":", "").replace("-", "")
    slug = "litellm-pro-gpt-products-converge"
    json_path = reports_dir / f"{slug}-{stamp}.json"
    md_path = reports_dir / f"{slug}-{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return md_path, json_path


def render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# LiteLLM Pro GPT Products Convergence Report")
    lines.append("")
    lines.append(f"- Status: **{result.get('status')}**")
    lines.append(f"- Mode: `{result.get('mode')}`")
    lines.append(f"- Namespace: `{result.get('namespace')}`")
    lines.append(f"- Config checksum before: `{result.get('config_checksum_before', '')}`")
    lines.append(f"- Config checksum desired: `{result.get('config_checksum_desired', '')}`")
    if result.get("config_checksum_after"):
        lines.append(f"- Config checksum after: `{result.get('config_checksum_after')}`")
    if result.get("backup_dir"):
        lines.append(f"- Backup dir: `{result.get('backup_dir')}`")
    lines.append("")

    if result.get("router_errors"):
        lines.append("## Router Errors")
        lines.append("")
        for error in result["router_errors"]:
            lines.append(f"- `{error}`")
        lines.append("")

    lines.append("## Router Diff")
    lines.append("")
    changes = result.get("router_changes") or []
    if changes:
        for change in changes:
            lines.append(f"- {change}")
    else:
        lines.append("- No router changes.")
    lines.append("")

    compat = result.get("model_compat") or {}
    if compat:
        lines.append("## Codex Compatibility Deployment")
        lines.append("")
        lines.append(f"- Group: `{compat.get('group')}`")
        lines.append(f"- Target LiteLLM model: `{compat.get('target_litellm_model')}`")
        if compat.get("errors"):
            lines.append(f"- Errors: `{json.dumps(compat.get('errors'), ensure_ascii=False)}`")
        compat_changes = compat.get("changes") or []
        if compat_changes:
            for change in compat_changes:
                lines.append(f"- {change}")
        else:
            lines.append("- No codex deployment changes.")
        current = compat.get("current") or []
        desired = compat.get("desired") or []
        if current or desired:
            lines.append("")
            lines.append("| State | Model ID | LiteLLM Model | API Base |")
            lines.append("|---|---|---|---|")
            for item in current:
                lines.append(f"| current | `{item.get('model_id')}` | `{item.get('litellm_model')}` | `{item.get('api_base')}` |")
            for item in desired:
                lines.append(f"| desired | `{item.get('model_id')}` | `{item.get('litellm_model')}` | `{item.get('api_base')}` |")
        after = compat.get("after") or []
        if after:
            lines.append("")
            lines.append("| After Model ID | After LiteLLM Model | After API Base |")
            lines.append("|---|---|---|")
            for item in after:
                lines.append(f"| `{item.get('model_id')}` | `{item.get('litellm_model')}` | `{item.get('api_base')}` |")
        lines.append("")

    for title, key in (("Cursor Key Audit Before", "cursor_audit_before"), ("Cursor Key Audit After", "cursor_audit_after")):
        audit = result.get(key) or {}
        if not audit:
            continue
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"- Status: **{audit.get('status')}**")
        lines.append(f"- Cursor key count: `{audit.get('cursor_key_count')}`")
        lines.append(f"- Exact 9 products: `{audit.get('keys_exactly_9_products')}`")
        lines.append(f"- Keys with extra models: `{audit.get('keys_with_extra_models')}`")
        lines.append(f"- Keys with hidden models: `{audit.get('keys_with_hidden_models')}`")
        lines.append(f"- Keys with aliases: `{audit.get('keys_with_aliases')}`")
        lines.append(f"- Keys with direct internal aliases: `{audit.get('keys_with_direct_internal_aliases')}`")
        examples = audit.get("examples") or []
        if examples:
            lines.append("")
            lines.append("| Key Alias | Extra Models | Missing Products | Aliases |")
            lines.append("|---|---|---|---|")
            for item in examples[:20]:
                lines.append(
                    f"| `{item.get('key_alias')}` | `{', '.join(item.get('extra_models') or []) or 'none'}` | "
                    f"`{', '.join(item.get('missing_products') or []) or 'none'}` | "
                    f"`{json.dumps(item.get('aliases') or {}, ensure_ascii=False)}` |"
                )
        lines.append("")

    apply_info = result.get("apply") or {}
    if apply_info:
        lines.append("## Apply")
        lines.append("")
        if apply_info.get("reason"):
            lines.append(f"- Reason: `{apply_info.get('reason')}`")
        if apply_info.get("config"):
            lines.append(f"- Config: `{apply_info.get('config')}`")
        probe = apply_info.get("api_alias_clear_probe") or {}
        if probe:
            lines.append(f"- API alias clear probe: `{probe.get('status')}`")
        codex_probe = apply_info.get("existing_spark_probe") or apply_info.get("codex_env_probe") or {}
        if codex_probe:
            lines.append(f"- Existing spark probe: `{codex_probe.get('status')}`")
        model_apply = apply_info.get("model_compat") or {}
        if model_apply:
            lines.append(f"- Codex deployment apply: `{model_apply.get('status')}`, changed=`{model_apply.get('changed')}`")
        failures = apply_info.get("key_update_failures") or []
        lines.append(f"- Key update failures: `{len(failures)}`")
        for failure in failures[:20]:
            lines.append(f"- `{failure.get('key_alias')}` HTTP `{failure.get('http_status')}`: {failure.get('error')}")
        lines.append("")
    return "\n".join(lines)


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
        "--backup-root",
        args.backup_root,
        "--nodeport",
        args.nodeport,
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
        remote_path = "/tmp/_litellm_pro_gpt_products_converge.py"
        subprocess.check_call([jms, "scp", local_path, f"AIYJY-litellm:{remote_path}"])
        command = "python3 " + shlex.quote(remote_path) + " " + shlex.join(remote_args)
        proc = subprocess.run(
            [jms, "ssh", "AIYJY-litellm", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        print(proc.stdout, end="")
        try:
            result = extract_result(proc.stdout)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return proc.returncode or 1
        md_path, json_path = write_reports(result, Path(args.reports_dir))
        print(f"report markdown -> {md_path}")
        print(f"report json     -> {json_path}")
        print(f"status          -> {result.get('status')}")
        return proc.returncode
    finally:
        os.unlink(local_path)


if __name__ == "__main__":
    sys.exit(main())
