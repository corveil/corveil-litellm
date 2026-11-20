# End-to-end check

`run.sh` builds the official LiteLLM proxy image with corveil-litellm
installed by pip, configures the proxy with the one callback line from the
README, sends it real traffic and checks what arrives at a stand-in Corveil.
The `End-to-end` workflow runs it every Monday against the current stable
LiteLLM image, and on demand.

| File | What it is |
|---|---|
| `Dockerfile` | The proxy image plus `python3 -m ensurepip && python3 -m pip install corveil-litellm` |
| `docker-compose.yml` | Postgres for the proxy's keys, the proxy, a fake provider and a fake Corveil |
| `litellm.config.yaml` | Two models, `claude-e2e` and `gpt-e2e`, plus the two lines from `../examples/config.example.yaml` |
| `fake_upstream.py` | Stands in for the Anthropic and OpenAI APIs, so no provider key is needed |
| `fake_corveil.py` | Stands in for Corveil's upload endpoint and records each upload |
| `send_conversation.py` | Sends one two-turn conversation in Anthropic or OpenAI format |
| `check_uploads.py` | Checks the recorded uploads |
| `run.sh` | Runs the whole check |

The proxy runs its real provider code. Only the provider and Corveil are fake.

## Needs

Docker with Compose, Python 3 with pip. Ports 4000 (LiteLLM) and 8010 (the
fake Corveil) free on 127.0.0.1; set `E2E_LITELLM_PORT` or
`E2E_FAKE_CORVEIL_PORT` to move them.

## Run it

```
./e2e/run.sh
```

1. **Install.** Builds a wheel from this checkout, installs it into the proxy
   image, and checks the proxy imports `corveil_litellm` from
   `site-packages`. Nothing is mounted next to `config.yaml`.
2. **Dry run.** With `CORVEIL_DRY_RUN=1`, creates a LiteLLM user
   `dana@example.com` in team `platform` with a virtual key, sends one
   conversation, waits for the documented line and checks nothing was sent:

   ```
   corveil_litellm INFO dry run: would send conversation e2e-dry-… (LiteLLM user dana@example.com): 4 turns, 76 tokens, cost $0.000028 as LiteLLM reports it, 375 bytes gzipped, {…}
   ```

3. **Live.** Sends two conversations, one in Anthropic format (streamed, as
   Claude Code does) and one in OpenAI format, each with two turns and
   `x-litellm-session-id`. After `CORVEIL_SESSION_IDLE_SECONDS` (20 here) of
   quiet, each is uploaded once. The script checks every upload: the URL, the
   key, the query (`name=LiteLLM · dana@example.com · platform`,
   `agent_kind=litellm`, the harness), the usage record LiteLLM's tokens and
   cost were summed into, and the four turns in order.

   ```
   ok litellm-e2e-anthropic-…: harness=claude 76 tokens, cost=0.00066, models=['anthropic/claude-sonnet-4-6'], 4 turns
   ok litellm-e2e-openai-…: harness=unknown 76 tokens, cost=2.76e-05, models=['openai/gpt-4o-mini'], 4 turns

   PASS: corveil-litellm 0.5.0, pip-installed in ghcr.io/berriai/litellm:main-stable
   ```

## Options

| Variable | Default | What it does |
|---|---|---|
| `LITELLM_IMAGE` | `ghcr.io/berriai/litellm:main-stable` | The proxy image to build on, e.g. `ghcr.io/berriai/litellm:v1.94.0` |
| `E2E_INSTALL` | the wheel built from this checkout | What to pip install instead, e.g. `corveil-litellm==0.5.0` from PyPI |
| `CORVEIL_URL`, `CORVEIL_API_KEY` | the fake Corveil | Send the live conversations to a real Corveil; the script then checks only that it accepted them |

Against a real Corveil, use a test key in a test organization. Each
conversation appears there under **Ontology** as an Agent Session named
`LiteLLM · dana@example.com · platform (…)` within about 25 minutes.

To start over, run `docker compose down -v` in this folder.
