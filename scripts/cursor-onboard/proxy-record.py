#!/usr/bin/env python3
"""Transparent recording reverse-proxy for the Cursor Connect-RPC API.

Forwards every inbound request verbatim to $UPSTREAM over TLS:443 and relays
the response back, while appending a full record of both directions to
$LOG_PATH as JSONL.  Connect-RPC framed bodies are decoded for readability.

Env:
  PORT      listen port           (default 8899)
  UPSTREAM  upstream hostname     (default api2.cursor.sh)
  LOG_PATH  jsonl output path     (default /tmp/cursor-proxy.jsonl)
"""

import binascii
import datetime
import http.client
import json
import os
import ssl
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8899"))
UPSTREAM = os.environ.get("UPSTREAM", "api2.cursor.sh")
LOG_PATH = os.environ.get("LOG_PATH", "/tmp/cursor-proxy.jsonl")
TIMEOUT = 120.0

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "te",
    "trailer",
    "trailers",
}

_counter_lock = threading.Lock()
_log_lock = threading.Lock()
_counter = 0


def next_n():
    global _counter
    with _counter_lock:
        _counter += 1
        return _counter


def total_n():
    with _counter_lock:
        return _counter


def printable(data, limit=None):
    if isinstance(data, bytes):
        out = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    else:
        out = data
    if limit is not None:
        out = out[:limit]
    return out


def maybe_decompress(body, encoding):
    """Best-effort decompress for the RECORDED copy only.

    The bytes relayed to the client are always the original ones; upstream may
    gzip because we forward the client's accept-encoding verbatim, and gzipped
    bytes would make frame decoding useless.
    """
    enc = (encoding or "").lower().strip()
    if not body or not enc or enc == "identity":
        return body
    try:
        if enc == "gzip":
            import gzip
            return gzip.decompress(body)
        if enc in ("deflate", "zlib"):
            import zlib
            try:
                return zlib.decompress(body)
            except Exception:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        if enc == "br":
            import brotli  # not stdlib; only if present
            return brotli.decompress(body)
    except Exception:
        pass
    return body


def decode_frames(body):
    """Decode a Connect-RPC framed body: repeated (>BI header, payload)."""
    frames = []
    off = 0
    n = len(body)
    while off + 5 <= n:
        flag, length = struct.unpack(">BI", body[off:off + 5])
        off += 5
        if length > n - off:
            frames.append({
                "flag": flag,
                "len": length,
                "truncated": True,
                "hex": binascii.hexlify(body[off:]).decode(),
            })
            break
        payload = body[off:off + length]
        off += length
        entry = {"flag": flag, "len": length}
        try:
            entry["json"] = json.loads(payload.decode("utf-8"))
        except Exception:
            entry["hex"] = binascii.hexlify(payload).decode()
        frames.append(entry)
    return frames


def read_chunked(rfile):
    chunks = []
    while True:
        line = rfile.readline()
        if not line:
            break
        line = line.strip()
        if b";" in line:
            line = line.split(b";", 1)[0]
        if not line:
            continue
        try:
            size = int(line, 16)
        except ValueError:
            break
        if size == 0:
            # consume trailers up to blank line
            while True:
                t = rfile.readline()
                if not t or t in (b"\r\n", b"\n"):
                    break
            break
        remaining = size
        while remaining > 0:
            piece = rfile.read(remaining)
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        rfile.read(2)  # trailing CRLF
    return b"".join(chunks)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cursor-proxy-record"
    timeout = TIMEOUT

    def log_message(self, fmt, *args):  # silence default access log
        pass

    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            return read_chunked(self.rfile)
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                length = int(cl)
            except ValueError:
                return b""
            data = b""
            while len(data) < length:
                piece = self.rfile.read(length - len(data))
                if not piece:
                    break
                data += piece
            return data
        return b""

    def _fwd_headers(self):
        out = []
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("host", "content-length"):
                # host is rewritten; content-length is recomputed from the
                # body we actually send (forwarding both would emit a
                # duplicate header, which AWS ELB rejects with 400)
                continue
            if lk in HOP_BY_HOP or lk.startswith("proxy-"):
                continue
            out.append((k, v))
        return out

    def handle_one(self):
        n = next_n()
        ts = datetime.datetime.now().isoformat()
        path = self.path
        method = self.command
        req_body = self._read_body()
        req_headers = dict(self.headers.items())

        status = 0
        resp_headers = {}
        resp_body = b""
        error = None

        conn = None
        try:
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(
                UPSTREAM, 443, timeout=TIMEOUT, context=ctx
            )
            hdrs = self._fwd_headers()
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", UPSTREAM)
            for k, v in hdrs:
                conn.putheader(k, v)
            conn.putheader("Content-Length", str(len(req_body)))
            conn.endheaders()
            if req_body:
                conn.send(req_body)

            resp = conn.getresponse()
            status = resp.status
            resp_headers = dict(resp.getheaders())
            try:
                resp_body = resp.read()
            except Exception as e:  # timeout mid-stream: keep what we have
                error = "resp_read: %r" % (e,)
                try:
                    resp_body = resp.fp.read() if resp.fp else b""
                except Exception:
                    resp_body = b""
        except Exception as e:
            error = "upstream: %r" % (e,)
            status = 502
            resp_headers = {"content-type": "text/plain"}
            resp_body = ("proxy error: %r" % (e,)).encode()
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        resp_enc = ""
        for k, v in resp_headers.items():
            if k.lower() == "content-encoding":
                resp_enc = v
        resp_plain = maybe_decompress(resp_body, resp_enc)

        rec = {
            "n": n,
            "ts": ts,
            "method": method,
            "path": path,
            "req_headers": req_headers,
            "req_body_len": len(req_body),
            "req_body_hex": binascii.hexlify(req_body).decode(),
            "req_body_printable": printable(req_body),
            "req_frames": decode_frames(req_body),
            "status": status,
            "resp_headers": resp_headers,
            "resp_body_len": len(resp_body),
            "resp_body_printable": printable(resp_plain, 4000),
            "resp_frames": decode_frames(resp_plain),
        }
        if error:
            rec["error"] = error

        with _log_lock:
            try:
                with open(LOG_PATH, "a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as e:
                print("!! log write failed: %r" % (e,), flush=True)

        print(
            "[%d] %s %s -> %s (req %d bytes, resp %d bytes)%s"
            % (
                n,
                method,
                path,
                status,
                len(req_body),
                len(resp_body),
                " ERR " + error if error else "",
            ),
            flush=True,
        )
        if "RunSSE" in path or "AgentService" in path:
            print("---- decoded request frames for %s ----" % path, flush=True)
            try:
                print(
                    json.dumps(rec["req_frames"], indent=2, ensure_ascii=False),
                    flush=True,
                )
            except Exception as e:
                print("(frame dump failed: %r)" % (e,), flush=True)
            print("---- end ----", flush=True)

        # relay to client
        try:
            self.send_response(status)
            for k, v in resp_headers.items():
                lk = k.lower()
                if lk in HOP_BY_HOP or lk == "content-length" or lk.startswith("proxy-"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            if resp_body:
                self.wfile.write(resp_body)
            self.wfile.flush()
        except Exception as e:
            print("[%d] relay to client failed: %r" % (n, e), flush=True)

    # every verb funnels through the same path
    def do_GET(self):
        self.handle_one()

    def do_POST(self):
        self.handle_one()

    def do_PUT(self):
        self.handle_one()

    def do_PATCH(self):
        self.handle_one()

    def do_DELETE(self):
        self.handle_one()

    def do_HEAD(self):
        self.handle_one()

    def do_OPTIONS(self):
        self.handle_one()


def main():
    print("=" * 66, flush=True)
    print(" cursor transparent recording reverse-proxy", flush=True)
    print(" listen   : 0.0.0.0:%d" % PORT, flush=True)
    print(" upstream : https://%s:443" % UPSTREAM, flush=True)
    print(" log      : %s" % LOG_PATH, flush=True)
    print(" timeout  : %.0fs" % TIMEOUT, flush=True)
    print("=" * 66, flush=True)

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down; %d requests recorded" % total_n(), flush=True)
    finally:
        try:
            srv.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
