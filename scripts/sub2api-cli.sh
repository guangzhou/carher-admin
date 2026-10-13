#!/usr/bin/env bash
# 198 sub2api 管理 CLI 包装:自动取 admin 密码 → 登录换 JWT → 跑 cli-anything-sub2api。
#
# 用法(本地 Mac,需能 ssh cltx@10.68.13.198):
#   scripts/sub2api-cli.sh status
#   scripts/sub2api-cli.sh accounts list --platform grok
#   scripts/sub2api-cli.sh --dry-run accounts action 42 clear-error
#   scripts/sub2api-cli.sh install        # 首次:从仓内 tools/ 装 CLI(uv tool, editable)
#   scripts/sub2api-cli.sh versions       # 现量:198 镜像历史 + 上游最新 tag(不需要 JWT)
#
# 默认加 --target 198 --json;要人读格式就设 S2A_HUMAN=1。
# 凭据足迹:密码只在本进程管道里过一次(不落盘、不进 argv);JWT 只进子进程 env,
# 24h 过期,本脚本不缓存。服务端 audit_logs 会多一行 auth.login。
#
# 可改:S2A_HOST(默认 cltx@10.68.13.198)、S2A_URL(默认 http://10.68.13.198:31880)、
#       S2A_EMAIL(默认 admin@sub2api.local)。已有 SUB2API_JWT/SUB2API_ADMIN_API_KEY 时跳过登录。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS="$REPO/tools/cli-anything-sub2api"
S2A_HOST="${S2A_HOST:-cltx@10.68.13.198}"
S2A_URL="${S2A_URL:-http://10.68.13.198:31880}"
S2A_EMAIL="${S2A_EMAIL:-admin@sub2api.local}"

if [[ "${1:-}" == "install" ]]; then
  command -v uv >/dev/null || { echo "需要 uv:brew install uv" >&2; exit 2; }
  uv tool install --force --editable "$HARNESS"
  echo "installed: $(command -v cli-anything-sub2api)"
  exit 0
fi

if [[ "${1:-}" == "versions" ]]; then
  # 版本一律现量:记忆/skill 里写的「当前线上」会过时(10-13 实测停了 12 天、漏两次升级)
  echo "== 198 sub2api 镜像历史(按创建时间;READY=1 的那行才是线上)"
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$S2A_HOST" \
    "sudo -n kubectl -n litellm-dev get rs --no-headers -o custom-columns=CREATED:.metadata.creationTimestamp,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image 2>/dev/null" |
    awk '$3 ~ /\/sub2api:/ {if ($2=="<none>") $2=0; print $1, $2, $3}' | sort | tail -6
  echo "== 198 health"
  curl -s -m 8 "$S2A_URL/health" >/dev/null && echo "ok" || echo "FAIL"
  if [[ -d "$HOME/codes/sub2api/.git" ]]; then
    echo "== 上游(~/codes/sub2api,先 fetch tags,限时 20s)"
    # github 时通时不通:不通时 git fetch 会挂几分钟不报错,用后台+计时器兜住(macOS 无 timeout)
    git -C "$HOME/codes/sub2api" fetch -q origin --tags 2>/dev/null & FP=$!
    ( sleep 20; kill "$FP" 2>/dev/null ) & KP=$!
    if wait "$FP" 2>/dev/null; then kill "$KP" 2>/dev/null; else echo "(fetch 失败/超时,下面是本地缓存;可走 188)"; fi
    git -C "$HOME/codes/sub2api" tag --sort=-v:refname | head -3
    echo "读 198 行为的源码:git -C ~/codes/sub2api show <线上tag>:<path>"
  fi
  exit 0
fi

CLI="$(command -v cli-anything-sub2api || true)"
if [[ -z "$CLI" ]]; then
  echo "cli-anything-sub2api 未安装,先跑: $0 install" >&2
  exit 2
fi

if [[ -z "${SUB2API_JWT:-}" && -z "${SUB2API_ADMIN_API_KEY:-}" ]]; then
  # sudo -n:永不弹密码提示(ssh 非 tty 下提示会挂死,形状像卡住)
  SUB2API_JWT="$(
    ssh -o BatchMode=yes -o ConnectTimeout=8 "$S2A_HOST" \
      "sudo -n kubectl -n litellm-dev get secret sub2api-secrets -o jsonpath='{.data.ADMIN_PASSWORD}' 2>/dev/null | base64 -d" |
    S2A_URL="$S2A_URL" S2A_EMAIL="$S2A_EMAIL" python3 -c '
import json, os, sys, urllib.error, urllib.request
pw = sys.stdin.read().strip()
if not pw:
    sys.exit("取不到 ADMIN_PASSWORD(ssh 或 sudo -n 失败)")
req = urllib.request.Request(
    os.environ["S2A_URL"] + "/api/v1/auth/login",
    data=json.dumps({"email": os.environ["S2A_EMAIL"], "password": pw}).encode(),
    headers={"Content-Type": "application/json"})
try:
    body = json.loads(urllib.request.urlopen(req, timeout=20).read())
except urllib.error.HTTPError as e:
    sys.exit("登录失败 HTTP %d: %s" % (e.code, e.read()[:200].decode(errors="replace")))
tok = (body.get("data") or {}).get("access_token")
if not tok:
    sys.exit("登录返回无 access_token: code=%s message=%s" % (body.get("code"), body.get("message")))
print(tok)
'
  )"
  export SUB2API_JWT
fi

export SUB2API_198_BASE_URL="$S2A_URL"
if [[ -n "${S2A_HUMAN:-}" ]]; then
  exec "$CLI" --target 198 "$@"
fi
exec "$CLI" --target 198 --json "$@"
