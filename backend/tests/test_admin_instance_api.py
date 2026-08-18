from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from backend import main
from backend.models import HerAddRequest


def test_instance_list_survives_legacy_pod_status_failure(monkeypatch):
    monkeypatch.setattr(main.crd_ops, "list_her_instances", lambda: [{
        "spec": {"userId": 7, "name": "CRD her", "prefix": "s1"},
        "status": {"phase": "Running"},
    }])
    monkeypatch.setattr(main, "_db_instances_excluding_crds", lambda: [])
    monkeypatch.setattr(
        main.k8s_ops,
        "get_all_pod_statuses",
        Mock(side_effect=RuntimeError("Kubernetes transport unavailable")),
    )

    result = main.api_list_instances()

    assert result["total"] == 1
    assert result["instances"][0]["id"] == 7
    assert result["instances"][0]["status"] == "Running"


def test_explicit_existing_id_returns_conflict_before_key_generation(monkeypatch):
    monkeypatch.setattr(main, "_require_cloudflare_for_create", lambda: None)
    monkeypatch.setattr(main.crd_ops, "get_her_instance", lambda uid: {"spec": {"userId": uid}})
    generate_key = Mock()
    monkeypatch.setattr(main.litellm_ops, "generate_key", generate_key)

    req = HerAddRequest(
        id=341,
        name="existing her",
        app_id="cli_test",
        app_secret="secret",
        provider="litellm",
    )

    with pytest.raises(HTTPException) as exc:
        main.api_add_instance(req)

    assert exc.value.status_code == 409
    generate_key.assert_not_called()
