#!/usr/bin/env bash
# litellm-198-max-input-tokens-audit.sh
#
# 审计 198 上 model_info.max_input_tokens（LiteLLM 入口 _pre_call_checks 读的那个真闸门）。
#
# ── 2026-09-25 重写。老版本是「这个问题反复出现多次」的机械原因，两个缺陷都实测复现过 ──
#   ① 它只把**空值**算 missing，`10000000` 被它算成"已配置" ⇒ 15 行全是关掉闸门的
#      pattern 上它输出 `missing=0 / AUDIT OK / EXIT=0`。门禁把错值判为正确。
#   ② 它打印的"修复方式"里硬编了 `{"max_input_tokens": 10000000}` ⇒ 照它办一次，
#      就把闸门关掉一次。用户面表现是模型目录里出现 `10M | 128k`。
#   ③ 它只读 DB。而 **DB 里 `<none>` 不等于运行时没值**：zerokey-pool-gpt-5.6-* 那批
#      DB 全 `<none>`，`/model/info` 读到的是 10,000,000。只查 DB = 看不见大多数病灶。
#
# 现在的判据（取值真源：litellm-198-model-ceilings.json，不在本文件硬编数字）：
#   ABSURD  >= 1e7        —— 闸门被写到够不着 = 关掉。同一条 refusal 见
#                            scripts/chatgpt-pool-add-codex-model.py
#   OVER    > 官方天花板   —— 只在能匹配到官方条目的模型上比，匹配不到的**不判红**
#                            （宁可漏报也不用一把过宽的尺子造一屏假红）
#   MISSING 空            —— 会按 litellm_params.model 的 slug 去内置价格表静默继承，
#                            继承到什么完全不受我们控制
#   RETIRED 已退役模型     —— 单独一类。给死模型填上限是空操作，别当"缺配置"去补
#
# 用法（在 198 上，或任何能 kubectl 到 litellm-product 的地方）：
#   ./litellm-198-max-input-tokens-audit.sh                  # DB + 运行时，全表
#   ./litellm-198-max-input-tokens-audit.sh 'zerokey-%'      # 只审 DB 里 id 匹配该前缀的行
#   RUNTIME=0 ./litellm-198-max-input-tokens-audit.sh        # 只查 DB（快，但会漏运行时继承）
#
# 退出码： 0=没有 ABSURD/OVER/MISSING   1=存在问题行   2=环境/连接错误
set -uo pipefail

NS="${NS:-litellm-product}"
DB_POD="${DB_POD:-litellm-db-0}"
PG_USER="${PG_USER:-litellm}"
PG_DB="${PG_DB:-litellm}"
PATTERN="${1:-%}"
RUNTIME="${RUNTIME:-1}"
PROD_LABEL="${PROD_LABEL:-carher.net/litellm-production-route=enabled}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CEILINGS="${CEILINGS:-$HERE/litellm-198-model-ceilings.json}"
[[ -f "$CEILINGS" ]] || { echo "AUDIT ERROR: 找不到官方口径表 $CEILINGS" >&2; exit 2; }

# KUBECTL 可以是多词（例如 "sudo k3s kubectl"），所以只在调用方没显式给时才自动探测
if [[ -z "${KUBECTL:-}" ]]; then
  if command -v kubectl >/dev/null 2>&1; then KUBECTL="kubectl"; else KUBECTL="k3s kubectl"; fi
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/mit-audit-XXXXXX") || exit 2
trap 'rm -rf "$WORK"' EXIT

sql() {
  # 走 base64 传 SQL，避开引号地狱（多行 SQL 经 ssh 双引号会被吃掉）
  local b64
  b64=$(printf '%s' "$1" | base64 | tr -d '\n')
  $KUBECTL exec -n "$NS" "$DB_POD" -- bash -c \
    "echo $b64 | base64 -d | psql -U $PG_USER -d $PG_DB -tAF'|' -v ON_ERROR_STOP=1" 2>/dev/null
}

# ── 1. DB 侧 ────────────────────────────────────────────────────────────────
sql "SELECT model_info->>'id',
            model_name,
            COALESCE(model_info->>'max_input_tokens','')
       FROM \"LiteLLM_ProxyModelTable\"
      WHERE model_info->>'id' LIKE '${PATTERN}'
      ORDER BY 2,1;" > "$WORK/db.tsv"

if [[ ! -s "$WORK/db.tsv" ]]; then
  echo "AUDIT ERROR: 查不到任何匹配 '${PATTERN}' 的 DB 行（连接失败或前缀写错）" >&2
  exit 2
fi

# ── 2. 运行时侧（唯一合法判据：逐副本 /model/info）────────────────────────────
: > "$WORK/rt.tsv"
if [[ "$RUNTIME" = "1" ]]; then
  POD=$($KUBECTL -n "$NS" get po -l "$PROD_LABEL" --no-headers -o name 2>/dev/null | head -1 | cut -d/ -f2)
  if [[ -z "$POD" ]]; then
    echo "AUDIT ERROR: 按 '$PROD_LABEL' 找不到生产车道 pod（这个标签在 Pod 上，不在 Deployment 上）" >&2
    exit 2
  fi
  # 同样 base64 传 python 源码：引号很多的 exec ... python3 -c 会 Operation not permitted
  RT_PY=$(cat <<'PY'
import json, os, urllib.request
key = os.environ["LITELLM_MASTER_KEY"]
req = urllib.request.Request("http://127.0.0.1:4000/v1/model/info",
                             headers={"Authorization": "Bearer " + key})
for r in json.load(urllib.request.urlopen(req, timeout=180))["data"]:
    mi = r.get("model_info") or {}
    lp = r.get("litellm_params") or {}
    print("|".join([str(mi.get("id")), str(r.get("model_name")),
                    "" if mi.get("max_input_tokens") is None else str(mi["max_input_tokens"]),
                    str(lp.get("model"))]))
PY
)
  RT_B64=$(printf '%s' "$RT_PY" | base64 | tr -d '\n')
  $KUBECTL exec -n "$NS" "$POD" -- bash -c \
    "echo $RT_B64 | base64 -d > /tmp/mit_rt.py && python3 /tmp/mit_rt.py" 2>/dev/null \
    | grep -E '^[^|]*\|[^|]*\|' > "$WORK/rt.tsv"
  if [[ ! -s "$WORK/rt.tsv" ]]; then
    echo "AUDIT ERROR: 运行时 /model/info 一行都没读到（pod=${POD}）。空输出不是阴性结论，作废这一轮。" >&2
    exit 2
  fi
  echo "# 运行时判据 pod=$POD 行数=$(wc -l < "$WORK/rt.tsv")"
fi

# ── 3. 分类 ─────────────────────────────────────────────────────────────────
CEILINGS="$CEILINGS" DBF="$WORK/db.tsv" RTF="$WORK/rt.tsv" PATTERN="$PATTERN" python3 - <<'PY'
import json, os, sys

spec = json.load(open(os.environ["CEILINGS"], encoding="utf-8"))
ABSURD = spec["_absurd_threshold"]
CEIL = spec["ceilings"]
RETIRED = [k for k in spec["retired"] if not k.startswith("_")]
# 长的 key 先匹配，避免 gpt-5.6-sol 被 gpt-5 之类的短 key 抢走
KEYS = sorted(CEIL, key=len, reverse=True)


def match(*names):
    # 槽位实例的组 `kiro-slots-X` 和老实例 `kiro-X` 打的是同一个上游、同一份目录 ⇒ 用同一条天花板
    # （10-25 前没这行：13 个 kiro-slots-* 组全落 UNKNOWN，上下文治理对它们没有判据）
    names = [n.replace("kiro-slots-", "kiro-") if n else n for n in names]
    for n in names:
        if not n:
            continue
        for k in KEYS:
            if k in n:
                return k
    return None


def retired(*names):
    for n in names:
        if not n:
            continue
        for k in RETIRED:
            if k in n:
                return k
    return None


def load(path, with_slug):
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        rid, name, val = parts[0].strip(), parts[1].strip(), parts[2].strip()
        slug = parts[3].strip() if with_slug and len(parts) > 3 else ""
        rows.append((rid, name, val, slug))
    return rows


def classify(rows, layer):
    buckets = {"ABSURD": [], "OVER": [], "MISSING": [], "RETIRED": [], "UNKNOWN": [], "OK": []}
    for rid, name, val, slug in rows:
        # 谁是这一行的真身：**slug（litellm_params.model）优先，model_name 只是兜底**。
        # 组名会撒谎 —— 实测 group `gpt-5.3-codex` 的腿 slug 是 `gpt-5.6-luna`，
        # 按名字判会给它套上 272000 这个不属于它的天花板。
        r = retired(slug, name)
        if r:
            buckets["RETIRED"].append((rid, name, val, f"已退役 ({r})"))
            continue
        if val == "":
            k = match(slug, name)
            if k is None:
                # 我们没有它的官方数 ⇒ 报出来，但不判红：一把过宽的尺子会造一屏假红
                buckets["UNKNOWN"].append((rid, name, val, "空，且官方口径表里没有这个模型"))
            else:
                buckets["MISSING"].append(
                    (rid, name, val, f"空 ⇒ 按 slug 静默继承（官方 {k} 应为 {CEIL[k]['max_input_tokens']}）"))
            continue
        try:
            n = int(float(val))
        except ValueError:
            buckets["UNKNOWN"].append((rid, name, val, "值不是数字"))
            continue
        if n >= ABSURD:
            buckets["ABSURD"].append((rid, name, val, f">= {ABSURD} ⇒ 闸门被关掉，不是能力声明"))
            continue
        k = match(slug, name)
        if k is None:
            buckets["UNKNOWN"].append((rid, name, val, "官方口径表里没有这个模型，不判红"))
        elif n > CEIL[k]["max_input_tokens"]:
            buckets["OVER"].append((rid, name, val, f"> 官方 {k} 的 {CEIL[k]['max_input_tokens']}"))
        else:
            buckets["OK"].append((rid, name, val, k))
    return buckets


def report(layer, rows):
    b = classify(rows, layer)
    print(f"\n==== {layer}（{len(rows)} 行）====")
    for cls in ("ABSURD", "OVER", "MISSING", "RETIRED"):
        if not b[cls]:
            continue
        # 同名同值的行压成一条，否则 11 副本 × 3 档会刷满一屏
        agg = {}
        for rid, name, val, why in b[cls]:
            agg.setdefault((name, val, why), []).append(rid)
        print(f"-- {cls}: {len(b[cls])} 行 / {len(agg)} 个模型名")
        for (name, val, why), ids in sorted(agg.items()):
            shown = ids[0] + (f" (+{len(ids)-1} 行)" if len(ids) > 1 else "")
            print(f"   {cls:<8} {name:<34} = {val or '<空>':<10} {why}   e.g. {shown}")
    print(f"-- OK: {len(b['OK'])} 行   UNKNOWN(不判红): {len(b['UNKNOWN'])} 行")
    return b


rt_rows = load(os.environ["RTF"], with_slug=True) if os.path.getsize(os.environ["RTF"]) > 0 else []
# DB 行自己不带 slug（litellm_params 是密文），但运行时那份带。用 id 把 slug 借过来，
# 否则像 zerokey-codex-*-bridge 这种「名字里没有型号」的行在 DB 层永远判不出来。
slug_by_id = {rid: slug for rid, _n, _v, slug in rt_rows if slug and slug != "None"}
db_rows = [(rid, name, val, slug_by_id.get(rid, "")) for rid, name, val, _ in
           load(os.environ["DBF"], with_slug=False)]

db = report(f"DB 层  LiteLLM_ProxyModelTable  id LIKE '{os.environ['PATTERN']}'（slug 借自运行时）", db_rows)
rt = report("运行时层  /model/info（唯一合法判据）", rt_rows) if rt_rows else None
bad = sum(len(b[c]) for b in filter(None, (db, rt)) for c in ("ABSURD", "OVER", "MISSING"))
print("\n----")
if bad == 0:
    print("AUDIT OK: 没有 ABSURD / OVER / MISSING 行。")
    sys.exit(0)

src = spec["sources"]["codex_catalog"]
print(f"""AUDIT FAIL: {bad} 个问题行。

取值只认官方口径表 {os.path.basename(os.environ['CEILINGS'])}
  出处 {src['url']}  字段 {src['field']}  实拉 {src['fetched_at']}
⛔ 不许再填 {ABSURD}（那是把闸门关掉，不是"不限制"）。
⛔ RETIRED 那一类不要补值 —— 给已退役模型填上限是空操作，该做的是下线那些行。

改法（外科式，五道门都在脚本里）：
  OLD=<旧值> NEW=<官方值> EXPECT=<行数> TARGETS="'name-a','name-b'" \\
    bash .claude/skills/litellm-context-window-govern/scripts/db_patch_window_generic.sh --apply
  （OLD 不给 ⇒ 条件是 is null，只补空键；给了 ⇒ 替换该旧值。两种都幂等。）

改完**不需要 rollout**：2026-09-06 与 09-25 两次实测，model_info 改完 4 副本靠 DB
轮询在 90~100 秒内自行收敛。先只读 /model/info 逐副本轮询等收敛，收敛不了再谈滚。
回归：scripts/regress_window.py，必须带闸门拦截腿（只有放行腿 = 没证明闸门在工作），
基准线 cursor-web-fc-82-terra。""")
sys.exit(1)
PY
