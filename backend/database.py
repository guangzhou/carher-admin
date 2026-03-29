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

SCHEMA_VERSION = 5

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
    deploy_group    TEXT NOT NULL DEFAULT 'stable',
    image_tag       TEXT NOT NULL DEFAULT 'v20260328',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deploys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    image_tag       TEXT NOT NULL,
    prev_image_tag  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    total           INTEGER NOT NULL DEFAULT 0,
    done            INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    current_wave    TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deploy_groups (
    name            TEXT PRIMARY KEY,
    priority        INTEGER NOT NULL DEFAULT 100,
    description     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""

MIGRATIONS = {
    2: [
        "ALTER TABLE her_instances ADD COLUMN deploy_group TEXT NOT NULL DEFAULT 'stable'",
        "ALTER TABLE her_instances ADD COLUMN image_tag TEXT NOT NULL DEFAULT 'v20260328'",
        """CREATE TABLE IF NOT EXISTS deploys (
            id INTEGER PRIMARY KEY AUTOINCREMENT, image_tag TEXT NOT NULL,
            prev_image_tag TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0, current_wave TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT)""",
    ],
    3: [
        """CREATE TABLE IF NOT EXISTS deploy_groups (
            name TEXT PRIMARY KEY, priority INTEGER NOT NULL DEFAULT 100,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""",
        "INSERT OR IGNORE INTO deploy_groups (name, priority, description) VALUES ('canary', 10, '金丝雀 — 最先更新')",
        "INSERT OR IGNORE INTO deploy_groups (name, priority, description) VALUES ('early', 50, '先行者 — 金丝雀通过后更新')",
        "INSERT OR IGNORE INTO deploy_groups (name, priority, description) VALUES ('stable', 100, '稳定 — 最后更新')",
    ],
    4: [
        """CREATE TABLE IF NOT EXISTS metrics_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            kind        TEXT NOT NULL,
            uid         INTEGER NOT NULL DEFAULT 0,
            cpu_m       REAL NOT NULL DEFAULT 0,
            memory_mi   REAL NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics_history(ts)",
        "CREATE INDEX IF NOT EXISTS idx_metrics_kind_uid ON metrics_history(kind, uid)",
    ],
    5: [
        "ALTER TABLE deploys ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal'",
    ],
}


def init_db():
    """Initialize DB: restore from backup if local missing, then ensure schema."""
    DB_DIR.mkdir(parents=True, exist_ok=True)

    if not DB_PATH.exists():
        _try_restore_from_backup()

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA_SQL)

    # Check schema version and run migrations
    cur = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    row = cur.fetchone()
    current_version = row[0] if row else 0
    if current_version == 0:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        for ver in sorted(MIGRATIONS.keys()):
            if ver > current_version:
                for sql in MIGRATIONS[ver]:
                    try:
                        conn.execute(sql)
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e).lower() and "already exists" not in str(e).lower():
                            raise
                conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (ver,))
                logger.info("Migrated DB to schema version %d", ver)

    conn.commit()
    conn.close()
    logger.info("Database initialized at %s (schema v%d)", DB_PATH, SCHEMA_VERSION)


def _try_restore_from_backup():
    """Restore DB from NAS backup if available."""
    backup = BACKUP_DIR / "admin.db"
    if backup.exists():
        DB_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(backup), str(DB_PATH))
        logger.info("Restored DB from backup: %s", backup)
    else:
        logger.info("No backup found, starting with empty DB")


_backup_dirty = False


def backup_to_nas():
    """Mark DB as needing backup. Actual backup runs via flush_backup() or sync_worker."""
    global _backup_dirty
    _backup_dirty = True


def flush_backup():
    """Execute the actual NAS backup if dirty. Called by sync_worker every 60s."""
    global _backup_dirty
    if not _backup_dirty or not DB_PATH.exists():
        return
    _backup_dirty = False
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(DB_PATH))
        dst = sqlite3.connect(str(BACKUP_DIR / "admin.db"))
        src.backup(dst)
        dst.close()
        src.close()

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
            """INSERT INTO her_instances (id, name, model, app_id, app_secret, prefix, owner, provider, bot_open_id, status, sync_status, deploy_group, image_tag, created_at, updated_at)
               VALUES (:id, :name, :model, :app_id, :app_secret, :prefix, :owner, :provider, :bot_open_id, :status, 'pending', :deploy_group, :image_tag, :now, :now)""",
            {
                "deploy_group": data.get("deploy_group", "stable"),
                "image_tag": data.get("image_tag", "v20260328"),
                **data,
                "now": _now(),
            },
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

    # Reverse-map model (most specific match first to avoid false positives)
    model_short = "gpt"
    primary_lower = primary.lower()
    if "opus" in primary_lower or "claude-opus" in primary_lower:
        model_short = "opus"
    elif "sonnet" in primary_lower or "claude-sonnet" in primary_lower:
        model_short = "sonnet"

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


# ──────────────────────────────────────
# Deploy groups
# ──────────────────────────────────────

def list_by_deploy_group(group: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM her_instances WHERE deploy_group = ? AND status = 'running' ORDER BY id",
            (group,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_deploy_group(uid: int, group: str):
    with get_db() as conn:
        conn.execute("UPDATE her_instances SET deploy_group = ? WHERE id = ?", (group, uid))
        _audit(conn, uid, f"deploy_group:{group}")
    backup_to_nas()


def batch_set_deploy_group(uids: list[int], group: str):
    """Move multiple instances to a deploy group at once."""
    with get_db() as conn:
        for uid in uids:
            conn.execute("UPDATE her_instances SET deploy_group = ? WHERE id = ?", (group, uid))
            _audit(conn, uid, f"deploy_group:{group}")
    backup_to_nas()


# ──────────────────────────────────────
# Deploy group registry
# ──────────────────────────────────────

def list_deploy_groups() -> list[dict]:
    """Return all deploy groups ordered by priority (low = first to deploy)."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM deploy_groups ORDER BY priority, name").fetchall()
        return [dict(r) for r in rows]


def get_wave_order() -> list[str]:
    """Return group names in deploy order (ascending priority)."""
    groups = list_deploy_groups()
    return [g["name"] for g in groups]


def create_deploy_group(name: str, priority: int, description: str = ""):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO deploy_groups (name, priority, description) VALUES (?, ?, ?)",
            (name, priority, description),
        )
        _audit(conn, None, f"deploy_group_created:{name}", f"priority={priority}")
    backup_to_nas()


def update_deploy_group(name: str, priority: int | None = None, description: str | None = None):
    sets, params = [], {"name": name}
    if priority is not None:
        sets.append("priority = :priority")
        params["priority"] = priority
    if description is not None:
        sets.append("description = :description")
        params["description"] = description
    if not sets:
        return
    with get_db() as conn:
        conn.execute(f"UPDATE deploy_groups SET {', '.join(sets)} WHERE name = :name", params)
    backup_to_nas()


def delete_deploy_group(name: str) -> int:
    """Delete a group and move its instances to 'stable'. Returns count moved."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE her_instances SET deploy_group = 'stable' WHERE deploy_group = ?", (name,)
        )
        moved = cur.rowcount
        conn.execute("DELETE FROM deploy_groups WHERE name = ?", (name,))
        _audit(conn, None, f"deploy_group_deleted:{name}", f"moved={moved}")
    backup_to_nas()
    return moved


def get_deploy_group_stats() -> dict[str, int]:
    """Return {group_name: instance_count} for running instances."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT deploy_group, COUNT(*) as cnt FROM her_instances WHERE status='running' GROUP BY deploy_group"
        ).fetchall()
        return {r["deploy_group"]: r["cnt"] for r in rows}


def set_image_tag(uid: int, tag: str):
    with get_db() as conn:
        conn.execute("UPDATE her_instances SET image_tag = ? WHERE id = ?", (tag, uid))


def get_current_image_tag() -> str:
    """Get the most common image_tag across running instances."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT image_tag, COUNT(*) as cnt FROM her_instances WHERE status='running' GROUP BY image_tag ORDER BY cnt DESC LIMIT 1"
        ).fetchone()
        return row["image_tag"] if row else "v20260328"


# ──────────────────────────────────────
# Deploy records
# ──────────────────────────────────────

def create_deploy(image_tag: str, prev_tag: str, total: int, mode: str = "normal") -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO deploys (image_tag, prev_image_tag, status, total, mode, created_at) VALUES (?, ?, 'pending', ?, ?, ?)",
            (image_tag, prev_tag, total, mode, _now()),
        )
        deploy_id = cur.lastrowid
        _audit(conn, None, "deploy:created", f"tag={image_tag} total={total} mode={mode}")
    backup_to_nas()
    return deploy_id


def update_deploy(deploy_id: int, **kwargs):
    sets = []
    params = {"id": deploy_id}
    for k, v in kwargs.items():
        sets.append(f"{k} = :{k}")
        params[k] = v
    if not sets:
        return
    sql = f"UPDATE deploys SET {', '.join(sets)} WHERE id = :id"
    with get_db() as conn:
        conn.execute(sql, params)


def get_deploy(deploy_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM deploys WHERE id = ?", (deploy_id,)).fetchone()
        return dict(row) if row else None


def get_active_deploy() -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM deploys WHERE status IN ('pending','canary','rolling','paused') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def list_deploys(limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM deploys ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ──────────────────────────────────────
# Metrics history
# ──────────────────────────────────────

def insert_metrics_batch(rows: list[tuple]):
    """Batch insert metrics samples. Each row: (ts, kind, uid, cpu_m, memory_mi)."""
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO metrics_history (ts, kind, uid, cpu_m, memory_mi) VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def cleanup_old_metrics(days: int = 7):
    """Delete metrics older than N days."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM metrics_history WHERE ts < datetime('now', ?)",
            (f"-{days} days",),
        )
        logger.info("Cleaned up metrics older than %d days", days)


def get_pod_metrics_history(uid: int, hours: int = 24) -> list[dict]:
    """Get historical metrics for a specific pod, sampled over last N hours."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT ts, cpu_m, memory_mi FROM metrics_history
               WHERE kind = 'pod' AND uid = ? AND ts >= datetime('now', ?)
               ORDER BY ts""",
            (uid, f"-{hours} hours"),
        ).fetchall()
        return [dict(r) for r in rows]


def get_node_metrics_history(hours: int = 24) -> list[dict]:
    """Get historical node metrics (aggregated, uid=0)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT ts, SUM(cpu_m) as cpu_m, SUM(memory_mi) as memory_mi
               FROM metrics_history
               WHERE kind = 'node' AND ts >= datetime('now', ?)
               GROUP BY ts ORDER BY ts""",
            (f"-{hours} hours",),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_pods_latest_metrics() -> dict[int, dict]:
    """Get the latest metrics sample for each pod."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT uid, cpu_m, memory_mi FROM metrics_history
               WHERE kind = 'pod' AND ts = (
                   SELECT MAX(ts) FROM metrics_history WHERE kind = 'pod'
               )""",
        ).fetchall()
        return {r["uid"]: {"cpu_m": r["cpu_m"], "memory_mi": r["memory_mi"]} for r in rows}
