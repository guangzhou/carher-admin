from app.state import AccountState, AccountStatus, is_routable, transition


def test_transition_legal():
    s = AccountStatus(name="a")
    assert transition(s, AccountState.COOLING, "5h_window") is True
    assert s.state is AccountState.COOLING
    assert s.last_state_reason == "5h_window"


def test_transition_idempotent_returns_false():
    s = AccountStatus(name="a", state=AccountState.HEALTHY)
    assert transition(s, AccountState.HEALTHY, "noop") is False


def test_transition_illegal_token_invalidated_to_healthy_blocked():
    # reauth 后必须先过 COOLING（等下一轮 wham probe），不允许直接跳 HEALTHY
    s = AccountStatus(name="a", state=AccountState.TOKEN_INVALIDATED)
    assert transition(s, AccountState.HEALTHY, "reauth_done") is False
    assert s.state is AccountState.TOKEN_INVALIDATED
    # 但允许 -> COOLING
    assert transition(s, AccountState.COOLING, "reauth_done_wait_probe") is True


def test_transition_disabled_only_to_cooling():
    s = AccountStatus(name="a", state=AccountState.DISABLED)
    assert transition(s, AccountState.HEALTHY, "enable") is False
    assert transition(s, AccountState.COOLING, "enable") is True


def test_is_routable_blocks_quota_exhausted():
    s = AccountStatus(name="a", primary_used_pct=100.0)
    assert is_routable(s) is False
    s2 = AccountStatus(name="b", secondary_used_pct=100.0)
    assert is_routable(s2) is False


def test_is_routable_blocks_cooldown():
    s = AccountStatus(name="a", cooldown_until=10**12)  # 远未来
    assert is_routable(s) is False


def test_is_routable_passes_healthy_within_quota():
    s = AccountStatus(name="a", primary_used_pct=50, secondary_used_pct=20)
    assert is_routable(s) is True


def test_is_routable_rejects_non_healthy():
    for st in (AccountState.COOLING, AccountState.OFFLINE, AccountState.TOKEN_INVALIDATED, AccountState.DISABLED):
        s = AccountStatus(name="a", state=st)
        assert is_routable(s) is False
