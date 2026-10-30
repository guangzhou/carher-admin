#!/usr/bin/env bash
# Gemini 网页池「只重登没登上的号」。**在本机 Mac 上跑**（198 连不上 188）。
#
#   bash scripts/gemini-web/relogin.sh                  # 自动挑：login_health 里 authed=false 的号
#   bash scripts/gemini-web/relogin.sh id1 id2 ...       # 指定号
#   DRY=1 bash scripts/gemini-web/relogin.sh             # 只打印要重登哪些号，不动浏览器、不重启
#   LANES=4 bash scripts/gemini-web/relogin.sh           # 并发车道（默认 3）
#   YES=1 bash scripts/gemini-web/relogin.sh             # 不问直接重启（后台/无人值守必须给）
#   CARDS=.work/xxx/new-cards.txt bash .../relogin.sh    # 卡密以这个文件为准（密码换过就必须给）
#
# 和 add-account.sh 的分工：
#   add-account.sh  = **加新号**（web + antigravity 两段，整池重启一次）
#   relogin.sh      = **只刷 cookie**，不碰 antigravity、不加号；**一次重启收口**（≈3 分钟中断）
#
# 做了什么：
#   1. 先问 198 哪些号没登上（判据 = SNlM0e，见 login_health.py）；给了 id 就只做这些
#   2. 卡密：默认用 198 上存的；给了 CARDS 就以文件为准并**先写回 198**（密码被改过的号必须走这条）
#   3. 188 playwright 多车道登录 → 只有**真拿到两个 cookie**的号才写回 198（失败的号保留旧 cookie，不会把配置搞坏）
#   4. 一次 build_config + 一次 Secret + 一次 rollout；重启前打印最近 10 分钟 LiteLLM 侧请求数
#   5. 重启后用 login_health check 复核**被改过的那些号**：authed 必须翻成 true，否则报 STOP
# 不打印密码 / cookie / TOTP。
set -euo pipefail

H198=cltx@10.68.13.198
H188=cltx@10.68.13.188
SSH="ssh -o ClearAllForwardings=yes"
HERE="$(cd "$(dirname "$0")" && pwd)"
D188=/tmp/gweb-relogin
IMG=mcr.microsoft.com/playwright/python:v1.60.0-noble
LANES="${LANES:-3}"
DRY="${DRY:-}"
OUT="$HERE/../../.work/1029-batch/d-gweb"; mkdir -p "$OUT"

# ---- 1. 要重登哪些号 ---------------------------------------------------------
if [[ $# -gt 0 ]]; then
  IDS="$*"
else
  echo "[1/6] 问 198：哪些号没登上（authed=false）"
  IDS=$($SSH $H198 'sudo python3 - <<PY
import json
d = json.load(open("/opt/chatgpt-acct-console/data/gweb_login.json"))
ck = d.get("check") or {}
print(" ".join(sorted(k for k, v in ck.items() if v.get("authed") is False)))
PY')
fi
N=$(wc -w <<<"$IDS" | tr -d ' ')
echo "    要重登 $N 个号：$(cut -c1-200 <<<"$IDS")$([[ ${#IDS} -gt 200 ]] && echo ' …')"
[[ "$N" -gt 0 ]] || { echo "没有要重登的号（都已登录）"; exit 0; }

# ---- 2. 卡密 -----------------------------------------------------------------
if [[ -n "${CARDS:-}" ]]; then
  echo "[2/6] 以 $CARDS 为准，先把新卡密写回 198"
  python3 - "$CARDS" <<'PY' > "$OUT/.newcards.json"
import json, re, sys
out = {}
for raw in open(sys.argv[1]):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    f = [x.strip() for x in line.split("----")]
    if len(f) == 3:
        email, pw, totp = f
    elif len(f) == 4:
        email, pw, _, totp = f
    else:
        sys.exit("卡密这行解析不了：%s" % line.split("----")[0])
    name = re.sub(r"[^a-z0-9]", "", email.split("@")[0].lower())
    out[name] = {"email": email, "password": pw, "totp_secret": totp.replace(" ", "")}
json.dump(out, sys.stdout)
PY
  $SSH $H198 "sudo python3 -c 'import json,os,sys
d = json.load(sys.stdin)
for name, card in d.items():
    p = \"/root/gemini-web-build/accounts/%s.json\" % name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, json.dumps(card).encode()); os.close(fd)
print(\"写回 %d 张卡密\" % len(d))'" < "$OUT/.newcards.json"
  rm -f "$OUT/.newcards.json"
else
  echo "[2/6] 用 198 上已存的卡密（密码若被改过，这一步必失败 ⇒ 要带 CARDS=）"
fi

if [[ -n "$DRY" ]]; then echo "DRY=1 ⇒ 到此为止，没动浏览器也没重启"; exit 0; fi

# ---- 3. 188 上登录 -----------------------------------------------------------
echo "[3/6] 送卡密到 188 并登录（$LANES 车道，每号 ~30s）"
$SSH $H188 "rm -rf $D188; mkdir -p $D188/acct $D188/out && chmod 700 $D188 $D188/acct $D188/out"
cleanup() { $SSH $H188 "rm -rf $D188/acct $D188/out/*.cookies.json" >/dev/null 2>&1 || true; }
trap cleanup EXIT
$SSH $H198 "sudo python3 /root/gw-tools/login_health.py cards-export $IDS" \
  | $SSH $H188 "umask 077; cat > $D188/cards.json"
$SSH $H188 "cd $D188 && python3 -c '
import json
for k, v in json.load(open(\"cards.json\")).items():
    open(\"acct/%s.json\" % k, \"w\").write(json.dumps(v))
' && chmod 600 $D188/acct/* && rm -f $D188/cards.json"
scp -q "$HERE/google_login.py" "$H188:$D188/google_login.py"

: > "$OUT/relogin.raw"
for L in $(seq 1 "$LANES"); do
  PART=$(tr ' ' '\n' <<<"$IDS" | awk -v l="$L" -v n="$LANES" 'NF && (NR-1)%n==(l-1)' | tr '\n' ' ')
  [[ -n "${PART// /}" ]] || continue
  (
    for NAME in $PART; do
      R=$($SSH $H188 "timeout 420 docker run --rm --ipc=host -v $D188:/work:ro -v $D188/acct:/acct:ro -v $D188/out:/out \
        -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:9$L $IMG bash -c \
        'Xvfb :9$L -screen 0 1440x1000x24 >/dev/null 2>&1 & sleep 1 && \
         pip install patchright==1.60.0 -q --root-user-action=ignore >/dev/null 2>&1 && \
         DISPLAY=:9$L python3 /work/google_login.py $NAME' 2>&1 | grep -E '^STOP|^OK' | head -1" || true)
      echo "$NAME ${R:-STOP no-output}"
    done
  ) >> "$OUT/relogin.raw" &
done
wait
awk '{print "    " $0}' "$OUT/relogin.raw" | cut -c1-200

# ---- 4. 成功的号写回 198 -----------------------------------------------------
echo "[4/6] 把真拿到 cookie 的号写回 198（失败的号保留旧 cookie）"
GOT=$($SSH $H188 "ls $D188/out/*.cookies.json 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.cookies\.json//'" || true)
GN=$(wc -w <<<"${GOT:-}" | tr -d ' ')
echo "    拿到 cookie：$GN / $N"
[[ "$GN" -gt 0 ]] || { echo "STOP 一个都没登上 ⇒ 不重启、不改配置"; exit 1; }
for NAME in $GOT; do
  $SSH $H188 "cat $D188/out/$NAME.cookies.json" \
    | $SSH $H198 "sudo sh -c 'umask 077; cat > /root/gemini-web-build/accounts/$NAME.cookies.json'"
done

# ---- 5. 一次重启收口 ---------------------------------------------------------
echo "[5/6] 重启前先看最近 10 分钟有没有真实流量"
$SSH $H198 'sudo bash -c "cat > /tmp/gw_t.sql <<EOF
select count(*) filter (where \"startTime\" > now() - interval '\''10 minutes'\'') last10m,
       max(\"startTime\") last_req
from \"LiteLLM_SpendLogs\" where model_group like '\''web-gemini-%'\'';
EOF
k3s kubectl -n litellm-product exec -i litellm-db-0 -- psql -U litellm -d litellm -At -F\"|\" < /tmp/gw_t.sql; rm -f /tmp/gw_t.sql"'
# YES=1 跳过确认（无人值守/后台跑时必须给，否则 read 读不到输入直接退出）
if [[ -n "${YES:-}" ]]; then
  yn=y; echo "    YES=1 ⇒ 直接重启"
else
  read -r -p "    继续？会重启 gemini-web（Recreate，约 3 分钟全池中断）[y/N] " yn
fi
[[ "$yn" == "y" || "$yn" == "Y" ]] || { echo "没重启。cookie 已写进 accounts/，下次重启自动生效"; exit 0; }
scp -q "$HERE/build_config.py" "$H198:/home/cltx/build_config.py"
$SSH $H198 'sudo bash -c "
set -e
TS=\$(date +%Y%m%dT%H%M%S)
cp -p /root/gemini-web-build/config.yaml /root/gemini-web-build/config.yaml.bak-\$TS
mv /home/cltx/build_config.py /root/gemini-web-build/build_config.py
python3 /root/gemini-web-build/build_config.py | cut -c1-120
K=\"k3s kubectl -n litellm-dev\"
\$K create secret generic gemini-web-secrets --from-file=config.yaml=/root/gemini-web-build/config.yaml --dry-run=client -o yaml | \$K apply -f - >/dev/null
date -u +\"    restart_start_utc=%F %T\"
\$K rollout restart deploy/gemini-web >/dev/null
\$K rollout status deploy/gemini-web --timeout=600s | tail -1
date -u +\"    restart_done_utc=%F %T\"
echo \"    回滚配置：/root/gemini-web-build/config.yaml.bak-\$TS\"
"'

# ---- 6. 复核 -----------------------------------------------------------------
echo "[6/6] 复核：重登过的号 authed 必须翻成 true"
$SSH $H198 "sudo python3 /root/gw-tools/login_health.py check $GOT 2>&1 | tail -$((GN + 2))"
echo "明细：$OUT/relogin.raw"
