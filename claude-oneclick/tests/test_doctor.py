"""Claude Code 自检(doctor)的回归(2026-10-16 加)。

mac 段真跑:空 HOME + 造好的 ~/.claude,网关指向本机假 HTTP 服务(不出网),验三件事:
  ① 只读 —— 跑前跑后 ~/.claude 与 rc 文件 sha 不变;
  ② key 明文绝不出现在屏幕/报告里;
  ③ 每种坏配置都在「结论」里报出来(阳性对照:好配置必须 0 条红)。
桌面 App 那一节依赖 /Applications/Claude.app,测试里 HOME 是空的、不碰真 App,只验命令行与网关部分。
Windows 段在 mac 上只能静态断言。
"""
import hashlib
import http.server
import json
import re
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "src" / "doctor-mac.sh"
PS = (ROOT / "src" / "doctor-windows.ps1").read_text(encoding="utf-8-sig")
KEY = "sk-doctorTEST0123456789abc"
BASE = "https://cc.auto-link.com.cn/pro"


def _settings(**over):
    env = {"ANTHROPIC_BASE_URL": BASE, "ANTHROPIC_API_KEY": KEY, "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
           "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5"}
    env.update({k: v for k, v in over.items() if k.isupper()})
    d = {"env": {k: v for k, v in env.items() if v is not None}}
    if "model" in over:
        d["model"] = over["model"]
    return json.dumps(d)


class _GW(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        return self.headers.get("x-api-key") == KEY or self.headers.get("Authorization") == f"Bearer {KEY}"

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "no key"})
        if self.path.endswith("/key/info"):
            return self._send(200, {"info": {"key_alias": "cursor-test"}})
        return self._send(200, {"data": [{"id": "claude-opus-5-5"}, {"id": "claude-sonnet-5-5"}, {"id": "claude-haiku-4-5"}, {"id": "gpt-5.6-terra"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        if not self._authed():
            return self._send(401, {"error": "no key"})
        return self._send(200, {"content": [{"type": "text", "text": "OK"}]})


@pytest.fixture(scope="module")
def gw():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GW)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}/pro"
    s.shutdown()


def _home(tmp_path, settings=None, zshrc=""):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    (h / "Desktop").mkdir()
    if settings is not False:
        (h / ".claude" / "settings.json").write_text(settings or _settings())
    (h / ".zshrc").write_text(zshrc)
    return h


def _snap(h):
    fs = [p for p in sorted((h / ".claude").rglob("*")) if p.is_file()] + [h / ".zshrc"]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in fs}


def _run(h, gw):
    # 带上 node(读 JSON 用)但不带用户的 claude:PATH 只给 node 所在目录
    node = subprocess.run(["bash", "-lc", "command -v node"], capture_output=True, text=True).stdout.strip()
    path = str(Path(node).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin" if node else "/usr/bin:/bin"
    env = {"HOME": str(h), "PATH": path, "CARHER_DOCTOR_BASE": gw, "LANG": "en_US.UTF-8",
           "CARHER_DOCTOR_APP": str(h / "no-such.app")}   # 不碰本机真装的 Claude.app
    r = subprocess.run(["bash", str(DOC)], env=env, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
    out = r.stdout.decode("utf-8") + r.stderr.decode("utf-8")   # 非法 UTF-8 直接炸 = 也是一条断言
    reports = list((h / "Desktop").glob("claude-自检-*.txt"))
    assert len(reports) == 1, out
    rep = reports[0].read_text()
    return out, rep, rep.split("=== 结论 ===", 1)[1]


def test_good_config_is_green_readonly_and_masked(tmp_path, gw):
    h = _home(tmp_path)
    before = _snap(h)
    out, rep, concl = _run(h, gw)
    assert _snap(h) == before, "doctor 改了配置(必须只读)"
    assert KEY not in out and KEY not in rep, "key 明文出现在输出里"
    assert "🔴" not in concl, concl
    assert "实打 claude-opus-5-5 : HTTP 200" in rep and "回答 OK" in rep
    assert "key 名字      : cursor-test" in rep


@pytest.mark.parametrize("case,kw,expect", [
    ("没跑过一键", dict(settings=False), "一键配置没跑成功"),
    ("key 错", dict(settings=_settings(ANTHROPIC_API_KEY="sk-wrongwrongwrong000")), "key 无效(HTTP 401)"),
    ("指错网关", dict(settings=_settings(ANTHROPIC_BASE_URL="https://api.anthropic.com")), "没指向公司网关"),
    ("AUTH_TOKEN 并存", dict(settings=_settings(ANTHROPIC_AUTH_TOKEN="sk-tokentoken0000000")), "同时有 AUTH_TOKEN 和 API_KEY"),
    ("关了模型发现", dict(settings=_settings(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")), "会关掉模型发现"),
    ("旧编码模型名", dict(settings=_settings(model="claude-router-6770742d352e35")), "旧编码名"),
    ("零宽字符", dict(settings=_settings(ANTHROPIC_API_KEY=KEY + "​")), "看不见的字符"),
    ("rc 里有覆盖 export", dict(zshrc="export ANTHROPIC_BASE_URL=https://x\n"), "会盖过配置"),
    ("JSON 坏了", dict(settings="{not json"), "不是合法 JSON"),
    # 10-19 sunqiang:没设 haiku ⇒ Claude Code 默认要 claude-haiku-5-5,网关没有 ⇒ 后台任务报错
    ("haiku 默认值网关没有", dict(settings=_settings(ANTHROPIC_DEFAULT_HAIKU_MODEL=None)), "claude-haiku-5-5 你的 key 没有"),
])

def test_each_failure_is_reported(tmp_path, gw, case, kw, expect):
    h = _home(tmp_path, **kw)
    out, rep, concl = _run(h, gw)
    assert expect in concl, f"[{case}] 结论里没报出来:\n{concl}"
    assert KEY not in rep

def test_alias_model_is_probed_by_real_name(tmp_path, gw):
    """10-19 sunqiang:model=sonnet 被原样拿去实打 ⇒ 403 误报。简称要换成 Claude Code 实际发的真名。"""
    import json
    st = json.loads(_settings()); st["model"] = "sonnet"
    h = _home(tmp_path, settings=json.dumps(st))
    out, rep, concl = _run(h, gw)
    assert "实打 claude-sonnet-5-5 : HTTP 200" in rep, rep
    assert "🔴" not in concl, concl


def test_no_bare_var_before_multibyte():
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", DOC.read_text())


def test_windows_doctor_static():
    assert not re.search(r"\?\?|\?\.", PS)
    for must in ("Get-AppxPackage", "Claude-3p", "configLibrary", ".verified", "ccdl/claude-code-releases",
                 "x-api-key", "/v1/messages", "Set-Clipboard", "KeyFp", "BadChars", "=== 结论 ==="):
        assert must in PS, must
    assert not re.search(r"P \"[^\"]*\$(key|tok)\b(?!\))", PS)


def test_build_outputs_doctor_files():
    s = (ROOT / "build.sh").read_text()
    assert "claude-doctor-mac.command" in s and "claude-doctor-windows.bat" in s


# ── 画图(10-16):安装器第 4d 步 + 常驻说明块 ──
def _run_4d(home):
    import subprocess, re
    src = (ROOT / "claude-mac.command").read_text(encoding="utf-8")
    helpers = "\n".join(l for l in src.splitlines() if re.match(r"^(BLUE|say|ok|warn|die)", l))
    blk = re.search(r"^# ── 4d\..*?(?=^# \(会盖过配置)", src, re.S | re.M).group(0)
    node = subprocess.run(["bash", "-lc", "command -v node"], capture_output=True, text=True).stdout.strip()
    return subprocess.run(["/bin/bash", "-c", helpers + f'\nCLAUDE_HOME="{home}/.claude"\n' + blk],
                          capture_output=True, text=True, env={"PATH": str(Path(node).parent) + ":/usr/bin:/bin", "HOME": str(home)})


def test_image_skill_installed_and_idempotent(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("# 我的规则\n不要用 emoji。\n")
    for _ in range(2):
        r = _run_4d(tmp_path)
        assert r.returncode == 0, r.stderr
    md = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert md.startswith("# 我的规则") and md.count("CARHER-IMAGE-BEGIN") == 1 and "carher_image.js" in md
    sk = (tmp_path / ".claude" / "skills" / "carher-image" / "SKILL.md").read_text()
    assert "@@" not in sk and "carher_image.js" in sk and sk.startswith("---\nname: carher-image")
    tool = tmp_path / ".claude" / "carher" / "carher_image.js"
    assert tool.read_bytes() == (ROOT.parent / "codex-oneclick" / "src" / "carher_image.js").read_bytes()
    assert list((tmp_path / ".claude").glob("CLAUDE.md.bak-carher-*")), "改 CLAUDE.md 前要备份"
