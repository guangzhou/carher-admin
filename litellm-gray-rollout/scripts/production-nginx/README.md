# Production nginx integration

The fixture renderer is not a production installer. For each rollout run:

1. Save the complete live nginx configuration as a root-owned mode `0600`
   template without credentials or private-key contents.
2. Add `# @@LITELLM_GRAY_HTTP_DIRECTIVES@@` once inside `http {}`, **before the
   first `map` directive in the http context** (see "Marker placement" below).
   Replace the
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

   **Do not hand-edit the live config to produce this template.** Run
   `scripts/build-base-template.py` instead:

   ```
   python3 scripts/build-base-template.py \
     --live-config /etc/nginx/sites-enabled/<site>.conf \
     --output <run>/nginx/base-template.conf \
     --expect-managed 7 --expect-redirect-only 2
   ```

   It imports the renderer as a module so both tools share one parser, replaces
   only the single `proxy_pass` line inside each managed `/pro` location by byte
   offset, carries `rewrite`/`proxy_set_header`/everything else through
   untouched, writes the output mode `0600`, and prints a structural digest
   (counts, location headers, checksums) rather than the config. The two
   `--expect-*` counts are the drift gate: a live config that no longer matches
   the measured table stops the build instead of silently producing a template
   with a location left unmanaged.

   It refuses rather than guesses on: a managed location with zero or more than
   one `proxy_pass`; a second `proxy_pass` to a variable upstream; a live config
   that already contains gray markers.

   **Marker placement is load-bearing, not cosmetic.** The rendered http block
   sets `map_hash_bucket_size`. nginx binds that value when it parses the
   **first `map` in the http context**, so setting it afterwards is rejected as
   `"map_hash_bucket_size" directive is duplicate` and fails `nginx -t` for the
   whole site. The builder therefore anchors the http marker at the top of the
   file, ahead of every directive — which was only safe to do after measuring
   that on 198 `conf.d/` is empty, `map_hash*` is set nowhere under
   `/etc/nginx`, and this file sorts first in `sites-enabled/`.

   ⚠️ That ordering constraint is outside this repo's control. Adding a
   `conf.d/*.conf` containing a `map`, or a `sites-enabled` file sorting before
   this one, makes the directive a duplicate again. It fails closed at
   `nginx -t`, before any reload — but it fails *during the window*. Re-check
   both directories before freezing the template.

   **WS-split marker.** A managed `/pro` location whose `proxy_pass` target is a
   *variable* is not the product upstream; it is a pre-existing upstream split
   some earlier change made. On 198 (2026-09-13) `location = /pro/v1/responses`
   reads `proxy_pass http://$pro_responses_backend;`, which sends
   `Upgrade: websocket` to the codex incremental terminator. The builder gives
   that location `# @@LITELLM_GRAY_WS_SPLIT_PROXY_DIRECTIVES@@` so the
   pre-existing half survives the gray state machine, and the renderer then
   needs `--ws-split-upstream <name>`. A *second* variable-target location is a
   shape neither tool has seen; the builder stops rather than assume the two
   splits compose.
3. Review and checksum that base template. Do not regenerate it from an
   unreviewed `nginx -T` during a routing transaction.
4. Configure `GRAY_RENDER_CMD` to invoke `render-production-nginx.py` with
   `--generation "$GRAY_GENERATION_DIR"`, the frozen template, a root-only
   debug-token file, the actual nginx candidate path, and
   `--attestation "$GRAY_RENDER_ATTESTATION_FILE"`. Add
   `--ws-split-upstream <name>` if the template carries a WS-split marker, and
   one `--inherited-access-log '<path> <format>'` per server-level `access_log`
   the managed locations were inheriting (see below).
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

A location-scope `access_log` **replaces** the inherited server-level one
rather than adding to it. So on a site whose `server {}` logs to, say,
`/var/log/nginx/zkreq.log`, dropping the gray marker into a `/pro` location
would silently stop that location from writing the log the rest of the site's
tooling reads. Pass each inherited line back with
`--inherited-access-log '/var/log/nginx/zkreq.log zkreq'` (repeatable); the
renderer re-emits it alongside the gray one.

`map_hash_bucket_size` / `map_hash_max_size` are **computed from the
generation's own data**, not hard-coded. nginx hashes every *literal* map key
into a fixed-size bucket and refuses to start — not degrade — when one does not
fit: `could not build map_hash, you should increase map_hash_bucket_size: 64`.
Regex keys (`~`, `~*`) occupy no hash slot, which is why the site's long
auth-token regex keys were harmless while a 64-hex debug token was not: it
makes the `$pool_hdr` key 68 bytes, and under a 64-byte bucket the usable key
length is only about 46 bytes. Since the debug token is operator-chosen (up to
128 bytes) and `key-sid.map` keys are whole virtual keys, a bigger hard-coded
number would only move the cliff. The renderer sizes the bucket from the
longest literal key it is actually about to write, and fails closed above 4096.

This was found by a rehearsal against the real nginx, not by the test suite:
the suite's other real-nginx test drives the *fixture* renderer, so nothing was
showing the production renderer's output to nginx. `test_base_template_builder`
now does, but note the ruler is version-dependent — nginx 1.31 catches the
directive-ordering duplicate while tolerating the oversized bucket that 198's
nginx 1.18 rejects. Rehearse on the target host before the window.
