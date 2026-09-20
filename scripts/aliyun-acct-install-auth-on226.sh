#!/usr/bin/env bash
# install-auth.sh <N> — 在 226 上跑:从 oauth job 日志抽 base64 auth.json → 校验
# → kubectl cp 进 acct pod 的 /chatgpt-auth/auth.json → rollout → 流式 smoke。
# 内容全程不过 PTY(只在节点本地流转),避免 base64 丢字节。
set -uo pipefail
N="${1:?need acct number}"
NS=carher
LOG=/tmp/j$N.log
OUTJ=/tmp/auth-acct-$N.json

kubectl -n $NS logs job/cgpt-onboard-oauth-$N > $LOG 2>&1 || { echo "FAIL: 取不到 job 日志"; exit 1; }
grep -q B64END $LOG || { echo "FAIL: acct-$N 日志无 B64END —— OAuth 没产出 auth.json"; 
  echo "--- 末尾诊断 ---"; grep -iE 'deactivated|verification|error|FATAL|RESULT' $LOG | tail -8; exit 2; }

python3 - "$LOG" "$OUTJ" <<'PYEOF'
import sys,base64,json,re
log,out=sys.argv[1],sys.argv[2]
t=open(log,encoding='utf-8',errors='replace').read()
m=re.search(r'B64BEGIN(.*?)B64END',t,re.S)
if not m: print("FAIL: no marker"); sys.exit(3)
raw=re.sub(r'\s+','',m.group(1))
d=json.loads(base64.b64decode(raw))
at=d.get('access_token','') or (d.get('tokens') or {}).get('access_token','')
assert len(at)>1000, f"access_token too short: {len(at)}"
open(out,'w').write(json.dumps(d,indent=2))
idt=d.get('id_token','') or (d.get('tokens') or {}).get('id_token','')
p=idt.split('.')[1]; p+='='*(-len(p)%4)
c=json.loads(base64.urlsafe_b64decode(p)); a=c.get('https://api.openai.com/auth',{})
print("OK parsed: email",c.get('email'),"plan",a.get('chatgpt_plan_type'))
print("   sub_until",a.get('chatgpt_subscription_active_until'),"acctid",a.get('chatgpt_account_id'))
print("   access_len",len(at),"rt_len",len((d.get('refresh_token') or '')))
PYEOF
[ $? -eq 0 ] || { echo "FAIL: acct-$N auth.json 解析/校验不过"; exit 4; }

P=$(kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath='{.items[0].metadata.name}')
[ -n "$P" ] || { echo "FAIL: acct-$N 无 pod"; exit 5; }
kubectl -n $NS cp $OUTJ $NS/$P:/chatgpt-auth/auth.json -c litellm || { echo "FAIL: cp 失败"; exit 6; }
# 回查:pod 内实际落盘长度(不信 cp 的返回码)
GOT=$(kubectl -n $NS exec $P -c litellm -- python3 -c \
  "import json;print(len(json.load(open('/chatgpt-auth/auth.json')).get('access_token','')))" 2>/dev/null | tr -dc 0-9)
echo "  pod 内 access_len=$GOT"
[ "${GOT:-0}" -gt 1000 ] || { echo "FAIL: acct-$N 落盘校验不过"; exit 7; }
kubectl -n $NS rollout restart deploy/chatgpt-acct-$N
kubectl -n $NS rollout status deploy/chatgpt-acct-$N --timeout=240s 2>&1 | tail -2
echo "INSTALL_OK acct-$N"
