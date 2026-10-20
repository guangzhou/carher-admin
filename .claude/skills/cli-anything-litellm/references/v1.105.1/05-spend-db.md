# 05 — Spend tracking / cost / SpendLogs write path (LiteLLM v1.105.1 + our live patches)

Paths are relative to `src/litellm/` unless prefixed `live-cb/`. "Not verified" = I could not confirm from code in this tree (e.g. prisma-client-py itself isn't installed locally).

## TL;DR

1. **Daily tables and key/team spend don't depend on the SpendLogs row.** Both come from the same in-memory `payload`, but they go into separate queues with separate flushes. A dropped SpendLogs row loses only the detail row. This is **confirmed by code** (§1).
2. **Top hypothesis for the 1.3% drop (not a conclusion): our own sanitizer creates the invalid JSON.** Every Json column (`proxy_server_request`, `metadata`, `response`, `messages`, `request_tags`) is already a **JSON text string** when it reaches `jsonify_object`. Our two scrubbers (`sitecustomize.py`, `null_byte_sanitize.py`) run regexes on that text and delete the 6-char sequences `\u0000` / `\uD8xx`–`\uDFxx`. If the prompt contains a **literal backslash-u sequence** (source code such as `'\u0000'`, `'\uD83D'`, or `r'[\ud800-\udfff]'`), json.dumps writes it as `\\u0000`. The regex then removes the last 6 chars and leaves a dangling `\`. The result is `\'`, `\"` or `\-`, which is invalid JSON. **I reproduced this offline using the real patch file** (§3). The value echoed in the prod error starts with `{"model": ..., "messages": [...]`, which is the shape of `proxy_server_request`. That fits the hypothesis but doesn't prove it.
3. **Two of our sitecustomize patches now interact badly with 1.105.1** (§4):
   - The transport-misclassification fix flips **every** `prisma.errors.DataError` to "not transport". That includes the P1001 "can't reach database server" outage, which prisma surfaces as a base `DataError` (upstream says so at `proxy/db/exception_handler.py:165-168, 306-310`). During a DB outage, poison isolation would then bisect and **drop every row** as "Postgres rejected". Auth would also answer with a "doesn't exist" error instead of 503.
   - The batch-rescue patch re-enqueues batches that 1.105.1 **already requeues itself**, so those rows end up queued twice.

---

## 1. Path from a finished request to the DB

### 1a. Request side: callback, payload, queues

| Step | Where | Runs in |
|---|---|---|
| `async_log_success_event` → `_PROXY_track_cost_callback` (unless a spend-event producer/sidecar is configured) | `proxy/hooks/proxy_track_cost_callback.py:122-139` | litellm logging callback, off the response path. The exact task wrapper isn't verified. |
| `response_cost` read from `standard_logging_object["response_cost"]` | `proxy_track_cost_callback.py:350-352` | same |
| `_update_database_and_spend_counters` → `db_spend_update_writer.update_database(...)` | `proxy_track_cost_callback.py:382-406`, `:798-810` | awaited inside the callback |
| `get_logging_payload(...)` builds the SpendLogs payload. All `safe_dumps` of request and response bodies happen **here, synchronously on the event loop** | `proxy/db/db_spend_update_writer.py:529-535` | awaited |
| `payload["spend"] = response_cost or 0.0`; startTime/endTime → isoformat (completionStartTime stays a `datetime`) | `db_spend_update_writer.py:536-540` | awaited |
| **(a) SpendLogs:** `_record_spend_log` → `_insert_spend_log_to_db` → `enqueue_spend_logs(prisma_client, (payload,))` into the in-memory `spend_log_transactions` | `db_spend_update_writer.py:548-551, 1473-1489`; `proxy/utils.py:7200-7229` | awaited (lock + append) |
| **(b)+(c) everything else:** `asyncio.create_task(self._batch_database_updates(payload=payload, ...))` | `db_spend_update_writer.py:570-584` | **separate background task** |
| `_batch_database_updates` deep-copies the payload, then enqueues the key/user/team/org/project/tag/agent spend increments (`spend_update_queue`) and the DailyUser/EndUser/Agent/Team/Org/Tag transactions (`daily_*_update_queue`), each in its own try/except | `db_spend_update_writer.py:996-1162` (key: `:1164-1183`) | background task |
| Redis budget counters `increment_spend_counters` | `proxy_track_cost_callback.py:828-842` | awaited after `update_database` |

### 1b. Flushes

- `update_spend` runs every `batch_writing_interval` (= `PROXY_BATCH_WRITE_AT`, 10 s; `constants.py:1845`) (`proxy/proxy_server.py:10648-10657`). It does two things:
  1. `db_update_spend_transaction_handler` commits key/user/team/org spend and daily user/team/org/end-user/agent spend (`proxy/utils.py:7436-7440`). The Redis-buffer variant is used when `use_redis_transaction_buffer` is set (`db_spend_update_writer.py:1518-1530`). Daily rows go through `build_bulk_upsert` + `execute_raw` (`db_spend_update_writer.py:2499-2501`).
  2. `recover_parked_spend_logs` (Redis → queue head), then `update_spend_logs_job` if the queue is non-empty (`proxy/utils.py:7443-7458`).
- `update_daily_tag_spend` runs as its own job every `10 s × DAILY_TAG_SPEND_BATCH_MULTIPLIER` (`proxy_server.py:10661-10672`; `proxy/utils.py:7532-7574`).
- `_monitor_spend_logs_queue` is a long-lived task (`proxy_server.py:10689-10700`). It polls every 2 s with backoff up to 30 s (`proxy/utils.py:7757-7830`). A Responses-API request wakes it early (`:7232-7243`; `db_spend_update_writer.py:1488-1489`). Both the monitor and `update_spend` serialize on `spend_log_write_lock` (`proxy/utils.py:7593`).
- `_run_spend_logs_job`: pops up to 10 000 rows (`proxy/utils.py:7607`) → `ProxyUpdateSpend.update_spend_logs` (`:7610-7616`). On `CancelledError` the rows are requeued (`:7617-7623`). Tool, model-usage and autorouter queues flush afterwards (`:7644-7702`).
- `update_spend_logs` (`proxy/utils.py:7312-7401`), for each 1000-row chunk:
  - `jsonify_object({**entry})` per row (`:7352`);
  - `spend_log_write_batches(..., 2 MB, 100 rows)` (`:7354-7358`; `constants.py:1802-1803`; `proxy/db/spend_log_batching.py:104-144`). A row bigger than 2 MB goes alone;
  - `_create_spend_logs_with_poison_isolation` per statement (`:7359-7363`).
  - Errors: if the error isn't transient and is a prisma error → `requeue_spend_logs` + raise (`:7376-7384`). If transient → retry 3× with 1/2/4 s sleeps, then requeue + raise (`:7385-7395`).
- `_create_spend_logs_with_poison_isolation` (`proxy/utils.py:7840-7903`): `create_many(data=rows, skip_duplicates=True)` (`:7868`). On a base `DataError` (exact type, `exception_handler.py:152-172`) that isn't classified service-unavailable or deadlock, it bisects. A single failing row gets logged as **"dropping spend log row Postgres rejected"** and is dropped (`:7878-7886`). The budget is 256 failed inserts per flush (`:7833`).

**Verdict on "a dropped SpendLogs row does not affect the daily tables or key spend": confirmed.** The daily and spend queues are filled from `payload_copy` in a separate task (`db_spend_update_writer.py:570-584, 996`). Their flush never reads SpendLogs (`proxy/utils.py:7436` runs before and independently of the spend-log job). Caveats:
- If `get_logging_payload` itself raises, **both** are lost. `update_database` catches the error, logs it, and returns True (`db_spend_update_writer.py:595-607`).
- The SpendLogs queue has its own loss modes that the daily tables don't share. `enqueue_spend_logs` drops the **oldest** rows past 64 MB (`proxy/utils.py:7221-7229`; `constants.py:1805`), and with prompts stored a row can be hundreds of KB. Worth grepping prod for "byte budget; dropped the".

## 2. Column shapes and the prisma wire format

What `get_logging_payload` produces (`proxy/spend_tracking/spend_tracking_utils.py:783-836`):

| Column (schema type) | Python value | Source |
|---|---|---|
| `metadata` (Json?) | **str** of JSON text | `safe_dumps(clean_metadata)` `:795` |
| `request_tags` (Json?) | **str** of JSON text | `safe_dumps(list)` `:583-587` |
| `messages` (Json?) | **str**, `"{}"` except `_arealtime` | `:1079-1093` |
| `response` (Json?) | **str**. Either `safe_dumps(sanitized)`, or, if the response was already a str, that **raw str with only NUL stripped (not guaranteed to be valid JSON)** | `:1647-1698`, esp. `:1688-1691` |
| `proxy_server_request` (Json?) | **str**: `safe_dumps(_sanitize_request_body(...))` | `:1569-1620`, esp. `:1612-1613` |
| `spend` (Float) | float | overwritten at `db_spend_update_writer.py:536` |
| `*_tokens` (Int) | int from usage | `:798-800` |
| `startTime`/`endTime` | ISO str; `completionStartTime` stays a datetime | `db_spend_update_writer.py:537-540` |
| all others | str / None | `:783-836` |

- `safe_dumps` = `json.dumps(safe_json_structure(...), default=str)` (`litellm_core_utils/safe_json_dumps.py:87-93`). It strips raw `\x00` from every str leaf **before** dumping (`:43, 56`). It does **not** strip lone surrogates. It uses default `ensure_ascii=True` and `allow_nan=True`, so the output is pure ASCII, a lone surrogate becomes the text `\ud83d`, and NaN becomes the token `NaN`. The depth cap is 100 (`constants.py:85`).
- `_sanitize_request_body_for_spend_logs_payload` truncates every str leaf longer than `MAX_STRING_LENGTH_PROMPT_IN_DB` (**default 2048**, `constants.py:529`; env unset ⇒ 2048, `spend_tracking_utils.py:75-86`). It keeps the first 716 + last 1331 chars plus a marker (`:1157-1187`). This runs on **Python str code points before json.dumps**, so it can't split a JSON escape. CPython stores an astral char as one code point, so it can't split a real surrogate pair either (checked offline). It caps each leaf, not the total: a long conversation is still hundreds of KB.
- `jsonify_object` (`proxy/utils.py:4623-4633`) deep-copies the row and json.dumps **only dict values**. Every Json column is already a str, so upstream jsonify is effectively a deep copy here.
- Our wrappers. Both stack, because their idempotence flags have different names:
  - sitecustomize patches `PrismaClient.jsonify_object` when `litellm.proxy.utils` is imported (`live-cb/sitecustomize.py:116-188`);
  - later, the callback import of `null_byte_sanitize` wraps that again (`live-cb/null_byte_sanitize.py:268-288`);
  - call order is null_byte scrub → sitecustomize scrub → upstream. Both walk the row and apply `_scrub_str` to **every str, including the JSON-text columns** (`sitecustomize.py:95-114`; `null_byte_sanitize.py:200-240`).
- `null_byte_sanitize._patch_safe_json_dumps` swaps the module attribute (`null_byte_sanitize.py:340-352`). But `spend_tracking_utils` did `from ... import safe_dumps` at import time (`spend_tracking_utils.py:47`), and it is imported eagerly by `proxy_server.py:800`, so the spend payload **most likely uses the unpatched safe_dumps**. Load order isn't verified; it's harmless either way.
- How prisma-client-py 0.11 handles it: `create_many(data=[...])` goes through `SpendLogsRepository(prisma_client).table` (`repositories/table_repositories.py:34-39, 82-83`; `proxy/utils.py:7360, 7868`). The prisma package isn't in this tree, so the builder internals are **not verified**. What the evidence shows: our own 09-19 capture recorded the engine message "`... LiteLLM_SpendLogsCreateManyInput` ... is not a valid `JSON String`. Underlying error: invalid escape" (`live-cb/sitecustomize.py:731-733`). So a Python str given to a Json field is passed through as JSON text and parsed by the Rust engine (serde_json). Valid JSON text is accepted (stored as its parsed jsonb). Invalid text fails client-side validation with exactly this "Unable to match input value" family of errors. `prisma.Json(x)` would just json.dumps x into the same kind of string, so no wrapper is needed for already-valid text.

## 3. The open problem: ranked hypotheses (none is a conclusion)

### H-A (most likely): our scrubber's text regexes corrupt valid JSON text

- **Hypothesis.** A long-context request contains a literal backslash-u sequence in a part of a str leaf that survives truncation. `\u0000`, `\uD800-\uDBFF` not followed by a low surrogate, or a lone `\uDC00-\uDFFF` all count. Typical sources are code or JSON in tool_result blocks, e.g. `'\u0000'`, `"\uD83D"`, `r'[\ud800-\udfff]'`. Our patches contain these exact literals (`live-cb/sitecustomize.py:84-93`), and so do many codebases. `safe_dumps` encodes the literal backslash as `\\`. `_scrub_str` then:
  - runs `.replace("\\u0000","")` (`sitecustomize.py:96-97`; `null_byte_sanitize.py:206-207`), and/or
  - runs `_LITERAL_HIGH/LOW_LONE_RE.sub` (`sitecustomize.py:87-93, 100-102`; `null_byte_sanitize.py:188-197, 213-215`).

  Either one removes the last 6 chars of `\\uXXXX`, leaving a lone `\` in front of the next char. That gives an "invalid escape", or a `\"` that swallows the closing quote. The engine then rejects `proxy_server_request` as not a valid JSON String. The poison-isolation path drops the row (`proxy/utils.py:7878-7886`).
- **Why it fits the observed shape.** Coding agents (Claude Code `/v1/messages`, Cursor → DeepSeek, grok) carry many file and tool outputs, so more text is a chance of hitting a literal. The echoed value starts with `{"model": ..., "messages":`, the shape of `proxy_server_request`. The 09-19 capture's own underlying error was **"invalid escape"**, which is exactly what this produces. Upstream already strips raw NUL before dumping (`safe_json_dumps.py:43`), so in 1.105.1 a `\u0000` in these JSON-text columns can **only** come from literal text. The `\u0000` replace now does nothing useful there; it can only do harm.
- **Offline reproduction (done, pure Python, no DB).** I loaded the real `live-cb/null_byte_sanitize.py` and the real `safe_json_dumps.py` with stubbed litellm modules, plus a verbatim copy of upstream `jsonify_object`. The column was built with `safe_dumps` of `{"model":..., "messages":[{"role":"user","content":[{"type":"tool_result","content": X}]}]}`. Every column was valid JSON before the scrub.

  | X | after patched `jsonify_object` |
  |---|---|
  | plain code, real `\x00`, real lone surrogate, real emoji | valid |
  | `_NUL_ESCAPED = "\\u0000"` | **INVALID** (`Expecting ',' delimiter`) |
  | `const NUL = '\u0000';` (literal backslash) | **INVALID** (`Invalid \escape`) |
  | `if (c === '\uD83D') {}` | **INVALID** (`Invalid \escape`) |
  | `re.compile(r'[\ud800-\udfff]')` | **INVALID** (`Invalid \escape`) |
  | `printf("\u0000x");` | **INVALID** (`Invalid \escape`) |

  Minimal standalone version:
  ```python
  import json, re
  H = re.compile(r"\\u[dD][89aAbB][0-9a-fA-F]{2}(?!\\u[dD][c-fC-F][0-9a-fA-F]{2})")
  L = re.compile(r"(?<!\\u[dD][89aAbB][0-9a-fA-F]{2})\\u[dD][c-fC-F][0-9a-fA-F]{2}")
  def scrub(s):  # == sitecustomize.py:95-103
      s = s.replace("\x00", "").replace("\\u0000", "")
      s = re.sub(r"[\ud800-\udfff]", "", s)
      return L.sub("", H.sub("", s)) if "\\u" in s else s
  col = json.dumps({"model": "m", "messages": [{"content": "const NUL = '\\u0000';"}]})
  json.loads(col)          # OK
  json.loads(scrub(col))   # json.decoder.JSONDecodeError: Invalid \escape
  ```
- **What would confirm it:**
  1. On the gray pod (no DB write): build the same row, call the live `prisma_client.jsonify_object`, and `json.loads` the `proxy_server_request`. It should raise.
  2. Log the **tail** of the rejection: `str(e)[-1500:]`, not the head. `log_truncate` cuts records at 4000 chars (`live-cb/log_truncate.py:17-28`), and upstream stdout is capped at 4096 (`constants.py:119`). Expect `... is not a valid JSON String. Underlying error: invalid escape at line 1 column N`, and the text near column N should be `\'`/`\"`/`\-`.
  3. For a few dropped request_ids from `cursor-liuguoxian-std` / `claude-code-*`, find the client-side transcript (Claude Code `~/.claude/projects/*.jsonl` at that timestamp) and grep the message text for `\\u0000|\\u[dD][89a-fA-F]..`.
- **What would rule it out:** the error tail names a different field, or shows `NaN`, `lone leading surrogate`, or `recursion limit`. Or the dropped requests' bodies contain no backslash-u literal in any kept slice (first 716 / last 1331 chars of each leaf). Or swapping the scrubber (below) on gray leaves the drop rate unchanged.
- **Candidate fix, not applied.** For the Json columns, scrub at object level: `json.dumps(scrub_obj(json.loads(col)))`, where `scrub_obj` removes raw NUL and lone-surrogate code points from str leaves and keys. Fall back to the original value if `json.loads` fails. Offline check: every case above stays valid and keeps its content exactly. Real NUL and lone surrogates are still removed, because `json.loads("\"\\ud83d\"")` returns a lone code point that the regex then strips. Then delete the literal-text regexes and the `\u0000` replace. The cost is one more parse plus dump per large row.

### H-B (lower): NaN / ±Infinity tokens in a Json column

`safe_dumps` uses `allow_nan=True` (`safe_json_dumps.py:93`), so `float('nan')` becomes `NaN`, which serde_json rejects. Candidates are `metadata.additional_usage_values` / `cost_breakdown` / `litellm_overhead_time_ms` (`spend_tracking_utils.py:649-761`) and the `response` logprobs. There is no specific link to long context. The error tail would show `expected value` at a `NaN`. Repro: `json.dumps({"a": float("nan")})` → `{"a": NaN}` (checked locally).

### H-C (lower): `response` stored as a raw non-JSON str

If the standard-logging `response` is a str (`litellm_core_utils/litellm_logging.py:5966-5968`), the column is that str with only NUL stripped (`spend_tracking_utils.py:1688-1689`). If it isn't valid JSON (plain text), the Json column is rejected. That would hit whole call types every time, not ~1.3% of long-context requests, and the echoed value would not start with `{"model"`. Check: `call_type` of the dropped ids.

### Ruled out (mechanically, from code)

- **Truncation splitting an escape or surrogate pair**: truncation works on code points before dumping (`spend_tracking_utils.py:1157-1187, 1612-1613`).
- **Lone surrogate surviving**: raw code points are stripped by `_LONE_SURROGATE_RE`, and single-backslash `\udXXX` by the literal regexes. Our scrubber handles this. It only misfires on the escaped-backslash form (H-A).
- **Int overflow**: token counts and `request_duration_ms` stay far below 2^31.
- **List vs Json mismatch**: every Json column is a str by construction.
- **Depth over serde's 128 limit**: `safe_dumps` caps depth at 100.
- **Missing column**: covered in the finding file.

### Arithmetic note

48–62 drops in ~15 min across 4 pods extrapolates to ~4.6–6k per day. The reconciled daily gap is ~1.1k. Either the sampled window was a peak, or one of the two counts is off. That's worth checking before using either number as the success metric for a fix.

## 4. Are our sitecustomize spend patches still meaningful on 1.105.1?

**Batch rescue** (`live-cb/sitecustomize.py:536-714`). It wraps `ProxyUpdateSpend.update_spend_logs`, and on a non-transport exception re-enqueues `logs_to_process` at the head, with a 3-strike drop per request_id.
- In 1.105.1, base-`DataError` rows are bisected and dropped **inside** poison isolation without raising (`proxy/utils.py:7870-7886`). So the rescue never sees the case it was written for, and its 3-strike drop is dead code for it.
- When `update_spend_logs` does raise, upstream **already** requeues. Non-transient prisma error → `requeue_spend_logs` (`:7376-7378`); transient error after retries → `:7392-7393`; cancel → `:7617-7618`. The rescue then requeues the same rows **again** (`sitecustomize.py:623-647`). Example: a `MissingRequiredValueError`, a `DataError` subclass and therefore not "exact base", raises out of isolation (`exception_handler.py:172`). Its rows get queued twice: Redis park plus in-memory, or twice in memory. `skip_duplicates=True` (`:7868`) prevents duplicate DB rows. But the copies count against the 64 MB queue budget and can push older real rows out (`:7221-7229`). Upstream has no strike counter, so the loop continues even after the rescue gives up.
- **Recommendation: remove the rescue.** Check prod for `[spendlog-rescue]` lines first. If there are none, it is inert.

**Transport-misclassification fix** (`live-cb/sitecustomize.py:785-918`).
- **Still needed in some form.** 1.105.1 still keyword-matches `str(e)` for any PrismaError (`exception_handler.py:195-212`). A validation `DataError` whose echoed row contains "timeout" would be classified service-unavailable (`:326-327`). Isolation would then re-raise instead of bisecting (`proxy/utils.py:7873-7874`), and the retry/requeue-at-head loop comes back (`:7385-7395`).
- **But it's too broad.** It returns False for *any* `DataError` (`sitecustomize.py:802-820, 848-861`). Upstream documents that prisma surfaces the P1001 "can't reach database server" outage as a base `DataError` (`exception_handler.py:165-168, 306-310`; prisma internals not verified here). With our patch, during an outage:
  - poison isolation sees "data error, not unavailable" and **drops every row** as "Postgres rejected" (`proxy/utils.py:7871-7886`);
  - auth's `_user_read_failure` returns a "User doesn't exist" ValueError instead of propagating the outage (`proxy/auth/auth_checks.py:2763-2768`);
  - the auth reconnect path is skipped (`auth_checks.py:3789`); so is health reconnect (`proxy/health_endpoints/_health_endpoints.py:1533`).
- **Suggested narrowing:** flip to False only when the message contains the engine-validation phrases ("Unable to match input value", "is not a valid `JSON String`"). Never flip when it contains "can't reach database server" or `P1001`/`P1017`.

## 5. Cost: which model name is priced

- The spend number is `standard_logging_object["response_cost"]` (`proxy_track_cost_callback.py:350-352`). It equals `model_call_details["response_cost"]`, set by `Logging._response_cost_calculator`. For streams, that runs on the **assembled** `complete_streaming_response` once the stream ends (`litellm_core_utils/litellm_logging.py:2794-2811`).
- `_response_cost_calculator` (`litellm_logging.py:1798-1932`):
  - if the result's `_hidden_params["response_cost"]` is set, it returns that directly (`:1857-1866`). Pass-through handlers set this from `litellm.completion_cost` (e.g. `proxy/pass_through_endpoints/llm_provider_handlers/openai_passthrough_logging_handler.py:417,438`);
  - otherwise it calls `litellm.response_cost_calculator(model=litellm_model_name or self.model, router_model_id=<model_info.id>, custom_pricing=..., base_model=...)` (`:1885-1924`);
  - `router_model_id` comes from hidden params or from `litellm_params.metadata.model_info.id` (`:1867-1874`, `:871-883`).
- Name selection (`cost_calculator.py:815-883`), first match wins:
  1. custom pricing → the deployment `model_info.id` entry, if it prices anything (`:850-858`);
  2. `base_model` or `provider_response_model` (`:860-861`);
  3. `hidden_params.model` (`:863-865`);
  4. `response.model` (`:867-868`);
  5. `model` (`:870-871`).

  A provider prefix is added if missing (`:873-881`). `self.model` is the **deployment's** `litellm_params.model` for the call that actually succeeded. So after `model_group_alias` or a fallback, the served deployment is what's priced, not the requested alias. That is how the code is meant to work; I didn't trace per-fallback Logging objects end to end. The provider-reported cost in hidden params short-circuits everything (`cost_calculator.py:2046-2050`).
- `pricing_overlay` (`live-cb/pricing_overlay.py`):
  - sets cached-input price = input price for the chatgpt bases, and sol output to $50/M, by editing `litellm.model_cost[<base key>]` (`:49-75`). This does **not** affect deployments priced through the custom-pricing `model_info.id` entry (rule 1 above);
  - registers image prices (`:89-92`);
  - wraps `litellm.response_cost_calculator` and `litellm.completion_cost` with ×1.3 for cost > 0 (`:95-115`). The logging path calls the attribute (`litellm_logging.py:1924`), so ×1.3 applies. The internal `completion_cost` call inside `response_cost_calculator` uses the module-local name (`cost_calculator.py:2052`), so there's no double multiply;
  - pass-through rows get ×1.3 through the `litellm.completion_cost` attribute. The hidden-params short-circuit at `litellm_logging.py:1857-1866` then returns that already-multiplied value without multiplying again. Consistent;
  - one gap: a cost precomputed in hidden params by code calling `cost_calculator.completion_cost` **directly** (not the attribute) would skip ×1.3. Not enumerated.

## 6. Retention cleanup

- `SpendLogCleanup.cleanup_old_spend_logs` (`proxy/db/db_transaction_queue/spend_log_cleanup.py:688-819`):
  - takes a Redis pod lock when Redis is present (`:726-741`);
  - run budget 300 s, split across table groups (`:743-758`; `constants.py:1786`).
- Per batch: `DELETE FROM "LiteLLM_SpendLogs" WHERE (request_id,"startTime") IN (SELECT ... WHERE "startTime" < $cutoff LIMIT $batch)` (`:360-367, 493-503`). It runs in its own tx with `SET LOCAL statement_timeout` and `lock_timeout` = min(30 s, remaining budget) (`:243-258, 307-312`). Then a 0.1 s sleep (`:463`). Default batch is 1000 rows, max 500 batches per run (`constants.py:1780-1781`), so at most ~500k rows per run. After 3 consecutive batch failures it stops for that table (`:425-434`). Partition-drop runs only with `use_spend_logs_partitioning` (`:589-596`).
- Only **one** retention period exists upstream (`maximum_spend_logs_retention_period`, `:236-241`). Our "two-tier" retention must be our own job; I couldn't find it in this tree (not verified).
- **Risks on a 75 GB table:**
  - DELETE doesn't return disk space; it creates dead tuples, mostly in **TOAST** because of the big Json columns. Space only comes back through autovacuum (reuse) or `VACUUM FULL`/`pg_repack` (lock or extra space);
  - the `startTime < cutoff LIMIT` subquery uses `@@index([startTime])` (schema line 740). Fine;
  - each 1000-row batch holds row locks for up to 30 s at most; there are no table-level locks unless partition DDL runs (ACCESS EXCLUSIVE, guarded by `lock_timeout`, `:581-596`);
  - a 300 s budget with 1000-row deletes of TOAST-heavy rows may not keep up with 84k+ rows/day if a statement nears its timeout. Check the cleanup summary log `stop_reason` (`:688-695`).

## 7. Surprising or risky

1. **Our sanitizer is the most likely poison source** (H-A). Upstream already strips NUL before dumping, so the `\u0000` replace on JSON text can now only do harm.
2. **The transport fix disables upstream's outage guard** in poison isolation and auth (§4). In a DB blip this turns into mass "dropping spend log row" plus wrong auth errors. Highest-impact latent risk in this area.
3. **The rescue double-requeues** on paths upstream already requeues (§4).
4. The `null_byte_sanitize` safe_dumps patch is probably a no-op for spend logs, because of the from-import binding (`spend_tracking_utils.py:47`).
5. Both scrubbers run on every row: two deep copies (`jsonify_object` `proxy/utils.py:4624` and the dict rebuilds) and four regex passes over hundreds of KB per row, inside the flush on the event loop. CPU cost, not correctness.
6. The SpendLogs queue sheds the oldest rows past 64 MB (`proxy/utils.py:7221-7229`). This is a second, silent-ish loss mode that the daily tables don't share. Grep prod for it before attributing the whole 1.3% to H-A.
7. The rejection log is cut from the tail (`log_truncate.py:17-28`), and the field name and "Underlying error" are at the end of prisma's message. To diagnose, log `str(e)[-1500:]`.
