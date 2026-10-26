#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。run 目录由 /root/l3/.gray-run-latest 指向（或 GRAY_RUN_POINTER）。
# 用法: prod-migrate.sh <template-run-dir> <tag>
#   例: prod-migrate.sh /root/l3/up1064-20261025T164519Z 20261101
# SOP §2 生产迁移：备份（dumpprod.sh 已跑过 ⇒ $BACKUP_DIR/litellm-prod.dump）→ 查长事务 → before 快照 → migrate Job
# （新镜像、lock_timeout=5s）→ after 快照 → 判：零表重写（relfilenode）、零列/索引/表丢失、无 invalid 索引、无失败迁移行。
# 任一不过 exit 1（迁移本身全加法，旧版照常读写，见 clone 四腿）。只读之外唯一的写 = 那个 migrate Job。
#
# 10-26 v1.106.4 焊死的坑：
#   - CREATE INDEX CONCURRENTLY 会等**所有**先于它开始的事务结束（Lock/virtualxid），别人一条 13 分钟的只读 SpendLogs 大查询
#     就把它卡住 10 分钟 ⇒ 先查 >2 分钟的事务；卡住时 LONG_TXN_CANCEL=1 只 cancel「只读 + psql 发起」的阻塞者
#     （memory feedback_cic_migration_blocked_by_long_readonly_query）
#   - wait 要 > 10 分钟，否则 after 快照取在半截（201/214 中间态）
#   - before/after 比对一律 LC_ALL=C sort，否则 join/comm 报 not sorted、读出假数
#   - kubectl exec </dev/null（被 bash -s 喂进来时会吃掉脚本后半段 ⇒ 静默只跑一半）
set -uo pipefail
export LC_ALL=C
TPL=${1:?模板 run 目录}; TAG=${2:?tag}
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; NS=litellm-product
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}); . $D/target.env; umask 077
M=$D/mig-prod; mkdir -p $M
[ -s "$BACKUP_DIR/litellm-prod.dump" ] || { echo "STOP: 没有备份 $BACKUP_DIR/litellm-prod.dump（先跑 dumpprod.sh）"; exit 1; }
sha256sum $BACKUP_DIR/litellm-prod.dump > $BACKUP_DIR/litellm-prod.dump.sha256
q(){ kubectl -n $NS exec litellm-db-0 -- psql -U litellm -d litellm -At -F$'\t' -c "$1" </dev/null; }
cap(){
  q "select c.relname, c.relfilenode from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind in ('r','p')" | sort > $M/rf.$1
  q "select table_name||'.'||column_name from information_schema.columns where table_schema='public'" | sort > $M/cols.$1
  q "select indexname from pg_indexes where schemaname='public'" | sort > $M/ix.$1
  q "select count(*) from _prisma_migrations where finished_at is not null" > $M/mig.count.$1; }
LONG=$(q "select count(*) from pg_stat_activity where datname='litellm' and xact_start < now()-interval '2 minutes' and pid<>pg_backend_pid()")
echo "long transactions (>2m) before migrate: $LONG"
cap before; echo "before: tables=$(wc -l < $M/rf.before) cols=$(wc -l < $M/cols.before) ix=$(wc -l < $M/ix.before) mig=$(cat $M/mig.count.before)"
J=litellm-production-schema-migration-$TAG
python3 - "$TPL/mig-prod/migrate-PROD.yaml" "$M/migrate-PROD.yaml" "$J" "$TARGET_DIGEST" <<'PY'
import yaml,sys
src,out,name,dig=sys.argv[1:]
d=yaml.safe_load(open(src)); d["metadata"]["name"]=name
c=d["spec"]["template"]["spec"]["containers"][0]; c["image"]="127.0.0.1:5000/litellm-carher@"+dig
d["spec"]["activeDeadlineSeconds"]=1800
yaml.safe_dump(d,open(out,"w"),sort_keys=False)
PY
kubectl -n $NS delete job $J --ignore-not-found --wait=true >/dev/null
kubectl apply -f $M/migrate-PROD.yaml >/dev/null
T0=$(date +%s)
while :; do
  st=$(kubectl -n $NS get job $J -o jsonpath='{.status.succeeded}{.status.failed}')
  [ "$st" = 1 ] && break; [[ "$st" == *1* ]] && { echo "STOP: migrate job failed"; kubectl -n $NS logs job/$J --tail=20; exit 1; }
  [ $(( $(date +%s)-T0 )) -gt 1500 ] && { echo "STOP: migrate >25 分钟"; exit 1; }
  B=$(q "select a.pid||' '||(pg_blocking_pids(a.pid))[1] from pg_stat_activity a where a.query ilike '%CONCURRENTLY%' and a.pid<>pg_backend_pid() and cardinality(pg_blocking_pids(a.pid))>0 limit 1")
  if [ -n "$B" ]; then
    BP=${B#* }; RO=$(q "select (query !~* '\m(insert|update|delete|alter|create|drop|truncate|copy)\M') and application_name='psql' from pg_stat_activity where pid=$BP")
    echo "CIC blocked by pid $BP (readonly-psql=$RO)"
    [ "${LONG_TXN_CANCEL:-0}" = 1 ] && [ "$RO" = t ] && echo "cancel $BP: $(q "select pg_cancel_backend($BP)")"
  fi
  sleep 20
done
kubectl -n $NS logs job/$J 2>&1 | grep -E 'Applying|successfully|up to date' > $M/migrate.log
cap after
echo "after: tables=$(wc -l < $M/rf.after) cols=$(wc -l < $M/cols.after) ix=$(wc -l < $M/ix.after) mig=$(cat $M/mig.count.after) applied=$(grep -c Applying $M/migrate.log)"
RW=$(join -t$'\t' $M/rf.before $M/rf.after | awk -F'\t' '$2!=$3' | wc -l)
LC=$(comm -23 $M/cols.before $M/cols.after | wc -l); LI=$(comm -23 $M/ix.before $M/ix.after | wc -l)
LT=$(comm -23 <(cut -f1 $M/rf.before) <(cut -f1 $M/rf.after) | wc -l)
INV=$(q "select count(*) from pg_index where not indisvalid"); FAILED=$(q "select count(*) from _prisma_migrations where finished_at is null or rolled_back_at is not null")
echo "rewritten=$RW cols_lost=$LC idx_lost=$LI tables_lost=$LT invalid_idx=$INV failed_rows=$FAILED"
[ $RW -eq 0 ] && [ $LC -eq 0 ] && [ $LI -eq 0 ] && [ $LT -eq 0 ] && [ "$INV" = 0 ] && [ "$FAILED" = 0 ] && echo "PROD MIGRATION OK" || { echo "STOP: 迁移后对比不过"; exit 1; }
