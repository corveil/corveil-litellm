#!/usr/bin/env python3
"""Send one two-turn conversation through a LiteLLM proxy, in Anthropic format
(/v1/messages) or OpenAI format (/v1/chat/completions).

Each request carries x-litellm-session-id, so the callback groups both
turns into one conversation. The second request replays the history, as chat
clients do. The script waits between turns so each turn has its own time.

    python3 send_conversation.py --format anthropic --session-id e2e-abc
    python3 send_conversation.py --format openai --session-id e2e-def --stream

--stream asks for server-sent events, as Claude Code always does.

Standard library only. Prints the session id and both answers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TURNS = [
    "Draft a two-sentence status update on the LiteLLM rollout for the platform team.",
    "Now shorten that update to one sentence and mention the next check is on Friday.",
]


def post(url: str, key: str, session_id: str, body: dict) -> bytes:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "x-litellm-session-id": session_id,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"{url} answered {e.code}: {e.read().decode()[:500]}")


def answer_text(fmt: str, raw: bytes, stream: bool) -> str:
    if not stream:
        resp = json.loads(raw)
        if fmt == "anthropic":
            return "".join(b.get("text", "") for b in resp.get("content") or [] if b.get("type") == "text")
        return resp["choices"][0]["message"]["content"]
    out = []
    for line in raw.decode().splitlines():
        if not line.startswith("data:") or line.strip() == "data: [DONE]":
            continue
        ev = json.loads(line[5:])
        if fmt == "anthropic":
            if ev.get("type") == "content_block_delta":
                out.append(ev["delta"].get("text", ""))
        else:
            for c in ev.get("choices") or []:
                out.append((c.get("delta") or {}).get("content") or "")
    return "".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--format", choices=["anthropic", "openai"], required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--base-url", default=os.getenv("LITELLM_URL", "http://127.0.0.1:4000"))
    ap.add_argument("--key", default=os.getenv("LITELLM_KEY", "sk-e2e-master"))
    ap.add_argument("--model", help="default claude-e2e for anthropic, gpt-e2e for openai")
    ap.add_argument("--pause", type=float, default=3.0, help="seconds between the two turns")
    ap.add_argument("--stream", action="store_true", help="ask for server-sent events")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    model = args.model or ("claude-e2e" if args.format == "anthropic" else "gpt-e2e")
    history: list[dict] = []
    for i, text in enumerate(TURNS):
        if i:
            time.sleep(args.pause)
        history.append({"role": "user", "content": text})
        if args.format == "anthropic":
            raw = post(f"{base}/v1/messages", args.key, args.session_id,
                       {"model": model, "max_tokens": 256, "messages": history, "stream": args.stream})
            answer = answer_text("anthropic", raw, args.stream)
            history.append({"role": "assistant", "content": [{"type": "text", "text": answer}]})
        else:
            raw = post(f"{base}/v1/chat/completions", args.key, args.session_id,
                       {"model": model, "messages": history, "stream": args.stream})
            answer = answer_text("openai", raw, args.stream)
            history.append({"role": "assistant", "content": answer})
        print(f"[{args.format} {args.session_id}] turn {i + 1}: {answer}")


if __name__ == "__main__":
    main()
