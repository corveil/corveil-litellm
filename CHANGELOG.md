# Changelog

All notable changes to corveil-litellm. Versions follow
[Semantic Versioning](https://semver.org/).

## 0.5.0 - unreleased

First release on PyPI: `pip install corveil-litellm`.

- The callback is the `corveil_litellm` package. The config line stays
  `callbacks: ["corveil_litellm.handler"]`.
- Needs LiteLLM 1.94.0 or later when installed with pip. The single file still
  works next to `config.yaml` on older releases.
- No change in what is sent from 0.4.0.

## 0.4.0

- Sends each conversation's prompt, completion and total tokens, cost and
  models, as LiteLLM computed them, in a usage record at the start of the
  upload. Corveil shows them on the Agent Session with `usage_source=litellm`.
- A model LiteLLM has no price for adds tokens but no cost.

## 0.3.0

- Each turn keeps its own time: a question is stamped when its request reached
  the proxy, and an answer when the request that produced it finished.
- Groups by LiteLLM's `session_id` when `request_correlation_in_logs` is on.
- In dry-run mode, conversations still waiting at shutdown are logged too.

## 0.2.1

- First version, distributed as a single file in the Corveil setup kit.
