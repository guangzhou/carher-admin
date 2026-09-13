#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s OUTPUT.sql -- COMMAND [ARG ...]\n' "$0"
}

OUTPUT="${1:-}"
[[ -n "$OUTPUT" ]] || { usage >&2; exit 2; }
shift
[[ "${1:-}" == "--" ]] || { usage >&2; exit 2; }
shift
[[ $# -gt 0 ]] || { usage >&2; exit 2; }

umask 077
parent="$(dirname "$OUTPUT")"
[[ -d "$parent" && ! -L "$parent" ]] || {
  printf 'schema-snapshot: output parent must be a pre-created dedicated directory\n' >&2
  exit 1
}
python3 - "$parent" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
info = os.lstat(path)
if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
    raise SystemExit("schema-snapshot: output parent is not a real directory")
if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
    raise SystemExit("schema-snapshot: output parent must be owner-only mode 0700")
PY
tmp="$(mktemp "$parent/.schema-snapshot.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

"$@" \
  | sed -E '/^(--|$)/d; /^[\\](un)?restrict /d' \
  >"$tmp"
[[ -s "$tmp" ]] || { printf 'schema-snapshot: normalized dump is empty\n' >&2; exit 1; }
chmod 600 "$tmp"
[[ ! -e "$OUTPUT" && ! -L "$OUTPUT" ]] || { printf 'schema-snapshot: output already exists\n' >&2; exit 1; }
mv -f "$tmp" "$OUTPUT"
trap - EXIT

checksum() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

printf '{"tool":"schema-snapshot","status":"PASS","sha256":"%s"}\n' "$(checksum "$OUTPUT")"
