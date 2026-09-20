#!/usr/bin/env python3
"""Repair CarHer Feishu DM access and group footer state in small waves.

The script is read-only by default.  With --apply it backs up each target's
ConfigMap, pairing allowlist, Deployment and Redis group-mode values, then
converges the DM allowlist and ASCII-only group-at mode before a rollout.
Run it in the ACK toolbox/admin pod (or a host with kubectl and /Data mounted).
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

NS = "carher"
OWNER_RE = re.compile(r"^ou_[A-Za-z0-9]+$")


def run(cmd: list[str], *, check: bool = True, input_text: str | None = None, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError((p.stderr or p.stdout or f"exit {p.returncode}").strip()[:500])
    return p


class Kube:
    def __init__(self, namespace: str, kubeconfig: str | None):
        self.base = ["kubectl"] + ([f"--kubeconfig={kubeconfig}"] if kubeconfig else []) + ["-n", namespace]

    def json(self, args: list[str]) -> dict[str, Any]:
        return json.loads(run([*self.base, *args, "-o", "json"]).stdout)

    def exec(self, pod: str, args: list[str], *, check: bool = True) -> str:
        return run([*self.base, "exec", pod, "--", *args], check=check).stdout.strip()


def owners(raw: Any) -> list[str]:
    out: list[str] = []
    for value in re.split(r"[|\s,;]+", str(raw or "").strip()):
        if OWNER_RE.fullmatch(value) and value not in out:
            out.append(value)
    return out


def access_state(cfg: dict[str, Any], expected: list[str]) -> bool:
    f = (cfg.get("channels") or {}).get("feishu") or {}
    return f.get("dmPolicy") == "allowlist" and f.get("allowFrom") == expected and (f.get("dm") or {}).get("allowFrom") == expected


def patch_access(cfg: dict[str, Any], expected: list[str]) -> dict[str, Any]:
    out = copy.deepcopy(cfg)
    f = out.setdefault("channels", {}).setdefault("feishu", {})
    f.setdefault("dm", {})["allowFrom"] = expected
    f["allowFrom"] = expected
    f["dmPolicy"] = "allowlist"
    out.setdefault("commands", {})["ownerAllowFrom"] = expected
    return out


def redis_pod(kube: Kube) -> str | None:
    for selector in ("app=carher-redis", "app.kubernetes.io/name=redis"):
        data = kube.json(["get", "pods", "-l", selector])
        for item in data.get("items", []):
            if item.get("status", {}).get("phase") == "Running":
                return item["metadata"]["name"]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default=NS)
    ap.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    ap.add_argument("--targets", nargs="+", type=int, required=True)
    ap.add_argument("--wave-size", type=int, default=2)
    ap.add_argument("--data-root", type=Path, default=Path("/Data"))
    ap.add_argument("--backup-root", type=Path, default=Path("/Data/_backups/carher-feishu-repair"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    if args.restart and not args.apply:
        ap.error("--restart requires --apply")
    kube = Kube(args.namespace, args.kubeconfig)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.backup_root / run_id
    if args.apply:
        backup.mkdir(parents=True, exist_ok=False)
        os.chmod(backup, 0o700)
    rpod = redis_pod(kube)
    print(f"mode={'apply' if args.apply else 'audit'} targets={args.targets} redis_pod={rpod or 'none'}")
    all_hers = kube.json(["get", "her"])["items"]
    by_uid = {int((h.get("spec") or {}).get("userId")): h for h in all_hers if str((h.get("spec") or {}).get("userId", "")).isdigit()}
    results: list[dict[str, Any]] = []
    for start in range(0, len(args.targets), args.wave_size):
        wave = args.targets[start : start + args.wave_size]
        print(f"wave {wave}")
        for uid in wave:
            row: dict[str, Any] = {"uid": uid, "status": "FAIL"}
            try:
                h = by_uid[uid]; spec = h.get("spec") or {}
                expected = owners(spec.get("owner"))
                if not expected: raise RuntimeError("no_valid_owner")
                cm = kube.json(["get", "cm", f"carher-{uid}-user-config"])
                raw = (cm.get("data") or {}).get("openclaw.json")
                if not raw: raise RuntimeError("missing_openclaw_json")
                cfg = json.loads(raw)
                dep = kube.json(["get", "deploy", f"carher-{uid}"])
                f = (cfg.get("channels") or {}).get("feishu") or {}
                app = str(spec.get("appId") or f.get("appId") or "")
                home = str((h.get("metadata", {}).get("annotations") or {}).get("carher.io/feishu-home-channel") or "")
                row.update(owner_count=len(expected), dm_ok=access_state(cfg, expected), app_id=app, home_channel=home)
                if args.apply:
                    (backup / f"carher-{uid}-config.json").write_text(json.dumps({"her": h, "config": cfg, "deployment": dep}, ensure_ascii=False, indent=2) + "\n")
                    if not access_state(cfg, expected):
                        cm["data"]["openclaw.json"] = json.dumps(patch_access(cfg, expected), ensure_ascii=False, indent=2) + "\n"
                        run([*kube.base, "replace", "-f", "-"], input_text=json.dumps(cm, ensure_ascii=False))
                    pvc = kube.json(["get", "pvc", f"carher-{uid}-data"])
                    volume = (pvc.get("spec") or {}).get("volumeName")
                    if volume:
                        path = args.data_root / volume / "credentials" / "feishu-allowFrom.json"
                        if path.exists(): (backup / f"carher-{uid}-allowfrom.bak").write_text(path.read_text())
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps({"version": 1, "allowFrom": expected}, ensure_ascii=False) + "\n")
                    if rpod and home and app:
                        key = f"group:mode:{home}:{app}"
                        old = kube.exec(rpod, ["redis-cli", "GET", key], check=False)
                        (backup / f"carher-{uid}-redis.bak").write_text(json.dumps({"key": key, "value": old}) + "\n")
                        payload = '{"mode":"group-at","context":"group-at runtime state; ascii-only","set_by":"carher-feishu-repair"}'
                        kube.exec(rpod, ["redis-cli", "SET", key, payload])
                        kube.exec(rpod, ["redis-cli", "SADD", f"group:tracked:{app}", home])
                    if args.restart:
                        run([*kube.base, "rollout", "restart", f"deployment/carher-{uid}"])
                        run([*kube.base, "rollout", "status", f"deployment/carher-{uid}", f"--timeout={args.timeout}s"], timeout=args.timeout + 30)
                row["status"] = "PASS" if args.apply else ("DRIFT" if not row["dm_ok"] else "PASS")
            except Exception as exc:
                row["detail"] = str(exc)[:300]
            print(json.dumps(row, ensure_ascii=False), flush=True)
            results.append(row)
        time.sleep(2)
    report = (backup / "report.json") if args.apply else Path(f"/tmp/carher-feishu-repair-{run_id}.json")
    report.write_text(json.dumps({"mode": "apply" if args.apply else "audit", "results": results}, ensure_ascii=False, indent=2) + "\n")
    print(f"report={report}")
    return 1 if any(r["status"] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
