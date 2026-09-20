#!/usr/bin/env bash
# chatgpt-acct-renew-status.sh — ZERO-CHROMIUM 查 198 池内 ChatGPT acct 的**真实续订状态**。
#
# 为什么需要它:
#   token 的 JWT claims 只有 `chatgpt_plan_type`(plan) + `chatgpt_subscription_active_until`
#   (period-end)——**没有 auto-renew / cancel 状态**。只看 plan=pro + sub_until 判"不用续订"
#   是假绿(2026-09-01 acct-151~164 实证: 全 plan=chatgptpro 但 12/14 其实 will_renew=False
#   期末取消)。**续订 NEED 的唯一判据 = /accounts/check/v4 的 last_active_subscription.will_renew**。
#
#   本脚本用**池内 pod 的 live token** 直连 chatgpt.com/backend-api/accounts/check/v4 读这个字段,
#   全程零 chromium、零登录、几秒/号(dozens of pods ~16s)。既用来**判续订 NEED**(跑 billing
#   renew 之前先筛出真 will_renew=False 的号, 别对已 True 的号白烧 chromium), 也用来**验续订
#   RESULT**(billing 的 `[BILLING-RESULT] now_renews` / `✅ RENEW ENABLED` in-page 读**两个方向都
#   不可信** —— False 假阴 + True 假阳都实证过; 点完 Renew 必须回这里复查 will_renew 才算数)。
#
# 用法:
#   bash scripts/chatgpt-acct-renew-status.sh 151 152 153 ...      # 指定号
#   bash scripts/chatgpt-acct-renew-status.sh all                 # 池内全部 chatgpt-acct-*
#
# 输出每行: acct-N plan=<..> has_active=<..> will_renew=<True|False|None> active_until=<..>
#   will_renew=False  → 期末取消, 需 billing renew(见 skill chatgpt-sub-renew-eip)
#   will_renew=True   → 自动续订中, 无需动
#   CHECK_HTTP_403    → CF 边缘瞬拦(同 /v1/me 瞬时 404), **re-probe** 常翻真值(本脚本自动重试 3 次)
#   SHELL_TOKEN_EMPTY → pod 内 auth.json 无 access_token(空壳, 见 add-chatgpt-acct-198 §空壳兜底)
#
# ⚠️ transport 铁律(2026-09-01 实证): 别用嵌套 heredoc + `bash -s`(脚本与内层 heredoc 抢 stdin),
#   也别 `kubectl exec` 不加 `-i` 去喂 `python3 -`(stdin 空→静默无输出)。本脚本把 python 走
#   base64→`python3 -c` 传参, 完全不碰 stdin。
set -uo pipefail
ASSET="${KX_ASSET:-AIYJY-litellm}"       # 198 主节点(带 kubectl)
NS="${NS:-litellm-product}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
JMS="$REPO/scripts/jms"

[ "$#" -ge 1 ] || { echo "usage: $0 <N...> | all"; exit 1; }

read -r -d '' PYSRC <<'PY' || true
import json,sys,urllib.request,urllib.error
N=sys.argv[1]
H={"Originator":"codex_cli_rs","User-Agent":"codex_cli_rs/0.30.0 (Linux; x86_64)"}
def g(u,t,a,to=15):
    r=urllib.request.Request(u,headers=dict(H,**{"Authorization":"Bearer "+t,"ChatGPT-Account-ID":a}))
    with urllib.request.urlopen(r,timeout=to) as x: return json.loads(x.read().decode())
try:
    a=json.load(open("/chatgpt-auth/auth.json"))
    t=a.get("access_token") or ""; aid=a.get("account_id","")
    if not t: print("acct-%s SHELL_TOKEN_EMPTY"%N); sys.exit()
    try:
        chk=g("https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",t,aid)
    except urllib.error.HTTPError as e:
        print("acct-%s CHECK_HTTP_%s"%(N,e.code)); sys.exit()
    accts=chk.get("accounts") or {}
    node=accts.get(aid) or accts.get("default") or (next(iter(accts.values())) if accts else {})
    ent=node.get("entitlement") or {}
    las=node.get("last_active_subscription") or {}
    print("acct-%s plan=%s has_active=%s will_renew=%s active_until=%s"%(
        N, ent.get("subscription_plan"), ent.get("has_active_subscription"),
        las.get("will_renew"), las.get("active_until")))
except Exception as e:
    print("acct-%s ERR=%r"%(N,e))
PY
PYB64=$(printf '%s' "$PYSRC" | base64 | tr -d '\n')

# 远端脚本: 解 args → 逐号 kubectl exec (python via -c, 无 stdin) → CHECK_HTTP_403 自动重试
REMOTE=$(cat <<EOF
set -u
NS=$NS
PY=\$(printf '%s' '$PYB64' | base64 -d)
ARGS="$*"
if [ "\$ARGS" = "all" ]; then
  NUMS=\$(kubectl -n \$NS get deploy -o name 2>/dev/null | grep -oE 'chatgpt-acct-[0-9]+' | grep -oE '[0-9]+' | sort -n)
else
  NUMS="\$ARGS"
fi
for N in \$NUMS; do
  POD=\$(kubectl -n \$NS get pod -l app=chatgpt-acct-\$N -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [ -z "\$POD" ]; then echo "acct-\$N POD=none"; continue; fi
  for try in 1 2 3; do
    R=\$(kubectl -n \$NS exec "\$POD" -c litellm -- python3 -c "\$PY" "\$N" 2>/dev/null)
    echo "\$R"
    echo "\$R" | grep -qE 'will_renew=(True|False)|SHELL_TOKEN_EMPTY|POD=none' && break
    sleep 8
  done
done
echo RENEW_STATUS_DONE
EOF
)
"$JMS" ssh "$ASSET" "$REMOTE" 2>&1 | grep -vaE "^Warning|Permission denied" | grep -aE "^acct-|RENEW_STATUS_DONE"
