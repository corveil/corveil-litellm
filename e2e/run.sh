#!/usr/bin/env bash
# End-to-end check: the official LiteLLM proxy image with corveil-litellm
# installed by pip, configured with the one callback line from the README,
# sends real proxy traffic to a stand-in Corveil. See README.md in this folder.
#
#   ./run.sh
#
# 1. Install: builds a wheel from this checkout (or installs $E2E_INSTALL, e.g.
#    corveil-litellm==0.5.0 from PyPI) into the proxy image, and checks the
#    proxy imports it from site-packages, not from a file next to config.yaml.
# 2. Dry run: with CORVEIL_DRY_RUN=1 the proxy logs the documented
#    "dry run: would send" line and the stand-in Corveil receives nothing.
# 3. Live: two two-turn conversations, one in Anthropic format (streamed, as
#    Claude Code does) and one in OpenAI format. After
#    CORVEIL_SESSION_IDLE_SECONDS of quiet each is uploaded once, and the
#    script checks every upload: the URL, the query, the key, the usage record
#    and the four turns.
#
# With CORVEIL_URL and CORVEIL_API_KEY set, step 3 sends to that Corveil
# instead and checks only that it accepted both conversations.
set -euo pipefail

cd "$(dirname "$0")"
LITELLM_URL=http://127.0.0.1:${E2E_LITELLM_PORT:-4000}
FAKE_CORVEIL_URL=http://127.0.0.1:${E2E_FAKE_CORVEIL_PORT:-8010}
export CORVEIL_SESSION_IDLE_SECONDS=${CORVEIL_SESSION_IDLE_SECONDS:-20}
REAL_CORVEIL=${CORVEIL_URL:-}
MASTER="Authorization: Bearer sk-e2e-master"
JSON="Content-Type: application/json"
EMAIL=dana@example.com

say() { printf '\n== %s\n' "$*"; }
die() { printf 'FAIL: %s\n' "$*" >&2; docker compose logs litellm 2>&1 | tail -40 >&2; exit 1; }
rand() { python3 -c 'import secrets; print(secrets.token_hex(4))'; }
json_get() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# wait_for SECONDS DESCRIPTION COMMAND...: retry COMMAND every 2s.
wait_for() {
  local secs=$1 what=$2; shift 2
  local end=$((SECONDS + secs))
  until "$@" >/dev/null 2>&1; do
    ((SECONDS < end)) || die "timed out after ${secs}s waiting for $what"
    sleep 2
  done
}

start_litellm() {
  docker compose up -d --force-recreate --no-build litellm >/dev/null 2>&1
  wait_for 180 "the LiteLLM proxy" curl -sf "$LITELLM_URL/health/readiness"
}

# Read the whole log first: grep -q stops early, which pipefail would report.
proxy_logged() { local log; log=$(docker compose logs litellm 2>&1); grep -qF "$1" <<<"$log"; }
received() { curl -sf "$FAKE_CORVEIL_URL/received"; }

say "1. Install"
rm -rf .work && mkdir -p .work/dist
if [ -z "${E2E_INSTALL:-}" ]; then
  python3 -m pip wheel -q --no-deps -w .work/dist .. >/dev/null
  ls .work/dist
fi
docker compose build -q litellm
docker compose up -d --wait postgres >/dev/null 2>&1
docker compose up -d fake-upstream fake-corveil >/dev/null 2>&1
wait_for 30 "the stand-in Corveil" curl -sf "$FAKE_CORVEIL_URL/health"
WHERE=$(docker compose run --rm --no-deps -T --entrypoint python3 litellm -c \
  "import os, corveil_litellm as c; print(c.__version__, c.__file__, os.path.exists('/app/corveil_litellm.py'))" 2>/dev/null | tail -1)
echo "installed: $WHERE"
read -r VERSION FILE LOOSE <<<"$WHERE"
[[ $FILE == */site-packages/corveil_litellm/__init__.py ]] || die "corveil_litellm was not imported from site-packages: $FILE"
[ "$LOOSE" = False ] || die "the image has a corveil_litellm.py next to config.yaml"

# A LiteLLM virtual key whose user has an email and a team, so the session is
# named after a person. LiteLLM keeps one user per email, so the user and team
# are made once and then reused.
LITELLM_USER=dana
LITELLM_TEAM=platform-e2e
litellm_key() {
  curl -sf "$LITELLM_URL/team/info?team_id=$LITELLM_TEAM" -H "$MASTER" >/dev/null ||
    curl -sf -X POST "$LITELLM_URL/team/new" -H "$MASTER" -H "$JSON" \
      -d "{\"team_id\":\"$LITELLM_TEAM\",\"team_alias\":\"platform\"}" >/dev/null || return 1
  local info
  info=$(curl -sf "$LITELLM_URL/user/info?user_id=$LITELLM_USER" -H "$MASTER" || true)
  [ -n "$info" ] ||
    curl -sf -X POST "$LITELLM_URL/user/new" -H "$MASTER" -H "$JSON" \
      -d "{\"user_id\":\"$LITELLM_USER\",\"user_email\":\"$EMAIL\"}" >/dev/null || return 1
  curl -sf -X POST "$LITELLM_URL/key/generate" -H "$MASTER" -H "$JSON" \
    -d "{\"user_id\":\"$LITELLM_USER\",\"team_id\":\"$LITELLM_TEAM\",\"key_alias\":\"dana-laptop-$(rand)\"}" | json_get "d['key']"
}

say "2. Dry run"
CORVEIL_URL=http://fake-corveil:8000 CORVEIL_DRY_RUN=1 start_litellm
proxy_logged "v$VERSION: sending conversations to http://fake-corveil:8000 after ${CORVEIL_SESSION_IDLE_SECONDS}s idle (dry run)" \
  || die "the proxy did not log the callback's dry-run start line"
VKEY=$(litellm_key) || die "could not make the LiteLLM user, team and key"
DRY="e2e-dry-$(rand)"
python3 send_conversation.py --format openai --session-id "$DRY" --key "$VKEY" --pause 1 --base-url "$LITELLM_URL"
wait_for $((CORVEIL_SESSION_IDLE_SECONDS + 60)) "the dry-run line" proxy_logged "dry run: would send conversation $DRY"
docker compose logs litellm 2>&1 | grep -F "dry run: would send conversation $DRY" | sed 's/^litellm-1  | //'
[ "$(received | json_get 'len(d)')" = 0 ] || die "the stand-in Corveil received an upload in dry-run mode"
echo "ok: logged the dry-run line and nothing was sent"

say "3. Live${REAL_CORVEIL:+ against $REAL_CORVEIL}"
CORVEIL_URL=${REAL_CORVEIL:-http://fake-corveil:8000} start_litellm
VKEY=$(litellm_key) || die "could not make the LiteLLM user, team and key"
ANTH="e2e-anthropic-$(rand)"
OAI="e2e-openai-$(rand)"
python3 send_conversation.py --format anthropic --stream --session-id "$ANTH" --key "$VKEY" --base-url "$LITELLM_URL"
python3 send_conversation.py --format openai --session-id "$OAI" --key "$VKEY" --base-url "$LITELLM_URL"
for sid in "$ANTH" "$OAI"; do
  wait_for $((CORVEIL_SESSION_IDLE_SECONDS + 90)) "the upload of $sid" proxy_logged "sent conversation $sid"
done
docker compose logs litellm 2>&1 | grep -E "sent conversation ($ANTH|$OAI)|Corveil (refused|answered)" | sed 's/^litellm-1  | //'

if [ -n "$REAL_CORVEIL" ]; then
  echo
  echo "PASS: corveil-litellm $VERSION, $REAL_CORVEIL accepted both conversations"
  exit 0
fi

received | python3 check_uploads.py --version "$VERSION" --email "$EMAIL" \
  --anthropic "$ANTH" --openai "$OAI" || die "an upload is not what Corveil expects"
echo
echo "PASS: corveil-litellm $VERSION, pip-installed in $(docker compose config --format json | json_get "d['services']['litellm']['build']['args']['LITELLM_IMAGE']")"
