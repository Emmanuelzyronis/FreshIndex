# FreshIndex

A reference change-data-capture pipeline that reads committed PostgreSQL row
changes from logical WAL, delivers them through Redis Streams, applies ordered
versions to Meilisearch, and independently measures commit-to-search
visibility against a **1 000 ms p99 staleness objective**.

This repository is infrastructure, not an application UI. It contains the
database schema, logical-replication reader, Redis-backed indexer, independent
staleness monitor, Docker Compose deployment, workload generator, and
verification tests needed to exercise the pipeline end to end.

---

## Validated benchmark

The pipeline was validated on **2026-09-01** with Docker Compose
`2.40.3+ds1-0ubuntu1~24.04.1`, PostgreSQL 16.4, Redis 7.4.0, Meilisearch
1.12.0, and the repository's default optimised indexer configuration.

The corrected steady-rate benchmark generated **300 independently committed
mutations at 5 mutations/second for 60 seconds**:

| Measurement | Result |
|---|---:|
| Samples | 300 |
| Throughput | 5.0 committed mutations/s |
| p50 staleness | 151.994 ms |
| p95 staleness | 207.060 ms |
| **p99 staleness** | **221.839 ms** |
| Maximum staleness | 225.274 ms |
| Historical violations | 0 |
| Active violations at completion | 0 |
| Redis pending messages | 0 |
| Dead-letter messages | 0 |
| `cdc_products_slot` WAL lag | 0 bytes |
| `staleness_monitor_slot` WAL lag | 0 bytes |
| Final PostgreSQL live row count | 200 |
| Final Meilisearch `products` document count | 247 (includes tombstones and retained prior-run data) |

The formal verifier passed the unchanged 1 000 ms p99 threshold. All 300
observations had distinct commit LSNs and distinct commit timestamps.

> **Note:** These measurements validate this specific Docker Compose deployment
> and workload. They are evidence, not a universal latency guarantee for
> different hardware, data volumes, query load, or deployment topologies.

---

## Problem statement

Search indexes are asynchronous materialised views. A database transaction can
commit successfully while a search client continues to see an older version, or
no version, until the indexing pipeline catches up. Basic delivery metrics do
not answer the important question:

> *How long after the authoritative commit did the corresponding change become
> independently observable in search?*

FreshIndex addresses that question with three properties:

1. PostgreSQL commit metadata is the source of truth for event time and order.
2. The indexing path is at-least-once but rejects older or duplicate document
   versions by commit LSN.
3. A **separate** logical-replication consumer measures visibility by reading an
   immutable event marker from Meilisearch after the product mutation succeeds.

---

## Architecture

### High-level data flow

```mermaid
flowchart LR
    W[Writer / load generator] -->|committed SQL transaction| PG[(PostgreSQL 16)]

    subgraph Indexing path
        PG -->|cdc_products_slot\npgoutput| R[CDC reader]
        R -->|XADD cdc_events\napprox MAXLEN 500k| RS[(Redis Streams)]
        RS -->|XREADGROUP batch\nXAUTOCLAIM recovery| I[Indexer workers ×4]
        I -->|versioned mutation\nor tombstone| P[(Meilisearch\nproducts)]
        I -->|immutable marker| V[(Meilisearch\ncdc_visibility)]
    end

    subgraph Measurement path
        PG -->|staleness_monitor_slot\npgoutput| M[Staleness monitor]
        M -->|GET marker by event_id| V
        M --> H["/staleness  /health  /metrics"]
    end
```

PostgreSQL is read through **two independent logical replication slots**. The
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

    Writer->>PostgreSQL: INSERT / UPDATE / DELETE
    PostgreSQL-->>Writer: Commit (LSN + timestamp assigned)

    par Indexing path
        PostgreSQL-->>Reader: pgoutput on cdc_products_slot
        Reader->>Redis: XADD deterministic event envelope (MAXLEN ~500k)
        Reader->>PostgreSQL: Replication feedback after commit
        Redis-->>Indexer: XREADGROUP batch (up to 50)
        Indexer->>Indexer: Acquire per-key lock, compare LSN
        Indexer->>Products: Apply version or tombstone (_lsn, _deleted)
        Products-->>Indexer: Product task completed
        Indexer->>Visibility: Write immutable marker (event_id, commit_lsn)
        Visibility-->>Indexer: Marker task completed
        Indexer->>Redis: XACK + clear retry counter
    and Measurement path
        PostgreSQL-->>Monitor: Same commit on independent slot
        loop Every STALENESS_POLL_SECONDS (default 0.12 s)
            Monitor->>Visibility: GET marker by event_id
        end
        Visibility-->>Monitor: Matching event_id and commit_lsn
        Monitor->>Monitor: Record visible_ts − commit_ts as staleness sample
        Monitor->>PostgreSQL: Advance slot after all commit events resolve
    end
```

### Failure and recovery paths

```mermaid
flowchart TD
    A[Indexer receives message] --> B{Apply succeeds?}
    B -- yes --> C[XACK + write marker]
    B -- no --> D[Increment retry counter\nleave message pending]
    D --> E{attempts >= CDC_MAX_ATTEMPTS?}
    E -- no --> F[XAUTOCLAIM reclaims\nafter CDC_CLAIM_IDLE_MS]
    F --> A
    E -- yes --> G[Write to cdc_events_dlq\nXACK source message]
    G --> H[Manual replay after fix]

    Reader[CDC reader disconnects] --> I[Exponential backoff\n1 s → 30 s max]
    I --> J[Reconnect and resume\nfrom slot position]

    Monitor[Monitor disconnects] --> K[Exponential backoff\n1 s → 30 s max]
    K --> L[Recreate slot if absent\nresume from position]
```

---

## SLO definition

For event **E**:

```
staleness(E) = first_successful_marker_probe_ts(E) − postgres_commit_ts(E)
```

- **T₀** is PostgreSQL's transaction commit timestamp decoded from the
  `pgoutput` commit message — not reader receipt time, Redis publish time,
  indexer dequeue time, or Meilisearch task-submission time.
- **T₁** is the monitor's first successful read of the event's immutable marker
  from the `cdc_visibility` Meilisearch index. The marker is written only after
  the product mutation task completes, making this a **conservative upper bound**
  on product visibility.

| Objective | Threshold |
|---|---|
| p99 commit-to-visibility | ≤ 1 000 ms |
| p99 violation detection delay | ≤ 500 ms |

---

## Components

### PostgreSQL

Runs `postgres:16.4` with `wal_level=logical`, `track_commit_timestamp=on`,
`max_replication_slots=10`, `max_wal_senders=10`.

Initialisation creates `public.products`, publication `cdc_pub`, logical slot
`cdc_products_slot`, replication role `cdc_reader`, and restricted writer role
`catalog_writer`. The monitor creates `staleness_monitor_slot` on first start.

| Column | Type |
|---|---|
| `id` | `bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY` |
| `sku` | `text NOT NULL UNIQUE` |
| `name` | `text NOT NULL` |
| `description` | `text NOT NULL` |
| `category` | `text NOT NULL` |
| `price_cents` | `integer NOT NULL CHECK (price_cents >= 0)` |
| `in_stock` | `boolean NOT NULL` |
| `updated_at` | `timestamptz NOT NULL DEFAULT clock_timestamp()` |

Replica identity is `DEFAULT`, so deletes reliably carry the primary-key
identity but do not guarantee a complete old-row image.

### Logical replication decoder

`services/shared/pgoutput_decoder.py` implements the `pgoutput` subset needed
by the schema: begin/commit, relation metadata, insert, update, delete, and
tuple decoding. Origin, type, and truncate messages are ignored. Row changes are
buffered until commit, then emitted with the authoritative commit LSN and
timestamp.

**Event envelope fields:**

| Field | Meaning |
|---|---|
| `event_id` | Deterministic SHA-256 of `db\|schema\|table\|lsn\|xid\|seq` |
| `op` | `c` insert, `u` update, `d` delete |
| `source` | `{db, schema, table}` |
| `pk` | Primary key (`id`) |
| `after` | New row for insert/update; `null` on delete |
| `before` | Old/key tuple for delete; `null` otherwise |
| `commit_lsn` | Commit WAL position — version and idempotency token |
| `commit_ts_us` | PostgreSQL commit timestamp in epoch µs — **T₀** |
| `xid` | PostgreSQL transaction ID |
| `captured_ts_us` | Reader decode time (diagnostic only) |
| `published_ts_us` | Redis XADD time (diagnostic only) |

### CDC reader (`services/replication-consumer/reader.py`)

Consumes `cdc_products_slot`, decodes committed transactions, appends JSON
envelopes to `cdc_events` with approximate MAXLEN trimming (~500 k entries by
default), and sends replication feedback after commit messages. Connection
failures retry with exponential backoff 1 → 30 s.

Internal endpoints on port 8082 (not host-published):

| Path | Purpose |
|---|---|
| `GET /ready` | Returns 200 when replication is active |
| `GET /health` | Returns events published and last error |

### Redis

Runs `redis:7.4.0-alpine` with password auth, AOF persistence
(`appendfsync everysec`).

| Key | Purpose |
|---|---|
| `cdc_events` | Main event stream (MAXLEN ~500 k, approximate trim) |
| `cdc_events_dlq` | Dead-letter stream (MAXLEN ~50 k, approximate trim) |
| `cdc_events:retries` | Per-message attempt counter hash |
| `cdc_events:document-lock:<id>` | Per-product serialisation lock |
| `cdc_events:visibility-cleanup-lock` | Marker cleanup coordination |

### Indexer (`services/indexer/main.py`)

Defaults: 4 worker threads, batches up to 50 Redis events.

Initialises:
- `products` — primary key `id`, filterable `_lsn` and `_deleted`
- `cdc_visibility` — primary key `event_id`, filterable `commit_ts_us`

**Batch path (distinct document IDs):** acquires per-key locks in sorted order,
compares LSNs, submits one `add_documents` task for all products, waits, submits
one marker task for all events, waits, then acknowledges each message.

**Fallback path (duplicate document IDs in batch):** routes each message through
the sequential single-event path to preserve explicit LSN ordering.

**Retry and DLQ:** failed messages stay pending for `XAUTOCLAIM` recovery. At
`CDC_MAX_ATTEMPTS` the event moves to `cdc_events_dlq` (with retention). Batch-
level failures also increment per-message retry counters so events progress
toward DLQ rather than looping silently.

Internal endpoints on port 8081 (not host-published):

| Path | Purpose |
|---|---|
| `GET /ready` | Returns 200 when workers are running |
| `GET /health` | Returns worker counters and queue depth |

### Meilisearch

Runs `getmeili/meilisearch:v1.12.0` in production mode with persistent storage.

- `products` — the searchable materialised view for downstream consumers.
  Application searches **must** filter `_deleted = false`.
- `cdc_visibility` — internal marker index. **Must not be exposed to
  application clients.**

Markers contain `event_id`, `document_id`, `op`, `commit_lsn`,
`commit_ts_us`, and `result` (`applied` or `superseded`). Default retention:
7 days. Retention must exceed the longest expected monitor outage.

### Staleness monitor (`services/staleness-monitor/main.py`)

Independent of Redis and the indexer. Consumes `staleness_monitor_slot`,
tracks each event in memory, and polls `cdc_visibility` by `event_id` every
`STALENESS_POLL_SECONDS`. Both `event_id` and `commit_lsn` must match.

Records a staleness sample on the first successful probe. Records a violation
once an unresolved event age exceeds the SLO. Replication feedback advances
only after every event in a commit has resolved. Rejects commit timestamps
>60 s in the future or >24 h in the past. Rolling sample capacity: 10 000.

Published endpoints on port 8080:

| Path | Purpose |
|---|---|
| `GET /staleness` | JSON latency distribution and violation metrics |
| `GET /metrics` | Prometheus text exposition with `# HELP`/`# TYPE` headers |
| `GET /health` | 200 `ok` when ready and no active violation; 503 `STALE` on violation |
| `GET /ready` | 200 when replication and probe are ready |

### Workload generator (`load-generator/main.py`)

Produces ~60% inserts, ~30% updates, ~10% deletes, paced by `LOADGEN_RATE`
and seeded by `LOADGEN_SEED`. Each mutation is an **independently committed**
transaction with its own commit LSN and timestamp (`autocommit=True`, no
connection context-manager wrapper).

---

## Observability

### Staleness JSON (`/staleness`)

```json
{
  "sample_count": 300,
  "p50_staleness_ms": 151.994,
  "p95_staleness_ms": 207.060,
  "p99_staleness_ms": 221.839,
  "max_staleness_ms": 225.274,
  "in_flight_count": 0,
  "oldest_in_flight_age_ms": null,
  "violation_count": 0,
  "active_violation_count": 0,
  "p99_detection_delay_ms": null,
  "violation_duration_seconds": 0.0,
  "replication_ready": true,
  "probe_ready": true
}
```

### Prometheus metrics (`/metrics`)

Every metric is emitted with `# HELP` and `# TYPE` headers:

| Metric | Type | Meaning |
|---|---|---|
| `cdc_staleness_samples_total` | counter | Samples in rolling window |
| `cdc_staleness_p50_milliseconds` | gauge | Rolling p50 |
| `cdc_staleness_p95_milliseconds` | gauge | Rolling p95 |
| `cdc_staleness_p99_milliseconds` | gauge | Rolling p99 — SLO threshold 1 000 ms |
| `cdc_staleness_max_milliseconds` | gauge | Maximum retained staleness |
| `cdc_staleness_in_flight` | gauge | Events awaiting markers |
| `cdc_staleness_oldest_in_flight_milliseconds` | gauge | Oldest unresolved age |
| `cdc_staleness_violations_total` | counter | Violations since monitor start |
| `cdc_staleness_active_violations` | gauge | Currently unresolved violations |
| `cdc_staleness_detection_delay_p99_milliseconds` | gauge | p99 delay beyond SLO at detection |
| `cdc_staleness_violation_duration_seconds` | gauge | Longest active violation duration |

Missing numeric values are exposed as `NaN`.

### Structured logs

All services emit structured JSON logs. Key log events:

| Event | Service | Key fields |
|---|---|---|
| `cdc_event_published` | reader | `commit_lsn`, `op`, `table`, `message_id` |
| `indexer_event` | indexer | `result`, `commit_lsn`, `stream_queue_latency_ms`, `batch_size` |
| `indexer_dead_lettered` | indexer | `message_id`, `attempts`, `reason` |
| `staleness_sample` | monitor | `staleness_ms`, `commit_lsn`, `pk`, `op` |
| `VIOLATION` | monitor | `commit_lsn`, `age_ms`, `detection_delay_ms` |

---

## Repository structure

```text
FreshIndex/
├── docker-compose.yml            # Full stack definition
├── .env.example                  # All required and optional variables
├── pyproject.toml                # Ruff lint and mypy config
├── requirements-dev.txt          # Lint/type-check dev tools
├── docs/
│   ├── architecture.md           # Architecture and event contract
│   ├── benchmark.md              # Benchmark procedure and scaling matrix
│   ├── guarantee.md              # SLO definition and measurement semantics
│   ├── limits.md                 # Build-phase limits and operational notes
│   └── operations.md             # Rollout and operations runbook
├── infra/postgres-init/          # SQL and shell init scripts
├── services/
│   ├── replication-consumer/     # CDC reader service
│   ├── indexer/                  # Meilisearch indexer service
│   ├── staleness-monitor/        # Independent SLO monitor
│   └── shared/pgoutput_decoder.py
├── load-generator/               # Deterministic workload generator
├── tests/
│   ├── unit/                     # Pure-Python unit tests (no Docker)
│   ├── integration/              # Live pipeline integration test
│   └── slo/verify_slo.py        # Formal SLO verifier
└── demo/                         # Portfolio evidence harness
```

---

## Prerequisites and setup

- Docker Engine
- Docker Compose v2 (`docker compose` — not legacy `docker-compose` v1)
- `curl`
- Python 3.12 for host-side unit and SLO commands
- `psycopg2-binary` for host-side integration test

```bash
cp .env.example .env
# Replace every example password and key with distinct strong values.
docker compose version
docker compose config --quiet
```

`.env` is ignored by Git. Before running host-side commands that reference its
variables, export it:

```bash
set -a
. ./.env
set +a
```

---

## Configuration

### Required secrets

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL administrator password |
| `CDC_DB_PASSWORD` | Replication role password |
| `WRITER_DB_PASSWORD` | Restricted writer role password |
| `REDIS_PASSWORD` | Redis password |
| `MEILI_MASTER_KEY` | Meilisearch master key (≥16 random bytes) |

### Key tunables

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_PORT` | `5432` | Loopback host port |
| `REDIS_PORT` | `6379` | Loopback host port |
| `MEILI_PORT` | `7700` | Loopback host port |
| `MONITOR_PORT` | `8080` | Monitor host port |
| `CDC_STREAM_MAXLEN` | `500000` | Approximate stream entry limit |
| `CDC_DLQ_MAXLEN` | `50000` | Approximate DLQ entry limit |
| `CDC_MAX_ATTEMPTS` | `5` | Attempts before DLQ |
| `CDC_CLAIM_IDLE_MS` | `30000` | Pending reclaim age |
| `CDC_KEY_LOCK_SECONDS` | `120` | Document lock timeout |
| `INDEXER_PROCESSING_DELAY_MS` | `0` | **Controlled violation test only** — must be 0 in normal operation |
| `INDEXER_BATCH_SIZE` | `50` | Maximum read batch |
| `INDEXER_WORKERS` | `4` | Worker threads |
| `VISIBILITY_MARKER_RETENTION_SECONDS` | `604800` | Marker retention (7 days) |
| `VISIBILITY_MARKER_CLEANUP_SECONDS` | `3600` | Cleanup interval |
| `STALENESS_SLO_MS` | `1000` | Violation threshold |
| `STALENESS_POLL_SECONDS` | `0.12` | Probe interval |
| `SAMPLE_WINDOW` | `10000` | Rolling sample capacity |
| `LOADGEN_RATE` | `5` | Mutations per second |
| `LOADGEN_DURATION_SECONDS` | `60` | Duration (0 = until stopped) |
| `LOADGEN_SEED` | `2026` | Deterministic seed |

---

## Starting and stopping

```bash
# Start the full pipeline (builds images on first run)
docker compose up -d --build

# Check all six services are healthy
docker compose ps

# Confirm monitor is ready
curl -fsS http://localhost:${MONITOR_PORT:-8080}/health

# Stream JSON latency metrics
curl -fsS http://localhost:${MONITOR_PORT:-8080}/staleness

# Structured logs for all pipeline services
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

---

## Tests

### Unit tests (no Docker required)

```bash
python -m unittest tests.unit.test_event_pipeline tests.unit.test_load_generator -v
```

Covers: deterministic event IDs, LSN ordering, tombstones, superseded events,
partial TOAST updates, batch task count, SDK document-model leak guard,
percentile boundary behaviour, dead-letter promotion, and workload transaction
semantics.

### Integration test (requires running stack)

```bash
set -a; . ./.env; set +a
RUN_INTEGRATION=1 \
DATABASE_URL="postgresql://$WRITER_DB_USER:$WRITER_DB_PASSWORD@localhost:${POSTGRES_PORT:-5432}/$POSTGRES_DB" \
MONITOR_URL="http://localhost:${MONITOR_PORT:-8080}/staleness" \
python -m unittest tests.integration.test_pipeline -v
```

Commits insert, update, and delete independently and waits for all three
monitor samples, then confirms no active violation.

Or run inside the reader image (no host `psycopg2-binary` required):

```bash
set -a; . ./.env; set +a
docker run --rm \
  --network freshindex_pipeline \
  -v "$PWD:/repo:ro" -w /repo -e PYTHONPATH=/repo \
  -e RUN_INTEGRATION=1 \
  -e DATABASE_URL="postgresql://$WRITER_DB_USER:$WRITER_DB_PASSWORD@postgres:5432/$POSTGRES_DB" \
  -e MONITOR_URL=http://monitor:8080/staleness \
  freshindex-cdc-reader \
  python -m unittest tests.integration.test_pipeline -v
```

### Lint and type checks

```bash
pip install -r requirements-dev.txt
ruff check services load-generator tests
mypy --config-file pyproject.toml
```

---

## Workload and benchmark

Run the representative workload:
```bash
docker compose --profile workload run --rm loadgen
```

Run with custom parameters:
```bash
LOADGEN_RATE=10 LOADGEN_DURATION_SECONDS=30 LOADGEN_SEED=42 \
  docker compose --profile workload run --rm loadgen
```

Run the formal SLO verifier (unchanged):
```bash
python tests/slo/verify_slo.py \
  --url "http://localhost:${MONITOR_PORT:-8080}/staleness" \
  --minimum-samples 100
```

Fails if sample count is not reached or p99 > 1 000 ms. For comparable runs,
reset state completely:
```bash
docker compose down -v
docker compose up -d --build
```

### Controlled violation test (disposable deployment only)

```bash
INDEXER_PROCESSING_DELAY_MS=1500 \
  docker compose up -d --build --force-recreate indexer
LOADGEN_RATE=1 LOADGEN_DURATION_SECONDS=5 \
  docker compose --profile workload run --rm loadgen
python tests/slo/verify_slo.py \
  --url "http://localhost:${MONITOR_PORT:-8080}/staleness" \
  --minimum-samples 1 --violation-test
```

Requires ≥1 violation and p99 detection delay ≤500 ms. **Restore
`INDEXER_PROCESSING_DELAY_MS=0` afterward.**

---

## Core guarantees and semantics

### Ordering and idempotency

- PostgreSQL commit LSN defines version order.
- Redis Streams provides at-least-once delivery.
- Event IDs are deterministic SHA-256 hashes — WAL replay produces the same ID.
- Per-document Redis locks serialise concurrent workers.
- The applied LSN is stored in every Meilisearch product document (`_lsn`).
- Incoming events with LSN ≤ stored LSN are marked `superseded` and cannot
  overwrite newer state.
- Markers are written for both `applied` and `superseded` events so every
  committed event remains measurable.

### Delete handling (tombstones)

Deletes are versioned tombstones, not physical removals:

```json
{"id": 7, "_lsn": "0/20", "_deleted": true}
```

Retaining `_lsn` prevents an older insert or update from resurrecting a deleted
document. **Application searches must filter tombstones:**

```bash
curl -sS -X POST http://localhost:7700/indexes/products/search \
  -H "Authorization: Bearer $MEILI_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"q":"","filter":"_deleted = false"}'
```

---

## Failure and recovery

| Failure | Behaviour |
|---|---|
| Reader disconnects | Marks unready, reconnects with backoff, resumes from slot |
| Monitor disconnects | Marks unready, reconnects with backoff, recreates slot if absent |
| Indexer message failure | Message stays pending; retry counter incremented |
| Indexer batch failure | Per-message retry counters incremented; batch remains pending |
| At `CDC_MAX_ATTEMPTS` | Event written to `cdc_events_dlq` (with MAXLEN trim); source acknowledged |

**DLQ replay:** fix the root cause, replay the original `event` field to
`cdc_events`, confirm its visibility marker in `cdc_visibility`, then remove
the DLQ entry. Deterministic IDs and LSN checks make replay idempotent.

---

## Alerts

Alert on:

- `cdc_staleness_active_violations > 0` immediately.
- `cdc_staleness_p99_milliseconds > 1000` over a representative window.
- `cdc_staleness_detection_delay_p99_milliseconds > 500` after an injected test.
- Any service readiness failure for more than two health intervals.
- PostgreSQL replication-slot WAL lag approaching the disk budget.
- Redis `XPENDING` growth, DLQ growth, or AOF persistence errors.
- Meilisearch task failures, disk pressure, or unavailable health endpoint.

---

## Design decisions

| Decision | Reason |
|---|---|
| Stock `pgoutput`, no extension | Available on unmodified PostgreSQL; no image changes needed |
| PostgreSQL commit time as T₀ | Authoritative; not inflated by reader or transport jitter |
| Independent monitoring slot | Cannot be gamed by the reader or indexer; measures real visibility |
| Marker submission after product task | Conservative — ensures product is actually visible |
| Per-event markers for all operations | Measures superseded events; handles rapid same-key bursts |
| LSN-versioned tombstones | Prevents resurrection of deleted documents |
| At-least-once + idempotent application | Simpler than exactly-once; correctness via LSN ordering |
| Loopback-only published ports | No accidental external exposure |
| Read-only non-root containers | Reduced container attack surface |
| Approximate MAXLEN on streams | Bounded Redis memory without synchronous trimming overhead |

---

## Known limitations

- Only `public.products` is published and decoded.
- Decoder covers current schema types and protocol forms, not all PostgreSQL
  logical-replication features.
- Replica identity `DEFAULT` does not provide complete delete before-images.
- Monitor metrics are in-memory and reset on process restart.
- Slots retain unbounded WAL during consumer outages.
- Health endpoints are unauthenticated (rely on network isolation).
- Compose is a single-host reference deployment without backup automation.
- Meilisearch document count includes tombstones and differs from live rows.
- The load generator updates/deletes only IDs inserted during that run.
- Validation did not include external search traffic, large documents,
  multi-host networking, or rates above 5 commits/second.

---

## Portfolio demo harness

`demo/` is a repeatable, single-command evidence demonstration of the real
pipeline:

| Scenario | What it proves |
|---|---|
| **A — happy path** | One product mutation traced from PostgreSQL commit through Redis, indexer, Meilisearch product doc, visibility marker, and monitor staleness sample with real timings |
| **B — ordering** | Same-row burst of 150 updates: final document equals newest committed LSN; older events are `superseded` |
| **SLO** | `verify_slo.py` against the real monitor endpoint with a dedicated mixed-key workload |
| **C — failure/recovery** | Indexer stopped; backlog and violations captured; restart; backlog → 0, pending → 0, DLQ → 0 |

```bash
docker compose up -d --build
bash demo/run.sh
```

See `demo/README.md` for the full procedure.

---

## Development workflow

1. Keep `.env` local and never commit secrets.
2. Run `ruff check` and `mypy` before pushing.
3. Run unit tests: `python -m unittest tests.unit.test_event_pipeline tests.unit.test_load_generator -v`
4. Build the affected service image.
5. Wait for health checks: `docker compose ps`
6. Run the integration test.
7. Reset benchmark state or the monitor window.
8. Run the workload and the unchanged SLO verifier.
9. Inspect queue depth, DLQ, WAL lag, Meilisearch state, and logs.
10. Preserve result JSON and logs for comparisons.

---

## CI

GitHub Actions runs three jobs on every push and pull request:

| Job | Steps |
|---|---|
| `lint` | `ruff check` on all Python source |
| `typecheck` | `mypy` with strict-ish config |
| `unit` | `compileall` + full unit test suite |

---

## Future work

- Automate benchmark artifact capture in CI on a Docker-capable runner.
- Add live restart-recovery, pending-reclaim, and DLQ replay integration tests.
- Record the controlled 500 ms violation-detection test.
- Run sustained capacity tests across the 1/2/4/8 indexer replica matrix.
- Add authenticated or isolated operational endpoints for non-local use.
- Expand decoder coverage when new published tables/types are introduced.
- Automate backup, restore, and full index rebuild procedures.
- Persist monitor samples to Redis for cross-restart continuity.

---

## License

MIT. See `LICENSE`.
