#!/usr/bin/env python3
"""A stand-in for Corveil's transcript upload endpoint, for the end-to-end check.

Accepts what the callback sends,

    POST /api/crow-sessions/{session_uid}/artifacts

and keeps each upload in memory. GET /received returns them as JSON:
[{"session_uid", "params", "authorization", "content_type", "user_agent",
"lines"}], with the gzipped NDJSON body already decoded into "lines".

Answers 201 to a well-formed upload, 401 without a bearer key, and 400 to a
body that isn't gzipped NDJSON, so a broken upload fails the check instead of
being recorded.

Standard library only. Listens on $PORT (default 8000).
"""

from __future__ import annotations

import gzip
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

UPLOAD = re.compile(r"^/api/crow-sessions/([^/]+)/artifacts$")
received: list[dict] = []
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self._send(200, {"ok": True})
        if path == "/received":
            with lock:
                return self._send(200, list(received))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlsplit(self.path)
        m = UPLOAD.match(url.path)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not m:
            return self._send(404, {"error": "not found"})
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer ") or not auth[7:].strip():
            return self._send(401, {"error": "missing API key"})
        try:
            lines = [json.loads(l) for l in gzip.decompress(body).decode().splitlines() if l.strip()]
        except (OSError, ValueError) as e:
            return self._send(400, {"error": f"not gzipped NDJSON: {e}"})
        with lock:
            received.append({
                "session_uid": unquote(m.group(1)),
                "params": dict(parse_qsl(url.query)),
                "authorization": auth,
                "content_type": self.headers.get("Content-Type"),
                "user_agent": self.headers.get("User-Agent"),
                "lines": lines,
            })
        self._send(201, {"id": str(len(received))})

    def log_message(self, fmt, *args):
        print("fake-corveil " + fmt % args, flush=True)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"fake-corveil listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
