#!/usr/bin/env python3
"""Append a fallback target to every 198 fallback chain that ends in an anchor.

Motivation
----------
The 198 ``litellm-product`` GPT-family model groups all end their fallback
chain in the official DeepSeek Flash *responses* group
(``deepseek-v4-flash-responses``) as a cheap last resort. We want to append the
official DeepSeek **Pro** responses group *after* flash — flash first (cheap),
Pro last (pricier, higher quality) — without disturbing any other chain or any
non-``fallbacks`` router setting.

Why not ``/config/update``
--------------------------
``POST /config/update`` with a partial ``router_settings`` **silently drops**
``model_group_alias`` (10 aliases -> {}), a footgun that only detonates on the
next restart (see feedback_config_update_silently_drops_unknown_router_settings,
2026-08-02). This script instead performs a surgical
``jsonb_set(param_value, '{fallbacks}', ...)`` on the ``LiteLLM_Config`` row,
which can only ever touch the ``fallbacks`` key; every other router setting is
preserved byte-for-byte (asserted on readback).

Runtime
-------
Runs where ``kubectl -n litellm-product ...`` works (i.e. on the 198 host as
root, or with a kubeconfig that has access). Reads the master key from the
``litellm-secrets`` Secret; reads/writes ``router_settings`` via the DB pod;
verifies live propagation on every proxy pod and, only if a raw DB write is not
picked up, triggers a zero-interruption ``rollout restart`` (on boot the proxy
reloads router_settings from the DB, which overrides the ConfigMap).

Default is dry-run. ``--apply`` requires ``--backup <path>``.

Examples
--------
  # preview the change (no writes)
  python3 scripts/litellm-198-gpt-fallback-append.py

  # apply: append deepseek-v4-pro-responses after deepseek-v4-flash-responses
  python3 scripts/litellm-198-gpt-fallback-append.py \
      --backup ~/198-fallbacks-$(date +%Y%m%dT%H%M%S).json --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

NAMESPACE = "litellm-product"
DB_POD = "litellm-db-0"
DB_USER = "litellm"
DB_NAME = "litellm"
# 🔴 生产车道的权威判据是 Service `litellm-proxy-nodeport`（nodePort 30402）的
#    selector，不是 Deployment 名。车道搬过家（09-24 前是 litellm-proxy-gray），
#    还会再搬，所以这里按**路由标签**选，不写死名字。
#    ⚠️ 标签在 **Pod**（podTemplate）上：`get deploy -l <该标签>` 返
#    "No resources found" 是**假红**。选 pod 一律 `get pods -l`。
#    2026-09-25 修：这里原先写死 `app=litellm-proxy`，会验到不在服务的车道。
PROXY_SELECTOR = "carher.net/litellm-production-route=enabled"
PROXY_PORT = "4000"
SECRET = "litellm-secrets"

DEFAULT_ANCHOR = "deepseek-v4-flash-responses"
DEFAULT_APPEND = "deepseek-v4-pro-responses"


def plan_fallbacks(
    fallbacks: list[dict], anchor: str, append: str,
    groups: set[str] | None = None,
) -> tuple[list[dict], list[tuple[str, list, list]]]:
    """Return (new_fallbacks, changes).

    Two modes:

    **anchor mode** (``groups is None``) — the original behaviour. For each
    single-key ``{group: [chain...]}`` entry whose chain *ends* with ``anchor``
    and does not already contain ``append``, append ``append`` to the chain.
    Every other entry is copied through unchanged. Idempotent: a chain that
    already ends ``[..., anchor, append]`` is left alone, and a chain that
    merely contains ``append`` anywhere is not double-appended.

    **named-groups mode** (``groups`` given) — ``anchor`` is ignored entirely.
    For each entry whose group is in ``groups``, make ``append`` the **last**
    leg. Added 2026-09-25 because "every gpt-series group must end in
    deepseek(openrouter)" does not map onto a single anchor: the 53 in-scope
    chains end in **7 different** legs, and anchor mode would drag in non-gpt
    chains sharing those tails (36 chains end in ``deepseek-v4-pro-responses``
    alone, not all of them gpt).

    Named-groups mode **de-duplicates rather than double-appends**: if the
    target is already present mid-chain it is *moved* to the end, not added a
    second time (``gpt-5.6-sol`` is exactly this case — it already carries
    ``openrouter-deepseek-v4.1-flash`` between ``sa-grok-4.6`` and the two
    ``deepseek-v4-*-responses`` legs).

    🔴 A group named in ``groups`` but absent from ``fallbacks`` is **not**
    created — it is returned to the caller as a miss (see ``plan_misses``).
    Silently inventing a chain for a group that has none would be a new routing
    decision disguised as an edit.
    """
    new: list[dict] = []
    changes: list[tuple[str, list, list]] = []
    for entry in fallbacks:
        if len(entry) != 1:
            raise ValueError(f"fallback entry is not a single-key dict: {entry!r}")
        (group, chain), = entry.items()
        chain = list(chain)
        if groups is not None:
            if group in groups and chain and chain[-1] != append:
                new_chain = [c for c in chain if c != append] + [append]
                new.append({group: new_chain})
                changes.append((group, chain, new_chain))
            else:
                new.append({group: chain})
            continue
        if chain and chain[-1] == anchor and append not in chain:
            new_chain = chain + [append]
            new.append({group: new_chain})
            changes.append((group, chain, new_chain))
        else:
            new.append({group: chain})
    return new, changes


def plan_misses(fallbacks: list[dict], groups: set[str]) -> list[str]:
    """Groups asked for that have no fallback row at all — caller must decide."""
    present = {next(iter(e)) for e in fallbacks}
    return sorted(groups - present)



# --------------------------------------------------------------------------- #
# I/O helpers (only used by main(); kept out of import path for testability).
# --------------------------------------------------------------------------- #
def _kubectl(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args],
        check=True, text=True, capture_output=True, input=input_text,
    ).stdout


def _psql(sql: str) -> str:
    return _kubectl(
        "exec", "-i", DB_POD, "--",
        "psql", "-U", DB_USER, "-d", DB_NAME, "-tAc", sql,
    ).strip()


def _read_router_settings() -> dict:
    raw = _kubectl(
        "exec", "-i", DB_POD, "--",
        "psql", "-U", DB_USER, "-d", DB_NAME, "-tAc",
        "SELECT param_value FROM \"LiteLLM_Config\" WHERE param_name='router_settings';",
    ).strip()
    if not raw:
        raise SystemExit("router_settings row not found in LiteLLM_Config")
    return json.loads(raw)


def _master_key() -> str:
    import base64
    raw = _kubectl("get", "secret", SECRET, "-o",
                   "jsonpath={.data.LITELLM_MASTER_KEY}")
    return base64.b64decode(raw).decode()


def _proxy_pod_ips() -> list[str]:
    out = _kubectl("get", "pods", "-l", PROXY_SELECTOR,
                   "--field-selector=status.phase=Running",
                   "-o", "jsonpath={range .items[*]}{.status.podIP}{'\\n'}{end}")
    return [ip for ip in out.split("\n") if ip.strip()]


def _production_lane_deployment() -> str:
    """Which Deployment owns the pods that currently carry the route label.

    🔴 Never hardcode the lane name — it has moved before (09-24 前是
    `litellm-proxy-gray`) and will move again. Derived from the labelled Pods'
    ownerRef (ReplicaSet) with the RS hash suffix stripped, so this follows the
    label wherever it goes. Refuses to guess if the labelled pods span more
    than one Deployment.
    """
    out = _kubectl("get", "pods", "-l", PROXY_SELECTOR,
                   "--field-selector=status.phase=Running", "-o",
                   "jsonpath={range .items[*]}{.metadata.ownerReferences[0].name}{'\\n'}{end}")
    lanes = {rs.rsplit("-", 1)[0] for rs in out.split("\n") if rs.strip()}
    if len(lanes) != 1:
        raise SystemExit(
            f"cannot identify a single production lane from route label; got {sorted(lanes)}. "
            "Restart it by hand rather than letting this script guess.")
    return lanes.pop()


def _pod_fallbacks(pod_ip: str, mk: str) -> list[dict]:
    import urllib.request
    req = urllib.request.Request(
        f"http://{pod_ip}:{PROXY_PORT}/get/config/callbacks",
        headers={"Authorization": f"Bearer {mk}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["router_settings"]["fallbacks"]


def _verify_preserved(before: dict, after: dict, expected_fb: list[dict]) -> list[str]:
    problems: list[str] = []
    if set(before) != set(after):
        problems.append(f"router_settings keys changed: {sorted(before)} -> {sorted(after)}")
    for k in before:
        if k == "fallbacks":
            continue
        if before.get(k) != after.get(k):
            problems.append(f"non-fallbacks key mutated: {k}")
    if after.get("fallbacks") != expected_fb:
        problems.append("fallbacks in DB != expected after write")
    return problems


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--anchor", default=DEFAULT_ANCHOR)
    p.add_argument("--append", dest="append_target", default=DEFAULT_APPEND)
    p.add_argument("--groups-file",
                   help="named-groups mode: one group name per line (# comments ok). "
                        "Makes --append the LAST leg of exactly these groups; "
                        "--anchor is ignored. Groups with no fallback row are "
                        "reported and skipped, never created.")
    p.add_argument("--backup", help="path to write current router_settings before applying")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--reload-timeout", type=int, default=60,
                   help="seconds to wait for live propagation before rollout restart")
    p.add_argument("--skip-live-verify", action="store_true",
                   help="write + DB-readback verify only; skip per-pod poll and "
                        "rollout restart (use when a coordinated external restart "
                        "will handle propagation, e.g. a concurrent CM change)")
    a = p.parse_args()

    rs = _read_router_settings()
    fb = rs.get("fallbacks", [])
    groups = None
    if a.groups_file:
        groups = {l.strip() for l in open(a.groups_file)
                  if l.strip() and not l.lstrip().startswith("#")}
        if not groups:
            raise SystemExit("--groups-file is empty")
    new_fb, changes = plan_fallbacks(fb, a.anchor, a.append_target, groups)

    if groups is not None:
        misses = plan_misses(fb, groups)
        print(f"mode=named-groups asked={len(groups)} append={a.append_target}")
        print(f"total fallback entries={len(fb)} to_change={len(changes)} "
              f"already_ok={len(groups) - len(changes) - len(misses)} no_chain={len(misses)}")
        if misses:
            print("  ⚠️ 这些组没有 fallback 行，**跳过不新建**（新建=一个新的路由决定，"
                  "不是一次编辑）：", ", ".join(misses))
    else:
        print(f"mode=anchor anchor={a.anchor} append={a.append_target}")
        print(f"total fallback entries={len(fb)} to_change={len(changes)}")

    for group, old, new in changes[:60]:
        print(f"  {group}: {old} -> {new}")
    if len(changes) > 60:
        print(f"  ... {len(changes) - 60} more")

    if not a.apply:
        print("[dry-run] no writes. add --apply --backup <path> to write.")
        return 0
    if not a.backup:
        raise SystemExit("--apply requires --backup <path>")
    if not changes:
        print("nothing to change; DB already in desired state.")
        return 0

    with open(a.backup, "w") as f:
        json.dump(rs, f, ensure_ascii=False, indent=2)
    print(f"backup written: {a.backup}")

    sql = (
        "UPDATE \"LiteLLM_Config\" SET param_value = "
        "jsonb_set(param_value, '{fallbacks}', $json$"
        + json.dumps(new_fb) +
        "$json$::jsonb) WHERE param_name='router_settings';"
    )
    print("writing via jsonb_set ...")
    print(_kubectl("exec", "-i", DB_POD, "--",
                   "psql", "-U", DB_USER, "-d", DB_NAME, "-c", sql).strip())

    after = _read_router_settings()
    problems = _verify_preserved(rs, after, new_fb)
    if problems:
        print("DB VERIFY FAILED:", file=sys.stderr)
        print(json.dumps(problems, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print("DB verify OK: fallbacks updated, all other router_settings preserved.")

    if a.skip_live_verify:
        print("--skip-live-verify: DB write verified; leaving propagation to a "
              "coordinated restart. Re-run without the flag (or check per-pod "
              "/get/config/callbacks) to confirm live state.")
        return 0

    # Live propagation: poll every proxy pod; rollout restart only if needed.
    mk = _master_key()
    pod_ips = _proxy_pod_ips()
    deadline = time.time() + a.reload_timeout
    live_ok = False
    while time.time() < deadline:
        states = {}
        for ip in pod_ips:
            try:
                states[ip] = _pod_fallbacks(ip, mk) == new_fb
            except Exception as exc:  # noqa: BLE001
                states[ip] = f"err:{exc}"
        if all(v is True for v in states.values()):
            live_ok = True
            break
        time.sleep(5)
    if live_ok:
        print(f"live propagation OK on all {len(pod_ips)} pods (no restart needed).")
        return 0

    print("raw DB write not picked up live; performing zero-interruption rollout restart.")
    lane = _production_lane_deployment()
    print(f"production lane (by route label, not by name) = deployment/{lane}")
    _kubectl("rollout", "restart", f"deployment/{lane}")
    _kubectl("rollout", "status", f"deployment/{lane}", "--timeout=360s")
    time.sleep(5)
    pod_ips = _proxy_pod_ips()
    final = {ip: (_pod_fallbacks(ip, mk) == new_fb) for ip in pod_ips}
    if all(final.values()):
        print(f"post-restart verify OK on all {len(pod_ips)} pods.")
        return 0
    print("POST-RESTART VERIFY FAILED:", json.dumps(final, default=str), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
