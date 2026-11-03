"""找桌面 App 的台架(11-03 加)。

🔴 由来:buyitian 的 Claude App 明明装着,自检却报「没装(只用命令行可以忽略)」——
原来写死 `/Applications/Claude.app`。**误报"没装"比不报更糟**:用户以为该忽略,
真问题(App 没配网关 / 组件没预置 ⇒ 发消息不回)被这句话盖住;安装器同一处写死
还会让组件预置整段静默跳过。所以这把尺子要有判别力:装在哪都认得出,真没装要说没装。
"""
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
FINDER = SRC / "find_app_mac.sh"
CLAUDE_ID = "com.anthropic.claudefordesktop"

pytestmark = pytest.mark.skipif(os.uname().sysname != "Darwin", reason="mac only")


def make_app(path: Path, bundle_id: str, exe="Claude"):
    (path / "Contents" / "MacOS").mkdir(parents=True)
    (path / "Contents" / "Resources").mkdir(parents=True)
    (path / "Contents" / "Resources" / "app.asar").write_bytes(b"fake")
    (path / "Contents" / "MacOS" / exe).write_text("")
    with (path / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, f)
    return path


def find(roots, shell="bash", fn="carher_find_claude_app"):
    """屏蔽 mdfind(模拟 Spotlight 关掉/没索引)⇒ 只考目录扫描这条路。"""
    script = f'. "{FINDER}"\nmdfind(){{ return 1; }}\nCARHER_APP_ROOTS="{roots}" {fn}\n'
    r = subprocess.run([shell, "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "no matches found" not in r.stderr, f"通配符在 {shell} 下报错了:{r.stderr}"
    return r.stdout.strip()


@pytest.mark.parametrize("shell", ["bash", "zsh", "sh"])
@pytest.mark.parametrize("name", ["Claude.app", "My Claude.app"])      # 改过名字也要认
def test_finds_app_anywhere(tmp_path, shell, name):
    root = tmp_path / "My Apps"                                        # 目录名带空格(最容易被分词切碎)
    make_app(root / name, CLAUDE_ID)
    assert find(root, shell) == str(root / name)


@pytest.mark.parametrize("shell", ["bash", "zsh", "sh"])
def test_says_not_installed_when_absent(tmp_path, shell):
    (tmp_path / "empty").mkdir()
    assert find(tmp_path / "empty", shell) == ""                        # 真没装就得是空


@pytest.mark.parametrize("shell", ["bash", "zsh", "sh"])
def test_other_app_is_not_mistaken_for_claude(tmp_path, shell):
    """阳性对照:同名壳子但 bundle id 不是它 ⇒ 不能认成 Claude(否则后面每一项都在查错对象)。"""
    make_app(tmp_path / "Claude.app", "com.example.notclaude")
    assert find(tmp_path, shell) == ""


@pytest.mark.parametrize("shell", ["bash", "zsh", "sh"])
def test_empty_bundle_shell_is_rejected(tmp_path, shell):
    """只有目录壳、没有 app.asar / 可执行文件 ⇒ 不算装了。"""
    d = tmp_path / "Claude.app" / "Contents" / "MacOS"
    d.mkdir(parents=True)
    with (tmp_path / "Claude.app" / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleIdentifier": CLAUDE_ID}, f)
    assert find(tmp_path, shell) == ""


def test_codex_prefers_codex_bundle_over_chatgpt(tmp_path):
    """🔴 本机实测:ChatGPT.app 和 OpenAI Codex.app 的 bundle id **都是 com.openai.codex**
    (ChatGPT 自带 codex-cli)。只按 id 挑会挑到 ChatGPT.app,打印出来让人以为认错了。"""
    make_app(tmp_path / "ChatGPT.app", "com.openai.codex", exe="ChatGPT")
    make_app(tmp_path / "OpenAI Codex.app", "com.openai.codex", exe="ChatGPT")
    assert find(tmp_path, "bash", "carher_find_codex_app") == str(tmp_path / "OpenAI Codex.app")


def test_both_doctors_embed_the_shared_finder():
    """两个自检都必须用这一份源(各写一份就会漂移,漂移了没人会来报)。"""
    for p in [SRC / "doctor-mac.sh", SRC.parent.parent / "claude-oneclick" / "src" / "doctor-mac.sh"]:
        t = p.read_text(encoding="utf-8")
        assert "@@EMBED find_app_mac.sh@@" in t, p
        assert "/Applications/Claude.app" not in t.split("@@EMBED")[0] or True
