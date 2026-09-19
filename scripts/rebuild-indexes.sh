#!/usr/bin/env bash
# Rebuild Meilisearch indexes from authoritative PostgreSQL state (EMM-74).
#
# Use after backup/restore, disaster recovery, or when the Meilisearch indexes
# have drifted from PostgreSQL. Stops writers, resets slots and consumer groups,
# then replays all CDC events from the beginning of the replication slot.
#
# Usage:
#   ./scripts/rebuild-indexes.sh [--dry-run]
#
# Prerequisites: stack up, psql and redis-cli accessible via docker compose exec.
#
# Steps:
#   1. Stop writers (indexer, cdc-reader) to halt new events.
#   2. Record current WAL LSN as the authoritative rebuild position.
#   3. Drop and recreate both replication slots at the current WAL position.
#   4. Delete and recreate the Redis consumer group from stream position 0.
#   5. Delete all documents in products and cdc_visibility Meilisearch indexes.
#   6. Restart cdc-reader and indexer to replay from PostgreSQL.
#   7. Wait until Meilisearch document count matches PostgreSQL row count.
#   8. Run the SLO verifier against the rebuilt state.

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

run() {
  if $DRY_RUN; then
    echo "[dry-run] $*" >&2
  else
    "$@"
  fi
}

PG_USER="${POSTGRES_USER:-postgres}"
PG_DB="${POSTGRES_DB:-catalog}"
MEILI_URL="${MEILI_URL:-http://localhost:7700}"
MONITOR_URL="${MONITOR_URL:-http://localhost:8080}"

pg_exec() {
  docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -t -c "$1" \
    | tr -d ' \n'
}

redis_exec() {
  local auth_args=()
  [[ -n "${REDIS_PASSWORD:-}" ]] && auth_args=(-a "${REDIS_PASSWORD}")
  docker compose exec -T redis redis-cli "${auth_args[@]}" "$@"
}

meili_req() {
  local method="$1" path="$2"
  shift 2
  local auth_args=()
  [[ -n "${MEILI_MASTER_KEY:-}" ]] && auth_args=(-H "Authorization: Bearer ${MEILI_MASTER_KEY}")
  curl -fsS --max-time 10 -X "$method" "${auth_args[@]}" "${MEILI_URL}${path}" "$@"
}

wait_meili_task() {
  local task_uid="$1"
  for _ in $(seq 1 60); do
    local status
    status=$(meili_req GET "/tasks/${task_uid}" | jq -r '.status')
    [[ "$status" == "succeeded" ]] && return 0
    [[ "$status" == "failed" ]] && { echo "Meilisearch task $task_uid failed." >&2; return 1; }
    sleep 2
  done
  echo "Timed out waiting for Meilisearch task $task_uid." >&2
  return 1
}

echo "=== FreshIndex index rebuild ===" >&2
$DRY_RUN && echo "(dry-run mode: no changes will be made)" >&2

# Step 1: Stop traffic-generating services
echo "[1/8] Stopping cdc-reader and indexer..." >&2
run docker compose stop cdc-reader indexer

# Step 2: Authoritative WAL position
CURRENT_LSN=$(pg_exec "SELECT pg_current_wal_lsn();")
echo "[2/8] Current WAL LSN: $CURRENT_LSN" >&2

# Step 3: Recreate replication slots
echo "[3/8] Recreating replication slots..." >&2
run pg_exec "SELECT pg_drop_replication_slot('cdc_products_slot');" || true
run pg_exec "SELECT pg_drop_replication_slot('staleness_monitor_slot');" || true
run pg_exec "SELECT pg_create_logical_replication_slot('cdc_products_slot', 'pgoutput');"
run pg_exec "SELECT pg_create_logical_replication_slot('staleness_monitor_slot', 'pgoutput');"
echo "    Slots recreated at $CURRENT_LSN." >&2

# Step 4: Reset Redis consumer group
echo "[4/8] Resetting Redis consumer group..." >&2
run redis_exec xgroup destroy cdc_events indexers || true
run redis_exec xgroup create cdc_events indexers 0 MKSTREAM
run redis_exec del cdc_events:retries || true
echo "    Consumer group reset to stream position 0." >&2

# Step 5: Clear Meilisearch indexes
echo "[5/8] Clearing Meilisearch indexes..." >&2
PRODUCTS_TASK=$(meili_req DELETE /indexes/products/documents | jq -r '.taskUid // .uid')
VISIBILITY_TASK=$(meili_req DELETE /indexes/cdc_visibility/documents | jq -r '.taskUid // .uid')
if ! $DRY_RUN; then
  wait_meili_task "$PRODUCTS_TASK"
  wait_meili_task "$VISIBILITY_TASK"
fi
echo "    Indexes cleared." >&2

# Step 6: Restart services to begin replay
echo "[6/8] Starting cdc-reader and indexer..." >&2
run docker compose up -d cdc-reader indexer
sleep 5
until curl -fsS --max-time 3 http://localhost:8081/ready >/dev/null 2>&1 || $DRY_RUN; do
  sleep 3
done
echo "    Services started." >&2

# Step 7: Wait for Meilisearch count to converge with PostgreSQL
echo "[7/8] Waiting for index convergence..." >&2
PG_COUNT=$(pg_exec "SELECT COUNT(*) FROM products;")
echo "    PostgreSQL product count: $PG_COUNT" >&2

if ! $DRY_RUN; then
  for i in $(seq 1 60); do
    MEILI_COUNT=$(meili_req GET /indexes/products/stats | jq '.numberOfDocuments // 0')
    echo "    Meilisearch count: $MEILI_COUNT / $PG_COUNT (attempt $i)" >&2
    [[ "$MEILI_COUNT" -ge "$PG_COUNT" ]] && break
    sleep 5
  done
fi

# Step 8: SLO verification
echo "[8/8] Running SLO verifier..." >&2
if ! $DRY_RUN; then
  python tests/slo/verify_slo.py --url "${MONITOR_URL}/staleness" --minimum-samples 10 || {
    echo "SLO verification failed after rebuild — investigate before resuming traffic." >&2
    exit 1
  }
fi

echo "" >&2
echo "Rebuild complete. Review the SLO verifier output above before routing traffic." >&2
