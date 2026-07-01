import json
import os
import time

import pytest

from app.auth import AuthBundle, atomic_write_auth, merge_refreshed, should_refresh


def test_from_file_reads_nested_tokens(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({
        "tokens": {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_at": 12345.0,
            "last_refresh": 11000.0,
        },
        "account_id": "acct-x",
    }))
    b = AuthBundle.from_file(p)
    assert b.access_token == "AT"
    assert b.refresh_token == "RT"
    assert b.expires_at == 12345.0
    assert b.account_id == "acct-x"


def test_from_file_reads_flat_dict(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({
        "access_token": "AT",
        "refresh_token": "RT",
    }))
    b = AuthBundle.from_file(p)
    assert b.access_token == "AT"


def test_atomic_write_replaces_existing(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text("oldcontent")
    b = AuthBundle(access_token="NEW", refresh_token="RT2", expires_at=99.0)
    atomic_write_auth(p, b)
    d = json.loads(p.read_text())
    assert d["tokens"]["access_token"] == "NEW"


def test_atomic_write_does_not_leave_tmp_on_error(tmp_path, monkeypatch):
    p = tmp_path / "auth.json"
    # 强制 json.dump 抛错
    def boom(*a, **kw):
        raise OSError("fake disk full")
    monkeypatch.setattr("app.auth.json.dump", boom)
    with pytest.raises(OSError):
        atomic_write_auth(p, AuthBundle(access_token="A", refresh_token="R"))
    leftover = [f for f in os.listdir(tmp_path) if f.startswith("auth.json.")]
    assert leftover == []


def test_should_refresh_respects_min_interval():
    now = 1000.0
    b = AuthBundle(access_token="A", refresh_token="R",
                   expires_at=now - 100,         # 已过期
                   last_refresh_at=now - 5)      # 5s 前刚 refresh 过
    assert should_refresh(b, now=now) is False


def test_should_refresh_when_near_expiry():
    now = 1000.0
    b = AuthBundle(access_token="A", refresh_token="R",
                   expires_at=now + 30,         # < 60s 寿命
                   last_refresh_at=now - 3600)
    assert should_refresh(b, now=now) is True


def test_should_refresh_skips_unknown_expiry():
    b = AuthBundle(access_token="A", refresh_token="R", expires_at=0.0)
    assert should_refresh(b, now=1000.0) is False


def test_merge_refreshed_rotates_both_tokens():
    old = AuthBundle(access_token="oldAT", refresh_token="oldRT", account_id="acc-x")
    new = merge_refreshed(old, {
        "access_token": "newAT", "refresh_token": "newRT", "expires_in": 3600,
    }, now=1000.0)
    assert new.access_token == "newAT"
    assert new.refresh_token == "newRT"
    assert new.expires_at == 1000.0 + 3600
    assert new.account_id == "acc-x"


def test_merge_refreshed_keeps_old_rt_when_idp_does_not_rotate():
    old = AuthBundle(access_token="oldAT", refresh_token="oldRT")
    new = merge_refreshed(old, {"access_token": "newAT", "expires_in": 600}, now=1.0)
    assert new.refresh_token == "oldRT"
