#!/usr/bin/env bash
# kiro.rs：拉上游 → 打本仓补丁 → cargo test 对照 → 构建镜像 → save tarball。全部在 188 上跑，不碰生产。
#
# 为什么要有这个脚本（10-15 升 v2026.10.1 时手搓踩过的，每条都差点漏）
# ------------------------------------------------------------------
# 1. 🔴 补丁真源 = 本仓 k8s/kiro-rs/patches/*.patch（按文件名排序打）。188 上的构建树只是产物，
#    每次从**干净上游**重建，避免「树里有个没进 git 的改动」。打完再跑一遍链式复现：
#    干净上游 + 仓库补丁 ⇒ 必须与构建树逐字相同。
# 2. 🔴 判据不是「全绿」：同一份 cargo test 在**干净上游**和**打补丁后**各跑一次，
#    失败集合必须相同；并且打补丁后的通过数 > 上游（补丁自带单测）。
# 3. 🔴 `warning: function `test_xxx` is never used` = 测试函数漏了 #[test]，cargo 不报错只是不跑。
#    10-15 移植 0001 时就漏了 2 个。脚本见到这句直接失败。
# 4. 两个 target 目录分开（共用会互相覆盖增量产物，读到的结果张冠李戴）；
#    cargo test 前置 admin-ui/dist/index.html（rust-embed 编译期要目录存在），构建前删掉，免得打进镜像。
# 5. 188 无 rust 工具链 ⇒ 测试跑在 rust:1.92-alpine 容器里，registry 缓存挂 /Data/kiro-build/cargo-registry。
# 6. `cargo fmt --check` 上游自己就不干净（10-15：9 个文件），不当门禁。
#
# 用法
#   scripts/kiro-rs-build.sh <上游sha或tag> <镜像tag>
#   scripts/kiro-rs-build.sh 22f36e69 v2026.10.1-carher
#   scripts/kiro-rs-build.sh 22f36e69 v2026.10.1-carher --skip-build   # 只打补丁+测试
#
# 产物（188）
#   /Data/kiro-build/kiro-up-pristine/   干净上游
#   /Data/kiro-build/kiro-up/            上游 + 补丁（下次改 kiro 从这里起）
#   /Data/kiro-build/kiro-rs-<tag>.tar.gz + 打印 sha256  ⇒ 交给 scripts/kiro-rs-ship.sh
#
# 补丁打不上（上游改了同一处）⇒ 脚本停在 dry-run，不留半成品。手动重做那个补丁的办法见
# k8s/kiro-rs/patches/README.md「升级时 0001 冲突」。
set -euo pipefail

REF="${1:?用法: $0 <上游sha|tag> <镜像tag> [--skip-build]}"
TAG="${2:?用法: $0 <上游sha|tag> <镜像tag> [--skip-build]}"
SKIP_BUILD=0; [ "${3:-}" = "--skip-build" ] && SKIP_BUILD=1

HOST="${KIRO_BUILD_HOST:-cltx@10.68.13.188}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCH_DIR="$REPO_ROOT/k8s/kiro-rs/patches"
# 存档的旧基线补丁（如 0001-…5ca5703.patch）不打；macOS bash 3.2 没有 mapfile
PATCHES=()
while IFS= read -r p; do PATCHES+=("$p"); done < <(cd "$PATCH_DIR" && ls [0-9][0-9][0-9][0-9]-*.patch | grep -v '\.[0-9a-f]\{7\}\.patch$' | sort)
[ "${#PATCHES[@]}" -gt 0 ] || { echo "🔴 $PATCH_DIR 下没有补丁"; exit 1; }
echo "补丁（${#PATCHES[@]} 个，按序）: ${PATCHES[*]}"

ssh "$HOST" "rm -rf /tmp/kiro-patches && mkdir /tmp/kiro-patches"
scp -q "${PATCHES[@]/#/$PATCH_DIR/}" "$HOST:/tmp/kiro-patches/"

# 远端命令行是 ssh 拼起来再交给 shell 的 ⇒ 带空格的值必须自己加引号（文件名只含 [0-9a-z.-]，单引号安全）
ssh "$HOST" "REF='$REF' TAG='$TAG' SKIP_BUILD='$SKIP_BUILD' PATCHES='${PATCHES[*]}' bash -s" <<'EOF'
set -euo pipefail
B=/Data/kiro-build; cd $B
fail() { echo "🔴 STOP: $*"; exit 1; }

echo "== 1. 拉干净上游 $REF"
rm -rf kiro-up-pristine.new && mkdir kiro-up-pristine.new
curl -fsSL -m 180 "https://codeload.github.com/hank9999/kiro.rs/tar.gz/$REF" | tar xz --strip-components=1 -C kiro-up-pristine.new \
  || fail "下载上游 $REF 失败"
rm -rf kiro-up-pristine && mv kiro-up-pristine.new kiro-up-pristine
echo "   version: $(grep -m1 '^version' kiro-up-pristine/Cargo.toml)"

echo "== 2. 打补丁（先全部 dry-run，任何一个打不上就停）"
rm -rf kiro-up.new && cp -a kiro-up-pristine kiro-up.new
for p in $PATCHES; do
  ( cd kiro-up.new && patch -p1 -s --dry-run --no-backup-if-mismatch < /tmp/kiro-patches/$p ) >/tmp/kiro-dry.log 2>&1 \
    || { cat /tmp/kiro-dry.log; rm -rf kiro-up.new; fail "$p 打不上（上游改了同一处，需重做该补丁）"; }
  ( cd kiro-up.new && patch -p1 -s --no-backup-if-mismatch < /tmp/kiro-patches/$p )
  echo "   ok $p"
done
[ -z "$(find kiro-up.new -name '*.orig' -o -name '*.rej')" ] || fail "有 .orig/.rej 残留"
rm -rf kiro-up && mv kiro-up.new kiro-up

echo "== 3. cargo test：干净上游 vs 打补丁后"
run_test() {  # $1=树
  mkdir -p $B/$1/admin-ui/dist $B/target-$1
  [ -f $B/$1/admin-ui/dist/index.html ] || echo '<html></html>' > $B/$1/admin-ui/dist/index.html
  docker run --rm -v $B/$1:/src-$1 -v $B/cargo-registry:/usr/local/cargo/registry -v $B/target-$1:/target \
    -e CARGO_TARGET_DIR=/target -w /src-$1 rust:1.92-alpine \
    sh -c 'apk add -q --no-cache musl-dev perl make >/dev/null 2>&1; cargo test --no-fail-fast 2>&1' > /tmp/kiro-test-$1.log 2>&1 || true
  grep -E '^error(\[|:)' /tmp/kiro-test-$1.log | head -5 && fail "$1 编译失败（日志 188:/tmp/kiro-test-$1.log）" || true
  grep -E '^test .* FAILED$' /tmp/kiro-test-$1.log | sort > /tmp/kiro-fail-$1.txt || true
}
passed() { grep -E '^test result' /tmp/kiro-test-$1.log | awk '{s+=$4} END {print s+0}'; }
run_test kiro-up-pristine; run_test kiro-up
P0=$(passed kiro-up-pristine); P1=$(passed kiro-up)
echo "   上游 passed=$P0 failed=$(wc -l < /tmp/kiro-fail-kiro-up-pristine.txt)"
echo "   补丁 passed=$P1 failed=$(wc -l < /tmp/kiro-fail-kiro-up.txt)"
diff /tmp/kiro-fail-kiro-up-pristine.txt /tmp/kiro-fail-kiro-up.txt >/dev/null || { diff /tmp/kiro-fail-kiro-up-pristine.txt /tmp/kiro-fail-kiro-up.txt; fail "失败集合与上游不同"; }
[ "$P1" -gt "$P0" ] || fail "打补丁后通过数 $P1 不大于上游 ${P0}（补丁单测没跑？）"
if grep -E 'function `test_[a-z0-9_]+` is never used' /tmp/kiro-test-kiro-up.log; then fail "上面的测试函数漏了 #[test]，不会被执行"; fi
echo "   ✅ 失败集合相同，补丁多跑 $((P1-P0)) 个，无漏 #[test]"

echo "== 4. 链式复现：干净上游 + 补丁 == kiro-up"
rm -rf /tmp/kiro-chain && cp -a kiro-up-pristine /tmp/kiro-chain
for p in $PATCHES; do ( cd /tmp/kiro-chain && patch -p1 -s --no-backup-if-mismatch < /tmp/kiro-patches/$p ); done
diff -r -x dist -x target /tmp/kiro-chain kiro-up >/dev/null || fail "链式复现与 kiro-up 不同"
rm -rf /tmp/kiro-chain; echo "   ✅ 逐字相同"

[ "$SKIP_BUILD" = 1 ] && { echo "== --skip-build，到此为止"; exit 0; }

echo "== 5. 构建 kiro-rs:${TAG}（约 4~5 分钟）"
rm -rf kiro-up/admin-ui/dist
( cd kiro-up && docker build -q -t kiro-rs:$TAG . ) >/dev/null || fail "docker build 失败"
docker save kiro-rs:$TAG | gzip -1 > kiro-rs-$TAG.tar.gz
echo "   tarball: $B/kiro-rs-$TAG.tar.gz"
sha256sum kiro-rs-$TAG.tar.gz
echo "✅ 下一步: scripts/kiro-rs-ship.sh $TAG <sha256>"
EOF
