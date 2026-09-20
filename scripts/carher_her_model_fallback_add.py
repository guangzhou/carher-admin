#!/usr/bin/env python3
"""Append a fallback model to CarHer Her instances' user-config.

Her-side fallback lives in `agents.defaults.model.fallbacks` inside the
per-instance `carher-<uid>-user-config` ConfigMap. It hot-reloads in ~15s with
zero pod restarts, which makes it the cheap way to stop a Her from sitting on a
single upstream.

READ THIS BEFORE PROMISING ANYTHING TO A USER
---------------------------------------------
OpenClaw has exactly ONE fallback chain and it is NOT per-model
(`docs/concepts/model-failover.md` inside the pod):

  * The chain is bound to the *configured default primary*. Runs started from
    the default primary, a cron job primary, an agent primary with its own
    `fallbacks`, or an auto-fallback override can walk it.
  * A model the user picked explicitly (`/model`, the model picker,
    `session_status(model=...)`, `sessions.patch`) is STRICT: it fails loudly
    instead of falling back. No config knob changes that.

So "give every model a fallback" is NOT achievable here. The only surface that
is per-model is LiteLLM `router_settings.fallbacks`, which is keyed by model
group and therefore global -- every instance sharing that group moves with it.
On Aliyun that file is YAML-only (the `wipe-db-config-rows` initContainer
DELETEs the `router_settings` row on every pod start), so it means CM edit plus
rollout, not a hot patch.

Gates encoded here, each one paid for by a real incident:

  preflight  - refuses to proceed unless the fallback target is in the key's
               allowlist AND answers a live tools-bearing probe, non-streaming
               and streaming, through the instance's own key. "Configured" is
               not "serving": Aliyun once had 15 gpt groups falling back to a
               `mode:responses` entry that 400s on every tools payload, so the
               whole safety net was dead and nobody noticed.
  add        - append-only. The existing chain is never replaced (a switch that
               silently dropped the old fallback is what put a fleet on a single
               self-hosted box), and a path-level leaf diff guard aborts on any
               removal or in-place change.
  verify     - asks the engine, not the file: `openclaw models list` must tag the
               model `fallback#N`. A file on disk proves nothing about what the
               gateway loaded.
  rollback   - restores the timestamped backup written by `add`.

contextWindow is never guessed. If `/model/info` reports `max_input_tokens:
null`, you must pass `--context-window` yourself, and you should pass a
conservative number: declaring it too large means the fallback attempt dies on a
context-overflow error, and overflow errors explicitly do NOT advance the chain,
so the user eats the error instead of getting a degraded answer.

Everything defaults to a dry run; pass --apply to write.

Typical run (see skills/carher-instance-config-override "加 fallback 模型"):

    ./carher_her_model_fallback_add.py preflight --uids 1000 --model grok-4.6
    ./carher_her_model_fallback_add.py add --uids 1000 --model grok-4.6 \
        --context-window 200000 --max-tokens 64000 --apply
    ./carher_her_model_fallback_add.py verify --uids 1000 --model grok-4.6
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Iterator

PROVIDER = "litellm"
FALLBACK_PATH = "/agents/defaults/model/fallbacks"


# --------------------------------------------------------------------------- #
# kubectl plumbing
# --------------------------------------------------------------------------- #


def kubectl(
    args: list[str],
    kubeconfig: str | None,
    namespace: str,
    *,
    input_text: str | None = None,
    check: bool = True,
) -> str:
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += ["-n", namespace, *args]
    proc = subprocess.run(cmd, input=input_text, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


# --------------------------------------------------------------------------- #
# cluster state
# --------------------------------------------------------------------------- #


@dataclass
class Instance:
    uid: str
    cm_name: str
    config: dict[str, Any]
    litellm_key: str = ""
    pod: str = ""
    restarts: int = -1

    @property
    def defaults(self) -> dict[str, Any]:
        return ((self.config.get("agents") or {}).get("defaults")) or {}

    @property
    def primary(self) -> str | None:
        return ((self.defaults.get("model")) or {}).get("primary")

    @property
    def fallbacks(self) -> list[str]:
        return list(((self.defaults.get("model")) or {}).get("fallbacks") or [])

    @property
    def catalog(self) -> list[dict[str, Any]]:
        providers = (self.config.get("models") or {}).get("providers") or {}
        return ((providers.get(PROVIDER)) or {}).get("models") or []


def load_instances(uids: list[str], kubeconfig: str | None, namespace: str) -> dict[str, Instance]:
    cms = json.loads(kubectl(["get", "cm", "-o", "json"], kubeconfig, namespace))["items"]
    raw = {
        cm["metadata"]["name"]: (cm.get("data") or {}).get("openclaw.json")
        for cm in cms
        if cm["metadata"]["name"].endswith("-user-config")
    }
    hers = json.loads(kubectl(["get", "her", "-o", "json"], kubeconfig, namespace))["items"]
    keys = {
        str(h["spec"].get("userId")): h["spec"].get("litellmKey", "")
        for h in hers
        if h.get("spec", {}).get("userId") is not None
    }
    pods = json.loads(kubectl(["get", "pods", "-o", "json"], kubeconfig, namespace))["items"]

    out: dict[str, Instance] = {}
    for uid in uids:
        name = f"carher-{uid}-user-config"
        if not raw.get(name):
            print(f"[{uid}] no {name} ConfigMap -- skipped", file=sys.stderr)
            continue
        inst = Instance(
            uid=uid,
            cm_name=name,
            config=json.loads(raw[name]),
            litellm_key=keys.get(uid, ""),
        )
        # The key in the CRD can lag; the config the pod actually uses wins.
        cfg_key = (
            ((inst.config.get("models") or {}).get("providers") or {}).get(PROVIDER) or {}
        ).get("apiKey") or ""
        if cfg_key and not cfg_key.startswith("${"):
            inst.litellm_key = cfg_key
        for pod in pods:
            if not pod["metadata"]["name"].startswith(f"carher-{uid}-"):
                continue
            inst.pod = pod["metadata"]["name"]
            statuses = pod.get("status", {}).get("containerStatuses") or []
            inst.restarts = sum(c.get("restartCount", 0) for c in statuses)
            break
        out[uid] = inst
    return out


# --------------------------------------------------------------------------- #
# diff guard
# --------------------------------------------------------------------------- #


def leaves(obj: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, obj


@dataclass
class Diff:
    added: list[tuple[str, Any]] = field(default_factory=list)
    changed: list[tuple[str, Any, Any]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def additions_only(self) -> bool:
        return not self.changed and not self.removed

    def render(self, indent: str = "      ") -> str:
        lines = [f"{indent}+ {p} = {v!r}" for p, v in self.added]
        lines += [f"{indent}~ {p}: {o!r} -> {n!r}" for p, o, n in self.changed]
        lines += [f"{indent}- REMOVED {p}" for p in self.removed]
        return "\n".join(lines) or f"{indent}(no change)"


def diff_leaves(old: dict[str, Any], new: dict[str, Any]) -> Diff:
    ol, nl = dict(leaves(old)), dict(leaves(new))
    return Diff(
        added=[(k, nl[k]) for k in nl if k not in ol],
        changed=[(k, ol[k], nl[k]) for k in set(ol) & set(nl) if ol[k] != nl[k]],
        removed=[k for k in ol if k not in nl],
    )


# --------------------------------------------------------------------------- #
# LiteLLM introspection + live probe
# --------------------------------------------------------------------------- #

_MODEL_INFO_SNIPPET = """
import json, urllib.request
KEY = {key!r}
TARGET = {target!r}
req = urllib.request.Request(
    "http://litellm-proxy.carher.svc:4000/model/info",
    headers={{"Authorization": "Bearer " + KEY}},
)
try:
    data = json.loads(urllib.request.urlopen(req, timeout=60).read() or b"{{}}")
except Exception as exc:
    print(json.dumps({{"ERR": repr(exc)[:300]}}))
    raise SystemExit(0)
hit = [d for d in data.get("data", []) if d.get("model_name") == TARGET]
names = sorted({{d.get("model_name") for d in data.get("data", [])}})
print(json.dumps({{"visible": names, "hit": hit[:1]}}))
"""


def _exec_in_pod(pod: str, script: str, kubeconfig: str | None, namespace: str) -> dict[str, Any]:
    """Run python inside a carher pod.

    It has to be a carher pod, not the litellm pod: the litellm image ships no
    curl and, more importantly, the point is to exercise the exact key and the
    exact in-cluster route the Her itself uses.
    """
    out = kubectl(
        ["exec", "-i", pod, "-c", "carher", "--", "python3", "-"],
        kubeconfig,
        namespace,
        input_text=script,
        check=False,
    )
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return {"ERR": (out or "no output")[-300:]}


_PROBE_SNIPPET = r"""
import json, urllib.request
KEY = {key!r}
TARGET = {target!r}
BASE = "http://litellm-proxy.carher.svc:4000/v1/chat/completions"
TOOLS = [{{
    "type": "function",
    "function": {{
        "name": "get_weather",
        "description": "query weather",
        "parameters": {{
            "type": "object",
            "properties": {{"city": {{"type": "string"}}}},
            "required": ["city"],
        }},
    }},
}}]

def call(stream):
    # Every real Her turn carries tools. A bare {{"messages": [...]}} smoke test
    # passes against upstreams that 400 on the shape production actually sends.
    body = {{
        "model": TARGET,
        "messages": [{{"role": "user", "content": "use get_weather for Beijing"}}],
        "tools": TOOLS,
        "max_tokens": 256,
    }}
    if stream:
        body["stream"] = True
    req = urllib.request.Request(
        BASE,
        data=json.dumps(body).encode(),
        headers={{"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        raw = resp.read().decode("utf-8", "replace")
        if stream:
            frames = [l for l in raw.splitlines() if l.startswith("data:")]
            return {{
                "http": resp.status,
                "frames": len(frames),
                "done": any("[DONE]" in l for l in frames),
            }}
        payload = json.loads(raw)
        choice = (payload.get("choices") or [{{}}])[0]
        return {{
            "http": resp.status,
            "finish_reason": choice.get("finish_reason"),
            "tool_calls": len((choice.get("message") or {{}}).get("tool_calls") or []),
            "usage": payload.get("usage", {{}}).get("total_tokens"),
        }}
    except Exception as exc:
        body = getattr(exc, "read", lambda: b"")()
        return {{"ERR": repr(exc)[:200], "body": body.decode("utf-8", "replace")[:300]}}

print(json.dumps({{"nonstream": call(False), "stream": call(True)}}))
"""


def probe_ok(res: dict[str, Any]) -> bool:
    ns, st = res.get("nonstream") or {}, res.get("stream") or {}
    return (
        ns.get("http") == 200
        and ns.get("tool_calls", 0) > 0
        and st.get("http") == 200
        and bool(st.get("done"))
    )


# --------------------------------------------------------------------------- #
# config surgery
# --------------------------------------------------------------------------- #


def build_catalog_entry(args: argparse.Namespace, info: dict[str, Any]) -> dict[str, Any]:
    """Catalog entry for the fallback model.

    Costs come from LiteLLM (per-token) converted to the per-million units the
    Her catalog uses, so the two never disagree. Window/output size are operator
    input: LiteLLM leaves `max_input_tokens` null for bridged models and we do
    not invent a number for a model whose real window we have not measured.
    """
    params = (info.get("litellm_params") or {}) if info else {}

    def per_m(field_name: str) -> float | int | None:
        v = params.get(field_name)
        if not isinstance(v, (int, float)):
            return None
        # Integral values render as `2`, not `2.0` -- the rest of the catalog is
        # written that way, and matching it keeps the diff to the new leaves.
        m = round(v * 1_000_000, 6)
        return int(m) if m == int(m) else m

    cost: dict[str, float] = {}
    for out_key, src in (
        ("input", "input_cost_per_token"),
        ("output", "output_cost_per_token"),
        ("cacheRead", "cache_read_input_token_cost"),
        ("cacheWrite", "cache_creation_input_token_cost"),
    ):
        v = per_m(src)
        if v is not None:
            cost[out_key] = v

    entry: dict[str, Any] = {
        "api": "openai-completions",
        "contextWindow": args.context_window,
        "id": args.model,
        "input": args.input.split(","),
        "maxTokens": args.max_tokens,
        "name": args.display_name or args.model,
        "reasoning": not args.no_reasoning,
    }
    if cost:
        entry["cost"] = cost
    return entry


def build_new_config(
    old: dict[str, Any], model: str, entry: dict[str, Any], alias: str | None
) -> dict[str, Any]:
    """Pure addition: catalog entry, alias map entry, and one chain element.

    Nothing existing is touched. The old chain keeps its order and its head --
    the new model goes on the end, because the caller asked to *add* a fallback,
    and demoting whatever was already there is a different decision.
    """
    new = json.loads(json.dumps(old))
    ref = f"{PROVIDER}/{model}"

    catalog = (
        new.setdefault("models", {})
        .setdefault("providers", {})
        .setdefault(PROVIDER, {})
        .setdefault("models", [])
    )
    if model not in [m.get("id") for m in catalog]:
        catalog.append(entry)

    defaults = new.setdefault("agents", {}).setdefault("defaults", {})
    if alias:
        defaults.setdefault("models", {}).setdefault(ref, {"alias": alias})

    chain = defaults.setdefault("model", {}).setdefault("fallbacks", [])
    if ref not in chain:
        chain.append(ref)
    return new


def serialize(cfg: dict[str, Any]) -> str:
    """Match the operator's own formatting so the diff stays surgical.

    The operator writes sorted-key, 2-space JSON with no trailing newline;
    reproducing it byte-for-byte keeps `kubectl diff` and the gateway's reload
    log limited to the paths we actually changed.
    """
    return json.dumps(cfg, indent=2, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #


def _gather(args: argparse.Namespace) -> dict[str, Instance]:
    return load_instances(parse_uids(args.uids), args.kubeconfig, args.namespace)


def cmd_preflight(args: argparse.Namespace) -> int:
    instances = _gather(args)
    if not instances:
        return 1
    rc = 0
    for uid, inst in instances.items():
        print(f"\n=== carher-{uid} ===")
        print(f"  primary   : {inst.primary}")
        print(f"  fallbacks : {inst.fallbacks or '(none)'}")
        if f"{PROVIDER}/{args.model}" in inst.fallbacks:
            print(f"  SKIP: {args.model} already in the chain")
            continue
        if not inst.pod:
            print("  FAIL: no running pod, cannot probe")
            rc = 1
            continue
        if not inst.litellm_key:
            print("  FAIL: no litellm key resolved")
            rc = 1
            continue

        info = _exec_in_pod(
            inst.pod,
            _MODEL_INFO_SNIPPET.format(key=inst.litellm_key, target=args.model),
            args.kubeconfig,
            args.namespace,
        )
        if info.get("ERR") or not info.get("hit"):
            print(f"  FAIL: {args.model} not visible to this key -- {info.get('ERR') or 'not in /model/info'}")
            print(f"        visible groups: {info.get('visible')}")
            rc = 1
            continue
        hit = info["hit"][0]
        mi = hit.get("model_info") or {}
        print(f"  allowlist : OK (id={mi.get('id')}, mode={mi.get('mode')})")
        print(f"  max_input_tokens declared upstream: {mi.get('max_input_tokens')}")
        if mi.get("max_input_tokens") is None:
            print("  NOTE: upstream declares no window -- --context-window is mandatory "
                  "and should be conservative (overflow does NOT advance the chain)")

        probe = _exec_in_pod(
            inst.pod,
            _PROBE_SNIPPET.format(key=inst.litellm_key, target=args.model),
            args.kubeconfig,
            args.namespace,
        )
        print(f"  probe     : {json.dumps(probe, ensure_ascii=False)}")
        if not probe_ok(probe):
            print("  FAIL: fallback target does not serve a tools payload -- "
                  "configuring it would create a dead safety net")
            rc = 1
    print(
        "\nReminder: this chain only covers the default-primary path. "
        "Models the user picks with /model stay strict and never fall back."
    )
    return rc


def cmd_add(args: argparse.Namespace) -> int:
    if args.context_window is None:
        print("--context-window is required (do not guess; see module docstring)", file=sys.stderr)
        return 2
    instances = _gather(args)
    if not instances:
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = args.backup_dir or f"/tmp/her-fallback-{args.model}-{stamp}"
    if args.apply:
        os.makedirs(backup_dir, exist_ok=True)

    rc = 0
    for uid, inst in instances.items():
        print(f"\n=== carher-{uid} ===")
        ref = f"{PROVIDER}/{args.model}"
        if ref in inst.fallbacks:
            print(f"  SKIP: {ref} already in chain {inst.fallbacks}")
            continue

        info: dict[str, Any] = {}
        if inst.pod and inst.litellm_key:
            probe = _exec_in_pod(
                inst.pod,
                _MODEL_INFO_SNIPPET.format(key=inst.litellm_key, target=args.model),
                args.kubeconfig,
                args.namespace,
            )
            info = (probe.get("hit") or [{}])[0]

        entry = build_catalog_entry(args, info)
        new = build_new_config(inst.config, args.model, entry, args.alias)
        diff = diff_leaves(inst.config, new)
        print(f"  chain: {inst.fallbacks} -> {inst.fallbacks + [ref]}")
        print(diff.render())
        if not diff.additions_only:
            print("  ABORT: patch is not purely additive", file=sys.stderr)
            rc = 1
            continue

        if not args.apply:
            print("  (dry run)")
            continue

        bak = os.path.join(backup_dir, f"{inst.cm_name}.json")
        with open(bak, "w", encoding="utf-8") as fh:
            fh.write(serialize(inst.config))
        patch = json.dumps({"data": {"openclaw.json": serialize(new)}})
        kubectl(
            ["patch", "cm", inst.cm_name, "--type", "merge", "--patch", patch],
            args.kubeconfig,
            args.namespace,
        )
        print(f"  applied (backup {bak})")
    if args.apply:
        print(f"\nbackups in {backup_dir}")
        print("hot reload lands in ~15s; verify with the `verify` subcommand")
    return rc


def cmd_verify(args: argparse.Namespace) -> int:
    instances = _gather(args)
    if not instances:
        return 1
    rc = 0
    for uid, inst in instances.items():
        print(f"\n=== carher-{uid} ===")
        if not inst.pod:
            print("  FAIL: no pod")
            rc = 1
            continue
        # The engine's own view, not the file on disk: a ConfigMap can be
        # correct while the gateway is still running the previous config.
        listing = kubectl(
            [
                "exec", inst.pod, "-c", "carher", "--", "sh", "-c",
                "cd /data && HOME=/data timeout 60 /opt/openclaw/bin/openclaw models list",
            ],
            args.kubeconfig,
            args.namespace,
            check=False,
        )
        rows = [l for l in listing.splitlines() if f"{PROVIDER}/{args.model}" in l]
        tagged = [l for l in rows if "fallback#" in l]
        print(f"  restarts  : {inst.restarts}")
        print(f"  models list: {rows[0].strip() if rows else '(model absent)'}")
        if tagged:
            print("  OK: engine has it in the fallback chain")
        else:
            print("  FAIL: engine does not tag it fallback#N -- config not loaded")
            rc = 1
        reload_log = kubectl(
            ["logs", inst.pod, "-c", "carher", "--since=10m"],
            args.kubeconfig,
            args.namespace,
            check=False,
        )
        for line in reload_log.splitlines():
            if "[reload]" in line:
                print(f"  {line.strip()}")
    return rc


def cmd_rollback(args: argparse.Namespace) -> int:
    if not args.backup_dir:
        print("--backup-dir is required", file=sys.stderr)
        return 2
    instances = _gather(args)
    rc = 0
    for uid, inst in instances.items():
        bak = os.path.join(args.backup_dir, f"{inst.cm_name}.json")
        if not os.path.exists(bak):
            print(f"[{uid}] no backup at {bak}", file=sys.stderr)
            rc = 1
            continue
        with open(bak, encoding="utf-8") as fh:
            old = json.load(fh)
        diff = diff_leaves(inst.config, old)
        print(f"\n=== carher-{uid} (restore) ===")
        print(diff.render())
        if not args.apply:
            print("  (dry run)")
            continue
        patch = json.dumps({"data": {"openclaw.json": serialize(old)}})
        kubectl(
            ["patch", "cm", inst.cm_name, "--type", "merge", "--patch", patch],
            args.kubeconfig,
            args.namespace,
        )
        print("  restored")
    return rc


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def parse_uids(raw: str) -> list[str]:
    out: list[str] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    ap.add_argument("--namespace", default="carher")
    ap.add_argument("--uids", required=True, help="comma separated Her uids, e.g. 1000,25,26")
    ap.add_argument("--model", required=True, help="LiteLLM model group to add as fallback, e.g. grok-4.6")
    ap.add_argument("--alias", default=None, help="short alias to register in agents.defaults.models")
    ap.add_argument("--display-name", default=None)
    ap.add_argument("--context-window", type=int, default=None,
                    help="conservative input window; required for `add`")
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--input", default="text", help="comma separated modalities, e.g. text,image")
    ap.add_argument("--no-reasoning", action="store_true")
    ap.add_argument("--backup-dir", default=None)
    ap.add_argument("--apply", action="store_true", help="write; omit for a dry run")
    ap.add_argument("command", choices=["preflight", "add", "verify", "rollback"])
    args = ap.parse_args()

    return {
        "preflight": cmd_preflight,
        "add": cmd_add,
        "verify": cmd_verify,
        "rollback": cmd_rollback,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
