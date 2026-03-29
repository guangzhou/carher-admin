"""CarHer AI Operations Agent.

Natural language interface for cluster operations.
Accepts Chinese and English, calls internal APIs via function dispatch.

Requires AGENT_LLM_API_KEY and optionally AGENT_LLM_BASE_URL env vars.
Defaults to OpenAI-compatible API (works with OpenRouter, Azure, etc).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import aiohttp

from . import database as db
from . import k8s_ops
from . import config_gen
from . import deployer

logger = logging.getLogger("carher-admin")

AGENT_MODEL = os.environ.get("AGENT_MODEL", "openai/gpt-4o")
AGENT_API_KEY = os.environ.get("AGENT_LLM_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""))
AGENT_BASE_URL = os.environ.get("AGENT_LLM_BASE_URL", "https://openrouter.ai/api/v1")

SYSTEM_PROMPT = """You are CarHer Ops Agent — an AI operations assistant for a Kubernetes cluster
running 500+ CarHer instances (Feishu AI assistants).

You can:
1. Query: list instances, check status, search by filters, view stats
2. Lifecycle: start/stop/restart instances
3. Deploy: trigger deployments, check deploy status
4. Groups: manage deploy groups, move instances between groups
5. Diagnose: read Pod logs, analyze errors, check health
6. Config: preview configurations

IMPORTANT RULES:
- Always confirm destructive actions (delete, purge) before executing
- For batch operations on >10 instances, summarize the plan first
- Return structured JSON in tool_calls when taking action
- Respond in the same language as the user's message

Available tools (call by returning JSON tool_calls):
"""

TOOLS = [
    {
        "name": "list_instances",
        "description": "List all instances with optional filters",
        "parameters": {
            "status": "optional: Running/Stopped/Failed",
            "model": "optional: gpt/sonnet/opus",
            "deploy_group": "optional: group name",
            "name": "optional: name search",
        },
    },
    {
        "name": "get_instance",
        "description": "Get detailed info about a specific instance",
        "parameters": {"uid": "required: instance ID (integer)"},
    },
    {
        "name": "get_stats",
        "description": "Get cluster statistics (counts, distributions)",
        "parameters": {},
    },
    {
        "name": "get_health",
        "description": "Get health status of all running instances",
        "parameters": {},
    },
    {
        "name": "restart_instance",
        "description": "Restart a specific instance",
        "parameters": {"uid": "required: instance ID"},
    },
    {
        "name": "stop_instance",
        "description": "Stop a specific instance",
        "parameters": {"uid": "required: instance ID"},
    },
    {
        "name": "start_instance",
        "description": "Start a stopped instance",
        "parameters": {"uid": "required: instance ID"},
    },
    {
        "name": "get_logs",
        "description": "Get Pod logs for an instance",
        "parameters": {"uid": "required: instance ID", "tail": "optional: number of lines (default 100)"},
    },
    {
        "name": "search_instances",
        "description": "Search instances with filters, returns matching list",
        "parameters": {
            "status": "optional", "model": "optional", "deploy_group": "optional",
            "owner": "optional", "name": "optional",
        },
    },
    {
        "name": "set_deploy_group",
        "description": "Move an instance to a deploy group",
        "parameters": {"uid": "required: instance ID", "group": "required: target group name"},
    },
    {
        "name": "create_deploy_group",
        "description": "Create a new deploy group",
        "parameters": {"name": "required", "priority": "required: integer", "description": "optional"},
    },
    {
        "name": "start_deploy",
        "description": "Start a deployment with specified image tag and mode",
        "parameters": {"image_tag": "required", "mode": "optional: normal/fast/canary-only"},
    },
    {
        "name": "get_deploy_status",
        "description": "Get current deployment status",
        "parameters": {},
    },
    {
        "name": "get_events",
        "description": "Get K8s events for an instance",
        "parameters": {"uid": "required: instance ID"},
    },
    {
        "name": "batch_action",
        "description": "Perform action on multiple instances",
        "parameters": {"uids": "required: list of IDs", "action": "required: stop/start/restart"},
    },
]


def _build_system_prompt() -> str:
    tools_desc = json.dumps(TOOLS, indent=2, ensure_ascii=False)
    return SYSTEM_PROMPT + tools_desc


async def _call_llm(messages: list[dict]) -> str:
    """Call LLM API (OpenAI-compatible)."""
    if not AGENT_API_KEY:
        return json.dumps({
            "answer": "AI Agent 未配置 (需要设置 AGENT_LLM_API_KEY 环境变量)",
            "actions_taken": [],
            "suggestions": ["设置 AGENT_LLM_API_KEY 环境变量后重启 admin"],
        })

    headers = {
        "Authorization": f"Bearer {AGENT_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": AGENT_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 4096,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{AGENT_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error("LLM API error %d: %s", resp.status, text[:200])
                return json.dumps({
                    "answer": f"LLM API 调用失败 (HTTP {resp.status})",
                    "actions_taken": [],
                    "suggestions": [],
                })
            data = await resp.json()
            choices = data.get("choices")
            if not choices or not isinstance(choices, list):
                logger.error("LLM API returned unexpected structure: %s", json.dumps(data)[:200])
                return json.dumps({
                    "answer": "LLM 返回格式异常，请稍后重试",
                    "actions_taken": [],
                    "suggestions": [],
                })
            return choices[0].get("message", {}).get("content", "")


def _execute_tool(name: str, params: dict, dry_run: bool = False) -> dict:
    """Execute a tool call and return the result."""
    result: dict[str, Any] = {"tool": name, "params": params}

    if dry_run:
        result["dry_run"] = True
        result["would_execute"] = f"{name}({json.dumps(params, ensure_ascii=False)})"
        return result

    try:
        if name == "list_instances":
            instances = db.list_all()
            filtered = [i for i in instances if i["status"] != "deleted"]
            if params.get("status"):
                pod_statuses = k8s_ops.get_all_pod_statuses()
                target = params["status"].lower()
                filtered = [i for i in filtered
                            if (pod_statuses.get(i["id"], {}).get("phase", "Stopped") or "Stopped").lower() == target]
            if params.get("model"):
                filtered = [i for i in filtered if i.get("model") == params["model"]]
            if params.get("deploy_group"):
                filtered = [i for i in filtered if i.get("deploy_group") == params["deploy_group"]]
            if params.get("name"):
                filtered = [i for i in filtered if params["name"].lower() in i.get("name", "").lower()]
            result["data"] = [{"id": i["id"], "name": i["name"], "model": i["model"], "deploy_group": i.get("deploy_group", "stable")} for i in filtered]
            result["count"] = len(filtered)

        elif name == "get_instance":
            uid = int(params["uid"])
            inst = db.get_by_id(uid)
            if not inst:
                result["error"] = f"Instance {uid} not found"
            else:
                pod = k8s_ops.get_pod_status(uid)
                health = k8s_ops.check_pod_health(uid) if pod.get("pod_exists") else {}
                result["data"] = {
                    "id": uid, "name": inst["name"], "model": inst["model"],
                    "status": pod.get("phase", "Stopped"),
                    "feishu_ws": health.get("feishu_ws", False),
                    "restarts": pod.get("restarts", 0),
                    "deploy_group": inst.get("deploy_group", "stable"),
                    "owner": inst.get("owner", ""),
                }

        elif name == "get_stats":
            instances = db.list_all()
            active = [i for i in instances if i["status"] != "deleted"]
            model_dist = {}
            for i in active:
                m = i.get("model", "gpt")
                model_dist[m] = model_dist.get(m, 0) + 1
            result["data"] = {
                "total": len(active),
                "stopped": sum(1 for i in active if i["status"] == "stopped"),
                "model_distribution": model_dist,
                "deploy_groups": db.get_deploy_group_stats(),
                "current_image": db.get_current_image_tag(),
            }

        elif name == "get_health":
            instances = db.list_all()
            pod_statuses = k8s_ops.get_all_pod_statuses()
            unhealthy = []
            for inst in instances:
                uid = inst["id"]
                if uid not in pod_statuses or inst["status"] == "deleted":
                    continue
                health = k8s_ops.check_pod_health(uid)
                if not health.get("feishu_ws"):
                    unhealthy.append({"id": uid, "name": inst["name"], "feishu_ws": False})
            result["data"] = {"unhealthy_count": len(unhealthy), "unhealthy": unhealthy[:20]}

        elif name == "restart_instance":
            uid = int(params["uid"])
            k8s_ops.delete_pod(uid)
            inst = db.get_by_id(uid)
            if inst:
                config_json = config_gen.generate_json_string(inst)
                k8s_ops.apply_configmap(uid, config_json)
                k8s_ops.create_pod(uid, prefix=inst.get("prefix", "s1"))
            result["data"] = {"id": uid, "action": "restarted"}

        elif name == "stop_instance":
            uid = int(params["uid"])
            k8s_ops.delete_pod(uid)
            db.set_status(uid, "stopped")
            result["data"] = {"id": uid, "action": "stopped"}

        elif name == "start_instance":
            uid = int(params["uid"])
            inst = db.get_by_id(uid)
            if inst:
                config_json = config_gen.generate_json_string(inst)
                k8s_ops.apply_configmap(uid, config_json)
                k8s_ops.create_pod(uid, prefix=inst.get("prefix", "s1"))
                db.set_status(uid, "running")
            result["data"] = {"id": uid, "action": "started"}

        elif name == "get_logs":
            uid = int(params["uid"])
            tail = int(params.get("tail", 100))
            logs = k8s_ops.get_logs(uid, tail=tail)
            result["data"] = {"id": uid, "logs": logs[-3000:] if len(logs) > 3000 else logs}

        elif name == "search_instances":
            return _execute_tool("list_instances", params, dry_run)

        elif name == "set_deploy_group":
            uid = int(params["uid"])
            group = params["group"]
            db.set_deploy_group(uid, group)
            result["data"] = {"id": uid, "deploy_group": group}

        elif name == "create_deploy_group":
            db.create_deploy_group(params["name"], int(params.get("priority", 100)), params.get("description", ""))
            result["data"] = {"name": params["name"], "priority": params.get("priority", 100)}

        elif name == "start_deploy":
            image_tag = params.get("image_tag", "")
            mode = params.get("mode", "canary-only")
            if not image_tag:
                result["error"] = "image_tag is required"
            else:
                result["data"] = {"note": f"Use POST /api/deploy with image_tag={image_tag} mode={mode}. Agent cannot trigger deploys directly for safety."}

        elif name == "get_deploy_status":
            result["data"] = deployer.get_deploy_status()

        elif name == "get_events":
            uid = int(params["uid"])
            try:
                v1 = k8s_ops._core()
                events = v1.list_namespaced_event("carher", field_selector=f"involvedObject.name=carher-{uid}")
                items = sorted(events.items, key=lambda e: e.last_timestamp or e.event_time or "", reverse=True)[:10]
                result["data"] = [{"type": e.type, "reason": e.reason, "message": e.message} for e in items]
            except Exception as e:
                result["error"] = str(e)

        elif name == "batch_action":
            uids = [int(u) for u in params.get("uids", [])]
            action = params.get("action", "")
            if action not in ("restart", "stop", "start"):
                result["error"] = f"Unknown batch action: {action}"
                return result
            done, failed = [], []
            for uid in uids:
                try:
                    if action == "restart":
                        _execute_tool("restart_instance", {"uid": uid})
                    elif action == "stop":
                        _execute_tool("stop_instance", {"uid": uid})
                    elif action == "start":
                        _execute_tool("start_instance", {"uid": uid})
                    done.append(uid)
                except Exception as e:
                    failed.append({"uid": uid, "error": str(e)})
            result["data"] = {"action": action, "done_count": len(done), "done_ids": done, "failed": failed}

        else:
            result["error"] = f"Unknown tool: {name}"

    except Exception as e:
        result["error"] = str(e)

    return result


def _extract_tool_calls(text: str) -> list[dict]:
    """Extract tool calls from LLM response. Supports JSON blocks and inline."""
    calls = []
    json_pattern = re.compile(r'```json\s*(.*?)\s*```', re.DOTALL)
    for match in json_pattern.finditer(text):
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict) and "tool" in data:
                calls.append(data)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "tool" in item:
                        calls.append(item)
        except json.JSONDecodeError:
            pass

    if not calls:
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "tool" in data:
                calls.append(data)
        except (json.JSONDecodeError, ValueError):
            pass

    return calls


async def handle_message(message: str, context: dict | None = None, dry_run: bool = False) -> dict:
    """Main entry: process a natural language message and return structured response."""
    messages = [
        {"role": "system", "content": _build_system_prompt()},
    ]

    if context:
        messages.append({"role": "system", "content": f"Context: {json.dumps(context, ensure_ascii=False)}"})

    if dry_run:
        messages.append({"role": "system", "content": "DRY RUN MODE: only describe actions, do not execute."})

    messages.append({"role": "user", "content": message})

    llm_response = await _call_llm(messages)

    try:
        direct = json.loads(llm_response)
        if "answer" in direct:
            return direct
    except (json.JSONDecodeError, ValueError):
        pass

    tool_calls = _extract_tool_calls(llm_response)
    actions_taken = []

    if tool_calls:
        for tc in tool_calls:
            tool_name = tc.get("tool", "")
            tool_params = tc.get("params", {})
            result = _execute_tool(tool_name, tool_params, dry_run=dry_run)
            actions_taken.append(result)

        tool_results_str = json.dumps(actions_taken, ensure_ascii=False, default=str)
        messages.append({"role": "assistant", "content": llm_response})
        messages.append({"role": "user", "content": f"Tool results:\n{tool_results_str}\n\nSummarize the results for the user."})
        summary = await _call_llm(messages)
    else:
        summary = llm_response

    clean_summary = re.sub(r'```json\s*.*?\s*```', '', summary, flags=re.DOTALL).strip()
    if not clean_summary:
        clean_summary = summary

    return {
        "answer": clean_summary,
        "actions_taken": actions_taken,
        "suggestions": [],
    }
