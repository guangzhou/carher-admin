@echo off
setlocal
set "CARHER_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=[IO.File]::ReadAllText($env:CARHER_SELF,[Text.Encoding]::UTF8);$i=$c.IndexOf('#'+'#CARHER-PS1');& ([scriptblock]::Create($c.Substring($i)))"
echo.
pause
exit /b
##CARHER-PS1
# CarHer Claude Code 自检 (Windows) —— 只读,不改任何配置
# 由 build.sh 生成，别直接改；源在 claude-oneclick/src/
# 与 doctor-mac.sh 同一张判据表;key 只打长度 + sha256 前 8 位。只用 PowerShell 5.1 语法(Windows 自带的那版)。
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$claudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }
$wantBase = 'https://cc.auto-link.com.cn/pro'
$claudeMin = [version]'2.1.257'
$script:out = New-Object System.Collections.Generic.List[string]
$script:red = New-Object System.Collections.Generic.List[string]
$script:yel = New-Object System.Collections.Generic.List[string]
function P([string]$s) { $script:out.Add($s); Write-Host $s }
function Red([string]$s) { $script:red.Add($s) }
function Yel([string]$s) { $script:yel.Add($s) }
function Mask([string]$s) { if ($null -eq $s) { return '' }; return ($s -replace 'sk-[A-Za-z0-9_-]{6,}', 'sk-***') }
function Sha8([string]$s) {
  $h = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($s))
  return (($h | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 8)
}
function BadChars([string]$s) { if (-not $s) { return 0 }; return ([regex]::Matches($s, '[^\x21-\x7e]')).Count }
function KeyFp([string]$s) { if (-not $s) { return '无' }; return "len=$($s.Length) sha=$(Sha8 $s) 异常字符=$(BadChars $s)" }
function Http([string]$url, [hashtable]$headers, [string]$body) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  try {
    $p = @{ Uri = $url; Headers = $headers; TimeoutSec = 60; UseBasicParsing = $true }
    if ($body) { $p.Method = 'Post'; $p.Body = [Text.Encoding]::UTF8.GetBytes($body); $p.ContentType = 'application/json' }
    $r = Invoke-WebRequest @p
    return @{ code = [int]$r.StatusCode; body = $r.Content; ms = $sw.ElapsedMilliseconds }
  } catch {
    $code = 0; $b = $_.Exception.Message
    if ($_.Exception.Response) {
      $code = [int]$_.Exception.Response.StatusCode
      try { $b = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd() } catch {}
    }
    return @{ code = $code; body = $b; ms = $sw.ElapsedMilliseconds }
  }
}
function ReadJson([string]$f) { try { return (Get-Content $f -Raw -Encoding UTF8) -replace '^﻿', '' | ConvertFrom-Json } catch { return $null } }

P "=== CarHer Claude Code 自检 (Windows) ==="
P "时间    : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')"
P "系统    : $([Environment]::OSVersion.VersionString) $env:PROCESSOR_ARCHITECTURE  PowerShell $($PSVersionTable.PSVersion)"
P "配置目录: $claudeHome"

P ""; P "--- 1) 命令行 claude ---"
foreach ($d in @((Join-Path $env:USERPROFILE '.carher-node'), (Join-Path $env:USERPROFILE '.carher-npm-global'))) {
  if (Test-Path $d) { $env:Path = "$d;$env:Path" }
}
$nd = Get-Command node -ErrorAction SilentlyContinue
if ($nd) { P "node    : $((& node --version 2>$null))" } else { P "node    : 没找到" }
$cl = Get-Command claude -ErrorAction SilentlyContinue | Select-Object -First 1
if ($cl) {
  $cv = ''; $vs = (& claude --version 2>$null | Select-Object -First 1); if ($vs -match '(\d+\.\d+\.\d+)') { $cv = $matches[1] }
  P "claude  : $($cl.Source)  $cv"
  if ($cv -and ([version]$cv -lt $claudeMin)) { Red "Claude Code $cv 太旧(需要 ≥ $claudeMin,否则新模型报 400):重新双击 claude-windows.bat" }
} else { P "claude  : 没找到"; Yel "找不到 claude:关掉 PowerShell 新开一个;还不行就重新双击 claude-windows.bat" }

P ""; P "--- 2) settings.json ---"
$s = Join-Path $claudeHome 'settings.json'; $key = ''; $model = ''
if (Test-Path $s) {
  P "settings.json : 有 ($((Get-Item $s).Length) 字节,改于 $((Get-Item $s).LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')))"
  $j = ReadJson $s
  if (-not $j) { Red "settings.json 不是合法 JSON:把它发给运维,或删掉后重新双击 claude-windows.bat" } else {
    $e = $j.env
    $base = [string]$e.ANTHROPIC_BASE_URL; $key = [string]$e.ANTHROPIC_API_KEY; $tok = [string]$e.ANTHROPIC_AUTH_TOKEN
    $disc = [string]$e.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY; $off = [string]$e.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
    $model = [string]$j.model
    $haiku = [string]$e.ANTHROPIC_DEFAULT_HAIKU_MODEL
    $npick = 0; if ($j.modelPicker -and $j.modelPicker.options) { $npick = @($j.modelPicker.options).Count }
    P "  ANTHROPIC_BASE_URL = $(if ($base) { $base } else { '<没写>' })"
    P "  ANTHROPIC_API_KEY  : $(KeyFp $key)"
    if ($tok) { P "  ANTHROPIC_AUTH_TOKEN : $(KeyFp $tok)" }
    P "  模型发现开关       = $(if ($disc) { $disc } else { '<没写>' })"
    P "  model              = $(if ($model) { $model } else { '<默认>' })"
    P "  modelPicker        = $npick 个"
    # 10-25:菜单只放我们按 key 生成的那份(每个模型一次);每次开 Claude Code 后台自动刷
    $rpl = ($j.modelPicker -and $j.modelPicker.replaceBuiltInOptions -eq $true)
    $hook = ($j.hooks -and (($j.hooks.SessionStart | ConvertTo-Json -Depth 8 -Compress) -match 'carher_models_refresh'))
    P "  菜单去重           = $(if ($rpl) { '是' } else { '否' })"
    P "  开机自动刷菜单     = $(if ($hook) { '已挂' } else { '没挂' })"
    if (-not $rpl) { Yel "/model 菜单里同一个模型会出现两次、还有画图模型:重新双击 claude-windows.bat" }
    if (-not $hook) { Yel "没挂开机自动刷菜单(key 新开的模型要手动说「刷模型」):重新双击 claude-windows.bat" }
    # 10-27:「Not logged in · Run /login」= 首次启动问「用不用这把 key」时回车选了默认的 No
    $gj = if ($env:CLAUDE_CONFIG_DIR) { Join-Path $env:CLAUDE_CONFIG_DIR '.claude.json' } else { Join-Path $env:USERPROFILE '.claude.json' }
    $kt = if ($key.Length -ge 20) { $key.Substring($key.Length - 20) } else { $key }
    $ks = 'new'; try { $r = (Get-Content $gj -Raw | ConvertFrom-Json).customApiKeyResponses; if ($r.rejected -contains $kt) { $ks = 'rejected' } elseif ($r.approved -contains $kt) { $ks = 'approved' } } catch {}
    P "  这把 key 已同意使用 = $(switch ($ks) { 'approved' { '是' } 'rejected' { '否(之前点了 No)' } default { '还没问过' } })"
    if ($ks -eq 'rejected') { Red "命令行显示「Not logged in」:这把 key 之前被选了「不使用」。下载最新的 claude-windows.bat 重跑一次(或在 Claude Code 里说「刷模型」)就会修好" }
    if ($base -ne $wantBase) { Red "settings.json 没指向公司网关:重新双击 claude-windows.bat" }
    if (-not $key) { Red "settings.json 里没有 key:重新双击 claude-windows.bat" }
    if ($tok) { Red "同时有 AUTH_TOKEN 和 API_KEY(/model 里只剩几个 Claude):重新双击 claude-windows.bat" }
    if ($off) { Red "开了 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC(会关掉模型发现):重新双击 claude-windows.bat" }
    if ($model -like 'claude-router-*') { Red "model 存的是旧编码名 $model(新开窗口会报 isn't recognized):重新双击 claude-windows.bat" }
  }
} else { P "settings.json : 没有"; Red "没有 $s ⇒ 一键配置没跑成功:重新双击 claude-windows.bat" }
if ((BadChars $key) -gt 0) { Red "key 里有看不见的字符(多半从飞书复制带进来的):去 key 表重新复制整格,重新双击 claude-windows.bat" }
$ov = @()
foreach ($v in 'ANTHROPIC_AUTH_TOKEN','ANTHROPIC_BASE_URL','ANTHROPIC_API_KEY','ANTHROPIC_MODEL','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC') {
  if ([Environment]::GetEnvironmentVariable($v, 'User') -or [Environment]::GetEnvironmentVariable($v, 'Machine')) { $ov += $v }
}
if ($ov.Count) { P "系统环境变量里的覆盖项: $($ov -join ' ')"; Red "系统环境变量里有 $($ov -join ' ') 会盖过配置:重新双击 claude-windows.bat,或在「编辑系统环境变量」里删掉" }

if ((Test-Path (Join-Path $claudeHome 'carher\carher_image.js')) -and (Test-Path (Join-Path $claudeHome 'skills\carher-image\SKILL.md'))) { P "画图工具      : 已装" }
else { P "画图工具      : 没装"; Yel "画图工具没装上:下载最新的 claude-windows.bat 重跑一次" }

P ""; P "--- 3) 桌面 App ---"
$pkg = $null
try { $pkg = Get-AppxPackage -Name 'Claude' -ErrorAction SilentlyContinue | Sort-Object Version -Descending | Select-Object -First 1 } catch {}
$asar = $null
if ($pkg -and $pkg.InstallLocation) { P "App     : MSIX $($pkg.Version)  $($pkg.InstallLocation)"; $c = Join-Path $pkg.InstallLocation 'app\resources\app.asar'; if (Test-Path $c) { $asar = $c } }
if (-not $asar) {
  foreach ($r in @($env:LOCALAPPDATA, (Join-Path $env:LOCALAPPDATA 'Programs'), $env:ProgramFiles)) {
    if (-not $r -or -not (Test-Path $r)) { continue }
    $hit = Get-ChildItem -Path $r -Filter app.asar -Recurse -Depth 5 -ErrorAction SilentlyContinue |
      Where-Object { $_.Directory.Name -eq 'resources' -and (Test-Path (Join-Path $_.Directory.Parent.FullName 'claude.exe')) } | Select-Object -First 1
    if ($hit) { $asar = $hit.FullName; P "App     : $($hit.Directory.Parent.FullName)"; break }
  }
}
if (-not $pkg -and -not $asar) { P "App     : 没找到(只用命令行可以忽略)" }
$run = Get-Process -Name 'claude' -ErrorAction SilentlyContinue
P "运行中  : $(if ($run) { '是' } else { '否' })"
$ud = Join-Path $env:LOCALAPPDATA 'Claude-3p'; $lib = Join-Path $ud 'configLibrary'
if ($pkg -or $asar) {
  $meta = ReadJson (Join-Path $lib '_meta.json')
  if ($meta -and $meta.appliedId -and (Test-Path (Join-Path $lib ($meta.appliedId + '.json')))) {
    $cf = ReadJson (Join-Path $lib ($meta.appliedId + '.json'))
    P "App 网关配置 : provider=$($cf.inferenceProvider)  base=$($cf.inferenceGatewayBaseUrl)  key: $(KeyFp ([string]$cf.inferenceGatewayApiKey))"
    if ($cf.inferenceProvider -ne 'gateway' -or $cf.inferenceGatewayBaseUrl -ne $wantBase) { Red "桌面 App 没指向公司网关:完全退出 App,重新双击 claude-windows.bat" }
    if ($key -and $cf.inferenceGatewayApiKey -and $key -ne $cf.inferenceGatewayApiKey) { Red "命令行和桌面 App 用的不是同一把 key:重新双击 claude-windows.bat" }
    # 10-25:这项没写 ⇒ App 给 Code/Cowork 会话开「严格沙箱」且一个外网都不放 ⇒ npm install 失败(终端正常)
    $eg = @($cf.coworkEgressAllowedHosts)
    P "App 外网放行 : $(if ($cf.coworkEgressAllowedHosts) { $eg -join ',' } else { '(没设 ⇒ 严格沙箱,命令连不上外网)' })"
    P "App 模型菜单 : $(if ($cf.inferenceModels) { @($cf.inferenceModels) -join ',' } else { '(没设 ⇒ App 自己拉,说「刷模型」后会和 key 一致)' })"
    if (-not ($eg -contains '*')) { Red "桌面 App 里跑命令连不上外网(npm install 会失败,设置页显示严格沙箱):完全退出 App,重新双击 claude-windows.bat" }
  } else { P "App 网关配置 : 没有 ($lib)"; Red "桌面 App 没配置过:完全退出 App,重新双击 claude-windows.bat" }
  if ($asar) {
    $pin = ''
    try { $txt = [Text.Encoding]::GetEncoding(28591).GetString([IO.File]::ReadAllBytes($asar)); if ($txt -match '\{"version":"(\d+\.\d+\.\d+)","manifest"') { $pin = $matches[1] } } catch {}
    if ($pin) {
      # MSIX 版 App 的本地数据可能被系统重定向到 Packages\Claude_*\LocalCache\Local 下 ⇒ 两处都找
      $bases = @(Join-Path $ud "claude-code\$pin")
      Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Packages') -Filter 'Claude_*' -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { $bases += (Join-Path $_.FullName "LocalCache\Local\Claude-3p\claude-code\$pin") }
      # 10-28:App 4.29 起目录是 claude-code\<ver>\<sha 前 12 位>\(老版是 claude-code\<ver>\)⇒ 两种都认
      $cands = @()
      foreach ($bd in $bases) { $cands += $bd; $cands += @(Get-ChildItem $bd -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }) }
      $hitDir = $cands | Where-Object { (Test-Path (Join-Path $_ '.verified')) -and (Test-Path (Join-Path $_ 'claude.exe')) } | Select-Object -First 1
      $anyExe = $cands | Where-Object { Test-Path (Join-Path $_ 'claude.exe') } | Select-Object -First 1
      if ($hitDir) { P "App 组件     : Claude Code $pin 已就绪 ($hitDir)" }
      elseif ($anyExe) { P "App 组件     : Claude Code $pin 有 claude.exe 但没有 .verified ($anyExe)"; Yel "桌面 App 组件可能没校验过:发消息不回时完全退出 App,重新双击 claude-windows.bat" }
      else { P "App 组件     : Claude Code $pin 没找到 (查了:$($cands -join ' ; '))"; Yel "桌面 App 组件没找到:如果 App 能正常回消息就不用管(App 可能自己下好放在别处);发消息不回再完全退出 App,重新双击 claude-windows.bat" }
      $mir = Http "https://cc.auto-link.com.cn/ccdl/claude-code-releases/$pin/manifest.json" @{} $null
      P "公司镜像     : $pin → HTTP $($mir.code)"
      if ($mir.code -ne 200) { Red "公司镜像里还没有 App 要的 Claude Code $pin(App 刚自动升级过):把这份报告发给运维" }
    } else { P "App 组件     : 读不到 App 钉死的版本" }
  }
  # App 代码(1.46388):win32 有 LOCALAPPDATA 时 setAppLogsPath(LOCALAPPDATA + 相对 appData 的 logs 路径) ⇒ %LOCALAPPDATA%\Claude-3p\logs
  $log = Join-Path $ud 'logs\main.log'
  if (-not (Test-Path $log)) { $log = Join-Path $env:APPDATA 'Claude-3p\logs\main.log' }
  if (Test-Path $log) {
    P "App 日志 ($log) 最近的关键行:"
    $tail = Get-Content $log -Tail 400 -Encoding UTF8
    $tail | Select-String -Pattern '3P mode active|Model discovery|Host Claude Code binary not available|still initializing|\[error\]' | Select-Object -Last 8 |
      ForEach-Object { $l = Mask $_.Line; if ($l.Length -gt 240) { $l = $l.Substring(0, 240) }; P "  $l" }
    if ($tail -match 'Host Claude Code binary not available') { Red "App 日志最近有「Host Claude Code binary not available」⇒ 组件没准备好:完全退出 App,重新双击 claude-windows.bat" }
  }
}

P ""; P "--- 4) 网络 ---"
# 🔴 10-19 sunqiang 报告:原来写 "设了代理 $v:…",PowerShell 把 `$v:连公司…` 当成驱动器作用域变量 ⇒
#    整段被吞成「设了代理 ,先关掉…」,且 HTTPS/HTTP 各打一遍。⇒ ${v} + 合成一条。
$px = @()
foreach ($v in 'HTTPS_PROXY','HTTP_PROXY','ALL_PROXY') {
  $pv = [Environment]::GetEnvironmentVariable($v)
  if ($pv) { P "代理变量 ${v} = $pv"; $px += $v }
}
if ($px.Count) { Yel ("设了代理(" + ($px -join ' / ') + "):网关通就不用管;连不上或很慢时先关掉 VPN/代理再试") }
try { $ie = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
  if ($ie.ProxyEnable -eq 1 -or $ie.AutoConfigURL) { P "系统代理      : 开着 ($($ie.ProxyServer)$($ie.AutoConfigURL))"; Yel "系统代理/VPN 开着:连不上或很慢时先关掉再试" } } catch {}
$n = Http "$wantBase/v1/models" @{} $null
P "网关(不带 key): HTTP $($n.code)  $($n.ms)ms   (401 = 网关通;0 = 连不上)"
if ($n.code -eq 0) { Red "连不上公司网关:检查网络,关掉 VPN/代理后再试" }

P ""; P "--- 5) 用这把 key 实打 ---"
if (-not $key) { P "(没有 key,跳过)" } else {
  $i = Http "$wantBase/key/info" @{ Authorization = "Bearer $key" } $null
  $alias = ''; if ($i.body -match '"key_alias"\s*:\s*"([^"]*)"') { $alias = $matches[1] }
  P "key 名字      : $(if ($alias) { $alias } else { '<查不到>' })"
  $h = @{ 'x-api-key' = $key; 'anthropic-version' = '2023-06-01' }
  $m = Http "$wantBase/v1/models" $h $null
  $ids = @([regex]::Matches([string]$m.body, '"id"\s*:\s*"([^"]*)"') | ForEach-Object { $_.Groups[1].Value })
  P "可用模型      : HTTP $($m.code),$($ids.Count) 个"
  if ($m.code -eq 401 -or $m.code -eq 403) { Red "key 无效(HTTP $($m.code)):去 key 表复制整格,重新双击 claude-windows.bat" }
  elseif ($m.code -ne 200) { Red "拉模型列表失败(HTTP $($m.code)):把这份报告发给运维" }
  # opus / sonnet / haiku 是 Claude Code 自己的简称,发出去前会换成真名 ⇒ 实打要用真名(10-19 sunqiang:拿 sonnet 直打 403 是误报)
  $mm = if ($model) { $model -replace '\[[^\]]*\]$', '' } else { 'claude-opus-5-5' }
  $hk = if ($haiku) { $haiku } else { 'claude-haiku-5-5' }
  switch ($mm) { 'opus' { $mm = 'claude-opus-5-5' } 'sonnet' { $mm = 'claude-sonnet-5-5' } 'haiku' { $mm = $hk } 'default' { $mm = 'claude-opus-5-5' } }
  if ($m.code -eq 200) {
    if ($ids -contains $mm) { P "  $mm : 已开通" } else { P "  $mm : 没开通"; Yel "这把 key 没开通 ${mm}:用 /model 换一个,或找运维开通" }
    # 后台小任务(起标题、子任务)用 haiku。Claude Code 2.1.30x 默认要 claude-haiku-5-5;key 没开通 ⇒ 时不时报错
    if ($ids -contains $hk) { P "  后台小任务模型 ${hk} : 已开通" }
    else { P "  后台小任务模型 ${hk} : 没开通$(if (-not $haiku) { '(Claude Code 默认值)' })"; Red "后台小任务用的 ${hk} 你的 key 没有 ⇒ 时不时报错:下载最新的 claude-windows.bat 重跑一次(会改成你 key 里有的 haiku)" }
    $r = Http "$wantBase/v1/messages" $h ('{"model":"' + $mm + '","max_tokens":64,"messages":[{"role":"user","content":"Reply with only: OK"}]}')
    $txt = ''; try { $txt = (@(($r.body | ConvertFrom-Json).content | ForEach-Object { $_.text }) -join '') } catch {}
    if ($txt.Length -gt 60) { $txt = $txt.Substring(0, 60) }
    P "实打 $mm : HTTP $($r.code)  $($r.ms)ms  回答 $(if ($txt) { $txt } else { '<无文字>' })"
    if ($r.code -ne 200) { $rb = Mask ([string]$r.body); if ($rb.Length -gt 300) { $rb = $rb.Substring(0, 300) }; P "  返回: $rb"; Red "用 $mm 实打失败(HTTP $($r.code)):把这份报告发给运维" }
  }
}

P ""; P "=== 结论 ==="
if ($script:red.Count -eq 0 -and $script:yel.Count -eq 0) { P "✅ 没查出问题。还有问题就把这份报告和出错时间、模型名一起发给运维。" }
foreach ($x in $script:red) { P "🔴 $x" }
foreach ($x in $script:yel) { P "⚠️  $x" }

$desk = [Environment]::GetFolderPath('Desktop'); if (-not $desk -or -not (Test-Path $desk)) { $desk = $env:USERPROFILE }
$rep = Join-Path $desk ("claude-自检-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + ".txt")
[IO.File]::WriteAllText($rep, ($script:out -join "`r`n") + "`r`n", (New-Object Text.UTF8Encoding $true))
Write-Host ""
$copied = $false
try { Set-Clipboard -Value ($script:out -join "`r`n") -ErrorAction Stop; $copied = $true } catch {}
if ($copied) { Write-Host "📋 报告已复制到剪贴板,也存在:$rep —— 直接粘给运维即可(里面没有 key)" -ForegroundColor Green }
else { Write-Host "📋 报告存在:$rep —— 把这个文件发给运维即可(里面没有 key)" -ForegroundColor Green }
