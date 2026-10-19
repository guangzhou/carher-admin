#!/usr/bin/env bash
# host-243-disk-check.sh —— 243 磁盘与"会不会再被 image GC 打挂"的只读巡检。
#
# 用法: HOST243_PW='...' scripts/host-243-disk-check.sh
# 退出码: 0=全绿  1=有 WARN/FAIL（逐行打出原因）
#
# 查什么（每一项都对应 2026-10-17 停机 19h 的一环）:
#   根盘/数据盘用量 · 节点 DiskPressure · /var/lib/rancher 是否真挂在 /Data（不是空目录上起了新集群）
#   k3s / snap.lxd 是否带 RequiresMountsFor · swap 在哪 · journald 上限
#   imagePullPolicy=Never 引用的镜像：在不在、有没有 pinned（没 pin 的会被 image GC 删，删了拉不回）
#   LXD 容器各在哪个池 · /Data/migration 里的恢复 tar
# 足迹: 只读。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

"$DIR/host-243-run.sh" - <<'EOF'
FAIL=0
ok(){ echo "OK    $*"; }
warn(){ echo "WARN  $*"; FAIL=1; }

pct(){ df --output=pcent "$1" | tail -1 | tr -dc 0-9; }
r=$(pct /); d=$(pct /Data)
[ "$r" -lt 75 ] && ok "根盘 ${r}%" || warn "根盘 ${r}%（kubelet image GC 高水位 85%、驱逐线 95%）"
[ "$d" -lt 80 ] && ok "/Data ${d}%" || warn "/Data ${d}%"

dp=$(kubectl get node -o jsonpath='{.items[0].status.conditions[?(@.type=="DiskPressure")].status}')
[ "$dp" = False ] && ok "DiskPressure=False" || warn "DiskPressure=${dp}（节点被打了 NoSchedule 污点，pod 全部 Pending）"

src=$(findmnt -no SOURCE /var/lib/rancher || true)
[ "$src" = "/dev/vdb1[/rancher]" ] && ok "/var/lib/rancher ← $src" || warn "/var/lib/rancher 挂载源='$src'（应为 /dev/vdb1[/rancher]）"
systemctl show k3s -p RequiresMountsFor | grep -q /var/lib/rancher && ok "k3s RequiresMountsFor=/var/lib/rancher" || warn "k3s 缺 RequiresMountsFor：/Data 挂不上时会在空目录上起一个新空集群"
systemctl show snap.lxd.daemon -p RequiresMountsFor | grep -q /mnt/lxd-data && ok "snap.lxd.daemon RequiresMountsFor=/mnt/lxd-data" || warn "snap.lxd.daemon 缺 RequiresMountsFor"
[ "$(findmnt -no SOURCE /mnt/lxd-data || true)" = "/dev/vdb1[/lxd/pool]" ] && ok "/mnt/lxd-data ← /Data/lxd/pool" || warn "/mnt/lxd-data 没挂上"

sw=$(swapon --noheadings --show=NAME | tr '\n' ' ')
[[ "$sw" == *"/Data/"* ]] && ok "swap: $sw" || warn "swap 不在 /Data: '$sw'"
[ -f /etc/systemd/journald.conf.d/99-size-cap.conf ] && ok "宿主 journald 上限 $(grep -h SystemMaxUse /etc/systemd/journald.conf.d/99-size-cap.conf)" || warn "宿主 journald 没设上限"
ok "宿主 journal 实占 $(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMG]')"

# imagePullPolicy=Never 的镜像必须在且 pinned
NEVER=$(kubectl get deploy,sts,ds -A -o jsonpath='{range .items[*].spec.template.spec.containers[?(@.imagePullPolicy=="Never")]}{.image}{"\n"}{end}' | sort -u)
IMGS=$(S k3s crictl images -o json)
for i in $NEVER; do
  case "$i" in */*) full="docker.io/$i";; *) full="docker.io/library/$i";; esac
  st=$(printf '%s' "$IMGS" | python3 -c "
import sys,json
t=sys.argv[1]
m=[x for x in json.load(sys.stdin)['images'] if t in x['repoTags']]
print('missing' if not m else ('pinned' if m[0].get('pinned') else 'unpinned'))" "$full")
  case $st in
    pinned) ok "镜像 $i pinned";;
    unpinned) warn "镜像 $i 没 pin ⇒ image GC 会删，删了拉不回（修: S k3s ctr -n k8s.io images label $full io.cri-containerd.pinned=pinned）";;
    *) warn "镜像 $i 不在节点上 ⇒ 用到它的 pod 会 ErrImageNeverPull（恢复 tar 见 /Data/migration/image-*.tar）";;
  esac
done

echo "--- LXD 容器所在池"
for c in $(S lxc list -c n -f csv </dev/null); do
  p=$(S lxc query "/1.0/instances/$c" </dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["expanded_devices"]["root"]["pool"])')
  echo "      $c=$p"
done
echo "--- /Data/migration 恢复材料"
ls -1sh /Data/migration/image-*.tar 2>/dev/null | sed 's/^/      /'
[ $FAIL = 0 ] && echo "=== ALL OK" || echo "=== 有 WARN"
exit $FAIL
EOF
