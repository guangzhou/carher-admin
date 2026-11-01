#!/usr/bin/env bash
# 198 上 mihomo 代理出口的一键入口（包装 cli-anything-mihomo，skill `cli-anything-mihomo`）。
#
#   scripts/mihomo-198.sh install          # 安装/更新 CLI（uv tool editable，指向仓内 tools/cli-anything-mihomo）
#   scripts/mihomo-198.sh quota            # 所有 iGuge 账户：已用/总量/到期（会重新拉订阅；>=80% 或 <30 天 ⇒ exit 1）
#   scripts/mihomo-198.sh status           # 198 上所有实例 + 每个端口的出口 IP/国家/连 Kiro 耗时
#   scripts/mihomo-198.sh verify           # 每个端口真搜一次谷歌学术（出口能不能用的最终判据）
#   scripts/mihomo-198.sh countries [账户] # 订阅里真实有哪些国家（按出口 IP，临时实例，几分钟）
#   scripts/mihomo-198.sh handoff <账户> <端口段> [--no-country] [--out 文件]   # 生成 kiro 30891 加槽 handoff
#   scripts/mihomo-198.sh <任意 cli-anything-mihomo 参数>                       # 透传，如 -a acct2 exit ports
#
# 每一步打印 OK / STOP + 原因；任何一步失败整体非 0。
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
M=cli-anything-mihomo

need_cli() {
  command -v "$M" >/dev/null 2>&1 || { echo "STOP 没装 ${M}：先跑 scripts/mihomo-198.sh install" >&2; exit 2; }
}

# 198 上实例名 = 账户名（default / acct2 …）；从本机已存订阅的账户里取
accounts() {
  "$M" --json instance list 2>/dev/null | python3 -c 'import json,sys; [print(r["account"] or r["instance"]) for r in json.load(sys.stdin) if r["running"]]'
}

rc=0
step() {  # step <标签> <命令…>
  local label="$1"; shift
  if "$@"; then echo "OK   ${label}"; else local r=$?; echo "STOP ${label} (rc=${r})"; rc=1; fi
}

cmd="${1:-help}"
case "$cmd" in
  install)
    uv tool install --editable "${REPO}/tools/cli-anything-mihomo" --reinstall -q && "$M" --version
    ;;
  quota)
    need_cli
    step "隧道 188" "$M" tunnel
    step "额度（全部账户）" "$M" sub quota --all-accounts
    exit $rc
    ;;
  status)
    need_cli
    step "198 实例" "$M" instance list
    for a in $(accounts); do
      echo "== ${a}"
      step "${a} 出口" "$M" -a "$a" exit table --samples 3
    done
    exit $rc
    ;;
  verify)
    need_cli
    for a in $(accounts); do
      echo "== ${a}"
      step "${a} 学术实搜" "$M" -a "$a" exit verify
    done
    exit $rc
    ;;
  countries)
    need_cli
    a="${2:-default}"
    step "隧道 188" "$M" tunnel
    step "${a} 真实出口国家" "$M" -a "$a" exit geo --all
    exit $rc
    ;;
  handoff)
    need_cli
    [ $# -ge 3 ] || { echo "用法：$0 handoff <账户> <端口段，如 18891-18895> [--no-country] [--out 文件]" >&2; exit 2; }
    a="$2"; ports="$3"; shift 3
    python3 "${REPO}/scripts/kiro-slots-egress-handoff.py" --account "$a" --ports "$ports" "$@"
    ;;
  help|-h|--help)
    sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
  *)
    need_cli
    exec "$M" "$@"
    ;;
esac
