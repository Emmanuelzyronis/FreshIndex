# Build-Phase Limits and Operational Notes

This document records constraints discovered during the end-to-end Build-phase
verification. It complements the design-time guarantee in `guarantee.md` and
is intentionally limited to behavior observed in the current Docker Compose
deployment.

## Independent logical replication slots

The pipeline uses two independent PostgreSQL logical replication slots:

- `cdc_products_slot` is consumed by `cdc-reader` and feeds the Redis Stream.
- `staleness_monitor_slot` is consumed directly by `monitor` for authoritative
  `(commit_lsn, commit_ts_us)` observations.

Each slot advances only while its consumer is connected and acknowledging WAL.
If either service restarts, it resumes from that service's own slot position;
the slots do not need manual advancement for an ordinary restart. If a service
falls behind or remains down, PostgreSQL retains WAL for the lagging slot, so
disk usage can grow until the consumer catches up. Operators should monitor
replication-slot lag and remove a slot only when its consumer is permanently
retired and the retained WAL is no longer needed. Dropping or manually moving a
slot is an operational recovery action, not part of normal startup.

## Python import path requirement

The service images set `PYTHONPATH=/app` so imports from the shared `services`
package resolve consistently. Direct host execution must set the repository
root on `PYTHONPATH` or run commands from the repository root.

## Happy-path evidence only

Build verification used a real Postgres insert and an independent Meilisearch
read-probe. The monitor observed the event, reported missing-document probes
while indexing was incomplete, and recorded a real `staleness_ms` sample of
351.575 ms for `pk=13`. The rolling `/staleness` metrics reflected that sample
with `in_flight_count=0` and `violation_count=0`.

This confirms that one normal-path sample was below the 1000 ms ceiling. It
does not establish a p99. The repository now includes integration and SLO
verification entry points, but no genuine >1000 ms violation result has been
recorded, so detection within 500 ms remains an unverified deployment claim.

## Visibility marker retention

Per-event markers make rapid intermediate versions measurable after a product
has moved to a newer state. They are retained for seven days by default and are
therefore not an indefinite audit log. Retention must exceed the longest
expected monitor outage; a marker removed before the monitor observes it cannot
be reconstructed from Meilisearch alone.
