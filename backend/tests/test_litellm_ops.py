"""Pin invariants on the per-instance LiteLLM key allowlist.

Background (2026-05-14 incident):
admin's `_BASE_MODELS` drifted from the live cluster's per-key allowlist.
Any `update_key`/`generate_key` call would silently strip aliases the her
runtime depends on (claude-haiku-4-5, wangsu-text-embedding-v3 — the bge-m3
embedding fallback installed during the OpenRouter outage rescue, and
anthropic.claude-opus-4-7 — the admin/anthropic-compat endpoint alias),
producing "Something went wrong" replies once the user picked one of those
models. These tests fail loudly the next time someone trims the list.
"""

from __future__ import annotations

from backend.litellm_ops import ALL_MODELS, MODEL_FALLBACK_MAP, _BASE_MODELS


REQUIRED_ALIASES = {
    # Core chat models actively routed by carher user-config aliases.
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gemini-3.1-pro-preview",
    "minimax-m2.7",
    "glm-5",
    "wangsu-gpt-5.5",
    "wangsu-deepseek-v4-pro",
    "wangsu-deepseek-v4-flash",
    # Embedding for memorySearch + the wangsu fallback installed
    # 2026-05-13 to ride out OpenRouter bge-m3 outages.
    "BAAI/bge-m3",
    "wangsu-text-embedding-v3",
    # admin/anthropic-compat endpoint exposes this alias; stripping it
    # 401s the admin-side health probe and any anthropic-SDK caller.
    "anthropic.claude-opus-4-7",
    "anthropic.claude-opus-4-8",
    "openrouter-claude-opus-4-8",
}


def test_all_models_contains_every_required_alias():
    missing = REQUIRED_ALIASES - set(ALL_MODELS)
    assert not missing, (
        f"ALL_MODELS missing required aliases {sorted(missing)} — "
        "any admin update_key/generate_key call would now strip them "
        "from the per-instance LiteLLM key allowlist (carher-NNN), "
        "reproducing the 2026-05-14 carher-2/11/30 'Something went wrong' "
        "incident."
    )


def test_all_models_includes_every_fallback_target():
    """Every fallback target must be in ALL_MODELS or LiteLLM rejects the
    fallback hop on the per-key authz check, defeating router_settings."""
    missing = set(MODEL_FALLBACK_MAP.values()) - set(ALL_MODELS)
    assert not missing, (
        f"Fallback targets not whitelisted: {sorted(missing)}"
    )


def test_all_models_is_deduplicated():
    assert len(ALL_MODELS) == len(set(ALL_MODELS)), (
        "ALL_MODELS contains duplicates — LiteLLM tolerates this but it "
        "indicates _BASE_MODELS / MODEL_FALLBACK_MAP overlap that should "
        "be reconciled."
    )


def test_base_models_has_no_accidental_anthropic_prefix_dupes():
    """Catch the d4220dc-style regression where a `litellm/anthropic.X`
    user-config alias and a bare `X` allowlist entry coexist for the same
    underlying model — the user-config alias would 401 against the key."""
    bare = {m for m in _BASE_MODELS if not m.startswith("anthropic.")}
    for m in _BASE_MODELS:
        if m.startswith("anthropic."):
            stem = m[len("anthropic."):]
            # anthropic.claude-opus-4-7 + claude-opus-4-7 IS the intended
            # state for the admin endpoint — only flag stems we did NOT
            # also list bare. (i.e. test fires if someone adds
            # anthropic.claude-haiku-4-5 alone without the bare alias.)
            assert stem in bare, (
                f"{m!r} present without bare alias {stem!r} — "
                "user-config aliases routed through the bare name will "
                "401 against this key."
            )
