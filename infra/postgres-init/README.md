# Postgres initialization

Run the numbered SQL files in lexical order after Postgres starts with:

```text
wal_level=logical
track_commit_timestamp=on
```

The scripts create the `products` table, the `cdc_pub` publication, and the
persistent `cdc_products_slot` pgoutput slot. The slot begins retaining WAL as
soon as it is created, so it must be consumed or explicitly dropped in any
long-lived environment.

Postgres startup configuration is intentionally not attempted from the SQL
scripts: `wal_level` requires a server restart, and a logical slot cannot be
created until the server is already running with logical WAL enabled. Pass the
settings as Postgres server arguments (as shown in the stage-one verification
commands) or set them in the eventual Compose service.

