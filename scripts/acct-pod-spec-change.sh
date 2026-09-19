#!/usr/bin/env bash
# 批量改 chatgpt-acct-* 的 pod-spec 字段（limits / requests / image / env …）——
# 串行、每个之间核对 ready、掉一个就停。
#
# 为什么需要这个脚本，而不是现场拼一行 kubectl
# ------------------------------------------------
# 1) `replicas>0` ≠ 在服务。2026-09-19 实测 198 `litellm-product`：165 个
#    `chatgpt-acct-*` 部署里只有 54 个真在服务，其余 111 个都是 replicas=1 但
#    一个 Ready 的 pod 都没有（refresh token 死了）。拿 replicas 建目标列表，
#    每个死号白等一个 rollout timeout，而且**让「我搞坏了」和「它本来就坏」
#    长得一模一样** —— 新 pod 是你的 patch 建的，时间戳紧贴你的操作窗口。
#    ⇒ 目标列表只认：存在 Running pod（无 deletionTimestamp）且其 `litellm`
#      容器 `ready: true`。
#
# 2) 一个正在服务的号，可能只靠**内存里那张 access token** 活着，盘上的
#    refresh token 已经死了。任何需要重启 pod 的变更都会**永久打死**它，
#    而且**回退配置救不回来**（损坏来自重启本身，不是那个配置值）。
#    2026-09-19 实测：批量 2Gi→3Gi，55 个里 acct-237 就是这样死的，
#    脚本按 FAIL 分支回退到 2Gi 完全没用。
#    ⇒ `ready:true` 只证明「此刻能服务」，不证明「重启后还能服务」，
#      而且**事前没有非侵入式探针能测出是哪几个**（盘上 auth.json 是陈旧快照）。
#    ⇒ 所以：默认 --dry-run 只报影响面；真跑时串行，**掉第一个就停**，
#      让人来决定要不要为剩下的继续付这个代价。
#
# 动了什么 / 备份在哪 / 怎么回滚
# ------------------------------
# 动：逐个 `kubectl patch` 指定字段，触发滚动重启。
# 备份：每个号 patch 前先 `get deploy -o yaml` 落到 $BACKUP_DIR/<name>.before.yaml（0600）。
# 回滚：`kubectl -n <ns> apply -f $BACKUP_DIR/<name>.before.yaml`
#       ⚠️ 回滚只恢复配置，**不能复活被重启打死的号** —— 那个只能重新登录。
#
# 用法：
#   ./acct-pod-spec-change.sh --jsonpath /resources/limits/memory --value 3Gi --dry-run
#   ./acct-pod-spec-change.sh --jsonpath /resources/limits/memory --value 3Gi --go
set -euo pipefail

NS="${NS:-litellm-product}"
CONTAINER="${CONTAINER:-litellm}"
JSONPATH=""
VALUE=""
MODE="dry-run"
ONLY_IF=""        # 可选：只改当前值等于这个的号（例如 --only-if 2Gi）
TIMEOUT="${TIMEOUT:-180s}"

while [ $# -gt 0 ]; do
  case "$1" in
    --jsonpath) JSONPATH="$2"; shift 2 ;;
    --value)    VALUE="$2"; shift 2 ;;
    --only-if)  ONLY_IF="$2"; shift 2 ;;
    --dry-run)  MODE="dry-run"; shift ;;
    --go)       MODE="go"; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[ -n "$JSONPATH" ] || { echo "缺 --jsonpath，例如 /resources/limits/memory" >&2; exit 2; }
[ -n "$VALUE" ]    || { echo "缺 --value，例如 3Gi" >&2; exit 2; }

BACKUP_DIR="${BACKUP_DIR:-$HOME/acct-pod-spec-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
LOG="$BACKUP_DIR/patch.log"; : > "$LOG"; chmod 600 "$LOG"

echo "ns=$NS container=$CONTAINER path=$JSONPATH value=$VALUE mode=$MODE"
echo "备份目录：$BACKUP_DIR"

# ---- 建目标列表：只认真在服务的号 -------------------------------------------
kubectl -n "$NS" get deploy -o json > "$BACKUP_DIR/deploy.json"
kubectl -n "$NS" get pods  -o json > "$BACKUP_DIR/pods.json"
chmod 600 "$BACKUP_DIR"/*.json

ONLY_IF="$ONLY_IF" CONTAINER="$CONTAINER" JSONPATH="$JSONPATH" \
BACKUP_DIR="$BACKUP_DIR" python3 - <<'PY'
import json, os
bd=os.environ['BACKUP_DIR']; cname=os.environ['CONTAINER']
only=os.environ.get('ONLY_IF',''); path=os.environ['JSONPATH']
D=json.load(open(bd+'/deploy.json')); P=json.load(open(bd+'/pods.json'))

serving=set()
for p in P['items']:
    m=p['metadata']; s=p.get('status',{})
    app=(m.get('labels') or {}).get('app','')
    if not app.startswith('chatgpt-acct-'): continue
    if m.get('deletionTimestamp') or s.get('phase')!='Running': continue
    for c in s.get('containerStatuses',[]) or []:
        if c['name']==cname and c.get('ready'): serving.add(app)

def dig(container, path):
    cur=container
    for seg in path.strip('/').split('/'):
        if not isinstance(cur,dict): return None
        cur=cur.get(seg)
    return cur

tgt=[]; skipped=0
for d in D['items']:
    n=d['metadata']['name']
    if not n.startswith('chatgpt-acct-') or n not in serving: continue
    cs=d['spec']['template']['spec']['containers']
    i=[k for k,c in enumerate(cs) if c['name']==cname]   # ⛔ 下标别假设 0
    if not i: continue
    if only:
        if str(dig(cs[i[0]], path))!=only:
            skipped+=1; continue
    tgt.append("%s\t%d" % (n, i[0]))

open(bd+'/targets.tsv','w').write("\n".join(sorted(tgt))+"\n")
print("在服务的号: %d" % len(serving))
if only: print("当前值 != %s 而跳过: %d" % (only, skipped))
print("本轮目标: %d" % len(tgt))
PY
chmod 600 "$BACKUP_DIR/targets.tsv"

N=$(grep -c . "$BACKUP_DIR/targets.tsv" || true)
if [ "$MODE" = "dry-run" ]; then
  echo
  echo "== dry-run，什么都没改 =="
  echo "会重启 $N 个正在服务的号。其中可能有若干个只靠内存里的 access token 活着，"
  echo "重启后永久掉线且回退配置救不回来 —— 事前测不出是哪几个。"
  echo "确认要付这个代价再加 --go 重跑。目标清单：$BACKUP_DIR/targets.tsv"
  exit 0
fi

# ---- 串行改，掉第一个就停 ----------------------------------------------------
OK=0
while IFS=$'\t' read -r n idx; do
  [ -n "$n" ] || continue
  kubectl -n "$NS" get deploy "$n" -o yaml > "$BACKUP_DIR/$n.before.yaml"
  chmod 600 "$BACKUP_DIR/$n.before.yaml"

  kubectl -n "$NS" patch deploy "$n" --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/$idx$JSONPATH\",\"value\":\"$VALUE\"}]" >/dev/null

  if kubectl -n "$NS" rollout status deploy/"$n" --timeout="$TIMEOUT" >/dev/null 2>&1; then
    echo "OK   $n $VALUE" | tee -a "$LOG"
    OK=$((OK+1))
    continue
  fi

  # 没起来：先回退配置（免得配置漂移），但别把它当成修复
  kubectl -n "$NS" apply -f "$BACKUP_DIR/$n.before.yaml" >/dev/null 2>&1 || true
  echo "FAIL $n —— 已回退配置，但号可能已永久掉线（重启丢了内存里的 access token）" | tee -a "$LOG"
  echo "     死因看这里：kubectl -n $NS logs -l app=$n -c $CONTAINER --tail=40" | tee -a "$LOG"
  echo "     若尾部是 'refresh token failed, re-login required' ⇒ 只能重新登录，回退救不回来" | tee -a "$LOG"
  echo
  echo "== 停在第 $((OK+1)) 个（前 $OK 个已成功）=="
  echo "按设计停下来：不要跑完再数尸体。要继续就把已处理的从 targets.tsv 摘掉后重跑。"
  exit 1
done < "$BACKUP_DIR/targets.tsv"

echo
echo "== 全部完成：$OK/$N =="
echo "日志 $LOG ；回滚料 $BACKUP_DIR/*.before.yaml（只恢复配置，不复活死号）"
