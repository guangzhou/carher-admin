"""Unit tests for backend/deployer.py.

All K8s / CRD calls are mocked. Tests run purely against in-memory
database state + deployer business logic.  The `db` fixture lives in
conftest.py and wires up a temp SQLite database.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from .conftest import make_instance


# ──────────────────────────────────────
# Helpers
# ──────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _crd_ops_stub(instances: list[dict] | None = None) -> MagicMock:
    """Return a mock crd_ops module that lists the given instances."""
    mock = MagicMock()
    mock.list_her_instances.return_value = instances or []
    mock.set_image.return_value = None
    mock.get_instance_status.return_value = {"phase": "Running", "feishuWS": "Connected"}
    return mock


# ──────────────────────────────────────
# _get_current_image_tag
# ──────────────────────────────────────

class TestGetCurrentImageTag:
    def test_most_common_tag_wins(self):
        from backend import deployer
        instances = [
            {"id": 1, "image_tag": "v1"},
            {"id": 2, "image_tag": "v1"},
            {"id": 3, "image_tag": "v2"},
        ]
        assert deployer._get_current_image_tag(instances) == "v1"

    def test_single_instance(self):
        from backend import deployer
        instances = [{"id": 1, "image_tag": "v42"}]
        assert deployer._get_current_image_tag(instances) == "v42"

    def test_empty_uses_db_fallback(self, db):
        from backend import deployer
        with patch.object(deployer, "db", db):
            result = deployer._get_current_image_tag([])
        assert isinstance(result, str)

    def test_skips_empty_tags(self, db):
        from backend import deployer
        instances = [
            {"id": 1, "image_tag": ""},
            {"id": 2, "image_tag": "v5"},
        ]
        with patch.object(deployer, "db", db):
            assert deployer._get_current_image_tag(instances) == "v5"


# ──────────────────────────────────────
# _list_all_deployable
# ──────────────────────────────────────

class TestListAllDeployable:
    def test_includes_running_db_instances(self, db):
        from backend import deployer
        db.insert(make_instance({"id": 1, "status": "running"}))
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None):
            result = deployer._list_all_deployable()
        assert any(i["id"] == 1 for i in result)

    def test_excludes_stopped_db_instances(self, db):
        from backend import deployer
        db.insert(make_instance({"id": 1, "status": "stopped"}))
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None):
            result = deployer._list_all_deployable()
        assert not any(i["id"] == 1 for i in result)

    def test_crd_instances_take_priority(self, db):
        from backend import deployer
        db.insert(make_instance({"id": 5, "status": "running", "image_tag": "db-tag"}))
        crd_mock = _crd_ops_stub([{
            "spec": {"userId": 5, "deployGroup": "canary", "image": "crd-tag", "prefix": "s1"}
        }])
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=crd_mock):
            result = deployer._list_all_deployable()
        entry = next(i for i in result if i["id"] == 5)
        assert entry["source"] == "crd"
        assert entry["image_tag"] == "crd-tag"

    def test_paused_crd_instances_excluded(self, db):
        from backend import deployer
        crd_mock = _crd_ops_stub([{
            "spec": {"userId": 9, "deployGroup": "stable", "image": "v1",
                     "prefix": "s1", "paused": True}
        }])
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=crd_mock):
            result = deployer._list_all_deployable()
        assert not any(i["id"] == 9 for i in result)


# ──────────────────────────────────────
# get_deploy_status
# ──────────────────────────────────────

class TestGetDeployStatus:
    def test_no_active_returns_inactive(self, db):
        from backend import deployer
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None):
            status = deployer.get_deploy_status()
        assert status["active"] is False

    def test_no_active_returns_last_deploy(self, db):
        from backend import deployer
        did = db.create_deploy("v2", "v1", 5)
        db.update_deploy(did, status="complete")
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None):
            status = deployer.get_deploy_status()
        assert status["last"] is not None
        assert status["last"]["image_tag"] == "v2"

    def test_active_deploy_in_status(self, db):
        from backend import deployer
        db.insert(make_instance({"id": 1}))
        did = db.create_deploy("v2", "v1", 1)
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None):
            status = deployer.get_deploy_status()
        assert status["active"] is True
        assert status["deploy"]["id"] == did

    def test_progress_pct_calculation(self, db):
        from backend import deployer
        did = db.create_deploy("v2", "v1", 10)
        db.update_deploy(did, done=5)
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None):
            status = deployer.get_deploy_status()
        assert status["progress_pct"] == 50


# ──────────────────────────────────────
# abort_deploy
# ──────────────────────────────────────

class TestAbortDeploy:
    def test_abort_no_active_returns_error(self, db):
        from backend import deployer
        with patch.object(deployer, "db", db):
            result = deployer.abort_deploy()
        assert "error" in result

    def test_abort_active_deploy(self, db):
        from backend import deployer
        did = db.create_deploy("v2", "v1", 5)
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_active_task", None):
            result = deployer.abort_deploy()
        assert result["action"] == "aborted"
        assert result["deploy_id"] == did
        assert db.get_deploy(did)["status"] == "failed"

    def test_abort_cancels_task(self, db):
        from backend import deployer
        db.create_deploy("v2", "v1", 5)
        task = MagicMock()
        task.done.return_value = False
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_active_task", task):
            deployer.abort_deploy()
        task.cancel.assert_called_once()


# ──────────────────────────────────────
# start_deploy (edge cases via mocking)
# ──────────────────────────────────────

class TestStartDeploy:
    def test_no_running_instances_returns_error(self, db):
        from backend import deployer
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None), \
             patch.object(deployer, "_active_task", None):
            result = _run(deployer.start_deploy("v-new"))
        assert "error" in result

    def test_already_deploying_same_tag_returns_already_deploying(self, db):
        from backend import deployer
        db.create_deploy("v-new", "v-old", 1)
        with patch.object(deployer, "db", db):
            result = _run(deployer.start_deploy("v-new"))
        assert result["status"] == "already_deploying"

    def test_already_deploying_different_tag_returns_error(self, db):
        from backend import deployer
        db.create_deploy("v-new", "v-old", 1)
        with patch.object(deployer, "db", db):
            result = _run(deployer.start_deploy("v-other"))
        assert "error" in result

    def test_already_deployed_returns_already_deployed(self, db):
        from backend import deployer
        did = db.create_deploy("v-same", "v-old", 1)
        db.update_deploy(did, status="complete")
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None):
            result = _run(deployer.start_deploy("v-same"))
        assert result["status"] == "already_deployed"

    def test_force_skips_already_deployed_check(self, db):
        from backend import deployer
        did = db.create_deploy("v-same", "v-old", 1)
        db.update_deploy(did, status="complete")
        db.insert(make_instance({"id": 1}))

        task_mock = MagicMock()
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None), \
             patch("asyncio.create_task", return_value=task_mock):
            result = _run(deployer.start_deploy("v-same", force=True))

        # Should NOT be "already_deployed" when force=True
        assert result.get("status") != "already_deployed"
        assert "error" not in result or result.get("status") != "already_deployed"

    def test_group_mode_filters_instances(self, db):
        from backend import deployer
        db.insert(make_instance({"id": 1, "deploy_group": "canary"}))
        db.insert(make_instance({"id": 2, "app_id": "cli_2", "deploy_group": "stable"}))

        task_mock = MagicMock()
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None), \
             patch("asyncio.create_task", return_value=task_mock):
            result = _run(deployer.start_deploy("v-new", mode="group:canary"))

        deploy = db.get_deploy(result["id"])
        assert deploy["total"] == 1

    def test_creates_deploy_record_with_ci_meta(self, db):
        from backend import deployer
        db.insert(make_instance({"id": 1}))
        ci_meta = {
            "branch": "main",
            "commit_sha": "abc123",
            "commit_msg": "feat: deploy test",
            "author": "alice",
            "repo": "org/repo",
            "run_url": "https://github.com/run/1",
        }
        task_mock = MagicMock()
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None), \
             patch("asyncio.create_task", return_value=task_mock):
            result = _run(deployer.start_deploy("v-new", ci_meta=ci_meta))

        deploy = db.get_deploy(result["id"])
        assert deploy["branch"] == "main"
        assert deploy["commit_sha"] == "abc123"
        assert deploy["author"] == "alice"


# ──────────────────────────────────────
# continue_deploy
# ──────────────────────────────────────

class TestContinueDeploy:
    def test_no_active_returns_error(self, db):
        from backend import deployer
        with patch.object(deployer, "db", db):
            result = _run(deployer.continue_deploy())
        assert "error" in result

    def test_not_paused_returns_error(self, db):
        from backend import deployer
        did = db.create_deploy("v2", "v1", 5)
        db.update_deploy(did, status="rolling")
        with patch.object(deployer, "db", db):
            result = _run(deployer.continue_deploy())
        assert "error" in result

    def test_paused_resumes(self, db):
        from backend import deployer
        did = db.create_deploy("v2", "v1", 5)
        db.update_deploy(did, status="paused", current_wave="canary")
        task_mock = MagicMock()
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None), \
             patch("asyncio.create_task", return_value=task_mock):
            result = _run(deployer.continue_deploy())
        assert result["id"] == did


# ──────────────────────────────────────
# rollback_deploy
# ──────────────────────────────────────

class TestRollbackDeploy:
    def test_no_active_returns_error(self, db):
        from backend import deployer
        with patch.object(deployer, "db", db):
            result = _run(deployer.rollback_deploy())
        assert "error" in result

    def test_no_prev_tag_returns_error(self, db):
        from backend import deployer
        db.create_deploy("v2", "", 3)
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=None):
            result = _run(deployer.rollback_deploy())
        assert "error" in result

    def test_rollback_marks_status(self, db):
        from backend import deployer
        db.insert(make_instance({"id": 1, "image_tag": "v2"}))
        did = db.create_deploy("v2", "v1", 1)
        crd_mock = _crd_ops_stub()
        with patch.object(deployer, "db", db), \
             patch.object(deployer, "_get_crd_ops", return_value=crd_mock), \
             patch.object(deployer, "_notify_deploy_event", return_value=None):
            result = _run(deployer.rollback_deploy())
        assert result["action"] == "rolled_back"
        assert db.get_deploy(did)["status"] == "rolled_back"


# ──────────────────────────────────────
# _health_check_batch
# ──────────────────────────────────────

class TestHealthCheckBatch:
    def test_all_healthy_returns_empty(self, db):
        from backend import deployer
        crd_mock = _crd_ops_stub()
        batch = [{"id": 1, "source": "crd"}]
        with patch.object(deployer, "_get_crd_ops", return_value=crd_mock):
            failures = _run(deployer._health_check_batch(batch))
        assert failures == []

    def test_failed_phase_reported(self, db):
        from backend import deployer
        crd_mock = MagicMock()
        crd_mock.get_instance_status.return_value = {"phase": "Failed", "feishuWS": "Connected"}
        batch = [{"id": 1, "source": "crd"}]
        with patch.object(deployer, "_get_crd_ops", return_value=crd_mock):
            failures = _run(deployer._health_check_batch(batch))
        assert len(failures) == 1
        assert failures[0]["id"] == 1

    def test_feishu_ws_disconnected_reported(self, db):
        from backend import deployer
        crd_mock = MagicMock()
        crd_mock.get_instance_status.return_value = {"phase": "Running", "feishuWS": "Disconnected"}
        batch = [{"id": 2, "source": "crd"}]
        with patch.object(deployer, "_get_crd_ops", return_value=crd_mock):
            failures = _run(deployer._health_check_batch(batch))
        assert len(failures) == 1
        assert "feishu" in failures[0]["reason"].lower()

    def test_exception_counts_as_unhealthy(self, db):
        from backend import deployer
        crd_mock = MagicMock()
        crd_mock.get_instance_status.side_effect = Exception("timeout")
        batch = [{"id": 3, "source": "crd"}]
        with patch.object(deployer, "_get_crd_ops", return_value=crd_mock):
            failures = _run(deployer._health_check_batch(batch))
        assert len(failures) == 1
