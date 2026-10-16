#!/usr/bin/env bash
# CarHer Codex 自检 (macOS / Linux) —— 只读,不改任何配置
# 由 build.sh 生成，别直接改；源在 codex-oneclick/src/
#
# 给运维用的一张"判据表":每一格都能单独判真假,key 只打长度 + sha256 前 8 位,明文不上屏。
# 结尾的「结论」只列查出来的问题和对应的修法;报告同时存到桌面并复制到剪贴板,用户直接粘给运维。
set +e
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
BASE_URL="${CARHER_DOCTOR_BASE:-https://cc.auto-link.com.cn/pro/v1}"
WANT_PROVIDER="carher_prod"
WANT_BASE="https://cc.auto-link.com.cn/pro/v1"
TS="$(date +%Y%m%d-%H%M%S)"
RED=(); YEL=()
red() { RED+=("$1"); }
yel() { YEL+=("$1"); }
P() { printf '%s\n' "$*"; }
mask() { sed -E 's/sk-[A-Za-z0-9_-]{6,}/sk-***/g'; }
sha8() { if command -v shasum >/dev/null 2>&1; then printf '%s' "$1" | shasum -a 256 | cut -c1-8; else printf '%s' "$1" | sha256sum | cut -c1-8; fi; }
# key 的"指纹":长度、sha256 前 8 位、非可见 ASCII 字节数(从飞书复制常带零宽字符,带上请求发不出去)
keyfp() {
  [ -n "$1" ] || { printf '无'; return; }
  local bad; bad=$(printf '%s' "$1" | LC_ALL=C tr -d '!-~' | wc -c | tr -d ' ')
  printf 'len=%s sha=%s 异常字符=%s' "${#1}" "$(sha8 "$1")" "$bad"
}
keybad() { [ "$(printf '%s' "$1" | LC_ALL=C tr -d '!-~' | wc -c | tr -d ' ')" != 0 ]; }

run() {
P "=== CarHer Codex 自检 ==="
P "时间    : $(date '+%F %T %z')"
P "系统    : $(uname -s) $(uname -r) $(uname -m)"
P "用户目录: $CODEX_HOME"

P ""; P "--- 1) 命令行 codex ---"
for d in "$HOME/.carher-node/bin" "$HOME/.carher-npm-global/bin"; do [ -d "$d" ] && PATH="$d:$PATH"; done
if command -v codex >/dev/null 2>&1; then
  P "codex   : $(command -v codex)  $(codex --version 2>/dev/null | head -n1)"
else
  P "codex   : 没找到"
  yel "终端里找不到 codex:关掉终端新开一个;还不行就重新跑一次 codex-mac.command"
fi
if command -v node >/dev/null 2>&1; then P "node    : $(node --version 2>/dev/null)"; else P "node    : 没找到"; fi
grep -ls '>>> carher-codex >>>' "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" 2>/dev/null | sed 's/^/PATH 块 : /'

P ""; P "--- 2) 桌面 App ---"
APP=""
for a in "/Applications/Codex.app" "/Applications/OpenAI Codex.app" "$HOME/Applications/Codex.app"; do [ -d "$a" ] && APP="$a" && break; done
if [ -n "$APP" ]; then
  P "App     : $APP  版本 $(defaults read "$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null)"
  if pgrep -f "$APP/Contents/MacOS" >/dev/null 2>&1; then P "运行中  : 是"; else P "运行中  : 否"; fi
else
  P "App     : 没装(只用命令行可以忽略)"
fi

P ""; P "--- 3) 配置文件 ---"
CFG="$CODEX_HOME/config.toml"; AUTH="$CODEX_HOME/auth.json"
K1=""; K2=""
if [ -f "$CFG" ]; then
  PROV=$(sed -n 's/^model_provider *= *"\(.*\)".*/\1/p' "$CFG" | head -n1)
  MODEL=$(sed -n 's/^model *= *"\(.*\)".*/\1/p' "$CFG" | head -n1)
  OBURL=$(sed -n 's/^openai_base_url *= *"\(.*\)".*/\1/p' "$CFG" | head -n1)
  # 只取 [model_providers.<当前 provider>] 这一段里的 base_url,别把别的 provider 的抓过来
  BURL=$(awk -v t="[model_providers.$PROV]" '$0==t{f=1;next} /^\[/{f=0} f&&/^base_url/{sub(/^base_url *= *"/,"");sub(/".*/,"");print;exit}' "$CFG")
  K1=$(awk -v t="[model_providers.$PROV]" '$0==t{f=1;next} /^\[/{f=0} f&&/^experimental_bearer_token/{sub(/^experimental_bearer_token *= *"/,"");sub(/".*/,"");print;exit}' "$CFG")
  P "config.toml   : 有 ($(wc -c <"$CFG" | tr -d ' ') 字节,改于 $(date -r "$CFG" '+%F %T' 2>/dev/null))"
  P "  model_provider = ${PROV:-<没写>}"
  P "  model          = ${MODEL:-<没写>}"
  P "  provider base_url = ${BURL:-<没写>}"
  P "  openai_base_url    = ${OBURL:-<没写>}"
  P "  key(config)    : $(keyfp "$K1")"
  grep -q '^model_catalog_json' "$CFG" && P "  model_catalog_json = $(sed -n 's/^model_catalog_json *= *//p' "$CFG" | head -n1)"
  # 两种写法都能走网关:一键脚本写的 model_provider=carher_prod;或不写 provider、靠 openai_base_url + auth.json
  if [ -n "$PROV" ]; then
    [ "$BURL" = "$WANT_BASE" ] || red "当前 provider「${PROV}」不指向公司网关(base_url=${BURL:-空}):重新跑一次 codex-mac.command"
  else
    [ "$OBURL" = "$WANT_BASE" ] || red "config.toml 没指向公司网关:重新跑一次 codex-mac.command"
  fi
  [ "$PROV" = "$WANT_PROVIDER" ] || P "  (不是一键脚本写的标准配置;有问题时先重新跑一次 codex-mac.command,原配置会自动备份)"
else
  P "config.toml   : 没有"
  red "没有 $CFG ⇒ 一键配置没跑成功:重新跑一次 codex-mac.command"
fi
if [ -f "$AUTH" ]; then
  MODE=$(sed -n 's/.*"auth_mode" *: *"\([^"]*\)".*/\1/p' "$AUTH" | head -n1)
  K2=$(sed -n 's/.*"OPENAI_API_KEY" *: *"\([^"]*\)".*/\1/p' "$AUTH" | head -n1)
  P "auth.json     : auth_mode=${MODE:-<没写>}  key(auth) : $(keyfp "$K2")"
  [ "$MODE" = "apikey" ] || red "auth.json 不是 apikey 模式(App 会要求登录 ChatGPT):重新跑一次 codex-mac.command"
else
  P "auth.json     : 没有"
  red "没有 auth.json ⇒ 桌面 App 用不了:重新跑一次 codex-mac.command"
fi
[ -n "$K1$K2" ] || red "config.toml 和 auth.json 里都没有 key:重新跑一次 codex-mac.command"
if [ -n "$K1" ] && [ -n "$K2" ] && [ "$K1" != "$K2" ]; then
  red "config.toml 和 auth.json 里是两把不同的 key(命令行和 App 会表现不一样):重新跑一次 codex-mac.command"
fi
KEY="${K1:-$K2}"
keybad "$KEY" && red "key 里有看不见的字符(多半从飞书复制带进来的):去 key 表重新复制整格,重新跑 codex-mac.command"
if [ -n "${OPENAI_API_KEY:-}" ]; then
  P "环境变量 OPENAI_API_KEY : $(keyfp "$OPENAI_API_KEY")$([ "$OPENAI_API_KEY" = "$KEY" ] && echo ' (和配置里同一把)' || echo ' (和配置里不是同一把)')"
  [ "$OPENAI_API_KEY" = "$KEY" ] || yel "终端里设了另一把 OPENAI_API_KEY,某些工具会优先用它:不需要的话从 ~/.zshrc 里删掉那行"
fi
if [ -f "$CODEX_HOME/carher-models.json" ]; then
  P "模型列表      : $(grep -c '"slug"' "$CODEX_HOME/carher-models.json") 个 (carher-models.json 改于 $(date -r "$CODEX_HOME/carher-models.json" '+%F %T' 2>/dev/null))"
else
  P "模型列表      : 没有 carher-models.json"
  yel "模型列表没同步(App 下拉里只有自带的几个 GPT,找不到 key 里的模型):下载最新的 codex-mac.command 重跑一次"
fi
[ -f "$CODEX_HOME/carher-models-sync.log" ] && { P "  同步日志最后 3 行:"; tail -n 3 "$CODEX_HOME/carher-models-sync.log" | mask | sed 's/^/    /'; }
if [ -f "$CODEX_HOME/carher_image.py" ] && grep -q 'CARHER-IMAGE-BEGIN' "$CODEX_HOME/AGENTS.md" 2>/dev/null; then
  P "画图工具      : 已装"
else
  P "画图工具      : 没装"
  yel "画图工具没装上(多半没有可用的 Python 3):装好 Python 3 后重新跑一次 codex-mac.command"
fi
ls -d "$CODEX_HOME"/backup-carher-* >/dev/null 2>&1 && P "配置备份      : $(ls -d "$CODEX_HOME"/backup-carher-* | wc -l | tr -d ' ') 份,最新 $(ls -d "$CODEX_HOME"/backup-carher-* | tail -n1)"

P ""; P "--- 4) 网络 ---"
for v in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy; do
  eval "pv=\${$v:-}"; [ -n "$pv" ] && P "代理变量 $v = $pv" && yel "终端里设了代理 $v:连公司网关不需要代理,装和用的时候先关掉 VPN/代理"
done
if [ "$(uname -s)" = Darwin ] && scutil --proxy 2>/dev/null | grep -qE 'HTTPSEnable : 1|ProxyAutoConfigEnable : 1'; then
  P "系统代理      : 开着"; yel "系统代理/VPN 开着:连不上或很慢时先关掉再试"
fi
NET=$(curl -s -o /dev/null -w '%{http_code} %{remote_ip} %{time_total}s' --max-time 15 "$BASE_URL/models" 2>&1)
P "网关(不带 key): $NET   (401 = 网关通;000 = 连不上)"
case "$NET" in 000*) red "连不上公司网关:检查网络,关掉 VPN/代理后再试";; esac

P ""; P "--- 5) 用这把 key 实打 ---"
if [ -z "$KEY" ]; then
  P "(没有 key,跳过)"
else
  INFO=$(curl -s --max-time 20 -H "Authorization: Bearer $KEY" "${BASE_URL%/v1}/key/info" 2>/dev/null)
  ALIAS=$(printf '%s' "$INFO" | sed -n 's/.*"key_alias" *: *"\([^"]*\)".*/\1/p' | head -n1)
  P "key 名字      : ${ALIAS:-<查不到>}"
  MC=$(curl -s -w '\n%{http_code}' --max-time 20 -H "Authorization: Bearer $KEY" "$BASE_URL/models" 2>/dev/null)
  CODE=$(printf '%s' "$MC" | tail -n1); BODY=$(printf '%s' "$MC" | sed '$d')
  N=$(printf '%s' "$BODY" | grep -o '"id" *: *"[^"]*"' | wc -l | tr -d ' ')
  P "可用模型      : HTTP $CODE,$N 个"
  case "$CODE" in
    401|403) red "key 无效(HTTP $CODE):去 key 表复制整格,重新跑一次 codex-mac.command";;
    200) :;;
    *) red "拉模型列表失败(HTTP $CODE):把这份报告发给运维";;
  esac
  # 默认模型不在这把 key 里 ⇒ 每一轮都会 403;直接告诉他该换成哪个(10-16 fenglili 就是这个)
  if [ "$CODE" = 200 ] && ! printf '%s' "$BODY" | grep -q "\"${MODEL:-gpt-5.6-terra}\""; then
    FIRST=$(printf '%s' "$BODY" | grep -o '"id" *: *"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' | grep -vE 'image|imagine|^codex-auto-review$' | head -n1)
    [ -n "$FIRST" ] && red "默认模型 ${MODEL:-gpt-5.6-terra} 不在这把 key 里(每一轮都会 403)。下载最新的 codex-mac.command 重跑一次会自动改成 ${FIRST};或在 Codex 里用 /model 选 ${FIRST}"
  fi
  for m in ${MODEL:-gpt-5.6-terra} muse-image; do
    if printf '%s' "$BODY" | grep -q "\"$m\""; then P "  $m : 已开通"; else P "  $m : 没开通"
      [ "$CODE" = 200 ] && yel "这把 key 没开通 $m:找运维开通,或换一个模型"; fi
  done
  if [ "$CODE" = 200 ]; then
    M=${MODEL:-gpt-5.6-terra}
    R=$(curl -s -w '\n%{http_code} %{time_total}s' --max-time 60 -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
      "$BASE_URL/responses" -d "{\"model\":\"$M\",\"input\":\"Reply with only: OK\",\"max_output_tokens\":400}" 2>/dev/null)
    RC=$(printf '%s' "$R" | tail -n1); RB=$(printf '%s' "$R" | sed '$d')
    # 推理模型先出 reasoning 段,真回答在 type=message 里;有 node 就精确取,没有就粗取
    if command -v node >/dev/null 2>&1; then
      TXT=$(printf '%s' "$RB" | node -e 'let b="";process.stdin.on("data",c=>b+=c).on("end",()=>{try{const d=JSON.parse(b);const t=(d.output||[]).filter(o=>o.type==="message").flatMap(o=>o.content||[]).map(c=>c.text||"").join("");process.stdout.write(t.slice(0,60))}catch(e){}})' 2>/dev/null)
    else
      TXT=$(printf '%s' "$RB" | grep -o '"output_text"[^}]*' | grep -o '"text" *: *"[^"]*"' | head -n1 | sed 's/^"text" *: *"//;s/"$//' | cut -c1-60)
    fi
    P "实打 $M : HTTP $RC  回答 ${TXT:-<无文字>}"
    case "$RC" in 200*) :;; *) P "  返回: $(printf '%s' "$RB" | head -c 300 | mask)"; red "用默认模型 $M 实打失败(HTTP ${RC%% *}):把这份报告发给运维";; esac
  fi
fi

P ""; P "--- 6) 桌面 App 最近的报错 ---"
LOGD="$HOME/Library/Logs/com.openai.codex"
# 🔴 目录不存在时 find 输出为空,`ls -t $(空)` 会列出**当前目录** ⇒ 先判空
LF=""; [ -d "$LOGD" ] && LF=$(find "$LOGD" -type f -name '*.log' -exec ls -t {} + 2>/dev/null | head -n1)
if [ -n "$LF" ]; then
  P "日志: $LF"
  grep -E 'Z error ' "$LF" | tail -n 6 | cut -c1-260 | mask | sed 's/^/  /'
  grep -qE 'Z error ' "$LF" || P "  (没有 error 级别的行)"
else
  P "(没找到 App 日志)"
fi

P ""; P "=== 结论 ==="
if [ ${#RED[@]} -eq 0 ] && [ ${#YEL[@]} -eq 0 ]; then P "✅ 没查出问题。还有问题就把这份报告和出错时间、模型名一起发给运维。"; fi
for x in "${RED[@]}"; do P "🔴 $x"; done
for x in "${YEL[@]}"; do P "⚠️  $x"; done
}

OUT_DIR="$HOME/Desktop"; [ -d "$OUT_DIR" ] || OUT_DIR="$HOME"
REPORT="$OUT_DIR/codex-自检-$TS.txt"
run 2>&1 | tee "$REPORT"
echo
if command -v pbcopy >/dev/null 2>&1 && pbcopy <"$REPORT" 2>/dev/null; then
  echo "📋 报告已复制到剪贴板,也存在:$REPORT —— 直接粘给运维即可(里面没有 key)"
else
  echo "📋 报告存在:$REPORT —— 把这个文件发给运维即可(里面没有 key)"
fi
