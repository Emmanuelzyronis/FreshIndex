# FreshIndex

A reference change-data-capture pipeline that reads committed PostgreSQL row
changes from logical WAL, delivers them through Redis Streams, applies ordered
versions to Meilisearch, and independently measures commit-to-search
visibility against a 1000 ms p99 staleness objective.

This repository is infrastructure, not an application UI. It contains the
database schema, logical-replication reader, Redis-backed indexer, independent
staleness monitor, Docker Compose deployment, workload generator, and
verification tests needed to exercise the pipeline end to end.

## Validation status

The pipeline was validated on 2026-09-01 with Docker Compose
`2.40.3+ds1-0ubuntu1~24.04.1`, PostgreSQL 16.4, Redis 7.4.0, Meilisearch 1.12.0,
and the repository's default optimized indexer configuration.

The corrected steady-rate benchmark generated 300 independently committed
mutations at 5 mutations per second for 60 seconds:

| Measurement | Result |
| --- | ---: |
| Samples | 300 |
| Throughput | 5.0 committed mutations/second |
| p50 staleness | 151.994 ms |
| p95 staleness | 207.060 ms |
| p99 staleness | 221.839 ms |
| Maximum staleness | 225.274 ms |
| Historical violations | 0 |
| Active violations at completion | 0 |
| Redis pending messages | 0 |
| Dead-letter messages | 0 |
| `cdc_products_slot` WAL lag | 0 bytes |
| `staleness_monitor_slot` WAL lag | 0 bytes |
| Final PostgreSQL live row count | 200 |
| Final Meilisearch `products` document count | 247, including tombstones and retained prior-run data |

The formal verifier passed the unchanged 1000 ms p99 threshold. All 300
observations had distinct commit LSNs and distinct commit timestamps. The
integration test also confirmed that an independently committed insert,
update, and delete were all measured with no active violation.

These measurements validate this specific Docker Compose deployment and
workload. They are evidence, not a universal latency guarantee for different
hardware, data volumes, query load, or deployment topologies.

## Problem statement

Search indexes are asynchronous materialized views. A database transaction can
commit successfully while a search client continues to see an older version,
or no version, until the indexing pipeline catches up. Basic delivery metrics
do not answer the important question: how long after the authoritative commit
did the corresponding change become independently observable in search?

This project addresses that question with three properties:

1. PostgreSQL commit metadata is the source of truth for event time and order.
2. The indexing path is at-least-once but rejects older or duplicate document
   versions by commit LSN.
3. A separate logical-replication consumer measures visibility by reading an
   immutable event marker from Meilisearch after the product mutation succeeds.

## Goals

- Capture committed `products` inserts, updates, and deletes from stock
  PostgreSQL `pgoutput` logical replication.
- Preserve authoritative commit LSN and commit timestamp metadata.
- Deliver events durably through Redis Streams with consumer groups, pending
  recovery, retry accounting, and a dead-letter stream.
- Apply document versions to Meilisearch without allowing an older retry to
  overwrite or resurrect newer state.
- Represent deletes as versioned tombstones.
- Measure commit-to-search visibility independently of the reader and indexer.
- Report a rolling latency distribution and active SLO violations through JSON
  and Prometheus-compatible endpoints.
- Provide reproducible unit, integration, workload, SLO, and controlled
  violation checks.

## Non-goals

- General-purpose CDC for arbitrary schemas or all PostgreSQL data types.
- Exactly-once transport. Redis delivery is intentionally at least once.
- A public search API or application query service.
- Cross-region, multi-host, Kubernetes, or managed-service manifests.
- An indefinite event audit log. Visibility markers have finite retention.
- Automatic repair of poison events after they enter the dead-letter stream.
- Benchmark claims beyond the workload and environment actually tested.
- The future applications listed later; they do not exist yet.

## Core guarantees and semantics

### Staleness SLO

For committed writes to `public.products`, the stated objective is:

```text
p99(commit-to-confirmed-search-visibility) <= 1000 ms
```

If an event remains invisible after 1000 ms, the monitor records a violation.
Controlled violation testing additionally checks that p99 detection delay is at
most 500 ms.

### Meaning of staleness

For event `E`:

```text
staleness(E) = first_successful_marker_probe_ts(E) - postgres_commit_ts(E)
```

The start is PostgreSQL's transaction commit timestamp decoded from the
`pgoutput` commit message. It is not the reader receipt time, Redis publish
time, indexer dequeue time, or Meilisearch task-submission time.

The end is the monitor's first successful read of the event's immutable marker
from the `cdc_visibility` Meilisearch index. The indexer writes that marker only
after the product mutation task has completed. Marker visibility is therefore
a conservative upper bound on product visibility.

### Delivery, ordering, and idempotency

- PostgreSQL commit LSN defines version order.
- Redis Streams provides at-least-once delivery.
- Event IDs are deterministic SHA-256 hashes of database, schema, table,
  commit LSN, transaction ID, and transaction-local row-change sequence.
- The applied LSN is stored in every Meilisearch product document.
- Incoming events with an LSN less than or equal to the stored LSN are marked
  `superseded` and cannot overwrite newer state.
- Redis locks serialize concurrent work per product ID across workers or
  replicas.
- A marker is written for applied and superseded events so each committed event
  remains measurable.

This makes WAL replay and Redis redelivery idempotent at the document-version
boundary, but it does not turn Redis transport into exactly-once delivery.

### Delete handling

Deletes are versioned tombstones, not physical removals:

```json
{"id": 7, "_lsn": "0/20", "_deleted": true}
```

Retaining `_lsn` prevents an older insert or update from resurrecting a deleted
document. Application searches must filter tombstones:

```bash
curl -sS -X POST http://localhost:7700/indexes/products/search \
  -H "Authorization: Bearer $MEILI_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"q":"","filter":"_deleted = false"}'
```

## Architecture

```mermaid
flowchart LR
    W[Writer or load generator] -->|committed SQL transaction| PG[(PostgreSQL 16)]
    PG -->|cdc_products_slot / pgoutput| R[CDC reader]
    R -->|XADD cdc_events| RS[(Redis Streams)]
    RS -->|XREADGROUP / XAUTOCLAIM| I[Indexer workers]
    I -->|versioned mutation| P[(Meilisearch products)]
    I -->|immutable marker| V[(Meilisearch cdc_visibility)]
    PG -->|staleness_monitor_slot / pgoutput| M[Staleness monitor]
    M -->|GET marker by event_id| V
    M --> H[/staleness /health /metrics]
```

PostgreSQL is read through two independent logical replication slots. The
monitor does not infer timing from Redis or trust an indexer-reported latency;
it independently observes the source commit and probes the search system.

### End-to-end sequence

```mermaid
sequenceDiagram
    participant Writer
    participant PostgreSQL
    participant Reader as CDC reader
    participant Redis
    participant Indexer
    participant Products as Meilisearch products
    participant Visibility as Meilisearch cdc_visibility
    participant Monitor

    Writer->>PostgreSQL: INSERT, UPDATE, or DELETE
    PostgreSQL-->>Writer: Commit
    par Indexing path
        PostgreSQL-->>Reader: pgoutput on cdc_products_slot
        Reader->>Redis: XADD deterministic event envelope
        Reader->>PostgreSQL: Replication feedback after commit
        Redis-->>Indexer: XREADGROUP batch
        Indexer->>Indexer: Lock document and compare LSN
        Indexer->>Products: Apply version or tombstone
        Products-->>Indexer: Product task completed
        Indexer->>Visibility: Add event marker
        Visibility-->>Indexer: Marker task completed
        Indexer->>Redis: XACK
    and Measurement path
        PostgreSQL-->>Monitor: Same commit on independent slot
        loop Every STALENESS_POLL_SECONDS
            Monitor->>Visibility: Get marker by event_id
        end
        Visibility-->>Monitor: Matching event_id and commit_lsn
        Monitor->>Monitor: Record visible_ts - commit_ts
        Monitor->>PostgreSQL: Advance slot after commit resolves
    end
```

## Components

### PostgreSQL

Compose runs `postgres:16.4` with `wal_level=logical`,
`track_commit_timestamp=on`, `max_replication_slots=10`, and
`max_wal_senders=10`.

Initialization creates `public.products`, publication `cdc_pub`, logical slot
`cdc_products_slot`, replication role `cdc_reader`, and restricted writer role
`catalog_writer`. The monitor creates `staleness_monitor_slot` if absent.

The product schema contains `id`, unique `sku`, `name`, `description`,
`category`, non-negative `price_cents`, `in_stock`, and `updated_at`. Replica
identity is `DEFAULT`, so deletes reliably carry the primary-key identity but
do not promise a complete old-row image.

### Logical replication decoder

`services/shared/pgoutput_decoder.py` implements the subset of PostgreSQL
`pgoutput` needed by the schema: begin/commit, relation metadata, insert,
update, delete, and tuple decoding. Origin, type, and truncate protocol messages
are ignored. Row changes are buffered until commit, then emitted with the
authoritative commit LSN and timestamp.

The event envelope contains:

| Field | Meaning |
| --- | --- |
| `event_id` | Deterministic SHA-256 identity |
| `op` | `c`, `u`, or `d` |
| `source` | Database, schema, and table |
| `pk` | Primary key, currently `id` |
| `after` | New row for insert/update, otherwise `null` |
| `before` | Old/key tuple for delete, otherwise `null` |
| `commit_lsn` | Commit position and document version token |
| `commit_ts_us` | Commit timestamp in epoch microseconds |
| `xid` | PostgreSQL transaction ID |
| `captured_ts_us` | Reader-side decode time |
| `published_ts_us` | Reader-side Redis publication time |

PostgreSQL may mark unchanged TOAST-backed update fields unavailable. The
single-event update path uses Meilisearch partial updates; the batch path merges
decoded fields into the current indexed document.

### CDC reader

`services/replication-consumer/reader.py` consumes `cdc_products_slot`, decodes
committed transactions, appends JSON envelopes to `cdc_events`, and sends
replication feedback after commit messages. Connection failures retry with
exponential backoff from 1 to 30 seconds.

Its internal `/ready` and `/health` endpoints on port 8082 report readiness,
events published, and the last error. The port is not host-published by default.

### Redis

Compose runs Redis 7.4.0 with password authentication, AOF persistence, and
`appendfsync everysec`.

Redis stores the `cdc_events` stream, consumer group `indexers`, pending
entries, retry hash `cdc_events:retries`, dead-letter stream `cdc_events_dlq`,
per-document locks, and the visibility-cleanup lock. The indexer uses
`XAUTOCLAIM` to recover messages idle for `CDC_CLAIM_IDLE_MS`. Stream entries
are not automatically trimmed.

### Indexer

`services/indexer/main.py` initializes:

- `products`, primary key `id`, filterable `_lsn` and `_deleted`;
- `cdc_visibility`, primary key `event_id`, filterable `commit_ts_us`.

The default uses four worker threads and batches up to 50 Redis events. For a
batch of distinct document IDs it acquires locks in sorted order, compares
LSNs, submits one product task, waits for it, submits one marker task, waits for
it, and acknowledges each message. Batches containing duplicate document IDs
fall back to sequential single-message processing.

Logs include stream queue latency, product and marker task waits, batch size,
lock wait, and processing duration. The internal port 8081 readiness endpoint
also returns worker and batch counters.

### Meilisearch

Compose runs Meilisearch 1.12.0 in production mode with persistent storage and
a required master key. `products` is the searchable materialized view;
`cdc_visibility` is internal and must not be exposed to application clients.

Markers contain `event_id`, `document_id`, operation, commit LSN, commit
timestamp, and result (`applied` or `superseded`). They default to seven-day
retention. Retention must exceed the longest expected monitor outage.

### Staleness monitor

`services/staleness-monitor/main.py` is independent of Redis and the reader. It
consumes `staleness_monitor_slot`, tracks each event in memory, and polls
`cdc_visibility` by event ID. Both event ID and commit LSN must match. It
records staleness on the first successful probe and records a violation once an
unresolved event exceeds the SLO.

Replication feedback advances only after every event in a commit has resolved.
The monitor rejects commit timestamps over 60 seconds in the future or over 24
hours in the past. The default rolling sample capacity is 10,000.

### Workload generator

`load-generator/main.py` produces a deterministic mixture of approximately 60%
inserts, 30% updates, and 10% deletes. It is paced by `LOADGEN_RATE` and seeded
by `LOADGEN_SEED`.

The psycopg2 connection uses `autocommit=True` without being used as a
connection context manager. Each mutation is independently committed and gets
its own commit LSN and timestamp. The connection closes explicitly in
`finally`. This detail is essential to benchmark validity.

## Failure and recovery behavior

Reader and monitor failures mark their services unready and reconnect with
bounded exponential backoff. Their logical slots retain WAL while disconnected.

When single-message indexing fails, the indexer increments a retry counter and
leaves the message pending for reclaim. At `CDC_MAX_ATTEMPTS` (default 5), it
writes the original event and failure metadata to `cdc_events_dlq`, then
acknowledges the source message. Batch-level failures are logged and remain
pending; reclaim later uses the single-event path.

DLQ replay is manual: fix the cause, replay the original `event` field to
`cdc_events`, and remove the DLQ entry only after confirming its visibility
marker. Deterministic IDs and LSN checks protect newer document state.

## Observability and endpoints

| Service | Endpoint | Published | Purpose |
| --- | --- | --- | --- |
| Monitor | `GET /staleness` on 8080 | Yes | JSON latency and violation metrics |
| Monitor | `GET /metrics` on 8080 | Yes | Prometheus text exposition |
| Monitor | `GET /health` or `/ready` | Yes | Readiness plus active-SLO health |
| Reader | `GET /health` or `/ready` on 8082 | No | Replication readiness and count |
| Indexer | `GET /health` or `/ready` on 8081 | No | Readiness and worker counters |
| Meilisearch | `GET /health` on 7700 | Yes | Search service availability |

`/staleness` returns sample count, p50/p95/p99/max, in-flight count and oldest
age, historical and active violations, p99 detection delay, active violation
duration, and replication/probe readiness. Percentiles use nearest rank with a
ceiling rank.

The monitor returns HTTP 200 with `status: "ok"` only when replication and
probing are ready and no active violation exists. It returns HTTP 503 with
`STALE` for an active violation or `unavailable` for dependency/readiness
failure. Historical violations do not keep health failed after resolution.

Prometheus metrics:

| Metric | Meaning |
| --- | --- |
| `cdc_staleness_samples_total` | Samples retained in the rolling window |
| `cdc_staleness_p50_milliseconds` | Rolling p50 |
| `cdc_staleness_p95_milliseconds` | Rolling p95 |
| `cdc_staleness_p99_milliseconds` | Rolling p99 |
| `cdc_staleness_max_milliseconds` | Maximum retained staleness |
| `cdc_staleness_in_flight` | Events awaiting markers |
| `cdc_staleness_oldest_in_flight_milliseconds` | Oldest unresolved age |
| `cdc_staleness_violations_total` | Violations since monitor start |
| `cdc_staleness_active_violations` | Currently unresolved violations |
| `cdc_staleness_detection_delay_p99_milliseconds` | p99 delay beyond SLO at detection |
| `cdc_staleness_violation_duration_seconds` | Longest active violation duration |

Missing numeric values are exposed as `NaN`.

## Docker Compose deployment

The six persistent services are PostgreSQL, Redis, Meilisearch, `cdc-reader`,
`indexer`, and `monitor`. `loadgen` is enabled only through the `workload`
profile. Named volumes persist PostgreSQL, Redis, and Meilisearch data.

Python services run as non-root users with read-only root filesystems, `/tmp`
tmpfs, init, `no-new-privileges`, and restart policies. Host-published ports
bind only to `127.0.0.1`.

## Repository structure

```text
.
├── docker-compose.yml
├── .env.example
├── docs/
├── infra/postgres-init/
├── services/
│   ├── replication-consumer/
│   ├── indexer/
│   ├── staleness-monitor/
│   └── shared/pgoutput_decoder.py
├── load-generator/
└── tests/{unit,integration,slo}/
```

## Prerequisites and setup

- Docker Engine
- Docker Compose v2, invoked as `docker compose`
- `curl`
- Python 3.12 for host-side unit and SLO commands
- `psycopg2-binary` for host-side integration discovery/execution

The validated host used Compose `2.40.3+ds1-0ubuntu1~24.04.1`. Legacy
`docker-compose` v1 does not support the repository's native top-level `name`
field and is not supported.

```bash
cp .env.example .env
# Replace every example password and key with distinct strong values.
docker compose version
docker compose config --quiet
```

`.env` is ignored by Git. Docker Compose loads it automatically. Before running
host-side commands that reference its variables, export it into the current
shell:

```bash
set -a
. ./.env
set +a
```

## Configuration

| Variable | Default/example | Purpose |
| --- | --- | --- |
| `POSTGRES_USER` | `postgres` | Bootstrap administrator |
| `POSTGRES_PASSWORD` | required | Administrator password |
| `POSTGRES_DB` | `catalog` | Database name |
| `CDC_DB_USER` | `cdc_reader` | Replication login |
| `CDC_DB_PASSWORD` | required | Replication password |
| `WRITER_DB_USER` | `catalog_writer` | Restricted writer |
| `WRITER_DB_PASSWORD` | required | Writer password |
| `REDIS_PASSWORD` | required | Redis password |
| `MEILI_MASTER_KEY` | required | Meilisearch master key |
| `POSTGRES_PORT` | `5432` | Loopback host port |
| `REDIS_PORT` | `6379` | Loopback host port |
| `MEILI_PORT` | `7700` | Loopback host port |
| `MONITOR_PORT` | `8080` | Monitor host port |
| `CDC_MAX_ATTEMPTS` | `5` | Attempts before DLQ |
| `CDC_CLAIM_IDLE_MS` | `30000` | Pending reclaim age |
| `CDC_KEY_LOCK_SECONDS` | `120` | Document lock timeout |
| `INDEXER_PROCESSING_DELAY_MS` | `0` | Controlled-test delay only |
| `INDEXER_BATCH_SIZE` | `50` | Maximum read batch |
| `INDEXER_WORKERS` | `4` | Worker threads |
| `VISIBILITY_MARKER_RETENTION_SECONDS` | `604800` | Marker retention |
| `VISIBILITY_MARKER_CLEANUP_SECONDS` | `3600` | Cleanup interval |
| `STALENESS_SLO_MS` | `1000` | Violation threshold |
| `STALENESS_POLL_SECONDS` | `0.12` | Probe/ack interval |
| `SAMPLE_WINDOW` | `10000` | Rolling sample capacity |
| `LOADGEN_RATE` | `5` | Mutations per second |
| `LOADGEN_DURATION_SECONDS` | `60` | Duration; zero runs until stopped |
| `LOADGEN_SEED` | `2026` | Deterministic seed |

Compose supplies internal names including `cdc_products_slot`,
`staleness_monitor_slot`, `cdc_pub`, `cdc_events`, `cdc_events_dlq`, `products`,
and `cdc_visibility`.

## Starting and stopping

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://localhost:${MONITOR_PORT:-8080}/health
curl -fsS http://localhost:${MONITOR_PORT:-8080}/staleness
```

Structured logs:

```bash
docker compose logs --no-color cdc-reader indexer monitor
```

Stop while preserving volumes:

```bash
docker compose down
```

Destructively remove all persisted data:

```bash
docker compose down -v
```

## Tests

Unit tests:

```bash
python -m unittest tests.unit.test_event_pipeline tests.unit.test_load_generator -v
```

They cover event identity, LSN ordering, tombstones, partial updates, batching,
marker matching, percentiles, dead letters, and workload transaction semantics.

Integration test with three explicit commits:

```bash
set -a
. ./.env
set +a
RUN_INTEGRATION=1 \
DATABASE_URL="postgresql://$WRITER_DB_USER:$WRITER_DB_PASSWORD@localhost:${POSTGRES_PORT:-5432}/$POSTGRES_DB" \
MONITOR_URL="http://localhost:${MONITOR_PORT:-8080}/staleness" \
python -m unittest tests.integration.test_pipeline -v
```

Full discovery:

```bash
python -m unittest discover -v
```

The integration module imports psycopg2 before applying its skip decorator, so
host discovery requires `psycopg2-binary`. The test can instead run inside the
reader image:

```bash
set -a
. ./.env
set +a
docker run --rm \
  --network freshindex_pipeline \
  -v "$PWD:/repo:ro" -w /repo -e PYTHONPATH=/repo \
  -e RUN_INTEGRATION=1 \
  -e DATABASE_URL="postgresql://$WRITER_DB_USER:$WRITER_DB_PASSWORD@postgres:5432/$POSTGRES_DB" \
  -e MONITOR_URL=http://monitor:8080/staleness \
  freshindex-cdc-reader \
  python -m unittest tests.integration.test_pipeline -v
```

## Workload and benchmark

Run the intended representative workload:

```bash
docker compose --profile workload run --rm loadgen
```

Defaults are 5 independently committed mutations/second for 60 seconds. To
override:

```bash
LOADGEN_RATE=10 LOADGEN_DURATION_SECONDS=30 LOADGEN_SEED=42 \
docker compose --profile workload run --rm loadgen
```

Run the unchanged formal verifier:

```bash
python tests/slo/verify_slo.py \
  --url "http://localhost:${MONITOR_PORT:-8080}/staleness" \
  --minimum-samples 100
```

It fails if sample count is not reached or p99 exceeds 1000 ms. For comparable
runs, clear prior samples; the fully isolated reset is:

```bash
docker compose down -v
docker compose up -d --build
```

Interpretation:

- `sample_count` is retained rolling samples, not necessarily lifetime events.
- p50/p95/p99/max are commit-to-first-marker-read latency.
- `in_flight_count` is observed commits still missing markers.
- `violation_count` is historical since monitor start.
- `active_violation_count` is unresolved violations.
- detection delay is age beyond the SLO when first detected; it is `null` when
  no violation occurred.
- workload throughput is `writes / duration_seconds`.

Record Redis `XPENDING`, DLQ length, replication-slot lag, and Meilisearch task
state alongside latency percentiles.

Controlled violation test, disposable deployments only:

```bash
INDEXER_PROCESSING_DELAY_MS=1500 \
docker compose up -d --build --force-recreate indexer
LOADGEN_RATE=1 LOADGEN_DURATION_SECONDS=5 \
docker compose --profile workload run --rm loadgen
python tests/slo/verify_slo.py \
  --url "http://localhost:${MONITOR_PORT:-8080}/staleness" \
  --minimum-samples 1 --violation-test
```

Restore the processing delay to zero afterward. The violation verifier requires
at least one breach and p99 detection delay at most 500 ms.

## Validation methodology and history

The final validation started the original Compose configuration with Compose
v2, waited for six healthy services, ran unit and integration tests, reset the
monitor sample window, ran 300 mutations at 5/second, verified 300 distinct
commit LSNs and timestamps, ran the unchanged SLO verifier, and inspected Redis
pending/DLQ state, both slot lags, PostgreSQL rows, Meilisearch documents, and
service health.

### Controlled integration test

The integration test commits insert, update, and delete separately. During the
earlier investigation, they measured approximately 150.426 ms, 395.939 ms, and
515.173 ms. The final post-fix test passed again in 0.638 seconds with no active
violation. Three samples prove end-to-end behavior, not a statistical p99.

### Earlier invalid burst measurement

An earlier 120-write run reported p50 1721.855 ms, p95/p99/max 2569.273 ms, 91
historical violations, and zero active violations at completion.

It was not a valid steady-rate p99. The generator used `with connection:` and
set `autocommit=True` inside that psycopg2 context. Psycopg2 2.9 created one
transaction for the context, so all 120 paced statements shared a transaction
ID, commit LSN, and timestamp and entered CDC as one committed burst. The result
measured burst-drain time, not 10 independent commits per second. It is retained
here as burst evidence and must not be compared directly with steady-state SLO
results.

### Corrected representative measurement

The generator now avoids the connection context manager, keeps autocommit, and
closes explicitly in `finally`. A regression test prevents reintroduction of
the invalid transaction scope. The corrected run produced 300 unique commit
LSNs/timestamps and p99 221.839 ms with zero violations.

## Important design decisions

- Stock PostgreSQL `pgoutput`, without a decoding extension.
- PostgreSQL commit time as T0.
- Independent monitoring slot rather than transport-derived timing.
- Marker submission only after product task completion.
- Per-event markers for rapid updates and deletes.
- LSN-versioned documents and tombstones.
- At-least-once transport plus idempotent application, not exactly once.
- Loopback-only published ports.
- Read-only, non-root Python service containers.

## Known limitations

- Only `public.products` is published and decoded.
- The decoder covers current schema types and protocol forms, not all PostgreSQL
  logical-replication features.
- Replica identity `DEFAULT` does not provide complete delete before-images.
- Redis streams and DLQ are not automatically trimmed.
- Visibility retention is finite.
- Monitor metrics are in memory and reset on process restart.
- Slots can retain unbounded WAL during consumer outages.
- Health endpoints are unauthenticated and rely on network isolation.
- Compose is a single-host reference deployment without backup automation.
- Meilisearch document count includes tombstones and differs from live rows.
- The load generator updates/deletes only IDs inserted during that run.
- Validation did not include external search traffic, large documents,
  multi-host networking, or rates above 5 commits/second.
- The successful steady-state run had no violations, so its detection-delay
  value was `null`; controlled detection validation requires an injected delay.

## Troubleshooting

**Compose rejects the `name` field:** use `docker compose` v2. Do not edit the
Compose file for legacy `docker-compose` v1.

**Port already in use:** override the corresponding `.env` port, for example
`MONITOR_PORT=18080`, and use that port in host requests.

**Monitor unavailable:** inspect `docker compose ps` and logs for `monitor`,
`postgres`, and `meilisearch`. `/health` includes readiness flags and last error.

**PostgreSQL disk grows:** inspect replication-slot lag. Restore the stopped
consumer; do not advance or drop a slot merely to silence lag.

**Redis pending grows:** inspect indexer errors, `XPENDING`, retry state, DLQ,
and Meilisearch task health before replay.

**Deleted records appear:** application queries must filter `_deleted = false`.

**Discovery cannot import psycopg2:** install the declared dependency or run
tests inside the reader image.

**Too few SLO samples:** confirm the workload completed and monitor readiness is
true; inspect monitor logs for missing markers or timestamp errors.

## Development and operations workflow

1. Keep `.env` local and never commit secrets.
2. Run focused unit tests.
3. Build the affected service image.
4. Wait for health checks.
5. Run the real integration test.
6. Reset benchmark state or the monitor window.
7. Run the representative workload and unchanged verifier.
8. Inspect queue, DLQ, WAL lag, Meilisearch state, and logs.
9. Preserve result JSON and logs for comparisons.

Production deployment additionally requires secret management, image scanning,
immutable digests, PostgreSQL backups/WAL archiving, Redis AOF recovery tests,
Meilisearch snapshots, storage alerts, and capacity planning. See
`docs/operations.md` and `docs/benchmark.md`.

## Future Applications & Validation Targets

The following are planned downstream demonstrations. They are not implemented,
deployed, or present in this repository today.

### Real-time Product Search

A future user-facing search application can query `products` with
`_deleted = false`. It is intended to demonstrate product discovery and visible
database changes within the measured staleness envelope.

### Live Inventory Dashboard

A future dashboard can visualize availability and price changes. It is intended
to exercise repeated updates, same-product ordering, tombstones, and visible
lag under continuously changing inventory.

### Event/Operations Observatory

A future internal observability application can combine monitor metrics,
replication-slot lag, Redis pending/DLQ state, and structured logs. It is
intended to demonstrate operational diagnosis and violation investigation. It
must not expose `cdc_visibility` directly to untrusted application clients.

These applications should consume this infrastructure as their source of truth
and add their own authorization, query contracts, interfaces, and tests. Future
validation should preserve the commit-based SLO definition rather than replace
it with frontend or API timing.

## Future infrastructure work

- Automate benchmark artifact capture in CI on a Docker-capable runner.
- Add live restart-recovery, pending-reclaim, and DLQ replay integration tests.
- Record the controlled 500 ms violation-detection test.
- Run sustained capacity tests across the documented 1/2/4/8 indexer matrix.
- Add stream and DLQ retention policies.
- Add authenticated or isolated operational endpoints for non-local use.
- Expand decoder coverage only when new published tables/types are introduced.
- Automate backup, restore, and full index rebuild procedures.

## License

MIT. See `LICENSE`.
