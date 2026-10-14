"""install-mac.sh 的离线回归。

只测「不联网也能判对错」的那几段：settings.json 合并、key 清洗、PATH 块幂等、bat 头。
真装 Node / 真装 claude / 真发请求属于端到端，在干净 HOME 里手跑(见 README)。

每条对应一个会**静默**坏掉的坑 —— 坏了用户只会看到 401 / 菜单少模型 / 原有设置丢了，
不会有人来报"合并逻辑错了"。
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "src" / "install-mac.sh").read_text(encoding="utf-8")


def _node_merge_js() -> str:
    """从真源码里切出合并那段 JS(不复制副本:副本会漂,漂了照样绿)。"""
    m = re.search(r"node -e '\n(.*?)\n' \|\| die", SRC, re.S)
    assert m, "没切到 settings.json 合并那段 node -e 脚本(源码结构变了?)"
    return m.group(1)


def _merge(initial, key="sk-FAKEtestkey0000000000", base="https://gw.example/pro"):
    js = _node_merge_js()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        if initial is not None:
            p.write_text(initial, encoding="utf-8")
        env = dict(os.environ, CARHER_KEY=key, CARHER_BASE=base, CARHER_SETTINGS=str(p))
        r = subprocess.run(["node", "-e", js], env=env, capture_output=True, text=True)
        out = p.read_text(encoding="utf-8") if p.exists() else None
        mode = oct(p.stat().st_mode & 0o777) if p.exists() else None
        return r.returncode, out, r.stderr, mode


def test_merge_preserves_user_settings():
    """用户原有的设置(权限、模型、别的 env、hooks)必须原样保留。"""
    before = {
        "permissions": {"allow": ["Bash(git *)"]},
        "model": "opus",
        "env": {"MY_OWN_VAR": "keep-me", "HTTPS_PROXY": "http://127.0.0.1:7890"},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
    }
    rc, out, err, _ = _merge(json.dumps(before))
    assert rc == 0, err
    d = json.loads(out)
    assert d["permissions"] == before["permissions"]
    assert d["model"] == "opus"
    assert d["hooks"] == before["hooks"]
    assert d["env"]["MY_OWN_VAR"] == "keep-me"
    assert d["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert d["env"]["ANTHROPIC_API_KEY"] == "sk-FAKEtestkey0000000000"
    assert d["env"]["ANTHROPIC_BASE_URL"] == "https://gw.example/pro"
    assert d["env"]["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"


def test_merge_removes_auth_token_and_nonessential_switch():
    """AUTH_TOKEN 能聊天但拉不到网关模型列表;NONESSENTIAL 开关会把模型发现一起关掉。
    两个都留着 ⇒ /model 里只剩几个 Claude,用户以为装坏了。"""
    before = {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-old", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}}
    rc, out, err, _ = _merge(json.dumps(before))
    assert rc == 0, err
    env = json.loads(out)["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in env


@pytest.mark.parametrize("initial", [None, "", "   \n", "﻿{}"])
def test_merge_from_nothing(initial):
    """没有文件 / 空文件 / 只有空白 / 带 BOM(Windows 记事本存的)都要能建出来。"""
    rc, out, err, _ = _merge(initial)
    assert rc == 0, err
    assert json.loads(out)["env"]["ANTHROPIC_API_KEY"].startswith("sk-FAKE")


def test_merge_refuses_broken_json_and_leaves_file_alone():
    """原文件是坏 JSON ⇒ **拒绝并原样不动**(上层已备份)。
    静默覆盖成新的 = 把用户的设置弄丢了,而且他不知道。"""
    broken = '{"permissions": {"allow": ["x"],}'   # 尾逗号
    rc, out, err, _ = _merge(broken)
    assert rc != 0
    assert out == broken, "坏 JSON 被改写了"
    assert "不是合法 JSON" in err


def test_merge_is_idempotent():
    """重跑两次结果逐字节相同(用户会重跑脚本)。"""
    _, once, _, _ = _merge(json.dumps({"model": "opus"}))
    _, twice, _, _ = _merge(once)
    assert once == twice


def test_merge_writes_0600():
    """settings.json 里有明文 key ⇒ 只给自己读。"""
    _, _, _, mode = _merge(None)
    assert mode == "0o600"


# ── key 清洗:零宽字符 ────────────────────────────────────────────────
def _clean_key(raw: bytes) -> str:
    """跑源码里那一行清洗(从源码切出那段 node 正则,不复制副本)。"""
    m = re.search(r"node -e 'process\.stdout\.write\(process\.env\.CARHER_RAW\.replace\((/.+?/g),\"\"\)\)'", SRC)
    assert m, "没切到 key 清洗那一行"
    js = "process.stdout.write(process.env.CARHER_RAW.replace(%s,''))" % m.group(1)
    r = subprocess.run(["node", "-e", js], env=dict(os.environ, CARHER_RAW=raw.decode("utf-8")),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_key_cleaning_clean_key_survives():
    """阴性对照:干净的 key 必须原样过。第一版用 BSD sed 时**干净 key 也被吃成空**
    (`RE error: invalid character range`)—— 只测脏样本的话,这个病是测不出来的。"""
    assert _clean_key(b"sk-abcDEF123") == "sk-abcDEF123"


@pytest.mark.parametrize("dirty,label", [
    ("sk-abc​", "ZWSP 结尾(09-29 同事实际报的那个)"),
    ("​sk-abc", "ZWSP 开头"),
    ("sk-a‌b‍c", "ZWNJ/ZWJ 中间"),
    ("﻿sk-abc", "BOM"),
    ("sk-abc ", "NBSP —— Headers 不会抛,但会 401"),
    ("  sk-abc \t\n", "普通空白"),
])
def test_key_cleaning_strips_invisible(dirty, label):
    assert _clean_key(dirty.encode("utf-8")) == "sk-abc", label


def test_key_cleaning_positive_control():
    """阳性对照:只 trim 普通空白的旧做法,ZWSP 必须**还在** ⇒ 证明上面那批样本真的需要专门处理,
    不是 trim 就能搞定的普通空白(否则上面的绿没有意义)。"""
    assert "sk-abc​".strip() != "sk-abc"


def test_key_never_passed_on_command_line():
    """key 只能走环境变量进 node,不能出现在命令行参数里(ps 能看到)。"""
    for line in SRC.splitlines():
        if "node -e" in line:
            # 允许 CARHER_RAW="${APIKEY:-}" 这种**环境变量赋值**前缀;不允许 $APIKEY 出现在 node 的参数里
            args = line.split("node -e", 1)[1]
            assert "$APIKEY" not in args and "${APIKEY" not in args, line


# ── PATH 块:幂等 + 不碰用户内容 ───────────────────────────────────────
def test_path_block_idempotent_and_preserves_user_lines():
    m = re.search(r"(add_path_block\(\) \{.*?\n\})", SRC, re.S)
    assert m
    fn = m.group(1)
    with tempfile.TemporaryDirectory() as d:
        rc = Path(d) / ".zshrc"
        rc.write_text("export MY=1\nalias ll='ls -l'\n", encoding="utf-8")
        script = f'NODE_HOME="{d}/n"; NPM_PREFIX="{d}/p"; {fn}\nadd_path_block "{rc}"; add_path_block "{rc}"'
        subprocess.run(["bash", "-c", script], check=True)
        text = rc.read_text(encoding="utf-8")
    assert text.count("# >>> carher-claude >>>") == 1, "跑两次出现两个块"
    assert "export MY=1" in text and "alias ll='ls -l'" in text, "用户原有内容被动了"


# ── 版本比较 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("a,b,want", [
    ("2.1.296", "2.1.257", True), ("2.1.257", "2.1.257", True), ("2.1.220", "2.1.257", False),
    ("2.10.0", "2.9.9", True),   # 字符串比较会错的那种
    ("3.0.0", "2.1.257", True),
])
def test_ver_ge(a, b, want):
    m = re.search(r"(ver_ge\(\) \{.*?\n\})", SRC, re.S)
    r = subprocess.run(["bash", "-c", f"{m.group(1)}\nver_ge {a} {b}"])
    assert (r.returncode == 0) is want, f"{a} >= {b}"


# ── 会盖过配置的旧 export:注释掉(不是删),只动那几个变量,别的原样 ────────
def test_override_exports_commented_not_deleted():
    m = re.search(r'(FIXED=0\nfor rc in .*?\ndone\n)', SRC, re.S)
    assert m, "没切到注释旧 export 那一段"
    block = m.group(1)
    with tempfile.TemporaryDirectory() as d:
        rc = Path(d) / ".zshrc"
        rc.write_text(
            "export MINE=1\n"
            "export ANTHROPIC_MODEL=anthropic.claude-opus-5\n"
            "export ANTHROPIC_AUTH_TOKEN=sk-old\n"
            "#export ANTHROPIC_AUTH_TOKEN=already-commented\n"
            "export ANTHROPIC_CUSTOM_MODEL_OPTION=keep\n"          # 不在名单里 ⇒ 不许动
            "# >>> carher-claude >>>\nexport PATH=\"/x:$PATH\"\n# <<< carher-claude <<<\n",
            encoding="utf-8")
        script = 'warn(){ :; }; HOME="%s"\n%s' % (d, block)
        subprocess.run(["bash", "-c", script], check=True)
        out = rc.read_text(encoding="utf-8")
        baks = list(Path(d).glob(".zshrc.bak-carher-*"))
    lines = out.splitlines()
    assert "export MINE=1" in lines
    assert "export ANTHROPIC_CUSTOM_MODEL_OPTION=keep" in lines, "名单外的变量被动了"
    assert not any(l.startswith("export ANTHROPIC_MODEL=") for l in lines), "ANTHROPIC_MODEL 没被注释"
    assert not any(l.startswith("export ANTHROPIC_AUTH_TOKEN=") for l in lines)
    assert any("[carher-claude 已注释" in l and "ANTHROPIC_MODEL=anthropic.claude-opus-5" in l for l in lines), "是删了不是注释(用户无法还原)"
    assert "#export ANTHROPIC_AUTH_TOKEN=already-commented" in lines, "已注释的行被重复处理"
    assert 'export PATH="/x:$PATH"' in lines, "我们自己的块被误伤"
    assert len(baks) == 1, "改 rc 前没留备份"


def test_merge_repairs_router_model_name_and_adds_picker_rows():
    """10-14 用户:新开窗口报 Model "claude-router-…" isn't recognized。
    旧版存下的 claude-router-<hex> 要还原真名;非 claude 网关模型进 modelPicker(behavesAs)。"""
    import json, os, re, subprocess, tempfile
    from pathlib import Path
    mac = (Path(__file__).resolve().parents[1] / "src" / "install-mac.sh").read_text(encoding="utf-8")
    js = re.search(r"CARHER_SETTINGS=\"\$SETTINGS\" node -e '\n(.*?)\n' \|\| die", mac, re.S).group(1)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        p.write_text(json.dumps({"model": "claude-router-" + "gemini-4-argon".encode().hex(), "theme": "dark"}))
        env = dict(os.environ, CARHER_KEY="sk-FAKE", CARHER_BASE="https://gw.example/pro", CARHER_SETTINGS=str(p),
                   CARHER_MODELS="claude-opus-5-5\ngpt-5.6-terra\ngrok-4.7\ngpt-image-2\ncodex-auto-review")
        subprocess.run(["node", "-e", js], env=env, check=True)
        out = json.loads(p.read_text())
    assert out["model"] == "gemini-4-argon"
    assert out["theme"] == "dark"
    assert [r["model"] for r in out["modelPicker"]["options"]] == ["gpt-5.6-terra", "grok-4.7"]
    assert all(r["behavesAs"] == "claude-sonnet-5-5" for r in out["modelPicker"]["options"])
