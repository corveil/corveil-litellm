# corveil-litellm

A [LiteLLM](https://github.com/BerriAI/litellm) proxy callback that sends the
conversations your proxy already handles to your [Corveil](https://corveil.com)
workspace, where each one becomes part of your company graph.

Requests keep going straight to your providers. The callback only reads a copy
of each log after the response is sent.

## Install

In the same Python environment as the LiteLLM proxy:

```
pip install corveil-litellm
```

This needs LiteLLM 1.94.0 or later: older proxies load a callback only from a
file next to `config.yaml`. Check with `litellm --version` first. On an older
LiteLLM, pip would upgrade it to meet that requirement, so use the single file
(see **Without pip**) instead.

Then add the callback to your LiteLLM `config.yaml`:

```yaml
litellm_settings:
  callbacks: ["corveil_litellm.handler"]
```

If you already have callbacks, add it to the list. Set `CORVEIL_API_KEY` and
restart the proxy.

### Docker

The official LiteLLM image has no `pip` command, so add it with `ensurepip`
when you build on top of the image:

```dockerfile
FROM ghcr.io/berriai/litellm:main-stable
RUN python3 -m ensurepip && python3 -m pip install --no-cache-dir corveil-litellm
```

[`examples/docker-compose.example.yml`](https://github.com/corveil/corveil-litellm/blob/main/examples/docker-compose.example.yml)
does the same with Docker Compose. With an image of LiteLLM 1.94.0 or later,
pip leaves the image's LiteLLM as it is.

### Without pip

The whole callback is one file:
[`src/corveil_litellm/__init__.py`](https://github.com/corveil/corveil-litellm/blob/main/src/corveil_litellm/__init__.py). Save it
as `corveil_litellm.py` in the same folder as your `config.yaml`. With
`docker-compose`, mount it next to the config. The config line is the same.
This also works on LiteLLM releases older than 1.94.0, which only load a
callback module from the config's folder; it has been tested back to 1.74.4.

If both are present, LiteLLM loads the `corveil_litellm.py` next to the config,
not the installed package.

Read the file before you install it. It is about 380 lines and imports only
`litellm` and `httpx`.

## 1. Try it without sending anything

1. Install it and add the callback line as above.
2. Set `CORVEIL_DRY_RUN=1` and `CORVEIL_SESSION_IDLE_SECONDS=60`, then restart
   the proxy.
3. Use Claude Code or your app through LiteLLM as usual (other clients need the
   session header described below). A minute after a conversation goes quiet,
   the proxy log shows a line like:

   ```
   corveil_litellm INFO dry run: would send conversation 7f3c… (LiteLLM user alice@acme.com): 12 turns, 48210 tokens, cost $0.3127 as LiteLLM reports it, 8312 bytes gzipped, {…}
   ```

   Nothing leaves your network in dry-run mode.

## 2. Send conversations to Corveil

1. Sign in to Corveil at https://corveil.io and create an API key under
   **People & Access → API Keys**. Or run `corveil login`.
2. Set `CORVEIL_API_KEY=sk-citadel-…`, remove `CORVEIL_DRY_RUN`, and set
   `CORVEIL_SESSION_IDLE_SECONDS` back to the default (900) or leave it out.
3. Restart the proxy. The log shows `sending conversations to https://corveil.io`.
4. Within about 25 minutes of a conversation going quiet (15 minutes of quiet,
   then up to 10 minutes for Corveil to build it), it appears in Corveil under
   **Ontology** as an Agent Session named `LiteLLM · <email>`, followed by the
   LiteLLM team if the key has one, then when the conversation started and the
   end of its session id. For example:
   `LiteLLM · alice@acme.com · platform (2026-11-19 14:05 UTC, 3f9c2a1b)`.
   It shows the conversation's token counts and cost, as LiteLLM reports
   them (see **Token counts and cost** below).

## Who a conversation is credited to

Corveil credits each conversation to the owner of the API key that sent it.
With one `CORVEIL_API_KEY`, everything is credited to that person, and the
LiteLLM user's email is kept in the session's name.

To credit each person, invite them to your Corveil workspace. Once they have
signed in, each of them creates a key under **People & Access → API Keys**.
Then map LiteLLM user emails to those keys:

```
CORVEIL_KEYS_BY_EMAIL={"alice@acme.com":"sk-citadel-…","bob@acme.com":"sk-citadel-…"}
```

The email comes from the LiteLLM virtual key's user (`user_email`).

## What it needs from LiteLLM

- **Message logging on** for the traffic you include. If
  `turn_off_message_logging` is true, LiteLLM blanks the conversation for every
  callback and this callback skips it.
- **A session id.** Claude Code sends one automatically. For other clients, send
  `x-litellm-session-id: <your conversation id>` on each request. Without one,
  each request counts as its own conversation, and one-off requests are skipped.

## Token counts and cost

LiteLLM works out the tokens and cost of every request. The callback adds them
up for each conversation and sends the totals with it, along with the models
it used. In Corveil the Agent Session shows `prompt_tokens`,
`completion_tokens`, `total_tokens`, `cost` (US dollars) and `models_used`,
with `usage_source` set to `litellm`, so you can tell them from usage Corveil
measured itself.

These are LiteLLM's numbers, not Corveil's. Corveil doesn't bill them or count
them toward your Corveil usage, spend limits or budgets. A model LiteLLM has no
price for adds tokens but no cost, and a conversation where no request had a
price shows no cost at all. Only requests with message logging on are counted.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `CORVEIL_API_KEY` | | Corveil key used to send conversations |
| `CORVEIL_KEYS_BY_EMAIL` | | JSON map of LiteLLM user email to that person's Corveil key |
| `CORVEIL_URL` | `https://corveil.io` | Your Corveil address, if self-hosted |
| `CORVEIL_SESSION_IDLE_SECONDS` | `900` | How long a conversation must be quiet before it is sent |
| `CORVEIL_MIN_USER_TURNS` | `2` | Shorter conversations are not sent |
| `CORVEIL_TEAMS` | | Comma list of LiteLLM team ids or aliases to include; empty means all |
| `CORVEIL_MAX_TOOL_RESULT_CHARS` | `2000` | Long tool output is cut before sending |
| `CORVEIL_MAX_UPLOAD_BYTES` | `8000000` | Gzipped upload limit; the oldest turns are dropped past it |
| `CORVEIL_DRY_RUN` | | `1` logs what would be sent and sends nothing |
| `CORVEIL_LOG_LEVEL` | `INFO` | `WARNING` for quieter logs |

## Good to know

- **What is sent:** user and assistant turns, tool calls, tool output cut to
  2,000 characters, and the conversation's token counts, cost and models. System
  prompts are not sent. Corveil keeps the transcript as sent for 30 days and
  redacts secrets in the session it builds from it.
- **Several workers or pods:** each LiteLLM worker keeps its own copy of a
  conversation, and Corveil keeps the first copy it receives. If you run more
  than one worker, tell us and we'll switch your workspace to keep the most
  complete copy.
- **Restarts:** conversations still waiting are sent when the proxy shuts down
  cleanly. A crash loses only the conversations that hadn't gone quiet yet.
- **Turning it off:** remove the callback line and restart. To delete what was
  sent, contact us.

## Supported LiteLLM versions

CI runs the tests against LiteLLM 1.94.0, the oldest supported release, and the
latest release, and every week runs the end-to-end check in [`e2e/`](https://github.com/corveil/corveil-litellm/tree/main/e2e):
the official LiteLLM proxy image with this package pip-installed, sending
conversations to a stand-in Corveil.

## Development

```
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

Releases are published to PyPI from GitHub Actions (`.github/workflows/release.yml`)
through a PyPI trusted publisher, so no PyPI token is stored. To release X.Y.Z:

1. Set `__version__` and the version in the module docstring
   (`src/corveil_litellm/__init__.py`) and the pin in
   `examples/docker-compose.example.yml` to X.Y.Z, and give `CHANGELOG.md` a
   dated `## X.Y.Z` section. Merge that to `main`.
2. Tag the merge commit `vX.Y.Z` and push the tag. The workflow runs the tests,
   checks the tag, code and changelog agree, publishes to PyPI and creates the
   GitHub release with the wheel, the sdist and `corveil_litellm.py`.

## License

[Apache License 2.0](https://github.com/corveil/corveil-litellm/blob/main/LICENSE).
