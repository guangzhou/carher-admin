from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, ROOT / "scripts" / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


add_model = _load("chatgpt-pool-add-codex-model.py", "chatgpt_pool_add_codex_model")
survey = _load("chatgpt-pool-codex-slug-survey.py", "chatgpt_pool_codex_slug_survey")


def row(**kw):
    args = dict(
        acct="165", group="chatgpt-gpt-6-sol", slug="gpt-6-sol", ns="litellm-product",
        api_key="sk-pool-xxx", in_cost=2e-06, out_cost=1e-05, max_in=872000, max_out=128000,
    )
    args.update(kw)
    return add_model.build_row(**args)


# ---------------------------------------------------------------- build_row

def test_litellm_model_is_the_group_name_not_the_slug():
    """写成 openai/<slug> 会让 acct pod 侧报 Invalid model name —— 这一条最容易手写错。"""
    _, body = row()
    assert body["litellm_params"]["model"] == "openai/chatgpt-gpt-6-sol"
    assert body["litellm_params"]["model"] != "openai/gpt-6-sol"


def test_api_key_is_present_because_omitting_it_yields_no_connected_db():
    """漏 api_key ⇒ acct pod 返 'No connected db.'，用户面被打码成「API 异常 (req:)」。"""
    _, body = row()
    assert body["litellm_params"]["api_key"] == "sk-pool-xxx"


def test_costs_live_in_both_litellm_params_and_model_info():
    """只写一处会让计价或展示其中一边为 0。"""
    _, body = row()
    for block in (body["litellm_params"], body["model_info"]):
        assert block["input_cost_per_token"] == 2e-06
        assert block["output_cost_per_token"] == 1e-05


def test_model_info_carries_responses_mode_and_base_model_and_window():
    _, body = row()
    mi = body["model_info"]
    assert mi["mode"] == "responses"
    assert mi["base_model"] == "gpt-6-sol"
    assert mi["max_input_tokens"] == 872000


def test_deployment_id_and_api_base_are_per_account():
    mid_a, body_a = row(acct="165")
    mid_b, body_b = row(acct="208")
    assert mid_a == "chatgpt-acct-165-gpt-6-sol"
    assert mid_b == "chatgpt-acct-208-gpt-6-sol"
    assert body_a["litellm_params"]["api_base"] == (
        "http://chatgpt-acct-165.litellm-product.svc.cluster.local:4000")
    assert body_a["litellm_params"]["api_base"] != body_b["litellm_params"]["api_base"]


def test_all_rows_share_one_group_name():
    """同组内 model_name 必须一致，否则它们不是一个组、不会被一起路由。"""
    assert len({row(acct=a)[1]["model_name"] for a in ("165", "190", "208")}) == 1


def test_script_refuses_gate_off_window_before_touching_the_cluster():
    """max_input_tokens=1e7 是把闸门关掉，不是能力声明。这个守卫必须在任何 kubectl 之前。"""
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "chatgpt-pool-add-codex-model.py"),
         "--slug", "gpt-6-sol", "--targets", "/nonexistent",
         "--in-cost", "1e-06", "--out-cost", "1e-06", "--max-in", "10000000"],
        capture_output=True, text=True, timeout=60)
    assert p.returncode != 0
    assert "闸门" in (p.stdout + p.stderr)


# ---------------------------------------------------------------- survey ruler

def test_result_anchor_must_be_at_line_start():
    """2026-09-23 一条 kubectl websocket 警告混进 stdout，被读成「编造名返 200」。
    先证明这把尺子解析得到干净行，再证明它拒绝被污染的行。"""
    clean = "RESULT\tgpt-6-sol\t200\t1.2\tK7VD2|usage=True"
    assert survey.RESULT_RE.match(clean) is not None       # 阳性对照：自己读得到
    polluted = 'E0923 warn: websocket closed RESULT\tgpt-6-nope\t200\t0.1\tx'
    assert survey.RESULT_RE.match(polluted) is None


def test_two_4xx_faces_are_not_merged():
    """404=付费号拿不到这个名；400=free 档。合并成一个桶就读不出 acct-85 那一课。"""
    assert survey.classify("HTTP404", "") == "404"
    assert survey.classify("HTTP400", "not supported when using Codex") == "400"
    assert survey.classify("HTTP404", "") != survey.classify("HTTP400", "")


def test_empty_200_is_its_own_bucket_not_a_failure():
    """HTTP 200 + 空 SSE 是瞬态节流，必须单独成桶以便复打，不能算成不可用。"""
    assert survey.classify("200", "|usage=False") == "EMPTY200"
    assert survey.classify("200", "K7VD2|usage=True") == "OK"


def test_unknown_status_falls_into_other_rather_than_passing_as_ok():
    """fail-closed：认不出的状态绝不能落进 OK。"""
    for status in ("HTTP500", "EXC", "", "weird"):
        assert survey.classify(status, "whatever") == "OTHER"
