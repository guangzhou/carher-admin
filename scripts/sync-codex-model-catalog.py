#!/usr/bin/env python3
"""Sync the Codex local model catalog from a LiteLLM /v1/models endpoint."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE_URL = os.environ.get("CODEX_MODELS_BASE_URL", "https://cc.auto-link.com.cn/pro/v1").rstrip("/")
CATALOG_PATH = Path(os.environ.get("CODEX_MODEL_CATALOG", "~/.codex/model-catalog-local.json")).expanduser()


def fetch_model_ids() -> list[str]:
    key = os.environ.get("CARHER_DEV_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("CARHER_DEV_KEY or OPENAI_API_KEY is required")
    request = urllib.request.Request(
        f"{BASE_URL}/models",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    ids = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
    return sorted({model_id for model_id in ids if model_id}, key=str.casefold)


def main() -> int:
    model_ids = fetch_model_ids()
    if not model_ids:
        raise RuntimeError("LiteLLM returned no models; refusing to overwrite the catalog")

    with CATALOG_PATH.open() as stream:
        catalog = json.load(stream)
    models = catalog.setdefault("models", [])
    existing = {model.get("slug"): model for model in models if model.get("slug")}
    template = existing.get("gpt-5.6-sol") or (models[0] if models else None)
    if template is None:
        raise RuntimeError("The local catalog has no template model")

    for model_id in model_ids:
        model = existing.get(model_id)
        if model is None:
            model = copy.deepcopy(template)
            model["slug"] = model_id
            model["display_name"] = model_id
            model["description"] = f"LiteLLM model: {model_id}"
            existing[model_id] = model
        model["visibility"] = "list"
        model["supported_in_api"] = True

    # Keep locally useful hidden entries, but place all visible models by name.
    visible = [model for model in existing.values() if model.get("visibility") == "list"]
    hidden = [model for model in existing.values() if model.get("visibility") != "list"]
    visible.sort(key=lambda model: (str(model.get("display_name", model["slug"])).casefold(), model["slug"].casefold()))
    for priority, model in enumerate(visible, start=1):
        model["priority"] = priority
    catalog["models"] = visible + hidden
    catalog["last_synced_at"] = datetime.now(timezone.utc).isoformat()

    backup = CATALOG_PATH.with_suffix(CATALOG_PATH.suffix + ".bak-before-sync")
    shutil.copy2(CATALOG_PATH, backup)
    temporary = CATALOG_PATH.with_suffix(CATALOG_PATH.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(catalog, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(CATALOG_PATH)
    print(f"synced {len(model_ids)} LiteLLM models into {CATALOG_PATH}")
    print("first models:", ", ".join(model["slug"] for model in visible[:8]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
