"""Non-secret connection profile persistence for the Sub2API CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_PROFILE_PATH = Path.home() / ".cli-anything-sub2api" / "profile.json"


def _locked_save_json(path: Path, data: dict[str, Any]) -> None:
    """Save JSON while holding the file lock before truncating existing content."""
    try:
        handle = path.open("r+", encoding="utf-8")
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w", encoding="utf-8")

    with handle:
        locked = False
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except (ImportError, OSError):
            pass
        try:
            handle.seek(0)
            handle.truncate()
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if locked:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_profile(path: Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    """Load a profile, treating a missing profile as an empty configuration."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Profile is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Profile must be a JSON object: {path}")
    return data


def save_profile(base_url: str, path: Path = DEFAULT_PROFILE_PATH, *, target: str | None = None) -> dict[str, str]:
    """Persist only the target URL and optional deployment name; never credentials."""
    normalized = base_url.rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("Base URL must start with http:// or https://")
    payload: dict[str, str] = {"base_url": normalized}
    if target:
        payload["target"] = target
    _locked_save_json(path, payload)
    return {**payload, "profile_path": str(path)}
