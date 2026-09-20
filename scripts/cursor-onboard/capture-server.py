#!/usr/bin/env python3
"""Diagnostic request-capture HTTP server for the `cursor-agent` CLI.

WHY THIS EXISTS
---------------
We hold a Cursor API key of the form `crsr_...`. When we call Cursor's internal
API at https://api2.cursor.sh ourselves -- our best guess at the RPC path, our
best guess at the auth header -- the server answers ERROR_NOT_LOGGED_IN. The
official `cursor-agent` CLI, using the *same* key, succeeds. Therefore the CLI
must be sending something extra that we are not: a different auth header name or
token encoding, extra client/version/session headers, a checksum, a different
RPC path, or a different Connect-RPC framing / content-type.

Rather than guess, we observe. `cursor-agent` accepts `-e/--endpoint <url>`, so
we point it at this local server and dump, verbatim, every byte it sends:
request line, every header (unfiltered -- the auth header format is the whole
point), body hex, body as printable ASCII, and a Connect-RPC envelope decode
attempt (5-byte `>BI` prefix, then JSON if the payload is JSON, otherwise it is
almost certainly protobuf and we dump hex).

To keep the CLI talking long enough to capture more than one request, every
response is a minimal *valid* Connect streaming reply: one `{}` message frame
plus an end-of-stream frame.

SAFETY / SCOPE
--------------
Read-only diagnostic tool. It never contacts Cursor, never forwards anything
upstream, never authenticates, and stores no credentials. It only writes what
the local CLI voluntarily sends to a local log file, so that log may contain the
key you deliberately pointed at it -- treat the log as sensitive and delete it
when done.

USAGE
-----
    PORT=8899 python3 capture-server.py
    cursor-agent --api-key $K -e http://127.0.0.1:8899 -f -p "say hi" \
        --output-format text
"""

import binascii
import datetime
import json
import os
import socketserver
import struct
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8899"))
LOG_PATH = os.environ.get("LOG_PATH", "/tmp/cursor-capture.jsonl")

HEX_BYTES_PER_ROW = 32
HEX_DUMP_LIMIT = 512
PRINTABLE_LIMIT = 1000
LOG_HEX_LIMIT = 2048

# The CLI may open several connections at once, and ThreadingHTTPServer serves
# each in its own thread, so the request counter and the log append need a lock.
# socketserver re-exports the threading module, which keeps us inside the
# stdlib import set above.
_LOCK = socketserver.threading.Lock()


def _now():
    return datetime.datetime.now().isoformat()


def emit(line=""):
    """Print immediately (SSH-friendly) and mirror nothing to the JSONL log."""
    print(line, flush=True)
    sys.stdout.flush()


def printable(raw):
    """ASCII-printable view; everything else (incl. tab/CR/LF) becomes '.' so a
    body never breaks the one-line-per-section layout of the dump."""
    return "".join(chr(b) if 32 <= b <= 126 else "." for b in raw)


def hex_rows(raw):
    """Yield readable hex rows of HEX_BYTES_PER_ROW bytes each."""
    rows = []
    for off in range(0, len(raw), HEX_BYTES_PER_ROW):
        chunk = raw[off:off + HEX_BYTES_PER_ROW]
        rows.append("  %08x  %s" % (off, binascii.hexlify(chunk).decode("ascii")))
    return rows


def append_log(record):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        emit("!! failed to write log %s: %s" % (LOG_PATH, exc))


class CaptureHandler(BaseHTTPRequestHandler):
    server_version = "cursor-capture/1.0"
    protocol_version = "HTTP/1.1"

    # --- all verbs funnel into one place -------------------------------------
    def do_GET(self):
        self._capture()

    def do_POST(self):
        self._capture()

    def do_PUT(self):
        self._capture()

    def do_PATCH(self):
        self._capture()

    def do_DELETE(self):
        self._capture()

    def do_OPTIONS(self):
        self._capture()

    def do_HEAD(self):
        self._capture()

    # BaseHTTPRequestHandler logs to stderr per request; we do our own dump.
    def log_message(self, fmt, *args):
        return

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            return self.rfile.read(length)
        # Chunked bodies: decode the transfer encoding by hand.
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        return b""

    def _capture(self):
        ts = _now()
        body = self._read_body()
        # Hold the lock across the whole dump so concurrent connections do not
        # interleave their output into an unreadable mess.
        with _LOCK:
            self.server.request_count += 1
            self._dump(self.server.request_count, ts, body)
        self._reply()

    def _dump(self, n, ts, body):
        emit()
        emit("=" * 78)
        emit("### REQUEST #%d  %s" % (n, ts))
        emit("=" * 78)
        emit("%s %s %s" % (self.command, self.path, self.request_version))
        emit()
        emit("-- headers (%d) --" % len(self.headers))
        headers = {}
        for key, value in self.headers.items():
            emit("%s: %s" % (key, value))
            if key in headers:
                headers[key] = headers[key] + ", " + value
            else:
                headers[key] = value
        emit()
        emit("-- body: %d bytes --" % len(body))

        if body:
            emit("-- hex (first %d bytes) --" % HEX_DUMP_LIMIT)
            for row in hex_rows(body[:HEX_DUMP_LIMIT]):
                emit(row)
            if len(body) > HEX_DUMP_LIMIT:
                emit("  ... (%d more bytes)" % (len(body) - HEX_DUMP_LIMIT))
            emit()
            emit("-- printable (first %d chars) --" % PRINTABLE_LIMIT)
            emit(printable(body)[:PRINTABLE_LIMIT])
        else:
            emit("(empty body)")

        envelope = self._decode_envelope(body)

        append_log({
            "n": n,
            "ts": ts,
            "method": self.command,
            "path": self.path,
            "headers": headers,
            "body_len": len(body),
            "body_hex": binascii.hexlify(body).decode("ascii")[:LOG_HEX_LIMIT],
            "body_printable": printable(body)[:PRINTABLE_LIMIT],
            "envelope": envelope,
        })

    def _decode_envelope(self, body):
        """Try to read a Connect-RPC envelope: 1 flag byte + big-endian uint32."""
        emit()
        emit("-- connect-rpc envelope --")
        if len(body) < 5:
            emit("body shorter than 5 bytes; no envelope to decode")
            return {}

        flag, declared = struct.unpack(">BI", body[:5])
        payload = body[5:]
        matches = declared == len(payload)
        emit("flag byte    : 0x%02x (%d)" % (flag, flag))
        emit("declared len : %d" % declared)
        emit("actual len-5 : %d" % len(payload))
        emit("len matches  : %s" % matches)

        envelope = {
            "flag": flag,
            "declared_len": declared,
            "len_matches": matches,
        }

        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            emit("payload is NOT valid JSON (%s) -- almost certainly protobuf" % exc)
            emit("payload hex:")
            for row in hex_rows(payload[:HEX_DUMP_LIMIT]):
                emit(row)
            if len(payload) > HEX_DUMP_LIMIT:
                emit("  ... (%d more bytes)" % (len(payload) - HEX_DUMP_LIMIT))
            envelope["payload_hex"] = binascii.hexlify(payload).decode("ascii")[:LOG_HEX_LIMIT]
        else:
            emit("payload parsed as JSON:")
            emit(json.dumps(parsed, indent=2, ensure_ascii=False))
            envelope["payload_json"] = parsed

        return envelope

    def _reply(self):
        # Minimal valid Connect streaming reply, in two envelope frames.
        #   flag 0x00 -> a normal message frame; payload here is the empty
        #                object `{}` (2 bytes).
        #   flag 0x02 -> bit 1 (value 2) is the Connect streaming END-OF-STREAM
        #                flag. Its payload is the trailers/error object; `{}`
        #                means "stream finished, no error, no trailers".
        # Sending both keeps the CLI's Connect client happy so it proceeds to
        # the next call instead of bailing on a malformed response.
        payload = struct.pack(">BI", 0, 2) + b"{}"
        end_of_stream = struct.pack(">BI", 2, 2) + b"{}"
        blob = payload + end_of_stream

        self.send_response(200)
        self.send_header("Content-Type", "application/connect+json")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(blob)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                emit("(client closed connection before response was written)")


class CaptureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_count = 0


def main():
    server = CaptureServer(("0.0.0.0", PORT), CaptureHandler)
    emit("=" * 78)
    emit("cursor-agent request capture server")
    emit("=" * 78)
    emit("listening : http://0.0.0.0:%d" % PORT)
    emit("log file  : %s (JSONL, appended)" % LOG_PATH)
    emit("mode      : diagnostic dump only -- nothing forwarded upstream")
    emit()
    emit("point the CLI at it:")
    emit('  cursor-agent --api-key $K -e http://127.0.0.1:%d -f -p "say hi" '
         "--output-format text" % PORT)
    emit()
    emit("Ctrl-C to stop.")
    emit("=" * 78)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        emit()
        emit("interrupted -- captured %d request(s); log: %s"
             % (server.request_count, LOG_PATH))
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
