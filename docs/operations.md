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

## Backup and restore

- PostgreSQL: use physical backups plus WAL archiving and test point-in-time restore.
- Redis: preserve the AOF and verify it can be loaded before relying on it.
- Meilisearch: schedule snapshots and test restoration of both indexes.

After restoring inconsistent points in time, stop writers, rebuild the products
and visibility indexes from an authoritative PostgreSQL/WAL position, recreate
consumer groups and slots deliberately, and then resume traffic. Never advance
a replication slot merely to silence lag.

## Upgrades

Test PostgreSQL logical-decoding compatibility, Redis `XAUTOCLAIM`, Meilisearch
task and document APIs, and Python dependency versions in a staging copy. Roll
one component at a time, keep backups, and rerun integration and SLO checks.
