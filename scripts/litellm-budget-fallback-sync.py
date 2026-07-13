#!/usr/bin/env python3
"""Synchronize the isolated zero-cost GPT-5.3 budget fallback group.

The source zerokey group remains unchanged. This script creates sibling DB
model rows under an isolated group, removes stale siblings, and can probe the
result with a real request. It is dry-run unless ``--apply`` is supplied.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


SOURCE_GROUP = "zerokey-pool-gpt-5.3-codex"
TARGET_GROUP = "chatgpt-budget-fallback-gpt-5.3"
TARGET_ID_PREFIX = "budget-fallback/"
DEFAULT_BASE = "http://127.0.0.1:30402"
_SOURCE_ID = re.compile(r"^zk-(\d+)-gpt-5\.3-codex$")
_ZERO_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
)


@dataclass(frozen=True)
class SyncPlan:
    create: list[dict[str, Any]]
    delete: list[str]


class LiteLLMApi:
    def __init__(self, base: str, master_key: str):
        self.base = base.rstrip("/")
        self.master_key = master_key

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int = 45,
    ) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {self.master_key}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="ignore")[:1000]
            raise RuntimeError(f"HTTP {exc.code} {path}: {detail}") from exc
        return json.loads(raw) if raw else {}

    def request_with_headers(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int = 45,
    ) -> tuple[Any, dict[str, str]]:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {self.master_key}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="ignore")[:1000]
            raise RuntimeError(f"HTTP {exc.code} {path}: {detail}") from exc
        return (json.loads(raw) if raw else {}), response_headers

    def model_rows(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/v1/model/info", timeout=60)
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise RuntimeError("/v1/model/info did not return a model list")
        return rows


def _model_id(row: dict[str, Any]) -> str:
    return str((row.get("model_info") or {}).get("id") or "")


def source_members(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    members = [row for row in rows if row.get("model_name") == SOURCE_GROUP]
    ids = [_model_id(row) for row in members]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source model IDs")
    for row, model_id in zip(members, ids):
        params = row.get("litellm_params") or {}
        if not _SOURCE_ID.fullmatch(model_id):
            raise ValueError(f"unexpected source model ID: {model_id}")
        if "zero-" not in str(params.get("api_base") or ""):
            raise ValueError(f"source is not a zerokey endpoint: {model_id}")
        if not params.get("model") or not params.get("api_base"):
            raise ValueError(f"source is missing model/api_base: {model_id}")
    return sorted(members, key=_model_id)


def target_members(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    members = [row for row in rows if row.get("model_name") == TARGET_GROUP]
    ids = [_model_id(row) for row in members]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate target model IDs")
    for model_id in ids:
        if not model_id.startswith(TARGET_ID_PREFIX + "zk-"):
            raise ValueError(f"unexpected target model ID: {model_id}")
    return sorted(members, key=_model_id)


def target_payload(source: dict[str, Any]) -> dict[str, Any]:
    source_id = _model_id(source)
    if not _SOURCE_ID.fullmatch(source_id):
        raise ValueError(f"unexpected source model ID: {source_id}")
    params = source.get("litellm_params") or {}
    target_params = {
        "model": params["model"],
        "api_base": params["api_base"],
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
        "cache_read_input_token_cost": 0,
    }
    if params.get("api_key"):
        target_params["api_key"] = params["api_key"]
    return {
        "model_name": TARGET_GROUP,
        "litellm_params": target_params,
        "model_info": {
            "id": TARGET_ID_PREFIX + source_id,
            "mode": "responses",
        },
    }


def _managed(row: dict[str, Any]) -> dict[str, Any]:
    params = row.get("litellm_params") or {}
    info = row.get("model_info") or {}
    return {
        "model_name": row.get("model_name"),
        "litellm_params": {
            "model": params.get("model"),
            "api_base": params.get("api_base"),
            "api_key": params.get("api_key"),
            **{field: float(params.get(field, -1)) for field in _ZERO_FIELDS},
        },
        "model_info": {"id": info.get("id"), "mode": info.get("mode")},
    }


def build_plan(
    source: list[dict[str, Any]], target: list[dict[str, Any]]
) -> SyncPlan:
    desired = {payload["model_info"]["id"]: payload for payload in map(target_payload, source)}
    current = {_model_id(row): row for row in target}
    delete = sorted(
        model_id
        for model_id, row in current.items()
        if model_id not in desired or _managed(row) != _managed(desired[model_id])
    )
    create = [
        desired[model_id]
        for model_id in sorted(desired)
        if model_id not in current or model_id in delete
    ]
    return SyncPlan(create=create, delete=delete)


def probe_payload() -> dict[str, Any]:
    return {
        "model": TARGET_GROUP,
        "messages": [{"role": "user", "content": "Reply exactly pong"}],
        "max_tokens": 8,
    }


def validate_probe(response: Any, headers: dict[str, str]) -> None:
    choices = response.get("choices") if isinstance(response, dict) else None
    if not choices:
        raise RuntimeError("fallback probe did not return choices")
    model_id = str(headers.get("x-litellm-model-id") or "")
    if not model_id.startswith(TARGET_ID_PREFIX + "zk-"):
        raise RuntimeError(
            f"fallback probe used unexpected deployment: {model_id or 'missing'}"
        )


def validate_target(rows: list[dict[str, Any]]) -> None:
    members = target_members(rows)
    if not members:
        raise RuntimeError("target group has no members")
    for row in members:
        model_id = _model_id(row)
        params = row.get("litellm_params") or {}
        if not model_id.startswith(TARGET_ID_PREFIX + "zk-"):
            raise RuntimeError(f"unexpected target ID: {model_id}")
        if any(float(params.get(field, -1)) != 0 for field in _ZERO_FIELDS):
            raise RuntimeError(f"non-zero target cost: {model_id}")


def print_status(rows: list[dict[str, Any]]) -> None:
    source = source_members(rows)
    target = target_members(rows)
    plan = build_plan(source, target)
    print(
        f"source={len(source)} target={len(target)} "
        f"create={len(plan.create)} delete={len(plan.delete)}"
    )
    for model_id in plan.delete:
        print(f"  delete {model_id}")
    for payload in plan.create:
        print(f"  create {payload['model_info']['id']}")


def apply_sync(api: LiteLLMApi, plan: SyncPlan) -> None:
    replacements = {
        payload["model_info"]["id"]: payload
        for payload in plan.create
        if payload["model_info"]["id"] in plan.delete
    }
    staging_ids = []
    for model_id, payload in replacements.items():
        staging = json.loads(json.dumps(payload))
        staging_id = model_id + "/sync-staging"
        staging["model_info"]["id"] = staging_id
        api.request("POST", "/model/new", staging)
        staging_ids.append(staging_id)
        print(f"created {staging_id}")

    for model_id in plan.delete:
        api.request("POST", "/model/delete", {"id": model_id})
        print(f"deleted {model_id}")
    for payload in plan.create:
        api.request("POST", "/model/new", payload)
        print(f"created {payload['model_info']['id']}")
    for staging_id in staging_ids:
        api.request("POST", "/model/delete", {"id": staging_id})
        print(f"deleted {staging_id}")


def remove_group(api: LiteLLMApi, rows: list[dict[str, Any]]) -> None:
    for row in target_members(rows):
        model_id = _model_id(row)
        api.request("POST", "/model/delete", {"id": model_id})
        print(f"deleted {model_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status", "sync", "remove"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument(
        "--base", default=os.environ.get("LITELLM_BASE", DEFAULT_BASE)
    )
    args = parser.parse_args()
    master_key = os.environ.get("LITELLM_MK", "")
    if not master_key:
        parser.error("LITELLM_MK is required")
    api = LiteLLMApi(args.base, master_key)
    rows = api.model_rows()

    if args.command == "status":
        print_status(rows)
        return 0
    if args.command == "remove":
        members = target_members(rows)
        print(f"target={len(members)}")
        if args.apply:
            remove_group(api, rows)
        return 0

    source = source_members(rows)
    target = target_members(rows)
    plan = build_plan(source, target)
    print_status(rows)
    if not args.apply:
        return 0
    apply_sync(api, plan)
    final_rows = api.model_rows()
    validate_target(final_rows)
    final_plan = build_plan(source_members(final_rows), target_members(final_rows))
    if final_plan.create or final_plan.delete:
        raise RuntimeError("target group still differs from source after sync")
    print(f"verified target={len(target_members(final_rows))}")
    if args.probe:
        result, headers = api.request_with_headers(
            "POST", "/v1/chat/completions", probe_payload(), timeout=120
        )
        validate_probe(result, headers)
        print("probe=ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
