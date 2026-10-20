# 06 — Proxy request pipeline, hooks, config loading, model registry (LiteLLM v1.105.1)

Source: `src/litellm/` (v1.105.1 tag). Live patches: `live-cb/*.py`. All paths are relative to `src/litellm/` unless they start with `live-cb/`.
Legend: **[code]** = I read the code path. **[on-path]** = also confirmed it is reached for our config. **[not verified]** = no runtime evidence; a local repro was not possible (importing `proxy_server` fails locally: `httpx2` and `expression` are not installed).

---

## TL;DR (most important)

1. **Callbacks import is fatal, not skipped.** `get_instance_fn` re-raises (`proxy/types_utils/utils.py:58-65`), `initialize_callbacks_on_proxy` doesn't catch (`proxy/common_utils/callback_utils.py:388-396`), and `load_config` doesn't catch (`proxy/proxy_server.py:6419-6426`). So one broken `module.instance` among the 37 makes the pod fail to boot. A class (instead of an instance), or a non-callable, is also fatal (`callback_utils.py:99-124`). The module is looked up first as `<dirname(config.yaml)>/<module>.py` via `spec_from_file_location`, then falls back to `importlib.import_module` on `sys.path` (`types_utils/utils.py:39-52`).
2. **Model sync interval = `proxy_config_reload_interval_seconds` (default 30s, env `PROXY_CONFIG_RELOAD_INTERVAL_SECONDS`)** (`constants.py:1846`, `proxy_server.py:10721-10757`). On top of that there is a **Redis pub/sub fast path**: every write to `LiteLLM_ProxyModelTable` publishes to `litellm_proxy.config_change` (`common_utils/config_sync_pubsub.py:30,42-60,141-147`, `repositories/table_repositories.py:34-39`). Every pod re-runs `add_deployment` 1–6s later, at most once per 10s (`config_sync_pubsub.py:31-33,244-253`). So `/model/new|update|delete` take effect on the receiving pod synchronously, and on the other 3 pods (all workers) within ~1–6s if Redis pub/sub works, otherwise within ≤30s.
3. **Config always wins (config > DB), and "runtime" writes are rejected for config-owned keys.** `resolve()` returns the yaml value if present (`config_resolvers/settings_rules.py:100-110`). `SettingsStore.__setitem__` raises `ConfigOwnedKeyError` for config-owned keys (`settings_store.py:109-111`). The save path returns HTTP 400 with `stored_database_values_ignored` (`proxy_server.py:5638-5675`). Runtime values sit on top of DB values for DB-owned keys, but are cleared when the DB row changes (`settings_store.py:79-87,145-152`). DB `router_settings` are re-applied to the Router on every 30s reconcile (`proxy_server.py:7516,7727-7745`).
4. **Hook exception semantics differ per hook.** These **fail the request**: `async_pre_call_hook`, `async_moderation_hook`, `async_post_call_success_hook`, the streaming iterator hook, `async_pre_call_deployment_hook`, and `async_filter_deployments`. These are **swallowed**: `async_post_call_failure_hook` (non-HTTPException), the logging success/failure events, `async_post_call_success_deployment_hook` (non-guardrail), `async_post_call_failure_deployment_hook`, `async_post_call_response_headers_hook`, and the per-chunk streaming deployment hooks. Citations are in §4.
5. **`async_pre_routing_hook` on a CustomLogger callback is DEAD in 1.105.1.** The Router only calls it on its own registered pre-routing strategies (auto/complexity/adaptive/quality routers) (`router.py:13796-13909`, `13825-13844`). No code iterates `litellm.callbacks` for it (grep: the only call sites are `router.py:13317,13451,13909` and `auto_router_endpoints.py:583`). So `chatgpt_responses_normalize.async_pre_routing_hook` and `deepseek_responses_adapt.async_pre_routing_hook` **never run**. `deepseek_responses_adapt.py:199-201` claims chatgpt_responses_normalize "actually works via pre_routing". That is false on 1.105.1 [code].
6. **`chatgpt_responses_normalize` and `codex_compaction_v2` gate on `str(call_type)`. `str()` of a `(str, Enum)` member is `'CallTypes.aresponses'` on every Python version (only f-string/`format()` output changed in 3.11/3.12), so the gate is always False when the hook receives a `CallTypes` enum.** This hits the **deployment hook**, which always receives an enum (`utils.py:1480-1484`). `codex_compaction_v2.async_pre_call_deployment_hook` is saved by its `or "responses" in str(call_type)` clause (`live-cb/codex_compaction_v2.py:597`). `chatgpt_responses_normalize.async_pre_call_deployment_hook` (`live-cb/chatgpt_responses_normalize.py:523`) is **dead**. The proxy **pre_call hook** receives a plain string (`route_type`, `common_request_processing.py:2211-2216`), so both pre_call gates do work. Net effect: chatgpt normalize runs exactly once, at the proxy entry, on the *client-requested* model group. It **never runs on fallback hops** or on the post-routing `kwargs`. Locally verified: `str(C.a)` → `C.a` for a `(str, Enum)` (Python 3.12 and 3.14). This does not depend on the pod's Python version.
7. **Hook dispatch is O(callbacks) per hook and mostly capability-gated.** The real O(callbacks × chunks) costs are: (a) the per-chunk `async_post_call_streaming_deployment_hook` loop on the Responses iterator, which runs **for every CustomLogger on every SSE event** (the Protocol check matches every CustomLogger because the base class defines the method) (`responses/streaming_iterator.py:136-137,779-796,1069`); and (b) the iterator-hook chain, one async-generator layer per callback that overrides `async_post_call_streaming_iterator_hook` (`proxy/utils.py:3947-3988`). Ours: `budget_notice`, `streaming_bridge`, plus internal `ResponsesIDSecurity`.
8. **Suspected (not verified at runtime): the 30s reconcile appends our 37 `module.instance` strings to `litellm.callbacks`/`success_callback`/`failure_callback`.** `_update_llm_router` → `_add_callbacks_from_db_config(config_data)` (`proxy_server.py:7512-7513`). `config_data` comes from `get_config()`, which includes the YAML `litellm_settings.callbacks` (`proxy_server.py:6051-6076`). For any string that is not a known integration, `_add_callback_from_db_to_in_memory_litellm_callbacks` calls `add_litellm_callback(callback)` (`proxy_server.py:7537-7543`). That appends the raw string, deduped (`logging_callback_manager.py:142-149,263-267`). Consequences if true: string entries in `litellm.callbacks` are resolved by `get_custom_logger_compatible_class` on uncached paths (`post_call_failure_hook`, `async_post_call_streaming_hook`, `_partition_post_call_callbacks`) and return None. That is harmless but wasteful. The success/failure handlers will treat `'x.y'` strings as legacy callbacks: `_should_run_sync_callbacks_for_async_calls` becomes True (strings are not filtered, `litellm_logging.py:4084-4085,4093-4106`), so **every request also submits `success_handler`/`failure_handler` to the thread executor** (`litellm_logging.py:2194-2204,2243-2245`). **Check on a pod: `GET /active/callbacks` (`proxy/health_endpoints/_health_endpoints.py:1562`) and look for `"budget_notice.budget_notice"`-style strings in `litellm.callbacks` / `litellm.success_callback`.**

---

## 1. Startup order

### 1.1 Entry
`proxy_startup_event` (`proxy/proxy_server.py:1231`):

1. `LITELLM_WORKER_STARTUP_HOOKS` run first; a failure is fatal (`:1256-1278`).
2. `proxy_config.load_config(router=llm_router, config_file_path=CONFIG_FILE_PATH or WORKER_CONFIG)` (`:1288-1313`).
3. Master-key boot verdict (`:1322-1336`).
4. `prisma_client` is created **after** `load_config` (`:1339-1345`). The module-level `prisma_client` starts as None (`:2609`, reset at `:1091`).
5. Coordination Redis is read from DB (`:1400-1405`).
6. `_initialize_startup_logging` → `cost_tracking()` adds `_ProxyDBLogger` + `ShadowEvalLogger` (`:2751-2759`), then `proxy_logging_obj.startup_event` → `_init_litellm_callbacks` (`proxy/utils.py:1234-1250,1379-1425`):
   - `_add_proxy_hooks` appends the internal hooks in `PROXY_HOOKS` order: parallel_request_limiter_v3, cache_control_check, responses_id_security, litellm_skills, max_iterations_limiter, max_budget_per_session_limiter, sensitive_data_routing, prompt_cache_prediction, autorouter_baseline_cache (`proxy/hooks/__init__.py:20-29`; `proxy/utils.py:1349-1371`).
   - The service logger is appended (`:1381`).
   - Every CustomLogger in `litellm.callbacks` is fanned out into success/failure/async lists (`:1412-1417`).
7. `initialize_scheduled_background_jobs` (`:1503-1515`). It includes the **first `add_deployment`**, which loads DB models, credentials, DB general_settings, and DB router_settings (`:10745-10766`).
8. UI settings sync (`:1520`), health checks (`:1523`), model-info refresh job (`:1547-1564`).

### 1.2 Inside `load_config` (`proxy_server.py:6235-7044`)

1. `get_config()` (`:6014-6076`):
   - Reads YAML from a file or bucket (`:6032-6051`).
   - `_load_yaml_settings_stores` loads YAML into the 4 SettingsStores (`:6053`, `5409-5411`).
   - **If `prisma_client` is set and `store_model_in_db is True`**, `_update_config_from_db` overlays the DB `general_settings/router_settings/litellm_settings/environment_variables` rows (`:6056-6061`, `8000-8030`).
   - **On the very first boot call, `prisma_client` is None (created later at `:1339`), so the DB overlay is skipped during `load_config`.** The module-level `store_model_in_db` is also still False (`:2654`); it is set from yaml only later, at `:6763`.
   - Then: resolved settings, secret manager, `os.environ/` resolution, `update_config_state`.
2. `_load_environment_variables`, coordination Redis, `callback_settings` (`:6275-6285`).
3. **`litellm_settings` keys in YAML dict order** (`:6303-6641`):
   - `callbacks` → `initialize_callbacks_on_proxy` (`:6419-6426`).
   - `success_callback`/`failure_callback` dotted strings → `get_instance_fn` (`:6472-6517`).
   - Everything else → `setattr(litellm, key, value)` (`:6620-6628`).
   - **Note:** a key that is consumed while processing `callbacks` (e.g. a callback-specific param) must already be in `litellm_settings`; ordering only matters for side effects that read `litellm.<attr>` at callback `__init__`.
4. `general_settings` (`:6651-6888`): `store_model_in_db` (`:6763-6766`), `custom_auth` and friends via `get_instance_fn`, `pass_through_endpoints`, `proxy_config_reload_interval_seconds` (`:6845`), `proxy_batch_write_at` (`:6843`).
5. YAML `model_list` (`:6903-6930`) and YAML `router_settings` (`:6968-6991`).
6. `litellm.Router(**router_params, ignore_invalid_deployments=True, ...)` (`:6992-7003`).
7. Guardrails v2, policies, prompts, credentials, MCP/agents/vector stores (`:7009-7041`).

### 1.3 After `load_config`: DB layer (first `add_deployment`, `proxy_server.py:8101-8187`)

`sync_ui_settings_to_general_settings` → lock → `prefetch_config_params` → `_get_models_from_db` (writer-pinned, `:8073-8099`) → `get_credentials` → `_update_llm_router` (`:7443-7518`). That function does:

- `get_config()`: re-reads YAML **and now** overlays DB rows, because `prisma_client` exists.
- `_delete_deployment` → `_add_deployment` (upsert DB rows).
- `_add_callbacks_from_db_config(config_data)`.
- `_add_router_settings_from_db_config` (DB `router_settings` row → `router_settings.apply_db_row` → `Router.update_settings`).

After that: DB `general_settings` → `_update_general_settings` (`:8166-8174`), then `_init_non_llm_objects_in_db` (`:8177`).

**So the effective order is:** YAML (all sections) → callbacks constructed (during YAML processing, before any DB read) → Router built from YAML models + YAML router_settings → prisma connect → DB models + DB settings + DB router_settings overlaid → `_add_callbacks_from_db_config`.

### 1.4 How `module.instance` resolves (`proxy/types_utils/utils.py:9-65`)

- The string is split on dots: module = all parts but the last, instance = the last part (`:32-36`).
- If `config_file_path` is set, it tries `os.path.join(dirname(config_file_path), *module.split(".")) + ".py"`. If that file exists, it is loaded via `spec_from_file_location(module_name, path)` + `exec_module` (`:39-50`). **This creates a fresh module object each time it is called and does not put it in `sys.modules`.** Anything else that imports the same module by name gets a *different* module instance.
- Otherwise it uses `importlib.import_module(module_name)` (`:51-52`), resolved via `sys.path`.
- `getattr(module, instance_name)` (`:55`). An ImportError is re-raised as `ImportError("Could not import X from Y")` (`:58-63`); any other exception is re-raised (`:64-65`).
- **Consequence for us:** if the live-cb files sit next to config.yaml, each callback module is exec'd by `get_instance_fn`. If a file is also imported by name elsewhere (e.g. `sitecustomize` imports `null_byte_sanitize`, or one callback imports another), there will be **two module copies**. Import-time monkey-patches then run twice. The `_xxx_patched` idempotency flags most of our patches set (e.g. `live-cb/budget_notice.py:2649-2667`) are what protect against double-wrapping. Where the patched files actually live on the pod is not verified.

### 1.5 Import failure

Fatal. The chain is: `get_instance_fn` raises (`types_utils/utils.py:58-65`) → `_loaded_callback_or_raise` doesn't catch (`callback_utils.py:388-396`) → `load_config` doesn't catch (`proxy_server.py:6419-6426`) → `proxy_startup_event` doesn't catch (`:1292-1298`). The lifespan fails, so the pod CrashLoops. Also fatal: resolving to a class or a non-callable (`callback_utils.py:91-131`). This is good (no silent skip), but **one bad patch file takes down a rollout**. There's no per-callback isolation.

---

## 2. Model registry

### 2.1 Merge of config.yaml `model_list` and `LiteLLM_ProxyModelTable`

- **Boot:** the Router is built from YAML only (`proxy_server.py:6903-6905,6992`). DB rows are added by `_add_deployment` → `Router.upsert_deployment` for each row (`:7373-7413`).
- **Identity is by deployment id, not by name.** The DB row id is `model_id` (`get_model_info_with_id`, `:7250-7254`, which also sets `db_model=True` and `blocked`). Config models use `model_info.id`, or `generate_model_id(model_name, litellm_params)`, a hash (`:7327-7336`; `router.py:8768`).
- **Name collision (same `model_name` in YAML and DB):** there's no special handling. Both deployments live in the same model group and are load-balanced together (Router groups by `model_name`). The 96 YAML + 523 DB rows → 192 names are therefore merged per group. This is the intended behavior, not an override. The only way a DB row "replaces" a YAML one is an identical id.
- **Id collision (DB `model_id` == YAML `model_info.id`):** `upsert_deployment` compares name, params, and model_info. If they differ, it pops and re-adds, so the **DB version wins** in memory (`router.py:9925-9965`). `_delete_deployment` keeps the id because it's in both lists (`proxy_server.py:7304-7361`). `/model/update` on such an id will find the DB row and edit it. `/model/update` on a YAML-only id → 400 "Model in config" (`model_management_endpoints.py:2605-2612`).
- **Deletes:** each reconcile evicts every router id that is not in (DB ids ∪ YAML ids) (`proxy_server.py:7339-7361`).
  - If YAML read fails, cleanup is skipped (`:7310-7318`).
  - If YAML has no `model_list`, YAML-sourced deployments are kept (`:7342-7357`).
  - If the DB fetch failed (returns None), the whole update is skipped and nothing is evicted (`:7466-7473`, `8095-8099`).
- `_add_deployment` skips rows whose `litellm_params` is not a dict, with an error log (`:7390-7400`). `upsert_deployment` failures are dropped with a warning, because of `ignore_invalid_deployments` (`router.py:9978-9988`).

### 2.2 Re-sync frequency / triggers (per worker process; each uvicorn worker runs its own scheduler and subscriber)

| Trigger | Where | Interval |
|---|---|---|
| APScheduler `add_deployment_job` | `proxy_server.py:10745-10757` | `proxy_config_reload_interval_seconds` (yaml `general_settings`, default `PROXY_CONFIG_RELOAD_INTERVAL_SECONDS`=30, `constants.py:1846`). `coalesce`, `max_instances=1` (`constants.py:1850-1857`), staggered per replica (`proxy_server.py:10978-10981`) |
| Redis pub/sub `litellm_proxy.config_change` | `config_sync_pubsub.py:163-277`, started at `proxy_server.py:8189-8206,10762-10766` | Event-driven: 1s debounce + 0–5s jitter, ≥10s between resyncs (`config_sync_pubsub.py:31-33,249-253`). Messages are drained, so a burst produces one resync |
| Publisher | Any create/update/upsert/delete through a `PrismaTableRepository.table` on `litellm_proxymodeltable` (and 14 other tables) (`table_repositories.py:34-39`; `config_sync_pubsub.py:38-60,125-160`); config-row writes via `invalidate_config_param` (`proxy/utils.py:4304-4307`; `config_sync_pubsub.py:115-122`) | — |
| `LiteLLM_Config` row cache | `proxy/utils.py:4252-4297` | DualCache TTL 60s (`LITELLM_CONFIG_PARAM_CACHE_TTL_SECONDS`). Every `add_deployment` re-warms it with `prefetch_config_params` (`proxy_server.py:8147-8156`), so settings reads are effectively fresh on each reconcile |

Pub/sub requires the coordination Redis (`redis_usage_cache`). With no Redis, there's only the 30s poll (`proxy_server.py:8195-8196`). Whether pub/sub is active on our pods is not verified; a pod-log check is "config sync subscriber redis error".

### 2.3 `/model/new`, `/model/update`, `/model/delete` on the receiving pod vs the others

- **`/model/new`** (`model_management_endpoints.py:2357-2539`):
  - DB `create` through the wrapped table, which publishes the pub/sub message (`:1510-1525`, `table_repositories.py:34-39`).
  - Then a **synchronous `proxy_config.add_deployment()` on the receiving worker only** (`:2486-2488`).
  - `raise_if_reload_degraded_serving` fails the call if the reload evicted things (`:2531-2537`).
  - Other workers in the same pod and the other 3 pods: pub/sub resync (~1–6s) or the next poll (≤30s).
- **`/model/update`** (`:2567-2735`): DB update (wrapped, publishes) → `clear_cache()` (`:2732`, `3224-3300`). That runs under `MODEL_RECONCILE_LOCK`: it wipes only auto-router DB deployments, then calls `_add_deployment_locked`. Ordinary DB deployments are converged by upsert, so there's no serving hole (`:3279-3289`). Same propagation to others.
- **`/model/{id}/update`** (PATCH, `:1089`) — not traced in detail.
- **`/model/delete`** (`:2182-2310`): DB delete (wrapped, publishes) → under `MODEL_RECONCILE_LOCK`, `llm_router.delete_deployment(id)` on the receiving worker only (`:2253-2256`). Others evict on resync, because `_delete_deployment` removes ids no longer in the DB (`proxy_server.py:7359-7361`).
- **Gotcha:** `MODEL_RECONCILE_LOCK` is an in-process `asyncio` lock. It serializes reconciles only within one worker; cross-worker/pod ordering relies on the writer-pinned read (`proxy_server.py:8082-8091`).

### 2.4 `blocked` rows

- **Loading:** `blocked` is copied into `model_info.blocked` (`proxy_server.py:7254`). Changing `blocked` changes `model_info`, so `upsert_deployment` re-adds the deployment (`router.py:9939-9944`; `model_management_endpoints.py:3286-3288`).
- **Routing:** blocked deployments are filtered on every routing entry: `_filter_blocked_deployments` (`router.py:14495-14509`), called at `8597, 8628, 13082, 14220, 14401`.
- **Fully blocked group:**
  - `route_request` raises 403 "Model is blocked" (`proxy/route_llm_request.py:58-73,545-546`), **only on the plain-router branch**.
  - On the `router_settings_override` branch (keys/teams with per-key router_settings, `:516-544`) the explicit 403 is skipped. The request then fails later with a no-healthy-deployments error. This matches main-thread notes; it is still blocked, but the error shape differs.
  - A specific deployment that is blocked → `ServiceUnavailableError` "paused" (`router.py:13041-13046,13485-13490`).
- **Listing:** `/v1/models` hides fully blocked names (`router.py:11736-11756`; `proxy_server.py:11643,11843`).
- **Fallbacks:** a fallback hop into a blocked group goes through the same filter, so a blocked leg acts as "no deployments" and the walk continues. That follows from the filter being on the common path. I didn't trace the fallback-specific error class.

---

## 3. Settings precedence

### 3.1 The rule
`settings_rules.resolve` (`proxy/config_resolvers/settings_rules.py:100-110`) is "**Config wins**". If YAML declares the key, it is config-owned, whatever the DB holds. Otherwise the DB value is used (a stored null counts as absent, `:113-118`). Otherwise unset.

`SettingsStore.__getitem__` (`settings_store.py:99-107`) gives this order: deleted-runtime → **runtime** value → resolve(yaml, db).

So the effective precedence is **runtime > config > DB, but runtime can never differ from config for a config-owned key**: `__setitem__` raises `ConfigOwnedKeyError` if `owned_by_config(key) and value != current` (`:109-111`); `__delitem__` raises too (`:115-119`). The real precedence is therefore **config > runtime > DB**. Runtime writes to DB-owned keys are dropped when the DB row for that key changes (`apply_db_row` → `_clear_runtime_keys`, `:79-87,145-152`).

**Your production `source` map (DB owns fallbacks/alias/num_retries/timeout/routing_strategy/model_group_retry_policy; config owns cooldown_time/allowed_fails/redis/pre-call checks) is exactly what this rule produces** when those keys are absent from / present in the YAML `router_settings`.

### 3.2 How often DB router_settings are re-read
On every `add_deployment` (30s poll + pub/sub). `_update_llm_router` → `_add_router_settings_from_db_config` (`proxy_server.py:7516,7727-7745`) → `router_settings.apply_db_row` → `resolved()` → `_apply_router_settings` → `Router.update_settings(...)` (`:7747-7759`).

`Router.update_settings` only applies `RUNTIME_UPDATABLE_ROUTER_SETTINGS` (`router.py:12227-12291`; `constants.py:13-33`):
- routing_strategy(_args), routing_groups, allowed_fails, cooldown_time, num_retries, timeout, max_retries, retry_after, fallbacks, context_window_fallbacks, retry_policy, model_group_retry_policy, model_group_alias, enable_weighted_failover, enable_tag_filtering, tag_routing_prefix, optional_pre_call_checks.
- Others are debug-logged and ignored (`router.py:12281-12282`).
- **`routing_strategy` re-init only happens if the value changed** (`:12262-12278`).

**Note:** because the resolved settings include the config-owned keys too, the YAML `cooldown_time`/`allowed_fails` are re-applied every 30s. That's harmless, but it means a runtime poke at `llm_router.cooldown_time` (e.g. via a monkey-patch) is overwritten within 30s.

`general_settings` DB row → `_update_general_settings` (`proxy_server.py:7833-7847`) on each reconcile, with side effects (`:7855-7871`).

`litellm_settings` DB row: only `LITELLM_SETTINGS_SAFE_DB_OVERRIDES` are `setattr`'d onto `litellm` (`proxy_server.py:8058-8062`; `constants.py:1891-1911`). So a DB-only `litellm_settings.callbacks` does **not** re-run `initialize_callbacks_on_proxy`; DB callbacks go through `_add_callbacks_from_db_config` (string-only path, §TL;DR 8).

### 3.3 Admin UI/API write to a config-owned key
- `save_config` → `_save_changed_config_section` → `reject_config_owned_writes` → **HTTP 400**, with `keys`, `section`, `stored_database_values_ignored`, and `resolution` (`proxy_server.py:5596-5675`).
- Direct store writes (e.g. `/allowed_ips`) catch `ConfigOwnedKeyError` → 400 (`ui_crud_endpoints/proxy_setting_endpoints.py:598-610`).
- `/config/update` validates router_settings keys against the runtime-updatable set + valid Router args, else 400 (`proxy_server.py:17981-17994`).
- At startup and each reconcile, a WARNING is logged once per shadowed key, for DB values shadowed by config (`proxy_server.py:8029,8032-8040`; message `settings_store.py:30-37`).

---

## 4. Hook dispatch for one request

`route_type` (string) = call_type passed to the proxy hooks: chat `"acompletion"` (`proxy_server.py:12007`), responses `"aresponses"` (`response_api_endpoints/endpoints.py:373`), messages `"anthropic_messages"` (`anthropic_endpoints/endpoints.py:155`).

### 4.1 Order

1. **Auth** (`Depends(user_api_key_auth)`). Budget check via `_virtual_key_max_budget_check` (`proxy/auth/user_api_key_auth.py:2421`).
2. `common_processing_pre_call_logic` (`common_request_processing.py:1913-2241`):
   1. `add_litellm_data_to_request` (`:2034-2041`). It strips client-supplied `mock_response`/`mock_tool_calls` and other control fields unless the key/team metadata allows it (`litellm_pre_call_utils.py:260-272,2018-2027`). **A pre_call_hook that sets `data["mock_response"]` runs *after* this strip, so it is honored.**
   2. Model resolution, `litellm.model_alias_map`, key aliases (`:2076-2107`).
   3. Key/team router_settings → `router_settings_override` (+ per-request `model_group_alias`) (`:2115-2143`).
   4. `litellm_call_id`, stream usage options (`:2145-2164`).
   5. **`function_setup(original_function=route_type)`** creates the Logging object (`:2169-2176`; `utils.py:981+`). It also fans `litellm.callbacks` (+ dynamic) into the success/failure lists each request (`utils.py:1012-1037`, deduped).
   6. Model-level guardrails merged (`:2187-2198`).
   7. **`proxy_logging_obj.pre_call_hook(call_type=route_type)`** (`:2211-2216`) — §4.2.
3. `_pre_call_with_fallbacks`: on `ProxyRateLimitError` raised from pre-call, it re-runs **the whole pre-call logic (all pre_call hooks again)** for each fallback model group on a pristine snapshot (`common_request_processing.py:2243-2340`). So **pre_call hooks can run more than once per request** when a local rate limit trips.
4. `_process_llm_request` (`:2526`):
   - `during_call_hook` is started as a task (`:2600-2611`).
   - `route_request` (`:2619-2625`).
   - The LLM call task runs **in parallel**, via `asyncio.gather` (`:2626-2637`). If during_call raises, the gather raises, so it fails the request; pending tasks are cancelled (`:2636-2637`).
5. **Inside the Router per attempt** (each retry and each fallback hop re-enters `litellm.acompletion`/`aresponses`/`anthropic_messages`, all `@client`-wrapped):
   - Deployment selection: `async_filter_deployments` for each CustomLogger (`router.py:8715-8755`, called at `13084`), then `async_pre_call_check` (`router.py:8664-8690`).
   - `wrapper_async` (`utils.py:1976+`): **`async_pre_call_deployment_hook`** for every CustomLogger in `litellm.callbacks` order, chained (`utils.py:2009-2011`, `1473-1493`).
   - `original_function(...)`.
   - On failure: `async_post_call_failure_deployment_hook` (`utils.py:2103-2113`), then `logging_obj.failure_handler` (sync) + `async_failure_handler` (`utils.py:2208-2223`).
   - On non-stream success: `async_post_call_success_deployment_hook` (`utils.py:2153-2159`), then success logging scheduled as a task (`utils.py:1347-1408`).
   - Responses API (non-proxy/sync path) also runs the deployment hook inside the HTTP handler, when any callback overrides it (`llms/custom_httpx/llm_http_handler.py:343-350,2414-2460,2545`).
   - Anthropic `/v1/messages` also runs **`async_pre_request_hook`** for every CustomLogger (`llms/anthropic/pass_through/messages/handler.py:158-173`).
6. Back in the proxy:
   - Streaming: `post_call_response_headers_hook` (`:2670-2675`), then the route-specific generator. Messages use `async_sse_data_generator` (`:2774-2798`); responses use `native_data_generator` + container wrap (`:2800-2814`); chat uses `select_data_generator`. All go through `async_streaming_data_generator` (`common_request_processing.py:3867-4017`):
     - `async_post_call_streaming_iterator_hook` chain (§4.4).
     - Per chunk, `async_post_call_streaming_hook` (only if some callback overrides it or a guardrail exists, `:3902-3928`).
     - On exception, `post_call_failure_hook` (`:3979-3989`).
   - Non-streaming: `post_call_success_hook` (`:2859-2863`) — §4.3.
   - Any exception → `_handle_llm_api_exception` → `post_call_failure_hook` → `post_call_response_headers_hook(response=None)` (`:3625-3692`).
7. Logging success/failure events: `async_log_success_event` / `async_log_failure_event` / `log_success_event` (§4.6).

### 4.2 `pre_call_hook` (`proxy/utils.py:2368-2581`)

- **Iterates `caps.resolved_callbacks` = `litellm.callbacks` in list order** (strings resolved to known integrations; unresolved strings dropped) (`:2454,2482`; caps built at `:2778-2878`).
- Fast exit if no callback overrides `async_pre_call_hook` and there are no guardrails (`:2460-2467`).
- A CustomLogger is called only if `async_pre_call_hook` is in **its own class `__dict__`** and differs from the base (`:2504-2510`). **An inherited override from an intermediate base class is NOT called.** All of ours define it on the leaf class (OK).
- Signature called: `(user_api_key_dict=, cache=, data=, call_type=)`, keyword args (`:2523-2528`).
- **Return value contract** (`process_pre_call_hook_response`, `:1768-1783`):
  - dict → **replaces `data`**;
  - Exception instance → raised;
  - str → reject: `RejectedRequestError` (returned as a 200-style canned chat response) for `completion`/`text_completion`, **else HTTP 400**. Note `"acompletion"` is not in that list, so chat via the proxy gets HTTP 400;
  - None → keep data.
- **Exceptions:** anything except `SensitiveDataRouteException` propagates and **fails the request** (`:2534-2581`).
- `mock_response`: setting `data["mock_response"]` in a pre_call hook survives to the call. The proxy only strips client-supplied `mock_response` before the hooks run (`litellm_pre_call_utils.py:2022-2027`). `GATED_MOCK_PARAM_NAMES` (mock_testing_*, mock_timeout, mock_delay) are rejected in `route_request` unless `dangerously_allow_mock_testing_request_params` is set (`route_llm_request.py:19-28,207-218,480`). So pre_call hooks must not set those, but `mock_response` is fine (`mock_heartbeat`, `budget_notice` rely on this).

### 4.3 `post_call_success_hook` (`proxy/utils.py:3494-3601`)
Pipelines → guardrails (sequential, then parallel) → **all non-guardrail CustomLoggers in `litellm.callbacks` order** (`_partition_post_call_callbacks`, `:590-607`; loop `:3593-3598`). This is **not capability-gated**: every CustomLogger's (mostly no-op) coroutine is awaited. A non-None return replaces `response`. Exceptions propagate and **fail the request** (`:3599-3600`).

### 4.4 Streaming iterator chain (`proxy/utils.py:3905-4021`)
- Built from `caps.iterator_overrides`: callbacks with `async_post_call_streaming_iterator_hook` in their leaf-class `__dict__`, or guardrails with `apply_guardrail`, **in `litellm.callbacks` order** (`:2831-2835`).
- Each wraps the previous: `current = wrap(cb, current)` (`:3947-3988`). So the **first callback in the list is innermost (closest to upstream) and the last is outermost (closest to the client)**.
- Exceptions propagate to `async_streaming_data_generator`, which runs the failure hook and emits an error frame (`common_request_processing.py:3979-4007`).
- **Our layer:** `budget_notice` monkey-patches `ProxyLogging.async_post_call_streaming_iterator_hook` itself, to wrap non-iterable `ResponsesAPIResponse` (`live-cb/budget_notice.py:2643-2667`). The patch keeps the `(self, response, user_api_key_dict, request_data)` signature, which still matches 1.105.1 (`proxy/utils.py:3905-3910`) (OK).

### 4.5 `during_call_hook` (`proxy/utils.py:2925-2992`)
Only for guardrails or callbacks overriding `async_moderation_hook` (MRO walk, `:1141-1147`). Runs them in parallel via `gather`; any exception **fails the request**. None of our live-cb override `async_moderation_hook`, so this is a no-op for us (fast exit at `:2931-2933`).

### 4.6 Failure hook and logging callbacks
- `post_call_failure_hook` (`proxy/utils.py:3197-3314`): iterates **raw `litellm.callbacks`** (strings resolved each time, `:3282-3290`).
  - An HTTPException returned or raised → first one wins and **replaces the error to the client**.
  - Any other exception → logged "[Non-Blocking]" and swallowed (`:3299-3312`).
  - It pops `litellm_logging_obj` from `request_data` before iterating (`:3275`). **Failure hooks never see the logging object.**
- `post_call_response_headers_hook` (`:3718-3775`): capability-gated; exceptions swallowed; later callbacks override earlier headers.
- Success logging: `async_success_handler` runs as a background task (`utils.py:1392-1404`, `asyncio.create_task`).
  - `async_logging_hook` loop, then `async_log_success_event` loop, over `litellm._async_success_callback` (+ dynamic) (`litellm_logging.py:3392-3576`). Each callback is wrapped in try/except "[Non-Blocking]" (`:3433-3438,3571-3576`).
  - Sync `success_handler` is submitted to a thread executor only if non-CustomLogger, non-internal callbacks exist (`litellm_logging.py:2194-2204,4006-4018`).
- Failure logging: called **inline, not as a task**, in `wrapper_async` (sync `failure_handler` first, then `await async_failure_handler`; "DO NOT MAKE THREADED - router retry fallback relies on this") (`utils.py:2213-2223`). A slow `async_log_failure_event` therefore **directly delays the retry/fallback**. `weighted_affinity.async_log_failure_event` does a cache write there (`live-cb/weighted_affinity.py:543-572`).
- **Dedup:** `has_logged_async_failure` is set after the first failure **on the shared logging object**, and `should_run_logging` blocks later failure logs (`litellm_logging.py:2249-2279,3909,3925`; acknowledged in `router_utils/fallback_event_handlers.py:55-57`). Unless the router gives each attempt a fresh logging object, `async_log_failure_event` fires **once per request, not per attempt**.
  - `weighted_affinity`'s docstring assumes it fires on every retry (`live-cb/weighted_affinity.py:547`), and `observability_attempt_pusher` wants attempt-level events (`live-cb/observability_attempt_pusher.py:5-8`).
  - Whether the router reuses the logging object across retries/fallbacks: **not verified** (see `deep/01-router.md` if it covered this). The deployment-level signal that does fire per attempt is `async_post_call_failure_deployment_hook` (`integrations/custom_logger.py:303-349`; `utils.py:2103-2113`).
  - Streaming sets `stream=True`, which skips `has_run_logging` (`litellm_logging.py:2273-2277`).

### 4.7 Which hooks run for which route
| Hook | chat `/v1/chat/completions` | `/v1/responses` | `/v1/messages` |
|---|---|---|---|
| proxy `async_pre_call_hook` call_type (str) | `acompletion` | `aresponses` | `anthropic_messages` |
| `function_setup` call_type | same | same | same (`utils.py:1133-1137` treats it like completion for pre-call rules) |
| `async_pre_call_deployment_hook` call_type (**CallTypes enum**) | `CallTypes.acompletion` | `CallTypes.aresponses` | `CallTypes.anthropic_messages` (`handler.py:228` `@client`) |
| `async_pre_request_hook` | — | — | yes (`anthropic/pass_through/messages/handler.py:158-173`) |
| per-chunk `async_post_call_streaming_deployment_hook` | final chunk only (`streaming_handler.py:2050-2053`) | **every event** (`responses/streaming_iterator.py:1069`) | not traced |
| proxy `async_post_call_streaming_hook` | yes, if overridden | yes, if overridden | yes, if overridden |
| metadata bucket | `metadata` | `litellm_metadata` | `litellm_metadata` (`litellm_pre_call_utils.py:239-245,655-662`) |

When `/v1/messages` is bridged to a chat or responses provider, nested `@client` wrappers run, and the deployment hook may fire again with the inner call_type. I didn't trace this per provider.

---

## 5. Per-request overhead with 37 callbacks (+ ~12 internal)

`litellm.callbacks` holds our 37, plus internal: 9 PROXY_HOOKS, service logger, `_PROXY_VirtualKeyModelMaxBudgetLimiter` (`proxy_server.py:2617`), `_ProxyDBLogger`, `ShadowEvalLogger`. That's roughly 50 entries, plus possible string entries (TL;DR 8).

| Hook | Iteration | Gated? |
|---|---|---|
| pre_call_hook | `resolved_callbacks` (cached, `utils.py:2775-2878`), `vars(cls)` check each; `time.time()` ×2 per callback | Skipped entirely only if no overrides; we have many, so the full walk runs |
| async_filter_deployments | every CustomLogger, awaited, **per deployment selection** (each retry/hop) (`router.py:8736-8755`) | No |
| async_pre_call_check | every CustomLogger, per selection (`router.py:8664`) | No |
| async_pre_call_deployment_hook | every CustomLogger, per attempt (`utils.py:1489-1493`) | No (except the HTTP-handler sync path) |
| async_pre_request_hook (messages) | every CustomLogger | No |
| post_call_success_hook | every non-guardrail CustomLogger + `_partition_post_call_callbacks` rebuilt per call (`utils.py:590-607`) | No |
| post_call_failure_hook | every callback, strings re-resolved | No |
| success logging | every async success callback (`async_logging_hook` + `async_log_success_event`) | Background task |
| failure logging | every async failure callback, **inline on the retry path** | — |
| post_call_response_headers | callbacks overriding it (`error_sanitize`) | Cached capability |
| during_call | none for us | Cached capability |

**O(callbacks × chunks):**
- **Responses streaming:** `_call_post_streaming_deployment_hook` runs on **every event**. It loops over every `litellm.callbacks` entry that satisfies `isinstance(cb, _HasPostStreamingDeploymentHook)`. That's a `runtime_checkable` Protocol whose only member, `async_post_call_streaming_deployment_hook`, exists on the CustomLogger base (`integrations/custom_logger.py:351-361`), so **all ~50 CustomLoggers match**. Each no-op coroutine is awaited per SSE event (`responses/streaming_iterator.py:136-137,759-799,1069`). On top of that, a runtime-checkable Protocol `isinstance` costs a `hasattr` per entry. So a 2,000-event Codex stream is ≈100k coroutine awaits. This is the main per-chunk callback cost on `/v1/responses`. Chat only does it on the final chunk (`streaming_handler.py:2050-2053`).
- **Iterator-hook chain:** one extra async-generator hop per overriding callback per chunk (`budget_notice`, `streaming_bridge`, `ResponsesIDSecurity` → 3 layers + `_wrap_streaming_iterator_with_enrichment` per layer, `proxy/utils.py:2755-2770`). When `iterator_overrides` is non-empty, `served_chunks.append` also keeps **every chunk in memory for the whole stream** (`proxy/utils.py:4002-4006`).
- Per-chunk `async_post_call_streaming_hook`: only if a callback overrides it or a guardrail exists (`common_request_processing.py:3902-3904`). None of our live-cb override it. Internal guardrails not checked.

---

## 6. live-cb dependencies on ordering / call_type / signatures

### 6.1 Signatures vs `integrations/custom_logger.py` (1.105.1)
All match the base signatures (base: `:189,192,266-274,276-278,413-422,442-449,470-476,491-497,522-529`):

| File | Hook(s) | Status |
|---|---|---|
| baiyu_image_route, client_metadata_strip, cursor_web_fc_sys_rewrite, embedding_sanitize, force_stream, mock_heartbeat, opus_47_fix, openrouter_provider_pin, codex_ua_pool_route, budget_notice | `async_pre_call_hook(user_api_key_dict, cache, data, call_type)` | OK (called with kwargs, `proxy/utils.py:2523-2528`) |
| deepseek_id_prefix | pre_call + `async_post_call_success_hook(data, user_api_key_dict, response)` | OK |
| budget_notice, error_sanitize | `async_post_call_failure_hook(request_data, original_exception, user_api_key_dict=None, traceback_str=None)` | OK |
| error_sanitize | `async_post_call_response_headers_hook(..., litellm_call_info=None)` | OK; receives `litellm_call_info` (`proxy/utils.py:3755-3762`) |
| budget_notice, streaming_bridge | `async_post_call_streaming_iterator_hook(user_api_key_dict, response, request_data)` | OK |
| weighted_affinity | `async_filter_deployments(model, healthy_deployments, messages=None, request_kwargs=None, parent_otel_span=None)`; `async_log_failure_event` | OK |
| observability_attempt_pusher, dead_deployment_retry, budget_notice | `async_log_success_event` / `async_log_failure_event` / `log_success_event` / `async_log_audit_log_event` | OK |
| chatgpt_responses_normalize, cursor_responses_shim, codex_compaction_v2, deepseek_responses_adapt, grok_*, local_gpu_max_tokens_clamp, reasoning_effort_minimal_normalize | `async_pre_call_deployment_hook(kwargs, call_type)` (positional call, `utils.py:1491`) | OK |
| chatgpt_responses_normalize, deepseek_responses_adapt | `async_pre_routing_hook(...)` | **Signature OK but never called (dead).** §TL;DR 5 |

### 6.2 call_type gating (`CallTypes` is `(str, Enum)`, `types/utils.py:449`; deployment hook gets the enum, `utils.py:1480-1484`; proxy pre_call gets the str)

| File | Gate | pre_call | deployment |
|---|---|---|---|
| chatgpt_responses_normalize | `str(call_type) in {"responses","aresponses"}` (`:34,515,523`) | works (str) | **dead** (enum → `'CallTypes.aresponses'`) |
| codex_compaction_v2 | `str(..) in set or "responses" in str(..)` (`:110,584,597`) | works | works via substring |
| codex_ua_pool_route | `str(call_type) in {...}` (`:46,167`) | works | n/a |
| deepseek_responses_adapt, grok_agent_message_flatten | `.value` (`:192-203`; `:44-47`) | works | works |
| cursor_responses_shim | `_call_type_value` = `.value` (`:73-75,411`) | works | works |
| force_stream | `call_type not in {"anthropic_messages"}` (`:113,160`) | works (str; enum `in` frozenset also works since `CallTypes` hashes/eq as str) | n/a |
| mock_heartbeat, embedding_sanitize | `call_type not in frozenset(...)` | works | n/a |
| local_gpu_max_tokens_clamp, reasoning_effort_minimal_normalize, grok_gif_to_png, grok_web_search_preview_rename | gate by api_base / model, not call_type | — | works |

Membership tests (`enum in {"aresponses"}`) work because a str-Enum compares and hashes equal to its value. Only `str(enum)` is broken, and it is broken on every Python version.

### 6.3 Ordering dependencies
- **pre_call chain order = `litellm.callbacks` order.** Internal PROXY_HOOKS are **appended after** our callbacks: ours are added during `load_config` (`proxy_server.py:6419`), PROXY_HOOKS later in `startup_event` (`proxy/utils.py:1355-1369`). So **ours run before** parallel_request_limiter_v3, cache_control_check, responses_id_security, max_budget_per_session, and so on.
  - Exception: `_PROXY_VirtualKeyModelMaxBudgetLimiter`, added at import time (`proxy_server.py:2617`), comes **before** ours, but it doesn't define `async_pre_call_hook`.
  - Consequence: a pre_call hook of ours that rewrites `data["model"]` (deepseek_responses_adapt `-responses` twin rewrite, `live-cb/deepseek_responses_adapt.py:985-993`) runs before the internal rate limiter sees the request. Rate limits are then charged to the rewritten group.
  - A pre_call that returns `mock_response` still lets later internal pre_call hooks run (the limiter reserves a slot).
- `deepseek_id_prefix.async_pre_call_hook` must see `tools` before `deepseek_responses_adapt` hoists them (`live-cb/deepseek_id_prefix.py:444-446`). Both are pre_call, so this **depends on config list order** (deepseek_id_prefix must come first). The YAML order wasn't available to me: not verified.
- `budget_notice`'s docstring says `_PROXY_MaxBudgetLimiter` "runs before us in the hook chain" (`live-cb/budget_notice.py:2726-2731`). **That class doesn't exist in 1.105.1** (no `proxy/hooks/max_budget_limiter.py`). `_patch_proxy_max_budget_limiter` hits its `except: return` (`:2732-2735`), so that patch is a **silent no-op**. The remaining 1.105.1 budget raises are in `auth/auth_checks.py`, `auth/user_api_key_auth.py`, `spend_tracking/budget_reservation.py`, and `hooks/model_max_budget_limiter.py`; budget_notice patches the first three (`:2709-2723,2763-2800`). **`_PROXY_VirtualKeyModelMaxBudgetLimiter` (per-model budget, via `async_filter_deployments`) is not covered.** That's harmless if no keys use per-model budgets; not verified.
- Iterator chain order: `budget_notice` vs `streaming_bridge` vs `ResponsesIDSecurity` nesting follows list order (`proxy/utils.py:3947-3988`). ResponsesIDSecurity (internal) is last, so it is outermost.
- `error_sanitize` relies on running in `post_call_failure_hook` *after* retries/fallbacks are exhausted (`live-cb/error_sanitize.py:74-80`). That's correct: the hook only runs from `_handle_llm_api_exception` / the streaming generator (`common_request_processing.py:3635,3983`).
- Monkey-patch stacking order (`BaseRespIter._process_chunk`: codex_compaction_v2 → streaming_output_backfill → deepseek_id_prefix) depends on import order, i.e. callback list order plus sitecustomize. This is already captured in `eff-table.txt`.

---

## 7. Surprising / risky / likely misunderstood

1. **"DB hot reload = 30s" is only half the story.** Model writes broadcast over Redis pub/sub and land fleet-wide in ~1–6s (`config_sync_pubsub.py`). Each of the N workers × 4 pods independently re-reads all 523 rows plus 5 config params on every message, once per ≥10s per worker. The 30s poll does the same full read per worker.
2. **The 30s reconcile also re-reads and re-parses config.yaml from disk**, twice: `_update_llm_router` → `get_config()` (`proxy_server.py:7456`) and `_delete_deployment` → `get_config()` (`:7311`). Editing the mounted ConfigMap therefore changes `model_list` deletion/keep decisions and DB-shadowing **without a restart**. But new YAML models are **not added** (only `load_config` adds YAML models), and YAML `router_settings` changes **are** re-applied through the SettingsStore (`_load_yaml_settings_stores` in `get_config`). So a half-applied config edit is possible on a running pod.
3. **YAML `model_list` removed in a hot-edited ConfigMap → those deployments are evicted at the next reconcile** (`proxy_server.py:7319-7361`; the keep-guard only applies when `model_list` is missing entirely).
4. **`async_pre_routing_hook` on callbacks is dead** (TL;DR 5), and **the chatgpt normalize deployment hook is dead** (TL;DR 6). Comments in our code claim otherwise.
5. **Failure logging is inline and deduped per logging object.** A slow `async_log_failure_event` delays retries. "Every retry" semantics in weighted_affinity / attempt_pusher may only see the first failure (not verified; see §4.6).
6. **One bad callback file = pod won't boot** (§1.5).
7. **`post_call_failure_hook` strips `litellm_logging_obj`** from `request_data` before calling hooks (`proxy/utils.py:3275`). Hooks needing call_type / model_call_details must read other fields.
8. **`str` returned from pre_call on `/v1/chat/completions` → HTTP 400**, not the canned 200, because `acompletion` is not in `["completion","text_completion"]` (`proxy/utils.py:1774-1782`).
9. **Leaf-class rule:** an `async_pre_call_hook` inherited from an intermediate class is ignored (`proxy/utils.py:2508`), unlike the streaming/moderation hooks, which walk the MRO.
10. **Per-key router_settings** replace global fallbacks for that request and skip the explicit "Model is blocked" 403 (`route_llm_request.py:516-544`).
11. `store_model_in_db` can be turned on from the DB `general_settings` row even when YAML says False (`proxy_server.py:10703-10719`).
12. `_PROXY_MaxBudgetLimiter` patch in `budget_notice` is a silent no-op on 1.105.1 (§6.3).
13. Suspected callback-string leak into `litellm.callbacks` on each reconcile, plus the sync executor being triggered (TL;DR 8). **Highest-value thing to verify on a pod.**

## Not verified (next checks)
- `GET /active/callbacks` on a prod pod: are `module.instance` strings present in `litellm.callbacks` / `success_callback` / `failure_callback`? (TL;DR 8)
- Is the config sync subscriber connected? (pod logs: "config sync subscriber redis error")
- YAML order of `litellm_settings.callbacks`: is `deepseek_id_prefix` before `deepseek_responses_adapt`?
- Does the Router reuse the logging object across retries/fallbacks? (decides the once-per-request vs per-attempt failure event)
- Are live-cb modules loaded via `spec_from_file_location` (next to config.yaml) or from `sys.path`? (double-module risk)
