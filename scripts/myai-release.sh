#!/usr/bin/env bash
# myai-release.sh —— 把本地 ai_token_dashboard 的一个提交发到 243（构建在 188）。
#
# 用法:
#   HOST243_PW='...' scripts/myai-release.sh build            # 188 构建 + 校验 + 导入 243 并 pin（不动 pod）
#   HOST243_PW='...' scripts/myai-release.sh deploy           # 备份 → 金丝雀(chat) → 主看板 → realtime → sync，逐个等就绪
#   HOST243_PW='...' scripts/myai-release.sh rollback         # 按发布前记录的镜像逐个换回
#   可调: REPO=<本地仓库，默认 .work/myai-admin-perf/ai_token_dashboard>  COMMIT=<默认 HEAD>
#         LOCK=<pip freeze 约束文件，默认用 188 上 9f250f8 线上镜像的 freeze>
#
# 为什么这样（每条都踩过）:
#   - 只打包已提交内容（git archive），镜像里的源码要和提交逐文件 sha 一致才继续
#   - pip 依赖必须锁：不锁直接 pip install 会漂（10-18 实测 certifi/pluggy 变了）
#   - 本地 tests 改前就有 15 个失败（前端/静态资源断言），所以比的是"失败集合不比基线多"，不是全绿
#   - 🔴 worker 逐个 rollout、中间等 30s：10-20 同时 rollout 两个 worker，启动时 apply_usage_schema
#     互锁（DeadlockDetected / LockNotAvailable），各自重启 1~2 次
#   - 镜像导入走 host-243-image-import.sh（会 pin；tar 留在 /Data/migration，image GC 删了还能重导）
#   - 金丝雀先换 chat 入口那份（只服务 chat.auto-link.com.cn/myai/），验收过了再换主看板
# 足迹: 188 /Data/myai-build-<commit>/；243 /Data/migration/image-perf-<commit>.tar、
#       usage_backup_<ts>_pre-perf-<commit>.dump（usage_daily/coverage/membership/refresh_requests 数据）、
#       images-pre-perf-<commit>.txt（回滚依据）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${REPO:-$ROOT/.work/myai-admin-perf/ai_token_dashboard}"
ACTION="${1:-}"
H188=cltx@10.68.13.188
LOCK="${LOCK:-/Data/myai-build-2818c0d/freeze-9f.txt}"
[ -d "$REPO/.git" ] || { echo "没有本地仓库 $REPO" >&2; exit 2; }
C="${COMMIT:-$(git -C "$REPO" rev-parse --short HEAD)}"
TAG="ai-token-dashboard:perf-$C"
RUN="$ROOT/scripts/host-243-run.sh"

build(){
  [ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ] || { echo "$REPO 有未提交的改动；只发已提交内容" >&2; exit 1; }
  echo "== 本地测试（失败集合不能比 9f250f8 基线多）"
  local base="$REPO/../baseline.txt"
  [ -f "$base" ] || { echo "缺基线 ${base}（在 9f250f8 上跑一遍 pytest 生成）" >&2; exit 1; }
  # 🔴 pytest 有失败就 rc=1，pipefail 会把它带到整条管道 ⇒ set -e 直接静默杀掉脚本（首跑就这样）。
  #    这里的判据是"失败集合"，不是 rc，所以 rc 一律吞掉；但输出里必须有 passed 行，否则是 pytest 本身没跑起来
  local out; out=$(cd "$REPO" && .venv/bin/python -m pytest tests -q -p no:cacheprovider 2>&1 || true)
  printf '%s\n' "$out" | grep -qE "[0-9]+ passed" || { echo "pytest 没跑起来：" >&2; printf '%s\n' "$out" | tail -5 >&2; exit 1; }
  local now; now=$(printf '%s\n' "$out" | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort -u || true)
  local extra; extra=$(comm -13 <(grep -E '^(FAILED|ERROR)' "$base" | sort -u) <(printf '%s\n' "$now" | grep -v '^$' | sort -u) || true)
  [ -z "$extra" ] || { echo "新增失败：" >&2; echo "$extra" >&2; exit 1; }
  echo "   OK（$(printf '%s\n' "$out" | grep -oE '[0-9]+ passed' | tail -1)；失败 $(printf '%s\n' "$now" | grep -c FAILED || true) 个，均在基线内）"

  echo "== 188 构建 $TAG"
  git -C "$REPO" archive --format=tgz "$C" | ssh -o BatchMode=yes $H188 \
    "rm -rf /Data/myai-build-$C && mkdir -p /Data/myai-build-$C/src && cat > /Data/myai-build-$C/src.tgz && tar xzf /Data/myai-build-$C/src.tgz -C /Data/myai-build-$C/src"
  ssh -o BatchMode=yes $H188 "C=$C TAG=$TAG LOCK=$LOCK bash -s" <<'EOF'
set -euo pipefail
D=/Data/myai-build-$C; cd $D/src
cp "$LOCK" requirements.lock.txt
sed "s#COPY requirements.txt .#COPY requirements.txt requirements.lock.txt ./#; s#pip install --no-cache-dir -r requirements.txt#pip install --no-cache-dir -r requirements.txt -c requirements.lock.txt#" Dockerfile > Dockerfile.pinned
grep -q "requirements.lock.txt" Dockerfile.pinned || { echo "Dockerfile 形状变了，锁版本的 sed 没生效" >&2; exit 1; }
docker build -q -f Dockerfile.pinned -t "$TAG" . | tail -1
docker run --rm --entrypoint pip "$TAG" freeze 2>/dev/null > ../freeze.txt
diff "$LOCK" ../freeze.txt >/dev/null || { echo "pip 依赖和约束文件不一致：" >&2; diff "$LOCK" ../freeze.txt >&2; exit 1; }
echo "   pip == 约束文件"
docker run --rm --entrypoint sh "$TAG" -c 'cd /app && find backend assets index.html -type f ! -path "*__pycache__*" | sort | xargs sha256sum' | sort -k2 > ../img.sha
find backend assets index.html -type f | xargs sha256sum | sort -k2 > ../git.sha
diff ../img.sha ../git.sha >/dev/null || { echo "镜像里的源码和提交不一致" >&2; exit 1; }
echo "   镜像源码 == commit $C"
docker save -o $D/image.tar "$TAG"
EOF
  echo "== 搬到 243 并导入 + pin"
  for i in 1 2 3; do
    ssh -o BatchMode=yes $H188 "cat /Data/myai-build-$C/image.tar" \
      | SSHPASS="$HOST243_PW" sshpass -e ssh -o StrictHostKeyChecking=no cltx@10.68.13.243 "cat > /Data/migration/image-perf-$C.tar" && break
    echo "   传输失败（第 $i 次），重试" >&2; sleep 3
  done
  "$ROOT/scripts/host-243-image-import.sh" "/Data/migration/image-perf-$C.tar"
}

# 依次 set image + 等就绪 + 检查重启数；任何一步失败就停（不自动回滚，留给人看）
deploy(){
  "$RUN" C="$C" TAG="$TAG" - <<'EOF'
set -uo pipefail
N=ai-token-dashboard; K="k3s kubectl -n $N"
U=$($K exec usage-db-0 -- printenv POSTGRES_USER); D=$($K exec usage-db-0 -- printenv POSTGRES_DB)
S k3s ctr -n k8s.io images ls -q | grep -qx "docker.io/library/$TAG" || { echo "节点上没有 ${TAG}，先跑 build" >&2; exit 1; }
TS=$(date -u +%Y%m%dT%H%M)
$K get deploy -o custom-columns=N:.metadata.name,C:.spec.template.spec.containers[0].name,IMG:.spec.template.spec.containers[0].image --no-headers > /Data/migration/images-pre-perf-$C.txt
echo "== 发布前镜像（回滚依据 /Data/migration/images-pre-perf-$C.txt）"; cat /Data/migration/images-pre-perf-$C.txt
OUT=/Data/migration/usage_backup_${TS}_pre-perf-$C.dump
$K exec usage-db-0 -- pg_dump -U $U -d $D -t usage_daily -t usage_sync_coverage -t usage_team_membership_daily -t usage_refresh_requests --data-only -Fc > $OUT
[ -s $OUT ] || { echo "备份为空，停止" >&2; exit 1; }
echo "== 备份 $OUT ($(du -h $OUT | cut -f1))"
roll(){  # $1 deploy  $2 container  $3 extra wait
  local d=$1 c=$2
  local before; before=$($K get pods -l app=$d -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
  $K set image deploy/$d $c=$TAG >/dev/null && $K rollout status deploy/$d --timeout=600s >/dev/null || { echo "!!! $d rollout 没完成，停止" >&2; return 1; }
  sleep ${3:-20}
  local bad; bad=$($K get pods --no-headers | awk -v d="$d" '$1 ~ "^"d"-[a-z0-9]+-[a-z0-9]+$" && $3=="Running" && $4!="0"{print $1" restarts="$4}')
  local img; img=$($K get deploy $d -o jsonpath='{.spec.template.spec.containers[0].image}')
  echo "   $d -> $img ${bad:+⚠️ $bad}"
  [ "$img" = "$TAG" ]
}
echo "== 金丝雀 ai-token-dashboard-chat"; roll ai-token-dashboard-chat dashboard 30 || exit 1
c=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:30810/api/health); [ "$c" = 200 ] || { echo "!!! 金丝雀 health=${c}，停止" >&2; exit 1; }
echo "== 主看板"; roll ai-token-dashboard dashboard 30 || exit 1
echo "== realtime worker"; roll usage-realtime-worker worker 40 || exit 1
echo "== sync worker"; roll usage-sync-worker worker 40 || exit 1
$K get pods --no-headers | grep -v Completed | awk '{print "   "$1, $3, "restarts="$4}'
EOF
  echo "== 外网验收（chat 入口）"
  "$ROOT/scripts/myai-chat-subpath/deploy.sh" verify | grep -vE "myai.carher.net" | tail -3
}

rollback(){
  "$RUN" C="$C" - <<'EOF'
set -uo pipefail
F=/Data/migration/images-pre-perf-$C.txt; [ -s $F ] || { echo "没有 ${F}，不知道换回哪个" >&2; exit 1; }
K="k3s kubectl -n ai-token-dashboard"
while read -r d c img; do
  case "$d" in cloudflared) continue;; esac
  echo "== $d -> $img"; $K set image deploy/$d $c=$img >/dev/null && $K rollout status deploy/$d --timeout=600s >/dev/null; sleep 30
done < $F
$K get deploy -o custom-columns=N:.metadata.name,IMG:.spec.template.spec.containers[0].image --no-headers
EOF
}

case "$ACTION" in
  build) : "${HOST243_PW:?需要 HOST243_PW}"; build ;;
  deploy) : "${HOST243_PW:?需要 HOST243_PW}"
          # 同步在跑时换 pod，新 pod 建表等锁 → liveness 杀（10-29 实测）；先等空闲
          [ "${SKIP_IDLE_WAIT:-}" = 1 ] || "$(dirname "$0")/myai-wait-sync-idle.sh" "${IDLE_WAIT_MAX:-1800}" || exit 1
          deploy ;;
  rollback) : "${HOST243_PW:?需要 HOST243_PW}"; rollback ;;
  *) echo "用法: $0 build|deploy|rollback（COMMIT=<sha> 可指定提交）" >&2; exit 2 ;;
esac
