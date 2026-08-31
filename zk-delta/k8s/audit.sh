#!/usr/bin/env bash
# zk-delta/k8s/audit.sh —— 只读巡检：集群上跑的，还是不是我验过的那个东西
#
# 为什么要有这个：
#   zk-delta 的正确性不是靠代码写得对来保证的，是靠**几条运行时不变量**保证的：
#   单副本、Recreate、跑的源码就是仓库里这份、409 没在偷偷涨。
#   这几条里任何一条被人改掉（或被一次误 apply 回退掉），它都还会返回 200，
#   不会有任何报警——只会静默退化甚至错位。所以必须有一条能定期跑的、
#   会用退出码说话的巡检。
#
#   这是 scripts/litellm-198-max-input-tokens-audit.sh 的同款做法：
#   把"没配就静默继承"这类看不见的事，变成会告警的事。
#
# 只读保证：全脚本只有 get / describe / curl，没有 apply / patch / delete / restart。
#
# 用法：
#   ZKD_SSH_PASS=... ./zk-delta/k8s/audit.sh
#   退出码 0 = 全部不变量成立；1 = 有漂移（打印是哪一条）；2 = 环境/连不上
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
NS=litellm-product
PUBLIC_URL="${ZKD_PUBLIC_URL:-https://cc.auto-link.com.cn/zkd}"

SSH_HOST="${ZKD_SSH_HOST:-cltx@10.68.13.198}"
SSH_PASS="${ZKD_SSH_PASS:-}"
if [[ -z "$SSH_PASS" ]]; then
  echo "需要 ZKD_SSH_PASS（198 的 ssh 口令）"; exit 2
fi

r198 () { sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 "$SSH_HOST" "$@" 2>/dev/null; }
K () { r198 "echo '$SSH_PASS' | sudo -S k3s kubectl -n $NS $*"; }

drift=0
warn=0
ok ()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad ()  { printf '  \033[31m✗\033[0m %s\n' "$1"; drift=1; }
soft () { printf '  \033[33m!\033[0m %s\n' "$1"; warn=1; }

echo "zk-delta 只读巡检  $(date '+%F %T')"
echo "============================================================"

# ---------- 0. 能不能连上 ----------
if ! r198 "true"; then echo "连不上 198"; exit 2; fi

DEP_JSON="$(K "get deploy zk-delta -o json")"
if [[ -z "$DEP_JSON" || "$DEP_JSON" != \{* ]]; then
  echo "拿不到 deploy/zk-delta —— 它还在吗？"; exit 1
fi
jqd () { printf '%s' "$DEP_JSON" | python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

# ---------- 1. 单副本 ----------
# 会话状态在进程内存里。两个副本各存各的，客户端的 handle 有一半会认不出 →
# 狂 409 → 全部退化成全量。不出错，但白装。
echo
echo "[1] 副本与更新策略"
REPL="$(jqd 'd["spec"]["replicas"]')"
[[ "$REPL" == "1" ]] && ok "replicas = 1" || bad "replicas = ${REPL}（必须是 1：会话状态在进程内存里，多副本必然 409 风暴）"

STRAT="$(jqd 'd["spec"]["strategy"]["type"]')"
[[ "$STRAT" == "Recreate" ]] && ok "strategy = Recreate" || bad "strategy = ${STRAT}（必须 Recreate：滚动更新期间新旧并存同样会 409 风暴）"

# ---------- 2. 镜像来源 ----------
# 铁律：K8s 不许从公网仓库拉。这里复用节点本地已有镜像 + Never，
# 一旦有人把 pullPolicy 改回 Always，下次调度就会去打 docker.io。
echo
echo "[2] 镜像来源"
IMG="$(jqd 'd["spec"]["template"]["spec"]["containers"][0]["image"]')"
POL="$(jqd 'd["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"]')"
[[ "$POL" == "Never" ]] && ok "imagePullPolicy = Never（${IMG}，用节点本地镜像）" \
  || bad "imagePullPolicy = ${POL}（必须 Never：镜像只在节点本地，Always 会去打公网仓库）"

# ---------- 3. 跑的源码 == 仓库里这份 ----------
# 源码是 ConfigMap 挂进去的，所以"pod 是新的"不等于"代码是新的"。
# 指纹算法必须与 apply.sh 完全一致：cat framing.js server.js | sha256 | 前 16 位。
echo
echo "[3] 集群上跑的源码是不是仓库这份"
LOCAL_SHA="$(cat "$ROOT/common/framing.js" "$ROOT/server/server.js" | shasum -a 256 | cut -c1-16)"
echo "     仓库指纹 = $LOCAL_SHA"

CM_SHA="$(K "get cm zk-delta-src -o json" | python3 -c '
import sys,json,base64,hashlib
d=json.load(sys.stdin)["data"]
blob=(d["framing.js"]+d["server.js"]).encode("utf-8")
print(hashlib.sha256(blob).hexdigest()[:16])
' 2>/dev/null)"
[[ "$CM_SHA" == "$LOCAL_SHA" ]] && ok "ConfigMap zk-delta-src 内容与仓库一致" \
  || bad "ConfigMap 指纹 = ${CM_SHA:-读不到}，与仓库 $LOCAL_SHA 不同（集群跑的不是这份代码）"

ANN="$(jqd 'd["spec"]["template"]["metadata"]["annotations"].get("zk-delta/src-sha","")')"
[[ "$ANN" == "$LOCAL_SHA" ]] && ok "pod 模板注解 src-sha 一致" \
  || bad "pod 模板注解 src-sha = ${ANN:-空}，与仓库 $LOCAL_SHA 不同"

# ---------- 4. pod 现状 ----------
echo
echo "[4] pod 现状"
POD_LINE="$(K "get pod -l app=zk-delta --no-headers -o custom-columns=N:.metadata.name,R:.status.containerStatuses[0].ready,RS:.status.containerStatuses[0].restartCount,P:.status.phase" | tr -s ' ')"
N_POD="$(printf '%s\n' "$POD_LINE" | grep -c . || true)"
if [[ "$N_POD" != "1" ]]; then
  bad "pod 数量 = ${N_POD}（应为 1）"
  printf '%s\n' "$POD_LINE" | sed 's/^/      /'
else
  read -r PN PR PRS PP <<<"$POD_LINE"
  [[ "$PR" == "true" && "$PP" == "Running" ]] && ok "$PN Running/Ready" || bad "$PN ready=$PR phase=$PP"
  if [[ "$PRS" == "0" ]]; then ok "重启次数 0"
  else soft "重启次数 $PRS —— 重启会丢光内存里的会话状态（不出错，但每条会话下一发退化成全量）"; fi
fi

# ---------- 5. 对外那扇门 ----------
echo
echo "[5] nginx /zkd/ 入口"
if r198 "grep -rlsq 'location .*\/zkd\/' /etc/nginx/sites-enabled/ 2>/dev/null"; then
  ok "nginx sites-enabled 里有 /zkd/ location"
else
  bad "nginx sites-enabled 里找不到 /zkd/ location（本机小代理会全部走回落，等于没装）"
fi

HZ="$(curl -fsS -m 10 "$PUBLIC_URL/healthz" 2>/dev/null)"
if [[ "$HZ" == *'"ok":true'* ]]; then
  ok "公网 $PUBLIC_URL/healthz → $HZ"
else
  bad "公网 /healthz 打不通或不健康：${HZ:-无响应}"
fi

# ---------- 6. 计数器：有没有在偷偷退化 ----------
# 409 本身是**设计行为**（认不出基线就硬报错让客户端重发全量），不是 bug。
# 但 409 占比高说明基线一直在失效，收益会被吃光；上游非 2xx 则是真问题。
echo
echo "[6] 运行计数器"
M="$(curl -fsS -m 10 "$PUBLIC_URL/metrics.json" 2>/dev/null)"
if [[ -z "$M" ]]; then
  bad "/metrics.json 读不到"
else
  python3 - "$M" <<'PY'
import sys,json
m=json.loads(sys.argv[1])
tot=m.get("req_total",0); r409=m.get("reject_409",0)
non2=m.get("upstream_non2xx",0); rb=m.get("rebuild_ok",0)
bi=m.get("bytes_in",0); bo=m.get("bytes_out",0)
print(f'     请求 {tot}（增量 {m.get("req_delta",0)} / 全量 {m.get("req_full",0)}），'
      f'重建成功 {rb}，409 {r409}，上游非2xx {non2}')
if bi: print(f'     广域网收进 {bi/1048576:.2f}MB → 内网发出 {bo/1048576:.2f}MB = 放大 {bo/bi:.1f}x')
print(f'     活跃会话 {m.get("conv_live",0)}，被挤掉 {m.get("conv_evicted",0)}，'
      f'状态占用 {m.get("store_bytes",0)/1048576:.2f}MB')
bads=[]
if tot and rb != tot - r409:
    bads.append(f'重建成功 {rb} != 请求 {tot} - 409 {r409}：有请求既没重建也没被拒，说明有第三条路径')
# 非 2xx 按状态码分开判，别一锅炖：
#   401/403 = 客户端凭据问题（比如 switch.sh 自检不带 Key），我们只是忠实透传，不是漂移；
#   400     = 要盯死的那个形状（Cursor 长会话撞入口闸门那条线），一发都要报；
#   其余 4xx/5xx = 真出事。
byst=m.get("upstream_by_status") or {}
if byst:
    print("     上游非2xx 分布：", json.dumps(byst, ensure_ascii=False))
    auth=sum(v for k,v in byst.items() if k in ("401","403"))
    hard={k:v for k,v in byst.items() if k not in ("401","403")}
    if auth: print(f'     其中 {auth} 发是 401/403（凭据问题，非漂移，不计入异常）')
    if hard: bads.append(f'上游硬失败 {json.dumps(hard, ensure_ascii=False)}（400 尤其要查）')
elif non2:
    # 老版本服务端没有 upstream_by_status，退回粗判，免得静默放过
    bads.append(f'上游非 2xx {non2} 发（该服务端版本没有分状态码计数，无法细分）')
if tot >= 20 and r409 / tot > 0.30:
    bads.append(f'409 占比 {r409*100/tot:.0f}% > 30%：基线一直在失效，收益会被吃光')
if m.get("reject_by_reason"): print("     拒绝原因分布：", json.dumps(m["reject_by_reason"], ensure_ascii=False))
for b in bads: print("  异常：" + b)
sys.exit(1 if bads else 0)
PY
  if [[ $? -eq 0 ]]; then ok "计数器全部正常"; else bad "计数器异常（原因见上）"; fi
fi

# ---------- 7. 容量：离 OOM 还有多远 ----------
# 为什么这条必须在事故**之前**就能红：
#   单副本 + 会话状态在进程内存里 → OOMKill 一次是所有人的会话全丢，
#   而且**不报错**，只是每条会话下一发退化成全量。事后只能从 restartCount 反推，
#   那时候已经丢过一轮了。所以要盯活的 RSS，不是盯重启次数。
#
# 斜率 1.24 的出处：zk-delta/tests/capacity_probe.js 本地实测（08-31，60 条 ~4.6MB 会话，
#   最小二乘 rss ≈ 175MB + 1.13×store，取相邻点最大边际斜率 1.24 做保守值）。
#   **不许用 rss/store 这个总倍率**——它被固定基座污染，store 小时会飙到 20x。
#   从当前这个实测点按边际斜率外推，就不需要猜基座是多少。
echo
echo "[7] 容量（离 OOM 还有多远）"
MEM_LIM="$(jqd 'd["spec"]["template"]["spec"]["containers"][0].get("resources",{}).get("limits",{}).get("memory","")')"
if [[ -z "$M" ]]; then
  soft "上一步没读到 /metrics.json，这条跳过"
elif [[ -z "$MEM_LIM" ]]; then
  bad "容器没设 memory limit —— 撑爆会连累整个节点（198 有过 disk/mem 压力把 proxy 拖 Pending 的先例）"
else
  python3 - "$M" "$MEM_LIM" <<'PY'
import sys,json,re
m=json.loads(sys.argv[1]); lim=sys.argv[2]
mult={"Ki":1024,"Mi":1048576,"Gi":1073741824,"K":1000,"M":10**6,"G":10**9}
mo=re.match(r'^(\d+)([A-Za-z]*)$', lim)
if not mo: print(f'  异常：memory limit "{lim}" 解析不了'); sys.exit(1)
LIM=int(mo.group(1))*mult.get(mo.group(2),1)
rss=m.get("rss_bytes"); store=m.get("store_bytes",0); cap=m.get("store_max_bytes")
if rss is None or cap is None:
    print('  异常：服务端 /metrics.json 里没有 rss_bytes/store_max_bytes —— '
          '跑的是加这两个字段之前的旧源码，容量这条腿量不了（先 apply.sh 推新源码）')
    sys.exit(1)
MB=1048576.0
K_MARGINAL=1.24          # 出处见上面注释
HEADROOM=400*MB          # 并发请求各自持有一份重建出的全量 buffer，给它留的空间
print(f'     limit {LIM/MB:.0f}MB，当前 RSS {rss/MB:.0f}MB（{rss*100/LIM:.0f}%），'
      f'会话状态 {store/MB:.1f}MB / 上限 {cap/MB:.0f}MB')
bads=[]; warns=[]
# 腿一：眼下这一刻的水位
if rss > LIM*0.85: bads.append(f'RSS 已到 limit 的 {rss*100/LIM:.0f}%（>85%），随时可能 OOMKill')
elif rss > LIM*0.70: warns.append(f'RSS 到 limit 的 {rss*100/LIM:.0f}%（>70%），该看是不是要调小 ZKD_MAX_BYTES 或加 limit')
# 腿二：把配的上限**撑满**会怎样。从当前实测点按边际斜率外推。
proj = rss + K_MARGINAL*max(0, cap-store)
print(f'     把 {cap/MB:.0f}MB 上限撑满 → RSS ≈ {proj/MB:.0f}MB（按实测边际 {K_MARGINAL}x 外推），'
      f'再留 {HEADROOM/MB:.0f}MB 给并发全量 buffer')
if proj+HEADROOM > LIM:
    want=int((LIM-HEADROOM-rss)/K_MARGINAL/MB + store/MB)
    bads.append(f'撑满会超 limit（{proj/MB:.0f}+{HEADROOM/MB:.0f} > {LIM/MB:.0f}）→ '
                f'ZKD_MAX_BYTES 要调到 {want}MB 以下，或把 limit 加上去')
else:
    print(f'     ✓ 撑满也在 limit 内')
# 腿三：条数上限单独拦不住。实测 400 条 × 4.6MB = 1847MB store，早越过 800MB 字节上限。
avg=m.get("conv_avg_bytes",0); cm=m.get("conv_max",0)
if avg and cm:
    conv_only=avg*cm
    print(f'     条数上限 {cm} × 当前均值 {avg/MB:.2f}MB = {conv_only/MB:.0f}MB '
          f'{"＞" if conv_only>cap else "≤"} 字节上限 {cap/MB:.0f}MB '
          f'→ 真正在拦的是{"字节上限（条数上限单独拦不住，两条都得留）" if conv_only>cap else "条数上限，字节上限兜底"}')
ev=m.get("conv_evicted",0)
if ev: print(f'     已淘汰 {ev} 条会话（被淘汰那条下一发退化成全量，不出错——这是设计行为，不是故障）')
for w in warns: print("  留意：" + w)
for b in bads: print("  异常：" + b)
sys.exit(1 if bads else (3 if warns else 0))
PY
  rc=$?
  if   [[ $rc -eq 0 ]]; then ok "容量有余量"
  elif [[ $rc -eq 3 ]]; then soft "容量偏紧（见上）"
  else bad "容量不安全（见上）"; fi
fi

# ---------- 汇总 ----------
echo
echo "============================================================"
if [[ "$drift" == "1" ]]; then
  echo "结论：有漂移 —— 上面标 ✗ 的每一条都要处理。"
  exit 1
fi
if [[ "$warn" == "1" ]]; then
  echo "结论：不变量全部成立，但有需要留意的项（标 !）。"
else
  echo "结论：全部不变量成立。"
fi
exit 0
