"""驾驶台判定回放：用 2026-10-17 v1.105.1 那晚真实出现过的读数，钉住「什么时候机器自己走、什么时候必须停」。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console"))

from engine import HUMAN_ONLY, Readings, evaluate, next_split  # noqa: E402


def healthy(split=10, **kw):
    base = dict(phase="normal_gray", split=split, run_id="r", generation="g", minutes_at_split=31, ok_cycles=6,
                bad_cycles=0, gray_requests=400, gray_5xx=0, stable_requests=14000, stable_5xx=90, worker_killed=0,
                restarts_changed=False, monitor_alive=True, noisy_keys_protected=True,
                capacity_status="PASS", capacity_bound_to_run=True, guarded_old_ready="2/2", guarded_old_health=200)
    base.update(kw)
    return Readings(**base)


def test_happy_path_auto_ramps():
    v = evaluate(healthy(split=5))
    assert v.state == "PASS" and v.next_action == "ramp" and v.next_arg == 10


def test_auto_off_never_acts():
    v = evaluate(healthy(split=5), auto_enabled=False)
    assert v.state == "PASS" and v.next_action is None and v.decision


def test_ladder():
    assert [next_split(x) for x in (0, 1, 5, 10, 50, 100)] == [1, 5, 10, 50, 100, None]


def test_100_needs_human_to_converge():
    v = evaluate(healthy(split=100))
    assert v.next_action is None and v.decision["recommend"] == "converge"


def test_not_enough_dwell_waits():
    v = evaluate(healthy(minutes_at_split=12))
    assert v.state == "INCONCLUSIVE" and v.next_action is None and v.decision is None


def test_inconclusive_too_long_escalates_to_human():
    # 10-17 1% 档只有 2 个新版请求：样本永远不够 ⇒ 拖到 2 倍驻留要人决定
    v = evaluate(healthy(split=1, gray_requests=2, minutes_at_split=60))
    assert v.state == "INCONCLUSIVE" and v.decision and v.next_action is None


def test_worker_killed_stops():
    v = evaluate(healthy(worker_killed=1))
    assert v.state == "FAIL" and v.next_action is None and any("worker" in x for x in v.reasons)


def test_gray_5xx_much_higher_stops():
    v = evaluate(healthy(gray_requests=715, gray_5xx=20, stable_requests=484, stable_5xx=0))
    assert v.state == "FAIL"


def test_small_gray_sample_is_not_judged_on_rate():
    v = evaluate(healthy(gray_requests=10, gray_5xx=2, minutes_at_split=31))
    assert v.state == "INCONCLUSIVE"


def test_noisy_keys_not_protected_blocks_ramp():
    # 10-17 run b：carher-hangzhou 没钉旧版 ⇒ 50% 档误触发回退
    v = evaluate(healthy(noisy_keys_protected=False))
    assert v.state == "FAIL"


def test_capacity_from_previous_run_blocks_50():
    # 10-17：读到 10-02 那轮残留 split_capacity.json
    v = evaluate(healthy(split=10, capacity_bound_to_run=False))
    assert v.state == "FAIL" and any("本 run" in x for x in v.reasons)


def test_capacity_not_measured_waits_before_50():
    v = evaluate(healthy(split=10, capacity_status=None))
    assert v.state == "INCONCLUSIVE"


def test_capacity_fail_stops():
    v = evaluate(healthy(split=10, capacity_status="FAIL", capacity_errors=["UNPRICED_CLASS"]))
    assert v.state == "FAIL"


def test_guarded_old_unhealthy_blocks_50():
    v = evaluate(healthy(split=10, guarded_old_ready="1/2"))
    assert v.state == "FAIL"


def test_monitor_dead_blocks():
    v = evaluate(healthy(monitor_alive=False))
    assert v.state == "FAIL"


def test_rolled_back_explains_known_causes():
    v = evaluate(Readings(phase="rolled_back", split=0, last_metrics_reason_codes=["SPEND_RECONCILIATION_FAILED"]))
    assert v.state == "ROLLED_BACK" and "402" in v.decision["what"] and v.next_action is None
    v = evaluate(Readings(phase="rolled_back", split=0, last_metrics_reason_codes=["USER_FAILURE_RATE"]))
    assert "杭州" in v.decision["what"]


def test_unknown_state_never_acts():
    for r in (Readings(phase="normal_gray", split=None), Readings(phase="weird", split=5)):
        v = evaluate(r)
        assert v.next_action is None


def test_machine_never_proposes_human_only_actions_as_auto():
    # 遍历一批读数，机器自动动作只能是 ramp；开始/收敛/提交/回退/关 run 永远只出现在决策卡里
    cases = [healthy(split=s) for s in (1, 5, 10, 50, 100)] + [Readings(phase=p, split=0) for p in
             ("preflight", "committed", "rolled_back", "prod_verified", "convergence_ready")]
    for r in cases:
        v = evaluate(r)
        assert v.next_action in (None, "ramp")
        assert v.next_action not in HUMAN_ONLY


def test_no_answer_default_is_safe():
    for r in (healthy(worker_killed=1), Readings(phase="rolled_back", split=0), healthy(split=100)):
        d = evaluate(r).decision
        assert d and any(w in d["if_no_answer"] for w in ("保持现状", "安全", "不影响", "什么都不做"))
