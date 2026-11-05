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

"${compose[@]}" up -d --build --quiet-pull
wait_up
key="$(generated_keys | head -1)"
[ -n "$key" ] || fail 'no key in the first start log'
[ "$(status "$key")" = 200 ] || fail 'the generated key was refused'
[ "$(status wrong)" = 401 ] || fail 'a wrong key was accepted'

[ "$(docker exec omni id -u)" != 0 ] || fail 'running as root'
docker exec omni sh -c 'for t in claude codex cursor-agent grok; do "$t" --version >/dev/null || exit 1; done' || fail 'a tool is missing'
docker exec omni sh -c '! touch /app/x 2>/dev/null' || fail 'the image filesystem is writable'
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

# A mode that writes needs the folder, and the compose file always gives it one
"${compose[@]}" down >/dev/null
OMNI_MODE=auto "${compose[@]}" up -d --quiet-pull
wait_up
docker logs omni 2>&1 | grep -q 'modo auto' || fail 'the auto mode did not reach the server'

echo "Docker smoke test passed."
