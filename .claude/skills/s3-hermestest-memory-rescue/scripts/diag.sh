#!/usr/bin/env bash
# scripts/diag.sh — diagnostic snapshot for hermestest-N S3 container
# Usage: bash scripts/diag.sh <N>
#
# Reports:
#   1. container health + uptime + restart count
#   2. resource usage (CPU, mem)
#   3. memory dir layout (sqlite files + sizes + WAL state)
#   4. sessions dir state (active scannable bytes / files / top 5 largest)
#   5. recent log signal counts (24h window)
#
# Exit 0 always (diagnostic only, never modifies state).

set -u
N="${1:?usage: diag.sh <N>}"
CONT="hermestest-$N"
JMS="${JMS:-scripts/jms}"

echo "=================================================================="
echo "  hermestest-$N diagnostic snapshot @ $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=================================================================="

echo
echo "--- container ---"
"$JMS" ssh JSZX-AI-03 "
  docker ps --filter name=^${CONT}\$ --format '{{.Names}}\t{{.Status}}'
  docker inspect $CONT --format 'StartedAt={{.State.StartedAt}}  RestartCount={{.RestartCount}}  OOMKilled={{.State.OOMKilled}}  ExitCode={{.State.ExitCode}}'
  docker stats --no-stream --format '{{.Name}}  CPU={{.CPUPerc}}  Mem={{.MemUsage}} ({{.MemPerc}})' $CONT
"

echo
echo "--- memory dir (sqlite + WAL + backups) ---"
"$JMS" ssh JSZX-AI-03 "docker exec $CONT ls -lh /data/.openclaw/memory/ | grep -v '^total'"

echo
echo "--- sessions dir ---"
"$JMS" ssh JSZX-AI-03 "docker exec $CONT bash -c '
  echo \"total: \$(du -sh /data/.openclaw/agents/main/sessions/ | cut -f1)\"
  echo
  echo \"active scannable (excluding *.archive.*):\"
  find /data/.openclaw/agents/main/sessions -maxdepth 1 \( -name \"*.jsonl\" -o -name \"*.json\" \) ! -name \"*.archive.*\" -printf \"%s\n\" \
    | awk \"BEGIN{n=0;s=0} {n++; s+=\\\$1} END{printf \\\"  %d files, %.0f MB\n\\\", n, s/1024/1024}\"
  echo
  echo \"top 5 active files by size:\"
  find /data/.openclaw/agents/main/sessions -maxdepth 1 \( -name \"*.jsonl\" -o -name \"*.json\" \) ! -name \"*.archive.*\" -printf \"%s %T@ %p\n\" \
    | sort -rn | head -5 \
    | awk \"{ printf \\\"  %6.1f MB  mtime=%s  %s\n\\\", \\\$1/1024/1024, strftime(\\\"%Y-%m-%d %H:%M\\\", \\\$2), \\\$3 }\"
  echo
  stale_locks=\$(find /data/.openclaw/agents/main/sessions -maxdepth 1 -name \"*.lock\" -mmin +60 2>/dev/null | wc -l)
  echo \"stale .lock files (mmin +60): \$stale_locks\"
'"

echo
echo "--- sqlite internals (chunks / embedding_cache / freelist) ---"
"$JMS" ssh JSZX-AI-03 "docker exec $CONT python3 -c '
import sqlite3
c = sqlite3.connect(\"file:/data/.openclaw/memory/main.sqlite?mode=ro\", uri=True)
for t in (\"chunks\",\"embedding_cache\",\"files\",\"chunks_fts_content\"):
    try:
        n = c.execute(f\"SELECT count(*) FROM \\\"{t}\\\"\").fetchone()[0]
        print(f\"  {t:25s} {n} rows\")
    except Exception as e:
        print(f\"  {t:25s} ERR: {e}\")
try:
    n, mb = c.execute(\"SELECT count(*), COALESCE(SUM(LENGTH(embedding))/1024/1024,0) FROM embedding_cache\").fetchone()
    print(f\"  embedding_cache size      {mb} MB\")
except Exception as e:
    print(f\"  embedding_cache size      ERR: {e}\")
ps = c.execute(\"PRAGMA page_size\").fetchone()[0]
pc = c.execute(\"PRAGMA page_count\").fetchone()[0]
fl = c.execute(\"PRAGMA freelist_count\").fetchone()[0]
print(f\"  total {pc*ps//1024//1024} MB, freelist {fl*ps//1024//1024} MB ({100*fl//max(pc,1)}%)\")
'"

echo
echo "--- log signals (last 24h) ---"
"$JMS" ssh JSZX-AI-03 "
  LOGS=\$(docker logs --since 24h $CONT 2>&1)
  for pat in \\
    'surface_error' \\
    'All models failed' \\
    'incomplete turn detected' \\
    'context overflow detected' \\
    'auto-compaction succeeded' \\
    'SessionWriteLockTimeoutError' \\
    'lane task error' \\
    'task timed out after' \\
    '\\[ws\\].*reconnect' \\
    'liveness warning' \\
    'model idle timeout' \\
    'Something went wrong'
  do
    c=\$(echo \"\$LOGS\" | grep -cE \"\$pat\")
    printf '  %-35s %s\n' \"\$pat\" \"\$c\"
  done
  echo
  echo '  worst eventLoopDelayMaxMs in last 24h:'
  echo \"\$LOGS\" | grep 'liveness warning' | grep -oE 'eventLoopDelayMaxMs=[0-9.]+' | sort -t= -k2 -nr | head -3 | sed 's/^/    /'
"

echo
echo "--- recent feishu activity (last 5 received messages) ---"
"$JMS" ssh JSZX-AI-03 "docker logs --since 24h $CONT 2>&1 | grep '\\[feishu\\]' | grep -E 'received message|message in chat' | tail -5"

echo
echo "=================================================================="
echo "  done. Compare against thresholds in SKILL.md."
echo "=================================================================="
