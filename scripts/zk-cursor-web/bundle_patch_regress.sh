#!/bin/sh
# bundle_patch_regress.sh —— 改了 cursor_team_setup.{js,py} 的 PATCHES 之后的四腿离线回归台架。
# 全程只读 live Cursor(不用退出、不改任何东西),产物都在临时目录。
#
#   sh bundle_patch_regress.sh                      # 三腿(旧版零漂移 / js==py / 假 app 端到端)
#   sh bundle_patch_regress.sh --new-bundles /tmp/cx319   # 再加第①腿:新版 bundle 锚点全 exactly-1
#   sh bundle_patch_regress.sh --new-app "/Volumes/Cursor Installer/Cursor.app"   # ①b:新版件C 4/4
#   sh bundle_patch_regress.sh --fetch              # 自己去官方拉最新 dmg 抽 bundle,再跑全四腿
#
# 四腿(照 skill cursor-client-bundle-patch「新版本漂移修复流程」第 4 步):
#   ① 新版两条 bundle:每个非 multi 锚点 hits=1(multi ≥1)——证明新正则认得出新版
#  ①b 新版 app 的件C(ctxwin)四个目标全 OK ——🔴 2026-09-23 补:①只量 workbench 解锁锚点,
#      ctxwin 那四个 bundle 完全没进台架,3.21.18 的锚点 B/C 命中 0 就是这样发出去的
#   ② 旧版 pristine bundle 打完 == 当前 live 已打 bundle(BYTE-EQUAL)——防"修新版把老用户改坏"
#   ③ js 产物 == py 产物(BYTE-EQUAL)——双实现不许分叉
#   ④ 假 app 端到端 --repair 打上 + node --check 过 + 再跑一次全 SKIP(幂等)
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
NEWDIR=""; NEWAPP=""; FETCH=0; FAIL=0; SKIP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --new-bundles) NEWDIR="$2"; shift 2 ;;
    --new-app) NEWAPP="$2"; shift 2 ;;
    --fetch) FETCH=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
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
  echo "--- fetch: 官方最新 stable(darwin-arm64)---"
  META="$(curl -fsS https://api2.cursor.sh/updates/api/download/stable/darwin-arm64/cursor)" || { bad "取更新元数据失败"; META=""; }
  URL="$(printf '%s' "$META" | sed -n 's/.*"downloadUrl":"\([^"]*\)".*/\1/p')"
  VER="$(printf '%s' "$META" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
  if [ -n "$URL" ]; then
    echo "  版本 $VER"
    curl -fsSL "$URL" -o "$TMP/cursor.dmg" || bad "下载 dmg 失败"
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
if [ -n "$NEWAPP" ]; then
  NAPP_RES="$NEWAPP/Contents/Resources/app"
  [ -d "$NAPP_RES" ] || NAPP_RES="$NEWAPP"      # 也接受直接给 Resources/app
  if [ -f "$NAPP_RES/extensions/cursor-agent-exec/dist/main.js" ]; then
    CURSOR_APP_ROOT="$NAPP_RES" node "$CTXWIN" --dry >"$TMP/cw.txt" 2>&1
    sed 's/^/    /' "$TMP/cw.txt"
    NOK=$(grep -c '^OK ' "$TMP/cw.txt")
    NXX=$(grep -c '^XX ' "$TMP/cw.txt")
    # 判据:4 个 OK 且 0 个 XX。先断言自己数得到(NOK 非空且是数字),否则尺子坏了当红。
    case "$NOK" in ''|*[!0-9]*) bad "①b 尺子坏了:数不出 OK 行数";; *)
      if [ "$NOK" = "4" ] && [ "$NXX" = "0" ]; then ok "①b 件C 新版 4/4 锚点 exactly-1"
      else bad "①b 件C 新版 OK=$NOK XX=$NXX(须 4/0)"; fi ;;
    esac
  else skip "①b:$NEWAPP 里找不到 extensions/cursor-agent-exec/dist/main.js"; fi
else skip "①b:没给 --new-app / --fetch(新版本漂移时这腿必须跑)"; fi

# ── ② 旧版 pristine 打完 == 当前 live 已打(零漂移真门) ──
echo "--- 2) 旧版零漂移(pristine 重打 vs live 已打)---"
LIVE_VER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version","?"))' "$LIVE_RES/package.json" 2>/dev/null || echo '?')"
REF=""
for d in "$BAK"/"$LIVE_VER"-*; do
  case "$d" in *-cfgonly) continue ;; esac
  [ -f "$d/workbench.desktop.main.js" ] && REF="$d"
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
    else bad "②$b 与 live 已打 bundle 不一致(旧代产物漂了:$TMP/old-$b)"; cp "$TMP/old-$b" /tmp/ 2>/dev/null; fi
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
if [ -z "$CXNODE" ]; then
  skip "⑥:没找到 Cursor 的 electron(CXNODE 空),这腿没跑"
elif [ -z "$BYOK_SRC" ]; then
  skip "⑥:没给 --new-bundles / --fetch ⇒ 没有 pristine 输入,这腿没跑"
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

echo ""
[ "$SKIP" -gt 0 ] && echo "($SKIP 腿跳过——跳过不是通过,看上面原因)"
if [ "$FAIL" -gt 0 ]; then echo "❌ $FAIL 项失败"; exit 1; fi
echo "✅ 全过。接着:VERIFIED_VERSIONS 加新大版本 → sh package_team_setup.sh → 换飞书文档附件。"
