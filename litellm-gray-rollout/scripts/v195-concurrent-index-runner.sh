#!/bin/sh
# Fixed-purpose runner for the v1.95 SpendLogToolIndex start_time index.

set -eu

MODE=${1:-inspect}
INDEX_NAME=LiteLLM_SpendLogToolIndex_start_time_idx
TABLE_NAME=LiteLLM_SpendLogToolIndex
COLUMN_NAME=start_time
RESULT_PATH=${RESULT_PATH:-/evidence/v195-index-result.json}
PREFLIGHT_JSON=null
CLEANUP_JSON=null

fail() {
  reason=$1
  write_result FAIL unknown "$reason"
  printf 'v195-concurrent-index-runner: %s\n' "$reason" >&2
  exit 1
}

require_integer() {
  name=$1
  value=$(eval "printf '%s' \"\${$name:-}\"")
  case "$value" in
    ''|*[!0-9]*) fail "${name}_missing_or_not_an_integer" ;;
  esac
}

require_binding() {
  test -n "${DATABASE_URL:-}" || fail DATABASE_URL_missing
  case "${GRAY_RUN_ID:-}" in *[!A-Za-z0-9._:-]*|'') fail run_id_invalid ;; esac
  case "${GRAY_GENERATION:-}" in *[!A-Za-z0-9._:-]*|'') fail generation_invalid ;; esac
  # Count the digest instead of spelling out 64 glob groups: the hand-written
  # pattern this replaces had only 61, so it rejected every real sha256.
  approval=${GRAY_INDEX_APPROVAL_SHA256:-}
  case "$approval" in
    sha256:*) digest=${approval#sha256:} ;;
    *) fail approval_checksum_invalid ;;
  esac
  case "$digest" in
    ''|*[!0-9a-f]*) fail approval_checksum_invalid ;;
  esac
  test "${#digest}" -eq 64 || fail approval_checksum_invalid
}

index_state() {
  psql "$DATABASE_URL" -X -qAt -v ON_ERROR_STOP=1 <<'SQL'
WITH candidate AS (
  SELECT
    i.indisvalid,
    i.indisready,
    i.indislive,
    i.indisunique,
    i.indpred IS NULL AS no_predicate,
    i.indexprs IS NULL AS no_expression,
    am.amname,
    tn.nspname AS table_schema,
    t.relname AS table_name,
    array_agg(a.attname ORDER BY keys.ordinality) AS key_columns
  FROM pg_class idx
  JOIN pg_namespace n ON n.oid = idx.relnamespace
  JOIN pg_index i ON i.indexrelid = idx.oid
  JOIN pg_class t ON t.oid = i.indrelid
  JOIN pg_namespace tn ON tn.oid = t.relnamespace
  JOIN pg_am am ON am.oid = idx.relam
  JOIN unnest(i.indkey) WITH ORDINALITY AS keys(attnum, ordinality) ON true
  JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = keys.attnum
  WHERE n.nspname = 'public'
    AND idx.relname = 'LiteLLM_SpendLogToolIndex_start_time_idx'
  GROUP BY i.indisvalid, i.indisready, i.indislive, i.indisunique,
           i.indpred, i.indexprs, am.amname, tn.nspname, t.relname
)
SELECT CASE
  WHEN NOT EXISTS (SELECT 1 FROM candidate) THEN 'absent'
  WHEN EXISTS (
    SELECT 1 FROM candidate
    WHERE indisvalid AND indisready AND indislive AND NOT indisunique
      AND no_predicate AND no_expression AND amname = 'btree'
      AND table_schema = 'public'
      AND table_name = 'LiteLLM_SpendLogToolIndex'
      AND key_columns = ARRAY['start_time']::name[]
  ) THEN 'valid'
  ELSE 'invalid'
END;
SQL
}

# CREATE INDEX CONCURRENTLY on a 166 GB production database is not a quick DDL: it
# scans the table twice and waits for every transaction older than itself. Four
# conditions turn that into an outage instead of a build, and none of them are
# visible from the index catalog the runner already reads:
#   - a long-running or prepared transaction: the build blocks on it, holds a
#     ShareUpdateExclusiveLock for hours, and blocks VACUUM behind it;
#   - a VACUUM already running on the same table: same lock class, so the build
#     either waits or is waited on;
#   - a stalled autovacuum on LiteLLM_SpendLogs: dead tuples keep piling up for the
#     whole build window, which is how this DB reached disk-pressure 502s before;
#   - not enough free space on the data volume for the new index.
# Probe all of them in one snapshot so the create gate reads measured numbers.
preflight_probe() {
  psql "$DATABASE_URL" -X -qAt -v ON_ERROR_STOP=1 <<'SQL'
SELECT
  coalesce((SELECT max(extract(epoch FROM (now() - xact_start)))::bigint
              FROM pg_stat_activity
             WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid()), 0),
  (SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'),
  (SELECT count(*) FROM pg_prepared_xacts),
  (SELECT count(*) FROM pg_stat_progress_vacuum
    WHERE relid IN (to_regclass('public."LiteLLM_SpendLogToolIndex"'),
                    to_regclass('public."LiteLLM_SpendLogs"'))),
  coalesce((SELECT (100 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0))::bigint
              FROM pg_stat_user_tables
             WHERE schemaname = 'public' AND relname = 'LiteLLM_SpendLogToolIndex'), 0),
  coalesce((SELECT (100 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0))::bigint
              FROM pg_stat_user_tables
             WHERE schemaname = 'public' AND relname = 'LiteLLM_SpendLogs'), 0),
  coalesce((SELECT extract(epoch FROM (now() - greatest(last_vacuum, last_autovacuum)))::bigint
              FROM pg_stat_user_tables
             WHERE schemaname = 'public' AND relname = 'LiteLLM_SpendLogs'), -1),
  coalesce(pg_total_relation_size(to_regclass('public."LiteLLM_SpendLogToolIndex"')), 0);
SQL
}

# Every threshold below is required, with no default. A default here would be a
# number nobody measured, and the create gate would then pass on it.
require_create_headroom() {
  require_integer GRAY_INDEX_MAX_XACT_AGE_SECONDS
  require_integer GRAY_INDEX_MAX_DEAD_TUP_PERCENT
  require_integer GRAY_INDEX_MAX_VACUUM_AGE_SECONDS
  require_integer GRAY_INDEX_FREE_BYTES
  require_integer GRAY_INDEX_REQUIRED_BYTES

  test "$GRAY_INDEX_REQUIRED_BYTES" -gt 0 || fail required_bytes_must_be_measured_on_the_clone
  test "$GRAY_INDEX_FREE_BYTES" -gt 0 || fail free_bytes_must_be_measured_with_df

  test "$xact_age" -le "$GRAY_INDEX_MAX_XACT_AGE_SECONDS" \
    || fail "oldest_transaction_${xact_age}s_exceeds_${GRAY_INDEX_MAX_XACT_AGE_SECONDS}s"
  test "$prepared_xacts" -eq 0 || fail "prepared_transactions_${prepared_xacts}_block_concurrent_build"
  test "$vacuum_running" -eq 0 || fail "vacuum_in_progress_on_${vacuum_running}_target_tables"
  test "$tool_dead_pct" -le "$GRAY_INDEX_MAX_DEAD_TUP_PERCENT" \
    || fail "tool_index_dead_tuples_${tool_dead_pct}pct_exceeds_${GRAY_INDEX_MAX_DEAD_TUP_PERCENT}pct"
  test "$spend_dead_pct" -le "$GRAY_INDEX_MAX_DEAD_TUP_PERCENT" \
    || fail "spendlogs_dead_tuples_${spend_dead_pct}pct_exceeds_${GRAY_INDEX_MAX_DEAD_TUP_PERCENT}pct"
  test "$spend_vacuum_age" -ge 0 || fail spendlogs_never_vacuumed
  test "$spend_vacuum_age" -le "$GRAY_INDEX_MAX_VACUUM_AGE_SECONDS" \
    || fail "spendlogs_vacuum_age_${spend_vacuum_age}s_exceeds_${GRAY_INDEX_MAX_VACUUM_AGE_SECONDS}s"

  headroom=$((GRAY_INDEX_REQUIRED_BYTES * 2))
  test "$GRAY_INDEX_FREE_BYTES" -ge "$headroom" \
    || fail "free_bytes_${GRAY_INDEX_FREE_BYTES}_below_twice_required_${GRAY_INDEX_REQUIRED_BYTES}"
}

write_result() {
  status=$1
  state=$2
  reason=$3
  timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  directory=$(dirname "$RESULT_PATH")
  umask 077
  mkdir -p "$directory"
  test ! -L "$RESULT_PATH" || return 1
  temporary="$RESULT_PATH.tmp.$$"
  cat >"$temporary" <<EOF
{"tool":"v195-concurrent-index-runner","schema_version":2,"status":"$status","mode":"$MODE","state":"$state","reason":"$reason","run_id":"${GRAY_RUN_ID:-unknown}","generation":"${GRAY_GENERATION:-unknown}","approval_sha256":"${GRAY_INDEX_APPROVAL_SHA256:-unknown}","index":"$INDEX_NAME","table":"$TABLE_NAME","column":"$COLUMN_NAME","preflight":$PREFLIGHT_JSON,"cleanup":$CLEANUP_JSON,"captured_at":"$timestamp"}
EOF
  mv -f "$temporary" "$RESULT_PATH"
  sha256sum "$RESULT_PATH" >"$RESULT_PATH.sha256"
}

# A failed CREATE INDEX CONCURRENTLY leaves an INVALID index behind. It is never
# used for reads, it is still maintained on every write, and it blocks the retry
# because `create` demands state=absent. Naming drop-invalid in a reason string
# does not remove it, so remove it here and record what happened either way.
CLEANUP_REASON=no_cleanup_needed
drop_invalid_leftover() {
  leftover=$(index_state) || leftover=inspect_failed
  case "$leftover" in
    invalid) ;;
    *)
      CLEANUP_JSON="{\"attempted\":false,\"state_after_failure\":\"$leftover\"}"
      CLEANUP_REASON="left_state_$leftover"
      return 0
      ;;
  esac
  if psql "$DATABASE_URL" -X -q -v ON_ERROR_STOP=1 \
    -c 'DROP INDEX CONCURRENTLY IF EXISTS public."LiteLLM_SpendLogToolIndex_start_time_idx"'
  then
    final=$(index_state) || final=inspect_failed
    CLEANUP_JSON="{\"attempted\":true,\"dropped\":true,\"state_after_cleanup\":\"$final\"}"
    if [ "$final" = absent ]; then
      CLEANUP_REASON=invalid_index_dropped
    else
      CLEANUP_REASON="invalid_index_still_$final"
    fi
  else
    CLEANUP_JSON='{"attempted":true,"dropped":false,"state_after_cleanup":"invalid"}'
    CLEANUP_REASON=invalid_index_drop_failed_manual_cleanup_required
  fi
  return 0
}

require_binding
probe=$(preflight_probe) || fail preflight_probe_failed
IFS='|' read -r xact_age idle_in_txn prepared_xacts vacuum_running \
  tool_dead_pct spend_dead_pct spend_vacuum_age table_bytes <<EOF
$probe
EOF
for field in "$xact_age" "$idle_in_txn" "$prepared_xacts" "$vacuum_running" \
  "$tool_dead_pct" "$spend_dead_pct" "$spend_vacuum_age" "$table_bytes"; do
  case "$field" in
    ''|*[!0-9-]*) fail preflight_probe_returned_unexpected_shape ;;
  esac
done
PREFLIGHT_JSON="{\"oldest_transaction_seconds\":$xact_age,\"idle_in_transaction\":$idle_in_txn,\"prepared_transactions\":$prepared_xacts,\"vacuum_in_progress_on_targets\":$vacuum_running,\"tool_index_dead_tuple_percent\":$tool_dead_pct,\"spendlogs_dead_tuple_percent\":$spend_dead_pct,\"spendlogs_vacuum_age_seconds\":$spend_vacuum_age,\"tool_index_total_bytes\":$table_bytes,\"declared_free_bytes\":\"${GRAY_INDEX_FREE_BYTES:-unset}\",\"declared_required_bytes\":\"${GRAY_INDEX_REQUIRED_BYTES:-unset}\"}"

before=$(index_state) || fail inspect_failed

case "$MODE" in
  inspect)
    write_result PASS "$before" inspected
    ;;
  create)
    test "$before" = absent || fail "create_requires_absent_found_$before"
    require_create_headroom
    if ! psql "$DATABASE_URL" -X -q -v ON_ERROR_STOP=1 \
      -c 'CREATE INDEX CONCURRENTLY "LiteLLM_SpendLogToolIndex_start_time_idx" ON public."LiteLLM_SpendLogToolIndex" USING btree ("start_time")'
    then
      drop_invalid_leftover
      fail "create_failed_$CLEANUP_REASON"
    fi
    after=$(index_state) || fail post_create_inspect_failed
    test "$after" = valid || fail "post_create_state_$after"
    write_result PASS "$after" created_concurrently
    ;;
  drop-invalid)
    test "$before" = invalid || fail "drop_invalid_requires_invalid_found_$before"
    psql "$DATABASE_URL" -X -q -v ON_ERROR_STOP=1 \
      -c 'DROP INDEX CONCURRENTLY IF EXISTS public."LiteLLM_SpendLogToolIndex_start_time_idx"' \
      || fail drop_invalid_failed
    after=$(index_state) || fail post_drop_inspect_failed
    test "$after" = absent || fail "post_drop_state_$after"
    write_result PASS "$after" invalid_index_removed
    ;;
  *)
    fail unsupported_mode
    ;;
esac

cat "$RESULT_PATH"
