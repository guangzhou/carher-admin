@echo off
setlocal
set "CARHER_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=[IO.File]::ReadAllText($env:CARHER_SELF,[Text.Encoding]::UTF8);$i=$c.IndexOf('#'+'#CARHER-PS1');& ([scriptblock]::Create($c.Substring($i)))"
echo.
pause
exit /b
##CARHER-PS1
# CarHer Codex 一键配置 (Windows)
# 由 build.sh 生成，别直接改；源在 codex-oneclick/src/
# 没有 codex 命令行会自动装（npmmirror）；Desktop App 需用户自己装（文档里有安装包）
$ErrorActionPreference = 'Stop'

function Say($m)  { Write-Host $m -ForegroundColor Cyan }
function Ok($m)   { Write-Host $m -ForegroundColor Green }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Die($m)  { Write-Host $m -ForegroundColor Red; exit 1 }

$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }

# 固定配置（用户无需感知）
$baseUrl  = 'https://cc.auto-link.com.cn/pro/v1'
$provider = 'carher_prod'
$model    = 'gpt-5.6-terra'

Write-Host ""
Say "==================================================="
Say "   CarHer Codex 一键配置 (Windows)"
Say "==================================================="
Write-Host ""

# --- 0. 没有 codex 命令行 / 太旧 ⇒ 自动装最新版 ---
# 和 Claude Code 一键脚本同一套做法：Node 装到 %USERPROFILE%\.carher-node、npm 全局装到
# %USERPROFILE%\.carher-npm-global，都走 npmmirror（不要管理员、不碰系统）。
# 装失败不阻断：配置照样写，Desktop App 不依赖命令行。
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest 的进度条在 PS5 上会让下载慢 10 倍
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$codexMin    = [version]'0.144.0'
$npmRegistry = 'https://registry.npmmirror.com'
$nodeMirror  = 'https://cdn.npmmirror.com/binaries/node'
$nodeMajorMin = 18; $nodeInstallMajor = 22
$nodeHome    = Join-Path $env:USERPROFILE '.carher-node'
$npmPrefix   = Join-Path $env:USERPROFILE '.carher-npm-global'
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { 'arm64' } else { 'x64' }

function Add-UserPath([string]$dir) {
  # 写进**用户级** PATH(不要管理员),并立刻对当前进程生效
  $cur = [Environment]::GetEnvironmentVariable('Path', 'User')
  $parts = @(); if ($cur) { $parts = $cur -split ';' | Where-Object { $_ } }
  if ($parts -notcontains $dir) {
    [Environment]::SetEnvironmentVariable('Path', (@($dir) + $parts) -join ';', 'User')
  }
  if (($env:Path -split ';') -notcontains $dir) { $env:Path = "$dir;$env:Path" }
}
function Get-NodeMajor { try { [int](& node -p "process.versions.node.split('.')[0]" 2>$null) } catch { 0 } }
function Get-CodexVer {
  if (-not (Get-Command codex -ErrorAction SilentlyContinue)) { return $null }
  try { $s = (& codex --version 2>$null | Select-Object -First 1) } catch { return $null }
  if ("$s" -match '(\d+\.\d+\.\d+)') { return [version]$Matches[1] } else { return $null }
}
function Install-Node {
  if ((Get-Command node -ErrorAction SilentlyContinue) -and (Get-NodeMajor) -ge $nodeMajorMin) { return $true }
  Say "→ 装一个 Node.js 到 $nodeHome(不需要管理员权限，不影响你原来的)"
  try {
    $idx = Invoke-RestMethod -Uri "$nodeMirror/index.json" -TimeoutSec 30
    $rel = $idx | Where-Object { $_.lts -and $_.version -like "v$nodeInstallMajor.*" } | Select-Object -First 1
    if (-not $rel) { return $false }
    $pkg = "node-$($rel.version)-win-$arch"
    $tmp = Join-Path $env:TEMP "carher-node-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    try {
      $zip = Join-Path $tmp "$pkg.zip"
      Invoke-WebRequest -Uri "$nodeMirror/$($rel.version)/$pkg.zip" -OutFile $zip -TimeoutSec 600 -UseBasicParsing
      # 校验:用官方 SHASUMS256.txt 比对,下坏的/被篡改的包不装
      $sums = (Invoke-WebRequest -Uri "$nodeMirror/$($rel.version)/SHASUMS256.txt" -TimeoutSec 30 -UseBasicParsing).Content
      $want = ($sums -split "`n" | Where-Object { $_ -match "\s$([regex]::Escape("$pkg.zip"))\s*$" } | Select-Object -First 1) -replace '\s.*$', ''
      $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
      if (-not $want -or $want.Trim().ToLower() -ne $got) { Warn "! Node.js 安装包校验失败，已停止"; return $false }
      if (Test-Path $nodeHome) { Remove-Item $nodeHome -Recurse -Force }
      Expand-Archive -Path $zip -DestinationPath $tmp -Force
      Move-Item (Join-Path $tmp $pkg) $nodeHome
    } finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    Add-UserPath $nodeHome
    return ((Get-NodeMajor) -ge $nodeMajorMin)
  } catch { return $false }
}

if (Test-Path (Join-Path $nodeHome 'node.exe')) { $env:Path = "$nodeHome;$env:Path" }
if (Test-Path $npmPrefix) { $env:Path = "$npmPrefix;$env:Path" }
$cur = Get-CodexVer
if ($cur -and $cur -ge $codexMin) {
  Ok "√ 检测到 codex 命令行 $cur"
} else {
  if ($cur) { Warn "! codex $cur 太旧(需要 >= $codexMin),自动升级 ..." }
  else { Say "→ 没有 codex 命令行，自动安装(约 1~3 分钟)..." }
  if (Install-Node) {
    New-Item -ItemType Directory -Path $npmPrefix -Force | Out-Null
    $env:npm_config_prefix = $npmPrefix
    Add-UserPath $npmPrefix
    # npm 在 Windows 上是 npm.cmd;走 cmd /c 避免 PowerShell 执行策略拦 npm.ps1
    & cmd /c "npm install -g @openai/codex --registry=$npmRegistry --no-fund --no-audit --update-notifier=false --loglevel=error"
    if ($LASTEXITCODE -eq 0) {
      $cur = Get-CodexVer
      if ($cur) { Ok "√ codex 命令行 $cur 已装好" } else { Warn "! 装完了但找不到 codex 命令" }
    } else {
      Warn "! 自动安装 codex 命令行失败（配置照样会写好，Desktop App 不受影响；命令行可稍后重跑本脚本）"
    }
    Remove-Item Env:npm_config_prefix -ErrorAction SilentlyContinue
  } else {
    Warn "! 准备 Node.js 失败，跳过安装 codex 命令行（配置照样会写好，Desktop App 不受影响）"
  }
}
Write-Host ""

# --- 1. 读 key ---
Say "粘贴你的 API key（sk- 开头，运维发给你的那个），然后回车："
$apiKey = (Read-Host "key").Trim()
if ([string]::IsNullOrWhiteSpace($apiKey)) { Die "x 没有输入 key，已取消。" }
if (-not $apiKey.StartsWith('sk-')) { Warn "! key 不是 sk- 开头，仍然继续。" }
Write-Host ""

# --- 2. 备份 ---
if (-not (Test-Path $codexHome)) { New-Item -ItemType Directory -Path $codexHome -Force | Out-Null }
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$bkDir = Join-Path $codexHome "backup-carher-$ts"
$backed = $false
foreach ($f in @('config.toml','auth.json','chatgpt.config.toml')) {
  $src = Join-Path $codexHome $f
  if (Test-Path $src) {
    if (-not (Test-Path $bkDir)) { New-Item -ItemType Directory -Path $bkDir -Force | Out-Null }
    Copy-Item $src (Join-Path $bkDir $f) -Force
    $backed = $true
  }
}
if ($backed) { Warn "↳ 已备份原有配置到：$bkDir" }

# --- 3. 写 config.toml（无 BOM，避免 TOML 解析问题） ---
# 顶层 model_provider 让裸 codex 和 Desktop App 都默认走 CarHer 网关（WS 增量同时生效）。
# 不用 env_key：GUI App 读不到 shell 环境变量，写了 env_key 而变量缺失会硬报错。
# 不用 [profiles.x]：codex >=0.147 视为 legacy，与 --profile 组合会报错；改用 chatgpt.config.toml。
$configText = @"
# Generated by CarHer Codex 一键配置 — $ts
model = "$model"
model_reasoning_effort = "medium"
model_provider = "$provider"
openai_base_url = "$baseUrl"

# 画图工具要联网：codex exec 默认是只读沙箱，且 workspace-write 沙箱默认断网。
# sandbox_mode 是顶层裸键，必须写在任何 [table] 之前。
sandbox_mode = "workspace-write"

[model_providers.$provider]
name = "CarHer"
base_url = "$baseUrl"
wire_api = "responses"
supports_websockets = true
experimental_bearer_token = "$apiKey"

# 允许沙箱内的命令联网。默认 workspace-write 沙箱是断网的，
# 断网时画图工具连 DNS 都解析不了（报 nodename nor servname provided）。
[sandbox_workspace_write]
network_access = true
"@
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $codexHome 'config.toml'), $configText, $utf8NoBom)
Ok "√ 已写 $codexHome\config.toml"

# --- 4. 写 chatgpt.config.toml（兼容老命令 codex --profile chatgpt） ---
$profileText = @"
model = "$model"
model_provider = "$provider"
"@
[System.IO.File]::WriteAllText((Join-Path $codexHome 'chatgpt.config.toml'), $profileText, $utf8NoBom)
Ok "√ 已写 $codexHome\chatgpt.config.toml"

# --- 5. 写 auth.json（apikey 模式，Desktop App 必需） ---
$authText = @"
{
  "auth_mode": "apikey",
  "OPENAI_API_KEY": "$apiKey"
}
"@
[System.IO.File]::WriteAllText((Join-Path $codexHome 'auth.json'), $authText, $utf8NoBom)
Ok "√ 已写 $codexHome\auth.json"

# --- 6. 装生图能力 ---
# 内置画图工具在本环境未注册，Codex 会退化成画 SVG。装一个零依赖小工具 + 一条常驻授权，
# 让 Codex 需要画图时直接调它。只用 Python 标准库，不需要 pip 装任何东西。
# 两个 Python 小工具内嵌在本脚本里（build.sh 打包时填入），用户只需要这一个文件。
$imgSrc = @'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CarHer 生图工具 — 由「Codex 一键配置」自动安装，用户无需关心。

只用 Python 标准库，不需要 pip 装任何东西。
key / base_url 从同目录 auth.json + config.toml 读取，不接受命令行传 key。

用法：
  python3 ~/.codex/carher_image.py "画面描述" [输出路径]

2026-10-13 起统一用 muse-image(用户点名「画图都用 muse 那个工具」)。
muse-image 返回的是**相对路径** `/v1/media/<32hex>.webp`(文件在 muse2api 上,经网关拿不到),
198 nginx 加了只读出口 `https://<网关域名>/muse-media/<32hex>.<ext>` ⇒ 这里把相对路径改写过去下载。
输出文件后缀以**实际图片格式**为准(webp 就存 .webp),不把 webp 字节写进 .png 名字里骗看图软件。
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
DEFAULT_MODEL = os.environ.get("CARHER_IMAGE_MODEL", "muse-image")
DEFAULT_SIZE = "1024x1024"


def read_credentials():
    """key 取自 auth.json，base_url 取自 config.toml —— 与一键脚本写入的位置一致。"""
    # 🔴 先读 auth.json(一键脚本写的那把),环境变量只作兜底:用户 shell 里常有一把**别的**
    #    OPENAI_API_KEY(10-13 本机实测:env 里那把没开 muse-image ⇒ 每次 403),
    #    Codex 本身用的是 auth.json 那把,画图要和它一致。
    key = ""
    try:
        with open(os.path.join(CODEX_HOME, "auth.json"), encoding="utf-8") as f:
            key = (json.load(f).get("OPENAI_API_KEY") or "").strip()
    except Exception:
        pass
    if not key:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        sys.exit("找不到 key。请重新运行一次「Codex 一键配置」脚本。")

    base = ""
    if True:
        try:
            with open(os.path.join(CODEX_HOME, "config.toml"), encoding="utf-8") as f:
                m = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', f.read(), re.M)
            if m:
                base = m.group(1).strip()
        except Exception:
            pass
    if not base:
        base = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not base:
        sys.exit("找不到网关地址。请重新运行一次「Codex 一键配置」脚本。")
    return key, base.rstrip("/")


def media_url(url, base):
    """相对路径 /v1/media/<hex>.<ext> → https://<网关根>/muse-media/<hex>.<ext>;绝对 URL 原样。"""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    m = re.match(r"^/v1/media/([0-9a-f]{32}\.(?:webp|png|jpe?g))$", url)
    if not m:
        sys.exit("生成失败：看不懂返回的图片地址 %s" % url[:120])
    root = re.match(r"^(https?://[^/]+)", base).group(1)
    return root + "/muse-media/" + m.group(1)


def download(url):
    last = None
    for _ in range(3):   # 文件刚生成时偶有一两秒还没落盘
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2)
    sys.exit("图片已生成但下载失败：%s: %s" % (type(last).__name__, last))


def fix_ext(path, data):
    """按文件头认格式,后缀不对就改成对的(webp 存成 .png 会让不少看图软件打不开)。"""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    else:
        return path
    root, cur = os.path.splitext(path)
    return path if cur.lower() in ((ext, ".jpeg") if ext == ".jpg" else (ext,)) else root + ext


def generate(prompt, out_path):
    key, base = read_credentials()
    body = json.dumps({
        "model": DEFAULT_MODEL,
        "prompt": prompt,
        "size": DEFAULT_SIZE,
        "n": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/images/generations",
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )

    started = time.time()
    print("正在生成，约需 30 秒…", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code == 403:
            sys.exit("你的 key 还没有开通画图模型 %s，请联系运维开通后重试。" % DEFAULT_MODEL)
        if e.code == 401:
            sys.exit("key 已失效，请找运维重新颁发。")
        sys.exit("生成失败（HTTP %s）：%s" % (e.code, detail[:300]))
    except Exception as e:
        sys.exit("生成失败：%s: %s" % (type(e).__name__, e))

    item = (payload.get("data") or [{}])[0]
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        data = download(media_url(item["url"], base))
    else:
        sys.exit("生成失败：返回里没有图片数据。")

    out_path = fix_ext(out_path, data)
    with open(out_path, "wb") as f:
        f.write(data)
    print("已生成：%s（%.1f MB，用时 %.0f 秒）"
          % (out_path, len(data) / 1048576.0, time.time() - started))
    return out_path


def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    if not args:
        sys.exit('用法：python3 %s "画面描述" [输出路径.png]' % sys.argv[0])
    prompt = args[0]
    name = "image-%s.png" % time.strftime("%Y%m%d-%H%M%S")
    out = args[1] if len(args) > 1 else os.getcwd()
    # 🔴 10-13 本机回归:Codex 传了「.」当输出路径 ⇒ 原来拼成 `..webp`(隐藏文件,用户找不到)。
    #    给的是目录(含 . / 结尾带斜杠)⇒ 在目录里自动起名;没有后缀的普通名字照常补。
    if os.path.isdir(out) or out.endswith(("/", "\\")):
        os.makedirs(out, exist_ok=True)
        out = os.path.join(out, name)
    generate(prompt, out)   # 后缀按实际格式自动改(muse-image 出的是 .webp)


if __name__ == "__main__":
    main()
'@
$syncSrc = @'
#!/usr/bin/env python3
"""把这把 key 在 CarHer 网关上能用的全部模型同步进 Codex 的模型选择器。

只用 Python 标准库。key / base_url 从 CODEX_HOME 下的 auth.json + config.toml 读，
与一键脚本写入的位置一致。可反复运行（定时任务每天跑一次）。

流程：
  1. GET {base_url}/models —— 网关已按 key 的白名单过滤，列出来的就是能用的
  2. 模板取本机 codex 自带的目录（codex debug models --bundled），
     字段格式自动跟随用户的 codex 版本，避免 schema 漂移导致 codex 起不来
  3. 生成新目录 → 用 codex 自己解析一遍 → 通过才原子替换
  4. config.toml 里没有 model_catalog_json 就补一行

任何一步失败都不动现有文件：目录写坏了 codex 会直接启动失败。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
CONFIG = CODEX_HOME / "config.toml"
DEFAULT_CATALOG = CODEX_HOME / "carher-models.json"
# 非 OpenAI 模型套 gpt-5.5 的元数据：它是普通函数调用。5.6/6 系的 tool_mode=code_mode_only +
# use_responses_lite 是 OpenAI 专属工具形状，套给 grok 实测拿不到 shell，一个文件都建不出来。
TEMPLATE_SLUG = "gpt-5.5"
# 不进选择器：生图模型不能对话；审查专用模型 codex 自己会调
SKIP = re.compile(r"image|^codex-auto-review$")


LOG_FILE = CODEX_HOME / "carher-models-sync.log"


def log(msg: str) -> None:
    line = f"[carher-models] {msg}"
    if sys.stdout is not None:  # pythonw（Windows 定时任务）没有 stdout
        print(line, flush=True)
    try:  # 定时任务里没人看终端，自己写日志，不依赖任务的输出重定向
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


def read_credentials() -> tuple[str, str]:
    key = ""
    try:
        key = json.loads((CODEX_HOME / "auth.json").read_text(encoding="utf-8")).get("OPENAI_API_KEY", "")
    except (OSError, ValueError):
        pass
    m = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', CONFIG.read_text(encoding="utf-8"), re.M)
    if not key or not m:
        raise RuntimeError("auth.json 里没有 key 或 config.toml 里没有 base_url，先跑一键配置脚本")
    return key, m.group(1).rstrip("/")


def fetch_model_ids(key: str, base_url: str) -> list[str]:
    req = urllib.request.Request(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp).get("data", [])
    return sorted({x["id"] for x in data if isinstance(x, dict) and x.get("id")}, key=str.casefold)


def fetch_input_limits(key: str, base_url: str) -> dict[str, int]:
    """网关 /model/info 里每个模型的输入上限（多条部署取最小）。拿不到就返回空，不影响同步。

    模板的 context_window 是 272000；claude-haiku 实际只有 200000，不压下来的话
    codex 会在超过 200k 后才想起压缩，请求先被上游拒掉。
    """
    req = urllib.request.Request(f"{base_url}/model/info", headers={"Authorization": f"Bearer {key}"})
    limits: dict[str, int] = {}
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.load(resp).get("data", [])
    except Exception as exc:
        log(f"读不到模型上下文上限，沿用默认值：{exc}")
        return limits
    for row in rows:
        name = row.get("model_name") if isinstance(row, dict) else None
        cap = (row.get("model_info") or {}).get("max_input_tokens") if name else None
        if isinstance(cap, int) and cap > 0:
            limits[name] = min(cap, limits.get(name, cap))
    return limits


def find_codex() -> str | None:
    appdata = os.environ.get("APPDATA")
    if appdata:  # Windows npm 装的：优先真二进制，绕开 codex.cmd 这层 cmd 转义
        npm = Path(appdata) / "npm" / "node_modules" / "@openai"
        try:
            hit = next(npm.glob("codex*/**/codex.exe"), None)
        except OSError:
            hit = None
        if hit:
            return str(hit)
    candidates = [
        shutil.which("codex"),
        "/Applications/Codex.app/Contents/Resources/codex",
        "/Applications/OpenAI Codex.app/Contents/Resources/codex",
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:  # Windows App 常见位置
        candidates += [str(Path(local) / "Programs" / "codex" / "codex.exe")]
    for c in candidates:
        if c and Path(c).exists():
            return c
    # Windows 只装了 Desktop App（msix）时，codex.exe 在 App 包目录里
    for root in (os.environ.get("ProgramFiles"), local):
        if not root:
            continue
        for pattern in ("WindowsApps/OpenAI.Codex*/**/codex.exe", "Programs/*odex*/**/codex.exe"):
            try:
                hit = next(Path(root).glob(pattern), None)
            except OSError:  # WindowsApps 目录可能没有列目录权限
                hit = None
            if hit:
                return str(hit)
    return None


def run_codex(codex: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CODEX_HOME=str(CODEX_HOME))
    # codex 输出 UTF-8（含中文/符号）；不写 encoding 会按系统编码解，中文 Windows(GBK) 直接解码失败
    return subprocess.run([codex, "debug", "models", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, env=env)


def load_templates(codex: str | None, current: Path) -> list[dict]:
    if codex:
        r = run_codex(codex, "--bundled")
        if r.returncode == 0:
            return json.loads(r.stdout)["models"]
        log(f"codex --bundled 失败，改用现有目录当模板：{r.stderr.strip()[:200]}")
    if current.exists():
        return json.loads(current.read_text(encoding="utf-8"))["models"]
    raise RuntimeError("找不到 codex，也没有现有目录可当模板")


def build_catalog(ids: list[str], templates: list[dict], limits: dict[str, int]) -> dict:
    by_slug = {m["slug"]: m for m in templates if m.get("slug")}
    base = by_slug.get(TEMPLATE_SLUG) or next(iter(by_slug.values()))
    wanted = [i for i in ids if not SKIP.search(i)]
    out: list[dict] = []
    # 官方自带的模型保留官方元数据，按官方顺序排在前面
    for slug, m in by_slug.items():
        m = copy.deepcopy(m)
        m["visibility"] = "list" if slug in wanted else "hide"
        out.append(m)
    for slug in wanted:
        if slug in by_slug:
            continue
        m = copy.deepcopy(base)
        m.update(slug=slug, display_name=slug, description=f"CarHer 网关模型：{slug}",
                 visibility="list", supported_in_api=True)
        m.update(tool_mode=None, use_responses_lite=False, multi_agent_version=None,
                 additional_speed_tiers=[], service_tiers=[])  # 模板版本变了也钉住
        # apply_patch 用普通 JSON 函数参数，不用 freeform：非 OpenAI 模型不会写裸 patch 语法。
        # 10-09 实测 freeform 下 kimi/composer/claude-sonnet-5/5.5 反复报
        # "first line of the patch must be '*** Begin Patch'"，sonnet-5.5 还会回 DONE 但文件没改；
        # 改 null 后 8 个模型改文件 0 报错、全部改对。
        m["apply_patch_tool_type"] = None
        m.pop("upgrade", None)
        m.pop("availability_nux", None)
        out.append(m)
    for priority, m in enumerate(out, start=1):
        m["priority"] = priority
        cap = limits.get(m["slug"])
        if cap:  # 只往下压，不放大
            for field in ("context_window", "max_context_window"):
                if isinstance(m.get(field), int) and m[field] > cap:
                    m[field] = cap
    return {"models": out}


def catalog_path_from_config(text: str) -> Path | None:
    m = re.search(r"""^\s*model_catalog_json\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')""", text, re.M)
    if not m:
        return None
    raw = m.group(1)
    value = json.loads(raw) if raw.startswith('"') else raw[1:-1]
    return Path(value).expanduser()


def toml_str(value: object) -> str:
    # json 字符串转义规则是 TOML 基本字符串的子集：反斜杠、引号、中文路径都安全
    return json.dumps(str(value), ensure_ascii=False)


def ensure_config_line(text: str, path: Path) -> str:
    line = f"model_catalog_json = {toml_str(path)}\n"
    first_table = re.search(r"^\[", text, re.M)  # 顶层键必须在任何 [table] 之前
    if first_table is None:
        return text.rstrip("\n") + "\n" + line
    return text[: first_table.start()] + line + "\n" + text[first_table.start():]


def main() -> int:
    key, base_url = read_credentials()
    ids = fetch_model_ids(key, base_url)
    if not ids:
        raise RuntimeError("网关没返回任何模型，保持现有目录不变")

    config_text = CONFIG.read_text(encoding="utf-8")
    target = catalog_path_from_config(config_text) or DEFAULT_CATALOG
    codex = find_codex()
    catalog = build_catalog(ids, load_templates(codex, target), fetch_input_limits(key, base_url))

    fd, tmp_name = tempfile.mkstemp(prefix=".carher-models-", suffix=".json", dir=str(CODEX_HOME))
    tmp = Path(tmp_name)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
    try:
        if codex:  # 让 codex 自己解析一遍，通不过就不替换
            # 值不加引号：-c 的值解析 TOML 失败时按原样字符串用，路径天然不是合法 TOML。
            # 这样 Windows 走 codex.cmd 时也不用经过 cmd 的引号转义。
            r = run_codex(codex, "-c", f"model_catalog_json={tmp}")
            if r.returncode != 0:
                raise RuntimeError(f"codex 解析新目录失败，未替换：{r.stderr.strip()[:300]}")
        if target.exists():
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

    if catalog_path_from_config(config_text) is None:
        CONFIG.write_text(ensure_config_line(config_text, target), encoding="utf-8")
        log(f"已在 config.toml 加 model_catalog_json -> {target}")

    shown = [m["slug"] for m in catalog["models"] if m["visibility"] == "list"]
    log(f"同步完成：选择器 {len(shown)} 个模型（网关可见 {len(ids)} 个）-> {target}")
    log("Codex App 要完全退出重开才会看到新模型")
    return 0


if __name__ == "__main__":
    # 定时任务把输出写进日志文件时，英文 Windows 的默认编码(cp1252)写不了中文，会在打印日志时崩
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # None（pythonw）或不支持
            pass
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512 * 1024:  # 每天几行，防止无限长
        LOG_FILE.write_text("", encoding="utf-8")
    try:
        raise SystemExit(main())
    except Exception as exc:  # 定时任务里跑，失败只留日志、不动现有配置
        log(f"失败：{exc}")
        raise SystemExit(1)
'@
[System.IO.File]::WriteAllText((Join-Path $codexHome 'carher_image.py'), $imgSrc, $utf8NoBom)
[System.IO.File]::WriteAllText((Join-Path $codexHome 'carher_models_sync.py'), $syncSrc, $utf8NoBom)
if ($true) {

  # 探测真正能跑的 python：Windows 上通常是 python 或 py，不是 python3。
  # 光看命令存在不算数（微软商店那个桩会假装存在），必须实际执行一次。
  $pyBin = $null
  foreach ($c in @('python','py','python3')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
      try { & $cmd.Source -c 'import json,urllib.request' 2>$null; if ($LASTEXITCODE -eq 0) { $pyBin = $cmd.Source; break } } catch {}
    }
  }

  if ($pyBin) {
    $agents = Join-Path $codexHome 'AGENTS.md'
    $begin  = '<!-- CARHER-IMAGE-BEGIN -->'
    $end    = '<!-- CARHER-IMAGE-END -->'
    $kept = ''
    if (Test-Path $agents) {
      if (-not (Test-Path $bkDir)) { New-Item -ItemType Directory -Path $bkDir -Force | Out-Null }
      Copy-Item $agents (Join-Path $bkDir 'AGENTS.md') -Force
      $skip = $false
      foreach ($ln in Get-Content $agents) {
        if ($ln -eq $begin) { $skip = $true; continue }
        if ($ln -eq $end)   { $skip = $false; continue }
        if (-not $skip) { $kept += $ln + "`n" }
      }
    }
    $imgPy   = Join-Path $codexHome 'carher_image.py'
    $block = @"
$begin
## 画图

用户要画图、生成图片、出图、做配图/插画/海报/头像时，**只用下面这个命令**（它调公司的 muse-image），
**不要询问、不要用 SVG / ASCII / HTML / matplotlib 代替，也不要用内置 image_gen 或别的生图方式**：

    "$pyBin" "$imgPy" "画面描述" 输出路径

- 画面描述用用户的原话，可以补充风格、构图、颜色等细节。
- 约 30 秒出图。key 已配好，不要向用户索要，也不要在命令里写 key。
- 输出文件的后缀会按实际格式自动改（一般是 .webp），以命令最后打印的「已生成：…」路径为准，把这个路径告诉用户。
- 命令报错就把报错原文告诉用户，不要换别的方式硬画。
$end
"@
    [System.IO.File]::WriteAllText($agents, ($kept + $block + "`n"), $utf8NoBom)
    Ok "√ 已开通生图能力"
  } else {
    Warn "! 没找到可用的 Python，生图能力未开通（其余配置已生效）"
  }
}

# --- 7. 模型列表自动同步 ---
# 网关 /v1/models 已按这把 key 的白名单过滤；同步进 Codex 的模型选择器，
# 网关上新加的模型第二天自动出现。装好后立即跑一次，之后每天 10:17 + 每次登录各一次。
if ($pyBin) {
  $syncPy = Join-Path $codexHome 'carher_models_sync.py'
  $env:CODEX_HOME = $codexHome
  & $pyBin $syncPy
  if ($LASTEXITCODE -eq 0) { Ok "√ 已同步模型列表" }
  else { Warn "! 模型列表同步失败，选择器暂时只显示默认模型（其余配置已生效）" }

  # 计划任务直接调 pythonw（不弹黑窗）。CODEX_HOME 不用传：脚本默认就是 %USERPROFILE%\.codex，
  # 日志由脚本自己写到 carher-models-sync.log，不靠 cmd 重定向（cmd /c 的引号嵌套在计划任务里很容易拼错）。
  $wName = if ((Split-Path $pyBin -Leaf) -ieq 'py.exe') { 'pyw.exe' } else { 'pythonw.exe' }
  $pyw = Join-Path (Split-Path $pyBin) $wName
  $runner = if (Test-Path $pyw) { $pyw } else { $pyBin }
  $tr = "`"$runner`" `"$syncPy`""
  # 顶部 ErrorActionPreference=Stop，schtasks 不可用（被策略禁掉）时会直接中断整个脚本，所以包 try
  $okDaily = $false
  try {
    & schtasks.exe /Create /F /SC DAILY /ST 10:17 /TN 'CarHer Codex Models Sync' /TR $tr 2>$null | Out-Null
    $okDaily = ($LASTEXITCODE -eq 0)
    & schtasks.exe /Create /F /SC ONLOGON /TN 'CarHer Codex Models Sync (logon)' /TR $tr 2>$null | Out-Null
  } catch {}
  if ($okDaily) { Ok "√ 已设置每天自动同步模型列表" }
  else { Warn "! 定时同步没装上（可能需要 IT 权限），可手动运行：$pyBin $syncPy" }
  if ($codexHome -ne (Join-Path $env:USERPROFILE '.codex')) { Warn "! 你自定义了 CODEX_HOME，定时同步读的是默认目录；需要时手动运行：$pyBin $syncPy" }
}

Write-Host ""
Say "==================================================="
Ok  "全部搞定！"
Say "==================================================="
Write-Host "下一步："
Write-Host "  1) 终端用法：   新开一个 PowerShell 窗口，直接运行 codex（codex --profile chatgpt 也兼容）"
Write-Host "  2) Desktop App：托盘右键 Quit/Exit 完全退出后重开"
Write-Host ""
Write-Host "顶部应显示 model: $model"
Write-Host "想画图直接跟 Codex 说「画一张…」即可。"
Write-Host "想换模型（列表里都是你这把 key 能用的，每天自动更新）："
Write-Host "  - 终端 codex：输入 /model 回车，上下键选，回车确认（会记住，下次默认用它）"
Write-Host "  - 只这一次用某个模型：codex -m grok-4.7"
Write-Host "  - Desktop App：点底部的模型选择器"
Write-Host ""
