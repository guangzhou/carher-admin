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

SCHEMA_VERSION = 12

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS her_instances (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT 'gpt',
    app_id          TEXT NOT NULL DEFAULT '',
    app_secret      TEXT NOT NULL DEFAULT '',
    prefix          TEXT NOT NULL DEFAULT 's1',
    owner           TEXT NOT NULL DEFAULT '',
    provider        TEXT NOT NULL DEFAULT 'wangsu',
    bot_open_id     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'running',
    sync_status     TEXT NOT NULL DEFAULT 'pending',
    deploy_group    TEXT NOT NULL DEFAULT 'stable',
    image_tag       TEXT NOT NULL DEFAULT 'fix-compact-eb348941',
    litellm_key     TEXT NOT NULL DEFAULT '',
    litellm_route_policy TEXT NOT NULL DEFAULT 'openrouter_first',
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
    mode            TEXT NOT NULL DEFAULT 'normal',
    current_wave    TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    branch          TEXT NOT NULL DEFAULT '',
    commit_sha      TEXT NOT NULL DEFAULT '',
    commit_msg      TEXT NOT NULL DEFAULT '',
    author          TEXT NOT NULL DEFAULT '',
    repo            TEXT NOT NULL DEFAULT '',
    run_url         TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS acr_image_tags (
    tag              TEXT PRIMARY KEY,
    repo_namespace   TEXT NOT NULL DEFAULT 'her',
    repo_name        TEXT NOT NULL DEFAULT 'carher',
    digest           TEXT NOT NULL DEFAULT '',
    image_id         TEXT NOT NULL DEFAULT '',
    image_size       INTEGER NOT NULL DEFAULT 0,
    image_update_ms  INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_acr_image_tags_updated_at ON acr_image_tags(updated_at DESC);

CREATE TABLE IF NOT EXISTS metrics_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    uid         INTEGER NOT NULL DEFAULT 0,
    cpu_m       REAL NOT NULL DEFAULT 0,
    memory_mi   REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics_history(ts);
CREATE INDEX IF NOT EXISTS idx_metrics_kind_uid ON metrics_history(kind, uid);

CREATE TABLE IF NOT EXISTS branch_rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern      TEXT NOT NULL UNIQUE,
    deploy_mode  TEXT NOT NULL DEFAULT 'normal',
    target_group TEXT NOT NULL DEFAULT '',
    auto_deploy  INTEGER NOT NULL DEFAULT 1,
    description  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    is_secret   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
    6: [
        "ALTER TABLE deploys ADD COLUMN branch TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE deploys ADD COLUMN commit_sha TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE deploys ADD COLUMN commit_msg TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE deploys ADD COLUMN author TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE deploys ADD COLUMN repo TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE deploys ADD COLUMN run_url TEXT NOT NULL DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS branch_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern     TEXT NOT NULL UNIQUE,
            deploy_mode TEXT NOT NULL DEFAULT 'normal',
            target_group TEXT NOT NULL DEFAULT '',
            auto_deploy INTEGER NOT NULL DEFAULT 1,
            description TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "INSERT OR IGNORE INTO branch_rules (pattern, deploy_mode, target_group, auto_deploy, description) VALUES ('main', 'normal', '', 1, '主分支 → 灰度部署')",
        "INSERT OR IGNORE INTO branch_rules (pattern, deploy_mode, target_group, auto_deploy, description) VALUES ('hotfix/*', 'fast', '', 1, '紧急修复 → 全量部署')",
        "INSERT OR IGNORE INTO branch_rules (pattern, deploy_mode, target_group, auto_deploy, description) VALUES ('feature/*', 'group:canary', 'canary', 0, '特性分支 → 仅金丝雀(需手动触发)')",
    ],
    7: [
        """CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL DEFAULT '',
            is_secret   INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('github_token', '', 1)",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('github_repos', '[\"guangzhou/CarHer\", \"guangzhou/carher-admin\"]', 0)",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('webhook_secret', '', 1)",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('feishu_webhook', '', 1)",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('agent_api_key', '', 1)",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('acr_registry', 'cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com', 0)",
    ],
    8: [
        """CREATE TABLE IF NOT EXISTS acr_image_tags (
            tag TEXT PRIMARY KEY,
            repo_namespace TEXT NOT NULL DEFAULT 'her',
            repo_name TEXT NOT NULL DEFAULT 'carher',
            digest TEXT NOT NULL DEFAULT '',
            image_id TEXT NOT NULL DEFAULT '',
            image_size INTEGER NOT NULL DEFAULT 0,
            image_update_ms INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_acr_image_tags_updated_at ON acr_image_tags(updated_at DESC)",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('acr_region_id', 'ap-southeast-1', 0)",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('acr_instance_id', '', 0)",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('acr_access_key_id', '', 1)",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('acr_access_key_secret', '', 1)",
    ],
    9: [
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('acr_username', '', 1)",
        "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('acr_password', '', 1)",
    ],
    10: [
        "ALTER TABLE her_instances ADD COLUMN litellm_key TEXT NOT NULL DEFAULT ''",
    ],
    11: [
        "ALTER TABLE her_instances RENAME TO her_instances_old",
        """CREATE TABLE her_instances (
            id              INTEGER PRIMARY KEY,
            name            TEXT NOT NULL DEFAULT '',
            model           TEXT NOT NULL DEFAULT 'gpt',
            app_id          TEXT NOT NULL DEFAULT '',
            app_secret      TEXT NOT NULL DEFAULT '',
            prefix          TEXT NOT NULL DEFAULT 's1',
            owner           TEXT NOT NULL DEFAULT '',
            provider        TEXT NOT NULL DEFAULT 'wangsu',
            bot_open_id     TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'running',
            sync_status     TEXT NOT NULL DEFAULT 'pending',
            deploy_group    TEXT NOT NULL DEFAULT 'stable',
            image_tag       TEXT NOT NULL DEFAULT 'fix-compact-eb348941',
            litellm_key     TEXT NOT NULL DEFAULT '',
            litellm_route_policy TEXT NOT NULL DEFAULT 'openrouter_first',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """INSERT INTO her_instances (
            id, name, model, app_id, app_secret, prefix, owner, provider,
            bot_open_id, status, sync_status, deploy_group, image_tag, litellm_key,
            litellm_route_policy,
            created_at, updated_at
        )
        SELECT
            id, name, model, app_id, app_secret, prefix, owner, provider,
            bot_open_id, status, sync_status, deploy_group, image_tag, litellm_key,
            'openrouter_first',
            created_at, updated_at
        FROM her_instances_old""",
        "DROP TABLE her_instances_old",
    ],
    12: [
        "ALTER TABLE her_instances ADD COLUMN litellm_route_policy TEXT NOT NULL DEFAULT 'openrouter_first'",
    ],
}

SEED_SQL = [
    "INSERT OR IGNORE INTO deploy_groups (name, priority, description) VALUES ('canary', 10, '金丝雀 — 最先更新')",
    "INSERT OR IGNORE INTO deploy_groups (name, priority, description) VALUES ('early', 50, '先行者 — 金丝雀通过后更新')",
    "INSERT OR IGNORE INTO deploy_groups (name, priority, description) VALUES ('stable', 100, '稳定 — 最后更新')",
    "INSERT OR IGNORE INTO branch_rules (pattern, deploy_mode, target_group, auto_deploy, description) VALUES ('main', 'normal', '', 1, '主分支 → 灰度部署')",
    "INSERT OR IGNORE INTO branch_rules (pattern, deploy_mode, target_group, auto_deploy, description) VALUES ('hotfix/*', 'fast', '', 1, '紧急修复 → 全量部署')",
    "INSERT OR IGNORE INTO branch_rules (pattern, deploy_mode, target_group, auto_deploy, description) VALUES ('feature/*', 'group:canary', 'canary', 0, '特性分支 → 仅金丝雀(需手动触发)')",
    "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('github_token', '', 1)",
    "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('github_repos', '[\"guangzhou/CarHer\", \"guangzhou/carher-admin\"]', 0)",
    "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('webhook_secret', '', 1)",
    "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('feishu_webhook', '', 1)",
    "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('agent_api_key', '', 1)",
    "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('acr_registry', 'cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com', 0)",
    "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('acr_username', '', 1)",
    "INSERT OR IGNORE INTO settings (key, value, is_secret) VALUES ('acr_password', '', 1)",
]


def _ensure_seed_data(conn: sqlite3.Connection):
    """Populate default groups, rules, and settings for both fresh and upgraded DBs."""
    for sql in SEED_SQL:
        conn.execute(sql)


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

    _ensure_seed_data(conn)
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
            """INSERT INTO her_instances (id, name, model, app_id, app_secret, prefix, owner, provider, bot_open_id, status, sync_status, deploy_group, image_tag, litellm_key, litellm_route_policy, created_at, updated_at)
               VALUES (:id, :name, :model, :app_id, :app_secret, :prefix, :owner, :provider, :bot_open_id, :status, 'pending', :deploy_group, :image_tag, :litellm_key, :litellm_route_policy, :now, :now)""",
            {
                "deploy_group": data.get("deploy_group", "stable"),
                "image_tag": data.get("image_tag", "fix-compact-eb348941"),
                "litellm_key": data.get("litellm_key", ""),
                "litellm_route_policy": data.get("litellm_route_policy", "openrouter_first"),
                **data,
                "now": _now(),
            },
        )
        _audit(conn, data["id"], "created", json.dumps({k: v for k, v in data.items() if k not in ("app_secret", "litellm_key")}, ensure_ascii=False))
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
        _audit(conn, uid, "updated", json.dumps({k: v for k, v in changes.items() if k not in ("app_secret", "litellm_key")}, ensure_ascii=False))
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
    if primary.startswith("litellm/"):
        provider = "litellm"
    elif primary.startswith("wangsu/"):
        provider = "wangsu"
    elif primary.startswith("anthropic/"):
        provider = "anthropic"
    else:
        provider = "openrouter"

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


def upsert_acr_image_tags(tags: list[dict[str, Any]]) -> int:
    """Upsert deduplicated ACR tags for the fixed her/carher repository."""
    if not tags:
        return 0

    deduped: dict[str, dict[str, Any]] = {}
    for item in tags:
        tag = str(item.get("tag", "")).strip()
        if not tag:
            continue
        current = deduped.get(tag)
        if current is None or int(item.get("image_update_ms", 0) or 0) >= int(current.get("image_update_ms", 0) or 0):
            deduped[tag] = {
                "tag": tag,
                "repo_namespace": item.get("repo_namespace", "her"),
                "repo_name": item.get("repo_name", "carher"),
                "digest": item.get("digest", ""),
                "image_id": item.get("image_id", ""),
                "image_size": int(item.get("image_size", 0) or 0),
                "image_update_ms": int(item.get("image_update_ms", 0) or 0),
                "updated_at": item.get("updated_at", _now()),
                "last_seen_at": _now(),
            }

    with get_db() as conn:
        for item in deduped.values():
            conn.execute(
                """INSERT INTO acr_image_tags
                   (tag, repo_namespace, repo_name, digest, image_id, image_size, image_update_ms, updated_at, last_seen_at)
                   VALUES (:tag, :repo_namespace, :repo_name, :digest, :image_id, :image_size, :image_update_ms, :updated_at, :last_seen_at)
                   ON CONFLICT(tag) DO UPDATE SET
                     repo_namespace = excluded.repo_namespace,
                     repo_name = excluded.repo_name,
                     digest = excluded.digest,
                     image_id = excluded.image_id,
                     image_size = excluded.image_size,
                     image_update_ms = excluded.image_update_ms,
                     updated_at = excluded.updated_at,
                     last_seen_at = excluded.last_seen_at""",
                item,
            )
        _audit(conn, None, "acr:sync", f"upserted={len(deduped)} repo=her/carher")
    backup_to_nas()
    return len(deduped)


def list_acr_image_tags(limit: int = 100) -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT tag FROM acr_image_tags
               WHERE repo_namespace = 'her' AND repo_name = 'carher'
               ORDER BY updated_at DESC, tag DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [r["tag"] for r in rows]


def get_current_image_tag() -> str:
    """Get the most common image_tag across running instances."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT image_tag, COUNT(*) as cnt FROM her_instances WHERE status='running' GROUP BY image_tag ORDER BY cnt DESC LIMIT 1"
        ).fetchone()
        return row["image_tag"] if row else "fix-compact-eb348941"


def list_image_tags(limit: int = 30) -> list[str]:
    """Return distinct image tags from ACR sync + deploy history + instances, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT tag FROM (
                 SELECT tag, MAX(ts) AS ts FROM (
                   SELECT tag, updated_at AS ts FROM acr_image_tags
                    WHERE repo_namespace = 'her' AND repo_name = 'carher'
                   UNION ALL
                   SELECT image_tag AS tag, MAX(created_at) AS ts FROM deploys GROUP BY image_tag
                   UNION ALL
                   SELECT image_tag AS tag, MAX(updated_at) AS ts FROM her_instances WHERE image_tag != '' GROUP BY image_tag
                 ) GROUP BY tag
               ) ORDER BY ts DESC, tag DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [r["tag"] for r in rows]


# ──────────────────────────────────────
# Deploy records
# ──────────────────────────────────────

def create_deploy(image_tag: str, prev_tag: str, total: int, mode: str = "normal",
                   branch: str = "", commit_sha: str = "", commit_msg: str = "",
                   author: str = "", repo: str = "", run_url: str = "") -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO deploys (image_tag, prev_image_tag, status, total, mode,
               branch, commit_sha, commit_msg, author, repo, run_url, created_at)
               VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (image_tag, prev_tag, total, mode, branch, commit_sha, commit_msg, author, repo, run_url, _now()),
        )
        deploy_id = cur.lastrowid
        _audit(conn, None, "deploy:created", f"tag={image_tag} total={total} mode={mode} branch={branch}")
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
# Branch rules (CI/CD → deploy mapping)
# ──────────────────────────────────────

def list_branch_rules() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM branch_rules ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def create_branch_rule(pattern: str, deploy_mode: str = "normal",
                       target_group: str = "", auto_deploy: bool = True,
                       description: str = "") -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO branch_rules (pattern, deploy_mode, target_group, auto_deploy, description)
               VALUES (?, ?, ?, ?, ?)""",
            (pattern, deploy_mode, target_group, 1 if auto_deploy else 0, description),
        )
        _audit(conn, None, "branch_rule:created", f"pattern={pattern} mode={deploy_mode}")
    backup_to_nas()
    return cur.lastrowid


def update_branch_rule(rule_id: int, **kwargs) -> bool:
    allowed = {"pattern", "deploy_mode", "target_group", "auto_deploy", "description"}
    sets, params = [], {"id": rule_id}
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            if k == "auto_deploy":
                v = 1 if v else 0
            sets.append(f"{k} = :{k}")
            params[k] = v
    if not sets:
        return False
    with get_db() as conn:
        conn.execute(f"UPDATE branch_rules SET {', '.join(sets)} WHERE id = :id", params)
    backup_to_nas()
    return True


def delete_branch_rule(rule_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM branch_rules WHERE id = ?", (rule_id,))
        _audit(conn, None, "branch_rule:deleted", f"id={rule_id}")
    backup_to_nas()


def match_branch_rule(branch: str) -> dict | None:
    """Find the first matching rule for a branch name. Supports glob patterns (*)."""
    import fnmatch
    rules = list_branch_rules()
    for rule in rules:
        if fnmatch.fnmatch(branch, rule["pattern"]):
            return rule
    return None


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
            """SELECT m.uid, m.cpu_m, m.memory_mi FROM metrics_history m
               INNER JOIN (
                   SELECT uid, MAX(ts) as max_ts FROM metrics_history
                   WHERE kind = 'pod' GROUP BY uid
               ) latest ON m.uid = latest.uid AND m.ts = latest.max_ts
               WHERE m.kind = 'pod'""",
        ).fetchall()
        return {r["uid"]: {"cpu_m": r["cpu_m"], "memory_mi": r["memory_mi"]} for r in rows}


# ──────────────────────────────────────
# Settings (key-value store)
# ──────────────────────────────────────

def get_all_settings(include_secrets: bool = False) -> dict[str, Any]:
    """Return all settings as {key: value}. Secrets are masked unless include_secrets=True."""
    with get_db() as conn:
        rows = conn.execute("SELECT key, value, is_secret FROM settings ORDER BY key").fetchall()
        result = {}
        for r in rows:
            if r["is_secret"] and not include_secrets and r["value"]:
                result[r["key"]] = "••••" + r["value"][-4:] if len(r["value"]) > 4 else "••••"
            else:
                result[r["key"]] = r["value"]
        return result


def get_setting(key: str) -> str:
    """Get a single setting value. Returns empty string if not found."""
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""


def update_settings(updates: dict[str, str]):
    """Update multiple settings at once. Empty string clears the value."""
    with get_db() as conn:
        for key, value in updates.items():
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, value, _now()),
            )
        _audit(conn, None, "settings:updated", ", ".join(updates.keys()))
    backup_to_nas()


def get_github_token() -> str:
    """Get GitHub token: prefer DB setting, fallback to env var."""
    db_token = get_setting("github_token")
    return db_token or os.environ.get("GITHUB_TOKEN", "")


def get_github_repos() -> list[str]:
    """Get configured GitHub repos as list."""
    raw = get_setting("github_repos")
    if raw:
        try:
            repos = json.loads(raw)
            if isinstance(repos, list):
                return repos
        except json.JSONDecodeError:
            pass
    return ["guangzhou/CarHer", "guangzhou/carher-admin"]


def get_webhook_secret() -> str:
    """Get webhook secret: prefer DB setting, fallback to env var."""
    db_secret = get_setting("webhook_secret")
    return db_secret or os.environ.get("DEPLOY_WEBHOOK_SECRET", "")


def get_acr_settings() -> dict[str, str]:
    """Get ACR Docker Registry v2 settings with env fallback."""
    return {
        "registry": get_setting("acr_registry") or os.environ.get("ACR_REGISTRY", "cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com"),
        "username": get_setting("acr_username") or os.environ.get("ACR_USERNAME", ""),
        "password": get_setting("acr_password") or os.environ.get("ACR_PASSWORD", ""),
    }
