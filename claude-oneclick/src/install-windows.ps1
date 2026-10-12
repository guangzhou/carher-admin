# CarHer Claude Code 一键配置 (Windows)
# ⚠️ 这是模板：改这里，然后跑 ../build.sh，生成用户拿到的单文件 install-windows.bat
#    （bat 头 + 本脚本，双击即运行，不需要 .ps1）
#
# 做四件事，用户只需要粘一次 key：
#   1. 没有 Node.js ≥22 就装一个到 %USERPROFILE%\.carher-node（不要管理员权限、不碰系统）
#   2. 没有 claude / 版本太旧就装最新版（走 npmmirror：官方 downloads.claude.ai 在国内不通）
#   3. 读 key
#   4. 把网关配置**合并**进 %USERPROFILE%\.claude\settings.json（保留用户原有设置），删模型列表缓存
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest 的进度条在 PS5 上会让下载慢 10 倍
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Say($m)  { Write-Host $m -ForegroundColor Cyan }
function Ok($m)   { Write-Host $m -ForegroundColor Green }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Die($m)  { Write-Host $m -ForegroundColor Red; exit 1 }

# ── 固定配置（用户无需感知）────────────────────────────────────────────
$baseUrl     = 'https://cc.auto-link.com.cn/pro'
$npmRegistry = 'https://registry.npmmirror.com'
$nodeMirror  = 'https://cdn.npmmirror.com/binaries/node'
$nodeMajorMin = 22
# 2.1.257 之前的版本打 5.x 模型会被上游以 claude_code_version_too_old 拒(400)
$claudeMin   = [version]'2.1.257'
$claudeHome  = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }
$nodeHome    = Join-Path $env:USERPROFILE '.carher-node'
$npmPrefix   = Join-Path $env:USERPROFILE '.carher-npm-global'

Write-Host ""
Say "==================================================="
Say "   CarHer Claude Code 一键配置 (Windows)"
Say "==================================================="
Write-Host ""

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

function Get-NodeMajor {
  try { [int](& node -p "process.versions.node.split('.')[0]" 2>$null) } catch { 0 }
}

# ── 1. Node.js ≥22 ───────────────────────────────────────────────────
if (Test-Path (Join-Path $nodeHome 'node.exe')) { $env:Path = "$nodeHome;$env:Path" }
$haveNode = [bool](Get-Command node -ErrorAction SilentlyContinue)
if ($haveNode -and (Get-NodeMajor) -ge $nodeMajorMin) {
  Ok "√ Node.js $(& node --version)"
} else {
  if ($haveNode) { Warn "! 现有 Node.js $(& node --version) 太旧(需要 >= v$nodeMajorMin),装一个新的到 $nodeHome(不影响你原来的)" }
  else { Say "→ 没有 Node.js,装一个到 $nodeHome(不需要管理员权限)" }
  try {
    $idx = Invoke-RestMethod -Uri "$nodeMirror/index.json" -TimeoutSec 30
  } catch { Die "x 取不到 Node.js 版本列表(网络?):$nodeMirror" }
  $rel = $idx | Where-Object { $_.lts -and $_.version -like "v$nodeMajorMin.*" } | Select-Object -First 1
  if (-not $rel) { Die "x 没找到 Node.js v$nodeMajorMin 的 LTS 版本" }
  $nodeVer = $rel.version
  $pkg = "node-$nodeVer-win-$arch"
  $tmp = Join-Path $env:TEMP "carher-node-$([guid]::NewGuid().ToString('N'))"
  New-Item -ItemType Directory -Path $tmp -Force | Out-Null
  try {
    Say "  下载 $pkg ..."
    $zip = Join-Path $tmp "$pkg.zip"
    Invoke-WebRequest -Uri "$nodeMirror/$nodeVer/$pkg.zip" -OutFile $zip -TimeoutSec 600 -UseBasicParsing
    # 校验:用官方 SHASUMS256.txt 比对,下坏的/被篡改的包不装
    $sums = (Invoke-WebRequest -Uri "$nodeMirror/$nodeVer/SHASUMS256.txt" -TimeoutSec 30 -UseBasicParsing).Content
    $want = ($sums -split "`n" | Where-Object { $_ -match "\s$([regex]::Escape("$pkg.zip"))\s*$" } | Select-Object -First 1) -replace '\s.*$', ''
    $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if (-not $want -or $want.Trim().ToLower() -ne $got) { Die "x Node.js 安装包校验失败(期望 $want,实际 $got),已停止" }
    if (Test-Path $nodeHome) { Remove-Item $nodeHome -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    Move-Item (Join-Path $tmp $pkg) $nodeHome
  } finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
  Add-UserPath $nodeHome
  if ((Get-NodeMajor) -lt $nodeMajorMin) { Die "x Node.js 装完仍不可用" }
  Ok "√ Node.js $(& node --version) 已装到 $nodeHome"
}

# npm 全局装到用户目录(不要管理员)
New-Item -ItemType Directory -Path $npmPrefix -Force | Out-Null
$env:npm_config_prefix = $npmPrefix
Add-UserPath $npmPrefix

# ── 2. Claude Code ───────────────────────────────────────────────────
function Get-ClaudeVer {
  $c = Get-Command claude -ErrorAction SilentlyContinue
  if (-not $c) { return $null }
  try { $s = (& claude --version 2>$null | Select-Object -First 1) } catch { return $null }
  if ($s -match '(\d+\.\d+\.\d+)') { return [version]$Matches[1] } else { return $null }
}
$cur = Get-ClaudeVer
if ($cur -and $cur -ge $claudeMin) {
  Ok "√ Claude Code $cur"
} else {
  if ($cur) { Warn "! Claude Code $cur 太旧(需要 >= $claudeMin),升级中 ..." }
  else { Say "→ 安装 Claude Code(约 1~3 分钟)..." }
  # npm 在 Windows 上是 npm.cmd;走 cmd /c 避免 PowerShell 执行策略拦 npm.ps1
  & cmd /c "npm install -g @anthropic-ai/claude-code --registry=$npmRegistry --no-fund --no-audit --loglevel=error"
  if ($LASTEXITCODE -ne 0) { Die "x 安装 Claude Code 失败(把上面的报错截图发给运维)" }
  $cur = Get-ClaudeVer
  if (-not $cur) { Die "x 装完了但找不到 claude 命令" }
  Ok "√ Claude Code $cur"
}
Write-Host ""

# ── 3. 读 key ────────────────────────────────────────────────────────
Say "粘贴你的 key(sk- 开头,和 Cursor / Codex 用的是同一把),然后回车:"
$apiKey = Read-Host "key"
# 剥掉所有空白 + 零宽字符(从飞书表格复制最容易带进来,带进去请求根本发不出去)
$apiKey = ($apiKey -replace '[\s\u00A0\u200B-\u200F\u2028-\u202F\uFEFF]', '')
if ([string]::IsNullOrWhiteSpace($apiKey)) { Die "x 没有输入 key,已取消。" }
if (-not $apiKey.StartsWith('sk-')) { Warn "! key 不是 sk- 开头,仍然继续。" }
Write-Host ""

# 先验 key 能用,再写配置
Say "→ 检查 key ..."
try {
  $r = Invoke-WebRequest -Uri "$baseUrl/v1/models" -Headers @{ 'x-api-key' = $apiKey } -TimeoutSec 30 -UseBasicParsing
  $code = [int]$r.StatusCode
} catch {
  $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
}
if ($code -eq 200) { Ok "√ key 可用" }
elseif ($code -eq 401 -or $code -eq 403) { Die "x key 无效(HTTP $code)。去 key 表里重新「全选复制」那一格再试。" }
else { Warn "! 暂时连不上网关(HTTP $code),配置照样写好,网络恢复后就能用。" }
Write-Host ""

# ── 4. 写配置:合并进 settings.json,不覆盖用户原有设置 ──────────────
New-Item -ItemType Directory -Path $claudeHome -Force | Out-Null
$settings = Join-Path $claudeHome 'settings.json'
if (Test-Path $settings) {
  $bk = "$settings.bak-carher-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
  Copy-Item $settings $bk -Force
  Warn "↳ 已备份原有配置到:$bk"
}
# 用 Node 合并 JSON(上面已保证有 Node;PS5 的 ConvertTo-Json 会把嵌套对象截断、转义中文,不可靠)
$env:CARHER_KEY = $apiKey; $env:CARHER_BASE = $baseUrl; $env:CARHER_SETTINGS = $settings
$merge = @'
const fs = require("fs");
const p = process.env.CARHER_SETTINGS;
let d = {};
if (fs.existsSync(p)) {
  const raw = fs.readFileSync(p, "utf8").replace(/^\uFEFF/, "");
  if (raw.trim()) {
    try { d = JSON.parse(raw); }
    catch (e) { console.error("x 原有 settings.json 不是合法 JSON,已备份,不动它:" + e.message); process.exit(3); }
  }
}
if (typeof d !== "object" || d === null || Array.isArray(d)) d = {};
d.env = Object.assign({}, d.env || {});
delete d.env.ANTHROPIC_AUTH_TOKEN;
delete d.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC;
d.env.ANTHROPIC_BASE_URL = process.env.CARHER_BASE;
d.env.ANTHROPIC_API_KEY = process.env.CARHER_KEY;
d.env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1";
fs.writeFileSync(p, JSON.stringify(d, null, 2) + "\n");
'@
$mjs = Join-Path $env:TEMP "carher-merge-$([guid]::NewGuid().ToString('N')).js"
[IO.File]::WriteAllText($mjs, $merge, (New-Object Text.UTF8Encoding $false))
try { & node $mjs; $rc = $LASTEXITCODE } finally {
  Remove-Item $mjs -Force -ErrorAction SilentlyContinue
  Remove-Item Env:CARHER_KEY, Env:CARHER_BASE, Env:CARHER_SETTINGS -ErrorAction SilentlyContinue
}
if ($rc -ne 0) { Die "x 写配置失败" }
Ok "√ 配置已写入 $settings"

# 模型列表缓存不会自己过期 ⇒ 删掉,下次启动重新拉
Remove-Item (Join-Path $claudeHome 'cache\gateway-models.json') -Force -ErrorAction SilentlyContinue

# ── 4b. Claude 桌面 App(Code 标签页)────────────────────────────────
# 桌面 App 不读 settings.json 切网关,走自己的「三方模式」;Windows 上目录是
# %LOCALAPPDATA%\Claude-3p\configLibrary(App 代码 fwA():win32 且有 LOCALAPPDATA ⇒ LOCALAPPDATA\Claude-3p)。
# 独立目录 ⇒ 用户原来登录的 Claude App 数据一个字节都不碰。详注见 install-mac.sh 同名段。
# ⚠️ Windows 这一段**没有真机实测**(mac 1.15962.0 实测过,路径按 App 代码推导)。
$appLib = Join-Path $env:LOCALAPPDATA 'Claude-3p\configLibrary'
$env:CARHER_KEY = $apiKey; $env:CARHER_BASE = $baseUrl; $env:CARHER_LIB = $appLib
$appJs = @'
const fs = require("fs"), path = require("path"), crypto = require("crypto");
const lib = process.env.CARHER_LIB;
fs.mkdirSync(lib, { recursive: true });
const metaP = path.join(lib, "_meta.json");
let meta = { appliedId: "", entries: [] };
try { const m = JSON.parse(fs.readFileSync(metaP, "utf8")); if (m && Array.isArray(m.entries)) meta = m; } catch {}
let ent = meta.entries.find(e => e && e.name === "CarHer" && /^[a-f0-9-]{36}$/.test(e.id || ""));
if (!ent) { ent = { id: crypto.randomUUID(), name: "CarHer" }; meta.entries.push(ent); }
meta.appliedId = ent.id;
const cfgP = path.join(lib, ent.id + ".json");
let cfg = {};
try { cfg = JSON.parse(fs.readFileSync(cfgP, "utf8")) || {}; } catch {}
Object.assign(cfg, {
  inferenceProvider: "gateway",
  inferenceGatewayBaseUrl: process.env.CARHER_BASE,
  inferenceGatewayApiKey: process.env.CARHER_KEY,
  inferenceGatewayAuthScheme: "x-api-key",
  inferenceCredentialKind: "static",
});
fs.writeFileSync(cfgP, JSON.stringify(cfg, null, 2) + "\n");
fs.writeFileSync(metaP, JSON.stringify(meta, null, 2) + "\n");
'@
$appMjs = Join-Path $env:TEMP "carher-app-$([guid]::NewGuid().ToString('N')).js"
[IO.File]::WriteAllText($appMjs, $appJs, (New-Object Text.UTF8Encoding $false))
try { & node $appMjs; $arc = $LASTEXITCODE } finally {
  Remove-Item $appMjs -Force -ErrorAction SilentlyContinue
  Remove-Item Env:CARHER_KEY, Env:CARHER_BASE, Env:CARHER_LIB -ErrorAction SilentlyContinue
}
if ($arc -eq 0) { Ok "√ Claude 桌面 App 已配好(Code 标签页走同一个网关)" }
else { Warn "! 桌面 App 配置没写成(不影响命令行 claude)" }

# ── 4c. 桌面 App 要的 Claude Code 二进制:预先放好 ────────────────────
# 真因与 mac 同(10-13 实测):App 每开会话先从 downloads.claude.ai 拉它钉死版本的 Claude Code,
# 国内超时 ⇒ "Host Claude Code binary not available" ⇒ 发消息不回复。
# Windows 上 App 只要 host 那一份(win32 无 bundle:<storageDir>\<ver>\claude.exe + .verified)。
# 从我们的镜像取官方原样文件,sha256 对 App 内嵌 manifest。
# ⚠️ Windows 这一段**没有真机实测**:目录/文件名按 App 代码推导(getBinaryPathForTarget 非 bundle 分支)。
# 找 App 的 app.asar:安装目录**不是从 App 代码读出来的**(没有 Windows 机器可验),所以不写死一处,
# 而是在几个常见位置里搜「同时有 resources\app.asar 且 exe 叫 claude.exe」的那个。找不到就跳过这一步
# (只影响桌面 App,命令行 claude 不受影响),并明说跳过了。
$appAsar = $null
$roots = @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA 'Programs')) | Where-Object { $_ -and (Test-Path $_) }
foreach ($r in $roots) {
  $hit = Get-ChildItem -Path $r -Filter app.asar -Recurse -Depth 5 -ErrorAction SilentlyContinue |
    Where-Object { $_.Directory.Name -eq 'resources' -and (Test-Path (Join-Path $_.Directory.Parent.FullName 'claude.exe')) } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($hit) { $appAsar = $hit.FullName; break }
}
if (-not $appAsar) { Warn "! 没找到 Claude 桌面 App 的安装目录,跳过桌面 App 组件这一步(没装 App 可以忽略)" }
if ($appAsar) {
  Say "→ 准备桌面 App 需要的 Claude Code 组件(约 55MB)..."
  $env:CARHER_ASAR = $appAsar; $env:CARHER_UD = (Join-Path $env:LOCALAPPDATA 'Claude-3p')
  $env:CARHER_CCDL = 'https://cc.auto-link.com.cn/ccdl/claude-code-releases'; $env:CARHER_ARCH = $arch
  $ccdJs = @'
const fs=require("fs"),path=require("path"),crypto=require("crypto"),zlib=require("zlib"),https=require("https");
const b=fs.readFileSync(process.env.CARHER_ASAR).toString("latin1");
const m=b.match(/JSON\.parse\('(\{"version":"(\d+\.\d+\.\d+)","manifest".*?)'\)/);
if(!m){console.error("读不到 App 内嵌的 Claude Code 版本");process.exit(2);}
const d=JSON.parse(m[1]), V=d.manifest.version, P=d.manifest.platforms;
const host="win32-"+(process.env.CARHER_ARCH==="arm64"?"arm64":"x64");
const e=P[host]; if(!e){console.error("manifest 里没有 "+host);process.exit(2);}
const dir=path.join(process.env.CARHER_UD,"claude-code",V), exe=path.join(dir,"claude.exe");
try{ if(fs.readFileSync(path.join(dir,".verified"),"utf8").trim()===e.checksum && fs.existsSync(exe)){console.log(V);process.exit(0);} }catch{}
if(typeof zlib.zstdDecompressSync!=="function"){console.error("Node 不支持 zstd(需要 Node>=22.15)");process.exit(3);}
https.get(`${process.env.CARHER_CCDL}/${V}/${host}/claude.exe.zst`,{headers:{"User-Agent":"carher-oneclick"}},r=>{
  if(r.statusCode!==200){console.error("HTTP "+r.statusCode);process.exit(3);}
  const bufs=[]; r.on("data",x=>bufs.push(x)); r.on("end",()=>{
    const z=Buffer.concat(bufs);
    if(crypto.createHash("sha256").update(z).digest("hex")!==e.checksum){console.error("sha256 不对,拒绝安装");process.exit(3);}
    fs.mkdirSync(dir,{recursive:true}); fs.writeFileSync(exe,zlib.zstdDecompressSync(z));
    fs.writeFileSync(path.join(dir,".verified"),e.checksum); console.log(V);
  });
}).on("error",x=>{console.error(x.message);process.exit(3);});
'@
  $ccdMjs = Join-Path $env:TEMP "carher-ccd-$([guid]::NewGuid().ToString('N')).js"
  [IO.File]::WriteAllText($ccdMjs, $ccdJs, (New-Object Text.UTF8Encoding $false))
  try { $ccdOut = (& node $ccdMjs 2>&1); $ccdRc = $LASTEXITCODE } finally {
    Remove-Item $ccdMjs -Force -ErrorAction SilentlyContinue
    Remove-Item Env:CARHER_ASAR, Env:CARHER_UD, Env:CARHER_CCDL, Env:CARHER_ARCH -ErrorAction SilentlyContinue
  }
  if ($ccdRc -eq 0) { Ok "√ 桌面 App 组件就绪(Claude Code $ccdOut)" }
  else { Warn "! 桌面 App 组件没准备好:$ccdOut。命令行 claude 不受影响。" }
}

# 会盖过 settings.json 的旧环境变量(mac 10-13 实跑踩到:ANTHROPIC_MODEL=anthropic.claude-opus-5 是
# claude-code-* 那族 key 的名字,cursor key 没有 ⇒ 每发 403,屏幕上只写 "Failed to authenticate")
$override = 'ANTHROPIC_AUTH_TOKEN','ANTHROPIC_BASE_URL','ANTHROPIC_API_KEY','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC',
            'ANTHROPIC_MODEL','ANTHROPIC_DEFAULT_OPUS_MODEL','ANTHROPIC_DEFAULT_SONNET_MODEL',
            'ANTHROPIC_DEFAULT_HAIKU_MODEL','ANTHROPIC_DEFAULT_FABLE_MODEL','ANTHROPIC_SMALL_FAST_MODEL'
Write-Host ""

# ── 5. 验证:真发一句 ────────────────────────────────────────────────
Say "→ 测试一下 ..."
$a = Get-Random -Minimum 1000 -Maximum 9999; $b = Get-Random -Minimum 1000 -Maximum 9999
foreach ($v in $override) { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
try { $out = (& cmd /c "claude --print ""What is $a+$b? Answer with just the number."" < NUL 2>NUL" | Select-Object -Last 1) } catch { $out = '' }
if ("$out" -match [regex]::Escape([string]($a + $b))) { Ok "√ 通了(问 $a+$b,答 $out)" }
else { Warn "! 测试没拿到预期回答(回:$out)。配置已写好,先新开一个终端试试 claude。" }

# 用户级环境变量里的旧值,新开窗口照样会盖过配置 ⇒ 删掉(先把原值存档,可还原)。
# 系统级(Machine)要管理员才能删 ⇒ 只提醒,不硬来。
$bkEnv = Join-Path $claudeHome "env-backup-carher-$(Get-Date -Format 'yyyyMMdd-HHmmss').txt"
$removed = @()
foreach ($v in $override) {
  $uv = [Environment]::GetEnvironmentVariable($v, 'User')
  if ($uv) {
    Add-Content -Path $bkEnv -Value "$v=$uv" -Encoding UTF8
    [Environment]::SetEnvironmentVariable($v, $null, 'User'); $removed += $v
  }
  if ([Environment]::GetEnvironmentVariable($v, 'Machine')) {
    Warn "! 系统级环境变量里有 $v,它会盖过刚写的配置。需要管理员在「编辑系统环境变量」里删掉。"
  }
}
if ($removed.Count -gt 0) {
  Warn "↳ 已删掉会盖过配置的用户环境变量:$($removed -join ', ')(原值存在 $bkEnv)"
  Warn "↳ 请**关掉现在这个窗口,新开一个**再用 claude"
}

Write-Host ""
Ok "==================================================="
Ok "   完成！"
Ok "==================================================="
Write-Host "  新开一个 PowerShell 或命令提示符窗口,进到你的项目文件夹,输入:  claude"
Write-Host "  换模型:在 Claude Code 里输入  /model"
Write-Host ""
