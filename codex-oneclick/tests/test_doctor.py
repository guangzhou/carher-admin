"""codex 自检(doctor)的回归(2026-10-16 加)。

mac 段真跑:空 HOME + 造好的 ~/.codex,网关指向本机一个假 HTTP 服务(不出网),验三件事:
  ① 只读 —— 跑前跑后 ~/.codex 每个文件 sha 不变;
  ② key 明文绝不出现在屏幕/报告里;
  ③ 每种坏配置都在「结论」里报出来(阳性对照:好配置必须 0 条红)。
Windows 段在 mac 上只能静态断言(PowerShell 5.1 语法 + 同一张判据表)。
"""
import hashlib
import http.server
import json
import os
import re
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "src" / "doctor-mac.sh"
PS = (ROOT / "src" / "doctor-windows.ps1").read_text(encoding="utf-8-sig")
KEY = "sk-doctorTEST0123456789abc"
GOOD_CFG = f'''model = "gpt-5.6-terra"
model_provider = "carher_prod"
openai_base_url = "https://cc.auto-link.com.cn/pro/v1"

[model_providers.carher_prod]
name = "CarHer"
base_url = "https://cc.auto-link.com.cn/pro/v1"
wire_api = "responses"
experimental_bearer_token = "{KEY}"
'''


class _GW(http.server.BaseHTTPRequestHandler):
    """假网关:认 KEY;/models 返回两个模型;/responses 回 OK。"""
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer {KEY}"

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "no key"})
        if self.path.endswith("/key/info"):
            return self._send(200, {"info": {"key_alias": "cursor-test"}})
        return self._send(200, {"data": [{"id": "gpt-5.6-terra"}, {"id": "muse-image"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        if not self._authed():
            return self._send(401, {"error": "no key"})
        return self._send(200, {"output": [{"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]})


@pytest.fixture(scope="module")
def gw():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GW)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}/pro/v1"
    s.shutdown()


def _home(tmp_path, cfg=GOOD_CFG, auth_key=KEY, mode="apikey"):
    h = tmp_path / "home"
    c = h / ".codex"
    c.mkdir(parents=True)
    (h / "Desktop").mkdir()
    if cfg is not None:
        (c / "config.toml").write_text(cfg)
    if auth_key is not None:
        (c / "auth.json").write_text(json.dumps({"auth_mode": mode, "OPENAI_API_KEY": auth_key}))
    (c / "carher-models.json").write_text('{"models":[{"slug":"gpt-5.6-terra"}]}')
    (c / "carher_image.py").write_text("#")
    (c / "AGENTS.md").write_text("<!-- CARHER-IMAGE-BEGIN -->\n<!-- CARHER-IMAGE-END -->\n")
    return h


def _snap(d):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


def _run(h, gw):
    env = {"HOME": str(h), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CARHER_DOCTOR_BASE": gw, "LANG": "en_US.UTF-8"}
    r = subprocess.run(["bash", str(DOC)], env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    reports = list((h / "Desktop").glob("codex-自检-*.txt"))
    assert len(reports) == 1, r.stdout + r.stderr
    rep = reports[0].read_text()
    concl = rep.split("=== 结论 ===", 1)[1]
    return r.stdout + r.stderr, rep, concl


def test_good_config_is_green_readonly_and_masked(tmp_path, gw):
    h = _home(tmp_path)
    before = _snap(h / ".codex")
    out, rep, concl = _run(h, gw)
    assert _snap(h / ".codex") == before, "doctor 改了 ~/.codex(必须只读)"
    assert KEY not in out and KEY not in rep, "key 明文出现在输出里"
    assert "🔴" not in concl, concl
    assert "实打 gpt-5.6-terra : HTTP 200" in rep and "回答 OK" in rep
    assert "key 名字      : cursor-test" in rep


@pytest.mark.parametrize("case,kw,expect", [
    ("没跑过一键", dict(cfg=None, auth_key=None), "一键配置没跑成功"),
    ("key 错", dict(cfg=GOOD_CFG.replace(KEY, "sk-wrongwrongwrong000"), auth_key="sk-wrongwrongwrong000"), "key 无效(HTTP 401)"),
    ("两把 key 不一致", dict(auth_key="sk-another00000000000"), "两把不同的 key"),
    ("零宽字符", dict(cfg=GOOD_CFG.replace(KEY, KEY + "​"), auth_key=KEY + "​"), "看不见的字符"),
    ("不是 apikey 模式", dict(mode="chatgpt"), "不是 apikey 模式"),
    ("provider 指错", dict(cfg=GOOD_CFG.replace('base_url = "https://cc.auto-link.com.cn/pro/v1"\nwire', 'base_url = "https://api.openai.com/v1"\nwire')), "不指向公司网关"),
])
def test_each_failure_is_reported(tmp_path, gw, case, kw, expect):
    h = _home(tmp_path, **kw)
    out, rep, concl = _run(h, gw)
    assert expect in concl, f"[{case}] 结论里没报出来:\n{concl}"
    assert KEY not in rep


def test_windows_doctor_static():
    # 只用 5.1 语法:不许 ?? / ?. / 三元
    assert not re.search(r"\?\?|\?\.", PS)
    for must in ("Get-AppxPackage", "auth_mode", "experimental_bearer_token", "muse-image", "/responses",
                 "Set-Clipboard", "Mask", "KeyFp", "BadChars", "=== 结论 ==="):
        assert must in PS, must
    # key 只走 KeyFp / 请求头,不直接 P 出来
    assert not re.search(r"P \"[^\"]*\$(key|k1|k2)\b(?!\))", PS)


def test_no_bare_var_before_multibyte():
    # 10-16 实测:`$PROV」` 在 bash 里会把多字节字符的前半截吃进变量名,输出出现非法 UTF-8 ⇒ 必须写 ${PROV}
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", DOC.read_text())


def test_build_outputs_doctor_files():
    s = (ROOT / "build.sh").read_text()
    assert "codex-doctor-mac.command" in s and "codex-doctor-windows.bat" in s
