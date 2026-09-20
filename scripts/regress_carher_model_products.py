#!/usr/bin/env python3
"""Run product-name model regressions through selected CarHer Pods."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import subprocess
import time
from typing import Any


NAMESPACE = "carher"
CHAT_MODELS = [
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
KEY_MODELS = CHAT_MODELS + ["bge-m3"]
ALIASES = {
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


def run(args: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout)


def kubectl_json(args: list[str]) -> Any:
    proc = run(["kubectl", "-n", NAMESPACE, *args, "-o", "json"])
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:500])
    return json.loads(proc.stdout)


def ready_pods() -> dict[str, str]:
    result: dict[str, str] = {}
    for pod in kubectl_json(["get", "pods"])["items"]:
        labels = pod["metadata"].get("labels", {})
        uid = labels.get("user-id")
        statuses = pod.get("status", {}).get("containerStatuses") or []
        if (
            labels.get("app") == "carher-user"
            and uid
            and pod.get("status", {}).get("phase") == "Running"
            and statuses
            and all(row.get("ready") for row in statuses)
        ):
            result[uid] = pod["metadata"]["name"]
    return result


def pod_probe_script() -> str:
    return r'''
import concurrent.futures,json,os,pathlib,time,urllib.error,urllib.parse,urllib.request

BASE='http://litellm-proxy.carher.svc.cluster.local:4000'
KEY=os.environ['LITELLM_API_KEY']
CHAT=%(chat)s
EXPECTED=%(expected)s
ALIASES=%(aliases)s

def request(method,path,payload=None,timeout=120):
 data=None if payload is None else json.dumps(payload).encode()
 req=urllib.request.Request(BASE+path,data=data,method=method,headers={
  'Authorization':'Bearer '+KEY,'Content-Type':'application/json'})
 started=time.monotonic()
 try:
  with urllib.request.urlopen(req,timeout=timeout) as response:
   body=response.read().decode(errors='replace')
   return response.status,time.monotonic()-started,dict(response.headers.items()),body
 except urllib.error.HTTPError as exc:
  return exc.code,time.monotonic()-started,dict(exc.headers.items()),exc.read().decode(errors='replace')
 except Exception as exc:
  return 0,time.monotonic()-started,{},type(exc).__name__+': '+str(exc)

def chat(model):
 status,elapsed,headers,body=request('POST','/v1/chat/completions',{
  'model':model,'messages':[{'role':'user','content':'Reply with exactly PONG.'}],
  'max_tokens':16,'stream':False})
 row={'model':model,'kind':'chat','status':status,'seconds':round(elapsed,2),
      'route':headers.get('x-litellm-model-id') or headers.get('X-LiteLLM-Model-Id')}
 try:
  data=json.loads(body);row['response_model']=data.get('model')
  choices=data.get('choices') or []
  if choices: row['text']=str((choices[0].get('message') or {}).get('content') or '')[:80]
  if status!=200: row['error']=str(data.get('error') or data)[:240]
 except Exception:
  if status!=200: row['error']=body[:240]
 return row

status,elapsed,headers,body=request('GET','/v1/models')
try: visible=sorted(row['id'] for row in json.loads(body).get('data',[]))
except Exception: visible=[]

encoded=urllib.parse.quote(KEY,safe='')
kstatus,kelapsed,kheaders,kbody=request('GET','/key/info?key='+encoded)
try:
 info=json.loads(kbody).get('info') or {}
 key_models=info.get('models') or []
 key_aliases=info.get('aliases') or {}
except Exception:
 key_models=[];key_aliases={}

runtime={'openclaw':'failed','hermes':'failed'}
try:
 d=json.loads(pathlib.Path('/data/.openclaw/openclaw.json').read_text())
 aliases=sorted(x.removeprefix('litellm/') for x in d['agents']['defaults']['models'])
 providers=sorted(x['id'] for x in d['models']['providers']['litellm']['models'])
 runtime['openclaw']='ok' if (d['agents']['defaults']['model']['primary']=='litellm/gpt-5.6-terra'
  and aliases==sorted(CHAT) and providers==sorted(CHAT)) else 'mismatch'
except Exception as exc: runtime['openclaw_error']=type(exc).__name__+': '+str(exc)
try:
 import yaml
 paths=[pathlib.Path('/opt/data/.hermes/config-litellm.yaml'),pathlib.Path('/opt/data/.hermes/config.yaml')]
 found=[p for p in paths if p.exists()]
 bad=[]
 for p in found:
  d=yaml.safe_load(p.read_text())
  models=sorted(d['providers']['litellm']['models'])
  if d['model']['default']!='gpt-5.6-terra' or models!=sorted(CHAT): bad.append(str(p))
 runtime['hermes']='ok' if found and not bad else 'mismatch'
 runtime['hermes_files']=[str(p) for p in found]
except Exception as exc: runtime['hermes_error']=type(exc).__name__+': '+str(exc)

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
 results=list(pool.map(chat,CHAT))

status,elapsed,headers,body=request('POST','/v1/embeddings',{'model':'bge-m3','input':'carher regression probe'})
embedding={'model':'bge-m3','kind':'embedding','status':status,'seconds':round(elapsed,2),
 'route':headers.get('x-litellm-model-id') or headers.get('X-LiteLLM-Model-Id')}
try:
 data=json.loads(body);rows=data.get('data') or []
 if rows: embedding['dimensions']=len(rows[0].get('embedding') or [])
 if status!=200: embedding['error']=str(data.get('error') or data)[:240]
except Exception:
 if status!=200: embedding['error']=body[:240]
results.append(embedding)

print(json.dumps({
 'models_http':status if False else 200 if visible else 0,
 'visible_models':visible,
 'visible_exact':visible==sorted(EXPECTED),
 'key_info_http':kstatus,
 'key_models_exact':key_models==EXPECTED,
 'key_aliases_exact':key_aliases==ALIASES,
 'runtime':runtime,
 'results':results,
},ensure_ascii=False))
''' % {"chat": repr(CHAT_MODELS), "expected": repr(KEY_MODELS), "aliases": repr(ALIASES)}


def probe(uid: str, pod: str) -> dict[str, Any]:
    encoded = base64.b64encode(pod_probe_script().encode()).decode()
    command = f"import base64;exec(base64.b64decode('{encoded}'))"
    proc = run(
        ["kubectl", "-n", NAMESPACE, "exec", "-i", pod, "-c", "carher", "--", "python3", "-c", command],
        timeout=600,
    )
    if proc.returncode:
        return {"uid": uid, "pod": pod, "status": "probe_failed", "error": (proc.stderr or proc.stdout).strip()[:500]}
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {"uid": uid, "pod": pod, "status": "invalid_output", "error": str(exc), "output": proc.stdout[-500:]}
    failed = [row for row in data["results"] if row.get("status") != 200]
    checks = [
        data.get("visible_exact"),
        data.get("key_info_http") == 200,
        data.get("key_models_exact"),
        data.get("key_aliases_exact"),
        data.get("runtime", {}).get("openclaw") == "ok",
        data.get("runtime", {}).get("hermes") == "ok",
        not failed,
    ]
    data.update({"uid": uid, "pod": pod, "status": "passed" if all(checks) else "failed", "failed_models": failed})
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uids", default="1000,10,266,337")
    parser.add_argument("--report", default="/tmp/carher-model-product-regression.json")
    args = parser.parse_args()
    uids = [value.strip() for value in args.uids.split(",") if value.strip()]
    pods = ready_pods()
    results = []
    for uid in uids:
        if uid not in pods:
            results.append({"uid": uid, "status": "skipped", "error": "no ready Pod"})
            continue
        result = probe(uid, pods[uid])
        results.append(result)
        print(json.dumps({
            "uid": uid,
            "status": result["status"],
            "passed_models": sum(row.get("status") == 200 for row in result.get("results", [])),
            "failed_models": [row["model"] for row in result.get("failed_models", [])],
        }, ensure_ascii=False), flush=True)
    summary = {
        "samples": len(results),
        "passed": sum(row["status"] == "passed" for row in results),
        "failed": sum(row["status"] == "failed" for row in results),
        "skipped": sum(row["status"] == "skipped" for row in results),
        "model_calls": sum(len(row.get("results", [])) for row in results),
        "model_calls_ok": sum(sum(item.get("status") == 200 for item in row.get("results", [])) for row in results),
    }
    with open(args.report, "w") as handle:
        json.dump({"summary": summary, "results": results}, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"summary": summary, "report": args.report}, ensure_ascii=False))
    return 0 if summary["failed"] == 0 and summary["skipped"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
