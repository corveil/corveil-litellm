"""corveil_litellm under the real litellm package.

litellm builds the standard_logging_object itself (from mock_response calls,
so nothing leaves the machine) and calls the handler the way the proxy does.
Uploads go through a real httpx client whose transport records the request
instead of sending it.

CI runs this file against the oldest litellm this package supports and the
latest one (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import asyncio
import gzip
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import litellm
import pytest

import corveil_litellm

ROOT = Path(__file__).resolve().parent.parent
OLDEST_LITELLM = (1, 94, 0)  # keep in step with pyproject.toml and the CI matrix
# litellm drops retired models from its price map, so the cost check uses the
# first of these that the installed version prices.
ANTHROPIC_MODEL = "anthropic/" + next(
    m for m in ("claude-sonnet-4-6", "claude-sonnet-4-5", "claude-sonnet-4-20250514")
    if (litellm.model_cost.get(m) or {}).get("input_cost_per_token"))
OPENAI_MODEL = "openai/gpt-4o-mini"


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3])


def test_runs_a_supported_litellm():
    installed = importlib.metadata.version("litellm")
    print(f"litellm {installed}")
    assert _version_tuple(installed) >= OLDEST_LITELLM
    req = [r for r in importlib.metadata.requires("corveil-litellm") or [] if r.startswith("litellm")]
    assert req == ["litellm>=" + ".".join(map(str, OLDEST_LITELLM))], req


# ---- packaging -------------------------------------------------------------

def test_exports_the_handler_litellm_is_configured_with():
    assert isinstance(corveil_litellm.handler, corveil_litellm.CorveilHandler)
    assert corveil_litellm.proxy_handler_instance is corveil_litellm.handler


def test_states_its_version_in_the_file():
    # The single-file copy has no package metadata next to it, so the file
    # itself says which release it is, in the docstring and in __version__.
    assert corveil_litellm.__version__ == importlib.metadata.version("corveil-litellm")
    assert f"corveil-litellm {corveil_litellm.__version__} (" in corveil_litellm.__doc__
    assert f"corveil-litellm/{corveil_litellm.__version__}" in _upload_user_agent()


def test_examples_install_this_version():
    compose = (ROOT / "examples" / "docker-compose.example.yml").read_text()
    pins = re.findall(r"corveil-litellm==([\w.]+)", compose)
    assert pins == [corveil_litellm.__version__], pins


def test_the_proxy_loads_the_installed_package(tmp_path):
    # The proxy resolves `callbacks: ["corveil_litellm.handler"]` with
    # get_instance_fn. With no corveil_litellm.py next to config.yaml it imports
    # the module, which is this installed package.
    from litellm.proxy.types_utils.utils import get_instance_fn

    config = tmp_path / "config.yaml"
    config.write_text('litellm_settings:\n  callbacks: ["corveil_litellm.handler"]\n')
    assert get_instance_fn("corveil_litellm.handler", config_file_path=str(config)) is corveil_litellm.handler


def _upload_user_agent() -> str:
    s = _session(messages=[{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    return corveil_litellm.handler._build("ua", s)[3]["User-Agent"]


def _session(**over):
    s = {"messages": [], "times": [], "final": "", "final_ts": None, "attempts": 0, "email": "", "alias": "",
         "team": "", "provider": "", "call_type": "", "models": [], "calls": 0, "prompt_tokens": 0,
         "completion_tokens": 0, "total_tokens": 0, "cost": None}
    s.update(over)
    s["times"] = [time.time()] * len(s["messages"])
    return s


# ---- the callback under litellm ---------------------------------------------

@pytest.fixture
def kit():
    litellm.callbacks = [corveil_litellm.handler]
    h = corveil_litellm.handler
    h.sessions.clear()
    h._client = None
    yield corveil_litellm
    h.sessions.clear()  # nothing left for the atexit flush to send
    litellm.callbacks = []


def recorder():
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json={"id": "a"})

    return sent, httpx.MockTransport(handle)


async def wait_for_calls(handler, sid, n, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = handler.sessions.get(sid)
        if s and s["calls"] >= n:
            return s
        await asyncio.sleep(0.05)
    raise AssertionError(f"litellm logged {handler.sessions.get(sid, {}).get('calls', 0)} of {n} calls for {sid}")


METADATA = {"user_api_key_user_email": "Dana@Example.com", "user_api_key_team_alias": "platform",
            "user_api_key_alias": "dana-laptop"}
TURNS = ["Draft a two-sentence status update on the rollout for the platform team.",
         "Now shorten that update to one sentence and mention Friday's check."]


async def openai_conversation(sid: str, per_request_trace: bool = False, model: str = OPENAI_MODEL):
    history = []
    for i, text in enumerate(TURNS):
        if i:
            await asyncio.sleep(1.1)  # so each turn has its own second
        history.append({"role": "user", "content": text})
        extra = {"litellm_trace_id": f"{sid}-request-{i}"} if per_request_trace else {}
        resp = await litellm.acompletion(model=model, messages=list(history),
                                         mock_response=f"answer {i + 1}", litellm_session_id=sid,
                                         metadata=dict(METADATA), api_key="sk-unused", **extra)
        history.append({"role": "assistant", "content": resp.choices[0].message.content})


async def anthropic_conversation(sid: str):
    history = []
    for i, text in enumerate(TURNS):
        if i:
            await asyncio.sleep(1.1)
        history.append({"role": "user", "content": text})
        resp = await litellm.anthropic.messages.acreate(
            model=ANTHROPIC_MODEL, messages=list(history), max_tokens=64,
            mock_response=f"answer {i + 1}", litellm_session_id=sid, metadata=dict(METADATA),
            api_key="sk-ant-unused")
        answer = resp["content"][0]["text"] if isinstance(resp, dict) else resp.content[0].text
        history.append({"role": "assistant", "content": [{"type": "text", "text": answer}]})


@pytest.mark.parametrize("fmt,converse,harness,model", [
    ("openai", openai_conversation, "unknown", OPENAI_MODEL),
    ("anthropic", anthropic_conversation, "claude", ANTHROPIC_MODEL),
])
def test_uploads_the_transcript_shape_corveil_derives(kit, fmt, converse, harness, model):
    sid = f"pytest-{fmt}-{os.getpid()}"
    sent, transport = recorder()

    async def main():
        h = kit.handler
        h._client = httpx.AsyncClient(transport=transport)
        await converse(sid)
        await wait_for_calls(h, sid, 2)
        assert await h._upload(sid, h.sessions.pop(sid)) is True
        h._flusher.cancel()

    asyncio.run(main())

    assert len(sent) == 1
    req = sent[0]
    assert req.url.host == "corveil.test"
    assert req.url.path == f"/api/crow-sessions/litellm-{sid}/artifacts"
    assert req.headers["Authorization"] == "Bearer sk-citadel-test"
    assert req.headers["Content-Type"] == "application/x-ndjson"
    assert req.headers["User-Agent"] == f"corveil-litellm/{kit.__version__}"
    params = dict(req.url.params)
    assert params == {"harness": harness, "format": "ndjson", "event_count": "5", "agent_kind": "litellm",
                      "name": "LiteLLM · dana@example.com · platform"}

    lines = [json.loads(l) for l in gzip.decompress(req.content).decode().splitlines()]
    # First the conversation's usage, summed from what litellm computed for
    # each request, then the turns.
    usage = lines.pop(0)
    assert usage["type"] == "litellm_usage"
    assert usage["requests"] == 2
    assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    # litellm logs the model with or without its provider prefix, by route.
    assert usage["models"] in ([model], [model.split("/", 1)[1]]), usage["models"]
    assert usage.get("cost", 0) > 0, usage  # both models are in litellm's price map
    assert [l["type"] for l in lines] == ["user", "assistant", "user", "assistant"]
    for l in lines:
        assert l["message"]["role"] == l["type"]
        assert l["timestamp"].endswith("Z")
    assert lines[0]["message"]["content"] == TURNS[0]
    assert lines[2]["message"]["content"] == TURNS[1]
    texts = [l["message"]["content"] for l in lines[1::2]]
    flat = [t if isinstance(t, str) else t[0]["text"] for t in texts]
    assert flat == ["answer 1", "answer 2"]

    # Each turn keeps its own time: in order, and the second question is
    # stamped at its own request, not at the first one's.
    times = [datetime.fromisoformat(l["timestamp"].replace("Z", "+00:00")) for l in lines]
    assert times == sorted(times)
    assert (times[2] - times[0]).total_seconds() >= 1.0
    assert times[1] < times[2]


@pytest.mark.skipif(not hasattr(litellm, "request_correlation_in_logs"),
                    reason="this litellm has no request_correlation_in_logs")
def test_groups_by_session_id_when_trace_ids_are_per_request(kit, monkeypatch):
    # With request_correlation_in_logs on, a caller's own per-request trace id
    # (x-litellm-trace-id) becomes trace_id, and the conversation is in
    # session_id instead.
    monkeypatch.setattr(litellm, "request_correlation_in_logs", True)
    sid = f"pytest-correlated-{os.getpid()}"

    async def main():
        await openai_conversation(sid, per_request_trace=True)
        s = await wait_for_calls(kit.handler, sid, 2)
        kit.handler._flusher.cancel()
        return s

    s = asyncio.run(main())
    assert list(kit.handler.sessions) == [sid]
    assert [m["role"] for m in s["messages"]] == ["user", "assistant", "user"]


def test_skips_traffic_when_message_logging_is_off(kit, monkeypatch):
    # turn_off_message_logging blanks the conversation for every callback.
    monkeypatch.setattr(litellm, "turn_off_message_logging", True)
    sid = f"pytest-nolog-{os.getpid()}"

    async def main():
        await openai_conversation(sid)
        await asyncio.sleep(1)  # the success callbacks run after the response

    asyncio.run(main())
    assert kit.handler.sessions == {}


def test_shutdown_sends_what_is_buffered(kit, monkeypatch):
    sid = f"pytest-shutdown-{os.getpid()}"
    sent, transport = recorder()
    real_client = httpx.Client
    monkeypatch.setattr(kit.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))

    async def main():
        await openai_conversation(sid)
        await wait_for_calls(kit.handler, sid, 2)
        kit.handler._flusher.cancel()

    asyncio.run(main())
    kit.handler._flush_all_sync()  # what atexit runs when the proxy stops
    monkeypatch.undo()
    assert [r.url.path for r in sent] == [f"/api/crow-sessions/litellm-{sid}/artifacts"]


def test_unpriced_model_reports_tokens_without_cost(kit):
    # A model litellm has no price for adds tokens but no cost.
    sid = f"pytest-unpriced-{os.getpid()}"

    async def main():
        await openai_conversation(sid, model="openai/corveil-unpriced-test-model")
        s = await wait_for_calls(kit.handler, sid, 2)
        kit.handler._flusher.cancel()
        return s

    s = asyncio.run(main())
    usage = kit._usage_record(s)
    assert usage["total_tokens"] > 0
    assert "cost" not in usage, usage


# ---- /v1/messages through litellm's real Anthropic provider code ---------------
# mock_response can't test cost on this route: litellm's mocked Messages reply
# names a fixed model, and litellm prices that one, not the model asked for. So
# these call e2e/fake_upstream.py, which answers as the Anthropic API would and
# echoes the requested model, the way the proxy's /v1/messages reaches Anthropic.

@pytest.fixture(scope="module")
def anthropic_api():
    from http.server import ThreadingHTTPServer
    sys.path.insert(0, str(ROOT / "e2e"))
    try:
        import fake_upstream
    finally:
        sys.path.remove(str(ROOT / "e2e"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fake_upstream.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def messages_usage(kit, base: str, model: str) -> dict:
    sid = f"pytest-messages-{model.rsplit('/', 1)[-1]}-{os.getpid()}"

    async def main():
        await litellm.anthropic.messages.acreate(
            model=model, messages=[{"role": "user", "content": TURNS[0]}], max_tokens=64,
            api_base=base, api_key="sk-ant-unused", litellm_session_id=sid, metadata=dict(METADATA))
        s = await wait_for_calls(kit.handler, sid, 1)
        kit.handler._flusher.cancel()
        return s

    return kit._usage_record(asyncio.run(main()))


def _listed(model: str) -> bool:
    return model in litellm.model_cost or model.split("/", 1)[1] in litellm.model_cost


@pytest.mark.parametrize("model", [
    "anthropic/claude-corveil-unmapped-test",
    # Retired from litellm's price map in later releases. There, litellm prices
    # it at 0 from zero prices of its own, without saying the price is missing.
    "anthropic/claude-sonnet-4-20250514",
])
def test_messages_route_model_outside_the_price_map_has_no_cost(kit, anthropic_api, model):
    if _listed(model):
        pytest.skip(f"this litellm still prices {model}")
    usage = messages_usage(kit, anthropic_api, model)
    assert usage["total_tokens"] > 0
    assert "cost" not in usage, usage


def test_messages_route_priced_model_reports_cost(kit, anthropic_api):
    usage = messages_usage(kit, anthropic_api, ANTHROPIC_MODEL)
    assert usage.get("cost", 0) > 0, usage


def test_a_model_the_price_map_lists_as_free_reports_zero_cost(kit, anthropic_api):
    # A 0 from a model the price map lists (here, as an operator's config would
    # register it) is a free call, not an unknown one.
    model = "anthropic/corveil-free-test-model"
    litellm.register_model({model: {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0,
                                    "litellm_provider": "anthropic", "mode": "chat"}})
    try:
        usage = messages_usage(kit, anthropic_api, model)
    finally:
        litellm.model_cost.pop(model, None)
    assert usage["total_tokens"] > 0
    assert usage.get("cost") == 0.0, usage


def test_zero_cost_counts_only_for_a_model_the_price_map_lists(monkeypatch):
    s = _session(calls=3)
    # Listed and free: counts as $0.
    corveil_litellm._add_usage(s, {"response_cost": 0.0, "model": "gpt-4o-mini", "custom_llm_provider": "openai"})
    assert s["cost"] == 0.0
    s = _session(calls=1)
    # Not listed, and litellm said nothing about it: unknown.
    corveil_litellm._add_usage(s, {"response_cost": 0.0, "model": "corveil-not-a-model", "custom_llm_provider": "anthropic"})
    # get_model_info answering with zero prices of its own, for a key the map
    # doesn't have (what litellm 1.110 does for claude-sonnet-4-20250514): unknown.
    monkeypatch.setattr(litellm, "get_model_info", lambda **kw: {"key": "anthropic/corveil-synth", "input_cost_per_token": 0})
    corveil_litellm._add_usage(s, {"response_cost": 0.0, "model": "corveil-synth", "custom_llm_provider": "anthropic"})
    assert s["cost"] is None
    # A positive cost never needs the price map.
    corveil_litellm._add_usage(s, {"response_cost": 0.5, "model": "corveil-synth"})
    assert s["cost"] == 0.5


# ---- in a fresh interpreter: settings are read at import ---------------------

def run_python(script: str, **env) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("CORVEIL_")}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)], env={**base, **env},
                          capture_output=True, text=True, timeout=180)


def test_dry_run_logs_the_documented_line_and_sends_nothing():
    res = run_python("""
        import asyncio
        import httpx, litellm
        def refuse(*a, **k):
            raise AssertionError("dry run created an HTTP client")
        httpx.AsyncClient = httpx.Client = refuse
        import corveil_litellm as c
        litellm.callbacks = [c.handler]
        async def main():
            hist = []
            for t in ["first question for the dry run", "second question for the dry run"]:
                hist.append({"role": "user", "content": t})
                await litellm.acompletion(model="openai/gpt-4o-mini", messages=list(hist), mock_response="ok",
                                          litellm_session_id="dry-1", api_key="x",
                                          metadata={"user_api_key_user_email": "dana@example.com"})
                hist.append({"role": "assistant", "content": "ok"})
            for _ in range(200):
                if c.handler.sessions.get("dry-1", {}).get("calls") == 2:
                    break
                await asyncio.sleep(0.05)
            await c.handler._upload("dry-1", c.handler.sessions.pop("dry-1"))
            c.handler._flusher.cancel()
        asyncio.run(main())
    """, CORVEIL_DRY_RUN="1")
    assert res.returncode == 0, res.stderr
    assert "(dry run)" in res.stderr
    assert "corveil_litellm INFO dry run: would send conversation dry-1 (LiteLLM user dana@example.com): 4 turns," in res.stderr


def test_without_a_key_it_warns_and_sends_nothing():
    res = run_python("import corveil_litellm as c\nprint(c.handler._build('x', {'email': '', 'messages': []}))\n")
    assert res.returncode == 0, res.stderr
    assert "CORVEIL_API_KEY is not set; nothing will be sent" in res.stderr
    assert res.stdout.strip() == "None"


@pytest.mark.parametrize("raw", ["[]", "null", "1", "true", '"x"', "{bad", ""])
def test_a_keys_by_email_that_is_not_a_json_object_is_ignored(raw):
    res = run_python("import corveil_litellm as c\nprint(repr(c.KEYS_BY_EMAIL))\n",
                     CORVEIL_API_KEY="sk-citadel-test", CORVEIL_KEYS_BY_EMAIL=raw)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "{}"


def test_keys_by_email_keeps_string_keys_and_lowercases_emails():
    res = run_python("import corveil_litellm as c\nprint(repr(c.KEYS_BY_EMAIL))\n", CORVEIL_API_KEY="sk-citadel-test",
                     CORVEIL_KEYS_BY_EMAIL='{"Alice@Acme.com":"sk-citadel-a","bob@acme.com":7}')
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "{'alice@acme.com': 'sk-citadel-a'}"


# ---- usage arithmetic ---------------------------------------------------------

def test_usage_ignores_bad_counts_and_unknown_costs():
    s = _session(calls=2)
    corveil_litellm._add_usage(s, {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": None,
                                   "response_cost": None, "model": "m"})
    corveil_litellm._add_usage(s, {"prompt_tokens": -4, "completion_tokens": "x", "total_tokens": 5,
                                   "response_cost": float("nan"), "model": "m"})
    # An unpriced model: litellm logs cost 0.0 and says why. Unknown, not free.
    corveil_litellm._add_usage(s, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
                                   "response_cost": 0.0, "model": "n",
                                   "response_cost_failure_debug_info": {"error_str": "This model isn't mapped yet."}})
    assert corveil_litellm._usage_record(s) == {
        "type": "litellm_usage", "prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 17,
        "models": ["m", "n"], "requests": 2,
    }
