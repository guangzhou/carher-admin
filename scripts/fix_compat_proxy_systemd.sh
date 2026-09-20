#!/usr/bin/env bash
# Move the DeepSeek compat_proxy (public :8000) from an ad-hoc SSH-session
# process onto its existing systemd unit, and defuse two stale units.
#
# Run ON the GPU host (h100 / 192.168.3.205) as cltx, with SUDOPW exported.
#
# WHY
# ---
# :8000 is the ONLY public ingress for deepseek-v4-flash, yet the proxy was
# running under /user.slice/.../session-NNNN.scope — a login session. When that
# session is reaped the public endpoint dies. It lands there because
# ops/daynight/lib.sh:ensure_proxy_upstream() kills any uvicorn on :8000 and
# respawns it as a plain background process.
#
# THREE UNITS EXIST AND ALL WERE WRONG OR OFF:
#   deepseek-v4-flash.service              enabled, inactive — the vLLM server.
#                                          daynight docs say NEVER start vLLM and
#                                          stop_deepseek() actively stops it, yet
#                                          it is enabled => a reboot starts vLLM
#                                          and it fights the Docker engine for GPUs.
#   ...-compat-proxy.service               enabled, inactive — upstream :8001
#                                          (dead port) + Requires= the vLLM unit.
#   ...-public-8000.service                DISABLED — base unit already has the
#                                          correct upstream :8767, but a drop-in
#                                          (upstream.conf) overrode it to :8766.
#
# So: fix the drop-in to :8767, add the proxy's runtime env, enable+start it,
# and disable the two hazards.
#
# ensure_proxy_upstream() will NOT fight this: it compares the live proxy's
# DEEPSEEK_COMPAT_UPSTREAM against $PROXY_UPSTREAM (= http://127.0.0.1:8767) and
# returns early on a match. That is exactly why the drop-in must set that env
# var explicitly rather than relying on the base unit default.
#
# Usage:
#   SUDOPW='...' ./fix_compat_proxy_systemd.sh apply
#   SUDOPW='...' ./fix_compat_proxy_systemd.sh status
#   SUDOPW='...' ./fix_compat_proxy_systemd.sh rollback

set -euo pipefail

UNIT=deepseek-v4-flash-public-8000.service
DROPIN=/etc/systemd/system/${UNIT}.d/upstream.conf
STALE_VLLM=deepseek-v4-flash.service
STALE_PROXY=deepseek-v4-flash-compat-proxy.service
FLASH_ROOT=/home/cltx/deepseek-v4-flash
HEALTH=http://127.0.0.1:8000/v1/models

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

# Run a command under sudo, feeding the password on stdin.
# ⚠ Because stdin carries the password, NEVER pipe or heredoc data into s():
#   s tee /etc/foo <<'EOF' ...   # WRONG — tee receives the PASSWORD, not the
#                                # heredoc, and writes the credential to the file.
# On 2026-08-07 exactly that wrote the sudo password into a 0644 root-owned
# drop-in. To write a root-owned file, stage it as the normal user first and
# then `s cp`, which does not read stdin. See write_root_file() below.
s() { printf '%s\n' "${SUDOPW:?SUDOPW not set}" | sudo -S -p '' "$@"; }

# Write stdin to a root-owned path safely (stage as $USER, then sudo cp).
write_root_file() {
  local dest="$1" tmp
  tmp="$(mktemp)"
  cat >"$tmp"
  s cp "$tmp" "$dest"
  s chown root:root "$dest"
  s chmod 0644 "$dest"
  shred -u "$tmp" 2>/dev/null || rm -f "$tmp"
}

wait_public() {
  for _ in $(seq 1 30); do
    curl -sf -m 3 "$HEALTH" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# Last-resort: bring the proxy back the old ad-hoc way so :8000 is never left dark.
manual_launch() {
  log "fallback: launching proxy ad-hoc (setsid, detached from this session)"
  cd "$FLASH_ROOT"
  DEEPSEEK_COMPAT_UPSTREAM=http://127.0.0.1:8767 \
  DEEPSEEK_MAX_INFLIGHT=16 \
  DEEPSEEK_ADMIT_WAIT_S=20 \
  setsid nohup ./venv/bin/python -m uvicorn scripts.compat_proxy:app \
    --host 0.0.0.0 --port 8000 --proxy-headers \
    >/tmp/compat_proxy_fallback.log 2>&1 &
  sleep 3
  wait_public && log "fallback proxy serving" || log "fallback ALSO failed — :8000 IS DOWN"
}

cmd_apply() {
  [[ -f "$DROPIN" ]] || die "drop-in not found: $DROPIN"
  s cp -p "$DROPIN" "${DROPIN}.bak-$(date +%Y%m%dT%H%M%S)"
  log "backed up drop-in"

  write_root_file "$DROPIN" <<'EOF'
# 2026-08-07: was DEEPSEEK_COMPAT_UPSTREAM=http://127.0.0.1:8766 (the retired
# Flash port). The live engine is the SGLang Docker container on :8767.
# This value must stay byte-identical to $PROXY_UPSTREAM in
# ops/daynight/lib.sh, otherwise ensure_proxy_upstream() kills this unit's
# process on every to_infer run and respawns it outside systemd.
[Service]
Environment=DEEPSEEK_COMPAT_UPSTREAM=http://127.0.0.1:8767
# Admission control. MAX_INFLIGHT stays 16 on purpose: prompts here are p50 ~46k
# tokens, so 16 concurrent already needs ~750k of the 1.63M-token KV pool.
# Raising it would thrash the pool. The fix for bursts is to WAIT for a slot
# instead of returning 429 instantly (was a hardcoded 0.01s).
Environment=DEEPSEEK_MAX_INFLIGHT=16
Environment=DEEPSEEK_ADMIT_WAIT_S=20
Environment=DEEPSEEK_MAX_INPUT_TOKENS=393216
Environment=DEEPSEEK_LONG_MAX_INPUT_TOKENS=393216
Environment=DEEPSEEK_UPSTREAM_TIMEOUT=900
Environment=DEEPSEEK_UPSTREAM_CONNECT_S=10
Restart=always
RestartSec=3
EOF
  log "wrote drop-in"

  s systemctl daemon-reload
  log "merged unit config:"
  systemctl cat "$UNIT" | grep -E 'ExecStart|Environment=DEEPSEEK|Restart' | sed 's/^/    /'

  # Guard: the drop-in must contain real systemd directives. A malformed drop-in
  # is silently ignored (the base unit's values win), which once masked a bad
  # write for half an hour. Assert the keys are actually there.
  grep -q '^Environment=DEEPSEEK_ADMIT_WAIT_S=' "$DROPIN" \
    || die "drop-in did not take: DEEPSEEK_ADMIT_WAIT_S missing from $DROPIN"
  grep -q '^Restart=always' "$DROPIN" \
    || die "drop-in did not take: Restart=always missing from $DROPIN"

  # Guard: the merged config must resolve to :8767, not :8766/:8001.
  local eff
  eff=$(systemctl show "$UNIT" -p Environment --value | tr ' ' '\n' \
        | sed -n 's/^DEEPSEEK_COMPAT_UPSTREAM=//p' | tail -1)
  [[ "$eff" == "http://127.0.0.1:8767" ]] \
    || die "effective upstream is [$eff], expected http://127.0.0.1:8767"
  log "guard OK: effective upstream=$eff"

  s systemctl enable "$UNIT"

  local old
  old="$(pgrep -f 'uvicorn scripts.compat_proxy:app' | head -1 || true)"
  if [[ -n "$old" ]]; then
    log "stopping ad-hoc proxy pid=$old (brief :8000 gap starts here)"
    kill "$old" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$old" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$old" 2>/dev/null || true
  fi
  s fuser -k 8000/tcp 2>/dev/null || true
  sleep 1

  log "starting $UNIT"
  s systemctl start "$UNIT" || { log "unit start failed"; manual_launch; return 1; }

  if ! wait_public; then
    log ":8000 not serving after unit start; unit status:"
    systemctl status "$UNIT" --no-pager -n 20 | sed 's/^/    /'
    s systemctl stop "$UNIT" || true
    manual_launch
    return 1
  fi
  log ":8000 serving under systemd"

  # Defuse boot hazards. A reboot is pending on this host, so an enabled vLLM
  # unit would come up and contend for the GPUs with the Docker engine.
  for u in "$STALE_VLLM" "$STALE_PROXY"; do
    if [[ "$(systemctl is-enabled "$u" 2>/dev/null)" == "enabled" ]]; then
      s systemctl disable "$u" && log "disabled stale unit $u"
    fi
  done

  log "verifying proxy cgroup is now systemd-owned:"
  local pid
  pid="$(pgrep -f 'uvicorn scripts.compat_proxy:app' | head -1)"
  tail -1 "/proc/$pid/cgroup" | sed 's/^/    /'
  # The env vars only reach the process if the drop-in parsed. Verify on the
  # LIVE process rather than trusting `systemctl show`.
  tr '\0' '\n' < "/proc/$pid/environ" | grep -E '^DEEPSEEK_' | sort | sed 's/^/    /'
  tr '\0' '\n' < "/proc/$pid/environ" | grep -q '^DEEPSEEK_ADMIT_WAIT_S=' \
    || die "proxy is running WITHOUT DEEPSEEK_ADMIT_WAIT_S — drop-in did not apply"
  [[ "$(systemctl show "$UNIT" -p Restart --value)" == "always" ]] \
    || die "effective Restart is not 'always'"
  log "apply OK"
}

cmd_rollback() {
  local baks
  baks=$(ls -1t "${DROPIN}.bak-"* 2>/dev/null | head -1 || true)
  [[ -n "$baks" ]] || die "no drop-in backup to restore"
  s cp -p "$baks" "$DROPIN"
  s systemctl daemon-reload
  s systemctl disable --now "$UNIT" || true
  log "restored $DROPIN from $baks and stopped $UNIT"
  manual_launch
}

cmd_status() {
  systemctl is-enabled "$UNIT" 2>&1 | sed 's/^/enabled: /'
  systemctl is-active "$UNIT" 2>&1 | sed 's/^/active: /'
  for u in "$STALE_VLLM" "$STALE_PROXY"; do
    printf '%s enabled=%s active=%s\n' "$u" \
      "$(systemctl is-enabled "$u" 2>&1)" "$(systemctl is-active "$u" 2>&1)"
  done
  local pid
  pid="$(pgrep -f 'uvicorn scripts.compat_proxy:app' | head -1 || true)"
  if [[ -n "$pid" ]]; then
    echo "proxy pid=$pid cgroup=$(tail -1 /proc/$pid/cgroup)"
    tr '\0' '\n' < "/proc/$pid/environ" | grep -E '^DEEPSEEK_' | sed 's/^/  /'
  else
    echo "NO proxy process on :8000"
  fi
  curl -sf -m 3 "$HEALTH" >/dev/null 2>&1 && echo "public :8000 OK" || echo "public :8000 DOWN"
}

case "${1:-}" in
  apply)    cmd_apply ;;
  rollback) cmd_rollback ;;
  status)   cmd_status ;;
  *) echo "usage: SUDOPW='...' $0 {apply|rollback|status}" >&2; exit 2 ;;
esac
