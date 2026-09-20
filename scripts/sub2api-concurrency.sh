#!/usr/bin/env bash
# sub2api concurrency gates: read them, change them, and judge whether the
# change actually took.  Run on 198.
#
# WHY THIS EXISTS (2026-09-09).  sub2api has TWO independent concurrency gates
# and the frontend puts them on two different pages:
#
#   user gate     users.concurrency         "this sub2api user may hold N slots
#                                            in total, across every platform"
#   account gate  accounts.concurrency      "this one upstream account may hold
#                                            N slots"
#
# All LiteLLM traffic to sub2api (grok + kimi + antigravity) arrives as ONE
# sub2api user (id=1, api key `grok-litellm` id=5).  So the user gate is the
# one that saturates first, and raising the per-account number -- the obvious
# thing to do when the symptom says "grok is out of capacity" -- changes
# nothing.  On 2026-09-09 the account gate went 10 -> 199 on four grok accounts
# and the failure rate did not move (620 slot timeouts in the 4 minutes after).
# The user gate was still 5.  Moving it 5 -> 500 stopped the failures in 29s.
#
# The upstream error text is `Concurrency limit exceeded for user, please retry
# later` (format string lives in the sub2api binary), which LiteLLM surfaces as
# an APIConnectionError -- NOT a 429.  So no deployment cooldown, no backoff:
# retries hammer the same full queue and the caller gets a 500.
#
# Usage (on 198):
#   bash sub2api-concurrency.sh show
#   bash sub2api-concurrency.sh set-user    <user_id>    <n>
#   bash sub2api-concurrency.sh set-account <account_id> <n>
#   bash sub2api-concurrency.sh judge [minutes] [litellm_model_group]
#
# `set-*` writes through the admin API (so sub2api's own cache is invalidated),
# then asserts the value from postgres -- the API cannot testify about itself.
# Effect is NOT instant: the gate is cached for ~30s.  Judge after that, and
# judge on real traffic, not on a probe.
set -euo pipefail

DEV_NS=litellm-dev
PROD_NS=litellm-product
LITELLM_DB_POD=litellm-db-0
S2A_BASE=${S2A:-http://127.0.0.1:31880}
HELPER_SRC=${HELPER_SRC:-$(dirname "$0")/grok-onboard/sub2api_admin.py}

K=(sudo kubectl)

die() { echo "FATAL: $*" >&2; exit 1; }

# Pod names here rotate (the sub2api deploy restarts often, litellm-product
# gets rolled).  Never hardcode them.
running_pod() {  # running_pod <ns> <label-app>
    "${K[@]}" get pods -n "$1" -l "app=$2" \
        --field-selector=status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

S2A_POD=$(running_pod "$DEV_NS" sub2api) || true
PG_POD=$(running_pod "$DEV_NS" sub2api-postgres) || true
[ -n "${PG_POD:-}" ] || die "no Running sub2api-postgres pod in $DEV_NS"

s2a_psql() { "${K[@]}" -n "$DEV_NS" exec -i "$PG_POD" -- psql -U sub2api "$@"; }
llm_psql() { "${K[@]}" -n "$PROD_NS" exec -i "$LITELLM_DB_POD" -- psql -U litellm -d litellm "$@"; }

# --- admin API -------------------------------------------------------------
# The password is a real secret.  It lands in a mode-600 file under a
# per-invocation name (a fixed /tmp path gets you another session's stale
# file), and is shredded on every exit path.
PW_FILE=""; HELPER=""
cleanup() {
    [ -n "$PW_FILE" ] && { shred -u "$PW_FILE" 2>/dev/null || rm -f "$PW_FILE"; }
    [ -n "$HELPER" ] && rm -f "$HELPER"
    return 0
}
trap cleanup EXIT

api_init() {
    [ -f "$HELPER_SRC" ] || die "helper not found: $HELPER_SRC (scp scripts/grok-onboard/sub2api_admin.py alongside this script)"
    local sfx="$$-$(date +%s)"
    PW_FILE="/tmp/.s2a-conc-$sfx.pw"
    HELPER="/tmp/.s2a-conc-$sfx.py"
    cp "$HELPER_SRC" "$HELPER"
    "${K[@]}" -n "$DEV_NS" get secret sub2api-secrets \
        -o jsonpath='{.data.ADMIN_PASSWORD}' | base64 -d > "$PW_FILE"
    chmod 600 "$PW_FILE"
    [ -s "$PW_FILE" ] || die "empty ADMIN_PASSWORD from secret sub2api-secrets"
}

api() {  # api <METHOD> <path> [json-body]
    S2A="$S2A_BASE" S2A_PW_FILE="$PW_FILE" python3 "$HELPER" "$@"
}

int_or_die() {
    case "$1" in ''|*[!0-9]*) die "not a non-negative integer: $1" ;; esac
}

# --- show ------------------------------------------------------------------
cmd_show() {
    echo "== user gate (this is the one that saturates: all LiteLLM traffic is one user)"
    s2a_psql -c "select id, email, concurrency, rpm_limit, status,
                        round(balance,2) as balance
                   from users order by id"
    echo "== account gate (per upstream account; only bites after the user gate is wide)"
    s2a_psql -c "select id, name, platform, concurrency, priority, status, schedulable
                   from accounts where status='active' and schedulable order by platform, id"
    echo "== headroom"
    s2a_psql -Atc "select 'user id=1 gate: ' || (select concurrency from users where id=1)
                     || '   schedulable grok account slots: '
                     || coalesce((select sum(concurrency) from accounts
                                  where platform='grok' and status='active' and schedulable),0)"
    echo "(if the account sum is BELOW the user gate, the account gate is the real ceiling)"
}

# --- set-user --------------------------------------------------------------
cmd_set_user() {
    local uid=$1 n=$2; int_or_die "$uid"; int_or_die "$n"
    api_init
    local before
    before=$(s2a_psql -Atc "select concurrency from users where id=$uid")
    [ -n "$before" ] || die "no such user: id=$uid"
    echo "user $uid concurrency: $before -> $n   (rollback value: $before)"

    api PUT "/api/v1/admin/users/$uid" "{\"concurrency\":$n}" | head -1

    # PUT /api/v1/admin/users/{id} has a history of returning 200 while
    # silently dropping fields (balance does exactly that).  Only postgres
    # counts.  Also re-read the whole row: a partial PUT must not have blanked
    # anything else.
    local after
    after=$(s2a_psql -Atc "select concurrency from users where id=$uid")
    [ "$after" = "$n" ] || die "DB readback says concurrency=$after, wanted $n -- write did NOT land"
    echo "DB readback OK: concurrency=$after"
    s2a_psql -x -c "select id,email,concurrency,rpm_limit,status,round(balance,2) balance,
                           balance_notify_enabled, restrict_public_groups
                      from users where id=$uid"
    echo
    echo "gate is cached ~30s.  Now: bash $0 judge 5"
}

# --- set-account -----------------------------------------------------------
cmd_set_account() {
    local aid=$1 n=$2; int_or_die "$aid"; int_or_die "$n"
    api_init
    local before
    before=$(s2a_psql -Atc "select concurrency from accounts where id=$aid")
    [ -n "$before" ] || die "no such account: id=$aid"
    local sub_before
    sub_before=$(s2a_psql -Atc "select coalesce(credentials->>'sub','') from accounts where id=$aid")
    echo "account $aid concurrency: $before -> $n   (rollback value: $before)"

    api PUT "/api/v1/admin/accounts/$aid" "{\"concurrency\":$n}" | head -1

    local after sub_after
    after=$(s2a_psql -Atc "select concurrency from accounts where id=$aid")
    sub_after=$(s2a_psql -Atc "select coalesce(credentials->>'sub','') from accounts where id=$aid")
    [ "$after" = "$n" ] || die "DB readback says concurrency=$after, wanted $n -- write did NOT land"
    # A partial PUT on an account is the dangerous one: blanking `credentials`
    # would take the upstream token with it and kill the leg for real.
    [ "$sub_after" = "$sub_before" ] \
        || die "credentials.sub changed ($sub_before -> $sub_after) -- the PUT ate the token, restore it NOW"
    echo "DB readback OK: concurrency=$after, credentials.sub unchanged"
}

# --- judge -----------------------------------------------------------------
cmd_judge() {
    local mins=${1:-5} mg=${2:-sa-grok-4.6}
    [ -n "${S2A_POD:-}" ] || die "no Running sub2api pod in $DEV_NS"

    echo "== sub2api slot timeouts, last ${mins}m (pod $S2A_POD)"
    local log
    log=$("${K[@]}" -n "$DEV_NS" logs "$S2A_POD" --since="${mins}m" 2>/dev/null || true)
    # Under load this container writes enough that kubelet rotation keeps only
    # a few MINUTES of log.  Print the oldest line's timestamp: "0 failures"
    # over a window the log does not actually cover is a false green.
    echo "-- oldest line available: $(printf '%s\n' "$log" | head -1 | cut -c1-29)"
    echo "-- events (bucketed by name; user_* vs account_* is the whole diagnosis):"
    printf '%s\n' "$log" | grep -o '[a-z_]*slot_acquire_failed' | sort | uniq -c || echo "   (none)"
    echo "-- last slot timeout:"
    printf '%s\n' "$log" | grep 'slot_acquire_failed' | tail -1 | cut -c1-29 || true
    echo "-- models affected:"
    printf '%s\n' "$log" | grep 'slot_acquire_failed' \
        | grep -o '"model": "[^"]*"' | sort | uniq -c || true

    echo
    echo "== LiteLLM real traffic for model_group=$mg, last ${mins}m"
    echo "   (this is the judge that matters: a probe of your own proves nothing"
    echo "    about a gate that only bites under concurrent real load)"
    llm_psql -c "select date_trunc('minute',\"startTime\") m, count(*) n,
                        sum((status='failure')::int) fail,
                        count(distinct api_key) keys
                   from \"LiteLLM_SpendLogs\"
                  where model_group = '$mg'
                    and \"startTime\" > now() - interval '$mins minutes'
                  group by 1 order by 1"
}

case "${1:-}" in
    show)        cmd_show ;;
    set-user)    shift; [ $# -eq 2 ] || die "usage: $0 set-user <user_id> <n>";    cmd_set_user "$@" ;;
    set-account) shift; [ $# -eq 2 ] || die "usage: $0 set-account <account_id> <n>"; cmd_set_account "$@" ;;
    judge)       shift; cmd_judge "$@" ;;
    *) sed -n '2,30p' "$0"; exit 2 ;;
esac
