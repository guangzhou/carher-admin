#!/usr/bin/env python3
"""Converge a CarHer instance OpenClaw/Hermes model config to the LiteLLM key allowlist.

Default primary: litellm/gpt-5.6-terra
Chat model catalog (OpenClaw agents.defaults.models + models.providers.litellm.models)
is rewritten to exactly these LiteLLM model_names (unprefixed ids):

  gpt-5.6-sol
  gpt-5.6-terra
  gpt-5.6-luna
  gpt-5.5
  claude-opus-4-8
  claude-sonnet-5
  claude-haiku-4-5
  deepseek-v4-flash
  gemini-3.5-flash
  glm-5
  qwen3.7-plus

Short aliases (requires carher multi-alias patch):
  /gpt               -> litellm/gpt-5.6-terra
  /opus /opus4.8     -> litellm/claude-opus-4-8
  /deepseek /ds /ds-flash /ds-pro -> litellm/deepseek-v4-flash (intranet)
  /gemini /gemini35  -> litellm/gemini-3.5-flash
  /sonnet /haiku /glm /qwen -> allowlisted targets above

bge-m3 and deepseek-v4-pro may remain on the LiteLLM key allowlist, but are not
exposed as chat-selectable Her models. Base-config provider catalogs
(anthropic/wangsu/openrouter opus-4.6 leftovers) are neutralized per-instance.

Also:
  - rewrites session model overrides that still use chatgpt-/wangsu-/openrouter- ids
  - optionally syncs LiteLLM VerificationToken.models to the allowlist
  - optionally patches Hermes config-litellm.yaml when present
  - optionally rollout-restarts the Deployment so sessions/Hermes reload

Note: OpenClaw short-alias maps are loaded into the gateway process. Updating
the ConfigMap alone is not enough for /gpt etc. to take effect — omit
--skip-restart (or restart later) after alias changes.

Dry-run by default. Pass --apply to write.

Examples (run on k8s-work-226 with kubeconfig):

  python3 carher_her_litellm_allowlist_models.py --uids 1000
  python3 carher_her_litellm_allowlist_models.py --uids 266,268 --apply
  python3 carher_her_litellm_allowlist_models.py --uids 266,268 --apply --skip-restart
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

ALLOWED_KEY_MODELS = [
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-astra",
    "gpt-5.5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "gemini-3.5-flash",
    "glm-5",
    "qwen3.7-plus",
    "bge-m3",
]

# Chat-selectable models. deepseek-v4-pro stays on the LiteLLM key allowlist
# but is not exposed in the Her model picker; all deepseek short aliases point
# at the intranet deepseek-v4-flash deployment.
CHAT_MODELS = [
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-astra",
    "gpt-5.5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "deepseek-v4-flash",
    "gemini-3.5-flash",
    "glm-5",
    "qwen3.7-plus",
]

# OpenClaw multi-alias patch supports string | string[].
# Keep short names (/gpt, /opus, /deepseek, /gemini) on allowlisted LiteLLM ids
# so base-config leftovers (opus-4.6 / wangsu-deepseek / openrouter) cannot win.
ALIASES: dict[str, str | list[str]] = {
    "gpt-5.6-sol": ["gpt-5.6-sol"],
    "gpt-5.6-terra": ["gpt", "gpt-5.6-terra"],
    "gpt-5.6-luna": ["gpt-5.6-luna"],
    "gpt-6-astra": ["gpt-6-astra"],
    "gpt-5.5": ["gpt-5.5"],
    "claude-opus-4-8": ["opus", "opus4.8"],
    "claude-sonnet-5": ["sonnet"],
    "claude-haiku-4-5": ["haiku"],
    "deepseek-v4-flash": ["deepseek", "ds", "ds-flash", "ds-pro"],
    "gemini-3.5-flash": ["gemini", "gemini35"],
    "glm-5": ["glm"],
    "qwen3.7-plus": ["qwen"],
}

DISPLAY = {
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-5.5": "GPT-5.5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-haiku-4-5": "Claude Haiku 4.5",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
    "gemini-3.5-flash": "Gemini 3.5 Flash",
    "glm-5": "GLM-5",
    "qwen3.7-plus": "Qwen3.7 Plus",
}

CONTEXT = {
    "gpt-5.6-sol": 272000,
    "gpt-5.6-terra": 272000,
    "gpt-5.6-luna": 272000,
    "gpt-6-astra": 1050000,
    "gpt-5.5": 272000,
    "claude-opus-4-8": 1000000,
    "claude-sonnet-5": 1000000,
    "claude-haiku-4-5": 272000,
    "deepseek-v4-pro": 128000,
    "deepseek-v4-flash": 128000,
    "gemini-3.5-flash": 1000000,
    "glm-5": 128000,
    "qwen3.7-plus": 1000000,
}

PRIMARY = "litellm/gpt-5.6-terra"
PRIMARY_PLAIN = "gpt-5.6-terra"

SESSION_REWRITE = {
    "chatgpt-gpt-5.6-terra": "gpt-5.6-terra",
    "chatgpt-gpt-5.6-sol": "gpt-5.6-sol",
    "chatgpt-gpt-5.6-luna": "gpt-5.6-luna",
    "chatgpt-gpt-6-astra": "gpt-6-astra",
    "gpt-6-astra": "gpt-6-astra",
    "chatgpt-gpt-5.5": "gpt-5.5",
    "chatgpt-gpt-5.4": "gpt-5.5",
    "chatgpt-gpt-5.3-codex": "gpt-5.5",
    "gpt-5.4": "gpt-5.5",
    "openai/gpt-5.4": "gpt-5.5",
    "claude-opus-4-6": "claude-opus-4-8",
    "claude-opus-4-7": "claude-opus-4-8",
    "anthropic/claude-opus-4.6": "claude-opus-4-8",
    "anthropic/claude-opus-4-6": "claude-opus-4-8",
    "openrouter-claude-opus-4-8": "claude-opus-4-8",
    "wangsu-deepseek-v4-pro": "deepseek-v4-flash",
    "wangsu-deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek-v4-flash",
    "ds-pro": "deepseek-v4-flash",
    "ds-flash": "deepseek-v4-flash",
    "deepseek": "deepseek-v4-flash",
    "ds": "deepseek-v4-flash",
    "gpt": "gpt-5.6-terra",
    "opus": "claude-opus-4-8",
    "opus4.6": "claude-opus-4-8",
    "opus4.7": "claude-opus-4-8",
    "opus4.8": "claude-opus-4-8",
    "claude-opus-4.6": "claude-opus-4-8",
    "claude-opus-4.8": "claude-opus-4-8",
    "wangsu-gemini-3.5-flash": "gemini-3.5-flash",
    "gemini-3.1-pro-preview": "gemini-3.5-flash",
    "wangsu-glm-5.1": "glm-5",
    "glm-5.1": "glm-5",
    "minimax-m2.7": "gpt-5.6-terra",
}
for _k, _v in list(SESSION_REWRITE.items()):
    SESSION_REWRITE[f"litellm/{_k}"] = f"litellm/{_v}" if not _v.startswith("litellm/") else _v


def kubectl(
    args: list[str],
    *,
    kubeconfig: str | None,
    namespace: str,
    input_text: str | None = None,
    check: bool = True,
) -> str:
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += ["-n", namespace, *args]
    proc = subprocess.run(cmd, input=input_text, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(args[:4])} failed: {(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return proc.stdout


def parse_uids(raw: str) -> list[str]:
    uids: list[str] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit():
            raise SystemExit(f"invalid uid: {part}")
        uids.append(part)
    if not uids:
        raise SystemExit("no uids provided")
    return uids


def load_cm(uid: str, kubeconfig: str | None, namespace: str) -> dict[str, Any]:
    raw = kubectl(
        [
            "get",
            "cm",
            f"carher-{uid}-user-config",
            "-o",
            "jsonpath={.data.openclaw\\.json}",
        ],
        kubeconfig=kubeconfig,
        namespace=namespace,
    )
    return json.loads(raw)


def summarize(cfg: dict[str, Any]) -> None:
    ad = (cfg.get("agents") or {}).get("defaults") or {}
    print("  primary", ad.get("model"))
    ms = ad.get("models") or {}
    print("  defaults.models:")
    for k, v in sorted(ms.items()):
        alias = (v or {}).get("alias") if isinstance(v, dict) else v
        print(f"    {k} -> {alias}")
    provs = ((cfg.get("models") or {}).get("providers") or {})
    litellm = provs.get("litellm") or {}
    ids = [m.get("id") for m in (litellm.get("models") or []) if isinstance(m, dict)]
    print("  provider.litellm.models", ids)
    for stale in ("anthropic", "wangsu", "openrouter"):
        models = (provs.get(stale) or {}).get("models")
        if models is None:
            print(f"  provider.{stale}: (absent)")
        else:
            print(f"  provider.{stale}.models count={len(models)}")


SHORT_ALIAS_EXPECT = {
    "gpt": "litellm/gpt-5.6-terra",
    "opus": "litellm/claude-opus-4-8",
    "opus4.8": "litellm/claude-opus-4-8",
    "deepseek": "litellm/deepseek-v4-flash",
    "ds": "litellm/deepseek-v4-flash",
    "ds-flash": "litellm/deepseek-v4-flash",
    "ds-pro": "litellm/deepseek-v4-flash",
    "sonnet": "litellm/claude-sonnet-5",
    "haiku": "litellm/claude-haiku-4-5",
    "gemini": "litellm/gemini-3.5-flash",
    "gemini35": "litellm/gemini-3.5-flash",
    "glm": "litellm/glm-5",
    "qwen": "litellm/qwen3.7-plus",
}


def verify_short_aliases(cfg: dict[str, Any]) -> bool:
    ms = ((cfg.get("agents") or {}).get("defaults") or {}).get("models") or {}
    idx: dict[str, str] = {}
    for k, v in ms.items():
        a = (v or {}).get("alias") if isinstance(v, dict) else None
        if isinstance(a, str):
            idx[a] = k
        elif isinstance(a, list):
            for x in a:
                idx[str(x)] = k
    bad = False
    print("  short-alias regression:")
    for alias, want in SHORT_ALIAS_EXPECT.items():
        got = idx.get(alias)
        ok = got == want
        print(f"    {'OK' if ok else 'BAD'} /{alias} -> {got}")
        if not ok:
            bad = True
    stale_prov = False
    provs = ((cfg.get("models") or {}).get("providers") or {})
    for name in ("anthropic", "wangsu", "openrouter"):
        models = (provs.get(name) or {}).get("models")
        if models:
            stale_prov = True
            print(f"    BAD provider.{name} still non-empty ({len(models)})")
    if not bad and not stale_prov:
        print("    RESULT PASS")
        return True
    print("    RESULT FAIL")
    return False


def transform_openclaw(cfg: dict[str, Any]) -> dict[str, Any]:
    ad = cfg.setdefault("agents", {}).setdefault("defaults", {})
    ad["model"] = {"primary": PRIMARY}
    ad["models"] = {f"litellm/{name}": {"alias": ALIASES[name]} for name in CHAT_MODELS}

    models_root = cfg.setdefault("models", {})
    providers = models_root.setdefault("providers", {})
    litellm = providers.setdefault("litellm", {})
    if not litellm.get("baseUrl"):
        litellm["baseUrl"] = "http://litellm-proxy.carher.svc:4000"
    old_catalog = {
        m.get("id"): m
        for m in (litellm.get("models") or [])
        if isinstance(m, dict) and m.get("id")
    }
    new_catalog = []
    for name in CHAT_MODELS:
        old = old_catalog.get(name) or old_catalog.get(f"chatgpt-{name}") or {}
        new_catalog.append(
            {
                "id": name,
                "name": DISPLAY[name],
                "api": old.get("api") or "openai-completions",
                "reasoning": True,
                "input": old.get("input") or ["text", "image"],
                "contextWindow": CONTEXT[name],
                "maxTokens": old.get("maxTokens") or min(CONTEXT[name], 128000),
                "cost": old.get("cost") or {"input": 0, "output": 0, "cacheRead": 0},
            }
        )
    litellm["models"] = new_catalog

    # Neutralize base-config provider catalogs that still advertise opus-4.6 /
    # wangsu-deepseek / openrouter leftovers. Deep-merge replaces these arrays
    # when the path is present in user-config.
    for stale in ("anthropic", "wangsu", "openrouter"):
        entry = providers.get(stale)
        if not isinstance(entry, dict):
            providers[stale] = {"models": []}
        else:
            entry["models"] = []
    return cfg


def rewrite_sessions(uid: str, kubeconfig: str | None, namespace: str, apply: bool) -> int:
    vol = kubectl(
        ["get", "pvc", f"carher-{uid}-data", "-o", "jsonpath={.spec.volumeName}"],
        kubeconfig=kubeconfig,
        namespace=namespace,
    ).strip()
    sess_path = Path(f"/Data/{vol}/agents/main/sessions/sessions.json")
    if not sess_path.exists():
        print("  sessions: missing", sess_path)
        return 0
    raw = sess_path.read_text()
    data = json.loads(raw)
    entries = data.get("sessions") if isinstance(data, dict) and "sessions" in data else data
    allowed = set(ALLOWED_KEY_MODELS)
    changed = 0
    for ent in entries.values() if isinstance(entries, dict) else []:
        if not isinstance(ent, dict):
            continue
        for fld in ("model", "modelOverride", "providerModel"):
            v = ent.get(fld)
            if not isinstance(v, str):
                continue
            if v in SESSION_REWRITE:
                ent[fld] = SESSION_REWRITE[v]
                changed += 1
                continue
            plain = v.split("/", 1)[-1].replace("chatgpt-", "")
            if plain in allowed:
                target = plain
                if v.startswith("litellm/"):
                    target = f"litellm/{plain}"
                if v != target:
                    ent[fld] = target
                    changed += 1
                continue
            ent[fld] = PRIMARY_PLAIN
            changed += 1
    print(f"  sessions rewrite fields={changed} path={sess_path}")
    if apply and changed:
        bak = sess_path.with_suffix(".json.bak-litellm-allowlist")
        if not bak.exists():
            bak.write_text(raw)
            print("  sessions backup", bak)
        sess_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return changed


def sync_key_allowlist(uid: str, kubeconfig: str | None, namespace: str, apply: bool) -> None:
    row = subprocess.run(
        [
            "kubectl",
            *(["--kubeconfig", kubeconfig] if kubeconfig else []),
            "-n",
            namespace,
            "exec",
            "litellm-db-0",
            "--",
            "psql",
            "-U",
            "litellm",
            "-d",
            "litellm",
            "-t",
            "-A",
            "-c",
            f"SELECT models::text FROM \"LiteLLM_VerificationToken\" WHERE key_alias='carher-{uid}';",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    current = [m for m in row.strip("{}").split(",") if m]
    print("  key allowlist before", current)
    if set(current) == set(ALLOWED_KEY_MODELS):
        print("  key allowlist already exact")
        return
    if not apply:
        print("  key allowlist would sync ->", ALLOWED_KEY_MODELS)
        return
    mk = subprocess.run(
        "kubectl "
        + (f"--kubeconfig {kubeconfig} " if kubeconfig else "")
        + f"-n {namespace} get secret litellm-secrets "
        + "-o jsonpath={.data.LITELLM_MASTER_KEY} | base64 -d",
        shell=True,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    plain = kubectl(
        ["get", "her", f"her-{uid}", "-o", "jsonpath={.spec.litellmKey}"],
        kubeconfig=kubeconfig,
        namespace=namespace,
    ).strip()
    svc = kubectl(
        ["get", "svc", "litellm-proxy", "-o", "jsonpath={.spec.clusterIP}"],
        kubeconfig=kubeconfig,
        namespace=namespace,
    ).strip()
    req = urllib.request.Request(
        f"http://{svc}:4000/key/update",
        data=json.dumps({"key": plain, "models": ALLOWED_KEY_MODELS}).encode(),
        headers={"Authorization": f"Bearer {mk}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("  key/update", resp.status)
        resp.read()


def patch_hermes(uid: str, kubeconfig: str | None, namespace: str, apply: bool) -> None:
    if not apply:
        print("  hermes: would patch if present")
        return
    pod = kubectl(
        [
            "get",
            "pod",
            "-l",
            f"user-id={uid}",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        kubeconfig=kubeconfig,
        namespace=namespace,
        check=False,
    ).strip()
    if not pod:
        print("  hermes: no pod")
        return
    helper = Path(f"/tmp/patch-hermes-{uid}.py")
    helper.write_text(
        f"""#!/usr/bin/env python3
import json
from pathlib import Path
try:
    import yaml
except Exception as e:
    print("NO_YAML", e)
    raise SystemExit(0)
ALLOWED = {json.dumps(CHAT_MODELS)}
CONTEXT = {json.dumps(CONTEXT)}
for p in ("/opt/data/.hermes/config-litellm.yaml", "/data/.hermes/config-litellm.yaml"):
    path = Path(p)
    if not path.exists():
        print("missing", p)
        continue
    d = yaml.safe_load(path.read_text()) or {{}}
    models = {{name: {{"context_length": CONTEXT[name]}} for name in ALLOWED}}
    d.setdefault("model", {{}})["default"] = "gpt-5.6-terra"
    d.setdefault("model", {{}})["provider"] = "litellm"
    prov = d.setdefault("providers", {{}}).setdefault("litellm", {{}})
    prov["default_model"] = "gpt-5.6-terra"
    prov["models"] = models
    for cp in d.get("custom_providers") or []:
        if isinstance(cp, dict) and cp.get("name") == "litellm":
            cp["default_model"] = "gpt-5.6-terra"
            cp["model"] = "gpt-5.6-terra"
            cp["models"] = models
    d["quick_commands"] = {{
        name: {{"type": "alias", "target": f"/model {{name}} --provider litellm --global"}}
        for name in models
    }}
    path.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
    print("hermes patched", p, "models", sorted(models))
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "kubectl",
            *(["--kubeconfig", kubeconfig] if kubeconfig else []),
            "-n",
            namespace,
            "cp",
            str(helper),
            f"{pod}:/tmp/patch-hermes-{uid}.py",
            "-c",
            "carher",
        ],
        check=False,
    )
    out = kubectl(
        ["exec", pod, "-c", "carher", "--", "python3", f"/tmp/patch-hermes-{uid}.py"],
        kubeconfig=kubeconfig,
        namespace=namespace,
        check=False,
    )
    print("  hermes:", out.strip() or "(no output)")


def ensure_crd_model(uid: str, kubeconfig: str | None, namespace: str, apply: bool) -> None:
    model = kubectl(
        ["get", "her", f"her-{uid}", "-o", "jsonpath={.spec.model}"],
        kubeconfig=kubeconfig,
        namespace=namespace,
    ).strip()
    print("  crd.model", model)
    if model == PRIMARY_PLAIN:
        return
    if not apply:
        print(f"  crd.model would patch -> {PRIMARY_PLAIN}")
        return
    kubectl(
        [
            "patch",
            "her",
            f"her-{uid}",
            "--type=merge",
            "-p",
            json.dumps({"spec": {"model": PRIMARY_PLAIN}}),
        ],
        kubeconfig=kubeconfig,
        namespace=namespace,
    )
    print(f"  crd.model patched -> {PRIMARY_PLAIN}")


def apply_cm(uid: str, cfg: dict[str, Any], kubeconfig: str | None, namespace: str) -> None:
    tmp = Path(f"/tmp/oc-{uid}-litellm-allowlist.json")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    yaml = kubectl(
        [
            "create",
            "cm",
            f"carher-{uid}-user-config",
            f"--from-file=openclaw.json={tmp}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        kubeconfig=kubeconfig,
        namespace=namespace,
    )
    print(
        " ",
        kubectl(
            ["apply", "-f", "-"],
            kubeconfig=kubeconfig,
            namespace=namespace,
            input_text=yaml,
        ).strip(),
    )


def restart(uid: str, kubeconfig: str | None, namespace: str) -> None:
    print(
        " ",
        kubectl(
            ["rollout", "restart", f"deploy/carher-{uid}"],
            kubeconfig=kubeconfig,
            namespace=namespace,
        ).strip(),
    )
    print(
        " ",
        kubectl(
            ["rollout", "status", f"deploy/carher-{uid}", "--timeout=300s"],
            kubeconfig=kubeconfig,
            namespace=namespace,
        ).strip(),
    )


def process_uid(
    uid: str,
    *,
    kubeconfig: str | None,
    namespace: str,
    apply: bool,
    sync_key: bool,
    do_restart: bool,
) -> None:
    print(f"=== carher-{uid} ===")
    cfg = load_cm(uid, kubeconfig, namespace)
    print("BEFORE")
    summarize(cfg)
    new_cfg = transform_openclaw(json.loads(json.dumps(cfg)))
    print("AFTER(plan)")
    summarize(new_cfg)
    ensure_crd_model(uid, kubeconfig, namespace, apply)
    if sync_key:
        sync_key_allowlist(uid, kubeconfig, namespace, apply)
    rewrite_sessions(uid, kubeconfig, namespace, apply)
    if apply:
        bak = Path(f"/tmp/carher-{uid}-user-config.bak-litellm-allowlist.json")
        bak.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        print("  cm backup", bak)
        apply_cm(uid, new_cfg, kubeconfig, namespace)
        patch_hermes(uid, kubeconfig, namespace, apply=True)
        if do_restart:
            restart(uid, kubeconfig, namespace)
            live_pod = kubectl(
                [
                    "get",
                    "pod",
                    "-l",
                    f"user-id={uid}",
                    "-o",
                    "jsonpath={.items[0].metadata.name}",
                ],
                kubeconfig=kubeconfig,
                namespace=namespace,
            ).strip()
            live = json.loads(
                kubectl(
                    ["exec", live_pod, "-c", "carher", "--", "cat", "/data/.openclaw/openclaw.json"],
                    kubeconfig=kubeconfig,
                    namespace=namespace,
                )
            )
            print("LIVE")
            summarize(live)
            verify_short_aliases(live)
            print(
                "  status",
                kubectl(
                    [
                        "get",
                        "her",
                        f"her-{uid}",
                        "-o",
                        "jsonpath={.status.phase} {.status.feishuWS}",
                    ],
                    kubeconfig=kubeconfig,
                    namespace=namespace,
                ),
            )
    else:
        print("  dry-run only (pass --apply to write)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uids", required=True, help="comma-separated her uids, e.g. 266,268")
    ap.add_argument("--namespace", default="carher")
    ap.add_argument("--kubeconfig", default="/root/.kube/config")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--skip-key-sync", action="store_true")
    ap.add_argument("--skip-restart", action="store_true")
    args = ap.parse_args()

    uids = parse_uids(args.uids)
    for uid in uids:
        process_uid(
            uid,
            kubeconfig=args.kubeconfig,
            namespace=args.namespace,
            apply=args.apply,
            sync_key=not args.skip_key_sync,
            do_restart=not args.skip_restart,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
