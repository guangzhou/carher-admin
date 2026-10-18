#!/usr/bin/env bash
# kiro.rs 镜像落地：188 tarball → 本机 → 198（分块）→ 242 containerd（特权 pod）→ [可选] set image + 验收。
#
# 为什么要有这个脚本（skill kiro-rs-ops §C 那段流水线每一步都有坑，10-15 一天手搓了三次）
# ------------------------------------------------------------------
# 1. 🔴 本机 → 198 整包 scp 会断（10-15 两次 Operation timed out，落地 0 字节 sha=e3b0c4…）；
#    198 也 ssh 不进 188。⇒ 本机中转，2MB 分块逐块 ssh cat，每块失败重试 3 次，三处对 sha256。
# 2. 🔴 进 242 只能特权 pod 挂 containerd socket（`ssh 242` 不存在）；`k3s ctr import` 不吃 gzip。
# 3. 🔴 Deployment 是 Recreate ⇒ 镜像没 import 成功就 set image = 纯停机。先 import、复核 `images ls`。
# 4. 两个容器都要改（`kiro-rs` + initContainer `seed-config`）。
# 5. 切之前备份 deploy JSON（0600）并打印回滚命令；切之后比池子 total/available/currentId 与切前一致。
#
# 用法
#   scripts/kiro-rs-ship.sh <镜像tag> <sha256>            # 只送进 242（不碰生产）
#   scripts/kiro-rs-ship.sh <镜像tag> <sha256> --keep-tarball  # 同上，198 留 ~/kiro-ship/img.tar.gz 给 e2e
#   scripts/kiro-rs-ship.sh <镜像tag> <sha256> --switch   # 送进 242 + 切生产（停机约 10s）
#   scripts/kiro-rs-ship.sh <镜像tag> <sha256> --switch --expect-ui '(\.\d{3})\d+/'  # 改了 admin-ui 时必带
#
# --expect-ui <字面串>：切完拉线上 Admin bundle，必须含这个字面串（grep -F），否则 STOP（但不自动回滚）。
#   🔴 只看「Admin UI 200 + 有 bundle」是假绿：旧镜像也 200、也有 bundle。字面串必须是补丁**新增**、
#   旧 bundle 里**没有**、压缩后仍保留的（字符串/正则字面量；变量名会被 vite 改掉；API 字段名如 lastUsedAt
#   旧组件本来就在用 ⇒ 旧镜像也命中 = 假绿，10-16 差点用它）。0004 用 '(\.\d{3})\d+/'，0006 用 '没有符合条件的凭据'。
#   改 UI 先跑 scripts/kiro-admin-ui/verify.sh（本机编 + mock 回放 live 池子 + Playwright），再 build/ship。
#
# 切完还要跑：scripts/kiro-lane-regress.sh --rounds 2（经 LiteLLM 打全部 kiro-* 道 + in_tok 基线比对）
set -euo pipefail

TAG="${1:?用法: $0 <镜像tag> <sha256> [--switch] [--keep-tarball]}"
SHA="${2:?用法: $0 <镜像tag> <sha256> [--switch] [--keep-tarball]}"
fail() { echo "🔴 STOP: $*"; exit 1; }
SWITCH=0; KEEP=0; EXPECT_UI=""
ARGS=("${@:3}")
for ((i=0; i<${#ARGS[@]}; i++)); do
  arg="${ARGS[$i]}"
  case "$arg" in
    --switch) SWITCH=1 ;;
    --expect-ui) i=$((i+1)); EXPECT_UI="${ARGS[$i]:-}"; [ -n "$EXPECT_UI" ] || fail "--expect-ui 要跟一个字面串" ;;
    # 🔴 10-16：不带它时第 3 步末尾删 198 上的 tarball ⇒ 紧接着跑 kiro-rs-e2e.sh --tarball 找不到文件，
    #    只能再分块送一遍。e2e 之前的那次 ship 带上它，e2e 跑完 `rm -rf ~/kiro-ship`。
    --keep-tarball) KEEP=1 ;;
    *) fail "未知参数 ${arg}（只认 --switch / --keep-tarball / --expect-ui <字面串>）" ;;
  esac
done

BUILD="${KIRO_BUILD_HOST:-cltx@10.68.13.188}"
PROD="${KIRO_PROD_HOST:-cltx@10.68.13.198}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$REPO_ROOT/work-kiro-ship-$TAG"     # .gitignore 已忽略 work-kiro-*/
IMG="docker.io/library/kiro-rs:$TAG"

shasum_of() { shasum -a 256 "$1" | awk '{print $1}'; }

echo "== 1. 188 → 本机"
mkdir -p "$WORK"; trap 'rm -rf "$WORK"' EXIT
scp -q "$BUILD:/Data/kiro-build/kiro-rs-$TAG.tar.gz" "$WORK/img.tar.gz"
[ "$(shasum_of "$WORK/img.tar.gz")" = "$SHA" ] || fail "本机 sha 不符（$(shasum_of "$WORK/img.tar.gz")）"
echo "   ✅ sha 一致"

echo "== 2. 本机 → 198（2MB 分块）"
ssh "$PROD" "rm -rf ~/kiro-ship && mkdir -m 700 ~/kiro-ship"
( cd "$WORK" && split -b 2m img.tar.gz part_ )
n=0
for f in "$WORK"/part_*; do
  b=$(basename "$f"); ok=0
  for _ in 1 2 3; do ssh "$PROD" "cat > ~/kiro-ship/$b" < "$f" && { ok=1; break; }; done
  [ $ok = 1 ] || fail "分块 $b 三次都没传上"
  n=$((n+1))
done
got=$(ssh "$PROD" 'cd ~/kiro-ship && cat part_* > img.tar.gz && rm part_* && sha256sum img.tar.gz' | awk '{print $1}')
[ "$got" = "$SHA" ] || fail "198 拼出来 sha 不符（${got}）"
echo "   ✅ $n 块，sha 一致"

echo "== 3. 198 → 242 containerd"
ssh "$PROD" "IMG='$IMG' SHA='$SHA' KEEP='$KEEP' bash -s" <<'EOF'
set -euo pipefail
K="sudo -n kubectl -n kiro-rs"
CUR=$($K get deploy kiro-rs -o jsonpath='{.spec.template.spec.containers[0].image}')
$K delete pod kiro-img-importer --ignore-not-found --wait=true >/dev/null
# importer 用当前在跑的镜像（242 上必然有，免拉取）
cat <<YAML | sudo -n kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: kiro-img-importer, namespace: kiro-rs}
spec:
  restartPolicy: Never
  nodeSelector: {kubernetes.io/hostname: aiyjy-litellm-242}
  tolerations: [{key: dedicated, operator: Equal, value: new-node, effect: NoSchedule}]
  containers:
  - name: shell
    image: $CUR
    command: ["sleep","900"]
    securityContext: {privileged: true}
    volumeMounts:
    - {name: sock, mountPath: /run/k3s/containerd/containerd.sock}
    - {name: hostbin, mountPath: /host-bin}
  volumes:
  - {name: sock, hostPath: {path: /run/k3s/containerd/containerd.sock, type: Socket}}
  - {name: hostbin, hostPath: {path: /usr/local/bin, type: Directory}}
YAML
trap "$K delete pod kiro-img-importer --wait=false >/dev/null 2>&1 || true" EXIT
$K wait --for=condition=Ready pod/kiro-img-importer --timeout=90s >/dev/null
$K cp ~/kiro-ship/img.tar.gz kiro-img-importer:/tmp/img.tar.gz
got=$($K exec kiro-img-importer -- sha256sum /tmp/img.tar.gz | awk '{print $1}')
[ "$got" = "$SHA" ] || { echo "🔴 STOP: 242 侧 sha 不符 $got"; exit 1; }
CTR="/host-bin/k3s ctr -a /run/k3s/containerd/containerd.sock"
$K exec kiro-img-importer -- sh -c "gunzip -c /tmp/img.tar.gz | $CTR images import - 2>&1 | grep unpacking"
# 🔴 先落变量再 grep：`exec … | grep -qx` 在 pipefail 下会因 grep 提前退出 ⇒ SIGPIPE ⇒ 假 STOP
# （10-16 镜像明明在 242，第二次送就报「没有」；时序决定，第一次送时没触发）
LS=$($K exec kiro-img-importer -- sh -c "$CTR images ls -q")
printf '%s\n' "$LS" | grep -qx "$IMG" || { echo "🔴 STOP: import 后 images ls 里没有 $IMG"; exit 1; }
if [ "$KEEP" = 1 ]; then
  echo "   ⚠️ tarball 留在 198:~/kiro-ship/img.tar.gz（给 kiro-rs-e2e.sh --tarball），用完 rm -rf ~/kiro-ship"
else
  rm -rf ~/kiro-ship
fi
echo "   ✅ $IMG 已在 242"
EOF

[ "$SWITCH" = 1 ] || { echo "== 未带 --switch：只送进 242，生产没动"; exit 0; }

echo "== 4. 切生产（Recreate，停机约 10s）"
pool() { python3 "$REPO_ROOT/scripts/kiro-pool.py" show 2>/dev/null | grep '^API' | sed 's/^API *//'; }
BEFORE=$(pool); echo "   切前 $BEFORE"
ssh "$PROD" "IMG='$IMG' TAG='$TAG' bash -s" <<'EOF'
set -euo pipefail
K="sudo -n kubectl -n kiro-rs"
BAK=~/kiro-rs-deploy.pre-$TAG.json
$K get deploy kiro-rs -o json > $BAK && chmod 600 $BAK
OLD=$($K get deploy kiro-rs -o jsonpath='{.spec.template.spec.containers[0].image}')
$K set image deploy/kiro-rs kiro-rs=$IMG seed-config=$IMG >/dev/null
$K rollout status deploy/kiro-rs --timeout=180s
echo "   备份 198:$BAK"
echo "   回滚: sudo kubectl -n kiro-rs set image deploy/kiro-rs kiro-rs=$OLD seed-config=$OLD"
EOF
AFTER=$(pool); echo "   切后 $AFTER"
[ "$BEFORE" = "$AFTER" ] || echo "⚠️ 池子读数切前切后不同（currentId 在两次读之间自然轮换也会变，人工看一眼）"

js=$(curl -s -m 8 http://10.68.13.198:30890/admin | grep -o '/admin/assets/index-[^"]*\.js' || true)
[ -n "$js" ] && echo "   Admin UI 200，bundle $js" || echo "⚠️ Admin UI 没拿到 bundle"
if [ -n "$EXPECT_UI" ]; then
  [ -n "$js" ] || fail "--expect-ui 要验 bundle，但没拿到 bundle 路径"
  # 先落变量再 grep（pipefail 下 curl | grep -q 会被 SIGPIPE 打成假 STOP，见 skill §C）
  body=$(curl -s -m 15 "http://10.68.13.198:30890$js" || true)
  [ -n "$body" ] || fail "bundle $js 拉回来是空的"
  printf '%s' "$body" | grep -Fq -- "$EXPECT_UI" \
    || fail "线上 bundle 里没有 「${EXPECT_UI}」 ⇒ 新 UI 没上（回滚命令见上）"
  echo "   ✅ 线上 bundle 含 「${EXPECT_UI}」"
fi
echo "✅ 下一步: scripts/kiro-lane-regress.sh --rounds 2"
