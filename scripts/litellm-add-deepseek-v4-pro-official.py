#!/usr/bin/env python3
"""Register the official-direct DeepSeek V4 Pro on the 198 ``litellm-product``.

What it does (all via the LiteLLM admin HTTP API — zero restart, DB-managed):

  1. Register ``deepseek-v4-pro`` (chat)  -> custom_openai/deepseek-v4-pro
  2. Register ``deepseek-v4-pro-responses`` (responses) -> openai/deepseek-v4-pro
     both at https://api.deepseek.com/v1, id deepseek-official/deepseek-v4-pro[-responses],
     Pro list price in=4.35e-07 out=8.7e-07 (double-written to litellm_params+model_info).
  3. Delete the OpenRouter member of the ``deepseek-v4-pro`` group so official is
     exclusive (guarded: only deletes a deployment whose id starts "openrouter/").
  4. Probe both new groups with tool-bearing payloads before declaring success
     (a DeepSeek reasoning model returns HTTP 200 with empty content when
     max_tokens is too low — see feedback_deepseek_flash_stream_only_nonstream_500 —
     so the chat probe uses max_tokens>=2000 and asserts non-empty content).

It does NOT touch router_settings.fallbacks. The gpt-family fallback append and
the deepseek-v4-pro chat fallback are separate jsonb_set edits (see
scripts/litellm-198-gpt-fallback-append.py and the runbook).

Secret hygiene: the DeepSeek key is read from the DEEPSEEK_API_KEY env var
(same account/key as the existing official flash entries and the Aliyun
litellm-secrets). It is never hardcoded, logged, or written to any backup; any
diagnostic dump redacts api_key.

Environment:
  LITELLM_BASE        default http://127.0.0.1:30402  (node-local NodePort on 198)
  LITELLM_MASTER_KEY  required (no default)
  DEEPSEEK_API_KEY    required for --apply (the official api.deepseek.com key)

Examples:
  # preview payloads + planned deletion (no writes, no key needed)
  LITELLM_MASTER_KEY=... python3 scripts/litellm-add-deepseek-v4-pro-official.py

  # apply
  LITELLM_MASTER_KEY=... DEEPSEEK_API_KEY=sk-... \
      python3 scripts/litellm-add-deepseek-v4-pro-official.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

API_BASE = "https://api.deepseek.com/v1"
IN_COST = 0.000000435   # $0.435 / 1M input tokens (cache-miss)
OUT_COST = 0.00000087   # $0.87  / 1M output tokens
MAX_IN = 393216
MAX_OUT = 65536

CHAT_GROUP = "deepseek-v4-pro"
RESPONSES_GROUP = "deepseek-v4-pro-responses"


def build_model_new_payload(
    *, model_name: str, litellm_model: str, mode: str, model_id: str, api_key: str
) -> dict:
    """Pure builder for a /model/new body. Mirrors the official flash entries,
    with Pro pricing and mode. Cost is double-written (litellm_params + model_info)
    because 198 has SpendLogs disabled and relies on cost fields for billing."""
    return {
        "model_name": model_name,
        "litellm_params": {
            "model": litellm_model,
            "api_key": api_key,
            "api_base": API_BASE,
            "input_cost_per_token": IN_COST,
            "output_cost_per_token": OUT_COST,
            "merge_reasoning_content_in_choices": False,
        },
        "model_info": {
            "id": model_id,
            "mode": mode,
            "input_cost_per_token": IN_COST,
            "output_cost_per_token": OUT_COST,
            "max_input_tokens": MAX_IN,
            "max_output_tokens": MAX_OUT,
        },
    }


def _redact(payload: dict) -> dict:
    clone = json.loads(json.dumps(payload))
    if clone.get("litellm_params", {}).get("api_key"):
        clone["litellm_params"]["api_key"] = "***REDACTED***"
    return clone


# --------------------------------------------------------------------------- #
# I/O (only used by main()).
# --------------------------------------------------------------------------- #
def _base() -> str:
    return os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402").rstrip("/")


def _master() -> str:
    mk = os.environ.get("LITELLM_MASTER_KEY")
    if not mk:
        raise SystemExit("LITELLM_MASTER_KEY is required")
    return mk


def api(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_base() + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_master()}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def _model_info_rows() -> list[dict]:
    d = api("GET", "/v1/model/info")
    return d["data"] if isinstance(d, dict) and "data" in d else d


def _group_member_ids(rows: list[dict], group: str) -> list[str]:
    ids = []
    for r in rows:
        if r.get("model_name") == group:
            mid = (r.get("model_info") or {}).get("id")
            if mid:
                ids.append(mid)
    return ids


def _probe_chat() -> None:
    body = {
        "model": CHAT_GROUP,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": "Reply with the single word PONG."}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }],
    }
    d = api("POST", "/v1/chat/completions", body)
    choice = (d.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content")
    tool_calls = msg.get("tool_calls")
    if not (content or tool_calls):
        raise SystemExit(f"chat probe: empty content and no tool_calls: {json.dumps(d)[:400]}")
    print(f"  chat probe OK: model={d.get('model')} "
          f"content_len={len(content or '')} tool_calls={len(tool_calls or [])}")


def _probe_responses() -> None:
    body = {
        "model": RESPONSES_GROUP,
        "max_output_tokens": 2000,
        "input": [{"role": "user", "content": "Reply with the single word PONG."}],
        "tools": [{
            "type": "function",
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }],
        "reasoning": {"summary": "auto"},
    }
    d = api("POST", "/v1/responses", body)
    status = d.get("status")
    if status != "completed":
        raise SystemExit(f"responses probe: status={status!r}: {json.dumps(d)[:400]}")
    print(f"  responses probe OK: status=completed model={d.get('model')}")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--skip-delete-openrouter", action="store_true",
                   help="register only; leave the OpenRouter member in the group")
    a = p.parse_args()

    rows = _model_info_rows()
    existing_chat = _group_member_ids(rows, CHAT_GROUP)
    existing_resp = _group_member_ids(rows, RESPONSES_GROUP)
    print(f"current {CHAT_GROUP} members: {existing_chat}")
    print(f"current {RESPONSES_GROUP} members: {existing_resp}")

    chat_payload = build_model_new_payload(
        model_name=CHAT_GROUP, litellm_model="custom_openai/deepseek-v4-pro",
        mode="chat", model_id="deepseek-official/deepseek-v4-pro", api_key="<from-env>")
    resp_payload = build_model_new_payload(
        model_name=RESPONSES_GROUP, litellm_model="openai/deepseek-v4-pro",
        mode="responses", model_id="deepseek-official/deepseek-v4-pro-responses",
        api_key="<from-env>")

    need_chat = "deepseek-official/deepseek-v4-pro" not in existing_chat
    need_resp = "deepseek-official/deepseek-v4-pro-responses" not in existing_resp
    openrouter_ids = [i for i in existing_chat if i.startswith("openrouter/")]

    print("\nplanned /model/new (chat):")
    print(json.dumps(_redact(chat_payload), ensure_ascii=False, indent=2))
    print("planned /model/new (responses):")
    print(json.dumps(_redact(resp_payload), ensure_ascii=False, indent=2))
    print(f"\nchat register needed: {need_chat}; responses register needed: {need_resp}")
    print(f"openrouter members to delete: {openrouter_ids}"
          f"{' (skipped)' if a.skip_delete_openrouter else ''}")

    if not a.apply:
        print("\n[dry-run] no writes. add --apply (with DEEPSEEK_API_KEY set) to write.")
        return 0

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("--apply requires DEEPSEEK_API_KEY (never hardcoded)")

    if need_chat:
        chat_payload["litellm_params"]["api_key"] = api_key
        api("POST", "/model/new", chat_payload)
        print(f"registered {CHAT_GROUP} (chat)")
    else:
        print(f"{CHAT_GROUP} official chat already present; skip register")
    if need_resp:
        resp_payload["litellm_params"]["api_key"] = api_key
        api("POST", "/model/new", resp_payload)
        print(f"registered {RESPONSES_GROUP} (responses)")
    else:
        print(f"{RESPONSES_GROUP} already present; skip register")

    if openrouter_ids and not a.skip_delete_openrouter:
        # Re-read to get the live deployment id (db id), then delete by id.
        rows2 = _model_info_rows()
        for r in rows2:
            if r.get("model_name") != CHAT_GROUP:
                continue
            mi = r.get("model_info") or {}
            if str(mi.get("id", "")).startswith("openrouter/"):
                del_id = mi.get("id")
                api("POST", "/model/delete", {"id": del_id})
                print(f"deleted openrouter member id={del_id}")

    # Verify + probe against live state.
    rows3 = _model_info_rows()
    final_chat = _group_member_ids(rows3, CHAT_GROUP)
    final_resp = _group_member_ids(rows3, RESPONSES_GROUP)
    print(f"\nfinal {CHAT_GROUP} members: {final_chat}")
    print(f"final {RESPONSES_GROUP} members: {final_resp}")
    problems = []
    if "deepseek-official/deepseek-v4-pro" not in final_chat:
        problems.append("official pro chat missing after write")
    if "deepseek-official/deepseek-v4-pro-responses" not in final_resp:
        problems.append("official pro responses missing after write")
    if not a.skip_delete_openrouter and any(i.startswith("openrouter/") for i in final_chat):
        problems.append("openrouter member still present in chat group")
    if problems:
        print("VERIFY FAILED:", json.dumps(problems), file=sys.stderr)
        return 2
    print("\nprobing live groups ...")
    _probe_chat()
    _probe_responses()
    print("\nall good: official DeepSeek V4 Pro registered and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
