#!/usr/bin/env python3
"""
cc-acct-status.py — 查所有 anthropic-auth 账号的 token 健康状态
在 188 上跑:
  ssh cltx@10.68.13.188 'python3 /tmp/cc-acct-status.py'

输出表格:
  acct    plan        opus    sonnet   haiku    说明
  acct-1  Team Tiger  ❌RL    ❌RL     ✅       Opus/Sonnet 共享池打满
"""
import json, subprocess, urllib.request, urllib.error, sys, os
from pathlib import Path

AUTH_DIR = "/Data/anthropic-auth"
MODELS = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"]

def probe(token, model):
    """1-token probe; return (status_code, body_excerpt)"""
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-dangerous-direct-browser-access": "true",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        data=json.dumps({
            "model": model,
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "x"}],
        }).encode(),
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, "OK"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:150]
        return e.code, body
    except Exception as e:
        return 0, str(e)[:150]

def classify(code, body):
    if code == 200:
        return "✅"
    if "rate_limit" in body or code == 429:
        return "🟠RL"
    if code in (401, 403):
        return "🔴AUTH"
    return f"❓{code}"

print(f"{'acct':<10} {'opus':<8} {'sonnet':<8} {'haiku':<8}  说明")
print("-" * 65)

for d in sorted(Path(AUTH_DIR).iterdir() if Path(AUTH_DIR).exists() else []):
    if not d.is_dir() or not d.name.startswith("acct-"):
        continue
    env_path = d / ".env"
    if not env_path.exists():
        print(f"{d.name:<10} {'NO-ENV':<30}  缺 .env 文件")
        continue
    # parse .env
    token = None
    for line in env_path.read_text().splitlines():
        if line.startswith("ANTHROPIC_OAUTH_TOKEN="):
            token = line.split("=", 1)[1].strip()
            break
    if not token:
        print(f"{d.name:<10} {'NO-TOKEN':<30}  .env 无 ANTHROPIC_OAUTH_TOKEN")
        continue

    results = {}
    for model in MODELS:
        code, body = probe(token, model)
        results[model] = classify(code, body)

    notes = []
    if results["claude-opus-4-7"].startswith("🟠") or results["claude-sonnet-4-6"].startswith("🟠"):
        notes.append("Team 共享池打满")
    if results["claude-haiku-4-5"].startswith("🔴"):
        notes.append("token 整体失效")
    note_str = ";".join(notes) if notes else "正常"

    print(f"{d.name:<10} {results['claude-opus-4-7']:<8} {results['claude-sonnet-4-6']:<8} {results['claude-haiku-4-5']:<8}  {note_str}")
