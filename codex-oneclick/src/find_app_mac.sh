# ── 找 Claude 桌面 App(mac)。安装器和自检共用一份源,别各写一份 ──
# 🔴 11-03 buyitian 实机:App 明明装着,自检却报「没装(只用命令行可以忽略)」 ——
#    因为这里原来写死 `/Applications/Claude.app`。装在别处(个人 ~/Applications、
#    或自己改过 .app 名字)就一律看不见,而**误报"没装"比不报更糟**:
#    用户按提示去装一遍,或者干脆以为自己该忽略,真问题(App 没配网关 / 组件没预置
#    ⇒ 发消息不回)被这句话盖住。安装器那边同一处写死还会让组件预置整段静默跳过。
# ⇒ 按 **bundle id** 找,不认死路径、也不认 .app 的名字。
#    CARHER_DOCTOR_APP 可强制指定;CARHER_APP_ROOTS 只给台架用。
CARHER_CLAUDE_BUNDLE_ID=com.anthropic.claudefordesktop
CARHER_CODEX_BUNDLE_ID=com.openai.codex
# 11-04:网页一行命令(ai-portal 安装码)要判 Cursor 装没装。Cursor 的 bundle id 是 todesktop 打包时给的,
#    本机 /Applications/Cursor.app 实测(`defaults read …/Info CFBundleIdentifier`)。
CARHER_CURSOR_BUNDLE_ID=com.todesktop.230313mzl4w4u92

# 真身判据 = 有 app.asar(Claude/Codex)或 app/package.json(Cursor 不打 asar,资源是目录)
#            + MacOS 下有可执行文件(光有个壳子目录不算)
_cfa_ok() {
  [ -n "$1" ] && [ -d "$1" ] &&
    { [ -f "$1/Contents/Resources/app.asar" ] || [ -f "$1/Contents/Resources/app/package.json" ]; } &&
    [ -n "$(ls "$1/Contents/MacOS" 2>/dev/null | head -n1)" ]
}
_cfa_id() { defaults read "$1/Contents/Info" CFBundleIdentifier 2>/dev/null; }

# 用法:carher_find_app_by_id <bundle id> [名字里该含的词]
#   第二个参数是**同 id 多装**时的挑选偏好,不是过滤条件:
#   🔴 11-03 本机实测:`/Applications/ChatGPT.app` 和 `/Applications/OpenAI Codex.app`
#   **CFBundleIdentifier 都是 com.openai.codex**(同一个 bundle,ChatGPT 自带 codex-cli)。
#   只按 id 挑会挑到 ChatGPT.app,屏幕上打出来让人以为认错了 App。
#   ⇒ 带这个词的优先;一个都不带才退回第一个命中(功能上两者都有 codex-cli,都能用)。
carher_find_app_by_id() {
  _cfa_want="$1"; _cfa_hint="$2"
  if [ -n "${CARHER_DOCTOR_APP:-}" ]; then
    [ -d "$CARHER_DOCTOR_APP" ] && printf '%s' "$CARHER_DOCTOR_APP"
    return
  fi
  [ "$(uname -s)" = Darwin ] || return

  _cfa_first=""
  # ① 问系统:按 bundle id 找,装在哪、叫什么名字都认
  #    🔴 结果必须逐行读:`for a in $(...)` 会按**空格**切开 ——
  #    「/Applications/Claude for Desktop.app」或装在「~/My Apps/」下都会被切碎成一堆不存在的路径,
  #    于是又回到「装着却说没装」。11-03 我自己先写错过一次,台架里的「改名+带空格」那条当场红。
  while IFS= read -r a; do
    _cfa_ok "$a" || continue
    [ -n "$_cfa_first" ] || _cfa_first="$a"
    case "$_cfa_hint" in
      "") printf '%s' "$a"; return;;
      *) case "$a" in *"$_cfa_hint"*) printf '%s' "$a"; return;; esac;;
    esac
  done <<EOF
$(mdfind "kMDItemCFBundleIdentifier == '$_cfa_want'" 2>/dev/null)
EOF
  [ -n "$_cfa_first" ] && { printf '%s' "$_cfa_first"; return; }

  # ② Spotlight 关了 / 刚装还没索引:扫常见目录,仍按 bundle id 认
  #    🔴 用 find 自己匹配,别在 shell 里写裸通配符:zsh 下没命中会报错中断(bash 是原样传)
  while IFS= read -r r; do
    [ -n "$r" ] && [ -d "$r" ] || continue
    while IFS= read -r a; do
      _cfa_ok "$a" || continue
      if [ "$(_cfa_id "$a")" = "$_cfa_want" ]; then printf '%s' "$a"; return; fi
    done <<EOF2
$(find "$r" -maxdepth 1 -name '*.app' -print 2>/dev/null)
EOF2
  done <<EOF3
$(printf '%s' "${CARHER_APP_ROOTS:-/Applications:$HOME/Applications}" | tr ':' '\n')
EOF3

  # ③ 正在跑就从进程路径反推(装在任何地方都兜得住)
  while IFS= read -r a; do
    [ -n "$a" ] || continue
    _cfa_ok "$a" || continue
    if [ "$(_cfa_id "$a")" = "$_cfa_want" ]; then printf '%s' "$a"; return; fi
  done <<EOF4
$(ps -Ao comm= 2>/dev/null | sed -n 's#\(/.*\.app\)/Contents/MacOS/.*#\1#p' | sort -u)
EOF4
}

carher_find_claude_app() { carher_find_app_by_id "$CARHER_CLAUDE_BUNDLE_ID" ""; }
carher_find_codex_app()  { carher_find_app_by_id "$CARHER_CODEX_BUNDLE_ID" Codex; }
carher_find_cursor_app() { carher_find_app_by_id "$CARHER_CURSOR_BUNDLE_ID" Cursor; }
