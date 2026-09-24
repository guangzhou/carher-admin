#!/usr/bin/env python3
"""Mint a her LiteLLM key by cloning a reference key's LIVE config.

WHY THIS EXISTS
---------------
Every her key must carry the same model entitlement as the reference instance on
the same gateway.  Hard-coding that entitlement (the way
``backend/litellm_ops.py::ALL_MODELS`` does) means the day someone edits the
reference key, every key minted afterwards silently disagrees with it.

So this script NEVER ships a built-in model list.  It reads the reference key
over the API on every single run and copies what it finds.  If that read fails,
the script aborts -- it does not fall back to a snapshot, because a stale
snapshot is the exact failure we are trying to prevent.

TWO GATEWAYS, TWO DIFFERENT SHAPES -- DO NOT CROSS THEM
-------------------------------------------------------
hangzhou (default)  gw.carher.net / vllm 47.96.23.21, compose stack
    template  bot-1000
    key_alias bot-<uid>
    models    ["all-team-models"]   <- entitlement lives in the TEAM, not the key
    aliases   {}                    <- this gateway uses NO per-key alias
    team_id   copied from the template; see the note below
    budget    none (all 352 live bot keys are unmetered)

    NOTE on team_id: a key with models=["all-team-models"] and NO team_id is
    NOT broken on this build -- measured 2026-09-24, such a key sees all 31
    models and calls fine (all-team-models with no team falls through to
    unrestricted). Two live keys, bot-313 and bot-313-migration-poc, are in
    exactly that state. We still copy team_id, because 351 of 352 keys carry it
    and it is where any future budget / rate limit would be enforced -- a
    team-less key would silently escape it.

singapore           litellm-proxy.carher.svc in the carher namespace, K8s
    template  carher-1000
    key_alias carher-<uid>
    models    ~20 explicit names, several of them alias-only
    aliases   ~15 entries; a key without the map 400s on every bare name
    team_id   not used
    budget    fleet default 100

Pointing a profile at the wrong gateway is the main foot-gun here, so the
script probes for the other profile's template and refuses loudly when it finds
it.  See --profile.

WHAT IS COPIED / WHAT IS NOT
----------------------------
copied   : models[], aliases{}, team_id     <- read live from the template, every run
not      : key_alias, user_id, metadata     <- per instance, built from --uid/--name
not      : max_budget                       <- profile default; override with --max-budget

USAGE
-----
  show    : print the live template, touch nothing
  plan    : print the exact payload that would be POSTed, touch nothing
  apply   : create the key, then re-read it and diff against the template
  verify  : diff an EXISTING key against the template (read-only)

  ./litellm-her-key-from-template.py show
  ./litellm-her-key-from-template.py plan   --uid 999
  ./litellm-her-key-from-template.py apply  --uid 999 --name "张三的her"
  ./litellm-her-key-from-template.py verify --uid 999

CONNECTION
----------
  --base-url   default http://127.0.0.1:4000
  --master-key default $LITELLM_MASTER_KEY

  hangzhou -- run it ON the box, where the master key already lives:
    scripts/jms ssh vllm 'cd /opt/llm-gateway && set -a && . ./.env && set +a && \
      /opt/llm-gateway/litellm-her-key-from-template.py show'

  singapore -- tunnel in first:
    kubectl port-forward -n carher svc/litellm-proxy 4000:4000
    export LITELLM_MASTER_KEY=$(kubectl get secret -n carher litellm-secrets \
      -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HTTP_TIMEOUT = 30

PROFILES: dict[str, dict] = {
    "hangzhou": {
        "template_alias": "bot-1000",
        "alias_fmt": "bot-{uid}",
        # Entitlement is inherited from the team on this gateway, so the team id
        # is part of the template, not a per-instance choice.
        "copy_team_id": True,
        # aliases is legitimately {} here -- do NOT treat empty as a failure.
        "expect_aliases": False,
        "default_budget": None,
        "gateway": "gw.carher.net / vllm 47.96.23.21 (阿里云杭州, compose)",
    },
    "singapore": {
        "template_alias": "carher-1000",
        "alias_fmt": "carher-{uid}",
        "copy_team_id": False,
        # Bare names like her-flash are alias-only here; an empty map is a bug.
        "expect_aliases": True,
        "default_budget": 100.0,
        "gateway": "litellm-proxy.carher.svc ns=carher (阿里云新加坡, K8s)",
    },
}
DEFAULT_PROFILE = "hangzhou"


class Fatal(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #

def _request(base_url: str, master_key: str, path: str, payload: dict | None = None) -> dict:
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        raise Fatal(f"{path} -> HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise Fatal(f"{path} -> unreachable: {exc}") from exc
    return json.loads(raw) if raw else {}


def read_key(base_url: str, master_key: str, alias: str) -> dict | None:
    """Read one key's full row by alias.

    NOTE: /key/info?key_alias= returns 404 on the singapore build -- the read
    path that works on BOTH gateways is /key/list with return_full_object=true.
    Verified 2026-09-24.
    """
    query = urllib.parse.urlencode(
        {"key_alias": alias, "return_full_object": "true", "size": 5}
    )
    data = _request(base_url, master_key, f"/key/list?{query}")
    for row in data.get("keys", []) or []:
        if isinstance(row, dict) and row.get("key_alias") == alias:
            return row
    return None


# --------------------------------------------------------------------------- #
# template
# --------------------------------------------------------------------------- #

def _wrong_gateway_hint(base_url: str, master_key: str, profile: str) -> str:
    """If the OTHER profile's template is present, we are on the wrong box."""
    for other, cfg in PROFILES.items():
        if other == profile:
            continue
        try:
            if read_key(base_url, master_key, cfg["template_alias"]) is not None:
                return (
                    f" Found {cfg['template_alias']} instead -- {base_url} looks like "
                    f"the '{other}' gateway ({cfg['gateway']}). "
                    f"You are running --profile {profile}. Fix the profile or the "
                    "base-url; do not cross the two."
                )
        except Fatal:
            pass
    return ""


def fetch_template(base_url: str, master_key: str, profile: str) -> dict:
    """Read the reference key live. Fail closed -- never substitute a built-in list."""
    cfg = PROFILES[profile]
    alias = cfg["template_alias"]
    row = read_key(base_url, master_key, alias)
    if row is None:
        raise Fatal(
            f"template key {alias} not found on {base_url}."
            + _wrong_gateway_hint(base_url, master_key, profile)
            + " Refusing to mint a key from a guessed model list."
        )
    models = list(row.get("models") or [])
    aliases = dict(row.get("aliases") or {})
    team_id = row.get("team_id") or ""
    if not models:
        raise Fatal(
            f"{alias} returned an EMPTY model list. That is either a read bug or "
            "someone wiped the template -- both are reasons to stop, not to fall "
            "back to a default."
        )
    if cfg["expect_aliases"] and not aliases:
        raise Fatal(
            f"{alias} returned an EMPTY alias map, but profile '{profile}' needs "
            "one: bare names are alias-only on that gateway, so a key without the "
            "map would 400 on every call. Stopping."
        )
    if cfg["copy_team_id"] and not team_id:
        raise Fatal(
            f"{alias} has no team_id, but profile '{profile}' expects the template "
            f"to carry one (models={models} inherits limits from the team). The "
            "template itself has drifted from the fleet convention -- fix it "
            "before minting from it. NOTE: a team-less key still reaches every "
            "model on this build; the loss is that it escapes any team-level "
            "budget or rate limit."
        )
    if not cfg["copy_team_id"] and not aliases and not cfg["expect_aliases"]:
        pass  # nothing to assert
    return {"models": models, "aliases": aliases, "team_id": team_id, "row": row}


def build_payload(
    uid: int,
    tpl: dict,
    profile: str,
    name: str = "",
    email: str = "",
    team_id: str = "",
    max_budget: float | None = None,
) -> dict:
    cfg = PROFILES[profile]
    alias = cfg["alias_fmt"].format(uid=uid)
    payload: dict[str, object] = {
        "key_alias": alias,
        "models": tpl["models"],
    }
    # aliases: only send it when the template actually carries one. Sending {}
    # on hangzhou would be harmless but noisy; sending nothing keeps the new key
    # byte-identical in shape to the 352 live ones.
    if tpl["aliases"]:
        payload["aliases"] = tpl["aliases"]

    chosen_team = team_id or (tpl["team_id"] if cfg["copy_team_id"] else "")
    if chosen_team:
        payload["team_id"] = chosen_team

    # The singapore fleet carries user_id/metadata; the hangzhou fleet has both
    # empty on all 352 keys. Follow whichever the template does, so a new key
    # does not become the only odd one out.
    tpl_row = tpl["row"]
    if tpl_row.get("user_id"):
        payload["user_id"] = alias
    tpl_meta = tpl_row.get("metadata") or {}
    metadata: dict[str, object] = {}
    if tpl_meta or name or email:
        metadata = {"instance": alias, "her_id": str(uid)}
        if name:
            metadata["owner_name"] = name
        if email:
            metadata["email"] = email
    if metadata:
        payload["metadata"] = metadata

    if max_budget is not None:
        payload["max_budget"] = max_budget
    return payload


def diff_against_template(row: dict, tpl: dict, profile: str) -> list[str]:
    cfg = PROFILES[profile]
    problems: list[str] = []

    got_models = set(row.get("models") or [])
    want_models = set(tpl["models"])
    for missing in sorted(want_models - got_models):
        problems.append(f"missing model: {missing}")
    for extra in sorted(got_models - want_models):
        problems.append(f"extra model: {extra}")

    got_aliases = dict(row.get("aliases") or {})
    for key in sorted(set(tpl["aliases"]) | set(got_aliases)):
        want = tpl["aliases"].get(key)
        got = got_aliases.get(key)
        if want != got:
            problems.append(f"alias {key}: want {want!r}, got {got!r}")

    # On hangzhou the team is where any budget / rate limit would be enforced,
    # so a team mismatch is a real drift even though the key still reaches every
    # model without one (measured 2026-09-24).
    if cfg["copy_team_id"]:
        want_team = tpl["team_id"]
        got_team = row.get("team_id") or ""
        if want_team != got_team:
            problems.append(f"team_id: want {want_team!r}, got {got_team!r}")
    return problems


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def _banner(args) -> None:
    cfg = PROFILES[args.profile]
    print(f"profile  = {args.profile}  ({cfg['gateway']})")
    print(f"base-url = {args.base_url}")
    print(f"template = {cfg['template_alias']}  (read live, never cached)")


def cmd_show(args) -> int:
    tpl = fetch_template(args.base_url, args.master_key, args.profile)
    _banner(args)
    print(f"\nmodels ({len(tpl['models'])}):")
    for m in tpl["models"]:
        print(f"  - {m}")
    if tpl["aliases"]:
        print(f"\naliases ({len(tpl['aliases'])}):")
        for k in sorted(tpl["aliases"]):
            print(f"  {k:<24} -> {tpl['aliases'][k]}")
    else:
        print("\naliases: {} (none -- correct for this gateway)")
    print(f"\nteam_id  = {tpl['team_id'] or '(none)'}")
    if tpl["models"] == ["all-team-models"] and tpl["team_id"]:
        expanded = _expand_team(args, tpl["team_id"])
        if expanded is not None:
            print(f"  ^ all-team-models expands to {expanded} live models")
    return 0


def _expand_team(args, team_id: str) -> int | None:
    """Best effort: how many models does this team actually grant right now."""
    try:
        info = _request(args.base_url, args.master_key,
                        f"/team/info?team_id={urllib.parse.quote(team_id)}")
        models = (info.get("team_info") or {}).get("models") or []
        if models == ["all-proxy-models"]:
            live = _request(args.base_url, args.master_key, "/v1/models")
            return len(live.get("data") or [])
        return len(models)
    except Fatal:
        return None


def cmd_plan(args) -> int:
    tpl = fetch_template(args.base_url, args.master_key, args.profile)
    alias = PROFILES[args.profile]["alias_fmt"].format(uid=args.uid)
    existing = read_key(args.base_url, args.master_key, alias)
    if existing is not None:
        _banner(args)
        print(f"\nREFUSE: {alias} already exists (key_name={existing.get('key_name')}).")
        print("        Use /key/update or pick another uid. Not clobbering it.")
        return 2
    payload = build_payload(
        args.uid, tpl, args.profile,
        name=args.name, email=args.email,
        team_id=args.team_id, max_budget=args.max_budget,
    )
    _banner(args)
    print(f"\nPOST {args.base_url.rstrip('/')}/key/generate")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\n(plan only, nothing written; copied {len(tpl['models'])} models / "
          f"{len(tpl['aliases'])} aliases / team_id="
          f"{tpl['team_id'] or '(none)'})")
    return 0


def cmd_apply(args) -> int:
    tpl = fetch_template(args.base_url, args.master_key, args.profile)
    alias = PROFILES[args.profile]["alias_fmt"].format(uid=args.uid)
    if read_key(args.base_url, args.master_key, alias) is not None:
        print(f"REFUSE: {alias} already exists. Not clobbering it.", file=sys.stderr)
        return 2
    payload = build_payload(
        args.uid, tpl, args.profile,
        name=args.name, email=args.email,
        team_id=args.team_id, max_budget=args.max_budget,
    )
    created = _request(args.base_url, args.master_key, "/key/generate", payload)
    secret = created.get("key", "")
    if not secret:
        print("FAIL: /key/generate returned no key field", file=sys.stderr)
        return 1

    # Verify against the SAME template read this run, not against a snapshot.
    back = read_key(args.base_url, args.master_key, alias)
    if back is None:
        print(f"FAIL: created {alias} but cannot read it back", file=sys.stderr)
        return 1
    problems = diff_against_template(back, tpl, args.profile)

    _banner(args)
    print(f"\ncreated {alias}")
    print(f"key = {secret}")
    print("  ^ plaintext is returned ONLY here. Store it now; the DB keeps a hash.")
    print(f"\nmodels={back.get('models')} "
          f"aliases={len(back.get('aliases') or {})} "
          f"team_id={back.get('team_id') or '(none)'} "
          f"budget={back.get('max_budget')}")
    if problems:
        print(f"\nVERIFY FAILED -- {alias} does NOT match "
              f"{PROFILES[args.profile]['template_alias']}:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"\nVERIFY OK: matches {PROFILES[args.profile]['template_alias']} exactly")
    print("NOTE: entitlement is only proven by a real inference call. /key/list "
          "reading back clean does NOT mean the key can reach a model.")
    return 0


def cmd_verify(args) -> int:
    tpl = fetch_template(args.base_url, args.master_key, args.profile)
    alias = PROFILES[args.profile]["alias_fmt"].format(uid=args.uid)
    row = read_key(args.base_url, args.master_key, alias)
    if row is None:
        print(f"{alias}: NOT FOUND on {args.base_url} (profile {args.profile})",
              file=sys.stderr)
        return 2
    problems = diff_against_template(row, tpl, args.profile)
    tmpl_alias = PROFILES[args.profile]["template_alias"]
    if not problems:
        print(f"{alias}: OK -- identical to {tmpl_alias}")
        return 0
    print(f"{alias}: DRIFTED from {tmpl_alias} ({len(problems)} differences)")
    for p in problems:
        print(f"  - {p}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mint a her key by cloning a reference key's live config.",
    )
    parser.add_argument("--profile", choices=sorted(PROFILES),
                        default=os.getenv("LITELLM_KEY_PROFILE", DEFAULT_PROFILE),
                        help=f"which gateway (default {DEFAULT_PROFILE})")
    parser.add_argument("--base-url",
                        default=os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000"))
    parser.add_argument("--master-key", default=os.getenv("LITELLM_MASTER_KEY", ""))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="print the live template")

    for name, fn in (("plan", cmd_plan), ("apply", cmd_apply)):
        p = sub.add_parser(name, help=f"{name} a new her key")
        p.add_argument("--uid", type=int, required=True)
        p.add_argument("--name", default="", help="owner display name -> metadata.owner_name")
        p.add_argument("--email", default="", help="owner email -> metadata.email")
        p.add_argument("--team-id", default="", help="override the template's team_id")
        p.add_argument("--max-budget", type=float, default=None,
                       help="default: the profile's fleet convention "
                            "(hangzhou=none, singapore=100); pass -1 for none")
        p.set_defaults(func=fn)

    pv = sub.add_parser("verify", help="diff an existing key against the template")
    pv.add_argument("--uid", type=int, required=True)
    pv.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    if args.cmd == "show":
        args.func = cmd_show
    if not args.master_key:
        print("missing --master-key / $LITELLM_MASTER_KEY", file=sys.stderr)
        return 2
    if hasattr(args, "max_budget"):
        if args.max_budget is None:
            args.max_budget = PROFILES[args.profile]["default_budget"]
        elif args.max_budget < 0:
            args.max_budget = None
    try:
        return args.func(args)
    except Fatal as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
