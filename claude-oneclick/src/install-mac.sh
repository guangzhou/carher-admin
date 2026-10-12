#!/usr/bin/env bash
# CarHer Claude Code 一键配置 (macOS / Linux)
# 用法：Finder 里双击本文件，或终端里执行 `bash install-mac.command`
# ⚠️ 这是模板：改这里，然后跑 ../build.sh 生成用户拿到的 install-mac.command
#
# 做四件事，用户只需要粘一次 key：
#   1. 没有 Node.js ≥22 就装一个（Claude Code 硬要求）—— 装到 ~/.carher-node，不碰系统、不要 sudo
#   2. 没有 claude / 版本太旧就装最新版（走 npmmirror：官方 downloads.claude.ai 在国内不通）
#   3. 读 key
#   4. 把网关配置**合并**进 ~/.claude/settings.json（保留用户原有设置），删模型列表缓存
set -euo pipefail

BLUE=$'\033[1;34m'; GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[1;31m'; NC=$'\033[0m'
say()  { printf '%s%s%s\n' "$BLUE" "$1" "$NC"; }
ok()   { printf '%s%s%s\n' "$GREEN" "$1" "$NC"; }
warn() { printf '%s%s%s\n' "$YELLOW" "$1" "$NC"; }
die()  { printf '%s%s%s\n' "$RED" "$1" "$NC" >&2; exit 1; }

# ── 固定配置（用户无需感知）────────────────────────────────────────────
BASE_URL="https://cc.auto-link.com.cn/pro"
NPM_REGISTRY="https://registry.npmmirror.com"
NODE_MIRROR="https://cdn.npmmirror.com/binaries/node"
NODE_MAJOR_MIN=22
# 2.1.257 之前的版本打 5.x 模型会被上游以 claude_code_version_too_old 拒(400)
CLAUDE_MIN="2.1.257"
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
NODE_HOME="$HOME/.carher-node"

echo
say "==================================================="
say "   CarHer Claude Code 一键配置 (macOS / Linux)"
say "==================================================="
echo

# ── 平台 ─────────────────────────────────────────────────────────────
case "$(uname -s)" in
  Darwin) OS=darwin ;;
  Linux)  OS=linux ;;
  *) die "✗ 不支持的系统：$(uname -s)。Windows 请用 install-windows.bat。" ;;
esac
case "$(uname -m)" in
  x86_64|amd64)  ARCH=x64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) die "✗ 不支持的 CPU 架构：$(uname -m)" ;;
esac
# Rosetta 下跑的 x64 shell 其实是 Apple 芯片 ⇒ 装 arm64 版(否则 Node/claude 全走转译)
if [ "$OS" = darwin ] && [ "$ARCH" = x64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = 1 ]; then
  ARCH=arm64
fi

ver_ge() {  # ver_ge A B ⇒ A >= B(纯数字点分)
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -n1)" = "$2" ]
}

# ── 1. Node.js ≥22 ───────────────────────────────────────────────────
# 先把我们自己装过的那份放进 PATH,重跑脚本时能认出来
[ -x "$NODE_HOME/bin/node" ] && export PATH="$NODE_HOME/bin:$PATH"

node_major() { node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0; }

if command -v node >/dev/null 2>&1 && [ "$(node_major)" -ge "$NODE_MAJOR_MIN" ]; then
  ok "✔ Node.js $(node --version)"
else
  if command -v node >/dev/null 2>&1; then
    warn "⚠ 现有 Node.js $(node --version) 太旧(需要 ≥ v$NODE_MAJOR_MIN),装一个新的到 $NODE_HOME(不影响你原来的)"
  else
    say "→ 没有 Node.js,装一个到 $NODE_HOME(不需要管理员密码)"
  fi
  NODE_VER="$(curl -fsSL --max-time 30 "$NODE_MIRROR/index.json" \
    | grep -oE '"version":"v'"$NODE_MAJOR_MIN"'\.[0-9]+\.[0-9]+"[^}]*"lts":"[A-Za-z]+"' \
    | head -n1 | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')" \
    || die "✗ 取不到 Node.js 版本列表(网络?):$NODE_MIRROR"
  [ -n "$NODE_VER" ] || die "✗ 没找到 Node.js v$NODE_MAJOR_MIN 的 LTS 版本"
  if [ "$OS" = darwin ]; then EXT=tar.gz; else EXT=tar.xz; fi
  PKG="node-$NODE_VER-$OS-$ARCH"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  say "  下载 $PKG ..."
  curl -fSL --max-time 600 --progress-bar -o "$TMP/$PKG.$EXT" "$NODE_MIRROR/$NODE_VER/$PKG.$EXT" \
    || die "✗ 下载 Node.js 失败"
  # 校验:用官方 SHASUMS256.txt 比对,下坏的/被篡改的包不装
  WANT="$(curl -fsSL --max-time 30 "$NODE_MIRROR/$NODE_VER/SHASUMS256.txt" | awk -v f="$PKG.$EXT" '$2==f{print $1}')"
  if command -v shasum >/dev/null 2>&1; then GOT="$(shasum -a 256 "$TMP/$PKG.$EXT" | awk '{print $1}')"
  else GOT="$(sha256sum "$TMP/$PKG.$EXT" | awk '{print $1}')"; fi
  [ -n "$WANT" ] && [ "$WANT" = "$GOT" ] || die "✗ Node.js 安装包校验失败(期望 ${WANT:-?},实际 $GOT),已停止"
  rm -rf "$NODE_HOME"; mkdir -p "$NODE_HOME"
  tar -xf "$TMP/$PKG.$EXT" -C "$NODE_HOME" --strip-components=1 || die "✗ 解压 Node.js 失败"
  export PATH="$NODE_HOME/bin:$PATH"
  [ "$(node_major)" -ge "$NODE_MAJOR_MIN" ] || die "✗ Node.js 装完仍不可用"
  ok "✔ Node.js $(node --version) 已装到 $NODE_HOME"
fi

# npm 全局装到用户目录(不要 sudo、不写 /usr/local)
NPM_PREFIX="$HOME/.carher-npm-global"
mkdir -p "$NPM_PREFIX"
export npm_config_prefix="$NPM_PREFIX"
export PATH="$NPM_PREFIX/bin:$PATH"

# ── 2. Claude Code ───────────────────────────────────────────────────
cur_claude_ver() { claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1; }
CUR="$(cur_claude_ver || true)"
if [ -n "$CUR" ] && ver_ge "$CUR" "$CLAUDE_MIN"; then
  ok "✔ Claude Code $CUR"
else
  if [ -n "$CUR" ]; then warn "⚠ Claude Code $CUR 太旧(需要 ≥ $CLAUDE_MIN),升级中 ..."
  else say "→ 安装 Claude Code(约 1~3 分钟)..."; fi
  npm install -g @anthropic-ai/claude-code --registry="$NPM_REGISTRY" --no-fund --no-audit --loglevel=error \
    || die "✗ 安装 Claude Code 失败(把上面的报错截图发给运维)"
  hash -r 2>/dev/null || true
  CUR="$(cur_claude_ver || true)"
  [ -n "$CUR" ] || die "✗ 装完了但找不到 claude 命令"
  ok "✔ Claude Code $CUR"
fi
CLAUDE_BIN="$(command -v claude)"

# ── 让以后新开的终端也能直接敲 claude ───────────────────────────────
# 只追加一个带标记的块,重跑先剥旧块,不碰用户别的内容
add_path_block() {
  local rc="$1"; [ -e "$rc" ] || touch "$rc"
  local tmp; tmp="$(mktemp)"
  awk '/# >>> carher-claude >>>/{s=1;next} /# <<< carher-claude <<</{s=0;next} !s' "$rc" > "$tmp"
  {
    cat "$tmp"
    echo '# >>> carher-claude >>>'
    [ -x "$NODE_HOME/bin/node" ] && echo "export PATH=\"$NODE_HOME/bin:\$PATH\""
    echo "export PATH=\"$NPM_PREFIX/bin:\$PATH\""
    echo '# <<< carher-claude <<<'
  } > "$rc"
  rm -f "$tmp"
}
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
  # .bash_profile 只在它已存在时改(macOS 上新建它会让 .bashrc 不再被读)
  [ "$rc" = "$HOME/.bash_profile" ] && [ ! -e "$rc" ] && continue
  add_path_block "$rc"
done
echo

# ── 3. 读 key ────────────────────────────────────────────────────────
say "粘贴你的 key(sk- 开头,和 Cursor / Codex 用的是同一把),然后回车:"
printf "key: "
read -r APIKEY || true
# 剥掉所有空白 + 零宽字符(从飞书表格复制最容易带进来,带进去请求根本发不出去)。
# 🔴 用 node 做,不用 sed:macOS 的 BSD sed 不认 `[\x8b-\x8f]` 这种字节区间,直接报
#    `RE error: invalid character range` 并输出**空** ⇒ 连干净的 key 也被吃成空字符串。
#    这是台架当场抓到的(第一版就是 sed),上面已保证有 node,用它最稳。key 走环境变量进去。
APIKEY="$(CARHER_RAW="${APIKEY:-}" node -e 'process.stdout.write(process.env.CARHER_RAW.replace(/[\s\u00A0\u200B-\u200F\u2028-\u202F\uFEFF]/g,""))')"
[ -n "$APIKEY" ] || die "✗ 没有输入 key,已取消。"
case "$APIKEY" in sk-*) : ;; *) warn "⚠ key 不是 sk- 开头,仍然继续。" ;; esac
echo

# 先验 key 能用,再写配置(写了个坏 key 进去用户只会看到 401 不知道为什么)
say "→ 检查 key ..."
CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -H "x-api-key: $APIKEY" "$BASE_URL/v1/models" || echo 000)"
case "$CODE" in
  200) ok "✔ key 可用" ;;
  401|403) die "✗ key 无效(HTTP $CODE)。去 key 表里重新「全选复制」那一格再试。" ;;
  *) warn "⚠ 暂时连不上网关(HTTP $CODE),配置照样写好,网络恢复后就能用。" ;;
esac
echo

# ── 4. 写配置:合并进 settings.json,不覆盖用户原有设置 ──────────────
mkdir -p "$CLAUDE_HOME"
SETTINGS="$CLAUDE_HOME/settings.json"
if [ -f "$SETTINGS" ]; then
  BK="$SETTINGS.bak-carher-$(date +%Y%m%d-%H%M%S)"
  cp "$SETTINGS" "$BK"; warn "↳ 已备份原有配置到:$BK"
fi
# 用 Node 合并 JSON(上面已保证有 Node,不依赖 python/jq)。key 走环境变量,不进命令行参数。
CARHER_KEY="$APIKEY" CARHER_BASE="$BASE_URL" CARHER_SETTINGS="$SETTINGS" node -e '
const fs = require("fs");
const p = process.env.CARHER_SETTINGS;
let d = {};
if (fs.existsSync(p)) {
  const raw = fs.readFileSync(p, "utf8").replace(/^\uFEFF/, "");
  if (raw.trim()) {
    try { d = JSON.parse(raw); }
    catch (e) { console.error("✗ 原有 settings.json 不是合法 JSON,已备份,不动它:" + e.message); process.exit(3); }
  }
}
if (typeof d !== "object" || d === null || Array.isArray(d)) d = {};
d.env = Object.assign({}, d.env || {});
// AUTH_TOKEN 能聊天但拉不到网关模型列表;两者同时在时还会抢优先级 ⇒ 只留 API_KEY
delete d.env.ANTHROPIC_AUTH_TOKEN;
// 这个开关会把模型发现一起关掉 ⇒ /model 里只剩几个 Claude
delete d.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC;
d.env.ANTHROPIC_BASE_URL = process.env.CARHER_BASE;
d.env.ANTHROPIC_API_KEY = process.env.CARHER_KEY;
d.env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1";
fs.writeFileSync(p, JSON.stringify(d, null, 2) + "\n", { mode: 0o600 });
fs.chmodSync(p, 0o600);
' || die "✗ 写配置失败"
ok "✔ 配置已写入 $SETTINGS"

# 模型列表缓存不会自己过期 ⇒ 删掉,下次启动重新拉
rm -f "$CLAUDE_HOME/cache/gateway-models.json"

# ── 4b. Claude 桌面 App(Code 标签页)────────────────────────────────
# 🔴 桌面 App **不读** ~/.claude/settings.json 里的 ANTHROPIC_BASE_URL 来切网关 —— 它有自己的
#    「三方模式」(3P),配置放在一个**单独的** userData 目录 `<userData>-3p/configLibrary/` 里
#    (从 App 自身代码读出来的:fwA()=userData+"-3p",hwA()=…/configLibrary,_meta.json 记 appliedId)。
#    用独立目录的好处:用户原来登录的 Claude App(聊天/Cowork)的数据**一个字节都不碰**。
#    键名与取值来自 App 的 schema:inferenceProvider=gateway + GatewayBaseUrl + GatewayApiKey,
#    认证头用 x-api-key(网关两种都认,x-api-key 与 CLI 一致),凭据类型 static。
#    实测(1.15962.0):日志 `[custom-3p] 3P mode active { provider: 'gateway' }`,
#    `Model discovery: 9 found`,真请求在 SpendLogs 落 kiro-claude-haiku-4-5 两发全 ok。
if [ "$OS" = darwin ]; then APP_UD="$HOME/Library/Application Support/Claude-3p"
else APP_UD="${XDG_CONFIG_HOME:-$HOME/.config}/Claude-3p"; fi
CARHER_KEY="$APIKEY" CARHER_BASE="$BASE_URL" CARHER_LIB="$APP_UD/configLibrary" node -e '
const fs = require("fs"), path = require("path"), crypto = require("crypto");
const lib = process.env.CARHER_LIB;
fs.mkdirSync(lib, { recursive: true, mode: 0o700 });
const metaP = path.join(lib, "_meta.json");
let meta = { appliedId: "", entries: [] };
try { const m = JSON.parse(fs.readFileSync(metaP, "utf8")); if (m && Array.isArray(m.entries)) meta = m; } catch {}
// 有我们之前建的那条就复用(重跑幂等);没有就新建一条叫 CarHer,并设为当前生效
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
fs.writeFileSync(cfgP, JSON.stringify(cfg, null, 2) + "\n", { mode: 0o600 }); fs.chmodSync(cfgP, 0o600);
fs.writeFileSync(metaP, JSON.stringify(meta, null, 2) + "\n", { mode: 0o600 });
' && ok "✔ Claude 桌面 App 已配好(Code 标签页走同一个网关)" \
  || warn "⚠ 桌面 App 配置没写成(不影响命令行 claude)"

# ── 4c. 桌面 App 要的 Claude Code 二进制:预先放好 ────────────────────
# 🔴 10-13 本机实测的「App 发消息不回复」真因:App 每开一个会话都要先从
#    downloads.claude.ai 拉一份**它自己钉死版本**的 Claude Code(本机 Mac 和 Linux VM 各一份),
#    国内直连那个域名 ERR_CONNECTION_TIMED_OUT ⇒ 日志
#    `Host Claude Code binary not available` ⇒ 会话永远起不来,界面上就是"不回复"。
#    App 只认自己的 storageDir + `.verified`(里面写 manifest 的 sha256),不认 npm 装的那个
#    (npm 版是 plain 二进制,App 在 mac 上要 claude.app bundle;实测两者 sha 不同)。
#    ⇒ 从我们自己的镜像 cc.auto-link.com.cn/ccdl/ 取**官方原样**文件,按 App 的目录形状放好,
#    并用 App 内嵌 manifest 的 sha256 校验。版本号从 App 里读(App 升级后自动跟)。
APP_ASAR="/Applications/Claude.app/Contents/Resources/app.asar"
CCDL="https://cc.auto-link.com.cn/ccdl/claude-code-releases"
if [ "$OS" = darwin ] && [ -f "$APP_ASAR" ]; then
  say "→ 准备桌面 App 需要的 Claude Code 组件(约 100MB,首次会慢一点)..."
  CARHER_ASAR="$APP_ASAR" CARHER_UD="$APP_UD" CARHER_CCDL="$CCDL" CARHER_ARCH="$ARCH" node -e '
const fs=require("fs"),path=require("path"),crypto=require("crypto"),zlib=require("zlib"),cp=require("child_process"),https=require("https");
const b=fs.readFileSync(process.env.CARHER_ASAR).toString("latin1");
// App 内嵌 manifest:JSON.parse(\x27{"version":"2.1.x","manifest":{...},"baseUrl":...}\x27)
const m=b.match(/JSON\.parse\(\x27(\{"version":"(\d+\.\d+\.\d+)","manifest".*?)\x27\)/);
if(!m){console.error("读不到 App 内嵌的 Claude Code 版本(App 版本太新/太旧?)");process.exit(2);}
const d=JSON.parse(m[1]), V=d.manifest.version, P=d.manifest.platforms;
const host = "darwin-"+(process.env.CARHER_ARCH==="arm64"?"arm64":"x64");
const vm   = "linux-"+(process.env.CARHER_ARCH==="arm64"?"arm64":"x64");
const get=(u)=>new Promise((res,rej)=>{https.get(u,{headers:{"User-Agent":"carher-oneclick"}},r=>{
  if(r.statusCode!==200){rej(new Error(u+" HTTP "+r.statusCode));r.resume();return;}
  const bufs=[];r.on("data",x=>bufs.push(x));r.on("end",()=>res(Buffer.concat(bufs)));}).on("error",rej);});
const sha=(x)=>crypto.createHash("sha256").update(x).digest("hex");
const ok=(dir,want)=>{try{return fs.readFileSync(path.join(dir,".verified"),"utf8").trim()===want;}catch{return false;}};
(async()=>{
  // ① 本机这份(mac 上是 claude.app bundle)
  const hb=P[host]&&P[host].bundle; const hd=path.join(process.env.CARHER_UD,"claude-code",V);
  if(hb && !(ok(hd,hb.checksum) && fs.existsSync(path.join(hd,"claude.app/Contents/MacOS/claude")))){
    const z=await get(`${process.env.CARHER_CCDL}/${V}/${host}/claude.app.tar.zst`);
    if(sha(z)!==hb.checksum) throw new Error("本机组件 sha256 不对,拒绝安装");
    fs.rmSync(hd,{recursive:true,force:true}); fs.mkdirSync(hd,{recursive:true});
    // 用 Node 自带的 zstd 解(22.15+ 内置;上面已保证 Node>=22)。⛔ 不依赖系统 zstd:
    // 同事 Mac 默认没有,第一版就这么写的,只在装了 brew zstd 的机器上能过 = 假绿。
    if(typeof zlib.zstdDecompressSync!=="function") throw new Error("Node 不支持 zstd(需要 Node>=22.15)");
    const t=path.join(hd,".dl.tar"); fs.writeFileSync(t,zlib.zstdDecompressSync(z));
    cp.execFileSync("tar",["-xf",t,"-C",hd]);
    fs.unlinkSync(t);
    if(!fs.existsSync(path.join(hd,"claude.app/Contents/MacOS/claude"))) throw new Error("本机组件解压失败(需要 zstd)");
    fs.writeFileSync(path.join(hd,".verified"),hb.checksum);
    try{cp.execFileSync("xattr",["-dr","com.apple.quarantine",hd]);}catch{}
  }
  // ② Linux VM 那份(App 的隔离工作区里跑)
  const vp=P[vm]; const vd=path.join(process.env.CARHER_UD,"claude-code-vm",V);
  if(vp && !(ok(vd,vp.checksum) && fs.existsSync(path.join(vd,"claude")))){
    const z=await get(`${process.env.CARHER_CCDL}/${V}/${vm}/claude.zst`);
    if(sha(z)!==vp.checksum) throw new Error("VM 组件 sha256 不对,拒绝安装");
    fs.mkdirSync(vd,{recursive:true});
    fs.writeFileSync(path.join(vd,"claude"),zlib.zstdDecompressSync(z)); fs.chmodSync(path.join(vd,"claude"),0o755);
    fs.writeFileSync(path.join(vd,".verified"),vp.checksum);
  }
  console.log(V);
})().catch(e=>{console.error(e.message);process.exit(3);});
' >"$TMPDIR/carher-ccd.out" 2>"$TMPDIR/carher-ccd.err" \
    && ok "✔ 桌面 App 组件就绪(Claude Code $(cat "$TMPDIR/carher-ccd.out"))" \
    || warn "⚠ 桌面 App 组件没准备好:$(tail -1 "$TMPDIR/carher-ccd.err")。命令行 claude 不受影响。"
  rm -f "$TMPDIR/carher-ccd.out" "$TMPDIR/carher-ccd.err"
fi

# (会盖过配置的旧 export 在第 5 步测试之后统一处理:注释掉 + 备份,见下)
echo

# ── 5. 验证:真发一句 ────────────────────────────────────────────────
say "→ 测试一下 ..."
A=$((RANDOM % 9000 + 1000)); B=$((RANDOM % 9000 + 1000))
# 只认 settings.json:把会盖过它的 shell 变量全清掉再测(测的是「新开终端」会拿到的东西)
UNSET_ARGS=""
for v in ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_API_KEY CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC \
         ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
         ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_DEFAULT_FABLE_MODEL ANTHROPIC_SMALL_FAST_MODEL; do
  UNSET_ARGS="$UNSET_ARGS -u $v"
done
# shellcheck disable=SC2086
OUT="$(env $UNSET_ARGS "$CLAUDE_BIN" --print "What is $A+$B? Answer with just the number." </dev/null 2>&1 | tail -n1 || true)"
if printf '%s' "$OUT" | grep -q "$((A + B))"; then
  ok "✔ 通了(问 $A+$B,答 $OUT)"
else
  warn "⚠ 测试没拿到预期回答(回:${OUT:-空})。配置已写好,先新开一个终端试试 claude。"
fi
# 测试是在「清掉 shell 变量」的条件下过的;如果 rc 里有会盖过它的 export,新开终端照样会坏 ⇒
# 不能只提醒,要把它们**注释掉**(加标记,可还原),否则用户一开终端就 403 而屏幕上只写"完成"。
FIXED=0
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
  [ -f "$rc" ] || continue
  if grep -qE '^[[:space:]]*export[[:space:]]+(ANTHROPIC_(AUTH_TOKEN|BASE_URL|API_KEY|MODEL|DEFAULT_(OPUS|SONNET|HAIKU|FABLE)_MODEL|SMALL_FAST_MODEL)|CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC)=' "$rc"; then
    cp -p "$rc" "$rc.bak-carher-$(date +%Y%m%d-%H%M%S)"
    tmp="$(mktemp)"
    awk '
      /# >>> carher-claude >>>/{s=1} /# <<< carher-claude <<</{s=0; print; next}
      !s && /^[[:space:]]*export[[:space:]]+(ANTHROPIC_(AUTH_TOKEN|BASE_URL|API_KEY|MODEL|DEFAULT_(OPUS|SONNET|HAIKU|FABLE)_MODEL|SMALL_FAST_MODEL)|CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC)=/ {
        print "# [carher-claude 已注释,会盖过 ~/.claude/settings.json] " $0; next }
      { print }' "$rc" > "$tmp" && cat "$tmp" > "$rc"
    rm -f "$tmp"; FIXED=1
    warn "↳ $rc 里会盖过配置的旧 export 已注释掉(原文件已备份成 $rc.bak-carher-*)"
  fi
done
[ "$FIXED" = 1 ] && warn "↳ 请**关掉现在这个终端,新开一个**再用 claude(旧变量还留在当前窗口里)"

echo
ok "==================================================="
ok "   完成！"
ok "==================================================="
echo "  新开一个终端窗口,进到你的项目文件夹,输入:  claude"
echo "  换模型:在 Claude Code 里输入  /model"
echo
