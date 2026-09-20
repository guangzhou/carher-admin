#!/bin/bash
# rh.sh — 在 226 节点上跑的 re-OAuth 远端驱动。由 scripts/reoauth-one-shot.sh 下发。
#
# 为什么所有 marker 都定义在本文件里、绝不出现在 jms 命令行:
#   jms ssh --tty 会把命令原文回显到输出流。marker 字面量若同时存在于命令和输出，
#   本地 grep 会把「命令回显」当成「远端结果」。2026-08-05 踩过: WAIT=DONE_B64 /
#   SUCCEEDED_NOB64 / FAILED 三个互斥值同时"出现"，全是回显。
#
# 另一条踩过的: apply 的 stderr 绝不能丢。丢了之后 Job 没建成也看不出原因，
#   轮询就对着一个不存在的 Job 空转满 540s(acct-129 实证，9 分钟全废)。
set -uo pipefail
NS=carher

case "$1" in
run)
  N="$2"; MAX="${3:-540}"; J=cgpt-onboard-oauth-$N
  T0=$(date +%s)

  # ── 幂等守卫: 上一次会话可能「输出没回来但远端真的跑了」──────────────────
  # jms PTY 的输出捕获不可靠，本地看不到 sha 不等于远端没执行(2026-08-05 acct-52 实证:
  # 本地判定"未执行"后重试，实际第一次已建 job、pod 已起 Xvfb，重建的 pod 撞上还活着的
  # 旧 Xvfb → "server already running" 42s 就 FATAL)。所以成败一律看 presence:
  #   已有产出 → 直接 dump，绝不重跑(重跑白烧一次真号登录)
  if kubectl -n $NS logs job/$J 2>/dev/null | grep -q B64END; then
    echo "RH_GUARD=reuse_existing_b64"
    echo "RH_JOBOK=$J"
    echo "RH_HASB64=1"
    echo "RH_DEACT=$(kubectl -n $NS logs job/$J 2>/dev/null | grep -ci 'account_deactivated')"
    bash "$0" dump "$N"
    exit 0
  fi
  # 正在跑且还没到结论 → 接着等，别删了重来
  ACTIVE=$(kubectl -n $NS get job $J -o jsonpath='{.status.active}' 2>/dev/null)
  if [ "${ACTIVE:-0}" != "0" ] && [ -n "${ACTIVE:-}" ]; then
    echo "RH_GUARD=attach_running_job"
  else
    kubectl -n $NS delete job $J --ignore-not-found >/dev/null 2>&1
    # 删 Job ≠ pod 立刻死。必须等 pod 真的消失，否则新 pod 的 Xvfb 撞旧 pod 的
    # (hostNetwork 下 X11 abstract socket 在 network namespace 里，同节点全局唯一)
    for i in $(seq 1 40); do
      C=$(kubectl -n $NS get pod -l job-name=$J --no-headers 2>/dev/null | grep -c .)
      [ "${C:-0}" = "0" ] && break
      sleep 3
    done
    echo "RH_PODSGONE=$(kubectl -n $NS get pod -l job-name=$J --no-headers 2>/dev/null | grep -c .)"
    A=$(kubectl apply -f /tmp/reoauth-$N.yaml 2>&1 | tr '\n' ';' | cut -c1-400)
    echo "RH_APPLYOUT=$A"
  fi

  # presence 校验: 不信任 apply 的输出(PTY 通道会截断成败行)
  GOT=$(kubectl -n $NS get job $J -o jsonpath='{.metadata.name}' 2>/dev/null)
  echo "RH_JOBOK=$GOT"
  if [ "$GOT" != "$J" ]; then echo "RH_ABORT=nojob"; exit 0; fi
  echo "RH_SECKEYS=$(kubectl -n $NS get secret cgpt-onboard-creds-$N -o go-template='{{len .data}}' 2>/dev/null)"

  # ── 轮询: 任何一步发现 Job 消失就立刻退出，不空转 ──
  REASON=timeout
  for i in $(seq 1 $((MAX/10))); do
    if ! kubectl -n $NS get job $J >/dev/null 2>&1; then REASON=jobvanished; break; fi
    if kubectl -n $NS logs job/$J 2>/dev/null | grep -q B64END; then REASON=gotb64; break; fi
    S=$(kubectl -n $NS get job $J -o jsonpath='{.status.succeeded}' 2>/dev/null)
    F=$(kubectl -n $NS get job $J -o jsonpath='{.status.failed}' 2>/dev/null)
    if [ "${S:-0}" = "1" ]; then REASON=succeeded; break; fi
    if [ -n "$F" ] && [ "$F" != "0" ]; then REASON=jobfailed; break; fi
    sleep 10
  done
  echo "RH_REASON=$REASON"
  echo "RH_ELAPSED=$(( $(date +%s) - T0 ))"
  echo "RH_STATE=s:$(kubectl -n $NS get job $J -o jsonpath='{.status.succeeded}' 2>/dev/null)|f:$(kubectl -n $NS get job $J -o jsonpath='{.status.failed}' 2>/dev/null)|a:$(kubectl -n $NS get job $J -o jsonpath='{.status.active}' 2>/dev/null)"

  # pod 侧诊断: 起不来时 Job 状态什么都看不出(ImagePullBackOff / 节点不可调度 等)
  P=$(kubectl -n $NS get pod -l job-name=$J -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  echo "RH_POD=$P"
  if [ -n "$P" ]; then
    echo "RH_PODPHASE=$(kubectl -n $NS get pod $P -o jsonpath='{.status.phase}' 2>/dev/null)"
    echo "RH_PODWAIT=$(kubectl -n $NS get pod $P -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}{.status.containerStatuses[0].state.terminated.reason}' 2>/dev/null)"
  else
    echo "RH_PODEVENTS=$(kubectl -n $NS get events --field-selector involvedObject.name=$J 2>/dev/null | tail -3 | tr '\n' ';' | cut -c1-300)"
  fi

  L=$(kubectl -n $NS logs job/$J 2>/dev/null)
  echo "RH_HASB64=$(echo "$L" | grep -c B64END)"
  echo "RH_DEACT=$(echo "$L" | grep -ci 'account_deactivated')"
  echo "RH_TCE=$(echo "$L" | grep -ci 'TargetClosedError')"
  echo "RH_OTPWAIT=$(echo "$L" | grep -ci 'waiting for otp\|no otp\|otp not found')"
  echo "RH_LAST=$(echo "$L" | tr -d '\r' | grep -v '^$' | tail -8 | tr '\n' '~' | cut -c1-900)"

  # ── 有产出才 dump ──
  if [ "$(echo "$L" | grep -c B64END)" != "0" ]; then bash "$0" dump "$N"; fi
  kubectl -n $NS delete job $J "secret/cgpt-onboard-creds-$N" --ignore-not-found >/dev/null 2>&1
  ;;

dump)
  N="$2"; J=cgpt-onboard-oauth-$N
  kubectl -n $NS logs job/$J > /tmp/rh-$N.log 2>&1
  python3 - /tmp/rh-$N.log /tmp/rh-auth-$N.json <<'PY'
import sys,re,json,base64
log,out=sys.argv[1],sys.argv[2]
t=open(log,encoding='utf-8',errors='replace').read()
m=re.search(r'B64BEGIN(.*?)B64END',t,re.S)
if not m: print("RH_DUMP=NOMARKER"); sys.exit(0)
d=json.loads(base64.b64decode(re.sub(r'\s+','',m.group(1))))
at=d.get('access_token','') or (d.get('tokens') or {}).get('access_token','')
if len(at)<=1000: print(f"RH_DUMP=SHORTTOKEN:{len(at)}"); sys.exit(0)
open(out,'w').write(json.dumps(d,indent=2))
idt=d.get('id_token','') or (d.get('tokens') or {}).get('id_token','')
try:
    p=idt.split('.')[1]; p+='='*(-len(p)%4)
    c=json.loads(base64.urlsafe_b64decode(p)); a=c.get('https://api.openai.com/auth',{})
    print(f"RH_IDENT=email:{c.get('email')}|plan:{a.get('chatgpt_plan_type')}|sub_until:{a.get('chatgpt_subscription_active_until')}|acctid:{a.get('chatgpt_account_id')}|access_len:{len(at)}|rt_len:{len(d.get('refresh_token') or '')}")
except Exception as e:
    print(f"RH_IDENT=UNPARSED:{e}|access_len:{len(at)}")
PY
  [ -s /tmp/rh-auth-$N.json ] || { echo "RH_DUMP=NOFILE"; exit 0; }
  gzip -c /tmp/rh-auth-$N.json | base64 -w0 > /tmp/rh-$N.b64
  echo "RH_BLEN=$(tr -d '\n' < /tmp/rh-$N.b64 | wc -c | tr -d ' ')"
  echo "RH_BSHA=$(tr -d '\n' < /tmp/rh-$N.b64 | sha256sum | cut -d' ' -f1)"
  fold -w 900 /tmp/rh-$N.b64 | sed 's/^/@@/;s/$/##/'
  ;;
esac
