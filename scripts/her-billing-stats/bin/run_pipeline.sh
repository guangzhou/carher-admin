#!/usr/bin/env bash
# End-to-end pipeline: inventory -> fetch active pods (parallel) -> scan paused PVCs ->
#                      fetch registration map -> build rows -> write to Bitable -> review
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${WORK_DIR:-$(pwd)/her-billing-run-$(date +%Y%m%d-%H%M%S)}"
NAMESPACE="${NAMESPACE:-carher}"
PARALLEL="${PARALLEL:-4}"
IDENTITY="${IDENTITY:-bot}"
REPLACE_EXISTING="${REPLACE_EXISTING:-1}"  # 1 = delete rows with 账户类型 OpenClaw-* before insert

mkdir -p "$WORK_DIR"/{stats,reg}
cd "$WORK_DIR"

echo "== Work dir: $WORK_DIR =="

echo "[1/6] inventory"
python3 "$SCRIPT_DIR/inventory.py" --namespace "$NAMESPACE" --out inventory.json
cat inventory.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'  active={len(d[\"active\"])} paused={len(d[\"paused\"])} total={len(d[\"all_uids\"])}')
"

echo "[2/6] fetch from active pods (parallel=$PARALLEL)"
python3 -c "
import json
d=json.load(open('inventory.json'))
for u,p in sorted(d['active'].items(), key=lambda x:int(x[0])):
    print(u,p)
" > active_pairs.txt
echo "  $(wc -l < active_pairs.txt) active pods"

OUT_DIR="$WORK_DIR/stats" \
NAMESPACE="$NAMESPACE" \
xargs -n 2 -P "$PARALLEL" -a active_pairs.txt \
  bash -c '"$0" "$@"' "$SCRIPT_DIR/run_one_pod.sh" 2>&1 | tee active_run.log | tail -5

echo "[3/6] scan paused PVCs (offline)"
python3 "$SCRIPT_DIR/scan_paused_pvcs.py" \
  --namespace "$NAMESPACE" \
  --inventory inventory.json \
  --out-dir "$WORK_DIR/stats" \
  --script "$SCRIPT_DIR/her-cost-stats.js"

echo "[4/6] fetch registration map"
python3 "$SCRIPT_DIR/fetch_registration.py" --out-dir "$WORK_DIR/reg" --as "$IDENTITY"

echo "[5/6] build rows + write to Bitable"
python3 "$SCRIPT_DIR/build_rows.py" \
  --stats-dir "$WORK_DIR/stats" \
  --uid-to-person "$WORK_DIR/reg/uid_to_person.json" \
  --out "$WORK_DIR/openclaw_rows.jsonl"

if [ "$REPLACE_EXISTING" = "1" ]; then
  python3 "$SCRIPT_DIR/write_to_bitable.py" \
    --rows "$WORK_DIR/openclaw_rows.jsonl" \
    --as "$IDENTITY" \
    --replace-category "OpenClaw-"
else
  python3 "$SCRIPT_DIR/write_to_bitable.py" \
    --rows "$WORK_DIR/openclaw_rows.jsonl" \
    --as "$IDENTITY"
fi

echo "[6/6] review"
python3 "$SCRIPT_DIR/review.py" \
  --stats-dir "$WORK_DIR/stats" \
  --uid-to-person "$WORK_DIR/reg/uid_to_person.json" \
  --as "$IDENTITY"

echo
echo "== Pipeline complete. Artifacts in: $WORK_DIR =="
