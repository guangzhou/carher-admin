"""compaction-drop: chatgpt /v1/responses input items 必须**整项 drop** reasoning + encrypted_content。

[[litellm-chatgpt-compaction-drop]]:
  上游 (chatgpt + wangsu) 拒收带 encrypted_content 的 input item, 返 400 missing required parameter。
  LiteLLM chatgpt_responses_normalize._normalize_item 早期只 pop encrypted_content 字段不删项 →
  剩个 type=reasoning 空壳上游同样 400。

修复策略 (3 行):
  for item in input:
    if item.type == "reasoning": drop
    elif "encrypted_content" in serialize(item): drop  # 同策略
"""
from __future__ import annotations

import json
from typing import Any


def _has_encrypted_content(obj: Any) -> bool:
    if isinstance(obj, dict):
        if "encrypted_content" in obj:
            return True
        return any(_has_encrypted_content(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_encrypted_content(v) for v in obj)
    return False


def drop_compaction_items(input_items: list[dict]) -> list[dict]:
    """Filter input items: drop type=reasoning + 任何包含 encrypted_content 的项。"""
    out: list[dict] = []
    for item in input_items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        if item.get("type") == "reasoning":
            continue
        if _has_encrypted_content(item):
            continue
        out.append(item)
    return out


def apply_to_responses_body(body: dict) -> dict:
    """对 /v1/responses request body 应用 compaction-drop, 返回新 dict (浅 copy)。"""
    if "input" not in body or not isinstance(body["input"], list):
        return body
    new = dict(body)
    new["input"] = drop_compaction_items(body["input"])
    return new
