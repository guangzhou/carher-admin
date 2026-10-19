#!/usr/bin/env bash
# host-243-lxd-move-to-data.sh —— 把 243 上一个 LXD 容器从根盘的 default 池挪到数据盘的 data 池。
#
# 用法: HOST243_PW='...' scripts/host-243-lxd-move-to-data.sh <container> [--i-know-it-serves-traffic]
#
# 🔴 在服务的出口容器（test-lxd-2 / astrill-2 / astrill-3）默认拒绝：
#   挪池必须先停容器，而 Astrill GUI 重启后活不过来（见 skill astrill-243-exit-ops §8），
#   停完要靠 GUI 自动化重新连。只在有人看着的时候、并且确认 198 能容忍这条出口断几分钟，才加 --i-know-it-serves-traffic。
#   判它在不在服务：宿主上 `ss -tn state established "( sport = :<端口> )"`（8118~8122）有没有 198 的连接。
#
# 流程: 先导出到 /Data/migration/lxd-export/<c>.tar.gz → stop → lxc move --storage data → 如果原来在跑就 start → 回读池名和进程
# 足迹: 新增一个导出包（约 850M）；容器停 1~2 分钟；挪完后根盘上的那份由 lxc move 删掉。
# 回滚: lxc move <c> --storage default（或者 lxc import 那个导出包）。
#
# 前置（2026-10-18 已在 243 上做好，本脚本只校验）:
#   data 池 source=/mnt/lxd-data ← fstab bind /Data/lxd/pool；snap.lxd.daemon 带 RequiresMountsFor=/mnt/lxd-data。
#   snap 版 LXD 的挂载命名空间里看不到 /Data，池路径也不能放在 /var/snap/lxd/common 下面（LXD 会拒绝）。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
C="${1:?用法: $0 <container> [--i-know-it-serves-traffic]}"
FORCE="${2:-}"
case "$C" in
  test-lxd-2|astrill-2|astrill-3)
    [ "$FORCE" = "--i-know-it-serves-traffic" ] || { echo "$C 是在服务的出口，默认拒绝（见脚本头注释）" >&2; exit 2; } ;;
esac

"$DIR/host-243-run.sh" C="$C" - <<'EOF'
set -eu
# 前置校验
[ "$(findmnt -no SOURCE /mnt/lxd-data || true)" = "/dev/vdb1[/lxd/pool]" ] || { echo "/mnt/lxd-data 没挂在 /Data 上，停止" >&2; exit 1; }
S lxc storage show data </dev/null | grep -q "source: /mnt/lxd-data" || { echo "没有 data 池" >&2; exit 1; }
# 🔴 状态一律用 lxc query 精确查。`lxc list <name>` 是前缀过滤：test-lxd 会同时命中 test-lxd-2，
#    得到两行 ⇒ 不等于 FROZEN ⇒ 守卫被跳过（2026-10-18 就这样把冻结的 test-lxd 停掉并挪走了）
inst(){ S lxc query "/1.0/instances/$C" </dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }
pool=$(inst 'd["expanded_devices"]["root"]["pool"]')
[ "$pool" = data ] && { echo "$C 已经在 data 池"; exit 0; }
WAS=$(inst 'd["status"]')
case "$WAS" in Running|Stopped) ;; *) echo "$C 状态=${WAS}（只处理 Running/Stopped；Frozen 不确定是谁冻的、为什么冻 ⇒ 不动，先问清楚）" >&2; exit 2;; esac

S mkdir -p /Data/migration/lxd-export
# 带时间戳，不覆盖以前的备份
OUT=/Data/migration/lxd-export/$C-$(date -u +%Y%m%dT%H%M).tar.gz
# snap 里看 /Data 要走 hostfs
S lxc export "$C" "/var/lib/snapd/hostfs$OUT" --instance-only --compression gzip </dev/null | tail -1
S test -s "$OUT" || { echo "导出包为空，停止" >&2; exit 1; }
echo "export: $(S ls -lh "$OUT" | awk '{print $5}')  state=$WAS"

S lxc stop "$C" --timeout 30 </dev/null || S lxc stop "$C" --force </dev/null
# dir 驱动是先拷后删：move 失败时原容器还在 default 池，把它按原状态拉起来再退出
if ! S lxc move "$C" --storage data </dev/null; then
  echo "lxc move 失败，容器仍在 $(inst 'd["expanded_devices"]["root"]["pool"]') 池" >&2
  [ "$WAS" = Running ] && S lxc start "$C" </dev/null
  exit 1
fi
[ "$WAS" = Running ] && S lxc start "$C" </dev/null
sleep 8
pool=$(inst 'd["expanded_devices"]["root"]["pool"]')
echo "pool=$pool state=$(inst 'd["status"]')（原来是 ${WAS}）"
S lxc exec "$C" -- sh -c 'ps -eo comm= | grep -E "^(astrill|tinyproxy|Xvfb)" | sort | uniq -c | tr "\n" " "' </dev/null; echo
df -h / | tail -1
[ "$pool" = data ]
EOF
