#!/bin/bash
# 深度扫描单个 carher pod（基于 her-68 诊断经验扩充）。
# 在 scan_one.sh 字段基础上额外采集：
#   - TMP_OLDEST_AGE_H : 最老 tmp 文件的小时年龄（卡多久了）
#   - CHUNKS           : main.sqlite chunks 表行数
#   - EMB_MB           : main.sqlite chunks.embedding 列总 MB
#   - EC_ROWS / EC_MB  : embedding_cache 行数与字节
#   - TMP_CHUNKS       : 任一活 tmp 内 chunks 行数（reindex 进度）
#   - TMP_HAS_META     : tmp 是否已写完 meta（YES/NO）
#   - WS_READY         : feishu-ws-ready ReadinessGate（YES/NO/UNKNOWN）
#   - POD_AGE_M        : pod 启动至今分钟数
#   - PROVIDER_KEY     : meta.providerKey 前 12 字符
#   - PROVIDER_MODEL   : meta.provider/model
# 单 pod 耗时 ~5-10s（vs scan_one ~1.7s）。串行 200 pod ≈ 20-30 min。
#
# 输出 TSV 一行（tab 分隔），列顺序固定：
#   POD HID RESTARTS LAST_OOM POD_AGE_M WS_READY \
#   MAIN_MB MAIN_AGE_H \
#   TMP_COUNT TMP_ACTIVE TMP_MB TMP_OLDEST_AGE_H \
#   CHUNKS EMB_MB EC_ROWS EC_MB TMP_CHUNKS TMP_HAS_META \
#   PROVIDER_MODEL PROVIDER_KEY \
#   MEM_MB STATUS

set -u
POD="${1:-}"
[ -z "$POD" ] && { echo "ERROR: need pod name" >&2; exit 1; }

HID=$(echo "$POD" | sed -n 's/^carher-\([0-9][0-9]*\)-.*/\1/p')

# 读 pod meta 一次性算好 RESTARTS / LAST_OOM / POD_AGE_M / WS_READY
META=$(kubectl get pod -n carher "$POD" -o json 2>/dev/null)
if [ -z "$META" ]; then
  printf '%s\t%s\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\tEXEC_FAIL\n' "$POD" "$HID"
  exit 0
fi

RESTARTS=$(echo "$META" | python3 -c "
import sys, json
try:
    p = json.load(sys.stdin)
    for c in p.get('status', {}).get('containerStatuses', []):
        if c['name'] == 'carher':
            print(c.get('restartCount', 0)); break
    else:
        print(0)
except Exception:
    print(0)
" 2>/dev/null)

LAST_OOM=$(echo "$META" | python3 -c "
import sys, json
try:
    p = json.load(sys.stdin)
    for c in p.get('status', {}).get('containerStatuses', []):
        if c['name'] == 'carher':
            ls = c.get('lastState', {}).get('terminated', {})
            print(ls.get('finishedAt', '-') if ls.get('reason') == 'OOMKilled' else '-')
            break
    else:
        print('-')
except Exception:
    print('-')
" 2>/dev/null)

POD_AGE_M=$(echo "$META" | python3 -c "
import sys, json, datetime
try:
    p = json.load(sys.stdin)
    st = p.get('status', {}).get('startTime')
    if not st:
        print('-'); sys.exit()
    t = datetime.datetime.strptime(st, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    delta = datetime.datetime.now(datetime.timezone.utc) - t
    print(int(delta.total_seconds() / 60))
except Exception:
    print('-')
" 2>/dev/null)

WS_READY=$(echo "$META" | python3 -c "
import sys, json
try:
    p = json.load(sys.stdin)
    for c in p.get('status', {}).get('conditions', []):
        if c.get('type') == 'carher.io/feishu-ws-ready':
            print('YES' if c.get('status') == 'True' else 'NO')
            break
    else:
        print('UNKNOWN')
except Exception:
    print('UNKNOWN')
" 2>/dev/null)

# 在 pod 内一次性采集所有静态文件 + sqlite 内部状态
OUT=$(kubectl exec -n carher "$POD" -c carher --request-timeout=30s -- sh -c '
DIR=/data/.openclaw/memory
[ -d "$DIR" ] || { echo "STATUS=NO_DIR"; exit 0; }
cd "$DIR"

# 主库 / tmp 文件统计
main_size=$(stat -c%s main.sqlite 2>/dev/null || echo 0)
main_mtime=$(stat -c%Y main.sqlite 2>/dev/null || echo 0)
tmp_count=0
tmp_active=0
tmp_size=0
tmp_oldest=0  # 最老的 mtime（unix 秒）
newest_active_tmp=""
for f in main.sqlite.tmp-*; do
  [ -e "$f" ] || continue
  tmp_count=$((tmp_count+1))
  s=$(stat -c%s "$f" 2>/dev/null || echo 0)
  mt=$(stat -c%Y "$f" 2>/dev/null || echo 0)
  tmp_size=$((tmp_size+s))
  if [ "$tmp_oldest" -eq 0 ] || [ "$mt" -lt "$tmp_oldest" ]; then
    tmp_oldest=$mt
  fi
  fd=$(ls -la /proc/*/fd/ 2>/dev/null | grep -c "$f" || true)
  fd=${fd:-0}
  if [ "$fd" != "0" ]; then
    tmp_active=$((tmp_active+1))
    newest_active_tmp="$f"
  fi
done

mem=$(cat /sys/fs/cgroup/memory.current 2>/dev/null \
  || cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null \
  || echo 0)

echo "STATUS=OK"
echo "MAIN_SIZE=$main_size"
echo "MAIN_MTIME=$main_mtime"
echo "TMP_COUNT=$tmp_count"
echo "TMP_ACTIVE=$tmp_active"
echo "TMP_SIZE=$tmp_size"
echo "TMP_OLDEST=$tmp_oldest"
echo "NEWEST_ACTIVE_TMP=$newest_active_tmp"
echo "MEM=$mem"

# 读 sqlite 内部状态（必须能 readOnly 打开，活 pod 是安全的）
TMP_PROBE="$newest_active_tmp" node -e "
const sqlite = require(\"node:sqlite\");
function inspect(label, path) {
  try {
    const db = new sqlite.DatabaseSync(path, {readOnly: true});
    let chunks = -1, embMB = -1, ecRows = -1, ecMB = -1, hasMeta = 0, pk = \"-\", model = \"-\", provider = \"-\";
    try {
      const m = db.prepare(\"SELECT value FROM meta WHERE key=?\").get(\"memory_index_meta_v1\");
      if (m && m.value) {
        hasMeta = 1;
        const parsed = JSON.parse(m.value);
        pk = (parsed.providerKey || \"-\").slice(0, 12);
        model = parsed.model || \"-\";
        provider = parsed.provider || \"-\";
      }
    } catch(e) {}
    try {
      const r = db.prepare(\"SELECT COUNT(*) c, SUM(LENGTH(embedding)) b FROM chunks WHERE embedding IS NOT NULL\").get();
      chunks = r.c || 0;
      embMB = Math.round((r.b || 0) / 1048576);
    } catch(e) {}
    try {
      const r = db.prepare(\"SELECT COUNT(*) c, SUM(LENGTH(embedding)) b FROM embedding_cache\").get();
      ecRows = r.c || 0;
      ecMB = Math.round((r.b || 0) / 1048576);
    } catch(e) {}
    db.close();
    console.log(label + \"_CHUNKS=\" + chunks);
    console.log(label + \"_EMB_MB=\" + embMB);
    console.log(label + \"_EC_ROWS=\" + ecRows);
    console.log(label + \"_EC_MB=\" + ecMB);
    console.log(label + \"_HAS_META=\" + hasMeta);
    console.log(label + \"_PK=\" + pk);
    console.log(label + \"_MODEL=\" + model);
    console.log(label + \"_PROVIDER=\" + provider);
  } catch(e) {
    console.log(label + \"_ERR=\" + e.message.slice(0, 60));
  }
}
inspect(\"MAIN\", \"/data/.openclaw/memory/main.sqlite\");
const tmp = process.env.TMP_PROBE;
if (tmp) inspect(\"TMP\", \"/data/.openclaw/memory/\" + tmp);
" 2>/dev/null
' 2>&1)

STATUS=$(echo "$OUT" | grep '^STATUS=' | head -1 | cut -d= -f2)
if [ "$STATUS" != "OK" ]; then
  printf '%s\t%s\t%s\t%s\t%s\t%s\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t%s\n' \
    "$POD" "$HID" "$RESTARTS" "$LAST_OOM" "${POD_AGE_M:-0}" "${WS_READY:-UNKNOWN}" "${STATUS:-EXEC_FAIL}"
  exit 0
fi

# 用 awk 解析所有 KEY=VAL（避免 grep | cut 多次重复）
parse() { echo "$OUT" | awk -F= -v k="$1" '$1==k {print $2; exit}'; }

MAIN_SIZE=$(parse MAIN_SIZE)
MAIN_MTIME=$(parse MAIN_MTIME)
TMP_COUNT=$(parse TMP_COUNT)
TMP_ACTIVE=$(parse TMP_ACTIVE)
TMP_SIZE=$(parse TMP_SIZE)
TMP_OLDEST=$(parse TMP_OLDEST)
MEM=$(parse MEM)

CHUNKS=$(parse MAIN_CHUNKS)
EMB_MB=$(parse MAIN_EMB_MB)
EC_ROWS=$(parse MAIN_EC_ROWS)
EC_MB=$(parse MAIN_EC_MB)
PROVIDER_MODEL="$(parse MAIN_PROVIDER)/$(parse MAIN_MODEL)"
PROVIDER_KEY=$(parse MAIN_PK)

# tmp 内部状态（如果有 active tmp）
TMP_CHUNKS=$(parse TMP_CHUNKS)
TMP_HAS_META=$(parse TMP_HAS_META)
[ -z "$TMP_CHUNKS" ] && TMP_CHUNKS="-"
[ -z "$TMP_HAS_META" ] && TMP_HAS_META="-"

NOW=$(date +%s)
AGE_H=$(awk -v now="$NOW" -v m="$MAIN_MTIME" 'BEGIN{ if(m==0){print "-"} else {printf "%.1f", (now-m)/3600} }')
TMP_OLDEST_AGE_H=$(awk -v now="$NOW" -v m="$TMP_OLDEST" 'BEGIN{ if(m==0){print "-"} else {printf "%.1f", (now-m)/3600} }')
MAIN_MB=$(awk -v s="$MAIN_SIZE" 'BEGIN{printf "%.0f", s/1048576}')
TMP_MB=$(awk -v s="$TMP_SIZE" 'BEGIN{printf "%.0f", s/1048576}')
MEM_MB=$(awk -v s="$MEM" 'BEGIN{printf "%.0f", s/1048576}')

# 输出固定列序
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\tOK\n' \
  "$POD" "$HID" \
  "$RESTARTS" "$LAST_OOM" "${POD_AGE_M:-0}" "${WS_READY:-UNKNOWN}" \
  "$MAIN_MB" "$AGE_H" \
  "$TMP_COUNT" "$TMP_ACTIVE" "$TMP_MB" "$TMP_OLDEST_AGE_H" \
  "${CHUNKS:-0}" "${EMB_MB:-0}" "${EC_ROWS:-0}" "${EC_MB:-0}" "$TMP_CHUNKS" "$TMP_HAS_META" \
  "${PROVIDER_MODEL:--/-}" "${PROVIDER_KEY:--}" \
  "$MEM_MB"
