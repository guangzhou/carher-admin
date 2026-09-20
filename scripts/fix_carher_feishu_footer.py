#!/usr/bin/env python3
"""Restore streaming Feishu cards and status footers per Her user config.

The script is intentionally instance-scoped: it edits only the selected
``carher-<uid>-user-config`` ConfigMaps and never touches shared config or
resource settings. It is dry-run by default; ``--apply`` enables writes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


NAMESPACE = "carher"
TARGET_FEISHU = {
    "streaming": True,
    "replyMode": {"default": "streaming", "group": "streaming", "direct": "streaming"},
    "footer": {
        "status": False,
        "elapsed": True,
        "tokens": False,
        "cache": False,
        "context": True,
        "model": True,
    },
}


def kubectl(*args: str) -> str:
    cmd = ["kubectl", "-n", NAMESPACE, *args]
    return subprocess.check_output(cmd, text=True)


def get_config(uid: str) -> tuple[dict[str, Any], str]:
    raw = kubectl("get", "cm", f"carher-{uid}-user-config", "-o", "json")
    cm = json.loads(raw)
    config_raw = cm.get("data", {}).get("openclaw.json")
    if not config_raw:
        raise ValueError(f"carher-{uid}: ConfigMap has no data.openclaw.json")
    return json.loads(config_raw), config_raw


def footer_projection(config: dict[str, Any]) -> dict[str, Any]:
    feishu = config.get("channels", {}).get("feishu", {})
    return {key: feishu.get(key) for key in ("streaming", "replyMode", "footer")}


def latest_pod(uid: str) -> dict[str, Any] | None:
    raw = kubectl("get", "pods", "-l", f"user-id={uid}", "--sort-by=.metadata.creationTimestamp", "-o", "json")
    items = json.loads(raw).get("items", [])
    return items[-1] if items else None


def gateway_healthy(pod_name: str) -> bool:
    result = subprocess.run(
        [
            "kubectl", "-n", NAMESPACE, "exec", pod_name, "-c", "carher", "--",
            "curl", "-fsS", "-m", "5", "http://127.0.0.1:18789/healthz",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def wait_for_gateway(uid: str, timeout: int) -> tuple[str, bool]:
    deadline = time.monotonic() + timeout
    pod_name = "missing"
    while time.monotonic() < deadline:
        pod = latest_pod(uid)
        if pod:
            pod_name = pod["metadata"]["name"]
            if gateway_healthy(pod_name):
                return pod_name, True
        time.sleep(10)
    return pod_name, False


def changed_paths(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in TARGET_FEISHU:
        if before.get("channels", {}).get("feishu", {}).get(key) != after["channels"]["feishu"][key]:
            paths.append(f"/channels/feishu/{key}")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uids", required=True, help="comma-separated numeric Her ids")
    parser.add_argument("--apply", action="store_true", help="write ConfigMaps")
    parser.add_argument("--restart", action="store_true", help="roll out changed Deployments")
    parser.add_argument("--verify", action="store_true", help="read back ConfigMaps and pod state")
    parser.add_argument("--health-timeout", type=int, default=600, help="seconds to wait for gateway health")
    parser.add_argument("--backup-dir", default="/tmp/carher-footer-backups")
    args = parser.parse_args()

    uids = [item.strip() for item in args.uids.split(",") if item.strip()]
    if not uids or any(not item.isdigit() for item in uids):
        parser.error("--uids must contain numeric ids")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(args.backup_dir)
    if args.apply:
        backup_dir.mkdir(parents=True, exist_ok=True)

    changed: list[str] = []
    for uid in uids:
        before, original_raw = get_config(uid)
        after = json.loads(json.dumps(before))
        feishu = after.setdefault("channels", {}).setdefault("feishu", {})
        feishu.update(json.loads(json.dumps(TARGET_FEISHU)))
        paths = changed_paths(before, after)
        print(f"[{uid}] changed={len(paths)} paths={','.join(paths) or '-'}")
        if not paths:
            continue
        changed.append(uid)
        if not args.apply:
            continue

        backup = backup_dir / f"carher-{uid}-user-config-{stamp}.openclaw.json"
        backup.write_text(original_raw, encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(after, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            rendered = handle.name
        try:
            manifest = subprocess.check_output(
                [
                    "kubectl", "-n", NAMESPACE, "create", "cm", f"carher-{uid}-user-config",
                    f"--from-file=openclaw.json={rendered}", "--dry-run=client", "-o", "yaml",
                ],
                text=True,
            )
            subprocess.run(["kubectl", "-n", NAMESPACE, "apply", "-f", "-"], input=manifest, text=True, check=True)
        finally:
            Path(rendered).unlink(missing_ok=True)
        print(f"[{uid}] applied backup={backup}")

    if args.restart and changed:
        for uid in changed:
            subprocess.run(["kubectl", "-n", NAMESPACE, "rollout", "restart", f"deployment/carher-{uid}"], check=True)
            subprocess.run(["kubectl", "-n", NAMESPACE, "rollout", "status", f"deployment/carher-{uid}", "--timeout=480s"], check=True)

    if args.verify:
        for uid in uids:
            config, _ = get_config(uid)
            print(f"[{uid}] config={json.dumps(footer_projection(config), ensure_ascii=False, separators=(',', ':'))}")
            pod_name, health = wait_for_gateway(uid, args.health_timeout)
            latest = latest_pod(uid)
            if latest:
                statuses = latest.get("status", {}).get("containerStatuses", [])
                ready = all(item.get("ready") for item in statuses) if statuses else False
                print(
                    f"[{uid}] pod={pod_name} phase={latest.get('status', {}).get('phase')} "
                    f"ready={ready} gateway_health={health}"
                )
            else:
                print(f"[{uid}] pod=missing gateway_health=False")
            if not health:
                raise RuntimeError(f"carher-{uid}: gateway did not become healthy within {args.health_timeout}s")

    if not args.apply:
        print("dry-run only; add --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
