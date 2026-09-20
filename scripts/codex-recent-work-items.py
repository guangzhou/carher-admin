#!/usr/bin/env python3
"""Read recent Codex Desktop work items through the official app-server protocol.

This is intentionally read-only: it starts a short-lived local app-server,
asks for ``thread/list``, prints the response, and exits.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_SOURCES = ["cli", "vscode", "exec", "appServer", "unknown"]
DEFAULT_TIMEOUT = 8.0
SECRET_PATTERN = re.compile(
    r"(?i)\b(?:app[_ -]?secret|password|passwd|api[_ -]?key|access[_ -]?token|token|secret|key)\b\s*[:=]\s*\S+"
)
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+\S+")
API_KEY_PATTERN = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{10,}\b")


class QueryError(RuntimeError):
    """Raised when the local Codex app-server cannot answer the query."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List recent Codex Desktop work items via app-server thread/list."
    )
    parser.add_argument("--limit", type=int, default=10, help="number of items (default: 10)")
    parser.add_argument("--cwd", help="only include threads whose cwd exactly matches this path")
    parser.add_argument("--search", help="substring match on the title/preview")
    parser.add_argument(
        "--archived",
        action="store_true",
        help="list archived threads instead of active threads",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json", "tsv"),
        default="markdown",
        help="output format (default: markdown)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="app-server response timeout in seconds (default: 8)",
    )
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex",
        help="Codex executable (default: CODEX_BIN or PATH lookup)",
    )
    parser.add_argument(
        "--codex-home",
        help="override CODEX_HOME; by default inherit the current Codex Desktop home",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= 100:
        raise QueryError("--limit must be between 1 and 100")
    if args.timeout <= 0 or args.timeout > 60:
        raise QueryError("--timeout must be greater than 0 and no more than 60 seconds")
    if args.cwd:
        args.cwd = str(Path(args.cwd).expanduser().resolve())


def request_payload(args: argparse.Namespace) -> list[dict[str, Any]]:
    return [
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "carher-codex-recent-work-items",
                    "version": "1.0.0",
                },
                "capabilities": None,
            },
        },
        {
            "method": "thread/list",
            "id": 2,
            "params": {
                "limit": args.limit,
                "sortKey": "recency_at",
                "sortDirection": "desc",
                "archived": True if args.archived else False,
                "useStateDbOnly": True,
                "sourceKinds": DEFAULT_SOURCES,
                **({"cwd": args.cwd} if args.cwd else {}),
                **({"searchTerm": args.search} if args.search else {}),
            },
        },
    ]


def run_query(args: argparse.Namespace) -> dict[str, Any]:
    environment = os.environ.copy()
    if args.codex_home:
        environment["CODEX_HOME"] = str(Path(args.codex_home).expanduser().resolve())
    try:
        child = subprocess.Popen(
            [args.codex_bin, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
    except OSError as exc:
        raise QueryError(f"cannot start Codex app-server with {args.codex_bin!r}: {exc}") from exc

    selector = selectors.DefaultSelector()
    assert child.stdout is not None
    assert child.stdin is not None
    assert child.stderr is not None
    selector.register(child.stdout, selectors.EVENT_READ, "stdout")
    selector.register(child.stderr, selectors.EVENT_READ, "stderr")
    stderr_lines: list[str] = []
    response: dict[str, Any] | None = None
    started = time.monotonic()

    try:
        try:
            child.stdin.write("".join(json.dumps(item) + "\n" for item in request_payload(args)))
            child.stdin.flush()
        except OSError as exc:
            raise QueryError(f"cannot send request to Codex app-server: {exc}") from exc
        while time.monotonic() - started < args.timeout:
            if child.poll() is not None and response is None:
                break
            for key, _ in selector.select(timeout=0.25):
                line = key.fileobj.readline()
                if not line:
                    continue
                if key.data == "stderr":
                    stderr_lines.append(line.rstrip())
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == 2:
                    response = message
                    break
            if response is not None:
                break
    finally:
        selector.close()
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1.0)

    if response is None:
        detail = next((line for line in stderr_lines if line.strip()), "")
        suffix = f": {detail[:300]}" if detail else ""
        raise QueryError(f"Codex app-server did not return thread/list before timeout{suffix}")
    if response.get("error"):
        error = response["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise QueryError(f"Codex app-server rejected thread/list: {message}")
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise QueryError("Codex app-server returned a malformed thread/list response")
    return result


def as_number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def local_time(seconds: Any) -> str:
    timestamp = as_number(seconds)
    if timestamp is None or timestamp <= 0:
        return "未知时间"
    value = dt.datetime.fromtimestamp(timestamp, tz=dt.datetime.now().astimezone().tzinfo)
    return value.strftime("%Y-%m-%d %H:%M")


def normalize_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    thread_id = raw.get("id")
    cwd = raw.get("cwd")
    if not isinstance(thread_id, str) or not isinstance(cwd, str):
        return None
    preview = raw.get("name") or raw.get("preview") or "（空会话）"
    title = safe_title(str(preview))
    if len(title) > 160:
        title = title[:157].rstrip() + "..."
    return {
        "id": thread_id,
        "title": title or "（空会话）",
        "cwd": cwd,
        "source": raw.get("source") or "unknown",
        "model_provider": raw.get("modelProvider") or "",
        "created_at": local_time(raw.get("createdAt")),
        "updated_at": local_time(raw.get("updatedAt")),
        "recency_at": local_time(raw.get("recencyAt")),
        "archived": bool(raw.get("archived", False)),
    }


def safe_title(value: str) -> str:
    """Keep bridge internals and obvious credential-shaped values out of replies."""
    if any(marker in value for marker in ("<bridge_context>", "<bridge_instructions>", "# lark-channel-bridge")):
        return "Codex bridge session"
    title = " ".join(value.split())
    title = BEARER_PATTERN.sub("Bearer [redacted]", title)
    title = API_KEY_PATTERN.sub("[redacted]", title)
    title = SECRET_PATTERN.sub(lambda match: f"{match.group(0).split(':', 1)[0].split('=', 1)[0]}=[redacted]", title)
    return title


def normalized_result(result: dict[str, Any]) -> dict[str, Any]:
    items = [item for raw in result.get("data", []) if (item := normalize_item(raw))]
    return {"items": items, "next_cursor": result.get("nextCursor")}


def print_markdown(result: dict[str, Any]) -> None:
    items = result["items"]
    print(f"## 最近 Codex 工作项（{len(items)} 条）")
    if not items:
        print("\n没有匹配的工作项。")
        return
    for index, item in enumerate(items, start=1):
        print(f"\n{index}. **{item['title']}**")
        print(
            f"   - 最近活动：{item['recency_at']}；更新：{item['updated_at']}；来源：`{item['source']}`"
        )
        print(f"   - 工作目录：`{item['cwd']}`")
        print(f"   - Thread：`{item['id']}`")


def print_tsv(result: dict[str, Any]) -> None:
    print("id\ttitle\trecency_at\tupdated_at\tsource\tcwd")
    for item in result["items"]:
        values = [
            str(item["id"]),
            str(item["title"]),
            str(item["recency_at"]),
            str(item["updated_at"]),
            str(item["source"]),
            str(item["cwd"]),
        ]
        print("\t".join(value.replace("\t", " ").replace("\n", " ") for value in values))


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        result = normalized_result(run_query(args))
    except QueryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.format == "tsv":
        print_tsv(result)
    else:
        print_markdown(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
