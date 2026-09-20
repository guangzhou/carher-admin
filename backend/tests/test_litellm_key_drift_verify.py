from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "litellm-key-drift-verify.py"
SPEC = importlib.util.spec_from_file_location("litellm_key_drift_verify", SCRIPT)
assert SPEC and SPEC.loader
drift = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drift)

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


def write(tmp_path, payload):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_allowlist_backup_shape_keeps_every_row(tmp_path):
    """``--backup`` keys rows by token and omits it from the row body.

    Reading only ``row["token"]`` dropped all 227 rows and reported
    ``snapshot=0 keys`` -- indistinguishable from a wrong --prefix.
    """
    path = write(tmp_path, {
        TOKEN_A: {"key_alias": "carher-75", "models": ["gpt-5.5"], "aliases": {}},
        TOKEN_B: {"key_alias": "carher-14", "models": ["gpt-5.5"], "aliases": {}},
    })
    snapshot = drift.load_snapshot(path)
    assert set(snapshot) == {TOKEN_A, TOKEN_B}
    assert snapshot[TOKEN_A]["token"] == TOKEN_A
    assert snapshot[TOKEN_A]["key_alias"] == "carher-75"


def test_list_of_rows_carrying_their_own_token_still_works(tmp_path):
    path = write(tmp_path, [
        {"token": TOKEN_A, "key_alias": "carher-75", "models": [], "aliases": {}},
    ])
    assert set(drift.load_snapshot(path)) == {TOKEN_A}


def test_row_token_wins_over_mapping_key_when_both_present(tmp_path):
    path = write(tmp_path, {
        "stale-index": {"token": TOKEN_A, "key_alias": "carher-75",
                        "models": [], "aliases": {}},
    })
    snapshot = drift.load_snapshot(path)
    assert set(snapshot) == {TOKEN_A}


def test_non_dict_values_are_skipped_not_crashed(tmp_path):
    path = write(tmp_path, {TOKEN_A: None, TOKEN_B: {"key_alias": "carher-14",
                                                    "models": [], "aliases": {}}})
    assert set(drift.load_snapshot(path)) == {TOKEN_B}
