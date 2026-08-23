# Architecture

This project measures and enforces bounded CDC-to-search staleness for a
Postgres product catalog. A real Postgres write is captured through logical
replication, published to Redis Streams, indexed in Meilisearch, and verified
by an independent read-probe monitor.

## Fixed Stack and Data Flow

```text
load generator
      |
      v
Postgres products -- logical replication (pgoutput) --> replication consumer
      |                                                        |
      |                                                        v
      +----------------------------------------------- Redis Stream (XADD)
                                                               |
                                                               v
                                                        indexer consumer
                                                               |
                                                               v
                                                           Meilisearch
                                                               ^
                                                               |
                                                staleness monitor read-probe
```

The load generator performs real writes against real Postgres. The monitor
queries real Postgres and real Meilisearch and does not trust an indexer's
self-report or Meilisearch task completion as proof of visibility.

## Domain Schema

The tracked table is `products`:

| Column | Type and constraint |
| --- | --- |
| `id` | `bigint` identity, primary key |
| `sku` | `text`, unique |
| `name` | `text` |
| `description` | `text` |
| `category` | `text` |
| `price_cents` | `int`, `>= 0` |
| `in_stock` | `bool` |
| `updated_at` | `timestamptz` domain data only; not the staleness clock |

Postgres is configured with `wal_level=logical` and
`track_commit_timestamp=on`. Publication `cdc_pub` is created `FOR TABLE
products`. Replica identity is `DEFAULT`.

## Replication and Decode

The replication consumer uses the core Postgres `pgoutput` logical decoding
plugin. This is a settled decision: `pgoutput` needs no extension and is the
production-standard plugin. `wal2json` is deliberately not used; it requires a
non-stock image or extension and was unobtainable in two separate sandbox
environments.

The consumer decodes transactions in authoritative commit-LSN order and emits
one `CDCEvent` per row change. `commit_ts_us` comes from Postgres commit
timestamp metadata and is T0 for staleness. `captured_ts_us` is only the
reader's decode timestamp, and `published_ts_us` is the Redis `XADD` timestamp.

## CDCEvent Envelope

```yaml
event_id: ULID
op: c | u | d
source:
  db: string
  schema: string
  table: products
pk:
  id: bigint
after: full row | null       # null on delete
before: row | null            # present on delete only
commit_lsn: string            # order and idempotency/version token
commit_ts_us: integer         # epoch microseconds; T0
xid: integer
captured_ts_us: integer       # reader decode time
published_ts_us: integer      # XADD time
```

`visible_ts_us` is intentionally absent from the payload. The monitor observes
it through a read-probe.

Because replica identity is `DEFAULT`, delete events include the old row only
to the extent Postgres supplies the key/old identity information; they do not
promise a full `before` image. Create and update events carry the full `after`
row.

## Delivery, Idempotency, and Ordering

Redis Streams delivery is at least once. `commit_lsn` therefore doubles as the
idempotency token. The indexer stores the last-applied LSN in each Meilisearch
document's filterable `_lsn` field and ignores an event whose LSN is less than or
equal to the stored value. Re-delivery is consequently harmless while newer
LSNs remain authoritative.

## Deployment Boundary

The repository is organized as:

```text
cdc-staleness-pipeline/
├── README.md
├── docker-compose.yml
├── docs/{guarantee.md, architecture.md, limits.md}
├── services/{replication-consumer, indexer, staleness-monitor}
├── load-generator/
├── infra/postgres-init/
└── tests/{integration, slo}
```

This document records the architecture and contracts only. Service
implementation, fixtures, and test data must use live system boundaries; no
mocked or fabricated data is permitted.

