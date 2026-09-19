# Operations Runbook

## Production rollout

1. Set unique PostgreSQL, Redis, and Meilisearch secrets in `.env` or the deployment's
   secret manager. Do not use the example values.
2. Provision persistent storage and verified backups for PostgreSQL, Redis AOF,
   and Meilisearch data.
3. Build and scan the four local images, then publish immutable image digests to
   the deployment registry.
4. Start PostgreSQL, Redis, and Meilisearch before the reader, indexer, and
   monitor. Wait for every readiness check.
5. Run the integration check, then a representative workload and the SLO
   verifier. Record the resulting sample count and latency distribution.
6. Route application searches through a query layer that always applies
   `_deleted = false` and never exposes `cdc_visibility`.

`INDEXER_PROCESSING_DELAY_MS` exists only for controlled violation testing. It
must be `0` in normal operation. A nonzero value intentionally delays every
event and will cause SLO breaches.

Tune `INDEXER_WORKERS` and `INDEXER_BATCH_SIZE` only after measuring Meilisearch
CPU, task backlog, Redis pending entries, and p99 staleness. Start with four
workers and a batch size of 50; scale replicas only when a single instance is
CPU- or queue-bound rather than when Meilisearch is saturated.

Docker Compose is the reproducible reference deployment. For a multi-host
production deployment, translate the same health, persistence, secret, and
shutdown contracts into the chosen orchestrator.

## Alerts

Alert on:

- `cdc_staleness_active_violations > 0` immediately.
- `cdc_staleness_p99_milliseconds > 1000` over a representative sample window.
- `cdc_staleness_detection_delay_p99_milliseconds > 500` after an injected test.
- Any service readiness failure for more than two health intervals.
- PostgreSQL replication-slot retained WAL approaching the disk budget.
- Redis `XPENDING` growth, dead-letter stream growth, or AOF persistence errors.
- Meilisearch task failures, disk pressure, or an unavailable health endpoint.

## Replication slots

`cdc_products_slot` feeds Redis and `staleness_monitor_slot` feeds the independent
monitor. A stopped consumer retains WAL in PostgreSQL. Monitor slot lag and
restore the consumer promptly. Drop a slot only after permanently retiring its
consumer and confirming the retained WAL is no longer required.

## Dead-letter recovery

Failed events move to `cdc_events_dlq` after `CDC_MAX_ATTEMPTS`. Inspect and fix
the underlying schema, data, or dependency problem before replay. Replay the
original `event` field to `cdc_events`; deterministic IDs and LSN checks make the
operation idempotent. Delete the DLQ entry only after its source message is
confirmed in `cdc_visibility`.

## Visibility retention

Markers default to seven days. The indexer removes up to 1000 expired markers
per cleanup interval. Set retention longer than the maximum expected monitor
outage and audit window. Watch index size if write volume exceeds the cleanup
rate; shorten the cleanup interval or run cleanup more frequently.

## Dead-letter replay procedure (EMM-69)

When events land in `cdc_events_dlq` they carry the original `event` payload,
the failure `error`, and the `failed_ts_us` timestamp. Replay is idempotent
because deterministic event IDs and LSN ordering prevent older events from
overwriting newer state.

```bash
# 1. Inspect DLQ entries (requires redis-cli with REDIS_PASSWORD set)
redis-cli -a "$REDIS_PASSWORD" xrange cdc_events_dlq - + COUNT 50

# 2. Identify the root cause from the `error` field.
#    Fix the schema, data quality, or dependency issue before replaying.

# 3. Replay one entry: extract `event` field and push it back to cdc_events.
#    Replace <msg-id> with the DLQ message ID from step 1.
EVENT=$(redis-cli -a "$REDIS_PASSWORD" xrange cdc_events_dlq <msg-id> <msg-id> \
  | awk 'NR==3{print}')
redis-cli -a "$REDIS_PASSWORD" xadd cdc_events "*" event "$EVENT"

# 4. Confirm the event_id appears in cdc_visibility (Meilisearch search):
#    GET http://localhost:7700/indexes/cdc_visibility/documents/<event_id>

# 5. Only after confirming visibility, delete the DLQ entry:
redis-cli -a "$REDIS_PASSWORD" xdel cdc_events_dlq <msg-id>
```

To replay all DLQ entries in one pass:
```bash
redis-cli -a "$REDIS_PASSWORD" xrange cdc_events_dlq - + | \
  awk '/^[0-9]/{id=$1} /event$/{getline; print id, $0}' | \
  while read -r msg_id payload; do
    redis-cli -a "$REDIS_PASSWORD" xadd cdc_events "*" event "$payload"
    echo "Replayed $msg_id"
  done
```

After bulk replay, verify final state:
- `XLEN cdc_events_dlq` should be 0 (delete entries only after confirmation).
- `XPENDING cdc_events indexers - + 100` should reach 0 within the claim window.
- Check SLO verifier: `python tests/slo/verify_slo.py --url http://localhost:8080/staleness`.

## Index rebuild from PostgreSQL (EMM-74)

Use `scripts/rebuild-indexes.sh` to rebuild both Meilisearch indexes from
authoritative PostgreSQL state after backup restore, slot drift, or data
inconsistency. The script stops writers, recreates slots and consumer groups,
clears indexes, replays from PostgreSQL, waits for convergence, and runs the
SLO verifier.

```bash
# Dry-run first to review all steps:
./scripts/rebuild-indexes.sh --dry-run

# Execute (on a Docker host with the stack up):
./scripts/rebuild-indexes.sh
```

## Backup and restore

- PostgreSQL: use physical backups plus WAL archiving and test point-in-time restore.
- Redis: preserve the AOF and verify it can be loaded before relying on it.
- Meilisearch: schedule snapshots and test restoration of both indexes.

After restoring inconsistent points in time, run `scripts/rebuild-indexes.sh`
to bring both Meilisearch indexes back to a consistent state. Never advance
a replication slot merely to silence lag.

## Monitoring endpoint security (EMM-73)

Set `MONITOR_OPS_TOKEN` in `.env` to restrict `/staleness` and `/metrics` to
bearer-token holders. `/health` and `/ready` remain unauthenticated for
orchestrator probes.

```bash
MONITOR_OPS_TOKEN=your-long-random-token  # add to .env
# Then access protected endpoints:
curl -H "Authorization: Bearer your-long-random-token" http://localhost:8080/staleness
```

For network-level restriction in production, place the monitor behind a reverse
proxy or firewall rule that limits `/staleness` and `/metrics` to trusted internal
CIDR ranges.

## Controlled violation detection test (EMM-70)

Run `scripts/violation-test.sh` on a disposable deployment to confirm that the
staleness monitor detects SLO violations within the ≤500 ms detection-delay target.

```bash
./scripts/violation-test.sh
# Artifact written to: artifacts/benchmarks/violation-<timestamp>.json
```

The script injects `INDEXER_PROCESSING_DELAY_MS=1500`, runs a minimal workload,
records the detection-delay p99, then restores normal operation. The artifact
includes `detection_within_slo: true/false` for the ≤500 ms objective.

## Capacity benchmarking (EMM-71)

Run `scripts/capacity-matrix.sh` to benchmark 1, 2, 4, and 8 indexer replicas.
Each run resets the stack to prevent sample carryover.

```bash
./scripts/capacity-matrix.sh
# Summary written to: artifacts/benchmarks/capacity-<timestamp>-summary.json
```

Publish the summary and identify the replica count where p99 approaches 1000 ms.

## Upgrades

Test PostgreSQL logical-decoding compatibility, Redis `XAUTOCLAIM`, Meilisearch
task and document APIs, and Python dependency versions in a staging copy. Roll
one component at a time, keep backups, and rerun integration and SLO checks.
