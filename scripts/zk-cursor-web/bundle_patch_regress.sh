#!/bin/sh
# bundle_patch_regress.sh —— 改了 cursor_team_setup.{js,py} 之后的离线回归台架(①~⑯)。
# 全程只读 live Cursor(不用退出、不改任何东西),产物都在临时目录。
#
#   sh bundle_patch_regress.sh                      # 三腿(旧版零漂移 / js==py / 假 app 端到端)
#   sh bundle_patch_regress.sh --new-bundles /tmp/cx319   # 再加第①腿:新版 bundle 锚点全 exactly-1
#   sh bundle_patch_regress.sh --new-app "/Volumes/Cursor Installer/Cursor.app"   # ①b:新版件C 4/4
#   sh bundle_patch_regress.sh --fetch              # 自己去官方拉最新 dmg 抽 bundle,再跑全四腿
#   sh bundle_patch_regress.sh --fetch-latest       # 同上,但拉**更新通道**的最新版(10-26:同事自动升到的 3.27.14
#                                                   #   不在 stable 下载页上,stable 那时还是 3.27.12 ⇒ --fetch 量的不是他那版)
#
# 各腿(照 skill cursor-client-bundle-patch「新版本漂移修复流程」第 4 步):
#   ① 新版两条 bundle:每个非 multi 锚点 hits=1(multi ≥1)——证明新正则认得出新版
#  ①b 新版 app 的件C(ctxwin)四个目标全 OK ——🔴 2026-09-23 补:①只量 workbench 解锁锚点,
#      ctxwin 那四个 bundle 完全没进台架,3.21.18 的锚点 B/C 命中 0 就是这样发出去的
#   ② 旧版 pristine bundle 打完 == 当前 live 已打 bundle(BYTE-EQUAL)——防"修新版把老用户改坏"
#   ③ js 产物 == py 产物(BYTE-EQUAL)——双实现不许分叉
#   ④ 假 app 端到端 --repair 打上 + node --check 过 + 再跑一次全 SKIP(幂等)
#   ⑤ 件C ctxwin:三锚点 + 语法阳性对照 + 行为(500 与 500000 同形)
#   ⑥ byok 路由(bOd)行为 + 阳性对照(原版在开关 off 下必须判不走 BYOK)
#   ⑦ 件D noloop:Agent Host 轮次路由器改判 connect —— 结构(锚点/位置/语法)
#      + 行为(daemon.cjs 真路由器,3 个坏出口全改判 + 每例原版对照)
#   ⑧ 家族隔离:workbench 的某一个锚点漂了,件C/件D 必须照样打上(exit 5,不是 exit 2 全盘退出)
#   ⑨ 还原源逐路径解析(两代备份)+ 还原后复查残留 marker(exit 6)
#   ⑩ 空串 Key 不算 Key:--uninstall 置空后再 --upgrade 必须说「还差一步」而不是「直接用」
#   ⑪ 小代理装不上但**活着**时,BYOK 地址不许被降级成公网直连(死了才降)
#   ⑫ Key 不可见字符 sanitize + 真 Headers 对账
#   ⑬ 普通 node 跑 DOCTOR 不许假红(RES 退回安装位置)
#   ⑭ 件C/件D **自己**的锚点漂了要列红 + exit 5(⑧ 量的是别人漂了它照打)
#   ⑮ plist / skill / launchCursor 落盘的必须是 Cursor 二进制,不是 node
#   ⑯ 「刷模型」副本比当前包旧要报出来
#
# 🔴 ⑧~⑪ 的**阳性对照一律在本腿里就地重实现旧行为**,不许写成 `git show HEAD:...` ——
#    修好的版本一进 HEAD,那种对照就退化成"新 vs 新",永远绿,等于台架上少了一腿却没人知道。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
JS="$HERE/cursor_team_setup.js"
PY="$HERE/cursor_team_setup.py"
PROBE="$HERE/bundle_anchor_probe.js"
CTXWIN="$HERE/cursor_ctxwin_patch.js"
APP="${CURSOR_APP:-/Applications/Cursor.app}"
LIVE_RES="$APP/Contents/Resources/app"
BAK="${CURSOR_BACKUP_DIR:-$HOME/.cursor-team-setup-backup}"
TMP="$(mktemp -d)"
NEWDIR=""; NEWAPP=""; FETCH=0; CHANNEL=stable; FAIL=0; SKIP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --new-bundles) NEWDIR="$2"; shift 2 ;;
    --new-app) NEWAPP="$2"; shift 2 ;;
    --fetch) FETCH=1; shift ;;
    --fetch-latest) FETCH=1; CHANNEL=latest; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "未知参数:$1" >&2; exit 64 ;;
  esac
done
ok()   { echo "  PASS  $*"; }
bad()  { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $*"; SKIP=$((SKIP+1)); }
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

command -v node >/dev/null 2>&1 || { echo "需要 node(装 Cursor 的机器可用 $APP/Contents/MacOS/Cursor + ELECTRON_RUN_AS_NODE=1)" >&2; exit 1; }

# ── AST 定位层要求的运行时(2026-09-15)──────────────────────────────────────
# 安装器现在用 AST 定位(acorn 取自 Cursor 自带 resources/app/node_modules/acorn)。
# 系统 node 跑时 RES 由 process.execPath 推导 → 指向 node 自己的目录 → 找不到 acorn →
# **静默退回正则**。台架若用系统 node,第③腿就退化成"正则 vs 正则",AST 那条路
# 一行都没跑却全绿 = 假绿。所以这里必须拿 Cursor 的 Electron 当 node,并用
# CX_REQUIRE_AST=1 锁死"退回正则就报红"。
CXNODE=""
if [ -x "$APP/Contents/MacOS/Cursor" ]; then CXNODE="$APP/Contents/MacOS/Cursor"
else for c in /usr/share/cursor/cursor /opt/cursor/cursor; do [ -x "$c" ] && CXNODE="$c" && break; done; fi
# ⚠️ 这里**不能**包成 shell 函数。POSIX sh 里 `VAR=x somefunc` 的前置赋值会**留在当前
# shell**(不像外部命令那样只作用于那一次调用)→ 第③腿的 CX_APPLY_TO_FILE/CX_APPLY_OUT
# 会泄漏到第④腿,让 --repair 走进测试钩子,日志里只有 "applied:" 而没有 "修复完成",
# 第④腿全红且原因完全指错方向(实测踩过)。所以每处都写全 `env ... "$CXNODE"`。
if [ -z "$CXNODE" ]; then
  echo "  !! 找不到 Cursor 可执行文件(CURSOR_APP=$APP)——AST 那条路跑不起来,第③腿会退化成正则vs正则" >&2
fi

# ── 0) 语法 + 双实现常量等价(改坏语法的话后面三腿会给出误导性的红) ──
echo "--- 0) 语法 / 常量等价 ---"
node --check "$JS" >/dev/null 2>&1 && ok "node --check cursor_team_setup.js" || bad "node --check cursor_team_setup.js"
python3 -m py_compile "$PY" 2>/dev/null && ok "py_compile cursor_team_setup.py" || bad "py_compile cursor_team_setup.py"
if [ -f "$HERE/setup_impl_parity.py" ]; then
  if python3 "$HERE/setup_impl_parity.py" >"$TMP/parity.log" 2>&1; then ok "setup_impl_parity.py"
  else bad "setup_impl_parity.py(见 $TMP/parity.log)"; cp "$TMP/parity.log" /tmp/cx-parity.log 2>/dev/null; fi
fi

# ── 拉新版 dmg(可选):不用装,挂载后直接拷两条 workbench bundle ──
if [ "$FETCH" = "1" ]; then
  echo "--- fetch: 官方最新 $CHANNEL(darwin-arm64)---"
  META="$(curl -fsS https://api2.cursor.sh/updates/api/download/$CHANNEL/darwin-arm64/cursor)" || { bad "取更新元数据失败"; META=""; }
  URL="$(printf '%s' "$META" | sed -n 's/.*"downloadUrl":"\([^"]*\)".*/\1/p')"
  VER="$(printf '%s' "$META" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
  if [ -n "$URL" ]; then
    echo "  版本 $VER"
    # 10-26:300MB 一次下载 900s 超时只到 310/316MB,hdiutil 报「无法识别映像」像是包坏了 ⇒
    #   按 Content-Length 续传到够数再挂,不够就判红(不许拿半截文件往下跑)
    WANT="$(curl -fsSI "$URL" | awk 'tolower($1)=="content-length:"{print $2}' | tr -d '\r')"
    for _try in 1 2 3 4 5 6; do
      HAVE="$(stat -f %z "$TMP/cursor.dmg" 2>/dev/null || stat -c %s "$TMP/cursor.dmg" 2>/dev/null || echo 0)"
      [ -n "$WANT" ] && [ "$HAVE" = "$WANT" ] && break
      curl -fsSL -C - --max-time 1200 "$URL" -o "$TMP/cursor.dmg" || true
    done
    HAVE="$(stat -f %z "$TMP/cursor.dmg" 2>/dev/null || stat -c %s "$TMP/cursor.dmg" 2>/dev/null || echo 0)"
    [ -n "$WANT" ] && [ "$HAVE" = "$WANT" ] || bad "下载 dmg 不完整($HAVE / ${WANT:-?} 字节)"
    MNT="$TMP/mnt"; mkdir -p "$MNT"
    if hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$TMP/cursor.dmg" >/dev/null; then
      NEWDIR="$TMP/new-$VER"; mkdir -p "$NEWDIR"
      cp "$MNT"/Cursor.app/Contents/Resources/app/out/vs/workbench/workbench.desktop.main.js \
         "$MNT"/Cursor.app/Contents/Resources/app/out/vs/workbench/workbench.glass.main.js "$NEWDIR"/ \
         && ok "抽出 $VER 的两条 bundle → $NEWDIR" || bad "拷 bundle 失败"
      # ①b 要的是件C 那四个目标(在 extensions/ 下,不是 workbench)——挂载期内一起拷走,
      # 否则 detach 之后①b 只能 SKIP,而 SKIP 不是通过。
      NEWAPP="$TMP/newapp-$VER"
      for rel in extensions/cursor-local-agent-runtime/dist/main.js \
                 extensions/cursor-agent-host/dist/main.js \
                 extensions/cursor-agent-exec/dist/main.js \
                 extensions/cursor-agent-host/dist/agent-host-daemon/dist/bin/daemon.cjs; do
        mkdir -p "$NEWAPP/Contents/Resources/app/$(dirname "$rel")"
        cp "$MNT/Cursor.app/Contents/Resources/app/$rel" \
           "$NEWAPP/Contents/Resources/app/$rel" 2>/dev/null || bad "拷件C 目标失败:$rel"
      done
      hdiutil detach "$MNT" >/dev/null 2>&1
    else bad "挂载 dmg 失败"; fi
  fi
fi

# ── ① 新版 bundle:锚点全 exactly-1 ──
echo "--- 1) 新版 bundle 锚点命中 ---"
if [ -n "$NEWDIR" ]; then
  set -- "$NEWDIR"/workbench.*.main.js
  if [ -e "$1" ]; then
    node "$PROBE" "$JS" "$@" > "$TMP/probe.txt" 2>&1
    sed 's/^/    /' "$TMP/probe.txt"
    # 判据:非 multi 行必须 hits=1(marker=已打 的行是已打过的 bundle,不该出现在 pristine 新版上)
    if grep -q 'hits=[0-9]*(multi)' "$TMP/probe.txt" && ! grep -q 'hits=0(multi)' "$TMP/probe.txt" \
       && ! grep -E 'hits=[0-9]+ ' "$TMP/probe.txt" | grep -v '(multi)' | grep -qv 'hits=1 '; then
      ok "新版锚点:非 multi 全 hits=1,multi ≥1"
    else bad "新版锚点有 hits!=1 的(见上)"; fi
  else skip "①:$NEWDIR 里没有 workbench.*.main.js"; fi
else skip "①:没给 --new-bundles / --fetch(只有新 Cursor 版本发布时才需要这腿)"; fi

# ── ①b 新版 app 的件C(ctxwin):四个目标全 OK,且不许出现"已打过" ──
# 🔴 为什么单开一腿:① 只量 workbench 两条 bundle 的解锁锚点,件C 的四个目标在
#    extensions/ 下,从来没进过台架。3.21.18 里锚点 B 的局部变量 `const n=[]` 摇成
#    `const r=[]`、C 的第三参数与局部变量互换 ⇒ 命中 0,安装器"警告后跳过"不阻断,
#    所以包发出去了、门禁全绿、用户拿不到修复。跳过不是通过。
echo "--- 1b) 新版 app 件C(ctxwin)锚点 ---"
# 🔴 回落:没给 --new-app 时,拿 ~/.cursor-ctxwin-backup/<ts>/ 的扁平快照还原出目录结构。
# 那份快照是**打补丁前**的原厂文件,对"锚点认不认得出这个版本"完全够用。
# 之前只认 --new-app ⇒ 平时跑台架它永远 SKIP,而这腿存在的唯一理由就是
# 「3.21.18 的 ctxwin 锚点命中 0 是因为它从来没进台架」—— 常年 SKIP 等于原病复发。
# 这时量的是**当前版本**而不是新版,输出里写清,别当成"新版已验"。
NEWAPP_IS_SNAPSHOT=0
if [ -z "$NEWAPP" ]; then
  CWSNAP="$(ls -d "$HOME"/.cursor-ctxwin-backup/* 2>/dev/null | tail -1)"
  if [ -n "$CWSNAP" ] && [ -f "$CWSNAP/extensions__cursor-agent-exec__dist__main.js" ]; then
    NEWAPP="$TMP/snapapp"; NEWAPP_IS_SNAPSHOT=1
    for fn in "$CWSNAP"/extensions__*; do
      rel="$(basename "$fn" | sed 's|__|/|g')"
      mkdir -p "$NEWAPP/$(dirname "$rel")"
      cp "$fn" "$NEWAPP/$rel"
    done
    echo "    (没给 --new-app → 用原厂快照 $CWSNAP 还原目录,量的是**当前版本**不是新版)"
  fi
fi
if [ -n "$NEWAPP" ]; then
  NAPP_RES="$NEWAPP/Contents/Resources/app"
  [ -d "$NAPP_RES" ] || NAPP_RES="$NEWAPP"      # 也接受直接给 Resources/app
  if [ -f "$NAPP_RES/extensions/cursor-agent-exec/dist/main.js" ]; then
    CURSOR_APP_ROOT="$NAPP_RES" node "$CTXWIN" --dry >"$TMP/cw.txt" 2>&1
    sed 's/^/    /' "$TMP/cw.txt"
    NOK=$(grep -c '^OK ' "$TMP/cw.txt")
    NXX=$(grep -c '^XX ' "$TMP/cw.txt")
    NNA=$(grep -c '^NA ' "$TMP/cw.txt")
    # 判据:XX 必须 0,且 OK+NA 覆盖全部 4 个目标。
    # 🔴 NA =「这份 bundle 不含摘要阈值,上游挪走了」(3.28.5 的 agent-host),不是失败;
    #    但 NA **不许全覆盖** —— 四个都 NA 说明换了结构或路径错了,那是真问题。
    case "$NOK" in ''|*[!0-9]*) bad "①b 尺子坏了:数不出 OK 行数";; *)
      # 🔴 10-28:没给 --new-app 时这腿**退回原厂快照**(= 当前版本),上面只打了一行小字,
      #    结论却照样写「新版」⇒ 同一个 app 两次跑出 4/0 和 3/1,看着像尺子抽风。
      #    现在把量的是哪一版写进结论里:快照来源 ⇒ 明说不是新版,且算 SKIP(跳过不是通过)。
      NVER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version","?"))' "$NAPP_RES/package.json" 2>/dev/null || echo '?')"
      if [ "$NEWAPP_IS_SNAPSHOT" = "1" ]; then
        skip "①b:没给 --new-app ⇒ 量的是 $CWSNAP 那份原厂快照(**不是新版**;快照目录里没有 package.json,所以这里不报版本号);新版本漂移时必须给 --new-app"
      elif [ "$NXX" = "0" ] && [ "$((NOK+NNA))" = "4" ] && [ "$NOK" -gt 0 ]; then
        ok "①b 件C 在 $NVER 上锚点全 exactly-1(该打的 $NOK 个全过,$NNA 个不适用)"
      else bad "①b 件C 在 $NVER 上 OK=$NOK XX=$NXX NA=$NNA(须 XX=0、OK+NA=4、OK>0)"; fi ;;
    esac
  else skip "①b:$NEWAPP 里找不到 extensions/cursor-agent-exec/dist/main.js"; fi
else skip "①b:没给 --new-app / --fetch(新版本漂移时这腿必须跑)"; fi

# ── ② 旧版 pristine 打完 == 当前 live 已打(零漂移真门) ──
echo "--- 2) 旧版零漂移(pristine 重打 vs live 已打)---"
LIVE_VER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version","?"))' "$LIVE_RES/package.json" 2>/dev/null || echo '?')"
REF=""
for d in "$BAK"/"$LIVE_VER"-*; do
  case "$d" in *-cfgonly) continue ;; esac
  [ -f "$d/workbench.desktop.main.js" ] || continue
  # 🔴 只认 **marker=0** 的备份当 pristine(10-13 实测):已打过补丁的机器再跑 UPGRADE 加新补丁,
  #    落的备份是「旧补丁全在、只差新那个」的 bundle(9 个 marker)。按目录名取最新 ⇒ 拿它当种子,
  #    ⑥ 在已打过 byok-force 的文本上找原版 bOd ⇒ 命中 0 报「尺子不可信」(②③④ 同样被污染)。
  m=$(grep -c '@cxteam-\|@cx-queue-pump:' "$d/workbench.desktop.main.js" 2>/dev/null | head -1 | tr -dc '0-9')
  [ "${m:-0}" = "0" ] && REF="$d"
done
# live 自己就是 pristine 的情况(补丁已 --revert / 这台机器从没装过 / 刚升过级还没 REPAIR):
# 那两条 live bundle **本身就是**合法的 pristine 种子,②③④ 三腿都能靠它跑起来。
# 之前只认备份目录,于是"live 干净"被当成"没种子"→ 三腿全 SKIP。SKIP 不是通过,
# 而这种 SKIP 恰好出现在最常见的状态上(改完代码想验一下,但手上没装补丁),等于台架白摆着。
# 判据不能用"备份在不在",要用**marker 计数**:0 = pristine,>0 = 已打过(不能当 pristine)。
if [ -z "$REF" ] && [ -f "$LIVE_RES/out/vs/workbench/workbench.desktop.main.js" ]; then
  LM=0
  for b in workbench.desktop.main.js workbench.glass.main.js; do
    # `grep -c` 零命中时 **既打印 0 又 rc=1** ⇒ 写 `$(grep -c … || echo 0)` 会拿到 "0\n0",
    # 送进 $(( )) 直接 syntax error(实测)。所以不用 ||,末尾 tr 掉换行兜住多行。
    n=$(grep -c '@cxteam-\|@cx-queue-pump:' "$LIVE_RES/out/vs/workbench/$b" 2>/dev/null | head -1 | tr -dc '0-9')
    LM=$((LM+${n:-0}))
  done
  if [ "$LM" = "0" ]; then
    REF="$TMP/live-pristine"; mkdir -p "$REF"
    cp "$LIVE_RES/out/vs/workbench/workbench.desktop.main.js" \
       "$LIVE_RES/out/vs/workbench/workbench.glass.main.js" "$REF/" 2>/dev/null
    LIVE_IS_PRISTINE=1
    echo "    live($LIVE_VER)本身是 pristine(marker=0)→ 拿它当种子跑 ③④(② 无从比较,见下)"
  fi
fi
: "${LIVE_IS_PRISTINE:=0}"
if [ -z "$REF" ]; then
  skip "②:$BAK 下没有与 live 版本($LIVE_VER)对应的含 bundle 备份 —— live 若已升级到新版,这腿要在升级前跑"
elif [ "$LIVE_IS_PRISTINE" = "1" ]; then
  # 这一腿问的是"pristine 重打出来的,和这台机器上**已经打过的**是不是逐字节一样"。
  # live 本身 pristine ⇒ 没有"已经打过的"那一边,比较对象不存在 —— 只能跳,
  # 但要说清是"没有对照物",不是"通过了"。
  skip "②:live 本身是 pristine(补丁没装 / 已 revert)⇒ 没有「已打」那一边可比。装上补丁后再跑这腿"
else
  echo "    live=$LIVE_VER  pristine=$REF"
  for b in workbench.desktop.main.js workbench.glass.main.js; do
    [ -f "$REF/$b" ] || { skip "②:备份里没有 $b"; continue; }
    CX_APPLY_TO_FILE="$REF/$b" CX_APPLY_OUT="$TMP/old-$b" node "$JS" >/dev/null 2>"$TMP/old-$b.log"
    if cmp -s "$TMP/old-$b" "$LIVE_RES/out/vs/workbench/$b"; then ok "②$b BYTE-EQUAL live"
    else
      # 🔴 这一红有**两种完全不同的成因**,别合成一句"旧代产物漂了"(2026-10-12 踩到):
      #   A 真漂移:改动意外影响了别的补丁 ⇒ 必须查;
      #   B **有意改了某个补丁**,而 live 上装的还是旧版 ⇒ 不一致是必然的。
      # B 的正确处置(SOP:"别把有意加补丁导致的②红当已知问题挥掉")=
      #   证明差异**恰好只有你改的那一处**:把 live 里的旧片段替换成新片段后,
      #   与新产物 `cmp` 逐字节相同。下面把首个差异的位置和上下文打出来,
      #   好让人当场判断落在哪一处补丁上,而不是只知道"不一致"。
      bad "②$b 与 live 已打 bundle 不一致 —— 可能是真漂移,也可能你刚有意改了某个补丁(见下方首个差异)"
      cp "$TMP/old-$b" /tmp/ 2>/dev/null
      python3 - "$LIVE_RES/out/vs/workbench/$b" "$TMP/old-$b" <<'PYDIFF' || true
import sys
a=open(sys.argv[1],encoding='utf8',errors='replace').read()
b=open(sys.argv[2],encoding='utf8',errors='replace').read()
n=min(len(a),len(b)); i=0
while i<n and a[i]==b[i]: i+=1
print('        长度 live=%d 新产物=%d;首个差异 @%d'%(len(a),len(b),i))
print('        live  : ...%s...'%a[max(0,i-70):i+70].replace('\n','\\n'))
print('        新产物: ...%s...'%b[max(0,i-70):i+70].replace('\n','\\n'))
PYDIFF
    fi
  done
fi

# ── ③ js 产物 == py 产物 ──
echo "--- 3) js/py 产物逐字节等价 ---"
SRCS=""
[ -n "$NEWDIR" ] && for f in "$NEWDIR"/workbench.*.main.js; do [ -f "$f" ] && SRCS="$SRCS $f"; done
[ -n "$REF" ] && for b in workbench.desktop.main.js workbench.glass.main.js; do [ -f "$REF/$b" ] && SRCS="$SRCS $REF/$b"; done
if [ -z "$SRCS" ]; then skip "③:没有可用的 pristine bundle"; else
  i=0
  for f in $SRCS; do
    i=$((i+1))
    NM="$(basename "$f")($(dirname "$f" | sed 's|.*/||'))"
    # js 走 **AST 定位**(CX_REQUIRE_AST=1:退回正则就 rc=3 报红,不许假绿)
    if [ -n "$CXNODE" ]; then
      env ELECTRON_RUN_AS_NODE=1 CX_REQUIRE_AST=1 CX_APPLY_TO_FILE="$f" CX_APPLY_OUT="$TMP/p$i.js.out" \
        "$CXNODE" "$JS" >"$TMP/p$i.js.log" 2>&1; RC=$?
      if [ $RC -ne 0 ]; then
        # rc=2 是"锚点命中!=1 拒绝动手":两条实现该同时拒,不算不一致(下面用 py 的 rc 对上账)
        CX_APPLY_TO_FILE="$f" CX_APPLY_OUT="$TMP/p$i.py.out" python3 "$PY" >/dev/null 2>&1; PRC=$?
        if [ $RC -eq 2 ] && [ $PRC -ne 0 ]; then ok "③$NM 两实现一致拒绝(该版本形状不匹配,rc=$RC/$PRC)"
        else bad "③$NM js(AST) rc=$RC / py rc=$PRC(见 $TMP/p$i.js.log)"; cp "$TMP/p$i.js.log" /tmp/ 2>/dev/null; fi
        continue
      fi
    else
      bad "③$NM 没有 Cursor 运行时 → AST 那条路没跑(拒绝按正则vs正则判绿)"; continue
    fi
    # py 走 **正则**(第二实现故意不同路):两条独立路子必须落同一字节。
    # 这比旧版"同一套锚点抄两遍"强:抄两遍时锚点写错会一起错(2026-09-15 的 `\w+` 就是两边一起漏)。
    CX_APPLY_TO_FILE="$f" CX_APPLY_OUT="$TMP/p$i.py.out" python3 "$PY" >/dev/null 2>&1
    if cmp -s "$TMP/p$i.js.out" "$TMP/p$i.py.out"; then ok "③$NM js(AST)==py(正则) BYTE-EQUAL"
    else bad "③$f js(AST)/py(正则) 产物不一致"; cp "$TMP/p$i.js.out" "$TMP/p$i.py.out" /tmp/ 2>/dev/null; fi
  done
fi

# ── ④ 假 app 端到端 --repair + 幂等重跑 ──
echo "--- 4) 假 app 端到端 --repair + 幂等 ---"
SEED=""
[ -n "$NEWDIR" ] && [ -f "$NEWDIR/workbench.desktop.main.js" ] && SEED="$NEWDIR"
[ -z "$SEED" ] && [ -n "$REF" ] && SEED="$REF"
if [ -z "$SEED" ]; then skip "④:没有可用的 pristine bundle 做种"; else
  # CURSOR_APP_ROOT 指的是 **Resources/app**(= live 的 $LIVE_RES),不是 .app;指错了安装器会 ENOENT,
  # 而"没打印拒绝动手"看起来像通过 —— 所以这腿只认阳性证据(patched 行 + marker 落盘)。
  # HOME 也换成临时目录:否则每跑一次就在 ~/.cursor-team-setup-backup 里留一个 unknown-* 假备份。
  FAKE="$TMP/fakeapp"; FU="$TMP/fakeuser"; FH="$TMP/fakehome"
  mkdir -p "$FAKE/out/vs/workbench" "$FU" "$FH"
  cp "$SEED"/workbench.desktop.main.js "$SEED"/workbench.glass.main.js "$FAKE/out/vs/workbench/" 2>/dev/null
  echo '{"version":"0.0.0-fake"}' > "$FAKE/package.json"
  R1="$TMP/repair1.log"; R2="$TMP/repair2.log"
  # 端到端也必须走 Cursor 运行时 + CX_REQUIRE_AST:用系统 node 的话 AST 那条路没跑,
  # 这腿就只证明了"正则能端到端打上",对新定位层零判别力。
  # (acorn 从 runtimeRoot 取,不受这里的 CURSOR_APP_ROOT=假 app 影响——见 loadAcorn 注释)
  env ELECTRON_RUN_AS_NODE=1 HOME="$FH" CURSOR_APP_ROOT="$FAKE" CURSOR_USER_DIR="$FU" \
    CX_SKIP_RUNNING_CHECK=1 CX_REQUIRE_AST=1 "$CXNODE" "$JS" --repair >"$R1" 2>&1
  env ELECTRON_RUN_AS_NODE=1 HOME="$FH" CURSOR_APP_ROOT="$FAKE" CURSOR_USER_DIR="$FU" \
    CX_SKIP_RUNNING_CHECK=1 CX_REQUIRE_AST=1 "$CXNODE" "$JS" --repair >"$R2" 2>&1
  if grep -q '修复完成' "$R1" && [ "$(grep -c '^   patched:' "$R1")" = "2" ]; then ok "④首跑两条 bundle 都 patched"
  else bad "④首跑没真打上(见 $R1)"; cp "$R1" /tmp/cx-repair1.log; fi
  if grep -q '无需修复' "$R2"; then ok "④重跑无需修复(幂等)"; else bad "④重跑不幂等(见 $R2)"; cp "$R2" /tmp/cx-repair2.log; fi
  for b in workbench.desktop.main.js workbench.glass.main.js; do
    f="$FAKE/out/vs/workbench/$b"
    n=$(grep -o '@cxteam-\|@cx-queue-pump:' "$f" | wc -l | tr -d ' ')
    [ "$n" -ge 6 ] && ok "④$b marker 落盘 x$n" || bad "④$b marker 只有 $n 个"
    node --check "$f" 2>/dev/null && ok "④$b 打完 node --check 过" || bad "④$b 打完语法坏了"
  done
fi

# ── ⑤ 件C @cx-ctxwin:v3 上下文窗口单位归一 ───────────────────────────────
# 判据三条,缺一条这腿就不算数:
#   a) 三锚点各 exactly-1 且真打上(marker 每文件 3 个)
#   b) 打完过 node --check,且**阳性对照必须报红**(证明这把语法尺子真在量)
#   c) 行为对:把打完的函数抠出来真跑,收到 500 必须与收到 500000 逐行同形
#      (阈值 450000、90% 触发、89% 不触发),0 原样不动(自定义模型行为零变化)
echo "--- 5) 件C ctxwin 单位归一 ---"
CWBK="$(ls -d "$HOME"/.cursor-ctxwin-backup/* 2>/dev/null | tail -1)"
if [ -z "$CWBK" ] || [ ! -d "$CWBK" ]; then
  SKIP=$((SKIP+1)); echo "  SKIP  ⑤:没有 ~/.cursor-ctxwin-backup/<ts>/ 原始快照(先在本机跑一次 cursor_ctxwin_patch.js --apply)"
else
  CWTMP="$(mktemp -d)"
  for spec in "extensions__cursor-local-agent-runtime__dist__main.js:min" \
              "extensions__cursor-agent-host__dist__main.js:min" \
              "extensions__cursor-agent-exec__dist__main.js:min" \
              "extensions__cursor-agent-host__dist__agent-host-daemon__dist__bin__daemon.cjs:src"; do
    fn="${spec%:*}"; kd="${spec##*:}"
    [ -f "$CWBK/$fn" ] || { bad "⑤快照缺 $fn"; continue; }
    out="$CWTMP/$fn"
    if env ELECTRON_RUN_AS_NODE=1 CX_CTXWIN_APPLY_TO_FILE="$CWBK/$fn" CX_CTXWIN_KIND="$kd" \
         CX_CTXWIN_OUT="$out" "$CXNODE" "$JS" >/dev/null 2>"$CWTMP/err"; then
      n=$(grep -o '@cx-ctxwin:v3' "$out" | wc -l | tr -d ' ')
      [ "$n" = "3" ] && ok "⑤${fn##*__} 三锚点全打上(marker x$n)" || bad "⑤${fn##*__} marker=$n(应为 3)"
      node --check "$out" 2>/dev/null && ok "⑤${fn##*__} 打完 node --check 过" || bad "⑤${fn##*__} 打完语法坏了"
    else
      bad "⑤${fn##*__} 打不上:$(cat "$CWTMP/err")"
    fi
  done
  # 阳性对照:在 marker 处插坏括号,语法尺子必须报红,否则上面那几个 PASS 不算数
  POI="$CWTMP/poison.js"
  cp "$CWTMP/extensions__cursor-agent-exec__dist__main.js" "$POI" 2>/dev/null &&
  python3 -c "
import sys
p=sys.argv[1]; s=open(p,encoding='utf8').read(); i=s.index('/*@cx-ctxwin:v3*/')
open(p,'w',encoding='utf8').write(s[:i]+'/*@cx-ctxwin:v3*/}}}if('+s[i+17:])" "$POI" &&
  { node --check "$POI" 2>/dev/null && bad "⑤阳性对照居然 PASS ⇒ 语法尺子是坏的,上面的 PASS 全不算数" \
      || ok "⑤阳性对照正确报红 ⇒ 语法尺子可信"; }
  # 行为腿:抠出打完的真函数跑,500 必须与 500000 同形
  node -e '
const fs=require("fs");
const f=process.argv[1];
const s=fs.readFileSync(f,"utf8");
const g=(i)=>{const o=s.indexOf("{",i);let d=0,j=o;for(;j<s.length;j++){const c=s[j];if(c==="{")d++;else if(c==="}"){d--;if(!d){j++;break}}}return s.slice(i,j)};
const nm=(n)=>{const r=new RegExp("function "+n+"\\(","g");const m=[...s.matchAll(r)];if(m.length!==1)throw new Error(n+" 命中="+m.length);return g(m[0].index)};
const M=eval("(function(){"+["isValidUsedTokensThreshold","getBackgroundSummarizationTriggerThreshold","shouldStartBackgroundSummarization"].map(nm).join("\n")+";return{s:shouldStartBackgroundSummarization,t:getBackgroundSummarizationTriggerThreshold}})()");
const P={unusedTokensThresholdToStartBackgroundSummarization:1e4,unusedPercentTokensThresholdToStartBackgroundSummarization:.1};
let bad=0;
for(const [raw,real] of [[500,500000],[300,300000],[272,272000],[256,256000],[1,1000000]]){
  if(M.t(raw,P)!==M.t(real,P)){console.log("🔴 "+raw+" 与 "+real+" 阈值不同形");bad++;continue}
  if(!M.s(Math.round(real*0.90),raw,P)){console.log("🔴 "+raw+" 在 90% 没触发");bad++}
  if(M.s(Math.round(real*0.89),raw,P)){console.log("🔴 "+raw+" 在 89% 误触发");bad++}
  if(M.s(1000,raw,P)){console.log("🔴 "+raw+" 发 1000 token 就压(病没修好)");bad++}
}
if(M.t(0,P)!==undefined){console.log("🔴 maxTokens=0 被动了(自定义模型行为应零变化)");bad++}
process.exit(bad?1:0);
' "$CWTMP/extensions__cursor-agent-host__dist__agent-host-daemon__dist__bin__daemon.cjs" \
    && ok "⑤行为:500/300/272/256/1 与真实窗口逐行同形,90%触发/89%不触发/0不动" \
    || bad "⑤行为腿不符(见上面 🔴)"
  rm -rf "$CWTMP"
fi

# ── 6) byok 路由行为腿(Free 档关掉 BYOK 开关 ⇒ 改道 Cursor 服务端)────────────────
#    需要一份新版 pristine bundle 做输入:没有就必须**报跳过**,不许静默当过。
echo "--- 6) byok 路由(bOd)行为 + 阳性对照 ---"
BYOK_SRC=""
[ -n "$NEWDIR" ] && [ -f "$NEWDIR/workbench.desktop.main.js" ] && BYOK_SRC="$NEWDIR/workbench.desktop.main.js"
# 🔴 回落到 $REF:这腿量的是**路由函数的行为**,只需要一份 pristine bundle,
# 跟"是不是新版"无关。之前只认 --new-bundles ⇒ 平时跑台架它永远 SKIP,
# 而 byok-force/byok-keepon 恰好是最近改动最多的两个补丁 —— 最该被量的那一腿常年没跑。
[ -z "$BYOK_SRC" ] && [ -n "$REF" ] && [ -f "$REF/workbench.desktop.main.js" ] && BYOK_SRC="$REF/workbench.desktop.main.js"
if [ -z "$CXNODE" ]; then
  skip "⑥:没找到 Cursor 的 electron(CXNODE 空),这腿没跑"
elif [ -z "$BYOK_SRC" ]; then
  skip "⑥:没有 pristine workbench bundle(既没 --new-bundles,$BAK 下也没有当前版本的备份),这腿没跑"
else
  BTMP="$(mktemp -d)"
  CX_APPLY_TO_FILE="$BYOK_SRC" CX_APPLY_OUT="$BTMP/patched.js" \
    env ELECTRON_RUN_AS_NODE=1 CX_REQUIRE_AST=1 "$CXNODE" "$JS" >/dev/null 2>&1
  if [ ! -s "$BTMP/patched.js" ]; then bad "⑥打补丁没产出产物"; else
    CX_BYOK_PRE="$BYOK_SRC" CX_BYOK_POST="$BTMP/patched.js" node "$HERE/cursor_byok_offline_cases.js" \
      && ok "⑥byok 路由行为全绿(含阳性对照:原版在开关 off 下必须判不走 BYOK)" \
      || bad "⑥byok 路由行为腿不符(见上面 FAIL)"
  fi
  rm -rf "$BTMP"
fi

# ── 7) 件D @cx-noloop:v1 —— Agent Host 轮次 runtime 路由器改判 connect ──────────
# 病:同事那台的服务端 feature gate `agent_host_local_loop` 是 on ⇒
# `createLocalLoopTurnRouter` 把这一轮交给 `managed-local`(Cursor 自家的
# InferenceService.RunInference)⇒ `[permission_denied] … is not enabled for this account`
# / 界面上是 "An unexpected error occurred. Request ID: <uuid>"。
# 而 Cursor 自己的源码里写着 local loop **不支持 BYOK**
# ("Turns with custom model credentials (BYOK/private models) are not supported on the local loop"),
# ⇒ 对我们这支纯 BYOK 的车队,两个非 connect 出口**都是错的**:带 key → fail,不带 key → 打它自家推理。
#
# 判据四条,缺一条这腿不算数:
#   a) 两个目标各 exactly-1 锚点、marker 各 1 个
#   b) marker 必须**紧贴生成器体开头**(= 插成第一条语句)。只断言"有 marker"不够:
#      插在函数尾部同样能数到 1 个,但一行都不会被执行 —— 那是零症状的假绿。
#   c) 打完过 node --check,且**阳性对照必须报红**(证明语法尺子真在量)
#   d) 行为:拿 daemon.cjs 里那份**真路由器**真跑,三个坏出口(managed-local /
#      fail / privateInference 那支)全改判 connect,且每例自带原版对照
#      ⇒ cursor_noloop_offline_cases.js
#   🔴 main.js 那份是混淆名,靠猜名造桩测的是我的猜名能力不是补丁 ⇒ 它只有 a/b/c
#      三条结构判据,没有行为判据。这半的强度低于 daemon.cjs 那半,不许合并成一句"全绿"。
echo "--- 7) 件D noloop:Agent Host 轮次路由改判 connect ---"
# 🔴 2026-10-12:这腿原来**只**喂 ~/.cursor-ctxwin-backup 里的旧版快照 ⇒ 新版形状从来没进过
#    门禁。3.24.12 上 noloop 两个目标锚点全灭(上游把 __awaiter+生成器换成原生 async 箭头,
#    且 gate 名与 logger 名之间插了代码从 80 字拉到 826 字),而件D 的设计是「只告警跳过、
#    不阻断」⇒ 台架全绿、包照样发、真机上装完才发现没打上。与 09-23 件C 那次逐字同病。
#    ⇒ 给了 --new-app 就**优先量新版**(从 app 真实目录取),没给才回落旧快照。
#    ⚠️ 种子**必须是 pristine**(没打过补丁)。`--new-app` 指 live Cursor 时它早就打过了,
#    下面的 pristine 断言会正确地判红 —— 那不是腿坏,是我喂错了东西。
#    所以挑种子的顺序是:① `--new-app` 且那份 app 是干净的(marker=0,比如刚挂载的 dmg)
#    → ② `~/.cursor-team-setup-backup/<当前版本>-<ts>/` 里的 pristine 快照(升级时自动落的,
#    扁平名 `cxctxwin__…`)→ ③ 旧的 `~/.cursor-ctxwin-backup/*`。
NLBK=""
NL_SEED_WHY=""
nl_try_seed() {   # $1=main.js 路径 $2=daemon.cjs 路径 $3=说明
  [ -f "$1" ] && [ -f "$2" ] || return 1
  grep -q '@cx-noloop' "$1" 2>/dev/null && return 1
  grep -q '@cx-noloop' "$2" 2>/dev/null && return 1
  NLBK="$(mktemp -d)"
  cp "$1" "$NLBK/extensions__cursor-agent-host__dist__main.js" || return 1
  cp "$2" "$NLBK/extensions__cursor-agent-host__dist__agent-host-daemon__dist__bin__daemon.cjs" || return 1
  NL_SEED_WHY="$3"; return 0
}
if [ -n "$NEWAPP" ] && [ -d "$NEWAPP/Contents/Resources/app" ]; then
  NAR="$NEWAPP/Contents/Resources/app"
  nl_try_seed "$NAR/extensions/cursor-agent-host/dist/main.js" \
              "$NAR/extensions/cursor-agent-host/dist/agent-host-daemon/dist/bin/daemon.cjs" \
              "--new-app 的真实 bundle(pristine,量新版)" || true
fi
if [ -z "$NLBK" ]; then
  LIVEVER="$(/usr/bin/defaults read "$APP/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null)"
  for d in $(ls -dt "$HOME"/.cursor-team-setup-backup/${LIVEVER:-x}-* 2>/dev/null); do
    [ -d "$d" ] || continue
    nl_try_seed "$d/cxctxwin__extensions__cursor-agent-host__dist__main.js" \
                "$d/cxctxwin__extensions__cursor-agent-host__dist__agent-host-daemon__dist__bin__daemon.cjs" \
                "同版本 pristine 备份 $(basename "$d")(量 $LIVEVER)" && break
  done
fi
if [ -z "$NLBK" ]; then
  OLDBK="$(ls -d "$HOME"/.cursor-ctxwin-backup/* 2>/dev/null | tail -1)"
  [ -n "$OLDBK" ] && nl_try_seed "$OLDBK/extensions__cursor-agent-host__dist__main.js" \
      "$OLDBK/extensions__cursor-agent-host__dist__agent-host-daemon__dist__bin__daemon.cjs" \
      "旧 ctxwin 快照(⚠️ 可能是旧版形状,量不到新版)" || true
fi
[ -n "$NLBK" ] && echo "    种子 = $NL_SEED_WHY"
if [ -z "$CXNODE" ]; then
  skip "⑦:没找到 Cursor 的 electron(CXNODE 空),这腿没跑"
elif [ -z "$NLBK" ] || [ ! -d "$NLBK" ]; then
  skip "⑦:没有种子(既没给 --new-app,也没有 ~/.cursor-ctxwin-backup/<ts>/ 快照)"
else
  NLTMP="$(mktemp -d)"
  NL_DAEMON_OUT=""
  for spec in "extensions__cursor-agent-host__dist__main.js:min" \
              "extensions__cursor-agent-host__dist__agent-host-daemon__dist__bin__daemon.cjs:src"; do
    fn="${spec%:*}"; kd="${spec##*:}"
    [ -f "$NLBK/$fn" ] || { bad "⑦快照缺 $fn"; continue; }
    # 先断言输入真的是 pristine。快照里已经带 marker 的话,下面"marker=1"会变成
    # 在量上一轮的残留,补丁本轮有没有生效完全测不出来 —— 报红不跳过。
    if grep -q '@cx-noloop:v1' "$NLBK/$fn"; then bad "⑦${fn##*__} 快照里已有 marker ⇒ 不是 pristine,这一例无判别力"; continue; fi
    out="$NLTMP/$fn"
    if env ELECTRON_RUN_AS_NODE=1 CX_NOLOOP_APPLY_TO_FILE="$NLBK/$fn" CX_NOLOOP_KIND="$kd" \
         CX_NOLOOP_OUT="$out" "$CXNODE" "$JS" >/dev/null 2>"$NLTMP/err"; then
      n=$(grep -o '@cx-noloop:v1' "$out" | wc -l | tr -d ' ')
      [ "$n" = "1" ] && ok "⑦${fn##*__} 锚点 exactly-1 且打上(marker x$n)" || bad "⑦${fn##*__} marker=$n(应为 1)"
      # b) 位置:marker + return 必须是**函数体第一条语句**。
      # 🔴 两代形状不同,这条断言原来写死了 `function*(){` ⇒ 在 3.24.12 上即使补丁打对了
      #    也会报红(假红,且理由指向"插进去不会执行"完全误导)。两代都认:
      #      ≤3.21   : `function*(){ /*marker*/return{runtime:"connect"`
      #      3.24.12+: `async t=>{ /*marker*/return{runtime:"connect"`(原生 async 箭头)
      h=$(grep -oE '(function\* *\( *\)|async *\(?[A-Za-z0-9_$]*\)? *=>) *\{ */\*@cx-noloop:v1\*/return\{runtime:"connect"' "$out" | wc -l | tr -d ' ')
      [ "$h" = "1" ] && ok "⑦${fn##*__} return 是函数体第一条语句(会被执行)" \
        || bad "⑦${fn##*__} marker 不在函数体开头(命中=$h)⇒ 插进去也不会执行"
      node --check "$out" 2>/dev/null && ok "⑦${fn##*__} 打完 node --check 过" || bad "⑦${fn##*__} 打完语法坏了"
      [ "$kd" = "src" ] && NL_DAEMON_OUT="$out"
    else
      bad "⑦${fn##*__} 打不上:$(cat "$NLTMP/err")"
    fi
  done
  # c) 阳性对照:在 marker 处插坏括号,语法尺子必须报红
  NPOI="$NLTMP/poison.js"
  if [ -n "$NL_DAEMON_OUT" ] && cp "$NL_DAEMON_OUT" "$NPOI" 2>/dev/null; then
    python3 -c "
import sys
p=sys.argv[1]; s=open(p,encoding='utf8').read(); i=s.index('/*@cx-noloop:v1*/')
open(p,'w',encoding='utf8').write(s[:i]+'/*@cx-noloop:v1*/}}}if('+s[i+17:])" "$NPOI" &&
    { node --check "$NPOI" 2>/dev/null && bad "⑦阳性对照居然 PASS ⇒ 语法尺子是坏的,上面的 PASS 全不算数" \
        || ok "⑦阳性对照正确报红 ⇒ 语法尺子可信"; }
  fi
  # d) 行为腿(只对 daemon.cjs 那份真名产物)
  if [ -n "$NL_DAEMON_OUT" ]; then
    if env CX_NL_PRE="$NLBK/extensions__cursor-agent-host__dist__agent-host-daemon__dist__bin__daemon.cjs" \
           CX_NL_POST="$NL_DAEMON_OUT" node "$HERE/cursor_noloop_offline_cases.js"; then
      ok "⑦行为(daemon.cjs 真路由器):3 个坏出口全改判 connect,原版对照全部成立"
    else
      bad "⑦行为腿不符(见上面 FAIL)"
    fi
  else
    bad "⑦行为腿没输入(daemon.cjs 那一例没产出)⇒ 结构过了也不代表路由真改判"
  fi
  rm -rf "$NLTMP"
fi


# ── ⑧ 家族隔离:一个锚点漂了,其余家族照打(2026-09-24 补,F4)───────────────
# 量的是什么:workbench 的某一个锚点在新版 Cursor 上认不出时,**ctxwin(件C)和
# noloop(件D)那几个完全不同的文件必须照样打上**。
# 为什么值得一条腿:09-23 那版里任何一个锚点认不出都会 `process.exit(2)` 直接终止整个
# 进程 ⇒ 用户报的"8146%"和"An unexpected error"两个毛病一个都没修上,而屏幕上只有
# 一行看不懂的 `!! xxx 锚点命中=0`。这是"用户不断随机反馈问题"里占比最大的那一类。
# 判据 = **前后对照**:同一份 fixture,不漂移时三家族全打上(证明 fixture 有判别力);
# 只把 byok-keepon 那一个锚点摇一下,则 desktop 一个字节没动、glass + ctxwin×4 +
# noloop×2 全部照打、退出码 5、屏幕上有红字清单。
echo "--- 8) 家族隔离:一个锚点漂了其余照打 ---"
CW8="${CWBK:-}"
if [ -z "$SEED" ] || [ -z "$CW8" ] || [ ! -d "$CW8" ]; then
  skip "⑧:缺 pristine 种子(workbench 来自 \$SEED / exthost 来自 ~/.cursor-ctxwin-backup)—— 这腿是端到端的,不能拿合成文件糊弄"
else
  F8="$TMP/f8"; FA8="$F8/app"; FU8="$F8/user"; FH8="$F8/home"
  mk8() {
    rm -rf "$F8"; mkdir -p "$FA8/out/vs/workbench" "$FU8" "$FH8"
    cp "$SEED/workbench.desktop.main.js" "$SEED/workbench.glass.main.js" "$FA8/out/vs/workbench/" 2>/dev/null
    echo '{"version":"0.0.0-fake8"}' > "$FA8/package.json"
    for fn in "$CW8"/extensions__*; do
      [ -f "$fn" ] || continue
      rel="$(basename "$fn" | sed 's|__|/|g')"
      mkdir -p "$FA8/$(dirname "$rel")"; cp "$fn" "$FA8/$rel"
    done
  }
  # 数某个 marker 在某个文件里出现几次。grep -c 零命中时 rc=1 且打印 0,所以不接 ||。
  n8() { [ -f "$1" ] || { echo 0; return; }; grep -o "$2" "$1" 2>/dev/null | wc -l | tr -d ' '; }
  D8="$FA8/out/vs/workbench/workbench.desktop.main.js"
  G8="$FA8/out/vs/workbench/workbench.glass.main.js"
  X8="$FA8/extensions/cursor-agent-exec/dist/main.js"
  N8="$FA8/extensions/cursor-agent-host/dist/agent-host-daemon/dist/bin/daemon.cjs"
  mk8
  # 种子自检:**这腿会数 marker,所以先断言自己数得到 0** —— 种子若本身带 marker,
  # 下面"打上了"的结论就是从旧 marker 抄来的,整腿零判别力。
  SEED_MK=$(( $(n8 "$D8" '@cxteam-') + $(n8 "$X8" '@cx-ctxwin:v3') + $(n8 "$N8" '@cx-noloop:v1') ))
  if [ ! -f "$X8" ] || [ ! -f "$N8" ]; then
    skip "⑧:~/.cursor-ctxwin-backup 快照里缺 agent-exec / daemon.cjs,凑不出三家族的 fixture"
  elif [ "$SEED_MK" != "0" ]; then
    skip "⑧:种子不是 pristine(marker=$SEED_MK)—— 拿它做对照会把旧 marker 读成新战果"
  else
    L8A="$TMP/leg8-control.log"
    env ELECTRON_RUN_AS_NODE=1 HOME="$FH8" CURSOR_APP_ROOT="$FA8" CURSOR_USER_DIR="$FU8" \
      CX_SKIP_RUNNING_CHECK=1 CX_REQUIRE_AST=1 "$CXNODE" "$JS" --repair >"$L8A" 2>&1
    RC8A=$?
    C_D=$(n8 "$D8" '@cxteam-'); C_G=$(n8 "$G8" '@cxteam-')
    C_X=$(n8 "$X8" '@cx-ctxwin:v3'); C_N=$(n8 "$N8" '@cx-noloop:v1')
    if [ "$RC8A" = "0" ] && [ "$C_D" -gt 0 ] && [ "$C_G" -gt 0 ] && [ "$C_X" = "3" ] && [ "$C_N" = "1" ]; then
      ok "⑧对照跑(不漂移):三家族全打上 desktop=$C_D glass=$C_G ctxwin=$C_X noloop=$C_N,exit=0"
    else
      bad "⑧对照跑就没打全(exit=$RC8A desktop=$C_D glass=$C_G ctxwin=$C_X noloop=$C_N)⇒ 下面的漂移对照不成立,见 $L8A"
      cp "$L8A" /tmp/ 2>/dev/null
    fi

    # 摇一个锚点。挑 byok-keepon(`this.setUseOpenAIKey(!1)`)是因为它是**正则腿**且只在
    # desktop/glass 里 —— 改它不会连带影响 exthost 那几个文件,漂移面是干净的。
    # ⚠️ 只摇 desktop 不摇 glass:同一家族里的另一份必须照常打上,否则"跳过"的粒度
    #     就成了整个家族,而不是"出问题的那一个文件"。
    mk8
    python3 - "$D8" <<'PY8'
import sys
p = sys.argv[1]
s = open(p, encoding="utf8", errors="surrogateescape").read()
old = "this.setUseOpenAIKey(!1)"
i = s.find(old)
if i < 0:
    sys.exit(3)
s = s.replace(old, "this.setUseOpenAIKey(!1,void 0)")
open(p, "w", encoding="utf8", errors="surrogateescape").write(s)
PY8
    if [ $? != 0 ]; then
      skip "⑧:种子里找不到 byok-keepon 那个锚点(Cursor 版本变了)⇒ 造不出这次漂移"
    else
      L8B="$TMP/leg8-drift.log"
      env ELECTRON_RUN_AS_NODE=1 HOME="$FH8" CURSOR_APP_ROOT="$FA8" CURSOR_USER_DIR="$FU8" \
        CX_SKIP_RUNNING_CHECK=1 CX_REQUIRE_AST=1 "$CXNODE" "$JS" --repair >"$L8B" 2>&1
      RC8B=$?
      C_D=$(n8 "$D8" '@cxteam-'); C_G=$(n8 "$G8" '@cxteam-')
      C_X=$(n8 "$X8" '@cx-ctxwin:v3'); C_N=$(n8 "$N8" '@cx-noloop:v1')
      [ "$C_D" = "0" ] && ok "⑧漂移后 desktop 一个字节没动(marker=0)—— 不半打" \
        || bad "⑧漂移后 desktop 上有 $C_D 个 marker ⇒ 打了一半就退出了"
      [ "$C_G" -gt 0 ] && ok "⑧同家族另一份 glass 照常打上(marker=$C_G)" \
        || bad "⑧glass 被连坐了(marker=$C_G)⇒ 跳过的粒度错成了整个家族"
      [ "$C_X" = "3" ] && ok "⑧件C ctxwin 照常打上(agent-exec marker=3)" \
        || bad "⑧件C 被 workbench 的锚点漂移连坐了(marker=$C_X)⇒ 8146% 那个毛病修不上"
      [ "$C_N" = "1" ] && ok "⑧件D noloop 照常打上(daemon.cjs marker=1)" \
        || bad "⑧件D 被连坐了(marker=$C_N)⇒ An unexpected error 那个毛病修不上"
      grep -q '整份跳过' "$L8B" && ok "⑧屏幕上说清了「哪一份被跳过」" \
        || bad "⑧没打印「整份跳过」⇒ 同事只会看到一行看不懂的锚点报错"
      grep -qF '这部分补丁没打上' "$L8B" && ok "⑧收尾有红字清单(告诉他要找管理员出新锚点)" \
        || bad "⑧收尾没有红字清单 ⇒ 部分失败被埋在滚动输出里"
      [ "$RC8B" = "5" ] && ok "⑧退出码 5(部分完成)—— 既不是 0 假绿也不是 2 全盘放弃" \
        || bad "⑧退出码=$RC8B(应为 5)"
    fi
  fi
fi

# ── ⑨ 还原源逐路径解析 + 还原后复查(2026-09-24 补,F1)────────────────────
# 量的是什么:一台机器上跑过两代安装器 ⇒ 原厂 workbench 躺在**较早**那个备份目录里,
# 而较晚那个目录里只有 09-23 才新增的 cxnoloop__ 两份。老规则"挑最新的含 bundle 备份、
# 只从它里面拿"会把 workbench 留在打过补丁的状态,却照样打印"恢复原状完成"。
# 🔴 阳性对照**在本腿里就地重实现老规则**,不用 `git show HEAD:` —— 一旦修好的版本
#    进了 HEAD,那种对照就退化成"新 vs 新",永远绿,等于没有对照。
echo "--- 9) 还原源逐路径解析 + 还原后复查 ---"
F9="$TMP/f9"; FA9="$F9/app"; FH9="$F9/home"; BR9="$FH9/.cursor-team-setup-backup"
A9="$BR9/9.9.9-20260922-100000"; B9="$BR9/9.9.9-20260923-100000"
DESK9="$FA9/out/vs/workbench/workbench.desktop.main.js"
DAEM9="$FA9/extensions/cursor-agent-host/dist/agent-host-daemon/dist/bin/daemon.cjs"
# $1 = B 目录里那份 daemon 备份的内容("脏"=还带着 ctxwin marker / "净"=原厂)
mk9() {
  rm -rf "$F9"
  mkdir -p "$FA9/out/vs/workbench" "$FA9/extensions/cursor-agent-exec/dist" \
           "$FA9/extensions/cursor-local-agent-runtime/dist" \
           "$FA9/extensions/cursor-agent-host/dist/agent-host-daemon/dist/bin" "$A9" "$B9"
  echo '{"version":"9.9.9"}' > "$FA9/package.json"
  # app 侧:六个文件都带着我们的 marker(= 装过补丁的样子)
  printf 'patched-desktop/*@cxteam-keepmine*/tail\n' > "$DESK9"
  printf 'patched-glass/*@cxteam-keepmine*/tail\n'   > "$FA9/out/vs/workbench/workbench.glass.main.js"
  printf 'patched/*@cx-ctxwin:v3*/tail\n' > "$FA9/extensions/cursor-agent-exec/dist/main.js"
  printf 'patched/*@cx-ctxwin:v3*/tail\n' > "$FA9/extensions/cursor-local-agent-runtime/dist/main.js"
  printf 'patched/*@cx-ctxwin:v3*//*@cx-noloop:v1*/tail\n' > "$FA9/extensions/cursor-agent-host/dist/main.js"
  printf 'patched/*@cx-ctxwin:v3*//*@cx-noloop:v1*/tail\n' > "$DAEM9"
  # A 代(09-22):原厂 workbench ×2 + 那时候打的 ctxwin 三份的原厂副本
  printf 'PRISTINE-DESKTOP\n' > "$A9/workbench.desktop.main.js"
  printf 'PRISTINE-GLASS\n'   > "$A9/workbench.glass.main.js"
  printf 'PRISTINE-EXEC\n'    > "$A9/cxctxwin__extensions__cursor-agent-exec__dist__main.js"
  printf 'PRISTINE-LAR\n'     > "$A9/cxctxwin__extensions__cursor-local-agent-runtime__dist__main.js"
  printf 'PRISTINE-HOST\n'    > "$A9/cxctxwin__extensions__cursor-agent-host__dist__main.js"
  # B 代(09-23):只新增了 noloop 两份。daemon 那份是**在 ctxwin 之后**备份的 ⇒ 默认"脏"。
  printf 'PRISTINE-HOST\n' > "$B9/cxnoloop__extensions__cursor-agent-host__dist__main.js"
  if [ "$1" = "净" ]; then printf 'PRISTINE-DAEMON\n' > "$B9/cxnoloop__extensions__cursor-agent-host__dist__agent-host-daemon__dist__bin__daemon.cjs"
  else printf 'half/*@cx-ctxwin:v3*/tail\n' > "$B9/cxnoloop__extensions__cursor-agent-host__dist__agent-host-daemon__dist__bin__daemon.cjs"; fi
}
mk9 脏
# a) 阳性对照:就地重跑老规则(排序后最后一个"含 bundle 文件"的目录,只从它里面拿)
OLDPICK=""
for d in $(ls "$BR9" | sort); do
  for f in "$BR9/$d"/workbench.desktop.main.js "$BR9/$d"/workbench.glass.main.js \
           "$BR9/$d"/cxctxwin__* "$BR9/$d"/cxnoloop__*; do
    [ -f "$f" ] && { OLDPICK="$d"; break; }
  done
done
if [ "$OLDPICK" = "$(basename "$B9")" ] && [ ! -f "$BR9/$OLDPICK/workbench.desktop.main.js" ]; then
  ok "⑨阳性对照成立:老规则挑中 $OLDPICK,而原厂 workbench 只在更早那代里 ⇒ 老代码还不回去"
else
  bad "⑨阳性对照不成立(老规则挑中 '$OLDPICK')⇒ 这腿量不到 F1,下面全绿也不算数"
fi
# b) 新代码:逐路径解析,workbench 必须从**更早**那代还回来
L9A="$TMP/leg9-dirty.log"
env ELECTRON_RUN_AS_NODE=1 HOME="$FH9" CURSOR_APP_ROOT="$FA9" CX_SKIP_RUNNING_CHECK=1 \
  "$CXNODE" "$JS" --revert >"$L9A" 2>&1
RC9A=$?
if grep -q '^PRISTINE-DESKTOP$' "$DESK9"; then ok "⑨workbench 从更早那代($(basename "$A9"))还回来了"
else bad "⑨workbench 没还回来(内容=$(head -c 40 "$DESK9"))⇒ F1 复发"; fi
grep -q "← $(basename "$A9")" "$L9A" && ok "⑨屏幕上标了每个文件是从**哪个**备份目录取的" \
  || bad "⑨没打印来源目录 ⇒ 还原了什么无从核对"
# c) "拷了几个文件"不是"还原干净了":B 里那份 daemon 本身带 ctxwin marker,必须被复查抓出来
if grep -qF '回滚**不完整**' "$L9A" && [ "$RC9A" = "6" ]; then
  ok "⑨还原后复查抓到残留 marker,退出码 6,不打印成功"
else
  bad "⑨还原后没复查出残留(exit=$RC9A)⇒ 半还原会被报成「完成」,见 $L9A"
fi
# d) 阴性对照:备份完整时必须干干净净地绿,否则这把尺子只会报红,等于没有
mk9 净
L9B="$TMP/leg9-clean.log"
env ELECTRON_RUN_AS_NODE=1 HOME="$FH9" CURSOR_APP_ROOT="$FA9" CX_SKIP_RUNNING_CHECK=1 \
  "$CXNODE" "$JS" --revert >"$L9B" 2>&1
RC9B=$?
if grep -q '回滚完成' "$L9B" && [ "$RC9B" = "0" ] && ! grep -q '@cx' "$DAEM9"; then
  ok "⑨阴性对照:备份完整时 exit=0 且复查无 marker(尺子不是只会报红)"
else
  bad "⑨阴性对照红了(exit=$RC9B)⇒ 复查有假阳,见 $L9B"
fi

# ── ⑩ 空串 Key 不算 Key(2026-09-24 补,F2)────────────────────────────────
# 量的是什么:`--uninstall` 清 Key 时写的是**空串**而不是删行。于是下次 `--upgrade`
# 读到 `""`(非 null)就判定"库里已有 Key,原样保留",最后打印"直接用" —— 而他一打开
# Cursor 就是 401。这是"装完看着成功、用起来报错"这一类反馈的其中一条。
# 判据 = 一对:空串必须报「没有 Key / 还差一步」,真值必须报「已有 Key / 直接用」。
# 只测空串那一半是不够的 —— 分不出"尺子在量"还是"这行字永远打印"。
echo "--- 10) 空串 Key 不算 Key ---"
F10="$TMP/f10"; FA10="$F10/app"; FU10="$F10/user"; FH10="$F10/home"
# 已全部打过补丁的假 bundle:让 plans 为空,这腿只关心 Key 那一步,不牵扯锚点。
# marker 清单**从安装器现抠**,写死的话加了新补丁就会静默退化成"锚点认不出"。
MK10="$(grep -o '@cxteam-[A-Za-z0-9]*' "$JS" | sort -u | sed 's|^|/*|; s|$|*/|' | tr -d '\n')"
QP10="$(grep -o '@cx-queue-pump:v[0-9]*' "$JS" | head -1)"
mk10() {
  rm -rf "$F10"; mkdir -p "$FA10/out/vs/workbench" "$FU10/globalStorage" "$FH10"
  echo '{"version":"0.0.0-fake10"}' > "$FA10/package.json"
  for b in workbench.desktop.main.js workbench.glass.main.js; do
    printf 'x%s/*%s*/y\n' "$MK10" "$QP10" > "$FA10/out/vs/workbench/$b"
  done
  python3 - "$FU10/globalStorage/state.vscdb" "$1" <<'PY10'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("CREATE TABLE IF NOT EXISTS ItemTable(key TEXT PRIMARY KEY, value BLOB)")
rows = [("secret://cursorAuth/openAIKey", sys.argv[2])]
# mergeConfig 读不到 applicationUser blob 会 exit(3) —— 那一步在 Key 那一步**之前**,
# 缺了它整腿测不到东西。给一份最小但形状完整的 blob。
rows.append(("src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
             ".persistentStorage.applicationUser",
             '{"openAIBaseUrl":"https://example.invalid/v1","useOpenAIKey":true,'
             '"aiSettings":{"userAddedModels":[],"modelOverrideEnabled":[]}}'))
c.executemany("INSERT OR REPLACE INTO ItemTable(key,value) VALUES(?,?)", rows)
c.commit(); c.close()
PY10
}
if [ -z "$MK10" ] || [ -z "$QP10" ] || ! python3 -c 'import sqlite3' 2>/dev/null; then
  skip "⑩:抠不到 marker 清单或没有 python3 sqlite3 ⇒ 造不出 fixture"
else
  mk10 ""
  # fixture 自检:先确认这份假 bundle 真的被判成"全部已打过"。判不成的话下面读到的
  # Key 那几行可能是在别的分支里打印的,整腿零判别力。⇒「会数东西的测试先断言自己数得到」
  L10S="$TMP/leg10-selfcheck.log"
  env ELECTRON_RUN_AS_NODE=1 HOME="$FH10" CURSOR_APP_ROOT="$FA10" CURSOR_USER_DIR="$FU10" \
    CX_SKIP_RUNNING_CHECK=1 "$CXNODE" "$JS" --repair >"$L10S" 2>&1
  if ! grep -q '无需修复' "$L10S"; then
    skip "⑩:假 bundle 没被判成「全部已打过」(见 $L10S)⇒ fixture 不成立,不硬跑"
  else
    ok "⑩fixture 自检:假 bundle 被判成全部已打过(Key 那一步才是唯一变量)"
    # 空串那一发
    L10A="$TMP/leg10-empty.log"
    env ELECTRON_RUN_AS_NODE=1 HOME="$FH10" CURSOR_APP_ROOT="$FA10" CURSOR_USER_DIR="$FU10" \
      CX_SKIP_RUNNING_CHECK=1 "$CXNODE" "$JS" --upgrade --apply --no-zk-delta >"$L10A" 2>&1
    if ! grep -q '5) 写入 API Key' "$L10A"; then
      # 跑没跑到 Key 那一步是**先决条件**,不是结论。没跑到就明说跳过,
      # 不许把"前面某一步 exit 了"读成"空串被当成有 Key"。
      skip "⑩:--upgrade 没跑到 Key 那一步(见 $L10A)⇒ 这腿这次什么都没量到"
    elif grep -qF '库里**没有** Key' "$L10A" && grep -q '还差一步' "$L10A"; then
      ok "⑩空串 Key ⇒ 明说「库里没有 Key」+「还差一步」"
    else
      bad "⑩空串 Key 被当成有 Key(见 $L10A)⇒ 升级完报成功,他一打开就 401"; cp "$L10A" /tmp/ 2>/dev/null
    fi
    # 阴性对照:真有 Key 时不许乱喊"没有"
    mk10 '"ZmFrZS1rZXk="'
    L10B="$TMP/leg10-present.log"
    env ELECTRON_RUN_AS_NODE=1 HOME="$FH10" CURSOR_APP_ROOT="$FA10" CURSOR_USER_DIR="$FU10" \
      CX_SKIP_RUNNING_CHECK=1 "$CXNODE" "$JS" --upgrade --apply --no-zk-delta >"$L10B" 2>&1
    if grep -q '库里已有 Key' "$L10B" && grep -q '直接用' "$L10B"; then
      ok "⑩阴性对照:真有 Key 时照常「原样保留」+「直接用」"
    else
      bad "⑩阴性对照红了(见 $L10B)⇒ keyPresent 把真 Key 也判成空了"; cp "$L10B" /tmp/ 2>/dev/null
    fi
  fi
fi
# ── ⑪ 装不上小代理 ≠ 把已经配好的地址降级(2026-09-24 补)────────────────────
# 量的是什么:`zkdInstall` 返回 ok:false 时,老逻辑一律把 BYOK 地址改写成公网直连。
# 最常见的触发不是"小代理坏了",而是**引擎不是从包里跑的**(管理员在 repo 里直接跑,
# `zk-delta/` 不在 __dirname 旁边 ⇒ missing_*),而那台机器上的小代理跑得好好的。
# 一次无关的 --upgrade 就把一台配好的机器静默降成"不省流量"。
# 判据改成打 /healthz 看**真相**;台架用 CX_ZKD_ALIVE 钩子把这一格两个值都走一遍。
# 🔴 两侧都要量:只量"活着别降级"分不出"这行字是不是永远打印"。
echo "--- 11) 小代理活着就别降级 ---"
if ! command -v mk10 >/dev/null 2>&1; then
  skip "⑪:⑩ 的 fixture 没造起来(见上),这腿共用它"
elif [ "$(uname -s)" != "Darwin" ]; then
  skip "⑪:非 macOS,zk-delta 这一步本来就整段跳过"
else
  BURL() { python3 -c 'import sqlite3,json,sys
c=sqlite3.connect(sys.argv[1]); r=c.execute("SELECT value FROM ItemTable WHERE key=?",
 ("src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser",)).fetchone()
print(json.loads(r[0]).get("openAIBaseUrl","?") if r else "?")' "$FU10/globalStorage/state.vscdb"; }
  # a) 小代理活着 ⇒ 地址必须保持 127.0.0.1(台架本身就在 repo 里跑,zk-delta/ 天然不在旁边)
  mk10 '"ZmFrZS1rZXk="'
  L11A="$TMP/leg11-alive.log"
  env ELECTRON_RUN_AS_NODE=1 HOME="$FH10" CURSOR_APP_ROOT="$FA10" CURSOR_USER_DIR="$FU10"     CX_SKIP_RUNNING_CHECK=1 CX_ZKD_ALIVE=1 "$CXNODE" "$JS" --upgrade --apply >"$L11A" 2>&1
  U11A="$(BURL)"
  if grep -q '拒绝安装 zk-delta' "$L11A"; then
    ok "⑪前提成立:这一跑确实装不上小代理(包里没有源码)"
  else
    bad "⑪前提不成立(装上了?见 $L11A)⇒ 下面量的不是这个病"
  fi
  case "$U11A" in
    http://127.0.0.1:8788/*) ok "⑪小代理活着 ⇒ 地址保持 $U11A(没被降级)" ;;
    *) bad "⑪小代理活着却被降级成 $U11A ⇒ 一次无关的 --upgrade 就把配好的机器改坏" ;;
  esac
  # b) 阴性对照:小代理真的死了,地址就必须改回公网 —— 指着一个死端口比不省流量坏得多
  mk10 '"ZmFrZS1rZXk="'
  L11B="$TMP/leg11-dead.log"
  env ELECTRON_RUN_AS_NODE=1 HOME="$FH10" CURSOR_APP_ROOT="$FA10" CURSOR_USER_DIR="$FU10"     CX_SKIP_RUNNING_CHECK=1 CX_ZKD_ALIVE=0 "$CXNODE" "$JS" --upgrade --apply >"$L11B" 2>&1
  U11B="$(BURL)"
  case "$U11B" in
    http://127.0.0.1:*) bad "⑪阴性对照红了:小代理死了还把地址指过去($U11B)⇒ Cursor 彻底连不上" ;;
    "?"|"") bad "⑪阴性对照读不到 blob(见 $L11B)" ;;
    *) ok "⑪小代理死了 ⇒ 地址改回 $U11B(不指死端口)" ;;
  esac
fi

# ── 12) Key 里的不可见字符(2026-09-29 同事实报的 ByteString 病)─────────────────
#    这腿不需要 Cursor、不需要 bundle、不需要 fixture:纯函数 + 真 `new Headers()`,
#    所以**永远不许 SKIP**。它自带阳性对照(旧行为只 trim,必须仍然抛)。
echo "--- 12) Key 不可见字符 sanitize + 真 Headers 对账 ---"
if CX_SETUP_JS="$JS" node "$HERE/cursor_keysan_offline_cases.js" >"$TMP/leg12.log" 2>&1; then
  ok "⑫Key sanitize 全绿($(grep -c '  PASS ' "$TMP/leg12.log") 项;含阳性对照:旧行为 trim 仍抛 ByteString)"
else
  bad "⑫Key sanitize 腿不符:"; sed -n '/FAIL/p' "$TMP/leg12.log"
fi

# ── 13) DOCTOR 用普通 node 跑也要找得到 Cursor(2026-10-28 的假红)─────────────
#    病:RES 从 process.execPath 推,只有「被 Cursor 二进制当 node 跑」时才对。
#    管理员直接 `node cursor_team_setup.js --doctor` ⇒ 推成 node 自己的 Resources/app,
#    workbench / ctxwin / noloop 全报「不存在」= 满屏假红,而机器上补丁好好的。
#    阳性对照就地重实现旧行为(只按 execPath 推),它必须红;新行为必须绿。
echo "--- 13) 普通 node 跑 DOCTOR 不许假红 ---"
if [ -d "$LIVE_RES/out/vs/workbench" ]; then
  # 阳性对照:旧逻辑(纯 execPath)在 node 下指到哪
  OLDRES=$(node -e 'const p=require("path");const d=p.dirname(process.execPath);console.log(process.platform==="darwin"?p.join(d,"..","Resources","app"):p.join(d,"resources","app"))')
  if [ -d "$OLDRES/out/vs/workbench" ]; then
    echo "  NOTE ⑬这台机器的 node 恰好就在 Cursor 里 ⇒ 本腿无判别力,跳过"; SKIP=$((SKIP+1))
  else
    ok "⑬阳性对照成立:旧逻辑在普通 node 下指到 $OLDRES(没有 workbench)⇒ 会假红"
    NEWRES=$(node "$JS" --doctor 2>/dev/null | sed -n 's/^资源目录  *: *//p' | head -1)
    if [ "$NEWRES" = "$LIVE_RES" ]; then ok "⑬新逻辑退回到了真安装位置($NEWRES)"
    else bad "⑬新逻辑还是没找到 Cursor:报的是 '$NEWRES',应为 '$LIVE_RES'"; fi
    if node "$JS" --doctor 2>/dev/null | grep -q '^  workbench.desktop.main.js: .*byokforce='; then
      ok "⑬普通 node 下补丁行有真读数(不是『不存在』)"
    else bad "⑬普通 node 下仍报不出 workbench 补丁读数(假红没修掉)"; fi
    # CURSOR_APP_ROOT 测试钩子不许被 fallback 盖掉(台架自己全靠它)
    FAKE="$TMP/leg13-fake"; mkdir -p "$FAKE"
    R13=$(CURSOR_APP_ROOT="$FAKE" node "$JS" --doctor 2>/dev/null | sed -n 's/^资源目录  *: *//p' | head -1)
    if [ "$R13" = "$FAKE" ]; then ok "⑬CURSOR_APP_ROOT 仍然最高优先(台架钩子没被 fallback 抢走)"
    else bad "⑬CURSOR_APP_ROOT 被忽略了:报的是 '$R13'"; fi
  fi
else
  echo "  SKIP ⑬:本机没有 live Cursor bundle"; SKIP=$((SKIP+1))
fi

# ── 14) 件C/件D 锚点认不出 ⇒ 必须收尾列红 + 退出码 5(2026-10-28)──────────────
#    病:原来件C/件D 打不上只在屏幕中段打一行 ⚠️ 就 return,**不进 MISSED** ⇒ 收尾
#    仍是「✅ 完成」、exit 0。件C 没打上的实际后果是每轮自动压缩对话(10-28 实测 4 人
#    每百条主请求压 10~54 次),同事只觉得「Cursor 比 Claude 慢」,不知道要双击 REPAIR。
#    ⑧量的是"别的家族漂了件C 照样打上";这一腿量的是"件C **自己**漂了说不说"。
#    阳性对照就地重实现旧行为(把 MISSED.push 那两行摘掉)⇒ 必须 exit 0 + 没有红字。
echo "--- 14) 件C/件D 自己的锚点漂了要说出来 ---"
if [ ! -f "$X8" ] || [ ! -f "$N8" ] || [ "${SEED_MK:-x}" != "0" ]; then
  skip "⑭:复用不到 ⑧ 的 pristine fixture(见 ⑧ 的跳过原因)"
else
  # 摇件C 的锚点 A(所有 kind=min 的目标都靠它)——只摇 agent-exec 一个文件
  break_ctxwin() {
    python3 - "$X8" <<'PY14' || return 3
import sys
p=sys.argv[1]
s=open(p,encoding="utf8",errors="surrogateescape").read()
old="usedTokens:0,maxTokens:0,breakdown:void 0"
if old not in s: sys.exit(3)
open(p,"w",encoding="utf8",errors="surrogateescape").write(s.replace(old,"usedTokens:0,maxTokens:0,breakdownX:void 0"))
PY14
  }
  # 阳性对照用的旧版脚本:摘掉本次新加的两处 MISSED.push
  OLDJS="$TMP/leg14-oldbehavior.js"
  python3 - "$JS" "$OLDJS" <<'PY14B'
import re,sys
s=open(sys.argv[1],encoding="utf8").read()
n=0
for fam in ("件C(上下文窗口归一)","件D(关本地循环)"):
    pat=re.compile(r"\n\s*MISSED\.push\(\{ rel: t\.rel, why: \""+re.escape(fam)+r"[^\n]*\n")
    s,k=pat.subn("\n",s); n+=k
open(sys.argv[2],"w",encoding="utf8").write(s)
sys.exit(0 if n==2 else 7)
PY14B
  if [ $? != 0 ]; then
    skip "⑭:造不出阳性对照(没摘到两处 MISSED.push)⇒ 这腿没有判别力,不许算通过"
  else
    mk8; break_ctxwin
    if [ $? != 0 ]; then
      skip "⑭:种子里找不到件C 锚点 A(Cursor 版本变了)"
    else
      L14A="$TMP/leg14-old.log"
      env ELECTRON_RUN_AS_NODE=1 HOME="$FH8" CURSOR_APP_ROOT="$FA8" CURSOR_USER_DIR="$FU8" \
        CX_SKIP_RUNNING_CHECK=1 CX_REQUIRE_AST=1 "$CXNODE" "$OLDJS" --repair >"$L14A" 2>&1
      RC14A=$?
      if [ "$RC14A" = "0" ] && ! grep -qF '这部分补丁没打上' "$L14A"; then
        ok "⑭阳性对照成立:旧行为下件C 漂了仍然 exit 0、收尾无红字(= 静默)"
      else
        bad "⑭阳性对照不成立(exit=$RC14A)⇒ 这腿量不到东西,见 $L14A"
      fi
      mk8; break_ctxwin
      L14B="$TMP/leg14-new.log"
      env ELECTRON_RUN_AS_NODE=1 HOME="$FH8" CURSOR_APP_ROOT="$FA8" CURSOR_USER_DIR="$FU8" \
        CX_SKIP_RUNNING_CHECK=1 CX_REQUIRE_AST=1 "$CXNODE" "$JS" --repair >"$L14B" 2>&1
      RC14B=$?
      grep -qF '这部分补丁没打上' "$L14B" && ok "⑭新行为:件C 漂了收尾列进红字清单" \
        || bad "⑭件C 漂了收尾还是没列红(见 $L14B)"
      grep -qF '每轮自动压缩对话' "$L14B" && ok "⑭红字里写清了后果(体感『慢』⇒ 同事知道要 REPAIR)" \
        || bad "⑭红字没写后果,同事看不出该干什么"
      [ "$RC14B" = "5" ] && ok "⑭退出码 5(部分完成)" || bad "⑭退出码=$RC14B(应为 5)"
      # 其余家族不许被连坐
      C_D14=$(n8 "$D8" '@cxteam-'); C_N14=$(n8 "$N8" '@cx-noloop:v1')
      { [ "$C_D14" -gt 0 ] && [ "$C_N14" = "1" ]; } && ok "⑭workbench 与件D 照常打上(desktop=$C_D14 noloop=$C_N14)" \
        || bad "⑭件C 自己漂了却连坐了别人(desktop=$C_D14 noloop=$C_N14)"
    fi
  fi
fi

# ── 15) 落盘的路径不许指到 node(2026-10-28)────────────────────────────────────
#    病:EXEC=process.execPath 被写进三处会长期留在机器上的东西 —— 小代理的 launchd
#    plist、「刷模型」skill、画图 skill;以及 launchCursor 从它推 .app。普通 node 跑
#    安装器 ⇒ 全被写成 `/opt/homebrew/Cellar/node/<版本>/bin/node`,homebrew 升级 node
#    后小代理起不来(而 BYOK 地址还指着 127.0.0.1:8788 = 彻底连不上)、两个 skill 失效,
#    **都不报错**。阳性对照 = 就地重实现旧行为(直接用 process.execPath)。
echo "--- 15) plist / skill 里写的必须是 Cursor 二进制 ---"
if [ ! -d "$LIVE_RES" ]; then
  skip "⑮:本机没有 live Cursor"
else
  B15=$(node -e '
const fs=require("fs"),path=require("path"),os=require("os");
const src=fs.readFileSync(process.argv[1],"utf8");
const seg=src.slice(src.indexOf("const EXEC = process.execPath"), src.indexOf("const BUNDLES = ["));
const r=new Function("fs","path","os","process",seg+"; return cursorBin();")(fs,path,os,process);
console.log(r);' "$JS" 2>/dev/null)
  OLD15="$(node -e 'console.log(process.execPath)')"
  case "$OLD15" in
    *Cursor*) echo "  NOTE ⑮这台机器的 node 就是 Cursor ⇒ 本腿无判别力,跳过"; SKIP=$((SKIP+1)) ;;
    *)
      ok "⑮阳性对照成立:旧行为会把 $OLD15 写进 plist/skill(node 升级即失效)"
      case "$B15" in
        */Cursor.app/Contents/MacOS/Cursor|*/Cursor.exe|*/cursor) ok "⑮新行为落盘用的是 Cursor 二进制($B15)" ;;
        *) bad "⑮落盘路径还是不对:$B15" ;;
      esac
      # 四处落盘/拉起点都必须走 cursorBin(),不许再出现裸 EXEC
      for pat in '<string>${esc(cursorBin())}</string>' 'spawn(cursorBin(), [__filename'; do
        grep -qF "$pat" "$JS" && ok "⑮已改用 cursorBin():$(echo "$pat" | cut -c1-34)…" \
          || bad "⑮这一处还在用裸 EXEC:$pat"
      done
      grep -q 'const bin = cursorBin();' "$JS" && ok "⑮launchCursor 用 cursorBin() 推 .app" \
        || bad "⑮launchCursor 还在从 process.execPath 推 .app(刷模型会关了 Cursor 打不开)"
      ;;
  esac
fi

# ── 16) 「刷模型」用的那份旧副本要被看见(2026-10-28)────────────────────────────
#    病:「刷模型」跑的是 ~/.carher/cursor-setup/ 里的副本,只在再跑 INSTALL/UPGRADE
#    时才更新 ⇒ 管理员修好的东西对只点「刷模型」的人等于没发布,且屏幕上无处可知。
#    现在 DOCTOR 打出它的 sha/日期并在不一致时给出下一步。
#    阳性对照:放一份**内容不同**的副本进假 HOME,必须报不一致;放一份相同的,必须不报。
echo "--- 16) 「刷模型」副本陈旧要报出来 ---"
FH16="$TMP/leg16-home"; mkdir -p "$FH16/.carher/cursor-setup"
printf 'console.log("old")\n' > "$FH16/.carher/cursor-setup/cursor_team_setup.js"
D16A="$TMP/leg16-stale.log"
env HOME="$FH16" node "$JS" --doctor >"$D16A" 2>&1
grep -q '两份不一样' "$D16A" && ok "⑯副本内容不同 ⇒ 报「刷模型走的是旧逻辑」+ 给出 UPGRADE" \
  || bad "⑯副本陈旧却没报出来(见 $D16A)"
grep -qE 'sha=[0-9a-f]{12}' "$D16A" && ok "⑯打了两份的 sha(可对账,不是只说一句『可能旧』)" \
  || bad "⑯没打 sha"
cp "$JS" "$FH16/.carher/cursor-setup/cursor_team_setup.js"
D16B="$TMP/leg16-fresh.log"
env HOME="$FH16" node "$JS" --doctor >"$D16B" 2>&1
grep -q '两份不一样' "$D16B" && bad "⑯阴性对照红了:副本和当前一致却仍报不一样(见 $D16B)" \
  || ok "⑯阴性对照:副本一致时不报警(尺子不是恒红)"
rm -rf "$FH16/.carher/cursor-setup"
D16C="$TMP/leg16-none.log"
env HOME="$FH16" node "$JS" --doctor >"$D16C" 2>&1
grep -q '没跑过带「刷模型」的安装器' "$D16C" && ok "⑯压根没有副本时说清了「刷模型不会生效」" \
  || bad "⑯没有副本时没说清(见 $D16C)"

# ── 17) 小版本没量过不许假装验过(2026-10-28)──────────────────────────────────
#    病:软闸原来只按**大版本**判 ⇒ 同事的 3.24.18 / 3.27.22 大版本对得上就**一声不吭**,
#    而那几个小版本我们从没数过锚点(10-28 压缩风暴 4 人里就有 3.24.18)。
#    阳性对照 = 就地重实现旧规则(按大版本 startsWith),它对 3.24.18 必须什么都不说。
echo "--- 17) 小版本没量过要提示自查 ---"
mkv17() { mkdir -p "$TMP/v17"; python3 -c "
import json,sys,pathlib
src=pathlib.Path('$LIVE_RES/package.json')
d=json.loads(src.read_text()) if src.exists() else {'name':'cursor'}
d['version']=sys.argv[1]
pathlib.Path('$TMP/v17/package.json').write_text(json.dumps(d))" "$1"; }
g17() { mkv17 "$1"; env CURSOR_APP_ROOT="$TMP/v17" CX_SKIP_RUNNING_CHECK=1 node "$JS" 2>&1 | sed -n '1,3p'; }
# 阳性对照:旧规则对 3.24.18 静默
OLDQ=$(node -e '
const V=["3.16","3.17","3.18","3.19","3.20","3.21","3.24","3.27"];
console.log(V.some(v=>"3.24.18".startsWith(v+"."))?"silent":"warn");')
[ "$OLDQ" = silent ] && ok "⑰阳性对照成立:旧规则(按大版本)对 3.24.18 一声不吭" \
  || bad "⑰阳性对照不成立(旧规则居然会报)⇒ 这腿没判别力"
OUT17=$(g17 3.24.18)
printf '%s' "$OUT17" | grep -q '这个\*\*小版本\*\*没在台架上数过锚点' \
  && ok "⑰3.24.18(大版本对得上、小版本没量过)⇒ 提示自查 DOCTOR" \
  || { bad "⑰3.24.18 没给提示:"; printf '%s\n' "$OUT17" | sed -n 2p; }
printf '%s' "$OUT17" | grep -q 'ctxwin 那四行' && ok "⑰提示里写明看哪一项(ctxwin 四行 =3)" \
  || bad "⑰提示没说看哪里,等于让人干瞪眼"
# 阴性对照一:量过的小版本必须什么都不说
OUT17B=$(g17 3.24.12)
printf '%s' "$OUT17B" | grep -qE 'ℹ️|⚠️' \
  && bad "⑰阴性对照红了:3.24.12 量过却还在提示(尺子恒红)" \
  || ok "⑰阴性对照:量过的 3.24.12 一声不吭"
# 阴性对照二:大版本都没验过的仍走原来那条告警
OUT17C=$(g17 3.25.9)
printf '%s' "$OUT17C" | grep -q '这个大版本没验过' \
  && ok "⑰3.25.9(大版本没验过)仍走原来的告警,没被新分支吞掉" \
  || bad "⑰3.25.9 的大版本告警丢了"
rm -rf "$TMP/v17"

# ── 18) 「上游把那段挪走了」不许报成「锚点漂了」(2026-10-28,3.28.5)──────────────
#    3.28.5 把摘要阈值整段从 cursor-agent-host 挪到另两条 bundle(那两条仍 3/3)。
#    旧逻辑两种情况都回 `锚A count=0` ⇒ ①被当成漂移去追 ②新加的红字把无害情况
#    报成「没打上会很慢」= 假红,让人去跑一个修不了也不用修的 REPAIR。
#    判据用上游自己的字段名 `unusedTokensThresholdTo…`(不被 minify)。
#    阳性对照:造一份**真漂移**(有阈值、但锚点被摇坏)⇒ 必须仍报漂移、仍红。
echo "--- 18) 不适用 vs 真漂移要分得开 ---"
if [ ! -f "$X8" ]; then
  skip "⑱:缺 pristine agent-exec fixture(见 ⑧)"
else
  # 🔴 ⑧/⑭ 跑完后 $X8 已被打过/摇坏 ⇒ 这里必须重铺一份 pristine,
  #    否则"找不到锚点 A"会被当成本腿的结论(实际是上一腿的残留)。
  mk8
  N18="$TMP/leg18-na.js"; D18="$TMP/leg18-drift.js"
  # (a) 不适用:把阈值名整段删掉(= 上游挪走的样子)
  python3 - "$X8" "$N18" <<'PY18'
import sys,re
s=open(sys.argv[1],encoding="utf8",errors="surrogateescape").read()
s2=re.sub(r"unusedTokensThresholdTo(Start|Persist)BackgroundSummarization","__moved_away__",s)
assert s2!=s, "fixture 里本来就没有阈值名,这腿造不出来"
open(sys.argv[2],"w",encoding="utf8",errors="surrogateescape").write(s2)
PY18
  # (b) 真漂移:阈值名留着,只摇锚点 A
  python3 - "$X8" "$D18" <<'PY18B'
import sys
s=open(sys.argv[1],encoding="utf8",errors="surrogateescape").read()
old="usedTokens:0,maxTokens:0,breakdown:void 0"
assert old in s, "fixture 里找不到锚点 A"
open(sys.argv[2],"w",encoding="utf8",errors="surrogateescape").write(s.replace(old,"usedTokens:0,maxTokens:0,breakdownX:void 0"))
PY18B
  R18A=$(CX_CTXWIN_APPLY_TO_FILE="$N18" CX_CTXWIN_KIND=min CX_CTXWIN_OUT=/dev/null node "$JS" 2>&1 | tail -1)
  R18B=$(CX_CTXWIN_APPLY_TO_FILE="$D18" CX_CTXWIN_KIND=min CX_CTXWIN_OUT=/dev/null node "$JS" 2>&1 | tail -1)
  case "$R18A" in *不适用*) ok "⑱阈值被挪走 ⇒ 说「不适用」而不是「锚点漂了」" ;; *) bad "⑱不适用被报成别的:$R18A" ;; esac
  case "$R18B" in *"锚A count=0"*) ok "⑱阳性对照:真漂移仍报锚A count=0(没被新判据吞掉)" ;; *) bad "⑱真漂移报成了:$R18B" ;; esac
  # DOCTOR 侧:不适用的那份不许进红字计数
  FH18="$TMP/leg18-app"; rm -rf "$FH18"; mkdir -p "$FH18/extensions/cursor-agent-exec/dist" "$FH18/out/vs/workbench"
  cp "$LIVE_RES/package.json" "$FH18/package.json" 2>/dev/null
  cp "$N18" "$FH18/extensions/cursor-agent-exec/dist/main.js"
  L18=$(CURSOR_APP_ROOT="$FH18" node "$JS" --doctor 2>&1 | sed -n '/ctxwin(/,/noloop(/p')
  printf '%s' "$L18" | grep -q '不适用' && ok "⑱DOCTOR 把挪走的那份标成「不适用」" \
    || { bad "⑱DOCTOR 没标不适用:"; printf '%s\n' "$L18" | head -4; }
  printf '%s' "$L18" | grep -q '件C 没打全' \
    && bad "⑱DOCTOR 仍把不适用算成「没打全」= 假红" \
    || ok "⑱不适用不计入红字(不会让人去跑无用的 REPAIR)"
fi

# ── 19) 「刷模型」顺手查补丁在不在(2026-10-28)────────────────────────────────
#    因果链(算出来的,不是猜):Cursor **自动升级会冲掉全部 bundle 补丁**,升级锁 10-13
#    才默认开 ⇒ 10-13 前装的人升级后件C 失效 ⇒ 阈值 min(300-1e4, 300*.9) = **-9700**
#    ⇒ used>=-9700 恒真 ⇒ 每轮都压缩(单次 43~98s),他只会说「Cursor 比 Claude 慢」。
#    「刷模型」是同事唯一会主动用的入口 ⇒ 让它顺手查一次。
#    判据:补丁齐全必须**一声不吭**(别天天吓人);被抹掉必须点名 + 写后果 + 指向 REPAIR。
echo "--- 19) 刷模型顺手查补丁 ---"
if [ ! -d "$LIVE_RES/out/vs/workbench" ]; then
  skip "⑲:本机没有 live Cursor"
else
  PH="$TMP/leg19"; rm -rf "$PH"; mkdir -p "$PH/ok" "$PH/bad"
  for d in ok bad; do
    ( cd "$LIVE_RES" && tar -cf - package.json out/vs/workbench/workbench.desktop.main.js         extensions/cursor-agent-exec/dist/main.js         extensions/cursor-agent-host/dist/agent-host-daemon/dist/bin/daemon.cjs ) | tar -xf - -C "$PH/$d"
  done
  python3 - "$PH/bad" <<'PY19'
import pathlib,sys
# 模拟自动升级冲掉补丁:抹掉两种 marker
for f in list(pathlib.Path(sys.argv[1]).rglob("*.js"))+list(pathlib.Path(sys.argv[1]).rglob("*.cjs")):
    s=f.read_text(encoding="utf8",errors="surrogateescape")
    f.write_text(s.replace("/*@cx-ctxwin:v3*/","").replace("/*@cxteam-byokforce*/",""),encoding="utf8",errors="surrogateescape")
PY19
  ph() { CURSOR_APP_ROOT="$1" node -e '
const fs=require("fs"),path=require("path"),os=require("os");
const src=fs.readFileSync(process.argv[1],"utf8");
const head=src.slice(src.indexOf("const EXEC = process.execPath"), src.indexOf("const STATE_DB"));
const cw=src.slice(src.indexOf("const CTXWIN_MARKER"), src.indexOf("const CW_A_MIN"));
const rel=src.slice(src.indexOf("const CW_RELEVANT"), src.indexOf("function ctxwinApplyToText"));
const fn=src.slice(src.indexOf("function patchHealth()"), src.indexOf("async function refreshModels"));
const r=new Function("fs","path","os","process", head+cw+rel+fn+"; return patchHealth();")(fs,path,os,process);
console.log(JSON.stringify(r));' "$JS"; }
  R19A=$(ph "$PH/ok"); R19B=$(ph "$PH/bad")
  printf '%s' "$R19A" | grep -q '"ok":true' && ok "⑲补丁齐全 ⇒ 一声不吭(不天天吓人)"     || bad "⑲补丁齐全却报警了:$R19A"
  printf '%s' "$R19B" | grep -q '"ok":false' && ok "⑲补丁被冲掉 ⇒ 报出来"     || bad "⑲补丁被冲掉却没报:$R19B"
  printf '%s' "$R19B" | grep -q '每轮自动压缩对话' && ok "⑲写了后果(发一句要等很久)"     || bad "⑲没写后果,同事看不出这跟「慢」有关"
  printf '%s' "$R19B" | grep -q '件C' && ok "⑲点名是哪个补丁(件C)" || bad "⑲没点名"
  # 阳性对照:旧行为(刷模型压根不查)⇒ 屏幕上一个字都没有
  grep -q 'patchHealth()' "$JS" && ok "⑲阳性对照:旧版没有 patchHealth,这一行就是新增的那把尺子"     || bad "⑲patchHealth 不在脚本里"
  # 只读:不许在 refreshModels 里自动改 bundle(那要先退出 Cursor,而它正跑在 Cursor 里)
  sed -n '/async function refreshModels/,/^}/p' "$JS" | grep -q 'writeFileSync'     && bad "⑲refreshModels 里出现了写文件 —— 不许在 Cursor 开着时偷偷改 bundle"     || ok "⑲只读:没在刷模型里自动改 bundle"
fi

# ── 20) 压缩现场探针(2026-10-28)────────────────────────────────────────────
#    官方 preCompact 钩子在压缩真发生时被调用,payload 带 context_window_size。
#    件C 失效时它是档位数字(300/500/1),正常时 300000 ⇒ 这是**现场证据**,
#    比让同事去数 marker 直接。官方明说这个钩子拦不住压缩,所以它只是量具。
#    判据:①坏值判 tier-number ②正常值判 real-tokens ③垃圾/空输入不许崩(否则同事发不出消息)
#    ④stdout 必须是合法 JSON ⑤合并写 hooks.json 时用户自己的钩子一个字不能动。
echo "--- 20) 压缩现场探针 ---"
PROBE="$HERE/cursor_precompact_probe.js"
if [ ! -f "$PROBE" ]; then
  skip "⑳:包里没有 cursor_precompact_probe.js"
else
  # 🔴 探针是追加写的 ⇒ 同一个 HOME 跑两遍计数就变 2,断言「=1」会莫名其妙地红。
  #    每次重铺一个干净的 HOME,量的才是这一次的三发。
  P20="$TMP/leg20"; rm -rf "$P20"; mkdir -p "$P20"
  j20() { echo "$1" | HOME="$P20" node "$PROBE" 2>&1; }
  O20A=$(j20 '{"trigger":"auto","context_window_size":300,"context_tokens":20100}')
  O20B=$(j20 '{"trigger":"auto","context_window_size":300000,"context_tokens":271000}')
  O20C=$(j20 'not json at all'); RC20C=$?
  O20D=$(printf '' | HOME="$P20" node "$PROBE" 2>&1); RC20D=$?
  for pair in "A:$O20A" "B:$O20B" "C:$O20C" "D:$O20D"; do
    case "${pair#*:}" in "{}") ;; *) bad "⑳stdout 不是 {}(会干扰 Cursor):${pair%%:*} ⇒ ${pair#*:}" ;; esac
  done
  ok "⑳四种输入 stdout 都是合法 JSON {}"
  { [ "$RC20C" = 0 ] && [ "$RC20D" = 0 ]; } && ok "⑳垃圾输入/空输入退出码 0(不会让同事发不出消息)" \
    || bad "⑳异常输入退出码非 0(C=$RC20C D=$RC20D)"
  R20=$(HOME="$P20" node "$PROBE" --report 2>&1)
  printf '%s' "$R20" | grep -qE '档位数字\(坏\)[[:space:]]*1' && ok "⑳坏值(窗口=300)判成档位数字" \
    || { bad "⑳坏值没判出来:"; printf '%s\n' "$R20" | head -4; }
  printf '%s' "$R20" | grep -qE '真实 token\(正常\)[[:space:]]*1' && ok "⑳正常值(窗口=300000)判成真实 token(不是恒红)" \
    || bad "⑳正常值被误判"
  printf '%s' "$R20" | grep -q '每一轮都压缩' && ok "⑳抓到坏值时写了后果 + 指向 REPAIR" || bad "⑳没写后果"
  # 阳性对照:界必须在 4096(300 判坏、200000 判好);5000 这种中间值也该判真实
  O20E=$(j20 '{"context_window_size":5000}')
  HOME="$P20" node "$PROBE" --report 2>/dev/null | grep -qE '真实 token\(正常\)[[:space:]]*2' \
    && ok "⑳界用 4096:5000 判真实(与件C 的 __cxN 同界)" || bad "⑳界不对:5000 没判成真实"
  # ⑤ 合并写:用户原有钩子不许动
  H20="$TMP/leg20-home"; rm -rf "$H20"; mkdir -p "$H20/.cursor"
  printf '{"version":1,"hooks":{"beforeSubmitPrompt":[{"command":"echo mine"}]}}' > "$H20/.cursor/hooks.json"
  HOME="$H20" node -e '
const fs=require("fs"),path=require("path"),os=require("os");
const src=fs.readFileSync(process.argv[1],"utf8");
const head=src.slice(src.indexOf("const EXEC = process.execPath"), src.indexOf("const BUNDLES = ["));
const cd=src.slice(src.indexOf("const CARHER_DIR"), src.indexOf("const KEY_COPY"));
const fn=src.slice(src.indexOf("const PROBE_REL"), src.indexOf("function installImageSkill"));
new Function("fs","path","os","process","__dirname","console", head+cd+fn+"; installPrecompactProbe(false);")(fs,path,os,process,process.argv[2],{log(){}});
' "$JS" "$HERE" 2>/dev/null
  python3 - "$H20/.cursor/hooks.json" <<'PY20' && ok "⑳合并写:用户原有钩子保住 + 我们那条加上了 + 留了备份" || bad "⑳合并写坏了(见上)"
import json,sys,pathlib
d=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf8"))
h=d.get("hooks",{})
assert [x.get("command") for x in h.get("beforeSubmitPrompt",[])]==["echo mine"], "用户原有钩子被动了"
pc=h.get("preCompact") or []
assert any("cursor_precompact_probe.js" in (x.get("command") or "") for x in pc), "我们那条没加上"
assert pathlib.Path(sys.argv[1]+".carher-bak").exists(), "没留备份"
PY20
fi

# ── 21) 安装器要用的辅助文件必须真打进包(2026-10-28)────────────────────────
#    今天踩过两次同形的坑:①「刷模型」跑的是 ~/.carher 里的旧副本 ②探针写完了但
#    package_team_setup.sh 没 cp 它 ⇒ 安装器那一步静默跳过。**发出去 ≠ 装上**。
#    🔴 判据只认**产物**:不看打包脚本里有没有那行 cp —— 第一版那么写,结果把
#    `cp "$HERE/../../codex-oneclick/src/carher_image.js"` 这种跨目录来源误报成缺失。
#    凡是 cursor_team_setup.js 按 __dirname 找的同目录文件,zip 里就必须有同名条目。
echo "--- 21) 辅助文件打进包了吗(按产物判)---"
if [ ! -f "$HERE/cursor-mac.zip" ]; then
  skip "㉑:还没打过包(先跑 sh package_team_setup.sh)"
else
  NEED=$(grep -oE 'path\.join\(__dirname, "[A-Za-z0-9_.-]+"\)' "$JS" | sed 's/.*"\(.*\)".*/\1/' | sort -u)
  Z21="$TMP/zip21.txt"; unzip -l "$HERE/cursor-mac.zip" > "$Z21" 2>/dev/null
  # 自检:列表里必须有安装器本体,否则这把尺子在量空气
  grep -q 'cursor_team_setup.js' "$Z21" || bad "㉑尺子坏了:zip 列表里连安装器本体都没有"
  # 🔴 有意不随包的要豁免,否则这腿恒红、下次没人看它:
  #    lark-skills 是 09-03 用户定的「默认不带」(LARK_SKILLS=1 才打进去,且安装器要 --lark),
  #    而且安装器 fs.existsSync 不在时会**明说原因**而不是静默跳过 ⇒ 不算本腿要抓的病。
  OPTIN21="lark-skills"
  M21=""
  for f in $NEED; do
    case " $OPTIN21 " in *" $f "*) continue ;; esac
    grep -qF "/$f" "$Z21" || M21="$M21 $f"
  done
  [ -z "$M21" ] && ok "㉑安装器按 __dirname 取的文件 zip 里都有(豁免按需项:$OPTIN21)" \
    || bad "㉑这些文件安装器会去找、但 zip 里没有 ⇒ 那一步会静默跳过:$M21"
  # 豁免项也要有阳性对照:它缺席时安装器必须**说得出原因**,不许静默
  grep -q '包里没有 lark-skills/ 目录' "$JS" \
    && ok "㉑豁免项缺席时安装器会明说原因(不是静默跳过)" \
    || bad "㉑lark-skills 缺席时安装器没有明说的分支 ⇒ 不能豁免它"
  grep -qF '/cursor_precompact_probe.js' "$Z21" \
    && ok "㉑探针在 zip 里(本轮新增那个)" || bad "㉑探针没进 zip"
fi

# ── 22) 补丁自愈(2026-10-28)───────────────────────────────────────────────
#    Cursor 自动升级冲掉补丁 ⇒ 件C 没了 ⇒ 每条消息都压缩。launchd 每 5 分钟拉
#    cursor_selfheal.sh:Cursor **关着**且补丁缺了才 --repair。五种状态都要量:
#      a) 补丁被冲掉 + Cursor 关着 ⇒ 必须补回来        (这是功能本身)
#      b) 补丁齐全                ⇒ 一个字节不动、不写日志(绝大多数时候)
#      c) Cursor 开着             ⇒ 一个字节不动        (安全闸)
#      d) 新版锚点认不出           ⇒ 1 小时内不重试      (防刷屏)
#      e) --uninstall 之后         ⇒ 自愈必须被摘掉      (否则会把补丁悄悄打回去 = 无视用户选择)
#    副本二进制必须是**真拷贝**不能是软链:软链会让 pgrep 量到本机开着的那个 Cursor。
echo "--- 22) 补丁自愈 ---"
SH22="$HERE/cursor_selfheal.sh"
# 🔴 `case … ;; esac` 写进 `$( )` 里 sh 会报 unexpected token —— 本 session 第二次踩
#    (第一次是页面终端命令)。所以挑种子写成普通函数,再用 $( ) 调它。
pick_pristine22() {
  for d in "$BAK"/*; do
    [ -d "$d" ] || continue
    case "$d" in *-cfgonly|*-preconfig) continue ;; esac
    [ -f "$d/workbench.desktop.main.js" ] || continue
    m=$(grep -c '@cxteam-' "$d/workbench.desktop.main.js" 2>/dev/null | tr -dc '0-9')
    [ "${m:-0}" = 0 ] && echo "$d"
  done | tail -1
}
PRIS22="$(pick_pristine22)"
if [ ! -f "$SH22" ]; then skip "㉒:没有 cursor_selfheal.sh"
elif [ -z "$PRIS22" ]; then skip "㉒:本机备份里没有 pristine workbench(造不出「升级后」的样子)"
elif [ ! -d "$LIVE_RES" ]; then skip "㉒:本机没有 live Cursor"
else
  A22="$TMP/leg22-app"; H22="$TMP/leg22-home"; rm -rf "$A22" "$H22"
  mkdir -p "$A22/Contents/MacOS" "$A22/Contents/Resources/app/out/vs/workbench" "$H22/.carher/cursor-setup" \
           "$H22/Library/Application Support/Cursor/User/globalStorage"
  cp "$APP/Contents/MacOS/Cursor" "$A22/Contents/MacOS/Cursor"
  ln -sfn "$APP/Contents/Frameworks" "$A22/Contents/Frameworks"
  # 🔴 没有 Info.plist 的 Cursor 二进制**一启动就被系统 SIGKILL(rc=137)**,而且没有任何输出。
  #    单变量实测 10-28:不链 0/5 存活、链上 5/5。第一版漏了它,整腿恒红,
  #    我还先误判成「偶发」—— 因为手工测的那几次碰巧都带着 Info.plist。
  ln -sfn "$APP/Contents/Info.plist" "$A22/Contents/Info.plist"
  ELECTRON_RUN_AS_NODE=1 "$A22/Contents/MacOS/Cursor" -e '1' >/dev/null 2>&1 \
    || bad "㉒fixture 自检:副本 Cursor 二进制起不来(rc=$?)⇒ 下面全部无判别力"
  R22="$A22/Contents/Resources/app"
  cp "$LIVE_RES/package.json" "$R22/"
  # 只放 workbench 两份 + 件C/件D 目标(够 repair 跑,不必拷 760MB)
  for rel in extensions/cursor-agent-exec/dist/main.js extensions/cursor-local-agent-runtime/dist/main.js \
             extensions/cursor-agent-host/dist/main.js extensions/cursor-agent-host/dist/agent-host-daemon/dist/bin/daemon.cjs; do
    mkdir -p "$R22/$(dirname "$rel")"; cp "$LIVE_RES/$rel" "$R22/$rel"
  done
  wipe22() {   # = 自动升级后的样子:换回原厂 bundle
    cp "$PRIS22/workbench.desktop.main.js" "$PRIS22/workbench.glass.main.js" "$R22/out/vs/workbench/"
    for f in "$PRIS22"/cxctxwin__*; do [ -f "$f" ] || continue
      rel=$(basename "$f" | sed 's/^cxctxwin__//; s|__|/|g'); cp "$f" "$R22/$rel"; done
  }
  cp "$JS" "$HERE/cursor_tier_catalog.json" "$H22/.carher/cursor-setup/"
  cp "$HOME/Library/Application Support/Cursor/User/globalStorage/state.vscdb" \
     "$H22/Library/Application Support/Cursor/User/globalStorage/" 2>/dev/null
  LOG22="$H22/Library/Logs/carher-cursor-selfheal.log"
  run22() { env HOME="$H22" CURSOR_APP="$A22" "$@" sh "$SH22"; }
  # 诊断:a 失败时把 repair 自己说了什么打出来(不靠猜)
  diag22() {
    echo "    ↳ 诊断 1:谁在用副本二进制跑(第一道闸看的就是这个)"
    pgrep -fl "$A22/Contents/MacOS/Cursor" 2>/dev/null | sed 's/^/      /' || echo "      (没有)"
    echo "    ↳ 诊断 2:补丁检测看到的"
    echo "      byokforce=$(bf22)  文件在吗=$([ -f "$R22/out/vs/workbench/workbench.desktop.main.js" ] && echo 在 || echo 不在)"
    echo "    ↳ 诊断 2b:每道 exit 0 的条件此刻的真值"
    echo "      BIN 可执行=$([ -x "$A22/Contents/MacOS/Cursor" ] && echo 是 || echo 否)  SETUP 在=$([ -f "$H22/.carher/cursor-setup/cursor_team_setup.js" ] && echo 是 || echo 否)  STAMP 在=$([ -f "$H22/.carher/cursor-selfheal.last" ] && echo 是 || echo 否)"
    [ -f "$H22/.carher/cursor-selfheal.last" ] && echo "      STAMP 内容=$(cat "$H22/.carher/cursor-selfheal.last")"
    echo "    ↳ 诊断 3:selfheal 的 -x 关键行"
    env HOME="$H22" CURSOR_APP="$A22" CX_SKIP_RUNNING_CHECK=1 sh -x "$SH22" 2>&1 \
      | grep -vE "^\+ (for f|p=|continue)" | sed -n '/need=1/,$p' | head -12 | sed 's/^/      /'
  }
  bf22() { grep -c 'cxteam-byokforce' "$R22/out/vs/workbench/workbench.desktop.main.js" 2>/dev/null | tr -dc '0-9'; }

  # a) 冲掉 + 关着 ⇒ 补回来
  # 🔴 每个子用例开头清防抖记录。第一版没清:H22 在 $TMP 里跨次复用不了,但同一次跑里
  #    ㉒d 写的「修失败」记录会挡住后面任何一次补 —— 而第一版 a 恰好排在 d 后面重跑时就恒红。
  rm -f "$H22/.carher/cursor-selfheal.last"; mkdir -p "$(dirname "$LOG22")"; : > "$LOG22"
  wipe22; [ "$(bf22)" = 0 ] || bad "㉒fixture 自检:wipe 之后居然还有补丁(种子不是原厂)"
  run22 env CX_SKIP_RUNNING_CHECK=1
  if [ "$(bf22)" -gt 0 ]; then ok "㉒a 补丁被冲掉 + Cursor 关着 ⇒ 自己补回来了"
  else bad "㉒a 没补回来(见 $LOG22)"; diag22; fi
  grep -q '修好了' "$LOG22" 2>/dev/null && ok "㉒a 日志写了「修好了」" || bad "㉒a 日志没写"

  # b) 齐全 ⇒ 不动、不写
  S22=$(shasum -a 256 "$R22/out/vs/workbench/workbench.desktop.main.js" | cut -c1-16); L22=$(wc -l < "$LOG22")
  run22 env CX_SKIP_RUNNING_CHECK=1
  [ "$S22" = "$(shasum -a 256 "$R22/out/vs/workbench/workbench.desktop.main.js" | cut -c1-16)" ] && [ "$L22" = "$(wc -l < "$LOG22")" ] \
    && ok "㉒b 补丁齐全 ⇒ 一个字节不动、不写日志" || bad "㉒b 补丁齐全却动了东西"

  # c) 开着 ⇒ 不动(起一个命令行 = 副本二进制、不带 .js 的进程冒充 GUI)
  wipe22; rm -f "$H22/.carher/cursor-selfheal.last"
  ( exec -a "$A22/Contents/MacOS/Cursor" sleep 20 ) & FK22=$!; sleep 1
  run22 env   # 注意:这里**不**设 CX_SKIP_RUNNING_CHECK —— 量的就是闸
  kill $FK22 2>/dev/null; wait $FK22 2>/dev/null
  [ "$(bf22)" = 0 ] && ok "㉒c Cursor 开着 ⇒ 一个字节不动(安全闸)" || bad "㉒c Cursor 开着却动手打补丁了!"
  # c 的阳性对照:小代理那种「Cursor 二进制 + .js」进程**不许**挡住自愈(否则 7×24 永不触发)
  rm -f "$H22/.carher/cursor-selfheal.last"
  ( exec -a "$A22/Contents/MacOS/Cursor /tmp/sidecar.js" sleep 20 ) & SC22=$!; sleep 1
  run22 env CX_SKIP_RUNNING_CHECK=1
  kill $SC22 2>/dev/null; wait $SC22 2>/dev/null
  [ "$(bf22)" -gt 0 ] && ok "㉒c 阳性对照:只有小代理(带 .js)在跑 ⇒ 不算开着,照常补回" \
    || bad "㉒c 小代理把自愈挡住了 ⇒ 装了小代理的机器自愈永远不触发"

  # d) 锚点认不出 ⇒ 防抖
  wipe22; rm -f "$H22/.carher/cursor-selfheal.last"; : > "$LOG22"
  python3 - "$R22/out/vs/workbench/workbench.desktop.main.js" <<'PY22'
import sys
p=sys.argv[1]; s=open(p,encoding="utf8",errors="surrogateescape").read()
open(p,"w",encoding="utf8",errors="surrogateescape").write(s.replace("this.setUseOpenAIKey(!1)","this.setUseOpenAIKey(!1,void 0)"))
PY22
  for _ in 1 2 3; do run22 env CX_SKIP_RUNNING_CHECK=1; done
  n22=$(grep -c '自动修复$' "$LOG22" 2>/dev/null | tr -dc '0-9')
  [ "${n22:-0}" = 1 ] && ok "㉒d 修不了时连跑 3 次只真跑 1 次(1 小时防抖)" || bad "㉒d 修不了还在反复重试(真跑了 ${n22:-0} 次)"
  grep -q '需要管理员出新锚点' "$LOG22" && ok "㉒d 日志说清了要找管理员" || bad "㉒d 没说清下一步"
  # d2) 管理员出了新锚点、同事 UPGRADE 换了安装器 ⇒ 防抖**不许**再挡(键里带安装器指纹)
  wipe22                                                     # 这次锚点是好的
  printf '\n// new-anchors %s\n' "$(date +%s)" >> "$H22/.carher/cursor-setup/cursor_team_setup.js"   # 模拟换了新安装器
  run22 env CX_SKIP_RUNNING_CHECK=1
  [ "$(bf22)" -gt 0 ] && ok "㉒d2 换了安装器 ⇒ 不被旧防抖挡,立刻补上" \
    || bad "㉒d2 换了新安装器还被旧防抖挡着 ⇒ 管理员修好了同事要白等一小时"

  # e) 卸载 ⇒ 摘掉
  grep -q 'removeSelfheal(false);' "$JS" && sed -n '/^async function revert()/,/^}/p' "$JS" | grep -q 'removeSelfheal' \
    && ok "㉒e --revert 入口先摘自愈" || bad "㉒e --revert 没摘自愈 ⇒ 回滚后 5 分钟补丁又回来"
  sed -n '/^async function uninstall(dry)/,/^}/p' "$JS" | grep -q 'removeSelfheal(dry)' \
    && ok "㉒e --uninstall 入口先摘自愈" || bad "㉒e --uninstall 没摘自愈 ⇒ 卸载后补丁会被悄悄打回去"
  # 自愈必须进包(㉑ 按 __dirname 抓不到 .sh,这里单独看产物)
  if [ -f "$HERE/cursor-mac.zip" ]; then
    unzip -l "$HERE/cursor-mac.zip" | grep -q 'cursor_selfheal.sh' && ok "㉒自愈脚本在 cursor-mac.zip 里" \
      || bad "㉒自愈脚本没进包 —— 重跑 package_team_setup.sh"
  fi
fi

# ── 23) Windows 版自愈:能在 mac 上验的部分(2026-10-28)───────────────────────
#    **没有 Windows 真机**。按规矩默认不装、要显式 --selfheal-win;这腿只验能离线验的:
#    ① 命令行引号配平、路径没有双反斜杠 ② 和「刷模型」skill 同一写法
#       (🔴 别写成「那条在 Windows 上跑通过」—— 查过,**没有任何证据**它在同事 Windows 上跑过;
#        SpendLogs 看不到 skill 执行,记忆里也没有。同形只保证两处一起改,不保证能用)
#    ③ 默认不装(没给开关就跳过并明说)④ 卸载路径会删计划任务。
#    🔴 这四条全绿 ≠ Windows 上能用。屏幕上已明说「未在 Windows 真机验证」。
echo "--- 23) Windows 版自愈(离线可验部分)---"
W23=$(node -e '
const fs=require("fs"),path=require("path");
const src=fs.readFileSync(process.argv[1],"utf8");
const a=src.indexOf("const SELFHEAL_TASK"), b=src.indexOf("function installSelfhealWin");
const D=String.raw`C:\Users\u\.carher`, B=()=>String.raw`C:\Users\u\AppData\Local\Programs\cursor\Cursor.exe`;
console.log(new Function("path","CARHER_DIR","cursorBin", src.slice(a,b)+"; return selfhealWinCmd();")(path.win32,D,B));' "$JS" 2>&1)
NQ=$(printf '%s' "$W23" | tr -cd '"' | wc -c | tr -d ' ')
[ $((NQ % 2)) = 0 ] && [ "$NQ" -gt 0 ] && ok "㉓命令行双引号配平($NQ 个)" || bad "㉓引号不配平:$W23"
printf '%s' "$W23" | grep -q '\\\\' && bad "㉓路径里出现双反斜杠:$W23" || ok "㉓路径没有双反斜杠"
printf '%s' "$W23" | grep -q -- '--selfheal"$' && ok "㉓调的是 --selfheal(判定在 JS 里,mac/Windows 共用一份)" || bad "㉓没调 --selfheal:$W23"
# ② 和刷模型 skill 同一写法
grep -q "cmd /c \"set ELECTRON_RUN_AS_NODE=1&& \"' + CB + '\" \"' + self + '\" --refresh-models\"" "$JS" \
  && ok "㉓与「刷模型」skill 同一种 cmd /c 写法(两处同形,改一处会提醒改另一处)" || bad "㉓写法和刷模型那条不一致"
# ③ 默认不装
grep -q 'process.argv.includes("--selfheal-win")' "$JS" && ok "㉓默认不装,要显式 --selfheal-win" || bad "㉓Windows 版默认就装了(没真机验过)"
grep -q '未在 Windows 真机验证' "$JS" && ok "㉓装上时屏幕明说未在真机验证" || bad "㉓没说清没验过"
# ④ 卸载会删
sed -n '/^function removeSelfheal(dry)/,/^}/p' "$JS" | grep -q 'removeSelfhealWin' \
  && ok "㉓--revert/--uninstall 在 Windows 上也会删计划任务" || bad "㉓Windows 卸载不删计划任务 ⇒ 会把补丁悄悄打回去"
# 判定只有一份:shell 包装里不许再出现「判 Cursor 开没开」「数 marker」的逻辑
grep -qE 'pgrep|cx-ctxwin|cxteam-byokforce' "$HERE/cursor_selfheal.sh" \
  && bad "㉓cursor_selfheal.sh 里又出现了判定逻辑(pgrep/marker)⇒ 与 JS 两份会漂" \
  || ok "㉓shell 包装只剩「找二进制再调 JS」,判定只有 JS 一份"

echo ""
[ "$SKIP" -gt 0 ] && echo "($SKIP 腿跳过——跳过不是通过,看上面原因)"
if [ "$FAIL" -gt 0 ]; then echo "❌ $FAIL 项失败"; exit 1; fi
echo "✅ 全过。接着:VERIFIED_VERSIONS 加新大版本 → sh package_team_setup.sh → 换飞书文档附件。"
