"""SQLite database layer for CarHer Admin.

DB is the single source of truth for user registry.
ConfigMaps are derived artifacts generated from DB data.

Storage strategy (risk mitigation for SQLite + NFS):
  - SQLite on hostPath (local disk), NOT on NAS
  - Backup to NAS after every write
  - On startup, restore from NAS if local DB missing
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("carher-admin")

DB_DIR = Path(os.environ.get("CARHER_ADMIN_DB_DIR", "/data/carher-admin"))
DB_PATH = DB_DIR / "admin.db"
BACKUP_DIR = Path(os.environ.get("CARHER_ADMIN_BACKUP_DIR", "/nas-backup/carher-admin"))

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS her_instances (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT 'gpt',
    app_id          TEXT NOT NULL DEFAULT '',
    app_secret      TEXT NOT NULL DEFAULT '',
    prefix          TEXT NOT NULL DEFAULT 's1',
    owner           TEXT NOT NULL DEFAULT '',
    provider        TEXT NOT NULL DEFAULT 'openrouter',
    bot_open_id     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'running',
    sync_status     TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""


def init_db():
    """Initialize DB: restore from backup if local missing, then ensure schema."""
    DB_DIR.mkdir(parents=True, exist_ok=True)

    if not DB_PATH.exists():
        _try_restore_from_backup()

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA_SQL)

    # Check schema version
    cur = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


def _try_restore_from_backup():
    """Restore DB from NAS backup if available."""
    backup = BACKUP_DIR / "admin.db"
    if backup.exists():
        DB_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(backup), str(DB_PATH))
        logger.info("Restored DB from backup: %s", backup)
    else:
        logger.info("No backup found, starting with empty DB")


def backup_to_nas():
    """Copy DB to NAS backup directory. Called after every write."""
    if not DB_PATH.exists():
        return
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        # Use sqlite3 backup API for consistency
        src = sqlite3.connect(str(DB_PATH))
        dst = sqlite3.connect(str(BACKUP_DIR / "admin.db"))
        src.backup(dst)
        dst.close()
        src.close()

        # Also keep a daily snapshot
        daily = BACKUP_DIR / f"admin-{datetime.now().strftime('%Y%m%d')}.db"
        if not daily.exists():
            shutil.copy2(str(BACKUP_DIR / "admin.db"), str(daily))

        logger.debug("DB backed up to NAS")
    except Exception as e:
        logger.warning("Backup to NAS failed (non-fatal): %s", e)


@contextmanager
def get_db():
    """Get a DB connection. Use as context manager."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _audit(conn: sqlite3.Connection, instance_id: int | None, action: str, detail: str = ""):
    conn.execute(
        "INSERT INTO audit_log (instance_id, action, detail) VALUES (?, ?, ?)",
        (instance_id, action, detail),
    )


# ──────────────────────────────────────
# CRUD
# ──────────────────────────────────────

def list_all() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM her_instances ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def get_by_id(uid: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM her_instances WHERE id = ?", (uid,)).fetchone()
        return dict(row) if row else None


def insert(data: dict) -> dict:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO her_instances (id, name, model, app_id, app_secret, prefix, owner, provider, bot_open_id, status, sync_status, created_at, updated_at)
               VALUES (:id, :name, :model, :app_id, :app_secret, :prefix, :owner, :provider, :bot_open_id, :status, 'pending', :now, :now)""",
            {**data, "now": _now()},
        )
        _audit(conn, data["id"], "created", json.dumps({k: v for k, v in data.items() if k != "app_secret"}, ensure_ascii=False))
    backup_to_nas()
    return get_by_id(data["id"])


def update(uid: int, changes: dict) -> dict | None:
    sets = []
    params: dict[str, Any] = {"uid": uid, "now": _now()}
    for k, v in changes.items():
        if v is not None and k not in ("id", "created_at"):
            sets.append(f"{k} = :{k}")
            params[k] = v
    if not sets:
        return get_by_id(uid)
    sets.append("sync_status = 'pending'")
    sets.append("updated_at = :now")
    sql = f"UPDATE her_instances SET {', '.join(sets)} WHERE id = :uid"
    with get_db() as conn:
        conn.execute(sql, params)
        _audit(conn, uid, "updated", json.dumps({k: v for k, v in changes.items() if k != "app_secret"}, ensure_ascii=False))
    backup_to_nas()
    return get_by_id(uid)


def set_status(uid: int, status: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE her_instances SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), uid),
        )
        _audit(conn, uid, f"status:{status}")
    backup_to_nas()


def set_sync_status(uid: int, sync_status: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE her_instances SET sync_status = ? WHERE id = ?",
            (sync_status, uid),
        )


def delete_instance(uid: int):
    with get_db() as conn:
        conn.execute("UPDATE her_instances SET status = 'deleted', updated_at = ? WHERE id = ?", (_now(), uid))
        _audit(conn, uid, "deleted")
    backup_to_nas()


def purge_instance(uid: int):
    with get_db() as conn:
        conn.execute("DELETE FROM her_instances WHERE id = ?", (uid,))
        _audit(conn, uid, "purged")
    backup_to_nas()


def next_id() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT MAX(id) as max_id FROM her_instances").fetchone()
        return (row["max_id"] or 0) + 1


# ──────────────────────────────────────
# knownBots (computed from DB, never stored per-user)
# ──────────────────────────────────────

def collect_known_bots() -> tuple[dict[str, str], dict[str, str]]:
    """Compute knownBots and knownBotOpenIds from all active instances."""
    bots: dict[str, str] = {}
    bot_open_ids: dict[str, str] = {}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT app_id, name, bot_open_id FROM her_instances WHERE status != 'deleted'"
        ).fetchall()
        for r in rows:
            if r["app_id"] and r["name"]:
                bots[r["app_id"]] = r["name"]
            if r["bot_open_id"] and r["app_id"]:
                bot_open_ids[r["bot_open_id"]] = r["app_id"]
    return bots, bot_open_ids


def get_pending_sync() -> list[dict]:
    """Get instances that need ConfigMap sync."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM her_instances WHERE sync_status = 'pending' AND status != 'deleted'"
        ).fetchall()
        return [dict(r) for r in rows]


def get_audit_log(instance_id: int | None = None, limit: int = 50) -> list[dict]:
    with get_db() as conn:
        if instance_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE instance_id = ? ORDER BY id DESC LIMIT ?",
                (instance_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# ──────────────────────────────────────
# Import from K8s ConfigMaps (one-time migration)
# ──────────────────────────────────────

def import_from_configmap_data(uid: int, cfg: dict):
    """Import a single instance from parsed openclaw.json config."""
    feishu = cfg.get("channels", {}).get("feishu", {})
    primary = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")

    # Reverse-map model
    model_short = "gpt"
    for short, full in [("sonnet", "claude-sonnet"), ("opus", "claude-opus"), ("gpt", "gpt-5")]:
        if short in primary.lower() or full in primary.lower():
            model_short = short
            break

    # Extract prefix from OAuth URL
    prefix = "s1"
    import re
    url = feishu.get("oauthRedirectUri", "")
    m = re.match(r"https://(s\d+)-u", url)
    if m:
        prefix = m.group(1)

    # Determine provider
    provider = "anthropic" if primary.startswith("anthropic/") else "openrouter"

    owners = feishu.get("dm", {}).get("allowFrom", [])

    data = {
        "id": uid,
        "name": feishu.get("name", ""),
        "model": model_short,
        "app_id": feishu.get("appId", ""),
        "app_secret": feishu.get("appSecret", ""),
        "prefix": prefix,
        "owner": "|".join(owners),
        "provider": provider,
        "bot_open_id": feishu.get("botOpenId", ""),
        "status": "running",
    }

    existing = get_by_id(uid)
    if existing:
        logger.info("Instance %d already in DB, skipping import", uid)
        return existing
    return insert(data)
