"""阿里云 (carher ns) chatgpt-acct 配额视图的判定逻辑回归。

这套测试存在的理由与 198 那套 (test_chatgpt_acct_quota_view.py) 相同，但要防的是
**把阿里云接进同一张飞书表时把"没有"印成"正常"**：

  · 阿里云没有 quota-rebalance 引擎，但上游配额是**实时探**出来的（pod 内直调
    /codex/usage）。所以 upstream_tier 不该是空的；探针失败时也不该是 HEALTHY。
  · 阿里云 scale=0 的号（实测 acct-7/11/123）必须在表上留一行。走 pod 列表会让
    它们整行消失 —— 「该退役还是该救」的状态正是最需要被记录的。
  · 服务平面（ready/refresh_err）与凭证平面（device_code/exp_gap）缺一漏一半，
    这是 198 侧 2026-08-02 用同一次采样的真实分布证过的。

判定函数必须在**模块层**（不是 render() 里的闭包），否则这里够不着 —— 2026-08-03
把 status_of/take_of/cause_of 提出来就是为了这个。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2] / "scripts"
          / "chatgpt_acct_quota_aliyun_view.py")


def _load():
    spec = importlib.util.spec_from_file_location("aliyun_quota_view", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


av = _load()

NOW = 1786000000.0


def _live(**over):
    """一个健康号的基线：spec=1、两次采样都 ready、无 refresh 错、上游 27%。"""
    row = {
        "acct": "acct-122", "pod": "chatgpt-acct-122-abc", "spec_replicas": 1,
        "ready": True, "ready_n": 2, "ready_samples": 2, "refresh_err": 0,
        "dcr": None, "p7d": 27, "live_plan": "pro", "sub_until": NOW + 20 * 86400,
        "expires_at": int(NOW + 86400), "jwt_exp": int(NOW + 86400),
        "mtime": int(NOW - 3600), "email": "x@mail.com", "probe_err": None,
        # 2026-08-05 加：官方 rate_limit.allowed / limit_reached。缺失时
        # upstream_tier_of 只按 pct 判 → 100% 落 7D_BRIM 而非 7D_CAP，
        # 即「不确定时按仍可服务处理」——误判 CAP 会把能用的号下线（净损失），
        # 误判 BRIM 只是请求失败后由 LiteLLM cooldown 兜住。
        "allowed": True, "limit_reached": False,
    }
    row.update(over)
    return row


# ── scale=0 必须留一行，且与"取不到"区分 ────────────────────────────────────

def test_scaled_down_stays_as_a_row_and_is_not_probe_failure():
    """acct-7/11/123 实测 scale=0。它们必须可判定，而不是变成 PROBE_ERR。

    如果 scale=0 被判成"探针失败"，退役号会永远挂在告警里，真正的探测故障
    就被噪声埋掉了。
    """
    row = {"acct": "acct-123", "spec_replicas": 0, "ready": False, "pod": ""}
    assert av.status_of(row, NOW) == "OFFLINE"
    assert av.upstream_tier_of(row) == "SCALED_DOWN"
    assert av.serving_verdict(row, av.upstream_tier_of(row)) == "OFFLINE"
    assert av.take_of(row, NOW) == "-"


def test_ready_cell_distinguishes_intentional_off_from_no_data():
    """'off'（有意 scale=0）与 '-'（没采到）绝不能混成一个符号。"""
    assert av.ready_cell({"spec_replicas": 0}) == "off"
    assert av.ready_cell({"spec_replicas": 1, "ready_samples": 0}) == "-"
    assert av.ready_cell({"spec_replicas": 1, "ready_n": 2, "ready_samples": 2}) == "2/2"
    assert av.ready_cell({"spec_replicas": 1, "ready_n": 0, "ready_samples": 2}) == "0/2"


def test_ready_cell_always_carries_denominator():
    """单点 ready 是噪声，分母必须在 —— 2/2 与 1/2 是不同的事实。"""
    cell = av.ready_cell({"spec_replicas": 1, "ready_n": 1, "ready_samples": 2})
    assert "/" in cell and cell == "1/2"


# ── 探针失败必须 fail-closed ────────────────────────────────────────────────

def test_probe_failure_is_never_healthy():
    """取不到 ≠ 健康。上游探针挂了就必须说不知道，不能 fall through 成 HEALTHY。

    这一条如果退化，阿里云的号会在探针全挂时集体显示"配额充足"。
    """
    row = _live(probe_err="probe_fail:TimeoutExpired", p7d=None)
    assert av.upstream_tier_of(row) == "PROBE_ERR"
    assert av.status_of(row, NOW) == "PROBE_ERR"
    assert av.take_of(row, NOW) == "-"          # 不接单
    assert av.serving_verdict(row, av.upstream_tier_of(row)) != "OK"


def test_missing_p7d_without_explicit_error_still_fails_closed():
    """探针没报错但 p7d 缺失，也必须是 PROBE_ERR —— 静默缺值最危险。"""
    row = _live(p7d=None)
    assert av.upstream_tier_of(row) == "PROBE_ERR"
    assert av.status_of(row, NOW) == "PROBE_ERR"


def test_token_invalidated_maps_to_reoauth_tier():
    row = _live(probe_err="token_invalidated", p7d=None)
    assert av.upstream_tier_of(row) == "401 需re-OAuth"
    assert av.status_of(row, NOW) == "TOKEN_X"


# ── 服务平面盖过上游（acct-121 那一类）──────────────────────────────────────

def test_upstream_healthy_but_not_ready_is_serving_dead():
    """198 侧 acct-121 的形态：上游 HEALTHY 7d=3%，同时刻 deploy 0/1 READY。

    表不是算错了，是正确地印了一个不完整的事实。服务平面必须盖过上游 ——
    撞顶会被摘掉，服务平面坏掉却一直留在池里当黑洞（Ready=False 绕过 preflight）。
    """
    row = _live(p7d=3, ready=False, ready_n=0, ready_samples=2, refresh_err=2)
    assert av.upstream_tier_of(row) == "HEALTHY"
    assert av.serving_verdict(row, "HEALTHY") == "SERVING_DEAD"


def test_device_code_hell_caught_while_still_fully_ready():
    """198 侧 acct-114 的形态：ready=2/2、rfx=0，只有 dcr 抓得到。

    与下一个用例成对：两个判据各漏一半，不能只留一套。
    """
    row = _live(ready_n=2, ready_samples=2, refresh_err=0, dcr=int(NOW))
    assert av.serving_verdict(row, "HEALTHY") == "DEVICE_CODE_HELL"


def test_serving_dead_caught_when_device_code_absent():
    """198 侧 acct-115 的形态：凭证轴已自愈（无 dcr），但 readiness 还在事故尾巴上。"""
    row = _live(ready_n=0, ready_samples=2, refresh_err=2, dcr=None)
    assert av.serving_verdict(row, "HEALTHY") == "SERVING_DEAD"


def test_refresh_dying_and_flapping_are_distinct_from_ok():
    dying = _live(refresh_err=1)
    assert av.serving_verdict(dying, "HEALTHY") == "REFRESH_DYING"
    flap = _live(ready_n=1, ready_samples=2)
    assert av.serving_verdict(flap, "HEALTHY") == "FLAPPING"


def test_no_serving_samples_is_unknown_not_ok():
    """服务平面没采到就是 '?' —— 空白会被读成没问题。"""
    row = _live(ready_samples=0, ready_n=0)
    assert av.serving_verdict(row, "HEALTHY") == "?"


def test_healthy_row_reads_ok():
    row = _live()
    assert av.serving_verdict(row, av.upstream_tier_of(row)) == "OK"
    assert av.status_of(row, NOW) == "ONLINE"
    assert av.take_of(row, NOW) == "yes"


def test_quota_full_surfaces_as_upstream_not_ok():
    """用满 100% 但官方仍放行 → 7D_BRIM（2026-08-05 词表与 198 侧对齐）。"""
    row = _live(p7d=100)
    assert av.upstream_tier_of(row) == "7D_BRIM"
    assert av.status_of(row, NOW) == "QUOTA"
    assert av.serving_verdict(row, "7D_BRIM") == "UPSTREAM:7D_BRIM"


def test_capped_and_brim_are_distinct_buckets():
    """`used_percent=100` 与 `allowed=False` 是两个事实，压成一档会误伤能用的号。

    198 侧 2026-08-05 实测：35 个 used_pct=100 里 7 个仍 allowed=True /
    limit_reached=False，官方还在放行。两个集群同一列必须同词表。
    """
    brim = _live(p7d=100, allowed=True, limit_reached=False)
    capped = _live(p7d=100, allowed=False, limit_reached=True)
    assert av.upstream_tier_of(brim) == "7D_BRIM"
    assert av.upstream_tier_of(capped) == "7D_CAP"
    assert av.upstream_tier_of(brim) != av.upstream_tier_of(capped)


def test_high_but_not_full_is_its_own_bucket():
    assert av.upstream_tier_of(_live(p7d=94)) == "7D_HIGH"
    assert av.upstream_tier_of(_live(p7d=89)) == "HEALTHY"


# ── exp_gap 毫秒口径 ────────────────────────────────────────────────────────

def test_exp_gap_normalizes_millisecond_expires_at():
    """acct-66 实测：expires_at 是毫秒、JWT exp 是秒。

    没归一化时算出 ≈-5.6 万年，而 never_refreshed 判据是 >0 → 这个垃圾值
    **不触发任何告警、静默混在表里**。归一化后必须与秒/秒同值（3.0）。
    """
    assert av.exp_gap_cell({"acct": "acct-66", "expires_at": 1785580844000,
                            "jwt_exp": 1785840014}) == 3.0


def test_exp_gap_none_when_inputs_missing():
    assert av.exp_gap_cell({"acct": "a", "expires_at": None, "jwt_exp": 123}) is None
    assert av.exp_gap_cell({"acct": "a"}) is None


def test_exp_gap_rejects_absurd_gap():
    """|gap|>60d 是垃圾值，置 None 而不是印出来。"""
    assert av.exp_gap_cell({"acct": "a", "expires_at": 1,
                            "jwt_exp": 1786000000}) is None


# ── build_rows 的诚实性 ────────────────────────────────────────────────────

def test_build_rows_marks_engine_only_columns_as_undefined_not_healthy():
    """阿里云没有的列要标成"无定义"，不能留空让人读成正常。

    · probe_age='live'（本次实探，比 198 的快照更新鲜，绝不能空）
    · auth_sync='no-local'（无 /Data 镜像可比，不是"一致"）
    · restore='-'（auto-revive 是引擎概念）
    · zk_*/codex_* = None（阿里云只跑一档 + zk 未接入）
    """
    rows = av.build_rows(
        [_live()], now=NOW,
        spend_24h={"acct-122": {"calls": 2558, "spend": 164.6}},
        spend_7d={"acct-122": {"calls": 9100, "spend": 601.3}})
    assert len(rows) == 1
    r = rows[0]
    assert r["site"] == "aliyun"
    assert r["probe_age"] == "live"
    assert r["auth_sync"] == "no-local"
    assert r["restore"] == "-"
    assert r["zk_n"] is None and r["codex_n"] is None
    assert r["upstream_tier"] == "HEALTHY"
    assert r["pct7d"] == 27
    assert r["main_n"] == 2558          # 24h 窗口
    assert r["main_n7"] == 9100         # 7d 窗口（2026-08-25 与 24h 并存）
    assert r["codex_n7"] is None        # 阿里云只跑一档，7d 列同样恒空


def test_build_rows_includes_scaled_down_accounts():
    """scale=0 的号必须出现在 rows 里 —— 这是它们从表上消失的那个 bug 的回归。"""
    scaled = {"acct": "acct-7", "spec_replicas": 0, "ready": False, "pod": ""}
    rows = av.build_rows([scaled, _live()], now=NOW, spend_24h={}, spend_7d={})
    accts = [r["acct"] for r in rows]
    assert "acct-7" in accts and len(rows) == 2
    row7 = next(r for r in rows if r["acct"] == "acct-7")
    assert row7["ready"] == "off"
    assert row7["upstream_tier"] == "SCALED_DOWN"
    # scale=0 的号不该被算成"闲置"（它本来就不该有流量）
    assert "idle_24h" not in row7["cause"]


def test_build_rows_device_code_dash_vs_no():
    """'-'（读不到 pod）与 'no'（读到了但没有 dcr）是不同的事实。"""
    rows = av.build_rows(
        [{"acct": "acct-7", "spec_replicas": 0, "ready": False, "pod": ""}, _live()],
        now=NOW, spend_24h={}, spend_7d={})
    by = {r["acct"]: r for r in rows}
    assert by["acct-7"]["device_code"] == "-"
    assert by["acct-122"]["device_code"] == "no"


def test_build_rows_sorted_numerically():
    rows = av.build_rows([_live(acct="acct-122"), _live(acct="acct-11"),
                          _live(acct="acct-7")], now=NOW, spend_24h={}, spend_7d={})
    assert [r["acct"] for r in rows] == ["acct-7", "acct-11", "acct-122"]


def test_pod_cred_age_is_descriptive_only():
    """pod_cred 是"多久没更新"，不做活性断言。

    198 侧实测 9 个 expires_at 已过的号全是 HEALTHY 且 24h 各跑 400~550 calls
    —— pod 内部刷新 access_token 不回写 auth.json。所以这一列不能回答"还能用吗"。
    """
    assert av.fmt_age_epoch(None, NOW) == "-"
    assert av.fmt_age_epoch(int(NOW - 2 * 86400 - 3600), NOW) == "2d01h"
    # 毫秒口径也要能处理
    assert av.fmt_age_epoch(int((NOW - 3600) * 1000), NOW) == "1h00m"


def test_cause_reports_sub_expiry_and_restarts():
    row = _live(sub_until=NOW - 86400, restarts=3)
    cause = av.cause_of(row, NOW, {"acct-122": {"calls": 10}})
    assert "sub_expired" in cause and "restarts=3" in cause
    assert av.status_of(row, NOW) == "SUB_EXP"


# ── 订阅到期：实探 /accounts/check/v4 优先于冻结的 JWT claim ──────────────────

def test_live_subscription_overrides_stale_jwt_claim():
    """acct-84 实证：JWT claim 冻结在旧到期日（已过），但实探 /accounts/check/v4
    返回续订后的未来到期日 + has_active。续订必须在表上可见，不能被 JWT 快照冒充。"""
    row = _live(sub_until=NOW - 86400,                 # JWT claim：已过期
                sub_until_live=NOW + 30 * 86400,       # 实探：续订到未来
                has_active=True, will_renew=True)
    # 生效到期日取实探值 → 不该报 SUB_EXP
    assert av.effective_sub_until(row) == NOW + 30 * 86400
    assert av.status_of(row, NOW) == "ONLINE"
    assert "sub_expired" not in av.cause_of(row, NOW, {"acct-122": {"calls": 10}})
    r = av.build_rows([row], now=NOW, spend_24h={}, spend_7d={})[0]
    assert r["sub_src"] == "live"
    assert r["sub_left"] != "expired"
    assert r["will_renew"] == "yes"


def test_missing_live_subscription_falls_back_to_jwt_and_marks_state():
    """实探拿不到订阅（accounts/check 失败）时退回 JWT 快照，且 sub_src=state
    如实标明来源 —— 不能默认 live，也不能把退回的快照冒充实探值。"""
    r = av.build_rows([_live()], now=NOW, spend_24h={}, spend_7d={})[0]   # 基线无 sub_until_live
    assert r["sub_src"] == "state"
    assert r["will_renew"] == "-"
    # sub_until 仍反映 JWT 快照（NOW+20d）
    assert r["sub_left"] != "expired"


def test_active_but_will_not_renew_is_shown():
    """acct-237 实证：has_active=true 但 will_renew=false（快到期且不续）。
    活跃不等于会续订，will_renew 必须如实呈现 no，且此刻不报 SUB_EXP。"""
    row = _live(sub_until_live=NOW + 5 * 86400, has_active=True, will_renew=False)
    r = av.build_rows([row], now=NOW, spend_24h={}, spend_7d={})[0]
    assert r["sub_src"] == "live"
    assert r["will_renew"] == "no"
    assert av.status_of(row, NOW) == "ONLINE"


def test_scaled_down_sub_src_is_off_not_state():
    """scale=0 的号没 pod 可探，sub_src 是 off（探不到）而非 state（退回快照）——
    两者语义不同：state 是"探了但订阅没拿到"，off 是"根本没探"。"""
    scaled = {"acct": "acct-7", "spec_replicas": 0, "ready": False, "pod": "",
              "sub_until": NOW + 20 * 86400}
    r = av.build_rows([scaled], now=NOW, spend_24h={}, spend_7d={})[0]
    assert r["sub_src"] == "off"
    assert r["will_renew"] == "-"


# ── in-pod 解析：accounts/check/v4 的真实嵌套（2026-08-20 实证的回归） ──────────
#
# 见 198 侧同名测试的说明：解析原内嵌在探针字符串里、单测够不着，「entitlement 在
# 顶层」的错误路径带病上线，18 个 live 号 sub_src=live 零命中。两个 view 各自一份
# 解析（自包含脚本，远端无本 repo），所以两边都要用实测形状钉死。

_CHECK_NODE = {
    "account": {"plan_type": "pro", "is_deactivated": False},
    "entitlement": {
        "has_active_subscription": True,
        "subscription_plan": "chatgptpro",
        "expires_at": "2026-09-19T11:40:18+00:00",
    },
    "last_active_subscription": {"will_renew": False, "cancellation_outcome": "deactivate"},
}
_CHECK_RESP = {"accounts": {"acct-uuid": _CHECK_NODE, "default": _CHECK_NODE}}


def test_sub_from_check_reads_nested_account_node():
    """entitlement/account/last_active_subscription 在 accounts[<id>] 下，不在顶层。"""
    r = av._sub_from_check(_CHECK_RESP, "acct-uuid")
    assert r["has_active"] is True
    assert r["sub_plan_live"] == "chatgptpro"
    assert r["is_deactivated"] is False
    assert r["will_renew"] is False        # 从 last_active_subscription 取
    from datetime import datetime as _dt
    assert abs(r["sub_until_live"]
               - _dt.fromisoformat("2026-09-19T11:40:18+00:00").timestamp()) < 1


def test_sub_from_check_falls_back_and_handles_empty():
    """account_id 对不上时退 default/首个；空响应全 None 且不抛。"""
    assert av._sub_from_check({"accounts": {"default": _CHECK_NODE}}, "x")["has_active"] is True
    assert av._sub_from_check({}, "x")["sub_until_live"] is None
