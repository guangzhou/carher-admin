#!/usr/bin/env python3
"""Splice ONE model entry into a live LiteLLM `config.yaml` ConfigMap, then gate the rollout.

Why this script exists
----------------------
2026-09-17: adding `openrouter-deepseek-v4.1-flash` to the Aliyun proxy (ns
`carher`) was done by hand. Every step that could lie, did lie once before:

* `kubectl apply -f k8s/litellm-proxy.yaml` would push ~600 lines of other
  people's pending edits -> the ONLY legal path is read live -> surgical splice
  -> `kubectl patch cm`.
* Several sessions edit the same CM. `resourceVersion` read before the write is
  the concurrency baseline; if it moved, someone else spliced too -- diff first.
* `rollout status` says "exceeded its progress deadline" on this Deployment as
  NORMAL output (grace 600s + hostPort + 2 replicas). It is not a failure, and
  it is also not proof of success: a pod can be Running with the OLD config.
  The only convergence judge is `sha256(container:/app/config.yaml) ==
  sha256(new CM data)` on EVERY ready pod.
* `/v1/models` listing the new name proves the config parsed, not that the leg
  works. Only a real request with a unique nonce does.

Footprint (exactly what this touches)
-------------------------------------
* writes `data["config.yaml"]` of the target ConfigMap (one key, nothing else)
* optional `kubectl rollout restart deployment/<--deployment>`
* backup: full ConfigMap JSON is dumped to --backup-dir BEFORE the patch, named
  `<cm>-<ts>-rv<resourceVersion>.json`
* rollback: `kubectl -n <ns> patch cm <cm> --type=merge --patch-file=<(jq '{data}' <backup>)`
  or re-run with --restore <backup>

Nothing else is written. Key allowlists are a separate tool
(`litellm-198-key-allowlist.py`), on purpose: a model going live and a key
being allowed to call it are two gates, and mixing them hides which one failed.

Examples
--------
    # plan (default): print the exact diff, write nothing
    ./litellm-aliyun-cm-add-model.py --ns carher \\
        --model-name openrouter-deepseek-v4.1-flash \\
        --upstream-model openrouter/deepseek/deepseek-v4.1-flash \\
        --api-key-env OPENROUTER_API_KEY \\
        --model-id openrouter/deepseek-v4.1-flash \\
        --price-in 3.0e-07 --price-out 1.2e-06 --price-cache-read 6.0e-09 \\
        --max-input-tokens 1048576 --max-output-tokens 384000

    # do it, restart the proxy, and gate on the per-pod config sha
    ... --apply --rollout --backup-dir /root/herflash

    # convergence judge only (no writes) -- also useful after someone else's change
    ./litellm-aliyun-cm-add-model.py --ns carher --verify-sha

    # undo
    ./litellm-aliyun-cm-add-model.py --ns carher --restore /root/herflash/<file>.json --apply

Pricing note: LiteLLM bills off `litellm_params`; `model_info` alone is
decoration. Both blocks are written. If the upstream price is time-tiered
(OpenRouter does this), pass the PEAK price -- under-pricing silently
under-bills and nobody notices.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time

CONFIG_KEY = "config.yaml"


# ---------------------------------------------------------------- pure logic


def build_entry(
    model_name: str,
    upstream_model: str,
    api_key_env: str,
    api_base: str | None = None,
    model_id: str | None = None,
    price_in: str | None = None,
    price_out: str | None = None,
    price_cache_read: str | None = None,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    access_group: str | None = None,
    mode: str = "chat",
) -> str:
    """Render one `model_list` entry as YAML text.

    Prices land in BOTH blocks: `litellm_params` is what the billing path
    reads, `model_info` is what `/model/info` shows. Absent tiers stay absent;
    a literal 0.0 would bill as free.
    """
    params = [f"    model: {upstream_model}", f"    api_key: os.environ/{api_key_env}"]
    if api_base:
        params.append(f"    api_base: {api_base}")
    info = [f"    mode: {mode}"]
    if model_id:
        info.append(f"    id: {model_id}")
    if access_group:
        info.append(f'    access_groups: ["{access_group}"]')
    if max_input_tokens:
        info.append(f"    max_input_tokens: {max_input_tokens}")
    if max_output_tokens:
        info.append(f"    max_output_tokens: {max_output_tokens}")
    for field, value in (
        ("input_cost_per_token", price_in),
        ("output_cost_per_token", price_out),
        ("cache_read_input_token_cost", price_cache_read),
    ):
        if value is not None:
            params.append(f"    {field}: {value}")
            info.append(f"    {field}: {value}")
    return "\n".join(
        [f"- model_name: {model_name}", "  litellm_params:", *params, "  model_info:", *info, ""]
    )


def find_insert_index(config: str) -> int:
    """Line index of the first top-level key after `model_list:` (== its end).

    The entry goes right before `litellm_settings:` / `router_settings:` so the
    splice never lands inside another entry's block.
    """
    lines = config.split("\n")
    for i, line in enumerate(lines):
        if i == 0 or not line:
            continue
        if not line[0].isspace() and not line.startswith("-"):
            return i
    raise ValueError("no top-level key after model_list: refusing to guess where model_list ends")


def count_entries(config: str) -> int:
    return config.count("\n- model_name:") + (1 if config.startswith("- model_name:") else 0)


def splice(config: str, entry: str) -> str:
    """Insert `entry` at the end of model_list. Pure; raises if it would touch anything else."""
    name = entry.split("\n", 1)[0].split("model_name:", 1)[1].strip()
    if f"model_name: {name}\n" in config or config.endswith(f"model_name: {name}"):
        raise ValueError(f"{name} already present -- refusing to add a duplicate model_name")
    idx = find_insert_index(config)
    lines = config.split("\n")
    new = "\n".join(lines[:idx]) + "\n" + entry + "\n".join(lines[idx:])
    if new.replace(entry, "", 1) != config:
        raise AssertionError("splice altered bytes outside the inserted entry")
    if count_entries(new) != count_entries(config) + 1:
        raise AssertionError("entry count did not grow by exactly one")
    return new


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------ cluster


def kubectl(ns: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "-n", ns, *args], capture_output=True, text=True)


def read_cm(ns: str, cm: str) -> dict:
    out = kubectl(ns, "get", "cm", cm, "-o", "json")
    if out.returncode:
        sys.exit(f"cannot read cm/{cm}: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def ready_pods(ns: str, selector: str) -> list[str]:
    out = kubectl(ns, "get", "pods", "-l", selector, "--no-headers")
    pods = []
    for line in out.stdout.splitlines():
        cols = line.split()
        if len(cols) >= 3 and cols[1] == "1/1" and cols[2] == "Running":
            pods.append(cols[0])
    return pods


def verify_sha(ns: str, cm: str, selector: str, container: str) -> bool:
    """The only trustworthy convergence judge: per-pod config sha == CM sha."""
    want = sha(read_cm(ns, cm)["data"][CONFIG_KEY])
    pods = ready_pods(ns, selector)
    if not pods:
        print("FAIL: zero ready pods -- an empty sample is a failure, not a pass")
        return False
    ok = True
    for pod in pods:
        got = kubectl(ns, "exec", pod, "-c", container, "--", "sha256sum", f"/app/{CONFIG_KEY}")
        digest = got.stdout.split()[0] if got.stdout.split() else "<none>"
        match = digest == want
        ok &= match
        print(f"  {pod}: {digest[:16]} {'==' if match else '!='} cm {want[:16]}")
    return ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ns", default="carher")
    p.add_argument("--cm", default="litellm-config")
    p.add_argument("--deployment", default="litellm-proxy")
    p.add_argument("--selector", default="app=litellm-proxy")
    p.add_argument("--container", default="litellm")
    p.add_argument("--model-name")
    p.add_argument("--upstream-model", help="litellm provider path, e.g. openrouter/deepseek/deepseek-v4.1-flash")
    p.add_argument("--api-key-env", help="env var name; resolved as os.environ/<NAME> by LiteLLM")
    p.add_argument("--api-base")
    p.add_argument("--model-id", help="model_info.id, the string that shows up as x-litellm-model-id")
    p.add_argument("--price-in")
    p.add_argument("--price-out")
    p.add_argument("--price-cache-read")
    p.add_argument("--max-input-tokens", type=int)
    p.add_argument("--max-output-tokens", type=int)
    p.add_argument("--access-group", help='e.g. pro198 -- only for 198-bridge models')
    p.add_argument("--entry-file", help="read the YAML entry verbatim instead of building it")
    p.add_argument("--restore", help="backup JSON produced by an earlier run")
    p.add_argument("--backup-dir", default=".")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rollout", action="store_true", help="rollout restart + wait for per-pod sha convergence")
    p.add_argument("--rollout-timeout", type=int, default=2100, help="seconds; this Deployment takes 15-25 min")
    p.add_argument("--verify-sha", action="store_true", help="only judge convergence, write nothing")
    args = p.parse_args()

    live = read_cm(args.ns, args.cm)
    config = live["data"][CONFIG_KEY]
    rv = live["metadata"]["resourceVersion"]
    print(f"cm={args.cm} rv={rv} entries={count_entries(config)} sha={sha(config)[:16]}")

    if args.verify_sha:
        return 0 if verify_sha(args.ns, args.cm, args.selector, args.container) else 2

    if args.restore:
        new = json.load(open(args.restore))["data"][CONFIG_KEY]
        print(f"restore from {args.restore}: entries {count_entries(config)} -> {count_entries(new)}")
    else:
        if args.entry_file:
            entry = open(args.entry_file).read()
            if not entry.endswith("\n"):
                entry += "\n"
        else:
            missing = [f for f in ("model_name", "upstream_model", "api_key_env") if not getattr(args, f)]
            if missing:
                return p.error("need --entry-file or " + ", ".join("--" + m.replace("_", "-") for m in missing))
            entry = build_entry(
                args.model_name, args.upstream_model, args.api_key_env,
                api_base=args.api_base, model_id=args.model_id,
                price_in=args.price_in, price_out=args.price_out,
                price_cache_read=args.price_cache_read,
                max_input_tokens=args.max_input_tokens,
                max_output_tokens=args.max_output_tokens,
                access_group=args.access_group,
            )
        print("--- entry ---")
        print(entry.rstrip())
        print("-------------")
        new = splice(config, entry)
        print(f"entries {count_entries(config)} -> {count_entries(new)}, insert before "
              f"line {find_insert_index(config) + 1}")

    if not args.apply:
        print("plan only: nothing written (pass --apply)")
        return 0

    ts = time.strftime("%Y%m%dT%H%M%S")
    backup = f"{args.backup_dir.rstrip('/')}/{args.cm}-{ts}-rv{rv}.json"
    with open(backup, "w") as fh:
        json.dump(live, fh, indent=1)
    print(f"backup={backup}  (rollback: --restore {backup} --apply)")

    patch = f"/tmp/{args.cm}-patch-{ts}.json"
    with open(patch, "w") as fh:
        json.dump({"data": {CONFIG_KEY: new}}, fh)
    res = kubectl(args.ns, "patch", "cm", args.cm, "--type=merge", f"--patch-file={patch}")
    print("patch:", res.returncode, res.stdout.strip() or res.stderr.strip()[:200])
    if res.returncode:
        return 1

    back = read_cm(args.ns, args.cm)
    if back["data"][CONFIG_KEY] != new:
        print("FAIL: readback differs byte-wise from what we wrote (concurrent writer?)")
        return 2
    print(f"readback identical, new rv={back['metadata']['resourceVersion']} sha={sha(new)[:16]}")

    if not args.rollout:
        print("NOTE: pods still run the OLD config until a rollout restart -- new name will 400")
        return 0

    res = kubectl(args.ns, "rollout", "restart", f"deployment/{args.deployment}")
    print("rollout:", res.stdout.strip() or res.stderr.strip()[:200])
    print("waiting for per-pod sha convergence "
          "('exceeded its progress deadline' from rollout status is normal here and is NOT the judge)")
    deadline = time.time() + args.rollout_timeout
    while time.time() < deadline:
        time.sleep(30)
        if verify_sha(args.ns, args.cm, args.selector, args.container):
            print("converged: every ready pod runs the new config")
            print("NEXT: a real request with a unique nonce. /v1/models listing the name proves nothing.")
            return 0
        print(f"  ... {int(deadline - time.time())}s left")
    print("FAIL: not converged within --rollout-timeout (pods may still be terminating; re-run --verify-sha)")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
