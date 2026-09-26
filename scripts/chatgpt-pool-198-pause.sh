#!/bin/bash
# 批量暂停 198 litellm-product 池里的 chatgpt-acct(停 pod + 摘 arms + 标 state)。
#
#   P198='..' P188='..' ./chatgpt-pool-198-pause.sh            115 116 ...   # dry-run 勘察
#   P198='..' P188='..' ./chatgpt-pool-198-pause.sh --apply    115 116 ...   # 真干
#   P188='..'           ./chatgpt-pool-198-pause.sh --verify   115 116 ...   # 隔一轮 cron 复查
#
#   ... --apply --keep-pods 175     # 只摘轮转(①②③⑤),**跳过 ④ scale=0**,pod 留着跑
#
# 只动「副本数 + router 成员关系 + governor 状态」,**不碰** PVC / auth.json / deployment 本体,
# 所以完全可逆 —— 恢复走 chatgpt-pool-198-rejoin.sh(或单纯 scale=1 + resume_acct)。
#
# ## 什么时候该加 --keep-pods
#
# SKILL 里那张表写明:「在池」三层中**只有 ②(router 成员)才叫"在轮转里"**。
# 所以「把某号从循环池摘除」这个诉求,①(pod 活着)根本不必动 —— 摘完 arms 它就吃不到流量了。
# 而 scale=0 之后再 scale=1 是一次**重启**:2026-09-19 实测重启一个正在服务的号可能
# **永久打死它**(acct-237,回退救不回,只能重新登录)。
# ⇒ 号还 `ready` 且失败原因**尚未定位到账号本身**时,默认 `--keep-pods`:
#    收益(省一个 idle pod)远小于代价(把一个也许还能救的号变成必须重登)。
#    真要彻底停机/换 auth 再单独 scale=0,那是另一个决定。
#
# ## 顺序不许换:state 先写,arms 后删
#
# 188 上的 quota governor 每 5min 跑一轮,会把它认为 ONLINE 的号用 `/model/new` **加回 router**。
# 先删 arms 再标 state,中间那段窗口 governor 能把刚删的号原样塞回去。
# 所以顺序固定:①标 state(paused+manual_offline) → ②删 arms → ③收敛复核 → ④scale=0 → ⑤跨轮复查。
#
# ## ⑤ 不是装饰
#
# governor 是**读-改-写整份 state.json**:它在本轮开头把旧 state 读进内存,结尾整份写回。
# 你在这中间写的 state 会被**静默还原**(2026-09-12 acct-176 实测)。
# ⇒ `--apply` 跑完 **必须** 隔 ≥6min 再 `--verify` 一次;翻回去的就再 `--apply` 一遍再等一轮。
#
# ## 判据纪律
#
# - 摘 arms 归因只能按 unique `model_info.id`(`/model/info` 是 alias 展开的,按行数算必错)。
# - `/model/delete` **返回 400 也可能已经成功**,判据永远是 readback 不是状态码。
# - 收敛要**逐 proxy pod** 复核,不能只打 NodePort(4 个副本各自持有 router 内存态)。
# - `Succeeded` 相的残留 pod 不是"排水中",是历史 scale-down 留下的对象,与本轮无关;
#   判"停干净了"只数 **Running**(见 feedback_pod_deletiontimestamp_is_deadline_not_delete_time)。
set -u
NS=litellm-product
M198=10.68.13.198
M188=10.68.13.188
POOLKEY=${POOLKEY:?需要 export POOLKEY=<198 prod master key>；不再内置默认值}
STATE=/home/cltx/.chatgpt-quota/state/state.json

MODE=survey
KEEP_PODS=0
while :; do
  case "${1:-}" in
    --apply)     MODE=apply;   shift ;;
    --verify)    MODE=verify;  shift ;;
    --keep-pods) KEEP_PODS=1;  shift ;;
    *) break ;;
  esac
done
[ $# -gt 0 ] || { sed -n '2,8p' "$0"; exit 2; }
ACCTS=$(printf '%s\n' "$@" | sort -n -u | tr '\n' ' ')
PYLIST=$(printf '%s,' $ACCTS)

: "${P188:?env P188 (188 cltx password) required}"
s188() { sshpass -p "$P188" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 cltx@$M188 "$@"; }
if [ "$MODE" != verify ]; then
  : "${P198:?env P198 (198 cltx password) required}"
  s198() { sshpass -p "$P198" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 cltx@$M198 "$@"; }
  kx()   { s198 "echo '$P198' | sudo -S sh -c '$1' 2>/dev/null"; }
fi

echo "== 目标 $(echo $ACCTS | wc -w | tr -d ' ') 个: $ACCTS"
echo "== 模式 $MODE"

# ---------------------------------------------------------------- ⑤ 复查(单独可跑)
if [ "$MODE" = verify ]; then
  s188 "python3 -c \"
import json
T=[$PYLIST]
d=json.load(open('$STATE'))
bad=[]
for n in T:
    a=d.get('acct-%d'%n)
    if a is None: bad.append((n,'缺条目')); continue
    if not (a.get('paused') and a.get('manual_offline')): bad.append((n,a.get('tier'),a.get('paused'),a.get('manual_offline')))
print('state 翻回去的:', bad if bad else '无 —— %d/%d 站住'%(len(T),len(T)))
\""
  s188 "curl -s -H 'Authorization: Bearer $POOLKEY' http://$M198:30402/pro/v1/model/info | python3 -c \"
import json,sys,re
T=set([$PYLIST]); ids=set()
for e in json.load(sys.stdin).get('data',[]):
    m=re.match(r'chatgpt-acct-(\d+)-',(e.get('model_info') or {}).get('id',''))
    if m: ids.add(int(m.group(1)))
print('router 现有 acct 数',len(ids),'| 目标里又冒出来的:',sorted(ids&T) or '无')
\""
  exit 0
fi

# ---------------------------------------------------------------- ⓪ 勘察
kx "kubectl -n $NS get deploy -o custom-columns=N:.metadata.name,R:.spec.replicas --no-headers" \
  | awk '{print $1,$2}' > /tmp/pause-deploys.txt
python3 - "$PYLIST" <<'PY'
import sys
T=[int(x) for x in sys.argv[1].rstrip(',').split(',')]
have={}
for L in open('/tmp/pause-deploys.txt'):
    p=L.split()
    if len(p)>=2 and p[0].startswith('chatgpt-acct-'):
        try: have[int(p[0].rsplit('-',1)[1])]=int(p[1])
        except ValueError: pass
miss=[n for n in T if n not in have]
run=[n for n in T if have.get(n,0)>0]
print('  deploy 缺失:',miss or '无','| replicas>0:',len(run),'| 已是 0:',len(T)-len(miss)-len(run))
PY

echo "-- router 中目标持有的 arms:"
ARMS=$(s198 "curl -s -H 'Authorization: Bearer $POOLKEY' http://127.0.0.1:30402/pro/v1/model/info | python3 -c \"
import json,sys,re
T=set([$PYLIST]); out=set()
for e in json.load(sys.stdin).get('data',[]):
    i=(e.get('model_info') or {}).get('id','')
    m=re.match(r'chatgpt-acct-(\d+)-',i)
    if m and int(m.group(1)) in T: out.add(i)
print('\n'.join(sorted(out)))
\"")
NARM=$(echo "$ARMS" | grep -c . || true)
NACC=$(echo "$ARMS" | sed -nE 's/^chatgpt-acct-([0-9]+)-.*/\1/p' | sort -u | grep -c . || true)
echo "  $NACC 个号 / $NARM 条 arms"

if [ "$MODE" = survey ]; then
  echo "== dry-run 结束。确认无误后加 --apply 重跑。"
  exit 0
fi

# ---------------------------------------------------------------- ① 标 state(必须最先)
BAK=$STATE.bak-pause-$(date +%Y%m%d%H%M%S)
s188 "cp $STATE $BAK && echo '  ✓ state 已备份 $BAK'" || { echo '!!!! state 备份失败,停手'; exit 1; }
s188 "python3 -c \"
import json,pathlib,time
p=pathlib.Path('$STATE'); d=json.loads(p.read_text()); n=0
for i in [$PYLIST]:
    a=d.setdefault('acct-%d'%i,{})
    a.update({'tier':'SCALED_DOWN','paused':True,'manual_offline':True,
              'cause':'manual_offline','restore_at':0,'ts':int(time.time())})
    n+=1
p.write_text(json.dumps(d,indent=2,ensure_ascii=False))
print('  ✓ PAUSED_WRITTEN',n)
\"" | grep PAUSED_WRITTEN || { echo '!!!! state 写入失败,停手(arms 还没动)'; exit 1; }

# ---------------------------------------------------------------- ② 删 arms
echo "$ARMS" | grep . > /tmp/arms-to-delete.txt || : > /tmp/arms-to-delete.txt
sshpass -p "$P198" scp -o StrictHostKeyChecking=no -q /tmp/arms-to-delete.txt cltx@$M198:/tmp/arms-to-delete.txt
s198 "n=0; while read -r id; do [ -n \"\$id\" ] || continue;
  curl -s -o /dev/null -X POST http://127.0.0.1:30402/pro/model/delete \
    -H 'Authorization: Bearer $POOLKEY' -H 'Content-Type: application/json' \
    -d \"{\\\"id\\\":\\\"\$id\\\"}\"; n=\$((n+1)); done < /tmp/arms-to-delete.txt; echo \"  ✓ SENT=\$n\""

# ---------------------------------------------------------------- ③ 逐 pod 收敛复核
#
# ⚠️ 这里**不能用 kx()**。kx 自己套了一层 `sudo -S sh -c '...'`,把
# `-H 'Authorization: Bearer ...'` 的单引号吃掉 ⇒ curl 收到残废参数报错 ⇒ 又被 kx 的
# `2>/dev/null` 吞掉 ⇒ 这一句恒返 0 字节。2026-09-26 实测:摘 acct-175 那次 10 轮全部
# 「复核 0 个 proxy 副本」,这道门禁**一次都没真的验过**,却没有报错。
# ⇒ 改走 s198(不加 sh -c 那层,单引号不塌陷)+ 远端 env 传 bearer。
#
# 而且「复核 0 个副本 / 残留 0 个」和「全部收敛」长得一模一样。所以下面**必须**先断言
# 自己数得到副本(seen>0),数不到就是尺子坏了,直接红退,绝不许 fall through 到 ④。
CONVERGED=0
for r in $(seq 1 10); do
  sleep 12
  OUT=$(s198 "export KUBECONFIG=/home/cltx/.kube/config; K='$POOLKEY'
    for ip in \$(kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[*].status.podIP}'); do
      curl -s -m 20 -H \"Authorization: Bearer \$K\" http://\$ip:4000/v1/model/info; echo
    done" 2>/dev/null \
    | python3 -c "
import json,sys,re
T=set([$PYLIST]); bad=0; seen=0
for L in sys.stdin:
    L=L.strip()
    if not L: continue
    try: d=json.loads(L)
    except Exception: continue          # 不是 JSON 的行不计入 seen,否则又是一种假绿
    if not isinstance(d.get('data'), list): continue
    seen+=1; ids=set()
    for e in d['data']:
        m=re.match(r'chatgpt-acct-(\d+)-',(e.get('model_info') or {}).get('id',''))
        if m: ids.add(int(m.group(1)))
    if ids&T: bad+=1
print(seen,bad)")
  set -- $OUT
  echo "  收敛轮$r: 复核 ${1:-0} 个 proxy 副本,仍含目标的 ${2:-?} 个"
  [ "${1:-0}" -gt 0 ] && [ "${2:-1}" = 0 ] && { echo '  ✓ 全副本收敛,目标零残留'; CONVERGED=1; break; }
done
if [ "$CONVERGED" != 1 ]; then
  echo "!!!! ③ 收敛复核没通过。注意区分两种情况:"
  echo "     「复核 0 个副本」= 尺子坏了(取不到 pod IP 或 bearer 不对),什么都没验;"
  echo "     「复核 N 个、仍含目标 M 个」= 真没收敛。"
  echo "     state 已标 paused、arms 已发删除请求;**没有** scale 任何 pod。"
  echo "     PREPARED, NOT CONVERGED —— 先手工 readback 逐 pod 的 /v1/model/info 再决定下一步。"
  exit 1
fi

# ---------------------------------------------------------------- ④ scale=0
if [ "$KEEP_PODS" = 1 ]; then
  echo "  ⏭  --keep-pods:跳过 ④ scale=0,pod 留在 replicas=1。"
  echo "     目标已不在 router 轮转里(②已收敛),吃不到流量;没有重启风险。"
  kx "kubectl -n $NS get deploy -o custom-columns=N:.metadata.name,R:.spec.replicas --no-headers" \
    | awk '{print $1,$2}' > /tmp/pause-deploys2.txt
  python3 - "$PYLIST" <<'PY'
import sys
T=set(int(x) for x in sys.argv[1].rstrip(',').split(','))
keep=[]
for L in open('/tmp/pause-deploys2.txt'):
    p=L.split()
    if len(p)>=2 and p[0].startswith('chatgpt-acct-'):
        try: n=int(p[0].rsplit('-',1)[1])
        except ValueError: continue
        if n in T: keep.append((n,int(p[1])))
print('  目标副本数(应保持原样):', sorted(keep) or '无')
PY
else
for n in $ACCTS; do
  kx "kubectl -n $NS scale deploy chatgpt-acct-$n --replicas=0" >/dev/null 2>&1
done
echo '  ✓ SCALED_ALL'
kx "kubectl -n $NS get deploy -o custom-columns=N:.metadata.name,R:.spec.replicas --no-headers" \
  | awk '{print $1,$2}' > /tmp/pause-deploys2.txt
python3 - "$PYLIST" <<'PY'
import sys
T=set(int(x) for x in sys.argv[1].rstrip(',').split(','))
bad=[]
for L in open('/tmp/pause-deploys2.txt'):
    p=L.split()
    if len(p)>=2 and p[0].startswith('chatgpt-acct-'):
        try: n=int(p[0].rsplit('-',1)[1])
        except ValueError: continue
        if n in T and int(p[1])>0: bad.append(n)
print('  目标里仍 >0 的:', bad or '无 —— 全停')
PY
# 只数 Running:Succeeded 相的是历史残留对象,不是排水中
kx "kubectl -n $NS get pod --no-headers --field-selector=status.phase=Running -o custom-columns=N:.metadata.name" \
  | sed -nE 's/^chatgpt-acct-([0-9]+)-.*/\1/p' | sort -u > /tmp/pause-running.txt
python3 - "$PYLIST" <<'PY'
import sys
T=set(int(x) for x in sys.argv[1].rstrip(',').split(','))
run={int(l) for l in open('/tmp/pause-running.txt') if l.strip()}
print('  目标里仍 Running 的 pod:', sorted(run&T) or '无')
PY
fi

echo
echo "== ⚠️ 还没完:等 ≥6min(一整轮 governor cron)后必须跑一次"
echo "   P188='***' $0 --verify $ACCTS"
echo "   翻回去的号就再 --apply 一遍,再等一轮。回滚 state 用备份 $BAK"
