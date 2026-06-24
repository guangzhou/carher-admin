#!/usr/bin/env bash
# vip-group-list.sh
#
# Inventory all chatgpt-vip-<group>-* groups + their backing acct + member keys.
# Read-only; safe to run anytime.

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/lib.sh"

echo "== chatgpt-vip groups =="
psql_cmd "SELECT model_name AS vip, model_id AS entry FROM \"LiteLLM_ProxyModelTable\" WHERE model_name LIKE 'chatgpt-vip-%' ORDER BY model_name;" \
  | awk -F'|' 'BEGIN{print "GROUP\tACCT\tSHORT"} {
      vip=$1; entry=$2;
      sub(/^chatgpt-vip-/, "", vip);
      n=split(vip, a, "-gpt-");
      grp=a[1]; short="gpt-"a[2];
      match(entry, /chatgpt-acct-[0-9]+/);
      acct=substr(entry, RSTART, RLENGTH);
      print grp"\t"acct"\t"short
    }' | column -t

echo
echo "== keys with vip aliases =="
psql_cmd "SELECT key_alias, aliases::text FROM \"LiteLLM_VerificationToken\" WHERE aliases::text LIKE '%chatgpt-vip-%' ORDER BY key_alias;" \
  | awk -F'|' '{
      if ($1=="") next;
      n=match($2, /chatgpt-vip-[a-z0-9_-]+-gpt-[0-9.a-z-]+/);
      vip=(n>0)?substr($2, RSTART, RLENGTH):"<none>";
      sub(/-gpt-[0-9.a-z-]+$/, "", vip);
      sub(/^chatgpt-vip-/, "", vip);
      print $1"\t"vip
    }' | column -t
