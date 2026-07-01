"""SSE 边界 buffer + event 解析。

chatgpt.com 流可能在 ~30 KB instructions 处把单个 event 跨 chunk 截断（vercel/ai#14473），
所以**必须**按 `\\n\\n` 边界 buffer，绝不允许单 event 拆段下发。

同时聚合 response.output_item.done 累积 output（response.completed.output 可能 null，
openai/openai-python#3312），response.completed 仅当 terminate signal + usage 来源。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Iterator


@dataclass
class SSEEvent:
    event: str | None
    data: str  # raw payload；解析失败时保留原文便于排查

    def json(self) -> dict | None:
        try:
            return json.loads(self.data)
        except (ValueError, TypeError):
            return None


class SSEBuffer:
    """逐块 feed bytes，按 \\n\\n 切 event。剩余不完整尾巴保留到下次 feed。"""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: bytes | str) -> list[SSEEvent]:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        self._buf += chunk
        events: list[SSEEvent] = []
        while True:
            idx = self._buf.find("\n\n")
            if idx < 0:
                break
            raw, self._buf = self._buf[:idx], self._buf[idx + 2:]
            ev = _parse_event(raw)
            if ev is not None:
                events.append(ev)
        return events

    def flush(self) -> list[SSEEvent]:
        """流结束时 drain 尾部（最后一个 event 可能没 \\n\\n）。"""
        if not self._buf.strip():
            self._buf = ""
            return []
        ev = _parse_event(self._buf)
        self._buf = ""
        return [ev] if ev is not None else []


def _parse_event(raw: str) -> SSEEvent | None:
    event_type: str | None = None
    data_lines: list[str] = []
    for line in raw.split("\n"):
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        # SSE id/retry 字段我们不消费
    if not data_lines and event_type is None:
        return None
    return SSEEvent(event=event_type, data="\n".join(data_lines))


@dataclass
class ResponseAggregator:
    """读 SSE 流，聚合 output items 跟 usage。

    output 累积只信 response.output_item.done。
    response.completed 仅作 terminate + usage 提取。
    """
    items: list[dict] = field(default_factory=list)
    usage: dict | None = None
    response_id: str | None = None
    status: str | None = None
    completed: bool = False

    def consume(self, ev: SSEEvent) -> None:
        if ev.event == "response.created" or ev.event == "response.in_progress":
            d = ev.json() or {}
            self.response_id = (d.get("response") or {}).get("id")
            self.status = (d.get("response") or {}).get("status")
        elif ev.event == "response.output_item.done":
            d = ev.json() or {}
            item = d.get("item")
            if isinstance(item, dict):
                self.items.append(item)
        elif ev.event == "response.completed":
            d = ev.json() or {}
            resp = d.get("response") or {}
            self.status = resp.get("status")
            usage = resp.get("usage")
            if isinstance(usage, dict):
                self.usage = usage
            self.completed = True


def aggregate(events: Iterable[SSEEvent]) -> ResponseAggregator:
    agg = ResponseAggregator()
    for ev in events:
        agg.consume(ev)
    return agg
