#!/usr/bin/env python3
"""sec_redact T0 mock upstream：回显请求里收到的内容 + 按指令回出敏感串。

用途两个：
  1) 请求方向判据：上游**真正收到的**正文（回显到响应里 + 记到 /dump）
  2) 响应方向判据：请求里写 `ECHO_KEY` / `ECHO_PHONE` / `ECHO_IP`，上游就把对应合成值
     按很小的片段流式回出来（专门切开，测跨片段）

三种上游形态：/v1/chat/completions（chat SSE/JSON）、/v1/responses（Responses SSE/JSON）、
/v1/messages（anthropic SSE/JSON）。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0kLmNoPqRsTuV"
PHONE = "13812345678"
IP = "10.68.13.198"
LAST = {"body": None}
LOCK = threading.Lock()


def answer_for(body: dict) -> str:
    raw = json.dumps(body, ensure_ascii=False)
    parts = ["好的。"]
    if "ECHO_KEY" in raw:
        parts.append(f"你的密钥是 {KEY} 请保管。")
    if "ECHO_PHONE" in raw:
        parts.append(f"联系电话 {PHONE} 。")
    if "ECHO_IP" in raw:
        parts.append(f"服务器 {IP} 。")
    parts.append("以上。" + "正文" * 60)
    return "".join(parts)


def pieces(s: str, n: int = 5):
    return [s[i:i + n] for i in range(0, len(s), n)]


def sse(ev, obj):
    return f"event: {ev}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/dump":
            with LOCK:
                self._send("application/json", json.dumps(LAST["body"], ensure_ascii=False).encode())
        else:
            self._send("application/json", b"{}")

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        with LOCK:
            LAST["body"] = body
        stream = bool(body.get("stream"))
        text = answer_for(body)
        if self.path.startswith("/midfail/"):
            return self.midfail(body)
        if self.path.endswith("/chat/completions"):
            self.chat(body, stream, text)
        elif self.path.endswith("/responses"):
            self.resp_api(body, stream, text)
        elif self.path.endswith("/messages"):
            self.anthropic(body, stream, text)
        else:
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()

    def _send(self, ct, payload: bytes):
        self.send_response(200)
        self.send_header("content-type", ct)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def chat(self, body, stream, text):
        model = body.get("model", "mock-gpt")
        usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        if not stream:
            self._send("application/json", json.dumps({
                "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": usage}, ensure_ascii=False).encode())
            return
        base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": model}
        out = b""
        seq = [({"role": "assistant", "content": ""}, None)] + [({"content": p}, None) for p in pieces(text)] + [({}, "stop")]
        for delta, fin in seq:
            c = dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": fin}])
            out += b"data: " + json.dumps(c, ensure_ascii=False).encode() + b"\n\n"
        c = dict(base, choices=[], usage=usage)
        out += b"data: " + json.dumps(c).encode() + b"\n\ndata: [DONE]\n\n"
        self._send("text/event-stream", out)

    def resp_api(self, body, stream, text):
        final = {
            "id": "resp_1", "object": "response", "created_at": 1, "status": "completed", "model": "mock-gpt",
            "output": [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            "parallel_tool_calls": False, "tool_choice": "auto", "tools": [], "error": None, "incomplete_details": None,
        }
        if not stream:
            self._send("application/json", json.dumps(final, ensure_ascii=False).encode())
            return
        created = {**final, "status": "in_progress", "output": []}
        seq = [("response.created", {"type": "response.created", "response": created}),
               ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                                               "item": {"type": "message", "id": "msg_1", "status": "in_progress",
                                                        "role": "assistant", "content": []}}),
               ("response.content_part.added", {"type": "response.content_part.added", "item_id": "msg_1",
                                                "output_index": 0, "content_index": 0,
                                                "part": {"type": "output_text", "text": "", "annotations": []}})]
        for p in pieces(text):
            seq.append(("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "msg_1",
                                                       "output_index": 0, "content_index": 0, "delta": p}))
        seq += [("response.output_text.done", {"type": "response.output_text.done", "item_id": "msg_1",
                                               "output_index": 0, "content_index": 0, "text": text}),
                ("response.content_part.done", {"type": "response.content_part.done", "item_id": "msg_1",
                                                "output_index": 0, "content_index": 0,
                                                "part": {"type": "output_text", "text": text, "annotations": []}}),
                ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0,
                                               "item": final["output"][0]}),
                ("response.completed", {"type": "response.completed", "response": final})]
        self._send("text/event-stream", b"".join(sse(ev, obj) for ev, obj in seq))

    def anthropic(self, body, stream, text):
        model = body.get("model", "claude-mock")
        if not stream:
            self._send("application/json", json.dumps({
                "id": "msg_1", "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 2}}, ensure_ascii=False).encode())
            return
        seq = [("message_start", {"type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
                    "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}}),
               ("content_block_start", {"type": "content_block_start", "index": 0,
                                        "content_block": {"type": "text", "text": ""}})]
        for p in pieces(text):
            seq.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "text_delta", "text": p}}))
        seq += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": 2}}),
                ("message_stop", {"type": "message_stop"})]
        self._send("text/event-stream", b"".join(sse(ev, obj) for ev, obj in seq))

    def midfail(self, body):
        """流式先发 3 段正文，再发一个 ChatGPT 风格的 overloaded error 事件（复现 10-28 gray 上的上游过载形状）。"""
        if self.path.endswith("/responses"):
            seq = [("response.created", {"type": "response.created", "response": {"id": "r", "object": "response",
                    "status": "in_progress", "output": [], "model": "m"}})]
            for p in ("部分", "正文", "然后"):
                seq.append(("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "m1",
                            "output_index": 0, "content_index": 0, "delta": p}))
            seq.append(("error", {"type": "error", "code": "server_is_overloaded",
                                  "message": "Our servers are currently overloaded. Please try again later."}))
            out = b"".join(sse(ev, obj) for ev, obj in seq)
        else:
            base = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m"}
            out = b""
            for p in ("部分", "正文", "然后"):
                out += b"data: " + json.dumps(dict(base, choices=[{"index": 0, "delta": {"content": p}, "finish_reason": None}]), ensure_ascii=False).encode() + b"\n\n"
            out += b"data: " + json.dumps({"error": {"message": "Our servers are currently overloaded. Please try again later.",
                                                     "type": "server_error", "code": "server_is_overloaded"}}).encode() + b"\n\n"
        self._send("text/event-stream", out)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9999), H).serve_forever()
