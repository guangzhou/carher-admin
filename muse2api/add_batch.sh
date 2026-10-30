#!/usr/bin/env bash
# 批量把 muse 号登进 198 号池。**在本机 Mac 上跑**（取码要 ssh 188，登录要在集群内）。
#
#   bash muse2api/add_batch.sh <卡密文件> [起始序号]
#   卡密文件每行：<邮箱> <generator.email 链接>
#
# 为什么登录在集群内跑（10-30）：
#   - muse 登录是纯 HTTP（五步 native-auth，整条链路没有验证码）⇒ 不需要浏览器
#   - 🔴 curl_cffi 和 ssh 的 SOCKS5 不兼容（curl: 97），而 mihomo/squid 的 HTTP 代理只对集群内放行
#     ⇒ 在 litellm-dev/gemini-web 这个带 curl_cffi 的 pod 里跑，直接用 HTTP 代理
# 取码在本机（pod 里没有 ssh/scp）：先读旧码当 stale，登录脚本等 /tmp/muse-otp-<邮箱> 文件，
# 本机取到新码后用 kubectl cp 喂进去。
#
# 🔴 出口怎么定（10-30 重写，原来是错的）：
#   以前按**行号轮流**从一个硬编码 IP 数组里挑，而且只用于登录 ——
#   服务时读的是 Secret `muse2api-egress-map`，那个 Secret 当时根本不存在
#   ⇒ 12 个号登录各走一个 IP，服务全从默认隧道 47.84.85.100 出去，两者对不上。
#   现在统一走 `egress_assign.py`（198 上的持久台账）：
#     claim <label> ⇒ 幂等分配一个**没被别的号占**的出口，重登复用同一个（一号一辈子一个 IP）
#     sync --apply  ⇒ 台账写进 Secret，让服务用的就是登录用的那条腿
#   台账满了（出口比号少）会明说，并让该号落默认隧道，不偷偷两号共用。
set -uo pipefail
CARDS="${1:?用法: add_batch.sh <卡密文件> [起始序号]}"
START="${2:-5}"
H198="cltx@10.68.13.198"; H188="cltx@10.68.13.188"
SSH="ssh -n -o ClearAllForwardings=yes"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../.work/1029-batch/muse-add"; mkdir -p "$OUT"
scp -q "$HERE/egress_assign.py" "$H198:/tmp/lgx-egress-assign.py"

scp -q "$HERE/login_http.py" "$H198:/tmp/lgx-login-http.py"
$SSH $H198 "K=\"sudo k3s kubectl -n litellm-dev\"; P=\$(\$K get pod -l app=gemini-web -o jsonpath='{.items[0].metadata.name}'); \$K cp /tmp/lgx-login-http.py \$P:/tmp/lh.py && rm -f /tmp/lgx-login-http.py && \$K exec \$P -- sh -c 'test -s /tmp/lh.py && echo 登录脚本已就位'"
scp -q "$HERE/genemail_code.py" "$H188:/tmp/muse-genemail-code.py"

i=0; n="$START"; ok=0; fail=0
while read -r EMAIL LINK <&3; do
  [[ -z "${EMAIL:-}" || "$EMAIL" == \#* ]] && continue
  LABEL="muse-gen-$(printf %02d "$n")"
  # 台账定出口（幂等）：已有的号拿回原来那条腿，新号拿一条没人占的。空 = 没有空闲出口，落默认隧道。
  PXNAME=$($SSH $H198 "sudo python3 /tmp/lgx-egress-assign.py claim ${LABEL}" 2>&1 | sed -n 's/^OK   .* -> \([^（]*\)（.*/\1/p' | tr -d ' ')
  if [[ -z "${PXNAME:-}" ]]; then
    echo "=== [$LABEL] $EMAIL  🔴 没有空闲出口 ⇒ 走默认隧道（和别的号共用 IP）"
  else
    echo "=== [$LABEL] $EMAIL  出口 ${PXNAME}"
  fi
  if [[ "$LINK" == *generator.email* ]]; then
    GETCODE="python3 /tmp/muse-genemail-code.py $EMAIL"
  else
    # 卖家接码链接：回 JSON，subject 里是「Use code NNNNNN」；只给最新一封 ⇒ 判新鲜只看码变没变
    GETCODE="curl -fsSL --max-time 25 -A Mozilla/5.0 '$LINK' | grep -o 'Use code [0-9]\{6\}' | grep -o '[0-9]\{6\}' | sed 's/^/CODE=/'"
  fi
  STALE=$($SSH $H188 "${GETCODE/\$EMAIL/$EMAIL} --wait 5" 2>/dev/null | grep -o 'CODE=[0-9]*' | cut -d= -f2 || true)
  if [[ "$LINK" != *generator.email* ]]; then
    STALE=$($SSH $H188 "$GETCODE" 2>/dev/null | grep -o 'CODE=[0-9]*' | cut -d= -f2 || true)
  fi
  LOG="$OUT/$LABEL.log"
  (
    # 代理 URL 含凭据 ⇒ 只在 198 上的那层 shell 里展开，不进本机日志、不进 argv 的可见部分
    $SSH $H198 "K=\"sudo k3s kubectl -n litellm-dev\"; P=\$(\$K get pod -l app=gemini-web -o jsonpath='{.items[0].metadata.name}'); \
      PX=\$(sudo python3 -c \"
import sys; sys.path.insert(0,'/tmp')
sys.argv=['x']
import importlib.util
s=importlib.util.spec_from_file_location('ea','/tmp/lgx-egress-assign.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
print(m.proxy_url('${PXNAME}') if '${PXNAME}' else '')\"); \
      if [ -n \"\$PX\" ]; then \$K exec \$P -- /app/.venv/bin/python /tmp/lh.py $LABEL $EMAIL --proxy \"\$PX\" --stale '${STALE:-}' --no-import --print-cookies; \
      else \$K exec \$P -- /app/.venv/bin/python /tmp/lh.py $LABEL $EMAIL --stale '${STALE:-}' --no-import --print-cookies; fi"
  ) > "$LOG" 2>&1 &
  LPID=$!
  # 等脚本报「要码了」，再去 188 取新码喂进去
  for _ in $(seq 1 40); do grep -q "WAITCODE=" "$LOG" 2>/dev/null && break; sleep 3; done
  if grep -q "WAITCODE=" "$LOG" 2>/dev/null; then
    if [[ "$LINK" == *generator.email* ]]; then
      CODE=$($SSH $H188 "python3 /tmp/muse-genemail-code.py $EMAIL --wait 150 ${STALE:+--stale $STALE}" 2>/dev/null | grep -o 'CODE=[0-9]*' | cut -d= -f2 || true)
    else
      CODE=""
      for _try in $(seq 1 15); do
        C=$($SSH $H188 "$GETCODE" 2>/dev/null | grep -o 'CODE=[0-9]*' | cut -d= -f2 || true)
        if [[ -n "${C:-}" && "$C" != "${STALE:-}" ]]; then CODE="$C"; break; fi
        sleep 10
      done
    fi
    if [[ -n "${CODE:-}" ]]; then
      $SSH $H198 "K=\"sudo k3s kubectl -n litellm-dev\"; P=\$(\$K get pod -l app=gemini-web -o jsonpath='{.items[0].metadata.name}'); \
        echo -n '$CODE' | \$K exec -i \$P -- sh -c 'cat > /tmp/muse-otp-$(echo "$EMAIL" | tr -cd 'A-Za-z0-9')'" >/dev/null 2>&1
      echo "    已喂验证码"
    else
      echo "    STOP 没取到新码"
    fi
  fi
  wait $LPID
  if grep -q "^COOKIEJSON=" "$LOG"; then
    # 本机把 cookie 导入号池（pod 里没有到 30610 的路由也不该有号池 key）
    python3 - "$LOG" "$LABEL" <<'PY'
import json, re, subprocess, sys, urllib.request
log, label = sys.argv[1], sys.argv[2]
m = re.search(r"^COOKIEJSON=(.*)$", open(log).read(), re.M)
d = json.loads(m.group(1))
key = subprocess.run(["ssh", "-o", "ClearAllForwardings=yes", "cltx@10.68.13.198",
                      "sudo k3s kubectl -n muse2api get secret muse2api-env -o jsonpath={.data.MUSE2API_KEY} | base64 -d"],
                     capture_output=True, text=True, timeout=60).stdout.strip()
def api(meth, path, body=None):
    req = urllib.request.Request("http://10.68.13.198:30610" + path, method=meth,
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    return json.loads(urllib.request.urlopen(req, timeout=200).read() or b"{}")
accts = api("GET", "/admin/accounts"); accts = accts.get("accounts", accts)
ex = [x for x in accts if x.get("label") == label]
if ex:
    aid = ex[0]["id"]
    api("POST", "/admin/accounts/%s/cookies" % aid, {"cookies": d["cookies"], "expires": d["expires"]})
    api("PATCH", "/admin/accounts/%s" % aid, {"enabled": True})
else:
    aid = api("POST", "/admin/accounts", {"label": label, "cookies": d["cookies"], "expires": d["expires"]})["added"][0]["id"]
t = api("POST", "/admin/accounts/%s/test" % aid)
print("    导入 %s id=%s 真登录=%s %s" % (label, aid, t.get("ok"), (t.get("message") or "")[:60]))
PY
    # 登录日志里有这条腿的真实出口 IP（login_http.py 的「① 首页 … | 出口: x.x.x.x」）⇒ 记进台账
    LOGIP=$(grep -oE '出口: [0-9.]+' "$LOG" | head -1 | sed 's/出口: //')
    if [[ -n "${LOGIP:-}" && -n "${PXNAME:-}" ]]; then
      $SSH $H198 "sudo python3 -c \"
import importlib.util
s=importlib.util.spec_from_file_location('ea','/tmp/lgx-egress-assign.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.record_exit_ip('${LABEL}','${LOGIP}')\"" >/dev/null 2>&1
      echo "    台账记下 ${LABEL} 登录出口 ${LOGIP}"
    fi
    ok=$((ok+1)); echo "    OK"
  else
    fail=$((fail+1)); echo "    STOP $(grep -E '^STOP' "$LOG" | tail -1)"
  fi
  i=$((i+1)); n=$((n+1))
done 3< "$CARDS"
echo "== 成功 ${ok} / 失败 ${fail}；日志 ${OUT}/muse-gen-*.log"
# 🔴 最后一步：把台账同步进 Secret，让**服务**用的就是刚才**登录**用的那条腿。
# 不做这一步 = 10-30 那次的坑：登录 IP 各不相同，服务全从默认隧道出去。
echo "== 台账 -> Secret（服务侧生效要 rollout restart，约 1~2 分钟中断）"
$SSH $H198 "sudo python3 /tmp/lgx-egress-assign.py sync" 2>&1 | tail -20
echo "   确认无误后执行：ssh ${H198} 'sudo python3 /tmp/lgx-egress-assign.py sync --apply' 再 rollout restart deploy/muse2api"
