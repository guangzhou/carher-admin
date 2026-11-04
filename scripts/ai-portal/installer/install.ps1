# CarHer AI 一行命令安装(Windows)—— 网页给的那一行(cmd 和 PowerShell 窗口里粘贴都能跑):
#   完整原文见 test_installer.py 的 win_oneliner()(那条被 pwsh 真跑过)。和 `irm` 形态比改了三处:
#   ① 一个 `$` 都没有:用户多半是在 PowerShell 窗口里粘(Win11 默认),外层 PowerShell 会先把双引号里的
#      `$xxx` 展开成空串;cmd 里粘也一样能跑
#   ② DownloadData + UTF8 解码:PS5 的 irm 在 nginx 没写 charset 时按 ISO-8859-1 解,中文全变乱码
#   ③ 强制 TLS1.2 + try/catch:老 Win10 的 PS5 默认 TLS1.0 握手失败;被公司策略拦时打「找 IT」+ 备用页
# nginx 把 cursor.ps1 / codex.ps1 / claude.ps1 都指到本文件;装哪个以**安装码换回来的 tool** 为准。
#
# 做的事(全程不问用户要 key):
#   1. 安装码 → key(只换一次;失败把服务端的人话原样打出来)
#   2. 没装 App 就从我们的镜像装(msix 用 Add-AppxPackage,exe 静默装),已装跳过
#   3. 下载并运行现有一键安装器(codex-windows.bat / claude-windows.bat 里的 PowerShell 正文 /
#      cursor-windows.zip 引擎),key 走环境变量 CARHER_KEY 交给它(不进命令行)
#   4. 替用户把 App 退出再打开
# 🔴 key 只打 长度 + 后 4 位。
# 只给测试用:$env:CARHER_PORTAL_BASE / CARHER_MIRROR_BASE / CARHER_SKIP_APP_INSTALL=1
param([string]$Code = '', [string]$Tool = '')

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # PS5 的进度条会让下载慢 10 倍
$fallbackPage = 'https://chat.auto-link.com.cn/oneclick/'
function Say($m)  { Write-Host $m -ForegroundColor Cyan }
function Ok($m)   { Write-Host $m -ForegroundColor Green }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Die($m, [int]$rc = 1) { Write-Host $m -ForegroundColor Red; exit $rc }

# 公司电脑把 PowerShell 锁成「受限语言模式」(AppLocker / WDAC)时,后面调 .NET 全会失败,报一堆看不懂的英文
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
  Die "x 这台电脑的 PowerShell 被公司策略限制了(语言模式:$($ExecutionContext.SessionState.LanguageMode))。`n  请找 IT 开放 PowerShell 执行权限;或用备用下载页:$fallbackPage"
}
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$portal = if ($env:CARHER_PORTAL_BASE) { $env:CARHER_PORTAL_BASE.TrimEnd('/') } else { 'https://chat.auto-link.com.cn/ai' }
$mirror = if ($env:CARHER_MIRROR_BASE) { $env:CARHER_MIRROR_BASE.TrimEnd('/') } else { 'https://chat.auto-link.com.cn/ccdl' }
if (-not $Code -and $env:CARHER_INSTALL_CODE) { $Code = $env:CARHER_INSTALL_CODE }

Write-Host ""
Say "==================================================="
Say "   CarHer AI 一行命令安装 (Windows)"
Say "==================================================="
Write-Host ""

if (-not $Code) { Die "x 命令里没有安装码。回网页 $portal/ 重新复制完整的一行命令。" }
# 码要拼进 JSON 请求体:只收字母数字 - _(防注入,也防复制时带进空格/引号)
if ($Code -notmatch '^[A-Za-z0-9_-]+$') { Die "x 安装码格式不对(只应有字母、数字、-、_)。回网页重新复制完整的一行命令。" }

function Mask-Key([string]$k) {
  $t = if ($k.Length -ge 4) { $k.Substring($k.Length - 4) } else { '' }
  "长度 $($k.Length),后4位 ...$t"
}

# ── 1. 安装码 → key ──────────────────────────────────────────────────
# 用 HttpClient:4xx 不抛异常、回包按 UTF-8 自己解(PS5 的 Invoke-RestMethod 在服务端不写 charset 时
# 按 ISO-8859-1 解,中文报错会变乱码);走系统代理。不重试:POST 已到服务端但回包丢了时,重试只会拿到「已用过」。
Say "→ 用安装码换取配置 ..."
try { Add-Type -AssemblyName System.Net.Http } catch {}
$status = 0; $body = ''; $netErr = ''
try {
  $hc = New-Object System.Net.Http.HttpClient
  $hc.Timeout = [TimeSpan]::FromSeconds(30)
  $content = New-Object System.Net.Http.StringContent("{`"code`":`"$Code`"}", [Text.Encoding]::UTF8, 'application/json')
  $resp = $hc.PostAsync("$portal/api/install-code/redeem", $content).GetAwaiter().GetResult()
  $status = [int]$resp.StatusCode
  $body = [Text.Encoding]::UTF8.GetString($resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult())
} catch {
  $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
  $netErr = $e.Message
}
$j = $null; try { $j = $body | ConvertFrom-Json } catch {}
if ($status -ne 200) {
  if ($status -eq 0) { Die "x 连不上安装服务($portal):$netErr`n  检查网络后,回网页重新生成一条命令再试。" }
  $msg = if ($j -and $j.error) { [string]$j.error } else { ($body -replace 'sk-[A-Za-z0-9_-]*', 'sk-***') }
  if ($msg.Length -gt 300) { $msg = $msg.Substring(0, 300) }
  Die "x 安装码没换成功(HTTP $status):$msg"
}
$key = if ($j) { [string]$j.key } else { '' }
$tool = if ($j) { [string]$j.tool } else { '' }
$body = $null; $j = $null
if (-not $key) { Die "x 安装服务回包里没有 key(服务端问题),把这一屏截图发给运维。" }
if ($key -notmatch '^[A-Za-z0-9_-]+$') { Die "x 安装服务给的 key 格式不对(服务端问题),把这一屏截图发给运维。" }
if (@('cursor', 'codex', 'claude') -notcontains $tool) { Die "x 安装服务没说要装哪个工具(tool=$tool),把这一屏截图发给运维。" }
if ($Tool -and $Tool -ne $tool) { Warn "↳ 这条命令的网址是 $Tool,但安装码是给 $tool 的 ⇒ 按安装码装 $tool。" }
Ok "√ 安装码有效:装 $tool,key 已拿到($(Mask-Key $key))"
Write-Host ""

$work = Join-Path ([IO.Path]::GetTempPath()) ("carher-install-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null

function Get-File([string]$url, [string]$out) {
  # Win10 1803+ 自带 curl.exe:有进度条、能重试;没有就退回 Invoke-WebRequest
  $curl = if ($env:SystemRoot) { Join-Path $env:SystemRoot 'System32\curl.exe' } else { $null }
  if ($curl -and (Test-Path $curl)) {
    & $curl -fSL --connect-timeout 15 --retry 3 --retry-delay 2 --progress-bar -o $out $url
    if ($LASTEXITCODE -ne 0) { throw "下载失败(curl 退出码 $LASTEXITCODE):$url" }
  } else {
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -TimeoutSec 1800
  }
}

$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { 'arm64' } else { 'x64' }
$skipApp = ($env:CARHER_SKIP_APP_INSTALL -eq '1')

# ── 2. App:找 / 装 ────────────────────────────────────────────────────
# 找法与现有安装器 / 自检一致(那边 10-15、11-03 同事实机踩过):
#   Claude / Codex 是 MSIX ⇒ Get-AppxPackage 模糊匹配(别用 -Name 精确匹配),Claude 再兜 .exe 安装版
#   Cursor 是 exe ⇒ 和 cursor_team_setup.cmd 同一批常见位置 + 注册表 App Paths
function Find-CursorExe {
  $c = @()
  if ($env:CURSOR_BIN) { $c += $env:CURSOR_BIN }
  if ($env:LOCALAPPDATA) { $c += (Join-Path $env:LOCALAPPDATA 'Programs\cursor\Cursor.exe'); $c += (Join-Path $env:LOCALAPPDATA 'cursor\Cursor.exe') }
  if ($env:ProgramFiles) { $c += (Join-Path $env:ProgramFiles 'Cursor\Cursor.exe') }
  if (${env:ProgramFiles(x86)}) { $c += (Join-Path ${env:ProgramFiles(x86)} 'Cursor\Cursor.exe') }
  $c += 'D:\Program Files\Cursor\Cursor.exe'; $c += 'D:\Cursor\Cursor.exe'
  foreach ($p in $c) { if ($p -and (Test-Path $p)) { return $p } }
  foreach ($h in 'HKCU:', 'HKLM:') {
    try { $v = (Get-ItemProperty "$h\Software\Microsoft\Windows\CurrentVersion\App Paths\Cursor.exe" -ErrorAction Stop).'(default)'; if ($v -and (Test-Path $v)) { return $v } } catch {}
  }
  return $null
}
function Find-AppxPkg([string]$t) {
  try {
    $all = Get-AppxPackage -ErrorAction SilentlyContinue
    if ($t -eq 'claude') { return ($all | Where-Object { $_.Name -match 'Claude' -and $_.Publisher -match 'Anthropic' } | Sort-Object Version -Descending | Select-Object -First 1) }
    if ($t -eq 'codex')  { return ($all | Where-Object { $_.Name -match 'Codex' } | Sort-Object Version -Descending | Select-Object -First 1) }
  } catch {}
  return $null
}
function Find-ClaudeExe {  # .exe 安装版(非 MSIX):有 resources\app.asar 且 exe 叫 claude.exe
  try { $pp = Get-Process -Name 'Claude' -ErrorAction SilentlyContinue | Where-Object { $_.Path } | Select-Object -First 1; if ($pp) { return $pp.Path } } catch {}
  foreach ($r in @($env:LOCALAPPDATA, (Join-Path "$env:LOCALAPPDATA" 'Programs'))) {
    if (-not $r -or -not (Test-Path $r)) { continue }
    $hit = Get-ChildItem -Path $r -Filter app.asar -Recurse -Depth 4 -ErrorAction SilentlyContinue |
      Where-Object { $_.Directory.Name -eq 'resources' -and (Test-Path (Join-Path $_.Directory.Parent.FullName 'claude.exe')) } | Select-Object -First 1
    if ($hit) { return (Join-Path $hit.Directory.Parent.FullName 'claude.exe') }
  }
  return $null
}
function Find-App {  # ⇒ @{ kind='exe'|'appx'; exe=…; pkg=… } 或 $null
  if ($tool -eq 'cursor') { $e = Find-CursorExe; if ($e) { return @{ kind = 'exe'; exe = $e } } ; return $null }
  $p = Find-AppxPkg $tool
  if ($p) { return @{ kind = 'appx'; pkg = $p } }
  if ($tool -eq 'claude') { $e = Find-ClaudeExe; if ($e) { return @{ kind = 'exe'; exe = $e } } }
  return $null
}
$appName = @{ cursor = 'Cursor'; codex = 'Codex'; claude = 'Claude 桌面 App' }[$tool]
$appProc = @{ cursor = @('Cursor'); codex = @('Codex', 'ChatGPT'); claude = @('Claude') }[$tool]

function Install-App {
  $file = switch ($tool) {
    'cursor' { "apps/Cursor-windows-$arch.exe" }
    'codex'  { "apps/Codex-windows-$arch.msix" }
    'claude' { "desktop/Claude-windows-$arch.msix" }
  }
  $ext = [IO.Path]::GetExtension($file)
  $out = Join-Path $work ("app" + $ext)
  Say "→ 没找到 $appName,从公司镜像下载安装(几百 MB,首次会慢一点)..."
  try { Get-File "$mirror/$file" $out } catch { Die "x 下载 $appName 失败:$($_.Exception.Message)`n  也可以去 $fallbackPage 手动下载安装 App 后,回网页重新生成命令再跑。" }
  if ($ext -eq '.msix') {
    # 签名由 Add-AppxPackage 自己校验(不可信签名直接拒装);不要管理员
    try { Add-AppxPackage -Path $out -ForceApplicationShutdown -ErrorAction Stop }
    catch {
      Die "x 安装 $appName 失败:$($_.Exception.Message)`n  公司电脑常见原因是禁止安装 MSIX 应用:请找 IT 开放「旁加载 / 安装应用」权限;或用备用下载页:$fallbackPage"
    }
  } else {
    $sig = Get-AuthenticodeSignature $out
    if ($sig.Status -ne 'Valid') { Die "x $appName 安装包签名校验没过($($sig.Status)),拒绝安装。把这一屏截图发给运维。" }
    Say "  签名:$($sig.SignerCertificate.Subject)"
    # Cursor 用的是 VS Code 同款 Inno Setup 用户版安装器:静默装到当前用户,不要管理员;!runcode = 装完别自己打开
    $p = Start-Process -FilePath $out -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/MERGETASKS=!runcode' -Wait -PassThru
    if ($p.ExitCode -ne 0) { Die "x 安装 $appName 失败(安装程序退出码 $($p.ExitCode))。可以用备用下载页手动装:$fallbackPage" }
  }
  Remove-Item $out -Force -ErrorAction SilentlyContinue
  Ok "√ $appName 已装好"
}

$app = $null
if (-not $skipApp) {
  $app = Find-App
  if ($app) { Ok "√ 已装 $appName" }
  else {
    Install-App
    $app = Find-App
    if (-not $app) { Die "x $appName 装完了却找不到,把这一屏截图发给运维。" }
  }
} else { Warn "↳ 跳过安装 App(CARHER_SKIP_APP_INSTALL=1)" }
Write-Host ""

# GUI 主进程:有窗口的那个(Electron 的 renderer/gpu 子进程没窗口;Cursor 自愈任务是借 Cursor.exe 跑 .js,也没窗口)
function Get-AppGui { Get-Process -Name $appProc -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } }
function Get-AppAll { Get-Process -Name $appProc -ErrorAction SilentlyContinue }
function Close-App([int]$wait = 30) {  # 正常关窗口(有没保存的 App 会自己问),不强杀 ⇒ $true=已退出
  $g = @(Get-AppGui); if (-not $g.Count) { return $true }
  foreach ($p in $g) { try { [void]$p.CloseMainWindow() } catch {} }
  for ($i = 0; $i -lt $wait; $i++) { Start-Sleep 1; if (-not @(Get-AppGui).Count) { Start-Sleep 2; return $true } }
  return $false
}
function Start-App {
  if (-not $app) { return }
  try {
    if ($app.kind -eq 'exe') { Start-Process -FilePath $app.exe | Out-Null }
    else {
      $id = (Get-AppxPackageManifest $app.pkg).Package.Applications.Application | Select-Object -First 1 -ExpandProperty Id
      Start-Process "shell:AppsFolder\$($app.pkg.PackageFamilyName)!$id" | Out-Null
    }
    Ok "√ 已打开 $appName"
  } catch { Warn "! $appName 没能自动打开,请自己打开一次。" }
}

# ── 3. Cursor 专属:设置库要先有、Cursor 要先退出(引擎两条硬闸:没库 exit 3,开着 exit 2)──
if ($tool -eq 'cursor' -and $app) {
  $cdb = Join-Path "$env:APPDATA" 'Cursor\User\globalStorage\state.vscdb'
  if (-not (Test-Path $cdb)) {
    Say "→ Cursor 第一次用:先自动打开一次让它初始化(10~60 秒),好了会自动关掉 ..."
    Start-App
    for ($i = 0; $i -lt 120 -and -not (Test-Path $cdb); $i++) { Start-Sleep 1 }
    if (-not (Test-Path $cdb)) { Die "x Cursor 打开 2 分钟还没初始化好。手动打开 Cursor 登录一次、完全退出后,回网页重新生成命令。" }
    Start-Sleep 5
  }
  if (@(Get-AppGui).Count) {
    Say "→ 先把 Cursor 退出(配置要在它关着时写;有没保存的文件 Cursor 会问你)..."
    if (-not (Close-App 60)) { Die "x Cursor 没退出(可能在等你保存文件)。保存后右键任务栏图标退出 Cursor,回网页重新生成命令再跑。" }
    Ok "√ Cursor 已退出"
  }
}

# ── 4. 运行现有一键安装器 ────────────────────────────────────────────
# 用**当前这个** PowerShell 的可执行文件起子进程(PS5 = powershell.exe;PS7 = pwsh):
#   现有安装器出错时 `exit 1`,在本进程里跑会把这里一起带走,后面「重启 App / 报失败原因」就没了。
$psExe = (Get-Process -Id $PID).Path
Say "→ 下载 $tool 一键安装器 ..."
$rc = 1
try {
  if ($tool -eq 'cursor') {
    $zip = Join-Path $work 'cursor.zip'
    try { Get-File "$portal/i/pkg/cursor-windows.zip" $zip } catch { Die "x 下载安装器失败:$($_.Exception.Message)" }
    Expand-Archive -Path $zip -DestinationPath $work -Force
    $eng = Join-Path $work 'cursor-windows'
    if (-not (Test-Path (Join-Path $eng 'cursor_team_setup.cmd'))) { Die "x 安装包里缺 cursor_team_setup.cmd(镜像出问题了),把这一屏截图发给运维。" }
    Write-Host ""
    $env:CARHER_KEY = $key
    if ($app -and $app.exe) { $env:CURSOR_BIN = $app.exe }
    $engJs = Get-Content -Raw -Encoding UTF8 (Join-Path $eng 'cursor_team_setup.js')
    # 直接 & 调 .cmd(PowerShell 交给 cmd.exe 跑,不受执行策略管);别拼 `cmd /c "…"`:PS5 对带引号带空格的参数
    # 转义规则和 PS7 不一样,临时目录在「C:\Users\张 三\…」这种路径下会切错
    $cmdPath = Join-Path $eng 'cursor_team_setup.cmd'
    if ($engJs -match 'process\.env\.CARHER_KEY') { & $cmdPath --apply }
    else { & $cmdPath --apply --key $key }   # 老引擎只认 --key
    $rc = $LASTEXITCODE
  } else {
    $bat = Join-Path $work "$tool-windows.bat"
    try { Get-File "$portal/i/pkg/$tool-windows.bat" $bat } catch { Die "x 下载安装器失败:$($_.Exception.Message)" }
    $txt = [IO.File]::ReadAllText($bat, [Text.Encoding]::UTF8)
    if ($txt.IndexOf('##CARHER-PS1') -lt 0) { Die "x 安装器下载不完整,回网页重新生成命令再试。" }
    Write-Host ""
    # 和 .bat 头同一段启动逻辑(从自身文件里切出 PowerShell 正文执行),只是不走 cmd、末尾不 pause
    $env:CARHER_SELF = $bat; $env:CARHER_KEY = $key
    $boot = '$c=[IO.File]::ReadAllText($env:CARHER_SELF,[Text.Encoding]::UTF8);$i=$c.IndexOf(''#''+''#CARHER-PS1'');& ([scriptblock]::Create($c.Substring($i)))'
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($boot))
    & $psExe -NoProfile -ExecutionPolicy Bypass -EncodedCommand $enc
    $rc = $LASTEXITCODE
  }
} finally {
  Remove-Item Env:CARHER_KEY, Env:CARHER_SELF -ErrorAction SilentlyContinue
  $key = $null
}
Write-Host ""

# ── 5. App 退出再开 ──────────────────────────────────────────────────
if ($app -and ($rc -eq 0 -or $rc -eq 5)) {
  if (@(Get-AppGui).Count) {
    Say "→ 重启 $appName 让配置生效 ..."
    if (Close-App 30) { Start-App } else { Warn "! $appName 没退出(可能在等你保存),请右键任务栏图标退出后再打开。" }
  } elseif (-not @(Get-AppAll).Count) { Start-App }
  else { Warn "↳ $appName 还在后台运行(托盘),请右键托盘图标退出后再打开,配置才生效。" }
}
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue

if ($rc -eq 0) { Ok "√ 全部完成。" }
elseif ($rc -eq 5) { Warn "! 基本装好了,但上面有标红的项(退出码 5)。把这一屏截图发给运维。"; exit 5 }
else { Die "x 安装器没跑完(退出码 $rc)。这个安装码已经用掉了:看上面的报错处理后,回网页重新生成一条命令再跑。`n  也可以用备用下载页:$fallbackPage" $rc }
