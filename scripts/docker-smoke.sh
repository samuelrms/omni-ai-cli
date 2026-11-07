#!/usr/bin/env bash
# Builds the image and checks what the Docker setup promises. Uses its own project and port, and removes them at the end.
set -euo pipefail
cd "$(dirname "$0")/.."

export OMNI_PORT="${OMNI_PORT:-18080}" SMART_AI_SERVER_KEY="" SMART_AI_SERVER_KEY_FILE=""
compose=(docker compose -p omni-smoke)
url="http://127.0.0.1:${OMNI_PORT}/v1/models"

if docker ps -a --format '{{.Names}}' | grep -qx omni; then
  echo "A container named omni already exists; remove it or run this elsewhere." >&2
  exit 1
fi
trap '"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true' EXIT

fail() { echo "FAIL: $1" >&2; "${compose[@]}" logs --no-color >&2 || true; exit 1; }
status() { curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $1" "$url" || true; }
wait_up() {
  for _ in $(seq 1 60); do
    [ "$(status probe)" = 401 ] && return 0
    sleep 1
  done
  fail 'the server did not answer'
}
generated_keys() { "${compose[@]}" logs --no-color omni | grep -oE 'omni-[A-Za-z0-9_-]{20,}' || true; }
saved_key() { docker exec omni cat /home/omni/.config/smart-ai/server_key.txt 2>/dev/null || true; }

"${compose[@]}" up -d --build --quiet-pull
wait_up
[ -z "$(generated_keys)" ] || fail 'the generated key went to the log'
key="$(saved_key)"
[ -n "$key" ] || fail 'no key saved on the first start'
[ "$(docker exec omni stat -c %a /home/omni/.config/smart-ai/server_key.txt)" = 600 ] || fail 'the saved key can be read by others'
[ "$(status "$key")" = 200 ] || fail 'the generated key was refused'
[ "$(status wrong)" = 401 ] || fail 'a wrong key was accepted'

[ "$(docker exec omni id -u)" != 0 ] || fail 'running as root'
docker exec omni sh -c 'for t in claude codex cursor-agent grok; do "$t" --version >/dev/null || exit 1; done' || fail 'a tool is missing'
docker exec omni sh -c '! touch /app/x 2>/dev/null' || fail 'the image filesystem is writable'
docker exec omni git --version >/dev/null || fail 'git is missing'
docker exec omni sh -c 'touch /workspace/.probe && rm /workspace/.probe' || fail 'the workspace is not writable'
docker port omni 8080 | grep -qx "127.0.0.1:${OMNI_PORT}" || fail 'published beyond 127.0.0.1'

# Recreated with the same volume: same key, and it is not printed again
"${compose[@]}" up -d --force-recreate --quiet-pull
wait_up
[ "$(status "$key")" = 200 ] || fail 'the key did not survive recreating the container'
[ -z "$(generated_keys)" ] || fail 'the key was printed again'

docker stop -t 15 omni >/dev/null
[ "$(docker inspect -f '{{.State.ExitCode}}' omni)" = 0 ] || fail 'unclean shutdown'

"${compose[@]}" down -v >/dev/null
export SMART_AI_SERVER_KEY="from-environment"
"${compose[@]}" up -d --quiet-pull
wait_up
[ "$(status from-environment)" = 200 ] || fail 'the key from the environment was refused'
[ -z "$(generated_keys)" ] || fail 'a key was generated despite the environment one'
[ -z "$(saved_key)" ] || fail 'a key was saved despite the environment one'

# A mode that writes needs the folder, and the compose file always gives it one
"${compose[@]}" down >/dev/null
OMNI_MODE=auto "${compose[@]}" up -d --quiet-pull
wait_up
# Read first: with pipefail, `grep -q` stops at the match and the cut-off `docker logs` counts as a failure
logs="$(docker logs omni 2>&1)"
grep -q 'modo auto' <<<"$logs" || fail 'the auto mode did not reach the server'

# The administration API: its own key, and a published port counts as external access
"${compose[@]}" down >/dev/null
export SMART_AI_ADMIN=1 SMART_AI_ADMIN_KEY="admin-from-environment"
"${compose[@]}" up -d --quiet-pull
wait_up
admin() { curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $1" "${@:2}" "http://127.0.0.1:${OMNI_PORT}/admin/health" || true; }
[ "$(admin "$SMART_AI_ADMIN_KEY")" = 403 ] || fail 'the admin API answered without HTTPS'
[ "$(admin "$SMART_AI_ADMIN_KEY" -H 'X-Forwarded-Proto: https')" = 200 ] || fail 'the admin API refused a request from the proxy'
[ "$(admin from-environment -H 'X-Forwarded-Proto: https')" = 401 ] || fail 'a /v1 key opened the admin API'

echo "Docker smoke test passed."
