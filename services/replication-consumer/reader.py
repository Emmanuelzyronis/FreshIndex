"""Minimal pgoutput reader and Redis Streams publisher."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import psycopg2
import redis
from psycopg2.extras import LogicalReplicationConnection, StopReplication
from services.shared.pgoutput_decoder import PgoutputDecoder


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Decode a pgoutput replication slot")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--slot", default=os.environ.get("CDC_SLOT", "cdc_products_slot"))
    parser.add_argument("--publication", default=os.environ.get("CDC_PUBLICATION", "cdc_pub"))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    parser.add_argument("--stream", default=os.environ.get("CDC_STREAM", "cdc_events"))
    parser.add_argument(
        "--stop-after",
        type=int,
        default=0,
        help="stop after this many committed row changes; zero runs forever",
    )
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn or DATABASE_URL is required")
    return args


def main() -> int:
    args = arguments()
    database = psycopg2.extensions.parse_dsn(args.dsn).get("dbname", "postgres")
    decoder = PgoutputDecoder(database)
    redis_client = redis.Redis.from_url(args.redis_url, decode_responses=True)
    emitted = 0

    connection = psycopg2.connect(
        args.dsn, connection_factory=LogicalReplicationConnection
    )
    cursor = connection.cursor()

    def consume(message: Any) -> None:
        nonlocal emitted
        for event in decoder.feed(bytes(message.payload)):
            event["published_ts_us"] = time.time_ns() // 1_000
            envelope = json.dumps(event, separators=(",", ":"))
            redis_client.xadd(args.stream, {"event": envelope})
            print(envelope, flush=True)
            emitted += 1
        message.cursor.send_feedback(flush_lsn=message.data_start)
        if args.stop_after and emitted >= args.stop_after:
            raise StopReplication

    try:
        cursor.start_replication(
            slot_name=args.slot,
            options={"proto_version": "1", "publication_names": args.publication},
            decode=False,
        )
        cursor.consume_stream(consume)
    except StopReplication:
        pass
    finally:
        cursor.close()
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
