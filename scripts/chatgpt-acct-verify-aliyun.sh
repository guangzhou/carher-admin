#!/usr/bin/env bash
# chatgpt-acct-verify-aliyun.sh — 阿里云 carher ns 里逐个 acct 的**独立验收**
#
# 为什么要这个脚本（不能只看 grinder 的 "GRINDER DONE ok=..."）:
#   grinder 的 DONE 只证明「pod 起来了 + auth.json 落盘了 + 非流式 HTTP 200」。
#   memory feedback_grinder_done_ok_is_false_positive_verify_pod_and_stream:
#   HTTP 200 可以是空 output，ready 1/1 也可以是死号。四件事必须逐个自证:
#     ident  这个编号装的到底是哪个邮箱（编号↔邮箱在飞书/188/PVC 三处都漂过，
#            唯一权威 = PVC 内 auth.json 的 id_token）
#     renew  live will_renew（**续订的唯一判据**，见 skill chatgpt-sub-renew-eip §0）
#     smoke  流式 /v1/responses 且断言 chars>0 && response.completed
#     image  必须 == 参照 acct 的 digest（CLAUDE.md ACR VPC + acct-82 digest 铁律）
#
# 用法:
#   ./scripts/chatgpt-acct-verify-aliyun.sh 209 211 212 213
#   ./scripts/chatgpt-acct-verify-aliyun.sh --all           # 池内所有 acct
#   VERIFY_NO_SMOKE=1 ...                                   # 只看 ident/renew/image（省额度）
#   EXPECT_209=isabella.trantow@mail.com ./... 209          # 断言邮箱（不符标 MISMATCH）
#
# ⚠ 探针会花掉被测号的真实额度（每号一次极小推理）。别拿它当 liveness 轮询。
set -uo pipefail

NS=carher
REF_ACCT="${VERIFY_REF_ACCT:-226}"
PROBE="$(cd "$(dirname "$0")" && pwd)/chatgpt-acct-verify-probe.py"
[ -s "$PROBE" ] || { echo "FATAL: 找不到探针 $PROBE"; exit 1; }
PROBE_B64=$(python3 -c "
import base64, pathlib
print(base64.b64encode(pathlib.Path('$PROBE').read_bytes()).decode())")

if [ "${1:-}" = "--all" ]; then
  ACCTS=$(kubectl -n $NS get deploy -l pool=chatgpt-acct \
    -o jsonpath='{range .items[*]}{.metadata.labels.account}{"\n"}{end}' | sort -n | tr '\n' ' ')
else
  ACCTS="$*"
fi
[ -n "${ACCTS// /}" ] || { echo "usage: $0 <N...> | --all"; exit 1; }

REF_IMG=$(kubectl -n $NS get deploy chatgpt-acct-$REF_ACCT \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
echo "# 参照镜像 (acct-$REF_ACCT): ${REF_IMG:-<取不到>}"
printf '%-6s %-34s %-8s %-6s %-10s %-9s %-6s %s\n' \
  ACCT EMAIL PLAN RENEW UNTIL SMOKE 7D% IMAGE
echo "--------------------------------------------------------------------------------------------------"

NEED_RENEW=""; BAD=""
for N in $ACCTS; do
  POD=$(kubectl -n $NS get pod -l app=chatgpt-acct-$N \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  IMG=$(kubectl -n $NS get deploy chatgpt-acct-$N \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
  IMGV=$([ -n "$IMG" ] && { [ "$IMG" = "$REF_IMG" ] && echo same || echo DRIFT; } || echo "-")
  if [ -z "$POD" ]; then
    printf '%-6s %-34s %-8s %-6s %-10s %-9s %-6s %s\n' "$N" "(no Running pod)" - - - - - "$IMGV"
    BAD="$BAD $N"; continue
  fi
  # ⚠ 探针**必须 base64 下发**: 它的 docstring 里有反引号和 `$`, 走未加引号的
  # heredoc 会被 shell 当命令替换执行 —— 那是静默改写脚本内容, 不是报错。
  # 同理见 memory feedback_jms_ssh_multiline_python_needs_heredoc_not_dash_c。
  J=$(kubectl -n $NS exec "$POD" -c litellm -- env \
        SMOKE_MODEL="${SMOKE_MODEL:-chatgpt-gpt-5.5}" \
        SMOKE_SKIP="${VERIFY_NO_SMOKE:-0}" \
        python3 -c "import base64;exec(compile(base64.b64decode('$PROBE_B64'),'probe','exec'))" \
        2>/dev/null | grep -o 'PROBE_JSON .*')
  J="${J#PROBE_JSON }"
  EXPECT_VAR="EXPECT_$N"; EXPECT="${!EXPECT_VAR:-}"
  read -r EMAIL PLAN RENEW UNTIL SMOKE PCT ERRS < <(EXPECT="$EXPECT" python3 -c '
import json,os,sys
try: d=json.loads(sys.stdin.read())
except Exception: print("PROBE_BROKEN - - - FAIL - probe_no_json"); raise SystemExit
i,r,s,u=d.get("ident",{}),d.get("renew",{}),d.get("smoke",{}),d.get("usage",{})
em=i.get("email") or "?"
exp=os.environ.get("EXPECT","")
if exp and em!=exp: em=f"MISMATCH:{em}"
wr=r.get("will_renew")
errs=[]
if r.get("http")!=200: errs.append("renew_http=%s"%r.get("http"))
# will_renew 读不出来 = **量具坏了**（响应换了形状），不是"这号没订阅"。
# 必须显式报错，绝不能让它安静地渲染成 "?" 被当成缺数据略过。
elif wr is None: errs.append("renew_unreadable:%s"%(r.get("err") or "will_renew=null"))
if s.get("verdict") not in ("PASS","SKIP"): errs.append("smoke=%s"%(s.get("err") or s.get("stream_err") or s.get("http")))
pct=u.get("primary_used_percent")
print(em, r.get("plan") or i.get("token_plan") or "?",
      {True:"YES",False:"NO",None:"?"}[wr] if wr in (True,False,None) else wr,
      (r.get("active_until") or i.get("token_until") or "?")[:10],
      "%s/%s"%(s.get("verdict","?"),s.get("chars","?")),
      "?" if pct is None else pct,
      ",".join(errs) or "-")
' <<<"$J")
  printf '%-6s %-34s %-8s %-6s %-10s %-9s %-6s %s\n' \
    "$N" "$EMAIL" "$PLAN" "$RENEW" "$UNTIL" "$SMOKE" "$PCT" "$IMGV"
  [ "$ERRS" != "-" ] && echo "         ↳ $ERRS"
  [ "$RENEW" = "NO" ] && NEED_RENEW="$NEED_RENEW $N"
  case "$EMAIL$SMOKE$IMGV$ERRS" in *MISMATCH*|*FAIL*|*DRIFT*|*renew_*) BAD="$BAD $N";; esac
done

echo "--------------------------------------------------------------------------------------------------"
echo "NEED_RENEW=${NEED_RENEW:-（none）}   （唯一判据 = 上面 RENEW 列的 live will_renew）"
echo "BAD=${BAD:-（none）}                 （身份不符 / 流式没出字 / 镜像漂移）"
[ -z "$BAD" ] || exit 3
