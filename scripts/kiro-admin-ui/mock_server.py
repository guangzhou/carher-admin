"""本地验 UI：静态 dist + 回放 live /credentials 快照。所有写请求一律 403，绝不转发生产。"""
import http.server, json, pathlib, sys
DIST = pathlib.Path(sys.argv[1]); POOL = json.load(open(sys.argv[2])); PORT = int(sys.argv[3])
WRITES = []
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/admin/credentials": return self._send(200, POOL)
        if p == "/api/admin/config/load-balancing": return self._send(200, {"mode": "priority"})
        if p.startswith("/api/admin/credentials/") and p.endswith("/balance"):
            return self._send(200, {"id": int(p.split("/")[4]), "subscriptionTitle": "MOCK", "currentUsage": 1, "usageLimit": 10, "remaining": 9, "usagePercentage": 10, "nextResetAt": None})
        if p.startswith("/admin"):
            rel = p[len("/admin"):].lstrip("/") or "index.html"
            f = DIST / rel
            if not f.is_file(): f = DIST / "index.html"
            ct = {"js": "application/javascript", "css": "text/css", "html": "text/html", "svg": "image/svg+xml"}.get(f.suffix[1:], "application/octet-stream")
            return self._send(200, f.read_bytes(), ct)
        self._send(404, {"error": "nf"})
    def _deny(self):
        WRITES.append(f"{self.command} {self.path}"); print("WRITE-ATTEMPT", self.command, self.path, flush=True)
        self._send(403, {"error": {"type": "mock", "message": "mock 拒绝写"}})
    do_POST = do_PUT = do_DELETE = _deny
http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
