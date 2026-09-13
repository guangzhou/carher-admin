# Production nginx integration

The fixture renderer is not a production installer. For each rollout run:

1. Save the complete live nginx configuration as a root-owned mode `0600`
   template without credentials or private-key contents.
2. Add `# @@LITELLM_GRAY_HTTP_DIRECTIVES@@` once inside `http {}`. Replace the
   proxy directives in **every** location that can match `/pro` (including
   higher-priority UI/static or regex locations) with
   `# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@`. Keep exactly one literal
   `/pro/` catch-all. The renderer rejects an unmanaged `/pro` location, since
   it would bypass convergence/bridge overrides and keep hitting prod.
   A regex shared by product and another prefix (for example `dev|pro`) must
   be split first; replacing it wholesale would route the other environment
   through the product state machine.

   **Exception — redirect-only locations.** A `/pro` location whose body is an
   unconditional `return 30[12]` with no `proxy_pass`/`try_files`/`*_pass`/
   `error_page` and no nested block cannot reach the product upstream, so it
   cannot bypass convergence/bridge overrides. The renderer exempts it, and
   **rejects** it if it carries a marker anyway: the marker expands to
   `access_log … litellm_gray`, and `collect-metrics.py` turns every matching
   line into a sample, so instant 301s would land in `uri_class=other` — they
   dilute the 5xx denominator *and* drag p95/p99 down. Both directions bias the
   gate toward false green. A `return` inside `if {}` is **not** exempt; the
   fallthrough path may still proxy.

   Measured on 198 (2026-09-13, `sites-enabled/cc.auto-link.com.cn.conf`), by
   running the renderer's own `location_blocks`/`parse_location_header`/
   `is_redirect_only` against the live file:

   | | count |
   |---|---:|
   | locations that can match `/pro` | 9 |
   | of which redirect-only (`= /pro`, `= /pro/ui`) — **exempt** | 2 |
   | **product proxy markers the base template must carry** | **7** |
   | literal `/pro/` catch-all (renderer requires exactly 1) | 1 |

   Re-run that probe on the day; the live config changes.
3. Review and checksum that base template. Do not regenerate it from an
   unreviewed `nginx -T` during a routing transaction.
4. Configure `GRAY_RENDER_CMD` to invoke `render-production-nginx.py` with
   `--generation "$GRAY_GENERATION_DIR"`, the frozen template, a root-only
   debug-token file, the actual nginx candidate path, and
   `--attestation "$GRAY_RENDER_ATTESTATION_FILE"`.
5. Configure `GRAY_NGINX_TEST_CMD`, `GRAY_RELOAD_CMD`, and
   `GRAY_POST_RELOAD_CMD` against that same candidate path.
6. Configure `GRAY_INITIAL_ROLLBACK_CMD` to restore and reload the reviewed
   pre-run live nginx configuration if the initial publish fails.
7. Configure `GRAY_ABORT_VERIFY_CMD` to run the reviewed guarded-old direct
   smoke used between `aborting_to_bridge` and `aborted`.

Production runs must also set:

- `GRAY_RENDER_BASE_TEMPLATE` to that mode `0600` base template.
- `GRAY_RENDER_DEBUG_TOKEN_FILE` to the mode `0600` token file.
- `GRAY_RENDERER_FILE` to the regular renderer file invoked by the command.
- `GRAY_RENDER_OUTPUT` to the nginx candidate path used by `nginx -t`.
- `GRAY_GATE_MAX_AGE_SECONDS` to the approved evidence freshness window
  (default `86400` if it is intentionally not overridden).

Run initialization freezes the exact render command, the base/token/renderer
file identities and checksums, and the output path identity in
`input-checksums.env`. It also freezes SHA-256 digests of
`GRAY_NGINX_TEST_CMD`, `GRAY_RELOAD_CMD`, `GRAY_POST_RELOAD_CMD`,
`GRAY_INITIAL_ROLLBACK_CMD`, `GRAY_ABORT_VERIFY_CMD`, and
`GRAY_GATE_MAX_AGE_SECONDS`. Every generation inherits that manifest. The
initial rollback command is verified against the manifest before the first
render. Before every later production routing transaction stages or renders a
generation, the transaction rejects drift in the test/reload/post-reload
commands; convergence abort also rejects a changed bridge verification command
before switching to the bridge. Evidence consumers reject a changed freshness
window. Test mode deliberately skips these run-contract checks so isolated
failure tests may replace hooks between transactions.

After those checks, the transaction verifies the frozen renderer inputs, exports
`GRAY_GENERATION_DIR` and `GRAY_RENDER_ATTESTATION_FILE`, and requires the
renderer to atomically write a mode `0600` attestation bound to the generation
checksum and rendered output checksum. The output is checked again after
`nginx -t`, before the active generation can switch, so an in-place candidate
change fails closed.

`log_format litellm_gray` is defined at `http` scope, but its `access_log` is
inserted only at each reviewed `/pro` proxy marker. This keeps unrelated
servers and environments out of the gray metrics stream.
