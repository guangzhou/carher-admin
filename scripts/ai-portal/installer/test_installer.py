"""ai-portal 一行命令安装的离线台架(11-04)。

跑法:  python3 -m pytest scripts/ai-portal/installer/test_installer.py -v
       CARHER_TEST_REAL_APP=1 再加跑一条「从镜像真装 App」(拷一份本机 Claude.app 当镜像,约 1GB 临时盘)

🔴 台架三条纪律:
  1. 不在真实 HOME 下跑任何安装器:HOME / TMPDIR 全指到 .work/ai-portal/installer/ 下的临时目录;
     被调起的「安装器」是桩(只记下收到的参数/环境变量),真安装器一行都不跑。
  2. 不碰本机真装着的 App:App 那步要么 CARHER_SKIP_APP_INSTALL=1 整段不碰,要么把 mdfind / ps / pgrep /
     osascript / open 全换成桩(找不到本机的 Codex/Claude/Cursor,也退不掉、开不了它们)。
  3. 不出网:安装码服务、安装器下载、App 镜像都是本机一个假 HTTP 服务。
pwsh:本机没装 ⇒ 用 .work/ai-portal/installer/pwsh/pwsh(官方 7.4.6 tar 包解压,不装进系统);
     也可以 CARHER_PWSH=/path/to/pwsh 指定。都没有就跳过 ps1 那几条(会显示 skipped)。
"""
import base64
import http.server
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SH = HERE / "install.sh"
PS1 = HERE / "install.ps1"
WORK = ROOT / ".work" / "ai-portal" / "installer"
WORK.mkdir(parents=True, exist_ok=True)

KEY = "sk-FAKEportal0000000000000000wxyz"     # 假 key,后 4 位 wxyz
KEY2 = "sk-FAKEportal1111111111111111abcd"
SYS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
NODE_DIR = str(Path(shutil.which("node") or "/opt/homebrew/bin/node").parent)
IS_MAC = os.uname().sysname == "Darwin"


def _pwsh():
    for c in (os.environ.get("CARHER_PWSH"), shutil.which("pwsh"), str(WORK / "pwsh" / "pwsh")):
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


PWSH = _pwsh()
need_pwsh = pytest.mark.skipif(not PWSH, reason="本机没有 pwsh(见文件头)")
mac_only = pytest.mark.skipif(not IS_MAC, reason="mac only")


# ── 假服务:安装码 + 安装器下载 + App 镜像 ───────────────────────────────
class Portal:
    def __init__(self):
        self.codes = {}          # code -> {"tool","key","state": ok|used|expired}
        self.files = {}          # path -> bytes
        self.requests = []       # 记下每个 redeem 请求体
        self.redeem_override = None   # (status, body bytes, content-type) 模拟网关错
        portal = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                if self.path != "/ai/api/install-code/redeem":
                    return self._send(404, b'{"error":"not found"}')
                portal.requests.append(raw)
                if portal.redeem_override:
                    s, b, t = portal.redeem_override
                    return self._send(s, b, t)
                try:
                    code = json.loads(raw)["code"]
                except Exception:
                    return self._send(400, json.dumps({"error": "请求格式不对"}).encode())
                c = portal.codes.get(code)
                err = lambda s, m: self._send(s, json.dumps({"error": m}, ensure_ascii=False).encode())
                if not c:
                    return err(404, "安装码不存在,请回网页重新生成")
                if c["state"] == "expired":
                    return err(410, "安装码已过期(10 分钟有效),请回网页重新生成")
                if c["state"] == "used":
                    return err(409, "安装码已经用过了,请回网页重新生成")
                c["state"] = "used"
                self._send(200, json.dumps({"key": c["key"], "base_url": "https://chat.auto-link.com.cn/model",
                                            "tool": c["tool"]}).encode())

            def do_GET(self):
                b = portal.files.get(self.path)
                if b is None:
                    return self._send(404, b"not found", "text/plain")
                self._send(200, b, "application/octet-stream")

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def work():
    d = Path(tempfile.mkdtemp(prefix="t-", dir=WORK))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# 桩安装器(mac):记下 key(环境变量)/ 参数 / stdin 有没有东西,并**故意把 key 打出来**验外层遮罩
STUB_SH = r'''#!/usr/bin/env bash
L="$STUB_LOG/$STUB_NAME"; mkdir -p "$L"
printf '%s' "${CARHER_KEY:-}" > "$L/key"
: > "$L/argv"; for a in "$@"; do printf '%s\n' "$a" >> "$L/argv"; done
# stdin 是什么:/dev/null 是字符设备且读立即 EOF;管道/终端都不算(bash 3.2 的 read -t 超时也返回 1,分不出来)
if [ -c /dev/stdin ] && [ ! -t 0 ] && ! read -r line; then echo "EOF"; elif [ -p /dev/stdin ]; then echo "PIPE"; else echo "OTHER"; fi > "$L/stdin"
echo "stub: 我是被调起的安装器,key=$CARHER_KEY"
exit "${STUB_RC:-0}"
'''


def cursor_zip(new_engine=True) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cursor-mac/cursor_team_setup.sh", STUB_SH.replace("$STUB_NAME", "cursor"))
        js = "// stub engine\nconst k = opt('--key','') || process.env.CARHER_KEY;\n" if new_engine else "// old engine, --key only\n"
        z.writestr("cursor-mac/cursor_team_setup.js", js)
    return buf.getvalue()


@pytest.fixture
def portal():
    p = Portal()
    # all.sh / all.ps1 和另外三个是**同一个文件**(nginx 都 alias 到同一份),这里也照这个形状喂
    for t in ("codex", "claude", "cursor", "all"):
        p.files[f"/ai/i/{t}.sh"] = SH.read_bytes()
        p.files[f"/ai/i/{t}.ps1"] = PS1.read_bytes()
    for t in ("codex", "claude"):
        p.files[f"/ai/i/pkg/{t}-mac.command"] = STUB_SH.replace("$STUB_NAME", t).encode()
    p.files["/ai/i/pkg/cursor-mac.zip"] = cursor_zip(True)
    yield p
    p.close()


def stub_bin(d: Path, open_log: Path) -> Path:
    """把会碰到本机真 App 的命令全换成桩。"""
    b = d / "stubbin"
    b.mkdir(exist_ok=True)
    stubs = {
        "mdfind": "#!/bin/sh\nexit 0\n",                 # Spotlight 什么都找不到
        "ps": "#!/bin/sh\nexit 0\n",                     # 没有任何进程(找不到正在跑的真 App)
        "pgrep": "#!/bin/sh\nexit 1\n",                  # App 都没在跑
        "osascript": f'#!/bin/sh\ncat >/dev/null; echo "osascript $*" >> "{open_log}"\n',
        "open": f'#!/bin/sh\necho "open $*" >> "{open_log}"\n',
    }
    for n, s in stubs.items():
        (b / n).write_text(s)
        (b / n).chmod(0o755)
    return b


def run_sh(work: Path, portal: Portal, args, extra_env=None, stdin=subprocess.DEVNULL, skip_app=True, cmd=None,
           timeout=90):
    home = work / "home"
    home.mkdir(exist_ok=True)
    tmp = work / "tmp"
    tmp.mkdir(exist_ok=True)
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    sb = stub_bin(work, logs / "open.log")
    env = {"HOME": str(home), "TMPDIR": str(tmp) + "/", "PATH": f"{sb}:{SYS_PATH}", "LANG": "en_US.UTF-8",
           "CARHER_PORTAL_BASE": portal.base + "/ai", "CARHER_MIRROR_BASE": portal.base + "/ccdl",
           "STUB_LOG": str(logs)}
    if skip_app:
        env["CARHER_SKIP_APP_INSTALL"] = "1"
    env.update(extra_env or {})
    r = subprocess.run(cmd or ["bash", str(SH), *args], env=env, stdin=stdin, capture_output=True,
                       timeout=timeout)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    return r.returncode, out, logs


def stub_got(logs: Path, name):
    d = logs / name
    if not d.exists():
        return None
    return {"key": (d / "key").read_text(), "argv": (d / "argv").read_text().splitlines(),
            "stdin": (d / "stdin").read_text().strip()}


# ── 1. 码对 ⇒ 调起对应安装器,key 走环境变量,不进参数,不上屏 ─────────────────
@pytest.mark.parametrize("tool", ["codex", "claude", "cursor"])
def test_good_code_runs_matching_installer_with_key(work, portal, tool):
    portal.codes["GOOD1"] = {"tool": tool, "key": KEY, "state": "ok"}
    # stdin 给一个一直开着、没有数据的管道(像终端):安装器若没接 /dev/null,桩会记成 PIPE
    p_r, p_w = os.pipe()
    try:
        rc, out, logs = run_sh(work, portal, ["--code", "GOOD1"], stdin=p_r)
    finally:
        os.close(p_r)
        os.close(p_w)
    assert rc == 0, out
    got = stub_got(logs, tool)
    assert got, f"{tool} 安装器桩没被调起:\n{out}"
    assert got["key"] == KEY
    if tool == "cursor":
        assert got["argv"] == ["--apply"], got["argv"]      # key 不进命令行(ps 看得见)
    else:
        assert got["argv"] == []
    assert got["stdin"] == "EOF", "安装器的 stdin 必须是 /dev/null(交互读立即返回,不会卡住)"
    assert KEY not in out, "key 上屏了(桩故意把 key 打出来,外层必须遮掉)"
    assert "后4位 …wxyz" in out and f"长度 {len(KEY)}" in out
    assert "stub: 我是被调起的安装器,key=***" in out, "子安装器输出没过遮罩"
    assert "全部完成" in out
    # 只换了一次码,请求体就是那个码
    assert [json.loads(x) for x in portal.requests] == [{"code": "GOOD1"}]


# ── 1b. tool=all:一个码装三个 ────────────────────────────────────────────────
ALL_ORDER = ["codex", "claude", "cursor"]   # 🔴 Cursor 必须最后(它要求先完全退出,最可能要人动手)


def test_all_runs_three_installers_with_same_key_and_one_redeem(work, portal):
    """tool=all:三个桩都被调起、各自拿到**同一把** key、码只换了一次。"""
    portal.codes["ALL1"] = {"tool": "all", "key": KEY, "state": "ok"}
    p_r, p_w = os.pipe()
    try:
        rc, out, logs = run_sh(work, portal, ["--code", "ALL1"], stdin=p_r, timeout=120)
    finally:
        os.close(p_r)
        os.close(p_w)
    assert rc == 0, out
    for t in ALL_ORDER:
        got = stub_got(logs, t)
        assert got, f"{t} 安装器桩没被调起:\n{out}"
        assert got["key"] == KEY, t
        assert got["stdin"] == "EOF", t          # 三个都不许读 stdin(curl|bash 下会吃掉脚本)
        assert got["argv"] == (["--apply"] if t == "cursor" else []), (t, got["argv"])
    # 🔴 只换一次码(码是一次性的,换第二次服务端就 400)
    assert [json.loads(x) for x in portal.requests] == [{"code": "ALL1"}], portal.requests
    # 进度提示 + 总体预估 + 汇总
    assert "三个都装" in out and "10~40 分钟" in out and "别关这个终端" in out
    for n in ("第 1/3 个", "第 2/3 个", "第 3/3 个"):
        assert n in out, n
    assert "安装结果" in out and "✅ 三个都装好了" in out
    for lbl in ("Codex", "Claude Code", "Cursor"):
        assert ("✔ " + lbl) in out, lbl
    assert KEY not in out
    assert "长度 %d" % len(KEY) in out and "后4位 …wxyz" in out


def test_all_installs_in_order_cursor_last(work, portal):
    """顺序钉死 codex → claude → cursor:Cursor 要先退出 App,放最后 ⇒ 卡住时另外两个已装完。"""
    portal.codes["ALL2"] = {"tool": "all", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, ["--code", "ALL2"], timeout=120)
    assert rc == 0, out
    # 屏幕上「第 N/3 个:<tool>」的出现顺序就是执行顺序
    seen = re.findall(r"第 \d/3 个:(codex|claude|cursor)", out)
    assert seen == ALL_ORDER, seen
    # 桩的落盘时间也对得上(同一个 key 文件的 mtime 单调)
    ts = [(logs / t / "key").stat().st_mtime for t in ALL_ORDER]
    assert ts == sorted(ts), ts


@pytest.mark.parametrize("bad", ALL_ORDER)
def test_all_one_failure_does_not_stop_the_others(work, portal, bad):
    """中间一个失败:另外两个仍然跑完、汇总写清谁没成、退出码 7(部分成功)。"""
    # 让 bad 那个工具的安装器桩退出码非 0(其余 0)
    fail = STUB_SH.replace('exit "${STUB_RC:-0}"', "exit 9")
    if bad == "cursor":
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("cursor-mac/cursor_team_setup.sh", fail.replace("$STUB_NAME", "cursor"))
            z.writestr("cursor-mac/cursor_team_setup.js", "// stub\nprocess.env.CARHER_KEY;\n")
        portal.files["/ai/i/pkg/cursor-mac.zip"] = buf.getvalue()
    else:
        portal.files[f"/ai/i/pkg/{bad}-mac.command"] = fail.replace("$STUB_NAME", bad).encode()
    portal.codes["ALL3"] = {"tool": "all", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, ["--code", "ALL3"], timeout=120)
    assert rc == 7, f"部分成功要用 7(拿到 {rc}):\n{out}"
    # 另外两个照样跑完、拿到同一把 key
    for t in ALL_ORDER:
        got = stub_got(logs, t)
        assert got and got["key"] == KEY, (t, out[-800:])
    label = {"codex": "Codex", "claude": "Claude Code", "cursor": "Cursor"}
    assert ("✗ %s  没装成" % label[bad]) in out, out[-900:]
    assert "退出码 9" in out
    for t in ALL_ORDER:
        if t != bad:
            assert ("✔ %s  装好了" % label[t]) in out, t
    # 怎么补:回网页单独选那个工具再装一次
    assert "单独选" in out and label[bad] in out and "2 个装好了" in out
    assert KEY not in out
    assert [json.loads(x) for x in portal.requests] == [{"code": "ALL3"}]   # 还是只换一次


def test_all_three_failures_exit_8(work, portal):
    """三个都没成 ⇒ 退出码 8,和部分成功(7)分得开。"""
    fail = STUB_SH.replace('exit "${STUB_RC:-0}"', "exit 9")
    for t in ("codex", "claude"):
        portal.files[f"/ai/i/pkg/{t}-mac.command"] = fail.replace("$STUB_NAME", t).encode()
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cursor-mac/cursor_team_setup.sh", fail.replace("$STUB_NAME", "cursor"))
        z.writestr("cursor-mac/cursor_team_setup.js", "// stub\nprocess.env.CARHER_KEY;\n")
    portal.files["/ai/i/pkg/cursor-mac.zip"] = buf.getvalue()
    portal.codes["ALL4"] = {"tool": "all", "key": KEY, "state": "ok"}
    rc, out, _ = run_sh(work, portal, ["--code", "ALL4"], timeout=120)
    assert rc == 8, out[-800:]
    assert "三个都没装成" in out
    for lbl in ("Codex", "Claude Code", "Cursor"):
        assert ("✗ " + lbl) in out, lbl


def test_all_download_failure_counts_as_one_tool_only(work, portal):
    """不是安装器退出码,而是**下载安装器本身**失败(镜像 404)⇒ 也只算这一个工具失败。"""
    del portal.files["/ai/i/pkg/claude-mac.command"]
    portal.codes["ALL5"] = {"tool": "all", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, ["--code", "ALL5"], timeout=120)
    assert rc == 7, out[-800:]
    assert stub_got(logs, "codex") and stub_got(logs, "cursor"), out[-800:]
    assert not stub_got(logs, "claude")
    assert "✗ Claude Code  没装成" in out and "下载安装器失败" in out


def test_all_tool_follows_code_not_url(work, portal):
    """网址是单工具、码是 all ⇒ 按码装三个(反之见 test_tool_follows_code_not_url)。"""
    portal.codes["ALL6"] = {"tool": "all", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, ["--code", "ALL6", "--tool", "cursor"], timeout=120)
    assert rc == 0, out
    assert "按安装码装三个" in out
    assert all(stub_got(logs, t) for t in ALL_ORDER)


def test_single_tool_output_unchanged_by_all_support(work, portal):
    """🔴 单工具路径一个字节都不许变:还是那句「✅ 全部完成。」,不出任何 all 的字样。"""
    portal.codes["SOLO"] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, ["--code", "SOLO"])
    assert rc == 0, out
    assert "✔ 安装码有效:装 codex,key 已拿到" in out
    assert "✅ 全部完成。" in out
    for s in ("三个都装", "第 1/3 个", "安装结果", "装好了。"):
        assert s not in out, s
    assert stub_got(logs, "codex")["key"] == KEY
    assert not stub_got(logs, "claude") and not stub_got(logs, "cursor")


def test_all_installer_is_byte_identical_to_single(portal):
    """/ai/i/all.sh 和 /ai/i/<tool>.sh 是同一个文件(nginx alias 同一份 install.sh)。"""
    portal.files["/ai/i/all.sh"] = SH.read_bytes()
    assert portal.files["/ai/i/all.sh"] == portal.files["/ai/i/cursor.sh"]


def test_old_cursor_engine_falls_back_to_key_flag(work, portal):
    """已发出去的老 cursor-mac.zip 引擎不认 CARHER_KEY ⇒ 退回 --key(老口子)。"""
    portal.files["/ai/i/pkg/cursor-mac.zip"] = cursor_zip(False)
    portal.codes["GOOD2"] = {"tool": "cursor", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, ["--code", "GOOD2"])
    assert rc == 0, out
    assert stub_got(logs, "cursor")["argv"] == ["--apply", "--key", KEY]
    assert KEY not in out


def test_tool_follows_code_not_url(work, portal):
    portal.codes["GOOD3"] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, ["--code", "GOOD3", "--tool", "cursor"])
    assert rc == 0, out
    assert stub_got(logs, "codex") and not stub_got(logs, "cursor")
    assert "按安装码装 codex" in out


# ── 2. 码错 / 过期 / 用过 / 服务挂 ⇒ 非 0 + 服务端人话原样 ─────────────────────
@pytest.mark.parametrize("state,code,msg", [
    ("expired", "OLD1", "安装码已过期(10 分钟有效),请回网页重新生成"),
    (None, "NOPE1", "安装码不存在,请回网页重新生成"),
])
def test_bad_code_prints_server_message(work, portal, state, code, msg):
    if state:
        portal.codes[code] = {"tool": "codex", "key": KEY, "state": state}
    rc, out, logs = run_sh(work, portal, ["--code", code])
    assert rc != 0
    assert msg in out, out
    assert not stub_got(logs, "codex"), "码没换成功还调起了安装器"
    assert KEY not in out


def test_code_is_single_use(work, portal):
    portal.codes["ONCE"] = {"tool": "claude", "key": KEY, "state": "ok"}
    rc1, out1, _ = run_sh(work, portal, ["--code", "ONCE"])
    assert rc1 == 0, out1
    rc2, out2, _ = run_sh(work, portal, ["--code", "ONCE"])
    assert rc2 != 0 and "安装码已经用过了" in out2 and "HTTP 409" in out2


def test_gateway_error_page_is_shown_and_key_scrubbed(work, portal):
    portal.redeem_override = (502, f"<html>502 Bad Gateway {KEY}</html>".encode(), "text/html")
    rc, out, _ = run_sh(work, portal, ["--code", "X1"])
    assert rc != 0 and "HTTP 502" in out and "502 Bad Gateway" in out
    assert KEY not in out


def test_unreachable_portal(work, portal):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    rc, out, _ = run_sh(work, portal, ["--code", "X2"], {"CARHER_PORTAL_BASE": f"http://127.0.0.1:{dead}/ai"})
    assert rc != 0 and "连不上安装服务" in out


@pytest.mark.parametrize("code", ['A"B', "a b", "x\\y", "码"])
def test_weird_code_rejected_before_network(work, portal, code):
    rc, out, _ = run_sh(work, portal, ["--code", code])
    assert rc != 0 and "安装码格式不对" in out
    assert portal.requests == [], "格式不对的码不许发出去(要拼进 JSON)"


def test_missing_code(work, portal):
    rc, out, _ = run_sh(work, portal, [])
    assert rc != 0 and "没有安装码" in out


# 真安装码的形状:4 组 8 个 base32 字符用 - 连起来(server.py:1614-1616 实读 ⇒ 35 字符)
REAL_CODE = "ABCDEFGH-IJKLMNOP-QRSTUVWX-YZ234567"


@pytest.mark.parametrize("args,desc", [
    ([REAL_CODE], "只粘了码(少复制了 --code 那几个字)"),
    (["--tool", "cursor", REAL_CODE], "裸码 + --tool"),
    (["--nonsense", REAL_CODE], "未知 flag + 裸码"),
])
def test_bare_code_without_the_code_flag_still_works(work, portal, args, desc):
    """用户少复制了 `--code` 时也得能装(零操作)。修前实测 `install.sh <码>` ⇒ rc=1 +
    「命令里没有安装码」—— 码明明就在命令里。
    正向对照见 test_short_positional_is_not_mistaken_for_a_code(短串不许被当成码)。"""
    portal.codes[REAL_CODE] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, args)
    assert rc == 0, f"{desc}:\n{out[-700:]}"
    assert stub_got(logs, "codex")["key"] == KEY, desc
    assert [json.loads(x) for x in portal.requests] == [{"code": REAL_CODE}], portal.requests


@pytest.mark.parametrize("junk", ["short", "ABCDEFGH", "--weird", "-x"])
def test_short_positional_is_not_mistaken_for_a_code(work, portal, junk):
    """🔴 正向对照:比真码短 / 像 flag 的东西**不许**被当成安装码发出去
    (否则等于把任意参数当码往服务端打)。"""
    rc, out, _ = run_sh(work, portal, [junk])
    assert rc != 0, out[-500:]
    assert "没有安装码" in out, out[-500:]
    assert portal.requests == [], f"{junk!r} 被当成码发出去了:{portal.requests}"


@need_pwsh
def test_ps1_bare_code_binds_positionally(work, portal):
    """Windows 侧不用改:param 的第一个位置就是 Code(实测 `-File install.ps1 ABC` ⇒ Code=[ABC])。
    这条钉住它,免得以后有人往 param 前面插别的参数。"""
    portal.files["/ai/i/pkg/codex-windows.bat"] = _win_stub_bat("codex")
    portal.codes[REAL_CODE] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, logs = run_ps(work, portal, ["-NoProfile", "-File", str(PS1), REAL_CODE])
    assert rc == 0, out[-900:]
    assert stub_got(logs, "codex")["key"] == KEY, out[-900:]


def test_installer_failure_propagates(work, portal):
    portal.codes["GOOD4"] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, _ = run_sh(work, portal, ["--code", "GOOD4"], {"STUB_RC": "1"})
    assert rc == 1 and "安装器没跑完(退出码 1)" in out and "回网页重新生成" in out


# ── 3. curl | bash 形态:stdin 被管道占着也不卡、脚本完整跑到底 ────────────────
@pytest.mark.parametrize("tool", ["claude", "cursor"])
def test_curl_pipe_bash_does_not_hang(work, portal, tool):
    portal.codes["PIPE1"] = {"tool": tool, "key": KEY, "state": "ok"}
    url = f"{portal.base}/ai/i/{tool}.sh"
    line = f"curl -fsSL {url} | bash -s -- --code PIPE1"
    # 外层 stdin 也给一个**一直开着**的管道(像终端那样):脚本里但凡有一处读 stdin 就会卡到 timeout
    p_r, p_w = os.pipe()
    try:
        rc, out, logs = run_sh(work, portal, [], stdin=p_r, cmd=["bash", "-c", line], timeout=60)
    finally:
        os.close(p_r)
        os.close(p_w)
    assert rc == 0, out
    assert "全部完成" in out
    got = stub_got(logs, tool)
    assert got["key"] == KEY and got["stdin"] == "EOF"
    assert KEY not in out


# ── 4. App:装 / 拒装(mac;mdfind/ps/pgrep/open/osascript 全是桩) ─────────────
def _fake_app(root: Path, bundle_id: str, name="Claude.app"):
    a = root / name
    (a / "Contents" / "MacOS").mkdir(parents=True)
    (a / "Contents" / "Resources").mkdir(parents=True)
    (a / "Contents" / "Resources" / "app.asar").write_bytes(b"fake")
    (a / "Contents" / "MacOS" / "Claude").write_text("#!/bin/sh\n")
    with (a / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, f)
    return a


def _app_env(work):
    dest = work / "Applications"
    dest.mkdir(exist_ok=True)
    return {"CARHER_APP_ROOTS": str(dest), "CARHER_APP_DEST_ROOTS": f"{work}/ro-not-there:{dest}"}, dest


@mac_only
def test_unsigned_app_from_mirror_is_refused(work, portal):
    """镜像里的包 bundle id 对、但没有官方签名 ⇒ 拒装,不调安装器。"""
    src = work / "pkgsrc"
    _fake_app(src, "com.anthropic.claudefordesktop")
    z = work / "Claude-mac.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(src / "Claude.app"), str(z)], check=True)
    portal.files["/ccdl/desktop/Claude-mac.zip"] = z.read_bytes()
    portal.codes["APP1"] = {"tool": "claude", "key": KEY, "state": "ok"}
    env, dest = _app_env(work)
    rc, out, logs = run_sh(work, portal, ["--code", "APP1"], env, skip_app=False)
    assert rc != 0 and "签名校验没过,拒绝安装" in out, out
    assert not (dest / "Claude.app").exists()
    assert not stub_got(logs, "claude")


@mac_only
def test_dmg_wrong_bundle_id_refused_and_unmounted(work, portal):
    """dmg 那条路(Cursor/Codex):挂载 → 认 bundle id → 不对就拒,且退出时卸载干净。"""
    src = work / "dmgsrc"
    _fake_app(src, "com.example.notcodex", name="Codex.app")
    dmg = work / "Codex-mac.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-fs", "HFS+", "-srcfolder", str(src), "-ov", str(dmg)], check=True)
    portal.files["/ccdl/apps/Codex-mac.dmg"] = dmg.read_bytes()
    portal.codes["APP2"] = {"tool": "codex", "key": KEY, "state": "ok"}
    env, dest = _app_env(work)
    rc, out, logs = run_sh(work, portal, ["--code", "APP2"], env, skip_app=False)
    assert rc != 0 and "bundle id 不对" in out, out
    info = subprocess.run(["hdiutil", "info"], capture_output=True, text=True).stdout
    assert str(work) not in info, "dmg 没卸载"
    assert not stub_got(logs, "codex")


@mac_only
def test_already_installed_app_is_skipped_and_reopened(work, portal):
    env, dest = _app_env(work)
    _fake_app(dest, "com.anthropic.claudefordesktop")
    portal.codes["APP3"] = {"tool": "claude", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, ["--code", "APP3"], env, skip_app=False)
    assert rc == 0, out
    assert "已装 Claude 桌面 App" in out and "从公司镜像下载" not in out
    assert stub_got(logs, "claude")["key"] == KEY
    assert f"open {dest / 'Claude.app'}" in (logs / "open.log").read_text()


@mac_only
@pytest.mark.skipif(not os.environ.get("CARHER_TEST_REAL_APP"), reason="慢:设 CARHER_TEST_REAL_APP=1 才跑")
def test_real_signed_app_installs_from_mirror(work, portal):
    """端到端装 App:拿本机正版 Claude.app 打成镜像 zip(只读源,不动它),装进临时目录。"""
    real = Path("/Applications/Claude.app")
    if not real.exists():
        pytest.skip("本机没有 /Applications/Claude.app 可当镜像")
    z = work / "Claude-mac.zip"
    subprocess.run(["ditto", "-c", "-k", "--zlibCompressionLevel", "0", "--keepParent", str(real), str(z)], check=True)
    portal.files["/ccdl/desktop/Claude-mac.zip"] = z.read_bytes()
    z.unlink()
    portal.codes["APP4"] = {"tool": "claude", "key": KEY, "state": "ok"}
    env, dest = _app_env(work)
    rc, out, logs = run_sh(work, portal, ["--code", "APP4"], env, skip_app=False, timeout=600)
    assert rc == 0, out
    inst = dest / "Claude.app"
    assert inst.is_dir() and "已装到" in out
    xa = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(inst)], capture_output=True)
    assert xa.returncode != 0, "隔离属性没去掉"
    assert stub_got(logs, "claude")["key"] == KEY
    assert f"open {inst}" in (logs / "open.log").read_text()
    portal.files.clear()


# ── 5. 静态:find_app 副本不漂、bash 3.2 全角坑、语法 ─────────────────────────
def test_embedded_find_app_is_byte_identical_to_source():
    src = (ROOT / "codex-oneclick" / "src" / "find_app_mac.sh").read_text(encoding="utf-8")
    m = re.search(r"^# >>> find_app_mac\.sh.*?>>>\n(.*?)^# <<< find_app_mac\.sh <<<\n", SH.read_text(encoding="utf-8"),
                  re.S | re.M)
    assert m, "install.sh 里没找到 find_app_mac.sh 那段"
    assert m.group(1) == src, "install.sh 里的 find_app_mac.sh 和 codex-oneclick/src 那份不一致了 —— 改源以后要同步过来"


@pytest.mark.parametrize("f", [SH, ROOT / "codex-oneclick/src/install-mac.sh", ROOT / "claude-oneclick/src/install-mac.sh",
                               ROOT / "codex-oneclick/src/find_app_mac.sh"])
def test_bash_syntax_and_cjk_lint(f):
    assert subprocess.run(["/bin/bash", "-n", str(f)], capture_output=True).returncode == 0
    lint = ROOT / "scripts" / "lint-bash-cjk-vars.py"
    r = subprocess.run(["python3", str(lint), str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ── 5b. 解释器矩阵:install.sh 要在 bash / sh / zsh / dash 下都真跑得对 ────────────
# 11-05 实测三条真缺陷(修前):
#   zsh  ⇒ `${PIPESTATUS[0]}: parameter not set`(安装器装成功了却报错 rc=1)
#   zsh  ⇒ `for t in $ALL_TOOLS` 不分词 ⇒ tool=all 变「第 1/1 个:codex claude cursor」rc=8
#   dash ⇒ 第 37 行 `set: Illegal option -o pipefail`,一个中文字都没打(rc=2)
# 用户默认 shell 是 zsh(mac),`curl | sh` 在 Linux 上多是 dash ⇒ 这四个都是真路径。
INTERPS = [p for p in ("/bin/bash", "/bin/sh", "/bin/zsh", "/bin/dash") if Path(p).exists()]


@pytest.mark.parametrize("interp", INTERPS)
@pytest.mark.parametrize("tool", ["codex", "cursor"])
def test_sh_runs_under_every_interpreter(work, portal, interp, tool):
    """单工具:四种解释器下都 rc=0、安装器真被调起、key 没上屏。"""
    portal.codes["IN1"] = {"tool": tool, "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, [], cmd=[interp, str(SH), "--code", "IN1"])
    assert rc == 0, f"{interp} rc={rc}:\n{out}"
    got = stub_got(logs, tool)
    assert got and got["key"] == KEY, f"{interp}:\n{out}"
    assert "全部完成" in out, out[-600:]
    assert KEY not in out
    # 解释器自己的报错一个都不许漏出来(这是修前 zsh / dash 的形状)
    for bad in ("PIPESTATUS", "parameter not set", "Illegal option", "Bad substitution", "bad substitution"):
        assert bad not in out, (interp, bad, out[-600:])


@pytest.mark.parametrize("interp", INTERPS)
def test_sh_all_runs_three_under_every_interpreter(work, portal, interp):
    """tool=all:四种解释器下都真装三个(修前 zsh 只循环一次、三个全没装)。"""
    portal.codes["IN2"] = {"tool": "all", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, [], cmd=[interp, str(SH), "--code", "IN2"], timeout=120)
    assert rc == 0, f"{interp} rc={rc}:\n{out[-1200:]}"
    seen = re.findall(r"第 \d/3 个:(codex|claude|cursor)", out)
    assert seen == ALL_ORDER, (interp, seen, out[-800:])
    for t in ALL_ORDER:
        got = stub_got(logs, t)
        assert got and got["key"] == KEY, (interp, t, out[-800:])
    assert "✅ 三个都装好了" in out
    # 修前 zsh 的形状:工具名变成整串
    assert "codex claude cursor" not in out, out[-800:]
    assert KEY not in out


@pytest.mark.parametrize("interp", INTERPS)
def test_installer_failure_code_is_seen_under_every_interpreter(work, portal, interp):
    """🔴 退出码改成走文件了(不再用 PIPESTATUS)⇒ 四种解释器下**失败仍必须被看见**。
    正向对照:这条就是「rc 读不回来」的对照 —— 若 rc 丢了会被当 0 ⇒ 这里的 rc==9 断言变红。"""
    portal.codes["IN3"] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, _ = run_sh(work, portal, [], {"STUB_RC": "9"}, cmd=[interp, str(SH), "--code", "IN3"])
    assert rc == 9, f"{interp} 安装器退出码 9 没传出来(拿到 {rc}):\n{out[-800:]}"
    assert "安装器没跑完(退出码 9)" in out, out[-600:]


def test_sh_has_no_bashism_left():
    """静态兜底:四个已踩过的 bash/zsh-only 构造不许再回来(语法检查查不出它们)。"""
    body = "\n".join(l for l in SH.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
    for pat, why in [(r"\bPIPESTATUS\b", "zsh 没有这个数组"),
                     (r"set -[a-z]*o[a-z]* pipefail|set -o pipefail", "dash 不认 pipefail"),
                     (r"\$\{[A-Za-z_][A-Za-z0-9_]*: -[0-9]", "${x: -n} dash 报 Bad substitution"),
                     (r"\$'", "$'...' dash 不认"),
                     (r"for [a-zA-Z_]+ in \$[A-Za-z_][A-Za-z0-9_]*(\s|;)", "zsh 不对普通变量分词")]:
        m = re.search(pat, body)
        assert not m, f"install.sh 里又出现了 {why}:{m.group(0)!r}"


def test_script_body_is_wrapped_in_main():
    """curl | bash 边下边跑:脚本要整个包在函数里、最后一行才调用(下载断在半截不会执行半截)。"""
    lines = [l for l in SH.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines[-1] == 'main "$@"'
    code = [l for l in lines if not l.startswith("#")]
    assert code[0] == "main() {"


# ── 5c. 失败出口都要有人话 + 下一步(不许只回一个非零码 / 只漏工具的英文报错)──────────
def _thin_path(work: Path, keep_out) -> Path:
    """造一个只含指定命令的 PATH 目录(模拟「这台电脑上没有 X」)。"""
    b = work / ("thin-" + "".join(sorted(keep_out))[:8].replace("/", "_"))
    b.mkdir(exist_ok=True)
    base = ("uname mktemp sed head printf date mv rm cat tr sort awk ls find curl unzip bash "
            "basename mkdir grep sleep chmod dirname perl cp touch df").split()
    for n in base:
        if n in keep_out:
            continue
        src = shutil.which(n)
        if src:
            tgt = b / n
            if not tgt.exists():
                tgt.symlink_to(src)
    return b


def test_no_curl_gives_plain_chinese_not_error_as_http_code(work, portal):
    """🔴 修前:curl 不在 PATH 时,-w 那段不输出 ⇒ 「command not found」被填进 HTTP 码的位置,
    屏幕原话 `✗ 安装码没换成功(HTTP install.sh: line 108: curl: command not found)`
    —— 错误信封当数据读。现在要给一句人话 + 怎么办。"""
    portal.codes["NC1"] = {"tool": "codex", "key": KEY, "state": "ok"}
    thin = _thin_path(work, {"curl"})
    rc, out, logs = run_sh(work, portal, ["--code", "NC1"], {"PATH": str(thin)})
    assert rc != 0
    assert "没有 curl" in out, out[-700:]
    assert "新开一个终端窗口" in out or "apt install curl" in out, out[-700:]
    assert "HTTP install.sh" not in out and "command not found)" not in out, out[-700:]
    assert not stub_got(logs, "codex")


def test_no_bash_says_you_need_bash_not_broken_package(work, portal):
    """修前:bash 不在 PATH ⇒ `bash -n` 失败 ⇒ 报「安装器下载不完整」,
    把「你缺 bash」说成「我们的包坏了」(实测)。"""
    portal.codes["NB1"] = {"tool": "codex", "key": KEY, "state": "ok"}
    thin = _thin_path(work, {"bash"})
    rc, out, _ = run_sh(work, portal, [], {"PATH": str(thin)},
                        cmd=["/bin/sh", str(SH), "--code", "NB1"])
    assert rc != 0
    assert "没有 bash" in out, out[-700:]
    assert "下载不完整" not in out, "缺 bash 被说成包坏了:\n" + out[-700:]


def test_unusable_tmpdir_falls_back_then_says_disk_in_chinese(work, portal):
    """TMPDIR 指到不存在的盘:先退 /tmp 把活干完(用户零操作);两个都不行才报磁盘满。
    修前只漏 mktemp 的英文一行 `mktemp: mkdtemp failed on …: No such file or directory`。"""
    portal.codes["TD1"] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, logs = run_sh(work, portal, ["--code", "TD1"],
                           {"TMPDIR": str(work / "no-such-dir-xyz") + "/"})
    assert rc == 0, out[-800:]           # 退到 /tmp 后照样装完 ⇒ 用户不用做任何事
    assert "改用 /tmp" in out, out[-700:]
    assert "mkdtemp failed" not in out, out[-700:]
    assert stub_got(logs, "codex")["key"] == KEY


def test_mask_stream_without_perl_still_masks_the_key(work, portal):
    """🔴 修前:没有 perl 时 mask_stream 退回 `cat` ⇒ 遮罩整个关掉,
    屏幕上真出现 `stub: 我是被调起的安装器,key=<整把 key>`(实测 KEY_ON_SCREEN=True)。
    正向对照:把本仓这条修改回 `else cat; fi`,这个断言立刻变红。"""
    portal.codes["NP1"] = {"tool": "codex", "key": KEY, "state": "ok"}
    thin = _thin_path(work, {"perl"})
    rc, out, logs = run_sh(work, portal, ["--code", "NP1"], {"PATH": str(thin)})
    assert rc == 0, out[-800:]
    assert stub_got(logs, "codex")["key"] == KEY     # 安装器照样拿到 key
    assert KEY not in out, "没有 perl 时 key 上屏了(遮罩被关掉了):\n" + out[-700:]
    assert "key=***" in out, "awk 兜底没遮成 ***:\n" + out[-700:]


def test_mask_stream_without_perl_or_awk_hides_output_rather_than_leak(work, portal):
    """perl / awk 都没有:宁可不显示子安装器的输出,也不许 key 上屏。"""
    portal.codes["NA1"] = {"tool": "codex", "key": KEY, "state": "ok"}
    thin = _thin_path(work, {"perl", "awk"})
    rc, out, logs = run_sh(work, portal, ["--code", "NA1"], {"PATH": str(thin)})
    assert stub_got(logs, "codex")["key"] == KEY
    assert KEY not in out, out[-700:]
    assert "没有 perl / awk" in out, out[-700:]


# ── 5d. key 的安全:不许进 log / 临时文件 / 命令行参数(ps) / 报错 ────────────────────
# 🔴 `ps` 能看到 argv 是实测过的,不是推测:`/bin/sh ./psleak.sh --apply --key sk-SECRETLEAKTEST123`
#    在 `ps -Ao args=` 里原样出现(11-05)。所以 key 只许走环境变量。
def test_key_never_lands_in_tempfiles_or_ps_or_output(work, portal):
    """安装器在跑的**那一刻**抓现场:扫整个临时目录树 + `ps -Ao args=` + 屏幕输出。
    正向对照见 test_key_audit_catches_a_planted_leak(故意种一处泄漏,这套断言必须变红)。"""
    # 桩安装器拖住 6 秒,给 Python 时间抓现场
    slow = STUB_SH.replace('exit "${STUB_RC:-0}"', 'sleep 6\nexit 0')
    portal.files["/ai/i/pkg/codex-mac.command"] = slow.replace("$STUB_NAME", "codex").encode()
    portal.codes["SEC1"] = {"tool": "codex", "key": KEY, "state": "ok"}
    found = _run_and_audit(work, portal, "SEC1")
    assert found["stub_key"] == KEY, "桩没拿到 key(台架自己坏了,下面的断言就不算数)"
    assert not found["tmp_hits"], f"key 落到了临时文件里:{found['tmp_hits']}"
    assert not found["ps_hits"], f"key 出现在命令行参数里(ps 看得见):{found['ps_hits']}"
    assert KEY not in found["out"], "key 上屏了"


def _run_and_audit(work: Path, portal: Portal, code: str, extra_env=None):
    """跑一遍 install.sh,在子安装器跑着的时候扫 临时目录 / ps;回来再扫输出。
    STUB_LOG 是台架自己存 key 的地方(断言要读),扫的时候排除它。"""
    import glob
    import time
    tmp = work / "tmp"
    tmp.mkdir(exist_ok=True)
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    hits = {"tmp_hits": [], "ps_hits": [], "stub_key": None}

    def scan():
        for root in glob.glob(str(tmp / "carher-install.*")):
            for dp, _dn, fn in os.walk(root):
                for n in fn:
                    f = Path(dp) / n
                    try:
                        if KEY.encode() in f.read_bytes():
                            hits["tmp_hits"].append(str(f))
                    except Exception:
                        pass
        ps = subprocess.run(["ps", "-Ao", "args="], capture_output=True).stdout.decode("utf-8", "replace")
        for line in ps.splitlines():
            if KEY in line:
                hits["ps_hits"].append(line[:200])

    done = threading.Event()

    def watcher():
        while not done.wait(0.4):
            scan()

    th = threading.Thread(target=watcher, daemon=True)
    th.start()
    try:
        rc, out, _ = run_sh(work, portal, ["--code", code], extra_env, timeout=120)
    finally:
        done.set()
        th.join(timeout=5)
    time.sleep(0.1)
    hits["out"] = out
    hits["rc"] = rc
    got = stub_got(logs, "codex")
    hits["stub_key"] = got["key"] if got else None
    return hits


def test_key_audit_catches_a_planted_leak(work, portal):
    """🔴 正向对照:故意让子安装器把 key 写进**临时目录**里一个文件 +
    用带 key 的命令行起一个子进程。上面那条审计的两个断言必须真的能抓到它们。"""
    # 🔴 种 argv 泄漏必须用「sh <脚本文件> --key K」这个形状,不能用 `sh -c 'sleep 6' --key K`:
    #    后者 sh 只有一条命令时会直接 exec 掉自己 ⇒ argv 被 `sleep 6` 顶替,ps 里反而看不到 key。
    #    第一版对照就是这么写的,结果「种了泄漏却抓不到」—— 是对照自己坏了,不是审计坏了(实测确认:
    #    `sh -c 'sleep 5' --planted-key SECRETXYZ` ⇒ ps 无;`sh sc.sh --key SECRETABC` ⇒ ps 里原样可见)。
    leaky = STUB_SH.replace('exit "${STUB_RC:-0}"',
                            'printf "%s" "$CARHER_KEY" > "$(dirname "$0")/leaked.txt"\n'
                            'printf "#!/bin/sh\\nsleep 6\\n" > "$(dirname "$0")/planted.sh"\n'
                            'sh "$(dirname "$0")/planted.sh" --planted-key "$CARHER_KEY" &\n'
                            'sleep 6\nexit 0')
    portal.files["/ai/i/pkg/codex-mac.command"] = leaky.replace("$STUB_NAME", "codex").encode()
    portal.codes["SEC2"] = {"tool": "codex", "key": KEY, "state": "ok"}
    found = _run_and_audit(work, portal, "SEC2")
    assert found["tmp_hits"], "种了一处临时文件泄漏却没抓到 ⇒ 这套审计是假的"
    assert found["ps_hits"], "种了一处 argv 泄漏(ps 看得见)却没抓到 ⇒ 这套审计是假的"


def test_key_not_in_rc_or_err_sidecar_files(work, portal):
    """退出码改走文件了(inst.rc)、tool=all 的失败原因也写文件(<tool>.err)⇒
    这两类「我们自己写的」文件里绝不能有 key。失败路径也要扫(报错信息里也不许有)。"""
    fail = STUB_SH.replace('exit "${STUB_RC:-0}"', "sleep 4\nexit 9")
    for t in ("codex", "claude"):
        portal.files[f"/ai/i/pkg/{t}-mac.command"] = fail.replace("$STUB_NAME", t).encode()
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cursor-mac/cursor_team_setup.sh", fail.replace("$STUB_NAME", "cursor"))
        z.writestr("cursor-mac/cursor_team_setup.js", "// stub\nprocess.env.CARHER_KEY;\n")
    portal.files["/ai/i/pkg/cursor-mac.zip"] = buf.getvalue()
    portal.codes["SEC3"] = {"tool": "all", "key": KEY, "state": "ok"}
    found = _run_and_audit(work, portal, "SEC3")
    assert found["rc"] == 8, found["out"][-600:]
    assert not found["tmp_hits"], f"失败路径把 key 写进临时文件了:{found['tmp_hits']}"
    assert not found["ps_hits"], f"失败路径让 key 进了 argv:{found['ps_hits']}"
    assert KEY not in found["out"]


def test_old_cursor_engine_key_in_argv_is_documented_risk():
    """🔴 老 cursor 引擎(不认 CARHER_KEY)那一支是**唯一**把 key 放进命令行的地方
    ⇒ `ps` 看得见(11-05 实测 argv 在 `ps -Ao args=` 里原样出现)。
    现役包已经认 CARHER_KEY(scripts/zk-cursor-web/cursor-mac.zip 实测命中 2 处)⇒ 正常走不到那支。
    这条钉住:① 代码里那支必须带警告注释;② 现役包必须还认环境变量(认了就走不到老支)。"""
    body = SH.read_text(encoding="utf-8")
    i = body.index("sh \"$ENG/cursor_team_setup.sh\" --apply --key \"$KEY\"")
    ctx = body[max(0, i - 900):i]
    assert "ps 看不到" in ctx or "ps 看得见" in ctx, "老引擎 --key 那支旁边要留着「key 进 argv」的警告"
    z = ROOT / "scripts/zk-cursor-web/cursor-mac.zip"
    if z.exists():
        js = subprocess.run(["unzip", "-p", str(z), "cursor-mac/cursor_team_setup.js"],
                            capture_output=True).stdout.decode("utf-8", "replace")
        assert "process.env.CARHER_KEY" in js, \
            "现役 cursor-mac.zip 不认 CARHER_KEY 了 ⇒ 会退回 --key ⇒ key 进 argh/ps。重建这个包"


# ── 5f. 幂等 / 可重跑:连跑两次要干净,且 install.sh 自己不碰用户配置 ──────────────────
QUIET_STUB = ('#!/usr/bin/env bash\nL="$STUB_LOG/$STUB_NAME"; mkdir -p "$L"\n'
              'printf "%s" "${CARHER_KEY:-}" > "$L/key"\n: > "$L/argv"\necho EOF > "$L/stdin"\nexit 0\n')


def _quiet_pkgs(portal: Portal):
    """安装器桩什么都不写(真安装器才写配置)⇒ HOME 里的任何变化都只能是 install.sh 自己干的。"""
    import io
    for t in ("codex", "claude"):
        portal.files[f"/ai/i/pkg/{t}-mac.command"] = QUIET_STUB.replace("$STUB_NAME", t).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cursor-mac/cursor_team_setup.sh", QUIET_STUB.replace("$STUB_NAME", "cursor"))
        z.writestr("cursor-mac/cursor_team_setup.js", "// stub\nprocess.env.CARHER_KEY;\n")
    portal.files["/ai/i/pkg/cursor-mac.zip"] = buf.getvalue()


def _snap(root: Path):
    s = {}
    for dp, dn, fn in os.walk(root):
        for n in fn:
            f = Path(dp) / n
            try:
                s[str(f)] = (f.stat().st_size, f.read_bytes()[:4096])
            except Exception:
                s[str(f)] = None
        for n in dn:
            s[str(Path(dp) / n) + "/"] = "dir"
    return s


def test_install_sh_never_writes_into_home_itself(work, portal):
    """install.sh 这一层不许往 $HOME 里写任何东西(写配置是子安装器的事,它们自己做备份)。
    这条也是「用户已有配置不许被覆盖没了」的第一道闸:这一层根本不碰,就谈不上覆盖。"""
    _quiet_pkgs(portal)
    portal.codes["HM1"] = {"tool": "all", "key": KEY, "state": "ok"}
    home = work / "home"
    home.mkdir(exist_ok=True)
    # 放一份「用户已有配置」进去,跑完必须一字不动
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "config.toml").write_text("model = '我的旧配置'\n", encoding="utf-8")
    (home / ".codex" / "sessions").mkdir(exist_ok=True)
    (home / ".codex" / "sessions" / "s1.jsonl").write_text('{"keep":"历史会话不许丢"}\n', encoding="utf-8")
    before = _snap(home)
    rc, out, logs = run_sh(work, portal, ["--code", "HM1"], timeout=120)
    assert rc == 0, out[-800:]
    after = _snap(home)
    assert after == before, f"install.sh 自己动了 $HOME 里的东西:\n新增/改动 = {set(after) ^ set(before)}"


def test_running_twice_is_clean_and_keeps_user_config(work, portal):
    """同一台机器连跑两次(两个码,码是一次性的):第二次照样成功、不装两份、
    用户已有配置一字不动、临时目录不留垃圾。"""
    _quiet_pkgs(portal)
    home = work / "home"
    home.mkdir(exist_ok=True)
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text('{"mine": "别覆盖我"}\n', encoding="utf-8")
    before = _snap(home)
    for i in (1, 2):
        portal.codes[f"TW{i}"] = {"tool": "all", "key": KEY, "state": "ok"}
        rc, out, logs = run_sh(work, portal, ["--code", f"TW{i}"], timeout=120)
        assert rc == 0, f"第 {i} 次跑失败:\n{out[-800:]}"
        assert "✅ 三个都装好了" in out, (i, out[-500:])
        for t in ALL_ORDER:
            assert stub_got(logs, t)["key"] == KEY, (i, t)
    assert _snap(home) == before, "跑两次之后用户配置变了"
    # 临时目录不留东西(每个 App 包几百 MB,留着就是用户的盘)
    leftover = list((work / "tmp").glob("carher-install.*"))
    assert not leftover, f"临时目录有残留:{leftover}"


@mac_only
def test_second_run_skips_already_installed_app(work, portal):
    """App 已经装上了 ⇒ 第二次不许再下一遍几百 MB,也不许装出第二份。"""
    _quiet_pkgs(portal)
    env, dest = _app_env(work)
    _fake_app(dest, "com.anthropic.claudefordesktop")
    for i in (1, 2):
        portal.codes[f"AP{i}"] = {"tool": "claude", "key": KEY, "state": "ok"}
        rc, out, logs = run_sh(work, portal, ["--code", f"AP{i}"], env, skip_app=False)
        assert rc == 0, (i, out[-800:])
        assert "已装 Claude 桌面 App" in out, (i, out[-500:])
        assert "从公司镜像下载" not in out, f"第 {i} 次又去下 App 了:\n{out[-500:]}"
    assert sorted(x.name for x in dest.iterdir()) == ["Claude.app"], \
        f"装出了多份:{sorted(x.name for x in dest.iterdir())}"


@pytest.mark.parametrize("interp", INTERPS)
def test_tempdir_cleaned_up_on_failure(work, portal, interp):
    """失败退出时也要把临时目录删掉(里面可能有刚下了一半的几百 MB)。"""
    portal.codes["CL1"] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, _ = run_sh(work, portal, [], {"STUB_RC": "9"}, cmd=[interp, str(SH), "--code", "CL1"])
    assert rc == 9, out[-600:]
    leftover = list((work / "tmp").glob("carher-install.*"))
    assert not leftover, f"{interp} 失败后临时目录没清:{leftover}"


# ── 5e. 下载被掐断:要么续传、要么重下,不能让用户从头跑一遍(安装码只有 10 分钟)──────
class FlakyPortal(Portal):
    """前 N 次 GET 在传到一半时掐断连接(= 公司网抖一下的形状),之后正常。
    支持 Range(和线上 nginx 静态目录一致)⇒ 能验证断点续传真的接上了。"""

    def __init__(self, break_times=2, size=600_000):
        self.break_left = break_times
        self.blob = b"Z" * size
        self.get_count = 0
        self.range_requests = []
        super().__init__()
        portal = self
        inner = self.srv.RequestHandlerClass

        class H(inner):
            def do_GET(self):
                if not self.path.endswith(".flaky"):
                    return inner.do_GET(self)
                portal.get_count += 1
                rng = self.headers.get("Range")
                start = 0
                if rng:
                    portal.range_requests.append(rng)
                    start = int(rng.split("=")[1].split("-")[0])
                body = portal.blob[start:]
                self.send_response(206 if rng else 200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                if rng:
                    self.send_header("Content-Range",
                                     f"bytes {start}-{len(portal.blob) - 1}/{len(portal.blob)}")
                self.end_headers()
                if portal.break_left > 0:
                    portal.break_left -= 1
                    self.wfile.write(body[: max(1, len(body) // 4)])
                    self.wfile.flush()
                    try:
                        self.connection.close()
                    except Exception:
                        pass
                    return
                self.wfile.write(body)

        self.srv.RequestHandlerClass = H


def test_dl_resumes_after_mid_transfer_break(work, portal):
    """🔴 修前 `curl -fSL --retry 3` 只重试**连接**失败:连上后被掐断一次都不重试。
    实测旧参数 ⇒ rc=18、文件只剩 500KB(App 包几百 MB,公司网抖一下就得从头来)。
    这条用一个「前两次中途断开」的假服务验 dl() 现在能把文件拿全。
    正向对照见 test_dl_old_flags_would_have_failed(同一个服务,旧参数必须失败)。"""
    fp = FlakyPortal(break_times=2)
    try:
        out_file = work / "got.bin"
        # 直接验 dl():把它从 install.sh 里切出来单跑(不想为了这条下一个真 App 包)
        body = SH.read_text(encoding="utf-8")
        i = body.index("DL_RETRY_ALL=\"\"")
        j = body.index("\n}\n", body.index("dl() {", i)) + 3
        snippet = ("set -eu\nYELLOW=''; NC=''\nwarn() { printf '%s\\n' \"$1\"; }\n"
                   + body[i:j] + f'\ndl "{fp.base}/big.flaky" "{out_file}"\n')
        r = subprocess.run(["/bin/sh", "-c", snippet], capture_output=True, timeout=120)
        o = (r.stdout + r.stderr).decode("utf-8", "replace")
        assert r.returncode == 0, f"被掐断两次就放弃了:\n{o[-800:]}"
        assert out_file.stat().st_size == len(fp.blob), (out_file.stat().st_size, len(fp.blob), o[-500:])
        # 🔴 这里**不**断言「从断点接着下」那句:实测在 curl 8.x 上 `--retry-all-errors` 自己就把
        #    「传一半被掐断」重试掉了(第一条 curl 里 `Warning: Problem (retrying all errors)` ⇒
        #    直接成功),外层那个续传循环根本没跑到。那是**更好**的路径,不该因此判失败。
        #    外层循环是给老 curl(< 7.71,不认 retry-all-errors,比如 Win10 1803 自带的 7.55)兜底的。
        assert fp.get_count >= 2, f"假服务没真被掐断过(只收到 {fp.get_count} 次 GET)⇒ 这条没测到东西"
    finally:
        fp.close()


def test_dl_resumes_on_old_curl_without_retry_all_errors(work, portal):
    """老 curl(< 7.71,不认 --retry-all-errors:Win10 1803 自带的是 7.55)走的是外层那个
    续传/重下循环。本机 curl 是 8.x ⇒ 用一个**假装不认这个 flag** 的 curl 包装来跑这条,
    否则这段兜底代码一行都没被执行过(「代码存在 ≠ 该路径跑了」)。"""
    fp = FlakyPortal(break_times=2)
    try:
        fake = work / "oldcurl"
        fake.mkdir(exist_ok=True)
        real = shutil.which("curl")
        # `curl --help all` 里不出现 retry-all-errors ⇒ dl_probe 判成 no;真下载时也把这个 flag 滤掉
        # 🔴 `exec real --help "$2" | grep -v …` 是错的:exec 作用在管道的**那一段**上,
        #    grep 过滤不掉前面已经 exec 出去的输出(第一版就这么写,fake 的 --help 里
        #    retry-all-errors 还在 ⇒ 探测仍判 yes)。要老老实实用 `|` 不用 exec。
        (fake / "curl").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--help" ]; then %s --help "$2" | grep -v -- "--retry-all-errors"; exit 0; fi\n' % real
            + 'for x in "$@"; do [ "$x" = "--retry-all-errors" ] && { echo "curl: option '
            '--retry-all-errors: is unknown" >&2; exit 2; }; done\n'
            'exec %s "$@"\n' % real)
        (fake / "curl").chmod(0o755)
        out_file = work / "oldgot.bin"
        body = SH.read_text(encoding="utf-8")
        i = body.index("DL_RETRY_ALL=\"\"")
        j = body.index("\n}\n", body.index("dl() {", i)) + 3
        snippet = ("set -eu\nYELLOW=''; NC=''\nwarn() { printf '%s\\n' \"$1\"; }\n"
                   + body[i:j] + f'\ndl "{fp.base}/big.flaky" "{out_file}"\n')
        env = dict(os.environ, PATH=f"{fake}:{SYS_PATH}")
        r = subprocess.run(["/bin/sh", "-c", snippet], capture_output=True, timeout=120, env=env)
        o = (r.stdout + r.stderr).decode("utf-8", "replace")
        assert r.returncode == 0, f"老 curl 下被掐断就放弃了:\n{o[-900:]}"
        assert out_file.stat().st_size == len(fp.blob), (out_file.stat().st_size, o[-500:])
        assert "从断点接着下" in o or "重试(第" in o, "外层兜底循环没跑到:\n" + o[-700:]
        assert fp.range_requests, f"老 curl 路径没用上断点续传(全从头下):{fp.get_count} 次 GET"
    finally:
        fp.close()


def test_dl_old_flags_would_have_failed(work):
    """🔴 正向对照:同一个假服务,用**修前**那条 curl 参数必须失败 ——
    证明上面那条测的是真缺陷,不是测了个本来就会过的东西。"""
    fp = FlakyPortal(break_times=2)
    try:
        out_file = work / "old.bin"
        r = subprocess.run(["curl", "-fSL", "--connect-timeout", "15", "--retry", "3",
                            "--retry-delay", "1", "-s", "-o", str(out_file),
                            f"{fp.base}/big.flaky"], capture_output=True, timeout=120)
        assert r.returncode != 0, "旧参数居然成功了 ⇒ 这个假服务没复现出「传一半被掐断」"
        assert out_file.stat().st_size < len(fp.blob)
    finally:
        fp.close()


def test_every_die_message_is_chinese_with_a_next_step():
    """静态:每一个 die 的第一句都得是中文人话(不是只甩一个英文/退出码)。"""
    # 先把 $(...) 抠掉再匹配:消息里嵌的命令替换自己带引号,不去掉的话正则会在那里断
    # (第一版就这么漂的:一条 die 被截成 `✗ 请求没发出去(本机 curl 报错):$(printf '%s' "`)。
    body = re.sub(r"\$\([^()]*\)", "<sub>", SH.read_text(encoding="utf-8"))
    msgs = re.findall(r'die "(✗[^"]*)"', body, re.S)
    assert len(msgs) >= 25, f"只抠到 {len(msgs)} 条 die,正则漂了"
    for m in msgs:
        first = m.splitlines()[0]
        assert re.search(r"[一-鿿]", first), f"die 第一句没有中文:{first!r}"
    # 「下一步怎么办」:整条消息里要出现一个出口(回网页 / 备用页 / 找 IT / 发运维 / 具体动作)
    exits = ("回网页", "备用下载页", "找 IT", "发给运维", "重新复制", "新开一个终端", "先装", "腾出",
             "网页上的 PowerShell", "手动打开", "按 Cmd+Q", "换个网络")
    for m in msgs:
        assert any(e in m for e in exits), f"这条 die 没告诉用户下一步:{m[:120]!r}"


# ── 6. 现有安装器的最小改动:CARHER_KEY 给了就不问、不读 stdin;不给照旧粘贴 ─────────
def _codex_key_block():
    mac = (ROOT / "codex-oneclick/src/install-mac.sh").read_text(encoding="utf-8")
    helpers = "\n".join(l for l in mac.splitlines() if re.match(r"^(BLUE=|say\(\)|ok\(\)|warn\(\)|die\(\))", l))
    blk = re.search(r"^# --- 1\. 读 key ---.*?(?=^# --- 1b\.)", mac, re.S | re.M).group(0)
    return "set -euo pipefail\n" + helpers + "\n" + blk + '\necho "APIKEY=$APIKEY ENVLEFT=${CARHER_KEY:-none}"\n'


def _claude_key_block():
    mac = (ROOT / "claude-oneclick/src/install-mac.sh").read_text(encoding="utf-8")
    helpers = "\n".join(l for l in mac.splitlines() if re.match(r"^(BLUE=|say\(\)|ok\(\)|warn\(\)|die\(\))", l))
    blk = re.search(r"^# ── 3\. 读 key.*?(?=^# 先验 key 能用)", mac, re.S | re.M).group(0)
    return "set -euo pipefail\n" + helpers + "\n" + blk + '\necho "APIKEY=$APIKEY ENVLEFT=${CARHER_KEY:-none}"\n'


@pytest.mark.parametrize("block", [_codex_key_block, _claude_key_block])
def test_mac_installers_take_key_from_env_without_reading_stdin(block, work):
    p_r, p_w = os.pipe()          # 一直开着的 stdin:读了就卡到 timeout
    try:
        r = subprocess.run(["/bin/bash", "-c", block()], stdin=p_r, capture_output=True, timeout=20,
                           env={"PATH": f"{NODE_DIR}:{SYS_PATH}", "HOME": str(work), "LANG": "en_US.UTF-8",
                                "CARHER_KEY": KEY})
    finally:
        os.close(p_r)
        os.close(p_w)
    out = r.stdout.decode()
    assert r.returncode == 0, out + r.stderr.decode()
    assert f"APIKEY={KEY} ENVLEFT=none" in out, "key 没接上,或 CARHER_KEY 没从环境里摘掉(会被 npm 等子进程继承)"
    assert "粘贴" not in out
    assert "后4位 …wxyz" in out
    assert out.count(KEY) == 1, "除了台架自己 echo 的那一次,key 不许上屏"


@pytest.mark.parametrize("block", [_codex_key_block, _claude_key_block])
def test_mac_installers_still_prompt_without_env(block, work):
    r = subprocess.run(["/bin/bash", "-c", block()], input=(KEY2 + "\n").encode(), capture_output=True, timeout=20,
                       env={"PATH": f"{NODE_DIR}:{SYS_PATH}", "HOME": str(work), "LANG": "en_US.UTF-8"})
    out = r.stdout.decode()
    assert r.returncode == 0 and "粘贴" in out and f"APIKEY={KEY2}" in out, out


def test_cursor_engine_accepts_env_key_and_scrubs_it():
    js = (ROOT / "scripts/zk-cursor-web/cursor_team_setup.js").read_text(encoding="utf-8")
    assert 'key: opt("--key", "") || process.env.CARHER_KEY || "",' in js
    i = js.index('key: opt("--key", "") || process.env.CARHER_KEY')
    j = js.index("delete process.env.CARHER_KEY;")
    k = js.index("if (args.bareSa)")
    assert i < j < k, "读完 CARHER_KEY 要立刻从环境里删掉(在 args 建好之后、任何 spawn 之前)"


# ── 7. Windows:pwsh 语法 + 在 pwsh 里真跑入口脚本(对假服务) ───────────────────
@need_pwsh
@pytest.mark.parametrize("f", [PS1, ROOT / "codex-oneclick/src/install-windows.ps1",
                               ROOT / "claude-oneclick/src/install-windows.ps1"])
def test_ps1_parses(f, work):
    ps = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:F,[ref]$t,[ref]$e);"
          "if($e){$e|%{ \"$($_.Extent.StartLineNumber): $($_.Message)\" };exit 1}")
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                       env={"HOME": str(work), "PATH": SYS_PATH, "F": str(f), "TMPDIR": str(work) + "/"})
    assert r.returncode == 0, r.stdout + r.stderr


STUB_BAT_PS = r'''$L = Join-Path $env:STUB_LOG $env:STUB_NAME
New-Item -ItemType Directory -Path $L -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $L 'key'), "$env:CARHER_KEY")
[IO.File]::WriteAllText((Join-Path $L 'argv'), '')
[IO.File]::WriteAllText((Join-Path $L 'stdin'), 'EOF')
Write-Host "stub-ps: CARHER_SELF=$env:CARHER_SELF"
exit [int]("0" + $env:STUB_RC)
'''


def win_oneliner(base: str, tool: str, code: str) -> list:
    """网页上给 Windows 用户的那一行(cmd 解析后交给 powershell 的 argv)。
    一个 `$` 都没有:在 PowerShell 窗口里粘时外层不会把它们展开成空串;cmd 里粘也一样。"""
    inner = ("try{[Net.ServicePointManager]::SecurityProtocol='Tls12';"
             "& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString((New-Object Net.WebClient).DownloadData("
             f"'{base}/ai/i/{tool}.ps1')))) -Code {code}}}"
             "catch{Write-Host '如果上面的报错提到 策略/Policy/被阻止/blocked:请找 IT 开放 PowerShell 执行权限;"
             "或用备用下载页 https://chat.auto-link.com.cn/oneclick/' -ForegroundColor Yellow;throw}")
    assert "$" not in inner
    return ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", inner]


def win_oneliner_page(base: str, tool: str, code: str) -> list:
    """🔴 **网页此刻真正发给用户的那一行**(scripts/ai-portal/static/index.html:1109 逐字对照):
        powershell -ExecutionPolicy Bypass -c "irm <ORIGIN>/ai/i/<tool>.ps1 | iex; Install-Carher -Code <码>"
    和 win_oneliner() 的 `& ([scriptblock]::Create(…)) -Code 码` **不是一个形状**:
    `irm | iex` 先把正文整跑一遍(那时没有码),再调 Install-Carher。
    11-05 实测:修前这条 rc=1,屏幕上是「x 命令里没有安装码。回网页…重新复制完整的一行命令。」
    —— 用户照网页复制粘贴一个字没错也装不上,还被说成是自己没复制全。"""
    return ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            f"irm {base}/ai/i/{tool}.ps1 | iex; Install-Carher -Code {code}"]


def run_ps(work: Path, portal: Portal, argv, extra=None, timeout=120):
    home = work / "home"
    home.mkdir(exist_ok=True)
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    env = {"HOME": str(home), "TMPDIR": str(work) + "/", "PATH": SYS_PATH, "LANG": "en_US.UTF-8",
           "CARHER_PORTAL_BASE": portal.base + "/ai", "CARHER_MIRROR_BASE": portal.base + "/ccdl",
           "CARHER_SKIP_APP_INSTALL": "1", "STUB_LOG": str(logs), "STUB_NAME": "codex",
           "CARHER_PWSH": PWSH or ""}   # 假 cursor 引擎的 .cmd 在 mac 上用它起 PowerShell(见 _win_stub_cursor_zip)
    env.update(extra or {})
    r = subprocess.run([PWSH, *argv], env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace"), logs


@need_pwsh
def test_ps1_oneliner_good_code(work, portal):
    bat = ("@echo off\r\nsetlocal\r\nexit /b\r\n##CARHER-PS1\r\n" + STUB_BAT_PS.replace("\n", "\r\n")).encode()
    portal.files["/ai/i/pkg/codex-windows.bat"] = bat
    portal.codes["WGOOD"] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, logs = run_ps(work, portal, win_oneliner(portal.base, "codex", "WGOOD"))
    assert rc == 0, out
    got = stub_got(logs, "codex")
    assert got and got["key"] == KEY, out
    assert "stub-ps: CARHER_SELF=" in out and "codex-windows.bat" in out
    assert KEY not in out and "后4位 ...wxyz" in out
    assert "全部完成" in out


def _win_stub_bat(tool: str, rc: str = "") -> bytes:
    """假的 <tool>-windows.bat:正文是 STUB_BAT_PS,但把 STUB_NAME 写死成这个工具
    (入口脚本不会替我们设 STUB_NAME,三个工具一起装时得各自落到自己的目录)。"""
    ps = STUB_BAT_PS.replace("$env:STUB_NAME", "'%s'" % tool)
    if rc:
        ps = ps.replace('exit [int]("0" + $env:STUB_RC)', "exit %s" % rc)
    return ("@echo off\r\nsetlocal\r\nexit /b\r\n##CARHER-PS1\r\n" + ps.replace("\n", "\r\n")).encode()


def _win_stub_cursor_zip(rc: str = "0") -> bytes:
    """假的 cursor-windows.zip:里面的 cursor_team_setup.cmd 走 PowerShell 落同样的日志。"""
    import io
    ps = ("$L = Join-Path $env:STUB_LOG 'cursor'\r\n"
          "New-Item -ItemType Directory -Path $L -Force | Out-Null\r\n"
          "[IO.File]::WriteAllText((Join-Path $L 'key'), \"$env:CARHER_KEY\")\r\n"
          "[IO.File]::WriteAllText((Join-Path $L 'argv'), ($args -join \"`n\"))\r\n"
          "[IO.File]::WriteAllText((Join-Path $L 'stdin'), 'EOF')\r\n"
          # 不往屏幕上打 key:install.ps1 没有 install.sh 那层 mask_stream(既有行为,这轮不改),
          # 桩故意打就等于自己造一条「key 上屏」的假阳性。key 只写进日志文件给断言读。
          "Write-Host 'stub-cursor: ran'\r\n"
          "exit %s\r\n" % rc)
    # mac 上没有 cmd.exe:pwsh 直接 `& x.cmd` 会按**可执行文件**跑它 ⇒ 这个桩得是带 shebang 的 sh 脚本。
    # 🔴 行尾必须 LF:写成 "#!/bin/sh\r\n" 的话解释器名字就成了 "/bin/sh\r",
    #    报的错是「No such file or directory」(看着像 .cmd 不存在,其实是 \r)。
    # 真 Windows 上这个文件由 cmd.exe 解释,形状不一样 ⇒ 这正是「Windows 真机没验」的那一处。
    cmd = ("#!/bin/sh\n"
           '"$CARHER_PWSH" -NoProfile -File "$(dirname "$0")/cursor_team_setup.ps1" "$@"\n')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # 带可执行位:mac 上 pwsh 调原生命令要 +x,否则报的是「does not exist」(很会误导)
        zi = zipfile.ZipInfo("cursor-windows/cursor_team_setup.cmd")
        zi.external_attr = (0o755 << 16) | 0o100000
        z.writestr(zi, cmd)
        z.writestr("cursor-windows/cursor_team_setup.ps1", ps)
        z.writestr("cursor-windows/cursor_team_setup.js", "// stub\nprocess.env.CARHER_KEY;\n")
    return buf.getvalue()


@need_pwsh
def test_ps1_page_oneliner_irm_iex_actually_installs(work, portal):
    """🔴 网页真给的那一行(irm|iex; Install-Carher -Code 码)必须真能装上。
    正向对照:把 install.ps1 末尾的派发那两行删掉(或把正文从函数里挪出来),这条立刻变红。"""
    portal.files["/ai/i/pkg/codex-windows.bat"] = _win_stub_bat("codex")
    portal.codes["WPAGE"] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, logs = run_ps(work, portal, win_oneliner_page(portal.base, "codex", "WPAGE"))
    assert rc == 0, out[-1200:]
    got = stub_got(logs, "codex")
    assert got and got["key"] == KEY, out[-1200:]
    assert "√ 全部完成。" in out, out[-900:]
    # 修前的形状:正文空跑一遍 ⇒ 这句 + CommandNotFound
    assert "命令里没有安装码" not in out, out[-900:]
    assert "Install-Carher" not in out or "CommandNotFound" not in out, out[-900:]
    assert KEY not in out
    assert [json.loads(x) for x in portal.requests] == [{"code": "WPAGE"}], portal.requests


@need_pwsh
def test_ps1_page_oneliner_matches_what_the_page_emits():
    """钉住形状本身:网页那段 JS 只要还在发 `irm … | iex; Install-Carher -Code`,
    本文件就必须有 Install-Carher 这个函数。两边谁先改都会在这里相遇。"""
    js = (ROOT / "scripts/ai-portal/static/index.html").read_text(encoding="utf-8")
    m = re.search(r'return \["powershell[^\n]*?/ai/i/" \+ tool \+ "\.ps1([^\n]*?)\];', js)
    assert m, "网页里生成 Windows 一行命令那段变了,来对一下这边的形状"
    emitted = m.group(1)
    if "iex" in emitted and "Install-Carher" in emitted:
        body = PS1.read_text(encoding="utf-8")
        assert re.search(r"^function Install-Carher\b", body, re.M), \
            "网页发的是 `irm|iex; Install-Carher …`,但 install.ps1 里没有 Install-Carher 函数 ⇒ 用户照抄也装不上"


@need_pwsh
@pytest.mark.parametrize("tool", ["codex", "all"])
def test_ps1_direct_invocation_still_works(work, portal, tool):
    """另一种形态(& scriptblock -Code 码 / -File … -Code 码)不许被上面那条改法弄坏。"""
    for t in ("codex", "claude"):
        portal.files[f"/ai/i/pkg/{t}-windows.bat"] = _win_stub_bat(t)
    portal.files["/ai/i/pkg/cursor-windows.zip"] = _win_stub_cursor_zip()
    portal.codes["WDIR"] = {"tool": tool, "key": KEY, "state": "ok"}
    rc, out, logs = run_ps(work, portal, ["-NoProfile", "-File", str(PS1), "-Code", "WDIR"], timeout=240)
    assert rc == 0, out[-1200:]
    if tool == "all":
        for t in ALL_ORDER:
            assert stub_got(logs, t), (t, out[-900:])
    else:
        assert stub_got(logs, "codex")["key"] == KEY, out[-900:]
    assert KEY not in out


@need_pwsh
def test_ps1_no_code_at_all_still_says_so(work, portal):
    """直接跑、一个码都没给(用户双击 / 自己敲):必须还是那句人话,不许静默什么都不做。"""
    rc, out, _ = run_ps(work, portal, ["-NoProfile", "-File", str(PS1)])
    assert rc != 0, out[-600:]
    assert "没有安装码" in out, out[-600:]


@need_pwsh
def test_ps1_all_runs_three_with_same_key_and_one_redeem(work, portal):
    """Windows 的 tool=all:三个都调起、同一把 key、只换一次码、汇总 + 退出 0。
    🔴 本机只有 pwsh(macOS 上跑);Windows 真机没验(App 那步 CARHER_SKIP_APP_INSTALL=1 整段跳过)。"""
    for t in ("codex", "claude"):
        portal.files[f"/ai/i/pkg/{t}-windows.bat"] = _win_stub_bat(t)
    portal.files["/ai/i/pkg/cursor-windows.zip"] = _win_stub_cursor_zip()
    portal.codes["WALL"] = {"tool": "all", "key": KEY, "state": "ok"}
    rc, out, logs = run_ps(work, portal, win_oneliner(portal.base, "all", "WALL"), timeout=240)
    assert rc == 0, out
    for t in ALL_ORDER:
        got = stub_got(logs, t)
        assert got, f"{t} 桩没被调起:\n{out[-1500:]}"
        assert got["key"] == KEY, t
    assert [json.loads(x) for x in portal.requests] == [{"code": "WALL"}], portal.requests
    seen = re.findall(r"第 \d/3 个:(codex|claude|cursor)", out)
    assert seen == ALL_ORDER, (seen, out[-800:])
    assert "三个都装" in out and "10~40 分钟" in out
    assert "安装结果" in out and "三个都装好了" in out
    assert KEY not in out and "后4位 ...wxyz" in out


@need_pwsh
def test_ps1_all_one_failure_keeps_going_and_exits_7(work, portal):
    portal.files["/ai/i/pkg/codex-windows.bat"] = _win_stub_bat("codex", rc="9")
    portal.files["/ai/i/pkg/claude-windows.bat"] = _win_stub_bat("claude")
    portal.files["/ai/i/pkg/cursor-windows.zip"] = _win_stub_cursor_zip()
    portal.codes["WALL2"] = {"tool": "all", "key": KEY, "state": "ok"}
    rc, out, logs = run_ps(work, portal, win_oneliner(portal.base, "all", "WALL2"), timeout=240)
    assert rc == 7, f"部分成功要用 7(拿到 {rc}):\n{out[-1500:]}"
    # codex 失败,claude / cursor 照样跑完
    for t in ("claude", "cursor"):
        assert stub_got(logs, t) and stub_got(logs, t)["key"] == KEY, (t, out[-1200:])
    assert "x Codex  没装成" in out and "退出码 9" in out
    assert "√ Claude Code  装好了" in out and "√ Cursor  装好了" in out
    assert "单独选" in out and "2 个装好了" in out
    assert KEY not in out


@need_pwsh
def test_ps1_single_tool_output_unchanged(work, portal):
    """🔴 Windows 单工具路径不许变:还是「√ 全部完成。」,不出 all 的字样。"""
    portal.files["/ai/i/pkg/codex-windows.bat"] = _win_stub_bat("codex")
    portal.codes["WSOLO"] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, logs = run_ps(work, portal, win_oneliner(portal.base, "codex", "WSOLO"))
    assert rc == 0, out
    assert "√ 安装码有效:装 codex,key 已拿到" in out and "√ 全部完成。" in out
    for s in ("三个都装", "第 1/3 个", "安装结果"):
        assert s not in out, s
    assert stub_got(logs, "codex")["key"] == KEY
    assert not stub_got(logs, "claude") and not stub_got(logs, "cursor")


@need_pwsh
@pytest.mark.parametrize("state,msg", [("expired", "安装码已过期(10 分钟有效),请回网页重新生成"),
                                       ("used", "安装码已经用过了,请回网页重新生成")])
def test_ps1_oneliner_bad_code(work, portal, state, msg):
    portal.codes["WBAD"] = {"tool": "codex", "key": KEY, "state": state}
    rc, out, logs = run_ps(work, portal, win_oneliner(portal.base, "codex", "WBAD"))
    assert rc != 0 and msg in out, out
    assert not stub_got(logs, "codex")


@need_pwsh
def test_ps1_installer_failure_propagates(work, portal):
    bat = ("@echo off\r\nexit /b\r\n##CARHER-PS1\r\n" + STUB_BAT_PS.replace("\n", "\r\n")).encode()
    portal.files["/ai/i/pkg/codex-windows.bat"] = bat
    portal.codes["WFAIL"] = {"tool": "codex", "key": KEY, "state": "ok"}
    rc, out, _ = run_ps(work, portal, win_oneliner(portal.base, "codex", "WFAIL"), {"STUB_RC": "1"})
    assert rc == 1 and "安装器没跑完(退出码 1)" in out, out


@need_pwsh
def test_ps1_download_failure_shows_it_hint(work, portal):
    """入口脚本本身拉不下来(被拦 / 断网)⇒ 一行命令里的 catch 打「找 IT」+ 备用页,退出码非 0。"""
    rc, out, _ = run_ps(work, portal, win_oneliner(portal.base, "nosuchtool", "X"))
    assert rc != 0 and "请找 IT 开放 PowerShell 执行权限" in out and "chat.auto-link.com.cn/oneclick/" in out


def _ps_key_block(path, start, end):
    ps = (ROOT / path).read_text(encoding="utf-8-sig")
    helpers = "\n".join(l for l in ps.splitlines() if re.match(r"^function (Say|Ok|Warn|Die)", l))
    blk = re.search(rf"^{re.escape(start)}.*?(?=^{re.escape(end)})", ps, re.S | re.M).group(0)
    return helpers + "\n" + blk + '\nWrite-Output "APIKEY=$apiKey ENVLEFT=$(if ($env:CARHER_KEY) { \'left\' } else { \'none\' })"\n'


@need_pwsh
@pytest.mark.parametrize("path,start,end", [
    ("codex-oneclick/src/install-windows.ps1", "# --- 1. 读 key ---", "# --- 1b."),
    ("claude-oneclick/src/install-windows.ps1", "# ── 3. 读 key", "# 先验 key 能用"),
])
def test_windows_installers_take_key_from_env(work, path, start, end):
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", _ps_key_block(path, start, end)],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                       env={"HOME": str(work), "PATH": SYS_PATH, "TMPDIR": str(work) + "/", "CARHER_KEY": KEY})
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"APIKEY={KEY} ENVLEFT=none" in r.stdout
    assert "粘贴" not in r.stdout and "后4位 ...wxyz" in r.stdout
