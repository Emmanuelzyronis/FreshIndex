# Replication consumer skeleton

`reader.py` opens the existing `cdc_products_slot` with pgoutput protocol v1,
tracks relation metadata, buffers row changes until the transaction commit, and
prints committed changes as JSON diagnostics.

It is intentionally not yet a Redis publisher. In particular, it does not
invent `event_id` or `published_ts_us`; the latter must be captured at a real
`XADD` in the indexer pipeline stage.

Run it with:

```bash
python reader.py \
  --dsn postgresql://postgres:postgres@localhost:5432/catalog \
  --slot cdc_products_slot \
  --publication cdc_pub
```

Use `--stop-after 1` for a one-event smoke check.

