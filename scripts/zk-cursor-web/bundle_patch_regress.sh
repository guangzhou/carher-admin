#!/bin/sh
# bundle_patch_regress.sh —— 改了 cursor_team_setup.{js,py} 的 PATCHES 之后的四腿离线回归台架。
# 全程只读 live Cursor(不用退出、不改任何东西),产物都在临时目录。
#
#   sh bundle_patch_regress.sh                      # 三腿(旧版零漂移 / js==py / 假 app 端到端)
#   sh bundle_patch_regress.sh --new-bundles /tmp/cx319   # 再加第①腿:新版 bundle 锚点全 exactly-1
#   sh bundle_patch_regress.sh --fetch              # 自己去官方拉最新 dmg 抽 bundle,再跑全四腿
#
# 四腿(照 skill cursor-client-bundle-patch「新版本漂移修复流程」第 4 步):
#   ① 新版两条 bundle:每个非 multi 锚点 hits=1(multi ≥1)——证明新正则认得出新版
#   ② 旧版 pristine bundle 打完 == 当前 live 已打 bundle(BYTE-EQUAL)——防"修新版把老用户改坏"
#   ③ js 产物 == py 产物(BYTE-EQUAL)——双实现不许分叉
#   ④ 假 app 端到端 --repair 打上 + node --check 过 + 再跑一次全 SKIP(幂等)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
JS="$HERE/cursor_team_setup.js"
PY="$HERE/cursor_team_setup.py"
PROBE="$HERE/bundle_anchor_probe.js"
APP="${CURSOR_APP:-/Applications/Cursor.app}"
LIVE_RES="$APP/Contents/Resources/app"
BAK="${CURSOR_BACKUP_DIR:-$HOME/.cursor-team-setup-backup}"
TMP="$(mktemp -d)"
NEWDIR=""; FETCH=0; FAIL=0; SKIP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --new-bundles) NEWDIR="$2"; shift 2 ;;
    --fetch) FETCH=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
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

# ── ② 旧版 pristine 打完 == 当前 live 已打(零漂移真门) ──
echo "--- 2) 旧版零漂移(pristine 重打 vs live 已打)---"
LIVE_VER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version","?"))' "$LIVE_RES/package.json" 2>/dev/null || echo '?')"
REF=""
for d in "$BAK"/"$LIVE_VER"-*; do
  case "$d" in *-cfgonly) continue ;; esac
  [ -f "$d/workbench.desktop.main.js" ] && REF="$d"
done
if [ -z "$REF" ]; then
  skip "②:$BAK 下没有与 live 版本($LIVE_VER)对应的含 bundle 备份 —— live 若已升级到新版,这腿要在升级前跑"
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

echo ""
[ "$SKIP" -gt 0 ] && echo "($SKIP 腿跳过——跳过不是通过,看上面原因)"
if [ "$FAIL" -gt 0 ]; then echo "❌ $FAIL 项失败"; exit 1; fi
echo "✅ 全过。接着:VERIFIED_VERSIONS 加新大版本 → sh package_team_setup.sh → 换飞书文档附件。"
