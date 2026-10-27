#!/usr/bin/env python3
# mock Kiro 上游（补丁 0013 回归用，配 testUpstreamOverride）：q 永远只回 contextUsageEvent（=静默空）；runtime 探测 MODE=fine 回文本 / restricted 回 CONTENT_FILTERED
import http.server, json, os, struct, zlib, sys
MODE = os.environ.get("MODE", "fine")
def frame(et, payload):
    h = b""
    for k, v in ((":message-type", "event"), (":event-type", et), (":content-type", "application/json")):
        h += bytes([len(k)]) + k.encode() + b"\x07" + struct.pack(">H", len(v)) + v.encode()
    p = json.dumps(payload).encode(); total = 12 + len(h) + len(p) + 4
    pre = struct.pack(">II", total, len(h)); pre += struct.pack(">I", zlib.crc32(pre))
    m = pre + h + p; return m + struct.pack(">I", zlib.crc32(m))
LOG = []
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        b = self.rfile.read(int(self.headers.get("content-length", 0)))
        try: mid = json.loads(b)["conversationState"]["currentMessage"]["userInputMessage"]["modelId"]
        except Exception: mid = "?"
        if self.path.startswith("/runtime"):
            body = frame("assistantResponseEvent", {"content": "是"}) if MODE == "fine" else b'{"stopReason":"CONTENT_FILTERED"}'
        else:
            body = frame("contextUsageEvent", {"contextUsagePercentage": 0.7})
        print(f"{self.path} model={mid}", flush=True)
        self.send_response(200); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), H).serve_forever()
