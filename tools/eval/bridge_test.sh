#!/bin/bash
# M0 acceptance test — proves the scanner stack and a cybench task server
# share an L3 path via `shared_net`.
#
# Exit codes:
#   0  both directions of the bridge work
#   1  setup failed (network, scanner stack, or task server didn't come up)
#   2  bridge failed (scanner-worker -> task or task -> callback-server)

set -eu

CYBENCH_DIR="${CYBENCH_DIR:-/home/enki/cybench}"
SCANNER_DIR="${SCANNER_DIR:-/home/enki/AI-active-scanner}"
TASK_DIR="${TASK_DIR:-$CYBENCH_DIR/benchmark/hkcert-ctf/CTF-Challenges/CTF-2022/05-motp}"
OVERRIDE="$CYBENCH_DIR/tools/eval/docker-compose.override.yml"

# MOTP task server: service `otp` on internal port 80, host port 8901
TASK_SERVICE_HOST="otp"
TASK_SERVICE_PORT="80"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[34m%s\033[0m\n' "$*"; }

cleanup() {
  local rc=$?
  blue "--- cleanup (task server only — scanner stack left running) ---"
  ( cd "$TASK_DIR" && bash stop_docker.sh ) >/dev/null 2>&1 || true
  exit $rc
}
trap cleanup EXIT

blue "--- 1. ensure shared_net exists ---"
docker network create shared_net >/dev/null 2>&1 || true
docker network inspect shared_net >/dev/null || { red "shared_net missing"; exit 1; }

blue "--- 2. bring up scanner stack joined to shared_net ---"
( cd "$SCANNER_DIR" && docker compose -f docker-compose.yml -f "$OVERRIDE" up -d ) \
  || { red "scanner stack failed to start"; exit 1; }

blue "--- 3. bring up MOTP task server ---"
( cd "$TASK_DIR" && bash start_docker.sh ) \
  || { red "MOTP start_docker.sh failed"; exit 1; }

blue "--- 4. wait up to 60s for both endpoints to be reachable from the host ---"
deadline=$(( $(date +%s) + 60 ))
while (( $(date +%s) < deadline )); do
  curl -sf http://localhost:8901/ >/dev/null 2>&1 && \
  curl -sf http://localhost:8880/api/health >/dev/null 2>&1 && break
  sleep 2
done

blue "--- 5. forward bridge: scanner-worker -> $TASK_SERVICE_HOST:$TASK_SERVICE_PORT ---"
worker_cid=$(docker ps --filter "name=scanner-worker" --format '{{.ID}}' | head -n1)
[[ -n "$worker_cid" ]] || { red "no scanner-worker container found"; exit 1; }
if docker exec "$worker_cid" sh -c "curl -sf -o /dev/null -w '%{http_code}' http://$TASK_SERVICE_HOST:$TASK_SERVICE_PORT/" \
  | grep -qE '^(200|302|301|403)$'; then
  green "  OK   scanner-worker can reach $TASK_SERVICE_HOST:$TASK_SERVICE_PORT"
else
  red   "  FAIL scanner-worker cannot reach $TASK_SERVICE_HOST:$TASK_SERVICE_PORT"
  exit 2
fi

blue "--- 6. reverse bridge: $TASK_SERVICE_HOST -> callback-server:8880 ---"
task_cid=$(docker ps --filter "name=otp" --format '{{.ID}}' | head -n1)
[[ -n "$task_cid" ]] || { red "no otp container found"; exit 1; }
if docker exec "$task_cid" sh -c "curl -sf -o /dev/null -w '%{http_code}' http://callback-server:8880/api/health" \
  | grep -q '^200$'; then
  green "  OK   $TASK_SERVICE_HOST can reach callback-server:8880"
else
  red   "  FAIL $TASK_SERVICE_HOST cannot reach callback-server:8880"
  exit 2
fi

green "--- M0 PASSED — bidirectional bridge over shared_net works ---"
