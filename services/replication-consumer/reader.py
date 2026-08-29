"""Read pgoutput logical replication and publish CDC events to Redis Streams."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg2
import redis
from psycopg2.extras import LogicalReplicationConnection, StopReplication

from services.shared.pgoutput_decoder import PgoutputDecoder


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Decode and publish a pgoutput slot")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--slot", default=os.environ.get("CDC_SLOT", "cdc_products_slot"))
    parser.add_argument("--publication", default=os.environ.get("CDC_PUBLICATION", "cdc_pub"))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    parser.add_argument("--stream", default=os.environ.get("CDC_STREAM", "cdc_events"))
    parser.add_argument("--stop-after", type=int, default=0)
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn or DATABASE_URL is required")
    return args


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def log(event: str, **fields: Any) -> None:
    logging.info(json.dumps({"event": event, **fields}, separators=(",", ":")))


class Reader:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.stop_event = threading.Event()
        self.ready = False
        self.last_error: str | None = None
        self.emitted = 0
        self.redis = redis.Redis.from_url(args.redis_url, decode_responses=True)

    def consume_once(self) -> None:
        database = psycopg2.extensions.parse_dsn(self.args.dsn).get("dbname", "postgres")
        decoder = PgoutputDecoder(database)
        self.redis.ping()
        connection = psycopg2.connect(
            self.args.dsn,
            connection_factory=LogicalReplicationConnection,
            application_name="cdc-reader",
        )
        cursor = connection.cursor()

        def consume(message: Any) -> None:
            if self.stop_event.is_set():
                raise StopReplication
            payload = bytes(message.payload)
            for event in decoder.feed(payload):
                event["published_ts_us"] = time.time_ns() // 1_000
                envelope = json.dumps(event, separators=(",", ":"))
                message_id = self.redis.xadd(self.args.stream, {"event": envelope})
                log(
                    "cdc_event_published",
                    message_id=message_id,
                    commit_lsn=event["commit_lsn"],
                    table=event["source"]["table"],
                    op=event["op"],
                )
                self.emitted += 1
            if payload[:1] == b"C":
                message.cursor.send_feedback(flush_lsn=message.data_start)
            if self.args.stop_after and self.emitted >= self.args.stop_after:
                self.stop_event.set()
                raise StopReplication

        try:
            cursor.start_replication(
                slot_name=self.args.slot,
                options={"proto_version": "1", "publication_names": self.args.publication},
                decode=False,
                status_interval=5,
            )
            self.ready = True
            self.last_error = None
            log("cdc_reader_started", slot=self.args.slot, stream=self.args.stream)
            cursor.consume_stream(consume, keepalive_interval=1)
        except StopReplication:
            pass
        finally:
            self.ready = False
            cursor.close()
            connection.close()

    def run(self) -> None:
        retry_seconds = 1.0
        while not self.stop_event.is_set():
            try:
                self.consume_once()
                retry_seconds = 1.0
            except Exception as exc:
                self.ready = False
                self.last_error = str(exc)
                logging.exception(
                    json.dumps({"event": "cdc_reader_connection_error"}, separators=(",", ":"))
                )
                self.stop_event.wait(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 30.0)


def serve(reader: Reader) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in ("/health", "/ready"):
                self.send_response(404)
                body = b"{}"
            else:
                status = 200 if reader.ready else 503
                body = json.dumps(
                    {
                        "status": "ok" if reader.ready else "unavailable",
                        "events_published": reader.emitted,
                        "error": reader.last_error,
                    },
                    separators=(",", ":"),
                ).encode()
                self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8082"))), Handler)
    server.timeout = 0.5
    while not reader.stop_event.is_set():
        server.handle_request()
    server.server_close()


def main() -> int:
    configure_logging()
    reader = Reader(arguments())
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: reader.stop_event.set())
    threading.Thread(target=serve, args=(reader,), daemon=True).start()
    reader.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
