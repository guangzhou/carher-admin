#!/usr/bin/env bash
# 把 src/ 下两个模板拼成给用户的两个单文件：
#   claude-mac.command  (macOS / Linux)
#   claude-windows.bat  (Windows，bat 头 + PowerShell 正文，双击即运行)
# 改脚本只改 src/，改完跑本脚本，再把两个产物传到飞书文档。
# 结构照抄 codex-oneclick/build.sh(那边 08-27 用户实机踩过 bat 头的坑,这里同一套防线)。
set -euo pipefail
cd "$(dirname "$0")"
# 10-15:产物名带产品+系统(用户从三篇文档下载,原来 Codex/Claude 都叫 install-mac.command,下载目录里分不清)
rm -f install-mac.command install-windows.bat

python3 -m pytest tests -q -p no:cacheprovider
bash -n src/install-mac.sh

python3 - <<'PY'
import re
from pathlib import Path

src = Path("src")

SHARED = Path("../codex-oneclick/src")   # 画图工具和 skill 与 Codex 共用一份源(不复制,防漂移)

def finish(template: str) -> str:
    def repl(m):
        own = src / m.group(1)   # 本工具自己的(如 carher_models_refresh.js)优先,其余取共用那份
        body = (own if own.exists() else SHARED / m.group(1)).read_text(encoding="utf-8").rstrip("\n")
        # 内嵌靠 heredoc / here-string 的结束符,正文里不能出现
        assert not re.search(r"^(CARHER_JS_EOF|CARHER_MD_EOF|'@)", body, re.M), m.group(1)
        return body
    template = re.sub(r"^@@EMBED (\S+)@@$", repl, template, flags=re.M)
    assert "@@EMBED" not in template
    out = re.sub(r"^# ⚠️ 这是模板：.*\n(#    .*\n)?",
                 "# 由 build.sh 生成，别直接改；源在 claude-oneclick/src/\n", template, flags=re.M)
    assert "这是模板" not in out
    return out

mac = finish((src / "install-mac.sh").read_text(encoding="utf-8"))
Path("claude-mac.command").write_text(mac, encoding="utf-8", newline="\n")

# bat 头：cmd 只执行到 exit /b，后面整段交给 PowerShell 从自身文件里读出来执行。
# 路径走环境变量传，避免路径里有引号/空格时拼命令出错。
head = (
    "@echo off\r\n"
    "setlocal\r\n"
    'set "CARHER_SELF=%~f0"\r\n'
    "powershell -NoProfile -ExecutionPolicy Bypass -Command "
    "\"$c=[IO.File]::ReadAllText($env:CARHER_SELF,[Text.Encoding]::UTF8);"
    "$i=$c.IndexOf('#'+'#CARHER-PS1');"
    "& ([scriptblock]::Create($c.Substring($i)))\"\r\n"
    "echo.\r\n"
    "pause\r\n"
    "exit /b\r\n"
    "##CARHER-PS1\r\n"
)
ps1 = finish((src / "install-windows.ps1").read_text(encoding="utf-8-sig"))
bat = (head + ps1.replace("\r\n", "\n").replace("\n", "\r\n")).encode("utf-8")
# cmd 按字节偏移逐行续读 .bat，头部有非 ASCII / chcp 就会吃掉行首字符(codex 那边 08-27 实机踩过)。
# 头部必须纯 ASCII + CRLF、无 BOM、无 chcp；PS 正文按 UTF-8 字符串读，不需要 BOM。
cut = bat.index(b"##CARHER-PS1")
assert not bat.startswith(b"\xef\xbb\xbf") and all(c < 128 for c in bat[:cut])
assert b"chcp" not in bat.lower() and bat.count(b"\n") == bat.count(b"\r\n")
Path("claude-windows.bat").write_bytes(bat)
print("built claude-mac.command, claude-windows.bat")

# 自检(doctor):只读,同一套 bat 头防线
dm = finish((src / "doctor-mac.sh").read_text(encoding="utf-8"))
Path("claude-doctor-mac.command").write_text(dm, encoding="utf-8", newline="\n")
dps = finish((src / "doctor-windows.ps1").read_text(encoding="utf-8-sig"))
dbat = (head + dps.replace("\r\n", "\n").replace("\n", "\r\n")).encode("utf-8")
dcut = dbat.index(b"##CARHER-PS1")
assert not dbat.startswith(b"\xef\xbb\xbf") and all(c < 128 for c in dbat[:dcut])
assert b"chcp" not in dbat.lower() and dbat.count(b"\n") == dbat.count(b"\r\n")
Path("claude-doctor-windows.bat").write_bytes(dbat)
print("built claude-doctor-mac.command, claude-doctor-windows.bat")
PY
chmod +x claude-mac.command claude-doctor-mac.command
bash -n claude-mac.command
bash -n claude-doctor-mac.command
# 上传到飞书后下载回来比这两个值，相同才算传对了
shasum -a 256 claude-mac.command claude-windows.bat claude-doctor-mac.command claude-doctor-windows.bat
