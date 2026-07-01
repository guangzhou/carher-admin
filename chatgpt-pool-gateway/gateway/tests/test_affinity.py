from app.affinity import AffinityMap, extract_conv_id, AFFINITY_TTL_S


def test_set_get_roundtrip():
    m = AffinityMap()
    m.set("conv-1", "acct-3", now=1000.0)
    assert m.get("conv-1", now=1000.0) == "acct-3"


def test_get_expired_returns_none_and_evicts():
    m = AffinityMap(ttl_s=60)
    m.set("conv-1", "acct-3", now=1000.0)
    assert m.get("conv-1", now=1100.0) is None
    # 二次 get 也 None (已 evict)
    assert m.get("conv-1", now=1050.0) is None


def test_get_missing_conv_id():
    m = AffinityMap()
    assert m.get(None) is None
    assert m.get("") is None
    assert m.get("missing") is None


def test_drop_clears_entry():
    m = AffinityMap()
    m.set("c", "a", now=1.0)
    m.drop("c")
    assert m.get("c", now=2.0) is None


def test_gc_removes_only_expired():
    m = AffinityMap(ttl_s=60)
    m.set("alive", "a1", now=1000.0)
    m.set("dead", "a2", now=900.0)
    removed = m.gc(now=1000.0)
    assert removed == 1
    assert m.get("alive", now=1010.0) == "a1"


def test_extract_conv_id_from_metadata_priority():
    body = {"metadata": {"conversation_id": "C-1", "session_id": "S-2"}}
    assert extract_conv_id(body) == "C-1"


def test_extract_conv_id_falls_back_to_session_id():
    body = {"metadata": {"session_id": "S-3"}}
    assert extract_conv_id(body) == "S-3"


def test_extract_conv_id_from_header_when_no_meta():
    assert extract_conv_id({}, {"X-Conversation-Id": "H-1"}) == "H-1"
    assert extract_conv_id({}, {"x-session-id": "H-2"}) == "H-2"
    assert extract_conv_id({}, {}) is None


def test_t3_five_turn_same_conv_same_acct():
    """T3: 5 turn 同 conv_id 应黏同 acct."""
    m = AffinityMap()
    m.set("C", "acct-7", now=0)
    chosen = [m.get("C", now=t) for t in range(5)]
    assert chosen == ["acct-7"] * 5
