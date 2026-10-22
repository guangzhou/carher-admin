"""门禁：`kiro-pool.py` 的 `add` 必须**自己判落地**，不许靠返回码。

2026-10-08 的坑（批量加 24 个号时暴露）：
admin API 拒绝时回的是 **HTTP 200 + body `{"error":{...}}`**，`curl` 对此 rc=0
⇒ `sh()` 不报错 ⇒ 整个 `add` 安静地 `exit 0`，**看着像加成功了**。
那轮 3 个号的 refreshToken 已死（`invalid_grant` / `Resource not found`），
服务端逐个拒，循环里 **24 行全是 rc=0**；真相只在「比对了盘上 N 个号」那个 N
停涨上看得出来（46/46、51/51、53/53），而最终池子是 66 不是 45+24=69。

⇒ 判据必须是**池子真多了一条、且就是我这个号**：
  · 条数门：after - before 非空（拦「什么都没发生」）
  · 身份门：新增那条的 email/machineId 与提交的一致（拦「涨的是别人那条」）

身份门读**盘上**那份而不是 API：API 的行里没有 `machineId`，且 social 号的
`email` 可能是 null（线上 id46~52 就是）⇒ 拿 API 比只有一把半判据。
两边都空**不算命中** —— 那是「两个字段都没有」，不是「两个字段相同」。
"""
from __future__ import annotations

import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(HERE, "scripts", "kiro-pool.py")


def _mod():
    """文件名带连字符，不能 import，按路径加载。"""
    spec = importlib.util.spec_from_file_location("kiro_pool_landing", SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _src() -> str:
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


def _pool(ids: list[int]) -> dict:
    return {"total": len(ids), "credentials": [{"id": i} for i in ids]}


def test_script_reads_and_has_the_gate():
    """会数东西的测试先证明自己数得到。"""
    src = _src()
    assert len(src) > 5000, "脚本被读空/截断，后面的断言全是假绿"
    assert "def assert_landed" in src, "落地复核那道门没了"
    m = _mod()
    assert callable(m.assert_landed)


def test_rejected_add_exits_nonzero(monkeypatch):
    """**本轮核心**：服务端拒（200 + error body、条数没涨）⇒ 必须非零退出。

    删掉条数门这条测试就红：没有它，`assert_landed` 对「什么都没发生」毫无反应。
    """
    m = _mod()
    monkeypatch.setattr(m, "disk_pool", lambda: [])
    before = _pool([1, 2, 3])
    after = _pool([1, 2, 3])          # 条数一模一样 = 没落地
    resp = ('{"error":{"type":"internal_error","message":"内部错误: IdC Token '
            '刷新失败: 400 Bad Request {\\"error\\":\\"invalid_grant\\"}"}}')
    with pytest.raises(SystemExit) as ei:
        m.assert_landed(before, after, {"email": "x@binus.ac.id"}, resp)
    msg = str(ei.value.code)
    assert "没落地" in msg
    # 报错必须把服务端那句原文带出来，否则还得再手搓一次脚本去读 body
    assert "invalid_grant" in msg, "服务端的失败原因被吞了"
    # 并且要指向下一步怎么查，而不是只说「失败了」
    assert "kiro-probe.py catalog" in msg


def test_successful_add_passes_on_email(monkeypatch):
    """阳性对照：真加成了必须放行。

    只验「能拦」不验「不误杀」的话，一道全拒的门长得一模一样。
    """
    m = _mod()
    monkeypatch.setattr(m, "disk_pool",
                        lambda: [{"id": 60, "email": "cut.ansari@binus.ac.id",
                                  "machineId": "a" * 64}])
    m.assert_landed(_pool([59]), _pool([59, 60]),
                    {"email": "cut.ansari@binus.ac.id"}, "{}")


def test_successful_add_passes_on_machine_id_when_email_is_null(monkeypatch):
    """social 号盘上 `email` 可能是 null ⇒ 身份门必须能只靠 machineId 认出来。

    线上 id46~52 正是这个形状。若身份门只比 email，这条会红。
    """
    m = _mod()
    monkeypatch.setattr(m, "disk_pool",
                        lambda: [{"id": 61, "email": None, "machineId": "b" * 64}])
    m.assert_landed(_pool([60]), _pool([60, 61]),
                    {"email": None, "machineId": "b" * 64}, "{}")


def test_both_sides_empty_is_not_a_match(monkeypatch):
    """⛔ 两边都空**不算命中** —— 否则「email 都是 None」会把别人的号认成我的。

    删掉 `want and got` 那两个真值判断，这条就红（None == None 会放行）。
    """
    m = _mod()
    monkeypatch.setattr(m, "disk_pool",
                        lambda: [{"id": 62, "email": None, "machineId": None}])
    with pytest.raises(SystemExit) as ei:
        m.assert_landed(_pool([60]), _pool([60, 62]),
                        {"email": None, "machineId": None}, "{}")
    assert "没有一条" in str(ei.value.code)


def test_gained_row_belonging_to_someone_else_is_refused(monkeypatch):
    """并发：条数涨了但涨的是别人那条 ⇒ 不许当成我的成功。

    单靠条数门这条会绿 —— 这就是身份门存在的理由。
    """
    m = _mod()
    monkeypatch.setattr(m, "disk_pool",
                        lambda: [{"id": 63, "email": "someone.else@binus.ac.id",
                                  "machineId": "c" * 64}])
    with pytest.raises(SystemExit) as ei:
        m.assert_landed(_pool([60]), _pool([60, 63]),
                        {"email": "me@binus.ac.id", "machineId": "d" * 64}, "{}")
    msg = str(ei.value.code)
    assert "别人同时在加号" in msg


def test_identity_gate_reads_disk_not_api(monkeypatch):
    """身份门必须读盘上那份：API 的行里没有 machineId。

    构造法：API 侧只给 id，machineId 只存在于盘上 ⇒ 若实现去 API 里找 machineId，
    就找不到而误判成「不是我的号」。
    """
    m = _mod()
    called = {"disk": 0}

    def fake_disk():
        called["disk"] += 1
        return [{"id": 64, "email": None, "machineId": "e" * 64}]

    monkeypatch.setattr(m, "disk_pool", fake_disk)
    m.assert_landed(_pool([60]), _pool([60, 64]),
                    {"email": None, "machineId": "e" * 64}, "{}")
    assert called["disk"] >= 1, "身份门没去读盘上那份 ⇒ 它比的是只有 id 的 API 行"


def test_dup_found_is_a_distinct_exception_type():
    """判重必须靠**异常类型**分流，不许按消息文本 `in` 判断。

    `dup_check` 有三条不同的消息（rt_sha / machineId / email），
    按文本分类漏一条就会把重复号记成失败（批量那张表会骗人）。
    """
    m = _mod()
    assert issubclass(m.DupFound, SystemExit), \
        "DupFound 必须继承 SystemExit，否则单号 add 的行为会变"
    src = _src()
    i = src.index("def cmd_add_batch")
    j = src.index("def cmd_remove")
    seg = src[i:j]
    assert "except DupFound" in seg, "add-batch 没用异常类型分流判重"
    assert "已经在池里" not in seg, \
        "add-batch 又退回按消息文本判重了 —— dup_check 有三条消息，这样会漏"


def test_dup_check_raises_dup_found_on_all_three_rulers(monkeypatch):
    """三把判据命中时抛的都得是 `DupFound`，一把漏了批量那张表就把它记成失败。"""
    m = _mod()
    pool = [{"id": 14, "email": "ferril.wananda@binus.ac.id",
             "refreshToken": "OLD-token-aaa",
             "machineId": "21415e01-6984-4a38-bea8-b850865aec20"}]
    monkeypatch.setattr(m, "disk_pool", lambda: pool)
    cases = {
        # 每条只让**一把**判据命中，否则分不清是哪把门拦的
        "rt_sha": {"email": "new@x.com", "refreshToken": "OLD-token-aaa",
                   "machineId": "f" * 64},
        "machineId": {"email": "new@x.com", "refreshToken": "NEW-token",
                      "machineId": "21415e01-6984-4a38-bea8-b850865aec20"},
        "email": {"email": "ferril.wananda@binus.ac.id",
                  "refreshToken": "NEW-token", "machineId": "f" * 64},
    }
    for name, body in cases.items():
        with pytest.raises(m.DupFound):
            m.dup_check(body)


def test_empty_pool_is_not_classified_as_dup(monkeypatch):
    """「盘上 0 条」是**真失败**不是重复 —— 它必须留在 `sys.exit` 那一类。

    否则批量会把「PVC 读空」这种整池性故障安静地记成「跳过，已在池里」。
    """
    m = _mod()
    monkeypatch.setattr(m, "disk_pool", lambda: [])
    with pytest.raises(SystemExit) as ei:
        m.dup_check({"email": "a@x.com", "refreshToken": "t", "machineId": "m"})
    assert not isinstance(ei.value, m.DupFound), \
        "空集假绿被归成判重了 ⇒ 批量会把它当「跳过」咽下去"
    assert "空集假绿" in str(ei.value.code)


def test_add_batch_refuses_empty_input(monkeypatch):
    """空输入必须报错：一张全绿的空表是最像成功的失败。"""
    import argparse
    m = _mod()
    with pytest.raises(SystemExit) as ei:
        m.cmd_add_batch(argparse.Namespace(files=[], priority=2))
    assert "空集假绿" in str(ei.value.code)


def test_add_batch_exits_nonzero_when_any_failed(tmp_path, monkeypatch, capsys):
    """批量：一个失败不中断，但**收尾必须非零退出**，且表里三类分开。

    这是 10-08 那段手搓 for 最致命的缺陷：它 rc 永远是最后一个号的 rc。
    """
    import argparse
    m = _mod()
    files = []
    for n in ("good", "dupe", "dead"):
        p = tmp_path / f"{n}.json"
        p.write_text("{}", encoding="utf8")
        files.append(str(p))

    def fake_add(ns):
        if ns.file.endswith("dupe.json"):
            raise m.DupFound("🔴 这个 refreshToken 已经在池里了：['id=14 x@y']")
        if ns.file.endswith("dead.json"):
            raise SystemExit("🔴 加号没落地：池子条数没变（65 → 65）。invalid_grant")

    monkeypatch.setattr(m, "cmd_add", fake_add)
    with pytest.raises(SystemExit) as ei:
        m.cmd_add_batch(argparse.Namespace(files=files, priority=2))
    out = capsys.readouterr().out
    assert "加成 1 / 跳过(重复) 1 / 失败 1" in out, out[-800:]
    assert "别把这当成功" in str(ei.value.code)


def test_add_batch_all_green_exits_zero(tmp_path, monkeypatch, capsys):
    """阳性对照：全成功时不许非零退出（否则这道门就是「永远报错」）。"""
    import argparse
    m = _mod()
    p = tmp_path / "ok.json"
    p.write_text("{}", encoding="utf8")
    monkeypatch.setattr(m, "cmd_add", lambda ns: None)
    m.cmd_add_batch(argparse.Namespace(files=[str(p)], priority=2))
    out = capsys.readouterr().out
    assert "加成 1 / 跳过(重复) 0 / 失败 0" in out
