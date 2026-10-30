#!/usr/bin/env bash
# Gemini 网页池「我们存的密码还对不对」批量体检。**在本机 Mac 上跑**（198 连不上 188）。
#
#   bash scripts/gemini-web/pwcensus.sh                 # 全池（config.yaml 里的号）
#   bash scripts/gemini-web/pwcensus.sh id1 id2 ...      # 只验这几个
#   LANES=6 bash scripts/gemini-web/pwcensus.sh          # 并发车道数（默认 4）
#
# 流程：198 导卡密（root 600）→ 送到 188 的 tmpfs 目录 → 188 playwright 镜像里每车道一个 Xvfb，
# 每号只走到「填密码」那一步读页面判词就关浏览器 → 结果回 198 `login_health.py pw-ingest`。
# **不登完、不改远端任何状态、不打印密码**。跑完 188 上的卡密目录必删（trap）。
#
# 为什么要它：10-29 查出 76/76 号的 Google 密码被外部改过（页面写 "Your password was changed N days ago"），
# cookie 全失效 ⇒ 整池在拿**免费 guest 身份**服务。以后要在用户撞上之前发现这件事，就靠这个 + login_health check。
set -euo pipefail

H198=cltx@10.68.13.198
H188=cltx@10.68.13.188
SSH="ssh -o ClearAllForwardings=yes"
HERE="$(cd "$(dirname "$0")" && pwd)"
D188=/tmp/gweb-pwcensus
IMG=mcr.microsoft.com/playwright/python:v1.60.0-noble
LANES="${LANES:-4}"
OUT="${OUT:-$HERE/../../.work/1029-batch/d-gweb}"
mkdir -p "$OUT"

echo "[1/4] 198 导卡密"
$SSH $H198 "sudo python3 /root/gw-tools/login_health.py cards-export $* " > "$OUT/.cards.json"
NAMES=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print(" ".join(sorted(k for k,v in d.items() if v.get("password"))))
' "$OUT/.cards.json")
N=$(wc -w <<<"$NAMES" | tr -d ' ')
echo "    $N 个号有密码可验"
[[ "$N" -gt 0 ]] || { rm -f "$OUT/.cards.json"; echo "没有可验的号"; exit 0; }

echo "[2/4] 送到 188"
$SSH $H188 "rm -rf $D188; mkdir -p $D188/acct && chmod 700 $D188 $D188/acct"
cleanup() { $SSH $H188 "rm -rf $D188/acct" >/dev/null 2>&1 || true; rm -f "$OUT/.cards.json"; }
trap cleanup EXIT
python3 - "$OUT/.cards.json" <<'PY' | $SSH $H188 "umask 077; cat > $D188/cards.ndjson"
import json, sys
d = json.load(open(sys.argv[1]))
for k, v in sorted(d.items()):
    if v.get("password"):
        print(json.dumps({"name": k, "card": v}))
PY
$SSH $H188 "cd $D188 && python3 -c '
import json
for ln in open(\"cards.ndjson\"):
    r = json.loads(ln)
    open(\"acct/%s.json\" % r[\"name\"], \"w\").write(json.dumps(r[\"card\"]))
' && chmod 600 $D188/acct/* && rm -f $D188/cards.ndjson"
scp -q "$HERE/pwcheck_remote.py" "$H188:$D188/pwcheck.py"

echo "[3/4] 188 上 $LANES 个车道跑浏览器（每号约 20~40s）"
: > "$OUT/pwcensus.raw"
for L in $(seq 1 "$LANES"); do
  PART=$(tr ' ' '\n' <<<"$NAMES" | awk -v l="$L" -v n="$LANES" 'NF && (NR-1)%n==(l-1)' | tr '\n' ' ')
  [[ -n "${PART// /}" ]] || continue
  $SSH $H188 "timeout 7200 docker run --rm --ipc=host -v $D188:/work:ro -v $D188/acct:/acct:ro \
    -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:9$L $IMG bash -c \
    'Xvfb :9$L -screen 0 1440x1000x24 >/dev/null 2>&1 & sleep 1 && \
     pip install patchright==1.60.0 -q --root-user-action=ignore >/dev/null 2>&1 && \
     DISPLAY=:9$L python3 /work/pwcheck.py $PART' 2>&1 | grep -E '^OK|^STOP'" >> "$OUT/pwcensus.raw" &
done
wait

echo "[4/4] 结果回 198"
python3 - "$OUT/pwcensus.raw" > "$OUT/pwcensus.json" <<'PY'
import json, re, sys
res = {}
for ln in open(sys.argv[1]):
    f = ln.split()
    if len(f) < 3 or f[0] not in ("OK", "STOP"):
        continue
    verdict, name, extra = f[1], f[2], " ".join(f[3:])[:120]
    res[name] = {"pw-accepted": "ok",
                 "pw-changed": "pw-changed:" + re.sub(r"^changed=", "", extra),
                 "pw-wrong": "pw-wrong", "captcha": "captcha"}.get(verdict, "other:" + extra)
json.dump(res, sys.stdout)
PY
$SSH $H198 "sudo python3 /root/gw-tools/login_health.py pw-ingest" < "$OUT/pwcensus.json"
python3 -c '
import collections, json, sys
d = json.load(open(sys.argv[1]))
c = collections.Counter(v.split(":")[0] for v in d.values())
print("== 汇总", dict(c))
for k, v in sorted(d.items()):
    if not v.startswith("ok"):
        print("   STOP %-24s %s" % (k, v))
' "$OUT/pwcensus.json"
echo "明细：$OUT/pwcensus.json · 原始：$OUT/pwcensus.raw"
