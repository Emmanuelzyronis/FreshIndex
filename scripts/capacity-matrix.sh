#!/usr/bin/env bash
# Indexer capacity matrix (EMM-71).
#
# Runs the same standard workload at 1, 2, 4, and 8 indexer replicas, capturing
# a benchmark artifact after each run. Compare artifacts to find the saturation
# point and optimal replica count for the target SLO.
#
# Usage (on a Docker host, from the project root):
#   ./scripts/capacity-matrix.sh
#
# Environment overrides:
#   REPLICAS       Space-separated list, default "1 2 4 8"
#   LOADGEN_RATE   Events/sec per run, default 5
#   LOADGEN_DURATION_SECONDS  Default 60
#
# Results: artifacts/benchmarks/capacity-<timestamp>-<N>replicas.json
# Summary: artifacts/benchmarks/capacity-<timestamp>-summary.json

set -euo pipefail

REPLICAS="${REPLICAS:-1 2 4 8}"
RATE="${LOADGEN_RATE:-5}"
DURATION="${LOADGEN_DURATION_SECONDS:-60}"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARTIFACT_DIR="artifacts/benchmarks"
mkdir -p "$ARTIFACT_DIR"
SUMMARY="${ARTIFACT_DIR}/capacity-${TIMESTAMP}-summary.json"

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

echo "=== FreshIndex capacity matrix ===" >&2
echo "Replicas to test: $REPLICAS" >&2
echo "Workload: ${RATE} events/sec for ${DURATION}s" >&2

RESULTS=()

for N in $REPLICAS; do
  echo "" >&2
  echo "--- $N replica(s) ---" >&2

  # Teardown and fresh start to eliminate sample carryover
  docker compose down -v >/dev/null 2>&1 || true
  docker compose up -d >/dev/null 2>&1
  echo "  Waiting for stack..." >&2
  until curl_monitor /ready 2>/dev/null | grep -q '"status":"ok"'; do sleep 3; done

  # Scale indexer
  docker compose up -d --scale indexer="$N" indexer >/dev/null 2>&1
  sleep 3

  # Run workload
  echo "  Running workload..." >&2
  LOADGEN_RATE="$RATE" LOADGEN_DURATION_SECONDS="$DURATION" \
    docker compose --profile workload run --rm loadgen >/dev/null 2>&1 || true

  # Wait for monitor to drain remaining observations
  sleep 10

  # Capture artifact
  ARTIFACT=$(MONITOR_URL="$MONITOR_URL" MONITOR_OPS_TOKEN="${MONITOR_OPS_TOKEN:-}" \
    bash scripts/benchmark-capture.sh "capacity-${N}replicas" 2>/dev/null \
    | tail -1 | jq -c '.' 2>/dev/null || echo '{}')

  RESULTS+=("$ARTIFACT")
  echo "  Done. p99=$(echo "$ARTIFACT" | jq '.staleness.p99_staleness_ms // "n/a"') ms" >&2
done

# Write summary
printf '%s\n' "${RESULTS[@]}" | jq -s '{
  captured_at: (.[0].captured_at // "unknown"),
  matrix: [.[] | {
    label: .label,
    replicas: (.label | capture("(?<n>[0-9]+)replicas") | .n | tonumber),
    p50_ms: .staleness.p50_staleness_ms,
    p99_ms: .staleness.p99_staleness_ms,
    max_ms: .staleness.max_staleness_ms,
    violations: .staleness.active_violation_count,
    samples: .staleness.sample_count,
    products_doc_count: .meilisearch.products_doc_count,
    pg_product_count: .postgres.product_row_count
  }]
}' > "$SUMMARY"

echo "" >&2
echo "Summary: $SUMMARY" >&2
jq '.matrix[] | [.replicas, .p99_ms, .violations] | @tsv' -r "$SUMMARY" \
  | column -t -N "replicas,p99_ms,violations" >&2
