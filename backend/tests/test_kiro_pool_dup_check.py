"""门禁：`kiro-pool.py` 的 `dup_check()` 不许退回到「只比 refreshToken sha256」。

2026-10-01 的坑：那一版只比 `refreshToken` 的 sha256，而**重新注册 OAuth client 会把
`clientId` + `refreshToken` 全换掉** ⇒ 同一个号的重新导出 sha 对不上、被当成新号放行。
那批 10 个里就有 1 个是池里 id14 的重新导出（`machineId` 逐字相同），当时靠邮箱那条
⚠️ 提醒**人工**比出来的，脚本自己是放行的。额度按**账号**算 ⇒ 并存只会白占一个 id
且两条共用同一个 credits 池。

现在三把判据都是**硬拦**（任一命中就 exit 不发请求）：
`refreshToken` sha256 / `machineId` / `email`。

⛔ 不许用 `userId`：导出里有，但**服务端不落盘**（线上 16 个键里没有它）⇒ 拿它跟池子比
是拿一个池里不存在的字段比，**恒不命中**。10-01 第一版 SOP 就写错成 `userId`，根本执行不了。
"""
from __future__ import annotations

import importlib.util
import os

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(HERE, "scripts", "kiro-pool.py")


def _mod():
    """文件名带连字符，不能 import，按路径加载。"""
    spec = importlib.util.spec_from_file_location("kiro_pool", SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _src() -> str:
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


def test_script_exists_and_is_executable():
    """会数东西的测试先证明自己数得到。"""
    assert os.path.isfile(SCRIPT), SCRIPT
    src = _src()
    assert len(src) > 5000, "脚本被读空/截断，后面的断言全是假绿"
    assert src.startswith("#!"), "缺 shebang"
    assert os.access(SCRIPT, os.X_OK), "缺 +x：文档里的 ./scripts/... 跑不起来"


def test_dup_check_blocks_on_machine_id(monkeypatch):
    """**本轮的核心**：machineId 相同而 refreshToken 不同 ⇒ 必须拦住（而不是只提醒）。

    这正是 10-01 漏进去的那个形状：同一个号换了 OAuth client 后重新导出。
    """
    m = _mod()
    pool = [{"id": 14, "email": "ferril.wananda@binus.ac.id",
             "refreshToken": "OLD-token-aaa",
             "machineId": "21415e01-6984-4a38-bea8-b850865aec20"}]
    monkeypatch.setattr(m, "disk_pool", lambda: pool)
    # 🔴 邮箱**故意写成不同的**：否则 email 那道门也会命中，这条测试就分不清
    # 「是 machineId 拦的」还是「是 email 拦的」—— 删掉 machineId 那道门它照样绿。
    # 一个分不清谁拦的对照，等于没有对照（实测过：删门后这条曾仍然通过）。
    body = {"email": "a-different-address@binus.ac.id",
            "refreshToken": "NEW-token-bbb",           # 换过 client ⇒ sha 对不上
            "machineId": "21415e01-6984-4a38-bea8-b850865aec20"}
    try:
        m.dup_check(body)
    except SystemExit as e:
        assert "machineId" in str(e), f"拦住了但理由不对：{e}"
        assert "重新导出" in str(e)
        return
    raise AssertionError("machineId 相同竟然放行了 —— 这就是 10-01 漏号的那条路")


def test_dup_check_is_case_insensitive_on_machine_id(monkeypatch):
    """machineId 大小写不同是同一个值，不能靠它绕过。"""
    m = _mod()
    monkeypatch.setattr(m, "disk_pool", lambda: [
        {"id": 1, "email": "a@x.com", "refreshToken": "t1",
         "machineId": "ABCDEF01-2345-6789-ABCD-EF0123456789"}])
    body = {"email": "b@x.com", "refreshToken": "t2",
            "machineId": "abcdef01-2345-6789-abcd-ef0123456789"}
    try:
        m.dup_check(body)
    except SystemExit:
        return
    raise AssertionError("大小写不同的同一个 machineId 被放行了")


def test_dup_check_blocks_on_refresh_token(monkeypatch):
    """原有那把判据不许在加新判据时被弄坏。"""
    m = _mod()
    monkeypatch.setattr(m, "disk_pool", lambda: [
        {"id": 4, "email": "a@x.com", "refreshToken": "same-token",
         "machineId": "aaaaaaaa-0000-0000-0000-000000000000"}])
    body = {"email": "different@x.com", "refreshToken": "same-token",
            "machineId": "bbbbbbbb-0000-0000-0000-000000000000"}
    try:
        m.dup_check(body)
    except SystemExit as e:
        assert "refreshToken" in str(e)
        return
    raise AssertionError("refreshToken 相同竟然放行了")


def test_dup_check_blocks_on_email(monkeypatch):
    """同邮箱是硬拦，不是 print 提醒 —— 额度按账号算，并存不会变两份配额。"""
    m = _mod()
    monkeypatch.setattr(m, "disk_pool", lambda: [
        {"id": 5, "email": "dup@x.com", "refreshToken": "t1",
         "machineId": "aaaaaaaa-0000-0000-0000-000000000000"}])
    body = {"email": "dup@x.com", "refreshToken": "t2",
            "machineId": "bbbbbbbb-0000-0000-0000-000000000000"}
    try:
        m.dup_check(body)
    except SystemExit as e:
        assert "邮箱" in str(e)
        return
    raise AssertionError("同邮箱只提醒没拦 —— 10-01 就是这样漏的")


def test_dup_check_passes_a_genuinely_new_account(monkeypatch, capsys):
    """🔴 **阳性对照：全新号必须放行。**

    只验「能拦」不验「不误杀」的话，**一道全拒的门长得和一道好门一模一样**。
    """
    m = _mod()
    monkeypatch.setattr(m, "disk_pool", lambda: [
        {"id": 1, "email": "old@x.com", "refreshToken": "t-old",
         "machineId": "aaaaaaaa-0000-0000-0000-000000000000"}])
    body = {"email": "brand-new@x.com", "refreshToken": "t-new",
            "machineId": "bbbbbbbb-1111-1111-1111-111111111111"}
    m.dup_check(body)          # 不抛 = 放行
    out = capsys.readouterr().out
    assert "非重复" in out, f"放行了但没打出判语：{out!r}"


def test_dup_check_refuses_on_empty_pool(monkeypatch):
    """空集假绿：盘上读到 0 条时「没重复」是读取失败，不是阴性结论。"""
    m = _mod()
    monkeypatch.setattr(m, "disk_pool", lambda: [])
    try:
        m.dup_check({"email": "a@x.com", "refreshToken": "t", "machineId": "m"})
    except SystemExit as e:
        assert "0 条" in str(e) or "空集" in str(e)
        return
    raise AssertionError("空池子竟然当成'没重复'放行了")


def test_user_id_is_not_used_as_a_ruler():
    """⛔ `userId` 服务端不落盘 ⇒ 拿它判重恒不命中，不许写成判据。"""
    src = _src()
    i = src.index("def dup_check")
    j = src.index("def cmd_add")
    body = src[i:j]
    # 只允许在「说明为什么不用它」的注释里出现
    code = "\n".join(l.split("#", 1)[0] for l in body.split("\n"))
    assert 'get("userId")' not in code and "['userId']" not in code, (
        "userId 服务端不落盘，拿它跟池子比恒不命中")
