#!/bin/bash
# https://chat.auto-link.com.cn/model/ = cc.auto-link.com.cn/pro/ 的「换皮」入口：用户只看到 chat 域名。
#
#   scripts/chat-model-subpath/deploy.sh render              # 只打印要插的段
#   scripts/chat-model-subpath/deploy.sh apply               # 198 装上（备份 → 插段 → nginx -t → reload → verify）
#   KEY=sk-... scripts/chat-model-subpath/deploy.sh verify   # 外网验收（不给 KEY 就跳过要真 key 的几项）
#   scripts/host-198-chat-subpath.sh remove model            # 回滚（同一套标记段）
#
# 链路：用户 → IT 入口(58.241.5.230) → 198:80 chat server `/model/…`
#       → 198 本机 127.0.0.1:80，Host 改成 cc.auto-link.com.cn，路径改成 /pro/…
#       → cc server 原样处理（封 key 块 / 灰度分流 / responses WS 分流 都照旧生效）→ LiteLLM
# 🔴 为什么不直接 proxy_pass 30402：cc server 里的 key-block 403、灰度 split、ws_ingress 都在 nginx 层，绕过去被封的 key 就能用。
# 🔴 cc 的 conf 一个字不动（灰度 render 会整份重写它）；只往 chat conf 插 `# >>> chat-subpath:model >>>` 段。
# 🔴 cc 的 geo $gray_debug_source 信任 127.0.0.1 ⇒ 绕本机后外部请求也算「本机」，必须清掉 X-LLM-Debug，否则外人能拿到池标签。
# 🔴 只开 API：/model/ 根(swagger)、/model/ui 等管理页一律 404 —— 那些页面里写死了 /pro/ 路径。
set -uo pipefail
H198=${H198:-cltx@10.68.13.198}
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o ClearAllForwardings=yes "$H198")
SITE=chat.auto-link.com.cn
CONF=/etc/nginx/sites-enabled/chat.auto-link.com.cn.conf
NAME=model
BEGIN="# >>> chat-subpath:${NAME} >>>"
END="# <<< chat-subpath:${NAME} <<<"

die(){ echo "STOP: $*" >&2; exit "${2:-1}"; }
remote(){ "${SSH[@]}" "$@" 2> >(grep -v 'port forwarding' >&2); }

render(){
  cat <<EOF
    ${BEGIN} 127.0.0.1:80 Host=cc /pro/（$(date '+%F') ${OPERATOR:-$USER}，scripts/chat-model-subpath/deploy.sh）。回滚=host-198-chat-subpath.sh remove ${NAME}
    location = /${NAME} { absolute_redirect off; return 302 /${NAME}/; }
    location = /${NAME}/ { default_type text/plain; return 200 "ok\n"; }
    location ^~ /${NAME}/ {
        # 管理页/文档不对外（页面里写死 /pro/）
        if (\$uri ~ "^/${NAME}/(ui|_next|swagger|litellm-asset-prefix|openapi\\.json|docs|redoc)(/|\$)") { return 404; }
        proxy_pass http://127.0.0.1:80/pro/;
        proxy_set_header Host cc.auto-link.com.cn;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-LLM-Debug "";
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade_owui;
        # cc 侧若回跳转，/pro/… 一律改回 /${NAME}/…（相对/绝对两种都兜）
        proxy_redirect ~^(?:https?://cc\\.auto-link\\.com\\.cn)?/pro/(.*)\$ https://${SITE}/${NAME}/\$1;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_request_buffering off;
        # 与 cc 一致：body 不限、header buffer 512k（/v1/responses 头可达数十 KB）
        client_max_body_size 0;
        proxy_buffer_size       512k;
        proxy_buffers           16 512k;
        proxy_busy_buffers_size 1024k;
        # 比 cc 的 570s 略长：让里层先超时、回干净的 504
        proxy_connect_timeout 60s;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
    ${END}
EOF
}

apply(){
  local used; used=$(remote "sudo -n grep -nE 'location[^/]*/${NAME}(/| |\\{|\$)' ${CONF}")
  [ -z "$used" ] || die "chat conf 里已有 /${NAME}：${used}（要重装先 host-198-chat-subpath.sh remove ${NAME}）"
  echo "== 要插入的段"; render
  local b64; b64=$(render | base64 | tr -d '\n')
  echo "== 198 上安装"
  remote "CONF='${CONF}' NAME='${NAME}' B64='${b64}' bash -s" <<'REMOTE'
set -uo pipefail
S="sudo -n"; BK=/etc/nginx/backups; TS=$(date +%Y%m%dT%H%M%S)
fail(){ echo "STOP: $*"; exit "${2:-1}"; }
c=$(curl -s -o /dev/null -m 5 -w '%{http_code}' -H 'Host: cc.auto-link.com.cn' http://127.0.0.1/pro/health/liveliness)
[ "${c}" = 200 ] || fail "本机 cc /pro/health/liveliness 回 ${c}（应 200）"
echo "OK   本机 cc /pro 可达"
$S mkdir -p "${BK}"; BAK="${BK}/chat.auto-link.com.cn.conf.${TS}.pre-${NAME}"
$S cp -a "${CONF}" "${BAK}" && echo "OK   备份 ${BAK}"
echo "${B64}" | base64 -d > /tmp/chat-subpath-blk.$$
$S python3 - "${CONF}" /tmp/chat-subpath-blk.$$ <<'PY' || { rm -f /tmp/chat-subpath-blk.$$; fail "插入失败（锚点不唯一），未改动"; }
import sys
p, b = sys.argv[1], sys.argv[2]
s = open(p).read(); blk = open(b).read()
a = '    location / {\n        proxy_pass http://open_webui_upstream;'
assert s.count(a) == 1, f"anchor count={s.count(a)}"
open(p, 'w').write(s.replace(a, blk + a))
PY
rm -f /tmp/chat-subpath-blk.$$
if ! $S nginx -t 2>&1; then
  $S cp -a "${BAK}" "${CONF}"; fail "nginx -t 失败，已恢复备份，未 reload" 2
fi
$S nginx -s reload && echo "OK   nginx reload"
REMOTE
  local rc=$?
  [ $rc = 255 ] && die "ssh 失败" 4
  [ $rc = 0 ] || exit $rc
  verify
}

# 外网验收。每项打 OK/FAIL；最后汇总。KEY 没给就跳过要真 key 的几项（明确打 SKIP）
verify(){
  local B="https://${SITE}/${NAME}" fail=0 h b c i
  ok(){ echo "OK   $*"; }; bad(){ echo "FAIL $*"; fail=$((fail+1)); }
  echo "== 外网验收（等新 worker 生效，最多 60s）"
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    b=$(curl -s -m 15 "${B}/health/liveliness"); [ "$b" = '"I'"'"'m alive!"' ] && break; sleep 5
  done
  [ "$b" = '"I'"'"'m alive!"' ] && ok "/model/health/liveliness → ${b}" || bad "/model/health/liveliness → $(echo "$b" | head -c 80)"
  h=$(curl -s -o /dev/null -D - -m 15 "${B}" | tr -d '\r')
  echo "$h" | grep -q '^HTTP/[0-9.]* 302' && echo "$h" | grep -qi '^location: /model/$' && ok "/model → 302 /model/" || bad "/model 跳转不对：$(echo "$h" | grep -iE '^(HTTP|location)' | tr '\n' ' ')"
  for p in / /ui/ /swagger/ /openapi.json /docs; do
    c=$(curl -s -o /tmp/cms.$$ -w '%{http_code}' -m 15 "${B}${p}")
    if grep -qiE 'cc\.auto-link|/pro/' /tmp/cms.$$; then bad "${p} 正文里有 cc 或 /pro/"; else ok "${p} → ${c}（无 cc / /pro/）"; fi
  done
  c=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "https://${SITE}/api/version")
  [ "$c" = 200 ] && ok "对照 Open WebUI /api/version → 200" || bad "Open WebUI /api/version → ${c}"
  # 封 key 块必须仍生效（伪造一把命中 tail-ybLw 规则的 key；JWT 类）
  b=$(curl -s -m 15 -H 'Authorization: Bearer sk-zzzzzzzzzzzzzzzzybLw' "${B}/v1/models")
  echo "$b" | grep -q 'blocked: expired api key' && ok "封 key 块生效（伪造 ybLw 尾 key → blocked）" || bad "封 key 块没生效：$(echo "$b" | head -c 100)"
  b=$(curl -s -m 15 -H 'Authorization: Bearer eyJhbGciOiJub25lIn0.e30.x' "${B}/v1/models")
  echo "$b" | grep -q 'blocked: OAuth/JWT' && ok "JWT 拦截生效" || bad "JWT 拦截没生效：$(echo "$b" | head -c 100)"
  # 调试头不能被外部拿到池标签
  # 调试头的值不进 git：现场从 cc conf 里读
  local dbg; dbg=$(remote "sudo -n grep -oE '\"1:[0-9a-f]{64}\"' /etc/nginx/sites-enabled/cc.auto-link.com.cn.conf | head -1 | tr -d '\"' | cut -d: -f2")
  if [ -z "$dbg" ]; then echo "SKIP 调试头检查（cc conf 里没读到调试值）"; else
    h=$(curl -s -o /dev/null -D - -m 15 -H "X-LLM-Debug: ${dbg}" "${B}/v1/models" | tr -d '\r')
    echo "$h" | grep -qi '^x-llm-pool: .' && bad "外部带调试头拿到了 X-LLM-Pool" || ok "调试头已清（无 X-LLM-Pool）"
  fi

  if [ -z "${KEY:-}" ]; then echo "SKIP 真 key 各项（没给 KEY）"; else
    local M=${MODEL:-claude-haiku-5-5}
    leak(){ grep -qiE 'cc\.auto-link|/pro/' "$1" && bad "$2 响应里有 cc 或 /pro/" || ok "$2 响应头+正文无 cc / /pro/"; }
    c=$(curl -s -D /tmp/cmh.$$ -o /tmp/cmb.$$ -w '%{http_code}' -m 30 -H "Authorization: Bearer ${KEY}" "${B}/v1/models")
    [ "$c" = 200 ] && ok "/v1/models → 200（$(grep -o '"id"' /tmp/cmb.$$ | wc -l | tr -d ' ') 个模型）" || bad "/v1/models → ${c}"
    cat /tmp/cmh.$$ /tmp/cmb.$$ > /tmp/cma.$$; leak /tmp/cma.$$ "/v1/models"
    c=$(curl -s -D /tmp/cmh.$$ -o /tmp/cmb.$$ -w '%{http_code}' -m 120 -H "Authorization: Bearer ${KEY}" -H 'Content-Type: application/json' \
      -d "{\"model\":\"${M}\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"回一个字：好\"}]}" "${B}/v1/chat/completions")
    [ "$c" = 200 ] && grep -q '"content"' /tmp/cmb.$$ && ok "chat/completions → 200 有内容" || bad "chat/completions → ${c} $(head -c 150 /tmp/cmb.$$)"
    cat /tmp/cmh.$$ /tmp/cmb.$$ > /tmp/cma.$$; leak /tmp/cma.$$ "chat/completions"
    c=$(curl -s -N -D /tmp/cmh.$$ -o /tmp/cmb.$$ -w '%{http_code}' -m 120 -H "x-api-key: ${KEY}" -H 'anthropic-version: 2023-06-01' -H 'Content-Type: application/json' \
      -d "{\"model\":\"${M}\",\"max_tokens\":20,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"回一个字：好\"}]}" "${B}/v1/messages")
    [ "$c" = 200 ] && grep -q 'message_stop' /tmp/cmb.$$ && ok "messages 流式 → 200 到 message_stop" || bad "messages 流式 → ${c} $(head -c 150 /tmp/cmb.$$)"
    cat /tmp/cmh.$$ /tmp/cmb.$$ > /tmp/cma.$$; leak /tmp/cma.$$ "messages 流式"
    c=$(curl -s -N -D /tmp/cmh.$$ -o /tmp/cmb.$$ -w '%{http_code}' -m 120 -H "Authorization: Bearer ${KEY}" -H 'Content-Type: application/json' \
      -d "{\"model\":\"${M}\",\"stream\":true,\"input\":\"回一个字：好\"}" "${B}/v1/responses")
    [ "$c" = 200 ] && grep -q 'response.completed' /tmp/cmb.$$ && ok "responses 流式 → 200 到 response.completed" || bad "responses 流式 → ${c} $(head -c 150 /tmp/cmb.$$)"
    cat /tmp/cmh.$$ /tmp/cmb.$$ > /tmp/cma.$$; leak /tmp/cma.$$ "responses 流式"
  fi
  rm -f /tmp/cms.$$ /tmp/cmh.$$ /tmp/cmb.$$ /tmp/cma.$$
  [ $fail = 0 ] && echo "VERIFY PASS" || { echo "VERIFY FAIL ${fail} 项"; return 3; }
}

case "${1:-}" in
  render) render;;
  apply) apply;;
  verify) verify;;
  *) sed -n 2,8p "$0" | sed 's/^# \{0,1\}//'; exit 1;;
esac
