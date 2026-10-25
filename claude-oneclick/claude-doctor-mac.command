#!/usr/bin/env bash
# CarHer Claude Code 自检 (macOS / Linux) —— 只读,不改任何配置
# 由 build.sh 生成，别直接改；源在 claude-oneclick/src/
#
# 给运维用的一张"判据表":每一格都能单独判真假,key 只打长度 + sha256 前 8 位,明文不上屏。
# 结尾的「结论」只列查出来的问题和对应的修法;报告同时存到桌面并复制到剪贴板,用户直接粘给运维。
set +e
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
WANT_BASE="https://cc.auto-link.com.cn/pro"
GW="${CARHER_DOCTOR_BASE:-$WANT_BASE}"   # 实际去打的地址(测试时指向本机假网关);判配置对不对仍用 WANT_BASE
CLAUDE_MIN="2.1.257"
TS="$(date +%Y%m%d-%H%M%S)"
RED=(); YEL=()
red() { RED+=("$1"); }
yel() { YEL+=("$1"); }
P() { printf '%s\n' "$*"; }
mask() { sed -E 's/sk-[A-Za-z0-9_-]{6,}/sk-***/g'; }
sha8() { if command -v shasum >/dev/null 2>&1; then printf '%s' "$1" | shasum -a 256 | cut -c1-8; else printf '%s' "$1" | sha256sum | cut -c1-8; fi; }
keyfp() {
  [ -n "$1" ] || { printf '无'; return; }
  local bad; bad=$(printf '%s' "$1" | LC_ALL=C tr -d '!-~' | wc -c | tr -d ' ')
  printf 'len=%s sha=%s 异常字符=%s' "${#1}" "$(sha8 "$1")" "$bad"
}
keybad() { [ -n "$1" ] && [ "$(printf '%s' "$1" | LC_ALL=C tr -d '!-~' | wc -c | tr -d ' ')" != 0 ]; }
ver_ge() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -n1)" = "$2" ]; }
# 读 JSON 里的一个字段(用 node;没有 node 就返回空)。路径用点分,如 env.ANTHROPIC_BASE_URL
jget() { command -v node >/dev/null 2>&1 || return 0
  CARHER_F="$1" CARHER_K="$2" node -e 'try{let v=JSON.parse(require("fs").readFileSync(process.env.CARHER_F,"utf8").replace(/^﻿/,""));for(const k of process.env.CARHER_K.split("."))v=v==null?undefined:v[k];if(v!==undefined)process.stdout.write(typeof v==="string"?v:JSON.stringify(v))}catch(e){process.stdout.write("<JSON坏了>")}' 2>/dev/null; }

run() {
P "=== CarHer Claude Code 自检 ==="
P "时间    : $(date '+%F %T %z')"
P "系统    : $(uname -s) $(uname -r) $(uname -m)"
P "配置目录: $CLAUDE_HOME"

P ""; P "--- 1) 命令行 claude ---"
for d in "$HOME/.carher-npm-global/bin" "$HOME/.carher-node/bin"; do [ -d "$d" ] && PATH="$d:$PATH"; done
if command -v node >/dev/null 2>&1; then P "node    : $(node --version 2>/dev/null)"; else P "node    : 没找到"; fi
CV=""
if command -v claude >/dev/null 2>&1; then
  CV=$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)
  P "claude  : $(command -v claude)  $CV"
  [ -n "$CV" ] && ! ver_ge "$CV" "$CLAUDE_MIN" && red "Claude Code $CV 太旧(需要 ≥ $CLAUDE_MIN,否则新模型报 400):重新跑一次 claude-mac.command"
else
  P "claude  : 没找到"
  yel "终端里找不到 claude:关掉终端新开一个;还不行就重新跑一次 claude-mac.command"
fi
grep -ls '>>> carher-claude >>>' "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" 2>/dev/null | sed 's/^/PATH 块 : /'

P ""; P "--- 2) settings.json ---"
S="$CLAUDE_HOME/settings.json"; KEY=""
if [ -f "$S" ]; then
  P "settings.json : 有 ($(wc -c <"$S" | tr -d ' ') 字节,改于 $(date -r "$S" '+%F %T' 2>/dev/null))"
  if command -v node >/dev/null 2>&1; then
    BASE=$(jget "$S" env.ANTHROPIC_BASE_URL); KEY=$(jget "$S" env.ANTHROPIC_API_KEY); TOK=$(jget "$S" env.ANTHROPIC_AUTH_TOKEN)
    DISC=$(jget "$S" env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY); OFF=$(jget "$S" env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC)
    MODEL=$(jget "$S" model); HAIKU=$(jget "$S" env.ANTHROPIC_DEFAULT_HAIKU_MODEL); NPICK=$(jget "$S" modelPicker.options | grep -o '"model"' | wc -l | tr -d ' ')
    case "$BASE$KEY" in *"<JSON坏了>"*) red "settings.json 不是合法 JSON:把它发给运维,或删掉后重新跑 claude-mac.command";; esac
    P "  ANTHROPIC_BASE_URL = ${BASE:-<没写>}"
    P "  ANTHROPIC_API_KEY  : $(keyfp "$KEY")"
    [ -n "$TOK" ] && P "  ANTHROPIC_AUTH_TOKEN : $(keyfp "$TOK")"
    P "  模型发现开关       = ${DISC:-<没写>}"
    P "  model              = ${MODEL:-<默认>}"
    P "  modelPicker        = ${NPICK} 个"
    # 10-25:菜单只放我们按 key 生成的那份(每个模型一次);每次开 Claude Code 后台自动刷
    RPL=$(jget "$S" modelPicker.replaceBuiltInOptions); HOOK=$(jget "$S" hooks.SessionStart | grep -c carher_models_refresh)
    P "  菜单去重           = $([ "$RPL" = true ] && echo 是 || echo 否)"
    P "  开机自动刷菜单     = $([ "${HOOK:-0}" -gt 0 ] && echo 已挂 || echo 没挂)"
    [ "$RPL" = true ] || yel "/model 菜单里同一个模型会出现两次、还有画图模型:重新跑一次 claude-mac.command"
    [ "${HOOK:-0}" -gt 0 ] || yel "没挂开机自动刷菜单(key 新开的模型要手动说「刷模型」):重新跑一次 claude-mac.command"
    [ "$BASE" = "$WANT_BASE" ] || red "settings.json 没指向公司网关(ANTHROPIC_BASE_URL=${BASE:-空}):重新跑一次 claude-mac.command"
    [ -n "$KEY" ] || red "settings.json 里没有 key:重新跑一次 claude-mac.command"
    [ -n "$TOK" ] && red "同时有 AUTH_TOKEN 和 API_KEY(/model 里只剩几个 Claude):重新跑一次 claude-mac.command"
    [ -n "$OFF" ] && red "开了 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC(会关掉模型发现):重新跑一次 claude-mac.command"
    case "$MODEL" in claude-router-*) red "model 存的是旧编码名 $MODEL(新开窗口会报 isn't recognized):重新跑一次 claude-mac.command";; esac
  else
    P "  (没有 node,读不了 JSON)"
  fi
else
  P "settings.json : 没有"
  red "没有 $S ⇒ 一键配置没跑成功:重新跑一次 claude-mac.command"
fi
keybad "$KEY" && red "key 里有看不见的字符(多半从飞书复制带进来的):去 key 表重新复制整格,重新跑 claude-mac.command"
[ -f "$CLAUDE_HOME/cache/gateway-models.json" ] && P "模型缓存      : $(grep -o '"id"' "$CLAUDE_HOME/cache/gateway-models.json" | wc -l | tr -d ' ') 个 (改于 $(date -r "$CLAUDE_HOME/cache/gateway-models.json" '+%F %T' 2>/dev/null))"
# 会盖过 settings.json 的 shell 变量(新开终端会带上)
OV=""
for v in ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_MODEL CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC; do
  eval "x=\${$v:-}"; [ -n "$x" ] && OV="$OV $v"
done
RC=$(grep -lE '^[[:space:]]*export[[:space:]]+(ANTHROPIC_(AUTH_TOKEN|BASE_URL|API_KEY|MODEL)|CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC)=' "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile" 2>/dev/null)
[ -n "$OV" ] && P "当前终端里的覆盖变量:$OV"
[ -n "$RC" ] && P "rc 文件里的覆盖 export:$(echo $RC)"
[ -n "$RC" ] && red "$(echo $RC) 里有 export ANTHROPIC_… 会盖过配置:重新跑一次 claude-mac.command(会自动注释掉并备份)"
[ -z "$RC" ] && [ -n "$OV" ] && yel "当前这个终端带着旧变量($OV):关掉终端新开一个再用 claude"

if [ -f "$CLAUDE_HOME/carher/carher_image.js" ] && [ -f "$CLAUDE_HOME/skills/carher-image/SKILL.md" ]; then P "画图工具      : 已装"
else P "画图工具      : 没装"; yel "画图工具没装上:下载最新的 claude-mac.command 重跑一次"; fi

P ""; P "--- 3) 桌面 App ---"
APP="${CARHER_DOCTOR_APP:-/Applications/Claude.app}"
if [ "$(uname -s)" = Darwin ] && [ -d "$APP" ]; then
  P "App     : $APP  版本 $(defaults read "$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null)"
  if pgrep -f "$APP/Contents/MacOS/Claude" >/dev/null 2>&1; then P "运行中  : 是"; else P "运行中  : 否"; fi
  UD="$HOME/Library/Application Support/Claude-3p"; LIB="$UD/configLibrary"
  if [ -f "$LIB/_meta.json" ] && command -v node >/dev/null 2>&1; then
    AID=$(jget "$LIB/_meta.json" appliedId)
    CF="$LIB/$AID.json"
    if [ -n "$AID" ] && [ -f "$CF" ]; then
      PV=$(jget "$CF" inferenceProvider); GB=$(jget "$CF" inferenceGatewayBaseUrl); GK=$(jget "$CF" inferenceGatewayApiKey)
      P "App 网关配置 : provider=$PV  base=$GB  key: $(keyfp "$GK")"
      [ "$PV" = gateway ] && [ "$GB" = "$WANT_BASE" ] || red "桌面 App 没指向公司网关:完全退出 App,重新跑一次 claude-mac.command"
      [ -n "$KEY" ] && [ -n "$GK" ] && [ "$KEY" != "$GK" ] && red "命令行和桌面 App 用的不是同一把 key:重新跑一次 claude-mac.command"
      # 10-25:这项没写 ⇒ App 给 Code/Cowork 会话开「严格沙箱」且一个外网都不放 ⇒ npm install 失败(终端正常)
      EG=$(jget "$CF" coworkEgressAllowedHosts)
      P "App 外网放行 : ${EG:-(没设 ⇒ 严格沙箱,命令连不上外网)}"
      case "$EG" in *'"*"'*) :;; *) red "桌面 App 里跑命令连不上外网(npm install 会失败,设置页显示严格沙箱):完全退出 App,重新跑一次 claude-mac.command";; esac
    else
      P "App 网关配置 : _meta.json 指向的配置不存在"; red "桌面 App 配置缺了:重新跑一次 claude-mac.command"
    fi
  else
    P "App 网关配置 : 没有 ($LIB)"; red "桌面 App 没配置过:完全退出 App,重新跑一次 claude-mac.command"
  fi
  # App 钉死的 Claude Code 版本 + 本机有没有预置好(没有 ⇒ 发消息不回)
  ASAR="$APP/Contents/Resources/app.asar"
  PIN=$(LC_ALL=C grep -aoE '\{"version":"[0-9]+\.[0-9]+\.[0-9]+","manifest"' "$ASAR" 2>/dev/null | head -n1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
  if [ -n "$PIN" ]; then
    D="$UD/claude-code/$PIN"
    if [ -f "$D/.verified" ] && [ -x "$D/claude.app/Contents/MacOS/claude" ]; then P "App 组件     : Claude Code $PIN 已就绪"
    else P "App 组件     : Claude Code $PIN 没准备好"
      red "桌面 App 需要的组件(Claude Code $PIN)没准备好 ⇒ 发消息不回/很慢:完全退出 App,重新跑一次 claude-mac.command"; fi
    MIR=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://cc.auto-link.com.cn/ccdl/claude-code-releases/$PIN/manifest.json")
    P "公司镜像     : $PIN → HTTP $MIR"
    [ "$MIR" = 200 ] || red "公司镜像里还没有 App 要的 Claude Code $PIN(App 刚自动升级过):把这份报告发给运维"
  else
    P "App 组件     : 读不到 App 钉死的版本"
  fi
  LOG="$HOME/Library/Logs/Claude-3p/main.log"
  if [ -f "$LOG" ]; then
    P "App 日志 ($LOG) 最近的关键行:"
    grep -E '3P mode active|Model discovery|Host Claude Code binary not available|still initializing|\[error\]' "$LOG" | tail -n 8 | cut -c1-240 | mask | sed 's/^/  /'
    tail -n 400 "$LOG" | grep -q 'Host Claude Code binary not available' && red "App 日志最近有「Host Claude Code binary not available」⇒ 组件没准备好:完全退出 App,重新跑一次 claude-mac.command"
  fi
elif [ "$(uname -s)" = Darwin ]; then
  P "App     : 没装(只用命令行可以忽略)"
fi

P ""; P "--- 4) 网络 ---"
PX=""
for v in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy; do
  eval "pv=\${$v:-}"; [ -n "$pv" ] && P "代理变量 ${v} = ${pv}" && PX="${PX} ${v}"
done
[ -n "$PX" ] && yel "终端里设了代理(${PX# }):网关通就不用管;连不上或很慢时先关掉 VPN/代理再试"
if [ "$(uname -s)" = Darwin ] && scutil --proxy 2>/dev/null | grep -qE 'HTTPSEnable : 1|ProxyAutoConfigEnable : 1'; then
  P "系统代理      : 开着"; yel "系统代理/VPN 开着:连不上或很慢时先关掉再试"
fi
NET=$(curl -s -o /dev/null -w '%{http_code} %{remote_ip} %{time_total}s' --max-time 15 "$GW/v1/models" 2>&1)
P "网关(不带 key): $NET   (401 = 网关通;000 = 连不上)"
case "$NET" in 000*) red "连不上公司网关:检查网络,关掉 VPN/代理后再试";; esac

P ""; P "--- 5) 用这把 key 实打 ---"
if [ -z "$KEY" ]; then
  P "(没有 key,跳过)"
else
  INFO=$(curl -s --max-time 20 -H "Authorization: Bearer $KEY" "$GW/key/info" 2>/dev/null)
  ALIAS=$(printf '%s' "$INFO" | sed -n 's/.*"key_alias" *: *"\([^"]*\)".*/\1/p' | head -n1)
  P "key 名字      : ${ALIAS:-<查不到>}"
  MC=$(curl -s -w '\n%{http_code}' --max-time 20 -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' "$GW/v1/models" 2>/dev/null)
  CODE=$(printf '%s' "$MC" | tail -n1); BODY=$(printf '%s' "$MC" | sed '$d')
  N=$(printf '%s' "$BODY" | grep -o '"id" *: *"[^"]*"' | wc -l | tr -d ' ')
  P "可用模型      : HTTP $CODE,$N 个"
  case "$CODE" in
    401|403) red "key 无效(HTTP $CODE):去 key 表复制整格,重新跑一次 claude-mac.command";;
    200) :;;
    *) red "拉模型列表失败(HTTP $CODE):把这份报告发给运维";;
  esac
  # opus / sonnet / haiku 是 Claude Code 的简称,发出去前会换成真名 ⇒ 实打要用真名(10-19 拿 sonnet 直打 403 是误报)
  M="${MODEL:-claude-opus-5-5}"; M="${M%%\[*}"
  HK="${HAIKU:-claude-haiku-5-5}"
  case "$M" in opus|default) M=claude-opus-5-5;; sonnet) M=claude-sonnet-5-5;; haiku) M="$HK";; esac
  if [ "$CODE" = 200 ]; then
    printf '%s' "$BODY" | grep -q "\"$M\"" && P "  $M : 已开通" || { P "  $M : 没开通"; yel "这把 key 没开通 ${M}:用 /model 换一个,或找运维开通"; }
    # 后台小任务(起标题、子任务)用 haiku。Claude Code 2.1.30x 默认要 claude-haiku-5-5;key 没开通 ⇒ 时不时报错
    if printf '%s' "$BODY" | grep -q "\"$HK\""; then P "  后台小任务模型 ${HK} : 已开通"
    else P "  后台小任务模型 ${HK} : 没开通${HAIKU:-(Claude Code 默认值)}"
      red "后台小任务用的 ${HK} 你的 key 没有 ⇒ 时不时报错:下载最新的 claude-mac.command 重跑一次(会改成你 key 里有的 haiku)"; fi
    R=$(curl -s -w '\n%{http_code} %{time_total}s' --max-time 60 -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
      "$GW/v1/messages" -d "{\"model\":\"$M\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with only: OK\"}]}" 2>/dev/null)
    RC=$(printf '%s' "$R" | tail -n1); RB=$(printf '%s' "$R" | sed '$d')
    TXT=$(printf '%s' "$RB" | grep -o '"text" *: *"[^"]*"' | head -n1 | sed 's/.*"text" *: *"//;s/"$//' | cut -c1-60)
    P "实打 $M : HTTP $RC  回答 ${TXT:-<无文字>}"
    case "$RC" in 200*) :;; *) P "  返回: $(printf '%s' "$RB" | head -c 300 | mask)"; red "用 $M 实打失败(HTTP ${RC%% *}):把这份报告发给运维";; esac
  fi
fi

P ""; P "=== 结论 ==="
if [ ${#RED[@]} -eq 0 ] && [ ${#YEL[@]} -eq 0 ]; then P "✅ 没查出问题。还有问题就把这份报告和出错时间、模型名一起发给运维。"; fi
for x in "${RED[@]}"; do P "🔴 $x"; done
for x in "${YEL[@]}"; do P "⚠️  $x"; done
}

OUT_DIR="$HOME/Desktop"; [ -d "$OUT_DIR" ] || OUT_DIR="$HOME"
REPORT="$OUT_DIR/claude-自检-$TS.txt"
run 2>&1 | tee "$REPORT"
echo
if command -v pbcopy >/dev/null 2>&1 && pbcopy <"$REPORT" 2>/dev/null; then
  echo "📋 报告已复制到剪贴板,也存在:$REPORT —— 直接粘给运维即可(里面没有 key)"
else
  echo "📋 报告存在:$REPORT —— 把这个文件发给运维即可(里面没有 key)"
fi
