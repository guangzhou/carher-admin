# Pitfalls & Gotchas

Critical issues discovered during the hot grayscale implementation. Each caused
silent failures or degraded zero-downtime guarantees.

## 1. SubPath Bind Mount vs Atomic Rename

**Problem:** The `config-reloader` sidecar initially used atomic writes
(`write to tmp → fs.renameSync`). This creates a new inode, but the main
container mounts `openclaw.json` via a SubPath bind mount which pins to the
original inode. Result: main container never sees the updated config.

**Fix:** Use `fs.writeFileSync(DST, ...)` directly — writes to the same inode,
which the SubPath bind mount follows immediately.

**Rule:** Never use atomic rename when the consumer reads via SubPath mount.

## 2. Pod Map Must Store Multiple Pods per UID

**Problem:** During rolling updates, two pods (old and new) share the same
`user-id` label. If the health checker's pod map stores only one pod per UID
(`map[int]*Pod`), the new pod's ReadinessGate never gets set, causing the
rolling update to stall indefinitely.

**Fix:** Use `map[int][]*Pod` and iterate over all pods per UID. Set
ReadinessGate on every Running pod independently.

## 3. ReadinessGate Fallback: TCP Ready ≠ WS Connected

**Problem:** When `/healthz` is unavailable, falling back to `container.Ready`
(TCP port open) doesn't guarantee the Feishu WebSocket is connected. WS
typically takes 5-15s after container start. Setting ReadinessGate too early
lets K8s terminate the old pod before the new pod's WS is actually connected.

**Fix:** Fallback requires both `container.Ready == true` AND container uptime
≥ 15 seconds before marking the gate as True.

## 4. RBAC Must Include Deployments and Services

**Problem:** The operator uses `Owns(&appsv1.Deployment{})` and performs CRUD
on Deployments and Services, but the ClusterRole was missing these API groups.
The operator would fail at startup with 403 errors.

**Fix:** Add explicit rules for `apiGroups: [apps]` (deployments) and
`apiGroups: [""]` (services) with full CRUD verbs in `k8s/operator-rbac.yaml`.

## 5. Unpause: Deployment Stays at 0 Replicas

**Problem:** When unpausing an instance, if no spec changes are present, neither
`needRollout` nor `hotReload` is triggered. The Deployment stays at 0 replicas.

**Fix:** Add an explicit `else if deploy.Replicas == 0` branch in `Reconcile()`
to call `scaleDeployment(ctx, uid, 1)`. Also sync `Replicas` in the Deployment
update path.

## 6. Unconditional Status Update → Infinite Reconcile Loop

**Problem:** Calling `r.Status().Update(ctx, &her)` unconditionally at the end
of `Reconcile()` bumps the resource version even when nothing changed, triggering
a new reconcile event in an infinite loop.

**Fix:** Track `statusChanged := needRollout || prevHash != configHash` and only
call `Status().Update()` when true.

## 7. ConfigMap Without OwnerReference → Resource Leak

**Problem:** Per-user ConfigMaps (`carher-{uid}-user-config`) created by the
operator had no OwnerReference. Deleting a HerInstance CRD left orphaned
ConfigMaps.

**Fix:** Set `OwnerReferences` on ConfigMap creation pointing to the HerInstance
CRD, and ensure it's copied during updates. K8s garbage collection handles
cleanup automatically.

## 8. Changing Defaults Only Affects New Instances

**Problem:** Updating default values in CRD schema, backend models, or frontend
forms only applies to **newly created** instances. Existing instances retain
their original values.

**Fix:** For existing instances, explicitly patch via `kubectl patch` or the
admin API batch endpoint. This triggers the operator's hot-reload path.

## 9. openclaw.json Config Schema Validation — Unknown Keys Crash the Process

**Problem:** Added `agents.providers.wangsu` (baseURL + apiKey) to the generated
`openclaw.json`. The CarHer process validates its config on startup and rejects
unrecognized keys. Result: `Config invalid — agents: Unrecognized key: "providers"`.
All 20 wangsu instances entered CrashLoopBackOff simultaneously.

**Impact:** 20 instances down for ~40 minutes. Hot-reload delivered the bad
config instantly to all running pods — no rollout, no canary, no chance to catch
it before full blast.

**Fix:** Removed `agents.providers` from config_gen. The wangsu provider config
already exists in the shared `carher-config.json` (mounted as base-config
ConfigMap) under `models.providers.wangsu`. Per-user `openclaw.json` inherits it
via `$include`.

**Rules:**
- **Never add unknown keys** to `openclaw.json`. Always check a running
  instance's actual config first: `kubectl exec <pod> -c carher -- cat
  /data/.openclaw/openclaw.json`
- **Provider definitions** (baseURL, apiKey, model list) belong in
  `carher-config.json` (`models.providers.*`), not in per-user config.
- **Per-user config** (`openclaw.json`) only sets: `agents.defaults.model`,
  `agents.defaults.models` (aliases), `channels.feishu`, `commands`,
  `plugins.entries.realtime`.
- **Hot-reload is a double-edged sword** — it delivers config changes instantly
  without pod restart, but a bad config will crash all affected instances
  simultaneously with no rollback window. Test config changes on a single
  instance first.

## 10. Frontend–Backend Model Map Desync

**Problem:** Frontend `PROVIDER_MODELS` offered `gemini` for all providers
(openrouter, wangsu), but the backend `MODEL_MAP` (Python) and `modelMap` (Go)
for the openrouter provider had no `gemini` entry. The fallback
`mm.get(model_short, model_short)` returned the raw string `"gemini"` as the
primary model — not the full identifier `"openrouter/google/gemini-3.1-pro-preview"`.

**Impact:** Any instance with `provider=openrouter, model=gemini` would get
`primary: "gemini"` in its config, which may not resolve to a valid model.

**Fix:** Added `"gemini": "openrouter/google/gemini-3.1-pro-preview"` to both
Python `MODEL_MAP` and Go `modelMap`.

**Rule:** When adding a new model option to the frontend `PROVIDER_MODELS`,
always add the corresponding mapping to **all three places**:
1. `frontend/src/components/InstanceDetail.jsx` — `PROVIDER_MODELS`
2. `backend/config_gen.py` — `MODEL_MAP` / `MODEL_MAP_*`
3. `operator-go/internal/controller/config_gen.go` — `modelMap` / `modelMap*`

And check that the Pydantic model description in `backend/models.py` includes
the new model name.
