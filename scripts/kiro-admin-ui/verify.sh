#!/usr/bin/env bash
# kiro.rs Admin UI（:30890/admin）本地验收：干净上游 admin-ui + 本仓 admin-ui 补丁 → 编译 → mock 回放 live 池子 → Playwright。
# 全程不碰生产写接口：live 只读一次 GET /credentials（经 kiro-pool.py），mock 对所有写请求回 403 并记数，>0 就 STOP。
#
# 为什么要有这个脚本（10-18 补丁 0006 手搓了一遍，每步都有坑）
# ------------------------------------------------------------------
# 1. UI 改动在 188 构建链里只会被 cargo test 间接覆盖（rust-embed 编进二进制），**页面行为没人验**。
#    这里在本机直接编 admin-ui，用线上真实 176+ 个号的数据看「每个查询条件筛出几条」。
# 2. 🔴 本机内存紧时 `pnpm build` 里的 tsc 被 SIGKILL（rc=137，没有任何报错文字）⇒ 直接 `node …/tsc` / `node …/vite.js` 跑。
# 3. 补丁要在**干净上游**上按序打（0004 → 0006 …），不是在上次的工作树上继续改，否则补丁和产物对不上。
# 4. ui_test.py 的期望数是 Python 从同一份快照独立算的 ⇒ 判据和前端实现不同源。
#
# 用法
#   scripts/kiro-admin-ui/verify.sh                 # 打全部「改了 admin-ui 的补丁」，跑测试
#   scripts/kiro-admin-ui/verify.sh --keep          # 跑完不删工作树（要接着改代码时用）
#   KIRO_UPSTREAM=~/codes/kiro.rs  上游源码位置（默认），须在补丁基线 commit 上（见 patches/README.md）
#
# 产物：.work/kiro-admin-ui-verify/{tree,live-pool.json,shot.png,shot-combo.png}
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UP="${KIRO_UPSTREAM:-$HOME/codes/kiro.rs}"
PATCH_DIR="$REPO_ROOT/k8s/kiro-rs/patches"
OUT="$REPO_ROOT/.work/kiro-admin-ui-verify"
PORT="${KIRO_UI_PORT:-18931}"
KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
fail() { echo "🔴 STOP: $*"; exit 1; }

[ -d "$UP/admin-ui/src" ] || fail "没有上游源码 $UP（先 scripts/kiro-rs-src-sync.sh）"
want=$(grep -o 'master `[0-9a-f]\{8\}`' "$PATCH_DIR/README.md" | head -1 | grep -o '[0-9a-f]\{8\}' || true)
have=$(git -C "$UP" rev-parse --short=8 HEAD)
[ -z "$want" ] || [ "$want" = "$have" ] || fail "上游 HEAD=${have}，补丁基线=${want}（git -C $UP checkout $want）"

echo "== 1. 干净上游 admin-ui + 补丁"
rm -rf "$OUT/tree" && mkdir -p "$OUT/tree"
rsync -a --exclude node_modules --exclude dist "$UP/admin-ui" "$OUT/tree/"
UI_PATCHES=$(cd "$PATCH_DIR" && grep -l '^+++ b/admin-ui/' [0-9][0-9][0-9][0-9]-*.patch | grep -v '\.[0-9a-f]\{7\}\.patch$' | sort)
[ -n "$UI_PATCHES" ] || fail "没有改 admin-ui 的补丁"
for p in $UI_PATCHES; do
  ( cd "$OUT/tree" && patch -p1 -s --no-backup-if-mismatch < "$PATCH_DIR/$p" ) || fail "$p 打不上"
  echo "   ok $p"
done

echo "== 2. 编译（node 直跑 tsc/vite，绕开 pnpm 下 tsc 被 SIGKILL）"
cd "$OUT/tree/admin-ui"
pnpm install --frozen-lockfile --silent
node node_modules/typescript/bin/tsc -b || fail "tsc 类型检查失败"
node node_modules/vite/bin/vite.js build --logLevel warn || fail "vite build 失败"
ls dist/assets/index-*.js >/dev/null || fail "dist 里没有 bundle"

echo "== 3. 拉 live 池子快照（只读 GET /credentials）"
python3 - "$REPO_ROOT" "$OUT/live-pool.json" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("kp", f"{sys.argv[1]}/scripts/kiro-pool.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
p = m.get_pool(); json.dump(p, open(sys.argv[2], "w"), ensure_ascii=False)
print(f"   total={p['total']} available={p['available']} currentId={p['currentId']}")
PY

echo "== 4. mock + Playwright"
python3 "$REPO_ROOT/scripts/kiro-admin-ui/mock_server.py" "$OUT/tree/admin-ui/dist" "$OUT/live-pool.json" "$PORT" > "$OUT/mock.log" 2>&1 &
MOCK=$!
trap '{ kill $MOCK && wait $MOCK; } 2>/dev/null || true; [ "$KEEP" = 1 ] || rm -rf "$OUT/tree"' EXIT
for _ in $(seq 20); do curl -s -o /dev/null "http://127.0.0.1:$PORT/admin" && break; sleep 0.3; done
rc=0; python3 "$REPO_ROOT/scripts/kiro-admin-ui/ui_test.py" "$PORT" "$OUT/live-pool.json" "$OUT/shot.png" || rc=$?
writes=$(grep -c WRITE-ATTEMPT "$OUT/mock.log" || true)
[ "$writes" = 0 ] || fail "测试期间页面发了 $writes 个写请求（见 $OUT/mock.log）"
[ "$rc" = 0 ] || fail "ui_test.py 有 STOP（见上）"
echo "✅ UI 验收通过；截图 $OUT/shot.png $OUT/shot-combo.png；写请求 0"
