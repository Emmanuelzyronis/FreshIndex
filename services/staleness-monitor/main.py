"""Independent CDC-to-Meilisearch staleness monitor."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import meilisearch
import psycopg2
from psycopg2.extras import LogicalReplicationConnection, StopReplication
from services.shared.pgoutput_decoder import PgoutputDecoder


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def log(event: str, **fields: Any) -> None:
    logging.info(json.dumps({"event": event, **fields}, separators=(",", ":")))


def lsn_value(lsn: str) -> int:
    high, low = lsn.split("/", 1)
    return (int(high, 16) << 32) | int(low, 16)


@dataclass
class Observation:
    table: str
    pk: str
    commit_lsn: str
    commit_ts_us: int
    observed_ts_us: int
    visible_ts_us: int | None = None
    violated: bool = False


class Monitor:
    def __init__(self) -> None:
        self.dsn = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/catalog")
        self.slot = os.environ.get("STALENESS_SLOT", "staleness_monitor_slot")
        self.publication = os.environ.get("CDC_PUBLICATION", "cdc_pub")
        self.meili = meilisearch.Client(
            os.environ.get("MEILISEARCH_URL", "http://localhost:7700"),
            os.environ.get("MEILI_MASTER_KEY"),
        )
        self.index = self.meili.index(os.environ.get("MEILI_INDEX", "products"))
        self.lock = threading.Lock()
        self.in_flight: dict[tuple[str, str, str], Observation] = {}
        self.samples: deque[float] = deque(maxlen=1000)
        self.violation_count = 0
        self.active_violations: dict[tuple[str, str, str], int] = {}

    def ensure_slot(self) -> None:
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", (self.slot,))
            if cur.fetchone() is None:
                cur.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (self.slot,))
                log("monitor_slot_created", slot=self.slot)

    def _decoder(self):
        database = psycopg2.extensions.parse_dsn(self.dsn).get("dbname", "postgres")
        return PgoutputDecoder(database)

    def observe(self) -> None:
        decoder = self._decoder()
        conn = psycopg2.connect(self.dsn, connection_factory=LogicalReplicationConnection)
        cursor = conn.cursor()

        def consume(message: Any) -> None:
            for event in decoder.feed(bytes(message.payload)):
                pk = event.get("pk", {}).get("id")
                table = event.get("source", {}).get("table")
                if pk is not None and table:
                    now = time.time_ns() // 1_000
                    item = Observation(table, str(pk), event["commit_lsn"], int(event["commit_ts_us"]), now)
                    with self.lock:
                        self.in_flight[(table, str(pk), item.commit_lsn)] = item
                    log("monitor_observed", table=table, pk=pk, commit_lsn=item.commit_lsn, commit_ts_us=item.commit_ts_us)
            message.cursor.send_feedback(flush_lsn=message.data_start)

        try:
            cursor.start_replication(slot_name=self.slot, options={"proto_version": "1", "publication_names": self.publication}, decode=False)
            cursor.consume_stream(consume)
        except StopReplication:
            pass

    def probe(self) -> None:
        now = time.time_ns() // 1_000
        with self.lock:
            items = list(self.in_flight.items())
        for key, item in items:
            try:
                document = self.index.get_document(item.pk)
                current = document.get("_lsn") if isinstance(document, dict) else getattr(document, "_lsn", None)
                visible = current is not None and lsn_value(str(current)) >= lsn_value(item.commit_lsn)
            except meilisearch.errors.MeilisearchApiError as exc:
                visible = False if exc.code == "document_not_found" else (_ for _ in ()).throw(exc)
            age_ms = (now - item.commit_ts_us) / 1_000
            if visible:
                item.visible_ts_us = now
                staleness_ms = (now - item.commit_ts_us) / 1_000
                with self.lock:
                    self.samples.append(staleness_ms)
                    self.in_flight.pop(key, None)
                    self.active_violations.pop(key, None)
                log("staleness_sample", table=item.table, pk=item.pk, commit_lsn=item.commit_lsn, staleness_ms=round(staleness_ms, 3))
            elif age_ms > 1000 and not item.violated:
                item.violated = True
                with self.lock:
                    self.violation_count += 1
                    self.active_violations[key] = now
                log("VIOLATION", table=item.table, pk=item.pk, commit_lsn=item.commit_lsn, commit_ts_us=item.commit_ts_us, age_ms=round(age_ms, 3))

    def metrics(self) -> dict[str, Any]:
        with self.lock:
            values = sorted(self.samples)
            def percentile(p: float) -> float | None:
                if not values:
                    return None
                return round(values[min(len(values) - 1, int((len(values) - 1) * p))], 3)
            now = time.time_ns() // 1_000
            ages = [(now - item.commit_ts_us) / 1_000 for item in self.in_flight.values()]
            duration = max(((now - started) / 1_000_000 for started in self.active_violations.values()), default=0.0)
            return {"p50_staleness_ms": percentile(.50), "p95_staleness_ms": percentile(.95), "p99_staleness_ms": percentile(.99), "in_flight_count": len(ages), "oldest_in_flight_age_ms": round(max(ages), 3) if ages else None, "violation_count": self.violation_count, "violation_duration_seconds": round(duration, 3)}

    def run(self) -> None:
        self.ensure_slot()
        threading.Thread(target=self.observe, daemon=True).start()
        log("monitor_started", slot=self.slot, host=socket.gethostname())
        while True:
            self.probe()
            time.sleep(0.12)


def serve(monitor: Monitor) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/staleness":
                body = json.dumps(monitor.metrics(), separators=(",", ":")).encode()
                self.send_response(200)
            elif self.path == "/health":
                metrics = monitor.metrics()
                stale = metrics["in_flight_count"] > 0 or metrics["violation_count"] > 0
                body = json.dumps({"status": "STALE" if stale else "ok", **metrics}, separators=(",", ":")).encode()
                self.send_response(200 if not stale else 503)
            else:
                self.send_response(404)
                body = b"{}"
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_: Any) -> None:
            return
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()


def main() -> int:
    configure_logging()
    monitor = Monitor()
    threading.Thread(target=serve, args=(monitor,), daemon=True).start()
    monitor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
