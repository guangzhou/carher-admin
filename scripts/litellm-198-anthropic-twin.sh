#!/usr/bin/env bash
# litellm-198-anthropic-twin.sh
#
# 给一批 copilot2api 后端的模型补一套 **anthropic 协议孪生 entry**,并把某把 key 的
# per-key aliases 指过去,让 Claude Code 这类走 /v1/messages 的客户端能正常落 SpendLogs。
#
# 背景(2026-09-02 实测定因):
#   Claude Code 打 /v1/messages(Anthropic Messages 协议)。若命中的 entry 声明成
#   `openai/claude-xxx`,LiteLLM 会把请求翻成 OpenAI 协议发给上游 —— 请求本身成功,
#   但 success handler 仍按 Anthropic 形状校验响应:
#     litellm_logging.py:3576  AnthropicResponse.model_validate(result)
#     → ValidationError: type/role/content/stop_reason/stop_sequence Field required
#     → "[Non-Blocking] LiteLLM.Success_Call Error"
#   Non-Blocking ⇒ 用户照常拿到答案,**只有 SpendLog 那一次写入被跳过**。
#   失败走的是另一条 failure hook,所以表里只剩 failure 行 —— 看起来像"成功的都没记"。
#
# 修法是**加法**(遵循「改名/换映射是加法不是替换」):
#   新建 `<model>-anthropic` entry,litellm_params.model = `anthropic/<model>`,
#   原来的 `openai/<model>` 一根不动(her 走的就是那条)。
#
# 用法:
#   ./scripts/litellm-198-anthropic-twin.sh snapshot <key_alias>     # 存当前 models+aliases
#   ./scripts/litellm-198-anthropic-twin.sh create-twins             # 建 8 条 anthropic entry
#   ./scripts/litellm-198-anthropic-twin.sh probe-twins              # 逐条实打 /v1/messages
#   ./scripts/litellm-198-anthropic-twin.sh repoint <key_alias>      # 改 key 的 models+aliases
#   ./scripts/litellm-198-anthropic-twin.sh verify <key_alias> [min] # 查 SpendLogs 回归
#   ./scripts/litellm-198-anthropic-twin.sh rollback <key_alias>     # aliases 摘掉 -anthropic 后缀
#
# 纪律:
#   - 只走管理 API(/model/new、/key/update)。api_base 在 DB 里是加密列,禁直接 SQL。
#   - `aliases` 是**整体替换**:先 GET /key/info 读旧值,合并后再写。
#   - `/key/update` 的字段名写错(如 model_aliases)会**返 200 但静默不生效** —— 每次写完
#     必须 GET 回读比对,不认 http code。
#   - 判据是用户真实会话的 SpendLogs 行(prompt_tokens 上万),不是本脚本的小探针。

set -euo pipefail

HOST="AIYJY-litellm"
NS="litellm-product"
# pod 内部监听 4000；30402 是宿主机上的 NodePort，pod 里打不通
PROXY="http://127.0.0.1:4000"
SNAPDIR="docs/litellm-key-snapshots"

# 需要补孪生的模型(= copilot2api 现有的 openai/ 那批)
MODELS=(
  claude-opus-5
  claude-opus-4.8
  claude-opus-4.8-fast
  claude-opus-4.7
  claude-sonnet-5
  claude-haiku-4.5
  claude-fable-5
  claude-fable-5.1
)

# 预先拼好 python 列表字面量。放在 remote "..." 里现算会丢引号(嵌套引号被外层吃掉)
MODELS_PY=$(printf "'%s'," "${MODELS[@]}")

# anthropic provider 自己拼 /v1/messages,api_base **不带 /v1**(与 openai/ 那批不同)
API_BASE="http://copilot2api-2.copilot2api.svc.cluster.local:7777"

# 注意: 这里的报错文案不能含 '}' —— 会提前闭合 ${...},sub 拿到的就不是子命令名了
sub="${1:?usage: see header - snapshot | create-twins | probe-twins | repoint | verify | rollback}"
shift || true

# 在 198 上跑一段 bash;$MK 是从 secret 取出的 master key
remote() {
  jms ssh "$HOST" "MK=\$(kubectl -n $NS get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d); $1"
}

# 在 db pod 里跑 SQL 文件(避开 jms ssh + kubectl exec + psql -c 的多层引号地狱)
run_sql() {
  # 注意: `local a="$1" b="$(f "$a")"` 里 b 的命令替换会在 a 赋值**之前**求值
  # (bash 先对整行做词展开),配合 set -u 直接炸 "unbound variable"。必须拆两行。
  local f="$1"
  local rp="/tmp/$(basename "$f")"
  jms scp "$f" "$HOST:$rp" >/dev/null
  jms ssh "$HOST" \
    "kubectl cp $rp $NS/litellm-db-0:$rp \
     && kubectl exec -n $NS litellm-db-0 -- bash -c \
       'PGPASSWORD=\$POSTGRES_PASSWORD psql -U \$POSTGRES_USER \$POSTGRES_DB -f $rp' \
     ; rm -f $rp"
}

case "$sub" in

# ── 1. 先存快照。改动前必跑,不然回滚只能靠推导 ──────────────────────────────
snapshot)
  ka="${1:?need key_alias}"
  mkdir -p "$SNAPDIR"
  out="$SNAPDIR/${ka}-$(date +%Y%m%d-%H%M%S).json"
  remote "kubectl -n $NS exec deploy/litellm-proxy -- env MK=\$MK python3 -c \"
import os,json,urllib.request
mk=os.environ['MK']
H={'Authorization':'Bearer '+mk}
def g(p):
    return json.load(urllib.request.urlopen(urllib.request.Request('$PROXY'+p,headers=H)))
# /key/info 只认 key/token,不认 key_alias —— 先用 /key/list 把 alias 解析成 token
tok=g('/key/list?key_alias=$ka&return_full_object=true&size=5')['keys'][0]['token']
d=g('/key/info?key='+tok)['info']
print(json.dumps({'key_alias':'$ka','token':tok,
                  'models':d.get('models'),'aliases':d.get('aliases')},
                 ensure_ascii=False,indent=2))\"" 2>/dev/null | grep -v '^\[sitecustomize\]' > "$out"
  echo "snapshot -> $out"
  head -5 "$out"
  ;;

# ── 2. 建孪生 entry。幂等: 先读现存 model_name 跳过已有 ─────────────────────
#    ⚠️ /model/new 撞唯一约束时只回一句通用的 "Failed to add model to db",
#       看不出是重复还是别的错 —— 真因只在 proxy 日志里 (UniqueViolationError:
#       Unique constraint failed on the fields: (model_id))。所以必须自己先查重,
#       不能靠解析错误文案。
create-twins)
  remote "kubectl -n $NS exec deploy/litellm-proxy -- env MK=\$MK python3 -c \"
import os,json,urllib.request,urllib.error
mk=os.environ['MK']; H={'Authorization':'Bearer '+mk,'Content-Type':'application/json'}
MODELS=[$MODELS_PY]
def api(p,body=None):
    return json.load(urllib.request.urlopen(urllib.request.Request('$PROXY'+p,
        data=json.dumps(body).encode() if body else None, headers=H)))
have={d['model_name'] for d in api('/model/info')['data']}
for m in MODELS:
    n=m+'-anthropic'
    if n in have:
        print('%-32s SKIP already exists'%n); continue
    body={'model_name':n,
          'litellm_params':{'model':'anthropic/'+m,'api_base':'$API_BASE',
                            'api_key':'dummy','timeout':300},
          'model_info':{'id':'copilot2api-2/anthropic/'+m,'base_model':m,'mode':'chat'}}
    try:
        api('/model/new',body); print('%-32s created'%n)
    except urllib.error.HTTPError as e:
        print('%-32s http=%s %s'%(n,e.code,e.read().decode()[:160]))\""
  echo
  echo '↓ 落库确认(唯一判据,别信上面的 http code)'
  cat > /tmp/twins_check.sql <<'SQL'
\pset format unaligned
\pset fieldsep ' | '
SELECT model_id, model_name FROM "LiteLLM_ProxyModelTable"
 WHERE model_name LIKE '%-anthropic' ORDER BY model_name;
SQL
  run_sql /tmp/twins_check.sql
  ;;

# ── 3. 逐条实打。形状必须是 message/assistant/end_turn,http 200 不够 ─────────
probe-twins)
  for m in "${MODELS[@]}"; do
    remote "kubectl -n $NS exec deploy/litellm-proxy -- env MK=\$MK python3 -c \"
import os,json,urllib.request,urllib.error
mk=os.environ['MK']
body={'model':'$m-anthropic','max_tokens':16,
      'messages':[{'role':'user','content':'say ok'}]}
req=urllib.request.Request('$PROXY/v1/messages',data=json.dumps(body).encode(),
    headers={'Authorization':'Bearer '+mk,'Content-Type':'application/json'})
try:
    d=json.load(urllib.request.urlopen(req))
    print('$m-anthropic -> http=200 shape=%s %s %s'%(d.get('type'),d.get('role'),d.get('stop_reason')))
except urllib.error.HTTPError as e:
    print('$m-anthropic -> http=%s %s'%(e.code,e.read().decode()[:200]))\""
  done
  ;;

# ── 4. 改 key。models 先加(不然 allowlist 401),aliases 整体替换 ─────────────
repoint)
  ka="${1:?need key_alias}"
  remote "kubectl -n $NS exec deploy/litellm-proxy -- env MK=\$MK python3 -c \"
import os,json,urllib.request
mk=os.environ['MK']; H={'Authorization':'Bearer '+mk,'Content-Type':'application/json'}
TW=[$MODELS_PY]

def api(p,body=None):
    req=urllib.request.Request('$PROXY'+p,
        data=json.dumps(body).encode() if body else None, headers=H)
    return json.load(urllib.request.urlopen(req))

tok=api('/key/list?key_alias=$ka&return_full_object=true&size=5')['keys'][0]['token']
info=api('/key/info?key='+tok)['info']   # 注意: info 里没有 token 字段,token 只能从 /key/list 拿
aliases=dict(info.get('aliases') or {})
models=list(info.get('models') or [])
for m in TW:
    n=m+'-anthropic'
    if n not in models: models.append(n)

# aliases: 凡是当前指向裸 <model> 的键,统统改指 <model>-anthropic
changed=0
for k,v in list(aliases.items()):
    if v in TW:
        aliases[k]=v+'-anthropic'; changed+=1
# Claude Code 发的 anthropic.<dashed> 形态若还没有键,补上
for m in TW:
    k='anthropic.'+m.replace('.','-')
    if k not in aliases:
        aliases[k]=m+'-anthropic'; changed+=1

api('/key/update',{'key':tok,'models':models})
api('/key/update',{'key':tok,'aliases':aliases})   # ⚠️ 字段名是 aliases,不是 model_aliases

# 写后必回读。字段名写错时上面两发都返 200,只有这里能发现没生效
back=api('/key/info?key='+tok)['info'].get('aliases') or {}
bad=[k for k,v in aliases.items() if back.get(k)!=v]
print('changed=%d total=%d readback_mismatch=%d'%(changed,len(aliases),len(bad)))
for k in sorted(aliases):
    if k.startswith('anthropic.'): print('  ',k,'->',back.get(k))
if bad: raise SystemExit('READBACK FAILED: '+','.join(bad[:5]))\""
  ;;

# ── 5. 回归。判据 = 真实会话行(prompt_tokens 上万),不是探针的 11 tok ────────
verify)
  ka="${1:?need key_alias}"; mins="${2:-25}"
  cat > /tmp/twin_verify.sql <<SQL
\\pset format unaligned
\\pset fieldsep ' | '
SELECT to_char("startTime"+INTERVAL '8 hours','HH24:MI:SS') t, model, call_type, status,
       prompt_tokens, completion_tokens,
       COALESCE(proxy_server_request->>'max_tokens','-') mt
  FROM "LiteLLM_SpendLogs"
 WHERE metadata->>'user_api_key_alias'='$ka'
   AND "startTime" >= NOW()-INTERVAL '$mins minutes'
 ORDER BY "startTime" DESC LIMIT 25;
SQL
  run_sql /tmp/twin_verify.sql
  cat <<'EOF'

判读:
  绿 = model 形如 anthropic/claude-*, call_type=anthropic_messages, status=success,
       且 prompt_tokens 上万(真实会话带全上下文)。
  假绿 = 只有 prompt_tokens 十几的行 —— 那是探针,证明不了客户端已生效。
  未修 = call_type 空 + status=failure 独占(成功轮次被 Non-Blocking 异常吞掉)。
EOF
  ;;

# ── 6. 回滚。只摘 -anthropic 后缀,entry 留着(没人指就没流量) ─────────────────
rollback)
  ka="${1:?need key_alias}"
  remote "kubectl -n $NS exec deploy/litellm-proxy -- env MK=\$MK python3 -c \"
import os,json,urllib.request
mk=os.environ['MK']; H={'Authorization':'Bearer '+mk,'Content-Type':'application/json'}
def api(p,body=None):
    req=urllib.request.Request('$PROXY'+p,
        data=json.dumps(body).encode() if body else None, headers=H)
    return json.load(urllib.request.urlopen(req))
tok=api('/key/list?key_alias=$ka&return_full_object=true&size=5')['keys'][0]['token']
info=api('/key/info?key='+tok)['info']   # info 里没有 token 字段
aliases=dict(info.get('aliases') or {}); n=0
for k,v in list(aliases.items()):
    if isinstance(v,str) and v.endswith('-anthropic'):
        aliases[k]=v[:-len('-anthropic')]; n+=1
api('/key/update',{'key':tok,'aliases':aliases})
back=api('/key/info?key='+tok)['info'].get('aliases') or {}
left=[k for k,v in back.items() if isinstance(v,str) and v.endswith('-anthropic')]
print('reverted=%d remaining_anthropic=%d'%(n,len(left)))\""
  echo '注意: 回滚只退 alias 指向。孪生 entry 保留(留着无害);models allowlist 也保留。'
  ;;

*) echo "unknown subcommand: $sub" >&2; exit 2 ;;
esac
