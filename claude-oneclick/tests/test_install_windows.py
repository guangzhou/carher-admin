"""install-windows.ps1 的离线回归(在 mac 上也能跑:合并逻辑是 node,能真跑;PS 部分做静态断言)。

Windows 真机端到端没有条件在这里跑,诚实标注:下面只证明
「合并逻辑与 mac 版同语义」+「几个已知会坏的 PS 写法没出现」。
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS = (ROOT / "src" / "install-windows.ps1").read_text(encoding="utf-8-sig")
MAC = (ROOT / "src" / "install-mac.sh").read_text(encoding="utf-8")


def _ps_merge_js():
    m = re.search(r"\$merge = @'\n(.*?)\n'@", PS, re.S)
    assert m, "没切到 PS 里的合并脚本"
    return m.group(1)


def _run(js, initial):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        if initial is not None:
            p.write_text(initial, encoding="utf-8")
        env = dict(os.environ, CARHER_KEY="sk-FAKEwin", CARHER_BASE="https://gw.example/pro", CARHER_SETTINGS=str(p))
        r = subprocess.run(["node", "-e", js], env=env, capture_output=True, text=True)
        return r.returncode, (p.read_text(encoding="utf-8") if p.exists() else None)


def test_windows_merge_same_semantics_as_mac():
    """两边合并逻辑必须同语义:同一份输入 ⇒ 同一份 env 结果。"""
    mac_js = re.search(r"node -e '\n(.*?)\n' \|\| die", MAC, re.S).group(1)
    before = json.dumps({"model": "opus", "env": {"ANTHROPIC_AUTH_TOKEN": "x", "KEEP": "1",
                                                  "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}})
    _, mac_out = _run(mac_js, before)
    _, win_out = _run(_ps_merge_js(), before)
    assert json.loads(mac_out) == json.loads(win_out)


def test_windows_merge_refuses_broken_json():
    rc, out = _run(_ps_merge_js(), '{"a":1,}')
    assert rc != 0 and out == '{"a":1,}'


def test_windows_strips_invisible_chars():
    """PS 那条正则要覆盖与 mac 同一批码位。"""
    m = re.search(r"\$apiKey = \(\$apiKey -replace '(\[[^']+\])', ''\)", PS)
    assert m, "没找到 key 清洗正则"
    cls = m.group(1)
    for cp in ["\\u200B", "\\u200F", "\\u2028", "\\u202F", "\\uFEFF", "\\u00A0", "\\s"]:
        assert cp in cls or (cp == "\\u200F" and "\\u200B-\\u200F" in cls) or \
               (cp == "\\u2028" and "\\u2028-\\u202F" in cls) or (cp == "\\u202F" and "\\u2028-\\u202F" in cls), cp


def test_ps_known_pitfalls_absent():
    # npm 在 PowerShell 里直接调会撞执行策略拦 npm.ps1 ⇒ 必须走 cmd /c
    assert re.search(r"cmd /c \"npm install", PS)
    # 写**用户级** PATH,不要管理员
    assert "'Path', 'User'" in PS
    # 🔴 真正要守的是「不往 Machine 级**写**」(要管理员,普通用户跑会抛 + 中断安装);
    #    **读** Machine 级只是为了提醒,是允许的。原来这条按字面串 "'Machine')" 判,
    #    把 GetEnvironmentVariable($v,'Machine')(只读)也当成违规 = 假红。
    assert not re.search(r"SetEnvironmentVariable\([^)]*'Machine'\)", PS), "往 Machine 级写了环境变量(要管理员)"
    # PS5 的 ConvertTo-Json 会截断嵌套 + 转义中文 ⇒ 合并必须交给 node,不能用它
    code = "\n".join(l for l in PS.splitlines() if not l.lstrip().startswith("#"))
    assert "ConvertTo-Json" not in code
    # 进度条在 PS5 上让 Invoke-WebRequest 慢 10 倍
    assert "$ProgressPreference = 'SilentlyContinue'" in PS
    # 临时文件里有 key ⇒ finally 里删
    assert re.search(r"finally \{[^}]*Remove-Item \$mjs", PS, re.S)


def test_bat_header_is_ascii_crlf(tmp_path):
    """bat 头部必须纯 ASCII + CRLF、无 BOM、无 chcp(codex 那边 08-27 实机踩过:
    cmd 按字节偏移逐行续读,头部有非 ASCII 会吃掉行首字符)。build.sh 里也有同样断言,这里再守一道。"""
    bat = ROOT / "install-windows.bat"
    if not bat.exists():
        import pytest
        pytest.skip("还没 build")
    b = bat.read_bytes()
    cut = b.index(b"##CARHER-PS1")
    assert not b.startswith(b"\xef\xbb\xbf")
    assert all(c < 128 for c in b[:cut])
    assert b"chcp" not in b.lower()
    assert b.count(b"\n") == b.count(b"\r\n")
