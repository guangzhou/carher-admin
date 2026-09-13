#!/usr/bin/env python3
"""Run the fail-closed local readiness contract for rollout artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent


def run(name: str, command: list[str]) -> dict[str, object]:
    result = subprocess.run(command, cwd=REPO, text=True, capture_output=True, check=False)
    output = (result.stdout + result.stderr)[-8000:]
    return {
        "name": name,
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "returncode": result.returncode,
        "output_sha256": "sha256:" + hashlib.sha256(output.encode()).hexdigest(),
        "output_tail": output[-1200:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--developer-mode", action="store_true", help="report missing optional tools without claiming production readiness")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    checks: list[dict[str, object]] = []
    required = ("helm", "nginx", "shellcheck", "kubeconform")
    missing = [name for name in required if shutil.which(name) is None]
    for name in required:
        checks.append({"name": f"tool:{name}", "status": "PASS" if name not in missing else "NOT_RUN"})

    checks.append(run("pytest", ["python3", "-m", "pytest", str(ROOT / "tests"), "-q"]))
    shell_dir = ROOT / "scripts"
    checks.append(
        run(
            "shellcheck",
            [
                "shellcheck",
                "-x",
                "-P",
                str(shell_dir),
                *map(str, sorted(shell_dir.glob("*.sh"))),
            ],
        )
        if "shellcheck" not in missing
        else {"name": "shellcheck", "status": "NOT_RUN"}
    )
    checks.append(run("nginx-http", ["python3", str(ROOT / "scripts/fixtures/nginx/run_fixture.py"), "--production-renderer", "--nginx-binary", "nginx"]) if "nginx" not in missing else {"name": "nginx-http", "status": "NOT_RUN"})
    if "helm" not in missing:
        checks.append(run("helm-lint", ["helm", "lint", str(ROOT / "chart")]))
    else:
        checks.append({"name": "helm-lint", "status": "NOT_RUN"})
    if "kubeconform" not in missing:
        # k8s/values-*.yaml 是 Helm values，不是 K8s 资源，没有 kind —— 必须排除，
        # 否则 kubeconform 会为这三份文件报 "missing 'kind' key" 导致整个门禁红掉，
        # 而真正的清单其实全过。把工具噪声当失败会训练人忽略这道门。
        checks.append(
            run(
                "kubeconform",
                [
                    "kubeconform",
                    "-strict",
                    "-kubernetes-version",
                    "1.30.0",
                    "-summary",
                    "-ignore-filename-pattern",
                    r"values-.*\.yaml$",
                    str(ROOT / "k8s"),
                ],
            )
        )
    else:
        checks.append({"name": "kubeconform", "status": "NOT_RUN"})

    blocked = any(item.get("status") == "FAIL" for item in checks) or (missing and not args.developer_mode)
    report = {
        "tool": "verify-readiness",
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "developer" if args.developer_mode else "production",
        "status": "FAIL" if blocked else ("DEVELOPER_PASS" if missing else "PASS"),
        "missing_tools": missing,
        "checks": checks,
    }
    rendered = json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
    print(rendered, end="")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
