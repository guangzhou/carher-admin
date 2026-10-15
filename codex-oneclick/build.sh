#!/usr/bin/env bash
# 把 src/ 下的模板 + 两个 Python 小工具拼成给用户的两个单文件：
#   codex-mac.command  (macOS / Linux)
#   codex-windows.bat  (Windows，bat 头 + PowerShell 正文，双击即运行)
# 改脚本只改 src/，改完跑本脚本，再把两个产物传到飞书文档。
set -euo pipefail
cd "$(dirname "$0")"
# 10-15:产物名带产品+系统(用户从三篇文档下载,原来 Codex/Claude 都叫 install-mac.command,下载目录里分不清)
rm -f install-mac.command install-windows.bat

# 回归测试不过就不出产物：每条测试都对应一次实测踩过的坑（tests/ 里有注释）
python3 -m pytest tests -q -p no:cacheprovider
bash -n src/install-mac.sh

python3 - <<'PY'
import re
from pathlib import Path

src = Path("src")

def embed(template: str) -> str:
    def repl(m):
        body = (src / m.group(1)).read_text(encoding="utf-8").rstrip("\n")
        # 内嵌靠 heredoc / here-string 的结束符，正文里不能出现
        assert not re.search(r"^(CARHER_PY_EOF|'@)", body, re.M), m.group(1)
        return body
    out = re.sub(r"^@@EMBED (\S+)@@$", repl, template, flags=re.M)
    out = re.sub(r"^# ⚠️ 这是模板：.*\n(#    .*\n)?", "# 由 build.sh 生成，别直接改；源在 codex-oneclick/src/\n", out, flags=re.M)
    assert "@@EMBED" not in out
    return out

mac = embed((src / "install-mac.sh").read_text(encoding="utf-8"))
Path("codex-mac.command").write_text(mac, encoding="utf-8", newline="\n")

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
ps1 = embed((src / "install-windows.ps1").read_text(encoding="utf-8-sig"))
bat = (head + ps1.replace("\r\n", "\n").replace("\n", "\r\n")).encode("utf-8")
# 08-27 用户实机踩过：cmd 按字节偏移逐行续读 .bat，头部有非 ASCII / chcp 就会吃掉行首字符。
# 头部（cmd 真正执行的部分）必须纯 ASCII + CRLF、无 BOM、无 chcp；PS 正文按 UTF-8 字符串读，不需要 BOM。
cut = bat.index(b"##CARHER-PS1")
assert not bat.startswith(b"\xef\xbb\xbf") and all(c < 128 for c in bat[:cut])
assert b"chcp" not in bat.lower() and bat.count(b"\n") == bat.count(b"\r\n")
Path("codex-windows.bat").write_bytes(bat)
print("built codex-mac.command, codex-windows.bat")
PY
chmod +x codex-mac.command
bash -n codex-mac.command
# 上传到飞书后下载回来比这两个值，相同才算传对了
shasum -a 256 codex-mac.command codex-windows.bat
