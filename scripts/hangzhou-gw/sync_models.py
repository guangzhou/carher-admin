#!/usr/bin/env python3
"""Reconcile the mirrored remote model catalogue through the LiteLLM API.

The upstream catalogue at ``https://cc.auto-link.com.cn/pro/v1/models`` is the
authoritative list of remote models the gateway should advertise. This script
mirrors that list into the proxy through its management API, so changes apply
to new requests without restarting the proxy.

Ownership rules:
  * every mirrored deployment is tagged ``model_info.managed_by = MANAGED_BY``,
    so a later run can recognise and retire deployments this script created;
  * models declared in ``config.yaml`` have no such tag, are file owned, and are
    never added, modified or deleted here;
  * a remote id that collides with a ``config.yaml`` model name is skipped,
    because two deployments sharing a ``model_name`` become a load-balanced
    model group and would silently change routing.

The remote credential is stored as an ``os.environ/`` reference, so the secret
stays in ``.env`` and is not copied into the proxy database.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:4000"
ENV_FILE = Path("/opt/llm-gateway/.env")
CONFIG = Path("/opt/llm-gateway/litellm/config.yaml")

UPSTREAM = os.environ.get(
    "MODEL_SYNC_UPSTREAM", "https://cc.auto-link.com.cn/pro/v1/models"
)
API_BASE = "https://cc.auto-link.com.cn/pro/v1"
KEY_ENV = "REMOTE_LITELLM_API_KEY"
MASTER_ENV = "LITELLM_MASTER_KEY"

# Referenced, not embedded: LiteLLM resolves this at request time, so the
# secret itself never enters the proxy database.
KEY_REF = "os.environ/" + KEY_ENV

MANAGED_BY = "model-sync"

# Non-chat models must not get LiteLLM's background health check: with no
# model_info.mode it calls them as chat. 2026-10-14: grok-imagine-image-2.0 got
# a chat request with response_format="b64_json" every ~5 min, x.ai rejected it
# with 422, ~530/day on 198 sub2api. Upstream /v1/models states mode for some
# (gpt-image-2 / bge-m3) but not for grok-imagine, so match by name too.
_NON_CHAT_MODES = {"image_generation", "embedding", "audio_transcription", "audio_speech",
                   "rerank", "video_generation", "moderation"}
_NON_CHAT_NAME = re.compile(r"(image|imagine|video|embed|bge|rerank|tts|whisper|moderation)", re.I)


def health_exempt_info(model_id: str, mode: object) -> dict:
    """model_info extras for a non-chat model ({} for chat/responses/unknown)."""
    is_non_chat = (isinstance(mode, str) and mode in _NON_CHAT_MODES) or bool(_NON_CHAT_NAME.search(model_id))
    if not is_non_chat:
        return {}
    info: dict = {"disable_background_health_check": True}
    if isinstance(mode, str) and mode in _NON_CHAT_MODES:
        info["mode"] = mode
    return info
HTTP_TIMEOUT = 60


def log(message: str) -> None:
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"{stamp} {message}", flush=True)


def read_env(name: str) -> str:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(name + "="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    raise SystemExit(f"missing {name} in {ENV_FILE}")


def fetch_upstream_catalog(api_key: str) -> dict[str, object]:
    """{model_id: upstream mode or None}, in upstream order."""
    request = urllib.request.Request(
        UPSTREAM, headers={"Authorization": "Bearer " + api_key}
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        payload = json.load(response)
    catalog: dict[str, object] = {}
    for item in payload.get("data", []):
        model_id = item.get("id")
        if model_id and model_id not in catalog:
            catalog[model_id] = item.get("mode")
    if not catalog:
        raise SystemExit("upstream returned no models; refusing to reconcile")
    return catalog


def config_model_names() -> set[str]:
    """Names owned by config.yaml. These always win a collision."""
    return set(re.findall(r"^  - model_name: (.+)$", CONFIG.read_text(), re.M))


def api(master_key: str, path: str, payload: dict | None = None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + master_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {body}") from exc


def managed_deployments(master_key: str) -> dict[str, str]:
    """Return {model_name: deployment_id} for deployments this script owns."""
    return {name: dep_id for name, (dep_id, _) in managed_rows(master_key).items()}


def managed_rows(master_key: str) -> dict[str, tuple[str, dict]]:
    """Return {model_name: (deployment_id, model_info)} for deployments this script owns."""
    payload = api(master_key, "/model/info")
    owned: dict[str, tuple[str, dict]] = {}
    for row in payload.get("data", []):
        info = row.get("model_info") or {}
        if info.get("managed_by") != MANAGED_BY or not info.get("db_model"):
            continue
        deployment_id = info.get("id")
        if deployment_id:
            owned[row["model_name"]] = (deployment_id, info)
    return owned


def add_model(master_key: str, model_id: str, extra_info: dict | None = None) -> None:
    api(
        master_key,
        "/model/new",
        {
            "model_name": model_id,
            "litellm_params": {
                "model": f"openai/{model_id}",
                "api_key": KEY_REF,
                "api_base": API_BASE,
            },
            "model_info": {"managed_by": MANAGED_BY, **(extra_info or {})},
        },
        method="POST",
    )


def delete_model(master_key: str, deployment_id: str) -> None:
    api(master_key, "/model/delete", {"id": deployment_id}, method="POST")


def main() -> int:
    master_key = read_env(MASTER_ENV)
    try:
        catalog = fetch_upstream_catalog(read_env(KEY_ENV))
        upstream_ids = list(catalog)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        log(f"upstream fetch failed ({exc}); leaving the proxy unchanged")
        return 1

    local = config_model_names()
    skipped = sorted(set(upstream_ids) & local)
    desired = [model_id for model_id in upstream_ids if model_id not in local]

    rows = managed_rows(master_key)
    current = {name: dep_id for name, (dep_id, _) in rows.items()}
    to_add = [model_id for model_id in desired if model_id not in current]
    to_remove = sorted(name for name in current if name not in set(desired))
    # existing non-chat rows missing the exemption: re-create that row only
    to_exempt = sorted(
        name for name in current
        if name in catalog and any(
            rows[name][1].get(k) != v for k, v in health_exempt_info(name, catalog[name]).items()
        )
    )

    if not to_add and not to_remove and not to_exempt:
        log(
            f"no change: {len(desired)} mirrored models already current"
            + (f"; skipped local collisions {skipped}" if skipped else "")
        )
        return 0

    failures: list[str] = []
    for model_id in to_add:
        try:
            add_model(master_key, model_id, health_exempt_info(model_id, catalog[model_id]))
        except (RuntimeError, urllib.error.URLError, OSError) as exc:
            failures.append(f"add {model_id}: {exc}")
    for name in to_remove:
        try:
            delete_model(master_key, current[name])
        except (RuntimeError, urllib.error.URLError, OSError) as exc:
            failures.append(f"remove {name}: {exc}")
    for name in to_exempt:
        # add first, then delete the old row, so the name never has zero deployments
        try:
            add_model(master_key, name, health_exempt_info(name, catalog[name]))
            delete_model(master_key, current[name])
        except (RuntimeError, urllib.error.URLError, OSError) as exc:
            failures.append(f"exempt {name}: {exc}")

    log(
        f"reconciled {len(desired)} mirrored models: "
        f"{len(to_add)} added, {len(to_remove)} removed, {len(to_exempt)} health-exempted"
        + (f"; skipped local collisions {skipped}" if skipped else "")
    )
    if to_add:
        log(f"added: {to_add}")
    if to_remove:
        log(f"removed: {to_remove}")
    if to_exempt:
        log(f"health-exempted: {to_exempt}")

    if failures:
        for failure in failures:
            log(f"FAILED {failure}")
        return 1

    live_rows = managed_rows(master_key)
    live = {name: dep_id for name, (dep_id, _) in live_rows.items()}
    missing = sorted(set(desired) - set(live))
    if missing:
        log(f"post-reconcile check is missing {missing}")
        return 1
    unexempt = sorted(
        n for n in desired if n in live_rows and any(
            live_rows[n][1].get(k) != v for k, v in health_exempt_info(n, catalog[n]).items()
        )
    )
    if unexempt:
        log(f"post-reconcile health exemption missing {unexempt}")
        return 1

    log("ok: proxy now mirrors the upstream catalogue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
