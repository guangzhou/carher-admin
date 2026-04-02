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
