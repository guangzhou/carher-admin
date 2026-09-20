#!/usr/bin/env python3
"""Converge Feishu DM owner allowlists for CarHer instances.

The script is read-only unless --apply is supplied. It is intended to run on an
ACK worker where both kubectl and the shared NAS mount at /Data are available.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_NAMESPACE = "carher"
DEFAULT_DATA_ROOT = Path("/Data")
DEFAULT_BACKUP_ROOT = Path("/Data/_backups/carher-feishu-dm-allowfrom")
OWNER_RE = re.compile(r"^ou_[A-Za-z0-9]+$")


@dataclass
class Target:
    uid: int
    her_name: str
    display_name: str
    owners: list[str]
    volume: str
    phase: str
    feishu_ws: str


@dataclass
class Result:
    uid: int
    name: str
    status: str
    changed_config: bool = False
    changed_store: bool = False
    restarted: bool = False
    runtime_verified: bool = False
    detail: str = ""


def run(
    cmd: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(f"{' '.join(cmd)} failed: {detail[:500]}")
    return proc


class Kube:
    def __init__(self, namespace: str, kubeconfig: str | None) -> None:
        self.namespace = namespace
        self.base = ["kubectl"]
        if kubeconfig:
            self.base.append(f"--kubeconfig={kubeconfig}")
        self.base.extend(["-n", namespace])

    def command(self, args: list[str]) -> list[str]:
        return [*self.base, *args]

    def json(self, args: list[str]) -> dict[str, Any]:
        proc = run(self.command([*args, "-o", "json"]))
        return json.loads(proc.stdout)

    def replace(self, obj: dict[str, Any]) -> None:
        run(
            self.command(["replace", "-f", "-"]),
            input_text=json.dumps(obj, ensure_ascii=False),
        )


def parse_owners(owner: str) -> list[str]:
    seen: set[str] = set()
    owners: list[str] = []
    for value in re.split(r"[|\s,;]+", str(owner or "").strip()):
        if not OWNER_RE.fullmatch(value) or value in seen:
            continue
        seen.add(value)
        owners.append(value)
    return owners


def config_access_state(cfg: dict[str, Any], owners: list[str]) -> dict[str, Any]:
    feishu = (cfg.get("channels") or {}).get("feishu") or {}
    dm = feishu.get("dm") or {}
    return {
        "dmPolicy": feishu.get("dmPolicy"),
        "allowFrom": feishu.get("allowFrom") if isinstance(feishu.get("allowFrom"), list) else [],
        "dmAllowFrom": dm.get("allowFrom") if isinstance(dm.get("allowFrom"), list) else [],
        "expected": owners,
    }


def config_is_converged(cfg: dict[str, Any], owners: list[str]) -> bool:
    state = config_access_state(cfg, owners)
    return (
        state["dmPolicy"] == "allowlist"
        and state["allowFrom"] == owners
        and state["dmAllowFrom"] == owners
    )


def patch_config(cfg: dict[str, Any], owners: list[str]) -> dict[str, Any]:
    patched = copy.deepcopy(cfg)
    feishu = patched.setdefault("channels", {}).setdefault("feishu", {})
    feishu.setdefault("dm", {})["allowFrom"] = owners
    feishu["allowFrom"] = owners
    feishu["dmPolicy"] = "allowlist"
    commands = patched.setdefault("commands", {})
    commands["ownerAllowFrom"] = owners
    return patched


def store_path(data_root: Path, volume: str) -> Path:
    return data_root / volume / "credentials" / "feishu-allowFrom.json"


def read_store(path: Path) -> tuple[bool, list[str], str]:
    if not path.exists():
        return False, [], "missing"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return True, [], f"invalid_json:{exc}"
    entries = raw.get("allowFrom")
    if not isinstance(entries, list):
        return True, [], "invalid_allowFrom"
    return True, [str(item) for item in entries], "ok"


def write_store(path: Path, owners: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(
        json.dumps({"version": 1, "allowFrom": owners}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def resolve_targets(
    kube: Kube,
    requested: set[int],
) -> tuple[list[Target], list[Result]]:
    hers = kube.json(["get", "her"])
    pvcs = kube.json(["get", "pvc"])
    volumes: dict[int, str] = {}
    for pvc in pvcs.get("items", []):
        name = pvc.get("metadata", {}).get("name", "")
        match = re.fullmatch(r"carher-(\d+)-data", name)
        if match:
            volumes[int(match.group(1))] = (pvc.get("spec") or {}).get("volumeName") or ""

    targets: list[Target] = []
    skipped: list[Result] = []
    found: set[int] = set()
    for item in hers.get("items", []):
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        uid_raw = spec.get("userId")
        if uid_raw is None:
            match = re.fullmatch(r"her-(\d+)", item.get("metadata", {}).get("name", ""))
            if not match:
                continue
            uid_raw = match.group(1)
        uid = int(uid_raw)
        if requested and uid not in requested:
            continue
        found.add(uid)
        display_name = str(spec.get("name") or "")
        phase = str(status.get("phase") or "")
        owners = parse_owners(str(spec.get("owner") or ""))
        reason = ""
        if bool(spec.get("paused")) or phase == "Paused":
            reason = "paused"
        elif not owners:
            reason = "no_valid_owner"
        elif not volumes.get(uid):
            reason = "no_bound_pvc"
        if reason:
            skipped.append(Result(uid, display_name, "SKIP", detail=reason))
            continue
        targets.append(
            Target(
                uid=uid,
                her_name=item.get("metadata", {}).get("name", f"her-{uid}"),
                display_name=display_name,
                owners=owners,
                volume=volumes[uid],
                phase=phase,
                feishu_ws=str(status.get("feishuWS") or ""),
            )
        )
    for uid in sorted(requested - found):
        skipped.append(Result(uid, "", "SKIP", detail="her_not_found"))
    return sorted(targets, key=lambda item: item.uid), skipped


def load_configmap(kube: Kube, target: Target) -> tuple[dict[str, Any], dict[str, Any]]:
    cm = kube.json(["get", "cm", f"carher-{target.uid}-user-config"])
    raw = (cm.get("data") or {}).get("openclaw.json")
    if not raw:
        raise RuntimeError("user ConfigMap has no openclaw.json")
    return cm, json.loads(raw)


def backup_access_state(
    run_dir: Path,
    target: Target,
    cfg: dict[str, Any],
    path: Path,
) -> None:
    exists, entries, store_status = read_store(path)
    commands = cfg.get("commands") or {}
    backup = {
        "uid": target.uid,
        "herName": target.her_name,
        "displayName": target.display_name,
        "ownersFromCrd": target.owners,
        "configAccess": config_access_state(cfg, target.owners),
        "commandsOwnerAllowFrom": commands.get("ownerAllowFrom"),
        "store": {
            "path": str(path),
            "exists": exists,
            "status": store_status,
            "allowFrom": entries,
        },
    }
    backup_path = run_dir / f"carher-{target.uid}.json"
    backup_path.write_text(json.dumps(backup, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(backup_path, 0o600)


def apply_configmap(kube: Kube, cm: dict[str, Any], cfg: dict[str, Any]) -> None:
    cm = copy.deepcopy(cm)
    cm["data"]["openclaw.json"] = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    for key in ("managedFields", "creationTimestamp"):
        cm.get("metadata", {}).pop(key, None)
    kube.replace(cm)


def rollout_and_verify(kube: Kube, target: Target, timeout: int) -> tuple[bool, str]:
    deployment = f"carher-{target.uid}"
    restart = run(kube.command(["rollout", "restart", f"deployment/{deployment}"]), check=False)
    if restart.returncode != 0:
        return False, f"restart_failed:{(restart.stderr or restart.stdout).strip()[:240]}"
    status = run(
        kube.command(["rollout", "status", f"deployment/{deployment}", f"--timeout={timeout}s"]),
        check=False,
        timeout=timeout + 30,
    )
    if status.returncode != 0:
        return False, f"rollout_failed:{(status.stderr or status.stdout).strip()[:240]}"
    return True, "rollout_ok"


def verify_runtime(kube: Kube, target: Target) -> tuple[bool, str]:
    expected = json.dumps(target.owners, ensure_ascii=True)
    probe = (
        "import json;"
        "d=json.load(open('/data/.openclaw/openclaw.json'));"
        "f=d.get('channels',{}).get('feishu',{});"
        f"e={expected};"
        "ok=f.get('dmPolicy')=='allowlist' and f.get('allowFrom')==e "
        "and f.get('dm',{}).get('allowFrom')==e;"
        "print(json.dumps({'ok':ok,'policy':f.get('dmPolicy'),"
        "'top':len(f.get('allowFrom',[])),'dm':len(f.get('dm',{}).get('allowFrom',[]))}));"
        "raise SystemExit(0 if ok else 3)"
    )
    proc = run(
        kube.command(
            [
                "exec",
                f"deployment/carher-{target.uid}",
                "-c",
                "carher",
                "--",
                "python3",
                "-c",
                probe,
            ]
        ),
        check=False,
        timeout=60,
    )
    detail = (proc.stdout or proc.stderr).strip().replace("\n", " ")[:300]
    return proc.returncode == 0, detail or f"runtime_probe_exit_{proc.returncode}"


def process_target(
    kube: Kube,
    target: Target,
    *,
    data_root: Path,
    run_dir: Path | None,
    apply: bool,
) -> Result:
    try:
        cm, cfg = load_configmap(kube, target)
        path = store_path(data_root, target.volume)
        store_exists, store_entries, store_status = read_store(path)
        config_ok = config_is_converged(cfg, target.owners)
        store_ok = store_exists and store_status == "ok" and store_entries == target.owners
        if config_ok and store_ok:
            return Result(target.uid, target.display_name, "PASS", detail="already_converged")
        if not apply:
            detail = f"config={'ok' if config_ok else 'drift'} store={'ok' if store_ok else store_status}"
            return Result(target.uid, target.display_name, "DRIFT", detail=detail)
        assert run_dir is not None
        backup_access_state(run_dir, target, cfg, path)
        result = Result(target.uid, target.display_name, "PASS")
        if not config_ok:
            apply_configmap(kube, cm, patch_config(cfg, target.owners))
            result.changed_config = True
        if not store_ok:
            write_store(path, target.owners)
            result.changed_store = True
        _, verified_cfg = load_configmap(kube, target)
        _, verified_entries, verified_store_status = read_store(path)
        if not config_is_converged(verified_cfg, target.owners):
            raise RuntimeError("ConfigMap verification mismatch")
        if verified_store_status != "ok" or verified_entries != target.owners:
            raise RuntimeError("pairing store verification mismatch")
        result.detail = "converged"
        return result
    except Exception as exc:
        return Result(target.uid, target.display_name, "FAIL", detail=str(exc)[:500])


def chunks(items: list[Target], size: int) -> list[list[Target]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def write_report(path: Path, args: argparse.Namespace, results: list[Result]) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    report = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "audit",
        "restart": bool(args.restart),
        "waveSize": args.wave_size,
        "counts": counts,
        "results": [asdict(result) for result in sorted(results, key=lambda row: row.uid)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit or repair Feishu DM owner allowlists in controlled waves."
    )
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--targets", nargs="*", type=int, default=[])
    parser.add_argument("--wave-size", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.wave_size < 1 or args.wave_size > 50:
        parser.error("--wave-size must be between 1 and 50")
    if args.restart and not args.apply:
        parser.error("--restart requires --apply")
    return args


def main() -> int:
    args = parse_args()
    kube = Kube(args.namespace, args.kubeconfig)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir: Path | None = None
    if args.apply:
        run_dir = args.backup_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(run_dir, 0o700)

    print(
        f"mode={'apply' if args.apply else 'audit'} wave_size={args.wave_size} "
        f"restart={args.restart}",
        flush=True,
    )
    targets, skipped = resolve_targets(kube, set(args.targets))
    print(f"targets={len(targets)} skipped={len(skipped)}", flush=True)
    results = list(skipped)
    started = time.monotonic()

    for wave_number, wave in enumerate(chunks(targets, args.wave_size), start=1):
        print(
            f"wave_start={wave_number} count={len(wave)} ids="
            + ",".join(str(target.uid) for target in wave),
            flush=True,
        )
        wave_results: dict[int, Result] = {}
        for target in wave:
            result = process_target(
                kube,
                target,
                data_root=args.data_root,
                run_dir=run_dir,
                apply=args.apply,
            )
            wave_results[target.uid] = result
            print(f"config her-{target.uid} {result.status} {result.detail}", flush=True)

        if args.apply and args.restart:
            for target in wave:
                result = wave_results[target.uid]
                if result.status != "PASS":
                    continue
                ok, detail = rollout_and_verify(kube, target, args.timeout)
                result.restarted = ok
                if not ok:
                    result.status = "FAIL"
                    result.detail = detail
                    print(f"rollout her-{target.uid} FAIL {detail}", flush=True)
                    continue
                runtime_ok, runtime_detail = verify_runtime(kube, target)
                result.runtime_verified = runtime_ok
                if not runtime_ok:
                    result.status = "FAIL"
                    result.detail = f"runtime_verify_failed:{runtime_detail}"
                else:
                    result.detail = f"{result.detail}; rollout_ok; runtime_ok"
                print(
                    f"rollout her-{target.uid} {'PASS' if runtime_ok else 'FAIL'} {runtime_detail}",
                    flush=True,
                )
        results.extend(wave_results.values())
        print(f"wave_end={wave_number}", flush=True)

    report_path = args.report
    if report_path is None:
        report_path = (
            run_dir / "report.json"
            if run_dir is not None
            else Path(f"/tmp/carher-feishu-dm-allowfrom-audit-{run_id}.json")
        )
    write_report(report_path, args, results)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print(
        "summary " + " ".join(f"{key}={value}" for key, value in sorted(counts.items())),
        flush=True,
    )
    print(f"report={report_path}", flush=True)
    if run_dir is not None:
        print(f"backup={run_dir}", flush=True)
    print(f"elapsed={time.monotonic() - started:.1f}s", flush=True)
    return 1 if counts.get("FAIL", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
