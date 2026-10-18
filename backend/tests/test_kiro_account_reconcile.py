"""门禁：`kiro-account-reconcile.py` 判「在不在池」不许退回到只比邮箱。

2026-10-19 的坑：核查飞书表 59~81 行时我**只按邮箱**比对表和池子，报了「5 个有缺口」。
其中 4 个（laetitia/fanny/anisa/dameria）**其实早在池里**（id49~52）——
social 号在池里存的邮箱被改过标签（binus→outlook），按邮箱匹配必然漏判。
拿 refreshToken 的 sha256 一比就对上了。错误结论已经报给用户，用户按它决策就会重复加号。

⇒ 判据必须是三把：`email` → `rt_sha256` → `machineId`，且**email 最弱**。
与 `kiro-pool.py dup_check()` / `kiro-batch-prepare.py classify()` 同一套。

另一条：线上读到 0 条凭据时必须 exit，不能让所有号都判成「不在池」——
那是空集假绿，和一把好尺子长得一样（同 `kiro-batch-prepare.py` 那句断言）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import os

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(HERE, "scripts", "kiro-account-reconcile.py")


def _mod():
    """文件名带连字符，不能 import，按路径加载。"""
    spec = importlib.util.spec_from_file_location("reconcile", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_email_relabeled_still_found_by_rt_sha():
    """🔴 这条就是 10-19 那个错：池里邮箱改了标签，只比邮箱会漏判。"""
    m = _mod()
    live = [{"id": 49, "email": "laetitia.rahaju@outlook.com",
             "refreshToken": "RT-LAETITIA", "machineId": "MID-A"}]
    idx = m.build_index(live)
    am_entry = {"refreshToken": "RT-LAETITIA", "machineId": "MID-A"}

    # 表里写的是 binus 邮箱，池里已是 outlook ⇒ 邮箱那把不中
    assert "laetitia.rahaju@binus.ac.id" not in idx["mail"]
    pid, how = m.locate("laetitia.rahaju@binus.ac.id", am_entry, idx)
    assert pid == 49, "rt_sha 必须接住邮箱漏判的号"
    assert how == "rt_sha"


def test_machine_id_is_third_ruler():
    """rt 换过（重新注册 OAuth client）但 machineId 还在 ⇒ 第三把要命中。"""
    m = _mod()
    live = [{"id": 14, "email": "ferril@x.com",
             "refreshToken": "RT-NEW", "machineId": "mid-stable"}]
    idx = m.build_index(live)
    pid, how = m.locate("other@y.com",
                        {"refreshToken": "RT-OLD", "machineId": "MID-STABLE"}, idx)
    assert (pid, how) == (14, "machineId")


def test_email_hit_wins_and_is_labelled():
    m = _mod()
    live = [{"id": 7, "email": "a@b.c", "refreshToken": "RT", "machineId": "M"}]
    pid, how = m.locate("a@b.c", None, m.build_index(live))
    assert (pid, how) == (7, "email")


def test_absent_account_reports_none():
    """真没加过的号必须回 None —— 别为了「都能匹配上」把判据放松成模糊匹配。"""
    m = _mod()
    live = [{"id": 1, "email": "x@y.z", "refreshToken": "RT1", "machineId": "M1"}]
    assert m.locate("nobody@nowhere.com",
                    {"refreshToken": "RT9", "machineId": "M9"},
                    m.build_index(live)) == (None, None)


def test_empty_live_pool_is_not_a_verdict():
    """空集下 locate 恒回 None ⇒ 必须靠 main 里那句 sys.exit 拦住，不能静默全判「不在池」。"""
    m = _mod()
    idx = m.build_index([])
    assert idx == {"rt": {}, "mid": {}, "mail": {}}
    assert m.locate("a@b.c", {"refreshToken": "RT", "machineId": "M"}, idx) == (None, None)
    src = open(SCRIPT, encoding="utf-8").read()
    assert "空集假绿" in src and "sys.exit" in src, "空集闸门不许删"


def test_markdown_mailto_cell_is_parsed():
    """飞书「账户」列常是 `[x@y](mailto:x@y)`，取不出邮箱会把整行判成没加过。"""
    m = _mod()
    assert m.mail_of("[hanes.eduardo@stud.ubbcluj.ro](mailto:hanes.eduardo@stud.ubbcluj.ro)") \
        == "hanes.eduardo@stud.ubbcluj.ro"
    assert m.mail_of("nur.inayah@binus.ac.id") == "nur.inayah@binus.ac.id"
    assert m.mail_of(None) == ""


def test_lark_output_not_in_tmp():
    """🔴 `lark-cli --output` 有路径白名单，写 /tmp 或 $TMPDIR 会被拒：
    `unsafe output path: … outside the built-in allowlist`。
    必须落在仓内 work-kiro-*/（已 gitignore）。这个坑只有不带 --lark-cache
    跑端到端才暴露，别为了「省一次导出」把它改回 /tmp。
    """
    src = open(SCRIPT, encoding="utf-8").read()
    assert "work-kiro-reconcile" in src
    assert 'TMPDIR' not in src, "不许把 lark 导出落到 $TMPDIR"
    assert "allowlist" in src, "这条坑的说明不许删"


def test_row_range_parsing():
    m = _mod()
    assert m.parse_rows("59-81", 126) == set(range(59, 82))
    assert m.parse_rows("3,7,12-14", 126) == {3, 7, 12, 13, 14}
    # 越界的行号要丢掉，别产生 KeyError
    assert m.parse_rows("125-130", 126) == {125, 126}
