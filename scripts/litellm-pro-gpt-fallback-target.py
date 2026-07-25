#!/usr/bin/env python3
"""Set the GPT-family fallback target chain on 198 litellm-product.

Generalises scripts/litellm-pro-carher-gpt-local-deepseek-fallback.py, which
hardcoded two wrong assumptions discovered 2026-07-21:

  1. It required the fallback target to be ``mode: responses`` in /model/info.
     WRONG for OpenRouter: ``deepseek-v4-pro`` is ``mode: chat`` and serves
     GPT (Responses API) fallbacks via LiteLLM's responses->chat *bridge*.
     A ``mode: responses`` group pointed at OpenRouter hits the provider's
     native ``/responses`` endpoint (which does not exist) -> 404 -> cooldown.
  2. It only ever set a single-element fallback list.

This script instead:
  * accepts an ORDERED list of targets (``--targets A B ...``);
  * validates every target with a real ``POST /v1/responses`` probe -- a 200
    is the only proof that the target can actually serve a GPT fallback,
    whether natively (local vLLM) or bridged (OpenRouter chat group);
  * writes fallbacks to the DB via ``/config/update`` (authoritative, because
    STORE_MODEL_IN_DB overrides the ConfigMap -- see
    [[feedback_litellm_db_overrides_cm_fallbacks]]);
  * syncs the ConfigMap and regenerates the drifted manifest for backup;
  * rolls the deployment and verifies via /get/config/callbacks + a header probe.

Default is dry-run. Remote work runs on AIYJY-litellm through scripts/jms.

Examples:
  # inspect current gpt fallbacks + probe candidate targets (no writes)
  scripts/litellm-pro-gpt-fallback-target.py \
      --targets deepseek-v4-pro local-deepseek-v4-flash-responses

  # apply: openrouter primary, local secondary
  scripts/litellm-pro-gpt-fallback-target.py \
      --targets deepseek-v4-pro local-deepseek-v4-flash-responses --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# The 16 GPT-family model groups whose fallback we manage. Kept explicit so we
# never touch the ~20 non-GPT entries (claude/gemini/glm/...) that also fall
# back to local deepseek.
GPT_GROUPS = [
    "gpt-5.5", "chatgpt-gpt-5.5",
    "gpt-5.4", "chatgpt-gpt-5.4", "gpt-5.4-mini",
    "gpt-5.2",
    "gpt-5.3-codex", "chatgpt-gpt-5.3-codex", "chatgpt-gpt-5.3-codex-spark",
    "chatgpt-pool-gpt-5.5",
    "gpt-5.6-sol", "chatgpt-gpt-5.6-sol",
    "gpt-5.6-terra", "chatgpt-gpt-5.6-terra",
    "gpt-5.6-luna", "chatgpt-gpt-5.6-luna",
]

BEGIN = "__GPT_FALLBACK_TARGET_BEGIN__"
END = "__GPT_FALLBACK_TARGET_END__"

REMOTE_SCRIPT = r'''
from __future__ import annotations
import argparse, base64, copy, json, subprocess, sys, time, urllib.error, urllib.request

GPT_GROUPS = __GPT_GROUPS__
BEGIN = "__GPT_FALLBACK_TARGET_BEGIN__"
END = "__GPT_FALLBACK_TARGET_END__"


def sh(a, input_text=None):
    return subprocess.check_output(a, input=input_text, text=True)


def http(method, url, key, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            t = r.read().decode(errors="replace")
            return r.status, {k.lower(): v for k, v in dict(r.headers).items()}, t
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in dict(e.headers).items()}, e.read().decode(errors="replace")
    except Exception as e:
        return 0, {}, str(e)


def master_key(ns, secret):
    raw = sh(["kubectl", "get", "secret", secret, "-n", ns, "-o",
              "jsonpath={.data.LITELLM_MASTER_KEY}"])
    return base64.b64decode(raw).decode()


def fb_get(base, mk):
    st, _, t = http("GET", f"{base}/get/config/callbacks", mk, timeout=60)
    if st != 200:
        raise SystemExit(f"/get/config/callbacks -> {st}: {t[:300]}")
    return json.loads(t)["router_settings"]["fallbacks"]


def model_names(base, mk):
    st, _, t = http("GET", f"{base}/v1/model/info", mk, timeout=60)
    if st != 200:
        raise SystemExit(f"/v1/model/info -> {st}: {t[:300]}")
    return {m["model_name"] for m in json.loads(t)["data"] if m.get("model_name")}


def probe_responses(base, mk, model):
    """A 200 proves the group can serve a GPT (Responses API) fallback."""
    body = {"model": model, "input": "reply one word: ok", "max_output_tokens": 64}
    st, _, t = http("POST", f"{base}/v1/responses", mk, body, timeout=90)
    err = ""
    if st != 200:
        try:
            err = str(json.loads(t).get("error"))[:200]
        except Exception:
            err = t[:200]
    return {"model": model, "http": st, "ok": st == 200, "error": err}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="litellm-product")
    p.add_argument("--configmap", default="litellm-config")
    p.add_argument("--nodeport", default="30402")
    p.add_argument("--master-secret", default="litellm-secrets")
    p.add_argument("--manifest", default="/root/litellm-product-manifests/30-cm-litellm-config.yaml")
    p.add_argument("--targets", nargs="+", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--no-restart", action="store_true")
    a = p.parse_args()

    base = f"http://localhost:{a.nodeport}"
    mk = master_key(a.namespace, a.master_secret)
    names = model_names(base, mk)
    fb = fb_get(base, mk)

    res = {"mode": "apply" if a.apply else "dry-run", "status": "PASS",
           "targets": a.targets, "errors": [], "probes": [], "changes": []}

    # 1) every target must exist AND serve a /v1/responses probe with 200.
    for tgt in a.targets:
        if tgt not in names:
            res["errors"].append(f"target missing from /v1/model/info: {tgt}")
            continue
        pr = probe_responses(base, mk, tgt)
        res["probes"].append(pr)
        if not pr["ok"]:
            res["errors"].append(
                f"target failed /v1/responses probe (http={pr['http']}): {tgt} "
                f"-- would 404/cooldown as a GPT fallback. {pr['error']}")

    # 2) build the desired fallback list (only the 16 GPT groups change).
    cur = {list(e.keys())[0]: e[list(e.keys())[0]] for e in fb}
    for g in GPT_GROUPS:
        old = cur.get(g)
        if old != a.targets:
            res["changes"].append(f"{g}: {old} -> {a.targets}")

    if res["errors"]:
        res["status"] = "FAIL"
    if not a.apply or res["errors"]:
        print(BEGIN); print(json.dumps(res, ensure_ascii=False, indent=2)); print(END)
        return 1 if res["status"] != "PASS" else 0

    # ---- apply ----
    new_fb = []
    for e in fb:
        k = list(e.keys())[0]
        new_fb.append({k: list(a.targets)} if k in GPT_GROUPS else e)

    # 2a) authoritative: DB via /config/update (deep-merges router_settings).
    st, _, t = http("POST", f"{base}/config/update", mk,
                    {"router_settings": {"fallbacks": new_fb}}, timeout=90)
    res["db_update_http"] = st
    if st != 200:
        res["status"] = "FAIL"; res["errors"].append(f"/config/update -> {st}: {t[:300]}")
        print(BEGIN); print(json.dumps(res, ensure_ascii=False, indent=2)); print(END)
        return 1

    # 2b) ConfigMap留底 + manifest regen (yaml round-trip; config has no anchors).
    import yaml
    cm = json.loads(sh(["kubectl", "get", "cm", a.configmap, "-n", a.namespace, "-o", "json"]))
    cfg = yaml.safe_load(cm["data"]["config.yaml"])
    rs = cfg.setdefault("router_settings", {})
    rs["fallbacks"] = new_fb
    cm["data"]["config.yaml"] = yaml.safe_dump(cfg, default_flow_style=False,
                                               allow_unicode=True, sort_keys=False)
    open("/tmp/gpt-fb-config.yaml", "w").write(cm["data"]["config.yaml"])
    gen = sh(["kubectl", "create", "configmap", a.configmap, "-n", a.namespace,
              "--from-file=config.yaml=/tmp/gpt-fb-config.yaml",
              "--dry-run=client", "-o", "yaml"])
    sh(["kubectl", "apply", "-n", a.namespace, "-f", "-"], input_text=gen)
    for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields", "generation"):
        cm["metadata"].pop(k, None)
    cm["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    cm.pop("status", None)
    try:
        clean = sh(["kubectl", "create", "--dry-run=client", "-o", "yaml", "-f", "-"],
                   input_text=json.dumps(cm))
        subprocess.run(["cp", a.manifest, a.manifest + ".bak"], check=False)
        open(a.manifest, "w").write(clean)
        res["manifest"] = "regenerated"
    except Exception as e:
        res["manifest"] = f"skip: {e}"

    # 2c) rollout so the router reloads router_settings from the DB.
    if not a.no_restart:
        sh(["kubectl", "rollout", "restart", f"deployment/litellm-proxy", "-n", a.namespace])
        sh(["kubectl", "rollout", "status", f"deployment/litellm-proxy", "-n", a.namespace,
            "--timeout=360s"])
    time.sleep(5)

    # 3) verify: DB reloaded + a live header probe on gpt-5.5.
    after = {list(e.keys())[0]: e[list(e.keys())[0]] for e in fb_get(base, mk)}
    res["verify_all_match"] = all(after.get(g) == a.targets for g in GPT_GROUPS)
    _, h, _ = http("POST", f"{base}/v1/responses", mk,
                   {"model": "gpt-5.5", "input": "reply one word: ok", "max_output_tokens": 200})
    res["gpt55_probe"] = {"model-id": h.get("x-litellm-model-id"),
                          "model-group": h.get("x-litellm-model-group"),
                          "attempted-fallbacks": h.get("x-litellm-attempted-fallbacks")}
    if not res["verify_all_match"]:
        res["status"] = "FAIL"
    print(BEGIN); print(json.dumps(res, ensure_ascii=False, indent=2)); print(END)
    return 0 if res["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jms", default=None, help="path to scripts/jms")
    p.add_argument("--asset", default="AIYJY-litellm")
    p.add_argument("--namespace", default="litellm-product")
    p.add_argument("--configmap", default="litellm-config")
    p.add_argument("--nodeport", default="30402")
    p.add_argument("--master-secret", default="litellm-secrets")
    p.add_argument("--manifest", default="/root/litellm-product-manifests/30-cm-litellm-config.yaml")
    p.add_argument("--targets", nargs="+", required=True,
                   help="ordered fallback targets, e.g. deepseek-v4-pro local-deepseek-v4-flash-responses")
    p.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    p.add_argument("--no-restart", action="store_true")
    return p.parse_args()


def extract(stdout: str) -> dict[str, Any]:
    if BEGIN not in stdout or END not in stdout:
        raise RuntimeError("remote output missing result markers:\n" + stdout[-2000:])
    s = stdout.index(BEGIN) + len(BEGIN)
    return json.loads(stdout[s:stdout.index(END, s)].strip())


def main() -> int:
    a = parse_args()
    repo = Path(__file__).resolve().parents[1]
    jms = a.jms or str(repo / "scripts" / "jms")
    remote_path = f"/tmp/gpt-fallback-target-{os.getpid()}.py"

    body = (REMOTE_SCRIPT
            .replace("__GPT_GROUPS__", json.dumps(GPT_GROUPS))
            .replace("__GPT_FALLBACK_TARGET_BEGIN__", BEGIN)
            .replace("__GPT_FALLBACK_TARGET_END__", END))
    remote_args = ["--namespace", a.namespace, "--configmap", a.configmap,
                   "--nodeport", a.nodeport, "--master-secret", a.master_secret,
                   "--manifest", a.manifest, "--targets", *a.targets]
    if a.apply:
        remote_args.append("--apply")
    if a.no_restart:
        remote_args.append("--no-restart")

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
        f.write(body)
        local_path = f.name
    try:
        subprocess.check_call([jms, "scp", local_path, f"{a.asset}:{remote_path}"])
        cmd = " ".join(["python3", shlex.quote(remote_path),
                        *[shlex.quote(x) for x in remote_args]])
        proc = subprocess.run([jms, "ssh", a.asset, cmd], text=True, capture_output=True)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr, end="")
        result = extract(proc.stdout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return proc.returncode
    finally:
        subprocess.run([jms, "ssh", a.asset, f"rm -f {shlex.quote(remote_path)}"], check=False)
        os.unlink(local_path)


if __name__ == "__main__":
    sys.exit(main())
