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


def test_subscription_buckets_expired_only_when_src_is_trusted():
    """2026-08-28 bug 回归：sub_left=expired 且 src∈{off,401:token_expired,
    ERR:403,state,no-pod,FETCH_FAIL} 的行**不算已过期** —— 那些 sub_until 是
    state.json 的 JWT 冻结快照（铸时到期日），探不到当前订阅态，把它们塞进
    "已过期"桶就是把陈旧快照当告警（一整轮 401:token_expired 波动可以把 8+
    活号误报为过期）。可信的到期证据只有 src=live（实探当日）或
    401:invalidated（token 被永久吊销 ≈ 号死）。"""
    rows = [
        _row(acct="acct-90", sub_left="expired", sub_src="401:token_expired"),
        _row(acct="acct-97", sub_left="expired", sub_src="ERR:403"),
        _row(acct="acct-33", sub_left="expired", sub_src="off"),
        _row(acct="acct-82", sub_left="expired", sub_src="state"),
        _row(acct="acct-121", sub_left="expired", sub_src="no-pod"),
        _row(acct="acct-1",  sub_left="expired", sub_src="live"),
        _row(acct="acct-116", sub_left="expired", sub_src="401:invalidated"),
    ]
    b = tl.subscription_buckets(rows)
    # 只有 live / 401:invalidated 进 expired 桶
    assert {ln.split()[0] for ln in b["expired"]} == {"acct-1", "acct-116"}
    # 其余 5 个 sub_left=expired 但 src 不可信 → 进 stale_expired（信息性）
    assert {ln.split()[0] for ln in b["stale_expired"]} == \
        {"acct-90", "acct-97", "acct-33", "acct-82", "acct-121"}


def test_subscription_buckets_d7_only_when_src_is_trusted():
    """同样的道理：135-154 全 src=off、sub_left=5d/6d，日期是 JWT 冻结快照
    （scale=0 号永远探不到），不能塞进 "7 天内到期" 告警桶。"""
    rows = [
        _row(acct="acct-135", sub_left="5d", sub_src="off"),
        _row(acct="acct-2",   sub_left="5d", sub_src="live"),
    ]
    b = tl.subscription_buckets(rows)
    assert {ln.split()[0] for ln in b["d7"]} == {"acct-2"}


# ── 阿里云 scale=0 但 198 同编号在服（2026-10-07 acct-229 误报 offline）────────────

def test_aliyun_scaled_down_with_198_serving_is_marked_migrated():
    ali = [{"acct": "acct-229", "upstream_tier": "SCALED_DOWN", "verdict": "OFFLINE",
            "status": "OFFLINE", "ready": "off"},
           {"acct": "acct-7", "upstream_tier": "SCALED_DOWN", "verdict": "OFFLINE",
            "status": "OFFLINE", "ready": "off"},
           {"acct": "acct-227", "upstream_tier": "TOKEN_X", "verdict": "UPSTREAM:401",
            "status": "TOKEN_X", "ready": "2/2"}]
    r198 = [{"acct": "acct-229", "ready": "2/2", "status": "ONLINE"},
            {"acct": "acct-227", "ready": "2/2", "status": "ONLINE"},
            {"acct": "acct-7", "ready": "off", "status": "OFFLINE"}]
    hit = tl.mark_migrated_to_198(ali, r198)
    assert hit == ["acct-229"]
    assert ali[0]["verdict"] == tl.MIGRATED_VERDICT
    assert ali[0]["status"] == "OFFLINE"          # status 是单选列，不许引入新值
    assert ali[1]["verdict"] == "OFFLINE"          # 198 也停着 ⇒ 真下线，不改
    assert ali[2]["verdict"] == "UPSTREAM:401"     # 阿里云在跑的行不改


def test_aliyun_migrated_rule_ignores_198_paused_or_offline():
    ali = [{"acct": "acct-211", "upstream_tier": "SCALED_DOWN", "verdict": "OFFLINE"}]
    r198 = [{"acct": "acct-211", "ready": "2/2", "status": "OFFLINE"}]
    assert tl.mark_migrated_to_198(ali, r198) == []
    assert ali[0]["verdict"] == "OFFLINE"


# ── 视图盲区：空格子在视图里和「不存在」同形（2026-10-10 acct-227）────────────────

def test_blank_cell_never_satisfies_a_numeric_comparison():
    """`预下线` 视图 filter 是 `7d% > 90`。实探 401 的号 `7d%` 是**空**（三分支逻辑
    刻意置空），空格子不满足任何数值比较 ⇒ 该视图结构性看不见这些行。
    2026-10-10 实测 164 行因此不可见，acct-227 是其中一个，用户据此判断「号不在表里」。"""
    assert tl._cell_matches(None, ">", 90) is False
    assert tl._cell_matches(None, "<", 90) is False      # 空不是 0
    assert tl._cell_matches(100, ">", 90) is True
    assert tl._cell_matches(90, ">", 90) is False
    # 文本占位也不该被当成数字参与比较
    assert tl._cell_matches("-", ">", 90) is False


def test_select_column_filter_value_is_a_list_but_cell_is_scalar():
    """单选列在视图 filter JSON 里写成 `["ONLINE"]`，而行里的格子是裸字符串。
    不脱这层 list，`online` 视图的第一条件会对每一行都判 False ⇒ 自检报「全藏」。"""
    assert tl._cell_matches("ONLINE", "==", ["ONLINE"]) is True
    assert tl._cell_matches("TOKEN_X", "==", ["ONLINE"]) is False
    assert tl._cell_matches("expired", "!=", "expired") is False
    assert tl._cell_matches("7d16h", "!=", "expired") is True


def test_unknown_operator_returns_none_not_a_guess():
    """不认的算子必须回 None（调用方据此跳过整个视图），不能猜 —— 猜错会印出
    一个看着精确的可见行数，而那个数字是编的。"""
    assert tl._cell_matches("x", "contains", "y") is None
    assert tl._cell_matches("x", "isEmpty", None) is False
    assert tl._cell_matches(None, "isEmpty", None) is True
    assert tl._cell_matches("-", "isNotEmpty", None) is False


def test_state_7d_column_is_wired_and_sits_next_to_live_7d():
    """`state_7d%` 承载引擎快照那份 pct —— `7d%` 实探失败时唯一有值的那个。
    没这列时「快照 100% 但探不到」的行在表上两列全空，与「真的没量」同形。"""
    names = [n for n, _ in tl.COLUMNS]
    assert "state_7d%" in names
    assert names.index("state_7d%") == names.index("7d%") + 1
    src = dict(tl.COLUMNS)["state_7d%"]
    assert src == "state_pct7d"
    assert {"name": "state_7d%", "type": "number"} in tl.NEW_FIELD_SPECS


# ── 档位列：空 plan ≠ free（2026-10-10 acct-227 误报）──────────────────────────────

def test_plan_column_is_wired_from_the_live_probe():
    """view 一直在取 `plan`（实探 /codex/usage 的 plan_type）但此前没有列承载它
    ⇒ 整表上没有一处显示档位 ⇒ 读者只能去读 id_token 里冻结的 chatgpt_plan_type。
    acct-227 就是这么被我误报成 free 的（token 10-05 过期，快照说 free，实为 pro）。"""
    names = [n for n, _ in tl.COLUMNS]
    assert "plan" in names
    assert dict(tl.COLUMNS)["plan"] == "plan"
    # text 而非单选：单选列漏补 options 会在 delete-all 之后写入全败 ⇒ 清空整表
    assert {"name": "plan", "type": "text"} in tl.NEW_FIELD_SPECS


def test_plan_plane_never_calls_an_unprobed_row_free(capsys):
    """探不到档位的行必须被单独点名为「探不到」，且输出里要明说空≠free。
    这一段的存在就是为了阻止「快照说 free ⇒ 报掉 free」这条推理再次发生。"""
    idx = {n: i for i, (n, _) in enumerate(tl.COLUMNS)}
    def row(acct, plan, probe):
        r = [None] * len(tl.COLUMNS)
        r[idx["site"]], r[idx["acct"]] = "198", acct
        r[idx["plan"]], r[idx["live_probe"]] = plan, probe
        return r
    rows = [row("acct-226", "pro", "live"),
            row("acct-227", None, "401:token_expired"),   # 真·pro，只是探不到
            row("acct-92", "free", "live")]
    tl.report_plan_plane(rows)
    out = capsys.readouterr().err
    assert "实探 pro (1)" in out and "198/acct-226" in out
    assert "实探 free (1)" in out and "198/acct-92" in out
    # 227 落进「探不到」桶，且不出现在任何一个档位结论里
    assert "1 行**探不到档位**" in out
    assert "空不等于 free" in out
    pro_line = [ln for ln in out.splitlines() if "实探 pro" in ln][0]
    free_line = [ln for ln in out.splitlines() if "实探 free" in ln][0]
    assert "acct-227" not in pro_line and "acct-227" not in free_line


def test_plan_plane_does_not_claim_spendlogs_is_a_verdict(capsys):
    """2026-10-10 对照组推翻了「打过 gpt-6 就是 pro」：实探确认 free 的 acct-120
    也有 591 次 gpt-6 success。自检文案必须把它说成排序信号而不是判据 ——
    否则下一个人会拿 SpendLogs 直接下档位结论，重犯 227 那类错（方向相反）。"""
    idx = {n: i for i, (n, _) in enumerate(tl.COLUMNS)}
    r = [None] * len(tl.COLUMNS)
    r[idx["site"]], r[idx["acct"]] = "198", "acct-227"
    r[idx["live_probe"]] = "401:token_expired"
    tl.report_plan_plane([r])
    out = capsys.readouterr().err
    assert "只有实探一条路" in out
    assert "不是判据" in out
    assert "acct-120" in out and "591" in out
