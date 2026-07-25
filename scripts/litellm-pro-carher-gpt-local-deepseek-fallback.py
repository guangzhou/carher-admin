#!/usr/bin/env python3
"""
Converge pro LiteLLM GPT product fallbacks to local DeepSeek for carher keys.

Default mode is dry-run. The remote work runs on AIYJY-litellm through
scripts/jms and writes local reports under reports/.
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

MANAGED_FALLBACK_SOURCES = [
    "gpt-5.5",
    "chatgpt-gpt-5.5",
    "chatgpt-pool-gpt-5.5",
    "gpt-5.4",
    "chatgpt-gpt-5.4",
    "gpt-5.2",
    "gpt-5.3-codex",
    "chatgpt-gpt-5.3-codex",
    "gpt-5.4-mini",
    "chatgpt-gpt-5.3-codex-spark",
]

BEGIN_MARKER = "__LITELLM_PRO_CARHER_GPT_LOCAL_DEEPSEEK_BEGIN__"
END_MARKER = "__LITELLM_PRO_CARHER_GPT_LOCAL_DEEPSEEK_END__"


def sh(args: list[str], *, input_text: str | None = None) -> str:
    return subprocess.check_output(args, input=input_text, text=True).strip()


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


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
            return {"status": resp.status, "headers": dict(resp.headers), "text": text, "json": parse_json(text)}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        return {"status": exc.code, "headers": dict(exc.headers), "text": text, "json": parse_json(text)}
    except Exception as exc:
        return {"status": 0, "headers": {}, "text": str(exc), "json": None}


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


def master_key(namespace: str, secret_name: str) -> str:
    raw = sh([
        "kubectl",
        "get",
        "secret",
        secret_name,
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


def config_checksum_from_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def config_checksum(cm: dict) -> str:
    return config_checksum_from_text(cm["data"]["config.yaml"])


def fallback_map(fallbacks: list[dict]) -> dict[str, list[str]]:
    mapped: dict[str, list[str]] = {}
    for item in fallbacks or []:
        if isinstance(item, dict):
            for source, targets in item.items():
                mapped[source] = list(targets or [])
    return mapped


def fallback_list(mapped: dict[str, list[str]]) -> list[dict]:
    return [{source: targets} for source, targets in mapped.items()]


def model_names(data: dict) -> set[str]:
    return {entry.get("model_name") for entry in data.get("model_list", []) if entry.get("model_name")}


def db_router_settings(namespace: str) -> dict:
    sql = 'SELECT COALESCE(param_value::text, \'\') FROM "LiteLLM_Config" WHERE param_name = \'router_settings\' LIMIT 1;'
    raw = psql(namespace, sql)
    return json.loads(raw) if raw else {}


def write_db_router_settings(namespace: str, router: dict) -> None:
    value = json.dumps(router, ensure_ascii=False, separators=(",", ":"))
    sql = (
        'UPDATE "LiteLLM_Config" '
        f"SET param_value = {sql_literal(value)}::jsonb "
        "WHERE param_name = 'router_settings';"
    )
    psql(namespace, sql)


def runtime_model_info(base: str, master: str) -> tuple[dict[str, dict], dict]:
    resp = http_json("GET", f"{base}/model/info", master, timeout=60)
    rows: dict[str, dict] = {}
    if resp["status"] == 200 and isinstance(resp["json"], dict):
        for item in resp["json"].get("data") or []:
            if isinstance(item, dict) and item.get("model_name"):
                rows[item["model_name"]] = item
    return rows, {"status": resp["status"], "count": len(rows), "error": resp["text"][:500] if resp["status"] != 200 else ""}


def sanitized_model_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    copied = copy.deepcopy(row)
    params = copied.get("litellm_params") or {}
    if "api_key" in params:
        params["api_key"] = "<redacted>"
    return copied


def desired_router(router: dict, fallback_target: str) -> tuple[dict, list[str]]:
    desired = copy.deepcopy(router or {})
    fallbacks = fallback_map(desired.get("fallbacks") or [])
    changes: list[str] = []
    for source in MANAGED_FALLBACK_SOURCES:
        old = fallbacks.get(source)
        new = [fallback_target]
        if old != new:
            changes.append(f"fallback {source}: {old or '<missing>'} -> {new}")
        fallbacks[source] = new
    desired["fallbacks"] = fallback_list(fallbacks)
    return desired, changes


def carher_key_rows(namespace: str) -> list[dict]:
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
  WHERE key_alias LIKE 'carher-%'
) t;
'''
    return psql_json(namespace, sql) or []


def carher_key_diff(row: dict) -> dict:
    models = list(row.get("models") or [])
    aliases = row.get("aliases") or {}
    missing = sorted(set(PRODUCT_MODELS) - set(models))
    return {
        "key_alias": row.get("key_alias"),
        "models_count": len(models),
        "missing_products": missing,
        "aliases": aliases,
        "blocked": row.get("blocked"),
        "needs_model_update": bool(missing),
    }


def carher_key_audit(rows: list[dict]) -> dict:
    diffs = [carher_key_diff(row) for row in rows]
    needing = [item for item in diffs if item["needs_model_update"]]
    alias_rows = [item for item in diffs if item["aliases"]]
    return {
        "carher_key_count": len(rows),
        "expected_product_models": PRODUCT_MODELS,
        "keys_with_all_products": sum(1 for item in diffs if not item["missing_products"]),
        "keys_missing_products": len(needing),
        "keys_with_aliases": len(alias_rows),
        "keys_blocked": sum(1 for item in diffs if item["blocked"]),
        "examples_missing": needing[:20],
        "examples_aliases": alias_rows[:20],
        "status": "PASS" if not needing else "FAIL",
    }


def update_key_models(base: str, master: str, token: str, models: list[str], aliases: dict) -> dict:
    return http_json("POST", f"{base}/key/update", master, {"key": token, "models": models, "aliases": aliases}, timeout=45)


def delete_key(base: str, master: str, alias: str) -> None:
    http_json("POST", f"{base}/key/delete", master, {"key_aliases": [alias]}, timeout=45)


def api_key_update_probe(args: argparse.Namespace, base: str, master: str) -> dict:
    alias = "codex-carher-gpt-fallback-probe-" + dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    body = {
        "key_alias": alias,
        "models": ["gpt-5.5"],
        "aliases": {"gpt-5.5": "chatgpt-gpt-5.5"},
        "max_budget": 1,
        "budget_duration": "1d",
        "metadata": {"purpose": "carher-gpt-local-deepseek-fallback-probe"},
    }
    created = http_json("POST", f"{base}/key/generate", master, body, timeout=45)
    result = {"alias": alias, "create_status": created["status"], "update_status": None, "remaining": None, "status": "FAIL"}
    try:
        if created["status"] != 200:
            result["error"] = created["text"][:500]
            return result
        token = psql(args.namespace, f'SELECT token FROM "LiteLLM_VerificationToken" WHERE key_alias={sql_literal(alias)} LIMIT 1;').strip()
        updated = update_key_models(base, master, token, PRODUCT_MODELS, {"gpt-5.5": "chatgpt-gpt-5.5"})
        result["update_status"] = updated["status"]
        row = psql_json(
            args.namespace,
            'SELECT row_to_json(t) FROM ('
            "SELECT COALESCE(models, ARRAY[]::text[]) AS models, COALESCE(aliases, '{}'::jsonb) AS aliases "
            f'FROM "LiteLLM_VerificationToken" WHERE key_alias={sql_literal(alias)} LIMIT 1'
            ") t;",
        )
        result["row_after_update"] = row
        result["status"] = "PASS" if updated["status"] == 200 and row and set(row.get("models") or []) == set(PRODUCT_MODELS) else "FAIL"
        return result
    finally:
        delete_key(base, master, alias)
        try:
            remaining = psql(args.namespace, f'SELECT COUNT(*)::int FROM "LiteLLM_VerificationToken" WHERE key_alias={sql_literal(alias)};').strip()
            result["remaining"] = int(remaining or "0")
        except Exception as exc:
            result["remaining_error"] = str(exc)


def request_probe(base: str, key: str, model: str, marker: str, *, mock_fallback: bool = False) -> dict:
    body = {
        "model": model,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": f"Reply OK only. {marker}"}]}],
        "max_output_tokens": 16,
        "stream": True,
        "store": False,
        "metadata": {"carher_gpt_local_deepseek_probe": marker},
    }
    if mock_fallback:
        body["mock_testing_fallbacks"] = True
    resp = http_json("POST", f"{base}/v1/responses", key, body, timeout=120)
    headers = {str(k).lower(): v for k, v in (resp.get("headers") or {}).items()}
    return {
        "http_status": resp["status"],
        "headers": headers,
        "text_prefix": resp["text"][:500] if resp["status"] != 200 else "",
    }


def spendlog_for_marker(namespace: str, marker: str) -> dict | None:
    like = "%" + marker + "%"
    sql = f'''
SELECT row_to_json(t)
FROM (
  SELECT
    request_id,
    call_type,
    model,
    api_base,
    metadata->>'model_group' AS model_group,
    metadata->>'deployment' AS deployment,
    metadata->>'user_api_key_alias' AS key_alias,
    metadata->>'status' AS status,
    "startTime"::text AS start_time
  FROM "LiteLLM_SpendLogs"
  WHERE metadata::text LIKE {sql_literal(like)}
     OR request_tags::text LIKE {sql_literal(like)}
  ORDER BY "startTime" DESC
  LIMIT 1
) t;
'''
    try:
        return psql_json(namespace, sql)
    except Exception:
        return None


def backup_state(args: argparse.Namespace, cm: dict, db_router: dict, key_rows: list[dict]) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(args.backup_root) / f"carher-gpt-local-deepseek-fallback-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    (backup_dir / "configmap.json").write_text(json.dumps(cm, indent=2, ensure_ascii=False), encoding="utf-8")
    (backup_dir / "config.yaml").write_text(cm["data"]["config.yaml"], encoding="utf-8")
    (backup_dir / "db-router-settings.json").write_text(json.dumps(db_router, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    (backup_dir / "carher-keys.json").write_text(json.dumps(key_rows, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    manifest = Path(args.manifest)
    if manifest.exists():
        (backup_dir / "manifest.raw").write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    return backup_dir


def restart_proxy(args: argparse.Namespace) -> None:
    if args.no_restart:
        return
    sh(["kubectl", "rollout", "restart", "deployment/litellm-proxy", "-n", args.namespace])
    sh(["kubectl", "rollout", "status", "deployment/litellm-proxy", "-n", args.namespace, "--timeout=300s"])


def apply_cm(args: argparse.Namespace, desired_cm: dict) -> None:
    manifest = Path(args.manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(yaml.safe_dump(desired_cm, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")
    sh(["kubectl", "apply", "-f", str(manifest), "-n", args.namespace])


def fallback_summary(router: dict, fallback_target: str) -> dict:
    fallbacks = fallback_map((router or {}).get("fallbacks") or [])
    return {
        "expected_target": fallback_target,
        "managed": {source: fallbacks.get(source) for source in MANAGED_FALLBACK_SOURCES},
        "all_managed_match": all(fallbacks.get(source) == [fallback_target] for source in MANAGED_FALLBACK_SOURCES),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="litellm-product")
    parser.add_argument("--configmap", default="litellm-config")
    parser.add_argument("--manifest", default="/root/litellm-product-manifests/30-cm-litellm-config.yaml")
    parser.add_argument("--backup-root", default="/root/litellm-product-manifests/backups")
    parser.add_argument("--nodeport", default="30402")
    parser.add_argument("--master-secret", default="litellm-secrets")
    parser.add_argument("--fallback-target", default="local-deepseek-v4-flash-responses")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-key-updates", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()

    base = f"http://localhost:{args.nodeport}"
    master = master_key(args.namespace, args.master_secret)
    cm = load_cm(args.namespace, args.configmap)
    cfg = load_config(cm)
    db_router = db_router_settings(args.namespace)
    runtime_rows, runtime_info = runtime_model_info(base, master)
    key_rows = carher_key_rows(args.namespace)
    key_audit_before = carher_key_audit(key_rows)

    target_row = runtime_rows.get(args.fallback_target)
    errors: list[str] = []
    if args.fallback_target not in model_names(cfg):
        errors.append(f"fallback target missing from ConfigMap model_list: {args.fallback_target}")
    if target_row is None:
        errors.append(f"fallback target missing from /model/info: {args.fallback_target}")
    elif ((target_row.get("model_info") or {}).get("mode") or "") != "responses":
        errors.append(f"fallback target is not responses mode in /model/info: {args.fallback_target}")

    desired_cm_cfg = copy.deepcopy(cfg)
    desired_cm_router, cm_changes = desired_router(desired_cm_cfg.get("router_settings") or {}, args.fallback_target)
    desired_cm_cfg["router_settings"] = desired_cm_router
    desired_cm = copy.deepcopy(cm)
    desired_cm["data"]["config.yaml"] = dump_config(desired_cm_cfg)
    desired_db_router, db_changes = desired_router(db_router, args.fallback_target)
    cm_changed = config_checksum(desired_cm) != config_checksum(cm)
    db_changed = json.dumps(desired_db_router, sort_keys=True, ensure_ascii=False) != json.dumps(db_router, sort_keys=True, ensure_ascii=False)

    result: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "status": "PASS",
        "started_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "namespace": args.namespace,
        "configmap": args.configmap,
        "fallback_target": args.fallback_target,
        "runtime_model_info": runtime_info,
        "fallback_target_runtime_row": sanitized_model_row(target_row),
        "errors": errors,
        "config_checksum_before": config_checksum(cm),
        "config_checksum_desired": config_checksum(desired_cm),
        "config_changed": cm_changed,
        "db_router_changed": db_changed,
        "configmap_fallback_before": fallback_summary(cfg.get("router_settings") or {}, args.fallback_target),
        "configmap_fallback_desired": fallback_summary(desired_cm_router, args.fallback_target),
        "db_fallback_before": fallback_summary(db_router, args.fallback_target),
        "db_fallback_desired": fallback_summary(desired_db_router, args.fallback_target),
        "configmap_changes": cm_changes,
        "db_router_changes": db_changes,
        "carher_key_audit_before": key_audit_before,
        "backup_dir": "",
        "apply": {},
    }

    if errors:
        result["status"] = "FAIL"
    if not args.apply:
        print(BEGIN_MARKER)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        print(END_MARKER)
        return 0 if result["status"] == "PASS" else 1
    if errors:
        print(BEGIN_MARKER)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        print(END_MARKER)
        return 1

    probe = api_key_update_probe(args, base, master)
    result["apply"]["key_update_probe"] = probe
    if probe.get("status") != "PASS" or probe.get("remaining") != 0:
        result["status"] = "FAIL"
        result["apply"]["reason"] = "key update probe failed"
        print(BEGIN_MARKER)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        print(END_MARKER)
        return 1

    backup_dir = backup_state(args, cm, db_router, key_rows)
    result["backup_dir"] = str(backup_dir)

    if cm_changed:
        apply_cm(args, desired_cm)
        result["apply"]["configmap"] = "applied"
    else:
        result["apply"]["configmap"] = "unchanged"
    if db_changed:
        write_db_router_settings(args.namespace, desired_db_router)
        result["apply"]["db_router"] = "applied"
    else:
        result["apply"]["db_router"] = "unchanged"

    key_failures = []
    if not args.skip_key_updates:
        for row in key_rows:
            diff = carher_key_diff(row)
            if not diff["needs_model_update"]:
                continue
            desired_models = sorted(set(row.get("models") or []) | set(PRODUCT_MODELS))
            resp = update_key_models(base, master, row["token"], desired_models, row.get("aliases") or {})
            if resp["status"] != 200:
                key_failures.append({"key_alias": row.get("key_alias"), "http_status": resp["status"], "error": resp["text"][:500]})
    result["apply"]["key_update_failures"] = key_failures

    if cm_changed or db_changed:
        restart_proxy(args)
    time.sleep(5)

    after_cm = load_cm(args.namespace, args.configmap)
    after_cfg = load_config(after_cm)
    after_db_router = db_router_settings(args.namespace)
    after_rows = carher_key_rows(args.namespace)
    after_audit = carher_key_audit(after_rows)
    result["config_checksum_after"] = config_checksum(after_cm)
    result["configmap_fallback_after"] = fallback_summary(after_cfg.get("router_settings") or {}, args.fallback_target)
    result["db_fallback_after"] = fallback_summary(after_db_router, args.fallback_target)
    result["carher_key_audit_after"] = after_audit

    marker = "carher-gpt-local-deepseek-post-" + dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    normal = request_probe(base, master, "gpt-5.5", marker, mock_fallback=False)
    result["apply"]["normal_probe"] = normal
    time.sleep(2)
    result["apply"]["normal_probe_spendlog"] = spendlog_for_marker(args.namespace, marker)

    fallback_marker = "carher-gpt-local-deepseek-mock-" + dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    mock = request_probe(base, master, "gpt-5.5", fallback_marker, mock_fallback=True)
    result["apply"]["mock_fallback_probe"] = mock
    time.sleep(2)
    result["apply"]["mock_fallback_probe_spendlog"] = spendlog_for_marker(args.namespace, fallback_marker)
    attempted = str((mock.get("headers") or {}).get("x-litellm-attempted-fallbacks") or "0")
    spendlog = result["apply"]["mock_fallback_probe_spendlog"] or {}
    result["fallback_execution_status"] = (
        "PASS"
        if mock.get("http_status") == 200
        and attempted not in {"", "0"}
        and args.fallback_target in json.dumps(spendlog, ensure_ascii=False)
        else "BLOCKED"
    )

    if key_failures or after_audit.get("status") != "PASS":
        result["status"] = "FAIL"
    if not result["configmap_fallback_after"]["all_managed_match"] or not result["db_fallback_after"]["all_managed_match"]:
        result["status"] = "FAIL"

    print(BEGIN_MARKER)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print(END_MARKER)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
"""


BEGIN_MARKER = "__LITELLM_PRO_CARHER_GPT_LOCAL_DEEPSEEK_BEGIN__"
END_MARKER = "__LITELLM_PRO_CARHER_GPT_LOCAL_DEEPSEEK_END__"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--jms", default=None)
    parser.add_argument("--namespace", default="litellm-product")
    parser.add_argument("--configmap", default="litellm-config")
    parser.add_argument("--manifest", default="/root/litellm-product-manifests/30-cm-litellm-config.yaml")
    parser.add_argument("--backup-root", default="/root/litellm-product-manifests/backups")
    parser.add_argument("--nodeport", default="30402")
    parser.add_argument("--master-secret", default="litellm-secrets")
    parser.add_argument("--fallback-target", default="local-deepseek-v4-flash-responses")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-key-updates", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    return parser.parse_args()


def extract_result(stdout: str) -> dict[str, Any]:
    if BEGIN_MARKER not in stdout or END_MARKER not in stdout:
        raise RuntimeError("remote output did not contain result markers")
    start = stdout.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = stdout.index(END_MARKER, start)
    return json.loads(stdout[start:end].strip())


def write_reports(result: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(result.get("started_at") or dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z")
    stamp = stamp.replace(":", "").replace("-", "")
    slug = "litellm-pro-carher-gpt-local-deepseek-fallback"
    json_path = reports_dir / f"{slug}-{stamp}.json"
    md_path = reports_dir / f"{slug}-{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return md_path, json_path


def render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# LiteLLM Pro Carher GPT Local DeepSeek Fallback Report")
    lines.append("")
    lines.append(f"- Status: **{result.get('status')}**")
    lines.append(f"- Mode: `{result.get('mode')}`")
    lines.append(f"- Namespace: `{result.get('namespace')}`")
    lines.append(f"- ConfigMap: `{result.get('configmap')}`")
    lines.append(f"- Fallback target: `{result.get('fallback_target')}`")
    lines.append(f"- Config checksum before: `{result.get('config_checksum_before', '')}`")
    lines.append(f"- Config checksum desired: `{result.get('config_checksum_desired', '')}`")
    if result.get("config_checksum_after"):
        lines.append(f"- Config checksum after: `{result.get('config_checksum_after')}`")
    if result.get("backup_dir"):
        lines.append(f"- Backup dir: `{result.get('backup_dir')}`")
    if result.get("fallback_execution_status"):
        lines.append(f"- Fallback execution status: `{result.get('fallback_execution_status')}`")
    lines.append("")

    errors = result.get("errors") or []
    if errors:
        lines.append("## Errors")
        lines.append("")
        for error in errors:
            lines.append(f"- `{error}`")
        lines.append("")

    lines.append("## Router Diff")
    lines.append("")
    for title, key in (("ConfigMap", "configmap_changes"), ("DB router_settings", "db_router_changes")):
        changes = result.get(key) or []
        lines.append(f"### {title}")
        lines.append("")
        if changes:
            for change in changes:
                lines.append(f"- `{change}`")
        else:
            lines.append("- No changes.")
        lines.append("")

    lines.append("## Carher Key Audit")
    lines.append("")
    for title, key in (("Before", "carher_key_audit_before"), ("After", "carher_key_audit_after")):
        audit = result.get(key) or {}
        if not audit:
            continue
        lines.append(f"### {title}")
        lines.append("")
        lines.append(f"- Status: `{audit.get('status')}`")
        lines.append(f"- Carher key count: `{audit.get('carher_key_count')}`")
        lines.append(f"- Keys with all GPT products: `{audit.get('keys_with_all_products')}`")
        lines.append(f"- Keys missing GPT products: `{audit.get('keys_missing_products')}`")
        lines.append(f"- Keys with aliases: `{audit.get('keys_with_aliases')}`")
        lines.append("")

    lines.append("## Apply")
    lines.append("")
    apply = result.get("apply") or {}
    if apply:
        lines.append(f"- ConfigMap: `{apply.get('configmap', 'n/a')}`")
        lines.append(f"- DB router: `{apply.get('db_router', 'n/a')}`")
        failures = apply.get("key_update_failures") or []
        lines.append(f"- Key update failures: `{len(failures)}`")
        normal = apply.get("normal_probe") or {}
        if normal:
            lines.append(f"- Normal probe HTTP: `{normal.get('http_status')}`")
        mock = apply.get("mock_fallback_probe") or {}
        if mock:
            headers = mock.get("headers") or {}
            lines.append(f"- Mock fallback probe HTTP: `{mock.get('http_status')}`")
            lines.append(f"- Mock attempted fallbacks: `{headers.get('x-litellm-attempted-fallbacks', '0')}`")
    else:
        lines.append("- Not applied.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    repo = Path(__file__).resolve().parents[1]
    jms = args.jms or str(repo / "scripts" / "jms")
    remote_path = f"/tmp/litellm-pro-carher-gpt-local-deepseek-fallback-{os.getpid()}.py"
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
        "--master-secret",
        args.master_secret,
        "--fallback-target",
        args.fallback_target,
    ]
    if args.apply:
        remote_args.append("--apply")
    if args.skip_key_updates:
        remote_args.append("--skip-key-updates")
    if args.no_restart:
        remote_args.append("--no-restart")

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
        f.write(REMOTE_SCRIPT)
        local_path = f.name
    try:
        subprocess.check_call([jms, "scp", local_path, f"AIYJY-litellm:{remote_path}"])
        cmd = " ".join(["python3", shlex.quote(remote_path), *[shlex.quote(x) for x in remote_args]])
        proc = subprocess.run([jms, "ssh", "AIYJY-litellm", cmd], text=True, capture_output=True)
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, file=sys.stderr, end="")
        result = extract_result(proc.stdout)
        md_path, json_path = write_reports(result, Path(args.reports_dir))
        print(f"\nWrote reports:\n- {md_path}\n- {json_path}")
        return proc.returncode
    finally:
        try:
            subprocess.run([jms, "ssh", "AIYJY-litellm", f"rm -f {shlex.quote(remote_path)}"], check=False)
        finally:
            os.unlink(local_path)


if __name__ == "__main__":
    sys.exit(main())
