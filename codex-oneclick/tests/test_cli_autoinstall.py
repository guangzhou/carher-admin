"""第 0 步「没有 codex 命令行就自动装」的回归（2026-10-13 加）。

mac 段能真跑：切出第 0 步，在空 HOME + 只有系统 PATH 里跑，用一个假 npm 代替真下载，
证明「装完写 PATH 块、重跑不重复、npm 失败不阻断」。Windows 段在 mac 上只能做静态断言。
真下载（npmmirror 上 Node 22 + @openai/codex）在 10-13 空 HOME 实测过一次：0.162.1 装上、
二次运行只打「检测到」，这里不重复联网。
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAC = (ROOT / "src" / "install-mac.sh").read_text(encoding="utf-8")
PS = (ROOT / "src" / "install-windows.ps1").read_text(encoding="utf-8-sig")


def _step0():
    helpers = "\n".join(l for l in MAC.splitlines() if re.match(r"^(BLUE|say|ok|warn|die)", l))
    body = re.search(r"^# --- 0\..*?(?=^# --- 1\.)", MAC, re.S | re.M).group(0)
    return helpers + "\n" + body + '\necho "CODEX_AT=$(command -v codex)"\n'


def _fake_bin(d, npm_rc):
    """假 node（版本 22）+ 假 npm：npm rc=0 时往 prefix/bin 放一个会报版本的 codex。"""
    b = Path(d) / "fakebin"
    b.mkdir()
    (b / "node").write_text("#!/bin/sh\necho 22\n")
    (b / "npm").write_text(
        "#!/bin/sh\n"
        f"[ {npm_rc} = 0 ] || exit {npm_rc}\n"
        'mkdir -p "$npm_config_prefix/bin"\n'
        'printf "#!/bin/sh\\necho codex-cli 0.162.1\\n" > "$npm_config_prefix/bin/codex"\n'
        'chmod +x "$npm_config_prefix/bin/codex"\n'
    )
    for f in b.iterdir():
        f.chmod(0o755)
    return b


def _run(home, fakebin):
    env = {"HOME": str(home), "PATH": f"{fakebin}:/usr/bin:/bin"}
    return subprocess.run(["bash", "-c", _step0()], env=env, capture_output=True, text=True)


def test_installs_when_missing_and_writes_path_block_once():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "home"
        home.mkdir()
        fb = _fake_bin(d, 0)
        r = _run(home, fb)
        assert r.returncode == 0, r.stderr
        assert "codex 命令行 0.162.1 已装好" in r.stdout
        assert f"CODEX_AT={home}/.carher-npm-global/bin/codex" in r.stdout
        # 重跑：认出已装的那份，不再装，PATH 块不重复
        r2 = _run(home, fb)
        assert "检测到 codex 命令行 0.162.1" in r2.stdout and "自动安装" not in r2.stdout
        assert (home / ".zshrc").read_text().count("# >>> carher-codex >>>") == 1
        # .bash_profile 原来不存在 ⇒ 不新建（新建会让 macOS 不再读 .bashrc）
        assert not (home / ".bash_profile").exists()


def test_npm_failure_does_not_abort_config():
    """命令行装不上不能让整个脚本 set -e 退出：配置照样写，App 不依赖命令行。"""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "home"
        home.mkdir()
        r = _run(home, _fake_bin(d, 7))
        assert r.returncode == 0, r.stderr
        assert "自动安装 codex 命令行失败" in r.stdout


def test_keeps_user_rc_content():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "home"
        home.mkdir()
        (home / ".zshrc").write_text("export MINE=1\n")
        _run(home, _fake_bin(d, 0))
        assert (home / ".zshrc").read_text().startswith("export MINE=1\n")


def test_windows_autoinstall_shape():
    # npm 在 PowerShell 里直接调会撞执行策略拦 npm.ps1 ⇒ 必须走 cmd /c
    assert re.search(r'& cmd /c "npm install -g @openai/codex ', PS)
    # 只写用户级 PATH（不要管理员）
    assert "SetEnvironmentVariable('Path', (@($dir) + $parts) -join ';', 'User')" in PS
    # 失败只 Warn 不 Die：第 0 步里不许出现 Die
    step0 = re.search(r"# --- 0\..*?(?=# --- 1\.)", PS, re.S).group(0)
    assert "Die " not in step0
    # Node 安装包要校验 SHASUMS256
    assert "SHASUMS256.txt" in step0
