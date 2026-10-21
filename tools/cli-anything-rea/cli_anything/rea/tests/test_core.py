"""Unit tests: no REA run, no network. They read the local clone (~/codes/rea) where a fact must come
from upstream source, so a change upstream breaks a test instead of silently drifting."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from cli_anything.rea.core import catalog, deps, source
from cli_anything.rea.core import env as env_core

ROOT = env_core.rea_root()
needs_clone = pytest.mark.skipif(not (ROOT / ".git").is_dir(), reason="~/codes/rea clone absent")


# ---------------------------------------------------------------- env file
def test_env_file_parsing_expands_home_and_ignores_comments(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", "/h")
    f = tmp_path / "rea.env"
    f.write_text('# c\nexport A="$HOME/x"\nexport B="${A}/y"\nnot an export\nexport C="/abs"\n')
    env = env_core.load_env(f)
    assert env["A"] == "/h/x" and env["B"] == "/h/x/y" and env["C"] == "/abs"


def test_env_file_missing_is_an_error(tmp_path: Path):
    with pytest.raises(env_core.ReaError):
        env_core.load_env(tmp_path / "nope.env")


def test_real_env_file_holds_paths_not_credentials():
    f = env_core.env_file()
    if not f.is_file():
        pytest.skip("rea.env absent")
    text = f.read_text(encoding="utf-8")
    assert not re.search(r"(?i)(token|secret|password|api_?key)\s*=", text)


# ---------------------------------------------------------------- catalog parse
SAMPLE = '''
const evidence = effects({ mutatesSession: true });
const browserEvidence = effects({
  mutatesSession: true,
  accessesNetwork: true,
});
export const TOOL_EFFECTS: Readonly<Record<string, ToolEffects>> = {
  a_read: effects(),
  b_ev: evidence,
  c_web: browserEvidence,
  d_run: effects({
    executesForeignCode: true,
    launchesProcess: true,
    idempotent: false,
  }),
  e_one: effects({ mayDiscardData: true }),
};
'''


def test_parse_effects_resolves_presets_and_multiline_entries():
    e = catalog.parse_effects(SAMPLE)
    assert set(e) == {"a_read", "b_ev", "c_web", "d_run", "e_one"}
    assert e["c_web"]["accessesNetwork"] and e["c_web"]["mutatesSession"]
    assert e["d_run"]["executesForeignCode"] and e["d_run"]["idempotent"] is False
    assert e["a_read"]["idempotent"] is True


def test_annotation_rule():
    e = catalog.parse_effects(SAMPLE)
    a = {k: catalog.annotate(v) for k, v in e.items()}
    assert a["a_read"] == {"readOnly": True, "destructive": False, "openWorld": False}
    assert a["b_ev"]["readOnly"] is False  # recording Evidence mutates the session
    assert a["d_run"]["destructive"] and a["d_run"]["openWorld"]
    assert a["e_one"]["destructive"]


def test_unknown_shorthand_is_refused():
    with pytest.raises(env_core.ReaError):
        catalog.parse_effects("export const TOOL_EFFECTS: X = {\n  x: mystery,\n};\n")


@needs_clone
def test_catalog_rule_matches_upstream():
    """Our destructive rule is copied from annotationsFromEffects; fail if upstream changes it."""
    text = catalog.effects_file().read_text(encoding="utf-8")
    body = text[text.index("export const annotationsFromEffects"):]
    assert catalog.DESTRUCTIVE_RULE in " ".join(body.split())


@needs_clone
def test_source_catalog_covers_every_contract():
    """Every tool name in TOOL_EFFECTS parses; count matches the upstream table length."""
    text = catalog.effects_file().read_text(encoding="utf-8")
    block = re.search(r"^export const TOOL_EFFECTS[^=]*=\s*\{\n(.*?)^\};", text, re.S | re.M).group(1)
    names = set(re.findall(r"^  ([a-z_0-9]+):", block, re.M))
    assert {t["name"] for t in catalog.source_catalog()} == names


def test_diff_detects_name_and_annotation_drift():
    src = [{"name": "a", "destructive": True, "readOnly": False}, {"name": "b", "destructive": False, "readOnly": True}]
    rt = {"tools": [{"name": "a", "annotations": {"destructiveHint": False}}, {"name": "c", "annotations": {}}]}
    d = catalog.diff(src, rt)
    assert d["only_in_source"] == ["b"] and d["only_in_runtime"] == ["c"]
    assert d["annotation_mismatch"][0]["name"] == "a" and d["ok"] is False


# ---------------------------------------------------------------- source
@needs_clone
def test_find_uses_pcre_not_ere():
    """git grep -E has no \\s/\\b and silently matched nothing; createBinarySession must be found."""
    hits = source.find("createBinarySession")
    assert any(h["path"] == "src/composition/binary.ts" for h in hits)


@needs_clone
def test_four_deep_providers_registered_in_this_order():
    """Hopper, Ghidra, IDA, Kuna: the registry the `auto`/`ambiguous` rule runs over."""
    text = source.show("src/composition/binary.ts:40", context=40)["lines"]
    joined = "\n".join(text)  # type: ignore[arg-type]
    assert "[hopper, ghidra, ida, kuna]" in joined


@needs_clone
def test_auto_with_two_usable_engines_is_ambiguous_upstream():
    hits = source.grep(r'selectionError\(\s*$|"ambiguous"', paths=("src/application/binary/AnalysisProviderRegistry.ts",))
    assert any('"ambiguous"' in str(h["text"]) for h in hits)


def test_show_rejects_bad_location():
    with pytest.raises(env_core.ReaError):
        source.show("no-line-number")


def test_find_rejects_non_identifier():
    with pytest.raises(env_core.ReaError):
        source.find("a;rm -rf /")


# ---------------------------------------------------------------- deps
def test_fill_requires_every_placeholder():
    assert deps._fill(["{X}/bin", "-v"], {"X": "/a"}) == ["/a/bin", "-v"]
    assert deps._fill(["{MISSING}"], {}) is None


def test_every_tool_row_names_its_source():
    for name, var, argv, src, unlocks in deps.TOOLS:
        assert src and unlocks, name


def test_unkill_ignores_non_macho_and_small_files(tmp_path: Path, monkeypatch):
    nm = tmp_path / "node_modules" / "x"
    nm.mkdir(parents=True)
    script = nm / "tool"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setattr(deps, "_runs", lambda p: 137)
    assert deps.unkill_node_modules(tmp_path) == []
