#!/bin/bash
# persist_chatgpt_block.sh —— 把 `127.0.0.1 chatgpt.com` 持久写进 /etc/hosts，并当场验收。
#
# 为什么需要它：Codex Desktop 新窗口首条消息转圈 33s 的根因是渲染层挂载前同步等一次经
# chatgpt.com 的 statsig 拉取，而本机 chatgpt.com 是 connect 挂住（不是快速失败）。
# `--host-resolver-rules` 只对用那条命令拉起的那一个进程有效，用户正常启动就没有 ⇒ 反复复发。
# 写 hosts 才是了结。详见 SKILL.md §3。
#
# 本机已知的两个坑（都已埋在脚本里）：
#   1) /etc/hosts 最后一行可能没有结尾换行 ⇒ `tee -a` 会把新行粘到上一行尾巴上，
#      变成 `##TEC_END##127.0.0.1 chatgpt.com`，被当成注释，静默不生效。
#   2) 文件里有 ##TEC_BEGIN## / ##TEC_END## 托管块（屏蔽 Apple 更新那套），
#      块内/块后的内容疑似会被该工具定期重写。⇒ 新行一律插在托管块**之前**。
#      注：「就是它冲掉的」尚未坐实，只是形状吻合。
#
# 用法：  bash persist_chatgpt_block.sh          # 写入并验收（会提示输密码）
#         bash persist_chatgpt_block.sh --check  # 只验收，不改任何东西，不需要密码
#         bash persist_chatgpt_block.sh --revert # 撤销（把该行删掉）
#
# 退出码：0 = 已生效（hosts 有该行 + 解析到 127.0.0.1 + connect 快速失败）
#         1 = 未生效，输出里会说卡在哪一步

set -uo pipefail

HOSTS=/etc/hosts
ENTRY_IP=127.0.0.1
ENTRY_HOST=chatgpt.com
PY=/opt/homebrew/bin/python3
[ -x "$PY" ] || PY=/usr/bin/python3

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
hdr()  { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

MODE=write
case "${1:-}" in
  --check)  MODE=check ;;
  --revert) MODE=revert ;;
  "")       MODE=write ;;
  *) echo "未知参数: $1（可用: --check / --revert）"; exit 2 ;;
esac

# ---------------------------------------------------------------- 验收（三条判据）
verify() {
  local ok=0

  hdr "判据 1/3：/etc/hosts 里有独立的一行"
  # 必须是"行首就是 IP"，粘在别的行尾巴上不算
  if grep -qE "^[[:space:]]*${ENTRY_IP}[[:space:]]+${ENTRY_HOST}([[:space:]]|$)" "$HOSTS"; then
    grn "  ✅ 有"
    grep -nE "${ENTRY_HOST}" "$HOSTS" | sed 's/^/     /'
  else
    ok=1
    if grep -q "$ENTRY_HOST" "$HOSTS"; then
      red "  ❌ 出现了 ${ENTRY_HOST}，但不是独立成行（多半粘在上一行尾巴上，见脚本头部坑 1）"
      grep -n "$ENTRY_HOST" "$HOSTS" | sed 's/^/     /'
    else
      red "  ❌ 文件里根本没有 ${ENTRY_HOST}"
    fi
  fi

  hdr "判据 2/3：DNS 真的解析到 ${ENTRY_IP}"
  local resolved
  resolved=$(dscacheutil -q host -a name "$ENTRY_HOST" 2>/dev/null | awk '/^ip_address:/{print $2}' | head -3 | tr '\n' ' ')
  if [ -z "$resolved" ]; then
    ylw "  ⚠️  dscacheutil 没返回 IPv4（可能只走了 IPv6）"; ok=1
  elif echo "$resolved" | grep -q "$ENTRY_IP"; then
    grn "  ✅ ${resolved}"
  else
    red "  ❌ 解析到 ${resolved}（不是 ${ENTRY_IP}）—— hosts 没被读到，或缓存没刷"; ok=1
  fi

  hdr "判据 3/3：连接变成快速失败，而不是挂住"
  local m ct tt
  m=$(curl -s -o /dev/null -m 6 -w '%{time_connect} %{time_total} %{http_code}' "https://${ENTRY_HOST}/" 2>/dev/null)
  ct=$(echo "$m" | awk '{print $1}'); tt=$(echo "$m" | awk '{print $2}')
  printf '     connect=%ss  total=%ss  code=%s\n' $m
  # 挂住的形状：connect 永远 0.000000 且 total 顶满 6s 超时
  if [ "$(echo "$tt >= 5.5" | bc -l 2>/dev/null || echo 0)" = "1" ]; then
    red "  ❌ total 顶满超时 ⇒ 仍在 HANG，没修好"; ok=1
  else
    grn "  ✅ 立刻返回（total ${tt}s）⇒ 快速失败，正是我们要的"
  fi

  return $ok
}

# ---------------------------------------------------------------- 只验收
if [ "$MODE" = check ]; then
  if verify; then
    hdr "结论"; grn "✅ 已生效。重开一次 Codex 后，新窗口挂载应回到 3~5s。"
    exit 0
  else
    hdr "结论"; red "❌ 未生效。去掉 --check 重跑本脚本即可写入。"
    exit 1
  fi
fi

# ---------------------------------------------------------------- 需要 root
if [ "$(id -u)" != 0 ]; then
  ylw "需要管理员权限改 ${HOSTS}，用 sudo 重新执行本脚本（下面会提示输你的登录密码）…"
  exec sudo -p "请输入 %u 的密码: " /bin/bash "$0" "${1:-}"
fi

# ---------------------------------------------------------------- 备份
BAK="${HOSTS}.bak.$(date +%Y%m%d-%H%M%S)"
cp -p "$HOSTS" "$BAK" && hdr "已备份" && echo "     $BAK"

# ---------------------------------------------------------------- 改写
hdr "改写 ${HOSTS}"
ENTRY_IP="$ENTRY_IP" ENTRY_HOST="$ENTRY_HOST" HOSTS="$HOSTS" MODE="$MODE" "$PY" - <<'PYEOF'
import os, re, sys

hosts = os.environ['HOSTS']
ip    = os.environ['ENTRY_IP']
host  = os.environ['ENTRY_HOST']
mode  = os.environ['MODE']
line  = f'{ip} {host}'

with open(hosts, 'r') as f:
    text = f.read()

# 坑 1：先确保文件以换行结尾，否则任何追加都会粘到上一行尾巴上
if text and not text.endswith('\n'):
    text += '\n'
    print('     · 原文件缺结尾换行，已补（这正是上次写入静默失效的原因）')

# 清掉任何形态的旧痕迹：独立成行的、以及被粘在别人行尾的
before = text
text = re.sub(rf'^[ \t]*{re.escape(ip)}[ \t]+{re.escape(host)}[ \t]*$\n?', '', text, flags=re.M)
text = re.sub(rf'{re.escape(ip)}[ \t]+{re.escape(host)}[ \t]*(?=$)', '', text, flags=re.M)
if text != before:
    print('     · 清掉了已存在的旧条目（含粘错行尾的那种）')

if mode == 'revert':
    with open(hosts, 'w') as f:
        f.write(text)
    print('     · 已撤销：该行已删除')
    sys.exit(0)

# 坑 2：插在 ##TEC_BEGIN## 托管块之前，别落进它的地盘
marker = '##TEC_BEGIN##'
if marker in text:
    text = text.replace(marker, f'{line}\n\n{marker}', 1)
    print(f'     · 检测到 {marker} 托管块，已把新行插在它**之前**')
else:
    text = text.rstrip('\n') + f'\n{line}\n'
    print('     · 无托管块，追加到文件末尾')

with open(hosts, 'w') as f:
    f.write(text)
print('     · 写入完成')
PYEOF

if [ $? -ne 0 ]; then red "改写失败，已保留备份 $BAK"; exit 1; fi

hdr "写入后的 ${HOSTS}"
grep -n . "$HOSTS" | sed 's/^/     /'

hdr "刷 DNS 缓存"
dscacheutil -flushcache && killall -HUP mDNSResponder 2>/dev/null
echo "     done"

sleep 1

# ---------------------------------------------------------------- 验收
if verify; then
  hdr "结论"
  grn "✅ 已生效。"
  echo "   下一步：完全退出并重开一次 Codex，然后跑"
  echo "     python3 ../../codex-desktop-startup-diagnose/scripts/startup_timing.py --days 1"
  echo "   最后一行应该 < 15000ms（此前不带修复时是 33~35s）。"
  echo
  ylw "   ⚠️ 副作用：本机从此打不开 chatgpt.com 网页。日后要用代理/VPN 访问，"
  ylw "      先跑 bash $0 --revert 把这行删掉。"
  echo
  echo "   备份留在：$BAK"
  exit 0
else
  hdr "结论"
  red "❌ 写进去了但没通过验收，看上面是哪一条判据红的。"
  echo "   回滚：sudo cp $BAK $HOSTS && sudo dscacheutil -flushcache"
  exit 1
fi
