#!/usr/bin/env python3
"""Offline cases for grok_reset_card.py's wire parsing (2026-10-14).

Why this file exists: on 10-14 the real "no cards" reply was 25 bytes — an
empty data frame plus a 0x80 trailer frame carrying `grpc-status:0` — with NO
grpc-status in the HTTP headers.  A parser that only reads headers sees
"status missing" and either crashes or (worse) defaults to success.  So both
faces are pinned here:

  - the live empty reply must read as status 0 + zero tokens (NONE)
  - a trailers-only auth error (header status 7, empty body) must read as 7
  - a reply with neither must read as None, never as success
  - a populated reply must decode tokenId / grantedAt / expiresAt

Run: python3 test_grok_reset_card.py   (no network)
"""
import importlib.util
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("g", os.path.join(HERE, "grok_reset_card.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

# verbatim bytes captured from grok.com for acct 74/75 on 2026-10-14
LIVE_EMPTY = bytes.fromhex("0000000000" "800000000f") + b"grpc-status:0\r\n"


def ld(fn, b):
    return g.varint((fn << 3) | 2) + g.varint(len(b)) + b


def tsmsg(sec):
    return g.varint(1 << 3) + g.varint(sec)


def test_live_empty_is_status0_no_tokens():
    data, tr = g.unframe(LIVE_EMPTY)
    assert data == b"" and g.grpc_status({}, tr) == ("0", "")
    assert g.decode_tokens(data) == []


def test_trailers_only_auth_error():
    st, _ = g.grpc_status({"grpc-status": "7", "grpc-message": "bad"}, g.unframe(b"")[1])
    assert st == "7"


def test_missing_status_is_not_success():
    assert g.grpc_status({}, {})[0] is None


def test_populated_tokens_decode():
    tok = ld(10, b"restok_abc") + ld(20, tsmsg(1786000000)) + ld(30, tsmsg(1789000000))
    raw = g.frame(ld(10, tok)) + LIVE_EMPTY[5:]
    data, tr = g.unframe(raw)
    toks = g.decode_tokens(data)
    assert g.grpc_status({}, tr)[0] == "0"
    assert len(toks) == 1 and toks[0]["id"] == "restok_abc"
    assert toks[0]["expires"].year == 2026 and toks[0]["granted"] < toks[0]["expires"]


def test_redeem_request_matches_orca_wire():
    # orca's own test: encodeRedeemResetRequest('restok_INVALID') → 52 0e + ascii
    assert g.redeem_req("restok_INVALID").hex() == "520e" + b"restok_INVALID".hex()


def test_empty_request_frame_is_five_zero_bytes():
    assert g.frame() == b"\x00" * 5 and struct.unpack(">I", g.frame(b"ab")[1:5])[0] == 2


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_"):
            f()
            n += 1
            print("ok", k)
    print("%d passed" % n)
