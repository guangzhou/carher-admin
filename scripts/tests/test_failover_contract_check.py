"""judge() 的判据单测：事故形状必须红、目标形状必须绿（不连 198）。"""
import importlib.util
import pathlib

_p = pathlib.Path(__file__).resolve().parents[1] / "litellm-198-failover-contract-check.py"
_spec = importlib.util.spec_from_file_location("fcc", _p)
fcc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fcc)
CHAIN = fcc.CHAIN


def _r(ok, main, hops, errs=()):
    return {"ok": ok, "err": None if ok else "MidStreamFallbackError", "main_sends": main,
            "hops": dict(zip(CHAIN, hops)), "import_errors": list(errs)}


def test_target_shape_passes():
    assert fcc.judge(_r(True, 4, [1] * len(CHAIN)))[0]


def test_incident_shape_fails():
    # 10-20 现场：主组 6 次、链 0 跳、抛错
    ok, why = fcc.judge(_r(False, 6, [0] * len(CHAIN)))
    assert not ok and len(why) == 3


def test_only_raising_limit_still_fails():
    # 只抬 ROUTER_MAX_FALLBACKS 不加 cap：主组 11 次、链 0 跳
    assert not fcc.judge(_r(False, 11, [0] * len(CHAIN)))[0]


def test_success_but_uncapped_fails():
    assert not fcc.judge(_r(True, 7, [1] * len(CHAIN)))[0]


def test_skipped_hop_fails():
    hops = [1] * len(CHAIN); hops[2] = 0
    assert not fcc.judge(_r(True, 4, hops))[0]


def test_import_error_fails_even_if_behaviour_ok():
    assert not fcc.judge(_r(True, 4, [1] * len(CHAIN), ["x.y: ImportError"]))[0]
