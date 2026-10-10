#!/bin/bash
# chatgpt-pool-198-add-acct.sh — 「acct-N 开通/续上 pro 了,加进 198 litellm 循环池」一条命令跑完。
#
#   P198='..' P188='..' ./scripts/chatgpt-pool-198-add-acct.sh [选项] <N> [N...]
#     --no-wait            跳过末尾 ≥6min 的 governor 跨轮复查(多号时只在最后一个号复查)
#     --take-from-aliyun   阿里云同号还在跑时:摘阿里云 prod+canary CM、rollout、scale=0 再加 198
#                          (不给这个选项则遇到阿里云在跑就红退,什么都不动)
#
# 多个号**串行**跑,一号失败不影响后面的号,末尾打汇总表。skill = chatgpt-pool-198-add-acct。
# 本机要:sshpass 通 198/188、kubectl 当前 context 是阿里云、lark-cli 能读密码表。
#
# 每个号的判决树(每段失败即红退并说清「动了什么/没动什么」):
#   0 预检     198 deploy/PVC 在;阿里云同号必须 0 副本(或 --take-from-aliyun 先摘)
#   1 现状     叶子实打 + live plan(wham/usage)。SERVES 且 pro ⇒ 不碰凭据不滚 pod,直接 5
#   2 refresh  pod 内 refresh grant(200 同轮写回)。200+pro ⇒ 用它;200+free ⇒ 订阅真没生效,停;401 ⇒ 3
#   3 OAuth    阿里云 EIP 节点 FORCE_OTP 烧 OAuth(凭据从飞书密码表按 acct 取、按邮箱核)
#   4 门禁     新 auth plan=pro 且 account_id == 198 PVC 原身份
#   5 回池     chatgpt-pool-198-rejoin.sh(换 PVC → pod → 叶子 → arms → state)
#   6 补腿     照参照号逐字段克隆缺的腿(chatgpt-pool-198-arms.py clone)
#   7 验收     每个生产 proxy 副本 unique arms == 参照号、无坏行
#   8 复查     ≥400s 后 state 仍 HEALTHY;并查入池后 SpendLogs 有没有真实流量
set -uo pipefail

NOWAIT=0; TAKE_ALI=0; ACCTS=()
for a in "$@"; do
  case "$a" in
    --no-wait) NOWAIT=1 ;;
    --take-from-aliyun) TAKE_ALI=1 ;;
    -*) echo "未知选项 $a"; exit 2 ;;
    *) [[ "$a" =~ ^[0-9]+$ ]] || { echo "号必须是裸号(207),不是 $a"; exit 2; }; ACCTS+=("$a") ;;
  esac
done
[ ${#ACCTS[@]} -gt 0 ] || { sed -n '4,8p' "$0"; exit 2; }
: "${P198:?env P198 required}"; : "${P188:?env P188 required}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NS=litellm-product
M198=10.68.13.198; M188=10.68.13.188
SHEET=FRVJsbGsTh9kWNtp7uycrPSYnzc; SHEET_ID=0MAGgd

s198() { sshpass -p "$P198" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 cltx@$M198 "$@"; }
s188() { sshpass -p "$P188" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 cltx@$M188 "$@"; }
kx()   { s198 "echo '$P198' | sudo -S sh -c '$1' 2>/dev/null"; }
# SQL 走 base64 落盘再 < 重定向:直接塞进 kx 的单引号里,SQL 自带的 '...' 会把引号塌掉
psql198() {
  # 文件名本地定死:`$$` 在 sudo sh -c 里是另一个 PID,读不到外层写的文件(10-07 实踩)
  local b f="/tmp/add-acct-q-$$-$RANDOM.sql"; b=$(printf '%s' "$1" | base64 | tr -d '\n')
  s198 "echo $b | base64 -d > $f; chmod 644 $f; echo '$P198' | sudo -S sh -c 'kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -tAF \" | \" < $f' 2>/dev/null; rm -f $f"
}
# 在 acct pod 里跑本目录下的一个 python 脚本(base64 直送,避开 heredoc/引号层)
inpod() {
  local pod="$1" f="$2" b; b=$(base64 < "$HERE/$f" | tr -d '\n')
  kx "kubectl -n $NS exec $pod -- python3 -c \"import base64;exec(compile(base64.b64decode(\\\"$b\\\"),\\\"p\\\",\\\"exec\\\"))\""
}
claims() { python3 -c "
import json,base64,sys
d=json.load(open(sys.argv[1])); p=d.get('id_token','').split('.')[1]; p+='='*(-len(p)%4)
c=json.loads(base64.urlsafe_b64decode(p)); a=c.get('https://api.openai.com/auth',{})
print(a.get('chatgpt_plan_type'), a.get('chatgpt_subscription_active_until'), a.get('chatgpt_account_id'), c.get('email'), len(d.get('access_token','')))
" "$1" 2>/dev/null; }
kv() { echo "$1" | grep -oE "(^| )$2=[^ ]*" | head -1 | cut -d= -f2; }   # 从 `k=v k=v` 行取值

running_pod() { kx "kubectl -n $NS get pod -l app=chatgpt-acct-$N --field-selector=status.phase=Running -o jsonpath={.items[0].metadata.name}" | tr -d ' \n'; }
leaf_probe() {  # $1=endpoint IP → SERVES / 失败原因
  local ep="$1" nonce out
  nonce=$(date +%s%N)
  out=$(s198 "curl -sS -m 150 -N http://$ep:4000/v1/responses -H 'Authorization: Bearer $AK' -H 'Content-Type: application/json' -d '{\"model\":\"chatgpt-gpt-5.6-terra\",\"stream\":true,\"input\":[{\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"say OK-$nonce and nothing else\"}]}]}'" 2>&1)
  if echo "$out" | grep -q 'response\.completed'; then echo SERVES
  else
    # 叶子把上游错误嵌在 message 字符串里(转义过的 \"code\": \"token_revoked\"),外层 code 只是 "401"
    echo "$out" | grep -oE 'token_[a-z_]+|refresh_token_[a-z_]+|usage_limit_reached|rate_limit[a-z_]*' | head -1 | grep . \
      || echo "$out" | grep -oE '"code": ?"[0-9a-z_]+"' | head -1 | tr -d ' "' | sed 's/code://' | grep . || echo 无响应
  fi
}

# 两把共用的 key 只取一次
POOLKEY=$(kx "kubectl -n $NS get secret litellm-secrets -o jsonpath={.data.LITELLM_MASTER_KEY}" | tr -d ' \n' | base64 -d)
AK=$(kx "kubectl -n $NS get secret chatgpt-pool-master-key -o jsonpath={.data.LITELLM_MASTER_KEY}" | tr -d ' \n' | base64 -d)
[ ${#POOLKEY} -eq 39 ] || { echo "!!!! 取不到 198 router master key(len=${#POOLKEY}),检查 ssh/sudo"; exit 1; }
[ ${#AK} -gt 20 ] || { echo "!!!! 取不到叶子 key chatgpt-pool-master-key"; exit 1; }
export POOLKEY
ARMS_B64=$(base64 < "$HERE/chatgpt-pool-198-arms.py" | tr -d '\n')
arms() { s198 "echo $ARMS_B64 | base64 -d > /tmp/arms-$N.py; K='$POOLKEY' AK='$AK' python3 /tmp/arms-$N.py $*; rc=\$?; rm -f /tmp/arms-$N.py; exit \$rc"; }
# ── 阿里云同号:摘 CM → rollout → 逐 pod 验 → scale=0 ─────────────────────
# 顺序不许换:先让 router 不再选它,再停 pod,否则 rollout 窗口里 router 会打空 svc。
# 10-06/07 226/228/229/209 四轮手工跑出来的做法,收成函数。
aliyun_take() {
  local ts d c cur old exp p sha n0 ok
  ts=$(date +%Y%m%d%H%M%S); d=/tmp/ali$N-$ts; ( umask 077; mkdir -p "$d" )
  say "  阿里云:备份 CM → $d"
  for c in litellm-config litellm-config-canary; do
    kubectl -n carher get cm $c -o yaml --request-timeout=30s > "$d/$c.cm.bak.yaml" || return 1
    kubectl -n carher get cm $c -o jsonpath='{.data.config\.yaml}' --request-timeout=30s > "$d/$c.yaml" || return 1
    if ! grep -qE "chatgpt-acct-${N}[/.]" "$d/$c.yaml"; then cp "$d/$c.yaml" "$d/$c.new.yaml"; continue; fi
    python3 "$HERE/aliyun-cm-drop-acct.py" "$d/$c.yaml" "$d/$c.new.yaml" "$N" >/dev/null 2>"$d/$c.drop.err" \
      || { cat "$d/$c.drop.err"; say "  drop-acct guard 拒绝($c),阿里云没动"; return 1; }
    [ "$(grep -cE "chatgpt-acct-${N}[/.]" "$d/$c.new.yaml")" = 0 ] || { say "  $c 摘完仍有引用,阿里云没动"; return 1; }
  done
  python3 - "$d" <<'PY' || { say "  有模型组会被清空,阿里云没动"; return 1; }
import yaml,sys,collections
d=sys.argv[1]
for c in ("litellm-config","litellm-config-canary"):
    o=collections.Counter(e["model_name"] for e in yaml.safe_load(open(f"{d}/{c}.yaml"))["model_list"])
    n=collections.Counter(e["model_name"] for e in yaml.safe_load(open(f"{d}/{c}.new.yaml"))["model_list"])
    empty=[g for g in o if n.get(g,0)==0]
    print("  %s %d -> %d 条 清空组=%s" % (c, sum(o.values()), sum(n.values()), empty))
    if empty: sys.exit(1)
PY
  ok=0
  for c in litellm-config litellm-config-canary; do
    cmp -s "$d/$c.yaml" "$d/$c.new.yaml" && continue
    # 防别的会话中途改:live 必须还等于刚备份那份
    cur=$(kubectl -n carher get cm $c -o jsonpath='{.data.config\.yaml}' --request-timeout=30s | shasum -a 256 | cut -c1-12)
    old=$(shasum -a 256 < "$d/$c.yaml" | cut -c1-12)
    [ "$cur" = "$old" ] || { say "  $c 在备份后被改过(cur=$cur bak=$old),阿里云没动"; return 1; }
    kubectl -n carher create cm $c --from-file=config.yaml="$d/$c.new.yaml" --dry-run=client -o yaml \
      | kubectl -n carher apply -f - --request-timeout=60s >/dev/null || return 1
    exp=$(shasum -a 256 < "$d/$c.new.yaml" | cut -c1-12)
    cur=$(kubectl -n carher get cm $c -o jsonpath='{.data.config\.yaml}' --request-timeout=30s | shasum -a 256 | cut -c1-12)
    [ "$cur" = "$exp" ] || { say "  $c apply 后 sha 不符(${cur}≠${exp});回滚: kubectl apply -f $d/$c.cm.bak.yaml"; return 1; }
    eval "EXP_${c//-/_}=$exp"; ok=1
  done
  if [ "$ok" = 1 ]; then
    kubectl -n carher rollout restart deploy/litellm-proxy deploy/litellm-proxy-canary --request-timeout=60s >/dev/null || return 1
    say "  阿里云 proxy rollout 中(hostPort 排水,常 ~5min;卡 free ports 是正常的,别回滚)"
    sleep 20
    for _ in $(seq 1 60); do
      [ "$(kubectl -n carher get deploy litellm-proxy litellm-proxy-canary -o jsonpath='{range .items[*]}{.status.updatedReplicas}/{.spec.replicas}/{.status.availableReplicas};{end}' --request-timeout=20s 2>/dev/null)" = "2/2/2;1/1/1;" ] \
        && [ "$(kubectl -n carher get pod --no-headers --request-timeout=20s 2>/dev/null | awk '/^litellm-proxy/{print $2}' | sort | uniq -c | tr -s ' ')" = " 3 1/1" ] && break
      sleep 15
    done
    for p in $(kubectl -n carher get pod --no-headers --request-timeout=30s | awk '/^litellm-proxy/ && $2=="1/1"{print $1}'); do
      case "$p" in litellm-proxy-canary-*) exp=${EXP_litellm_config_canary:-} ;; *) exp=${EXP_litellm_config:-} ;; esac
      read -r sha n0 <<<"$(kubectl -n carher exec "$p" -c litellm --request-timeout=90s -- python3 -c "
import os,json,hashlib,urllib.request
s=hashlib.sha256(open('/app/config.yaml','rb').read()).hexdigest()[:12]
k=os.environ.get('LITELLM_MASTER_KEY','')
d=json.loads(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:4000/model/info',headers={'Authorization':'Bearer '+k}),timeout=60).read())['data']
print(s, len({(e.get('model_info') or {}).get('id','') for e in d if str((e.get('model_info') or {}).get('id','')).startswith('chatgpt-acct-$N/')}))
" 2>/dev/null | tail -1)"
      say "  阿里云 $p sha=$sha arms($N)=$n0"
      { [ -z "$exp" ] || [ "$sha" = "$exp" ]; } && [ "$n0" = 0 ] || { say "  $p 没收敛,阿里云 deploy 没 scale"; return 1; }
    done
  fi
  kubectl -n carher scale deploy "chatgpt-acct-$N" --replicas=0 --request-timeout=60s >/dev/null || return 1
  for _ in $(seq 1 30); do
    [ "$(kubectl -n carher get pod -l app=chatgpt-acct-$N --field-selector=status.phase=Running --no-headers --request-timeout=30s 2>/dev/null | wc -l | tr -d ' ')" = 0 ] && break
    sleep 5
  done
  say "  阿里云 chatgpt-acct-$N 已摘路由 + scale=0(回滚: apply $d/*.cm.bak.yaml + rollout + scale=1)"
}
say()  { echo "[$N] $*"; }
fail() { echo "!!!! acct-$N $*"; RESULT="FAIL: $*"; return 1; }

# 读密码表 → 起 OAuth job → 取 auth → 门禁。成功时设 REJOIN_SRC
do_oauth() {
  say "3 OAuth —— 从飞书密码表取凭据"
  lark-cli sheets +csv-get --spreadsheet-token $SHEET --sheet-id $SHEET_ID --range A1:H400 --format csv > "$TMP/sheet.json" 2>/dev/null \
    || { fail "lark-cli 读密码表失败"; return 1; }
  python3 - "$TMP/sheet.json" "$N" "$OEMAIL" > "$TMP/creds.tsv" <<'PY' || { fail "密码表里找不到 acct-$N(或邮箱与 PVC 原身份不符)"; return 1; }
import json,csv,io,sys
d=json.load(open(sys.argv[1])); n=sys.argv[2]; want=sys.argv[3]
lines=[l.split('] ',1)[1] if l.startswith('[row=') else l for l in d['data']['annotated_csv'].splitlines()]
for r in csv.DictReader(io.StringIO('\n'.join(lines))):
    if r['acct'].strip()=='acct-'+n:
        e=r['邮箱'].strip()
        # join key 只能是邮箱:编号↔邮箱历史上漂过(173/174 记在 163/164 名下)
        if want not in ('None','') and e.lower()!=want.lower(): sys.exit(2)
        print('\t'.join([e, r['邮箱密码'].strip(), r['GPT密码'].strip(), r['2FA密钥'].strip()])); sys.exit(0)
sys.exit(1)
PY
  local EM MPW GPW TOTP ST NPLAN NUNTIL NACCT NEMAIL NALEN
  IFS=$'\t' read -r EM MPW GPW TOTP < "$TMP/creds.tsv"
  OAUTH_RAN=1
  say "  起 OAuth job($EM,≈6min)"
  FORCE_OTP=1 "$HERE/aliyun-eip-onboard.sh" oauth "$N" "$EM" "$MPW" "$GPW" ${TOTP:+"$TOTP"} > "$TMP/oauth.log" 2>&1 \
    || { grep -vF -e "$MPW" -e "$GPW" "$TMP/oauth.log" | tail -15; fail "OAuth job 没提交成功"; return 1; }
  unset MPW GPW TOTP; rm -f "$TMP/creds.tsv" "$TMP/sheet.json"
  for _ in $(seq 1 60); do
    ST=$(kubectl -n carher get job "cgpt-onboard-oauth-$N" -o jsonpath='{.status.succeeded}/{.status.failed}' 2>/dev/null)
    case "$ST" in 1/*|*/1) break;; esac; sleep 10
  done
  "$HERE/aliyun-onboard-workspace.sh" log "$N" oauth 2>/dev/null | grep -E 'MANUAL_PHONE|account_deactivated|RESULT=|FATAL' | tail -5
  [ "${ST%%/*}" = 1 ] || { fail "OAuth job 未成功(status=$ST)。日志: aliyun-onboard-workspace.sh log $N oauth;MANUAL_PHONE=手机墙(飞书「卡密」列有值会自动接码),account_deactivated=死号别重跑"; return 1; }
  ( cd "$TMP" && "$HERE/aliyun-onboard-workspace.sh" auth "$N" >/dev/null 2>&1 )
  mv -f "/tmp/auth-acct-$N.json" "$AUTH" 2>/dev/null || { fail "取 auth.json 失败(workspace.sh auth $N)"; return 1; }
  chmod 600 "$AUTH"
  read -r NPLAN NUNTIL NACCT NEMAIL NALEN <<<"$(claims "$AUTH")"
  say "4 新凭据 $NEMAIL plan=$NPLAN until=$NUNTIL acct=$NACCT access_len=$NALEN"
  [ "$NACCT" = "$ORIG_ACCT" ] || { fail "account_id $NACCT ≠ PVC 原身份 $ORIG_ACCT —— 装错会两个 pod 共用 RT 互相作废(198 没动)"; return 1; }
  [ "$NPLAN" = pro ] || { fail "新凭据 plan=$NPLAN —— 订阅没生效(198 没动)"; return 1; }
  [ "${NALEN:-0}" -gt 1000 ] || { fail "access_token 太短 ($NALEN)"; return 1; }
  REJOIN_SRC="$AUTH"
}
add_one() {
  local PVDIR ALI POD EP V L LP RG RGCODE RGPLAN REF PROXIES URLS M PREV PASS OUT SKIP_CRED=0
  REJOIN_SRC=""; OAUTH_RAN=0; RESULT=""; LIVE_FINAL="-"
  TMP=$(mktemp -d /tmp/add-acct-$N.XXXX); chmod 700 "$TMP"; AUTH="$TMP/auth-acct-$N.json"

  # ── 0 预检 ──
  say "0 预检"
  kx "kubectl -n $NS get deploy chatgpt-acct-$N -o name" | grep -q deploy \
    || { fail "198 没有 deploy chatgpt-acct-$N(全新号走 skill aliyun-chatgpt-acct-onboard)"; return 1; }
  PVDIR=$(kx "ls -d /Data/rancher/storage/*_${NS}_chatgpt-acct-$N-auth" | tail -1)
  [ -n "$PVDIR" ] || { fail "找不到 198 PVC 目录"; return 1; }
  ALI=$(kubectl -n carher get deploy "chatgpt-acct-$N" -o jsonpath='{.spec.replicas}' --request-timeout=30s 2>/dev/null || true)
  if [ -n "$ALI" ] && [ "$ALI" != 0 ]; then
    [ "$TAKE_ALI" = 1 ] || { fail "阿里云 carher/chatgpt-acct-$N replicas=$ALI —— 两处共用 RT 会互相作废。确认归 198 后加 --take-from-aliyun 重跑(没动任何东西)"; return 1; }
    aliyun_take || { fail "阿里云摘号没走完(见上);198 没动"; return 1; }
  fi
  s198 "echo '$P198' | sudo -S -p '' cat $PVDIR/auth.json" > "$TMP/orig.json" 2>/dev/null
  read -r _ _ ORIG_ACCT OEMAIL _ <<<"$(claims "$TMP/orig.json")"
  [ -n "${ORIG_ACCT:-}" ] && [ "$ORIG_ACCT" != None ] || { fail "读不到 PVC 原身份 account_id"; return 1; }
  say "  PVC 原身份 $OEMAIL acct=$ORIG_ACCT"

  # ── 1 现状:叶子 + live plan ──
  POD=$(running_pod)
  if [ -n "$POD" ]; then
    EP=$(kx "kubectl -n $NS get pod $POD -o jsonpath={.status.podIP}" | tr -d ' \n')
    V=$(leaf_probe "$EP")
    L=$(inpod "$POD" chatgpt-acct-live-status-inpod.py | grep '^LIVE ' | tail -1)
    LP=$(kv "$L" plan)
    say "1 叶子=$V  live: ${L#LIVE }"
    # 🔴 SERVES ≠ pro:free 号也能打通部分模型(10-07 acct-165 订阅已过期仍 8h 298 次 success)。
    # 只有「能打 且 live pro」才跳过凭据;在服务的号不滚 pod(重启可能永久打死它,acct-237)
    [ "$V" = SERVES ] && [ "$LP" = pro ] && SKIP_CRED=1
  else
    say "1 没有 Running pod,跳过叶子实打"
  fi

  if [ "$SKIP_CRED" = 0 ]; then
    # ── 2 refresh grant ──
    # 刚开通的号:手里的 AT 是开通前签的(plan=free),refresh 才签出 pro 的新 AT(206/208/224/162/178/196 都是这样)
    if [ -n "$POD" ]; then
      RG=$(inpod "$POD" chatgpt-acct-refresh-grant-inpod.py | grep '^RG ' | tail -1)
      say "2 refresh → ${RG#RG }"
    else
      RG=""; say "2 没有 Running pod,跳过 refresh(盘上 RT 不动,直接 OAuth)"
    fi
    RGCODE=$(kv "$RG" code); RGPLAN=$(kv "$RG" plan)
    if [ "$RGCODE" = 200 ]; then
      [ "$(kv "$RG" writeback)" = OK ] || { fail "refresh 200 但写回没确认 —— 人工查 pod 内 /chatgpt-auth/*.bak-rg-*"; return 1; }
      if [ "$RGPLAN" != pro ]; then
        # 拿新签的 AT 再读一次账户订阅,让红退信息带上「到底哪天过期、有没有在续」
        L=$(inpod "$POD" chatgpt-acct-live-status-inpod.py | grep '^LIVE ' | tail -1)
        fail "refresh 200 但 plan=$RGPLAN(新签 AT)—— OpenAI 侧订阅没生效:${L#LIVE }。确认付款的是 $OEMAIL;重烧 OAuth 也没用"
        return 1
      fi
      say "  凭据救回且 plan=pro,已写回盘;rejoin 不换 auth"
    else
      do_oauth || return 1
    fi
  fi

  # ── 5 回池 ──
  if [ "$SKIP_CRED" = 1 ]; then
    say "5 已 SERVES+pro:不滚 pod,只注册 arms + 置 state"
    s188 "set -a; source /home/cltx/.chatgpt-quota/env; set +a; python3 -c \"
import importlib.util,sys
spec=importlib.util.spec_from_file_location('qr','/home/cltx/quota-rebalance.py')
qr=importlib.util.module_from_spec(spec); sys.modules['qr']=qr; spec.loader.exec_module(qr)
print('RESUME_CREATED=', qr.resume_acct('acct-$N', {'port': 40$N, 'location': '198'}))
\"" 2>&1 | grep -E 'RESUME_CREATED|failed' | tail -1 | sed "s/^/[$N]   /"
    s188 "python3 -c \"
import json,pathlib,time
p=pathlib.Path('/home/cltx/.chatgpt-quota/state/state.json')
d=json.loads(p.read_text()); a=d.setdefault('acct-$N',{})
a.update({'tier':'HEALTHY','paused':False,'manual_offline':False,'consecutive_401':0,
          'consecutive_probe_err':0,'probe_err_alerted':False,'restore_at':0,'cause':None,'ts':int(time.time())})
p.write_text(json.dumps(d,indent=2,ensure_ascii=False)); print('STATE_OK')
\"" | grep -q STATE_OK || { fail "state.json 写入失败"; return 1; }
  else
    say "5 rejoin ${REJOIN_SRC:+(换 auth)}"
    P198="$P198" P188="$P188" "$HERE/chatgpt-pool-198-rejoin.sh" "$N" $REJOIN_SRC 2>&1 \
      | grep -vE 'router 中 .* unique arms' | sed "s/^/[$N]   /" | tee "$TMP/rejoin.log"
    grep -q 'DONE' "$TMP/rejoin.log" || { fail "rejoin 未走完(看上面 !!!! 那行)"; return 1; }
  fi
  STATE_TS=$(date +%s)

  # ── 6 补腿 ──
  REF=$(arms pick-ref "$N" | tr -d ' \n')
  [ -n "$REF" ] || { fail "选不出参照号"; return 1; }
  PROXIES=$(kx "kubectl -n $NS get endpoints litellm-proxy-nodeport -o jsonpath={.subsets[*].addresses[*].ip}")
  URLS=""; for ip in $PROXIES; do URLS="$URLS http://$ip:4000"; done
  [ -n "$URLS" ] || { fail "读不到生产 proxy 副本 IP"; return 1; }
  say "6 参照号 acct-$REF;生产 proxy 副本 $(echo $PROXIES | wc -w | tr -d ' ') 个"
  # 等 resume 刚建的腿传播到 NodePort 再算缺口(缺口数连续两次相同),否则会重建撞 500(10-06 acct-210)
  PREV=-1
  for _ in $(seq 1 8); do
    M=$(arms missing "$N" "$REF" http://127.0.0.1:30402 | wc -l | tr -d ' ')
    [ "$M" = "$PREV" ] && break; PREV=$M; sleep 15
  done
  arms clone "$N" "$REF" http://127.0.0.1:30402 --apply | grep -E '^clone|-> |CLONE=' | sed "s/^/[$N]   /"

  # ── 7 逐副本验收 ──
  say "7 逐副本验收"
  PASS=0
  for _ in $(seq 1 12); do
    OUT=$(arms audit "$N" "$REF" $URLS)
    echo "$OUT" | grep -q 'AUDIT=PASS' && { PASS=1; break; }; sleep 15
  done
  echo "$OUT" | sed "s/^/[$N]   /"
  [ "$PASS" = 1 ] || { fail "副本没收敛到参照号(上表 missing/bad);pod/state 已就位,只差腿"; return 1; }

  POD=$(running_pod)
  LIVE_FINAL=$(inpod "$POD" chatgpt-acct-live-status-inpod.py | grep '^LIVE ' | tail -1)
  RESULT="OK"
}

# ── 8 跨轮复查(多号时共用一次等待)──
# macOS 自带 bash 3.2:没有 declare -A / ${arr[-1]},每号结果用 R_<N>_* 动态变量存
setv() { eval "R_$1_$2=\$3"; }
getv() { eval "printf '%s' \"\${R_$1_$2:-}\""; }

recheck() {
  local n t since rows
  for n in "$@"; do
    t=$(s188 "python3 -c \"import json;a=json.load(open('/home/cltx/.chatgpt-quota/state/state.json')).get('acct-$n',{});print(a.get('tier'),a.get('paused'),a.get('manual_offline'))\"")
    since=$(date -u -r "$(getv "$n" joined)" '+%Y-%m-%d %H:%M:%S')
    rows=$(psql198 "select status||'='||count(*) from \"LiteLLM_SpendLogs\" where \"startTime\" >= '$since' and model_id like 'chatgpt-acct-$n-%' group by status" | tr '\n' ' ')
    echo "[$n] 8 state=$t  入池后流量: ${rows:-暂无(池里号多时几分钟才轮到,不算失败)}"
    [ "$t" = "HEALTHY False False" ] || setv "$n" status "FAIL: state 被 cron 翻回去了($t)—— 重跑本脚本即可(幂等)"
  done
}

OKS=""; LAST=0
for N in "${ACCTS[@]}"; do
  echo "================ acct-$N ================"
  add_one; rc=$?
  rm -rf "$TMP"
  if [ "${OAUTH_RAN:-0}" = 1 ]; then
    kubectl -n carher delete job "cgpt-onboard-oauth-$N" --ignore-not-found --request-timeout=30s >/dev/null 2>&1
    "$HERE/aliyun-onboard-workspace.sh" rm >/dev/null 2>&1
  fi
  setv "$N" status "${RESULT:-FAIL}"; setv "$N" live "${LIVE_FINAL#LIVE }"
  if [ $rc = 0 ]; then OKS="$OKS $N"; setv "$N" joined "$STATE_TS"; LAST=$STATE_TS; fi
done

if [ -n "$OKS" ]; then
  if [ "$NOWAIT" = 1 ]; then
    echo "8 --no-wait:跳过跨轮复查。⚠️ 6min 后自查 state.json 里$OKS 的 tier"
  else
    echo "8 等 governor 跑一轮(≥400s)再复查$OKS"
    while [ $(( $(date +%s) - LAST )) -lt 400 ]; do sleep 20; done
    # shellcheck disable=SC2086
    recheck $OKS
  fi
fi

echo "================ 汇总 ================"
ALLOK=1
for N in "${ACCTS[@]}"; do
  S=$(getv "$N" status)
  printf 'acct-%-4s %s\n' "$N" "$S"
  if [ "$S" = OK ]; then printf '          live: %s\n' "$(getv "$N" live)"; else ALLOK=0; fi
done
[ "$ALLOK" = 1 ]
