#!/usr/bin/env bash
# litellm-my-models.sh —— 拿自己的 key，一键列出「我能调哪些模型」，并可选真打一遍确认哪些是活的
#
#   export LITELLM_KEY=sk-xxxx
#   ./scripts/litellm-my-models.sh              # 只列名单（不花钱、不产生请求计数）
#   ./scripts/litellm-my-models.sh --probe      # 逐个真打一次，分出「真能用 / 报错」
#   ./scripts/litellm-my-models.sh --probe -j 8 # 并发 8（默认 4）
#   ./scripts/litellm-my-models.sh --probe -m 'cr-g-*'   # 只探名字匹配该 glob 的
#   ./scripts/litellm-my-models.sh --json       # 机器可读
#
# 判据纪律（这三条是本脚本存在的理由，别绕过）：
#   1. 「我有哪些名」只认 GET /v1/models。/model/info 对普通 key 恒返回空数组，
#      看起来像「你一个模型都没有」，那是假红。
#   2. 「这个名能不能落地」/v1/models 答不了 —— 白名单只发通行证，能否落地取决于
#      per-key alias 有没有把它映射到本集群真实存在的模型组。只有 --probe 真打能判。
#   3. --probe 的判据只看 HTTP 状态码和 error.code，不看 choices[].content。
#      推理模型在小 max_tokens 下会返 200 + content=None（预算被 reasoning 吃光），
#      那是活的，不是死的。拿 content 判死活会造出一屏假红。
#
# 对应文档：docs/198-litellm-my-models.md
set -euo pipefail

BASE="${LITELLM_BASE:-https://cc.auto-link.com.cn/pro}"
KEY="${LITELLM_KEY:-}"
JOBS=4
PROBE=0
JSON=0
MATCH='*'

while [ $# -gt 0 ]; do
  case "$1" in
    --probe)  PROBE=1; shift ;;
    --json)   JSON=1; shift ;;
    -j)       JOBS=${2:?-j 需要一个数字}; shift 2 ;;
    -m)       MATCH=${2:?-m 需要一个 glob}; shift 2 ;;
    __probe1) shift; PROBE_ONE=1; break ;;   # 内部用：探单个模型，由 xargs 调起
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $1（-h 看用法）" >&2; exit 2 ;;
  esac
done

[ -n "$KEY" ] || { echo "需要先 export LITELLM_KEY=sk-...（你自己的 virtual key）" >&2; exit 2; }

# ---------- 内部：探单个模型 ----------
# 输出一行 TSV：<model>\t<http_code>\t<秒>\t<摘要>
if [ "${PROBE_ONE:-0}" = 1 ]; then
  M=$1
  PAYLOAD=$(M="$M" python3 -c 'import os,json;print(json.dumps({"model":os.environ["M"],"messages":[{"role":"user","content":"hi"}],"max_tokens":16}))')
  OUT=$(curl -sS -m 90 -w $'\n<<%{http_code}|%{time_total}>>' \
        "$BASE/v1/chat/completions" \
        -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
        -d "$PAYLOAD" 2>&1 || true)
  META=${OUT##*<<}; META=${META%%>>*}
  CODE=${META%%|*}; SECS=${META##*|}
  BODY=${OUT%$'\n'<<*}
  SUM=$(BODY="$BODY" python3 <<'PY'
import os, json
b = os.environ["BODY"]
try:
    d = json.loads(b)
except Exception:
    print(" ".join(b.split())[:120]); raise SystemExit
e = d.get("error")
if isinstance(e, dict):
    code = e.get("code") or e.get("type") or ""
    msg = " ".join(str(e.get("message") or "").split())[:120]
    print((str(code) + ": " if code else "") + msg)
else:
    u = d.get("usage") or {}
    print("in=%s out=%s" % (u.get("prompt_tokens", 0), u.get("completion_tokens", 0)))
PY
) || SUM="(响应无法解析)"
  printf '%s\t%s\t%s\t%s\n' "$M" "${CODE:-000}" "${SECS:-0}" "$SUM"
  exit 0
fi

# ---------- 1. 名单 ----------
fetch() { # fetch <path>；带一次重试（198 上 nginx 偶发 5xx，重试即好）
  local out code body try=0
  while :; do
    out=$(curl -sS -m 60 -w $'\n<<%{http_code}>>' "$BASE$1" -H "Authorization: Bearer $KEY")
    code=${out##*<<}; code=${code%%>>*}
    [ "$code" = 200 ] && break
    try=$((try+1)); [ "$try" -ge 2 ] && break
    sleep 2
  done
  body=${out%$'\n'<<*}
  if [ "$code" != 200 ]; then
    echo "HTTP $code  GET $BASE$1" >&2
    echo "$body" >&2
    case "$code" in
      401)
        if printf '%s' "$body" | grep -q token_not_found_in_db; then
          echo "→ 401 token_not_found_in_db：这把 key 在本集群库里不存在（打错了/被删了/被轮换了）。" >&2
          echo "  注意 litellm.carher.net 是阿里云那一套，两边的 key 不通用。" >&2
        else
          echo "→ 401：先确认地址是 ${BASE}，再确认 key 属于这个集群。" >&2
        fi ;;
      404) echo "→ 404 且返回的是 nginx 网页，一般是漏了 /pro 前缀" >&2 ;;
    esac
    return 1
  fi
  printf '%s' "$body"
}

MODELS_JSON=$(fetch /v1/models) || exit 1
KEYINFO_JSON=$(fetch /key/info) || KEYINFO_JSON='{}'

MODELS=$(MODELS_JSON="$MODELS_JSON" python3 -c '
import os, json
print("\n".join(sorted(m["id"] for m in json.loads(os.environ["MODELS_JSON"])["data"])))')

# -m 过滤（case 的 pattern 写成 ($MATCH) 带左括号：在 $( ) 里裸 `)` 会被当成命令替换的结束）
if [ "$MATCH" != '*' ]; then
  MODELS=$(printf '%s\n' "$MODELS" | while IFS= read -r m; do
    # shellcheck disable=SC2254
    case "$m" in ($MATCH) echo "$m" ;; esac
  done)
fi

N=$(printf '%s\n' "$MODELS" | grep -c . || true)

# ---------- 2. 不探活：直接输出名单 + key 状态 ----------
if [ "$PROBE" = 0 ]; then
  if [ "$JSON" = 1 ]; then
    MODELS="$MODELS" KEYINFO_JSON="$KEYINFO_JSON" python3 -c '
import os, json
i = json.loads(os.environ["KEYINFO_JSON"]).get("info", {})
print(json.dumps({
  "models": [m for m in os.environ["MODELS"].split("\n") if m],
  "key_alias": i.get("key_alias"),
  "aliases": i.get("aliases") or {},
  "spend": i.get("spend"), "max_budget": i.get("max_budget"),
  "expires": i.get("expires"), "blocked": i.get("blocked"),
}, ensure_ascii=False, indent=2))'
    exit 0
  fi
  KEYINFO_JSON="$KEYINFO_JSON" python3 <<'PY'
import os, json
i = json.loads(os.environ["KEYINFO_JSON"]).get("info") or {}
if i:
    print("key: %s   spend %s / %s   到期 %s   blocked=%s" % (
        i.get("key_alias") or "(无 alias)",
        round(i.get("spend") or 0, 4), i.get("max_budget") or "无上限",
        i.get("expires") or "永不", i.get("blocked")))
PY
  echo "白名单里有 $N 个模型名："
  printf '%s\n' "$MODELS" | sed 's/^/  /'
  KEYINFO_JSON="$KEYINFO_JSON" python3 <<'PY'
import os, json
a = (json.loads(os.environ["KEYINFO_JSON"]).get("info") or {}).get("aliases") or {}
if a:
    print("\nper-key 映射 %d 条（其余名字走同名真实模型组）：" % len(a))
    for k, v in sorted(a.items()):
        print("   %s -> %s" % (k, v))
PY
  echo
  echo "⚠️ 以上是「通行证」，不是「一定能调通」。要判真能用：加 --probe。"
  exit 0
fi

# ---------- 3. 探活 ----------
echo "对 $N 个模型各真打一次（并发 ${JOBS}，会产生 $N 次请求和少量费用）…" >&2
RES=$(printf '%s\n' "$MODELS" | grep . \
      | LITELLM_KEY="$KEY" LITELLM_BASE="$BASE" \
        xargs -P "$JOBS" -I{} "$0" __probe1 {})

if [ "$JSON" = 1 ]; then
  RES="$RES" python3 -c '
import os, json
rows=[]
for line in os.environ["RES"].splitlines():
    p=line.split("\t")
    if len(p)==4:
        rows.append({"model":p[0],"http":int(p[1]),"seconds":float(p[2]),
                     "detail":p[3],"usable":p[1]=="200"})
print(json.dumps(sorted(rows,key=lambda r:(not r["usable"],r["model"])),ensure_ascii=False,indent=2))'
  exit 0
fi

RES="$RES" python3 <<'PY'
import os
rows = [l.split("\t") for l in os.environ["RES"].splitlines() if l.count("\t") == 3]
ok  = sorted(r for r in rows if r[1] == "200")
bad = sorted((r for r in rows if r[1] != "200"), key=lambda r: (r[1], r[0]))
w = max([len(r[0]) for r in rows] or [20]) + 2

print(f"\n✅ 真能调通 {len(ok)} 个")
for m, c, s, d in ok:
    print(f"  {m:<{w}}{float(s):5.1f}s  {d}")

if bad:
    print(f"\n❌ 调不通 {len(bad)} 个")
    for m, c, s, d in bad:
        print(f"  {m:<{w}}{c}  {d}")
    print("""
读错误码：
  403 key_model_access_denied  这个名不在你白名单里（脚本正常不会出现，出现说明名单和实际权限不一致）
  400                          名字有通行证但落不了地：多半 per-key alias 指向的组在本集群不存在
  404                          上游已退役这个名
  500                          先重试；反复 500 且是图像/视频模型，说明它不该走 /v1/chat/completions
""".rstrip())
print(f"\n合计 {len(rows)} 个名，可用 {len(ok)}，不可用 {len(bad)}。")
PY
