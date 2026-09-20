#!/usr/bin/env python3
"""Add a model that lives on 198 LiteLLM to the Aliyun LiteLLM (ns carher), or verify one.

Why this script exists
----------------------
2026-09-09 the `pro198` access group went live on the Aliyun proxy. Before it,
adding one 198-bridged model cost three gates, the third being "write the
allowlist to 375 carher-* keys" -- pure repetitive labour, once per model.
With the access group that gate is gone: tag the CM entry with
`model_info.access_groups: ["pro198"]` and every carher-* key already carries
the single token `pro198`.

So the whole job is now two writes plus a rollout:

  1. widen the 198-side bridge key so it may call the upstream model name
  2. append one entry to the Aliyun CM `litellm-config`, then rollout restart

...and a verification pass that is longer than the change itself, because every
cheap judge in this path has been caught lying at least once. See --mode verify.

Modes
-----
  --mode plan    (default) read live state, print the exact diff, write nothing
  --mode apply   do 1 + 2 + rollout, then run the verify pass
  --mode verify  verify an already-onboarded model, no writes

Examples
--------
  # what would happen (no writes anywhere)
  ./aliyun-pro198-add-model.py --public-name gemini-3.7-flash \
      --upstream-name ag-gemini-3.7-flash

  # verify the one that is already live
  ./aliyun-pro198-add-model.py --mode verify --public-name gemini-3.8-flash \
      --upstream-name ag-gemini-3.8-flash

  # do it (needs the 198 master key to widen the bridge key)
  MK198=$(...) ./aliyun-pro198-add-model.py --mode apply \
      --public-name gemini-3.7-flash --upstream-name ag-gemini-3.7-flash

Hard rules encoded here (each one cost a round to learn)
--------------------------------------------------------
* NEVER `kubectl apply` the litellm-proxy manifest. The repo yaml is ~600 lines
  behind live; apply would push someone else's pending edits. Only surgical
  `kubectl patch cm` + `rollout restart`.
* NEVER introduce a `model_name` containing `*`. That turns `pro198` into a
  wildcard-route access group and, without an enterprise licence, EVERY key
  write starts returning 403. The script refuses to run if live already has one.
* `/pro/key/update` replaces fields, it does not merge -> read-merge-write.
* `models == []` means UNRESTRICTED; writing a list to such a key REVOKES access.
* Pricing must be written into `litellm_params`; `model_info` alone is silently
  ignored by the billing path. Unused tiers are left absent, never `0.0`
  (literal zero bills as free).
* No `timeout`/`shuf` -- absent on macOS, and a missing command makes the whole
  pipeline a silent no-op whose output is indistinguishable from "no matches".
* Every probe carries a unique nonce. LiteLLM response caching will happily
  serve a byte-identical body without touching the upstream.
* Sample count zero is an explicit FAIL, never folded into "identical"/"clean".
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

NS = "carher"
CM_NAME = "litellm-config"
CM_KEY = "config.yaml"
DEPLOY = "litellm-proxy"
DB_POD = "litellm-db-0"
SECRET_NAME = "carher-env-keys"
BRIDGE_ENV = "PRO198_BRIDGE_API_KEY"
ACCESS_GROUP = "pro198"
ID_PREFIX = "pro198/"
BASE_198 = "https://cc.auto-link.com.cn/pro"
API_BASE_IN_CM = BASE_198 + "/v1"

# Pricing keys copied from the 198 entry, in the order LiteLLM documents them.
# Anything absent upstream stays absent here -- see the 0.0 rule above.
COST_KEYS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
    "input_cost_per_token_above_200k_tokens",
    "output_cost_per_token_above_200k_tokens",
)

FAIL: list[str] = []


def fail(msg: str) -> None:
    FAIL.append(msg)
    print(f"  FAIL {msg}")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def info(msg: str) -> None:
    print(f"  --   {msg}")


def run(cmd: list[str], stdin: str | None = None, check: bool = True) -> str:
    """Run a command, surfacing stderr. Never 2>/dev/null: a swallowed stderr
    turns 'the command did not execute' into 'the command found nothing'."""
    p = subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, timeout=900
    )
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip()
        if check:
            raise RuntimeError(f"{' '.join(cmd[:4])}... rc={p.returncode}: {detail[:500]}")
        print(f"  (rc={p.returncode}) {detail[:300]}")
    return p.stdout


def kube(*args: str, stdin: str | None = None, check: bool = True) -> str:
    # --request-timeout, not the `timeout` binary: it does not exist on macOS.
    return run(["kubectl", "-n", NS, "--request-timeout=60s", *args], stdin=stdin, check=check)


def secret_value(key: str) -> str:
    raw = kube("get", "secret", SECRET_NAME, "-o", f"jsonpath={{.data.{key}}}").strip()
    if not raw:
        raise RuntimeError(f"secret {SECRET_NAME} has no key {key}")
    return run(["base64", "-d"], stdin=raw).strip()


def master_key_aliyun() -> str:
    raw = kube(
        "get", "secret", "litellm-secrets", "-o",
        "jsonpath={.data.LITELLM_MASTER_KEY}",
    ).strip()
    return run(["base64", "-d"], stdin=raw).strip()


def http(url: str, key: str, body: dict | None = None, timeout: int = 300) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


# ---------------------------------------------------------------- CM handling

def cm_read() -> tuple[str, str]:
    """Return (config.yaml text, resourceVersion). resourceVersion is the
    concurrency baseline: other sessions edit this CM."""
    out = kube("get", "cm", CM_NAME, "-o", "json")
    obj = json.loads(out)
    return obj["data"][CM_KEY], obj["metadata"]["resourceVersion"]


def cm_model_names(text: str) -> list[str]:
    return re.findall(r"^- model_name:\s*(\S+)\s*$", text, re.M)


def wildcard_census(names: list[str]) -> list[str]:
    return [n for n in names if "*" in n]


def build_entry(public: str, upstream: str, costs: dict[str, float]) -> str:
    lines = [
        f"- model_name: {public}",
        "  litellm_params:",
        f"    model: custom_openai/{upstream}",
        f"    api_key: os.environ/{BRIDGE_ENV}",
        f"    api_base: {API_BASE_IN_CM}",
    ]
    for k in COST_KEYS:
        if k in costs:
            lines.append(f"    {k}: {costs[k]}")
    lines += [
        "  model_info:",
        "    mode: chat",
        f"    id: {ID_PREFIX}{public}",
        f'    access_groups: ["{ACCESS_GROUP}"]',
    ]
    for k in COST_KEYS:
        if k in costs:
            lines.append(f"    {k}: {costs[k]}")
    return "\n".join(lines) + "\n"


def splice(text: str, entry: str) -> str:
    """Insert the entry at the end of model_list, i.e. right before the next
    top-level block. Surgical text edit, not a yaml round-trip: a round-trip
    would reformat 165 unrelated entries and make the diff unreviewable."""
    anchor = "\nlitellm_settings:"
    if text.count(anchor) != 1:
        raise RuntimeError(
            f"anchor 'litellm_settings:' appears {text.count(anchor)} times, expected 1"
        )
    return text.replace(anchor, "\n" + entry.rstrip("\n") + anchor, 1)


def cm_write(new_text: str, expect_rv: str) -> None:
    _, rv_now = cm_read()
    if rv_now != expect_rv:
        raise RuntimeError(
            f"CM changed under us (resourceVersion {expect_rv} -> {rv_now}). "
            "Someone else is editing. Re-run --mode plan and diff before writing."
        )
    ts = int(time.time())
    bak = f"/tmp/pro198-cm-backup-{ts}.yaml"
    with open(bak, "w") as f:
        f.write(cm_read()[0])
    print(f"  backup -> {bak}")
    patch = f"/tmp/pro198-cm-patch-{ts}.json"
    with open(patch, "w") as f:
        json.dump({"data": {CM_KEY: new_text}}, f)
    # patch, never apply: the repo manifest is ~600 lines behind live.
    kube("patch", "cm", CM_NAME, "--type=merge", f"--patch-file={patch}")
    back, _ = cm_read()
    if back != new_text:
        raise RuntimeError("readback differs from what we wrote")
    ok(f"CM written and read back byte-identical (backup {bak})")


# ------------------------------------------------------------- port-forward

def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def port_forward() -> str:
    port = free_port()
    proc = subprocess.Popen(
        ["kubectl", "-n", NS, "port-forward", f"svc/{DEPLOY}", f"{port}:4000"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    atexit.register(proc.terminate)
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            raise RuntimeError(f"port-forward died: {(proc.stderr.read() or '')[:300]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return base
        except OSError:
            continue
    raise RuntimeError("port-forward never became reachable")


# ------------------------------------------------------------------- probes

def infer(base: str, key: str, model: str) -> tuple[bool, str]:
    """One real inference with a unique nonce. Uniqueness is mandatory:
    LiteLLM's response cache serves byte-identical bodies without ever
    reaching the upstream, which reads as a green that proves nothing."""
    nonce = uuid.uuid4().hex[:12].upper()
    code, body = http(
        f"{base}/v1/chat/completions", key,
        {
            "model": model,
            "messages": [{"role": "user",
                          "content": f"Reply with exactly this string and nothing else: {nonce}"}],
            "max_tokens": 512,  # small caps truncate reasoning models into a false red
        },
    )
    if code != 200:
        snippet = json.dumps(body)[:150] if isinstance(body, dict) else str(body)[:150]
        return False, f"HTTP {code} {snippet}"
    content = (body["choices"][0]["message"].get("content") or "")
    out = body.get("usage", {}).get("completion_tokens")
    if nonce not in content:
        return False, f"200 but nonce not echoed (out={out}, content={content[:60]!r})"
    return True, f"200 out={out} nonce echoed"


def a_carher_key() -> tuple[str, str] | None:
    """A real raw carher key, straight out of a her instance's ConfigMap.
    /spend/keys hands out the sha256 token, and using that as a Bearer gives
    401 -- a wrong-credential error that reads exactly like a broken gate."""
    names = kube(
        "get", "cm", "-o", "custom-columns=:.metadata.name", "--no-headers",
    ).split()
    cms = [n for n in names if re.fullmatch(r"carher-\d+-user-config", n)]
    for name in sorted(cms)[:5]:
        # go-template, not jsonpath: `{.data.openclaw\.json}` returns an empty
        # string for some objects while working for others.
        raw = kube("get", "cm", name, "-o",
                   'go-template={{index .data "openclaw.json"}}',
                   check=False)
        if not raw.strip():
            continue
        try:
            key = json.loads(raw)["models"]["providers"]["litellm"]["apiKey"]
        except Exception:
            continue
        if key:
            return name.replace("-user-config", ""), key
    return None


def psql(sql: str) -> str:
    return kube("exec", DB_POD, "--", "psql", "-U", "litellm", "-d", "litellm",
                "-A", "-F|", "-c", sql)


# ------------------------------------------------------------- 198 bridge key

def bridge_key_info(mk198: str, raw_key: str) -> dict:
    code, body = http(f"{BASE_198}/key/info?key={raw_key}", mk198)
    if code != 200 or not isinstance(body, dict):
        raise RuntimeError(f"198 /key/info -> {code} {str(body)[:200]}")
    return body["info"]


def widen_bridge_key(mk198: str, raw_key: str, upstream: str, apply: bool) -> None:
    inf = bridge_key_info(mk198, raw_key)
    cur = list(inf.get("models") or [])
    if not cur:
        # models == [] is UNRESTRICTED. Writing a list here would REVOKE
        # everything else this key can reach.
        fail("198 bridge key has models==[] (unrestricted) -- refusing to write a list")
        return
    if upstream in cur:
        ok(f"198 bridge key already allows {upstream} ({len(cur)} entries)")
        return
    want = cur + [upstream]
    info(f"198 bridge key models {len(cur)} -> {len(want)} (+{upstream})")
    if not apply:
        return
    # /key/update replaces fields; carry aliases through untouched.
    code, body = http(f"{BASE_198}/key/update", mk198,
                      {"key": raw_key, "models": want})
    if code != 200:
        fail(f"198 /key/update -> {code} {str(body)[:200]}")
        return
    back = bridge_key_info(mk198, raw_key)
    if upstream in (back.get("models") or []):
        ok(f"198 bridge key widened, readback confirms {upstream}")
    else:
        fail("198 /key/update returned 200 but readback lacks the name")


def costs_from_198(mk198: str | None, bridge: str, upstream: str) -> dict[str, float]:
    """Copy pricing off the 198 entry so the two sides cannot drift. Reading
    /model/info works with the bridge key too, so this needs no master key."""
    code, body = http(f"{BASE_198}/model/info", mk198 or bridge)
    if code != 200 or not isinstance(body, dict):
        fail(f"198 /model/info -> {code}; pass costs explicitly")
        return {}
    for m in body.get("data", []):
        if m.get("model_name") != upstream:
            continue
        src = {**(m.get("model_info") or {}), **(m.get("litellm_params") or {})}
        return {k: src[k] for k in COST_KEYS if isinstance(src.get(k), (int, float))}
    fail(f"198 has no model_group named {upstream}")
    return {}


def leg_count_198(bridge: str, upstream: str) -> int | None:
    code, body = http(f"{BASE_198}/model/info", bridge)
    if code != 200 or not isinstance(body, dict):
        return None
    return sum(1 for m in body.get("data", []) if m.get("model_name") == upstream)


# ------------------------------------------------------------------- rollout

def rollout_and_wait(expect_sha: str) -> None:
    """~25 min is normal here: terminationGracePeriodSeconds 600 + nodeAffinity
    on 3 nodes + hostPort 4000 + replicas 2/maxSurge 1 means each new pod waits
    for an old one to fully terminate. `exceeded its progress deadline` is
    expected output, not a failure -- the real criterion is ready>=1 throughout
    plus the sha256 below."""
    kube("rollout", "restart", f"deployment/{DEPLOY}")
    print("  rollout restarted; expect ~25 min. Judging on in-container sha256, not on rollout status.")
    deadline = time.time() + 45 * 60
    while time.time() < deadline:
        time.sleep(30)
        pods = json.loads(kube("get", "pods", "-l", f"app={DEPLOY}", "-o", "json"))
        matched, ready, total = 0, 0, 0
        for p in pods["items"]:
            if p["metadata"].get("deletionTimestamp"):
                continue
            total += 1
            if all(c.get("ready") for c in p["status"].get("containerStatuses") or [{}]):
                ready += 1
                sha = pod_config_sha(p["metadata"]["name"])
                if sha == expect_sha:
                    matched += 1
        print(f"    ready={ready}/{total} carrying_new_config={matched}")
        if total and matched == total and ready == total:
            ok(f"all {total} pods carry /app/config.yaml sha256 {expect_sha[:16]}")
            return
    fail("rollout did not converge on the expected config sha within 45 min")


def pod_config_sha(pod: str) -> str:
    out = kube("exec", pod, "-c", "litellm", "--",
               "sha256sum", "/app/config.yaml", check=False)
    return out.split()[0] if out.split() else ""


# -------------------------------------------------------------------- verify

def verify(public: str, upstream: str, bridge: str) -> None:
    print("\n=== verify ===")
    text, _ = cm_read()
    names = cm_model_names(text)

    wild = wildcard_census(names)
    if wild:
        fail(f"live CM has wildcard model_name(s) {wild} -- pro198 key writes will 403")
    else:
        ok(f"0 wildcard model_name in CM ({len(names)} entries) -- enterprise gate not armed")

    if public not in names:
        fail(f"{public} absent from CM")
        return
    block = text.split(f"- model_name: {public}\n", 1)[1].split("\n- model_name:", 1)[0]
    if f'access_groups: ["{ACCESS_GROUP}"]' in block or f"access_groups: ['{ACCESS_GROUP}']" in block:
        ok(f"{public} carries access_groups {ACCESS_GROUP}")
    else:
        fail(f"{public} lacks access_groups {ACCESS_GROUP} -- keys will 403 on it")
    if f"os.environ/{BRIDGE_ENV}" in block:
        ok(f"{public} uses {BRIDGE_ENV}")
    else:
        fail(f"{public} does not reference {BRIDGE_ENV}")

    expect = hashlib.sha256(text.encode()).hexdigest()
    pods = [p["metadata"]["name"] for p in json.loads(
        kube("get", "pods", "-l", f"app={DEPLOY}", "-o", "json"))["items"]
        if not p["metadata"].get("deletionTimestamp")]
    if not pods:
        fail("0 pods found -- absence is a FAIL, not a pass")
    for p in pods:
        sha = pod_config_sha(p)
        if sha == expect:
            ok(f"{p} config sha matches CM")
        else:
            fail(f"{p} config sha {sha[:16]} != CM {expect[:16]} (stale pod)")

    legs = leg_count_198(bridge, upstream)
    if legs is None:
        info("could not count 198 legs")
    elif legs <= 1:
        info(f"WARNING {upstream} has {legs} leg on 198 and no fallback -- single point")
    else:
        ok(f"{upstream} has {legs} legs on 198")

    base = port_forward()
    mk = master_key_aliyun()

    code, body = http(f"{base}/v1/models", mk)
    ids = [m["id"] for m in body.get("data", [])] if isinstance(body, dict) else []
    if public in ids:
        ok(f"/v1/models lists {public} ({len(ids)} total) -- listed is not healthy, continuing")
    else:
        fail(f"/v1/models does not list {public}")

    good, why = infer(base, mk, public)
    (ok if good else fail)(f"master-key inference on {public}: {why}")

    # The decisive one: a real key whose allowlist contains `pro198` but NOT
    # the model's own name. Green here can only come from the access group.
    picked = a_carher_key()
    if not picked:
        fail("could not obtain a real raw carher key -- access-group leg UNVERIFIED")
    else:
        alias, raw = picked
        code, kinfo = http(f"{base}/key/info?key={raw}", mk)
        models = (kinfo.get("info", {}).get("models") or []) if isinstance(kinfo, dict) else []
        has_group, has_name = ACCESS_GROUP in models, public in models
        info(f"{alias}: {len(models)} models, has_{ACCESS_GROUP}={has_group}, has_{public}={has_name}")
        good, why = infer(base, raw, public)
        if good and has_group and not has_name:
            ok(f"DECISIVE: {alias} reaches {public} via {ACCESS_GROUP} alone ({why})")
        elif good:
            ok(f"{alias} reaches {public} ({why}) -- but it also lists the name, "
               f"so this does not isolate the access group")
        else:
            fail(f"{alias} cannot reach {public}: {why}")

    rows = psql(f"""
        SELECT count(*) FILTER (WHERE '{ACCESS_GROUP}' = ANY(models)) AS with_group,
               count(*) AS scoped,
               count(*) FILTER (WHERE cardinality(models)=0) AS unrestricted
        FROM "LiteLLM_VerificationToken" WHERE key_alias LIKE 'carher-%';""")
    line = [l for l in rows.splitlines() if re.match(r"^\d+\|", l)]
    if not line:
        fail("psql key census returned 0 rows -- ruler failed, not a clean result")
    else:
        with_group, scoped, unres = line[0].split("|")[:3]
        if with_group == scoped and unres == "0":
            ok(f"psql: {with_group}/{scoped} carher-* keys carry {ACCESS_GROUP}, unrestricted=0")
        else:
            fail(f"psql: only {with_group}/{scoped} carry {ACCESS_GROUP}, unrestricted={unres}")

    # Dual column, always: `model_group` is the requested name, `model` is where
    # it landed. An exact match on one column silently halves the row set.
    rows = psql(f"""
        SELECT model_group, model, prompt_tokens, completion_tokens, spend
        FROM "LiteLLM_SpendLogs"
        WHERE model_group ILIKE '%{public}%' OR model ILIKE '%{upstream}%'
        ORDER BY "startTime" DESC LIMIT 3;""")
    data = [l for l in rows.splitlines() if l.count("|") >= 4 and not l.startswith("model_group")]
    if not data:
        fail(f"no SpendLogs row for {public} -- callback layer may not be logging")
    else:
        billed = [l for l in data if l.split("|")[3] not in ("0", "")]
        if billed:
            ok(f"SpendLogs dual-column ok, {len(billed)}/{len(data)} rows with output tokens")
            info(f"latest: {billed[0]}")
            info("spend reconciles as uncached_in*in + cached*cache_read + reasoning*out; "
                 "reasoning tokens bill but are not in completion_tokens")
        else:
            fail("SpendLogs rows exist but all have completion_tokens=0")


# ---------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("plan", "apply", "verify"), default="plan")
    ap.add_argument("--public-name", required=True, help="name her/clients will call, e.g. gemini-3.8-flash")
    ap.add_argument("--upstream-name", required=True, help="model_group on 198, e.g. ag-gemini-3.8-flash")
    ap.add_argument("--skip-198", action="store_true",
                    help="bridge key already allows the upstream name")
    ap.add_argument("--cost", action="append", default=[], metavar="KEY=VALUE",
                    help="override a pricing field instead of copying 198's")
    args = ap.parse_args()

    public, upstream = args.public_name, args.upstream_name
    if "*" in public or "*" in upstream:
        print("refusing: a model_name containing '*' turns pro198 into a wildcard "
              "access group and makes every key write 403 without an enterprise licence")
        return 2

    print(f"=== {args.mode}: {public}  <-  198:{upstream} ===")
    bridge = secret_value(BRIDGE_ENV)
    ok(f"bridge key from secret/{SECRET_NAME}:{BRIDGE_ENV} (sha256 {hashlib.sha256(bridge.encode()).hexdigest()[:12]})")

    if args.mode == "verify":
        verify(public, upstream, bridge)
        return report()

    text, rv = cm_read()
    names = cm_model_names(text)
    wild = wildcard_census(names)
    if wild:
        print(f"  ABORT live CM already contains wildcard model_name {wild}; "
              "adding an access-group entry now would 403 every key write")
        return 2
    ok(f"{len(names)} model_name entries live, 0 wildcards, resourceVersion {rv}")
    if public in names:
        print(f"  ABORT {public} already exists in the CM. Use --mode verify.")
        return 2

    mk198 = os.environ.get("MK198", "").strip()
    costs = costs_from_198(mk198 or None, bridge, upstream)
    for spec in args.cost:
        k, _, v = spec.partition("=")
        costs[k.strip()] = float(v)
    if not costs:
        print("  ABORT no pricing resolved; pass --cost input_cost_per_token=... etc. "
              "Absent pricing bills at LiteLLM defaults, and a literal 0.0 bills as free.")
        return 2
    ok("pricing copied from 198: " + ", ".join(f"{k}={v}" for k, v in costs.items()))

    legs = leg_count_198(bridge, upstream)
    if legs is not None:
        info(f"{upstream} has {legs} leg(s) on 198"
             + (" -- single point, no fallback" if legs <= 1 else ""))

    entry = build_entry(public, upstream, costs)
    new_text = splice(text, entry)
    print("\n--- CM diff (the only change) ---")
    print(entry.rstrip())
    print("---")

    if args.mode == "plan":
        if not args.skip_198:
            if mk198:
                widen_bridge_key(mk198, bridge, upstream, apply=False)
            else:
                info("MK198 not set -- cannot preview the 198 bridge-key widening")
        print("\nplan only, nothing written. Re-run with --mode apply.")
        return report()

    if not args.skip_198:
        if not mk198:
            print("  ABORT --mode apply needs MK198 (198 master key) to widen the bridge key, "
                  "or --skip-198 if you already did it")
            return 2
        widen_bridge_key(mk198, bridge, upstream, apply=True)
        # The 198 leg is probed over the public URL, so no port-forward here.
        good, why = infer(BASE_198, bridge, upstream)
        (ok if good else fail)(f"198 leg alive through the bridge key: {why}")
        if FAIL:
            print("\nstopping before the CM write: the 198 leg is not proven good")
            return report()

    cm_write(new_text, rv)
    rollout_and_wait(hashlib.sha256(new_text.encode()).hexdigest())
    verify(public, upstream, bridge)
    return report()


def report() -> int:
    print()
    if FAIL:
        print(f"=== {len(FAIL)} FAIL ===")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("=== all checks passed ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
