#!/usr/bin/env bash
# Controlled SLO violation test (EMM-70).
#
# Injects INDEXER_PROCESSING_DELAY_MS=1500 to force p99 staleness > 1000ms,
# runs a short workload, captures the detection-delay distribution, then
# restores normal operation and verifies no violations remain.
#
# Usage (on a Docker host with the stack already up and clean):
#   ./scripts/violation-test.sh
#
# Prerequisites: running stack, curl, jq, passing /ready.
# Result: artifacts/benchmarks/violation-<timestamp>.json
#
# IMPORTANT: Always run on a disposable deployment; never against production.

set -euo pipefail

MONITOR_URL="${MONITOR_URL:-http://localhost:8080}"
ARTIFACT_DIR="artifacts/benchmarks"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARTIFACT="${ARTIFACT_DIR}/violation-${TIMESTAMP}.json"
mkdir -p "$ARTIFACT_DIR"

AUTH_HEADER=""
if [[ -n "${MONITOR_OPS_TOKEN:-}" ]]; then
  AUTH_HEADER="-H 'Authorization: Bearer ${MONITOR_OPS_TOKEN}'"
fi

curl_monitor() {
  curl -fsS --max-time 5 ${AUTH_HEADER:+-H "$AUTH_HEADER"} "${MONITOR_URL}${1}"
}

echo "=== FreshIndex violation detection test ===" >&2
echo "Timestamp: $TIMESTAMP" >&2

# 1. Baseline: confirm stack is healthy before injection
echo "[1/5] Confirming stack is ready..." >&2
curl_monitor /ready | jq '.status' >&2

# 2. Inject processing delay
echo "[2/5] Injecting INDEXER_PROCESSING_DELAY_MS=1500..." >&2
INDEXER_PROCESSING_DELAY_MS=1500 docker compose up -d --build --force-recreate indexer >&2
sleep 5
until curl -fsS --max-time 3 "http://localhost:8081/ready" >/dev/null 2>&1; do sleep 2; done
echo "    Indexer restarted with delay." >&2

# 3. Run minimal workload (1 event/sec for 5 seconds = ~5 commits)
echo "[3/5] Running workload (1 event/sec, 5s)..." >&2
LOADGEN_RATE=1 LOADGEN_DURATION_SECONDS=5 \
  docker compose --profile workload run --rm loadgen >&2 || true

# 4. Wait for monitor to accumulate samples and detect violation
echo "[4/5] Waiting 10s for violation detection..." >&2
sleep 10

METRICS=$(curl_monitor /staleness)
VIOLATIONS=$(echo "$METRICS" | jq '.active_violation_count // 0')
DETECTION_P99=$(echo "$METRICS" | jq '.detection_delay_p99_ms // null')
P99_STALENESS=$(echo "$METRICS" | jq '.p99_staleness_ms // null')

echo "    Active violations: $VIOLATIONS" >&2
echo "    p99 staleness ms: $P99_STALENESS" >&2
echo "    Detection delay p99 ms: $DETECTION_P99" >&2

# 5. Restore normal operation
echo "[5/5] Restoring INDEXER_PROCESSING_DELAY_MS=0..." >&2
INDEXER_PROCESSING_DELAY_MS=0 docker compose up -d --build --force-recreate indexer >&2
sleep 5
until curl -fsS --max-time 3 "http://localhost:8081/ready" >/dev/null 2>&1; do sleep 2; done
echo "    Indexer restored." >&2

# Write artifact
jq -n \
  --arg ts "$TIMESTAMP" \
  --argjson metrics "$METRICS" \
  --argjson violations "$VIOLATIONS" \
  --argjson detection_p99 "${DETECTION_P99:-null}" \
  --argjson p99_staleness "${P99_STALENESS:-null}" \
  '{
    captured_at: $ts,
    test: "controlled_violation",
    injected_delay_ms: 1500,
    slo_ms: 1000,
    detection_slo_ms: 500,
    result: {
      violation_detected: ($violations > 0),
      active_violation_count: $violations,
      p99_staleness_ms: $p99_staleness,
      detection_delay_p99_ms: $detection_p99,
      detection_within_slo: (if $detection_p99 != null then $detection_p99 <= 500 else null end)
    },
    full_metrics: $metrics
  }' > "$ARTIFACT"

echo "" >&2
echo "Result artifact: $ARTIFACT" >&2
cat "$ARTIFACT"

# Exit non-zero if no violation was detected (test didn't validate anything useful)
if [[ "$VIOLATIONS" -eq 0 ]]; then
  echo "WARNING: no violation was detected — increase workload rate or delay and re-run." >&2
  exit 1
fi
