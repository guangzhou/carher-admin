#!/usr/bin/env python3
"""
Scan paused her PVCs by spinning up a debug node pod that mounts the PVCs readOnly,
run her-cost-stats.js offline, collect JSON into --out-dir.

Usage:
  scan_paused_pvcs.py --uids 14,75,83 --out-dir ./out
  scan_paused_pvcs.py --inventory inventory.json --out-dir ./out   # reads "paused" array

Notes:
- Mounts each PVC at /d/<uid> inside the debug pod
- her-cost-stats.js is invoked with --root /d/<uid>
- Limited by max volumes per pod; chunk if > 20 paused PVCs (current deployment has 4)
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NS_DEFAULT = "carher"
POD_NAME = "her-stats-scan"
CHUNK_SIZE = 20


def kapply(ns: str, yaml: str):
    p = subprocess.run(["kubectl", "apply", "-f", "-"], input=yaml, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"kubectl apply failed: {p.stderr}")


def kdel(ns: str, name: str):
    subprocess.run(["kubectl", "-n", ns, "delete", "pod", name, "--ignore-not-found", "--wait=false"],
                   capture_output=True, text=True)


def wait_running(ns: str, name: str, timeout_s: int = 60) -> bool:
    for _ in range(timeout_s):
        p = subprocess.run(["kubectl", "-n", ns, "get", "pod", name, "-o", "jsonpath={.status.phase}"],
                           capture_output=True, text=True)
        if p.stdout.strip() == "Running":
            return True
        time.sleep(1)
    return False


def build_pod_yaml(ns: str, name: str, uids: list[str]) -> str:
    mounts = "\n".join(
        f"        - {{name: d{u}, mountPath: /d/{u}, readOnly: true}}" for u in uids
    )
    volumes = "\n".join(
        f"    - {{name: d{u}, persistentVolumeClaim: {{claimName: carher-{u}-data, readOnly: true}}}}"
        for u in uids
    )
    return f"""apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: {ns}
spec:
  restartPolicy: Never
  containers:
    - name: scan
      image: node:22-alpine
      command: ["sh","-c","sleep 3600"]
      volumeMounts:
{mounts}
  volumes:
{volumes}
"""


def install_script(ns: str, pod: str, script_path: Path):
    b64 = base64.b64encode(script_path.read_bytes()).decode()
    subprocess.run(
        ["kubectl", "-n", ns, "exec", pod, "--",
         "sh", "-c", f"echo '{b64}' | base64 -d > /tmp/s.js"],
        check=True, capture_output=True,
    )


def run_for_uid(ns: str, pod: str, uid: str, out_dir: Path):
    out_path = out_dir / f"uid-{uid}.json"
    err_path = out_dir / f"uid-{uid}.err"
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        subprocess.run(
            ["kubectl", "-n", ns, "exec", pod, "--",
             "node", "/tmp/s.js", "--json", "--root", f"/d/{uid}"],
            stdout=out, stderr=err, timeout=300, check=False,
        )
    sz = out_path.stat().st_size
    print(f"uid={uid} size={sz}", file=sys.stderr)
    if sz < 100:
        out_path.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default=NS_DEFAULT)
    ap.add_argument("--pod-name", default=POD_NAME)
    ap.add_argument("--uids", help="comma-separated uid list")
    ap.add_argument("--inventory", help="inventory.json from inventory.py")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--script", default=str(Path(__file__).parent / "her-cost-stats.js"))
    args = ap.parse_args()

    if args.inventory:
        inv = json.load(open(args.inventory))
        uids = list(inv.get("paused", []))
    elif args.uids:
        uids = [u.strip() for u in args.uids.split(",") if u.strip()]
    else:
        ap.error("--uids or --inventory required")

    if not uids:
        print("no paused uids to scan", file=sys.stderr)
        return

    script_path = Path(args.script)
    if not script_path.exists():
        ap.error(f"script not found: {script_path}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Chunk to respect per-pod volume limits
    for chunk_i in range(0, len(uids), CHUNK_SIZE):
        chunk = uids[chunk_i : chunk_i + CHUNK_SIZE]
        pod_name = f"{args.pod_name}-{chunk_i // CHUNK_SIZE}"
        print(f"[chunk {chunk_i // CHUNK_SIZE + 1}] {len(chunk)} PVCs -> pod {pod_name}", file=sys.stderr)

        kdel(args.namespace, pod_name)
        time.sleep(1)
        kapply(args.namespace, build_pod_yaml(args.namespace, pod_name, chunk))
        if not wait_running(args.namespace, pod_name):
            raise RuntimeError(f"pod {pod_name} did not reach Running")

        try:
            install_script(args.namespace, pod_name, script_path)
            for uid in chunk:
                run_for_uid(args.namespace, pod_name, uid, out_dir)
        finally:
            kdel(args.namespace, pod_name)


if __name__ == "__main__":
    main()
