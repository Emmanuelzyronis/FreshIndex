# FreshIndex Project Handoff

## Executive summary

FreshIndex is a reference change-data-capture (CDC) pipeline for a PostgreSQL product catalog. It captures committed row changes from PostgreSQL logical WAL, publishes them through Redis Streams, applies ordered versions to a Meilisearch materialized view, and independently measures how long each committed change takes to become observable in search.

This is infrastructure, not a user-facing application. The primary contract is:

    p99(commit-to-confirmed-search-visibility) <= 1000 ms

PostgreSQL commit metadata is the source of truth for event time and ordering. A second logical-replication slot feeds the staleness monitor, which probes immutable visibility markers in Meilisearch after the product mutation has completed.

## Problem and goals

Search indexes are asynchronous materialized views. A database transaction can commit while search still exposes an older version or no document. FreshIndex provides authoritative commit timing, durable at-least-once delivery, LSN-based ordering, versioned delete tombstones, independent SLO measurement, reproducible Docker deployment, deterministic load generation, and verification tests.

## Scope and non-goals

Included: PostgreSQL 16 logical replication with stock pgoutput; public.products; Redis 7 Streams; Meilisearch 1.12; CDC reader, indexer, monitor, workload generator, Docker Compose, and tests.

Not included: a user interface or application presentation layer, arbitrary schemas/types, exactly-once transport, cross-region/Kubernetes manifests, indefinite audit retention, automatic poison-event repair, or universal latency guarantees outside the tested environment. Meilisearch remains fully in scope as the search-index and materialized-view target.

## Architecture

    Writer / load generator
            |
            v
    PostgreSQL products
       |                 |
       | cdc_products_slot              staleness_monitor_slot
       v                 v
    CDC reader       Staleness monitor
       |
       | XADD
       v
    Redis Stream: cdc_events
       |
       | XREADGROUP / XAUTOCLAIM
       v
    Indexer workers
       |
       +--> Meilisearch products
       +--> Meilisearch cdc_visibility markers
                             ^
                             |
                     monitor read-probe

Flow: a writer commits; PostgreSQL emits WAL on two slots; the reader decodes and publishes one event per row change; indexers lock the product key, compare LSNs, update Meilisearch, write a marker, and acknowledge Redis; the monitor probes the marker and records latency.

## Components

### PostgreSQL

Compose runs postgres:16.4 with wal_level=logical, track_commit_timestamp=on, max_replication_slots=10, and max_wal_senders=10. Initialization creates products, publication cdc_pub, slot cdc_products_slot, replication role cdc_reader, and restricted writer role catalog_writer. The monitor creates staleness_monitor_slot when absent.

Products columns: id (bigint identity primary key), sku (unique text), name, description, category, price_cents (non-negative integer), in_stock (boolean), and updated_at (timestamptz). Replica identity is DEFAULT, so deletes include key identity but not necessarily a complete old-row image.

### Decoder and reader

services/shared/pgoutput_decoder.py handles begin, commit, relation, insert, update, delete, and tuple messages. Changes are buffered until commit and emitted with commit LSN and commit timestamp. It is intentionally not a general PostgreSQL decoder.

services/replication-consumer/reader.py consumes cdc_products_slot, creates deterministic event IDs, publishes JSON to Redis stream cdc_events, sends replication feedback after Redis accepts commit events, and reconnects with exponential backoff. Internal endpoints are /ready and /health on port 8082.

### Redis

Redis 7.4.0 uses password authentication and AOF persistence. It stores cdc_events, consumer group indexers, pending entries, retry hash cdc_events:retries, dead-letter stream cdc_events_dlq, per-document locks, and a cleanup lock. Streams and the DLQ are not automatically trimmed.

### Indexer

services/indexer/main.py defaults to four workers and batches of 50. It creates products (primary key id, filterable _lsn and _deleted) and cdc_visibility (primary key event_id, filterable commit_ts_us). It locks a product ID, compares incoming and stored LSNs, applies a replacement/partial update/tombstone only when newer, waits for the product task, writes and waits for the marker task, acknowledges Redis, and clears retry state. Duplicate-key batches use sequential processing. Pending messages are reclaimed with XAUTOCLAIM. Internal endpoints are /ready and /health on port 8081.

### Meilisearch

Meilisearch 1.12.0 runs in production mode with persistent storage and a required master key. products is the search-facing materialized view for downstream consumers; no UI is built in this repository. cdc_visibility is internal and must not be exposed to untrusted clients. Markers contain event ID, document ID, operation, commit LSN, commit timestamp, and result (applied or superseded). Default retention is seven days.

### Staleness monitor

services/staleness-monitor/main.py independently consumes staleness_monitor_slot, tracks unresolved events in memory, and probes cdc_visibility every STALENESS_POLL_SECONDS. Both event ID and commit LSN must match. It records a sample on the first successful probe and a violation once an event exceeds the SLO. Feedback advances only after all events from a commit resolve. Timestamps over 60 seconds in the future or 24 hours in the past are rejected.

### Workload generator

load-generator/main.py performs real, independently committed writes: approximately 60% inserts, 30% updates, and 10% deletes. Rate, duration, and seed are configurable. It intentionally avoids a connection context-manager transaction scope so every mutation has its own commit LSN and timestamp.

## Event contract

Each row change becomes one JSON event with event_id, op (c/u/d), source database/schema/table, pk, after, before, commit_lsn, commit_ts_us, xid, captured_ts_us, and published_ts_us.

event_id is a deterministic SHA-256 hash of database, schema, table, commit LSN, transaction ID, and transaction-local row sequence. commit_ts_us is PostgreSQL commit time and the staleness clock start; capture and publication timestamps are diagnostic only. Updates may omit unchanged TOAST fields, so the indexer uses partial updates.

## Ordering, idempotency, and deletes

PostgreSQL commit LSN defines version order. Redis is at least once, so correctness depends on per-document locks, stored _lsn, rejection of older or duplicate LSNs, deterministic IDs, and markers for both applied and superseded events.

Deletes are retained as tombstones such as {"id":7,"_lsn":"0/20","_deleted":true}. Application queries must filter _deleted = false.

## SLO and measurement

For event E:

    staleness(E) = first_successful_marker_probe_ts(E) - postgres_commit_ts(E)

The clock does not start at reader receipt, Redis publication, indexer dequeue, or Meilisearch task submission. It ends at the monitor's first successful read of the matching marker. Because the marker is written only after the product task succeeds, this is a conservative upper bound on product visibility.

Objectives: p99 staleness <= 1000 ms, and controlled violation detection p99 <= 500 ms.

## Observability

| Service | Endpoint | Purpose |
| --- | --- | --- |
| Monitor | GET /staleness on 8080 | JSON latency and violation metrics |
| Monitor | GET /metrics on 8080 | Prometheus exposition |
| Monitor | GET /health and /ready | Readiness and active-SLO health |
| Reader | GET /health and /ready on 8082 | Replication readiness |
| Indexer | GET /health and /ready on 8081 | Worker/readiness counters |
| Meilisearch | GET /health on 7700 | Search availability |

Metrics include sample count, p50/p95/p99/max staleness, in-flight count, oldest age, historical and active violations, detection-delay p99, active violation duration, and readiness. Structured logs include event IDs, LSNs, queue latency, task waits, batch size, lock waits, retries, DLQ events, and errors.

## Repository layout

    FreshIndex/
    ├── README.md
    ├── PROJECT_HANDOFF.md
    ├── docker-compose.yml
    ├── .env.example
    ├── docs/
    ├── infra/postgres-init/
    ├── services/
    ├── load-generator/
    └── tests/{unit,integration,slo}/

## Setup

Prerequisites: Docker Engine, Docker Compose v2, curl, Python 3.12, and psycopg2-binary for host integration discovery.

    cp .env.example .env
    # Replace every example secret with a distinct strong value.
    docker compose config --quiet
    docker compose up -d --build
    docker compose ps
    curl -fsS http://localhost:8080/health
    curl -fsS http://localhost:8080/staleness

Export .env for host commands:

    set -a
    . ./.env
    set +a

Stop without deleting volumes: docker compose down. Reset all data: docker compose down -v.

## Configuration

Required secrets: POSTGRES_PASSWORD, CDC_DB_PASSWORD, WRITER_DB_PASSWORD, REDIS_PASSWORD, and MEILI_MASTER_KEY.

Important defaults: POSTGRES_DB=catalog; CDC_DB_USER=cdc_reader; WRITER_DB_USER=catalog_writer; host ports 5432/6379/7700/8080; CDC_MAX_ATTEMPTS=5; CDC_CLAIM_IDLE_MS=30000; CDC_KEY_LOCK_SECONDS=120; INDEXER_BATCH_SIZE=50; INDEXER_WORKERS=4; STALENESS_SLO_MS=1000; STALENESS_POLL_SECONDS=0.12; SAMPLE_WINDOW=10000; LOADGEN_RATE=5; LOADGEN_DURATION_SECONDS=60; LOADGEN_SEED=2026.

INDEXER_PROCESSING_DELAY_MS is for controlled violation tests only and must be zero during normal operation. Visibility markers default to seven-day retention and a one-hour cleanup interval.

## Tests

Unit tests:

    python -m unittest tests.unit.test_event_pipeline tests.unit.test_load_generator -v

Integration test (requires running Compose stack and psycopg2-binary):

    set -a; . ./.env; set +a
    RUN_INTEGRATION=1 DATABASE_URL="postgresql://$WRITER_DB_USER:$WRITER_DB_PASSWORD@localhost:5432/$POSTGRES_DB" MONITOR_URL="http://localhost:8080/staleness" python -m unittest tests.integration.test_pipeline -v

Full discovery:

    python -m unittest discover -v

The integration test commits insert, update, and delete independently and waits for all three monitor samples. Unit coverage includes deterministic IDs, LSN ordering, tombstones, partial updates, batching, marker matching, percentiles, DLQ behavior, and workload transaction semantics.

## Workload and benchmark

    docker compose --profile workload run --rm loadgen
    python tests/slo/verify_slo.py --url http://localhost:8080/staleness --minimum-samples 100

Defaults are five independently committed mutations per second for 60 seconds. For comparable runs, reset volumes and record sample count, p50/p95/p99/max, throughput, Redis pending/DLQ, both slot lags, Meilisearch tasks, and final row/document counts.

Controlled violation test, disposable deployment only:

    INDEXER_PROCESSING_DELAY_MS=1500 docker compose up -d --build --force-recreate indexer
    LOADGEN_RATE=1 LOADGEN_DURATION_SECONDS=5 docker compose --profile workload run --rm loadgen
    python tests/slo/verify_slo.py --url http://localhost:8080/staleness --minimum-samples 1 --violation-test

Restore processing delay to zero afterward.

## Validated results

Validated with Compose 2.40.3+ds1-0ubuntu1~24.04.1, PostgreSQL 16.4, Redis 7.4.0, Meilisearch 1.12.0, and the optimized default indexer.

Workload: 300 independently committed mutations at 5/sec for 60 seconds.

| Measurement | Result |
| --- | ---: |
| Samples | 300 |
| Throughput | 5.0 committed mutations/sec |
| p50 staleness | 151.994 ms |
| p95 staleness | 207.060 ms |
| p99 staleness | 221.839 ms |
| Maximum staleness | 225.274 ms |
| Historical violations | 0 |
| Active violations | 0 |
| Redis pending | 0 |
| Dead-letter messages | 0 |
| Both slot WAL lags | 0 bytes |
| PostgreSQL live rows | 200 |
| Meilisearch documents | 247, including tombstones and retained prior-run data |

All 300 observations had distinct commit LSNs and timestamps. The formal verifier passed the 1000 ms p99 threshold. These results validate this workload and deployment, not all future hardware or traffic shapes.

## Failure and recovery

Reader and monitor outages mark services unready, reconnect with bounded backoff, and retain WAL in their slots. Monitor lag and disk usage; do not advance or drop slots merely to silence lag.

Indexer failures remain pending for retry. After CDC_MAX_ATTEMPTS, the event and failure metadata move to cdc_events_dlq and the source message is acknowledged. Fix the cause, replay the original event field to cdc_events, confirm its marker, then remove the DLQ entry.

Markers are finite-retention measurement aids, not an indefinite audit log. Back up PostgreSQL with physical backups and WAL archiving, preserve Redis AOF, and snapshot both Meilisearch indexes. After inconsistent restore, stop writers and rebuild from an authoritative PostgreSQL/WAL position.

## Alerts

Alert on active violations, p99 above 1000 ms, detection-delay p99 above 500 ms after an injected test, readiness failures, retained WAL approaching disk limits, Redis pending/DLQ growth, Redis AOF errors, Meilisearch task failures, disk pressure, or health failure.

## Known limitations

- Only public.products is published and decoded.
- Decoder coverage is limited to the current schema and protocol subset.
- Replica identity DEFAULT does not provide complete delete before-images.
- Redis streams and DLQ have no automatic retention policy.
- Visibility retention is finite; monitor metrics reset on restart.
- Consumer outages can retain unbounded PostgreSQL WAL.
- Health endpoints are unauthenticated and rely on network isolation.
- Compose is a single-host reference deployment without backup automation.
- Meilisearch document count includes tombstones and differs from live rows.
- Validation did not cover multi-host networking, external search traffic, large documents, or sustained rates above 5 commits/sec.
- The successful steady-state run had no violations; controlled delay testing is required to validate detection delay.

## Future work

Automate benchmark artifact capture; add restart/reclaim/DLQ integration tests; record the controlled detection test; run 1/2/4/8 replica capacity tests; add stream/DLQ retention; secure operational endpoints; expand decoder coverage when needed; and automate backup, restore, and full index rebuild procedures.

No UI or application presentation layer is planned as part of this repository. Future infrastructure work is focused on scaling tests, backup automation, replay tooling, broader decoder support, and operational hardening. Any external consumer of the Meilisearch materialized view must filter _deleted = false and must not expose cdc_visibility.

## Source documents

- README.md: complete overview, commands, history, and troubleshooting.
- docs/architecture.md: architecture and event contract.
- docs/guarantee.md: SLO definition and measurement semantics.
- docs/benchmark.md: benchmark and scaling procedures.
- docs/limits.md: build-phase limits.
- docs/operations.md: rollout and operations runbook.

## One-paragraph Linear summary

FreshIndex is a Docker Compose reference implementation of a bounded-staleness PostgreSQL-to-search CDC pipeline. PostgreSQL product commits are decoded from logical WAL using pgoutput, emitted as deterministic events into Redis Streams, and applied to a versioned Meilisearch materialized view with LSN ordering, per-key locking, retries, pending recovery, DLQ handling, and tombstones. A second logical-replication consumer independently observes the same commits and probes immutable Meilisearch visibility markers, measuring commit-to-search latency without trusting the reader or indexer. The target is p99 staleness under 1000 ms and violation detection under 500 ms. The validated 300-write, 5/sec run achieved p99 221.839 ms, zero active or historical violations, zero pending/DLQ messages, and zero replication-slot lag. The project is infrastructure only: Meilisearch is the in-scope search/materialized-view target, while UI and application presentation work are outside the repository.
