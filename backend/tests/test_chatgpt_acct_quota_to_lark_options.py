import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location(
    "tl", pathlib.Path("scripts/chatgpt_acct_quota_to_lark.py"))
tl = importlib.util.module_from_spec(spec); spec.loader.exec_module(tl)


def test_missing_select_options_finds_new_enum_values():
    """2026-08-05 事故的最小复现：status 新增 UNTRACKED、take 新增 '?'，
    而飞书单选列 options 里没有 → 批量写全部被拒 (code 800030005)，
    而删除已经执行完了 → 表被清空。"""
    status = {"name": "status", "options": [{"name": "ONLINE"}, {"name": "OFFLINE"}]}
    assert tl.missing_select_options(status, {"ONLINE", "UNTRACKED"}) == ["UNTRACKED"]
    take = {"name": "take", "options": [{"name": "yes"}, {"name": "-"}]}
    assert tl.missing_select_options(take, {"yes", "-", "?"}) == ["?"]


def test_missing_select_options_empty_when_covered():
    f = {"name": "status", "options": [{"name": "ONLINE"}, {"name": "UNTRACKED"}]}
    assert tl.missing_select_options(f, {"ONLINE", "UNTRACKED"}) == []
    assert tl.missing_select_options(f, set()) == []


def test_missing_select_options_ignores_none_and_blank():
    f = {"name": "take", "options": [{"name": "yes"}]}
    assert tl.missing_select_options(f, {"yes", ""}) == []


def test_hues_are_all_accepted_by_lark():
    """'Violet' 实测被飞书拒：Invalid enum value。别凭印象写颜色名。"""
    assert "Violet" not in tl.LARK_HUES
    assert set(tl.LARK_HUES) == {"Red", "Orange", "Yellow", "Lime", "Green",
                                 "Turquoise", "Wathet", "Blue", "Carmine",
                                 "Purple", "Gray"}


def test_normalize_aliyun_live_fields_is_explicit_not_blank():
    """阿里云行必须显式标 live_probe，留白与 'live' 在表上同形 —— 会让读者以为
    阿里云那半边没实探过，而真相恰好相反。"""
    r = {"acct": "acct-122", "upstream_tier": "HEALTHY", "ready": "2/2", "pct7d": 71}
    tl.normalize_aliyun_live_fields(r)
    assert r["live_probe"] == "live"
    assert r["state_tier"] == "no-engine"      # 无引擎≠取不到快照
    off = {"acct": "acct-7", "upstream_tier": "SCALED_DOWN", "ready": "off",
           "pct7d": None}
    tl.normalize_aliyun_live_fields(off)
    assert off["live_probe"] == "off"


# ── 订阅到期专项分桶（2026-08-25 加）─────────────────────────────────────────

def _row(**over):
    """按 COLUMNS 顺序造一行 list（默认健康：live、30d、will_renew=yes）。"""
    d = {name: None for name, _ in tl.COLUMNS}
    d.update({"site": "198", "acct": "acct-1", "sub_until": "2026-09-20 13:31 UTC",
              "sub_left": "30d", "sub_src": "live", "will_renew": "yes"})
    d.update(over)
    return [d[name] for name, _ in tl.COLUMNS]


def test_subscription_buckets_catches_expired_and_d7_and_no_renew():
    """三类都是要人工处置的信号，漏一类就是漏一个 $200/月 的号。
    分桶不排斥：≤7d 且 will_renew=no 的号两个桶都要出现（最该看的组合）。"""
    rows = [
        _row(acct="acct-1", sub_left="expired", sub_until="2026-08-20 07:31 UTC"),
        _row(acct="acct-2", sub_left="5d", will_renew="no"),
        _row(acct="acct-3", sub_left="30d"),
    ]
    b = tl.subscription_buckets(rows)
    assert len(b["expired"]) == 1 and "acct-1" in b["expired"][0]
    assert len(b["d7"]) == 1 and "acct-2" in b["d7"][0]
    assert len(b["no_renew"]) == 1 and "acct-2" in b["no_renew"][0]


def test_subscription_buckets_seven_days_is_exclusive_boundary():
    """7d 整不算"7 天内"（与 aliyun view 的 sub<7d 同边界），6d 才算。"""
    b = tl.subscription_buckets([_row(acct="acct-9", sub_left="7d"),
                                 _row(acct="acct-8", sub_left="6d")])
    assert len(b["d7"]) == 1 and "acct-8" in b["d7"][0]


def test_subscription_buckets_marks_stale_src_rows():
    """sub_src≠live 且带日期的行必须被点名 —— 冻结快照的日期不反映续订
    （2026-08-20 acct-84 实证：快照说 expired，实探是续订到下月）。"""
    rows = [_row(acct="acct-4", sub_src="off"),
            _row(acct="acct-5", sub_src="state"),
            _row(acct="acct-6", sub_src="live")]
    b = tl.subscription_buckets(rows)
    assert set(b["stale_src"]) == {"acct-4", "acct-5"}


def test_subscription_buckets_aliyun_rows_are_tagged_with_site():
    """两个集群同表，brief 里必须能看出是哪个集群的号。"""
    b = tl.subscription_buckets([_row(site="aliyun", acct="acct-122",
                                      sub_left="expired")])
    assert "acct-122@aliyun" in b["expired"][0]
