#!/usr/bin/env python3
"""Patch 198 chatgpt-acct pods for Claude Messages web_search compatibility.

Fixes two Anthropic /v1/messages -> Responses -> ChatGPT backend shape mismatches:
  1. tools[].type web_search_preview -> web_search
  2. forced tool_choice {type:function,name:web_search} -> {type:web_search}

Default mode is dry-run. Run on 198 / AIYJY-litellm where kubectl can reach
namespace litellm-product:

    python3 patch_chatgpt_web_search.py --dry-run
    python3 patch_chatgpt_web_search.py --apply
    python3 patch_chatgpt_web_search.py --verify-only --acct 109

The script patches both source-tree and site-packages copies inside every
chatgpt-acct-* deployment template, then rolls deployments and verifies the
new pods. It does not patch litellm-proxy; the outbound ChatGPT provider layer
is the blast-radius boundary.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from typing import Iterable

NS = "litellm-product"
LABEL_PREFIX = "chatgpt-acct-"
PATCH_PATHS = [
    "/app/litellm/llms/chatgpt/responses/transformation.py",
    "/app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py",
]
MARKER = "carher_web_search_tool_choice_patch"

PATCH_SNIPPET = r'''
        # carher_web_search_tool_choice_patch: Claude /v1/messages -> Responses
        # maps Anthropic web search to OpenAI-style web_search_preview and forced
        # tool choice to {type:function,name:web_search}. ChatGPT Codex backend
        # accepts web_search and {type:web_search}; normalize at the provider exit.
        tools = request.get("tools")
        if isinstance(tools, list):
            rewritten_tools = []
            changed_tools = False
            for tool in tools:
                if isinstance(tool, dict) and tool.get("type") == "web_search_preview":
                    new_tool = dict(tool)
                    new_tool["type"] = "web_search"
                    rewritten_tools.append(new_tool)
                    changed_tools = True
                else:
                    rewritten_tools.append(tool)
            if changed_tools:
                request["tools"] = rewritten_tools
        tool_choice = request.get("tool_choice")
        if isinstance(tool_choice, dict):
            tc_type = tool_choice.get("type")
            tc_name = tool_choice.get("name") or (tool_choice.get("function") or {}).get("name")
            if tc_type == "web_search_preview" or (tc_type == "function" and tc_name == "web_search"):
                request["tool_choice"] = {"type": "web_search"}
'''


def run(args: list[str], *, input_text: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(args, input=input_text, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(args)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc.stdout


def deployment_names(accts: list[str] | None) -> list[str]:
    if accts:
        return [f"chatgpt-acct-{a}" for a in accts]
    raw = run(["kubectl", "-n", NS, "get", "deploy", "-o", "json"])
    data = json.loads(raw)
    names = [item["metadata"]["name"] for item in data.get("items", [])]
    return sorted(n for n in names if n.startswith(LABEL_PREFIX))


def current_pod(deploy: str) -> str:
    raw = run([
        "kubectl", "-n", NS, "get", "pod", "-l", f"app={deploy}",
        "-o", "jsonpath={.items[0].metadata.name}",
    ])
    if not raw:
        raise RuntimeError(f"no pod found for {deploy}")
    return raw


def pod_has_patch(pod: str) -> bool:
    script = "\n".join([
        "from pathlib import Path",
        f"paths = {PATCH_PATHS!r}",
        f"marker = {MARKER!r}",
        "ok = True",
        "for p in paths:",
        "    text = Path(p).read_text()",
        "    print(p, marker in text, 'tool_choice_web_search=', '{\\\"type\\\": \\\"web_search\\\"}' in text or \"{'type': 'web_search'}\" in text)",
        "    ok = ok and marker in text",
        "raise SystemExit(0 if ok else 1)",
    ])
    rc = subprocess.run(
        ["kubectl", "-n", NS, "exec", pod, "--", "python", "-c", script],
        text=True,
    ).returncode
    return rc == 0


def patch_command() -> str:
    payload = {
        "paths": PATCH_PATHS,
        "marker": MARKER,
        "snippet": PATCH_SNIPPET,
    }
    inner = r'''
import json
from pathlib import Path
cfg = json.loads(open('/tmp/carher-web-search-patch.json').read())
needle = 'request["stream"] = True\n'
for p in cfg['paths']:
    path = Path(p)
    text = path.read_text()
    if cfg['marker'] in text:
        print(f'{p}: already patched')
        continue
    if needle not in text:
        raise SystemExit(f'{p}: insertion anchor missing')
    text = text.replace(needle, needle + cfg['snippet'], 1)
    path.write_text(text)
    print(f'{p}: patched')
'''
    return (
        "cat >/tmp/carher-web-search-patch.json <<'JSON'\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\nJSON\npython /tmp/carher-web-search-patch.json >/dev/null 2>&1 || true\npython - <<'PY'\n"
        + inner
        + "\nPY"
    )


def patch_deploy(deploy: str) -> None:
    cmd = patch_command()
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "litellm",
                            "lifecycle": {
                                "postStart": {
                                    "exec": {"command": ["/bin/sh", "-lc", cmd]}
                                }
                            },
                        }
                    ]
                }
            }
        }
    }
    run([
        "kubectl", "-n", NS, "patch", "deployment", deploy,
        "--type", "strategic", "-p", json.dumps(patch, ensure_ascii=False),
    ])


def rollout_and_verify(deploys: Iterable[str]) -> tuple[int, list[str]]:
    failed: list[str] = []
    total = 0
    for deploy in deploys:
        total += 1
        print(f"rollout {deploy}")
        try:
            run(["kubectl", "-n", NS, "rollout", "status", f"deployment/{deploy}", "--timeout=180s"])
            pod = current_pod(deploy)
            if not pod_has_patch(pod):
                failed.append(deploy)
        except Exception as exc:
            print(f"!! {deploy}: {exc}", file=sys.stderr)
            failed.append(deploy)
    return total, failed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="patch deployment templates and rollout")
    parser.add_argument("--dry-run", action="store_true", help="print target deployments only")
    parser.add_argument("--verify-only", action="store_true", help="verify current pods contain the patch")
    parser.add_argument("--acct", action="append", help="limit to account number, repeatable")
    args = parser.parse_args(argv)

    deploys = deployment_names(args.acct)
    if not deploys:
        print("no target deployments")
        return 1
    print(f"targets={len(deploys)}", " ".join(deploys[:20]) + (" ..." if len(deploys) > 20 else ""))

    if args.verify_only:
        bad = []
        for deploy in deploys:
            pod = current_pod(deploy)
            ok = pod_has_patch(pod)
            print(f"{deploy} {pod} patched={ok}")
            if not ok:
                bad.append(deploy)
        print(f"verify-only: ok={len(deploys)-len(bad)} bad={len(bad)}")
        return 0 if not bad else 2

    if not args.apply:
        print("dry-run only; pass --apply to patch and rollout")
        return 0

    for deploy in deploys:
        print(f"patch template {deploy}")
        patch_deploy(deploy)
    total, failed = rollout_and_verify(deploys)
    print(f"done: total={total} failed={len(failed)}")
    if failed:
        print("failed:", " ".join(failed), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
