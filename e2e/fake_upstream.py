#!/usr/bin/env python3
"""A stand-in for the Anthropic and OpenAI APIs, for the end-to-end check.

LiteLLM's anthropic/ and openai/ models point their api_base here, so the
proxy runs its real provider code (request translation, response parsing,
streaming) without a provider key. Each answer quotes the question, so the
transcript shows which answer belongs to which turn.

    POST /v1/messages          Anthropic Messages, JSON or SSE (stream: true)
    POST /v1/chat/completions  OpenAI Chat Completions, JSON or SSE

Standard library only. Listens on $PORT (default 8090).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def last_user_text(messages) -> str:
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def answer_for(messages) -> str:
    q = last_user_text(messages).strip()
    return f"Fake upstream answer to: {q[:80]}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # one short line per request
        print("fake-upstream", self.command, self.path, flush=True)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _json(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, events) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for ev in events:
            self.wfile.write(ev.encode())
            self.wfile.flush()
        self.close_connection = True

    def do_GET(self):
        self._json({"ok": True})

    def do_POST(self):
        body = self._body()
        model = body.get("model", "fake")
        text = answer_for(body.get("messages"))
        words = text.split(" ")
        if self.path.endswith("/v1/messages"):
            self._anthropic(body, model, text, words)
        elif self.path.endswith("/chat/completions"):
            self._openai(body, model, text, words)
        else:
            self.send_error(404)

    def _anthropic(self, body, model, text, words):
        mid = "msg_" + uuid.uuid4().hex[:24]
        usage = {"input_tokens": 20, "output_tokens": len(words)}
        if not body.get("stream"):
            self._json({"id": mid, "type": "message", "role": "assistant", "model": model,
                        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                        "stop_sequence": None, "usage": usage})
            return

        def ev(name, data):
            return f"event: {name}\ndata: {json.dumps(data)}\n\n"

        events = [ev("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 20, "output_tokens": 1}}}),
            ev("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "text", "text": ""}})]
        for i, w in enumerate(words):
            events.append(ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                     "delta": {"type": "text_delta", "text": (" " if i else "") + w}}))
        events += [ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
                   ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                        "usage": {"output_tokens": len(words)}}),
                   ev("message_stop", {"type": "message_stop"})]
        self._sse(events)

    def _openai(self, body, model, text, words):
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        now = int(time.time())
        usage = {"prompt_tokens": 20, "completion_tokens": len(words), "total_tokens": 20 + len(words)}
        if not body.get("stream"):
            self._json({"id": cid, "object": "chat.completion", "created": now, "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                     "finish_reason": "stop"}], "usage": usage})
            return

        def chunk(delta, finish=None, with_usage=False):
            c = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if with_usage:
                c["usage"] = usage
            return f"data: {json.dumps(c)}\n\n"

        events = [chunk({"role": "assistant", "content": ""})]
        events += [chunk({"content": (" " if i else "") + w}) for i, w in enumerate(words)]
        events += [chunk({}, "stop", with_usage=True), "data: [DONE]\n\n"]
        self._sse(events)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8090"))
    print(f"fake-upstream listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
