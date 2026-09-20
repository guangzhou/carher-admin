#!/usr/bin/env bash
# Verify a LiteLLM key-allowlist rollout, from the DB instead of the API.
#
# The script that does the writing (litellm-198-key-allowlist.py) reads back
# through the same /key/* API it wrote with, so it cannot testify about itself.
# This is the other ruler. Run it after every rollout.
#
# It encodes one lesson the hard way (2026-09-09): a verification query that
# says `where blocked is null or blocked = false` puts the blocked keys outside
# the ruler. Those keys keep their allowlist, so a model you just revoked is
# still sitting on them and comes back the day anyone unblocks the key -- and
# the rollout report says "0 remaining". Every count below is over the WHOLE
# table, split by blocked rather than filtered by it.
#
# Usage (default target is 198 prod; see the env block for the Aliyun proxy):
#   bash litellm-198-key-allowlist-verify.sh <model> [<model> ...]
#
#   REVOKED=1 bash ... <model>   # assert the names are GONE  (exit 1 if any remain)
#   GRANTED=1 bash ... <model>   # assert the names are REACHABLE on every unblocked key
#
# With neither flag it just reports and always exits 0.
#
# Env:
#   FAMS   key_alias prefixes in scope, comma separated.
#          Default 'cursor-,claude-,carher-'.
#          This is the assertion scope too, not just the report scope -- an
#          earlier version hardcoded cursor/claude in the GRANTED query while
#          reporting on all three, so a rollout that missed every carher-* key
#          passed. Narrow it explicitly for a single-family rollout.
#   ACCESS_GROUPS access-group tokens that also satisfy GRANTED, comma separated.
#          (Was GROUPS until 2026-09-16: bash makes GROUPS a readonly builtin
#          array of the caller's gids, so `GROUPS=pro198` was silently dropped and
#          read back as "0" -- every pro198 model verified as 375 false misses.)
#          Since 2026-09-09 a key can reach a model without listing its name:
#          the CM tags the entry with `model_info.access_groups: ["pro198"]`
#          and the key carries the single token `pro198`. Counting only literal
#          names would then report 375 misses on a fleet that works.
#          Conversely, for REVOKED, remember a group token still grants access:
#          removing the literal name from a key changes nothing while the group
#          is on it. The report prints group holders so this cannot hide.
#   NS / DB_POD / KUBECTL  to point at another deployment. For the Aliyun proxy:
#          NS=carher DB_POD=litellm-db-0 KUBECTL=kubectl FAMS=carher- ACCESS_GROUPS=pro198 \
#            GRANTED=1 bash litellm-198-key-allowlist-verify.sh grok-4.6
#          (198 prod needs `sudo kubectl` and lives in litellm-product; the
#          Aliyun cluster is reached over the jms tunnel with a plain kubectl.)
set -euo pipefail

NS=${NS:-litellm-product}
DB_POD=${DB_POD:-litellm-db-0}
KUBECTL=${KUBECTL:-sudo kubectl}
# shellcheck disable=SC2206
PSQL=(${KUBECTL} -n "$NS" exec -i "$DB_POD" -- psql -U litellm -d litellm)
FAMS=${FAMS:-cursor-,claude-,carher-}

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <model> [<model> ...]" >&2
    echo "  REVOKED=1 to assert absence, GRANTED=1 to assert presence" >&2
    echo "  FAMS=carher- ACCESS_GROUPS=pro198 NS=carher KUBECTL=kubectl for the Aliyun proxy" >&2
    exit 2
fi

# Reject anything that could break out of the single-quoting rather than trying
# to escape it. Applies to model names, group tokens and family prefixes alike.
reject_unsafe() {
    case "$1" in
        *"'"*|*'"'*|*';'*|*'\'*|"")
            echo "refusing unsafe value (quote/semicolon/backslash/empty): $1" >&2
            exit 2 ;;
    esac
}

# Build a SQL array literal, single-quoting each name.
sql_list=""
for m in "$@"; do
    reject_unsafe "$m"
    [ -n "$sql_list" ] && sql_list="$sql_list, "
    sql_list="$sql_list'$m'"
done

# Group tokens. Empty stays a valid IN-list that matches nothing.
group_list="''"
if [ -n "${ACCESS_GROUPS:-}" ]; then
    group_list=""
    IFS=, read -r -a _groups <<<"${ACCESS_GROUPS:-}"
    for g in "${_groups[@]}"; do
        reject_unsafe "$g"
        [ -n "$group_list" ] && group_list="$group_list, "
        group_list="$group_list'$g'"
    done
fi

# Family prefixes -> a CASE for grouping and a WHERE for scoping. Both derive
# from the same FAMS so the report and the assertions can never disagree.
fam_case="case"
fam_filter=""
IFS=, read -r -a _fams <<<"$FAMS"
for f in "${_fams[@]}"; do
    reject_unsafe "$f"
    fam_case="$fam_case when key_alias like '$f%' then '${f%-}'"
    [ -n "$fam_filter" ] && fam_filter="$fam_filter or "
    fam_filter="$fam_filter key_alias like '$f%'"
done
fam_case="$fam_case else 'other' end"

echo "=== targets: $* ==="
echo "=== scope: ns=$NS fams=$FAMS groups=${ACCESS_GROUPS:-<none>} ==="

# 1. Who still holds these names, split by family AND by blocked. The blocked
#    column is the whole point: it is where a "clean" rollout hides its misses.
"${PSQL[@]}" <<SQL
\echo '--- 1. holders, by family x blocked (NOT filtered by blocked) ---'
select $fam_case as fam,
       coalesce(blocked,false) as blk,
       m as model,
       count(*) as keys
from "LiteLLM_VerificationToken" t, unnest(t.models) m
where m in ($sql_list)
group by 1,2,3 order by 3,1,2;

\echo '--- 2. per-key aliases still referencing these names (src or dst) ---'
select k as alias_src, v as alias_dst,
       coalesce(blocked,false) as blk, count(*) as keys
from "LiteLLM_VerificationToken" t,
     jsonb_each_text(coalesce(t.aliases,'{}'::jsonb)) a(k,v)
where k in ($sql_list) or v in ($sql_list)
group by 1,2,3 order by 4 desc;

\echo '--- 3. shape check: avg models/aliases must shift by a whole name count ---'
select $fam_case as fam, coalesce(blocked,false) as blk, count(*) as keys,
       round(avg(cardinality(models)),2) as avg_models,
       round(avg(x.n),2) as avg_alias,
       count(*) filter (where cardinality(models)=0) as unrestricted
from "LiteLLM_VerificationToken",
     lateral (select count(*) n from jsonb_object_keys(coalesce(aliases,'{}'::jsonb))) x
where $fam_filter
group by 1,2 order by 1,2;

\echo '--- 4. models=[] keys: no allowlist to subtract, so a revoke CANNOT reach them ---'
select key_alias, coalesce(blocked,false) as blk
from "LiteLLM_VerificationToken"
where cardinality(models)=0 and ($fam_filter)
order by 1;

\echo '--- 5. resolvable? a name in models with no alias and no real group => 400 ---'
select m as model,
       count(*) as in_models,
       count(*) filter (where t.aliases ? m) as also_has_alias,
       count(*) filter (where not (t.aliases ? m)) as alias_missing
from "LiteLLM_VerificationToken" t, unnest(t.models) m
where m in ($sql_list)
group by 1 order by 1;

\echo '--- 6. is it a real model group at all? (empty => needs an alias to resolve) ---'
\echo '        (Aliyun ns=carher is ConfigMap-authoritative: this table is empty there'
\echo '         by design, so empty proves nothing -- read the CM model_list instead.)'
select model_name from "LiteLLM_ProxyModelTable"
where model_name in ($sql_list) order by 1;

\echo '--- 7. access-group tokens in scope (these grant WITHOUT the literal name) ---'
select m as group_token, $fam_case as fam,
       coalesce(blocked,false) as blk, count(*) as keys
from "LiteLLM_VerificationToken" t, unnest(t.models) m
where m in ($group_list)
group by 1,2,3 order by 1,2,3;
SQL

# Assertions. Counted over the whole table, blocked included.
remaining=$("${PSQL[@]}" -tAc "select count(*) from \"LiteLLM_VerificationToken\" t
    where exists (select 1 from unnest(t.models) m where m in ($sql_list));" | tr -d '[:space:]')

echo
echo "=== keys still holding any target name (blocked included): $remaining ==="

if [ "${REVOKED:-0}" = "1" ]; then
    if [ "$remaining" != "0" ]; then
        echo "FAIL: revoke incomplete -- $remaining key(s) still hold a target name." >&2
        echo "      If they are all blocked, rerun the write with --include-blocked." >&2
        exit 1
    fi
    # models=[] keys were never reachable by a revoke; say so out loud so it is
    # not silently reported as a clean sweep.
    unres=$("${PSQL[@]}" -tAc "select count(*) from \"LiteLLM_VerificationToken\"
        where cardinality(models)=0 and ($fam_filter);" | tr -d '[:space:]')
    echo "PASS: 0 keys hold the target name(s)."
    echo "NOTE: $unres unrestricted (models=[]) key(s) in scope still reach every model."
    echo "      A revoke cannot touch them -- report them separately, not as 0 remaining."
    if [ -n "${ACCESS_GROUPS:-}" ]; then
        viagrp=$("${PSQL[@]}" -tAc "select count(*) from \"LiteLLM_VerificationToken\" t
            where ($fam_filter)
              and exists (select 1 from unnest(t.models) m where m in ($group_list));" \
            | tr -d '[:space:]')
        echo "NOTE: $viagrp key(s) carry an access-group token (${ACCESS_GROUPS:-})."
        echo "      If the revoked model is a member of that group, dropping the literal"
        echo "      name revoked nothing -- check the CM's model_info.access_groups too."
    fi
fi

if [ "${GRANTED:-0}" = "1" ]; then
    # Reachable = holds a target name OR a group token OR the literal '*'.
    # '*' is all-model access, not a name: litellm's _check_model_access_helper
    # sets all_model_access when '*' is in the (non-group) allowlist. Measured
    # 2026-09-16 on the Aliyun proxy -- a throwaway key with models=["*"] got
    # 200 on a name it did not list. Counting those as misses reported 39 false
    # FAILs on keys that already work.
    wildcard=$("${PSQL[@]}" -tAc "select count(*) from \"LiteLLM_VerificationToken\"
        where ($fam_filter) and (blocked is null or blocked = false)
          and '*' = any(models);" | tr -d '[:space:]')
    missing=$("${PSQL[@]}" -tAc "select count(*) from \"LiteLLM_VerificationToken\" t
        where ($fam_filter)
          and (blocked is null or blocked = false)
          and cardinality(models) > 0
          and not ('*' = any(t.models))
          and not exists (select 1 from unnest(t.models) m
                          where m in ($sql_list) or m in ($group_list));" \
        | tr -d '[:space:]')
    if [ "$missing" != "0" ]; then
        echo "FAIL: $missing unblocked restricted key(s) in scope ($FAMS) cannot reach the name(s)." >&2
        exit 1
    fi
    echo "PASS: every unblocked restricted key in scope ($FAMS) can reach the name(s)."
    if [ "$wildcard" != "0" ]; then
        echo "NOTE: $wildcard key(s) counted as reachable via models containing '*'"
        echo "      (all-model access, so they reach every name -- and a revoke of a"
        echo "      literal name cannot touch them either)."
    fi
    if [ -n "${ACCESS_GROUPS:-}" ]; then
        echo "NOTE: reachability counted names OR group tokens (${ACCESS_GROUPS:-}). A group token"
        echo "      only grants if the CM entry carries model_info.access_groups -- this"
        echo "      query cannot see the CM, so pair it with a real raw-key probe."
    fi
fi
