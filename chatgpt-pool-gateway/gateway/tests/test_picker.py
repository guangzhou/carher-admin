from app.picker import pick
from app.state import AccountState


def test_pick_picks_lowest_priority(make_account):
    a = make_account(name="hi-prio", priority=200, primary=0)
    b = make_account(name="lo-prio", priority=10, primary=0)
    res = pick([a, b])
    assert res.account is not None
    assert res.account.name == "lo-prio"
    assert "priority=10" in res.reason


def test_pick_tiebreaks_on_primary_used(make_account):
    cold = make_account(name="cold", priority=100, primary=5.0)
    warm = make_account(name="warm", priority=100, primary=80.0)
    res = pick([cold, warm])
    assert res.account.name == "cold"


def test_pick_skips_quota_capped(make_account):
    full = make_account(name="full", primary=100)
    ok = make_account(name="ok", primary=20)
    res = pick([full, ok])
    assert res.account.name == "ok"
    assert res.rejected["full"].startswith("primary_window")


def test_pick_returns_none_when_all_blocked(make_account):
    a = make_account(name="a", state=AccountState.COOLING)
    b = make_account(name="b", state=AccountState.OFFLINE)
    res = pick([a, b])
    assert res.account is None
    assert res.reason == "no_routable_account"
    assert set(res.rejected.keys()) == {"a", "b"}


def test_pick_skips_cooldown(make_account):
    iced = make_account(name="iced", cooldown_until=10**12)
    fresh = make_account(name="fresh")
    res = pick([iced, fresh])
    assert res.account.name == "fresh"


def test_pick_prefers_more_recent_probe(make_account):
    a = make_account(name="a", last_probe_at=100.0)
    b = make_account(name="b", last_probe_at=200.0)
    res = pick([a, b])
    # 同 priority/primary/secondary，last_probe_at 越新越优
    assert res.account.name == "b"
