---
name: carher-pvc-workspace-cleanup
version: 1.1.0
description: "Use when Aliyun reports LowAvailableCapacity / PVC running out of space for carher-*-data, when Her PVC quota is over threshold, or when ACK node logs/backups need safe storage cleanup."
metadata:
  requires:
    bins: ["kubectl", "python3", "scripts/jms"]
---

# CarHer PVC Workspace Cleanup

> Resolve `carher-<uid>-data` space alerts without deleting durable memory, sessions, config, tokens, or original work directories.

## First Decision: Alert Suppression vs Real Storage Release

Do not treat moving files from a PVC to worker-local disk as "freeing storage" in the general sense. It only frees the PVC/CNFS subpath quota and consumes worker-local disk until the rollback copy is deleted.

Before cleaning, state which goal applies:

| Goal | Correct action | What to report |
|------|----------------|----------------|
| Stop a `carher-*-data` PVC quota alert quickly | Move safe `workspace/tmp`, `workspace/artifacts`, `workspace/exports`, and `memory/main.sqlite.tmp-*` to worker-local rollback; clear browser caches | "PVC quota reduced; rollback copy retained on worker; total bytes still exist until rollback is deleted" |
| Actually reduce total storage footprint | Delete safe cache/temp data directly, or delete worker-local rollback after the rollback window is no longer needed | "Storage was permanently released; rollback is no longer available for deleted paths" |
| Unsure what the user means by "optimize storage" | Ask or explicitly assume one of the above before moving large data | The assumption and its consequence |

If you moved data to `/root/carher-pvc-backups/...`, finish with a rollback lifecycle decision: keep temporarily, delete now to truly free space, or schedule/manual follow-up. Never end by implying worker-local rollback copies reduced total storage.

## Alert Threshold Rule

Aliyun CNFS/NAS `LowAvailableCapacity` alerts fire at `used percentage >= 85%`. For a 25Gi PVC, the target is **below 21.25Gi** as reported by CSI. `du` on the worker can be lower than CSI's next sample for several minutes.

After cleanup:
- Treat `du -sh /Data/<pv>` as the immediate source of truth for directory contents.
- Still wait at least one CSI sampling window and inspect `lastTimestamp`; the alert is only cleared when `LowAvailableCapacity` stops refreshing or the latest sampled percentage is below 85%.
- If the latest event refreshes at 85-90%, continue removing/moving enough safe or approved workspace data to give margin; do not stop at "almost below threshold".
- If `du` is low but events keep reporting old usage, check for `.nfs*` delayed-delete files and rollback directories under `/Data`.

## Safety Boundary

Safe to clean or move:
- `workspace/tmp`
- `workspace/artifacts`
- `workspace/exports`
- `browser/**/Cache`, `browser/**/Code Cache`, `browser/**/GPUCache`, `browser/**/Service Worker/CacheStorage`
- `memory/main.sqlite.tmp-*`

Do not clean without explicit user approval:
- `memory/main.sqlite`, `memory/main.sqlite-wal`, `memory/main.sqlite-shm`
- `sessions`, `agents`, `feishu-user-tokens`, `identity`, `devices`, `workflow`
- user-created workspace roots such as `workspace/baic*`, `workspace/BAIC-*`, media input files, or `source.mp4`

Workspace project directories such as `workspace/CHER-*`, `workspace/cher-*`, `workspace/FL3-*`, `workspace/jira_*`, `workspace/avm_*`, and similar task outputs are not cache. They may be the only copy of user work. For an urgent PVC alert, you may move old, bulky project directories to `/root/carher-pvc-backups/<cleanup-id>/workspace/` and recreate empty source directories to preserve path compatibility, but report clearly:
- this reduces PVC quota pressure only;
- rollback now consumes worker-local disk;
- deleting the rollback is required for permanent storage release.

For PVC quota alerts, move large safe content to a rollback directory outside the PVC. Do not leave backups under `/Data/_backups` as the final state: Aliyun CNFS quota accounting can continue charging moved files to the original PVC even after a same-NAS `mv`. Use `/Data/_backups` only as a short staging path if needed, then move the backup off NAS to a worker-local ext4 path such as `/root/carher-pvc-backups/<cleanup-id>/`.

For real storage release, delete safe cache/temp content instead of retaining a rollback copy, or delete the worker-local rollback after verification and explicit rollback expiry. The worker-local rollback is operational insurance, not cleanup.

## Setup

Open the Kubernetes API tunnel through JumpServer. Current asset names may differ from older examples; check with `scripts/jms list` if the asset is not found.

```bash
scripts/jms proxy k8s-work-226 16443 172.16.1.163 6443
```

Run `kubectl` locally against `~/.kube/config`. Run NAS scans on a worker that mounts the NAS root at `/Data`:

```bash
scripts/jms ssh k8s-work-226 'ls /Data | head'
```

## Single PVC Workflow

1. Capture PVC and PV:

```bash
HER_UID=187
kubectl --kubeconfig ~/.kube/config -n carher get pvc carher-$HER_UID-data -o wide
kubectl --kubeconfig ~/.kube/config -n carher get pods -l app=carher-user,user-id=$HER_UID -o wide
kubectl --kubeconfig ~/.kube/config get pv "$(kubectl --kubeconfig ~/.kube/config -n carher get pvc carher-$HER_UID-data -o jsonpath='{.spec.volumeName}')" -o yaml
```

Capture the PV `spec.csi.volumeAttributes.path`, usually `/nas-<uuid>`.

2. Scan the NAS directory:

```bash
PV=nas-bdd55e09-f2ea-4274-b5fb-7fef66e34209
scripts/jms ssh k8s-work-226 'bash -s' <<EOF
BASE=/Data/$PV
du -xh -d1 "\$BASE" 2>/dev/null | sort -h | tail -40
du -xh -d1 "\$BASE/workspace" 2>/dev/null | sort -h | tail -80
find "\$BASE/memory" -maxdepth 1 -type f -name "main.sqlite.tmp-*" -printf "%s %p\n" 2>/dev/null | sort -n
find "\$BASE/browser" -maxdepth 5 -type d \( -name Cache -o -name "Code Cache" -o -name GPUCache -o -name CacheStorage \) -print0 2>/dev/null | xargs -0 -r du -sh 2>/dev/null | sort -h
EOF
```

3. Move safe content to a rollback directory:

```bash
PV=nas-bdd55e09-f2ea-4274-b5fb-7fef66e34209
HER_UID=187
scripts/jms ssh k8s-work-226 'bash -s' <<EOF
set -euo pipefail
BASE=/Data/$PV
TS=\$(date +%Y%m%d-%H%M%S)
BACKUP=/root/carher-pvc-backups/carher-$HER_UID-pvc-cleanup-\$TS
mkdir -p "\$BACKUP"

move_dir() {
  src="\$1"
  rel="\${src#\$BASE/}"
  if [ -d "\$src" ]; then
    mkdir -p "\$BACKUP/\$(dirname "\$rel")"
    mv "\$src" "\$BACKUP/\$rel"
    mkdir -p "\$src"
  fi
}

move_dir "\$BASE/workspace/tmp"
move_dir "\$BASE/workspace/artifacts"
move_dir "\$BASE/workspace/exports"

mkdir -p "\$BACKUP/memory"
find "\$BASE/memory" -maxdepth 1 -type f -name "main.sqlite.tmp-*" -print0 2>/dev/null |
  while IFS= read -r -d "" f; do mv "\$f" "\$BACKUP/memory/"; done

for d in \
  "\$BASE/browser/openclaw/user-data/Default/Cache" \
  "\$BASE/browser/openclaw/user-data/Default/Code Cache" \
  "\$BASE/browser/openclaw/user-data/Default/GPUCache" \
  "\$BASE/browser/openclaw/user-data/Default/Service Worker/CacheStorage"; do
  [ -d "\$d" ] && find "\$d" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
done

sync
du -sh "\$BASE" "\$BACKUP" 2>/dev/null || true
echo "ROLLBACK_RETAINED_ON_WORKER=\$BACKUP"
echo "This reduced PVC quota usage only; delete the rollback after approval if the goal is real storage release."
EOF
```

4. Verify:

```bash
kubectl --kubeconfig ~/.kube/config -n carher get pod -l app=carher-user,user-id=$HER_UID -o wide
kubectl --kubeconfig ~/.kube/config -n carher describe pvc carher-$HER_UID-data | sed -n '1,130p'
```

CSI `LowAvailableCapacity` events can keep showing the old usage string for several minutes. Treat `du -sh /Data/<pv>` as the immediate source of truth; verify that the event age stops refreshing. If an event keeps refreshing while `du` is low, check that backups were not left anywhere under `/Data`; move any `/Data/_backups/<cleanup-id>` directory to worker-local disk and wait another CSI sampling interval.

If the user asked to reduce storage footprint, verify and then delete the worker-local rollback:

```bash
scripts/jms ssh k8s-work-226 'du -sh /root/carher-pvc-backups/<cleanup-id> && rm -rf /root/carher-pvc-backups/<cleanup-id> && df -h /root'
```

Do this only after explicitly stating that rollback for those moved safe paths will no longer be available.

## Cluster Scan

Generate a PV to PVC mapping and scan only likely cleanup targets. Prefer this over recursive `du` across every file:

```bash
kubectl --kubeconfig ~/.kube/config get pv -o json > /tmp/pv.json
python3 - <<'PY' >/tmp/carher-pv-map.tsv
import json
d=json.load(open('/tmp/pv.json'))
for pv in d['items']:
    ref=pv.get('spec',{}).get('claimRef',{})
    ns=ref.get('namespace')
    pvc=ref.get('name','')
    if ns == 'carher' and pvc.startswith('carher-') and pvc.endswith('-data'):
        attrs=pv.get('spec',{}).get('csi',{}).get('volumeAttributes',{})
        path=attrs.get('path','')
        print(pvc[:-5], pv['metadata']['name'], path)
PY
```

Upload `/tmp/carher-pv-map.tsv` to a worker and scan these paths:

```bash
scripts/jms scp /tmp/carher-pv-map.tsv k8s-work-226:/tmp/carher-pv-map.tsv
scripts/jms ssh k8s-work-226 'bash -s' <<'EOF'
while read -r her pv path; do
  base=/Data/$pv
  [ -d "$base" ] || continue
  total=$(du -s "$base" 2>/dev/null | awk '{print $1}')
  tmp=$(du -s "$base/workspace/tmp" 2>/dev/null | awk '{print $1+0}')
  artifacts=$(du -s "$base/workspace/artifacts" 2>/dev/null | awk '{print $1+0}')
  exports=$(du -s "$base/workspace/exports" 2>/dev/null | awk '{print $1+0}')
  browser=$(du -s "$base/browser" 2>/dev/null | awk '{print $1+0}')
  memtmp=$(find "$base/memory" -maxdepth 1 -type f -name 'main.sqlite.tmp-*' -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{printf "%.0f", s/1024}')
  printf "%s\t%s\t%d\t%d\t%d\t%d\t%d\t%d\n" "$her" "$pv" "${total:-0}" "${tmp:-0}" "${artifacts:-0}" "${exports:-0}" "${browser:-0}" "${memtmp:-0}"
done < /tmp/carher-pv-map.tsv | sort -k3 -nr
EOF
```

Clean only PVCs above the alert threshold or with several GiB of safe cleanup. If retaining rollback, keep one backup directory per run on worker-local disk and report its size as storage still consumed:

```bash
RUN=carher-pvc-batch-cleanup-$(date +%Y%m%d-%H%M%S)
BACKUP=/root/carher-pvc-backups/$RUN
```

After batch cleanup, always run a post-scan and a rollback accounting summary:

```bash
scripts/jms ssh k8s-work-226 'bash -s' <<'EOF'
du -sh /root/carher-pvc-backups/* 2>/dev/null | sort -h | tail -20
du -sh /Data/_backups 2>/dev/null || true
df -h /root /Data
EOF
```

If the request is "free storage" rather than "stop PVC alerts", delete the new rollback directory after verification or ask for confirmation before claiming the work is done.

## Old NAS Backup Cleanup

Old rollback directories under `/Data/_backups` can keep charging quota or waste NAS space. Before deleting, sample contents and confirm they are only orphan temp files:

```bash
scripts/jms ssh k8s-work-226 'bash -s' <<'EOF'
du -sh /Data/_backups/* 2>/dev/null | sort -h
for d in /Data/_backups/orphan-tmp-cleanup*; do
  [ -d "$d" ] || continue
  echo "DIR $d"
  find "$d" -maxdepth 3 -type f | sed -n "1,20p"
done
EOF
```

If the sample only contains `memory/main.sqlite.tmp-*` rollback files from old runs, and no current rollback is needed, remove those old backup directories:

```bash
scripts/jms ssh k8s-work-226 'bash -s' <<'EOF'
set -euo pipefail
before=$(du -sk /Data/_backups 2>/dev/null | awk '{print $1+0}')
find /Data/_backups -maxdepth 1 -mindepth 1 -type d -name 'orphan-tmp-cleanup*' -print -exec rm -rf {} +
sync || true
after=$(du -sk /Data/_backups 2>/dev/null | awk '{print $1+0}')
echo "freed_kb=$((before-after))"
du -sh /Data/_backups 2>/dev/null || true
EOF
```

Do not delete today's `/root/carher-pvc-backups/<date>/...` rollback directories unless rollback is explicitly no longer needed.

## ACK Node Log Cleanup

Use existing `loongcollector` Pods for read-only discovery because they mount host root at `/logtail_host`:

```bash
kubectl --kubeconfig ~/.kube/config -n kube-system get pods -l k8s-app=loongcollector-ds -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.nodeName}{"\n"}{end}' |
while IFS="$(printf '\t')" read -r pod node; do
  echo "NODE $node POD $pod"
  kubectl --kubeconfig ~/.kube/config -n kube-system exec "$pod" -c loongcollector -- \
    sh -c 'find /logtail_host/var/log -xdev -type f -size +100M -exec ls -lh {} \; 2>/dev/null | sort -k5 -hr | head -30'
done
```

Clean via one-shot Pods in namespace `carher` using an existing ACR VPC image with `imagePullSecrets` (`acr-secret`, `acr-vpc-secret`) and only a `/var/log` hostPath mount. Safe cleanup targets:
- truncate top-level `/var/log/syslog`, `/var/log/messages`, and `/var/log/*.log` only when a file is larger than 100MiB
- delete archived systemd journal files matching `/var/log/journal/**/system@*.journal`
- delete rotated Pod logs matching `/var/log/pods/**/*.log.*` or `*.gz`

Do not delete the active `system.journal` file or active Pod `0.log` files. After cleanup, verify:

```bash
kubectl --kubeconfig ~/.kube/config -n kube-system get pods -l k8s-app=loongcollector-ds -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.nodeName}{"\n"}{end}' |
while IFS="$(printf '\t')" read -r pod node; do
  count=$(kubectl --kubeconfig ~/.kube/config -n kube-system exec "$pod" -c loongcollector -- \
    sh -c 'find /logtail_host/var/log -xdev -type f -size +100M 2>/dev/null | wc -l' || echo err)
  printf '%s\tlarge_log_files_gt_100M=%s\n' "$node" "$count"
done | sort
```

## Rollback

Move the backed-up directory contents back to the original PV path:

```bash
PV=nas-bdd55e09-f2ea-4274-b5fb-7fef66e34209
BACKUP=/root/carher-pvc-backups/carher-187-pvc-cleanup-20260611-011247
scripts/jms ssh k8s-work-226 'bash -s' <<EOF
set -euo pipefail
BASE=/Data/$PV
cd "$BACKUP"
find . -mindepth 1 -maxdepth 3 -type d -print | while read -r d; do mkdir -p "$BASE/$d"; done
find . -type f -print0 | while IFS= read -r -d "" f; do
  dest="$BASE/${f#./}"
  mkdir -p "$(dirname "$dest")"
  mv "$f" "$dest"
done
EOF
```

## Edge Cases

- **PVC event still shows 98% after cleanup**: CSI events retain the last message and may refresh slowly. Compare `du -s /Data/<pv>` with the PVC capacity threshold and watch whether event age stops updating.
- **PVC event still refreshes after same-NAS backup**: moved files under `/Data/_backups` can still count against the original PVC quota. Move backups off NAS to worker-local ext4 (`/root/carher-pvc-backups/...`) or delete them if no rollback is needed.
- **User asks to "optimize storage" or "free space"**: do not stop after moving files to `/root/carher-pvc-backups`. That only shifts bytes from PVC/NAS quota to worker-local disk. Either delete safe data directly, or delete the worker rollback after verification and clearly state rollback is gone.
- **Worker-local disk grows after cleanup**: this is expected if rollback is retained. Report `/root/carher-pvc-backups` size and ask/delete according to the goal; do not describe it as storage released.
- **`du` is slow**: scan targeted directories first (`workspace/tmp`, `workspace/artifacts`, `workspace/exports`, `browser`, `memory/main.sqlite.tmp-*`) instead of walking the whole PVC.
- **Need more space after safe cleanup**: do not delete source media or `workspace/baic*` directories automatically. Report the largest remaining directories and ask before removing original work material.
