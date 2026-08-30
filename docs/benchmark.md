# Benchmark Procedure

The PRoot environment cannot run the Docker, PostgreSQL, Redis, and Meilisearch
stack reliably. Run these commands on a Docker host and retain the JSON output
and service logs for each run.

## Common setup

```bash
cp .env.example .env
# Set all passwords and keys in .env.
docker compose down -v
docker compose up -d --build
until curl -fsS http://localhost:8080/ready >/dev/null; do sleep 2; done
```

Capture metrics before and after each workload:

```bash
curl -fsS http://localhost:8080/staleness | tee results-before.json
docker compose --profile workload run --rm loadgen
curl -fsS http://localhost:8080/staleness | tee results-after.json
docker compose logs --no-color cdc-reader indexer monitor > run.log
```

The result JSON includes `sample_count`, p50/p95/p99 staleness, violations,
p99 detection delay, and readiness. Logs include commit-to-Redis timestamps,
queue/dequeue latency, batch size, product and marker task waits, lock waits,
and dead-letter events.

## Baseline and optimized runs

Baseline (single worker and single-message batches):

```bash
INDEXER_WORKERS=1 INDEXER_BATCH_SIZE=1 LOADGEN_RATE=5 LOADGEN_DURATION_SECONDS=60 \
  docker compose up -d --build --force-recreate indexer
docker compose --profile workload run --rm loadgen
curl -fsS http://localhost:8080/staleness | tee results-baseline.json
```

Optimized single replica:

```bash
INDEXER_WORKERS=4 INDEXER_BATCH_SIZE=50 LOADGEN_RATE=5 LOADGEN_DURATION_SECONDS=60 \
  docker compose up -d --build --force-recreate indexer
docker compose --profile workload run --rm loadgen
curl -fsS http://localhost:8080/staleness | tee results-batched-1.json
```

## Replica scaling

Run the same burst with 1, 2, 4, and 8 indexer replicas. Consumer names are
hostname-derived, so each scaled container has a distinct Redis consumer name.

```bash
for replicas in 1 2 4 8; do
  docker compose up -d --scale indexer="$replicas" indexer
  docker compose --profile workload run --rm loadgen
  curl -fsS http://localhost:8080/staleness > "results-replicas-${replicas}.json"
  docker compose logs --no-color indexer > "indexer-replicas-${replicas}.log"
done
```

Reset the database and indexes between separate comparison runs with
`docker compose down -v` followed by the common setup. Do not compare runs with
old samples still in the monitor's rolling window.

## Burst and same-document tests

For the 300-event tail case, run a one-shot burst from the writer role at a
known timestamp and report the maximum `staleness_ms` and event ID of the last
resolved marker. The included load generator can be made burstier with a high
`LOADGEN_RATE` and short duration.

For same-document ordering, use a temporary SQL workload that updates one row
in 60 committed transactions, then verify the products document `_lsn` equals
the newest event LSN and that all 60 event IDs appear in `cdc_visibility`.

## Violation detection

Run only in a disposable test deployment:

```bash
INDEXER_PROCESSING_DELAY_MS=1500 docker compose up -d --build --force-recreate indexer
LOADGEN_RATE=1 LOADGEN_DURATION_SECONDS=5 docker compose --profile workload run --rm loadgen
python tests/slo/verify_slo.py --url http://localhost:8080/staleness \
  --minimum-samples 1 --violation-test
```

Restore `INDEXER_PROCESSING_DELAY_MS=0` and recreate the indexer before normal
measurements.

## Report back

Send `results-*.json`, the corresponding `indexer-*.log` files, `run.log`, the
Docker image versions, and the workload rate/duration. The important values are
sample count, p50/p95/p99/max staleness, p99 detection delay, throughput,
queueing latency, pending count, errors, duplicate markers, and final same-key
LSN correctness.
