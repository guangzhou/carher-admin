from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "litellm-198-her-name-probe.py"
SPEC = importlib.util.spec_from_file_location("litellm_198_her_name_probe", SCRIPT)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def ok(content, finish="stop"):
    return {"status": 200, "model_id": "x",
            "body": {"choices": [{"message": {"content": content}, "finish_reason": finish}]}}


def test_default_prompt_is_arithmetic_not_repeat_token():
    # 2026-09-29 grok-4.7 refused every "reply with exactly this token" prompt.
    prompt = probe.make_prompt("arith", "", 123, 456)
    assert "123*456" in prompt and "token" not in prompt


def test_nonce_prompt_is_still_available():
    assert "n-1" in probe.make_prompt("nonce", "n-1", 1, 2)


def test_refusal_is_a_miss_not_ok():
    # Positive control for the 09-29 false red: 200 + refusal text must read MISS,
    # so a nonce-mode run against grok-4.7 stays visibly red, not silently green.
    note = probe.verdict(ok("I can't comply with requests to output an exact token"), "hp47-1")
    assert note.startswith("MISS")


def test_arith_answer_with_thousands_separator_is_ok():
    assert probe.verdict(ok("71,883"), "71883") == "OK"


def test_empty_content_from_length_cutoff_is_a_miss():
    assert probe.verdict(ok("", finish="length"), "71883").startswith("MISS finish=length")


def test_non_200_and_missing_choices():
    assert probe.verdict({"status": 403, "model_id": "", "body": "denied"}, "1") == "HTTP_403"
    assert probe.verdict({"status": 200, "model_id": "", "body": {"choices": []}}, "1") == "NO_CHOICES"
