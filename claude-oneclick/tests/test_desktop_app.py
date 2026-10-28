"""桌面 App(Code 标签页)三方模式配置的离线回归。

形状来自 Claude.app 1.15962.0 自身代码(configLibrary/_meta.json + <id>.json),并在 mac 上真机验过:
日志 `[custom-3p] 3P mode active { provider: 'gateway' }`,真请求落 SpendLogs。
这里守的是"重跑 / 已有配置"这两种最容易把用户东西弄坏的情况。
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAC = (ROOT / "src" / "install-mac.sh").read_text(encoding="utf-8")
PS = (ROOT / "src" / "install-windows.ps1").read_text(encoding="utf-8-sig")


def _mac_js():
    m = re.search(r'CARHER_LIB="\$APP_UD/configLibrary" node -e \'\n(.*?)\n\' &&', MAC, re.S)
    assert m, "没切到 mac 版桌面 App 配置脚本"
    return m.group(1)


def _win_js():
    m = re.search(r"\$appJs = @'\n(.*?)\n'@", PS, re.S)
    assert m, "没切到 win 版桌面 App 配置脚本"
    return m.group(1)


def _run(js, lib, key="sk-FAKEapp", base="https://gw.example/pro"):
    env = dict(os.environ, CARHER_KEY=key, CARHER_BASE=base, CARHER_LIB=str(lib))
    r = subprocess.run(["node", "-e", js], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _read(lib):
    meta = json.loads((lib / "_meta.json").read_text())
    cfg = json.loads((lib / (meta["appliedId"] + ".json")).read_text())
    return meta, cfg


def test_fresh_install_shape():
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d) / "configLibrary"
        _run(_mac_js(), lib)
        meta, cfg = _read(lib)
        assert re.fullmatch(r"[a-f0-9-]{36}", meta["appliedId"]), "App 只认 36 位 uuid 当 appliedId"
        assert [e["name"] for e in meta["entries"]] == ["CarHer"]
        assert cfg == {
            "inferenceProvider": "gateway",
            "inferenceGatewayBaseUrl": "https://gw.example/pro",
            "inferenceGatewayApiKey": "sk-FAKEapp",
            "inferenceGatewayAuthScheme": "x-api-key",
            "inferenceCredentialKind": "static",
            "coworkEgressAllowedHosts": ["*"],
        }


def test_egress_unset_means_strict_sandbox_so_we_write_star():
    """10-25 同事:App 里 npm install 失败、设置页「严格沙箱 · 组织开启」,终端正常。
    受控对照(本机 claude 2.1.x,--managed-settings 喂 App 在 coworkEgressAllowedHosts 缺省时下发的那份):
    allowedDomains=[] ⇒ npm E403 "user denied"、curl 000;不带沙箱 ⇒ 正常装上。⇒ 安装器写 ["*"]。"""
    for js in (_mac_js(), _win_js()):
        assert 'cfg.coworkEgressAllowedHosts = ["*"]' in js


def test_user_egress_list_preserved():
    """管理员/用户自己写过放行列表 ⇒ 尊重,不改成 *。空列表当没写。"""
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d) / "configLibrary"
        _run(_mac_js(), lib)
        meta, cfg = _read(lib)
        p = lib / (meta["appliedId"] + ".json")
        cfg["coworkEgressAllowedHosts"] = ["registry.npmjs.org"]
        p.write_text(json.dumps(cfg))
        _run(_mac_js(), lib)
        assert _read(lib)[1]["coworkEgressAllowedHosts"] == ["registry.npmjs.org"]
        cfg["coworkEgressAllowedHosts"] = []
        p.write_text(json.dumps(cfg))
        _run(_mac_js(), lib)
        assert _read(lib)[1]["coworkEgressAllowedHosts"] == ["*"]


def test_rerun_is_idempotent_no_duplicate_entries():
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d) / "configLibrary"
        _run(_mac_js(), lib)
        first = _read(lib)
        _run(_mac_js(), lib, key="sk-FAKEnewkey")      # 换了 key 重跑
        meta, cfg = _read(lib)
        assert len(meta["entries"]) == 1, "重跑多出了一条 CarHer"
        assert meta["appliedId"] == first[0]["appliedId"], "重跑换了 id(用户会看到配置被换掉)"
        assert cfg["inferenceGatewayApiKey"] == "sk-FAKEnewkey", "换 key 没生效"


def test_existing_user_configs_preserved():
    """用户可能已经在 App 里配过别的三方配置(Bedrock 之类)⇒ 那些条目和文件必须原样保留,
    只是把生效的那条切到 CarHer。"""
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d) / "configLibrary"
        lib.mkdir(parents=True)
        other = "11111111-2222-3333-4444-555555555555"
        (lib / "_meta.json").write_text(json.dumps({"appliedId": other, "entries": [{"id": other, "name": "My Bedrock"}]}))
        (lib / f"{other}.json").write_text(json.dumps({"inferenceProvider": "bedrock", "inferenceBedrockRegion": "us-east-1"}))
        _run(_mac_js(), lib)
        meta = json.loads((lib / "_meta.json").read_text())
        names = [e["name"] for e in meta["entries"]]
        assert "My Bedrock" in names and "CarHer" in names
        assert json.loads((lib / f"{other}.json").read_text())["inferenceProvider"] == "bedrock", "用户的配置文件被改了"
        carher = next(e for e in meta["entries"] if e["name"] == "CarHer")
        assert meta["appliedId"] == carher["id"]


def test_mac_and_windows_same_semantics():
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d) / "a", Path(d) / "b"
        _run(_mac_js(), a)
        _run(_win_js(), b)
        assert _read(a)[1] == _read(b)[1], "mac/win 写出来的 App 配置不一致"


def test_app_config_key_not_on_command_line():
    for src, pat in [(MAC, r"node -e"), (PS, r"& node")]:
        for line in src.splitlines():
            if re.search(pat, line):
                tail = re.split(pat, line, 1)[1]
                assert "$APIKEY" not in tail and "$apiKey" not in tail, line


# ── 4c 桌面 App 二进制预置 ───────────────────────────────────────────
def _ccd_js(src, pat):
    m = re.search(pat, src, re.S)
    assert m, "没切到二进制预置脚本"
    return m.group(1)


def test_manifest_regex_matches_real_app():
    """安装器靠这条正则从 App 里读「钉死的 Claude Code 版本 + sha」。
    App 每次升级都可能改这段 ⇒ 拿**真 app.asar** 验一次(本机有 App 才跑)。"""
    import pytest
    asar = Path("/Applications/Claude.app/Contents/Resources/app.asar")
    if not asar.exists():
        pytest.skip("本机没装 Claude App")
    js = _ccd_js(MAC, r"CARHER_OS=\"\$OS\" node -e '\n(.*?)\n' >\"\$TMPDIR/carher-ccd\.out\"")
    rx = re.search(r"b\.match\((/.*?/)\);", js).group(1)
    # JS 正则里的 \x27 是单引号;换成 Python 的写法
    py = rx[1:-1].replace(r"\x27", "'")
    m = re.search(py, asar.read_bytes().decode("latin1"))
    assert m, "App 里找不到内嵌 manifest —— App 升级改了形状,预置会整体跳过"
    d = json.loads(m.group(1))
    assert re.fullmatch(r"\d+\.\d+\.\d+", d["manifest"]["version"])
    assert d["manifest"]["platforms"]["darwin-arm64"]["bundle"]["checksum"]


def test_seed_refuses_bad_checksum():
    """镜像被换成别的文件 ⇒ 必须拒绝安装(不能把没校验过的二进制塞给 App 执行)。"""
    js = _ccd_js(MAC, r"CARHER_OS=\"\$OS\" node -e '\n(.*?)\n' >\"\$TMPDIR/carher-ccd\.out\"")
    assert js.count("sha256 不对,拒绝安装") == 3, "mac host / Linux host / VM 三份都要校验"
    wjs = re.search(r"\$ccdJs = @'\n(.*?)\n'@", PS, re.S).group(1)
    assert "sha256 不对,拒绝安装" in wjs


def test_seed_does_not_need_system_zstd():
    """同事 Mac 默认没有 zstd;第一版 shell 出去调 zstd,只在装了 brew zstd 的机器上能过 = 假绿。"""
    js = _ccd_js(MAC, r"CARHER_OS=\"\$OS\" node -e '\n(.*?)\n' >\"\$TMPDIR/carher-ccd\.out\"")
    assert "zstdDecompressSync" in js
    assert not re.search(r"\bzstd -dc\b|\bunzstd\b", js), "又依赖系统 zstd 了"


# ── 10-28 App 4.29:本机组件目录 = claude-code/<ver>/<sha 前 12 位>/ ─────────────────
def _seed_run(js, asar_text, os_name, arch, tmp):
    """真跑预置脚本:假 asar + 本地 http 镜像(官方原样 zstd 文件,sha 与假 manifest 对上)。"""
    import hashlib, http.server, threading, zlib
    payload = {}
    def zst(raw):
        out = subprocess.run(["node", "-e", "process.stdout.write(require('zlib').zstdCompressSync(require('fs').readFileSync(0)))"],
                             input=raw, capture_output=True, check=True).stdout
        return out, hashlib.sha256(out).hexdigest()
    plain, ck_plain = zst(b"#!/bin/sh\necho fake-claude\n")
    plats = {}
    for p in ("linux-x64", "linux-arm64", "darwin-arm64", "darwin-x64"):
        payload[f"/9.9.9/{p}/claude.zst"] = plain
        plats[p] = {"binary": "claude.zst", "checksum": ck_plain, "size": len(plain)}
    # mac bundle:tar 里放 claude.app/Contents/MacOS/claude
    bd = tmp / "bundle"; (bd / "claude.app/Contents/MacOS").mkdir(parents=True)
    (bd / "claude.app/Contents/MacOS/claude").write_text("#!/bin/sh\n"); 
    tarb = subprocess.run(["tar", "-cf", "-", "-C", str(bd), "claude.app"], capture_output=True, check=True).stdout
    bz, ck_b = zst(tarb)
    for p in ("darwin-arm64", "darwin-x64"):
        payload[f"/9.9.9/{p}/claude.app.tar.zst"] = bz
        plats[p]["bundle"] = {"checksum": ck_b, "size": len(bz)}
    man = json.dumps({"version": "9.9.9", "manifest": {"version": "9.9.9", "platforms": plats}, "baseUrl": "x"}, separators=(",", ":"))  # 真 App 内嵌的是紧凑 JSON
    asar = tmp / "app.asar"; asar.write_text(asar_text.replace("@@MAN@@", man), encoding="latin1")
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            b = payload.get(self.path)
            self.send_response(200 if b else 404); self.end_headers(); self.wfile.write(b or b"")
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    # 脚本用 https.get ⇒ 测试里换成 http
    js2 = js.replace('require("https")', 'require("http")')
    ud = tmp / "ud"
    env = dict(os.environ, CARHER_ASAR=str(asar), CARHER_UD=str(ud), CARHER_CCDL=f"http://127.0.0.1:{srv.server_port}",
               CARHER_ARCH=arch, CARHER_OS=os_name)
    r = subprocess.run(["node", "-e", js2], env=env, capture_output=True, text=True)
    srv.shutdown()
    assert r.returncode == 0, r.stderr
    return ud, ck_plain, ck_b


NEW_APP = "x;throw Error(`... it cannot name an install folder`);JSON.parse('@@MAN@@');"
OLD_APP = "x;JSON.parse('@@MAN@@');"


def test_seed_uses_hashed_dir_on_new_app_mac(tmp_path):
    """App 4.29(本机 3.1623 起)只认 claude-code/<ver>/<sha12>/;写老目录 ⇒ doctor 误报 + 重跑又下 100MB。"""
    js = _ccd_js(MAC, r"CARHER_OS=\"\$OS\" node -e '\n(.*?)\n' >\"\$TMPDIR/carher-ccd\.out\"").replace("\\x27", "'")
    ud, _, ck_b = _seed_run(js, NEW_APP, "darwin", "arm64", tmp_path)
    d = ud / "claude-code/9.9.9" / ck_b[:12]
    assert (d / ".verified").read_text() == ck_b and (d / "claude.app/Contents/MacOS/claude").exists()
    assert not (ud / "claude-code/9.9.9/.verified").exists()
    assert (ud / "claude-code-vm/9.9.9/.verified").exists(), "VM 那份目录没变"


def test_seed_keeps_flat_dir_on_old_app_mac(tmp_path):
    js = _ccd_js(MAC, r"CARHER_OS=\"\$OS\" node -e '\n(.*?)\n' >\"\$TMPDIR/carher-ccd\.out\"").replace("\\x27", "'")
    ud, _, ck_b = _seed_run(js, OLD_APP, "darwin", "arm64", tmp_path)
    assert (ud / "claude-code/9.9.9/.verified").read_text() == ck_b


def test_seed_linux_app_plain_binary_hashed_dir(tmp_path):
    """Linux App(deb)本机这份是单个 claude 二进制(useBundle 只在 darwin),同样进 <sha12>/。"""
    js = _ccd_js(MAC, r"CARHER_OS=\"\$OS\" node -e '\n(.*?)\n' >\"\$TMPDIR/carher-ccd\.out\"").replace("\\x27", "'")
    ud, ck, _ = _seed_run(js, NEW_APP, "linux", "x64", tmp_path)
    exe = ud / "claude-code/9.9.9" / ck[:12] / "claude"
    assert exe.read_bytes().startswith(b"#!/bin/sh") and os.access(exe, os.X_OK)


def test_seed_windows_hashed_dir(tmp_path):
    js = re.search(r"\$ccdJs = @'\n(.*?)\n'@", PS, re.S).group(1)
    assert 'it cannot name an install folder' in js and 'e.checksum.slice(0,12)' in js


def test_doctors_accept_both_layouts():
    dm = (ROOT / "src/doctor-mac.sh").read_text()
    assert '"$UD/claude-code/$PIN"/*/' in dm
    dw = (ROOT / "src/doctor-windows.ps1").read_text()
    assert "Get-ChildItem $bd -Directory" in dw
