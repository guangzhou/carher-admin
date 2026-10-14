# LiteLLM /spend/logs/v2: 10000 count cap, research report

Legend: [V] verified from source/PR text I read. [I] inferred. [N] not verified (tooling limit).
Method note: ~/codes/litellm is a stale fork (origin guangzhou/litellm, HEAD May 2026, tags end at v1.83.x, no
SPEND_LOGS_PAGINATION_COUNT_CAP) and github.com:443 git access timed out. So `git log -S` was NOT usable. I used the
GitHub REST API + raw.githubusercontent.com via curl (works, but the unauthenticated API got rate-limited mid-run).
Prod file = research/live_spend_endpoints.py (v1.103.2). Its line numbers below; "main" = raw main fetched today.

## 1. Origin of the cap
- [V] Introduced by PR #31825 "fix(spend): bound the logs-tab pagination count to stop full-window scans", author
  yassin-berriai, Linear LIT-4119, merged 2026-07-07T16:41Z into `litellm_internal_staging`, merge commit
  dc48b2049138ff4eb69879c2a65381922febe8c1. https://github.com/BerriAI/litellm/pull/31825
  Files: spend_management_endpoints.py (+ tests, + UI LogsTableToolbar.tsx / log_filter_logic.tsx for the "10000+" label).
- [V] Motivation (PR body): a customer saw RDS Aurora ACU spikes after an upgrade; Performance Insights flagged
  `SELECT COUNT(*) FROM (SELECT request_id FROM "LiteLLM_SpendLogs" WHERE ...)` at ~472s avg on a multi-TB table. The UI
  list endpoint computed an exact total over the whole window on every load. Fix = `SELECT COUNT(*) FROM (SELECT 1 ... LIMIT cap+1)`
  and a new `total_is_capped` flag; UI shows "10,000+" and "Page 1 of 200+". PR's own demo: 30,000 rows ->
  `{"total":10000,"total_pages":200,"total_is_capped":true}` at page_size 50. Below the cap the total stays exact.
- [V] Predecessor: PR #31423 (LIT-4027) folded the total into the page query as COUNT(*) OVER (); #31825 says that still drained
  every matching row, hence the bounded count. https://github.com/BerriAI/litellm/pull/31423
- [V] PR #33994 (merged 2026-07-20, LIT-4558) raised /spend/logs/v2 page_size cap from 100 to 1000. https://github.com/BerriAI/litellm/pull/33994
- First release tag: [V] raw tag probes: constant absent in v1.90.0, v1.91.0, v1.92.0; present in v1.93.1, v1.95.0, v1.96.0, v1.97.0,
  v1.98.0, v1.99.0/.1/.3, v1.100.0*, v1.101.0, v1.102.0, v1.103.0. So the earliest tag I could prove contains it is **v1.93.1**.
  [N] v1.93.0, v1.94.0, v1.94.1 were unreachable (curl 000); v1.93.2 returned a file WITHOUT the constant (patch tags sit on
  different branches, so tag numbering is not linear). Likely the first release is the v1.93.x line (inferred from the
  July merge date); treat "v1.93.0 or v1.93.1" as the answer and confirm with `git tag --contains dc48b2049` on a fresh clone.
- [V] Not a v1.103 novelty: prod v1.103.2 line 67 = `SPEND_LOGS_PAGINATION_COUNT_CAP: Final = 10000`; main line 86, same value.

## 2. Alternatives in v1.103.2 / main
Params of /spend/logs/v2 in v1.103.2 [V, read from signature]: api_key, user_id, request_id, session_id, team_id, min_spend,
max_spend, start_date, end_date, page, page_size(ge=1, le=1000, line 2405), status_filter, cache_hit_filter, model, model_id,
model_group, key_alias, end_user, error_code, error_message, sort_by, sort_order, exclude_internal_health_checks,
group_by_session, session_cursor, search. main adds `span_type`, `session_id` only. Nothing else.
- Cursor/keyset for plain rows: [V] NO. `session_cursor` (+ response `next_session_cursor`, `has_more`) exists only for
  group_by_session=true (lines 2442, 3015-3108, doc says "UI route only, honored when sorting by startTime"). Plain rows are
  OFFSET only (`LIMIT $p OFFSET $p+1`, lines 2890-2893). main (s_main.py:2578) is identical in this respect.
- Omit heavy columns / select fields: [V] NO parameter. The list SQL already omits messages/response/proxy_server_request
  (comment ~2696) but always selects `metadata` (_SPEND_LOG_LIST_COLUMNS, lines 73-85) - that is your 96KB. No `fields`,
  `exclude_metadata`, `include_*` anywhere in v1.103.2 or main.
- Env var / config to change the cap: see section 3.
Other endpoints:
| endpoint | capped? | sub-day start/end? | max page size / notes |
|---|---|---|---|
| /spend/logs/v2 | count capped 10000, rows reachable by OFFSET beyond (lines 2862-2866) [V] | YES: `YYYY-MM-DD HH:MM:SS` or date (line ~2495-2520), UTC, inclusive >= and <= | 1000 |
| /spend/logs/ui | same function/cap (line 2483 `is_v2` switch) [V] | seconds only (no date-only format) | 1000; only route with session cursor + session_total_count enrichment (extra GROUP BY query) |
| /spend/logs (v1) | `summarize=false` -> hard `take=10000`, newest first, NO pagination, header `x-litellm-spend-logs-truncated: true` (lines 3324-3345, 3395-3460, 330-335) [V] | date only (`%Y-%m-%d`, lines ~3417-3420) | n/a; unusable for 150k |
| /global/spend/logs | no rows; per-day spend aggregate, last 30d, admin, optional api_key (lines 3688-3705) [V] | no | n/a |
| /user/daily/activity (+ /aggregated) | not row-capped by the constant; paginated, page_size le=1000 (internal_user_endpoints.py:2812 in v1.103.2) [V] | NO: start/end are `YYYY-MM-DD`; table LiteLLM_DailyUserSpend is whole-UTC-day buckets; `timezone` only maps to whole dates (common_daily_activity.py:559-602 docstring) | per-day breakdown by model/api_key/provider; cannot give hourly or caller-local-hour boundaries |
| export endpoint | [N] none found; I did not exhaustively grep routers | | |
Summary: for per-request rows with sub-day precision, /spend/logs/v2 is the only route; daily-activity is day-granular only.

## 3. Raising/disabling the cap
- [V] No env var, no config key: the constant is a module-level literal (v1.103.2 line 67; main line 86). Neither
  os.getenv nor litellm/constants.py mention it (grep of v1.103.2 constants.py and main constants.py = 0 hits; the only
  os.getenv in the file is DATABASE_URL at line 3624). [N] docs/config_settings.md fetch failed (curl 000), so I could not grep the docs; the
  code makes docs support impossible anyway. Only option: patch the source (not recommended; the cap exists to protect Postgres).

## 4. Issues about wrong total_pages / 10000 limit
- [V] No issue found reporting "total_pages wrong / pagination stops at 100 pages". GitHub search for `total_is_capped`,
  "spend/logs/v2 pagination", "total_pages" returned only PRs (#31825, #38972, #39257, #40644/#40661 UI "jump to last page",
  #42876, #40733) and these issues:
  - #36666 (open, 2026-08-12) "Filter spend logs by endTime in GET /spend/logs/v2" - startTime-only filter. https://github.com/BerriAI/litellm/issues/36666
  - #37007 (open, 2026-08-15) "GET /spend/logs has no pagination and OOM-kills the proxy" (110k rows, 371MB). https://github.com/BerriAI/litellm/issues/37007
  [N] I could not read maintainer comments on either (API rate limit; HTML is JS-rendered). No maintainer recommendation obtained.
- [V] The only "official" guidance is in code: the v1 warning log says "use /spend/logs/v2 for paginated access" (line 333).
- UI side [V from #40661 body]: "The existing 10,000-session count cap defines the last displayed page" - i.e. LiteLLM treats the cap as intended.

## 5. Recommended client algorithm for 120k-150k rows / 24h on v1.103.2
Key facts [V]: response carries `total`, `total_pages`, `total_is_capped`. If `total_is_capped` is false the total is EXACT
(count probes at most cap+1 rows). So a window is "safe" iff total_is_capped == false.
1. Time-partition with adaptive bisection: request window W page=1; if total_is_capped true, split W in half (recurse) until
   every leaf has total_is_capped false (<=10000 rows). Start with ~1h or smaller windows (150k/24 ~ 6k/h average; peaks need
   splitting). Leaf with total_pages known -> fetch pages 2..N.
2. Window boundaries: SQL is `startTime >= start AND startTime <= end` both inclusive, bound params parsed with second
   precision (strptime, lines 2490-2520; SQL lines 2706-2710) [V]. startTime has sub-second precision [I: Prisma DateTime]. So
   `[12:00:00,12:59:59]` then `[13:00:00,...]` MISSES rows in (12:59:59.000, 13:00:00). Use contiguous windows sharing the
   boundary second (end_i = start_{i+1}) and de-dupe by request_id (PK). Never use "end = start+N-1s".
3. page_size: max 1000 (le=1000, line 2405). With ~96KB/row a 1000-row page is ~96MB of JSON [I: arithmetic from your figure;
   PR #33994 measured only 4MB/1000 rows with small metadata]. Prefer 100-250 to bound proxy memory/latency; the count query
   is rerun for every page, so fewer, bigger pages cost less DB work - tune by measuring. Parallelism: leaves are independent; run 3-4 at once.
4. Ordering: v2 non-session ORDER BY is only `"startTime" ASC` with NO tiebreaker (lines ~2880-2884); the session variant
   adds `, request_id`. [V] So rows with identical startTime may be returned in different order on different OFFSET pages ->
   a row can be skipped at a page edge (and another duplicated). Mitigations: (a) de-dupe by request_id and verify
   len(unique ids) == leaf `total`; (b) if short, re-fetch that leaf with sort_order=desc (different tie order) or shrink the
   leaf to smaller windows / add a filter (model_id, api_key, user, team_id are equality filters, lines ~2724-2736) and
   union; (c) final reconcile by comparing the sum of leaf totals to a fresh count per leaf.
5. Rows still being written [I, based on how spend logs are batch-written asynchronously; the flush interval itself was not verified]:
   startTime is the request START, but the row is inserted at request end plus queue flush delay. So (a) only treat windows
   older than (max request duration + flush delay), e.g. 10-15 min, as final; re-sweep the trailing window next run;
   (b) a late insert into an earlier OFFSET position shifts later rows forward, so ascending paging yields DUPLICATES (harmless
   with de-dupe), not losses; deletes (spend-log cleanup retention) would cause losses - do not paginate near the retention edge;
   (c) always re-read `total` at the end of a leaf and refetch if it grew.
6. Cheaper alternative if the dashboard only needs aggregates: /user/daily/activity (day-granular, no cap, no 96KB metadata)
   - but it cannot honour arbitrary hour/timezone boundaries, so it only helps for whole-UTC-day totals.
7. Do not try: /spend/logs (10000 hard take), raising the cap (no knob), group_by_session (collapses rows; its cursor is for sessions).
