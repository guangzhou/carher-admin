# CLI-Anything Sub2API Test Plan

## Inventory

- `test_core.py`: 16 unit tests are planned for configuration resolution, URL and
  query normalization, response-envelope parsing, sensitive export detection, and
  the persistent REPL profile store.
- `test_full_e2e.py`: 15 end-to-end tests are planned for a live local HTTP server,
  installed CLI subprocesses, authentication propagation, read-only discovery, and
  mutation handling.

## Unit Test Plan

### `core/client.py`

- Resolve the base URL and choose an admin API key before a JWT.
- Reject missing base URL or credentials without leaking credential values.
- Normalize both `/admin/...` and `/api/v1/admin/...` paths.
- Reject a non-admin relative path before performing a request.
- Encode query parameters, including repeated values.
- Unwrap the Sub2API `{code, message, data}` envelope.
- Raise actionable errors for non-zero response codes and malformed JSON.

### `core/profile.py`

- Write and load non-secret connection profile metadata using a locked JSON save.
- Ensure credentials are never persisted in the profile document.

### `core/operations.py`

- Mark account and proxy exports as sensitive so CLI commands require an explicit
  output file.
- Build the documented account-list query map without sending empty filters.
- Resolve the known 198 target and keep deployment metadata separate from credentials.
- Filter transient Grok password/SSO fields from persisted OAuth credentials.

## End-to-End Test Plan

The test suite starts an in-process `ThreadingHTTPServer`, which implements the
actual Sub2API HTTP response envelope and validates headers, paths, query strings,
and request JSON. It is the real backend contract for this harness; no Python
reimplementation of Sub2API business operations is used.

- Run `status` against the server and verify `x-api-key` authentication plus the
  `/api/v1/admin/system/version` endpoint.
- Run `accounts list` with filtering and verify the request URL and returned
  result payload.
- Run `accounts update` and verify the `PUT` route and exact JSON mutation body.
- Run a generic resource command (`users get`) against its actual REST endpoint.
- Verify dangerous exports require `--file` and never print sensitive content.
- Verify `--dry-run` suppresses mutations before network traffic is sent.
- Verify a missing administrator credential exits cleanly without a Python traceback.
- Invoke the installed `cli-anything-sub2api` executable for `--help`, `--json`,
  and a full status/list/update workflow from an unrelated working directory.
- Read Grok OAuth capabilities and verify password import dry-run redacts stdin data.

## Realistic Workflow

### Inspect, update, and verify an upstream account

**Simulates:** An operator diagnosing an upstream account then increasing its
concurrency safely.

**Operations chained:**

1. `status` confirms the configured Sub2API server and admin credentials work.
2. `accounts list --status active --search ...` narrows the target set.
3. `accounts get <id>` inspects the target object.
4. `accounts update <id> --json '{"concurrency":10}'` sends the native API
   mutation.
5. `accounts get <id>` verifies the backend response.

**Verified:** The local server records each native API call, receives exactly the
expected auth header and JSON body, and returns real JSON consumed by the CLI.

## Results

### Source-tree Test Run

Command:

```bash
PYTHONPATH=. python3 -m pytest cli_anything/sub2api/tests/ -v --tb=short
```

Result (Python 3.14.6):

```text
collected 26 items

cli_anything/sub2api/tests/test_core.py::test_connection_prefers_explicit_values_over_environment PASSED
cli_anything/sub2api/tests/test_core.py::test_connection_falls_back_to_environment_and_profile PASSED
cli_anything/sub2api/tests/test_core.py::test_missing_credentials_are_rejected_without_echoing_values PASSED
cli_anything/sub2api/tests/test_core.py::test_normalize_path_supports_documented_admin_forms PASSED
cli_anything/sub2api/tests/test_core.py::test_normalize_path_rejects_foreign_urls_and_non_admin PASSED
cli_anything/sub2api/tests/test_core.py::test_encode_query_drops_empty_and_lowercases_booleans PASSED
cli_anything/sub2api/tests/test_core.py::test_unwrap_response_raises_on_error_envelope PASSED
cli_anything/sub2api/tests/test_core.py::test_api_error_from_http_status_is_actionable PASSED
cli_anything/sub2api/tests/test_core.py::test_sensitive_export_detection PASSED
cli_anything/sub2api/tests/test_core.py::test_account_list_query_matches_native_filters PASSED
cli_anything/sub2api/tests/test_core.py::test_json_parser_requires_exactly_one_source PASSED
cli_anything/sub2api/tests/test_core.py::test_ids_and_params_validation PASSED
cli_anything/sub2api/tests/test_core.py::test_profile_never_persists_credentials PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestLiveBackend::test_health_and_version PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestLiveBackend::test_bad_admin_key_is_an_actionable_error PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestLiveBackend::test_account_update_payload_is_exact PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestCLISubprocess::test_help PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestCLISubprocess::test_status_json PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestCLISubprocess::test_accounts_list_filters_and_json PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestCLISubprocess::test_account_update_json PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestCLISubprocess::test_users_get_dedicated_command PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestCLISubprocess::test_sensitive_export_requires_file PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestCLISubprocess::test_dry_run_never_sends_mutations PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestCLISubprocess::test_missing_credentials_returns_a_clean_error PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestCLISubprocess::test_profile_flow_saves_only_base_url PASSED
cli_anything/sub2api/tests/test_full_e2e.py::TestCLISubprocess::test_api_escape_hatch_records_native_request PASSED

32 passed in 8.20s
```

### Installed-command Release Run

The host Homebrew Python installations currently have a broken `pyexpat` dynamic
link, so release verification used an isolated healthy Python 3.12.12 virtual
environment. The harness was installed with `pip install -e .`, and the test run
required the installed console script:

```bash
PATH="$TEST_VENV/bin:$PATH" CLI_ANYTHING_FORCE_INSTALLED=1 \
  "$TEST_VENV/bin/python" -m pytest cli_anything/sub2api/tests/ -q --tb=short
```

```text
..........................                                               [100%]
26 passed in 7.14s
```

The test collection printed:

```text
[_resolve_cli] Using installed command: .../bin/cli-anything-sub2api
```

This confirms subprocess tests executed the installed entry point rather than a
module fallback.

### Coverage Notes

- The local E2E service validates actual HTTP methods, paths, query strings,
  headers, JSON request bodies, Sub2API envelopes, sensitive export behavior,
  and installed command behavior.
- It intentionally does not connect to a production deployment or exercise every
  administrator route. The generic `api` command covers newly added endpoints;
  operators should use read-only calls and `--dry-run` before production writes.
