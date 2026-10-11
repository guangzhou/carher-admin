"""Tests for ``scripts/litellm-198-key-fallback.py``'s single-group drop path.

Each test pins a failure mode that was actually hit on 198 on 2026-10-10, so a
future edit cannot quietly undo the guard. The ones that matter most are the
silent ones: an empty table and a half-dropped chain both look like success.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "litellm-198-key-fallback.py"


def _load():
    spec = importlib.util.spec_from_file_location("kf_drop", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kf_drop"] = mod
    spec.loader.exec_module(mod)
    return mod


kf = _load()


# The real shapes, trimmed: a cursor key reaching astra only through the global
# model_group_alias, and the global table's two astra rows.
ASTRA_CHAIN = ["chatgpt-gpt-6-sol", "chatgpt-gpt-6-luna", "chatgpt-gpt-5.6-sol",
               "sa-grok-4.20", "openrouter-deepseek-v4.1-flash"]
GLOB = {
    "gpt-6-astra": list(ASTRA_CHAIN),
    "chatgpt-gpt-6-astra": list(ASTRA_CHAIN),
    "gpt-5.6-sol": ["chatgpt-gpt-5.6-luna", "sa-grok-4.20"],
    "sa-grok-4.6": ["openrouter-deepseek-v4.1-flash"],
    "kiro-claude-opus-5-5": ["sa-grok-4.20", "anthropic.wangsu5.claude-opus-5"],
}
MGA = {"gpt-6-astra": "chatgpt-gpt-6-astra",
       "chatgpt-gpt-6-astra": "chatgpt-gpt-6-astra"}
CURSOR_KEY = {
    "key_alias": "cursor-someone-ab12",
    "token": "f" * 64,
    "models": ["gpt-6-astra", "gpt-5.6-sol", "grok-4.6", "gpt-image-2"],
    "aliases": {"grok-4.6": "sa-grok-4.6"},
    "router_settings": {},
}


class TestLookupNames:
    def test_global_model_group_alias_adds_the_second_spelling(self):
        """The bug this prevents: dropping only `gpt-6-astra` leaves the chain
        alive under `chatgpt-gpt-6-astra`, with no symptom at all."""
        names = kf.lookup_names(CURSOR_KEY, "gpt-6-astra", MGA)
        assert names == {"gpt-6-astra", "chatgpt-gpt-6-astra"}

    def test_per_key_alias_adds_both_directions(self):
        names = kf.lookup_names(CURSOR_KEY, "sa-grok-4.6", MGA)
        assert names == {"grok-4.6", "sa-grok-4.6"}

    def test_bare_group_with_no_alias_is_just_itself(self):
        assert kf.lookup_names(CURSOR_KEY, "gpt-5.6-sol", MGA) == {"gpt-5.6-sol"}

    def test_item_form_alias_value_is_understood(self):
        """model_group_alias entries may be {"alias": {"model": ...}}."""
        mga = {"pub-name": {"model": "real-group", "hidden": True}}
        assert kf.lookup_names({}, "real-group", mga) == {"pub-name", "real-group"}

    def test_malformed_alias_value_does_not_crash(self):
        # The map can come from a key row rather than validated config.
        assert kf.lookup_names({}, "g", {"a": None, "b": 7}) == {"g"}


class TestBuildDropTable:
    def test_drops_both_astra_spellings_and_keeps_the_rest(self):
        drop = kf.lookup_names(CURSOR_KEY, "gpt-6-astra", MGA)
        table = kf.build_drop_table(CURSOR_KEY, drop, GLOB)
        written = {k: v for row in table for k, v in row.items()}
        assert "gpt-6-astra" not in written
        assert "chatgpt-gpt-6-astra" not in written
        assert written["gpt-5.6-sol"] == GLOB["gpt-5.6-sol"]
        assert written["sa-grok-4.6"] == GLOB["sa-grok-4.6"]

    def test_copies_chains_bytewise_so_nothing_else_changes(self):
        drop = {"gpt-6-astra", "chatgpt-gpt-6-astra"}
        table = kf.build_drop_table(CURSOR_KEY, drop, GLOB)
        for row in table:
            for g, chain in row.items():
                assert chain == GLOB[g], f"{g} was not copied verbatim"

    def test_does_not_invent_rows_for_groups_the_global_table_lacks(self):
        """A group with no global row has no fallback; writing one would be a new
        routing decision, not an edit."""
        key = dict(CURSOR_KEY, models=["gpt-6-astra", "brand-new-group"])
        table = kf.build_drop_table(key, {"gpt-6-astra"}, GLOB)
        assert all("brand-new-group" not in row for row in table)

    def test_mutating_the_result_cannot_corrupt_the_global_table(self):
        table = kf.build_drop_table(CURSOR_KEY, {"gpt-6-astra"}, GLOB)
        for row in table:
            for chain in row.values():
                chain.append("tampered")
        assert "tampered" not in GLOB["gpt-5.6-sol"]

    def test_alias_targets_are_carried_even_when_not_in_the_allowlist(self):
        """`sa-grok-4.6` is reachable only as an alias target, and it has a
        global chain; losing its row would silently drop that chain."""
        table = kf.build_drop_table(CURSOR_KEY, {"gpt-6-astra"}, GLOB)
        assert any("sa-grok-4.6" in row for row in table)


class TestKeyInfoGuards:
    """`/key/info?key_alias=` answers 200 with every field null on 198."""

    def _stub_api(self, monkeypatch, payload, status=200):
        import json as _json
        calls = []

        def fake(method, path, body=None, key=None, timeout=120):
            calls.append(path)
            return status, {}, _json.dumps(payload).encode()

        monkeypatch.setattr(kf, "api", fake)
        return calls

    def test_reads_via_key_list_not_the_null_returning_key_info(self, monkeypatch):
        calls = self._stub_api(monkeypatch, {"keys": [CURSOR_KEY]})
        info = kf.key_info("cursor-someone-ab12")
        assert info["token"] == "f" * 64
        assert calls and calls[0].startswith("/key/list")
        assert not any("/key/info" in c for c in calls)

    def test_all_null_row_is_refused_instead_of_silently_empty(self, monkeypatch):
        """The exact /key/info payload: 200, every field null. Must not become
        a zero-row table."""
        null_row = {"key_alias": "cursor-someone-ab12", "token": None,
                    "models": None, "aliases": None, "router_settings": None}
        self._stub_api(monkeypatch, {"keys": [null_row]})
        with pytest.raises(SystemExit):
            kf.key_info("cursor-someone-ab12")

    def test_ambiguous_match_is_refused(self, monkeypatch):
        self._stub_api(monkeypatch, {"keys": [CURSOR_KEY, dict(CURSOR_KEY)]})
        with pytest.raises(SystemExit):
            kf.key_info("cursor-someone-ab12")

    def test_substring_match_is_not_accepted_as_the_key(self, monkeypatch):
        """`key_alias=` filters server-side; a near-miss row must not be taken
        for the one asked about."""
        self._stub_api(monkeypatch, {"keys": [dict(CURSOR_KEY, key_alias="cursor-someone-ab12-old")]})
        with pytest.raises(SystemExit):
            kf.key_info("cursor-someone-ab12")


class TestGlobalTableGuards:
    def test_non_dict_router_settings_is_refused(self, monkeypatch):
        """The endpoint has returned a list in other versions; parsing it as a
        dict yields an empty map that reads as "no fallbacks anywhere"."""
        import json as _json
        monkeypatch.setattr(kf, "api", lambda *a, **k:
                            (200, {}, _json.dumps({"router_settings": []}).encode()))
        with pytest.raises(SystemExit):
            kf.model_group_aliases()


class TestNoOpAndEmptyRefusals:
    def _wire(self, monkeypatch, info, glob, mga):
        monkeypatch.setattr(kf, "key_info", lambda a: info)
        monkeypatch.setattr(kf, "global_fallbacks", lambda: glob)
        monkeypatch.setattr(kf, "model_group_aliases", lambda: mga)

    def test_group_with_no_reachable_global_row_is_a_refusal(self, monkeypatch):
        """The claude-key case: a non-empty per-key table already replaces the
        global one, so the group has no chain and there is nothing to drop."""
        self._wire(monkeypatch, CURSOR_KEY, {"gpt-5.6-sol": ["x"]}, {})
        args = type("A", (), {"key": ["cursor-someone-ab12"],
                              "group": "kiro-claude-opus-5-5",
                              "apply": False, "backup": None})()
        with pytest.raises(SystemExit):
            kf.cmd_drop(args)

    def test_empty_table_is_refused_even_when_a_row_would_be_dropped(self, monkeypatch):
        key = {"key_alias": "k", "token": "t", "models": ["gpt-6-astra"],
               "aliases": {}, "router_settings": {}}
        self._wire(monkeypatch, key, {"gpt-6-astra": list(ASTRA_CHAIN)}, MGA)
        args = type("A", (), {"key": ["k"], "group": "gpt-6-astra",
                              "apply": False, "backup": None})()
        with pytest.raises(SystemExit):
            kf.cmd_drop(args)

    def test_dry_run_writes_nothing(self, monkeypatch):
        self._wire(monkeypatch, CURSOR_KEY, GLOB, MGA)
        posted = []
        monkeypatch.setattr(kf, "api", lambda m, p, b=None, **k: posted.append((m, p)) or (200, {}, b"{}"))
        args = type("A", (), {"key": ["cursor-someone-ab12"], "group": "gpt-6-astra",
                              "apply": False, "backup": None})()
        assert kf.cmd_drop(args) == 0
        assert not posted

    def test_apply_without_backup_is_refused(self, monkeypatch):
        self._wire(monkeypatch, CURSOR_KEY, GLOB, MGA)
        args = type("A", (), {"key": ["cursor-someone-ab12"], "group": "gpt-6-astra",
                              "apply": True, "backup": None})()
        with pytest.raises(SystemExit):
            kf.cmd_drop(args)


class TestApplyWriteShape:
    def test_update_sends_neither_models_nor_aliases(self, monkeypatch, tmp_path):
        """/key/update replaces whole fields: sending `models` would clobber a
        32-entry allowlist, sending `aliases` a 25-entry map."""
        monkeypatch.setattr(kf, "key_info", lambda a: CURSOR_KEY)
        monkeypatch.setattr(kf, "global_fallbacks", lambda: GLOB)
        monkeypatch.setattr(kf, "model_group_aliases", lambda: MGA)
        sent = {}

        def fake(method, path, body=None, key=None, timeout=120):
            sent.update({"method": method, "path": path, "body": body})
            return 200, {}, b"{}"

        monkeypatch.setattr(kf, "api", fake)
        bk = tmp_path / "bak.json"
        args = type("A", (), {"key": ["cursor-someone-ab12"], "group": "gpt-6-astra",
                              "apply": True, "backup": str(bk)})()
        assert kf.cmd_drop(args) == 0
        assert sent["path"] == "/key/update"
        assert set(sent["body"]) == {"key_alias", "router_settings"}
        assert "models" not in sent["body"]
        assert "aliases" not in sent["body"]

    def test_backup_captures_what_restore_needs(self, monkeypatch, tmp_path):
        import json as _json
        monkeypatch.setattr(kf, "key_info", lambda a: CURSOR_KEY)
        monkeypatch.setattr(kf, "global_fallbacks", lambda: GLOB)
        monkeypatch.setattr(kf, "model_group_aliases", lambda: MGA)
        monkeypatch.setattr(kf, "api", lambda *a, **k: (200, {}, b"{}"))
        bk = tmp_path / "bak.json"
        args = type("A", (), {"key": ["cursor-someone-ab12"], "group": "gpt-6-astra",
                              "apply": True, "backup": str(bk)})()
        kf.cmd_drop(args)
        saved = _json.loads(bk.read_text())
        assert saved["router_settings"] == CURSOR_KEY["router_settings"]
        assert saved["models"] == CURSOR_KEY["models"]
        assert saved["aliases"] == CURSOR_KEY["aliases"]


class TestDropCheck:
    def _wire(self, monkeypatch, written_rows):
        info = dict(CURSOR_KEY, router_settings={"fallbacks": written_rows})
        monkeypatch.setattr(kf, "key_info", lambda a: info)
        monkeypatch.setattr(kf, "global_fallbacks", lambda: GLOB)
        monkeypatch.setattr(kf, "model_group_aliases", lambda: MGA)
        return type("A", (), {"key": ["cursor-someone-ab12"], "group": "gpt-6-astra"})()

    def _good_rows(self):
        drop = kf.lookup_names(CURSOR_KEY, "gpt-6-astra", MGA)
        return kf.build_drop_table(CURSOR_KEY, drop, GLOB)

    def test_passes_on_a_correct_write(self, monkeypatch):
        assert kf.cmd_drop_check(self._wire(monkeypatch, self._good_rows())) == 0

    def test_fails_when_the_chain_survives_under_the_other_spelling(self, monkeypatch):
        rows = self._good_rows() + [{"chatgpt-gpt-6-astra": list(ASTRA_CHAIN)}]
        assert kf.cmd_drop_check(self._wire(monkeypatch, rows)) == 1

    def test_fails_when_a_collateral_row_was_lost(self, monkeypatch):
        rows = [r for r in self._good_rows() if "gpt-5.6-sol" not in r]
        assert kf.cmd_drop_check(self._wire(monkeypatch, rows)) == 1

    def test_fails_on_drift_against_the_global_table(self, monkeypatch):
        """The snapshot cost made measurable: a later global edit shows up here
        instead of silently not reaching the key."""
        rows = [{"gpt-5.6-sol": ["stale-leg"]} if "gpt-5.6-sol" in r else r
                for r in self._good_rows()]
        assert kf.cmd_drop_check(self._wire(monkeypatch, rows)) == 1

    def test_fails_on_an_empty_table(self, monkeypatch):
        assert kf.cmd_drop_check(self._wire(monkeypatch, [])) == 1


class TestGlobalFallbacksShape:
    """`router_settings` is a dict on 198, not a list of field sections.

    The old parser iterated it as a list of ``{field_name, field_value}``
    sections. Iterating a dict yields its keys, ``str.get`` raised
    ``AttributeError``, the except swallowed it, and the table came back empty
    -- then fell through to ``/config/yaml``, which ``STORE_MODEL_IN_DB=True``
    makes authoritatively wrong. Measured: real table 109 rows, parsed 0.
    """

    def _stub(self, monkeypatch, payload, status=200):
        import json as _json
        seen = []

        def fake(method, path, body=None, key=None, timeout=120):
            seen.append(path)
            return status, {}, _json.dumps(payload).encode()

        monkeypatch.setattr(kf, "api", fake)
        return seen

    def test_parses_the_dict_shape(self, monkeypatch):
        self._stub(monkeypatch, {"router_settings": {"fallbacks": [
            {"gpt-6-astra": list(ASTRA_CHAIN)}, {"sa-grok-4.6": ["x"]}]}})
        assert kf.global_fallbacks() == {"gpt-6-astra": ASTRA_CHAIN, "sa-grok-4.6": ["x"]}

    def test_never_falls_back_to_config_yaml(self, monkeypatch):
        """DB overlays yaml entirely, so yaml is the one source guaranteed to
        disagree with what is live."""
        seen = self._stub(monkeypatch, {"router_settings": {"fallbacks": [{"g": ["t"]}]}})
        kf.global_fallbacks()
        assert not any("config/yaml" in p for p in seen)

    def test_list_shape_is_refused_not_silently_empty(self, monkeypatch):
        self._stub(monkeypatch, {"router_settings": [
            {"field_name": "fallbacks", "field_value": [{"g": ["t"]}]}]})
        with pytest.raises(SystemExit):
            kf.global_fallbacks()

    def test_empty_table_is_refused(self, monkeypatch):
        self._stub(monkeypatch, {"router_settings": {"fallbacks": []}})
        with pytest.raises(SystemExit):
            kf.global_fallbacks()

    def test_duplicate_row_is_refused(self, monkeypatch):
        self._stub(monkeypatch, {"router_settings": {"fallbacks": [
            {"g": ["a"]}, {"g": ["b"]}]}})
        with pytest.raises(SystemExit):
            kf.global_fallbacks()

    def test_chains_are_copied_so_callers_cannot_corrupt_them(self, monkeypatch):
        self._stub(monkeypatch, {"router_settings": {"fallbacks": [{"g": ["a"]}]}})
        t = kf.global_fallbacks()
        t["g"].append("tampered")
        assert kf.global_fallbacks()["g"] == ["a"]


class TestNoOpJudgedAgainstWhatTheKeyReads:
    """The no-op test must ask "does THIS KEY have the chain", not "does a
    global row exist".

    A live group always has a global row, so judging by ``glob`` has no
    discriminating power: it waves through both the already-done key and the
    key that never had the chain. Which table is live depends on whether the
    per-key table is empty.
    """

    def _wire(self, monkeypatch, router_settings):
        info = dict(CURSOR_KEY, router_settings=router_settings)
        monkeypatch.setattr(kf, "key_info", lambda a: info)
        monkeypatch.setattr(kf, "global_fallbacks", lambda: GLOB)
        monkeypatch.setattr(kf, "model_group_aliases", lambda: MGA)
        monkeypatch.setattr(kf, "api", lambda *a, **k: (200, {}, b"{}"))
        return type("A", (), {"key": ["cursor-someone-ab12"], "group": "gpt-6-astra",
                              "apply": False, "backup": None})()

    def test_empty_per_key_table_means_the_global_chain_is_live(self, monkeypatch):
        """Nothing written yet: the key reads the global table, so there IS a
        chain to drop and the dry-run must proceed."""
        assert kf.cmd_drop(self._wire(monkeypatch, {})) == 0

    def test_already_dropped_key_is_refused_as_a_no_op(self, monkeypatch):
        """Re-running on a done key must refuse, even though the global astra
        row still exists."""
        drop = kf.lookup_names(CURSOR_KEY, "gpt-6-astra", MGA)
        done = kf.build_drop_table(CURSOR_KEY, drop, GLOB)
        with pytest.raises(SystemExit):
            kf.cmd_drop(self._wire(monkeypatch, {"fallbacks": done}))

    def test_non_empty_table_lacking_the_group_is_refused(self, monkeypatch):
        """The claude-key shape: a 31-row table with no opus-5.5 row already has
        no fallback for it, because a non-empty per-key table replaces the
        global one."""
        with pytest.raises(SystemExit):
            kf.cmd_drop(self._wire(monkeypatch, {"fallbacks": [{"gpt-5.6-sol": ["x"]}]}))

    def test_non_empty_table_that_does_carry_the_chain_proceeds(self, monkeypatch):
        rows = [{"gpt-6-astra": list(ASTRA_CHAIN)}, {"gpt-5.6-sol": ["x"]}]
        assert kf.cmd_drop(self._wire(monkeypatch, {"fallbacks": rows})) == 0

    def test_chain_live_only_under_the_alias_spelling_still_proceeds(self, monkeypatch):
        """Half-dropped state: present as `chatgpt-gpt-6-astra` only. It is
        still live, so the drop must run and clean up both names."""
        rows = [{"chatgpt-gpt-6-astra": list(ASTRA_CHAIN)}, {"gpt-5.6-sol": ["x"]}]
        assert kf.cmd_drop(self._wire(monkeypatch, {"fallbacks": rows})) == 0
