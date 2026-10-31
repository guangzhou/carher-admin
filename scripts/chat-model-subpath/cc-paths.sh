#!/bin/bash
# 把 cc.auto-link.com.cn 上的 4 个对外路径原样挂到 chat.auto-link.com.cn（路径名不变，客户端只换域名）：
#   /muse-media/…            muse-image 生图文件（只 GET/HEAD，id 32 位 hex）
#   /gemini-media/openai/v1/media/<32hex>   gemini 聊天出图副本
#   /ccdl/                   一键配置下载用的静态二进制（只读）
#   /zkd/                    zk-delta 增量终结点（Cursor 本机小代理用；流式、长超时、body 不限）
#
#   scripts/chat-model-subpath/cc-paths.sh render              # 只打印要插的 4 段
#   scripts/chat-model-subpath/cc-paths.sh apply               # 198 装上（一次备份 → 插 4 段 → nginx -t → reload → verify）
#   KEY=sk-... scripts/chat-model-subpath/cc-paths.sh verify   # 外网验收（不给 KEY 就跳过 zkd 真 envelope；MODEL= 可换模型）
#   scripts/host-198-chat-subpath.sh remove <name>             # 回滚单段（name = muse-media / gemini-media / ccdl / zkd）
#
# 链路（与 /model 同款）：chat server 命中段 → 198 本机 127.0.0.1:80，Host 改成 cc.auto-link.com.cn
#       → cc server 原样处理（封 key 块 / limit_except / alias 全照旧）
# 🔴 cc 的 conf 一个字不动（灰度 render 会整份重写它）；只往 chat conf 插 `# >>> chat-subpath:<name> >>>` 段。
# 🔴 proxy_pass 一律带 URI（用 nginx 规范化后的路径/正则捕获重拼），不把客户端原始 URI 原样甩给 cc：
#    原始 URI 里的 %2e%2e 之类到 cc 那边再规范化一次可能落进 /pro/ 等别的 location。
# 🔴 正则 location 只在 muse-media / gemini-media 两段，锚在各自前缀上，不会压到 /oneclick /model 等已有段。
set -uo pipefail
H198=${H198:-cltx@10.68.13.198}
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o ClearAllForwardings=yes "$H198")
SITE=chat.auto-link.com.cn
CONF=/etc/nginx/sites-enabled/chat.auto-link.com.cn.conf
NAMES="muse-media gemini-media ccdl zkd"

die(){ echo "STOP: $*" >&2; exit "${2:-1}"; }
remote(){ "${SSH[@]}" "$@" 2> >(grep -v 'port forwarding' >&2); }

hdr(){ echo "    # >>> chat-subpath:${1} >>> 127.0.0.1:80 Host=cc 同路径（$(date '+%F') ${OPERATOR:-$USER}，scripts/chat-model-subpath/cc-paths.sh）。回滚=host-198-chat-subpath.sh remove ${1}"; }
ftr(){ echo "    # <<< chat-subpath:${1} <<<"; }

render(){
  hdr muse-media
  cat <<'EOF'
    location ~ "^/muse-media/(?:v1/media/)?([0-9a-f]{32}\.(?:webp|png|jpg|jpeg|mp4))$" {
        limit_except GET HEAD { deny all; }
        proxy_pass http://127.0.0.1:80/muse-media/v1/media/$1;
        proxy_set_header Host cc.auto-link.com.cn;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }
EOF
  ftr muse-media
  hdr gemini-media
  cat <<'EOF'
    location ~ "^/gemini-media/openai/v1/media/([0-9a-f]{32})$" {
        limit_except GET HEAD { deny all; }
        proxy_pass http://127.0.0.1:80/gemini-media/openai/v1/media/$1;
        proxy_set_header Host cc.auto-link.com.cn;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }
EOF
  ftr gemini-media
  hdr ccdl
  cat <<'EOF'
    location ^~ /ccdl/ {
        limit_except GET HEAD { deny all; }
        proxy_pass http://127.0.0.1:80/ccdl/;
        proxy_set_header Host cc.auto-link.com.cn;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        # 大文件（Cursor/Codex 安装包上百 MB）：流式透传，不落临时文件
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
EOF
  ftr ccdl
  hdr zkd
  cat <<'EOF'
    location ^~ /zkd/ {
        proxy_pass http://127.0.0.1:80/zkd/;
        proxy_set_header Host cc.auto-link.com.cn;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-LLM-Debug "";
        proxy_set_header Connection "";
        proxy_http_version 1.1;
        # 与 cc server 级一致：流式、body 不限、header buffer 512k
        proxy_buffering off;
        proxy_request_buffering off;
        client_max_body_size 0;
        proxy_buffer_size       512k;
        proxy_buffers           16 512k;
        proxy_busy_buffers_size 1024k;
        # 比 cc 的 600s 略长：让里层先超时、回干净的 504
        proxy_connect_timeout 60s;
        proxy_read_timeout 610s;
        proxy_send_timeout 610s;
    }
EOF
  ftr zkd
}

apply(){
  local n used
  for n in $NAMES; do
    used=$(remote "sudo -n grep -nE 'chat-subpath:${n} |location[^/]*/${n}(/| |\\{|\$)' ${CONF}")
    [ -z "$used" ] || die "chat conf 里已有 /${n}：${used}（要重装先 host-198-chat-subpath.sh remove ${n}）"
  done
  echo "OK   chat conf 里没有 ${NAMES}"
  echo "== 要插入的段"; render
  local b64; b64=$(render | base64 | tr -d '\n')
  echo "== 198 上安装"
  remote "CONF='${CONF}' B64='${b64}' bash -s" <<'REMOTE'
set -uo pipefail
S="sudo -n"; BK=/etc/nginx/backups; TS=$(date +%Y%m%dT%H%M%S)
fail(){ echo "STOP: $*"; exit "${2:-1}"; }
cc(){ curl -s -m 10 -H 'Host: cc.auto-link.com.cn' "$@"; }
z=0123456789abcdef0123456789abcdef
cc "http://127.0.0.1/zkd/healthz" | grep -q '"ok":true' || fail "本机 cc /zkd/healthz 不通"
c=$(cc -o /dev/null -w '%{http_code}' http://127.0.0.1/ccdl/apps/versions.json); [ "${c}" = 200 ] || fail "本机 cc /ccdl/apps/versions.json 回 ${c}"
cc "http://127.0.0.1/muse-media/v1/media/${z}.webp" | grep -q not_found_error || fail "本机 cc /muse-media 随机 id 没回 muse2api 的 404"
cc "http://127.0.0.1/gemini-media/openai/v1/media/${z}" | grep -q 'expired or unknown' || fail "本机 cc /gemini-media 随机 id 没回 gemini-media 的 404"
echo "OK   本机 cc 四个路径都可达"
$S mkdir -p "${BK}"; BAK="${BK}/chat.auto-link.com.cn.conf.${TS}.pre-cc-paths"
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

# 外网验收。每项打 OK/FAIL；判据是返回内容是对应服务的，不是 Open WebUI 首页（OWUI 对任何路径回 200+首页）
verify(){
  local C="https://${SITE}" fail=0 b c i t z=0123456789abcdef0123456789abcdef
  local W=${WORK:-.work/chat-subpath-media}; mkdir -p "$W"; t="$W/verify.$$"
  ok(){ echo "OK   $*"; }; bad(){ echo "FAIL $*"; fail=$((fail+1)); }
  echo "== 外网验收（等新 worker 生效，最多 60s）"
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    b=$(curl -s -m 15 "${C}/zkd/healthz"); echo "$b" | grep -q '"ok":true' && break; sleep 5
  done
  echo "$b" | grep -q '"ok":true' && ok "/zkd/healthz → ${b}" || bad "/zkd/healthz → $(echo "$b" | head -c 80)"
  b=$(curl -s -m 15 "${C}/zkd/metrics.json"); echo "$b" | grep -q '"req_total"' && ok "/zkd/metrics.json 有 req_total" || bad "/zkd/metrics.json → $(echo "$b" | head -c 80)"
  # 封 key 块要照旧生效（伪造一把 ybLw 尾 key）
  b=$(curl -s -m 15 -H 'Authorization: Bearer sk-zzzzzzzzzzzzzzzzybLw' -H 'Content-Type: application/json' -d '{}' "${C}/zkd/v1/delta")
  echo "$b" | grep -q 'blocked: expired api key' && ok "/zkd 封 key 块生效（伪造 ybLw 尾 key → blocked）" || bad "/zkd 封 key 块没生效：$(echo "$b" | head -c 100)"
  # 协议面：版本不对的 envelope 必须是 zk-delta 自己回的 409
  b=$(curl -s -m 15 -w ' %{http_code}' -H 'Content-Type: application/json' -d '{"v":0}' "${C}/zkd/v1/delta")
  echo "$b" | grep -q 'proto_version_mismatch.* 409$' && ok "/zkd/v1/delta 坏 envelope → zk-delta 409 proto_version_mismatch" || bad "/zkd/v1/delta → $(echo "$b" | head -c 120)"
  if [ -z "${KEY:-}" ]; then echo "SKIP /zkd 真 envelope（没给 KEY）"; else
    # 真全量首轮 envelope（与 sidecar sendFull 同形），流式；zk-delta 重建后打 LiteLLM，要流到 [DONE] 且回 x-zk-handle
    local env; env=$(MODEL="${MODEL:-claude-haiku-5-5}" python3 - <<'PY'
import json, hashlib, os
body = {"model": os.environ["MODEL"], "stream": True, "max_tokens": 20, "messages": [{"role": "user", "content": "reply with one word: ok"}]}
d = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=False)
tpl = {k: ([] if k == "messages" else v) for k, v in body.items()}
print(d({"v": 1, "path": "/v1/chat/completions", "array_key": "messages", "handle": None, "base_count": 0,
         "base_digest": hashlib.sha256(b"zkd1:0").hexdigest(), "template_digest": hashlib.sha256(d(tpl).encode()).hexdigest(),
         "template": tpl, "delta": body["messages"], "expect_bytes": len(d(body).encode())}))
PY
)
    c=$(curl -s -N -D "$t.h" -o "$t" -w '%{http_code}' -m 120 -H "Authorization: Bearer ${KEY}" -H 'Content-Type: application/json' -H 'x-zkd-synthetic: 1' -d "$env" "${C}/zkd/v1/delta")
    [ "$c" = 200 ] && grep -q '\[DONE\]' "$t" && grep -qi '^x-zk-handle: ' "$t.h" && ok "/zkd/v1/delta 真 envelope → 200 流到 [DONE]，带 x-zk-handle" || bad "/zkd/v1/delta 真 envelope → ${c} $(head -c 200 "$t")"
  fi

  c=$(curl -s -o "$t" -D "$t.h" -w '%{http_code}' -m 30 "${C}/ccdl/apps/versions.json")
  [ "$c" = 200 ] && grep -q '"cursor"' "$t" && grep -qi '^content-type: application/json' "$t.h" && ok "/ccdl/apps/versions.json → 200 json，与 cc 同内容: $(curl -s -m 30 https://cc.auto-link.com.cn/ccdl/apps/versions.json | cmp -s - "$t" && echo 是 || echo 否)" || bad "/ccdl/apps/versions.json → ${c} $(head -c 80 "$t")"
  c=$(curl -s -o "$t" -w '%{http_code}' -m 30 -r 0-15 "${C}/ccdl/apps/Codex-mac.dmg")
  [ "$c" = 206 ] && [ "$(wc -c < "$t" | tr -d ' ')" = 16 ] && ok "/ccdl 大文件 Range → 206 16 字节" || bad "/ccdl Range → ${c} $(wc -c < "$t") 字节"
  c=$(curl -s -o "$t" -w '%{http_code}' -m 15 "${C}/ccdl/")
  grep -q '<title>Open WebUI' "$t" && bad "/ccdl/ 回的是 Open WebUI 页面" || ok "/ccdl/ → ${c}（不列目录、不是 OWUI）"
  c=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -X POST "${C}/ccdl/apps/versions.json"); [ "$c" = 403 ] && ok "/ccdl POST → 403" || bad "/ccdl POST → ${c}"

  b=$(curl -s -m 15 "${C}/muse-media/v1/media/${z}.webp"); echo "$b" | grep -q not_found_error && ok "/muse-media 随机 id → muse2api 的 404 JSON" || bad "/muse-media 随机 id → $(echo "$b" | head -c 100)"
  c=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -X POST "${C}/muse-media/v1/media/${z}.webp"); [ "$c" = 403 ] && ok "/muse-media POST → 403" || bad "/muse-media POST → ${c}"
  b=$(curl -s -m 15 "${C}/gemini-media/openai/v1/media/${z}"); echo "$b" | grep -q 'expired or unknown' && ok "/gemini-media 随机 id → gemini-media 的 404 JSON" || bad "/gemini-media 随机 id → $(echo "$b" | head -c 100)"
  c=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -X POST "${C}/gemini-media/openai/v1/media/${z}"); [ "$c" = 403 ] && ok "/gemini-media POST → 403" || bad "/gemini-media POST → ${c}"
  # 两个媒体段只放 32hex，别的路径必须落回 Open WebUI（不暴露 muse2api/gemini-media 其它接口）
  b=$(curl -s -m 15 "${C}/muse-media/v1/models"); echo "$b" | grep -qE '"(object|data|error)"' && bad "/muse-media/v1/models 被放行到 muse2api：$(echo "$b" | head -c 80)" || ok "/muse-media/v1/models 不放行（不是 muse2api 回的）"

  # 已有段不受影响
  for p in /oneclick/ /model/health/liveliness /api/version; do
    c=$(curl -s -o "$t" -w '%{http_code}' -m 15 "${C}${p}"); [ "$c" = 200 ] && ok "已有 ${p} → 200" || bad "已有 ${p} → ${c}"
  done
  b=$(curl -s -m 15 https://cc.auto-link.com.cn/zkd/healthz); echo "$b" | grep -q '"ok":true' && ok "老 cc /zkd/healthz 仍通" || bad "老 cc /zkd/healthz → $(echo "$b" | head -c 80)"
  c=$(curl -s -o /dev/null -w '%{http_code}' -m 15 https://cc.auto-link.com.cn/ccdl/apps/versions.json); [ "$c" = 200 ] && ok "老 cc /ccdl 仍通" || bad "老 cc /ccdl → ${c}"
  rm -f "$t" "$t.h"
  [ $fail = 0 ] && echo "VERIFY PASS" || { echo "VERIFY FAIL ${fail} 项"; return 3; }
}

case "${1:-}" in
  render) render;;
  apply) apply;;
  verify) verify;;
  *) sed -n 2,11p "$0" | sed 's/^# \{0,1\}//'; exit 1;;
esac
