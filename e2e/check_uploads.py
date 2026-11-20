#!/usr/bin/env python3
"""Check what the stand-in Corveil received in the end-to-end run.

Reads fake_corveil.py's GET /received JSON on stdin and checks that each of
the two conversations was uploaded exactly once, in the shape Corveil builds
an Agent Session from. Prints one summary line per conversation.

    curl -s localhost:8010/received | python3 check_uploads.py \\
        --version 0.5.0 --email dana@example.com --anthropic <sid> --openai <sid>
"""

from __future__ import annotations

import argparse
import json
import sys


def check(up: dict, sid: str, harness: str, version: str, email: str) -> list[str]:
    errs = []

    def want(cond: bool, msg: str) -> None:
        if not cond:
            errs.append(f"{sid}: {msg}")

    p, lines = up["params"], up["lines"]
    want(up["authorization"] == "Bearer sk-citadel-e2e", f"authorization {up['authorization']!r}")
    want(up["content_type"] == "application/x-ndjson", f"content type {up['content_type']!r}")
    want(up["user_agent"] == f"corveil-litellm/{version}", f"user agent {up['user_agent']!r}")
    want(p.get("name") == f"LiteLLM · {email} · platform", f"name {p.get('name')!r}")
    want(p.get("agent_kind") == "litellm" and p.get("format") == "ndjson", f"params {p}")
    want(p.get("harness") == harness, f"harness {p.get('harness')!r}, want {harness!r}")
    want(p.get("event_count") == str(len(lines)), f"event_count {p.get('event_count')!r} for {len(lines)} lines")
    want(bool(lines) and lines[0].get("type") == "litellm_usage", "first line is not the usage record")
    if errs:
        return errs
    usage, turns = lines[0], lines[1:]
    want(usage.get("requests") == 2, f"usage requests {usage.get('requests')!r}")
    want(usage.get("total_tokens", 0) > 0, f"usage total_tokens {usage.get('total_tokens')!r}")
    want(usage.get("total_tokens") == usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
         f"usage tokens don't add up: {usage}")
    want(len(usage.get("models") or []) == 1, f"usage models {usage.get('models')!r}")
    if harness == "unknown":  # gpt-4o-mini is in every LiteLLM price map
        want(usage.get("cost", 0) > 0, f"usage cost {usage.get('cost')!r}")
    want([t.get("type") for t in turns] == ["user", "assistant", "user", "assistant"],
         f"turns {[t.get('type') for t in turns]}")
    want(all(t.get("message", {}).get("role") == t.get("type") for t in turns), "a turn's role differs from its type")
    want(all(str(t.get("timestamp", "")).endswith("Z") for t in turns), "a turn has no UTC timestamp")
    stamps = [t.get("timestamp") for t in turns]
    want(stamps == sorted(stamps), f"turns out of order: {stamps}")
    if not errs:
        print(f"ok {up['session_uid']}: harness={harness} {usage['total_tokens']} tokens, "
              f"cost={usage.get('cost')}, models={usage['models']}, {len(turns)} turns")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--anthropic", required=True, help="session id of the Anthropic-format conversation")
    ap.add_argument("--openai", required=True, help="session id of the OpenAI-format conversation")
    a = ap.parse_args()
    uploads = json.load(sys.stdin)
    errs = []
    for sid, harness in ((a.anthropic, "claude"), (a.openai, "unknown")):
        mine = [u for u in uploads if u["session_uid"] == f"litellm-{sid}"]
        if len(mine) != 1:
            errs.append(f"{sid}: uploaded {len(mine)} times, want 1")
            continue
        errs += check(mine[0], sid, harness, a.version, a.email)
    for e in errs:
        print("FAIL " + e, file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
