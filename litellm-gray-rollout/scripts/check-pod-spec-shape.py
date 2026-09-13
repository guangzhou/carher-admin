#!/usr/bin/env python3
"""Compare a live workload's Pod spec against the target render, mount by mount.

`check-release-deletion-set.py` is an **object-level** gate: it diffs the set of
Kubernetes objects and fails closed on any that disappear.  Production's entire
patch mechanism does not live in an object -- it lives *inside* the Pod spec, as
40 volumeMounts, 38 of them `subPath` single-file overlays, 4 of which overwrite
files under `site-packages/` (measured 2026-09-13 on litellm-product/litellm-proxy).

The failure shape this tool exists for: none of those ConfigMaps is deleted, so
the deletion-set gate reads green; the Deployment converges, so `kubectl rollout
status` reads green; the container starts and /health returns 200, so the probes
read green.  The only thing that changed is that every runtime patch stopped
being mounted -- and all three rulers are blind to it.

This tool never talks to the cluster and never mutates anything.  It reads two
already captured manifests and fails closed unless every removed or altered
shape item is named in an approval file together with the consumer that loses it.

Environment variable **values are never read** -- only names.  A credential
cannot leak into the diff, the evidence file, or the approval file.

ConfigMap *contents* are read only to digest them, and only when the operator
supplies the captures (`--live-configmaps` / `--target-configmaps`).  This
matters because the chart content-addresses its ConfigMaps
(`<release>-<snapshot>-<checksum>`) while production's are named by hand, so
every single mount would otherwise read as `changed` purely because the
ConfigMap was renamed -- 40 approvals that all say "yes, same bytes", which is a
rubber stamp, not a gate.  With the captures, a difference is marked inert only
when the mounted bytes are provably identical; without them nothing is inert and
the tool behaves exactly as it did before.  Secrets are never digested: a digest
of a low-entropy secret is a crackable artefact, so Secret-backed mounts are
always compared by name.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


TOOL = "check-pod-spec-shape"
SCHEMA_VERSION = 2
STATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
UNNAMED_CONSUMER = {"unknown", "none", "n/a", "na", "-", "tbd", ""}
# Volume source kinds worth naming in the diff. Everything else collapses to its
# own key name, which is enough to notice that the source changed shape.
VOLUME_SOURCE_KEYS = (
    "configMap",
    "secret",
    "persistentVolumeClaim",
    "emptyDir",
    "hostPath",
    "projected",
    "downwardAPI",
    "csi",
    "nfs",
)


def emit(payload: dict[str, Any], code: int) -> int:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return code


def digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def load_workload(
    path: Path, kind: str, name: str, namespace: str
) -> tuple[dict[str, Any] | None, list[str], bool]:
    """Return (pod spec, errors, looks_live).

    `looks_live` is true when the document carries the fields only the apiserver
    writes.  A rendered manifest has none of them.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, [f"UNREADABLE:{path}"], False
    try:
        documents = list(yaml.safe_load_all(raw))
    except yaml.YAMLError:
        return None, [f"INVALID_YAML:{path}"], False

    matches = [
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == kind
        and isinstance(document.get("metadata"), dict)
        and document["metadata"].get("name") == name
        and (document["metadata"].get("namespace") or namespace) == namespace
    ]
    if len(matches) != 1:
        return None, [f"EXPECTED_ONE_{kind.upper()}_{name}:{len(matches)}"], False

    workload = matches[0]
    metadata = workload["metadata"]
    annotations = metadata.get("annotations") or {}
    looks_live = bool(
        workload.get("status")
        or metadata.get("uid")
        or metadata.get("resourceVersion")
        or annotations.get("deployment.kubernetes.io/revision")
    )
    spec = workload.get("spec")
    if not isinstance(spec, dict):
        return None, [f"MISSING_SPEC:{kind}/{name}"], looks_live
    template = spec.get("template")
    if not isinstance(template, dict) or not isinstance(template.get("spec"), dict):
        return None, [f"MISSING_POD_TEMPLATE:{kind}/{name}"], looks_live
    return template["spec"], [], looks_live


def load_configmaps(
    path: Path | None, namespace: str, *, expect_live: bool, explicit: bool = True
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Return ({ConfigMap name: data}, errors) from a captured manifest stream.

    The same trap as the workload sides applies here, in a nastier form: feeding
    the *rendered* ConfigMaps as the live capture would fabricate content matches
    and turn a genuine content swap into a proven-inert difference.  So each side
    is checked for the fields only the apiserver writes, and a mismatch is an
    error rather than a silently trusted digest.
    """
    if path is None:
        return {}, []
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}, [f"UNREADABLE:{path}"]
    try:
        documents = list(yaml.safe_load_all(raw))
    except yaml.YAMLError:
        return {}, [f"INVALID_YAML:{path}"]

    found: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    # `kubectl get configmap -o yaml` -- the capture command this flag is
    # documented with -- emits a single `kind: List` wrapper, not a stream of
    # ConfigMap documents.  Without this unwrap the loader walked straight past
    # every object and reported zero, with no error: the content-digest feature
    # would have been dead on arrival in the window and the operator would have
    # been handed 48 "yes, same bytes" approvals to sign, which is the exact
    # rubber stamp it exists to prevent.  Measured 2026-09-13 against a real
    # capture of litellm-product.
    for document in list(documents):
        if isinstance(document, dict) and isinstance(document.get("items"), list):
            documents.extend(
                item for item in document["items"] if isinstance(item, dict)
            )
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "ConfigMap":
            continue
        metadata = document.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
            continue
        if (metadata.get("namespace") or namespace) != namespace:
            continue
        looks_live = bool(metadata.get("uid") or metadata.get("resourceVersion"))
        if looks_live is not expect_live:
            errors.append(
                "LIVE_CONFIGMAPS_ARE_NOT_LIVE_OBJECTS"
                if expect_live
                else "TARGET_CONFIGMAPS_ARE_NOT_A_RENDER"
            )
            continue
        data = document.get("data")
        binary = document.get("binaryData")
        merged: dict[str, str] = {}
        if isinstance(data, dict):
            merged.update({k: v for k, v in data.items() if isinstance(v, str)})
        if isinstance(binary, dict):
            merged.update(
                {f"{k}#binary": v for k, v in binary.items() if isinstance(v, str)}
            )
        found[metadata["name"]] = merged
    if not found and not errors and explicit:
        # A capture that parses to nothing is a broken capture, not a cluster
        # with no ConfigMaps.  Staying quiet here only costs extra approvals, so
        # it would survive review for a long time while the content gate did
        # nothing at all.
        #
        # Only for a capture the operator actually named.  The target side falls
        # back to the target manifest itself, and a render that mounts nothing
        # from a ConfigMap legitimately contains none -- nothing can be inert
        # there anyway, so reporting zero is honest rather than broken.
        errors.append(
            "LIVE_CONFIGMAPS_CAPTURE_IS_EMPTY"
            if expect_live
            else "TARGET_CONFIGMAPS_CAPTURE_IS_EMPTY"
        )
    return found, errors


def volume_source(volume: dict[str, Any]) -> str:
    """A stable, credential-free description of where a volume's content comes from."""
    for key in VOLUME_SOURCE_KEYS:
        source = volume.get(key)
        if isinstance(source, dict):
            source_name = source.get("name") or source.get("secretName") or source.get("claimName")
            if isinstance(source_name, str):
                return f"{key}/{source_name}"
            return key
        if source is not None:
            return key
    unknown = sorted(key for key in volume if key != "name")
    return "unknown:" + ",".join(unknown) if unknown else "unknown"


def shape_of(
    pod_spec: dict[str, Any], configmaps: dict[str, dict[str, str]] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Flatten the Pod spec into ({item key: item value}, {item key: content digest}).

    The second map is populated only for ConfigMap-backed items whose ConfigMap
    was actually captured, and it is advisory: it can prove two differing items
    carry the same bytes, it can never prove they differ.
    """
    shape: dict[str, str] = {}
    content: dict[str, str] = {}
    captured = configmaps or {}

    volumes = pod_spec.get("volumes")
    volume_sources: dict[str, str] = {}
    volume_configmaps: dict[str, str] = {}
    if isinstance(volumes, list):
        for volume in volumes:
            if not isinstance(volume, dict) or not isinstance(volume.get("name"), str):
                continue
            source = volume_source(volume)
            volume_sources[volume["name"]] = source
            shape[f"volume/{volume['name']}"] = source
            config_map = volume.get("configMap")
            if isinstance(config_map, dict) and isinstance(config_map.get("name"), str):
                volume_configmaps[volume["name"]] = config_map["name"]
                if config_map["name"] in captured:
                    content[f"volume/{volume['name']}"] = digest(
                        captured[config_map["name"]]
                    )

    container_groups = (
        ("container", pod_spec.get("containers")),
        ("initContainer", pod_spec.get("initContainers")),
    )
    for role, containers in container_groups:
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, dict) or not isinstance(container.get("name"), str):
                continue
            name = container["name"]
            prefix = f"{role}/{name}"

            for field in ("command", "args"):
                if field in container:
                    shape[f"{prefix}/{field}"] = json.dumps(
                        container[field], ensure_ascii=True, sort_keys=True, separators=(",", ":")
                    )

            mounts = container.get("volumeMounts")
            if isinstance(mounts, list):
                for mount in mounts:
                    if not isinstance(mount, dict) or not isinstance(mount.get("mountPath"), str):
                        continue
                    sub_path = mount.get("subPath") or mount.get("subPathExpr") or ""
                    key = f"{prefix}/mount/{mount['mountPath']}/{sub_path}"
                    # The source is part of the value, not the key: the same
                    # mountPath backed by a different ConfigMap is a silent
                    # content swap, which must surface as a change, not as a
                    # matched pair.
                    source = volume_sources.get(mount.get("name", ""), "unbound")
                    read_only = "ro" if mount.get("readOnly") else "rw"
                    shape[key] = f"{source}|{read_only}"
                    # A subPath mount exposes exactly one key, so digest that key
                    # and not the whole ConfigMap: two ConfigMaps that differ in
                    # an unmounted key are identical as far as this mount is
                    # concerned, and a changed mounted key must not hide behind a
                    # whole-object digest that also moved for other reasons.
                    config_map = volume_configmaps.get(mount.get("name", ""))
                    data = captured.get(config_map) if config_map else None
                    if data is not None:
                        if not sub_path:
                            content[key] = digest(data)
                        elif sub_path in data:
                            content[key] = digest(data[sub_path])

            lifecycle = container.get("lifecycle")
            if isinstance(lifecycle, dict):
                for hook in ("postStart", "preStop"):
                    if hook in lifecycle:
                        shape[f"{prefix}/lifecycle/{hook}"] = json.dumps(
                            lifecycle[hook],
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )

            # Names only. A value never enters this tool's output, so a diff can
            # be pasted into a review or an approval file without redaction.
            env = container.get("env")
            if isinstance(env, list):
                for item in env:
                    if isinstance(item, dict) and isinstance(item.get("name"), str):
                        shape[f"{prefix}/env/{item['name']}"] = "set"
            env_from = container.get("envFrom")
            if isinstance(env_from, list):
                for item in env_from:
                    if not isinstance(item, dict):
                        continue
                    for key in ("secretRef", "configMapRef"):
                        source = item.get(key)
                        if isinstance(source, dict) and isinstance(source.get("name"), str):
                            shape[f"{prefix}/envFrom/{key}/{source['name']}"] = "set"

    return shape, content


def load_approval(path: Path | None) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Approval maps a shape item key to {consumer, disposition, approver}."""
    if path is None:
        return {}, []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, [f"APPROVAL_UNREADABLE:{path}"]
    if not isinstance(parsed, dict) or not isinstance(parsed.get("shape_changes"), list):
        return {}, ["APPROVAL_INVALID"]

    approved: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for entry in parsed["shape_changes"]:
        if not isinstance(entry, dict):
            errors.append("APPROVAL_ENTRY_INVALID")
            continue
        if set(entry) != {"item", "consumer", "disposition", "approver"}:
            errors.append("APPROVAL_ENTRY_INVALID")
            continue
        if not all(isinstance(value, str) and value.strip() for value in entry.values()):
            errors.append("APPROVAL_ENTRY_INVALID")
            continue
        # An unnamed consumer is exactly the empty data column the diagnosis
        # discipline forbids: nobody analysed who loses this mount.
        if entry["consumer"].strip().lower() in UNNAMED_CONSUMER:
            errors.append(f"APPROVAL_CONSUMER_UNNAMED:{entry['item']}")
            continue
        if entry["item"] in approved:
            errors.append(f"APPROVAL_DUPLICATE:{entry['item']}")
            continue
        approved[entry["item"]] = {
            "consumer": entry["consumer"].strip(),
            "disposition": entry["disposition"].strip(),
            "approver": entry["approver"].strip(),
        }
    return approved, errors


def secure_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    errors: list[str] = []
    if not STATE_ID_RE.fullmatch(args.run_id):
        errors.append("RUN_ID_INVALID")
    if not STATE_ID_RE.fullmatch(args.generation):
        errors.append("GENERATION_INVALID")

    live_spec, live_errors, live_looks_live = load_workload(
        args.live, args.kind, args.name, args.namespace
    )
    target_spec, target_errors, target_looks_live = load_workload(
        args.target, args.kind, args.name, args.namespace
    )
    approved, approval_errors = load_approval(args.approval)
    live_configmaps, live_cm_errors = load_configmaps(
        args.live_configmaps, args.namespace, expect_live=True
    )
    target_configmaps, target_cm_errors = load_configmaps(
        args.target_configmaps or args.target,
        args.namespace,
        expect_live=False,
        explicit=args.target_configmaps is not None,
    )
    errors.extend(
        live_errors + target_errors + approval_errors + live_cm_errors + target_cm_errors
    )

    # Ruler integrity. `helm get manifest` reports what Helm believes it applied,
    # not what is running; litellm-proxy is changed with `set image`/`patch` and
    # never `apply`, so the two can differ. Feeding a render as the live side
    # would compare the target against itself and read green by construction.
    if live_spec is not None and not live_looks_live:
        errors.append("LIVE_SIDE_IS_NOT_A_LIVE_OBJECT")
    if target_spec is not None and target_looks_live:
        errors.append("TARGET_SIDE_IS_NOT_A_RENDER")

    live_shape, live_content = (
        shape_of(live_spec, live_configmaps) if live_spec is not None else ({}, {})
    )
    target_shape, target_content = (
        shape_of(target_spec, target_configmaps) if target_spec is not None else ({}, {})
    )

    removed = sorted(set(live_shape) - set(target_shape))
    added = sorted(set(target_shape) - set(live_shape))
    changed = sorted(
        key
        for key in set(live_shape) & set(target_shape)
        if live_shape[key] != target_shape[key]
    )

    # The chart content-addresses its ConfigMaps, production names them by hand.
    # Without this step every mount reads as `changed` because the ConfigMap was
    # renamed, and the approval file degenerates into 40 lines of "same bytes".
    # A difference is inert only when the mounted bytes are *proven* identical;
    # an unresolvable digest is never inert, so the default stays fail-closed.
    inert: set[str] = set()
    for key in changed:
        live_digest = live_content.get(key)
        if not live_digest or live_digest != target_content.get(key):
            continue
        if key.startswith("volume/"):
            inert.add(key)
        elif "/mount/" in key:
            # Content equality says nothing about ro/rw, which is the rest of the
            # value; a patch mount that turned writable is a real change.
            if live_shape[key].split("|")[1:] == target_shape[key].split("|")[1:]:
                inert.add(key)

    # A renamed volume shows up as one removal plus one addition. It is inert
    # only when the pairing is unambiguous: two candidates with the same digest
    # mean we cannot say which became which, so neither is excused. Mounts are
    # deliberately excluded -- their key *is* the path, so a moved mount is a
    # moved file, never a rename.
    renamed_volumes: list[dict[str, str]] = []
    live_by_digest: dict[str, list[str]] = {}
    for key in removed:
        if key.startswith("volume/") and live_content.get(key):
            live_by_digest.setdefault(live_content[key], []).append(key)
    target_by_digest: dict[str, list[str]] = {}
    for key in added:
        if key.startswith("volume/") and target_content.get(key):
            target_by_digest.setdefault(target_content[key], []).append(key)
    for content_digest, live_keys in sorted(live_by_digest.items()):
        target_keys = target_by_digest.get(content_digest, [])
        if len(live_keys) == 1 and len(target_keys) == 1:
            inert.add(live_keys[0])
            renamed_volumes.append(
                {"live": live_keys[0], "target": target_keys[0], "content": content_digest}
            )

    # No digest can excuse a patch that stopped being mounted at all, so a
    # removed mount must never reach `inert`. Today nothing can put it there --
    # only the rename pairing adds removed keys, and that is volumes-only -- so
    # this is an invariant, not a filter: if a later change ever breaks it the
    # run goes red instead of quietly excusing the exact failure shape this tool
    # was written for.
    excused_mount_removals = sorted(
        key for key in inert if key in set(removed) and "/mount/" in key
    )
    if excused_mount_removals:
        inert -= set(excused_mount_removals)
        errors.append("INTERNAL_MOUNT_REMOVAL_MARKED_INERT")

    # A removed mount stops a patch from being applied; a changed one silently
    # swaps its content. Both are invisible to every other gate, so both need an
    # approval naming the consumer.
    needs_approval = sorted((set(removed) | set(changed)) - inert)
    unapproved = [key for key in needs_approval if key not in approved]
    stale_approvals = sorted(set(approved) - set(needs_approval))
    if unapproved:
        errors.append("UNAPPROVED_SHAPE_CHANGES")
    if stale_approvals:
        errors.append("APPROVAL_DOES_NOT_MATCH_DIFF")
    if live_spec is not None and not any(key.startswith("volume/") for key in live_shape):
        # Zero volumes on the live side means the capture is wrong, not that
        # production stopped mounting anything.
        errors.append("LIVE_SIDE_HAS_NO_VOLUMES")

    mount_removals = [key for key in removed if "/mount/" in key]
    status = "PASS" if not errors else "FAIL"
    result: dict[str, Any] = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "run_id": args.run_id,
        "generation": args.generation,
        "namespace": args.namespace,
        "workload": f"{args.kind}/{args.name}",
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "live_shape_sha256": digest(live_shape),
        "target_shape_sha256": digest(target_shape),
        "counts": {
            "live": len(live_shape),
            "target": len(target_shape),
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "mount_removals": len(mount_removals),
            "inert_by_content": len(inert),
            "renamed_volumes": len(renamed_volumes),
            "live_configmaps": len(live_configmaps),
            "target_configmaps": len(target_configmaps),
        },
        "added": added,
        "removed": [
            {
                "item": key,
                "live": live_shape[key],
                "inert": key in inert,
                **approved.get(key, {"consumer": "", "disposition": "", "approver": ""}),
            }
            for key in removed
        ],
        "changed": [
            {
                "item": key,
                "live": live_shape[key],
                "target": target_shape[key],
                "inert": key in inert,
                **approved.get(key, {"consumer": "", "disposition": "", "approver": ""}),
            }
            for key in changed
        ],
        "mount_removals": mount_removals,
        "inert_by_content": sorted(inert),
        "renamed_volumes": renamed_volumes,
        "unapproved_shape_changes": unapproved,
        "stale_approvals": stale_approvals,
        "errors": sorted(set(errors)),
    }
    result["result_sha256"] = digest({k: v for k, v in result.items() if k != "result_sha256"})
    return result, 0 if status == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        type=Path,
        required=True,
        help=(
            "`kubectl -n <ns> get <kind> <name> -o yaml` captured before the upgrade. "
            "NOT `helm get manifest`: that reports what Helm believes it applied, and "
            "litellm-proxy is changed with set image/patch, never apply"
        ),
    )
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="output of `helm template <release> <frozen chart> --values <frozen values>`",
    )
    parser.add_argument(
        "--live-configmaps",
        type=Path,
        help=(
            "optional `kubectl -n <ns> get configmap -o yaml` captured alongside --live. "
            "Only used to digest mounted content so a content-addressed rename is not "
            "mistaken for a content swap. Contents never reach the output -- only their "
            "sha256 -- and Secrets are never digested at all; keep the capture 0600 and "
            "delete it after the run. Omit it and nothing is inert"
        ),
    )
    parser.add_argument(
        "--target-configmaps",
        type=Path,
        help="defaults to --target, which already contains the rendered ConfigMaps",
    )
    parser.add_argument("--kind", default="Deployment")
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument(
        "--approval",
        type=Path,
        help='JSON: {"shape_changes":[{"item":..,"consumer":..,"disposition":..,"approver":..}]}',
    )
    parser.add_argument("--output", type=Path, help="write the 0600 evidence file here")
    args = parser.parse_args(argv)

    result, code = run(args)
    if args.output is not None:
        secure_write(args.output, json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return emit(result, code)


if __name__ == "__main__":
    sys.exit(main())
