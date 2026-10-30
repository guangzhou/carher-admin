"""10-29 换对外域名:老用户 settings.json / 桌面 App 里的旧网关地址,「刷模型」/开会话自动刷新时迁到新地址。

判据(用户定):① 只改「我们网关」那条(旧地址精确匹配),别的不动;② 先用他自己的 key 打新地址 /v1/models 拿到 200
才改;不通就保持原样;③ 新旧两种地址共存期都算我们的(doctor 对旧地址只报黄)。
新地址用 CARHER_MIGRATE_TO 指到本机假网关(不出网);旧地址就是真实的那串字面量(只做字符串比对,不会被请求)。
"""
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REFRESH = ROOT / "src" / "carher_models_refresh.js"
OLD = "https://cc.auto-link.com.cn/pro"
KEY = "sk-FAKEmigrate000000000001"


class _GW(http.server.BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _GW.hits.append((self.path, self.headers.get("x-api-key")))
        ok = self.headers.get("x-api-key") == KEY
        b = json.dumps({"data": [{"id": "claude-opus-5-5"}, {"id": "grok-4.7"}]} if ok else {"error": "no"}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b)


@pytest.fixture(scope="module")
def gw():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GW)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}/model"
    s.shutdown()


def _run(home: Path, settings: dict, new_base: str, quiet=False):
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    p = home / ".claude" / "settings.json"
    p.write_text(json.dumps(settings))
    env = {k: v for k, v in os.environ.items() if k not in ("CARHER_MODELS", "CARHER_BASE", "CARHER_KEY", "CLAUDE_CONFIG_DIR")}
    env.update(HOME=str(home), CARHER_MIGRATE_TO=new_base)
    r = subprocess.run(["node", str(REFRESH)] + (["--quiet"] if quiet else []), env=env, capture_output=True, text=True, timeout=60)
    return r, json.loads(p.read_text())


def _settings(base=OLD, **extra):
    return {"env": {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": base, "MY_VAR": "keep"}, "theme": "dark", **extra}


def test_old_base_moves_when_new_reachable(tmp_path, gw):
    _GW.hits.clear()
    r, d = _run(tmp_path, _settings(), gw)
    assert r.returncode == 0, r.stderr
    assert d["env"]["ANTHROPIC_BASE_URL"] == gw, "新地址通了却没换"
    assert d["env"]["MY_VAR"] == "keep" and d["theme"] == "dark" and d["env"]["ANTHROPIC_API_KEY"] == KEY, "别的字段被动了"
    assert [o["model"] for o in d["modelPicker"]["options"]] == ["claude-opus-5-5", "grok-4.7"], "菜单应来自新地址拉到的列表"
    assert ("/model/v1/models", KEY) in _GW.hits, "必须用用户自己的 key 打新地址 /v1/models"
    assert "网关地址已换成新地址" in r.stdout
    assert KEY not in r.stdout + r.stderr


def test_old_base_kept_when_new_unreachable(tmp_path):
    """新地址不通 ⇒ 地址原样;菜单照旧从旧地址拉(这里旧地址在测试里也打不到 ⇒ 整份 settings 不动)。"""
    s = _settings()
    r, d = _run(tmp_path, s, "http://127.0.0.1:9/model", quiet=True)
    assert r.returncode == 0 and r.stdout == ""
    assert d == s, "新地址不通还改了配置"
    log = (tmp_path / ".claude" / "carher" / "refresh.log").read_text()
    assert "keep old base" in log


def test_wrong_key_on_new_does_not_move(tmp_path, gw):
    s = _settings()
    s["env"]["ANTHROPIC_API_KEY"] = "sk-someone-else-000000000"
    r, d = _run(tmp_path, s, gw, quiet=True)
    assert d["env"]["ANTHROPIC_BASE_URL"] == OLD, "新地址 401 也换了"


@pytest.mark.parametrize("base", ["https://my-own-gateway.example/pro", "http://127.0.0.1:8788", OLD + "/v1", "https://cc.auto-link.com.cn/other"])
def test_other_bases_never_touched(tmp_path, gw, base):
    """只认我们那一个旧地址的精确字面量;用户自己配的别家网关/本机代理/形状不同的一律不碰。"""
    r, d = _run(tmp_path, _settings(base=base), gw, quiet=True)
    assert d["env"]["ANTHROPIC_BASE_URL"] == base


def test_new_base_is_left_alone(tmp_path, gw):
    _GW.hits.clear()
    r, d = _run(tmp_path, _settings(base=gw), gw)
    assert r.returncode == 0 and d["env"]["ANTHROPIC_BASE_URL"] == gw
    assert "网关地址已换成新地址" not in r.stdout


def test_desktop_app_entry_moves_too(tmp_path, gw):
    if sys.platform != "darwin":
        pytest.skip("路径按 mac 布局造")
    lib = tmp_path / "Library" / "Application Support" / "Claude-3p" / "configLibrary"
    lib.mkdir(parents=True)
    me, other = "11111111-2222-3333-4444-555555555555", "99999999-2222-3333-4444-555555555555"
    (lib / "_meta.json").write_text(json.dumps({"appliedId": me, "entries": [{"id": me, "name": "CarHer"}, {"id": other, "name": "Mine"}]}))
    (lib / f"{me}.json").write_text(json.dumps({"inferenceProvider": "gateway", "inferenceGatewayBaseUrl": OLD,
                                                "inferenceGatewayApiKey": KEY, "coworkEgressAllowedHosts": ["*"]}))
    mine = {"inferenceProvider": "gateway", "inferenceGatewayBaseUrl": OLD, "inferenceGatewayApiKey": "sk-other-person-0000"}
    (lib / f"{other}.json").write_text(json.dumps(mine))
    r, d = _run(tmp_path, _settings(), gw)
    c = json.loads((lib / f"{me}.json").read_text())
    assert c["inferenceGatewayBaseUrl"] == gw and c["coworkEgressAllowedHosts"] == ["*"] and c["inferenceGatewayApiKey"] == KEY
    assert c["inferenceModels"] == ["claude-opus-5-5"]
    assert json.loads((lib / f"{other}.json").read_text()) == mine, "别人那条(不是 CarHer / key 不同)被动了"
    assert "桌面 App 的网关地址也换成了" in r.stdout


def test_installers_default_to_new_and_fall_back_to_old():
    src = ROOT / "src"
    mac = (src / "install-mac.sh").read_text(encoding="utf-8")
    win = (src / "install-windows.ps1").read_text(encoding="utf-8-sig")
    assert 'BASE_URL="https://chat.auto-link.com.cn/model"' in mac and 'LEGACY_BASE_URL="https://cc.auto-link.com.cn/pro"' in mac
    assert "$baseUrl     = 'https://chat.auto-link.com.cn/model'" in win and "$legacyBaseUrl = 'https://cc.auto-link.com.cn/pro'" in win
    assert 'CCDL="https://chat.auto-link.com.cn/ccdl/claude-code-releases"' in mac
    assert "CARHER_CCDL = 'https://chat.auto-link.com.cn/ccdl/claude-code-releases'" in win


def test_mac_installer_falls_back_when_new_down(tmp_path, gw):
    """切出安装器「检查 key」那段真跑:新地址不通、旧地址通 ⇒ 用旧地址写配置(不让新装的人装完就坏)。"""
    import re
    mac = (ROOT / "src" / "install-mac.sh").read_text(encoding="utf-8")
    helpers = "\n".join(l for l in mac.splitlines() if re.match(r"^(BLUE|say|ok|warn|die)", l))
    blk = re.search(r'^say "→ 检查 key \.\.\."\n.*?^esac\n', mac, re.S | re.M).group(0)
    for new, want in (("http://127.0.0.1:9/model", gw), (gw, gw)):
        script = helpers + f'\nAPIKEY="{KEY}"\nBASE_URL="{new}"\nLEGACY_BASE_URL="{gw}"\n' + blk + '\necho "BASE=$BASE_URL"\n'
        r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=60)
        assert f"BASE={want}" in r.stdout, r.stdout + r.stderr
        assert "key 可用" in r.stdout
