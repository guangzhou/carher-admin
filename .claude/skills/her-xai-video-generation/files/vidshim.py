#!/usr/bin/env python3
"""xAI 视频 URL shim —— 去程注入 storage_options，回程把短命 url 换成永久 public_url。

为什么需要它（三段式里的「数据」在 README/交接记录里）：
  - openclaw 的 xai video provider 的 buildCreateBody 只发
    model/prompt/duration/aspect_ratio/resolution，**从不发 storage_options**；
    不发就拿不到永久地址（官方文档：video.url 是 ephemeral）。
  - openclaw 的 readXaiStatusResponse 只读 video.url，
    file_output/public_url 在整个 openclaw dist 里出现 0 次。
  ⇒ 两头都得代劳：去程补参数，回程把永久地址搬到 openclaw 会读的那个字段上。

第三件事（可选，靠 VIDSHIM_ALLOWED_SHA256 开启）：鉴权翻译。
  her 实例里没有任何 sub2api/xai 的 key，而 openclaw 只认 env 形态的 secret ref
  （coerceSecretRef 只接受 env: 一种来源，没有 file:），给它发新 env var 就得重建容器。
  所以改成：实例拿它**本来就有**的 CARHER_PROD_KEY 当门票，shim 校验其 sha256
  在白名单里，然后换上自己持有的 sub2api key 打上游。
  - 白名单存的是 sha256，不是明文；不在名单里的一律 401，不落到上游。
  - 不开启（没配 VIDSHIM_ALLOWED_SHA256）时行为与之前完全一致：原样透传鉴权头。
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("VIDSHIM_UPSTREAM", "http://10.43.97.195:8080")
LISTEN_PORT = int(os.environ.get("VIDSHIM_PORT", "18098"))
# 官方允许 3600..2592000；不填则永久占用 Files 配额，故默认 7 天
EXPIRES_AFTER = int(os.environ.get("VIDSHIM_EXPIRES_AFTER", str(7 * 24 * 3600)))
# 门票白名单：调用方 Bearer 的 sha256（逗号分隔）。空 = 不做翻译，原样透传。
ALLOWED_SHA256 = {h.strip().lower() for h in
                  (os.environ.get("VIDSHIM_ALLOWED_SHA256") or "").split(",")
                  if h.strip()}
# 翻译后真正打上游用的 key，从文件读（不进 unit 的 Environment=，不进 ps）
UPSTREAM_KEY_FILE = os.environ.get("VIDSHIM_UPSTREAM_KEY_FILE", "")
HOP = {"connection", "keep-alive", "transfer-encoding", "te",
       "trailer", "upgrade", "proxy-authorization", "proxy-authenticate",
       "content-length", "host", "accept-encoding"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def inject_storage_options(raw):
    """去程：给创建请求补上 storage_options。返回 (新body, 是否注入)。"""
    try:
        d = json.loads(raw)
    except Exception:
        return raw, False
    if not isinstance(d, dict):
        return raw, False
    if "storage_options" in d:          # 调用方自己给了就不覆盖
        return raw, False
    d["storage_options"] = {
        "filename": "openclaw-video.mp4",
        "public_url": True,
        "expires_after": EXPIRES_AFTER,
    }
    return json.dumps(d).encode(), True


def promote_public_url(raw):
    """回程：把 file_output.public_url 搬到 video.url。返回 (新body, 是否改写)。"""
    try:
        d = json.loads(raw)
    except Exception:
        return raw, False
    if not isinstance(d, dict):
        return raw, False
    video = d.get("video")
    if not isinstance(video, dict):
        return raw, False
    fo = video.get("file_output")
    pub = fo.get("public_url") if isinstance(fo, dict) else None
    if not isinstance(pub, str) or not pub.startswith("https://"):
        return raw, False
    if video.get("url") == pub:
        return raw, False
    video["ephemeral_url"] = video.get("url")   # 保留原值便于取证
    video["url"] = pub
    return json.dumps(d).encode(), True


def load_upstream_key():
    """翻译模式下读上游 key；读不到返回 None（调用方会 503，绝不降级成透传）。"""
    if not UPSTREAM_KEY_FILE:
        return None
    try:
        with open(UPSTREAM_KEY_FILE) as f:
            return f.read().strip() or None
    except Exception as e:
        log("[shim] upstream key file unreadable: %s" % e.__class__.__name__)
        return None


def translate_auth(headers):
    """翻译鉴权。返回 (新headers, 状态)。
    状态: 'off' 未启用 | 'ok' 已换 key | 'deny' 门票不在白名单 | 'nokey' 上游key缺失
    """
    if not ALLOWED_SHA256:
        return headers, "off"
    presented = ""
    for k, v in headers.items():
        if k.lower() == "authorization":
            presented = v.strip()
            break
    if presented.lower().startswith("bearer "):
        presented = presented[7:].strip()
    if not presented:
        return headers, "deny"
    digest = hashlib.sha256(presented.encode()).hexdigest()
    if digest not in ALLOWED_SHA256:
        return headers, "deny"
    key = load_upstream_key()
    if not key:
        return headers, "nokey"
    out = {k: v for k, v in headers.items() if k.lower() != "authorization"}
    out["Authorization"] = "Bearer " + key
    return out, "ok"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vidshim"

    def log_message(self, fmt, *args):
        log("[access] %s %s" % (self.command, self.path), fmt % args)

    def _fail(self, status, msg):
        body = json.dumps({"error": {"message": msg}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _relay(self, body=None):
        url = UPSTREAM + self.path
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP}
        headers["Accept-Encoding"] = "identity"   # 必须拿到未压缩字节才能改写

        headers, authmode = translate_auth(headers)
        if authmode == "deny":
            log("[shim] %s %s -> 401 (ticket not in allowlist)"
                % (self.command, self.path))
            self._fail(401, "vidshim: unauthorized")
            return
        if authmode == "nokey":
            log("[shim] %s %s -> 503 (upstream key missing)"
                % (self.command, self.path))
            self._fail(503, "vidshim: upstream credential unavailable")
            return

        injected = False
        if body and self.path.rstrip("/").endswith("/videos/generations"):
            body, injected = inject_storage_options(body)

        req = urllib.request.Request(url, data=body, headers=headers,
                                     method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                status, rhdrs, raw = r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            status, rhdrs, raw = e.code, dict(e.headers), e.read()
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            msg = json.dumps({"error": {"message": "vidshim upstream error: %s"
                                        % e.__class__.__name__}}).encode()
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        promoted = False
        if "application/json" in (rhdrs.get("Content-Type") or ""):
            raw, promoted = promote_public_url(raw)

        log("[shim] %s %s -> %s injected=%s promoted=%s auth=%s"
            % (self.command, self.path, status, injected, promoted, authmode))

        self.send_response(status)
        for k, v in rhdrs.items():
            if k.lower() in HOP or k.lower() == "content-encoding":
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Vidshim", "injected=%d;promoted=%d;auth=%s"
                         % (injected, promoted, authmode))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._relay()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._relay(self.rfile.read(n) if n else b"")


if __name__ == "__main__":
    log("vidshim listening on :%d -> %s (expires_after=%ds, auth_translate=%s,"
        " allowlist=%d)"
        % (LISTEN_PORT, UPSTREAM, EXPIRES_AFTER,
           bool(ALLOWED_SHA256), len(ALLOWED_SHA256)))
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
