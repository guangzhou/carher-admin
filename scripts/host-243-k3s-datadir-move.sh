#!/bin/bash
# host-243-k3s-datadir-move.sh —— 单节点 k3s 的 /var/lib/rancher 整体搬到数据盘，再用 fstab bind 挂回原路径。
# 2026-10-18 在 243 上跑过一次（根盘 38G → 腾出 2.7G；隧道断约 1.5 分钟，dashboard 停约 2 分钟）。现在 243 已经是挂载点，再跑会直接退出。
# 失败回滚后 $DST 会留着（不自动删，里面可能是唯一一份新拷贝）；确认没用后手动挪走，才能重跑。
#
# ⚠️ 以 root 在目标机上跑，并且必须脱离 ssh 会话（ssh 断开不能把它杀在半路）:
#   scp 本文件 cltx@<host>:/Data/migration/  然后
#   sudo systemd-run --unit=k3s-move-$(date +%Y%m%d) --collect /bin/bash /Data/migration/host-243-k3s-datadir-move.sh
#   进度: /Data/migration/k3s-move.log；结果: /Data/migration/k3s-move.status = RUNNING|OK|FAILED|WARN
# 可调: NS=<停机期间要缩到 0 的业务 ns>  DATA=/Data
#
# 为什么不改 --data-dir：bind mount 对 k3s 透明，回滚只是 umount + 把 .old 挪回去。
# 保护（每条都对应一个会丢数据或"看起来丢了"的失败形状）:
#   - 先把业务 deploy 缩到 0，再缩 sts ⇒ pg 收到 SIGTERM 时已经没有客户端，正常关库。
#     k3s-killall.sh 对 shim 用的是 kill -9，不能让它碰到数据库
#   - rsync -aHAX --numeric-ids 拷完后再跑一遍 -c 校验，必须零差异，条目数和 state.db sha 也要对上
#   - k3s 加 RequiresMountsFor=/var/lib/rancher：数据盘没挂上时 k3s 不启动。
#     否则它会在空目录上初始化一个全新的空集群，所有资源"消失"
#   - 原目录改名为 .old 而不是删掉；任何一步失败都自动回滚到原目录，并把业务按原副本数拉起
# 足迹: 写 /etc/fstab（追加一行 bind）、新增 /etc/systemd/system/k3s.service.d/10-rancher-on-data.conf、
#       创建 $DATA/rancher、把 /var/lib/rancher 改名为 /var/lib/rancher.old（确认无误后由人删除或挪走）。
#       备份: $DATA/migration/{replicas,k8s-all}-pre-k3s-move.*
set -u -o pipefail
NS=${NS:-ai-token-dashboard}
DATA=${DATA:-/Data}
SRC=/var/lib/rancher
DST=$DATA/rancher
M=$DATA/migration
mkdir -p "$M"
LOG=$M/k3s-move.log
exec >>"$LOG" 2>&1
K="k3s kubectl"
say(){ echo "[$(date -u +%T)] $*"; }

wait_node_ready(){
  for _ in $(seq 1 60); do
    $K get node 2>/dev/null | grep -q " Ready" && return 0; sleep 5
  done; return 1
}
scale_up(){
  while read -r kind name rep; do
    [ -n "$name" ] && $K -n "$NS" scale "$kind/$name" --replicas="$rep"
  done < "$M/replicas-pre-k3s-move.txt"
}
# 等到"实际 Ready 的 pod 数 == 原副本数之和"。只数"没 Ready 的"会在 pod 还没被创建时得 0 ⇒ 1 秒就假绿（10-18 实际发生过）
wait_workload_ready(){
  want=$(awk '{s+=$3} END{print s+0}' "$M/replicas-pre-k3s-move.txt")
  for _ in $(seq 1 60); do
    got=$($K -n "$NS" get pods --no-headers 2>/dev/null | awk '$3=="Running"{split($2,a,"/"); if (a[1]==a[2]) n++} END{print n+0}')
    [ "$got" -ge "$want" ] && { say "ready $got/$want"; return 0; }
    sleep 10
  done
  say "only $got/$want ready after 10min"; return 1
}
# 属于 Deployment（owner=ReplicaSet）且还没退出的 pod 数
deploy_pods(){
  $K -n "$NS" get pods -o jsonpath='{range .items[*]}{.metadata.ownerReferences[0].kind}{" "}{.status.phase}{"\n"}{end}' 2>/dev/null \
    | awk '$1=="ReplicaSet" && $2!="Succeeded" && $2!="Failed"' | wc -l | tr -d ' '
}
rollback(){
  say "!!! ROLLBACK: $1"
  systemctl stop k3s 2>/dev/null; /usr/local/bin/k3s-killall.sh >/dev/null 2>&1
  if mountpoint -q $SRC; then umount $SRC; fi
  if [ -d ${SRC}.old ]; then rmdir $SRC 2>/dev/null; mv ${SRC}.old $SRC; fi
  sed -i "\#^$DST\s#d" /etc/fstab
  rm -f /etc/systemd/system/k3s.service.d/10-rancher-on-data.conf; systemctl daemon-reload
  systemctl start k3s; wait_node_ready && scale_up
  say "rollback done"; echo FAILED > "$M/k3s-move.status"; exit 1
}

say "=== start NS=$NS DST=$DST"
mountpoint -q $SRC && { say "$SRC already a mountpoint, nothing to do"; exit 1; }
[ -e "$DST" ] && { say "$DST exists, abort"; exit 1; }
mountpoint -q "$DATA" || { say "$DATA not mounted, abort"; exit 1; }
echo RUNNING > "$M/k3s-move.status"

# 1. 记录现状 + 资源清单备份
$K -n "$NS" get deploy,sts -o jsonpath='{range .items[*]}{.kind}{" "}{.metadata.name}{" "}{.spec.replicas}{"\n"}{end}' \
  | sed 's/^Deployment/deployment/; s/^StatefulSet/statefulset/' > "$M/replicas-pre-k3s-move.txt"
cat "$M/replicas-pre-k3s-move.txt"
$K get all,cm,secret,pvc,pv,ingress -A -o yaml > "$M/k8s-all-pre-k3s-move.yaml"
chmod 600 "$M/k8s-all-pre-k3s-move.yaml"

# 2. 先停 pg 的客户端，再停有状态服务
say "scale down"
$K -n "$NS" scale deploy --all --replicas=0
for _ in $(seq 1 24); do
  [ "$(deploy_pods)" = 0 ] && break; sleep 5
done
$K -n "$NS" scale sts --all --replicas=0
for _ in $(seq 1 36); do
  n=$($K -n "$NS" get pods --no-headers 2>/dev/null | grep -vcE "Completed|Evicted|Error")
  [ "$n" = 0 ] && break; sleep 5
done
# 🔴 这里不能走 rollback：rollback 会跑 k3s-killall（kill -9），会把还没停下的 pg 硬杀。k3s 还没动，按原副本数拉回去即可
if $K -n "$NS" get pods --no-headers | grep -vE "Completed|Evicted|Error"; then
  say "!!! pods did not stop, k3s untouched, scaling back up"
  scale_up; echo FAILED > "$M/k3s-move.status"; exit 1
fi
say "all $NS pods gone"

# 3. 停 k3s
systemctl stop k3s
/usr/local/bin/k3s-killall.sh >/dev/null 2>&1
sleep 3
pgrep -f "containerd-shim|/k3s " && rollback "k3s processes still alive"
grep -q " $SRC/" /proc/self/mounts && { grep " $SRC/" /proc/self/mounts; rollback "mounts left under $SRC"; }
say "k3s stopped"

# 4. 拷贝 + 逐字节校验
mkdir -p "$DST"
rsync -aHAX --numeric-ids $SRC/ "$DST"/ || rollback "rsync failed"
DIFF=$(rsync -naHAXc --numeric-ids --delete --out-format='%n' $SRC/ "$DST"/)
[ -n "$DIFF" ] && { echo "$DIFF" | head; rollback "rsync verify not empty"; }
a=$(find $SRC | wc -l); b=$(find "$DST" | wc -l); say "entries src=$a dst=$b"
[ "$a" = "$b" ] || rollback "entry count mismatch"
s1=$(sha256sum $SRC/k3s/server/db/state.db | cut -d' ' -f1); s2=$(sha256sum "$DST"/k3s/server/db/state.db | cut -d' ' -f1)
[ "$s1" = "$s2" ] || rollback "state.db sha mismatch"
say "verified, state.db=$s1"

# 5. 切换：原目录改名留存，bind 挂回
mv $SRC ${SRC}.old && mkdir $SRC
printf '%s\t%s\tnone\tbind,nofail,x-systemd.requires-mounts-for=%s\t0\t0\n' "$DST" $SRC "$DATA" >> /etc/fstab
systemctl daemon-reload
mount $SRC || rollback "bind mount failed"
mkdir -p /etc/systemd/system/k3s.service.d
printf '[Unit]\nRequiresMountsFor=%s\n' $SRC > /etc/systemd/system/k3s.service.d/10-rancher-on-data.conf
systemctl daemon-reload
dev=$(findmnt -no SOURCE "$DATA")
[ "$(findmnt -no SOURCE $SRC)" = "${dev}[/rancher]" ] || rollback "mount source wrong: $(findmnt -no SOURCE $SRC)"
[ "$(sha256sum $SRC/k3s/server/db/state.db | cut -d' ' -f1)" = "$s1" ] || rollback "state.db via mount mismatch"
say "mounted"

# 6. 起 k3s + 业务
systemctl start k3s
wait_node_ready || rollback "node not ready"
say "node ready"
scale_up
if wait_workload_ready; then
  $K -n "$NS" get pods
  say "=== done (原目录留在 ${SRC}.old，确认无误后再挪走/删除)"; echo OK > "$M/k3s-move.status"
else
  # 数据已经在新位置并且校验过；pod 起不来通常是应用自己的问题（10-18 是启动时等锁），不自动回滚
  $K -n "$NS" get pods
  say "=== moved but workload not fully ready, check manually"; echo WARN > "$M/k3s-move.status"
fi
