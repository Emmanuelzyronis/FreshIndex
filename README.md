# Bounded-Staleness CDC Pipeline

A production-oriented reference pipeline that captures committed PostgreSQL
product changes, delivers them through Redis Streams, applies ordered versions
to Meilisearch, and independently measures commit-to-search visibility against
a 1000 ms p99 target.

## Data flow

```text
PostgreSQL 16 (pgoutput) -> CDC reader -> Redis Stream -> indexer -> Meilisearch
          \----------------------------------------------------> monitor
```

The indexer stores each product's PostgreSQL commit LSN and a logical deletion
flag. After the product mutation is confirmed, it writes an immutable event
marker to a separate Meilisearch index. The monitor consumes its own logical
replication slot and read-probes that marker. This preserves independent proof
for rapid updates and deletes even after a newer product version is indexed.

## Stack

- Python 3.12
- PostgreSQL 16 logical replication with `pgoutput`
- Redis 7 Streams with consumer groups, pending recovery, and a dead-letter stream
- Meilisearch 1.12
- Docker Compose
- JSON structured logs and Prometheus-compatible metrics

## Run

Requirements: Docker Engine and Docker Compose v2.

```bash
cp .env.example .env
# Replace every example password and key in .env.
docker compose up -d --build
docker compose ps
curl -fsS http://localhost:8080/staleness
```

Run a 60-second mixed insert/update/delete workload:

```bash
docker compose --profile workload run --rm loadgen
curl -fsS http://localhost:8080/staleness
```

Use `LOADGEN_RATE` and `LOADGEN_DURATION_SECONDS` in `.env` to control the
workload. The generator uses real committed PostgreSQL transactions.

## Search contract

Deletes are versioned tombstones so an older retry cannot resurrect deleted
data. Product searches must exclude tombstones:

```bash
curl -sS -X POST http://localhost:7700/indexes/products/search \
  -H "Authorization: Bearer $MEILI_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"q":"","filter":"_deleted = false"}'
```

Do not expose the `cdc_visibility` index to application clients. It is an
internal, append-only measurement index with configurable retention.

## Endpoints

| Service | Endpoint | Purpose |
| --- | --- | --- |
| Monitor | `GET :8080/staleness` | JSON latency and violation metrics |
| Monitor | `GET :8080/metrics` | Prometheus text metrics |
| Monitor | `GET :8080/health` | Dependency readiness and active SLO state |
| Reader | `GET :8082/ready` | Container-internal readiness |
| Indexer | `GET :8081/ready` | Container-internal readiness |

The reader and indexer ports are not published by default; Compose uses their
endpoints for health checks inside the network.

## Delivery guarantees

- PostgreSQL commit timestamps are the authoritative start time.
- Redis delivery is at least once.
- Deterministic event IDs make WAL re-delivery idempotent.
- Per-document Redis locks serialize multiple indexer consumers.
- Product `_lsn` values reject older or duplicate mutations.
- Deletes retain `_lsn` in tombstones and cannot be undone by an older event.
- Abandoned pending messages are reclaimed with `XAUTOCLAIM`.
- Events that fail repeatedly are moved to `cdc_events_dlq` after five attempts.
- Visibility markers are written only after the product mutation task succeeds.

The indexer defaults to four bounded workers and batches up to 50 independent
document IDs. Same-document events still serialize under the Redis lock; a
batch containing duplicate IDs falls back to the single-event path.

## Verification

Focused unit verification:

```bash
python -m unittest tests.unit.test_event_pipeline -v
```

Real integration verification after the stack is healthy:

```bash
RUN_INTEGRATION=1 \
DATABASE_URL='postgresql://catalog_writer:YOUR_WRITER_PASSWORD@localhost:5432/catalog' \
python -m unittest tests.integration.test_pipeline -v
```

Collect at least 100 real samples and check the stated p99:

```bash
python tests/slo/verify_slo.py --minimum-samples 100
```

Measure the 500 ms violation-detection bound separately in a non-production
test deployment:

```bash
# Set INDEXER_PROCESSING_DELAY_MS=1500 in .env, then:
docker compose up -d --force-recreate indexer
LOADGEN_RATE=1 LOADGEN_DURATION_SECONDS=5 \
  docker compose --profile workload run --rm loadgen
python tests/slo/verify_slo.py --minimum-samples 1 --violation-test
# Restore INDEXER_PROCESSING_DELAY_MS=0 and recreate the indexer afterward.
```

The repository records one historical happy-path sample of 351.575 ms. That is
real evidence, but it is not sufficient to claim a statistically meaningful
p99. Publish results from the SLO command only after running it on the target
deployment.

## Operations

See [docs/operations.md](docs/operations.md) for alerts, backups, recovery,
retention, dead-letter replay, upgrades, and production rollout checks.
See [docs/benchmark.md](docs/benchmark.md) for the 1/2/4/8-replica benchmark
matrix and the exact result files to send back.
Architecture and guarantee details are in [docs/architecture.md](docs/architecture.md)
and [docs/guarantee.md](docs/guarantee.md).

## License

MIT
