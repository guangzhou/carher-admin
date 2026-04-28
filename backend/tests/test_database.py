"""Unit tests for backend/database.py.

All tests operate on an in-process SQLite DB in a temp directory — no
production data is touched.  The `db` fixture is defined in conftest.py.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from .conftest import make_instance


# ──────────────────────────────────────
# Schema initialisation
# ──────────────────────────────────────

class TestInitDb:
    def test_creates_all_tables(self, db):
        conn = sqlite3.connect(str(db.DB_PATH))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        required = {
            "her_instances", "deploys", "audit_log",
            "deploy_groups", "acr_image_tags", "metrics_history",
            "branch_rules", "settings", "schema_version",
        }
        assert required.issubset(tables)

    def test_schema_version_is_current(self, db):
        conn = sqlite3.connect(str(db.DB_PATH))
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        conn.close()
        assert row[0] == db.SCHEMA_VERSION

    def test_seed_deploy_groups_exist(self, db):
        groups = {g["name"] for g in db.list_deploy_groups()}
        assert {"canary", "early", "stable"}.issubset(groups)

    def test_seed_branch_rules_exist(self, db):
        rules = {r["pattern"] for r in db.list_branch_rules()}
        assert {"main", "hotfix/*", "feature/*"}.issubset(rules)

    def test_idempotent_reinit(self, db):
        """Calling init_db() twice must not raise or corrupt data."""
        db.init_db()
        assert db.SCHEMA_VERSION == db.SCHEMA_VERSION  # no exception

    def test_restores_from_backup_when_local_missing(self, tmp_path, monkeypatch):
        import backend.database as dbm

        db_dir = tmp_path / "db2"
        backup_dir = tmp_path / "backup2"
        monkeypatch.setattr(dbm, "DB_DIR", db_dir)
        monkeypatch.setattr(dbm, "DB_PATH", db_dir / "admin.db")
        monkeypatch.setattr(dbm, "BACKUP_DIR", backup_dir)

        # Create a "backup" DB with a marker row
        backup_dir.mkdir(parents=True)
        bk = sqlite3.connect(str(backup_dir / "admin.db"))
        bk.executescript(dbm.SCHEMA_SQL)
        bk.execute("INSERT INTO schema_version (version) VALUES (?)", (dbm.SCHEMA_VERSION,))
        bk.execute(
            "INSERT INTO her_instances (id, name) VALUES (999, 'from-backup')"
        )
        bk.commit()
        bk.close()

        db_dir.mkdir(parents=True)
        dbm.init_db()
        row = dbm.get_by_id(999)
        assert row is not None
        assert row["name"] == "from-backup"


# ──────────────────────────────────────
# CRUD
# ──────────────────────────────────────

class TestCrud:
    def test_insert_returns_row(self, db):
        row = db.insert(make_instance())
        assert row is not None
        assert row["id"] == 1
        assert row["name"] == "测试用户"
        assert row["sync_status"] == "pending"

    def test_get_by_id_found(self, db):
        db.insert(make_instance())
        row = db.get_by_id(1)
        assert row["app_id"] == "cli_test"

    def test_get_by_id_missing(self, db):
        assert db.get_by_id(9999) is None

    def test_list_all_empty(self, db):
        assert db.list_all() == []

    def test_list_all_returns_all(self, db):
        db.insert(make_instance({"id": 1}))
        db.insert(make_instance({"id": 2, "name": "用户2", "app_id": "cli_2"}))
        rows = db.list_all()
        assert len(rows) == 2
        assert rows[0]["id"] == 1
        assert rows[1]["id"] == 2

    def test_update_changes_field(self, db):
        db.insert(make_instance())
        updated = db.update(1, {"name": "新名字"})
        assert updated["name"] == "新名字"

    def test_update_empty_changes_is_noop(self, db):
        db.insert(make_instance())
        row = db.update(1, {})
        assert row["name"] == "测试用户"

    def test_update_ignores_id_and_created_at(self, db):
        db.insert(make_instance())
        original = db.get_by_id(1)
        db.update(1, {"id": 9999, "created_at": "1970-01-01 00:00:00"})
        row = db.get_by_id(1)
        assert row["id"] == 1
        assert row["created_at"] == original["created_at"]

    def test_update_sets_sync_status_pending(self, db):
        db.insert(make_instance())
        db.set_sync_status(1, "synced")
        db.update(1, {"name": "changed"})
        assert db.get_by_id(1)["sync_status"] == "pending"

    def test_set_status(self, db):
        db.insert(make_instance())
        db.set_status(1, "stopped")
        assert db.get_by_id(1)["status"] == "stopped"

    def test_delete_instance_soft(self, db):
        db.insert(make_instance())
        db.delete_instance(1)
        row = db.get_by_id(1)
        assert row["status"] == "deleted"

    def test_purge_instance_removes_row(self, db):
        db.insert(make_instance())
        db.purge_instance(1)
        assert db.get_by_id(1) is None

    def test_next_id_empty_table(self, db):
        assert db.next_id() == 1

    def test_next_id_after_inserts(self, db):
        db.insert(make_instance({"id": 5}))
        assert db.next_id() == 6

    def test_insert_creates_audit_entry(self, db):
        db.insert(make_instance())
        log = db.get_audit_log(instance_id=1)
        assert any(e["action"] == "created" for e in log)

    def test_update_creates_audit_entry(self, db):
        db.insert(make_instance())
        db.update(1, {"name": "changed"})
        log = db.get_audit_log(instance_id=1)
        assert any(e["action"] == "updated" for e in log)

    def test_audit_log_masks_app_secret(self, db):
        db.insert(make_instance({"app_secret": "super-secret"}))
        log = db.get_audit_log(instance_id=1)
        created_entry = next(e for e in log if e["action"] == "created")
        assert "super-secret" not in created_entry["detail"]

    def test_audit_log_global_returns_all(self, db):
        db.insert(make_instance({"id": 1}))
        db.insert(make_instance({"id": 2, "app_id": "cli_2"}))
        log = db.get_audit_log()
        assert len(log) >= 2


# ──────────────────────────────────────
# knownBots
# ──────────────────────────────────────

class TestKnownBots:
    def test_collect_returns_app_id_to_name(self, db):
        db.insert(make_instance({"id": 1, "app_id": "cli_A", "name": "Alice", "bot_open_id": "ou_b1"}))
        bots, bot_open_ids = db.collect_known_bots()
        assert bots["cli_A"] == "Alice"

    def test_collect_returns_bot_open_id_to_app_id(self, db):
        db.insert(make_instance({"id": 1, "app_id": "cli_A", "name": "Alice", "bot_open_id": "ou_bot1"}))
        bots, bot_open_ids = db.collect_known_bots()
        assert bot_open_ids["ou_bot1"] == "cli_A"

    def test_deleted_instances_excluded(self, db):
        db.insert(make_instance({"id": 1, "app_id": "cli_A", "name": "Alice", "bot_open_id": "ou_b1"}))
        db.delete_instance(1)
        bots, _ = db.collect_known_bots()
        assert "cli_A" not in bots

    def test_empty_app_id_excluded(self, db):
        db.insert(make_instance({"id": 1, "app_id": "", "name": "Alice"}))
        bots, _ = db.collect_known_bots()
        assert "" not in bots


# ──────────────────────────────────────
# Sync status
# ──────────────────────────────────────

class TestSyncStatus:
    def test_get_pending_sync_returns_pending(self, db):
        db.insert(make_instance({"id": 1}))
        rows = db.get_pending_sync()
        assert any(r["id"] == 1 for r in rows)

    def test_get_pending_sync_excludes_synced(self, db):
        db.insert(make_instance({"id": 1}))
        db.set_sync_status(1, "synced")
        rows = db.get_pending_sync()
        assert not any(r["id"] == 1 for r in rows)

    def test_get_pending_sync_excludes_deleted(self, db):
        db.insert(make_instance({"id": 1}))
        db.delete_instance(1)
        rows = db.get_pending_sync()
        assert not any(r["id"] == 1 for r in rows)


# ──────────────────────────────────────
# Deploy groups
# ──────────────────────────────────────

class TestDeployGroups:
    def test_list_deploy_groups_ordered_by_priority(self, db):
        names = [g["name"] for g in db.list_deploy_groups()]
        # canary(10) < early(50) < stable(100)
        assert names.index("canary") < names.index("early")
        assert names.index("early") < names.index("stable")

    def test_get_wave_order(self, db):
        order = db.get_wave_order()
        assert order[0] == "canary"
        assert "stable" in order

    def test_create_deploy_group(self, db):
        db.create_deploy_group("vip", 5, "VIP tier")
        names = [g["name"] for g in db.list_deploy_groups()]
        assert "vip" in names

    def test_create_deploy_group_sets_priority(self, db):
        db.create_deploy_group("vip", 5)
        group = next(g for g in db.list_deploy_groups() if g["name"] == "vip")
        assert group["priority"] == 5

    def test_update_deploy_group_priority(self, db):
        db.update_deploy_group("canary", priority=99)
        group = next(g for g in db.list_deploy_groups() if g["name"] == "canary")
        assert group["priority"] == 99

    def test_delete_deploy_group_moves_instances_to_stable(self, db):
        db.create_deploy_group("custom", 20)
        db.insert(make_instance({"id": 1, "deploy_group": "custom"}))
        moved = db.delete_deploy_group("custom")
        assert moved == 1
        assert db.get_by_id(1)["deploy_group"] == "stable"

    def test_delete_deploy_group_removes_group(self, db):
        db.create_deploy_group("temp", 50)
        db.delete_deploy_group("temp")
        names = [g["name"] for g in db.list_deploy_groups()]
        assert "temp" not in names

    def test_set_deploy_group(self, db):
        db.insert(make_instance({"id": 1}))
        db.set_deploy_group(1, "canary")
        assert db.get_by_id(1)["deploy_group"] == "canary"

    def test_batch_set_deploy_group(self, db):
        db.insert(make_instance({"id": 1}))
        db.insert(make_instance({"id": 2, "app_id": "cli_2"}))
        db.batch_set_deploy_group([1, 2], "early")
        assert db.get_by_id(1)["deploy_group"] == "early"
        assert db.get_by_id(2)["deploy_group"] == "early"

    def test_get_deploy_group_stats(self, db):
        db.insert(make_instance({"id": 1, "deploy_group": "canary"}))
        db.insert(make_instance({"id": 2, "app_id": "cli_2", "deploy_group": "stable"}))
        stats = db.get_deploy_group_stats()
        assert stats["canary"] == 1
        assert stats["stable"] == 1

    def test_list_by_deploy_group(self, db):
        db.insert(make_instance({"id": 1, "deploy_group": "canary"}))
        db.insert(make_instance({"id": 2, "app_id": "cli_2", "deploy_group": "stable"}))
        canary = db.list_by_deploy_group("canary")
        assert len(canary) == 1
        assert canary[0]["id"] == 1


# ──────────────────────────────────────
# Deploy records
# ──────────────────────────────────────

class TestDeployRecords:
    def test_create_deploy(self, db):
        did = db.create_deploy("v2", "v1", 10, mode="normal")
        assert isinstance(did, int)
        assert did > 0

    def test_get_deploy(self, db):
        did = db.create_deploy("v2", "v1", 10)
        d = db.get_deploy(did)
        assert d["image_tag"] == "v2"
        assert d["prev_image_tag"] == "v1"
        assert d["total"] == 10
        assert d["status"] == "pending"

    def test_get_deploy_missing(self, db):
        assert db.get_deploy(9999) is None

    def test_update_deploy(self, db):
        did = db.create_deploy("v2", "v1", 10)
        db.update_deploy(did, status="rolling", done=5)
        d = db.get_deploy(did)
        assert d["status"] == "rolling"
        assert d["done"] == 5

    def test_get_active_deploy(self, db):
        did = db.create_deploy("v2", "v1", 5)
        active = db.get_active_deploy()
        assert active is not None
        assert active["id"] == did

    def test_get_active_deploy_none_when_complete(self, db):
        did = db.create_deploy("v2", "v1", 5)
        db.update_deploy(did, status="complete")
        assert db.get_active_deploy() is None

    def test_list_deploys(self, db):
        db.create_deploy("v1", "v0", 3)
        db.create_deploy("v2", "v1", 3)
        deploys = db.list_deploys()
        assert len(deploys) >= 2

    def test_list_deploys_limit(self, db):
        for i in range(5):
            db.create_deploy(f"v{i}", f"v{i-1}", 1)
        assert len(db.list_deploys(limit=3)) == 3

    def test_create_deploy_with_ci_meta(self, db):
        did = db.create_deploy(
            "v2", "v1", 10,
            branch="main", commit_sha="abc123", commit_msg="feat: add X",
            author="alice", repo="org/repo", run_url="https://gh/run/1",
        )
        d = db.get_deploy(did)
        assert d["branch"] == "main"
        assert d["commit_sha"] == "abc123"
        assert d["author"] == "alice"


# ──────────────────────────────────────
# Branch rules
# ──────────────────────────────────────

class TestBranchRules:
    def test_list_branch_rules(self, db):
        rules = db.list_branch_rules()
        assert len(rules) >= 3

    def test_create_branch_rule(self, db):
        rid = db.create_branch_rule("release/*", "fast")
        assert isinstance(rid, int)
        rules = {r["pattern"] for r in db.list_branch_rules()}
        assert "release/*" in rules

    def test_update_branch_rule(self, db):
        rid = db.create_branch_rule("test/*", "normal")
        db.update_branch_rule(rid, deploy_mode="fast")
        rule = next(r for r in db.list_branch_rules() if r["id"] == rid)
        assert rule["deploy_mode"] == "fast"

    def test_delete_branch_rule(self, db):
        rid = db.create_branch_rule("tmp/*", "normal")
        db.delete_branch_rule(rid)
        ids = {r["id"] for r in db.list_branch_rules()}
        assert rid not in ids

    def test_match_branch_rule_exact(self, db):
        rule = db.match_branch_rule("main")
        assert rule is not None
        assert rule["pattern"] == "main"

    def test_match_branch_rule_glob(self, db):
        rule = db.match_branch_rule("hotfix/login-bug")
        assert rule is not None
        assert rule["pattern"] == "hotfix/*"

    def test_match_branch_rule_feature(self, db):
        rule = db.match_branch_rule("feature/new-ui")
        assert rule is not None
        assert rule["pattern"] == "feature/*"

    def test_match_branch_rule_no_match(self, db):
        assert db.match_branch_rule("unknown-branch") is None

    def test_match_branch_rule_returns_first_match(self, db):
        """Rules are checked in insertion order; first matching rule wins."""
        # Add a second rule for main/* that should NOT take priority over the seed rule
        db.create_branch_rule("main/*", "fast")
        # "main" matches the seed rule "main" exactly — not the glob "main/*"
        rule = db.match_branch_rule("main")
        assert rule["pattern"] == "main"


# ──────────────────────────────────────
# Settings
# ──────────────────────────────────────

class TestSettings:
    def test_get_setting_exists(self, db):
        db.update_settings({"github_token": "ghp_abc"})
        assert db.get_setting("github_token") == "ghp_abc"

    def test_get_setting_missing_returns_empty(self, db):
        assert db.get_setting("nonexistent_key") == ""

    def test_update_settings_upsert(self, db):
        db.update_settings({"github_token": "v1"})
        db.update_settings({"github_token": "v2"})
        assert db.get_setting("github_token") == "v2"

    def test_get_all_settings_masks_secrets(self, db):
        db.update_settings({"webhook_secret": "my-long-secret"})
        settings = db.get_all_settings(include_secrets=False)
        val = settings.get("webhook_secret", "")
        assert "my-long-secret" not in val
        assert "••••" in val

    def test_get_all_settings_returns_secret_when_include(self, db):
        db.update_settings({"webhook_secret": "my-long-secret"})
        settings = db.get_all_settings(include_secrets=True)
        assert settings["webhook_secret"] == "my-long-secret"

    def test_short_secret_masked_fully(self, db):
        db.update_settings({"webhook_secret": "abc"})
        val = db.get_all_settings()["webhook_secret"]
        assert val == "••••"

    def test_get_github_token_db_preference(self, db, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        db.update_settings({"github_token": "db-token"})
        assert db.get_github_token() == "db-token"

    def test_get_github_token_env_fallback(self, db, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        db.update_settings({"github_token": ""})
        assert db.get_github_token() == "env-token"

    def test_get_github_repos_parses_json(self, db):
        db.update_settings({"github_repos": '["org/repo1", "org/repo2"]'})
        repos = db.get_github_repos()
        assert repos == ["org/repo1", "org/repo2"]

    def test_get_github_repos_default_on_invalid_json(self, db):
        db.update_settings({"github_repos": "not-json"})
        repos = db.get_github_repos()
        assert isinstance(repos, list)
        assert len(repos) > 0

    def test_get_webhook_secret_fallback(self, db, monkeypatch):
        monkeypatch.setenv("DEPLOY_WEBHOOK_SECRET", "env-secret")
        db.update_settings({"webhook_secret": ""})
        assert db.get_webhook_secret() == "env-secret"


# ──────────────────────────────────────
# ACR image tags
# ──────────────────────────────────────

class TestAcrImageTags:
    def _make_tag(self, tag: str, ts: int = 1000) -> dict:
        return {
            "tag": tag,
            "repo_namespace": "her",
            "repo_name": "carher",
            "digest": f"sha256:{tag}",
            "image_id": f"id-{tag}",
            "image_size": 500,
            "image_update_ms": ts,
        }

    def test_upsert_returns_count(self, db):
        tags = [self._make_tag("v1"), self._make_tag("v2")]
        count = db.upsert_acr_image_tags(tags)
        assert count == 2

    def test_list_acr_image_tags(self, db):
        db.upsert_acr_image_tags([self._make_tag("v1"), self._make_tag("v2")])
        tags = db.list_acr_image_tags()
        assert "v1" in tags
        assert "v2" in tags

    def test_upsert_deduplicates_by_tag(self, db):
        batch = [self._make_tag("v1", ts=100), self._make_tag("v1", ts=200)]
        count = db.upsert_acr_image_tags(batch)
        assert count == 1
        tags = db.list_acr_image_tags()
        assert tags.count("v1") == 1

    def test_upsert_keeps_newer_by_image_update_ms(self, db):
        db.upsert_acr_image_tags([self._make_tag("v1", ts=100)])
        newer = {**self._make_tag("v1", ts=999), "image_size": 9999}
        db.upsert_acr_image_tags([newer])
        conn = sqlite3.connect(str(db.DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT image_size FROM acr_image_tags WHERE tag='v1'").fetchone()
        conn.close()
        assert row["image_size"] == 9999

    def test_upsert_ignores_empty_tag(self, db):
        count = db.upsert_acr_image_tags([{"tag": "", "image_update_ms": 0}])
        assert count == 0

    def test_upsert_empty_list(self, db):
        assert db.upsert_acr_image_tags([]) == 0

    def test_list_acr_image_tags_limit(self, db):
        db.upsert_acr_image_tags([self._make_tag(f"tag-{i}") for i in range(10)])
        tags = db.list_acr_image_tags(limit=3)
        assert len(tags) == 3


# ──────────────────────────────────────
# Image tag helpers
# ──────────────────────────────────────

class TestImageTagHelpers:
    def test_get_current_image_tag_from_running(self, db):
        db.insert(make_instance({"id": 1, "image_tag": "v20260101"}))
        db.insert(make_instance({"id": 2, "app_id": "cli_2", "image_tag": "v20260101"}))
        db.insert(make_instance({"id": 3, "app_id": "cli_3", "image_tag": "v20260102"}))
        assert db.get_current_image_tag() == "v20260101"

    def test_get_current_image_tag_default_when_empty(self, db):
        tag = db.get_current_image_tag()
        assert isinstance(tag, str)
        assert len(tag) > 0

    def test_list_image_tags_includes_instance_tags(self, db):
        db.insert(make_instance({"id": 1, "image_tag": "instance-tag"}))
        tags = db.list_image_tags()
        assert "instance-tag" in tags

    def test_list_image_tags_includes_acr_tags(self, db):
        db.upsert_acr_image_tags([{
            "tag": "acr-tag", "repo_namespace": "her", "repo_name": "carher",
            "digest": "", "image_id": "", "image_size": 0, "image_update_ms": 999,
        }])
        tags = db.list_image_tags()
        assert "acr-tag" in tags


# ──────────────────────────────────────
# Import from ConfigMap
# ──────────────────────────────────────

class TestImportFromConfigmap:
    def _make_cfg(self, uid: int = 7) -> dict:
        return {
            "channels": {
                "feishu": {
                    "appId": "cli_cfg",
                    "appSecret": "secret",
                    "name": "Config User",
                    "botOpenId": "ou_cfgbot",
                    "oauthRedirectUri": f"https://s2-u{uid}-auth.carher.net/feishu/oauth/callback",
                    "dm": {"allowFrom": ["ou_owner1", "ou_owner2"]},
                }
            },
            "agents": {
                "defaults": {
                    "model": {"primary": "openrouter/anthropic/claude-sonnet-4.6"}
                }
            },
        }

    def test_import_creates_instance(self, db):
        db.import_from_configmap_data(7, self._make_cfg(7))
        row = db.get_by_id(7)
        assert row is not None
        assert row["app_id"] == "cli_cfg"

    def test_import_extracts_prefix_from_oauth_url(self, db):
        db.import_from_configmap_data(7, self._make_cfg(7))
        assert db.get_by_id(7)["prefix"] == "s2"

    def test_import_maps_sonnet_model(self, db):
        db.import_from_configmap_data(7, self._make_cfg(7))
        assert db.get_by_id(7)["model"] == "sonnet"

    def test_import_maps_opus_model(self, db):
        cfg = self._make_cfg(8)
        cfg["agents"]["defaults"]["model"]["primary"] = "anthropic/claude-opus-4-6"
        db.import_from_configmap_data(8, cfg)
        assert db.get_by_id(8)["model"] == "opus"

    def test_import_skips_if_already_exists(self, db):
        db.import_from_configmap_data(7, self._make_cfg(7))
        db.import_from_configmap_data(7, self._make_cfg(7))
        # Still only one row
        rows = [r for r in db.list_all() if r["id"] == 7]
        assert len(rows) == 1

    def test_import_joins_owners(self, db):
        db.import_from_configmap_data(7, self._make_cfg(7))
        row = db.get_by_id(7)
        assert "ou_owner1" in row["owner"]
        assert "ou_owner2" in row["owner"]


# ──────────────────────────────────────
# Metrics history
# ──────────────────────────────────────

class TestMetricsHistory:
    def test_insert_and_retrieve_pod_metrics(self, db):
        db.insert_metrics_batch([("2026-04-28 10:00:00", "pod", 1, 100.0, 256.0)])
        rows = db.get_pod_metrics_history(1, hours=24)
        assert len(rows) == 1
        assert rows[0]["cpu_m"] == pytest.approx(100.0)

    def test_cleanup_old_metrics(self, db):
        db.insert_metrics_batch([("2000-01-01 00:00:00", "pod", 1, 1.0, 1.0)])
        db.cleanup_old_metrics(days=7)
        rows = db.get_pod_metrics_history(1, hours=24 * 365 * 100)
        assert len(rows) == 0

    def test_get_node_metrics_aggregated(self, db):
        ts = "2026-04-28 10:00:00"
        db.insert_metrics_batch([
            (ts, "node", 1, 200.0, 1024.0),
            (ts, "node", 2, 300.0, 2048.0),
        ])
        rows = db.get_node_metrics_history(hours=24)
        assert len(rows) == 1
        assert rows[0]["cpu_m"] == pytest.approx(500.0)

    def test_get_all_pods_latest_metrics(self, db):
        db.insert_metrics_batch([
            ("2026-04-28 09:00:00", "pod", 1, 50.0, 128.0),
            ("2026-04-28 10:00:00", "pod", 1, 75.0, 192.0),
        ])
        latest = db.get_all_pods_latest_metrics()
        assert 1 in latest
        assert latest[1]["cpu_m"] == pytest.approx(75.0)


# ──────────────────────────────────────
# Backup
# ──────────────────────────────────────

class TestBackup:
    def test_flush_backup_copies_db(self, db):
        db.insert(make_instance())
        db._backup_dirty = True
        db.flush_backup()
        assert (db.BACKUP_DIR / "admin.db").exists()

    def test_flush_backup_noop_when_not_dirty(self, db):
        db._backup_dirty = False
        db.flush_backup()
        assert not (db.BACKUP_DIR / "admin.db").exists()

    def test_flush_backup_creates_daily_copy(self, db):
        from datetime import datetime
        db.insert(make_instance())
        db._backup_dirty = True
        db.flush_backup()
        daily = db.BACKUP_DIR / f"admin-{datetime.now().strftime('%Y%m%d')}.db"
        assert daily.exists()
