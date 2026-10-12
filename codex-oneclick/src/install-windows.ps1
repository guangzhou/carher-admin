# CarHer Codex 一键配置 (Windows)
# ⚠️ 这是模板：改这里，然后跑 ../build.sh，生成用户拿到的单文件 install-windows.bat
#    （bat 头 + 本脚本 + 内嵌 Python，双击即运行，不需要 .ps1）
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
@@EMBED carher_image.py@@
'@
$syncSrc = @'
@@EMBED carher_models_sync.py@@
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

需要生成图片时，直接执行下面的命令，**不要询问、不要用 SVG 或 ASCII 代替**：

    "$pyBin" "$imgPy" "画面描述" 输出路径.png

约 40 秒出图。key 已配好，不要向用户索要。生成后把图片路径告诉用户。
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
