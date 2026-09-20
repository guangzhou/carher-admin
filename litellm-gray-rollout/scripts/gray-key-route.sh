#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

ACTION="${1:-}"
shift || true

case "$ACTION" in
  list)
    load_state
    for name in protected-prod force-prod force-gray; do
      printf '%s:\n' "$name"
      map_keys "$(active_dir)/$name.map" | while IFS= read -r key; do
        [[ -n "$key" ]] && printf '  sid=%s\n' "$(sid_for_key "$key")"
      done
    done
    ;;
  verify)
    load_state
    require_empty=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --require-empty) require_empty="${2:?missing list name}"; shift 2 ;;
        *) die "unknown verify argument: $1" ;;
      esac
    done
    if [[ -n "$require_empty" ]]; then
      case "$require_empty" in protected-prod|force-prod|force-gray) ;; *) die "unknown list: $require_empty" ;; esac
      [[ -z "$(map_keys "$(active_dir)/$require_empty.map")" ]] || die "$require_empty is not empty"
    fi
    printf 'verify=ok\n'
    ;;
  force-gray|force-prod|protect-prod|remove)
    lock_acquire
    load_state
    [[ "$STATE_PHASE" == "normal_gray" && "$STATE_FROZEN" == "0" ]] || die "key routing is frozen in phase=$STATE_PHASE"
    # Which direction does this action move a key? Everything that can put a key
    # ON the new build is an escalation and is gated like a ramp step; everything
    # that moves a key OFF it is de-escalation and must never be gated, because
    # that is the incident path and a blocked incident path is worse than the hole
    # it would close.
    #
    # `force-gray` routes to the gray pool INDEPENDENTLY of split (see
    # route_model.py evaluate(): force_gray is matched before bucket_for()), so a
    # named-key pilot at split=0 is already real users on the new build. Until
    # 2026-09-21 this script had neither a workload binding nor a gate, while
    # require_workload_binding only guarded split > 0 -- so ring ④, which carried
    # the FIRST production traffic and dwelled 84.8h last run, was the one ring
    # where nothing checked which build was running.
    #
    # `remove` de-escalates out of force-gray, but it also drops protected-prod,
    # and with a live split a formerly protected key then falls to bucket_for()
    # and can land on gray. That is an escalation in everything but name, so it
    # binds whenever split > 0 -- and stays free at split=0, where every bucket
    # is prod and the action cannot expose anyone.
    case "$ACTION" in
      force-gray)
        require_workload_binding "routing a key to the gray build"
        require_gate_evidence key_pilot_entry
        ;;
      remove)
        [[ "$STATE_SPLIT" == "0" ]] || require_workload_binding "removing a key override while split=$STATE_SPLIT is live"
        ;;
    esac
    IFS= read -r -s key || true
    printf '\n' >&2
    valid_key "$key" || die "invalid key format"
    sid="$(sid_for_key "$key")"
    current="$(active_dir)"
    already=0
    case "$ACTION" in
      force-gray) map_has "$current/force-gray.map" "$key" && already=1 ;;
      force-prod) map_has "$current/force-prod.map" "$key" && already=1 ;;
      protect-prod) map_has "$current/protected-prod.map" "$key" && already=1 ;;
      remove)
        if ! map_has "$current/protected-prod.map" "$key" && ! map_has "$current/force-prod.map" "$key" && ! map_has "$current/force-gray.map" "$key"; then
          already=1
        fi
        ;;
    esac
    if [[ "$already" == "1" ]]; then
      printf 'action=%s sid=%s unchanged\n' "$ACTION" "$sid"
      exit 0
    fi
    stage_from_active
    for map in protected-prod.map force-prod.map force-gray.map; do
      remove_key_from_map "$STAGE_DIR/$map" "$key"
    done
    remove_key_from_map "$STAGE_DIR/key-sid.map" "$key"
    target=""
    case "$ACTION" in
      force-gray) target=force-gray.map ;;
      force-prod) target=force-prod.map ;;
      protect-prod) target=protected-prod.map ;;
    esac
    if [[ -n "$target" ]]; then
      write_key_map "$STAGE_DIR/$target" "$key" "$STAGE_DIR/key-sid.map"
    fi
    render_fragments "$STAGE_DIR" "$STATE_MODE" "$STATE_SPLIT" "$STATE_BRIDGE"
    write_state "$STAGE_DIR" "$STATE_RUN_ID" "$(basename "$STAGE_DIR")" "$STATE_PHASE" "$STATE_MODE" "$STATE_SPLIT" "$STATE_BRIDGE" "$STATE_FROZEN"
    commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"
    printf '%s operator=%s sid=%s action=%s\n' "$(date -u +%FT%TZ)" "${SUDO_USER:-${USER:-unknown}}" "$sid" "$ACTION" >>"$GRAY_ROOT/audit.log"
    chmod 600 "$GRAY_ROOT/audit.log"
    printf 'action=%s sid=%s\n' "$ACTION" "$sid"
    ;;
  -h|--help|"")
    printf 'Usage: %s force-gray|force-prod|protect-prod|remove|list|verify\n' "$0"
    [[ -n "$ACTION" ]] || exit 2
    ;;
  *) die "unknown action: $ACTION" ;;
esac
