#!/usr/bin/env python3
"""身份头防泄漏探针：在 gray litellm pod 里跑（需 /app/kiro_slots_identity.py）。
主组 api_base 指向 kiro-rs-slots 主机名但端口不通 ⇒ 必失败 ⇒ 兜底到本地假第三方；打印假第三方收到的身份头，期望「无」。
  kubectl -n litellm-product cp scripts/kiro-slots-identity-leak-probe.py <gray-pod>:/tmp/p.py && kubectl exec <gray-pod> -- python3 /tmp/p.py
"""
from litellm import Router
got = []
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        got.append({k.lower(): v for k, v in self.headers.items()})
        self.rfile.read(int(self.headers.get("content-length", 0)))
        b = json.dumps({"id": "m", "type": "message", "role": "assistant", "model": "x", "content": [{"type": "text", "text": "OK"}],
                        "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
spec = importlib.util.spec_from_file_location("ksi", "/app/kiro_slots_identity.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
litellm.callbacks = [m.kiro_slots_identity]
r = Router(model_list=[
    {"model_name": "slots", "litellm_params": {"model": "anthropic/claude-x", "api_base": "http://kiro-rs-slots.kiro-rs.svc.cluster.local:1", "api_key": "x", "timeout": 3}},
    {"model_name": "third", "litellm_params": {"model": "anthropic/claude-x", "api_base": f"http://127.0.0.1:{port}", "api_key": "x"}},
], fallbacks=[{"slots": ["third"]}], num_retries=0)
async def main():
    out = await r.aanthropic_messages(model="slots", max_tokens=4, messages=[{"role": "user", "content": "hi"}],
        litellm_metadata={"user_api_key_hash": "h123", "user_api_key_alias": "cursor-alice"})
    return out
res = asyncio.run(main())
print("兜底到第三方的请求数:", len(got))
for g in got: print("第三方收到的身份头:", {k: v for k, v in g.items() if "user_api_key" in k} or "无")
