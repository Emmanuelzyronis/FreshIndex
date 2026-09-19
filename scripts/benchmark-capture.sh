#!/usr/bin/env bash
# Capture a timestamped benchmark artifact for one FreshIndex run.
#
# Usage:
#   ./scripts/benchmark-capture.sh [label]
#
# Outputs: artifacts/benchmark-<timestamp>[-label].json
# Requires: running stack (docker compose up -d), curl, docker, jq.
#
# The artifact is self-contained: it records the environment, configuration,
# performance distribution, infrastructure state, and final counts so that
# runs can be compared without access to the running stack.

set -euo pipefail

LABEL="${1:-}"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARTIFACT_DIR="artifacts/benchmarks"
FILENAME="${ARTIFACT_DIR}/benchmark-${TIMESTAMP}${LABEL:+-${LABEL}}.json"

mkdir -p "$ARTIFACT_DIR"

echo "Capturing benchmark artifact → $FILENAME" >&2

# --- Environment metadata ---------------------------------------------------
MONITOR_URL="${MONITOR_URL:-http://localhost:8080}"
AUTH_HEADER=""
if [[ -n "${MONITOR_OPS_TOKEN:-}" ]]; then
  AUTH_HEADER="Authorization: Bearer ${MONITOR_OPS_TOKEN}"
fi

curl_monitor() {
  if [[ -n "$AUTH_HEADER" ]]; then
    curl -fsS --max-time 5 -H "$AUTH_HEADER" "${MONITOR_URL}${1}"
  else
    curl -fsS --max-time 5 "${MONITOR_URL}${1}"
  fi
}

STALENESS_JSON=$(curl_monitor /staleness 2>/dev/null || echo '{}')
READY_JSON=$(curl_monitor /ready 2>/dev/null || echo '{}')

# --- Docker image versions --------------------------------------------------
IMAGES_JSON=$(docker compose images --format json 2>/dev/null \
  | jq -sc 'map({service:.Service, image:.Image, tag:.Tag}) | sort_by(.service)' \
  2>/dev/null || echo '[]')

# --- Compose config summary (no secrets) ------------------------------------
COMPOSE_ENV=$(docker compose config --no-interpolate 2>/dev/null \
  | grep -E '^\s+(INDEXER_|LOADGEN_|CDC_|STALENESS_|SAMPLE_WINDOW|VISIBILITY_)' \
  | sed 's/^\s*//' | sed 's/\s*$//' | sort \
  | jq -Rn '[inputs | split(": ") | {(.[0]): .[1]}] | add // {}' \
  2>/dev/null || echo '{}')

# --- Redis stream and DLQ state ---------------------------------------------
REDIS_CMD="docker compose exec -T redis redis-cli"
if [[ -n "${REDIS_PASSWORD:-}" ]]; then
  REDIS_CMD="$REDIS_CMD -a ${REDIS_PASSWORD}"
fi

redis_run() { $REDIS_CMD "$@" 2>/dev/null || echo ""; }

STREAM_LEN=$(redis_run xlen cdc_events || echo 0)
DLQ_LEN=$(redis_run xlen cdc_events_dlq || echo 0)
PENDING_RAW=$(redis_run xpending cdc_events indexers - + 10 2>/dev/null || echo "")
PENDING_COUNT=$(echo "$PENDING_RAW" | grep -c '[0-9]' 2>/dev/null || echo 0)
RETRY_KEYS=$(redis_run hlen cdc_events:retries || echo 0)

# --- PostgreSQL replication slot lags ---------------------------------------
PG_CMD="docker compose exec -T postgres psql -U ${POSTGRES_USER:-postgres} -d ${POSTGRES_DB:-catalog} -t -c"

slot_lag() {
  $PG_CMD "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)
           FROM pg_replication_slots WHERE slot_name = '$1';" 2>/dev/null \
    | tr -d ' \n' || echo "null"
}

PRODUCTS_LAG=$(slot_lag cdc_products_slot)
MONITOR_LAG=$(slot_lag staleness_monitor_slot)

# --- Meilisearch task state -------------------------------------------------
MEILI_URL="${MEILI_URL:-http://localhost:7700}"
MEILI_AUTH=""
if [[ -n "${MEILI_MASTER_KEY:-}" ]]; then
  MEILI_AUTH="Authorization: Bearer ${MEILI_MASTER_KEY}"
fi

meili_get() {
  if [[ -n "$MEILI_AUTH" ]]; then
    curl -fsS --max-time 5 -H "$MEILI_AUTH" "${MEILI_URL}${1}" 2>/dev/null || echo '{}'
  else
    curl -fsS --max-time 5 "${MEILI_URL}${1}" 2>/dev/null || echo '{}'
  fi
}

MEILI_TASKS=$(meili_get /tasks?limit=5 | jq '{results: .results[-5:]}' 2>/dev/null || echo '{}')
PRODUCTS_COUNT=$(meili_get /indexes/products/stats | jq '.numberOfDocuments // 0' 2>/dev/null || echo 0)
VISIBILITY_COUNT=$(meili_get /indexes/cdc_visibility/stats | jq '.numberOfDocuments // 0' 2>/dev/null || echo 0)

# --- PostgreSQL product count -----------------------------------------------
PG_PRODUCT_COUNT=$($PG_CMD "SELECT COUNT(*) FROM products;" 2>/dev/null | tr -d ' \n' || echo 0)

# --- Assemble artifact ------------------------------------------------------
jq -n \
  --arg ts "$TIMESTAMP" \
  --arg label "$LABEL" \
  --argjson staleness "$STALENESS_JSON" \
  --argjson ready "$READY_JSON" \
  --argjson images "$IMAGES_JSON" \
  --argjson config "$COMPOSE_ENV" \
  --argjson meili_tasks "$MEILI_TASKS" \
  --argjson stream_len "${STREAM_LEN:-0}" \
  --argjson dlq_len "${DLQ_LEN:-0}" \
  --argjson pending_count "${PENDING_COUNT:-0}" \
  --argjson retry_keys "${RETRY_KEYS:-0}" \
  --arg products_slot_lag "$PRODUCTS_LAG" \
  --arg monitor_slot_lag "$MONITOR_LAG" \
  --argjson products_doc_count "${PRODUCTS_COUNT:-0}" \
  --argjson visibility_doc_count "${VISIBILITY_COUNT:-0}" \
  --argjson pg_product_count "${PG_PRODUCT_COUNT:-0}" \
  '{
    captured_at: $ts,
    label: $label,
    staleness: $staleness,
    ready: $ready,
    images: $images,
    config: $config,
    redis: {
      stream_len: $stream_len,
      dlq_len: $dlq_len,
      pending_count: $pending_count,
      retry_keys: $retry_keys
    },
    replication_slots: {
      cdc_products_slot_lag_bytes: $products_slot_lag,
      staleness_monitor_slot_lag_bytes: $monitor_slot_lag
    },
    meilisearch: {
      recent_tasks: $meili_tasks,
      products_doc_count: $products_doc_count,
      visibility_doc_count: $visibility_doc_count
    },
    postgres: {
      product_row_count: $pg_product_count
    }
  }' > "$FILENAME"

echo "Artifact written: $FILENAME" >&2
cat "$FILENAME"
