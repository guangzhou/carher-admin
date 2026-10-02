"""feishu-broadcast 的纯逻辑单测。

重点是「发错人 / 发两遍 / 把空表读成没人」这几类事故，HTTP 管道只测鉴权与确认闸门。
样本数据的形状照抄 2026-10-01 从真实多维表读回来的格子（人员字段是对象列表、
文本字段是 [{"text":..,"type":"text"}]、单选是裸字符串）。
"""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile

import pytest

os.environ.setdefault("BROADCAST_DATA_DIR", tempfile.mkdtemp(prefix="fb-test-"))
_SPEC = importlib.util.spec_from_file_location(
    "feishu_broadcast_server",
    pathlib.Path(__file__).resolve().parents[2] / "broadcast" / "server.py",
)
fb = importlib.util.module_from_spec(_SPEC)
sys.modules["feishu_broadcast_server"] = fb
_SPEC.loader.exec_module(fb)

FIELDS = ["是否在职", "组织名称", "账户类型", "先锋"]


def person(oid, name, email=""):
    return {"id": oid, "name": name, "en_name": name, "email": email}


def rec(p, **fields):
    return {"fields": {"姓名": [p] if p else None, **fields}}


ALICE = person("ou_a", "张三", "a@x.com")
BOB = person("ou_b", "李四")            # 真实表里 192 人没有 email
CAROL = person("ou_c", "王五", "c@x.com")

RECORDS = [
    rec(ALICE, 是否在职="在职", 组织名称="无锡车联", 账户类型=["Cursor"]),
    rec(ALICE, 是否在职="在职", 组织名称="无锡车联", 账户类型=["Claude Code"]),
    rec(BOB, 是否在职="离职", 组织名称="上海车联", 账户类型=["Cursor"], 先锋=["zerokey"]),
    rec(CAROL, 是否在职="在职", 账户类型=["Claude Code"]),   # 组织名称为空
    rec(None, 是否在职="在职"),                               # 没填人的行
]


@pytest.fixture
def people():
    ppl, stats = fb.build_people(RECORDS, "姓名", FIELDS)
    return ppl


# ------------------------------------------------------------ build_people

def test_one_person_many_rows_is_one_recipient():
    ppl, stats = fb.build_people(RECORDS, "姓名", FIELDS)
    assert stats == {"rows": 5, "rows_without_person": 1, "people": 3}
    assert sorted(p["open_id"] for p in ppl) == ["ou_a", "ou_b", "ou_c"]


def test_attrs_are_union_of_rows(people):
    alice = next(p for p in people if p["open_id"] == "ou_a")
    assert alice["attrs"]["账户类型"] == ["Claude Code", "Cursor"]
    assert alice["rows"] == 2


def test_person_without_email_is_still_a_recipient(people):
    # 按邮箱寻址会把这 192 人静默漏掉——寻址必须走 open_id。
    bob = next(p for p in people if p["open_id"] == "ou_b")
    assert bob["email"] == ""


def test_empty_cell_is_filterable_value(people):
    carol = next(p for p in people if p["open_id"] == "ou_c")
    assert carol["attrs"]["组织名称"] == [fb.EMPTY]
    assert carol["attrs"]["先锋"] == [fb.EMPTY]


@pytest.mark.parametrize("cell,expected", [
    (None, [fb.EMPTY]),
    ("", [fb.EMPTY]),
    ([], [fb.EMPTY]),
    ("在职", ["在职"]),
    (["Cursor", "Claude Code"], ["Cursor", "Claude Code"]),
    ([{"text": "buyiitan", "type": "text"}], ["buyiitan"]),
    ([{"name": "张三", "id": "ou_x"}], ["张三"]),
    (3.5, ["3.5"]),
])
def test_cell_values(cell, expected):
    assert fb.cell_values(cell) == expected


def test_filter_options_count_people_not_rows(people):
    opts = fb.filter_options(people, FIELDS)
    # 张三两行都是「在职」，但只算 1 人
    assert opts["是否在职"] == {"在职": 2, "离职": 1}


# ------------------------------------------------------------ select

def ids(ppl):
    return sorted(p["open_id"] for p in ppl)


def test_no_filter_selects_everyone(people):
    assert ids(fb.select(people, {})) == ["ou_a", "ou_b", "ou_c"]


def test_default_excludes_resigned(people):
    assert ids(fb.select(people, fb.DEFAULT_FILTERS)) == ["ou_a", "ou_c"]


def test_same_field_is_or(people):
    sel = fb.select(people, {"组织名称": ["无锡车联", "上海车联"]})
    assert ids(sel) == ["ou_a", "ou_b"]


def test_different_fields_are_and(people):
    sel = fb.select(people, {"是否在职": ["在职"], "账户类型": ["Cursor"]})
    assert ids(sel) == ["ou_a"]


def test_empty_value_list_means_unrestricted(people):
    assert ids(fb.select(people, {"组织名称": []})) == ["ou_a", "ou_b", "ou_c"]


def test_can_select_the_empty_bucket(people):
    assert ids(fb.select(people, {"组织名称": [fb.EMPTY]})) == ["ou_c"]


def test_include_overrides_filter_and_exclude_overrides_include(people):
    sel = fb.select(people, {"是否在职": ["在职"]}, include=["ou_b"])
    assert ids(sel) == ["ou_a", "ou_b", "ou_c"]
    sel = fb.select(people, {"是否在职": ["在职"]}, include=["ou_b"], exclude=["ou_b", "ou_a"])
    assert ids(sel) == ["ou_c"]


def test_unknown_value_selects_nobody(people):
    assert fb.select(people, {"组织名称": ["不存在的公司"]}) == []


# ------------------------------------------------------------ content

def test_render_only_name_placeholder():
    p = {"name": "张三"}
    assert fb.render("{name} 你好", p) == "张三 你好"
    # 正文里的大括号不能让发送炸掉（str.format 会 KeyError）
    assert fb.render('配置 {"a": 1} 和 {other}', p) == '配置 {"a": 1} 和 {other}'


def test_build_content_text():
    t, c = fb.build_content("text", "hi")
    assert t == "text" and json.loads(c) == {"text": "hi"}


def test_build_content_markdown_is_card_with_optional_header():
    t, c = fb.build_content("markdown", "**b**")
    card = json.loads(c)
    assert t == "interactive" and card["schema"] == "2.0"
    assert card["body"]["elements"] == [{"tag": "markdown", "content": "**b**"}]
    assert "header" not in card
    _, c = fb.build_content("markdown", "x", "通知")
    assert json.loads(c)["header"]["title"]["content"] == "通知"


def test_build_content_rejects_unknown_type():
    with pytest.raises(ValueError):
        fb.build_content("image", "x")


# ------------------------------------------------------------ idempotency / errors

def test_uuid_stable_per_root_and_recipient():
    a = fb.recipient_uuid("root1", "ou_a")
    assert a == fb.recipient_uuid("root1", "ou_a")       # 重试复用 ⇒ 飞书去重
    assert a != fb.recipient_uuid("root1", "ou_b")
    assert a != fb.recipient_uuid("root2", "ou_a")       # 新的一次群发不能被当成重复
    assert len(a) <= 50                                   # 飞书 uuid 上限


@pytest.mark.parametrize("code,bucket", [
    (230013, "not_in_scope"),
    (99992361, "cross_app"),
    (99992351, "cross_app"),   # 2026-10-01 e2e 里真实返回的「invalid open_id」
    (99991400, "rate_limited"),
    (fb.NET_ERROR, "network"),
    (230002, "other"),
])
def test_bucket_of(code, bucket):
    assert fb.bucket_of(code) == bucket


@pytest.mark.parametrize("code,status,expected", [
    (fb.NET_ERROR, 0, True),
    (99991400, 400, True),
    (1, 429, True),
    (1, 502, True),
    (230013, 400, False),      # 不在可用范围：重试一万次也一样
    (99992361, 400, False),
])
def test_retryable(code, status, expected):
    assert fb.retryable(code, status) is expected


def test_send_one_retries_network_then_succeeds_with_same_uuid(monkeypatch):
    calls = []

    def fake_api(method, path, body=None):
        calls.append(body["uuid"])
        if len(calls) < 3:
            raise fb.FeishuError(fb.NET_ERROR, "boom")
        return {"data": {"message_id": "om_1"}}

    monkeypatch.setattr(fb, "api", fake_api)
    monkeypatch.setattr(fb.time, "sleep", lambda s: None)
    job = {"root_id": "r", "msg_type": "text", "body": "hi {name}", "title": ""}
    r = {"open_id": "ou_a", "name": "张三", "status": "sending", "attempts": 0}
    fb.send_one(job, r)
    assert r["status"] == "sent" and r["message_id"] == "om_1" and r["attempts"] == 3
    assert len(set(calls)) == 1


def test_send_one_does_not_retry_permanent_error(monkeypatch):
    n = []

    def fake_api(method, path, body=None):
        n.append(1)
        raise fb.FeishuError(230013, "Bot has NO availability to this user.", 400)

    monkeypatch.setattr(fb, "api", fake_api)
    r = {"open_id": "ou_a", "name": "张三", "status": "sending", "attempts": 0}
    fb.send_one({"root_id": "r", "msg_type": "text", "body": "x", "title": ""}, r)
    assert len(n) == 1
    assert r["status"] == "failed" and r["bucket"] == "not_in_scope" and r["code"] == 230013


def test_send_one_renders_name_per_recipient(monkeypatch):
    seen = []
    monkeypatch.setattr(fb, "api", lambda m, p, body=None: seen.append(body) or {"data": {}})
    fb.send_one({"root_id": "r", "msg_type": "text", "body": "{name} 好", "title": ""},
                {"open_id": "ou_a", "name": "张三", "status": "sending"})
    assert json.loads(seen[0]["content"]) == {"text": "张三 好"}
    assert seen[0]["receive_id"] == "ou_a"


# ------------------------------------------------------------ table cache

def _fake_feishu(fields_ok: bool, advanced: bool):
    """records 恒空；字段接口和表元信息按参数决定。"""
    def fake_api(method, path, body=None):
        if "/records/search" in path:
            return {"data": {"items": [], "has_more": False}}
        if "/fields" in path:
            if not fields_ok:
                raise fb.FeishuError(91403, "Forbidden", 403)
            return {"data": {"items": [], "total": 31}}
        return {"data": {"app": {"is_advanced": advanced}}}
    return fake_api


@pytest.mark.parametrize("fields_ok,advanced,hint", [
    (False, False, "EMPTY_HINT_NOT_COLLAB"),
    # 2026-10-01 真实形状：bot 已是「可阅读」协作者，表开了高级权限 ⇒ 字段能读、行 0
    (True, True, "EMPTY_HINT_ADVANCED"),
    (True, False, "EMPTY_HINT_UNKNOWN"),
])
def test_empty_read_is_error_and_names_the_right_layer(monkeypatch, fields_ok, advanced, hint):
    # 0 行一律 code 0。必须当故障报、且不能把「高级权限滤光」报成「没加协作者」——
    # 那正是我第一版的提示，让用户加完协作者后又白等一轮。
    monkeypatch.setattr(fb, "api", _fake_feishu(fields_ok, advanced))
    ppl, stats = fb.TableCache().get(force=True)
    assert ppl == [] and stats["error"] == getattr(fb, hint)


def test_send_refused_while_table_unreadable(monkeypatch):
    monkeypatch.setattr(fb, "api", _fake_feishu(True, True))
    fb.TABLE.fetched_at = 0
    fb.TABLE.get(force=True)
    h = fb.Handler.__new__(fb.Handler)
    with pytest.raises(ValueError, match="高级权限"):
        h._broadcast({"body": "x", "confirm_count": 0})


def test_pagination_reads_all_pages(monkeypatch):
    pages = {"": (RECORDS[:2], True, "p2"), "p2": (RECORDS[2:], False, "")}

    def fake_api(method, path, body=None):
        tok = dict(x.split("=") for x in path.split("?")[1].split("&")).get("page_token", "")
        items, more, nxt = pages[tok]
        return {"data": {"items": items, "has_more": more, "page_token": nxt}}

    monkeypatch.setattr(fb, "api", fake_api)
    ppl, stats = fb.TableCache().get(force=True)
    assert stats["rows"] == 5 and stats["people"] == 3 and stats["error"] == ""


def test_has_more_without_token_refuses_partial_list(monkeypatch):
    monkeypatch.setattr(fb, "api", lambda *a, **k: {
        "data": {"items": RECORDS[:1], "has_more": True, "page_token": ""}})
    ppl, stats = fb.TableCache().get(force=True)
    assert ppl == [] and "分页没有正常结束" in stats["error"]


# ------------------------------------------------------------ auth

def test_login_ok_and_session():
    a = fb.Auth("correct-horse")
    sid, err = a.login("correct-horse", now=0)
    assert sid and not err
    assert a.check(sid, now=1)
    assert not a.check(sid, now=fb.SESSION_TTL + 1)
    assert not a.check("forged", now=1)
    assert not a.check(None, now=1)


def test_login_locks_after_max_fails_even_with_right_password():
    a = fb.Auth("correct-horse")
    for i in range(fb.LOGIN_MAX_FAILS):
        sid, _ = a.login("wrong", now=i)
        assert sid is None
    sid, err = a.login("correct-horse", now=fb.LOGIN_MAX_FAILS)
    assert sid is None and "秒后再试" in err
    sid, _ = a.login("correct-horse", now=fb.LOGIN_MAX_FAILS + fb.LOGIN_LOCK_SECONDS + 1)
    assert sid


def test_old_fails_age_out():
    a = fb.Auth("correct-horse")
    for i in range(fb.LOGIN_MAX_FAILS - 1):
        a.login("wrong", now=i)
    sid, err = a.login("wrong", now=fb.LOGIN_LOCK_SECONDS + 100)
    assert err == "口令错误"            # 之前的失败已过期，没触发锁定


def test_empty_password_never_logs_in():
    sid, _ = fb.Auth("").login("")
    assert sid is None


# ------------------------------------------------------------ message parsing

@pytest.mark.parametrize("body,err", [
    ({"body": "  "}, "不能为空"),
    ({"body": "x", "msg_type": "image"}, "msg_type"),
    ({"body": "x" * (fb.MAX_BODY_CHARS + 1)}, "超过"),
])
def test_parse_message_rejects(body, err):
    with pytest.raises(ValueError, match=err):
        fb._parse_message(body)


def test_parse_selection_drops_unknown_fields():
    f, inc, exc = fb._parse_selection({"filters": {"是否在职": ["在职"], "API Key": ["sk-x"]},
                                       "include": ["ou_a"]})
    assert f == {"是否在职": ["在职"]} and inc == ["ou_a"] and exc == []
