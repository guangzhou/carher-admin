# LiteLLM CLI Harness Test Plan

## Test inventory

- `test_core.py`: 16 unit tests planned
- `test_full_e2e.py`: 9 end-to-end and installed-command subprocess tests planned
- `test_198_core.py` (1.1.0): 14 unit tests planned — no network, `remote198.run` stubbed
- `test_198_e2e.py` (1.1.0): 12 tests against the real 198 deployment — require ssh to 198;
  they **fail** (not skip) when 198 is unreachable, per HARNESS "no graceful degradation"

## 1.1.0 additions: 198 read-only views

### Unit plan (`test_198_core.py`)

- `remote198.psql` parses tab-separated output into dicts, drops `SET`/row-count lines,
  and sends `default_transaction_read_only=on` + `statement_timeout` every time
- `remote198.run` raises `RemoteError` with stderr on non-zero exit, filters ssh forwarding noise
- `source.resolve_tag`: lane given → tag must equal live version; missing tag in fork → error
- `source.show_symbol`: body ends at the next line at the same indent; decorators included
- `db._lit` rejects quotes, semicolons, spaces (SQL never sees free text)
- `db.spend_reconcile` builds index-friendly SQL (`"startTime" >=` range, no function on the column)
- `patches.owner_of`: missing → `-`; layer chain wins over patched_by; early owners need evidence
  (sentinel / `__wrapped__` / non-litellm baseline) before they are claimed
- `patches.compare_lanes` flags only differing symbols
- probe `layer_chain` follows `__wrapped__` and closures, stops at `litellm.*`, survives cycles

### E2E plan (`test_198_e2e.py`, real backend)

- `db anchor`: prod has ≥100 proxy model rows and a non-empty DB fallbacks list (dev has 3 / none)
- read-only guard: a `CREATE TABLE` through `remote198.psql` is refused by Postgres
- `db catalog`: every schema model of the live tag exists in the DB; 0 unfinished migrations
- `db config-owner`: `fallbacks` owned by db, `redis_host` owned by config; cross-checked against
  the proxy's own `/router/settings` `source` map is out of scope (needs master key) — documented gap
- `db retention`: SpendLogs oldest is within 14 days (retention is running)
- `db spend-reconcile`: totals are integers; gap = daily − rows for each key
- `src find` / `src show`: `Router.should_retry_this_error` found in `litellm/router.py`; body
  contains its own fingerprint `_num_healthy_deployments` (positive control for the guard in
  `dead_deployment_retry`)
- `src find` with an explicit lane and a wrong tag exits 1 (version gate positive control)
- `patches targets --json`: 0 import errors on prod; `_process_chunk` has 3 wrappers ending in
  `deepseek_id_prefix`; `jsonify_object` shows both `sitecustomize` and `null_byte_sanitize`
- `patches compare`: prod and gray identical (same image, same CMs)
- every command above runs through the installed `cli-anything-litellm` from `/tmp`

## Unit test plan

### `core/session.py`

- default session values
- locked save and load round-trip
- malformed session rejection
- URL normalization and validation
- dry-run mutation behavior
- secret fields never accepted or persisted

### `core/config.py`

- YAML proxy configuration parsing
- deployment and provider summary extraction
- recursive redaction of API keys, tokens, passwords, and secrets
- invalid YAML and invalid top-level values

### `utils/litellm_backend.py`

- executable discovery
- native argument forwarding without shell interpretation
- HTTP request headers, response parsing, and error mapping

## End-to-end test plan

- install the harness in editable mode
- invoke `cli-anything-litellm --help` from outside the repository
- verify `--json backend status` discovers the real LiteLLM installation
- inspect a real LiteLLM YAML configuration through the installed command
- persist and reload a non-secret profile through separate processes
- verify `--dry-run` returns the proposed profile without creating a session file
- start a local LiteLLM proxy using its native executable and a deterministic mock-response deployment
- call the proxy health and models endpoints through the harness
- send a chat completion through the harness and validate the OpenAI-compatible response structure and content

## Realistic workflow

### Configure, inspect, and call a LiteLLM proxy

Simulates an agent receiving a proxy configuration, checking the installed backend, saving the proxy URL and model, confirming server health and model discovery, and issuing an inference request. The workflow verifies that machine-readable output is valid JSON, secrets do not appear in inspection output, state survives process boundaries, and the final response is returned by a real LiteLLM proxy process.

## Test results

Command:

```text
python -m pytest agent-harness/cli_anything/litellm/tests -v -s --tb=no
```

Result: 25 passed in 4.78s. The suite covered 16 unit tests and 9 installed-command/real-backend tests. The E2E fixture launched LiteLLM 1.79.0 with a deterministic mock-response deployment, then verified health, model discovery, and a real OpenAI-compatible chat completion response.

The installed harness used FastAPI 0.115.14 and Starlette 0.46.2 because the validated LiteLLM 1.79.x proxy line requires the pre-0.116 FastAPI dependency surface. These constraints are encoded in the harness package so a clean reinstall remains runnable.

Remaining coverage gap: provider credentials and upstream provider billing are intentionally not exercised. The mock deployment validates the proxy and harness protocol without spending money or requiring external API keys.

## 1.1.0 test results (2026-10-20)

Command (from `/tmp`, so the installed command is exercised, not the repo checkout):

```text
CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest tools/cli-anything-litellm/cli_anything/litellm/tests -v --tb=short
```

Result: **58 passed in 153.71s** — 16 + 9 original tests, 21 unit (`test_198_core.py`), 12 against the real 198 (`test_198_e2e.py`).

```text
test_198_core.py::test_psql_parses_rows_and_drops_noise PASSED [  1%]
test_198_core.py::test_psql_empty_output_is_empty_list PASSED [  3%]
test_198_core.py::test_run_raises_with_filtered_stderr PASSED [  5%]
test_198_core.py::test_lane_deploy_rejects_unknown PASSED [  6%]
test_198_core.py::test_resolve_tag_requires_match_with_lane PASSED [  8%]
test_198_core.py::test_resolve_tag_missing_in_fork PASSED [ 10%]
test_198_core.py::test_show_symbol_bounds_and_decorators PASSED [ 12%]
test_198_core.py::test_lit_rejects_unsafe[a'b] PASSED [ 13%]
test_198_core.py::test_lit_rejects_unsafe[x;drop] PASSED [ 15%]
test_198_core.py::test_lit_rejects_unsafe[a b] PASSED [ 17%]
test_198_core.py::test_lit_rejects_unsafe[] PASSED [ 18%]
test_198_core.py::test_lit_rejects_unsafe[xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx] PASSED [ 20%]
test_198_core.py::test_spend_reconcile_sql_is_index_friendly PASSED [ 22%]
test_198_core.py::test_spend_reconcile_rejects_bad_date PASSED [ 24%]
test_198_core.py::test_owner_of_rules PASSED [ 25%]
test_198_core.py::test_purpose_catalog_covers_live_modules PASSED [ 27%]
test_198_core.py::test_live_modules_exist_in_repo PASSED [ 29%]
test_198_core.py::test_compare_lanes_flags_only_differences PASSED [ 31%]
test_198_core.py::test_probe_layer_chain_follows_closures_and_wraps PASSED [ 32%]
test_198_e2e.py::test_db_anchor_is_prod PASSED [ 34%]
test_198_e2e.py::test_psql_is_read_only PASSED [ 36%]
test_198_e2e.py::test_db_catalog_matches_schema PASSED [ 37%]
test_198_e2e.py::test_db_config_owner PASSED [ 39%]
test_198_e2e.py::test_db_retention_is_running PASSED [ 41%]
test_198_e2e.py::test_db_spend_reconcile_arithmetic PASSED [ 43%]
test_198_e2e.py::test_src_find_and_show_with_fingerprint PASSED [ 44%]
test_198_e2e.py::test_src_version_gate_rejects_wrong_tag PASSED [ 46%]
test_198_e2e.py::test_patches_prod_no_import_errors PASSED [ 48%]
test_198_e2e.py::test_patches_process_chunk_stack PASSED [ 50%]
test_198_e2e.py::test_patches_jsonify_two_layers PASSED [ 51%]
test_198_e2e.py::test_patches_prod_equals_gray PASSED [ 53%]
test_core.py::test_default_session_for_missing_file PASSED [ 55%]
test_core.py::test_session_round_trip_and_permissions PASSED [ 56%]
test_core.py::test_malformed_session_rejected PASSED [ 58%]
test_core.py::test_invalid_base_urls_rejected[localhost:4000] PASSED [ 60%]
test_core.py::test_invalid_base_urls_rejected[ftp://host] PASSED [ 62%]
test_core.py::test_invalid_base_urls_rejected[https://host/path?key=value] PASSED [ 63%]
test_core.py::test_invalid_base_urls_rejected[https://host/#fragment] PASSED [ 65%]
test_core.py::test_base_url_is_normalized PASSED [ 67%]
test_core.py::test_secret_session_fields_rejected PASSED [ 68%]
test_core.py::test_config_inspection_summarizes_and_redacts PASSED [ 70%]
test_core.py::test_invalid_config_top_level_rejected PASSED [ 72%]
test_core.py::test_invalid_model_list_rejected PASSED [ 74%]
test_core.py::test_backend_status_has_real_executable_mapping PASSED [ 75%]
test_core.py::test_native_forwarding_uses_argument_vector PASSED [ 77%]
test_core.py::test_missing_native_executable_is_clear PASSED [ 79%]
test_core.py::test_http_request_sends_bearer_and_json PASSED [ 81%]
test_core.py::test_organization_list_encodes_alias PASSED [ 82%]
test_core.py::test_organization_create_dry_run_does_not_call_backend PASSED [ 84%]
test_full_e2e.py::test_installed_help PASSED [ 86%]
test_full_e2e.py::test_backend_status_json_discovers_litellm PASSED [ 87%]
test_full_e2e.py::test_config_inspect_from_installed_command PASSED [ 89%]
test_full_e2e.py::test_profile_persists_between_processes PASSED [ 91%]
test_full_e2e.py::test_profile_dry_run_does_not_persist PASSED [ 93%]
test_full_e2e.py::test_real_proxy_health PASSED [ 94%]
test_full_e2e.py::test_real_proxy_models PASSED [ 96%]
test_full_e2e.py::test_real_proxy_chat_completion PASSED [ 98%]
test_full_e2e.py::test_native_server_version PASSED [100%]
```

Coverage notes:

- The read-only guard is proven by a write that Postgres refuses, not by inspecting the SQL.
- `db config-owner` applies the 1.105 precedence rule to the two sources; it was cross-checked once by hand against
  the proxy's own `GET /router/settings` `source` map (needs the master key, so not automated).
- `patches` covers 27 symbols chosen from the live callbacks; a callback that patches a symbol not in `TARGETS` is
  invisible to it. `test_purpose_catalog_covers_live_modules` only guards the purpose catalog.
- Found while testing: an unfiltered day-wide `group by api_key` on SpendLogs exceeded the 40s statement timeout;
  `spend-reconcile` now selects the top keys from the daily table and counts each by index lookup (3.8s).
