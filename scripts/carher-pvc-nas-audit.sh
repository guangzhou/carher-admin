#!/usr/bin/env bash
# Run on an ACK worker with /Data mounted. It only reports PVC usage.

set -euo pipefail

MAP_FILE=${1:?usage: carher-pvc-nas-audit.sh MAP_FILE OUTPUT_FILE}
OUTPUT_FILE=${2:?usage: carher-pvc-nas-audit.sh MAP_FILE OUTPUT_FILE}

: > "$OUTPUT_FILE"
while IFS=$'\t' read -r uid pv capacity; do
  base="/Data/$pv"
  [[ -d "$base" ]] || continue

  # Targeted directories avoid a full recursive NAS scan for every PVC.
  total=$(timeout 90 du -sk "$base" 2>/dev/null | awk '{print $1+0}' || true)
  tmp=$(timeout 30 du -sk "$base/workspace/tmp" 2>/dev/null | awk '{print $1+0}' || true)
  artifacts=$(timeout 30 du -sk "$base/workspace/artifacts" 2>/dev/null | awk '{print $1+0}' || true)
  exports=$(timeout 30 du -sk "$base/workspace/exports" 2>/dev/null | awk '{print $1+0}' || true)
  browser=$(timeout 30 du -sk "$base/browser" 2>/dev/null | awk '{print $1+0}' || true)
  memtmp=$(find "$base/memory" -maxdepth 1 -type f -name 'main.sqlite.tmp-*' -printf '%s\n' 2>/dev/null |
    awk '{sum += $1} END {printf "%.0f", sum / 1024}' || true)

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$uid" "$pv" "$capacity" "${total:-0}" "${tmp:-0}" \
    "${artifacts:-0}" "${exports:-0}" "${browser:-0}" "${memtmp:-0}" >> "$OUTPUT_FILE"
done < "$MAP_FILE"
