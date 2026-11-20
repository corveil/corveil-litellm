"""Test setup shared by every test.

The callback reads its settings from the environment when it is imported, so
they are set here, before any test imports it. Prices come from the copy of
LiteLLM's price map that ships with the installed version, not from the
network, so a cost assertion means the same thing on every run.
"""

import os

os.environ["CORVEIL_API_KEY"] = "sk-citadel-test"
os.environ["CORVEIL_URL"] = "https://corveil.test"
for _k in ("CORVEIL_DRY_RUN", "CORVEIL_KEYS_BY_EMAIL", "CORVEIL_TEAMS", "CORVEIL_MIN_USER_TURNS"):
    os.environ.pop(_k, None)
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
