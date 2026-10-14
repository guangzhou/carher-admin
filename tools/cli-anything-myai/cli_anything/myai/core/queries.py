"""Read-only SQL catalog against the dashboard's Postgres (usage-db-0).

Table semantics come from the ai_token_dashboard source at 2818c0d:
- usage_sync_runs          one row per sync attempt (status ok/partial/failed/skipped/running)
- usage_sync_state         singleton worker heartbeat; last_success_at only moves on status=ok
- usage_snapshot_state     singleton; published_at = last snapshot publish (partial still publishes
                           the backends that passed the quality gate)
- usage_refresh_requests   durable refresh queue; pending rows retry with backoff
- usage_realtime_settlement per-backend realtime verified_through
- billing_order / organization_billing_order / customer_outbox  billing + async upstream sync
"""

from __future__ import annotations

import re


def _literal(value: str) -> str:
    """Quote a text literal for psql; the transaction is read-only regardless."""
    return "'" + value.replace("'", "''") + "'"


def _int(value: int, *, low: int = 1, high: int = 3650) -> int:
    return max(low, min(high, int(value)))


def sync_state() -> str:
    return """
SELECT s.worker_id, s.status, s.heartbeat_at, s.last_started_at, s.last_finished_at,
       s.last_success_at, s.last_error, s.snapshot_revision,
       p.published_at, p.start_date AS published_start, p.end_date AS published_end,
       array_to_string(p.backend_ids, ',') AS published_backends
FROM usage_sync_state s CROSS JOIN usage_snapshot_state p
"""


def sync_runs_by_day(days: int) -> str:
    return f"""
SELECT date_trunc('day', started_at)::date AS day, status, count(*) AS runs,
       min(start_date) AS min_start, max(end_date) AS max_end,
       left(max(error_message), 120) AS sample_error
FROM usage_sync_runs
WHERE started_at > now() - interval '{_int(days)} days'
GROUP BY 1, 2 ORDER BY 1 DESC, 2
"""


def sync_runs_recent(limit: int) -> str:
    return f"""
SELECT id, started_at, finished_at, start_date, end_date, status, backend_count, row_count,
       left(error_message, 160) AS error_message
FROM usage_sync_runs ORDER BY started_at DESC LIMIT {_int(limit, high=500)}
"""


def sync_errors(days: int) -> str:
    return f"""
SELECT error_message, status, count(*) AS runs, max(started_at) AS last_seen
FROM usage_sync_runs
WHERE started_at > now() - interval '{_int(days)} days' AND error_message <> ''
GROUP BY 1, 2 ORDER BY runs DESC LIMIT 20
"""


def refresh_queue(include_done: bool) -> str:
    where = "" if include_done else "WHERE status <> 'completed'"
    return f"""
SELECT request_key, start_date, end_date, status, requested_at, attempts,
       next_attempt_at, round(last_duration_ms) AS last_duration_ms, left(last_error, 120) AS last_error
FROM usage_refresh_requests {where}
ORDER BY requested_at LIMIT 200
"""


def realtime_settlement() -> str:
    return """
SELECT backend_id, status, verified_through,
       extract(epoch FROM now() - verified_through)::int AS lag_seconds,
       left(last_error, 120) AS last_error, updated_at
FROM usage_realtime_settlement ORDER BY backend_id
"""


def coverage(days: int) -> str:
    return f"""
SELECT usage_date, string_agg(backend_id || '@' || to_char(synced_at, 'MM-DD HH24:MI'), ' ' ORDER BY backend_id) AS synced
FROM usage_sync_coverage
WHERE usage_date > current_date - {_int(days)}
GROUP BY 1 ORDER BY 1 DESC
"""


def usage_daily_totals(days: int, snapshot_only: bool = False) -> str:
    """Default reads ``usage_query_daily`` — the view the dashboard itself reads:
    historical snapshot rows, overridden by realtime rows once a day's realtime
    mirror is ready+complete. ``snapshot_only`` reads the raw ``usage_daily``
    table, whose newest date shows how far snapshot publishing has got."""
    source = "usage_daily" if snapshot_only else "usage_query_daily"
    return f"""
SELECT usage_date, backend_id, count(DISTINCT user_id) AS users,
       sum(request_count) AS requests, round(sum(spend)::numeric, 2) AS spend,
       max(collected_at) AS collected_at
FROM {source}
WHERE usage_date > current_date - {_int(days)}
GROUP BY 1, 2 ORDER BY 1 DESC, 2
"""


def view_daily_by_backend(backend: str, start: str, end: str) -> str:
    """What the dashboard shows per UTC-aligned usage_date, plus which source fed it."""
    return f"""
SELECT q.usage_date, sum(q.request_count) AS requests, round(sum(q.spend)::numeric, 2) AS spend,
       max(q.collected_at) AS collected_at
FROM usage_query_daily q
WHERE q.backend_id = {_literal(backend)} AND q.usage_date BETWEEN {_literal(start)} AND {_literal(end)}
GROUP BY 1 ORDER BY 1
"""


def snapshot_frontier() -> str:
    return """
SELECT backend_id, max(usage_date) AS last_snapshot_day, max(collected_at) AS last_collected_at
FROM usage_daily GROUP BY 1 ORDER BY 1
"""


def billing_orders(status: str | None, limit: int) -> str:
    where = f"WHERE status = {_literal(status)}" if status else ""
    return f"""
SELECT 'personal' AS kind, trade_no, user_id AS owner, channel, amount_usd, money_cny, status,
       sync_state, left(sync_error, 100) AS sync_error, created_at, submitted_at, completed_at
FROM billing_order {where}
UNION ALL
SELECT 'organization', trade_no, organization_id, channel, amount_usd, money_cny, status,
       sync_state, left(sync_error, 100), created_at, submitted_at, completed_at
FROM organization_billing_order {where}
ORDER BY created_at DESC LIMIT {_int(limit, high=500)}
"""


def outbox(include_sent: bool) -> str:
    where = "" if include_sent else "WHERE status <> 'sent'"
    return f"""
SELECT id, kind, aggregate_id, status, attempts, available_at, sent_at,
       left(last_error, 160) AS last_error, created_at
FROM customer_outbox {where}
ORDER BY created_at DESC LIMIT 200
"""


def identity_find(text: str) -> str:
    pattern = _literal(f"%{text.lower()}%")
    return f"""
SELECT backend_id, user_id, display_name, employee_email, name_source, confidence, updated_at
FROM usage_identity_directory
WHERE lower(user_id) LIKE {pattern} OR lower(display_name) LIKE {pattern}
   OR lower(employee_email) LIKE {pattern}
ORDER BY backend_id, user_id LIMIT 100
"""


def view_daily_by_attribution(backend: str, start: str, end: str) -> str:
    """Dashboard view per day split by attribution_source (key_rollup = filled by key)."""
    return f"""
SELECT usage_date, attribution_source, sum(request_count) AS requests,
       round(sum(spend)::numeric, 2) AS spend, count(DISTINCT user_id) AS users
FROM usage_query_daily
WHERE backend_id = {_literal(backend)} AND usage_date BETWEEN {_literal(start)} AND {_literal(end)}
GROUP BY 1, 2 ORDER BY 1, 2
"""


def table_sizes() -> str:
    return """
SELECT relname AS table_name, n_live_tup AS rows,
       pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC
"""


_READ_ONLY_START = re.compile(r"^\s*(select|with|explain|show|table|values)\b", re.IGNORECASE)


def validate_adhoc(statement: str) -> str:
    """Reject anything that is not a single read statement before it leaves the machine.

    The server-side read-only transaction is the real guard; this keeps the
    failure local and the error message obvious.
    """
    body = statement.strip().rstrip(";").strip()
    if not body:
        raise ValueError("SQL 为空")
    if ";" in body:
        raise ValueError("只允许单条语句")
    if body.startswith("\\") or "\n\\" in body:
        raise ValueError("不允许 psql 元命令")
    if not _READ_ONLY_START.match(body):
        raise ValueError("只允许 SELECT / WITH / EXPLAIN / SHOW / TABLE / VALUES")
    return body
