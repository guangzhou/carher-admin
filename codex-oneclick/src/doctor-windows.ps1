# CarHer Codex 自检 (Windows) —— 只读,不改任何配置
# ⚠️ 这是模板：改这里，然后跑 ../build.sh 生成用户拿到的 codex-doctor-windows.bat
# 与 doctor-mac.sh 同一张判据表;key 只打长度 + sha256 前 8 位。只用 PowerShell 5.1 语法(Windows 自带的那版)。
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$wantBase = 'https://cc.auto-link.com.cn/pro/v1'
$wantProv = 'carher_prod'
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
  # 返回 @{code=…; body=…; ms=…};连不上 code=0。不用 -UseBasicParsing 以外的参数,5.1 也能跑
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

P "=== CarHer Codex 自检 (Windows) ==="
P "时间    : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')"
P "系统    : $([Environment]::OSVersion.VersionString) $env:PROCESSOR_ARCHITECTURE  PowerShell $($PSVersionTable.PSVersion)"
P "用户目录: $codexHome"

P ""; P "--- 1) 命令行 codex ---"
foreach ($d in @((Join-Path $env:USERPROFILE '.carher-node'), (Join-Path $env:USERPROFILE '.carher-npm-global'))) {
  if (Test-Path $d) { $env:Path = "$d;$env:Path" }
}
$cx = Get-Command codex -ErrorAction SilentlyContinue | Select-Object -First 1
if ($cx) { P "codex   : $($cx.Source)  $((& codex --version 2>$null | Select-Object -First 1))" }
else { P "codex   : 没找到"; Yel "找不到 codex:关掉 PowerShell 新开一个;还不行就重新双击 codex-windows.bat" }
$nd = Get-Command node -ErrorAction SilentlyContinue
if ($nd) { P "node    : $((& node --version 2>$null))" } else { P "node    : 没找到" }

P ""; P "--- 2) 桌面 App ---"
$pkg = $null
try { $pkg = Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'Codex' -or $_.Name -match 'OpenAI' } | Select-Object -First 1 } catch {}
if ($pkg) { P "App     : $($pkg.Name) $($pkg.Version)" } else { P "App     : 没找到(只用命令行可以忽略)" }
$run = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(Codex|ChatGPT)$' }
P "运行中  : $(if ($run) { '是' } else { '否' })"

P ""; P "--- 3) 配置文件 ---"
$cfg = Join-Path $codexHome 'config.toml'; $auth = Join-Path $codexHome 'auth.json'
$k1 = ''; $k2 = ''; $prov = ''; $model = ''
if (Test-Path $cfg) {
  $lines = Get-Content $cfg -Encoding UTF8
  $sect = ''; $burl = ''; $oburl = ''
  foreach ($l in $lines) {
    if ($l -match '^\s*\[(.+)\]\s*$') { $sect = $matches[1]; continue }
    if ($sect -eq '' -and $l -match '^model_provider\s*=\s*"(.*)"') { $prov = $matches[1] }
    if ($sect -eq '' -and $l -match '^model\s*=\s*"(.*)"') { $model = $matches[1] }
    if ($sect -eq '' -and $l -match '^openai_base_url\s*=\s*"(.*)"') { $oburl = $matches[1] }
  }
  foreach ($l in $lines) {
    if ($l -match '^\s*\[(.+)\]\s*$') { $sect = $matches[1]; continue }
    if ($prov -and $sect -eq "model_providers.$prov") {
      if ($l -match '^base_url\s*=\s*"(.*)"') { $burl = $matches[1] }
      if ($l -match '^experimental_bearer_token\s*=\s*"(.*)"') { $k1 = $matches[1] }
    }
  }
  P "config.toml   : 有 ($((Get-Item $cfg).Length) 字节,改于 $((Get-Item $cfg).LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')))"
  P "  model_provider    = $(if ($prov) { $prov } else { '<没写>' })"
  P "  model             = $(if ($model) { $model } else { '<没写>' })"
  P "  provider base_url = $(if ($burl) { $burl } else { '<没写>' })"
  P "  openai_base_url   = $(if ($oburl) { $oburl } else { '<没写>' })"
  P "  key(config)       : $(KeyFp $k1)"
  if ($prov) { if ($burl -ne $wantBase) { Red "当前 provider「$prov」不指向公司网关:重新双击 codex-windows.bat" } }
  elseif ($oburl -ne $wantBase) { Red "config.toml 没指向公司网关:重新双击 codex-windows.bat" }
  if ($prov -ne $wantProv) { P "  (不是一键脚本写的标准配置;有问题时先重新双击 codex-windows.bat,原配置会自动备份)" }
} else { P "config.toml   : 没有"; Red "没有 $cfg ⇒ 一键配置没跑成功:重新双击 codex-windows.bat" }
if (Test-Path $auth) {
  try { $a = Get-Content $auth -Raw -Encoding UTF8 | ConvertFrom-Json; $k2 = [string]$a.OPENAI_API_KEY; $mode = [string]$a.auth_mode } catch { $mode = '<JSON坏了>' }
  P "auth.json     : auth_mode=$mode  key(auth) : $(KeyFp $k2)"
  if ($mode -ne 'apikey') { Red "auth.json 不是 apikey 模式(App 会要求登录 ChatGPT):重新双击 codex-windows.bat" }
} else { P "auth.json     : 没有"; Red "没有 auth.json ⇒ 桌面 App 用不了:重新双击 codex-windows.bat" }
if (-not ($k1 + $k2)) { Red "config.toml 和 auth.json 里都没有 key:重新双击 codex-windows.bat" }
if ($k1 -and $k2 -and $k1 -ne $k2) { Red "config.toml 和 auth.json 里是两把不同的 key:重新双击 codex-windows.bat" }
$key = if ($k1) { $k1 } else { $k2 }
if ((BadChars $key) -gt 0) { Red "key 里有看不见的字符(多半从飞书复制带进来的):去 key 表重新复制整格,重新双击 codex-windows.bat" }
if ($env:OPENAI_API_KEY) {
  $same = if ($env:OPENAI_API_KEY -eq $key) { '(和配置里同一把)' } else { '(和配置里不是同一把)' }
  P "环境变量 OPENAI_API_KEY : $(KeyFp $env:OPENAI_API_KEY) $same"
  if ($env:OPENAI_API_KEY -ne $key) { Yel "系统里设了另一把 OPENAI_API_KEY,某些工具会优先用它:不需要的话在「编辑系统环境变量」里删掉" }
}
$cat = Join-Path $codexHome 'carher-models.json'
if (Test-Path $cat) { P "模型列表      : $(([regex]::Matches((Get-Content $cat -Raw), '"slug"')).Count) 个" }
else { P "模型列表      : 没有 carher-models.json"; Yel "模型列表没同步(App 下拉里只有自带的几个 GPT,找不到 key 里的模型):下载最新的 codex-windows.bat 重跑一次" }
$slog = Join-Path $codexHome 'carher-models-sync.log'
if (Test-Path $slog) {
  P "  同步日志最后 3 行:"; Get-Content $slog -Tail 3 | ForEach-Object { P ("    " + (Mask $_)) }
  $last = Get-Content $slog | Select-String -Pattern '同步完成|失败' | Select-Object -Last 1
  if ($last -and $last.Line -match '失败') { Red "模型同步最近一次失败(下拉里只有自带的 GPT):下载最新的 codex-windows.bat 重跑一次;还不行把这份报告发给运维" }
}
try { $tk = & schtasks.exe /Query /TN 'CarHer Codex Models Sync' 2>$null; if ($LASTEXITCODE -eq 0) { P "每日同步      : 已装" } else { P "每日同步      : 没装"; Yel "模型列表不会每天自动更新:重新双击 codex-windows.bat" } } catch {}
$agents = Join-Path $codexHome 'AGENTS.md'
if (((Test-Path (Join-Path $codexHome 'carher_image.js')) -or (Test-Path (Join-Path $codexHome 'carher_image.py'))) -and (Test-Path $agents) -and (Select-String -Path $agents -Pattern 'CARHER-IMAGE-BEGIN' -Quiet)) { P "画图工具      : 已装" }
else { P "画图工具      : 没装"; Yel "画图工具没装上:下载最新的 codex-windows.bat 重跑一次" }

P ""; P "--- 4) 网络 ---"
foreach ($v in 'HTTPS_PROXY','HTTP_PROXY','ALL_PROXY') {
  $pv = [Environment]::GetEnvironmentVariable($v)
  # ${v}:写成 "$v:中文" 时 PowerShell 当成驱动器作用域变量,整段被吞(10-19 claude 自检同病)
  if ($pv) { P "代理变量 ${v} = $pv"; Yel "设了代理 ${v}:网关通就不用管;连不上或很慢时先关掉 VPN/代理再试" }
}
try { $ie = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
  if ($ie.ProxyEnable -eq 1 -or $ie.AutoConfigURL) { P "系统代理      : 开着 ($($ie.ProxyServer)$($ie.AutoConfigURL))"; Yel "系统代理/VPN 开着:连不上或很慢时先关掉再试" } } catch {}
$n = Http "$wantBase/models" @{} $null
P "网关(不带 key): HTTP $($n.code)  $($n.ms)ms   (401 = 网关通;0 = 连不上)"
if ($n.code -eq 0) { Red "连不上公司网关:检查网络,关掉 VPN/代理后再试" }

P ""; P "--- 5) 用这把 key 实打 ---"
if (-not $key) { P "(没有 key,跳过)" } else {
  $h = @{ Authorization = "Bearer $key" }
  $infoUrl = ($wantBase -replace '/v1$', '') + '/key/info'
  $i = Http $infoUrl $h $null
  $alias = ''; if ($i.body -match '"key_alias"\s*:\s*"([^"]*)"') { $alias = $matches[1] }
  P "key 名字      : $(if ($alias) { $alias } else { '<查不到>' })"
  $m = Http "$wantBase/models" $h $null
  $ids = @([regex]::Matches([string]$m.body, '"id"\s*:\s*"([^"]*)"') | ForEach-Object { $_.Groups[1].Value })
  P "可用模型      : HTTP $($m.code),$($ids.Count) 个"
  if ($m.code -eq 401 -or $m.code -eq 403) { Red "key 无效(HTTP $($m.code)):去 key 表复制整格,重新双击 codex-windows.bat" }
  elseif ($m.code -ne 200) { Red "拉模型列表失败(HTTP $($m.code)):把这份报告发给运维" }
  $mm = if ($model) { $model } else { 'gpt-5.6-terra' }
  if ($m.code -eq 200 -and -not ($ids -contains $mm)) {
    $first = $ids | Where-Object { $_ -notmatch 'image|imagine' -and $_ -ne 'codex-auto-review' } | Select-Object -First 1
    if ($first) { Red "默认模型 $mm 不在这把 key 里(每一轮都会 403)。下载最新的 codex-windows.bat 重跑一次会自动改成 ${first};或在 Codex 里用 /model 选 ${first}" }
  }
  foreach ($x in @($mm, 'muse-image')) {
    if ($ids -contains $x) { P "  $x : 已开通" } else { P "  $x : 没开通"; if ($m.code -eq 200) { Yel "这把 key 没开通 ${x}:找运维开通,或换一个模型" } }
  }
  if ($m.code -eq 200) {
    $r = Http "$wantBase/responses" $h ('{"model":"' + $mm + '","input":"Reply with only: OK","max_output_tokens":400}')
    $txt = ''
    try { $j = $r.body | ConvertFrom-Json; $txt = (@($j.output | Where-Object { $_.type -eq 'message' } | ForEach-Object { $_.content } | ForEach-Object { $_.text }) -join '') } catch {}
    if ($txt.Length -gt 60) { $txt = $txt.Substring(0, 60) }
    P "实打 $mm : HTTP $($r.code)  $($r.ms)ms  回答 $(if ($txt) { $txt } else { '<无文字>' })"
    if ($r.code -ne 200) { $rb = Mask ([string]$r.body); if ($rb.Length -gt 300) { $rb = $rb.Substring(0, 300) }; P "  返回: $rb"; Red "用默认模型 $mm 实打失败(HTTP $($r.code)):把这份报告发给运维" }
  }
}

P ""; P "=== 结论 ==="
if ($script:red.Count -eq 0 -and $script:yel.Count -eq 0) { P "✅ 没查出问题。还有问题就把这份报告和出错时间、模型名一起发给运维。" }
foreach ($x in $script:red) { P "🔴 $x" }
foreach ($x in $script:yel) { P "⚠️  $x" }

$desk = [Environment]::GetFolderPath('Desktop'); if (-not $desk -or -not (Test-Path $desk)) { $desk = $env:USERPROFILE }
$rep = Join-Path $desk ("codex-自检-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + ".txt")
[IO.File]::WriteAllText($rep, ($script:out -join "`r`n") + "`r`n", (New-Object Text.UTF8Encoding $true))
Write-Host ""
$copied = $false
try { Set-Clipboard -Value ($script:out -join "`r`n") -ErrorAction Stop; $copied = $true } catch {}
if ($copied) { Write-Host "📋 报告已复制到剪贴板,也存在:$rep —— 直接粘给运维即可(里面没有 key)" -ForegroundColor Green }
else { Write-Host "📋 报告存在:$rep —— 把这个文件发给运维即可(里面没有 key)" -ForegroundColor Green }
