#!/usr/bin/env python3
"""Read-only runtime audit for the carher-1000 model catalog rollout."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
from typing import Any


NAMESPACE = "carher"
TARGET_CHAT_MODELS = sorted([
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "gemini-3.5-flash",
    "glm-5",
    "qwen3.7-plus",
])


def run(args: list[str], *, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout)


def kubectl_json(args: list[str]) -> Any:
    proc = run(["kubectl", "-n", NAMESPACE, *args, "-o", "json"], timeout=90)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:500])
    return json.loads(proc.stdout)


def pod_exec(pod: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    return run(["kubectl", "-n", NAMESPACE, "exec", "-i", pod, "-c", "carher", "--", *command])


def audit_pod(item: tuple[str, str]) -> dict[str, Any]:
    uid, pod = item
    result: dict[str, Any] = {"uid": uid, "pod": pod, "openclaw": "unknown", "hermes": "unknown"}
    openclaw_script = (
        "const d=require('/data/.openclaw/openclaw.json');"
        "console.log(JSON.stringify({primary:d.agents?.defaults?.model?.primary,"
        "aliases:Object.keys(d.agents?.defaults?.models||{}).map(x=>x.replace(/^litellm\\//,'')).sort(),"
        "provider:(d.models?.providers?.litellm?.models||[]).map(x=>x.id).sort(),"
        "memory:d.agents?.defaults?.memorySearch?.model,include:Object.hasOwn(d,'$include')}))"
    )
    proc = pod_exec(pod, ["node", "-e", openclaw_script])
    if proc.returncode != 0:
        result["openclaw"] = "exec_failed"
        result["openclaw_error"] = (proc.stderr or proc.stdout).strip()[:240]
    else:
        try:
            live = json.loads(proc.stdout.strip().splitlines()[-1])
            result["openclaw"] = "ok" if (
                live.get("primary") == "litellm/gpt-5.6-terra"
                and live.get("aliases") == TARGET_CHAT_MODELS
                and live.get("provider") == TARGET_CHAT_MODELS
                and live.get("memory") == "bge-m3"
                and live.get("include") is False
            ) else "mismatch"
            if result["openclaw"] != "ok":
                result["openclaw_live"] = live
        except Exception as exc:
            result["openclaw"] = "invalid_output"
            result["openclaw_error"] = str(exc)[:240]

    hermes_script = (
        "import json,pathlib,yaml\n"
        f"target={TARGET_CHAT_MODELS!r}\n"
        "found=[];bad=[]\n"
        "for p in [pathlib.Path('/opt/data/.hermes/config-litellm.yaml'),pathlib.Path('/opt/data/.hermes/config.yaml')]:\n"
        " if not p.exists(): continue\n"
        " found.append(str(p))\n"
        " try:\n"
        "  d=yaml.safe_load(p.read_text()) or {}; models=sorted(d.get('providers',{}).get('litellm',{}).get('models',{}))\n"
        "  if d.get('model',{}).get('default')!='gpt-5.6-terra' or d.get('providers',{}).get('litellm',{}).get('default_model')!='gpt-5.6-terra' or models!=target: bad.append(str(p))\n"
        " except Exception: bad.append(str(p)+':parse')\n"
        "print(json.dumps({'found':found,'bad':bad}))\n"
    )
    proc = pod_exec(pod, ["sh", "-lc", "test -x /opt/hermes/.venv/bin/python3"])
    if proc.returncode != 0:
        result["hermes"] = "not_present"
        return result
    proc = pod_exec(pod, ["/opt/hermes/.venv/bin/python3", "-c", hermes_script])
    if proc.returncode != 0:
        result["hermes"] = "exec_failed"
        result["hermes_error"] = (proc.stderr or proc.stdout).strip()[:240]
        return result
    try:
        live = json.loads(proc.stdout.strip().splitlines()[-1])
        if not live.get("found"):
            result["hermes"] = "not_present"
        elif live.get("bad"):
            result["hermes"] = "mismatch"
            result["hermes_bad"] = live["bad"]
        else:
            result["hermes"] = "ok"
    except Exception as exc:
        result["hermes"] = "invalid_output"
        result["hermes_error"] = str(exc)[:240]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--report", default="/tmp/carher-model-runtime-audit.json")
    args = parser.parse_args()

    pods = kubectl_json(["get", "pods"])["items"]
    current: dict[str, str] = {}
    non_running: dict[str, list[str]] = {}
    for pod in pods:
        labels = pod["metadata"].get("labels", {})
        uid = labels.get("user-id")
        if labels.get("app") != "carher-user" or not uid:
            continue
        phase = pod.get("status", {}).get("phase", "Unknown")
        statuses = pod.get("status", {}).get("containerStatuses") or []
        ready = phase == "Running" and statuses and all(row.get("ready") for row in statuses)
        if ready:
            current[uid] = pod["metadata"]["name"]
        else:
            non_running.setdefault(uid, []).append(f"{pod['metadata']['name']}:{phase}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(audit_pod, sorted(current.items(), key=lambda row: int(row[0]))))

    summary = {
        "ready_pods": len(results),
        "openclaw_ok": sum(row["openclaw"] == "ok" for row in results),
        "openclaw_bad": sum(row["openclaw"] != "ok" for row in results),
        "hermes_ok": sum(row["hermes"] == "ok" for row in results),
        "hermes_not_present": sum(row["hermes"] == "not_present" for row in results),
        "hermes_bad": sum(row["hermes"] not in {"ok", "not_present"} for row in results),
        "non_running_uids": sorted(uid for uid in non_running if uid not in current),
    }
    payload = {
        "summary": summary,
        "bad": [row for row in results if row["openclaw"] != "ok" or row["hermes"] not in {"ok", "not_present"}],
        "non_running": non_running,
    }
    with open(args.report, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"report": args.report, **summary}, ensure_ascii=False))
    return 0 if not payload["bad"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
