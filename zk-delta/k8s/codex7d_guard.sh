#!/usr/bin/env bash
# zk-delta/k8s/codex7d_guard.sh —— 只读：codex 那条线的 7 天窗口，量一次存一份
#
# 为什么要有这个：
#   `cursor-web-fc-82-terra` 的 deployment id 是 zerokey-cursor-web-fc-82-terra，钉在 acct-82；
#   而 codex 的两个组（chatgpt-gpt-5.3-codex / chatgpt-codex-auto-review）各 35 个账号，
#   **acct-82 就在里面**。同一个 ChatGPT 账号 = 同一个 24h/7d 窗口。
#   也就是说 Cursor 这条线和 codex 那条线共用配额，我在这边跑回归，那边的额度就少一格。
#
#   zk-delta 本身不会影响它：重建发生在 LiteLLM 之前，上游收到的请求数与字节
#   和不装它时逐字节相同（这是 ⑨/⑩ 那两组等式的直接推论）。**能影响它的是我的回归流量。**
#   所以要有一把尺子，改动前后各量一次，用数据说「没动过」，而不是用我的说法。
#
# 只读保证：全脚本只有 select，没有 insert/update/delete，也不碰任何 k8s 资源。
#
# 用法：
#   ZKD_SSH_PASS=... ./zk-delta/k8s/codex7d_guard.sh snap before
#   ZKD_SSH_PASS=... ./zk-delta/k8s/codex7d_guard.sh snap after
#   ZKD_SSH_PASS=... ./zk-delta/k8s/codex7d_guard.sh diff before after
#
#   退出码：0 = 正常（diff 时 = codex 侧没被动）；1 = codex 侧有增量；2 = 环境问题
#
# ⚠️ 这把尺子的有效范围（08-31 实测撞出来的，写在这里免得下次又误读）：
#   前后差**只有在集群空闲时**才等于「我打的量」。实测 08-31 03:00 那一小时里，
#   gpt-5.6-sol 全公司正常业务就有 4165 发，而我自己只打了 26 发到 local-deepseek-v4-flash。
#   于是 diff 报出一堆 ! 和「codex 侧有新增请求」—— 那全是别人正常上班的流量，不是我的。
#   **上班时段拿这个 diff 判「我有没有碰 codex」会 100% 假阳。**
#
#   真正有效的判据是 `mine` 子命令：直接看这段时间里各模型的请求数，
#   我打了什么模型是我自己控制的，别人的流量落在别的模型上，一眼分得开。
#   diff 只在夜间/集群静默时才有意义，留着是因为那种场合它确实能用。
set -uo pipefail

SSH_HOST="${ZKD_SSH_HOST:-cltx@10.68.13.198}"
SSH_PASS="${ZKD_SSH_PASS:-}"
NS=litellm-product
# 凭据必须来自 env，不留硬编码兜底默认值：兜底值等于把真口令提交进仓库，
# 且口令轮转后老默认值会静默继续生效，认证失败看不出是"忘了设 env"还是"口令换了"。
PGPASS="${ZKD_PGPASS:?需要 ZKD_PGPASS（litellm-db-0 的 PG 口令，别写进文件/命令行历史）}"
OUTDIR="${ZKD_SNAP_DIR:-/tmp/zkd-codex7d}"

[[ -n "$SSH_PASS" ]] || { echo "需要 ZKD_SSH_PASS（198 的 ssh 口令）"; exit 2; }

# codex 那条线的组名。注意：SpendLogs 的 model_group 记的是**客户端请求的那个别名**，
# 不是 ProxyModelTable 里的部署组名 —— 我第一版照抄了部署组名（chatgpt-codex-auto-review
# 之类），查出来 0 行。0 行当时看着像「codex 没流量」，其实是我查错了列的含义。
# 下面这四个是从 SpendLogs 里实测捞出来的真名。
CODEX_GROUPS="gpt-5.3-codex,chatgpt-gpt-5.3-codex,codex-auto-review,zerokey-codex-bridge"

# SQL 用 base64 传过去。直接拼字符串行不通：表名 "LiteLLM_SpendLogs" 自带双引号
# （Postgres 大小写敏感标识符必须带），而命令要穿过 本地 shell → ssh → 远端 shell →
# kubectl exec 四层，那对双引号会把 psql -c 的参数提前截断。截断后 psql 报错，
# 而我第一版还写了 2>/dev/null —— 看到的「0 行」是被吞掉的报错，不是「codex 没流量」。
# **别再靠层层转义猜对不对，编码一次传过去。**
sql () {
  local b64
  b64="$(printf '%s' "$1" | base64 | tr -d '\n')"
  sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=25 "$SSH_HOST" \
    "echo '$SSH_PASS' | sudo -S k3s kubectl exec -n $NS litellm-db-0 -- \
       sh -c 'echo $b64 | base64 -d > /tmp/zkd_q.sql; PGPASSWORD=$PGPASS psql -U litellm -d litellm -At -F \"|\" -f /tmp/zkd_q.sql'" \
    | grep -v '^\[sudo\]'
}

# 7 天窗口内 codex 组的账：按组分，再单独把 acct-82（共用的那一个）拎出来。
# request_id 计数而不是 count(*)，因为一次调用可能落多行（流式/重试）。
snap_sql () {
  local groups_in
  groups_in="$(printf "'%s'," ${CODEX_GROUPS//,/ } | sed "s/,$//")"
  cat <<SQL
select 'group:'||model_group, count(distinct request_id), coalesce(sum(total_tokens),0), coalesce(round(sum(spend)::numeric,6),0)
  from "LiteLLM_SpendLogs"
 where "startTime" > now() - interval '7 days' and model_group in ($groups_in)
 group by model_group
union all
select 'acct82:'||coalesce(model_group,'?'), count(distinct request_id), coalesce(sum(total_tokens),0), coalesce(round(sum(spend)::numeric,6),0)
  from "LiteLLM_SpendLogs"
 where "startTime" > now() - interval '7 days' and model_id ~ '(^|[^0-9])82([^0-9]|\$)'
 group by model_group
order by 1
SQL
}

case "${1:-}" in
  snap)
    TAG="${2:-$(date +%Y%m%d-%H%M%S)}"
    mkdir -p "$OUTDIR"
    F="$OUTDIR/$TAG.txt"
    # 快照要带时间戳：7 天窗口是滑动的，两次快照之间窗口本身也在往前挪，
    # 对账时必须知道差了多久，否则「窗口滑出去的老数据」会被误读成「我动了它」。
    echo "# snap_at=$(date -u '+%FT%TZ')" > "$F"
    ROWS="$(sql "$(snap_sql)")"
    # 空结果必须当失败处理，不能当「codex 没流量」—— 见 sql() 上面那段注释。
    if [[ -z "$ROWS" ]]; then
      echo "!! 查询返回 0 行。codex 这条线不可能 7 天零流量，所以这是查询坏了，不是没数据。" >&2
      rm -f "$F"; exit 2
    fi
    printf '%s\n' "$ROWS" >> "$F"
    echo "快照 -> $F"
    grep -v '^#' "$F" | sed 's/^/  /'
    ;;
  diff)
    A="$OUTDIR/${2:?需要 before 快照名}.txt"; B="$OUTDIR/${3:?需要 after 快照名}.txt"
    [[ -f "$A" && -f "$B" ]] || { echo "快照不存在：$A / $B"; exit 2; }
    python3 - "$A" "$B" <<'PY'
import sys,re
def load(p):
    at=None; rows={}
    for ln in open(p):
        ln=ln.rstrip('\n')
        if ln.startswith('# snap_at='): at=ln.split('=',1)[1]; continue
        if ln.startswith('#') or not ln.strip(): continue
        f=ln.split('|')
        if len(f)>=4:
            try: rows[f[0]]=(int(f[1]),int(f[2]),float(f[3]))
            except ValueError: pass
    return at,rows
at_a,a=load(sys.argv[1]); at_b,b=load(sys.argv[2])
print(f'before {at_a}  ->  after {at_b}')
print('-'*74)
keys=sorted(set(a)|set(b))
moved=[]
for k in keys:
    ra=a.get(k,(0,0,0.0)); rb=b.get(k,(0,0,0.0))
    d=(rb[0]-ra[0], rb[1]-ra[1], round(rb[2]-ra[2],6))
    flag='   ' if d[0]==0 and d[1]==0 else ' ! '
    if d[0]!=0 or d[1]!=0: moved.append((k,d))
    print(f'{flag}{k:<46} 请求 {ra[0]:>6} -> {rb[0]:<6} Δ{d[0]:<+6} tokens Δ{d[1]:<+10} 花费 Δ{d[2]:+.6f}')
print('-'*74)
# 7 天窗口是滑动的：老数据滑出去会让 Δ 变负。负数不是「我动了它」，
# 只有**正的**增量才说明这一轮往 codex 那条线上打了流量。
grew=[(k,d) for k,d in moved if d[0]>0]
if grew:
    print('codex 侧有新增请求：')
    for k,d in grew: print(f'  {k}  +{d[0]} 发 / +{d[1]} tokens')
    sys.exit(1)
shrank=[(k,d) for k,d in moved if d[0]<0]
if shrank:
    print(f'只有 {len(shrank)} 项变负 = 7 天窗口把老数据滑出去了，不是本轮动的。')
print('codex 那条线在这一轮里零新增请求。')
PY
    exit $?
    ;;
  mine)
    # 「我这一轮打了什么」——按小时 × 模型列出来。这才是能回答「有没有碰 codex」的那把尺子：
    # 我打哪个模型是我自己控制的，别人的流量落在别的模型上，两者在这张表里分得清清楚楚。
    # 用法：codex7d_guard.sh mine [小时数，默认 3]
    HRS="${2:-3}"
    ROWS="$(sql "select date_trunc('hour',\"startTime\"), model_group, count(distinct request_id)
                   from \"LiteLLM_SpendLogs\"
                  where \"startTime\" > now() - interval '$HRS hours'
                  group by 1,2 having count(distinct request_id) > 0
                  order by 1 desc, 3 desc")"
    [[ -n "$ROWS" ]] || { echo "!! 查询返回 0 行 = 查询坏了（见 sql() 上的注释）" >&2; exit 2; }
    printf '%s\n' "$ROWS" | python3 -c '
import sys,collections
CODEX={"gpt-5.3-codex","chatgpt-gpt-5.3-codex","codex-auto-review","zerokey-codex-bridge"}
by=collections.OrderedDict()
for ln in sys.stdin:
    f=ln.rstrip("\n").split("|")
    if len(f)<3: continue
    by.setdefault(f[0],[]).append((f[1],int(f[2])))
for h,rows in by.items():
    print(f"\n{h}")
    for g,n in sorted(rows,key=lambda x:-x[1])[:12]:
        tag=" ← codex 那条线" if g in CODEX else ""
        print(f"   {n:>6}  {g}{tag}")
print("\n判读：自己那几发落在自己用的模型名上；codex 那几行的量是全公司在跑，")
print("      跟我打了多少没关系。要证明「没碰 codex」，看的是我的模型名对不对，")
print("      不是看 codex 那行有没有涨（上班时段它必然涨）。")
'
    ;;
  *) echo "用法: $0 {snap [tag] | diff <before> <after> | mine [小时数]}"; exit 2 ;;
esac
