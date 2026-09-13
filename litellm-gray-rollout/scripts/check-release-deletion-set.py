#!/usr/bin/env python3
"""Compare a live Helm release manifest against the target render, object by object.

`helm upgrade --reset-values` onto a different chart deletes every object the
old release owned and the new chart does not render.  A rollout status check
cannot see that: the Deployment converges while a PodDisruptionBudget, an
extra Service, or a ConfigMap silently disappears.

This tool never talks to the cluster and never mutates anything.  It reads two
already captured manifests, produces the added/kept/deleted object sets, and
fails closed unless every deletion is named in an approval file together with
the consumer that will lose it.
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


TOOL = "check-release-deletion-set"
SCHEMA_VERSION = 1
STATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
# Kinds whose disappearance is never merely cosmetic: losing them changes
# availability, reachability, or what the Pod actually loads.
CRITICAL_KINDS = {
    "ConfigMap",
    "Deployment",
    "HorizontalPodAutoscaler",
    "NetworkPolicy",
    "PersistentVolumeClaim",
    "PodDisruptionBudget",
    "Secret",
    "Service",
    "ServiceAccount",
    "StatefulSet",
}


def emit(payload: dict[str, Any], code: int) -> int:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return code


def digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def group_of(api_version: str) -> str:
    return api_version.split("/", 1)[0] if "/" in api_version else ""


def load_objects(path: Path, default_namespace: str) -> tuple[list[str], list[str]]:
    """Return (sorted object keys, errors). Key is group/kind/namespace/name."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return [], [f"UNREADABLE:{path}"]
    try:
        documents = list(yaml.safe_load_all(raw))
    except yaml.YAMLError:
        return [], [f"INVALID_YAML:{path}"]

    keys: list[str] = []
    errors: list[str] = []
    for document in documents:
        if document is None:
            continue
        if not isinstance(document, dict):
            errors.append(f"NOT_AN_OBJECT:{path}")
            continue
        kind = document.get("kind")
        api_version = document.get("apiVersion")
        metadata = document.get("metadata")
        if not isinstance(kind, str) or not isinstance(api_version, str):
            errors.append(f"MISSING_KIND_OR_APIVERSION:{path}")
            continue
        if kind == "List":
            errors.append(f"LIST_KIND_UNSUPPORTED:{path}")
            continue
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
            errors.append(f"MISSING_NAME:{path}:{kind}")
            continue
        namespace = metadata.get("namespace") or default_namespace
        key = f"{group_of(api_version)}/{kind}/{namespace}/{metadata['name']}"
        if key in keys:
            errors.append(f"DUPLICATE_OBJECT:{key}")
            continue
        keys.append(key)
    if not keys and not errors:
        errors.append(f"EMPTY_MANIFEST:{path}")
    return sorted(keys), errors


def load_approval(path: Path | None) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Approval maps an object key to {consumer, disposition, approver}."""
    if path is None:
        return {}, []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, [f"APPROVAL_UNREADABLE:{path}"]
    if not isinstance(parsed, dict) or not isinstance(parsed.get("deletions"), list):
        return {}, ["APPROVAL_INVALID"]

    approved: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for item in parsed["deletions"]:
        if not isinstance(item, dict):
            errors.append("APPROVAL_ENTRY_INVALID")
            continue
        if set(item) != {"object", "consumer", "disposition", "approver"}:
            errors.append("APPROVAL_ENTRY_INVALID")
            continue
        if not all(isinstance(value, str) and value.strip() for value in item.values()):
            errors.append("APPROVAL_ENTRY_INVALID")
            continue
        # "unknown"/"none"/"n/a" is exactly the empty data column the diagnosis
        # discipline forbids: an unnamed consumer is not an analysed consumer.
        if item["consumer"].strip().lower() in {"unknown", "none", "n/a", "na", "-", "tbd"}:
            errors.append(f"APPROVAL_CONSUMER_UNNAMED:{item['object']}")
            continue
        if item["object"] in approved:
            errors.append(f"APPROVAL_DUPLICATE:{item['object']}")
            continue
        approved[item["object"]] = {
            "consumer": item["consumer"].strip(),
            "disposition": item["disposition"].strip(),
            "approver": item["approver"].strip(),
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

    live, live_errors = load_objects(args.live_manifest, args.namespace)
    target, target_errors = load_objects(args.target_manifest, args.namespace)
    approved, approval_errors = load_approval(args.approval)
    errors.extend(live_errors + target_errors + approval_errors)

    live_set, target_set = set(live), set(target)
    deleted = sorted(live_set - target_set)
    added = sorted(target_set - live_set)
    kept = sorted(live_set & target_set)

    unapproved = [key for key in deleted if key not in approved]
    stale_approvals = sorted(set(approved) - set(deleted))
    if unapproved:
        errors.append("UNAPPROVED_DELETIONS")
    if stale_approvals:
        # An approval for an object that is not actually being deleted means the
        # approval was written against a different render than the one measured.
        errors.append("APPROVAL_DOES_NOT_MATCH_DIFF")

    critical_deletions = sorted(key for key in deleted if key.split("/")[1] in CRITICAL_KINDS)
    status = "PASS" if not errors else "FAIL"
    result: dict[str, Any] = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "run_id": args.run_id,
        "generation": args.generation,
        "namespace": args.namespace,
        "release": args.release,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "live_manifest_sha256": digest(live),
        "target_manifest_sha256": digest(target),
        "counts": {
            "live": len(live),
            "target": len(target),
            "kept": len(kept),
            "added": len(added),
            "deleted": len(deleted),
        },
        "added": added,
        "kept": kept,
        "deleted": [
            {"object": key, **approved.get(key, {"consumer": "", "disposition": "", "approver": ""})}
            for key in deleted
        ],
        "critical_deletions": critical_deletions,
        "unapproved_deletions": unapproved,
        "stale_approvals": stale_approvals,
        "errors": sorted(set(errors)),
    }
    result["result_sha256"] = digest({k: v for k, v in result.items() if k != "result_sha256"})
    return result, 0 if status == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live-manifest",
        type=Path,
        required=True,
        help="output of `helm get manifest <release> -n <ns>` captured before the upgrade",
    )
    parser.add_argument(
        "--target-manifest",
        type=Path,
        required=True,
        help="output of `helm template <release> <frozen chart> --values <frozen values>`",
    )
    parser.add_argument("--release", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument(
        "--approval",
        type=Path,
        help='JSON: {"deletions":[{"object":..,"consumer":..,"disposition":..,"approver":..}]}',
    )
    parser.add_argument("--output", type=Path, help="write the 0600 evidence file here")
    args = parser.parse_args(argv)

    result, code = run(args)
    if args.output is not None:
        secure_write(args.output, json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return emit(result, code)


if __name__ == "__main__":
    sys.exit(main())
