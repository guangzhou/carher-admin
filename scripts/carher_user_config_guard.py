#!/usr/bin/env python3
"""Audit and repair CarHer per-user OpenClaw ConfigMaps.

This guards against shell-generated patches that accidentally turn "$include"
into an empty-string key, which can make OpenClaw gateway startup reject configs
because gateway.mode is not visible at the root.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


USER_CM_RE = re.compile(r"^carher-(\d+)-user-config$")
DEFAULT_GATEWAY = {
    "mode": "local",
    "bind": "lan",
    "auth": {"mode": "token", "token": "${CARHER_GATEWAY_TOKEN}"},
    "controlUi": {
        "allowInsecureAuth": True,
        "dangerouslyAllowHostHeaderOriginFallback": True,
        "dangerouslyDisableDeviceAuth": True,
    },
}


@dataclass
class Finding:
    uid: int
    name: str
    include: str | None
    has_empty_key: bool
    has_gateway_mode: bool
    primary: str | None
    bot_open_id: bool

    @property
    def needs_fix(self) -> bool:
        return self.has_empty_key or self.include != "./carher-config.json"


def run(args: list[str], input_text: str | None = None) -> str:
    return subprocess.check_output(args, input=input_text, text=True)


def kubectl(args: list[str], kubeconfig: str | None, namespace: str) -> str:
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += ["-n", namespace, *args]
    return run(cmd)


def load_configmaps(kubeconfig: str | None, namespace: str) -> list[dict[str, Any]]:
    raw = kubectl(["get", "cm", "-o", "json"], kubeconfig, namespace)
    return json.loads(raw).get("items", [])


def openclaw_json(cm: dict[str, Any]) -> dict[str, Any] | None:
    raw = (cm.get("data") or {}).get("openclaw.json")
    if not raw:
        return None
    return json.loads(raw)


def finding_for(cm: dict[str, Any]) -> Finding | None:
    name = cm.get("metadata", {}).get("name", "")
    match = USER_CM_RE.match(name)
    if not match:
        return None
    cfg = openclaw_json(cm)
    if cfg is None:
        return None
    model = (((cfg.get("agents") or {}).get("defaults") or {}).get("model") or {})
    feishu = ((cfg.get("channels") or {}).get("feishu") or {})
    gateway = cfg.get("gateway") or {}
    return Finding(
        uid=int(match.group(1)),
        name=name,
        include=cfg.get("$include"),
        has_empty_key="" in cfg,
        has_gateway_mode=bool(gateway.get("mode")),
        primary=model.get("primary"),
        bot_open_id=bool(feishu.get("botOpenId")),
    )


def patch_config(cfg: dict[str, Any], *, ensure_gateway_mode: bool) -> dict[str, Any]:
    fixed = dict(cfg)
    fixed.pop("", None)
    fixed["$include"] = "./carher-config.json"
    if ensure_gateway_mode:
        gateway = dict(DEFAULT_GATEWAY)
        gateway.update(fixed.get("gateway") or {})
        gateway["mode"] = gateway.get("mode") or "local"
        fixed["gateway"] = gateway
    return fixed


def patch_cm(name: str, fixed_cfg: dict[str, Any], kubeconfig: str | None, namespace: str) -> None:
    patch = {"data": {"openclaw.json": json.dumps(fixed_cfg, ensure_ascii=False, indent=2)}}
    cmd = ["patch", "cm", name, "--type", "merge", "-p", json.dumps(patch, ensure_ascii=False)]
    kubectl(cmd, kubeconfig, namespace)


def audit(args: argparse.Namespace) -> int:
    rows: list[tuple[Finding, dict[str, Any]]] = []
    for cm in load_configmaps(args.kubeconfig, args.namespace):
        finding = finding_for(cm)
        if finding:
            rows.append((finding, cm))

    requested_uids = {int(uid) for uid in args.uids.split(",") if uid.strip()} if args.uids else set()
    bad = [finding for finding, _ in rows if finding.needs_fix]
    gateway_targets = [
        finding
        for finding, _ in rows
        if args.ensure_gateway_mode
        and not finding.has_gateway_mode
        and (not requested_uids or finding.uid in requested_uids)
    ]
    print(f"user_config_total {len(rows)}")
    print(f"bad_count {len(bad)}")
    print(f"gateway_mode_targets {len(gateway_targets)}")
    for item in sorted(bad, key=lambda x: x.uid):
        print(
            "\t".join(
                [
                    str(item.uid),
                    item.name,
                    f"empty={item.has_empty_key}",
                    f"include={item.include}",
                    f"gateway_mode={item.has_gateway_mode}",
                    f"primary={item.primary}",
                    f"botOpenId={item.bot_open_id}",
                ]
            )
        )
    if args.apply:
        if args.ensure_gateway_mode and not requested_uids:
            raise SystemExit("--ensure-gateway-mode requires --uids to avoid touching the whole fleet")
        for finding, cm in sorted(rows, key=lambda x: x[0].uid):
            should_patch = finding.needs_fix or (
                args.ensure_gateway_mode and finding.uid in requested_uids and not finding.has_gateway_mode
            )
            if not should_patch:
                continue
            cfg = openclaw_json(cm)
            if cfg is None:
                continue
            print(f"patching {finding.name}", file=sys.stderr)
            patch_cm(
                finding.name,
                patch_config(cfg, ensure_gateway_mode=args.ensure_gateway_mode or finding.needs_fix),
                args.kubeconfig,
                args.namespace,
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="carher")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--apply", action="store_true", help="apply repairs; default is audit only")
    parser.add_argument("--uids", help="comma-separated Her ids for targeted gateway.mode repair")
    parser.add_argument(
        "--ensure-gateway-mode",
        action="store_true",
        help="also add root gateway.mode for --uids; useful for old H75 images",
    )
    args = parser.parse_args()
    return audit(args)


if __name__ == "__main__":
    raise SystemExit(main())
