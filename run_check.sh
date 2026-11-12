#!/usr/bin/env bash
# Full reproduction run for one Open WebUI image tag.
#
#   ./run_check.sh v0.13.3 redis       # recreate Redis under load   (what happened on Nov 12)
#   ./run_check.sh v0.13.3 redis-oom   # Redis refuses writes (OOM)  (what happened on Nov 5)
#   ./run_check.sh v0.13.3 db-stall    # DB unreachable for 45 s     (tests the DB feedback loop alone)
#   ./run_check.sh dev redis
#
# Phases:  up -> users -> tabs -> 90 s baseline -> trigger -> 6 min observation -> verdict
# Everything is written to results/<tag>-<mode>-<timestamp>/
set -uo pipefail

TAG="${1:-v0.13.3}"
MODE="${2:-redis}"
USERS="${USERS:-5}"
TABS="${TABS:-4}"
BASELINE_S="${BASELINE_S:-90}"
OBSERVE_S="${OBSERVE_S:-360}"
PORT="${OWUI_PORT:-3000}"
URL="http://localhost:${PORT}"

export OWUI_TAG="$TAG" OWUI_PORT="$PORT"
cd "$(dirname "$0")"
OUT="results/${TAG//\//_}-${MODE}-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/run.log"; }

count_app() {  # count_app <since> <pattern>
  docker compose logs --no-log-prefix --since "$1" open-webui 2>/dev/null | grep -c -- "$2"
}
pg_conns() {
  docker compose exec -T postgres psql -U owui -d owui -tAc \
    "select count(*) from pg_stat_activity where datname='owui'" 2>/dev/null || echo "?"
}
snapshot() {  # one observation line
  local since="$1"
  printf "%s pool_timeouts=%s session_not_found=%s cannot_publish=%s pg_conns=%s\n" \
    "$(date +%H:%M:%S)" \
    "$(count_app "$since" 'QueuePool limit')" \
    "$(count_app "$since" 'Session not found')" \
    "$(count_app "$since" 'Cannot publish to redis')" \
    "$(pg_conns)" | tee -a "$OUT/observe.log"
}

# ---------- 1. stack ----------
log "Starting stack with image tag '$TAG' (pool size ${POOL_SIZE:-2}, overflow ${POOL_OVERFLOW:-0}, timeout ${POOL_TIMEOUT:-5}s)"
docker compose down -v --remove-orphans >/dev/null 2>&1
docker compose pull -q open-webui || { log "image pull failed for tag $TAG"; exit 1; }
docker compose up -d || exit 1
log "Waiting for $URL/health ..."
for _ in $(seq 1 90); do curl -fsS "$URL/health" >/dev/null 2>&1 && break; sleep 3; done
curl -fsS "$URL/health" >/dev/null 2>&1 || { log "Open WebUI did not become healthy"; docker compose logs open-webui | tail -50; exit 1; }
VERSION=$(curl -fsS "$URL/api/version" 2>/dev/null)
log "Open WebUI is up: $VERSION"
echo "$VERSION" > "$OUT/version.json"
IMG="ghcr.io/open-webui/open-webui:$TAG"
REV=$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMG" 2>/dev/null)
DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' "$IMG" 2>/dev/null)
log "Image: $IMG  revision=${REV:-?}  digest=${DIGEST:-?}"
printf "image=%s\nrevision=%s\ndigest=%s\n" "$IMG" "$REV" "$DIGEST" > "$OUT/image.txt"

# ---------- 2. users & tabs ----------
python3 setup_users.py --url "$URL" --users "$USERS" --out "$OUT/tokens.json" | tee -a "$OUT/run.log" || exit 1
log "Opening $((USERS*TABS)) tabs ..."
python3 open_tabs.py --url "$URL" --tokens "$OUT/tokens.json" --users "$USERS" --tabs-per-user "$TABS" \
  --duration $((BASELINE_S + OBSERVE_S + 120)) --csv "$OUT/tabs_stats.csv" > "$OUT/tabs.log" 2>&1 &
TABS_PID=$!
sleep 35   # page loads + 20 s warm-up inside open_tabs.py
kill -0 "$TABS_PID" 2>/dev/null || { log "open_tabs.py exited early:"; tail -20 "$OUT/tabs.log"; exit 1; }

# ---------- 3. baseline ----------
log "Baseline for ${BASELINE_S}s (expect ~0 pool timeouts and ~0 ws_new)"
T_BASE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sleep "$BASELINE_S"
snapshot "$T_BASE"
BASE_TIMEOUTS=$(count_app "$T_BASE" 'QueuePool limit')
if [ "$BASE_TIMEOUTS" -gt 0 ]; then
  log "WARNING: $BASE_TIMEOUTS pool timeouts already in baseline. Re-run with POOL_SIZE=5 so the baseline is clean."
fi

# ---------- 4. trigger ----------
T_TRIG=$(date -u +%Y-%m-%dT%H:%M:%SZ)
case "$MODE" in
  redis)
    log "TRIGGER: recreating the Redis container (data lost, like a recreated pod)"
    docker compose rm -sf redis >/dev/null && docker compose up -d redis >/dev/null
    ;;
  redis-oom)
    log "TRIGGER: Redis refuses writes for 60 s (maxmemory=1mb, noeviction)"
    docker compose exec -T redis redis-cli CONFIG SET maxmemory-policy noeviction >/dev/null
    docker compose exec -T redis redis-cli CONFIG SET maxmemory 1mb >/dev/null
    sleep 60
    docker compose exec -T redis redis-cli CONFIG SET maxmemory 0 >/dev/null
    log "Redis writes allowed again"
    ;;
  db-stall)
    log "TRIGGER: pausing PostgreSQL for 45 s (every DB call hangs, then pool timeouts)"
    docker compose pause postgres
    sleep 45
    docker compose unpause postgres
    log "PostgreSQL unpaused"
    ;;
  *) log "unknown mode '$MODE'"; kill "$TABS_PID" 2>/dev/null; exit 1 ;;
esac

# ---------- 5. observe ----------
log "Observing for ${OBSERVE_S}s. Trigger is over; a healthy system should settle within ~1 min."
END=$((SECONDS + OBSERVE_S))
LAST_T=$(date -u +%Y-%m-%dT%H:%M:%SZ)
LATE_TIMEOUTS=0; LATE_WINDOWS=0
while [ $SECONDS -lt $END ]; do
  sleep 15
  N=$(count_app "$LAST_T" 'QueuePool limit')
  snapshot "$LAST_T"
  # "late" = more than 2 min after the trigger ended
  if [ $((END - SECONDS)) -lt $((OBSERVE_S - 120)) ]; then
    LATE_WINDOWS=$((LATE_WINDOWS + 1))
    [ "$N" -gt 0 ] && LATE_TIMEOUTS=$((LATE_TIMEOUTS + 1))
  fi
  LAST_T=$(date -u +%Y-%m-%dT%H:%M:%SZ)
done

# ---------- 6. collect & verdict ----------
kill "$TABS_PID" 2>/dev/null; wait "$TABS_PID" 2>/dev/null
docker compose logs --no-log-prefix --timestamps open-webui > "$OUT/open-webui.log" 2>&1
docker compose logs --no-log-prefix --timestamps redis > "$OUT/redis.log" 2>&1
TOTAL_TIMEOUTS=$(grep -c 'QueuePool limit' "$OUT/open-webui.log")
TOTAL_SNF=$(grep -c 'Session not found' "$OUT/open-webui.log")
WS_AFTER=$(awk -F, -v t="$(date -d "$T_TRIG" +%H:%M:%S 2>/dev/null || echo 00:00:00)" 'NR>1 && $1>=t {s+=$2} END{print s+0}' "$OUT/tabs_stats.csv")

{
  echo
  echo "================ RESULT ($TAG / $MODE) ================"
  echo "version:                  $VERSION"
  echo "image:                    $IMG  revision=${REV:-?}"
  echo "tabs:                     $((USERS*TABS))  (pool size ${POOL_SIZE:-2}, overflow ${POOL_OVERFLOW:-0})"
  echo "QueuePool timeouts total: $TOTAL_TIMEOUTS   (baseline: $BASE_TIMEOUTS)"
  echo "'Session not found':      $TOTAL_SNF"
  echo "websocket reconnects after trigger (all tabs): $WS_AFTER"
  echo "observation windows >2 min after trigger with pool timeouts: $LATE_TIMEOUTS / $LATE_WINDOWS"
  if [ "$LATE_WINDOWS" -gt 0 ] && [ "$LATE_TIMEOUTS" -ge $((LATE_WINDOWS / 2)) ] && [ "$WS_AFTER" -gt $((USERS*TABS*3)) ]; then
    echo "VERDICT: REPRODUCED - pool timeouts and reconnects continued long after the trigger ended (self-sustaining loop)."
  elif [ "$TOTAL_TIMEOUTS" -gt "$BASE_TIMEOUTS" ]; then
    echo "VERDICT: PARTIAL - the trigger caused pool timeouts, but the system recovered within ~2 min."
  else
    echo "VERDICT: NOT REPRODUCED - no pool timeouts caused by the trigger."
  fi
  echo "artifacts: $OUT/  (open-webui.log, redis.log, tabs_stats.csv, observe.log)"
} | tee -a "$OUT/run.log"

log "Stack left running for inspection. Stop with: docker compose down -v"
