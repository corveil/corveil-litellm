"""Corveil for LiteLLM: send your LiteLLM proxy's conversations to Corveil.

corveil-litellm 0.5.0 (https://github.com/corveil/corveil-litellm)

Install it where the proxy runs (LiteLLM 1.94.0 or later):

    pip install corveil-litellm

or, without pip, put this file next to your LiteLLM config.yaml as
corveil_litellm.py. Then add:

    litellm_settings:
      callbacks: ["corveil_litellm.handler"]

and set CORVEIL_API_KEY (a Corveil API key, sk-citadel-...). Restart the proxy.

What it does
  LiteLLM calls this after every request. Requests are grouped into conversations
  by LiteLLM's trace_id (or its session_id, when request_correlation_in_logs is
  on), which LiteLLM fills from Claude Code's session id or from an
  x-litellm-session-id / x-session-id header. When a conversation has been
  quiet for CORVEIL_SESSION_IDLE_SECONDS it is uploaded once to Corveil:

      POST {CORVEIL_URL}/api/crow-sessions/litellm-{trace_id}/artifacts

  Corveil redacts it and turns it into an AgentSession in your company graph.
  Requests still go straight to your providers; this only sees a copy of the log.

  The upload starts with one usage record: the conversation's prompt, completion
  and total tokens and its cost, summed from what LiteLLM computed for each
  request (standard_logging_object), and the models it used. Corveil shows them
  on the AgentSession as reported by LiteLLM (usage_source=litellm). They are
  not Corveil spend and are never billed.

Settings (environment variables)
  CORVEIL_API_KEY               required. Sessions are attributed to this key's owner.
  CORVEIL_KEYS_BY_EMAIL         optional JSON {"alice@acme.com": "sk-citadel-..."}: use each
                                person's own Corveil key, matched on the LiteLLM virtual key's
                                user email, so sessions are attributed to them.
  CORVEIL_URL                   default https://corveil.io
  CORVEIL_SESSION_IDLE_SECONDS  default 900
  CORVEIL_MIN_USER_TURNS        default 2; shorter conversations are not sent
  CORVEIL_TEAMS                 optional comma list of LiteLLM team ids or aliases to include
  CORVEIL_MAX_TOOL_RESULT_CHARS default 2000; longer tool output is cut before upload
  CORVEIL_MAX_UPLOAD_BYTES      default 8000000 (gzipped); oldest turns are dropped past this
  CORVEIL_DRY_RUN               "1" logs what would be sent and sends nothing (no key needed)
  CORVEIL_LOG_LEVEL             default INFO

Needs LiteLLM message logging on for the included traffic (turn_off_message_logging
must be false). Run the proxy with one worker per instance, or ask Corveil to turn on
transcript re-uploads for your org so the most complete copy wins.
"""

from __future__ import annotations

import asyncio
import atexit
import gzip
import json
import os
import random
import time
from datetime import datetime, timezone
from urllib.parse import quote

import logging

import httpx
from litellm.integrations.custom_logger import CustomLogger

# Own logger, so these lines show up without turning on LiteLLM's debug logging.
verbose_logger = logging.getLogger("corveil_litellm")
if not verbose_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s corveil_litellm %(levelname)s %(message)s"))
    verbose_logger.addHandler(_h)
    verbose_logger.propagate = False
verbose_logger.setLevel(os.getenv("CORVEIL_LOG_LEVEL", "INFO").upper())

__version__ = "0.5.0"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


CORVEIL_URL = os.getenv("CORVEIL_URL", "https://corveil.io").rstrip("/")
DEFAULT_KEY = os.getenv("CORVEIL_API_KEY", "").strip()


def _keys_by_email(raw: str) -> dict:
    """Parse CORVEIL_KEYS_BY_EMAIL. Anything but a JSON object of strings is
    ignored with a warning, so a config typo never stops the proxy loading this."""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        verbose_logger.warning("CORVEIL_KEYS_BY_EMAIL is not valid JSON; ignoring it")
        return {}
    if not isinstance(parsed, dict):
        verbose_logger.warning("CORVEIL_KEYS_BY_EMAIL must be a JSON object of email to key; ignoring it")
        return {}
    keys = {k.strip().lower(): v.strip() for k, v in parsed.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}
    if len(keys) != len(parsed):
        verbose_logger.warning("CORVEIL_KEYS_BY_EMAIL: ignoring %d entries that aren't an email and a key", len(parsed) - len(keys))
    return keys


KEYS_BY_EMAIL = _keys_by_email(os.getenv("CORVEIL_KEYS_BY_EMAIL", ""))
IDLE_SECONDS = _env_int("CORVEIL_SESSION_IDLE_SECONDS", 900)
MIN_USER_TURNS = _env_int("CORVEIL_MIN_USER_TURNS", 2)
TEAMS = {t.strip() for t in os.getenv("CORVEIL_TEAMS", "").split(",") if t.strip()}
MAX_TOOL_CHARS = _env_int("CORVEIL_MAX_TOOL_RESULT_CHARS", 2000)
MAX_UPLOAD_BYTES = _env_int("CORVEIL_MAX_UPLOAD_BYTES", 8_000_000)
DRY_RUN = os.getenv("CORVEIL_DRY_RUN", "") == "1"
MAX_SESSIONS = 5000
MAX_MODELS = 20  # per conversation; Corveil keeps no more than this
MAX_ATTEMPTS = 5


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _response_text(slp: dict) -> str:
    resp = slp.get("response")
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices and isinstance(choices[0], dict):
            content = (choices[0].get("message") or {}).get("content")
            if isinstance(content, str):
                return content
    return ""


def _trim_content(content):
    """Cut long tool output: it bloats the upload and is stored unredacted."""
    if not isinstance(content, list):
        return content
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            block = dict(block)
            inner = block.get("content")
            text = inner if isinstance(inner, str) else json.dumps(inner, default=str)
            if len(text) > MAX_TOOL_CHARS:
                block["content"] = text[:MAX_TOOL_CHARS] + f"\n[cut {len(text) - MAX_TOOL_CHARS} chars]"
        elif isinstance(block, dict) and block.get("type") == "image":
            block = {"type": "text", "text": "[image]"}
        out.append(block)
    return out


def _count(v) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _add_usage(s: dict, slp: dict) -> None:
    """Add one request's tokens and cost, as LiteLLM computed them, to its conversation."""
    prompt, completion = _count(slp.get("prompt_tokens")), _count(slp.get("completion_tokens"))
    s["prompt_tokens"] += prompt
    s["completion_tokens"] += completion
    s["total_tokens"] += _count(slp.get("total_tokens")) or prompt + completion
    cost = slp.get("response_cost")
    # LiteLLM reports 0.0 for a model it has no price for, and says why in
    # response_cost_failure_debug_info. That is an unknown cost, not a free call.
    priced = not slp.get("response_cost_failure_debug_info")
    if priced and isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0 and cost == cost and cost != float("inf"):
        s["cost"] = (s["cost"] or 0.0) + float(cost)  # stays None until some request reports a cost
    model = slp.get("model")
    if model and str(model) not in s["models"] and len(s["models"]) < MAX_MODELS:
        s["models"].append(str(model))


def _usage_record(s: dict) -> dict:
    """The conversation's usage, sent as the upload's first line. Corveil shows it
    as reported by LiteLLM and never bills it."""
    rec = {"type": "litellm_usage", "prompt_tokens": s["prompt_tokens"],
           "completion_tokens": s["completion_tokens"], "total_tokens": s["total_tokens"],
           "models": s["models"], "requests": s["calls"]}
    if s["cost"] is not None:
        rec["cost"] = round(s["cost"], 10)
    return rec


def _user_turns(msgs) -> int:
    return sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user")


class CorveilHandler(CustomLogger):
    def __init__(self):
        super().__init__()
        self.sessions: dict[str, dict] = {}
        self._flusher: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        atexit.register(self._flush_all_sync)
        if not DEFAULT_KEY and not KEYS_BY_EMAIL and not DRY_RUN:
            verbose_logger.warning("CORVEIL_API_KEY is not set; nothing will be sent")
        else:
            verbose_logger.info("v%s: sending conversations to %s after %ss idle%s",
                                __version__, CORVEIL_URL, IDLE_SECONDS, " (dry run)" if DRY_RUN else "")

    # ---- LiteLLM hooks ----------------------------------------------------
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            self._record(kwargs.get("standard_logging_object") or {})
        except Exception as e:  # never break the proxy
            verbose_logger.warning("could not record a request: %s", e)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        return  # failed calls add nothing to a transcript

    # ---- buffering --------------------------------------------------------
    def _record(self, slp: dict) -> None:
        # session_id is the caller's conversation id when the proxy runs with
        # request_correlation_in_logs on; trace_id then names one request only.
        sid, msgs = slp.get("session_id") or slp.get("trace_id"), slp.get("messages")
        if not sid or not isinstance(msgs, list) or not msgs:
            return
        if msgs == [{"role": "user", "content": "redacted-by-litellm"}]:
            return  # message logging is off for this request
        md = slp.get("metadata") or {}
        if TEAMS and (md.get("user_api_key_team_id") or "") not in TEAMS and (md.get("user_api_key_team_alias") or "") not in TEAMS:
            return
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.get_running_loop().create_task(self._flush_loop())
        now = time.time()
        started, ended = slp.get("startTime") or now, slp.get("endTime") or now
        s = self.sessions.get(sid)
        if s is None:
            if len(self.sessions) >= MAX_SESSIONS:
                return
            s = self.sessions[sid] = {
                "messages": [], "times": [], "final": "", "final_ts": None, "attempts": 0,
                "email": (md.get("user_api_key_user_email") or "").lower(),
                "alias": md.get("user_api_key_alias") or "", "team": md.get("user_api_key_team_alias") or "",
                "provider": slp.get("custom_llm_provider") or "", "call_type": slp.get("call_type") or "",
                "models": [], "calls": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": None,
            }
        s["calls"] += 1
        _add_usage(s, slp)
        s["last_seen"] = now
        # Each turn keeps its own time: a user turn is when its request reached
        # the proxy, an answer is when the request that produced it finished.
        hist, times = s["messages"], s["times"]
        if len(msgs) >= len(hist) and msgs[: len(hist)] == hist:
            # The client replayed the whole history. The first assistant turn
            # after it is the answer we logged last time.
            answer_ts = s["final_ts"]
            for m in msgs[len(hist):]:
                if isinstance(m, dict) and m.get("role") == "assistant" and answer_ts is not None:
                    times.append(answer_ts)
                    answer_ts = None
                else:
                    times.append(started)
            s["messages"] = list(msgs)
        else:
            if s["final"]:
                hist.append({"role": "assistant", "content": s["final"]})
                times.append(s["final_ts"] or started)
            for m in msgs:
                if isinstance(m, dict) and m.get("role") == "user":
                    hist.append(m)
                    times.append(started)
        s["final"], s["final_ts"] = _response_text(slp), ended

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(max(5, min(60, IDLE_SECONDS // 4)))
            cutoff = time.time() - IDLE_SECONDS
            for sid in [k for k, v in self.sessions.items() if v["last_seen"] < cutoff and v.get("retry_at", 0) <= time.time()]:
                s = self.sessions.pop(sid)
                try:
                    done = await self._upload(sid, s)
                except Exception as e:
                    verbose_logger.warning("upload of %s failed: %s", sid, e)
                    done = False
                if not done and s["attempts"] < MAX_ATTEMPTS:
                    s["retry_at"] = time.time() + min(600, 15 * 2 ** s["attempts"]) + random.random() * 5
                    self.sessions.setdefault(sid, s)

    # ---- upload -----------------------------------------------------------
    def _build(self, sid: str, s: dict):
        key = KEYS_BY_EMAIL.get(s["email"], DEFAULT_KEY)
        if (not key and not DRY_RUN) or _user_turns(s["messages"]) < MIN_USER_TURNS:
            return None
        lines = []
        for m, ts in zip(s["messages"], s["times"]):
            role = m.get("role") if isinstance(m, dict) else None
            if role in ("user", "assistant"):  # system prompts are left out
                lines.append({"type": role, "timestamp": _iso(ts),
                              "message": {"role": role, "content": _trim_content(m.get("content"))}})
        if s["final"]:
            lines.append({"type": "assistant", "timestamp": _iso(s["final_ts"]),
                          "message": {"role": "assistant", "content": [{"type": "text", "text": s["final"]}]}})
        usage = json.dumps(_usage_record(s))
        truncated = False
        while True:
            # The usage record goes first, so Corveil reads it even from a long transcript.
            body = gzip.compress(("\n".join([usage] + [json.dumps(l, default=str) for l in lines]) + "\n").encode())
            if len(body) <= MAX_UPLOAD_BYTES or len(lines) <= 4:
                break
            lines = lines[: 1] + lines[max(2, len(lines) // 4):]  # keep the opening turn, drop the oldest middle
            truncated = True
        harness = "claude" if s["call_type"] == "anthropic_messages" or s["provider"] == "anthropic" else "unknown"
        who = s["email"] or s["alias"] or "someone"
        params = {"harness": harness, "format": "ndjson", "event_count": str(len(lines) + 1), "agent_kind": "litellm",
                  "name": f"LiteLLM · {who}" + (f" · {s['team']}" if s["team"] else "")}
        if truncated:
            params["truncated"] = "true"
        url = f"{CORVEIL_URL}/api/crow-sessions/{quote('litellm-' + sid, safe='')}/artifacts"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/x-ndjson",
                   "User-Agent": f"corveil-litellm/{__version__}"}
        return url, params, body, headers, len(lines)

    def _outcome(self, sid: str, status: int, text: str, n: int) -> bool:
        if status in (200, 201):
            verbose_logger.info("sent conversation %s (%d turns)", sid, n)
            return True
        if status == 409:
            verbose_logger.info("conversation %s was already sent", sid)
            return True
        if status in (400, 401, 403, 404, 413):
            verbose_logger.warning("Corveil refused conversation %s (%s): %s", sid, status, text[:200])
            return True  # retrying will not help
        verbose_logger.warning("Corveil answered %s for %s; will retry", status, sid)
        return False

    async def _upload(self, sid: str, s: dict) -> bool:
        built = self._build(sid, s)
        if built is None:
            return True
        url, params, body, headers, n = built
        s["attempts"] += 1
        if DRY_RUN:
            self._log_dry_run(sid, s, n, body, params)
            return True
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        r = await self._client.post(url, params=params, content=body, headers=headers)
        return self._outcome(sid, r.status_code, r.text, n)

    @staticmethod
    def _log_dry_run(sid: str, s: dict, n: int, body: bytes, params: dict) -> None:
        cost = "unknown" if s["cost"] is None else "$" + (f"{s['cost']:.6f}".rstrip("0").rstrip(".") or "0")
        verbose_logger.info("dry run: would send conversation %s (LiteLLM user %s): %d turns, %d tokens, cost %s as LiteLLM reports it, %d bytes gzipped, %s",
                            sid, s["email"] or "unknown", n, s["total_tokens"], cost, len(body), params)

    def _flush_all_sync(self):
        """On shutdown, send what is still buffered instead of dropping it."""
        if not self.sessions:
            return
        if DRY_RUN:
            for sid, s in list(self.sessions.items()):
                built = self._build(sid, s)
                if built:
                    url, params, body, headers, n = built
                    self._log_dry_run(sid, s, n, body, params)
            return
        try:
            with httpx.Client(timeout=10) as c:
                for sid, s in list(self.sessions.items()):
                    built = self._build(sid, s)
                    if built:
                        url, params, body, headers, n = built
                        r = c.post(url, params=params, content=body, headers=headers)
                        self._outcome(sid, r.status_code, r.text, n)
        except Exception as e:
            verbose_logger.warning("could not send buffered conversations at shutdown: %s", e)


handler = CorveilHandler()
proxy_handler_instance = handler  # LiteLLM's documented name also works
