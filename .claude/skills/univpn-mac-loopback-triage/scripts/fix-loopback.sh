#!/usr/bin/env bash
# 修复被 UniVPNCS 崩溃删掉的 macOS 回环路由 127/8 -> lo0
#
# 2026-10-10 实证：UniVPNCS 在 ROUTE_Ensure_ExitRoute 段错误时，把回环路由
# 一起删掉且没恢复 ⇒ 全机本机连接失效 ⇒ UI 连不上 29191 ⇒「接收返回码超时」。
#
# 必须 root。加回的是系统本来就该有的路由，可逆，重启也会自动重建。
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root。请跑： sudo $0" >&2
  exit 1
fi

echo "--- 修复前 ---"
netstat -rn -f inet | grep -E '^127' || echo "(无 127 路由 —— 确认损坏)"
route -n get 127.0.0.1 2>/dev/null | awk '/interface:|flags:/'

# 优先用 lo0 down/up 让系统自己重建：比手工 route add 更干净，
# 因为系统会一次性把 127/8、127.0.0.1/32、::1 全套恢复。
echo
echo "--- 重置 lo0（系统自动重建全套回环路由）---"
ifconfig lo0 down && ifconfig lo0 up
sleep 1

# 兜底：若 down/up 后仍缺，再手工补一条。
if ! netstat -rn -f inet | grep -qE '^127'; then
  echo "--- down/up 未恢复，手工补路由 ---"
  route -n add -net 127.0.0.0 -netmask 255.0.0.0 127.0.0.1 2>&1 || true
fi

echo
echo "--- 修复后 ---"
netstat -rn -f inet | grep -E '^127' || echo "✗ 仍无 127 路由"
iface="$(route -n get 127.0.0.1 2>/dev/null | awk '/interface:/{print $2}')"
echo "127.0.0.1 出口接口 = ${iface:-未知}"

if [ "$iface" = "lo0" ]; then
  echo "✓ 回环已恢复"
  echo
  echo "下一步：完全退出 UniVPN（三个进程都要死）再打开 —— 否则 UI 还连着旧的坏状态："
  echo "    pkill -f UniVPN && sleep 2 && open -a UniVPN"
  exit 0
else
  echo "✗ 回环仍未恢复，需要重启机器"
  exit 1
fi
