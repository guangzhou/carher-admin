#!/usr/bin/env python3
"""Converge Aliyun CarHer keys and runtime model catalogs to carher-1000.

The script is intentionally batch-oriented and failure-tolerant:

- defaults to dry-run;
- processes at most ``--batch-size`` Her instances per batch (default 10);
- updates one instance at a time inside each batch;
- records and skips individual failures instead of stopping the fleet run;
- uses LiteLLM ``/key/update`` so key caches are refreshed;
- never deletes or restarts a serving Pod.

OpenClaw configuration is made self-contained to prevent the shared H75 base
catalog from re-introducing retired model names through ``$include``. Per-Her
identity, plugins and non-model settings stay local to each instance.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


NAMESPACE = "carher"
TARGET_PRIMARY = "litellm/gpt-5.6-terra"
TARGET_SPEC_MODEL = "gpt-5.6-terra"
TARGET_CHAT_MODELS = [
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
]
TARGET_KEY_MODELS = TARGET_CHAT_MODELS + ["bge-m3"]
TARGET_ALIASES = {
    "bge-m3": "BAAI/bge-m3",
    "gpt-5.5": "chatgpt-gpt-5.5",
    "gpt-5.6-sol": "chatgpt-gpt-5.6-sol",
    "gpt-5.6-terra": "chatgpt-gpt-5.6-terra",
    "gpt-5.6-luna": "chatgpt-gpt-5.6-luna",
    "claude-opus-4-8": "chatgpt-gpt-5.6-sol",
    "claude-sonnet-5": "chatgpt-gpt-5.6-terra",
    "claude-haiku-4-5": "chatgpt-gpt-5.6-luna",
    "deepseek-v4-pro": "wangsu-deepseek-v4-pro",
    "deepseek-v4-flash": "local-deepseek-v4-flash",
    "gemini-3.5-flash": "wangsu-gemini-3.5-flash",
    "qwen3.7-plus": "wangsu-qwen3.7-plus",
}
MODEL_MIGRATIONS = {
    "gpt-5.4": "gpt-5.6-terra",
    "gpt-5.3-codex": "gpt-5.6-terra",
    "chatgpt-gpt-5.4": "gpt-5.6-terra",
    "chatgpt-gpt-5.3-codex": "gpt-5.6-terra",
    "claude-opus-4-6": "claude-opus-4-8",
    "claude-opus-4-7": "claude-opus-4-8",
    "anthropic.claude-opus-4-6": "claude-opus-4-8",
    "anthropic.claude-opus-4-7": "claude-opus-4-8",
    "openrouter-claude-opus-4-8": "claude-opus-4-8",
    "claude-sonnet-4-6": "claude-sonnet-5",
    "gemini-3.1-pro-preview": "gemini-3.5-flash",
    "wangsu-gemini-3.5-flash": "gemini-3.5-flash",
    "wangsu-glm-5.1": "glm-5",
    "minimax-m2.7": "gpt-5.6-terra",
    "minimax-m3": "gpt-5.6-terra",
    "wangsu-deepseek-v4-pro": "deepseek-v4-pro",
    "wangsu-deepseek-v4-flash": "deepseek-v4-flash",
    "local-deepseek-v4-flash-chat": "deepseek-v4-flash",
    "openai/gpt-5.5": "gpt-5.5",
    "anthropic/claude-opus-4.6": "claude-opus-4-8",
}


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, input=input_text, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:5])}: {(proc.stderr or proc.stdout).strip()[:500]}")
    return proc


def kubectl(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["kubectl", "-n", NAMESPACE, *args], input_text=input_text, check=check)


def kubectl_json(args: list[str]) -> Any:
    return json.loads(kubectl([*args, "-o", "json"]).stdout)


def utc_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def deep_merge(left: Any, right: Any) -> Any:
    if isinstance(left, list) and isinstance(right, list):
        # Lists are complete config values, not additive fragments. Concatenating
        # reference and per-Her lists duplicates plugins/tools on every rerun.
        return copy.deepcopy(right)
    if isinstance(left, dict) and isinstance(right, dict):
        out = copy.deepcopy(left)
        for key, value in right.items():
            out[key] = deep_merge(out[key], value) if key in out else copy.deepcopy(value)
        return out
    return copy.deepcopy(right)


def target_openclaw_config(reference: dict[str, Any], current: dict[str, Any], litellm_key: str) -> dict[str, Any]:
    """Preserve per-Her fields and replace only the model-owned paths."""
    out = copy.deepcopy(current)
    out.pop("$include", None)

    # A self-contained config needs the non-model H75 behavior inherited by the
    # reference instance. Merge it under the current Her so identity wins.
    base = copy.deepcopy(reference)
    for root in ("channels", "commands"):
        base.pop(root, None)
    base_agents = ((base.get("agents") or {}).get("defaults") or {})
    for field_name in ("model", "models", "memorySearch", "contextTokens"):
        base_agents.pop(field_name, None)
    base.pop("models", None)
    out = deep_merge(base, out)
    out.pop("$include", None)

    ref_defaults = reference["agents"]["defaults"]
    defaults = out.setdefault("agents", {}).setdefault("defaults", {})
    defaults["model"] = {"primary": TARGET_PRIMARY}
    defaults["models"] = copy.deepcopy(ref_defaults["models"])
    defaults["memorySearch"] = copy.deepcopy(ref_defaults["memorySearch"])
    out["models"] = copy.deepcopy(reference["models"])
    provider = out["models"]["providers"]["litellm"]
    provider["apiKey"] = litellm_key
    defaults["memorySearch"].setdefault("remote", {})["baseUrl"] = provider.get(
        "baseUrl", "http://litellm-proxy.carher.svc:4000"
    )
    defaults["memorySearch"]["remote"]["apiKey"] = litellm_key

    if sorted(k.removeprefix("litellm/") for k in defaults["models"]) != sorted(TARGET_CHAT_MODELS):
        raise ValueError("reference alias catalog does not match target products")
    provider_ids = [m["id"] for m in out["models"]["providers"]["litellm"]["models"]]
    if sorted(provider_ids) != sorted(TARGET_CHAT_MODELS):
        raise ValueError("reference provider catalog does not match target products")
    if defaults["memorySearch"].get("model") != "bge-m3":
        raise ValueError("reference memorySearch model is not bge-m3")
    return out


def target_hermes_config(current: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(current)
    context_lengths = {
        "gpt-5.6-sol": 272000,
        "gpt-5.6-terra": 272000,
        "gpt-5.6-luna": 272000,
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
    model_map = {model: {"context_length": context_lengths[model]} for model in TARGET_CHAT_MODELS}
    out.setdefault("model", {})["provider"] = "litellm"
    out["model"]["default"] = "gpt-5.6-terra"
    providers = out.setdefault("providers", {}).setdefault("litellm", {})
    providers["default_model"] = "gpt-5.6-terra"
    providers["models"] = copy.deepcopy(model_map)
    custom = out.get("custom_providers") or []
    found = False
    for provider in custom:
        if provider.get("name") == "litellm":
            provider["default_model"] = "gpt-5.6-terra"
            provider["model"] = "gpt-5.6-terra"
            provider["models"] = copy.deepcopy(model_map)
            found = True
    if not found:
        custom.append({
            "name": "litellm",
            "base_url": providers.get("base_url", "http://litellm-proxy.carher.svc:4000/v1"),
            "key_env": "LITELLM_API_KEY",
            "api_mode": "chat_completions",
            "transport": "chat_completions",
            "default_model": "gpt-5.6-terra",
            "models": copy.deepcopy(model_map),
            "model": "gpt-5.6-terra",
        })
    out["custom_providers"] = custom
    out["quick_commands"] = {
        model: {"type": "alias", "target": f"/model {model} --provider litellm --global"}
        for model in TARGET_CHAT_MODELS
    }
    return out


def api_json(base: str, master: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {master}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {path} HTTP {exc.code}: {body}") from exc


@dataclass
class Result:
    uid: str
    key_alias: str
    status: str = "pending"
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class Converger:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.reference = json.loads(
            kubectl_json(["get", "cm", f"carher-{args.reference_uid}-user-config"])["data"]["openclaw.json"]
        )
        self.hers = kubectl_json(["get", "her"])["items"]
        self.cms = kubectl_json(["get", "cm"])["items"]
        self.deploys = kubectl_json(["get", "deploy"])["items"]
        self.pods = kubectl_json(["get", "pods"])["items"]
        self.her_by_uid = {str(h["spec"]["userId"]): h for h in self.hers}
        self.cm_by_uid: dict[str, dict[str, Any]] = {}
        for cm in self.cms:
            name = cm["metadata"]["name"]
            if name.startswith("carher-") and name.endswith("-user-config"):
                uid = name[len("carher-") : -len("-user-config")]
                self.cm_by_uid[uid] = cm
        self.deploy_by_uid: dict[str, dict[str, Any]] = {}
        for deploy in self.deploys:
            labels = deploy.get("spec", {}).get("selector", {}).get("matchLabels", {})
            if labels.get("app") == "carher-user" and labels.get("user-id"):
                self.deploy_by_uid[labels["user-id"]] = deploy
        self.pod_by_uid: dict[str, dict[str, Any]] = {}
        for pod in self.pods:
            labels = pod["metadata"].get("labels", {})
            uid = labels.get("user-id")
            if labels.get("app") == "carher-user" and uid and pod.get("status", {}).get("phase") == "Running":
                self.pod_by_uid[uid] = pod

        proxy_pods = [p for p in self.pods if p["metadata"].get("labels", {}).get("app") == "litellm-proxy" and p.get("status", {}).get("phase") == "Running"]
        if not proxy_pods:
            raise RuntimeError("no running LiteLLM proxy pod")
        self.proxy_pod = proxy_pods[0]["metadata"]["name"]
        master_proc = kubectl(["exec", self.proxy_pod, "-c", "litellm", "--", "printenv", "LITELLM_MASTER_KEY"])
        self.master_key = master_proc.stdout.strip()
        if not self.master_key:
            raise RuntimeError("empty LiteLLM master key")
        self.api_base = "http://127.0.0.1:4000"

        key_sql = '''
SELECT COALESCE(json_agg(json_build_object(
  'key_alias', key_alias,
  'token', token,
  'models', COALESCE(models, ARRAY[]::text[]),
  'aliases', COALESCE(aliases, '{}'::jsonb)
) ORDER BY key_alias), '[]'::json)
FROM "LiteLLM_VerificationToken"
WHERE key_alias LIKE 'carher-%';
'''
        raw = kubectl(["exec", "litellm-db-0", "--", "psql", "-U", "litellm", "-d", "litellm", "-At", "-c", key_sql]).stdout.strip()
        self.keys = json.loads(raw or "[]")
        self.keys_by_uid: dict[str, list[dict[str, Any]]] = {}
        for row in self.keys:
            uid = row["key_alias"].removeprefix("carher-")
            self.keys_by_uid.setdefault(uid, []).append(row)

    def select_uids(self) -> list[str]:
        if self.args.uids:
            selected = [x.strip() for x in self.args.uids.split(",") if x.strip()]
        else:
            # Include key-only carher-* rows as well as HerInstances. A stale
            # virtual key must not remain on the old catalog just because its
            # deployment was already removed.
            selected = sorted(
                set(self.her_by_uid) | set(self.keys_by_uid),
                key=lambda value: (0, int(value)) if value.isdigit() else (1, value),
            )
        excluded = {x.strip() for x in self.args.exclude.split(",") if x.strip()}
        return [uid for uid in selected if uid not in excluded]

    def pod_exec(self, uid: str, command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        pod = self.pod_by_uid.get(uid)
        if not pod:
            raise RuntimeError("no running pod")
        return kubectl(["exec", "-i" if input_text is not None else "-t", pod["metadata"]["name"], "-c", "carher", "--", *command], input_text=input_text, check=check)

    def current_config(self, uid: str) -> dict[str, Any]:
        cm = self.cm_by_uid.get(uid)
        raw = ((cm or {}).get("data") or {}).get("openclaw.json")
        if not raw:
            raise RuntimeError("missing user ConfigMap")
        return json.loads(raw)

    def update_key(self, uid: str, result: Result) -> None:
        rows = self.keys_by_uid.get(uid, [])
        if len(rows) != 1:
            raise RuntimeError(f"expected exactly one key, found {len(rows)}")
        row = rows[0]
        if row.get("models") == TARGET_KEY_MODELS and row.get("aliases") == TARGET_ALIASES:
            result.actions.append("key_already_target")
            return
        result.actions.append("key_update")
        if self.args.apply:
            script = (
                "import json,os,urllib.request\n"
                "p=json.loads(os.environ['PAYLOAD'])\n"
                "r=urllib.request.Request('http://127.0.0.1:4000/key/update',data=json.dumps(p).encode(),"
                "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],'Content-Type':'application/json'},method='POST')\n"
                "print(urllib.request.urlopen(r,timeout=45).read().decode())\n"
            )
            payload = {"key": row["token"], "models": TARGET_KEY_MODELS, "aliases": TARGET_ALIASES}
            kubectl([
                "exec", self.proxy_pod, "-c", "litellm", "--", "env",
                f"PAYLOAD={json.dumps(payload, separators=(',', ':'))}", "python3", "-c", script,
            ])

    def update_her(self, uid: str, result: Result) -> None:
        her = self.her_by_uid.get(uid)
        if not her:
            raise RuntimeError("missing HerInstance")
        if her["spec"].get("model") == TARGET_SPEC_MODEL:
            result.actions.append("spec_already_terra")
            return
        result.actions.append(f"spec_model:{her['spec'].get('model')}->{TARGET_SPEC_MODEL}")
        if self.args.apply:
            kubectl(["patch", "her", her["metadata"]["name"], "--type=merge", "-p", json.dumps({"spec": {"model": TARGET_SPEC_MODEL}})])

    def update_config(self, uid: str, result: Result) -> None:
        current = self.current_config(uid)
        rows = self.keys_by_uid.get(uid, [])
        her_key = (self.her_by_uid.get(uid, {}).get("spec", {}).get("litellmKey") or "").strip()
        if len(rows) == 1 and not her_key:
            # The CRD may omit the key while the running Pod still has it. Do
            # not copy carher-1000's key; retrieve only the target's own key.
            pod = self.pod_by_uid.get(uid)
            if pod:
                proc = self.pod_exec(uid, ["sh", "-lc", "printf %s \"$LITELLM_API_KEY\""])
                her_key = proc.stdout.strip()
        if not her_key:
            raise RuntimeError("missing target LiteLLM key")
        target = target_openclaw_config(self.reference, current, her_key)
        current_raw = json.dumps(current, sort_keys=True, separators=(",", ":"))
        target_raw = json.dumps(target, sort_keys=True, separators=(",", ":"))
        if current_raw == target_raw:
            result.actions.append("config_already_target")
            return
        result.actions.append("config_update")
        if not self.args.apply:
            return
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"carher-{uid}-user-config", "namespace": NAMESPACE},
            "data": {"openclaw.json": json.dumps(target, ensure_ascii=False, indent=2)},
        }
        kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))

    def update_sessions(self, uid: str, result: Result) -> None:
        if uid not in self.pod_by_uid:
            result.warnings.append("sessions_skipped_no_running_pod")
            return
        script = r'''
import datetime as dt,json,pathlib,shutil
p=pathlib.Path('/data/.openclaw/agents/main/sessions/sessions.json')
if not p.exists():
 print(json.dumps({'updated':0,'missing':True})); raise SystemExit(0)
d=json.loads(p.read_text())
allowed=set(%(allowed)s)
migrations=%(migrations)s
rows=[]
for key,state in d.items():
 if not isinstance(state,dict) or not state.get('modelOverride'): continue
 model=state.get('modelOverride')
 provider=state.get('providerOverride') or ''
 bare=model.split('/',1)[-1] if '/' in model else model
 if provider=='litellm' and bare in allowed: continue
 target=migrations.get(model) or migrations.get(bare) or 'gpt-5.6-terra'
 rows.append({'session':key,'from':provider+'/'+model,'to':'litellm/'+target})
 state['providerOverride']='litellm';state['modelOverride']=target;state['modelOverrideSource']='user'
if rows:
 stamp=dt.datetime.now(dt.UTC).strftime('%%Y%%m%%dT%%H%%M%%SZ')
 shutil.copyfile(p,p.with_name(p.name+'.bak-model-converge-'+stamp))
 p.write_text(json.dumps(d,ensure_ascii=False,indent=2))
print(json.dumps({'updated':len(rows),'rows':rows[:20]}))
''' % {"allowed": repr(TARGET_CHAT_MODELS), "migrations": repr(MODEL_MIGRATIONS)}
        result.actions.append("session_audit")
        if self.args.apply:
            proc = self.pod_exec(uid, ["python3", "-c", script])
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            if payload.get("updated"):
                result.actions.append(f"sessions_migrated:{payload['updated']}")

    def update_hermes(self, uid: str, result: Result) -> None:
        if uid not in self.pod_by_uid:
            result.warnings.append("hermes_skipped_no_running_pod")
            return
        script = r'''
import datetime as dt,json,pathlib,shutil,yaml
paths=[pathlib.Path('/opt/data/.hermes/config-litellm.yaml'),pathlib.Path('/opt/data/.hermes/config.yaml')]
chat=%(chat)s
lengths=%(lengths)s
changed=[]
parse_repaired=[]
for p in paths:
 if not p.exists(): continue
 try:
  d=yaml.safe_load(p.read_text()) or {}
 except Exception:
  # A malformed Hermes file cannot be merged safely. The OpenClaw/CRD planes
  # are already authoritative, so back up and rebuild only this model file.
  d={}
  parse_repaired.append(str(p))
 models={m:{'context_length':lengths[m]} for m in chat}
 d.setdefault('model',{})['provider']='litellm';d['model']['default']='gpt-5.6-terra'
 lp=d.setdefault('providers',{}).setdefault('litellm',{})
 lp['default_model']='gpt-5.6-terra';lp['models']=models
 found=False
 for cp in d.get('custom_providers') or []:
  if cp.get('name')=='litellm':
   cp['default_model']='gpt-5.6-terra';cp['model']='gpt-5.6-terra';cp['models']=models;found=True
 if not found:
  d.setdefault('custom_providers',[]).append({'name':'litellm','base_url':lp.get('base_url','http://litellm-proxy.carher.svc:4000/v1'),'key_env':'LITELLM_API_KEY','api_mode':'chat_completions','transport':'chat_completions','default_model':'gpt-5.6-terra','models':models,'model':'gpt-5.6-terra'})
 d['quick_commands']={m:{'type':'alias','target':f'/model {m} --provider litellm --global'} for m in chat}
 new=yaml.safe_dump(d,sort_keys=False,allow_unicode=True)
 if p.read_text()!=new:
  stamp=dt.datetime.now(dt.UTC).strftime('%%Y%%m%%dT%%H%%M%%SZ')
  shutil.copyfile(p,p.with_name(p.name+'.bak-model-converge-'+stamp))
  p.write_text(new);changed.append(str(p))
print(json.dumps({'changed':changed,'parse_repaired':parse_repaired,'files':[str(p) for p in paths if p.exists()]}))
''' % {
            "chat": repr(TARGET_CHAT_MODELS),
            "lengths": repr({
                "gpt-5.6-sol": 272000, "gpt-5.6-terra": 272000, "gpt-5.6-luna": 272000,
                "gpt-5.5": 272000, "claude-opus-4-8": 1000000, "claude-sonnet-5": 1000000,
                "claude-haiku-4-5": 272000, "deepseek-v4-pro": 128000, "deepseek-v4-flash": 128000,
                "gemini-3.5-flash": 1000000, "glm-5": 128000, "qwen3.7-plus": 1000000,
            }),
        }
        result.actions.append("hermes_audit")
        if self.args.apply:
            probe = self.pod_exec(
                uid,
                ["sh", "-lc", "test -x /opt/hermes/.venv/bin/python3 && "
                 "ls /opt/data/.hermes/config-litellm.yaml /opt/data/.hermes/config.yaml 2>/dev/null | head -1"],
                check=False,
            )
            if probe.returncode != 0 or not probe.stdout.strip():
                result.warnings.append("hermes_not_present")
                return
            proc = self.pod_exec(uid, ["/opt/hermes/.venv/bin/python3", "-c", script])
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            if payload.get("changed"):
                result.actions.append(f"hermes_files_updated:{len(payload['changed'])}")
            if payload.get("parse_repaired"):
                result.actions.append(f"hermes_parse_repaired:{len(payload['parse_repaired'])}")

    def verify(self, uid: str, result: Result) -> None:
        if not self.args.apply or self.args.skip_runtime_verify:
            return
        # Re-read the key through the real user key so this checks cache state.
        pod = self.pod_by_uid.get(uid)
        if pod:
            key_check = self.pod_exec(uid, ["sh", "-lc", "key=$LITELLM_API_KEY; curl -sS 'http://litellm-proxy.carher.svc.cluster.local:4000/key/info?key='$key -H 'Authorization: Bearer '$key"]).stdout
            info = json.loads(key_check)["info"]
            if info.get("models") != TARGET_KEY_MODELS or info.get("aliases") != TARGET_ALIASES:
                raise RuntimeError("key verification mismatch")
        else:
            result.warnings.append("key_cache_verify_skipped_no_running_pod")

        deadline = time.time() + self.args.reload_timeout
        last = ""
        while pod and time.time() < deadline:
            proc = self.pod_exec(uid, ["node", "-e", (
                "const d=require('/data/.openclaw/openclaw.json');"
                "console.log(JSON.stringify({primary:d.agents?.defaults?.model?.primary,"
                "aliases:Object.keys(d.agents?.defaults?.models||{}).map(x=>x.replace(/^litellm\\//,'' )).sort(),"
                "provider:(d.models?.providers?.litellm?.models||[]).map(x=>x.id).sort(),"
                "memory:d.agents?.defaults?.memorySearch?.model,include:Object.hasOwn(d,'$include')}))"
            )], check=False)
            last = proc.stdout.strip()
            if proc.returncode == 0 and last:
                live = json.loads(last.splitlines()[-1])
                if (live.get("primary") == TARGET_PRIMARY and live.get("aliases") == sorted(TARGET_CHAT_MODELS)
                        and live.get("provider") == sorted(TARGET_CHAT_MODELS) and live.get("memory") == "bge-m3"
                        and live.get("include") is False):
                    break
            time.sleep(5)
        else:
            if pod:
                raise RuntimeError(f"OpenClaw hot reload did not converge: {last[:300]}")

        if pod:
            statuses = pod.get("status", {}).get("containerStatuses") or []
            if not statuses or not all(c.get("ready") for c in statuses):
                result.warnings.append("pod_not_fully_ready")
            # Verify Hermes files without changing engine state.
            hprobe = self.pod_exec(
                uid,
                ["sh", "-lc", "test -x /opt/hermes/.venv/bin/python3 && "
                 "ls /opt/data/.hermes/config-litellm.yaml /opt/data/.hermes/config.yaml 2>/dev/null | head -1"],
                check=False,
            )
            if hprobe.returncode != 0 or not hprobe.stdout.strip():
                return
            hcheck = self.pod_exec(uid, ["/opt/hermes/.venv/bin/python3", "-c", (
                "import json,yaml,pathlib;bad=[];found=[];"
                "chat=" + repr(sorted(TARGET_CHAT_MODELS)) + ";"
                "\nfor p in [pathlib.Path('/opt/data/.hermes/config-litellm.yaml'),pathlib.Path('/opt/data/.hermes/config.yaml')]:\n"
                " if not p.exists(): continue\n"
                " found.append(str(p))\n"
                " d=yaml.safe_load(p.read_text());mods=sorted(d['providers']['litellm']['models']);"
                " bad += [] if d['model']['default']=='gpt-5.6-terra' and d['providers']['litellm']['default_model']=='gpt-5.6-terra' and mods==chat else [str(p)]\n"
                "print(json.dumps({'bad':bad,'found':found}))"
            )])
            if json.loads(hcheck.stdout.strip().splitlines()[-1])["bad"]:
                raise RuntimeError("Hermes verification mismatch")

    def process(self, uid: str) -> Result:
        result = Result(uid=uid, key_alias=f"carher-{uid}")
        try:
            if uid not in self.her_by_uid:
                if uid in self.keys_by_uid:
                    self.update_key(uid, result)
                    result.status = "success" if self.args.apply else "dry_run"
                    result.warnings.append("key_only_no_HerInstance")
                else:
                    result.status = "skipped"
                    result.warnings.append("no_HerInstance")
                return result
            if uid not in self.cm_by_uid:
                raise RuntimeError("missing user ConfigMap")
            self.update_key(uid, result)
            # Change the source-of-truth default first. The final ConfigMap
            # write then wins over any reconcile triggered by this spec patch.
            self.update_her(uid, result)
            self.update_config(uid, result)
            self.update_sessions(uid, result)
            self.update_hermes(uid, result)
            self.verify(uid, result)
            result.status = "success" if self.args.apply else "dry_run"
        except Exception as exc:  # individual failures must not stop the fleet
            result.status = "failed"
            result.error = str(exc)[:800]
        return result


def write_report(path: pathlib.Path, args: argparse.Namespace, results: list[Result]) -> None:
    payload = {
        "created_at": utc_stamp(),
        "apply": args.apply,
        "batch_size": args.batch_size,
        "target_key_models": TARGET_KEY_MODELS,
        "target_chat_models": TARGET_CHAT_MODELS,
        "target_primary": TARGET_PRIMARY,
        "summary": {
            "total": len(results),
            "success": sum(r.status == "success" for r in results),
            "dry_run": sum(r.status == "dry_run" for r in results),
            "skipped": sum(r.status == "skipped" for r in results),
            "failed": sum(r.status == "failed" for r in results),
            "warnings": sum(bool(r.warnings) for r in results),
        },
        "results": [r.__dict__ for r in results],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--uids", default="", help="Comma-separated Her ids; default is every HerInstance.")
    parser.add_argument("--exclude", default="", help="Comma-separated Her ids to skip.")
    parser.add_argument("--reference-uid", default="1000")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--batch-pause", type=int, default=15)
    parser.add_argument("--reload-timeout", type=int, default=150)
    parser.add_argument(
        "--skip-runtime-verify",
        action="store_true",
        help="Skip per-instance hot-reload polling during bulk writes; run a final audit afterwards.",
    )
    parser.add_argument("--report", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.batch_size < 1 or args.batch_size > 10:
        raise SystemExit("--batch-size must be between 1 and 10")
    converger = Converger(args)
    uids = converger.select_uids()
    report = pathlib.Path(args.report or f"/tmp/carher-model-converge-{utc_stamp()}.json")
    results: list[Result] = []
    batches = [uids[i : i + args.batch_size] for i in range(0, len(uids), args.batch_size)]
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "targets": len(uids), "batches": len(batches), "batch_size": args.batch_size, "report": str(report)}))
    for index, batch in enumerate(batches, 1):
        print(json.dumps({"event": "batch_start", "batch": index, "uids": batch}))
        for uid in batch:
            result = converger.process(uid)
            results.append(result)
            print(json.dumps({"event": "result", **result.__dict__}, ensure_ascii=False))
            write_report(report, args, results)
        print(json.dumps({
            "event": "batch_done", "batch": index,
            "success": sum(r.status == "success" for r in results),
            "dry_run": sum(r.status == "dry_run" for r in results),
            "skipped": sum(r.status == "skipped" for r in results),
            "failed": sum(r.status == "failed" for r in results),
        }))
        if index < len(batches) and args.batch_pause > 0:
            time.sleep(args.batch_pause)
    write_report(report, args, results)
    print(json.dumps({"event": "complete", "report": str(report), "summary": json.loads(report.read_text())["summary"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
