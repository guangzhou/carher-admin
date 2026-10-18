"""回归结果解析：用 10-17 v1.105.1 灰度时真实跑出来的输出做 fixture，钉住 PASS / 同形 / 不过 的判定。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console"))

import regress  # noqa: E402

FX = Path(__file__).resolve().parent / "fixtures" / "console-regress"


def _r(name):
    return (FX / name).read_text()


def test_every_case_documents_what_pass_and_why():
    for c in regress.CASES:
        assert c["what"] and c["pass"] and c["why"], c["id"]


def test_sweep_one_sided_is_fail():
    # 10-17 第一次并排：gray 多了 4 个单边（kiro-sonnet-5-5 400 + deepseek 重复文本）
    r = regress.parse("sweep", {"A|B": _r("sweep1.txt")})
    assert r["status"] == "FAIL" and "单边 4" in r["summary"] and r["details"]


def test_sweep_same_failures_both_sides_is_same_fail():
    # 100% 后：66/69 = 66/69、0 单边；3 个红两边都是 cursor-fc-grok-4.6-low 503
    r = regress.parse("sweep", {"A|B": _r("sweep-100.txt")})
    assert r["status"] == "SAME_FAIL" and "单边 0" in r["summary"]


def test_kiro_both_full_is_pass():
    r = regress.parse("kiro", {"A": _r("kiro-gray.txt"), "B": _r("kiro-prod.txt")})
    assert r["status"] == "PASS" and "63/63" in r["summary"]


def test_budget_same_failures_is_same_fail():
    # 旧用例 S4~S6 两边同样红 = 用例过期，不是版本问题
    t = _r("bn-prod.txt")
    r = regress.parse("budget", {"A": t, "B": t})
    assert r["status"] == "SAME_FAIL"


def test_budget_one_side_fail_is_fail():
    t = _r("bn-prod.txt")
    ok = "\n".join(l for l in t.splitlines() if l.startswith("PASS")) + "\n8/8 PASS\n"
    r = regress.parse("budget", {"A": t, "B": ok})
    assert r["status"] == "FAIL"


def test_key_behavior():
    good = "  grok-4.7 new 200 OK\n✅ 行为层通过\n"
    bad = "  claude-opus-5 old(阳性) 403 0.01s RED 期望 200，得 403\n"
    assert regress.parse("key_behavior", {"A:cursor": good, "B:cursor": good})["status"] == "PASS"
    r = regress.parse("key_behavior", {"A:cursor": bad, "B:cursor": good})
    assert r["status"] == "FAIL" and any("RED" in d for d in r["details"])


def test_auth_parity():
    assert regress.parse("auth_parity", {"A|B": "names checked 600 auth one-sided 0 (A403,B403) {}"})["status"] == "PASS"
    r = regress.parse("auth_parity", {"A|B": "  AUTH DIFF claude anthropic.claude-haiku-4-5 A=403 B=200\nnames checked 600 auth one-sided 1"})
    assert r["status"] == "FAIL" and r["details"]


def test_codex_random_reasoning_is_not_a_failure_but_compaction_is():
    diff_ok = ("zk.compact  same\nds.normal  CHANGED\n变了: ['ds.normal']\n没变: ['zk.compact']\n")
    assert regress.parse("codex", {"diff": diff_ok})["status"] == "PASS"
    diff_bad = ("zk.compact  CHANGED\n变了: ['zk.compact']\n")
    assert regress.parse("codex", {"diff": diff_bad})["status"] == "FAIL"


def test_missing_summary_is_error_not_pass():
    for case in ("sweep", "auth_parity", "kiro", "budget", "codex"):
        logs = {"A": "Traceback ...", "B": "Traceback ...", "A|B": "Traceback ...", "diff": "Traceback ..."}
        assert regress.parse(case, logs)["status"] == "ERROR", case
