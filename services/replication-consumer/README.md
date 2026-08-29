# Replication consumer

`reader.py` consumes the PostgreSQL `pgoutput` slot, decodes committed row
changes, assigns deterministic event IDs, and publishes JSON envelopes to a
Redis Stream. It acknowledges WAL only after Redis accepts all decoded events
from the message. The process reconnects with bounded exponential backoff and
exposes `/health` and `/ready` on its container-internal HTTP port.

Required configuration:

- `DATABASE_URL`
- `CDC_SLOT` (default `cdc_products_slot`)
- `CDC_PUBLICATION` (default `cdc_pub`)
- `REDIS_URL`
- `CDC_STREAM` (default `cdc_events`)
