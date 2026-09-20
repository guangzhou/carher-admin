#!/usr/bin/env python3
"""Switch the default model of CarHer Her instances by copying a reference instance.

Changing a Her's default model cannot go through the CRD: `spec.model` has an enum
allowlist that self-hosted models are not in. The only route is a per-instance
user-config override, which hot-reloads in ~120s with zero pod restarts.

Two production outages came out of doing this by hand, so the gates are code here:

  preflight  - refuses to proceed unless, for every target: the LiteLLM key
               allowlists the model, the base-config ConfigMap *that the pod
               actually mounts* carries a catalog entry for it, and the model
               group has either a second deployment or a router fallback.
  switch     - copies `agents.defaults.model` from a reference instance, writes a
               timestamped backup, and applies a path-level leaf diff guard so a
               malformed patch cannot silently drop unrelated config.
  verify     - reads the merged config back out of each pod and reports restarts.
  rollback   - restores the timestamped backups, guarded on the expected old primary.

Everything defaults to a dry run; pass --apply to write.

Typical run (see skills/carher-instance-config-override):

    ./carher_her_model_switch.py preflight --uids 25,26,71 --reference 1000
    ./carher_her_model_switch.py switch    --uids 25,26,71 --reference 1000 --apply
    ./carher_her_model_switch.py verify    --uids 25,26,71 --reference 1000
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Iterator

MODEL_PATH_PREFIX = "/agents/defaults/model"


# --------------------------------------------------------------------------- #
# kubectl plumbing
# --------------------------------------------------------------------------- #


def kubectl(
    args: list[str],
    kubeconfig: str | None,
    namespace: str,
    *,
    input_text: str | None = None,
    check: bool = True,
) -> str:
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += ["-n", namespace, *args]
    proc = subprocess.run(cmd, input=input_text, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def litellm_pod(kubeconfig: str | None, namespace: str) -> str:
    out = kubectl(
        ["get", "pods", "--no-headers", "-l", "app=litellm-proxy", "-o", "name"],
        kubeconfig,
        namespace,
    )
    names = [line.split("/", 1)[-1] for line in out.split("\n") if line.strip()]
    if not names:
        raise RuntimeError("no litellm-proxy pod found")
    return names[0]


def litellm_query(script: str, kubeconfig: str | None, namespace: str) -> str:
    """Run a Python snippet inside the LiteLLM pod.

    The proxy image ships no curl, so stdin-fed python3 is the only way in.
    """
    pod = litellm_pod(kubeconfig, namespace)
    return kubectl(
        ["exec", "-i", pod, "-c", "litellm", "--", "python3", "-"],
        kubeconfig,
        namespace,
        input_text=script,
    )


# --------------------------------------------------------------------------- #
# cluster state
# --------------------------------------------------------------------------- #


@dataclass
class Instance:
    uid: str
    cm_name: str
    config: dict[str, Any]
    litellm_key: str = ""
    pod: str = ""
    restarts: str = "?"
    age: str = "?"
    base_config_cm: str = ""

    @property
    def primary(self) -> str | None:
        return (((self.config.get("agents") or {}).get("defaults") or {}).get("model") or {}).get(
            "primary"
        )


def load_instances(
    uids: list[str], kubeconfig: str | None, namespace: str
) -> dict[str, Instance]:
    cms = json.loads(kubectl(["get", "cm", "-o", "json"], kubeconfig, namespace))["items"]
    raw = {
        cm["metadata"]["name"]: (cm.get("data") or {}).get("openclaw.json")
        for cm in cms
        if cm["metadata"]["name"].endswith("-user-config")
    }
    hers = json.loads(kubectl(["get", "her", "-o", "json"], kubeconfig, namespace))["items"]
    keys = {
        str(h["spec"].get("userId")): h["spec"].get("litellmKey", "")
        for h in hers
        if h.get("spec", {}).get("userId") is not None
    }
    pods = json.loads(kubectl(["get", "pods", "-o", "json"], kubeconfig, namespace))["items"]

    out: dict[str, Instance] = {}
    for uid in uids:
        name = f"carher-{uid}-user-config"
        if not raw.get(name):
            print(f"[{uid}] no {name} ConfigMap -- skipped", file=sys.stderr)
            continue
        inst = Instance(uid=uid, cm_name=name, config=json.loads(raw[name]), litellm_key=keys.get(uid, ""))
        for pod in pods:
            pname = pod["metadata"]["name"]
            if not pname.startswith(f"carher-{uid}-"):
                continue
            inst.pod = pname
            statuses = pod.get("status", {}).get("containerStatuses") or []
            inst.restarts = str(sum(c.get("restartCount", 0) for c in statuses))
            inst.age = pod.get("status", {}).get("startTime", "?")
            # Never infer which base-config a pod mounts from its ID range --
            # instances outside the h75 range mount carher-base-config-h75 too.
            mounted = [
                v["configMap"]["name"]
                for v in pod["spec"].get("volumes", [])
                if v.get("configMap") and "base-config" in v["configMap"]["name"]
            ]
            inst.base_config_cm = mounted[0] if mounted else ""
            break
        out[uid] = inst
    return out


# --------------------------------------------------------------------------- #
# diff guard
# --------------------------------------------------------------------------- #


def leaves(obj: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, obj


@dataclass
class Diff:
    changed: list[tuple[str, Any, Any]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def render(self, indent: str = "      ") -> str:
        lines = [f"{indent}{p}: {old!r} -> {new!r}" for p, old, new in self.changed]
        lines += [f"{indent}REMOVED {p}" for p in self.removed]
        return "\n".join(lines)


def diff_leaves(old: dict[str, Any], new: dict[str, Any]) -> Diff:
    ol, nl = dict(leaves(old)), dict(leaves(new))
    return Diff(
        changed=[(k, ol.get(k, "<absent>"), nl[k]) for k in nl if ol.get(k, "<absent>") != nl[k]],
        removed=[k for k in ol if k not in nl],
    )


def build_new_config(
    old: dict[str, Any], ref_model: dict[str, Any], ref_models: dict[str, Any], target_model: str
) -> dict[str, Any]:
    """Copy the reference instance's model block; keep every existing alias.

    Users rely on the old aliases (`/model gpt`) to switch back themselves, so
    aliases are only ever added, never replaced.
    """
    new = json.loads(json.dumps(old))
    defaults = new.setdefault("agents", {}).setdefault("defaults", {})
    defaults["model"] = json.loads(json.dumps(ref_model))
    for alias_key, alias_val in ref_models.items():
        if alias_key.endswith(target_model):
            defaults.setdefault("models", {}).setdefault(alias_key, json.loads(json.dumps(alias_val)))
    return new


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #

_PREFLIGHT_SNIPPET = """
import json, os, urllib.request
MK = os.environ["LITELLM_MASTER_KEY"]
B = "http://127.0.0.1:4000"
KEYS = json.loads({keys!r})
TARGET = {target!r}

def get(path):
    req = urllib.request.Request(B + path, headers={{"Authorization": "Bearer " + MK}})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=90).read() or b"{{}}")
    except Exception as exc:
        return {{"ERR": repr(exc)[:200]}}

info = get("/model/info")
members = [
    {{
        "id": (d.get("model_info") or {{}}).get("id"),
        "mode": (d.get("model_info") or {{}}).get("mode"),
        "weight": (d.get("litellm_params") or {{}}).get("weight"),
        "api_base": (d.get("litellm_params") or {{}}).get("api_base"),
    }}
    for d in info.get("data", [])
    if d.get("model_name") == TARGET
]
allow = {{}}
for uid, key in KEYS.items():
    ki = get("/key/info?key=" + key)
    allow[uid] = TARGET in ((ki.get("info") or {{}}).get("models") or [])
print(json.dumps({{"members": members, "allow": allow}}))
"""


def preflight(
    instances: dict[str, Instance], target_model: str, kubeconfig: str, namespace: str
) -> bool:
    keys = {uid: i.litellm_key for uid, i in instances.items() if i.litellm_key}
    raw = litellm_query(
        _PREFLIGHT_SNIPPET.format(keys=json.dumps(keys), target=target_model),
        kubeconfig,
        namespace,
    )
    try:
        data = json.loads(raw.strip().split("\n")[-1])
    except (ValueError, IndexError):
        print(f"preflight: could not parse LiteLLM response: {raw[:300]}", file=sys.stderr)
        return False

    members, allow = data["members"], data["allow"]
    ok = True

    print(f"model group {target_model}: {len(members)} deployment(s)")
    for m in members:
        print(f"  id={m['id']} mode={m['mode']} weight={m['weight']} base={m['api_base']}")
    if len(members) < 2:
        # This is exactly what took 9 production instances down on 2026-08-05:
        # a brand-new group with one self-hosted box behind it and no fallback.
        print(
            "  FAIL single deployment and no in-group backup -- a box outage hits users directly",
            file=sys.stderr,
        )
        ok = False
    if any(m["mode"] != "chat" for m in members):
        print("  WARN not every member is mode=chat; mode=responses injects strict=None into tools")

    # The catalog entry must live in the base-config the pod really mounts.
    base_cms = {i.base_config_cm for i in instances.values() if i.base_config_cm}
    for cm_name in sorted(base_cms):
        body = kubectl(["get", "cm", cm_name, "-o", "json"], kubeconfig, namespace)
        present = target_model in body
        print(f"base-config {cm_name}: catalog entry for {target_model} -> {present}")
        if not present:
            print("  FAIL missing catalog entry; her would send vendor-spec maxTokens", file=sys.stderr)
            ok = False

    for uid in sorted(instances, key=int):
        inst = instances[uid]
        allowed = allow.get(uid)
        flags = []
        if not inst.litellm_key:
            flags.append("NO-KEY")
        if allowed is False:
            flags.append("NOT-IN-ALLOWLIST")
        if not inst.base_config_cm:
            flags.append("NO-BASE-CONFIG-MOUNT")
        status = "ok" if not flags else "FAIL " + ",".join(flags)
        print(f"[{uid:>4}] base={inst.base_config_cm or '?':24s} allowlisted={allowed} {status}")
        if flags:
            ok = False
    return ok


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #


def cmd_preflight(args: argparse.Namespace) -> int:
    uids = parse_uids(args.uids)
    instances = load_instances(uids, args.kubeconfig, args.namespace)
    ref = load_instances([args.reference], args.kubeconfig, args.namespace)[args.reference]
    target = ref.primary
    if not target:
        print(f"reference carher-{args.reference} has no primary model", file=sys.stderr)
        return 2
    print(f"reference carher-{args.reference} primary = {target}\n")
    ok = preflight(instances, target.split("/", 1)[-1], args.kubeconfig, args.namespace)
    print("\nPREFLIGHT " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def cmd_switch(args: argparse.Namespace) -> int:
    uids = parse_uids(args.uids)
    ref = load_instances([args.reference], args.kubeconfig, args.namespace)[args.reference]
    ref_defaults = (ref.config.get("agents") or {}).get("defaults") or {}
    ref_model = ref_defaults.get("model") or {}
    target = ref_model.get("primary")
    if not target:
        print(f"reference carher-{args.reference} has no primary model", file=sys.stderr)
        return 2

    instances = load_instances(uids, args.kubeconfig, args.namespace)
    stamp = args.stamp or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(args.backup_dir, exist_ok=True)
    print(f"reference carher-{args.reference} model block = {json.dumps(ref_model, ensure_ascii=False)}")
    print(f"backup dir = {args.backup_dir}  stamp = {stamp}\n")

    if not args.skip_preflight:
        if not preflight(instances, target.split("/", 1)[-1], args.kubeconfig, args.namespace):
            print("\nPREFLIGHT FAILED -- refusing to switch (override with --skip-preflight)", file=sys.stderr)
            return 1
        print()

    planned: list[Instance] = []
    for uid in sorted(instances, key=int):
        inst = instances[uid]
        new = build_new_config(
            inst.config, ref_model, ref_defaults.get("models") or {}, target.split("/", 1)[-1]
        )
        d = diff_leaves(inst.config, new)
        print(f"[{uid:>4}] changed={len(d.changed)} removed={len(d.removed)}")
        if d.changed or d.removed:
            print(d.render())

        if d.removed:
            print(f"[{uid}] GUARD FAIL: patch would remove keys", file=sys.stderr)
            return 1
        stray = [p for p, _, _ in d.changed if not p.startswith(MODEL_PATH_PREFIX)]
        if stray:
            print(f"[{uid}] GUARD FAIL: changes outside {MODEL_PATH_PREFIX}: {stray}", file=sys.stderr)
            return 1
        if args.expect_old and inst.primary != args.expect_old:
            print(
                f"[{uid}] GUARD FAIL: primary is {inst.primary!r}, expected {args.expect_old!r}",
                file=sys.stderr,
            )
            return 1

        # Timestamped filenames: a fixed /tmp path will happily run a stale file
        # left behind by an earlier, unrelated session.
        write_json(os.path.join(args.backup_dir, f"backup-{uid}-{stamp}.json"), inst.config)
        write_json(os.path.join(args.backup_dir, f"new-{uid}-{stamp}.json"), new)
        planned.append(inst)

    print(f"\nplanned={len(planned)}/{len(uids)}  apply={args.apply}")
    if not args.apply:
        print("DRY RUN -- nothing written to the cluster")
        return 0
    for inst in planned:
        apply_cm(inst.cm_name, os.path.join(args.backup_dir, f"new-{inst.uid}-{stamp}.json"),
                 args.kubeconfig, args.namespace)
        print(f"  applied {inst.cm_name}")
    print(f"\nRollback: {sys.argv[0]} rollback --uids {args.uids} --stamp {stamp} "
          f"--backup-dir {args.backup_dir} --expect-old '{target}' --apply")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    uids = parse_uids(args.uids)
    ref = load_instances([args.reference], args.kubeconfig, args.namespace)[args.reference]
    target = ref.primary
    instances = load_instances(uids, args.kubeconfig, args.namespace)
    ok = 0
    for uid in sorted(instances, key=int):
        inst = instances[uid]
        if not inst.pod:
            print(f"[{uid:>4}] no pod")
            continue
        # The ConfigMap is not the running state: read the merged file in the pod.
        out = kubectl(
            [
                "exec", inst.pod, "-c", "carher", "--", "python3", "-c",
                "import json;d=json.load(open('/data/.openclaw/openclaw.json'));"
                "print((d.get('agents',{}).get('defaults',{}).get('model') or {}).get('primary'))",
            ],
            args.kubeconfig,
            args.namespace,
            check=False,
        ).strip()
        good = out == target
        ok += good
        print(
            f"[{uid:>4}] pod={inst.pod:34s} restarts={inst.restarts:>3s} "
            f"started={inst.age} primary={out or '<empty>'} {'OK' if good else 'NOT-YET'}"
        )
    print(f"\n{ok}/{len(instances)} effective in pod (target {target})")
    return 0 if ok == len(instances) else 1


def cmd_rollback(args: argparse.Namespace) -> int:
    uids = parse_uids(args.uids)
    instances = load_instances(uids, args.kubeconfig, args.namespace)
    restored = 0
    for uid in sorted(instances, key=int):
        path = os.path.join(args.backup_dir, f"backup-{uid}-{args.stamp}.json")
        if not os.path.exists(path):
            print(f"[{uid:>4}] backup missing: {path} -- skipped", file=sys.stderr)
            continue
        with open(path, encoding="utf-8") as fh:
            backup = json.load(fh)
        old_primary = (
            ((backup.get("agents") or {}).get("defaults") or {}).get("model") or {}
        ).get("primary")
        # Guard so a stale or foreign backup cannot be restored onto a live instance.
        if args.expect_old and old_primary != args.expect_old:
            print(
                f"[{uid:>4}] GUARD FAIL: backup primary {old_primary!r} != {args.expect_old!r} -- skipped",
                file=sys.stderr,
            )
            continue
        print(f"[{uid:>4}] guard ok  live={instances[uid].primary!r} -> restore {old_primary!r}")
        if args.apply:
            apply_cm(instances[uid].cm_name, path, args.kubeconfig, args.namespace)
            restored += 1
    print(f"\n{'restored ' + str(restored) if args.apply else 'DRY RUN -- pass --apply to write'}")
    return 0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def parse_uids(raw: str) -> list[str]:
    uids = [u.strip() for u in raw.split(",") if u.strip()]
    if not uids:
        raise SystemExit("--uids is required and must be non-empty")
    return uids


def write_json(path: str, obj: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)


def apply_cm(cm_name: str, json_path: str, kubeconfig: str | None, namespace: str) -> None:
    manifest = kubectl(
        ["create", "cm", cm_name, f"--from-file=openclaw.json={json_path}",
         "--dry-run=client", "-o", "yaml"],
        kubeconfig,
        namespace,
    )
    kubectl(["apply", "-f", "-"], kubeconfig, namespace, input_text=manifest)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--namespace", default="carher")
    parser.add_argument("--kubeconfig")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--uids", required=True, help="comma-separated Her userIds")
        p.add_argument("--reference", default="1000", help="instance to copy the model block from")

    p = sub.add_parser("preflight", help="check keys, catalog and group redundancy")
    common(p)
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("switch", help="copy the reference model block onto the targets")
    common(p)
    p.add_argument("--apply", action="store_true", help="write; default is a dry run")
    p.add_argument("--backup-dir", default="/tmp/carher-model-switch")
    p.add_argument("--stamp", help="backup stamp; defaults to the current UTC time")
    p.add_argument("--expect-old", help="refuse to switch unless the current primary matches")
    p.add_argument("--skip-preflight", action="store_true", help="not recommended")
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("verify", help="read the merged config back out of each pod")
    common(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("rollback", help="restore timestamped backups")
    common(p)
    p.add_argument("--apply", action="store_true", help="write; default is a dry run")
    p.add_argument("--backup-dir", default="/tmp/carher-model-switch")
    p.add_argument("--stamp", required=True)
    p.add_argument("--expect-old", help="required primary inside the backup")
    p.set_defaults(func=cmd_rollback)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
