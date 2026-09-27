#!/usr/bin/env bash
# 逐副本读 /model/info 里目标行的窗口分布 —— DB 改完等轮询收敛用，也是回归判据 1。
# 多副本必须全查：只查一个 = 另一个可能还在跑旧值（2026-09-06 就出现过 3/4 先收敛）。
#
# 用法：
#   NAMES='chatgpt-gpt-6-astra' bash modelinfo_per_replica.sh
#   NAMES='zerokey-pool-gpt-5.6-luna,zerokey-pool-gpt-5.6-sol' bash modelinfo_per_replica.sh
#   KUBECTL='sudo kubectl' NAMES=... bash modelinfo_per_replica.sh      # 198 上要 sudo
#
# 2026-09-25 修两个坑：
#   ① 名字原来焊死在脚本里（chatgpt-gpt-6-astra），改别的行时它还在读 astra ⇒ 假绿。
#   ② 结尾是 `| grep astra`：换了名字就一行都不匹配，输出空 —— 而**空输出不是阴性结论**，
#      看着像"这个副本没有这一行"。现在打印行 + 副本计数，一个副本读不到就显式喊出来。
set -uo pipefail
NS="${NS:-litellm-product}"
NAMES="${NAMES:?需要 NAMES（逗号分隔的 model_name）}"
if [[ -z "${KUBECTL:-}" ]]; then KUBECTL="kubectl"; fi

PY=$(cat <<'PYSRC'
import os, json, urllib.request
from collections import Counter
names = [n.strip() for n in os.environ["NAMES"].split(",") if n.strip()]
r = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:4000/model/info",
    headers={"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"]}), timeout=60)
d = json.load(r)["data"]
for n in names:
    c = Counter(str((m.get("model_info") or {}).get("max_input_tokens"))
                for m in d if m.get("model_name") == n)
    print(f"MIT {n} {dict(c) if c else 'NO-SUCH-ROW'}")
PYSRC
)
B64=$(printf '%s' "$PY" | base64 | tr -d '\n')

PODS=$($KUBECTL -n "$NS" get po -l app=litellm-proxy --no-headers -o name | cut -d/ -f2)
[[ -n "$PODS" ]] || { echo "ERROR: 一个 proxy pod 都没找到，本轮作废" >&2; exit 2; }
SEEN=0; SILENT=0
for P in $PODS; do
  SEEN=$((SEEN+1))
  OUT=$($KUBECTL -n "$NS" exec "$P" -- env NAMES="$NAMES" bash -c \
        "echo $B64 | base64 -d > /tmp/mit_replica.py && python3 /tmp/mit_replica.py" 2>/dev/null \
        | grep '^MIT ')
  if [[ -z "$OUT" ]]; then
    echo "$P: ⚠️ 读不到（空输出不是阴性结论，别当成「这个副本没这行」）"; SILENT=$((SILENT+1))
  else
    echo "$OUT" | sed "s|^|$P: |"
  fi
done
echo "---- 副本 $SEEN 个，其中读取失败 $SILENT 个"
[[ "$SILENT" -eq 0 ]] || exit 2
