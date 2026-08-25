"""chatgpt_acct_quota_view.py 的纯函数回归测试。

存在的理由（2026-08-02）：表上 acct-121 印 `tier=HEALTHY`，同一时刻 198 上
`deployment/chatgpt-acct-121` 是 `0/1 READY, AVAILABLE=0`，pod 日志每 5min 报
`refresh token failed: 401 auth.openai.com/oauth/token`。用户问「明明有问题为什么
检查不出来」—— 因为表当时只有上游配额平面和凭证平面两个平面，没有服务平面。

这些用例把当晚的真实数据钉成基准，防止 verdict 再退回「上游绿就印 OK」。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "chatgpt_acct_quota_view.py"


def _load():
    spec = importlib.util.spec_from_file_location("quota_view", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


qv = _load()


# --- serving_verdict：核心回归 ------------------------------------------------

def test_acct121_regression_upstream_healthy_but_not_ready():
    """2026-08-02 真实数据：上游 HEALTHY + deploy readyReplicas=0。

    这是本文件存在的唯一理由 —— 改前这一行读出 HEALTHY，改后必须是 SERVING_DEAD。
    """
    serving = {"spec": 1, "ready_n": 0, "samples": 2, "refresh_err": 2}
    assert qv.serving_verdict("HEALTHY", serving) == "SERVING_DEAD"


def test_upstream_healthy_ready_but_refresh_broken():
    """还能服务但 token 刷新已断 —— 不允许读成 OK（早晚掉）。"""
    serving = {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 2}
    assert qv.serving_verdict("HEALTHY", serving) == "REFRESH_DYING"


def test_flapping_is_not_collapsed_to_healthy_or_dead():
    """Ready 抖动必须自成一态。

    当晚 deploy 级与 pod 级两次单点采样结论互斥（112/121 一次 0/1 一次 true），
    单点收敛成任何一个极端都是错的。
    """
    serving = {"spec": 1, "ready_n": 1, "samples": 2, "refresh_err": 0}
    assert qv.serving_verdict("HEALTHY", serving) == "FLAPPING"


def test_all_green_reads_ok():
    serving = {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}
    assert qv.serving_verdict("HEALTHY", serving) == "OK"


def test_scaled_down_is_offline_not_dead():
    """spec=0 是预期内下线，不该和"服务已死"混在一起。"""
    serving = {"spec": 0, "ready_n": 0, "samples": 2, "refresh_err": None}
    assert qv.serving_verdict("SCALED_DOWN", serving) == "OFFLINE"


def test_missing_serving_data_is_unknown_not_ok():
    """取数失败必须是 '?'，绝不能因为上游绿就读成 OK —— 空白会被读成没问题。"""
    assert qv.serving_verdict("HEALTHY", None) == "?"


def test_upstream_problem_passes_through_when_serving_fine():
    serving = {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}
    assert qv.serving_verdict("401 需re-OAuth", serving) == "UPSTREAM:401 需re-OAuth"


# --- device-code 地狱：与 ready/refresh_err 互补，缺一漏一半 --------------------

def test_device_code_hell_detected_while_still_ready():
    """acct-120 形态：dcr 存在。还在 Ready 但每个请求都试 refresh→401→同步阻塞轮询。"""
    serving = {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}
    pod = {"dcr": 1785633579.88, "exp": 1785580844, "jwt_exp": 1785840014}
    assert qv.serving_verdict("HEALTHY", serving, pod) == "DEVICE_CODE_HELL"


def test_device_code_absent_but_serving_dead_still_caught():
    """acct-115/118 形态：dcr=None、exp_gap=0（refresh 已成功过、凭证轴自愈），

    但 readiness 还在事故尾巴上。只用 dcr 判据会漏掉这一类 —— 这是两套判据
    必须并存的实证依据。
    """
    serving = {"spec": 1, "ready_n": 0, "samples": 2, "refresh_err": 2}
    pod = {"dcr": None, "exp": 1786476561, "jwt_exp": 1786476561}
    assert qv.serving_verdict("HEALTHY", serving, pod) == "SERVING_DEAD"


def test_device_code_cell():
    assert qv.device_code_cell({"dcr": 1785633579.88}) == "yes"
    assert qv.device_code_cell({"dcr": None}) == "no"
    assert qv.device_code_cell(None) == "-"


def test_exp_gap_flags_never_refreshed():
    """gap>0 = 从上线起从未成功 refresh 过（onboard 写死 expires_at=issue+7d）。"""
    assert qv.exp_gap_cell({"exp": 1785580844, "jwt_exp": 1785840014}) == 3.0
    # 刷成功一次后两字段被一起改成一致 → gap 归零、永久自愈
    assert qv.exp_gap_cell({"exp": 1786476561, "jwt_exp": 1786476561}) == 0.0
    assert qv.exp_gap_cell(None) is None
    assert qv.exp_gap_cell({"exp": None, "jwt_exp": 1}) is None


def test_exp_gap_normalizes_millisecond_expires_at():
    """acct-66 实测：expires_at 是毫秒、JWT exp 是秒。

    2026-08-02 没归一化时算出 -20656055.3d（≈-5.6万年），而 never_refreshed 判据是
    `>0`，所以这个垃圾值**没触发任何告警、静默混在表里**。归一化后必须与秒/秒同值。
    """
    assert qv.exp_gap_cell({"exp": 1785580844000, "jwt_exp": 1785840014}) == 3.0


def test_exp_gap_rejects_implausible_value():
    """差 60 天以上说明解析仍有问题 —— 置 None 而不是当真值混进表。"""
    assert qv.exp_gap_cell({"exp": 1, "jwt_exp": 1785840014}) is None


def test_diagnostics_reports_device_code_and_never_refreshed():
    rows = [
        {**_row("acct-120", "HEALTHY", "DEVICE_CODE_HELL", "2/2", 0),
         "device_code": "yes", "exp_gap": 3.0},
        {**_row("acct-115", "HEALTHY", "SERVING_DEAD", "0/2", 2),
         "device_code": "no", "exp_gap": 0.0},
        {**_row("acct-100", "HEALTHY", "OK", "2/2", 0),
         "device_code": "no", "exp_gap": 0.0},
    ]
    diag = qv.serving_plane_diagnostics(rows)
    assert [x["acct"] for x in diag["device_code_hell"]] == ["acct-120"]
    assert [x["acct"] for x in diag["never_refreshed"]] == ["acct-120"]
    # 互补性回归：dcr 判据漏掉的 115 必须仍被 serving_dead 抓到
    assert [x["acct"] for x in diag["serving_dead_upstream_ok"]] == ["acct-115"]


def test_serving_dead_outranks_upstream_problem():
    """服务平面盖过上游：撞顶会被引擎 scale=0 摘掉，服务死掉却留在池里当黑洞。"""
    serving = {"spec": 1, "ready_n": 0, "samples": 2, "refresh_err": 0}
    assert qv.serving_verdict("OFFLINE-7D", serving) == "SERVING_DEAD"


# --- ready_cell：分母必须在 ---------------------------------------------------

@pytest.mark.parametrize("serving,expected", [
    ({"spec": 1, "ready_n": 2, "samples": 2}, "2/2"),
    ({"spec": 1, "ready_n": 0, "samples": 2}, "0/2"),
    ({"spec": 1, "ready_n": 1, "samples": 2}, "1/2"),
    ({"spec": 0, "ready_n": 0, "samples": 2}, "off"),
    (None, "-"),
])
def test_ready_cell_always_carries_denominator(serving, expected):
    assert qv.ready_cell(serving) == expected


# --- auth_sync_verdict：'ok' 这个词会骗人 -------------------------------------

def test_same_fingerprint_with_refresh_401_is_both_dead():
    """acct-121：两份指纹一致，但那个 RT 已经被 auth.openai.com 拒了。

    改名前这一格印 'ok'，让"两份都是死的"看起来是绿的。
    """
    same = {"rt": "abc1234567", "exp": 1785600000, "mtime": 1785600000}
    assert qv.auth_sync_verdict(same, dict(same), refresh_err=2) == "both_dead"


def test_same_fingerprint_without_refresh_error_is_same_not_ok():
    same = {"rt": "abc1234567", "exp": 1785600000, "mtime": 1785600000}
    assert qv.auth_sync_verdict(same, dict(same), refresh_err=0) == "same"


def test_local_newer_still_flags_auth_not_in_pvc():
    """引擎只读 pod 那份 —— /Data 更新但没进 PVC 是真故障，不能被新逻辑冲掉。"""
    local = {"rt": "newnewnew1", "exp": 2000000000}
    pod = {"rt": "oldoldold1", "exp": 1000000000}
    assert qv.auth_sync_verdict(local, pod) == "local↑"


def test_no_pod_credential():
    assert qv.auth_sync_verdict({"rt": "x" * 10}, None) == "no-pod"


# --- probe_age：口径是"真探测"不是"写入" --------------------------------------

def test_probe_age_measures_probe_time_not_write_time():
    """acct-121 实测：state.ts=1785606239 对齐 cron.log 最后一条 HEALTHY 01:43:59。

    state.json 每 5min tick 全量重写，但 ts 只在真探测时更新。
    """
    now = 1785606239 + 9 * 60 + 30
    assert qv.probe_age({"ts": 1785606239}, now) == "0h09m"


def test_probe_age_missing_ts():
    assert qv.probe_age({}, 1785606239) == "-"


# --- 平面隔离：诊断必须报出被误杀过的那个交集 --------------------------------

def _row(acct, upstream, verdict, ready, err, pod_cred="9d20h"):
    return {
        "acct": acct, "upstream_tier": upstream, "verdict": verdict,
        "ready": ready, "refresh_err": err, "pod_cred": pod_cred,
        "probe_age": "0h09m", "auth_sync": "same",
        "device_code": "no", "exp_gap": 0.0,
    }


def test_diagnostics_flags_upstream_green_but_serving_dead():
    rows = [
        _row("acct-121", "HEALTHY", "SERVING_DEAD", "0/2", 2),
        _row("acct-100", "HEALTHY", "OK", "2/2", 0),
    ]
    diag = qv.serving_plane_diagnostics(rows)
    assert [x["acct"] for x in diag["serving_dead_upstream_ok"]] == ["acct-121"]


def test_diagnostics_reports_stale_cred_and_unhealthy_intersection():
    """2026-08-02 误杀过的交集：8 个 readyReplicas=0 的号 8/8 都落在 pod_cred 陈旧集合。

    当时我拿 tier=HEALTHY 当对照组，把这个满召回信号判成了噪声。
    """
    rows = [
        _row("acct-121", "HEALTHY", "SERVING_DEAD", "0/2", 2),
        _row("acct-118", "HEALTHY", "REFRESH_DYING", "2/2", 2),
        _row("acct-100", "HEALTHY", "OK", "2/2", 0),
    ]
    diag = qv.serving_plane_diagnostics(rows)
    assert sorted(x["acct"] for x in diag["stale_cred_and_unhealthy"]) == [
        "acct-118", "acct-121"]


def test_diagnostics_reports_unknown_serving_plane():
    """取数失败要报，否则空白被读成没问题。"""
    rows = [_row("acct-77", "HEALTHY", "?", "-", None, pod_cred="-")]
    diag = qv.serving_plane_diagnostics(rows)
    assert [x["acct"] for x in diag["serving_unknown"]] == ["acct-77"]


# --- 上游实探平面（2026-08-05 加）--------------------------------------------
# 用例全部钉 2026-08-05 07:39 UTC 那次 74 个号的真实实探数据。
# 触发这一段存在的事故：在此之前 198 侧 upstream_tier/7d%/reset 是引擎 state.json
# 快照（实测有 4h46m 陈旧），我拿它当"现状"汇报，被当场指出走了缓存。


def test_live_tier_cap_and_brim_are_not_the_same_bucket():
    """`used_pct=100` 与 `allowed=False` 是两个事实，压成一个标签会误伤能用的号。

    2026-08-05 实测：40 个探通的号里 35 个 used_pct=100，但 78/90/92/115/118/119/121
    共 7 个仍是 allowed=True / limit_reached=False —— 官方还在放行。并成"撞顶"
    会导致对这 7 个做下线动作，是净损失。
    """
    capped = {"http": 200, "used_pct": 100, "allowed": False, "limit_reached": True}
    brim = {"http": 200, "used_pct": 100, "allowed": True, "limit_reached": False}
    assert qv.live_tier(1, capped) == "7D_CAP"
    assert qv.live_tier(1, brim) == "7D_BRIM"
    assert qv.live_tier(1, capped) != qv.live_tier(1, brim)


def test_live_tier_thresholds():
    mk = lambda pct: {"http": 200, "used_pct": pct, "allowed": True,
                      "limit_reached": False}
    assert qv.live_tier(1, mk(94)) == "7D_HIGH"
    assert qv.live_tier(1, mk(89)) == "HEALTHY"
    assert qv.live_tier(1, mk(20)) == "HEALTHY"


def test_live_tier_401_keeps_literal_string_for_auth_diagnostics():
    """401 必须回字面 '401 需re-OAuth' —— auth_plane_diagnostics 按它分桶。

    改这个字符串会让「认证没进 PVC / repair_frozen / 需实跑 OAuth」整段静默失效
    （不报错，只是永远空）。
    """
    tier = qv.live_tier(1, {"http": 401, "body": "token is expired"})
    assert tier == "401 需re-OAuth"
    rows = [{"acct": "acct-33", "upstream_tier": tier, "auth_sync": "local↑",
             "pod_cred": "18d11h", "repair_frozen": True, "repair_attempts": 5,
             "probe_age": "live", "state_ts": None, "status": "OFFLINE",
             "cred_after_verdict": False}]
    diag = qv.auth_plane_diagnostics(rows)
    assert [x["acct"] for x in diag["auth_not_in_pvc"]] == ["acct-33"]


def test_live_tier_distinguishes_off_nopod_and_probe_err():
    """三种"没有数值"的原因必须可区分，不能都渲染成空白。"""
    assert qv.live_tier(0, None) == "SCALED_DOWN"
    assert qv.live_tier(1, None) == "NO_POD"
    assert qv.live_tier(1, {"http": 500, "body": "boom"}) == "PROBE_ERR"
    assert qv.live_probe_cell(0, None) == "off"
    assert qv.live_probe_cell(1, None) == "no-pod"
    assert qv.live_probe_cell(1, {"http": 500}) == "ERR:500"


def test_live_401_kinds_split_by_body_fingerprint():
    """19 个 401 的实测分布：token_expired 12 / invalidated 5 /
    could_not_parse 1 / no_access_token 1。四种处置动作不同，不能并成一句。"""
    assert qv.live_401_kind("Provided authentication token is expired.") == "token_expired"
    assert qv.live_401_kind("Your authentication token has been invalidated.") == "invalidated"
    assert qv.live_401_kind("Could not parse your authentication token.") == "could_not_parse"
    assert qv.live_401_kind("KeyError:'access_token'") == "no_access_token"


def test_fetch_fail_falls_back_to_snapshot_and_says_so():
    """实探链路整体失败时退回快照，但必须**显式标注**，不许假装是实探值。"""
    assert qv.live_tier(1, None, fetched=False, state_tier="HEALTHY") == "HEALTHY"
    assert qv.live_probe_cell(1, None, fetched=False) == "FETCH_FAIL"


def test_build_rows_universe_is_state_union_deploy():
    """行宇宙 = state.json ∪ deploy。2026-08-05 实测 198 有 74 个 deploy 而 state
    只有 60 个 key —— 差的 14 个里 acct-2/15 正跑着且官方 401。

    表上没有的行，看表的人不会去找。
    """
    state = {"acct-78": {"tier": "HEALTHY", "primary_pct": 100, "ts": 1785900000}}
    serving = {
        "acct-78": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0},
        "acct-2": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0},
        "acct-1": {"spec": 0, "ready_n": 0, "samples": 2},
    }
    live = {
        "acct-78": {"http": 200, "used_pct": 100, "allowed": True,
                    "limit_reached": False, "plan": "pro", "reset_at": None,
                    "email": "mike_oditgve@mail.com", "extras": [{"pct": 3}]},
        "acct-2": {"http": 401, "body": "Could not parse your authentication token."},
    }
    rows = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={},
                         serving=serving, live=live, live_fetched=True)
    by = {r["acct"]: r for r in rows}
    assert sorted(by) == ["acct-1", "acct-2", "acct-78"]
    assert by["acct-78"]["tracked"] is True
    assert by["acct-2"]["tracked"] is False
    assert by["acct-1"]["tracked"] is False


def test_untracked_is_not_zombie():
    """UNTRACKED 与 ZOMBIE 必须分开：前者是引擎看不见的活号（acct-2 spec=1 且
    官方 401），后者是引擎清理后的残留。混成 ZOMBIE 会暗示"该清行"，方向反了。"""
    assert qv.status({}, tracked=False) == "UNTRACKED"
    assert qv.status({}, tracked=True) == "ZOMBIE"
    assert qv.take({}, tracked=False) == "?"   # 引擎答不了，不能印 '-'


def test_untracked_accounts_are_reported():
    state = {}
    serving = {"acct-2": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}}
    live = {"acct-2": {"http": 401, "body": "Could not parse your authentication token."}}
    rows = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={},
                         serving=serving, live=live, live_fetched=True)
    diag = qv.live_plane_diagnostics(rows)
    assert [x["acct"] for x in diag["untracked"]] == ["acct-2"]
    assert [x["acct"] for x in diag["live_401"]] == ["acct-2"]
    assert rows[0]["live_probe"] == "401:could_not_parse"


def test_build_rows_prefers_live_over_snapshot_and_marks_probe_age_live():
    """实探成功时 7d% 取实探值、probe_age 恒 'live'；引擎快照挪到 state_tier。"""
    state = {"acct-105": {"tier": "HEALTHY", "primary_pct": 3, "ts": 1785900000}}
    serving = {"acct-105": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}}
    live = {"acct-105": {"http": 200, "used_pct": 100, "allowed": False,
                         "limit_reached": True, "plan": "pro",
                         "reset_at": 1786160148, "extras": []}}
    rows = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={},
                         serving=serving, live=live, live_fetched=True)
    r = rows[0]
    assert r["pct7d"] == 100 and r["upstream_tier"] == "7D_CAP"
    assert r["state_tier"] == "HEALTHY" and r["state_pct7d"] == 3
    assert r["probe_age"] == "live"
    assert r["live_probe"] == "live"
    diag = qv.live_plane_diagnostics(rows)
    assert [x["acct"] for x in diag["live_vs_state"]] == ["acct-105"]
    assert [x["acct"] for x in diag["capped"]] == ["acct-105"]


def test_live_vs_state_ignores_pure_rename_noise():
    """`OFFLINE-7D`(引擎) 与 `7D_CAP`/`7D_BRIM`(实探) 说的是同一件事 —— 7d 窗口已满。

    第一版按 tier **字符串**比，74 行里报出 35 个"不一致"，全是我换词表造成的噪声，
    真分歧被埋在里面。比较必须先归粗类。
    """
    state = {"acct-105": {"tier": "OFFLINE-7D", "primary_pct": 100, "ts": 1785900000}}
    serving = {"acct-105": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}}
    live = {"acct-105": {"http": 200, "used_pct": 100, "allowed": False,
                         "limit_reached": True, "extras": []}}
    rows = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={},
                         serving=serving, live=live, live_fetched=True)
    assert rows[0]["upstream_tier"] == "7D_CAP" and rows[0]["state_tier"] == "OFFLINE-7D"
    assert qv.live_plane_diagnostics(rows)["live_vs_state"] == []


def test_live_vs_state_catches_pct_gap_within_same_class():
    """同一粗类内 pct 差 ≥10 仍要报：引擎说 3%、实探说 40%，是真分歧。"""
    state = {"acct-99": {"tier": "HEALTHY", "primary_pct": 3, "ts": 1785900000}}
    serving = {"acct-99": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}}
    live = {"acct-99": {"http": 200, "used_pct": 40, "allowed": True,
                        "limit_reached": False, "extras": []}}
    rows = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={},
                         serving=serving, live=live, live_fetched=True)
    d = qv.live_plane_diagnostics(rows)["live_vs_state"]
    assert [x["acct"] for x in d] == ["acct-99"]
    assert (d[0]["live_pct"], d[0]["state_pct"]) == (40, 3)


def test_probe_failure_leaves_pct_blank_not_zero():
    """探不通时 7d% 必须是空，不能拿快照冒充、也不能落成 0。

    落 0 会让"探不通"和"这周没用量"在表上同形，而两者的处置动作相反。
    """
    state = {"acct-33": {"tier": "TOKEN_INVALID", "primary_pct": 100, "ts": 1785900000}}
    serving = {"acct-33": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}}
    live = {"acct-33": {"http": 401, "body": "token is expired"}}
    rows = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={},
                         serving=serving, live=live, live_fetched=True)
    r = rows[0]
    assert r["pct7d"] is None and r["pct7d_cell"] == "-"
    assert r["state_pct7d"] == 100          # 快照原值仍可见，只是不冒充实探
    assert r["upstream_tier"] == "401 需re-OAuth"


def test_live_fetched_defaults_to_whether_live_data_exists():
    """不传 live 时必须按快照口径，**不能**默认 True。

    默认 True 会让每一行走 live_tier(spec=None, info=None) → SCALED_DOWN、
    live_probe → 'off'，即把「没探」静默渲染成「已下线」。
    """
    state = {"acct-78": {"tier": "HEALTHY", "primary_pct": 42, "ts": 1785900000}}
    rows = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={})
    r = rows[0]
    assert r["live_probe"] == "FETCH_FAIL"
    assert r["upstream_tier"] == "HEALTHY"      # 退回快照，不是 SCALED_DOWN
    assert r["pct7d"] == 42


def test_email_prefers_live_probe_response():
    """实探响应体里的 email 是官方给的，优先级高于本机 .creds 与 id_token。"""
    state = {"acct-78": {"tier": "HEALTHY", "ts": 1785900000}}
    serving = {"acct-78": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}}
    live = {"acct-78": {"http": 200, "used_pct": 10, "allowed": True,
                        "limit_reached": False, "email": "mike_oditgve@mail.com",
                        "extras": []}}
    rows = qv.build_rows(state, now=1785920000, emails={"acct-78": "stale@old.com"},
                         spend_recent={}, zk={}, serving=serving, live=live,
                         live_fetched=True)
    assert rows[0]["email"] == "mike_oditgve@mail.com"


# ── 订阅到期：实探 /accounts/check/v4 优先于冻结的 JWT claim ──────────────────

def test_live_subscription_overrides_stale_state_snapshot():
    """acct-84 实证：state.json 的 subscription_active_until（JWT claim）冻结在旧
    到期日（已过），实探 /accounts/check/v4 返回续订后的未来日期 + will_renew。
    续订必须在表上可见 —— sub_until 取实探值、sub_src=live、will_renew=yes。"""
    state = {"acct-84": {"tier": "HEALTHY", "primary_pct": 3, "ts": 1785900000,
                         "subscription_active_until": 1785920000 - 86400}}  # 已过期
    serving = {"acct-84": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}}
    live = {"acct-84": {"http": 200, "used_pct": 3, "allowed": True,
                        "limit_reached": False, "plan": "pro", "extras": [],
                        "sub_until_live": 1785920000 + 30 * 86400,
                        "has_active": True, "will_renew": True}}
    r = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={},
                      serving=serving, live=live, live_fetched=True)[0]
    assert r["sub_src"] == "live"
    assert r["sub_left"] != "expired"       # 生效日期是未来
    assert r["will_renew"] == "yes"
    # 快照原值仍留档（供对照，不冒充实探）
    assert r["state_sub_until"] != r["sub_until"]


def test_missing_live_subscription_falls_back_to_state_snapshot():
    """实探 200 但没拿到订阅字段时退回 JWT 快照，sub_src=state 如实标明。"""
    state = {"acct-78": {"tier": "HEALTHY", "primary_pct": 10, "ts": 1785900000,
                         "subscription_active_until": 1785920000 + 20 * 86400}}
    serving = {"acct-78": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}}
    live = {"acct-78": {"http": 200, "used_pct": 10, "allowed": True,
                        "limit_reached": False, "extras": []}}   # 无 sub_until_live
    r = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={},
                      serving=serving, live=live, live_fetched=True)[0]
    assert r["sub_src"] == "state"
    assert r["will_renew"] == "-"
    assert r["sub_left"] != "expired"       # 用快照的未来日期


def test_sub_src_marks_fetch_fail_when_whole_link_down():
    """整条实探链路挂了（live_fetched=False）→ sub 退回快照，sub_src=FETCH_FAIL，
    不能把陈旧快照冒充实探值。与 pct7d 的 FETCH_FAIL 同精神。"""
    state = {"acct-78": {"tier": "HEALTHY", "ts": 1785900000,
                         "subscription_active_until": 1785920000 + 20 * 86400}}
    r = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={})[0]
    assert r["sub_src"] == "FETCH_FAIL"
    assert r["will_renew"] == "-"
    assert r["sub_left"] != "expired"


def test_sub_src_carries_401_kind_when_probe_rejected():
    """实探 401 时 sub 退回快照且 sub_src 带上 401:<kind>（与 live_probe 同源），
    读者据此知道这行的订阅日期不是现值。"""
    state = {"acct-33": {"tier": "TOKEN_INVALID", "ts": 1785900000,
                         "subscription_active_until": 1785920000 + 20 * 86400}}
    serving = {"acct-33": {"spec": 1, "ready_n": 2, "samples": 2, "refresh_err": 0}}
    live = {"acct-33": {"http": 401, "body": "token is expired"}}
    r = qv.build_rows(state, now=1785920000, emails={}, spend_recent={}, zk={},
                      serving=serving, live=live, live_fetched=True)[0]
    assert r["sub_src"].startswith("401")
    assert r["sub_src"] == r["live_probe"]


# ── in-pod 解析：accounts/check/v4 的真实嵌套（2026-08-20 实证的回归） ──────────
#
# 这层单测存在的唯一理由：解析逻辑原本内嵌在发往 pod 的字符串常量里，单测够不着，
# 于是「entitlement 在顶层」的错误路径带病上线 —— 76 个测试全绿，线上 18 个 live 号
# 却 sub_src=live 零命中（acct-237 实测 CHECK 200 但退回 state）。现在解析抽成单一源
# `_SUB_PARSE_SRC`（exec 进模块 + 拼进探针串），这里用实测形状把结构钉死。

# 从 acct-237 pod 内实抓的 /accounts/check/v4-2023-04-27 响应结构（截去无关字段）。
_ACCT237_ID = "3a2388fa-1ddd-4c53-adcd-8a52926f756b"
_CHECK_NODE = {
    "account": {"plan_type": "pro", "is_deactivated": False},
    "entitlement": {
        "has_active_subscription": True,
        "subscription_plan": "chatgptpro",
        "expires_at": "2026-09-19T11:40:18+00:00",
    },
    # will_renew 是 entitlement 的**兄弟**键，不在 entitlement 里 —— 旧代码取错层。
    "last_active_subscription": {"will_renew": False, "cancellation_outcome": "deactivate"},
}
_CHECK_RESP = {"accounts": {_ACCT237_ID: _CHECK_NODE, "default": _CHECK_NODE},
               "account_ordering": []}


def test_sub_from_check_reads_nested_account_node():
    """entitlement/account/last_active_subscription 在 accounts[<id>] 下，不在顶层。

    从顶层取（旧 bug）会让每个键都 None → sub_until_live=None → 全表退 state。
    """
    r = qv._sub_from_check(_CHECK_RESP, _ACCT237_ID)
    assert r["has_active"] is True
    assert r["sub_plan_live"] == "chatgptpro"
    assert r["is_deactivated"] is False
    # will_renew 必须从 last_active_subscription 取（acct-237 实测 false）
    assert r["will_renew"] is False
    from datetime import datetime as _dt
    assert abs(r["sub_until_live"]
               - _dt.fromisoformat("2026-09-19T11:40:18+00:00").timestamp()) < 1


def test_sub_from_check_falls_back_to_default_then_first_node():
    """auth.json 的 account_id 对不上 accounts 的 key 时，退 'default'、再退首个节点。"""
    only_default = {"accounts": {"default": _CHECK_NODE}}
    assert qv._sub_from_check(only_default, "mismatch-id")["has_active"] is True
    only_other = {"accounts": {"some-other-id": _CHECK_NODE}}
    assert qv._sub_from_check(only_other, "mismatch-id")["sub_plan_live"] == "chatgptpro"


def test_sub_from_check_empty_response_is_all_none():
    """响应无 accounts（或空）时所有字段 None，绝不抛异常（探针 best-effort）。"""
    r = qv._sub_from_check({}, "x")
    assert r["sub_until_live"] is None and r["will_renew"] is None
    assert qv._sub_from_check({"accounts": {}}, "x")["has_active"] is None
