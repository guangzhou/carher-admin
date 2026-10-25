#!/usr/bin/env python3
"""grok_bitable / grok-warp-onboard 登记步骤的纯函数单测（零网络）。

    python3 scripts/grok-onboard/test_grok_bitable.py
"""
import importlib.util
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import grok_bitable as gb  # noqa: E402

spec = importlib.util.spec_from_file_location("onb", os.path.join(HERE, "grok-warp-onboard.py"))
onb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(onb)

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  got=%r want=%r" % (got, want)))


S = {"id": "165", "sub": "uid-1"}
# 空表行 ⇒ 两列都补
check("empty row fills both", gb.id_fields({}, S), ({gb.COL_ID: "165", gb.COL_UID: "uid-1"}, []))
# 已有且相等 ⇒ 不动
check("equal row is a no-op",
      gb.id_fields({gb.COL_ID: "165", gb.COL_UID: [{"text": "uid-1"}]}, S), ({}, []))
# 🔴 已有但不等 ⇒ 只报 mismatch，绝不覆盖（阴性对照）
f, m = gb.id_fields({gb.COL_ID: "99"}, S)
check("mismatch is not overwritten", (gb.COL_ID in f, len(m)), (False, 1))
# sub2api 没 sub ⇒ 不写空串进表
check("missing sub is skipped", gb.id_fields({}, {"id": "7", "sub": ""}), ({gb.COL_ID: "7"}, []))
check("cell joins rich text", gb.cell([{"text": "a"}, {"text": "b"}]), "ab")


def creds(text, pw_login=False):
    fd, p = tempfile.mkstemp()
    os.write(fd, text.encode())
    os.close(fd)
    try:
        return onb.local_creds(types.SimpleNamespace(creds=p, password_login=pw_login))
    finally:
        os.unlink(p)


# 短行：中间段 = x.ai 密码（§C6），末段 sso
check("short line", creds("A@x.com----Gx!pw----eyJsso\n"),
      {"a@x.com": {"sso": "eyJsso", "xai_pw": "Gx!pw"}})
# 8 段长行：sso 在第 8 段，中间那段不是 x.ai 密码 ⇒ 不许当密码写表
check("8-field line has no pw", creds("b@x.com----1----2----3----4----5----uid----eyJs\n"),
      {"b@x.com": {"sso": "eyJs"}})
# --password-login：末段才是 x.ai 密码，没有 sso
check("password-login line", creds("c@x.com----mailpw----xaipw\n", True),
      {"c@x.com": {"xai_pw": "xaipw"}})

print("\n%d failed" % fails)
sys.exit(1 if fails else 0)
