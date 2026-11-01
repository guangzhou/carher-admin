@echo off
setlocal
set "CARHER_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=[IO.File]::ReadAllText($env:CARHER_SELF,[Text.Encoding]::UTF8);$i=$c.IndexOf('#'+'#CARHER-PS1');& ([scriptblock]::Create($c.Substring($i)))"
echo.
pause
exit /b
##CARHER-PS1
# CarHer Claude Code 一键配置 (Windows)
# 由 build.sh 生成，别直接改；源在 claude-oneclick/src/
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
$baseUrl     = 'https://chat.auto-link.com.cn/model'
$legacyBaseUrl = 'https://cc.auto-link.com.cn/pro'   # 10-29 前的旧地址,共存期只在新地址不通时兜底
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
# 10-29 换了网关域名(旧地址还能用一段时间):新地址这会儿不通、旧地址通 ⇒ 先用旧地址,以后自动刷新会换过去
if ($code -ne 200 -and $code -ne 401 -and $code -ne 403) {
  try {
    $r2 = Invoke-WebRequest -Uri "$legacyBaseUrl/v1/models" -Headers @{ 'x-api-key' = $apiKey } -TimeoutSec 30 -UseBasicParsing
    if ([int]$r2.StatusCode -eq 200) { Warn "↳ 新网关地址暂时不通(HTTP $code),这次先用旧地址;以后会自动换到新地址。"; $baseUrl = $legacyBaseUrl; $code = 200 }
  } catch {}
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
# 网关模型清单(给 modelPicker 用);拿不到就跳过这一项
try {
  $gm = Invoke-RestMethod -Uri "$baseUrl/v1/models" -Headers @{ "x-api-key" = $apiKey; "anthropic-version" = "2023-06-01" } -TimeoutSec 30
  $env:CARHER_MODELS = (@($gm.data | ForEach-Object { $_.id }) -join "`n")
} catch { $env:CARHER_MODELS = "" }
$env:CARHER_KEY = $apiKey; $env:CARHER_BASE = $baseUrl; $env:CARHER_SETTINGS = $settings
# 「刷模型」用的是同一份逻辑,先落盘,合并时 require 它
$carherDir = Join-Path $claudeHome 'carher'
New-Item -ItemType Directory -Force -Path $carherDir | Out-Null
$refreshJs = @'
#!/usr/bin/env node
/* 「刷模型」—— 按这把 key 重写 Claude Code 的模型菜单(2026-10-19)。

   Claude Code 自己会做网关模型发现(CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1),但非 claude 的
   网关模型要靠 settings.json 里的 modelPicker(behavesAs)才不报「isn't recognized」。那张表以前只在
   安装时写一次 ⇒ 网关删掉的模型一直留在 /model 里(点了 403),新开通的进不来。
   本脚本:读 settings.json 里的 key/地址 → GET /v1/models → 重写 modelPicker(旧的删、新的加)
   → 删模型缓存 → 当前 model 已没权限就清掉(回到默认)→ haiku 默认指到 key 里真有的那个。安装器第 4 步和「刷模型」共用这一份。

   只用 Node 自带模块(Node ≥ 18)。用法:
     node carher_models_refresh.js                 # 拉网关并写入
     CARHER_MODELS="a\nb" node carher_models_refresh.js   # 不出网,用给定列表(安装器/测试用)
   拉不到列表就不动 settings.json(绝不拿空列表把菜单清空)。改前备份到 settings.json.bak-carher-refresh。 */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");

const CLAUDE_HOME = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
const SETTINGS = process.env.CARHER_SETTINGS || path.join(CLAUDE_HOME, "settings.json");
const BEHAVES_AS = "claude-sonnet-5-5";

/* 10-29 换对外域名:网关地址从旧的 /pro 换成 chat.auto-link.com.cn/model(同一个网关,路径改名)。
   旧地址共存一段时间 ⇒ 新旧两种都认作「我们的网关」。是旧的 ⇒ 先用这把 key 打新地址 /v1/models,
   拿到列表(200)才改写;不通就保持原样,下次开会话再试。只认我们那一个地址,用户自己指的别的网关不碰。 */
const NEW_BASE = process.env.CARHER_MIGRATE_TO || "https://chat.auto-link.com.cn/model";   // 环境变量只给测试用
const LEGACY_BASE = /^https:\/\/cc\.auto-link\.com\.cn\/pro\/?$/;
const isLegacyBase = (b) => LEGACY_BASE.test(String(b || "").trim());
const CLAUDE_FAMILY = ["fable", "opus", "sonnet", "haiku"];
const familyRank = (i) => { const k = CLAUDE_FAMILY.findIndex((f) => i.startsWith(`claude-${f}-`)); return k < 0 ? CLAUDE_FAMILY.length : k; };

/* /model 菜单 = 这把 key 能聊天的全部模型,每个只出现一次(10-25)。
   以前只写非 claude 的、再叠上 Claude Code 自带 + 网关发现 ⇒ 同一个模型出现两次、出图模型和 key 没有的 Fable 也在菜单里。
   现在 replaceBuiltInOptions:true ⇒ 菜单只剩「Default + 这里的行」(官方 settings-reference#modelpicker)。
   claude-*:不写 label,Claude Code 自己显示成 Opus 5.5 这类名字;同家族按版本新→旧。
   非 claude:behavesAs 指一个它认识的模型 ⇒ 不再告警「isn't recognized」。出图/视频/音乐、审查专用不进。 */
function pickerRows(ids) {
  const seen = new Set();
  const chat = ids.filter((i) => typeof i === "string" && i && !seen.has(i) && seen.add(i))
    .filter((i) => !/image|imagine|video|music/.test(i) && i !== "codex-auto-review");
  const claude = chat.filter((i) => /^claude-/.test(i))
    .sort((a, b) => familyRank(a) - familyRank(b) || b.localeCompare(a, "en", { numeric: true }));
  return [
    ...claude.map((i) => ({ model: i })),
    ...chat.filter((i) => !/^claude-/.test(i)).map((i) => ({ model: i, label: i, behavesAs: BEHAVES_AS })),
  ];
}

/* 把网关列表并进 settings 对象。返回 {d, added, removed, modelReset}。纯函数,测试直接调。 */
function applyModels(d, ids) {
  const before = ((d.modelPicker && d.modelPicker.options) || []).map((o) => o && o.model).filter(Boolean);
  const rows = pickerRows(ids);
  const after = rows.map((r) => r.model);
  if (rows.length) d.modelPicker = { ...(d.modelPicker || {}), options: rows, replaceBuiltInOptions: true };
  else delete d.modelPicker;
  // 当前钉着的 model 已不在网关里 ⇒ 每次新开窗口都报错。清掉 = 回到 Claude Code 默认模型。
  // 只认「网关名」:上次菜单里的,或 claude-* 全名(`[1m]` 后缀先剥)。opus / sonnet 这类别名不动。
  let modelReset = null;
  if (typeof d.model === "string" && d.model) {
    const bare = d.model.replace(/\[[^\]]*\]$/, "");
    if ((before.includes(bare) || /^claude-/.test(bare)) && !ids.includes(bare)) { modelReset = d.model; delete d.model; }
  }
  /* 10-19 sunqiang/linsen:Claude Code 2.1.30x 的 haiku 默认是 claude-haiku-5-5(后台起标题、子任务用),
     网关没有这个模型 ⇒ 后台请求一直 403。⇒ 指到这把 key 里真有的 haiku(按版本新→旧挑);
     一个都没有就删掉这项(让 Claude Code 自己决定)。用户自己设的、且仍可用的不动。
     10-25 网关上了 claude-haiku-5-5:之前写进去的老 haiku(4-5)仍可用但不是最新 ⇒ 也升到最新;
     用户指到非 haiku 的(比如 sonnet)仍不动。 */
  d.env = Object.assign({}, d.env || {});
  // 10-25:每次开会话的自动刷新顺带补上这项 ⇒ 装过带 hook 版本的同事以后不用重跑安装也能拿到(用户自己写过不动)
  if (!d.env.ENABLE_TOOL_SEARCH) d.env.ENABLE_TOOL_SEARCH = "true";
  const haikus = ids.filter((i) => /^claude-haiku-\d/.test(i))
    .sort((a, b) => b.localeCompare(a, "en", { numeric: true }));
  const curH = d.env.ANTHROPIC_DEFAULT_HAIKU_MODEL;
  let haikuSet = null;
  const staleHaiku = curH && /^claude-haiku-\d/.test(curH) && haikus.length && curH !== haikus[0];
  if (!(curH && ids.includes(curH)) || staleHaiku) {
    if (haikus.length) { d.env.ANTHROPIC_DEFAULT_HAIKU_MODEL = haikus[0]; haikuSet = haikus[0]; }
    else delete d.env.ANTHROPIC_DEFAULT_HAIKU_MODEL;
  }
  return {
    d,
    haikuSet,
    added: after.filter((m) => !before.includes(m)),
    removed: before.filter((m) => !after.includes(m)),
    modelReset,
  };
}

/* 桌面 App(Code 标签页)的模型菜单(10-27 用户:「刷模型在 App 里也搞定啊」)。
   App 不读 settings.json 的 modelPicker,它自己在**启动时**去网关拉一次 /v1/models,结果只存在内存里。
   App 配置项 `inferenceModels` 一旦写了,App 就不再自己拉、菜单 = 这份列表(本机实测日志:
   「Model discovery: skipped (inferenceModels obviates discovery); picker = 2 (inferenceModels)」)。
   ⇒ 刷模型把 key 里的 claude-* 写进去(App 只认 Claude 模型,非 claude 写进去会被它拒),菜单就和 key 完全一致;
   App 要**完全退出再打开**才读新配置。只改我们自己那条(_meta.json 里叫 CarHer 的),别的配置不碰。 */
function appConfigEntries() {
  const roots = [];
  if (process.platform === "darwin") roots.push(path.join(os.homedir(), "Library", "Application Support", "Claude-3p"));
  else if (process.platform === "win32") {
    if (process.env.LOCALAPPDATA) roots.push(path.join(process.env.LOCALAPPDATA, "Claude-3p"));
    if (process.env.APPDATA) roots.push(path.join(process.env.APPDATA, "Claude-3p"));
    // MSIX 版数据会被重定向到 Packages\Claude_*\LocalCache\Local\Claude-3p
    try {
      const pk = path.join(process.env.LOCALAPPDATA || "", "Packages");
      for (const d of fs.readdirSync(pk)) if (/^Claude_/i.test(d)) roots.push(path.join(pk, d, "LocalCache", "Local", "Claude-3p"));
    } catch (e) { /* 没有就算了 */ }
  } else roots.push(path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "Claude-3p"));
  const out = [];
  for (const r of roots) {
    const lib = path.join(r, "configLibrary");
    let meta; try { meta = JSON.parse(fs.readFileSync(path.join(lib, "_meta.json"), "utf8")); } catch (e) { continue; }
    for (const e of (meta && meta.entries) || []) {
      if (e && e.name === "CarHer" && /^[a-f0-9-]{36}$/.test(e.id || "")) out.push(path.join(lib, e.id + ".json"));
    }
  }
  return out;
}
/* newBase:这次已用这把 key 在新地址上拉到了列表 ⇒ App 我们那条还写着旧地址就一起换过去(10-29 换域名)。 */
function syncAppModels(ids, key, newBase) {
  const want = ids.filter((i) => /^claude-/.test(i) && !/image|imagine|video|music/.test(i))
    .sort((a, b) => familyRank(a) - familyRank(b) || b.localeCompare(a, "en", { numeric: true }));
  const res = [];
  for (const p of appConfigEntries()) {
    let c; try { c = JSON.parse(fs.readFileSync(p, "utf8")); } catch (e) { continue; }
    if (key && c.inferenceGatewayApiKey && c.inferenceGatewayApiKey !== key) continue;   // 不是这把 key 配的那条,不动
    const baseMoved = !!(newBase && isLegacyBase(c.inferenceGatewayBaseUrl));
    if (baseMoved) c.inferenceGatewayBaseUrl = newBase;
    const before = Array.isArray(c.inferenceModels) ? c.inferenceModels.map((m) => (typeof m === "string" ? m : m && m.name)).filter(Boolean) : null;
    let menuChanged = true;
    if (!want.length) {
      // key 里一个 Claude 模型都没有 ⇒ 别写空列表(App 会没有可选的),交回 App 自己拉
      if (before) { delete c.inferenceModels; } else if (baseMoved) menuChanged = false; else continue;
    } else if (before && before.join("\n") === want.join("\n")) {
      if (!baseMoved) { res.push({ p, changed: false, models: want }); continue; }
      menuChanged = false;
    } else c.inferenceModels = want;
    const tmp = p + ".carher-tmp-" + process.pid;
    fs.writeFileSync(tmp, JSON.stringify(c, null, 2) + "\n", { mode: 0o600 });
    fs.renameSync(tmp, p);
    res.push({ p, changed: true, menuChanged, baseMoved, models: want, added: want.filter((m) => !(before || []).includes(m)), removed: (before || []).filter((m) => !want.includes(m)) });
  }
  return res;
}

/* settings.json 里是旧网关地址 ⇒ 用这把 key 打新地址;拿到列表才改 d.env.ANTHROPIC_BASE_URL。
   fetch 可注入(测试)。返回 {ids, base, moved:bool, why?}:moved=false 时 ids 为 null,调用方照旧地址拉。 */
async function migrateIfReachable(d, key, fetch = fetchIdsLive) {
  const cur = d && d.env && d.env.ANTHROPIC_BASE_URL;
  if (!isLegacyBase(cur)) return { ids: null, base: cur, moved: false };
  try {
    const ids = await fetch(NEW_BASE, key);
    if (!ids.length) return { ids: null, base: cur, moved: false, why: "新地址没返回任何模型" };
    d.env.ANTHROPIC_BASE_URL = NEW_BASE;
    return { ids, base: NEW_BASE, moved: true };
  } catch (e) { return { ids: null, base: cur, moved: false, why: e.message }; }
}

/* 把这把 key 记成「已同意」(10-27 王德升:命令行右下角一直「Not logged in · Run /login」)。
   Claude Code 第一次看到 settings 里的 ANTHROPIC_API_KEY 会问「Do you want to use this API key?」,默认选中 **No**;
   回车一下就记进全局配置 customApiKeyResponses.rejected,之后**每次启动都不用这把 key** ⇒ 显示未登录(请求照样走,
   因为 App / -p 另有来源,但交互界面认为没登录)。本机隔离复现:rejected ⇒ Not logged in;approved ⇒ 正常。
   比较的是 key 末尾 20 个字符(Claude Code 自己的 Vpe())。用户换 key 后旧条目留着无害。
   配置文件:CLAUDE_CONFIG_DIR/.claude.json,没设时是 ~/.claude.json(和 settings.json 不在一个目录)。 */
function approveApiKey(key, configDir) {
  const tail = String(key || "").trim().slice(-20);
  if (!tail) return false;
  const p = configDir ? path.join(configDir, ".claude.json") : path.join(os.homedir(), ".claude.json");
  let g = {};
  try { g = JSON.parse(fs.readFileSync(p, "utf8").replace(/^\uFEFF/, "")) || {}; } catch (e) { if (fs.existsSync(p)) return false; }
  const r = (g.customApiKeyResponses && typeof g.customApiKeyResponses === "object") ? g.customApiKeyResponses : {};
  const approved = Array.isArray(r.approved) ? r.approved.filter((x) => typeof x === "string") : [];
  const rejected = Array.isArray(r.rejected) ? r.rejected.filter((x) => typeof x === "string") : [];
  if (approved.includes(tail) && !rejected.includes(tail)) return false;
  g.customApiKeyResponses = { ...r, approved: approved.includes(tail) ? approved : [...approved, tail], rejected: rejected.filter((x) => x !== tail) };
  const tmp = p + ".carher-tmp-" + process.pid;
  fs.writeFileSync(tmp, JSON.stringify(g, null, 2) + "\n", { mode: 0o600 });
  fs.renameSync(tmp, p);
  return true;
}

/* 在 settings.hooks.SessionStart 里挂(或更新)我们那一条自动刷新 hook。重跑幂等:先摘掉旧的
   (认 command 里的 carher_models_refresh.js),用户自己的 hook 一条不动。
   exec 形式(args)不经 shell ⇒ 路径带空格/中文、Windows 没 Git Bash 都不怕(hook args 需 2.1.139+)。 */
function hookEntry(d, nodeAbs, scriptAbs) {
  const ours = (h) => h && JSON.stringify(h).includes("carher_models_refresh.js");
  d.hooks = d.hooks && typeof d.hooks === "object" && !Array.isArray(d.hooks) ? d.hooks : {};
  const list = (Array.isArray(d.hooks.SessionStart) ? d.hooks.SessionStart : [])
    .map((g) => (g && Array.isArray(g.hooks) ? { ...g, hooks: g.hooks.filter((h) => !ours(h)) } : g))
    .filter((g) => !(g && Array.isArray(g.hooks) && g.hooks.length === 0));
  list.push({ matcher: "startup", hooks: [{ type: "command", command: nodeAbs, args: [scriptAbs, "--quiet"], async: true, timeout: 60 }] });
  d.hooks.SessionStart = list;
  return d;
}

async function fetchIds(base, key) {
  if (process.env.CARHER_MODELS != null) return process.env.CARHER_MODELS.split("\n").map((s) => s.trim()).filter(Boolean);
  return fetchIdsLive(base, key);
}
/* 一定真打网关(不吃 CARHER_MODELS):换地址前「新地址真的通」只能靠真请求判 */
async function fetchIdsLive(base, key) {
  const r = await fetch(String(base).replace(/\/+$/, "") + "/v1/models", {
    headers: { "x-api-key": key, "anthropic-version": "2023-06-01" }, signal: AbortSignal.timeout(30000),
  });
  if (r.status === 401 || r.status === 403) throw new Error(`key 无效或已停用(HTTP ${r.status})`);
  if (!r.ok) throw new Error(`网关返回 HTTP ${r.status}`);
  return ((await r.json()).data || []).map((m) => m && m.id).filter(Boolean);
}

/* 每次开 Claude Code 自动刷一次(10-25):安装器在 settings.json 里挂一个 SessionStart 异步 hook 跑
   `node carher_models_refresh.js --quiet`。后台跑、不挡输入;--quiet 不往屏幕/对话里打任何东西,
   拉不到就原样退出 0(断网不报错),结果只记到 ~/.claude/carher/refresh.log。
   ⇒ 管理员给 key 开/关模型后,用户下次开 Claude Code 菜单就对了,不用记得说「刷模型」。 */
const QUIET = process.argv.includes("--quiet");
const LOG = path.join(CLAUDE_HOME, "carher", "refresh.log");
function logLine(s) {
  if (!QUIET) return;
  try {
    fs.mkdirSync(path.dirname(LOG), { recursive: true });
    let old = ""; try { old = fs.readFileSync(LOG, "utf8"); } catch (e) { /* 第一次 */ }
    const lines = (old + new Date().toISOString() + " " + s + "\n").split("\n").slice(-200);
    fs.writeFileSync(LOG, lines.join("\n"), { mode: 0o600 });
  } catch (e) { /* 记不了日志也不影响 */ }
}
const say = (s) => { if (!QUIET) console.log(s); };

async function main() {
  let raw;
  try { raw = fs.readFileSync(SETTINGS, "utf8").replace(/^\uFEFF/, ""); }
  catch (e) { throw new Error(`找不到 ${SETTINGS},先跑一键配置脚本`); }
  const d = raw.trim() ? JSON.parse(raw) : {};
  const env = d.env || {};
  const key = process.env.CARHER_KEY || env.ANTHROPIC_API_KEY, base = process.env.CARHER_BASE || env.ANTHROPIC_BASE_URL;
  if (!key || !base) throw new Error("settings.json 里没有 ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL,先跑一键配置脚本");
  let ids;
  // 10-29 换域名:旧地址 ⇒ 新地址拿得到列表才改写(只在没被 CARHER_BASE 指定时;安装器/测试会指定)
  const mig = process.env.CARHER_BASE ? { ids: null, moved: false } : await migrateIfReachable(d, key);
  if (mig.moved) { ids = mig.ids; logLine(`moved base ${base} -> ${mig.base}`); }
  else if (mig.why) logLine(`keep old base (新地址不通:${mig.why})`);
  try { if (!ids) ids = await fetchIds(base, key); }
  catch (e) { throw new Error(`没拉到模型列表(${e.message}),菜单没动`); }
  if (!ids.length) throw new Error("网关没返回任何模型,菜单没动");
  const r = applyModels(d, ids);
  const out = JSON.stringify(r.d, null, 2) + "\n";
  if (out !== raw) {
    if (raw.trim()) fs.writeFileSync(SETTINGS + ".bak-carher-refresh", raw, { mode: 0o600 });
    // 先写临时文件再改名:Claude Code 在监视这个文件,写到一半被它读到会当成坏 JSON
    const tmp = SETTINGS + ".carher-tmp-" + process.pid;
    fs.writeFileSync(tmp, out, { mode: 0o600 });
    fs.renameSync(tmp, SETTINGS);
    try { fs.chmodSync(SETTINGS, 0o600); } catch (e) { /* Windows */ }
  }
  try { fs.unlinkSync(path.join(CLAUDE_HOME, "cache", "gateway-models.json")); } catch (e) { /* 没有就算了 */ }
  let app = [];
  // App 那条只在「这一趟真从新地址拉到了列表」时才换地址(CARHER_MODELS 注入的不算真通)
  const newVerified = mig.moved || (base === NEW_BASE && process.env.CARHER_MODELS == null);
  try { app = syncAppModels(ids, key, newVerified ? NEW_BASE : null); } catch (e) { logLine("app sync failed " + e.message); }
  for (const a of app) if (a.changed) logLine("app menu " + a.models.join(",") + (a.baseMoved ? " base->" + NEW_BASE : ""));
  let keyFixed = false;
  try { keyFixed = approveApiKey(key, process.env.CLAUDE_CONFIG_DIR); } catch (e) { /* 写不了就算了,不影响菜单 */ }
  if (keyFixed) logLine("approved api key (was rejected/unanswered)");
  const n = r.d.modelPicker ? r.d.modelPicker.options.length : 0;
  logLine(`ok menu=${n} added=${r.added.join(",") || "-"} removed=${r.removed.join(",") || "-"}` +
    (r.modelReset ? ` reset=${r.modelReset}` : "") + (r.haikuSet ? ` haiku=${r.haikuSet}` : ""));
  say(`/model 菜单现在一共 ${n} 个模型,就是你的 key 能用来聊天的全部(画图/视频类不放进菜单)`);
  if (r.added.length) say(`新增 ${r.added.length}:${r.added.join(", ")}`);
  if (r.removed.length) say(`删除 ${r.removed.length}:${r.removed.join(", ")}`);
  if (!r.added.length && !r.removed.length) say("菜单和上次一样,没有变化");
  if (r.modelReset) say(`你之前选的 ${r.modelReset} 已经没有权限,已改回默认模型`);
  if (r.haikuSet) say(`后台小任务用的模型设为 ${r.haikuSet}(你的 key 里最新的 haiku)`);
  if (mig.moved) say(`网关地址已换成新地址 ${NEW_BASE}(同一个网关,换了域名)`);
  for (const a of app) {
    if (!a.changed) { say(`桌面 App 菜单和上次一样(${a.models.length} 个 Claude 模型)`); continue; }
    if (a.baseMoved) say(`桌面 App 的网关地址也换成了 ${NEW_BASE}`);
    if (a.menuChanged === false) { say("  ⚠️ 桌面 App 要**完全退出再打开**才会用新地址"); continue; }
    say(`桌面 App 菜单已更新为 ${a.models.length} 个 Claude 模型:${a.models.join(", ")}`);
    if (a.added && a.added.length) say(`  App 新增:${a.added.join(", ")}`);
    if (a.removed && a.removed.length) say(`  App 删除:${a.removed.join(", ")}`);
    say("  ⚠️ 桌面 App 要**完全退出再打开**才会显示新菜单(Mac:Cmd+Q;Windows:右下角托盘图标右键退出)");
  }
  if (keyFixed) say("已修好「Not logged in」:这把 key 之前被拒绝过,现在记成已同意");
  say("新开一个 Claude Code 会话(或桌面 App 新开对话)后 /model 里就是新列表");
}

module.exports = { pickerRows, applyModels, hookEntry, approveApiKey, syncAppModels, appConfigEntries, migrateIfReachable, isLegacyBase, NEW_BASE };

if (require.main === module) {
  main().catch((e) => {
    if (QUIET) { logLine("skip " + e.message); process.exit(0); }   // 后台自动刷:失败不打扰用户
    console.error("✗ " + e.message); process.exit(1);
  });
}
'@
$refreshPath = Join-Path $carherDir 'carher_models_refresh.js'
[IO.File]::WriteAllText($refreshPath, $refreshJs, (New-Object Text.UTF8Encoding $false))
$env:CARHER_REFRESH = $refreshPath
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
// 10-25:MCP 工具按需加载(tool search)。Claude Code 2.1.304 起走网关才默认开,装机下限 2.1.257 ⇒ 老版本同事
// 每句话都把所有 MCP 工具说明整份发出去(本机 rea 210 个工具约 1MB,叠上聊天记录超 100 万上限卡 25 分钟)。
// 用户自己写过(包括 false)就不动。
if (!d.env.ENABLE_TOOL_SEARCH) d.env.ENABLE_TOOL_SEARCH = "true";
// 旧版 Claude Code 存下的 claude-router-<hex> 模型名 ⇒ 还原真名(与 mac 版同)
const real = (n) => { const m = /^claude-router-([0-9a-f]+)$/.exec(n || ""); return m ? Buffer.from(m[1], "hex").toString("utf8") : n; };
if (typeof d.model === "string") d.model = real(d.model);
// 非 claude 网关模型加进 modelPicker(behavesAs),新开窗口不再报「isn't recognized」;
// 旧的删、新的加、选中的已没权限就回默认 —— 与「刷模型」同一份逻辑(carher_models_refresh.js)
const ids = (process.env.CARHER_MODELS || "").split("\n").filter(Boolean);
if (ids.length) require(process.env.CARHER_REFRESH).applyModels(d, ids);
// 10-27:不预先同意 ⇒ 首次启动问「用不用这把 key」默认 No,回车就被记成拒绝 ⇒ 永远「Not logged in」
try { require(process.env.CARHER_REFRESH).approveApiKey(process.env.CARHER_KEY, process.env.CLAUDE_CONFIG_DIR); } catch (e) {}
fs.writeFileSync(p, JSON.stringify(d, null, 2) + "\n");
'@
$mjs = Join-Path $env:TEMP "carher-merge-$([guid]::NewGuid().ToString('N')).js"
[IO.File]::WriteAllText($mjs, $merge, (New-Object Text.UTF8Encoding $false))
try { & node $mjs; $rc = $LASTEXITCODE } finally {
  Remove-Item $mjs -Force -ErrorAction SilentlyContinue
  Remove-Item Env:CARHER_KEY, Env:CARHER_BASE, Env:CARHER_SETTINGS, Env:CARHER_MODELS, Env:CARHER_REFRESH -ErrorAction SilentlyContinue
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
// 10-25 同事反馈:App 的 Code/Cowork 里 npm install 失败,设置页显示「严格沙箱 · 组织开启」,
//    bypass 也没用,终端里正常。真因:这项不写 ⇒ App 按 failClosed 当成 [] ⇒ 给会话下发
//    sandbox{enabled, allowUnsandboxedCommands:false, network.allowedDomains:[], allowManagedDomainsOnly}
//    ⇒ 命令一个外网都连不上(npm E403 "user denied")。官方文档:含 "*" 且没设 allowedWorkspaceFolders
//    时两种沙箱都不开(和终端里的 claude 一样)。只列 npm 源不够:沙箱还挡 ~/.npm 缓存写入(实测 EPERM)。
//    用户/管理员自己在这条里写过非空列表 ⇒ 尊重,不覆盖。
if (!Array.isArray(cfg.coworkEgressAllowedHosts) || !cfg.coworkEgressAllowedHosts.length) cfg.coworkEgressAllowedHosts = ["*"];
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
# 🔴 10-15 同事实机:App 装了却报「没找到安装目录」。官方 Windows 包是 **MSIX**,装在
#    C:\Program Files\WindowsApps\Claude_<版本>_x64__<发布者>\app\ —— 那个目录普通用户**列不出来**,
#    递归搜永远搜不到。⇒ 先问系统要 MSIX 的安装位置(Get-AppxPackage,不要管理员),
#    文件本身是可读的(app\resources\app.asar),拿不到再退回老的目录搜索(兼容 .exe 安装版)。
try {
  $pkg = Get-AppxPackage -Name 'Claude' -ErrorAction SilentlyContinue | Sort-Object Version -Descending | Select-Object -First 1
  if ($pkg -and $pkg.InstallLocation) {
    $cand = Join-Path $pkg.InstallLocation 'app\resources\app.asar'
    if (Test-Path $cand) { $appAsar = $cand }
  }
} catch {}
$roots = @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA 'Programs')) | Where-Object { $_ -and (Test-Path $_) }
if (-not $appAsar) { foreach ($r in $roots) {
  $hit = Get-ChildItem -Path $r -Filter app.asar -Recurse -Depth 5 -ErrorAction SilentlyContinue |
    Where-Object { $_.Directory.Name -eq 'resources' -and (Test-Path (Join-Path $_.Directory.Parent.FullName 'claude.exe')) } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($hit) { $appAsar = $hit.FullName; break }
} }
if (-not $appAsar) { Warn "! 没找到 Claude 桌面 App 的安装目录,跳过桌面 App 组件这一步(没装 App 可以忽略;装了 App 的话先打开一次 App 再重跑本脚本)" }
if ($appAsar) {
  Say "→ 准备桌面 App 需要的 Claude Code 组件(约 55MB)..."
  $env:CARHER_ASAR = $appAsar; $env:CARHER_UD = (Join-Path $env:LOCALAPPDATA 'Claude-3p')
  $env:CARHER_CCDL = 'https://chat.auto-link.com.cn/ccdl/claude-code-releases'; $env:CARHER_ARCH = $arch
  $ccdJs = @'
const fs=require("fs"),path=require("path"),crypto=require("crypto"),zlib=require("zlib"),https=require("https");
const b=fs.readFileSync(process.env.CARHER_ASAR).toString("latin1");
const m=b.match(/JSON\.parse\('(\{"version":"(\d+\.\d+\.\d+)","manifest".*?)'\)/);
if(!m){console.error("读不到 App 内嵌的 Claude Code 版本");process.exit(2);}
const d=JSON.parse(m[1]), V=d.manifest.version, P=d.manifest.platforms;
const host="win32-"+(process.env.CARHER_ARCH==="arm64"?"arm64":"x64");
const e=P[host]; if(!e){console.error("manifest 里没有 "+host);process.exit(2);}
// 10-28 App 4.29 起本机组件目录 = claude-code\<ver>\<sha 前 12 位>\(老 App 没这段代码 ⇒ 仍是 claude-code\<ver>\)
const newLayout=b.includes("it cannot name an install folder");
const dir=path.join(process.env.CARHER_UD,"claude-code",V,...(newLayout?[e.checksum.slice(0,12).toLowerCase()]:[])), exe=path.join(dir,"claude.exe");
try{ if(fs.readFileSync(path.join(dir,".verified"),"utf8").trim()===e.checksum && fs.existsSync(exe)){console.log(V);process.exit(0);} }catch{}
if(typeof zlib.zstdDecompressSync!=="function"){console.error("Node 不支持 zstd(需要 Node>=22.15)");process.exit(3);}
const get1=(u)=>new Promise((res,rej)=>{https.get(u,{headers:{"User-Agent":"carher-oneclick"}},r=>{
  if(r.statusCode!==200){rej(new Error("HTTP "+r.statusCode));r.resume();return;}
  const bufs=[]; r.on("data",x=>bufs.push(x)); r.on("error",rej); r.on("aborted",()=>rej(new Error("下载中断")));
  r.on("end",()=>res(Buffer.concat(bufs)));}).on("error",rej);});
// 10-28:大文件下载偶发 socket hang up ⇒ 断了自动重试 3 次再报
(async()=>{
  let z; for(let i=1;;i++){try{z=await get1(`${process.env.CARHER_CCDL}/${V}/${host}/claude.exe.zst`);break;}
    catch(x){if(i>=3||/HTTP 4/.test(x.message))throw x;await new Promise(r=>setTimeout(r,2000*i));}}
  if(crypto.createHash("sha256").update(z).digest("hex")!==e.checksum){console.error("sha256 不对,拒绝安装");process.exit(3);}
  fs.mkdirSync(dir,{recursive:true}); fs.writeFileSync(exe,zlib.zstdDecompressSync(z));
  fs.writeFileSync(path.join(dir,".verified"),e.checksum); console.log(V);
})().catch(x=>{console.error(x.message);process.exit(3);});
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

# ── 4d. 画图工具(10-16):命令行和桌面 App 的 Code 标签页都会读 ~/.claude/skills ──
# 一个零依赖的 Node 小工具(调 muse-image)+ 一个 skill;skill 目录名固定 carher-image,不碰用户自己的 skill。
$imgDir = Join-Path $claudeHome 'carher'
$skillDir = Join-Path $claudeHome 'skills\carher-image'
New-Item -ItemType Directory -Force -Path $imgDir, $skillDir | Out-Null
$imgJs = @'
#!/usr/bin/env node
/* CarHer 画图工具(Node 版)—— 由三个一键配置(Codex / Claude Code / Cursor)自动安装,用户无需关心。

   为什么有 Node 版:Python 版(carher_image.py)要求电脑有 Python,很多 Windows 同事没有 ⇒ 画图整段不装。
   三个一键脚本本来就会装 Node(codex / claude 命令行要),所以统一用这一份;逻辑与 .py 一致。

   用法:  node carher_image.js "画面描述" [输出路径或目录]
   key / 网关地址按顺序找(先找到的为准),不接受命令行传 key:
     1. CARHER_IMAGE_KEY / CARHER_IMAGE_BASE 环境变量(给测试用)
     2. ~/.codex/auth.json + ~/.codex/config.toml        (Codex 一键)
     3. ~/.claude/settings.json 的 env.ANTHROPIC_API_KEY  (Claude Code 一键;网关地址加 /v1)
     4. ~/.carher/image.json {"key","base"}              (Cursor 一键写的)
   2026-10-13 起统一用 muse-image。它返回相对路径 /v1/media/<32hex>.webp(文件在 muse2api 上),
   198 nginx 开了只读出口 https://<网关域名>/muse-media/<32hex>.<ext> ⇒ 改写过去下载。
   输出文件后缀以实际图片格式为准(webp 就存 .webp)。 */
"use strict";
const fs = require("fs"), os = require("os"), path = require("path");

const MODEL = process.env.CARHER_IMAGE_MODEL || "muse-image";
const SIZE = "1024x1024";
const HOME = os.homedir();

function readJson(p) { try { return JSON.parse(fs.readFileSync(p, "utf8").replace(/^﻿/, "")); } catch (e) { return null; } }
function die(msg) { console.error(msg); process.exit(1); }

function credentials() {
  if (process.env.CARHER_IMAGE_KEY && process.env.CARHER_IMAGE_BASE)
    return [process.env.CARHER_IMAGE_KEY.trim(), process.env.CARHER_IMAGE_BASE.trim().replace(/\/+$/, "")];
  // Codex
  const ch = process.env.CODEX_HOME || path.join(HOME, ".codex");
  const auth = readJson(path.join(ch, "auth.json"));
  if (auth && auth.OPENAI_API_KEY) {
    let base = "";
    try { const m = /^\s*base_url\s*=\s*"([^"]+)"/m.exec(fs.readFileSync(path.join(ch, "config.toml"), "utf8")); if (m) base = m[1]; } catch (e) {}
    if (base) return [String(auth.OPENAI_API_KEY).trim(), base.replace(/\/+$/, "")];
  }
  // Claude Code(网关地址不带 /v1)
  const s = readJson(path.join(process.env.CLAUDE_CONFIG_DIR || path.join(HOME, ".claude"), "settings.json"));
  const env = (s && s.env) || {};
  if (env.ANTHROPIC_API_KEY && env.ANTHROPIC_BASE_URL)
    return [String(env.ANTHROPIC_API_KEY).trim(), String(env.ANTHROPIC_BASE_URL).replace(/\/+$/, "") + "/v1"];
  // Cursor
  const c = readJson(path.join(HOME, ".carher", "image.json"));
  if (c && c.key && c.base) return [String(c.key).trim(), String(c.base).replace(/\/+$/, "")];
  die("找不到 key。请重新运行一次一键配置脚本。");
}

function mediaUrl(url, base) {
  if (/^https?:\/\//.test(url)) return url;
  const m = /^\/v1\/media\/([0-9a-f]{32}\.(?:webp|png|jpe?g))$/.exec(url);
  if (!m) die("生成失败:看不懂返回的图片地址 " + url.slice(0, 120));
  return /^(https?:\/\/[^/]+)/.exec(base)[1] + "/muse-media/" + m[1];
}

async function download(url) {
  let last;
  for (let i = 0; i < 3; i++) {   // 文件刚生成时偶有一两秒还没落盘
    try {
      const r = await fetch(url, { signal: AbortSignal.timeout(120000) });
      if (r.ok) return Buffer.from(await r.arrayBuffer());
      last = new Error("HTTP " + r.status);
    } catch (e) { last = e; }
    await new Promise((res) => setTimeout(res, 2000));
  }
  die("图片已生成但下载失败:" + (last && last.message));
}

function fixExt(p, d) {
  let ext = "";
  if (d.slice(0, 4).toString("latin1") === "RIFF" && d.slice(8, 12).toString("latin1") === "WEBP") ext = ".webp";
  else if (d.slice(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) ext = ".png";
  else if (d[0] === 0xff && d[1] === 0xd8 && d[2] === 0xff) ext = ".jpg";
  if (!ext) return p;
  const cur = path.extname(p).toLowerCase();
  if (cur === ext || (ext === ".jpg" && cur === ".jpeg")) return p;
  return p.slice(0, p.length - path.extname(p).length) + ext;
}

async function generate(prompt, out) {
  const [key, base] = credentials();
  const t0 = Date.now();
  console.log("正在生成,约需 30 秒…");
  let r;
  try {
    r = await fetch(base + "/images/generations", {
      method: "POST", signal: AbortSignal.timeout(300000),
      headers: { Authorization: "Bearer " + key, "Content-Type": "application/json" },
      body: JSON.stringify({ model: MODEL, prompt, size: SIZE, n: 1 }),
    });
  } catch (e) { die("生成失败:" + e.message); }
  if (!r.ok) {
    const detail = (await r.text()).slice(0, 300);
    if (r.status === 403) die("你的 key 还没有开通画图模型 " + MODEL + ",请联系运维开通后重试。");
    if (r.status === 401) die("key 已失效,请找运维重新颁发。");
    die("生成失败(HTTP " + r.status + "):" + detail);
  }
  const item = ((await r.json()).data || [{}])[0] || {};
  let data;
  if (item.b64_json) data = Buffer.from(item.b64_json, "base64");
  else if (item.url) data = await download(mediaUrl(item.url, base));
  else die("生成失败:返回里没有图片数据。");
  out = fixExt(out, data);
  fs.writeFileSync(out, data);
  console.log("已生成:" + out + "(" + (data.length / 1048576).toFixed(1) + " MB,用时 " + Math.round((Date.now() - t0) / 1000) + " 秒)");
}

const args = process.argv.slice(2).filter((a) => a.trim());
if (!args.length) die('用法:node ' + path.basename(process.argv[1]) + ' "画面描述" [输出路径.png]');
const pad = (n) => String(n).padStart(2, "0"), d = new Date();
const name = "image-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds()) + ".png";
let out = args[1] || process.cwd();
// 给的是目录(含 . / 结尾带斜杠)⇒ 在目录里自动起名(10-13:传「.」曾拼成隐藏文件 ..webp)
let isDir = false; try { isDir = fs.statSync(out).isDirectory(); } catch (e) {}
if (isDir || /[\/\\]$/.test(out)) { fs.mkdirSync(out, { recursive: true }); out = path.join(out, name); }
generate(args[0], out).catch((e) => die("生成失败:" + e.message));
'@
$imgMd = @'
---
name: carher-image
description: 画图 / 生成图片 / 出图 / 做配图、插画、海报、头像、示意图时用。调公司的 muse-image 出一张真图片(约 30 秒),保存到当前文件夹。用户说「画一张…」「生成一张图」「帮我做个配图」都用它,不要用 SVG、ASCII、HTML 或代码画图代替。
---

# 画图

用户要图片时,**只用下面这个命令**(它调公司的 muse-image,key 已经配好):

```
@@NODE@@ "@@TOOL@@" "画面描述" 输出路径
```

- 画面描述用用户的原话,可以补充风格、构图、颜色等细节;用中文或英文都行。
- 输出路径给当前项目里的一个文件夹(比如 `.`)或文件名;后缀会按实际格式自动改(一般是 `.webp`)。
- 约 30 秒出图。以命令最后打印的「已生成:…」那一行为准,把这个路径告诉用户。
- **不要**向用户要 key,也不要在命令里写 key。
- 命令报错就把报错原文告诉用户(比如「你的 key 还没有开通画图模型」),不要换别的方式硬画。
'@
$noBom = New-Object Text.UTF8Encoding $false
$toolPath = Join-Path $imgDir 'carher_image.js'
[IO.File]::WriteAllText($toolPath, $imgJs, $noBom)
$nodeAbs = (Get-Command node -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if ($nodeAbs) {
  [IO.File]::WriteAllText((Join-Path $skillDir 'SKILL.md'), $imgMd.Replace('@@NODE@@', $nodeAbs).Replace('@@TOOL@@', $toolPath), $noBom)
  # ~/.claude/CLAUDE.md 里放一段带标记的常驻说明(只有 skill 时一句「画一张…」常被写成 SVG);重跑只换本块,改前备份
  $cmdMd = Join-Path $claudeHome 'CLAUDE.md'
  $old = ''; if (Test-Path $cmdMd) { Copy-Item $cmdMd ($cmdMd + '.bak-carher-' + (Get-Date -Format 'yyyyMMdd-HHmmss')); $old = [IO.File]::ReadAllText($cmdMd) }
  $B = '<!-- CARHER-IMAGE-BEGIN -->'; $E = '<!-- CARHER-IMAGE-END -->'
  $i = $old.IndexOf($B); $j = $old.IndexOf($E)
  if ($i -ge 0 -and $j -gt $i) { $old = $old.Substring(0, $i) + $old.Substring($j + $E.Length) }
  $old = $old.TrimEnd()
  $line = '"' + $nodeAbs + '" "' + $toolPath + '" "画面描述" 输出路径'
  $blk = $B + "`n## 画图`n`n用户要画图、生成图片、出图、做配图/插画/海报/头像时,**只用这个命令**(调公司的 muse-image,key 已配好),不要用 SVG / ASCII / HTML / 代码画图代替:`n`n    " + $line + "`n`n- 约 30 秒出图;以命令最后打印的「已生成:…」路径为准告诉用户。`n- 不要向用户要 key。命令报错就把原文告诉用户,不要换别的方式硬画。`n" + $E + "`n"
  if ($old) { $blk = $old + "`n`n" + $blk }
  [IO.File]::WriteAllText($cmdMd, $blk, $noBom)
  Ok "√ 已开通画图(说「画一张…」即可)"

  # ── 4d2. 一句话生成视频(11-01):skill carher-video + CLAUDE.md 标记块 ──
  $vSkillDir = Join-Path $claudeHome 'skills\carher-video'
  New-Item -ItemType Directory -Force -Path $vSkillDir | Out-Null
  $vidJs = @'
#!/usr/bin/env node
/* CarHer 生成视频工具(Node,零依赖)—— 由三个一键配置(Codex / Claude Code / Cursor)自动安装,用户无需关心。

   用法:
     node carher_video.js "一句话描述" [输出路径或目录] [--portrait]   提交并等最多 90 秒
     node carher_video.js --wait [任务号]                              接着等(不写任务号 = 最近一个)
     --portrait / --竖屏  竖屏 720x1280(默认横屏 1280x720;上游只支持这两种)
     --retry              同一个描述刚失败过,确认要再扣一次额度重试

   为什么分「提交」和「接着等」(11-01 在三个 App 里实测出来的):
     一条视频要 1.5~3 分钟,而 agent 的命令工具不擅长等长命令 ——
     Codex App 10 秒就先交回半截输出,模型以为「输出丢了」就自己重跑(多扣额度);
     App 关掉会话时子进程被一起带走(额度照扣、片子没了);Claude Code 默认 2 分钟掐命令。
     ⇒ 真正干活的是一个**脱离会话的后台进程**(会话关了也继续,出完自己落盘);
       前台命令最多等 90 秒就返回,没好就打印一条 --wait 命令让 agent 接着等。
     ⇒ 同一个描述 + 同一个输出位置**不会重复提交**:还在生成就接上去等,刚出完就直接报结果,
       刚失败过要加 --retry 才会再提交。agent 再怎么重跑也只扣一份额度。

   key / 网关地址的找法与 carher_image.js 完全一致(先找到的为准),不接受命令行传 key:
     1. CARHER_IMAGE_KEY / CARHER_IMAGE_BASE 环境变量(给测试用)
     2. ~/.codex/auth.json + ~/.codex/config.toml        (Codex 一键)
     3. ~/.claude/settings.json 的 env.ANTHROPIC_API_KEY  (Claude Code 一键;网关地址加 /v1)
     4. ~/.carher/image.json {"key","base"}              (Cursor 一键写的)

   流程 = OpenAI 视频三段式:POST /videos → 每 10 秒 GET /videos/{id} 直到 completed → GET /videos/{id}/content 存 mp4。
   🔴 同一把 key 同时提交两个视频,网关目前只能取回其中一个(另一个查进度 404,额度照扣)
      ⇒ 本机用一个锁把视频排队:同一时间只跑一个,后来的等前一个出完再提交。 */
"use strict";
const fs = require("fs"), os = require("os"), path = require("path"), crypto = require("crypto");

const MODEL = process.env.CARHER_VIDEO_MODEL || "gemini-video";
const HOME = os.homedir();
const POLL_MS = Number(process.env.CARHER_VIDEO_POLL_MS) || 10000;     // env 只给离线测试用
const WAIT_MS = Number(process.env.CARHER_VIDEO_WAIT_MS) || 90000;     // 前台每次最多等多久(< Claude Code 默认 2 分钟)
const MAX_WAIT_MS = 15 * 60 * 1000;     // 上游最慢的号约 10 分钟才回超时,留足余量
const LOCK_STALE_MS = 20 * 60 * 1000;   // 锁的心跳比这还老 = 持锁进程死了没清,直接接管
const RECENT_MS = 30 * 60 * 1000;       // 这么久以内的同一任务:不重复提交
const STATE_DIR = process.env.CARHER_VIDEO_LOCK_DIR || path.join(HOME, ".carher");
const JOBS = path.join(STATE_DIR, "video-jobs");

let haveLock = false;
function readJson(p) { try { return JSON.parse(fs.readFileSync(p, "utf8").replace(/^﻿/, "")); } catch (e) { return null; } }
function die(msg) { releaseLock(); console.error(msg); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function credentials() {
  if (process.env.CARHER_IMAGE_KEY && process.env.CARHER_IMAGE_BASE)
    return [process.env.CARHER_IMAGE_KEY.trim(), process.env.CARHER_IMAGE_BASE.trim().replace(/\/+$/, "")];
  const ch = process.env.CODEX_HOME || path.join(HOME, ".codex");
  const auth = readJson(path.join(ch, "auth.json"));
  if (auth && auth.OPENAI_API_KEY) {
    let base = "";
    try { const m = /^\s*base_url\s*=\s*"([^"]+)"/m.exec(fs.readFileSync(path.join(ch, "config.toml"), "utf8")); if (m) base = m[1]; } catch (e) {}
    if (base) return [String(auth.OPENAI_API_KEY).trim(), base.replace(/\/+$/, "")];
  }
  // 桌面 App 起的 Claude Code 可能带着别的 CLAUDE_CONFIG_DIR ⇒ 两处都找
  for (const dir of [process.env.CLAUDE_CONFIG_DIR, path.join(HOME, ".claude")]) {
    if (!dir) continue;
    const s = readJson(path.join(dir, "settings.json"));
    const env = (s && s.env) || {};
    if (env.ANTHROPIC_API_KEY && env.ANTHROPIC_BASE_URL)
      return [String(env.ANTHROPIC_API_KEY).trim(), String(env.ANTHROPIC_BASE_URL).replace(/\/+$/, "") + "/v1"];
  }
  const c = readJson(path.join(HOME, ".carher", "image.json"));
  if (c && c.key && c.base) return [String(c.key).trim(), String(c.base).replace(/\/+$/, "")];
  return null;
}

// ── 任务记录:~/.carher/video-jobs/<任务号>.json(原子写:先写临时文件再改名)──
function jobPath(id) { return path.join(JOBS, id + ".json"); }
function readJob(id) { return readJson(jobPath(id)); }
function writeJob(job) {
  job.updated = Date.now();
  const p = jobPath(job.id), tmp = p + "." + process.pid + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(job, null, 2));
  fs.renameSync(tmp, p);
}
function listJobs() {
  let names = [];
  try { names = fs.readdirSync(JOBS).filter((n) => /\.json$/.test(n)); } catch (e) {}
  return names.map((n) => readJson(path.join(JOBS, n))).filter((j) => j && j.id).sort((a, b) => b.created - a.created);
}
function cleanupOld() {                          // 7 天前的记录删掉(只删记录,不碰视频文件)
  for (const j of listJobs()) if (Date.now() - j.created > 7 * 864e5) try { fs.rmSync(jobPath(j.id), { force: true }); } catch (e) {}
}
function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; }
}
function finished(j) { return j.state === "done" || j.state === "failed"; }
// 后台进程死了却没写结局(被强杀 / 关机)⇒ 当失败处理,免得前台永远等
function settle(j) {
  if (j && !finished(j) && !pidAlive(j.pid) && Date.now() - j.updated > 15000) {
    j.state = "failed"; j.msg = "后台生成进程意外退出了(电脑睡眠/关机或被强制结束)。" + (j.videoId ? "任务 id:" + j.videoId : "");
    try { writeJob(j); } catch (e) {}
  }
  return j;
}

// ── 本机排队锁(目录锁:mkdir 是原子的,Mac/Windows 都一样)──
const LOCK = path.join(STATE_DIR, "video.lock");
const OWNER = path.join(LOCK, "owner"), BREAK = LOCK + ".break";
function ownerPid() { try { return parseInt(fs.readFileSync(OWNER, "utf8"), 10) || 0; } catch (e) { return 0; } }
function releaseLock() {
  if (!haveLock) return;
  haveLock = false;
  if (ownerPid() !== process.pid) return;         // 锁已被别人接管(我们拖太久被判死)⇒ 别删别人的锁
  try { fs.rmSync(LOCK, { recursive: true, force: true }); } catch (e) {}
}
// 心跳:持锁期间刷新 owner 的 mtime,「锁老」按最后一次心跳算,不按建锁时间
function touchLock() { if (haveLock && ownerPid() === process.pid) try { fs.writeFileSync(OWNER, String(process.pid)); } catch (e) {} }
function lockState() {                            // "gone" | "alive" | "stale"
  let age;
  try { age = Date.now() - fs.statSync(LOCK).mtimeMs; } catch (e) { return "gone"; }
  try { age = Date.now() - fs.statSync(OWNER).mtimeMs; } catch (e) {}
  const pid = ownerPid();
  let alive = age < 3000;                         // 刚建、还没写 owner:当作活着
  if (pid) alive = pidAlive(pid);
  return alive && age <= LOCK_STALE_MS ? "alive" : "stale";   // pid 被复用时靠心跳超时兜底
}
// 两个排队进程同时发现死锁:各自「删 + mkdir」会把对方刚建的新锁删掉 ⇒ 两个一起提交。
// 用第二把短锁把「判死 + 删」串起来,拿到短锁后重新判一次。
function breakStale() {
  try { fs.mkdirSync(BREAK); } catch (e) {
    if (e.code !== "EEXIST") throw e;
    try { if (Date.now() - fs.statSync(BREAK).mtimeMs > 10000) fs.rmSync(BREAK, { recursive: true, force: true }); } catch (e2) {}
    return;
  }
  try { if (lockState() === "stale") fs.rmSync(LOCK, { recursive: true, force: true }); }
  finally { try { fs.rmdirSync(BREAK); } catch (e) {} }
}
async function acquireLock(job) {
  const t0 = Date.now();
  for (;;) {
    try { fs.mkdirSync(LOCK); haveLock = true; fs.writeFileSync(OWNER, String(process.pid)); return; }
    catch (e) { if (e.code !== "EEXIST") throw e; }
    const st = lockState();
    if (st === "gone") continue;
    if (st === "stale") { breakStale(); await sleep(200); continue; }
    if (job.state !== "queued") { job.state = "queued"; writeJob(job); } else if (Date.now() - job.updated > 10000) writeJob(job);
    if (Date.now() - t0 > LOCK_STALE_MS) throw new Error("等前一个视频等了 20 分钟还没出完,请稍后再试。");
    await sleep(3000);
  }
}

async function api(key, url, opts = {}) {
  let last;
  for (let i = 0; i < 3; i++) {                  // 只重试网络抖动;HTTP 错误码直接交给调用方判断
    try {
      return await fetch(url, { ...opts, signal: AbortSignal.timeout(opts.timeout || 60000),
        headers: { Authorization: "Bearer " + key, ...(opts.headers || {}) } });
    } catch (e) { last = e; await sleep(3000); }
  }
  throw last;
}

function explain(status, detail) {
  if (status === 403) return "你的 key 还没有开通视频模型 " + MODEL + ",请联系运维开通后重试。";
  if (status === 401) return "key 已失效,请找运维重新颁发。";
  if (status === 404 && !/model/i.test(detail))
    return "任务查不到了(HTTP 404,常见原因:同一把 key 另有视频在同时生成),请稍后单独重试一次。原文:" + detail.slice(0, 200);
  if (/quota_exceeded|capacity|already being generated/i.test(detail))
    return "视频服务这会儿忙(上游额度/排队),过几分钟再试。原文:" + detail.slice(0, 200);
  if (/not ready before the deadline/i.test(detail))
    return "上游这次没在时限内出片(偶发),过几分钟再试一次。原文:" + detail.slice(0, 200);
  return "生成失败" + (status ? "(HTTP " + status + ")" : "") + ":" + detail.slice(0, 300);
}

// ── 后台进程:排队 → 提交一次 → 轮询 → 下载落盘,每一步写进任务记录 ──
async function worker(id) {
  const job = readJob(id);
  if (!job) process.exit(2);
  job.pid = process.pid; writeJob(job);
  const fail = (msg) => { releaseLock(); job.state = "failed"; job.msg = msg; writeJob(job); process.exit(1); };
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(sig, () => fail("后台生成进程被结束了。" + (job.videoId ? "任务 id:" + job.videoId : "")));
  process.on("exit", releaseLock);
  try {
    const cred = credentials();
    if (!cred) return fail("找不到 key。请重新运行一次一键配置脚本。");
    const [key, base] = cred;
    await acquireLock(job);
    job.state = "submitting"; writeJob(job);
    let r;
    try {
      // 只提交一次,不重试:生成类请求重试 = 多出一份、多扣一份额度
      r = await fetch(base + "/videos", {
        method: "POST", signal: AbortSignal.timeout(120000),
        headers: { Authorization: "Bearer " + key, "Content-Type": "application/json" },
        body: JSON.stringify({ model: MODEL, prompt: job.prompt, size: job.portrait ? "720x1280" : "1280x720" }),
      });
    } catch (e) { return fail("提交失败:" + e.message); }
    if (!r.ok) return fail(explain(r.status, await r.text()));
    let st = await r.json();
    if (!st || !st.id) return fail("提交失败:返回里没有任务 id。");
    job.videoId = st.id; job.state = "running"; job.started = Date.now(); job.progress = st.progress || 0; writeJob(job);

    while (st.status !== "completed") {
      if (st.status === "failed" || st.status === "cancelled") {
        const e = st.error || {};
        return fail(explain(0, String(e.message || e.code || st.status)));
      }
      if (Date.now() - job.started > MAX_WAIT_MS) return fail("等了 15 分钟还没出来,放弃。任务 id:" + job.videoId);
      await sleep(POLL_MS);
      touchLock();
      const q = await api(key, base + "/videos/" + encodeURIComponent(job.videoId));
      if (!q.ok) return fail(explain(q.status, await q.text()));
      st = await q.json();
      if (st.progress != null && st.progress !== job.progress) { job.progress = st.progress; writeJob(job); }
      else if (Date.now() - job.updated > 30000) writeJob(job);
    }
    job.state = "downloading"; job.progress = 100; writeJob(job);
    let data, last;
    for (let i = 0; i < 3 && !data; i++) {        // 刚完成时文件偶尔还没就绪
      touchLock();
      try {
        const c = await api(key, base + "/videos/" + encodeURIComponent(job.videoId) + "/content", { timeout: 300000 });
        if (c.ok) { data = Buffer.from(await c.arrayBuffer()); break; }
        last = new Error("HTTP " + c.status + " " + (await c.text()).slice(0, 200));
      } catch (e) { last = e; }
      await sleep(3000);
    }
    if (!data) return fail("视频已生成但下载失败:" + (last && last.message));
    if (data.length < 1024 || data.slice(4, 8).toString("latin1") !== "ftyp")
      return fail("下载到的不是视频文件(" + data.length + " 字节):" + data.slice(0, 120).toString("utf8"));
    fs.mkdirSync(path.dirname(job.out), { recursive: true });
    fs.writeFileSync(job.out, data);
    releaseLock();
    job.state = "done"; job.bytes = data.length; job.seconds = Math.round((Date.now() - job.created) / 1000); writeJob(job);
    process.exit(0);
  } catch (e) { fail("生成失败:" + (e && e.message || e)); }
}

// ── 前台:打印进度,最多等 WAIT_MS;好了报结果,没好告诉 agent 怎么接着等 ──
function waitCmd(id) {
  const self = '"' + process.execPath + '" "' + __filename + '"';
  if (process.env.ELECTRON_RUN_AS_NODE) {          // Cursor:用它自带的 node 跑,要带上这个开关
    return process.platform === "win32"
      ? 'cmd /c "set ELECTRON_RUN_AS_NODE=1&& ' + self + ' --wait ' + id + '"'
      : "ELECTRON_RUN_AS_NODE=1 " + self + " --wait " + id;
  }
  return self + " --wait " + id;
}
function describe(j) {
  if (j.state === "queued") return "排队中(前面还有一个视频在生成,同一把 key 一次只能出一个)";
  if (j.state === "running") return "生成中 " + (j.progress || 0) + "%";
  if (j.state === "downloading") return "已生成,正在下载";
  return "准备提交";
}
async function follow(id) {
  const t0 = Date.now();
  let shown = "";
  for (;;) {
    const j = settle(readJob(id));
    if (!j) die("找不到任务 " + id + "(记录可能已过期)。");
    if (j.state === "done") {
      console.log("已生成:" + j.out + "(" + ((j.bytes || 0) / 1048576).toFixed(1) + " MB,用时 " + (j.seconds ?? "?") + " 秒)");
      process.exit(0);
    }
    if (j.state === "failed") die("生成失败:" + j.msg);
    const s = describe(j);
    if (s !== shown) { console.log("  " + Math.round((Date.now() - j.created) / 1000) + " 秒:" + s); shown = s; }
    if (Date.now() - t0 >= WAIT_MS) {
      console.log("");
      console.log("⏳ 视频还没好(" + s + "),它在后台继续生成,关掉对话也不影响。");
      console.log("   不要重新提交(会多扣一次额度)。过一会儿运行下面这条接着等:");
      console.log("   " + waitCmd(id));
      process.exit(0);
    }
    await sleep(2000);
  }
}

function videoName(id) {
  const pad = (n) => String(n).padStart(2, "0"), d = new Date();
  return "video-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds()) + "-" + id.slice(-4) + ".mp4";
}

async function submit(prompt, outArg, portrait, retry) {
  if (!credentials()) die("找不到 key。请重新运行一次一键配置脚本。");
  try { await fetch(credentials()[1] + "/models", { method: "HEAD", signal: AbortSignal.timeout(15000) }); }
  catch (e) {
    const c = (e && e.cause && e.cause.code) || "";
    if (/ENOTFOUND|EAI_AGAIN|EPERM|ECONNREFUSED/.test(c)) die("连不上网关(" + c + "),多半是命令沙箱禁止联网。请让这条命令在沙箱外运行;还没提交,不扣额度。");
  }
  let out = path.resolve(outArg || process.cwd());
  let isDir = false; try { isDir = fs.statSync(out).isDirectory(); } catch (e) {}
  if (!isDir && /[\/\\]$/.test(outArg || "")) isDir = true;
  if (!isDir && !/\.mp4$/i.test(out)) out += ".mp4";
  // 提交前就把目录建好:等 2 分钟、扣完额度才发现父目录不存在 = 白扣一份
  try { fs.mkdirSync(isDir ? out : path.dirname(out), { recursive: true }); } catch (e) { die("输出路径不可用:" + e.message); }
  // Cursor / Codex 的命令沙箱默认不让写 ~/.carher、不让联网 ⇒ 在提交之前就说清楚(此时还没扣额度)
  try { fs.mkdirSync(JOBS, { recursive: true }); fs.accessSync(JOBS, fs.constants.W_OK); }
  catch (e) {
    if (/EPERM|EACCES|EROFS/.test(e.code || "")) die("被命令沙箱拦住了(写不了 " + JOBS + ")。请让这条命令在沙箱外运行(需要联网);还没提交,不扣额度。");
    throw e;
  }
  cleanupOld();

  // 防重复提交:同一描述 + 同一输出位置 + 同一画幅,30 分钟内的任务直接接上
  const sig = crypto.createHash("sha1").update([prompt, isDir ? out + path.sep : out, portrait ? "p" : "l"].join("\n")).digest("hex").slice(0, 16);
  const prev = listJobs().find((j) => j.sig === sig && Date.now() - j.created < RECENT_MS);
  if (prev) {
    settle(prev);
    if (!finished(prev)) { console.log("这个视频已经在生成了(任务 " + prev.id + "),接着等,不会重复提交。"); return follow(prev.id); }
    if (prev.state === "done" && fs.existsSync(prev.out) && !retry) {
      console.log("这个视频刚生成过,直接用上次的结果(要重新生成一条请加 --retry)。");
      return follow(prev.id);
    }
    if (prev.state === "failed" && !retry)
      die("这个视频刚失败过:" + prev.msg + "\n确认要再试一次(会再扣一次额度)请在命令末尾加 --retry。");
  }

  const id = Date.now().toString(36) + crypto.randomBytes(3).toString("hex");
  const job = { id, sig, prompt, portrait, out: isDir ? path.join(out, videoName(id)) : out, state: "starting",
                created: Date.now(), progress: 0, model: MODEL };
  writeJob(job);
  const { spawn } = require("child_process");
  // detached:自成一个进程组/会话 ⇒ agent 的命令被掐、对话被关、App 退出都不会带走它
  const child = spawn(process.execPath, [__filename, "--worker", id], { detached: true, stdio: "ignore", windowsHide: true, env: process.env });
  child.unref();   // 不在这里回写记录:后台进程可能已经写了新状态,前台再写会把它盖回去(只由后台写 pid)
  console.log("已提交(任务 " + id + "),一条约 1.5~3 分钟,下面每隔一会儿报一次进度…");
  return follow(id);
}

const raw = process.argv.slice(2);
if (raw[0] === "--worker") { worker(raw[1]); }
else if (raw[0] === "--wait") {
  const id = raw[1] || (listJobs()[0] || {}).id;
  if (!id) die("没有正在生成的视频。");
  follow(id).catch((e) => die("出错了:" + e.message));
} else {
  const FLAGS = ["--portrait", "--竖屏", "--retry"];
  const portrait = raw.some((a) => a === "--portrait" || a === "--竖屏");
  const retry = raw.includes("--retry");
  const args = raw.filter((a) => a.trim() && !FLAGS.includes(a));
  if (!args.length) die('用法:node ' + path.basename(process.argv[1]) + ' "一句话描述" [输出路径.mp4] [--portrait]\n      node ' + path.basename(process.argv[1]) + ' --wait [任务号]');
  submit(args[0], args[1], portrait, retry).catch((e) => die("生成失败:" + e.message));
}
'@
  $vidMd = @'
---
name: carher-video
description: 生成视频 / 做短视频 / 出一段视频 / 把一句话变成视频 / 做个动画视频时用。调公司的 gemini-video 真实生成一段约 10 秒、带声音的 mp4(约 2 分钟),保存到当前文件夹。用户说「生成一个视频…」「做个短视频」「帮我出一段…的视频」都用它,不要用代码画动画、PIL/ffmpeg 合成、HTML 或别的方式代替,也不要说「没法生成视频」。
---

# 生成视频

用户要视频时,**只用下面这个命令**(它调公司的 gemini-video,key 已经配好):

```
@@NODE@@ "@@TOOL@@" "画面描述" 输出路径
```

- 画面描述用用户的原话,可以补充镜头、动作、风格、光线等细节;中英文都行。
  输出路径给当前项目里的一个文件夹(比如 `.`)或 `.mp4` 文件名。用户要竖屏(手机、抖音)就在末尾加 `--portrait`。
- 命令最多等约 1.5 分钟就会自己结束。
  - 最后一行是「已生成:…」⇒ 把这个路径告诉用户,完成。
  - 打印「⏳ 视频还没好」⇒ 视频在后台继续生成。**运行它给出的那条 `--wait` 命令接着等**,
    还没好就再运行一次,直到出现「已生成:…」。不要重新提交原命令,不要另开 ls / ps / sleep 去查。
- 这条命令要联网、要写 `~/.carher`:在有命令沙箱的工具里请直接申请在沙箱外运行(Cursor:`required_permissions: ["all"]`)。
  报「被命令沙箱拦住了」「连不上网关」时还没扣额度,在沙箱外重跑同一条命令即可。
  命令被工具转到后台时,等它结束、读最后几行结果,不要直接结束回合。
- 一次只出一个;用户要多个就一个出完再出下一个。
- **不要**向用户要 key,也不要在命令里写 key。
- 命令报错就把报错原文告诉用户(比如「你的 key 还没有开通视频模型」「视频服务这会儿忙」),**不要自动重试**、不要换别的方式硬做;
  用户说再试一次时,在原命令末尾加 `--retry`。
'@
  $vToolPath = Join-Path $imgDir 'carher_video.js'
  [IO.File]::WriteAllText($vToolPath, $vidJs, $noBom)
  [IO.File]::WriteAllText((Join-Path $vSkillDir 'SKILL.md'), $vidMd.Replace('@@NODE@@', $nodeAbs).Replace('@@TOOL@@', $vToolPath), $noBom)
  $old = [IO.File]::ReadAllText($cmdMd)
  $VB = '<!-- CARHER-VIDEO-BEGIN -->'; $VE = '<!-- CARHER-VIDEO-END -->'
  $i = $old.IndexOf($VB); $j = $old.IndexOf($VE)
  if ($i -ge 0 -and $j -gt $i) { $old = $old.Substring(0, $i) + $old.Substring($j + $VE.Length) }
  $old = $old.TrimEnd()
  $vline = '"' + $nodeAbs + '" "' + $vToolPath + '" "画面描述" 输出路径'
  $vblk = $VB + "`n## 生成视频`n`n用户要生成视频、做短视频、出一段视频时，**只用下面这个命令**（它调公司的 gemini-video 真实生成 10 秒带声音的 mp4），**不要说「没法生成视频」，不要用代码画动画 / PIL / ffmpeg / HTML 合成代替**：`n`n    " + $vline + "`n`n- 画面描述用用户的原话，可以补充镜头、动作、风格、光线等细节。用户要竖屏（手机、抖音）就在末尾加 --portrait。`n- 命令最多等约 1.5 分钟就自己结束：最后一行是「已生成：…」就把这个路径告诉用户；`n  打印「⏳ 视频还没好」就**运行它给出的那条 --wait 命令接着等**（还没好就再运行一次），直到「已生成：…」。`n  不要重新提交原命令（会多扣一次额度），不要另开 ls / ps / sleep 去查。`n- 一次只出一个。key 已配好，不要向用户索要，也不要在命令里写 key。`n- 命令报错就把报错原文告诉用户，**不要自动重试**、不要换别的方式硬做；用户说再试一次时在原命令末尾加 --retry。`n" + $VE + "`n"
  if ($old) { $vblk = $old + "`n`n" + $vblk }
  [IO.File]::WriteAllText($cmdMd, $vblk, $noBom)
  Ok "√ 已开通生成视频(说「生成一个…的视频」即可)"

  # ── 4e. 「刷模型」(10-19):说这三个字就按 key 重拉 /model 菜单 ──
  # 10-27:旧名和 Cursor 的同名 ⇒ Cursor agent 读到这份、跑成 Claude 的刷新。改名并删旧目录
  Remove-Item (Join-Path $claudeHome 'skills\carher-refresh-models') -Recurse -Force -ErrorAction SilentlyContinue
  $rsDir = Join-Path $claudeHome 'skills\carher-refresh-models-claude'
  New-Item -ItemType Directory -Force -Path $rsDir | Out-Null
  $rsMd = @'
---
name: carher-refresh-models-claude
description: 仅限 Claude Code(命令行 claude 或 Claude 桌面 App)里用:用户说「刷模型」「更新模型」「刷新模型列表」,或说 /model 里的模型不对时。按用户的 key 从公司网关重新拉模型列表,同时更新 Claude Code 命令行和 Claude 桌面 App 的模型菜单。⛔ 如果你是 Cursor 或 Codex,不要用这个,用你自己那个工具的刷模型。
---

# 刷模型

直接运行下面这条命令,不要先问用户:

```
@@NODE@@ "@@TOOL@@"
```

- 把输出里「新增 / 删除 / 已改回默认模型」那几行原样告诉用户。
- 提醒用户:命令行新开一个 claude 会话就是新列表;Claude 桌面 App 要**完全退出再打开**才显示新菜单。
- 命令报错就把原文告诉用户,不要自己改 settings.json。
- 平时不用手动刷:每次打开 Claude Code 都会在后台自动刷一次。说「刷模型」= 现在立刻刷。
'@
  [IO.File]::WriteAllText((Join-Path $rsDir 'SKILL.md'), $rsMd.Replace('@@NODE@@', $nodeAbs).Replace('@@TOOL@@', $refreshPath), $noBom)
  $old = [IO.File]::ReadAllText($cmdMd)
  $B = '<!-- CARHER-REFRESH-BEGIN -->'; $E = '<!-- CARHER-REFRESH-END -->'
  $i = $old.IndexOf($B); $j = $old.IndexOf($E)
  if ($i -ge 0 -and $j -gt $i) { $old = $old.Substring(0, $i) + $old.Substring($j + $E.Length) }
  $old = $old.TrimEnd()
  $line = '"' + $nodeAbs + '" "' + $refreshPath + '"'
  $blk = $B + "`n## 刷模型`n`n(仅 Claude Code / Claude 桌面 App)用户说「刷模型」「更新模型」「刷新模型列表」,或说 /model 里的模型不对时,**直接运行这条命令,不要先问**:`n`n    " + $line + "`n`n- 把输出里「新增 / 删除 / 已改回默认模型」几行原样告诉用户,并提醒新开一个会话后 /model 里就是新列表。`n- 报错就把原文告诉用户,不要自己改 settings.json。`n" + $E + "`n"
  if ($old) { $blk = $old + "`n`n" + $blk }
  [IO.File]::WriteAllText($cmdMd, $blk, $noBom)
  Ok "√ 以后在 Claude Code 里说「刷模型」就能更新模型列表"
  # 10-25:每次开 Claude Code 后台自动刷一次菜单(SessionStart 异步 hook,不挡输入、不出声),与 mac 同
  $env:CARHER_REFRESH = $refreshPath; $env:CARHER_NODE = $nodeAbs; $env:CARHER_SETTINGS = $settings
  $hookJs = @'
const fs = require("fs"), p = process.env.CARHER_SETTINGS;
const d = JSON.parse(fs.readFileSync(p, "utf8").replace(/^\uFEFF/, ""));
require(process.env.CARHER_REFRESH).hookEntry(d, process.env.CARHER_NODE, process.env.CARHER_REFRESH);
fs.writeFileSync(p, JSON.stringify(d, null, 2) + "\n");
'@
  $hjs = Join-Path $env:TEMP "carher-hook-$([guid]::NewGuid().ToString('N')).js"
  [IO.File]::WriteAllText($hjs, $hookJs, $noBom)
  try { & node $hjs; $hrc = $LASTEXITCODE } finally {
    Remove-Item $hjs -Force -ErrorAction SilentlyContinue
    Remove-Item Env:CARHER_REFRESH, Env:CARHER_NODE, Env:CARHER_SETTINGS -ErrorAction SilentlyContinue
  }
  if ($hrc -eq 0) { Ok "√ 以后每次打开 Claude Code 会自动按 key 更新 /model 菜单" } else { Warn "! 自动刷新没挂上(说「刷模型」照样能手动刷)" }
} else { Warn "! 没找到 Node,画图和「刷模型」没开通(命令行 claude 不受影响)" }

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
# 🔴 10-15 同事实机:原来走 `cmd /c "claude --print ""…$a+$b…"""`,cmd 把引号里的内容切了,
#    claude 收到的只有「9511+」⇒ 回「第二个数呢」。⇒ 不经 cmd,PowerShell 直接调 claude,
#    题目放变量里当一个参数传;stdin 用 $null 关掉(否则 claude 会等输入)。
#    题目里不带 + 号和引号(只用字母和数字),走 cmd /c 也切不坏;claude 在 Windows 上是 npm 装的
#    claude.cmd,必须经 cmd /c 调(直接调会撞执行策略)。stdin 接 NUL 免得它等输入。
$q = "What is $a plus $b. Reply with only the number"
try { $out = (& cmd /c "claude --print `"$q`" < NUL 2>NUL" | Select-Object -Last 1) } catch { $out = '' }
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
