"""Shared pytest fixtures for CarHer Admin backend tests."""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh isolated SQLite DB for each test.

    Monkeypatches the module-level DB_PATH/DB_DIR/BACKUP_DIR so every
    call inside database.py (get_db, init_db, backup_to_nas, …) targets
    a temp directory instead of the production path.
    """
    import backend.database as dbm

    db_dir = tmp_path / "db"
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(dbm, "DB_DIR", db_dir)
    monkeypatch.setattr(dbm, "DB_PATH", db_dir / "admin.db")
    monkeypatch.setattr(dbm, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(dbm, "_backup_dirty", False)

    db_dir.mkdir(parents=True, exist_ok=True)
    dbm.init_db()
    return dbm


def make_instance(overrides: dict | None = None) -> dict:
    """Return a minimal valid her_instances row dict."""
    base = {
        "id": 1,
        "name": "测试用户",
        "model": "gpt",
        "app_id": "cli_test",
        "app_secret": "secret123",
        "prefix": "s1",
        "owner": "ou_abc",
        "provider": "wangsu",
        "bot_open_id": "ou_bot1",
        "status": "running",
        "deploy_group": "stable",
        "image_tag": "v20260101",
        "litellm_key": "",
        "litellm_route_policy": "openrouter_first",
    }
    if overrides:
        base.update(overrides)
    return base
