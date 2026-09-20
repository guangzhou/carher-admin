#!/bin/sh
# astrill-243-survey.sh —— 巡检 243 上五个 Astrill 容器当前各持有什么出口。
#
# 用法: ASTRILL243_PW=xxx astrill-243-survey.sh
#
# 输出每个容器的 tun0 + --remote/--port。这是判断"还剩几个并发坑位"的唯一可靠方式
# （账号上限 5 条，官方 FAQ）。
#
# ⚠️ 纯只读：不改配置、不删文件、不杀进程。可以在生产出口在线时安全运行。
#
# 注意：这里**故意不用 `set -e`** —— 闲置容器的 grep 无匹配会返回非 0，
# 而"某个容器没有连接"正是这个巡检要报告的正常状态，不是错误。
LX="$(dirname "$0")/astrill-243-lx.sh"

N=0
for C in test-lxd-2 astrill-2 astrill-3 astrill-4 astrill-5; do
  T=$("$LX" "$C" 'ip -4 a show tun0 2>/dev/null | grep -o "inet [0-9.]*"' || true)
  R=$("$LX" "$C" 'ps -eo args | grep [a]sovpnc | grep -oE -- "--remote [0-9.]+ --port [0-9-]+" | head -1' || true)
  T=$(echo "$T" | tr -d '\n' | sed 's/ *$//')
  [ -n "$T" ] && N=$((N + 1))
  printf '%-12s tun=[%-18s]  %s\n' "$C" "${T:-无}" "${R:-无连接}"
done
echo "---"
echo "在线 $N 条 / 账号上限 5 条，空闲坑位 $((5 - N)) 个"
