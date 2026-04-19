#!/usr/bin/env python3
"""
Inventory carher namespace: running her pods + paused her PVCs.
Writes JSON to stdout or --out file:
  {
    "active":  {"<uid>": "<pod-name>", ...},
    "paused":  ["<uid>", ...],    # PVC exists, no running pod
    "all_uids": ["<uid>", ...]
  }
"""
import json, re, subprocess, sys, argparse


def kubectl_json(args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"kubectl failed: {p.stderr[:400]}")
    return json.loads(p.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default="carher")
    ap.add_argument("--out", default="-")
    args = ap.parse_args()

    pods = kubectl_json(["kubectl", "-n", args.namespace, "get", "pods", "-o", "json"])
    active = {}
    for p in pods["items"]:
        n = p["metadata"]["name"]
        m = re.match(r"^carher-(\d+)-", n)
        if not m:
            continue
        if p["status"].get("phase") != "Running":
            continue
        active[m.group(1)] = n

    pvcs = kubectl_json(["kubectl", "-n", args.namespace, "get", "pvc", "-o", "json"])
    pvc_uids = set()
    for p in pvcs["items"]:
        n = p["metadata"]["name"]
        m = re.match(r"^carher-(\d+)-data$", n)
        if m and p["status"]["phase"] == "Bound":
            pvc_uids.add(m.group(1))

    paused = sorted(pvc_uids - set(active), key=int)
    all_uids = sorted(set(active) | pvc_uids, key=int)

    out = {"active": active, "paused": paused, "all_uids": all_uids}
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out == "-":
        sys.stdout.write(text + "\n")
    else:
        open(args.out, "w").write(text)
        print(f"active={len(active)} paused={len(paused)} total={len(all_uids)}", file=sys.stderr)


if __name__ == "__main__":
    main()
