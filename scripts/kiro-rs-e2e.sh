#!/usr/bin/env bash
# kiro.rs 新镜像隔离 E2E：在 198 上用 docker 起一个**只挂一个号、refreshToken 换成假值**的容器，打真 Kiro 上游。
# 不经 LiteLLM、不碰生产 Deployment；用完删容器和凭据副本。
#
# 为什么这样隔离（10-15 两次实测的形状）
# ------------------------------------------------------------------
# - 凭据从生产 pod 现读，只留一个号；refreshToken 换假值 ⇒ 盘上 accessToken 有效期内能用，
#   过期也不会去刷新，**不会把线上号的 token 轮转掉**（social 号刷新会轮转 rt ⇒ 线上那份当场死）。
#   脚本先检查 accessToken 至少还剩 10 分钟。
# - 198 上 docker 要 sudo；镜像 tarball 是 gzip，`docker load` 要先 gunzip（否则报 media type text/html）。
# - 判落点不认响应 model 字段（kiro 原样回显请求名）；映射落点由 cargo 单测管（含阳性对照）。
# - `tool_result` 夹图片这一例是 v2026.10.1 上游修的 bug：旧版把图片静默丢掉，
#   模型答「只收到文字 screenshot captured」。拿 `--image <旧tag>` 跑一遍就是阴性对照。
# - fable-5-1 400 Invalid model ≠ 镜像坏：先 `kiro-probe.py has fable --account <id>` 看号的目录。
#
# 用法
#   scripts/kiro-rs-e2e.sh <镜像tag>                 # 镜像需已在 198 的 docker 里，或给 --tarball
#   scripts/kiro-rs-e2e.sh v2026.10.1-carher --tarball ~/kiro-ship/img.tar.gz
#   scripts/kiro-rs-e2e.sh v2026.10.1-carher --cred-id 138
#
# 每例打 OK/STOP + 原因，分母 = 全部用例；全过 rc=0。
set -euo pipefail

TAG="${1:?用法: $0 <镜像tag> [--tarball 198上的路径] [--cred-id N]}"; shift
TARBALL=""; CRED_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tarball) TARBALL="$2"; shift 2 ;;
    --cred-id) CRED_ID="$2"; shift 2 ;;
    *) echo "未知参数 $1"; exit 2 ;;
  esac
done
PROD="${KIRO_PROD_HOST:-cltx@10.68.13.198}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

ssh "$PROD" "mkdir -m 700 -p ~/kiro-e2e"
scp -q "$REPO_ROOT/scripts/kiro_rs_e2e_cases.py" "$PROD:~/kiro-e2e/cases.py"

ssh "$PROD" "TAG='$TAG' TARBALL='$TARBALL' CRED_ID='$CRED_ID' bash -s" <<'EOF'
set -euo pipefail; umask 077
D="sudo -n docker"; K="sudo -n kubectl -n kiro-rs"; W=~/kiro-e2e
cleanup() { $D rm -f kiro-e2e >/dev/null 2>&1 || true; rm -rf $W; }
trap cleanup EXIT
[ -n "$TARBALL" ] && gunzip -c "$TARBALL" | $D load -q
$D image inspect "docker.io/library/kiro-rs:$TAG" >/dev/null 2>&1 || { echo "🔴 STOP: 198 docker 里没有 kiro-rs:${TAG}（给 --tarball）"; exit 1; }

P=$($K get pod -l app=kiro-rs -o name | head -1)
$K exec $P -c kiro-rs -- cat /app/config/config.json > $W/config.json
$K exec $P -c kiro-rs -- cat /app/config/credentials.json > $W/all.json
python3 - "$W" "${CRED_ID:-}" <<'PY'
import datetime as dt, json, os, sys
w, want = sys.argv[1], sys.argv[2]
cs = json.load(open(f"{w}/all.json")); os.remove(f"{w}/all.json")
if want:
    pick = [c for c in cs if str(c.get("id")) == want]
else:  # 缺省：未禁用的号里 accessToken 过期最晚的那个
    pick = sorted((c for c in cs if not c.get("disabled")), key=lambda c: c.get("expiresAt") or "", reverse=True)[:1]
if not pick:
    sys.exit(f"🔴 STOP: 找不到凭据 {want or '(未禁用)'}")
c = dict(pick[0])
exp = dt.datetime.fromisoformat(c["expiresAt"][:26].rstrip("Z") + "+00:00") if c.get("expiresAt") else None
left = (exp - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60 if exp else -1
if left < 10:
    sys.exit(f"🔴 STOP: id={c.get('id')} accessToken 只剩 {left:.0f} 分钟，等它被线上刷新后再跑（别让隔离容器去刷）")
c["refreshToken"] = "FAKE-E2E-NO-REFRESH"; c["priority"] = 0; c["disabled"] = False
json.dump([c], open(f"{w}/credentials.json", "w"))
cfg = json.load(open(f"{w}/config.json")); cfg["port"] = 18990; json.dump(cfg, open(f"{w}/config.json", "w"))
print(f"   凭据 id={c.get('id')} accessToken 剩 {left:.0f} 分钟，refreshToken 已换假值")
PY
$D rm -f kiro-e2e >/dev/null 2>&1 || true
$D run -d --name kiro-e2e --network host -v $W:/app/config "docker.io/library/kiro-rs:$TAG" >/dev/null
for _ in $(seq 1 20); do ss -ltn | grep -q ':18990 ' && break; sleep 0.5; done
ss -ltn | grep -q ':18990 ' || { $D logs kiro-e2e 2>&1 | tail -5; echo "🔴 STOP: 容器没起来"; exit 1; }
rc=0; python3 $W/cases.py http://127.0.0.1:18990 $W/config.json || rc=$?
n=$($D logs kiro-e2e 2>&1 | grep -ciE 'refresh.*(fail|error)|刷新.*失败' || true)
echo "   隔离容器刷新尝试: ${n}（应为 0）"
exit $rc
EOF
