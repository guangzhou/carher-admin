#!/usr/bin/env bash
# scripts/rescue.sh — A + B + C rescue for hermestest-N S3 container
#   A. Archive *.trajectory.jsonl + *.trajectory-path.json older than 7 days
#      (rename to *.archive.<TS>, NOT delete — kept 7 days as safety net)
#   B. sqlite cleanup: VACUUM INTO backup → integrity check → GC embedding_cache → VACUUM
#   C. docker restart + WAL truncate
#
# Usage: bash scripts/rescue.sh <N>
#
# Safe to re-run; idempotent. Total runtime ~3-4 min per container.
# Modifies state: archives sessions, deletes embedding_cache rows >7d old,
# restarts the container (~30s downtime, Feishu SDK auto-reconnects).

set -e
N="${1:?usage: rescue.sh <N>}"
CONT="hermestest-$N"
JMS="${JMS:-scripts/jms}"
DATE=$(date +%Y%m%d)
TS=$(date +%Y%m%d-%H%M)

INNER_SCRIPT=$(mktemp /tmp/rescue-inner.XXXXXX.sh)
trap "rm -f $INNER_SCRIPT" EXIT

cat > "$INNER_SCRIPT" <<INNER
#!/bin/bash
# This script runs INSIDE the container as: bash /tmp/rescue-inner.sh
set -e
DATE=$DATE
TS=$TS
MEM=/data/.openclaw/memory
SES=/data/.openclaw/agents/main/sessions

echo "============== A. session GC (mtime > 7 days) =============="
cd "\$SES"
N=0; SZ=0
for f in *.trajectory.jsonl *.trajectory-path.json; do
  [ -e "\$f" ] || continue
  if [ -n "\$(find "\$f" -mtime +7 -print -quit 2>/dev/null)" ]; then
    sz=\$(stat -c %s "\$f" 2>/dev/null || echo 0)
    mv "\$f" "\$f.archive.\$TS"
    N=\$((N+1)); SZ=\$((SZ+sz))
  fi
done
for f in *.lock; do
  [ -e "\$f" ] || continue
  if [ -n "\$(find "\$f" -mmin +60 -print -quit 2>/dev/null)" ]; then
    rm -f "\$f"
  fi
done
echo "  archived \$N session files (\$((SZ/1024/1024)) MB), removed stale .lock files"
echo "  sessions dir total: \$(du -sh "\$SES" | cut -f1)"

echo
echo "============== B. sqlite cleanup =============="
echo "--- B1. VACUUM INTO backup ---"
python3 - <<PY
import sqlite3, os, time
src = "\$MEM/main.sqlite"
bak = "\$MEM/main.sqlite.bak.\$DATE"
if os.path.exists(bak):
    print(f"  existing backup, replacing: {bak}")
    os.remove(bak)
c = sqlite3.connect(src, timeout=60)
t = time.time()
c.execute(f"VACUUM INTO '{bak}'")
print(f"  done in {time.time()-t:.1f}s -> {os.path.getsize(bak)//1024//1024} MB")
PY

echo "--- B2. integrity check on backup ---"
python3 - <<PY
import sqlite3
c = sqlite3.connect(f"file:\$MEM/main.sqlite.bak.\$DATE?mode=ro", uri=True)
r = c.execute("PRAGMA integrity_check").fetchone()
print(f"  integrity: {r}")
if r[0] != "ok":
    raise SystemExit("ABORT: backup not ok, do not continue")
for t in ("chunks","embedding_cache","files","chunks_fts_content"):
    n = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
    print(f"  {t}: {n}")
PY

echo "--- B3. embedding_cache GC (>7d) ---"
python3 - <<PY
import sqlite3, time
c = sqlite3.connect("\$MEM/main.sqlite", timeout=60)
# updated_at is milliseconds (NOT seconds — verified by inspecting min/max in real DB)
cutoff_ms = int((time.time() - 7*86400) * 1000)
b = c.execute("SELECT count(*), COALESCE(SUM(LENGTH(embedding))/1024/1024, 0) FROM embedding_cache").fetchone()
print(f"  before: {b[0]} rows, {b[1]} MB")
t = time.time()
r = c.execute("DELETE FROM embedding_cache WHERE updated_at < ?", (cutoff_ms,))
c.commit()
print(f"  DELETE done in {time.time()-t:.1f}s, rows deleted: {r.rowcount}")
a = c.execute("SELECT count(*), COALESCE(SUM(LENGTH(embedding))/1024/1024, 0) FROM embedding_cache").fetchone()
print(f"  after:  {a[0]} rows, {a[1]} MB")
PY

echo "--- B4. VACUUM main ---"
python3 - <<PY
import sqlite3, time
c = sqlite3.connect("\$MEM/main.sqlite", timeout=120)
t = time.time()
c.execute("VACUUM")
print(f"  done in {time.time()-t:.1f}s")
PY

echo "--- B5. final memory dir ---"
ls -lh "\$MEM"
INNER

echo "=================================================================="
echo "  Rescuing $CONT @ $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=================================================================="

# Stage script onto JSZX-AI-03 → into container, run
"$JMS" scp "$INNER_SCRIPT" "JSZX-AI-03:/tmp/rescue-inner.sh"
"$JMS" ssh JSZX-AI-03 "docker cp /tmp/rescue-inner.sh $CONT:/tmp/rescue-inner.sh && docker exec $CONT bash /tmp/rescue-inner.sh"

echo
echo "============== C. docker restart ($CONT) =============="
"$JMS" ssh JSZX-AI-03 "
  docker restart $CONT
  echo '  waiting healthy...'
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 5
    s=\$(docker inspect -f '{{.State.Health.Status}}' $CONT 2>/dev/null)
    echo \"    \${i}x5s: health=\$s\"
    [ \"\$s\" = healthy ] && break
  done
"

echo
echo "--- C2. WAL truncate (must do manually — docker restart does NOT auto-truncate) ---"
"$JMS" ssh JSZX-AI-03 "docker exec $CONT python3 -c '
import sqlite3, time
c = sqlite3.connect(\"/data/.openclaw/memory/main.sqlite\", timeout=60)
print(\"  journal_mode:\", c.execute(\"PRAGMA journal_mode\").fetchone())
t = time.time()
r = c.execute(\"PRAGMA wal_checkpoint(TRUNCATE)\").fetchone()
print(f\"  wal_checkpoint TRUNCATE in {time.time()-t:.1f}s -> busy={r[0]} log={r[1]} ckpt={r[2]}\")
'"

echo
echo "--- final state ---"
"$JMS" ssh JSZX-AI-03 "docker exec $CONT ls -lh /data/.openclaw/memory/ | grep -v '^total'"

echo
echo "=================================================================="
echo "  rescue done. Run scripts/diag.sh $N in 10+ minutes to verify."
echo "  Backup kept at /data/.openclaw/memory/main.sqlite.bak.$DATE"
echo "  (safe to delete after 7 days of stable operation)"
echo "=================================================================="
