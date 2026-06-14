#!/usr/bin/env python3
"""
Verify 198 LiteLLM dev/pro GPT product model visibility, primary routing, and fallbacks.

The remote checks run on AIYJY-litellm through scripts/jms. Reports are written
locally under reports/.
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
import time
from pathlib import Path
from typing import Any


REMOTE_SCRIPT = r"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

import yaml


PRODUCTS = [
    {"model": "gpt-5.5", "primary": "chatgpt-pool-gpt-5.5", "fallback": "wangsu-gpt-5.5"},
    {"model": "chatgpt-gpt-5.5", "primary": "chatgpt-pool-gpt-5.5", "fallback": "wangsu-gpt-5.5"},
    {"model": "gpt-5.4", "primary": "chatgpt-gpt-5.4", "fallback": "wangsu-gpt-5.4"},
    {"model": "chatgpt-gpt-5.4", "primary": "chatgpt-gpt-5.4", "fallback": "wangsu-gpt-5.4"},
    {"model": "gpt-5.2", "primary": "chatgpt-gpt-5.4", "fallback": "wangsu-gpt-5.4"},
    {
        "model": "gpt-5.3-codex",
        "primary": "chatgpt-gpt-5.3-codex",
        "primary_contains": "gpt-5.3-codex-spark",
        "fallback": "wangsu7-gpt-5.3-codex",
    },
    {
        "model": "chatgpt-gpt-5.3-codex",
        "primary": "chatgpt-gpt-5.3-codex",
        "primary_contains": "gpt-5.3-codex-spark",
        "fallback": "wangsu7-gpt-5.3-codex",
    },
    {"model": "gpt-5.4-mini", "primary": "chatgpt-gpt-5.3-codex-spark", "fallback": "wangsu-gpt-5.4"},
    {"model": "chatgpt-gpt-5.3-codex-spark", "primary": "chatgpt-gpt-5.3-codex-spark", "fallback": "wangsu-gpt-5.4"},
]

INTERNAL_MODELS = [
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
    "glm-5",
    "glm-5.1",
    "wangsu-glm-5.1",
    "gemini-3.1-pro-preview",
    "wangsu-gemini-3.1-pro-preview",
    "minimax-m2.7",
]

PRODUCT_MODELS = [p["model"] for p in PRODUCTS]
RELATED_MODEL_NAMES = sorted(set(PRODUCT_MODELS + INTERNAL_MODELS + [p["primary"] for p in PRODUCTS if p.get("primary")]))
BEGIN_MARKER = "__LITELLM_DEV_GPT_VERIFY_RESULT_BEGIN__"
END_MARKER = "__LITELLM_DEV_GPT_VERIFY_RESULT_END__"

PROFILE_DEFAULTS = {
    "dev": {
        "namespace": "litellm-dev",
        "configmap": "litellm-config",
        "nodeport": "30400",
        "base_prefix": "/dev",
        "alias_prefix": "codex-dev-gpt-products",
        "report_slug": "litellm-dev-gpt-products",
    },
    "pro": {
        "namespace": "litellm-product",
        "configmap": "litellm-config",
        "nodeport": "30402",
        "base_prefix": "",
        "alias_prefix": "codex-pro-gpt-products-regress",
        "report_slug": "litellm-pro-gpt-products",
    },
}


def sh(args: list[str], *, input_text: str | None = None) -> str:
    return subprocess.check_output(args, input=input_text, text=True).strip()


def http_json(method: str, url: str, key: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode(errors="replace")
            return {
                "status": resp.status,
                "headers": {k.lower(): v for k, v in resp.headers.items()},
                "text": text,
                "json": parse_json(text),
            }
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        return {
            "status": exc.code,
            "headers": {k.lower(): v for k, v in exc.headers.items()},
            "text": text,
            "json": parse_json(text),
        }
    except Exception as exc:
        return {"status": 0, "headers": {}, "text": str(exc), "json": None}


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def psql_json(namespace: str, sql: str) -> Any:
    out = sh([
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
    if not out:
        return None
    return json.loads(out)


def psql_scalar(namespace: str, sql: str) -> str:
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


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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


def config_checksum(namespace: str, configmap: str) -> str:
    raw = sh(["kubectl", "get", "cm", "-n", namespace, configmap, "-o", "json"])
    cm = json.loads(raw)
    return hashlib.sha256(cm["data"]["config.yaml"].encode()).hexdigest()


def config_metadata(namespace: str, configmap: str) -> dict:
    raw = sh(["kubectl", "get", "cm", "-n", namespace, configmap, "-o", "json"])
    cm = json.loads(raw)
    config_text = cm["data"]["config.yaml"]
    metadata = {
        "checksum": hashlib.sha256(config_text.encode()).hexdigest(),
        "general_store_model_in_db": None,
        "litellm_store_model_in_db": None,
    }
    try:
        config = yaml.safe_load(config_text) or {}
        metadata["general_store_model_in_db"] = (config.get("general_settings") or {}).get("store_model_in_db")
        metadata["litellm_store_model_in_db"] = (config.get("litellm_settings") or {}).get("store_model_in_db")
    except Exception as exc:
        metadata["parse_error"] = str(exc)
    return metadata


def db_router_hash(namespace: str) -> dict:
    try:
        raw = psql_scalar(
            namespace,
            'SELECT COALESCE(param_value::text, \'\') FROM "LiteLLM_Config" '
            "WHERE param_name = 'router_settings' LIMIT 1;",
        )
    except Exception as exc:
        return {"hash": "", "error": str(exc)}
    return {"hash": hashlib.sha256(raw.encode()).hexdigest() if raw else "", "bytes": len(raw)}


def pods(namespace: str) -> list[str]:
    raw = sh(["kubectl", "get", "pods", "-n", namespace, "-o", "json"])
    data = json.loads(raw)
    return [
        f"{item['metadata']['name']}:{item['status'].get('phase')}:{','.join(c.get('ready') and 'ready' or 'not-ready' for c in item['status'].get('containerStatuses', []))}"
        for item in data.get("items", [])
    ]


def get_model_info(base: str, key: str) -> tuple[list[dict], dict]:
    resp = http_json("GET", f"{base}/model/info", key)
    data = []
    if resp["status"] == 200 and isinstance(resp["json"], dict):
        data = resp["json"].get("data") or []
    return data, resp


def related_model_info(model_info: list[dict]) -> list[dict]:
    rows = []
    for item in model_info:
        name = item.get("model_name")
        if name not in RELATED_MODEL_NAMES:
            continue
        params = item.get("litellm_params") or {}
        info = item.get("model_info") or {}
        rows.append({
            "model_name": name,
            "litellm_model": params.get("model"),
            "api_base": params.get("api_base"),
            "model_id": info.get("id"),
            "mode": info.get("mode"),
        })
    return rows


def upstream_connectivity(model_info: list[dict]) -> list[dict]:
    checks = []
    seen = set()
    for row in model_info:
        name = row.get("model_name") or ""
        params = row.get("litellm_params") or {}
        api_base = params.get("api_base") or ""
        if not name.startswith("chatgpt") and "chatgpt" not in name:
            continue
        if not api_base.startswith("http://"):
            continue
        parsed = urllib.parse.urlparse(api_base)
        port = parsed.port or 80
        key = (parsed.hostname, port)
        if not parsed.hostname or key in seen:
            continue
        seen.add(key)
        try:
            with socket.create_connection((parsed.hostname, port), timeout=3):
                status = "open"
                error = ""
        except Exception as exc:
            status = "closed"
            error = str(exc)
        checks.append({
            "api_base": api_base,
            "host": parsed.hostname,
            "port": port,
            "status": status,
            "error": error,
        })
    return checks


def create_key(base: str, key: str, alias: str, profile: str) -> tuple[str, dict]:
    body = {
        "key_alias": alias,
        "user_id": f"codex-{profile}-gpt-products-verify",
        "models": PRODUCT_MODELS,
        "max_budget": 5,
        "budget_duration": "1d",
        "metadata": {"purpose": f"{profile}-gpt-products-verify"},
    }
    resp = http_json("POST", f"{base}/key/generate", key, body)
    if resp["status"] != 200 or not isinstance(resp["json"], dict) or "key" not in resp["json"]:
        raise RuntimeError(f"key/generate failed HTTP {resp['status']}: {resp['text'][:500]}")
    return resp["json"]["key"], resp


def token_hash(namespace: str, alias: str) -> str:
    sql = (
        'SELECT COALESCE(token, \'\') FROM "LiteLLM_VerificationToken" '
        f"WHERE key_alias = {sql_literal(alias)} LIMIT 1;"
    )
    return psql_scalar(namespace, sql).strip()


def delete_key(base: str, key: str, alias: str) -> dict:
    return http_json("POST", f"{base}/key/delete", key, {"key_aliases": [alias]})


def key_count(namespace: str, alias: str) -> int:
    sql = (
        'SELECT COUNT(*)::int FROM "LiteLLM_VerificationToken" '
        f"WHERE key_alias = {sql_literal(alias)};"
    )
    out = psql_scalar(namespace, sql).strip()
    return int(out or "0")


def response_model(resp: dict) -> str | None:
    data = resp.get("json")
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("model"), str):
        return data["model"]
    if isinstance(data.get("response"), dict) and isinstance(data["response"].get("model"), str):
        return data["response"]["model"]
    return None


def error_summary(resp: dict) -> str:
    data = resp.get("json")
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        err = data["error"]
        parts = [str(err.get(k)) for k in ("type", "code", "message") if err.get(k)]
        return " | ".join(parts)[:500]
    return str(resp.get("text", ""))[:500]


def request_payload(endpoint: str, model: str, case_id: str, *, mock: bool) -> dict:
    prompt = f"Reply OK only. case_id={case_id}"
    metadata = {"dev_gpt_verify_case": case_id}
    if endpoint == "responses":
        body = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            "max_output_tokens": 16,
            "stream": True,
            "store": False,
            "metadata": metadata,
        }
    else:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "metadata": metadata,
        }
    if mock:
        body["mock_testing_fallbacks"] = True
    return body


def infer(base: str, key: str, model: str, case_id: str, *, mock: bool, allow_chat_retry: bool) -> dict:
    attempts = []
    started_at = dt.datetime.utcnow()
    resp = http_json(
        "POST",
        f"{base}/v1/responses",
        key,
        request_payload("responses", model, case_id, mock=mock),
        timeout=90,
    )
    attempts.append({"endpoint": "responses", "response": scrub_response(resp)})
    if resp["status"] == 200 or not allow_chat_retry:
        finished_at = dt.datetime.utcnow()
        return {
            "endpoint": "responses",
            "status": resp["status"],
            "headers": resp["headers"],
            "response_model": response_model(resp),
            "error": error_summary(resp) if resp["status"] != 200 else "",
            "attempts": attempts,
            "started_at": ts(started_at),
            "finished_at": ts(finished_at),
        }

    chat = http_json(
        "POST",
        f"{base}/v1/chat/completions",
        key,
        request_payload("chat", model, case_id, mock=mock),
        timeout=90,
    )
    attempts.append({"endpoint": "chat", "response": scrub_response(chat)})
    chosen = chat if chat["status"] == 200 else resp
    endpoint = "chat" if chat["status"] == 200 else "responses"
    finished_at = dt.datetime.utcnow()
    return {
        "endpoint": endpoint,
        "status": chosen["status"],
        "headers": chosen["headers"],
        "response_model": response_model(chosen),
        "error": error_summary(chosen) if chosen["status"] != 200 else "",
        "attempts": attempts,
        "started_at": ts(started_at),
        "finished_at": ts(finished_at),
    }


def scrub_response(resp: dict) -> dict:
    return {
        "status": resp.get("status"),
        "headers": {
            k: v
            for k, v in resp.get("headers", {}).items()
            if k.startswith("x-litellm") or k in {"content-type", "request-id"}
        },
        "model": response_model(resp),
        "error": error_summary(resp) if resp.get("status") != 200 else "",
    }


def ts(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def spend_log_select() -> list[str]:
    return [
        "    request_id,",
        "    call_type,",
        "    spend,",
        "    total_tokens,",
        '    "startTime"::text AS start_time,',
        "    model,",
        "    model_id,",
        "    model_group,",
        "    custom_llm_provider,",
        "    api_base,",
        "    status,",
        "    metadata->>'status' AS metadata_status,",
        "    metadata#>>'{error_information,error_code}' AS error_code,",
        "    metadata#>>'{error_information,error_class}' AS error_class,",
        "    left(COALESCE(metadata#>>'{error_information,error_message}', ''), 1200) AS error_message",
    ]


def spend_logs(namespace: str, token: str, case_id: str, started_at: str, finished_at: str) -> list[dict]:
    like = "%" + case_id + "%"
    sql = "\n".join([
        "SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)",
        "FROM (",
        "  SELECT",
        *spend_log_select(),
        '  FROM "LiteLLM_SpendLogs"',
        f"  WHERE api_key = {sql_literal(token)}",
        "    AND \"startTime\" >= now() - interval '2 hours'",
        "    AND (",
        f"      metadata::text LIKE {sql_literal(like)}",
        f"      OR request_tags::text LIKE {sql_literal(like)}",
        f"      OR messages::text LIKE {sql_literal(like)}",
        f"      OR response::text LIKE {sql_literal(like)}",
        f"      OR proxy_server_request::text LIKE {sql_literal(like)}",
        "    )",
        '  ORDER BY "startTime" DESC',
        "  LIMIT 10",
        ") t;",
    ])
    for _ in range(12):
        rows = psql_json(namespace, sql) or []
        if rows:
            return rows
        time.sleep(1)
    window_sql = "\n".join([
        "SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)",
        "FROM (",
        "  SELECT",
        *spend_log_select(),
        '  FROM "LiteLLM_SpendLogs"',
        f"  WHERE api_key = {sql_literal(token)}",
        f"    AND \"startTime\" >= ({sql_literal(started_at)}::timestamp - interval '5 seconds')",
        f"    AND \"startTime\" <= ({sql_literal(finished_at)}::timestamp + interval '45 seconds')",
        '  ORDER BY "startTime" DESC',
        "  LIMIT 12",
        ") t;",
    ])
    for _ in range(8):
        rows = psql_json(namespace, window_sql) or []
        if rows:
            return rows
        time.sleep(1)
    return []


def latest_log(logs: list[dict]) -> dict | None:
    return logs[0] if logs else None


def joined_log_fields(row: dict | None) -> str:
    if not row:
        return ""
    return " ".join(str(row.get(k) or "") for k in (
        "model", "model_id", "model_group", "api_base", "custom_llm_provider", "error_message"
    ))


def route_log_fields(row: dict | None) -> str:
    if not row:
        return ""
    return " ".join(str(row.get(k) or "") for k in (
        "model", "model_id", "model_group", "api_base", "custom_llm_provider"
    ))


def is_chatgpt_log(row: dict | None, product: dict) -> bool:
    if not row:
        return False
    fields = route_log_fields(row).lower()
    if "wangsu" in fields or "openrouter" in fields:
        return False
    primary = str(product.get("primary") or "").lower()
    primary_contains = str(product.get("primary_contains") or "").lower()
    if primary_contains:
        return "chatgpt" in fields and primary_contains in fields
    if primary == "chatgpt-pool-gpt-5.5":
        return "chatgpt" in fields and "gpt-5.5" in fields and "wangsu" not in fields
    return primary in fields or ("chatgpt" in fields and primary.replace("chatgpt-", "") in fields)


def matches_fallback(row: dict | None, expected: str) -> bool:
    if not row:
        return False
    fields = route_log_fields(row)
    if expected == "wangsu-gpt-5.5":
        return "wangsu" in fields and "gpt-5.5" in fields
    if expected == "wangsu-gpt-5.4":
        return "wangsu" in fields and "gpt-5.4" in fields
    if expected == "wangsu7-gpt-5.3-codex":
        return "wangsu" in fields and "gpt-5.3-codex" in fields
    return expected in fields


def attempted_fallbacks(headers: dict) -> int:
    raw = headers.get("x-litellm-attempted-fallbacks")
    if raw is None:
        return 0
    try:
        return int(raw)
    except Exception:
        return 0


def hidden_access_denied(resp: dict) -> bool:
    if resp["status"] in {403, 404}:
        return True
    text = error_summary(resp).lower()
    return "key_model_access_denied" in text or "model not found" in text or "not found" in text


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


def cursor_key_audit(namespace: str) -> dict:
    rows = cursor_key_rows(namespace)
    product_set = set(PRODUCT_MODELS)
    hidden_set = set(INTERNAL_MODELS)
    examples = []
    counters = {
        "cursor_key_count": len(rows),
        "keys_exactly_9_products": 0,
        "keys_with_extra_models": 0,
        "keys_with_missing_products": 0,
        "keys_with_hidden_models": 0,
        "keys_with_aliases": 0,
        "keys_with_direct_internal_aliases": 0,
        "examples": examples,
    }
    for row in rows:
        models = list(row.get("models") or [])
        aliases = row.get("aliases") or {}
        extra = sorted(set(models) - product_set)
        missing = sorted(product_set - set(models))
        hidden = sorted(set(models) & hidden_set)
        direct_aliases = {
            key: value
            for key, value in aliases.items()
            if key in product_set and str(value).startswith(("wangsu", "openrouter"))
        }
        if not extra and not missing:
            counters["keys_exactly_9_products"] += 1
        if extra:
            counters["keys_with_extra_models"] += 1
        if missing:
            counters["keys_with_missing_products"] += 1
        if hidden:
            counters["keys_with_hidden_models"] += 1
        if aliases:
            counters["keys_with_aliases"] += 1
        if direct_aliases:
            counters["keys_with_direct_internal_aliases"] += 1
        if (extra or missing or aliases) and len(examples) < 20:
            examples.append({
                "key_alias": row.get("key_alias"),
                "extra_models": extra,
                "missing_products": missing,
                "hidden_models": hidden,
                "aliases": aliases,
                "direct_internal_aliases": direct_aliases,
            })
    counters["status"] = "PASS" if (
        counters["keys_exactly_9_products"] == counters["cursor_key_count"]
        and counters["keys_with_extra_models"] == 0
        and counters["keys_with_missing_products"] == 0
        and counters["keys_with_aliases"] == 0
        and counters["keys_with_direct_internal_aliases"] == 0
    ) else "FAIL"
    return counters


def classify_blocker(call: dict, logs: list[dict]) -> str:
    text = " ".join([
        str(call.get("error") or ""),
        " ".join(str(attempt.get("response", {}).get("error") or "") for attempt in call.get("attempts", [])),
        " ".join(str(row.get("error_message") or "") for row in logs),
    ]).lower()
    if "apikey validate fail" in text or "auth_failed" in text:
        return "WANGSU7_AUTH_FAILED: wangsu7-gpt-5.3-codex upstream rejected the configured key"
    if "cannot connect to host 10.68.13.188" in text or "connect call failed ('10.68.13.188'" in text:
        return "CHATGPT_PRIMARY_UNREACHABLE: ChatGPT account endpoint on 10.68.13.188 is not reachable"
    return ""


def status_from_checks(checks: list[dict]) -> str:
    statuses = [item.get("status") for item in checks]
    if any(s == "FAIL" for s in statuses):
        return "FAIL"
    if any(s == "BLOCKED" for s in statuses):
        return "BLOCKED"
    return "PASS"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--namespace", default="litellm-dev")
    parser.add_argument("--configmap", default="litellm-config")
    parser.add_argument("--nodeport", default="30400")
    parser.add_argument("--base-prefix", default="/dev")
    parser.add_argument("--alias-prefix", default="codex-dev-gpt-products")
    parser.add_argument("--report-slug", default="litellm-dev-gpt-products")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--check-cursor-keys", action="store_true")
    parser.add_argument("--output-json")
    args = parser.parse_args()

    started = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    run_id = f"{args.profile}-gpt-products-" + dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    alias = f"{args.alias_prefix}-{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    base = f"http://localhost:{args.nodeport}{args.base_prefix.rstrip('/')}"

    result: dict[str, Any] = {
        "run": {
            "id": run_id,
            "profile": args.profile,
            "started_at": started,
            "base": base,
            "report_slug": args.report_slug,
            "strict": args.strict,
            "check_cursor_keys": args.check_cursor_keys,
        },
        "environment": {
            "namespace": args.namespace,
            "nodeport": args.nodeport,
            "base_prefix": args.base_prefix,
            "configmap": args.configmap,
            "upstream_connectivity": [],
            "pods": [],
            "config_checksum_before": "",
            "config_checksum_after": "",
            "db_router_hash_before": {},
            "db_router_hash_after": {},
            "general_store_model_in_db": None,
            "litellm_store_model_in_db": None,
            "test_key_alias": alias,
            "test_key_token_hash": "",
        },
        "model_info": {"available_model_names": [], "related": []},
        "setup": {},
        "visibility": {},
        "cursor_key_audit": {},
        "direct_internal": [],
        "normal_path": [],
        "fallback": [],
        "blockers": [],
        "cleanup": {},
        "overall_status": "BLOCKED",
    }

    key = ""
    token = ""
    mk = ""

    try:
        mk = master_key(args.namespace)
        result["environment"]["pods"] = pods(args.namespace)
        before_config = config_metadata(args.namespace, args.configmap)
        result["environment"]["config_checksum_before"] = before_config.get("checksum", "")
        result["environment"]["general_store_model_in_db"] = before_config.get("general_store_model_in_db")
        result["environment"]["litellm_store_model_in_db"] = before_config.get("litellm_store_model_in_db")
        result["environment"]["db_router_hash_before"] = db_router_hash(args.namespace)

        model_info, model_info_resp = get_model_info(base, mk)
        names = sorted({m.get("model_name") for m in model_info if m.get("model_name")})
        result["model_info"] = {
            "status": model_info_resp["status"],
            "available_model_names": names,
            "related": related_model_info(model_info),
        }
        result["environment"]["upstream_connectivity"] = upstream_connectivity(model_info)
        if args.check_cursor_keys:
            result["cursor_key_audit"] = cursor_key_audit(args.namespace)

        key, _ = create_key(base, mk, alias, args.profile)
        token = token_hash(args.namespace, alias)
        result["environment"]["test_key_token_hash"] = token

        models_resp = http_json("GET", f"{base}/v1/models", key)
        actual_models = []
        if models_resp["status"] == 200 and isinstance(models_resp["json"], dict):
            actual_models = sorted(
                item.get("id")
                for item in models_resp["json"].get("data", [])
                if isinstance(item, dict) and item.get("id")
            )
        expected = sorted(PRODUCT_MODELS)
        extra = sorted(set(actual_models) - set(expected))
        missing = sorted(set(expected) - set(actual_models))
        hidden_visible = sorted(set(actual_models) & set(INTERNAL_MODELS))
        result["visibility"] = {
            "status": "PASS" if models_resp["status"] == 200 and not extra and not missing and not hidden_visible else "FAIL",
            "http_status": models_resp["status"],
            "expected": expected,
            "actual": actual_models,
            "missing": missing,
            "extra": extra,
            "hidden_visible": hidden_visible,
        }

        for hidden in INTERNAL_MODELS:
            case_id = f"{run_id}-direct-{hidden}".replace(".", "-")
            resp = http_json(
                "POST",
                f"{base}/v1/chat/completions",
                key,
                request_payload("chat", hidden, case_id, mock=False),
                timeout=45,
            )
            result["direct_internal"].append({
                "model": hidden,
                "status": "PASS" if hidden_access_denied(resp) else "FAIL",
                "http_status": resp["status"],
                "error": error_summary(resp),
            })

        for product in PRODUCTS:
            model = product["model"]
            case_id = f"{run_id}-normal-{model}".replace(".", "-")
            call = infer(base, key, model, case_id, mock=False, allow_chat_retry=True)
            logs = spend_logs(args.namespace, token, case_id, call["started_at"], call["finished_at"])
            row = latest_log(logs)
            attempted = attempted_fallbacks(call["headers"])
            blocker = classify_blocker(call, logs)
            if call["status"] == 200 and is_chatgpt_log(row, product):
                status = "PASS"
                reason = ""
            elif call["status"] == 200 and product["fallback"] and matches_fallback(row, product["fallback"]):
                status = "BLOCKED"
                reason = "PRIMARY_NOT_VERIFIED: request succeeded through fallback instead of the expected ChatGPT primary"
            elif blocker:
                status = "BLOCKED"
                reason = blocker
            else:
                status = "FAIL"
                reason = "normal request did not route to expected primary"
            result["normal_path"].append({
                "model": model,
                "primary": product["primary"],
                "primary_contains": product.get("primary_contains"),
                "status": status,
                "reason": reason,
                "endpoint": call["endpoint"],
                "http_status": call["status"],
                "attempted_fallbacks": attempted,
                "response_model": call["response_model"],
                "error": call["error"],
                "spend_logs": logs,
                "attempts": call["attempts"],
            })

        available = set(names)
        for product in PRODUCTS:
            model = product["model"]
            expected_fb = product["fallback"]
            case_id = f"{run_id}-fallback-{model}".replace(".", "-")

            if expected_fb and expected_fb not in available:
                result["fallback"].append({
                    "model": model,
                    "expected_fallback": expected_fb,
                    "status": "BLOCKED",
                    "reason": f"{expected_fb} not available in /model/info",
                })
                continue

            call = infer(base, key, model, case_id, mock=True, allow_chat_retry=bool(expected_fb))
            logs = spend_logs(args.namespace, token, case_id, call["started_at"], call["finished_at"])
            row = latest_log(logs)
            attempted = attempted_fallbacks(call["headers"])
            blocker = classify_blocker(call, logs)

            if expected_fb:
                fallback_seen = matches_fallback(row, expected_fb)
                if call["status"] == 200 and attempted >= 1 and fallback_seen:
                    status = "PASS"
                    reason = ""
                elif fallback_seen and blocker:
                    status = "BLOCKED"
                    reason = blocker
                elif call["status"] == 200 and attempted >= 1 and not row:
                    status = "BLOCKED"
                    reason = "SPENDLOG_DELAY: response/header proves fallback, but SpendLogs row was not found in the polling window"
                elif call["status"] == 200 and attempted == 0 and is_chatgpt_log(row, product):
                    status = "BLOCKED"
                    reason = (
                        "MOCK_FALLBACK_NOT_TRIGGERED: request-level mock_testing_fallbacks was not propagated; "
                        "request completed on the expected primary"
                    )
                else:
                    status = "FAIL"
                    reason = "fallback did not reach expected target"
            else:
                has_hidden_log = any(
                    "wangsu" in route_log_fields(row).lower()
                    or "openrouter" in route_log_fields(row).lower()
                    for row in logs
                )
                if call["status"] == 200 and attempted == 0 and is_chatgpt_log(row, product):
                    status = "BLOCKED"
                    reason = (
                        "MOCK_FALLBACK_NOT_TRIGGERED: request-level mock_testing_fallbacks was not propagated; "
                        "request completed on the expected primary"
                    )
                else:
                    ok = call["status"] != 200 and not has_hidden_log
                    status = "PASS" if ok else "FAIL"
                    reason = "no fallback expected"

            result["fallback"].append({
                "model": model,
                "expected_fallback": expected_fb,
                "status": status,
                "reason": reason,
                "endpoint": call["endpoint"],
                "http_status": call["status"],
                "attempted_fallbacks": attempted,
                "response_model": call["response_model"],
                "error": call["error"],
                "spend_logs": logs,
                "attempts": call["attempts"],
            })

    except Exception as exc:
        result["setup"] = {
            "status": "FAIL",
            "reason": f"{type(exc).__name__}: {str(exc)[:1000]}",
        }

    finally:
        cleanup: dict[str, Any] = {}
        if key and mk:
            cleanup["delete_key_response"] = scrub_response(delete_key(base, mk, alias))
            try:
                cleanup["remaining_key_count"] = key_count(args.namespace, alias)
            except Exception as exc:
                cleanup["remaining_key_count_error"] = str(exc)
            cleanup["manual_cleanup_sql"] = (
                'SELECT token, key_alias FROM "LiteLLM_VerificationToken" '
                f"WHERE key_alias = {sql_literal(alias)};"
            )
            delete_status = (cleanup.get("delete_key_response") or {}).get("status")
            if delete_status == 200 and cleanup.get("remaining_key_count") == 0:
                cleanup["status"] = "PASS"
            else:
                cleanup["status"] = "FAIL"
        elif key:
            cleanup["status"] = "FAIL"
            cleanup["reason"] = "test key was created but master key is unavailable for cleanup"
        else:
            cleanup["status"] = "PASS" if result.get("setup", {}).get("status") == "FAIL" else "FAIL"
            cleanup["reason"] = "test key was not created"
        try:
            after_config = config_metadata(args.namespace, args.configmap)
            result["environment"]["config_checksum_after"] = after_config.get("checksum", "")
        except Exception as exc:
            result["environment"]["config_checksum_after_error"] = str(exc)
        result["environment"]["db_router_hash_after"] = db_router_hash(args.namespace)
        result["cleanup"] = cleanup

    checks = [
        result["setup"],
        result["visibility"],
        result["cursor_key_audit"] if args.check_cursor_keys else {},
        *result["direct_internal"],
        *result["normal_path"],
        *result["fallback"],
        result["cleanup"],
    ]
    result["blockers"] = sorted({
        item.get("reason")
        for item in checks
        if item.get("status") == "BLOCKED" and item.get("reason")
    })
    result["overall_status"] = status_from_checks(checks)

    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json:
        Path(args.output_json).write_text(payload, encoding="utf-8")

    print(BEGIN_MARKER)
    print(payload)
    print(END_MARKER)
    return 0 if result["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
"""


BEGIN_MARKER = "__LITELLM_DEV_GPT_VERIFY_RESULT_BEGIN__"
END_MARKER = "__LITELLM_DEV_GPT_VERIFY_RESULT_END__"

PROFILE_DEFAULTS = {
    "dev": {
        "namespace": "litellm-dev",
        "configmap": "litellm-config",
        "nodeport": "30400",
        "base_prefix": "/dev",
        "alias_prefix": "codex-dev-gpt-products",
        "report_slug": "litellm-dev-gpt-products",
    },
    "pro": {
        "namespace": "litellm-product",
        "configmap": "litellm-config",
        "nodeport": "30402",
        "base_prefix": "",
        "alias_prefix": "codex-pro-gpt-products-regress",
        "report_slug": "litellm-pro-gpt-products",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFAULTS), default="dev")
    parser.add_argument("--namespace")
    parser.add_argument("--configmap")
    parser.add_argument("--nodeport")
    parser.add_argument("--base-prefix")
    parser.add_argument("--alias-prefix")
    parser.add_argument("--report-slug")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--jms", default=None, help="path to jms wrapper")
    parser.add_argument("--strict", action="store_true", help="record strict-mode metadata for pro convergence checks")
    parser.add_argument("--check-cursor-keys", action="store_true", help="also assert real cursor keys are converged")
    args = parser.parse_args()
    defaults = PROFILE_DEFAULTS[args.profile]
    for key, value in defaults.items():
        if getattr(args, key) is None:
            setattr(args, key, value)
    return args


def extract_result(stdout: str) -> dict[str, Any] | None:
    if BEGIN_MARKER not in stdout or END_MARKER not in stdout:
        return None
    start = stdout.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = stdout.index(END_MARKER, start)
    payload = stdout[start:end].strip()
    return json.loads(payload)


def short_log(row: dict[str, Any] | None) -> str:
    if not row:
        return "no SpendLogs row"
    base = (
        f"model={row.get('model')}; group={row.get('model_group')}; "
        f"id={row.get('model_id')}; api_base={row.get('api_base')}; status={row.get('status')}"
    )
    if row.get("error_code") or row.get("error_class"):
        base += f"; error={row.get('error_code')}/{row.get('error_class')}"
    return base


def write_reports(result: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = result["run"]["started_at"].replace(":", "").replace("-", "").replace("Z", "Z")
    slug = result["run"].get("report_slug") or "litellm-dev-gpt-products"
    json_path = reports_dir / f"{slug}-{stamp}.json"
    md_path = reports_dir / f"{slug}-{stamp}.md"

    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return md_path, json_path


def render_markdown(result: dict[str, Any]) -> str:
    env = result["environment"]
    profile_name = str(result.get("run", {}).get("profile") or "dev").upper()
    lines: list[str] = []
    lines.append(f"# LiteLLM {profile_name} GPT Products Verification Report")
    lines.append("")
    lines.append(f"- Overall: **{result['overall_status']}**")
    lines.append(f"- Run ID: `{result['run']['id']}`")
    lines.append(f"- Profile: `{result['run'].get('profile', '')}`")
    lines.append(f"- Strict: `{result['run'].get('strict', False)}`")
    lines.append(f"- Cursor key audit enabled: `{result['run'].get('check_cursor_keys', False)}`")
    lines.append(f"- Started: `{result['run']['started_at']}`")
    lines.append(f"- Namespace: `{env['namespace']}`")
    lines.append(f"- Base: `{result['run']['base']}`")
    lines.append(f"- Test key alias: `{env['test_key_alias']}`")
    lines.append(f"- Test key token hash: `{env.get('test_key_token_hash') or '<missing>'}`")
    lines.append(f"- Config checksum before: `{env['config_checksum_before']}`")
    lines.append(f"- Config checksum after: `{env['config_checksum_after']}`")
    lines.append(f"- DB router hash before: `{(env.get('db_router_hash_before') or {}).get('hash', '')}`")
    lines.append(f"- DB router hash after: `{(env.get('db_router_hash_after') or {}).get('hash', '')}`")
    lines.append(f"- store_model_in_db general/litellm: `{env.get('general_store_model_in_db')}` / `{env.get('litellm_store_model_in_db')}`")
    lines.append("")
    if result.get("blockers"):
        lines.append("## Blockers")
        lines.append("")
        for blocker in result["blockers"]:
            lines.append(f"- `{blocker}`")
        lines.append("")

    setup = result.get("setup") or {}
    if setup:
        lines.append("## Setup")
        lines.append("")
        lines.append(f"- Status: **{setup.get('status', '<unknown>')}**")
        if setup.get("reason"):
            lines.append(f"- Reason: `{setup.get('reason')}`")
        lines.append("")

    lines.append("## Environment Diagnostics")
    lines.append("")
    lines.append("| Check | Status | Detail |")
    lines.append("|---|---:|---|")
    for check in env.get("upstream_connectivity") or []:
        detail = f"{check.get('api_base')} ({check.get('host')}:{check.get('port')})"
        if check.get("error"):
            detail += f" - {check.get('error')}"
        lines.append(f"| upstream tcp | {check.get('status')} | {detail} |")
    if not env.get("upstream_connectivity"):
        lines.append("| upstream tcp | n/a | no chatgpt http api_base found |")
    lines.append("")

    lines.append("## Model Info Snapshot")
    lines.append("")
    lines.append(f"- `/model/info` HTTP: `{(result.get('model_info') or {}).get('status', '<unknown>')}`")
    related = (result.get("model_info") or {}).get("related") or []
    lines.append(f"- Related model rows: `{len(related)}`")
    lines.append("| Model Name | LiteLLM Model | Model ID | API Base | Mode |")
    lines.append("|---|---|---|---|---|")
    for row in related[:80]:
        lines.append(
            f"| `{row.get('model_name')}` | `{row.get('litellm_model')}` | "
            f"`{row.get('model_id')}` | `{row.get('api_base')}` | `{row.get('mode')}` |"
        )
    if len(related) > 80:
        lines.append(f"| ... | ... | ... | truncated {len(related) - 80} rows | ... |")
    lines.append("")

    lines.append("## Model Visibility")
    vis = result["visibility"]
    lines.append("")
    lines.append(f"- Status: **{vis['status']}**")
    lines.append(f"- HTTP: `{vis['http_status']}`")
    lines.append(f"- Missing products: `{', '.join(vis['missing']) or 'none'}`")
    lines.append(f"- Extra visible models: `{', '.join(vis['extra']) or 'none'}`")
    lines.append(f"- Hidden models visible: `{', '.join(vis['hidden_visible']) or 'none'}`")
    lines.append("")

    audit = result.get("cursor_key_audit") or {}
    if audit:
        lines.append("## Cursor Key Audit")
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

    lines.append("## Normal Path")
    lines.append("")
    lines.append("| Product | Expected Primary | Required Route Marker | Status | Endpoint | HTTP | Attempted | SpendLogs Evidence | Reason |")
    lines.append("|---|---|---|---:|---|---:|---:|---|---|")
    for item in result["normal_path"]:
        row = item.get("spend_logs", [None])[0] if item.get("spend_logs") else None
        lines.append(
            f"| `{item['model']}` | `{item['primary']}` | `{item.get('primary_contains') or ''}` | {item['status']} | "
            f"`{item.get('endpoint') or ''}` | `{item.get('http_status')}` | "
            f"`{item.get('attempted_fallbacks', '')}` | {short_log(row)} | {item.get('reason') or ''} |"
        )
    lines.append("")

    lines.append("## Fallback Path")
    lines.append("")
    lines.append("| Product | Expected Fallback | Status | Endpoint | HTTP | Attempted | SpendLogs Evidence | Reason |")
    lines.append("|---|---|---:|---|---:|---:|---|---|")
    for item in result["fallback"]:
        row = item.get("spend_logs", [None])[0] if item.get("spend_logs") else None
        lines.append(
            f"| `{item['model']}` | `{item.get('expected_fallback') or 'none'}` | {item['status']} | "
            f"`{item.get('endpoint') or ''}` | `{item.get('http_status', '')}` | "
            f"`{item.get('attempted_fallbacks', '')}` | {short_log(row)} | {item.get('reason') or ''} |"
        )
    lines.append("")

    lines.append("## Direct Internal Requests")
    lines.append("")
    lines.append("| Internal Model | Status | HTTP | Error |")
    lines.append("|---|---:|---:|---|")
    for item in result["direct_internal"]:
        err = (item.get("error") or "").replace("\n", " ")[:180]
        lines.append(f"| `{item['model']}` | {item['status']} | `{item['http_status']}` | {err} |")
    lines.append("")

    lines.append("## Cleanup")
    cleanup = result.get("cleanup") or {}
    lines.append("")
    lines.append(f"- Status: **{cleanup.get('status', '<unknown>')}**")
    lines.append(f"- Remaining key count: `{cleanup.get('remaining_key_count', '<unknown>')}`")
    lines.append(f"- Delete key HTTP: `{(cleanup.get('delete_key_response') or {}).get('status', '<unknown>')}`")
    if cleanup.get("reason"):
        lines.append(f"- Reason: `{cleanup.get('reason')}`")
    lines.append(f"- Manual cleanup SQL: `{cleanup.get('manual_cleanup_sql', '')}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    jms = args.jms or os.path.join(here, "jms")

    remote_args = [
        "--profile",
        args.profile,
        "--namespace",
        args.namespace,
        "--configmap",
        args.configmap,
        "--nodeport",
        args.nodeport,
        "--base-prefix",
        args.base_prefix,
        "--alias-prefix",
        args.alias_prefix,
        "--report-slug",
        args.report_slug,
    ]
    if args.strict:
        remote_args.append("--strict")
    if args.check_cursor_keys:
        remote_args.append("--check-cursor-keys")

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(REMOTE_SCRIPT)
        local_path = f.name

    try:
        remote_path = "/tmp/_litellm_dev_gpt_products_verify.py"
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        remote_result_path = f"/tmp/_litellm_{args.profile}_gpt_products_verify_{stamp}.json"
        remote_log_path = f"/tmp/_litellm_{args.profile}_gpt_products_verify_{stamp}.log"
        subprocess.check_call([jms, "scp", local_path, f"AIYJY-litellm:{remote_path}"])
        remote_args_with_output = remote_args + ["--output-json", remote_result_path]
        command = (
            "nohup python3 "
            + shlex.quote(remote_path)
            + " "
            + shlex.join(remote_args_with_output)
            + " > "
            + shlex.quote(remote_log_path)
            + " 2>&1 < /dev/null & echo $!"
        )
        pid = subprocess.check_output([jms, "ssh", "AIYJY-litellm", command], text=True).strip().splitlines()[-1]
        print(f"remote verification pid -> {pid}", flush=True)
        print(f"remote result path      -> {remote_result_path}", flush=True)

        result = None
        deadline = time.monotonic() + 1800
        poll_errors = 0
        while time.monotonic() < deadline:
            probe = "\n".join([
                f"if [ -s {shlex.quote(remote_result_path)} ]; then",
                "  echo __DONE__",
                f"  cat {shlex.quote(remote_result_path)}",
                f"elif ps -p {shlex.quote(pid)} >/dev/null 2>&1; then",
                "  echo __RUNNING__",
                "else",
                "  echo __EXITED__",
                f"  test -f {shlex.quote(remote_log_path)} && tail -200 {shlex.quote(remote_log_path)}",
                "fi",
            ])
            try:
                out = subprocess.check_output(
                    [jms, "ssh", "AIYJY-litellm", probe],
                    text=True,
                    stderr=subprocess.STDOUT,
                )
                poll_errors = 0
            except subprocess.CalledProcessError as exc:
                poll_errors += 1
                print(
                    f"remote poll failed ({poll_errors}/10): {(exc.output or '').strip()}",
                    file=sys.stderr,
                    flush=True,
                )
                if poll_errors >= 10:
                    print("ERROR: remote polling failed repeatedly", file=sys.stderr)
                    return exc.returncode or 1
                time.sleep(15)
                continue
            marker, _, rest = out.partition("\n")
            marker = marker.strip()
            if marker == "__DONE__":
                result = json.loads(rest)
                break
            if marker == "__RUNNING__":
                print("remote verification still running...", flush=True)
                time.sleep(15)
                continue
            print(rest or out, file=sys.stderr)
            print("ERROR: remote verification exited before writing result JSON", file=sys.stderr)
            return 1

        if result is None:
            print("ERROR: remote verification timed out before writing result JSON", file=sys.stderr)
            return 124

        md_path, json_path = write_reports(result, Path(args.reports_dir))
        print(f"report markdown -> {md_path}")
        print(f"report json     -> {json_path}")
        print(f"overall status  -> {result['overall_status']}")
        subprocess.call([
            jms,
            "ssh",
            "AIYJY-litellm",
            "rm -f " + shlex.quote(remote_result_path) + " " + shlex.quote(remote_log_path),
        ])
        return 0 if result["overall_status"] == "PASS" else 1
    finally:
        os.unlink(local_path)


if __name__ == "__main__":
    sys.exit(main())
