# Staleness Guarantee

## SLO

For every committed write to a tracked `products` row, the time from the
Postgres commit timestamp to the first confirmed visibility of the corresponding
document in Meilisearch is at most **1000 ms at p99**.

When the bound is violated, the system must detect and report the violation
within **500 ms**. A violation must never be silently absorbed.

The staleness clock starts at the source commit timestamp (`commit_ts_us`) and
ends at the monitor's first successful read-probe (`visible_ts_us`). It never
starts at reader receipt time, indexer receipt time, or any self-reported task
completion time.

## Budget Derivation

The 1000 ms bound is based on an approximate per-hop p99 budget of 650 ms:

| Hop | p99 budget |
| --- | ---: |
| WAL flush and decode | ~50 ms |
| Reader decode | ~25 ms |
| Redis `XADD` | ~10 ms |
| Consumer wake-up | ~15 ms |
| Batch and enqueue | ~100 ms |
| Meilisearch asynchronous indexing | ~400 ms |
| Read-probe overhead | ~50 ms |
| **Subtotal** | **~650 ms** |

Meilisearch indexing is the dominant term: `add-documents` returns a `taskUid`
immediately, while the document becomes searchable only after that task has
processed. The remaining approximately 35% is headroom for tail effects and
ordinary scheduling variance.

The 500 ms detection budget is N/2 and is compatible with a watchdog polling
interval of 100-150 ms.

## Measurement and Detection

The read-probe is the metric of record. For event `(key=k, commit_lsn=L)`, the
monitor queries Meilisearch until document `k` is returned with `_lsn >= L`.
The timestamp of the first successful probe is `visible_ts_us`; staleness is:

```text
visible_ts_us - commit_ts_us
```

Meilisearch task `finishedAt` is retained only as a diagnostic proxy. It is not
authoritative for visibility and cannot satisfy the SLO by itself.

The monitor polls real Postgres and real Meilisearch directly. It reports every
observed breach with the event identity, commit LSN, commit timestamp, observed
visibility timestamp, and calculated staleness. No timeout, retry, or indexing
component may turn a breach into an absent measurement.

## Scope and Limits

The guarantee applies to committed writes for tracked tables, currently the
`products` table. It assumes the declared production stack is available and
healthy enough for measurements to be made: Python, Postgres logical
replication, Redis Streams, Meilisearch, and Docker Compose.

