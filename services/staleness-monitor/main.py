"""Independently measure PostgreSQL commit-to-Meilisearch visibility."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
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
    event_id: str
    table: str
    pk: str
    op: str
    commit_lsn: str
    commit_ts_us: int
    observed_ts_us: int
    visible_ts_us: int | None = None
    violated: bool = False


class Monitor:
    def __init__(self) -> None:
        self.dsn = os.environ.get(
            "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/catalog"
        )
        self.slot = os.environ.get("STALENESS_SLOT", "staleness_monitor_slot")
        self.publication = os.environ.get("CDC_PUBLICATION", "cdc_pub")
        self.meili = meilisearch.Client(
            os.environ.get("MEILISEARCH_URL", "http://localhost:7700"),
            os.environ.get("MEILI_MASTER_KEY"),
        )
        self.index = self.meili.index(os.environ.get("MEILI_INDEX", "products"))
        self.visibility_index = self.meili.index(
            os.environ.get("MEILI_VISIBILITY_INDEX", "cdc_visibility")
        )
        self.slo_ms = float(os.environ.get("STALENESS_SLO_MS", "1000"))
        self.poll_seconds = float(os.environ.get("STALENESS_POLL_SECONDS", "0.12"))
        self.lock = threading.Lock()
        self.in_flight: dict[tuple[str, str, str, str], Observation] = {}
        self.samples: deque[float] = deque(maxlen=int(os.environ.get("SAMPLE_WINDOW", "10000")))
        self.detection_delays: deque[float] = deque(maxlen=10000)
        self.violation_count = 0
        self.active_violations: dict[tuple[str, str, str, str], int] = {}
        self.stop_event = threading.Event()
        self.replication_ready = False
        self.probe_ready = False
        self.last_error: str | None = None
        self.pending_commits: deque[tuple[int, list[tuple[str, str, str, str]]]] = deque()
        self.replication_cursor: Any = None
        self.replication_lock = threading.Lock()

    def ensure_slot(self) -> None:
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", (self.slot,))
            if cur.fetchone() is None:
                cur.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (self.slot,))
                log("monitor_slot_created", slot=self.slot)

    def _decoder(self) -> PgoutputDecoder:
        database = psycopg2.extensions.parse_dsn(self.dsn).get("dbname", "postgres")
        return PgoutputDecoder(database)

    def observe_once(self) -> None:
        decoder = self._decoder()
        conn = psycopg2.connect(
            self.dsn,
            connection_factory=LogicalReplicationConnection,
            application_name="staleness-monitor",
        )
        cursor = conn.cursor()

        def consume(message: Any) -> None:
            if self.stop_event.is_set():
                raise StopReplication
            event_keys: list[tuple[str, str, str, str]] = []
            payload = bytes(message.payload)
            for event in decoder.feed(payload):
                pk = event.get("pk", {}).get("id")
                table = event.get("source", {}).get("table")
                if pk is not None and table:
                    now = time.time_ns() // 1_000
                    item = Observation(
                        event_id=event["event_id"],
                        table=table,
                        pk=str(pk),
                        op=event["op"],
                        commit_lsn=event["commit_lsn"],
                        commit_ts_us=int(event["commit_ts_us"]),
                        observed_ts_us=now,
                    )
                    key = (table, str(pk), item.commit_lsn, item.event_id)
                    with self.lock:
                        self.in_flight[key] = item
                    event_keys.append(key)
                    log(
                        "monitor_observed",
                        table=table,
                        pk=pk,
                        op=item.op,
                        commit_lsn=item.commit_lsn,
                        commit_ts_us=item.commit_ts_us,
                    )
            if payload[:1] == b"C":
                with self.lock:
                    self.pending_commits.append((message.data_start, event_keys))
                if not event_keys:
                    with self.replication_lock:
                        message.cursor.send_feedback(flush_lsn=message.data_start)

        try:
            cursor.start_replication(
                slot_name=self.slot,
                options={"proto_version": "1", "publication_names": self.publication},
                decode=False,
                status_interval=5,
            )
            self.replication_ready = True
            self.replication_cursor = cursor
            self.last_error = None
            log("monitor_replication_started", slot=self.slot, host=socket.gethostname())
            cursor.consume_stream(consume, keepalive_interval=1)
        except StopReplication:
            pass
        finally:
            self.replication_ready = False
            self.replication_cursor = None
            with self.lock:
                self.pending_commits.clear()
            cursor.close()
            conn.close()

    def observe(self) -> None:
        retry_seconds = 1.0
        while not self.stop_event.is_set():
            try:
                self.ensure_slot()
                self.observe_once()
                retry_seconds = 1.0
            except Exception as exc:
                self.replication_ready = False
                self.last_error = str(exc)
                logging.exception(
                    json.dumps({"event": "monitor_replication_error"}, separators=(",", ":"))
                )
                self.stop_event.wait(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 30.0)

    @staticmethod
    def marker_visible(item: Observation, marker: Any) -> bool:
        event_id = marker.get("event_id") if isinstance(marker, dict) else getattr(marker, "event_id", None)
        commit_lsn = marker.get("commit_lsn") if isinstance(marker, dict) else getattr(marker, "commit_lsn", None)
        return event_id == item.event_id and commit_lsn == item.commit_lsn

    def probe(self) -> None:
        self.meili.health()
        now = time.time_ns() // 1_000
        with self.lock:
            items = list(self.in_flight.items())
        for key, item in items:
            age_ms = (now - item.commit_ts_us) / 1_000
            if age_ms < -60_000 or age_ms > 86_400_000:
                log(
                    "monitor_invalid_commit_timestamp",
                    table=item.table,
                    pk=item.pk,
                    commit_lsn=item.commit_lsn,
                    commit_ts_us=item.commit_ts_us,
                    now_ts_us=now,
                    age_ms=round(age_ms, 3),
                )
                raise RuntimeError(
                    f"invalid commit timestamp for event {item.event_id}: age_ms={age_ms}"
                )
            try:
                marker = self.visibility_index.get_document(item.event_id)
                visible = self.marker_visible(item, marker)
                if not visible:
                    log(
                        "monitor_probe_wrong_marker",
                        table=item.table,
                        pk=item.pk,
                        op=item.op,
                        commit_lsn=item.commit_lsn,
                        event_id=item.event_id,
                    )
            except meilisearch.errors.MeilisearchApiError as exc:
                if exc.code != "document_not_found":
                    raise
                visible = False
                log(
                    "monitor_probe_missing_document",
                    table=item.table,
                    pk=item.pk,
                    op=item.op,
                    commit_lsn=item.commit_lsn,
                    event_id=item.event_id,
                )

            if visible:
                item.visible_ts_us = now
                staleness_ms = (now - item.commit_ts_us) / 1_000
                with self.lock:
                    self.samples.append(staleness_ms)
                    self.in_flight.pop(key, None)
                    self.active_violations.pop(key, None)
                log(
                    "staleness_sample",
                    table=item.table,
                    pk=item.pk,
                    op=item.op,
                    commit_lsn=item.commit_lsn,
                    staleness_ms=round(staleness_ms, 3),
                )
            elif age_ms > self.slo_ms and not item.violated:
                item.violated = True
                detection_delay_ms = age_ms - self.slo_ms
                with self.lock:
                    self.violation_count += 1
                    self.detection_delays.append(detection_delay_ms)
                    self.active_violations[key] = now
                log(
                    "VIOLATION",
                    table=item.table,
                    pk=item.pk,
                    op=item.op,
                    commit_lsn=item.commit_lsn,
                    commit_ts_us=item.commit_ts_us,
                    age_ms=round(age_ms, 3),
                    detection_delay_ms=round(detection_delay_ms, 3),
                )
        self.probe_ready = True
        self.last_error = None

    @staticmethod
    def percentile(values: list[float], quantile: float) -> float | None:
        if not values:
            return None
        rank = max(0, min(len(values) - 1, math.ceil(quantile * len(values)) - 1))
        return round(values[rank], 3)

    def metrics(self) -> dict[str, Any]:
        with self.lock:
            values = sorted(self.samples)
            delays = sorted(self.detection_delays)
            now = time.time_ns() // 1_000
            ages = [(now - item.commit_ts_us) / 1_000 for item in self.in_flight.values()]
            duration = max(
                ((now - started) / 1_000_000 for started in self.active_violations.values()),
                default=0.0,
            )
            return {
                "sample_count": len(values),
                "p50_staleness_ms": self.percentile(values, 0.50),
                "p95_staleness_ms": self.percentile(values, 0.95),
                "p99_staleness_ms": self.percentile(values, 0.99),
                "max_staleness_ms": round(max(values), 3) if values else None,
                "in_flight_count": len(ages),
                "oldest_in_flight_age_ms": round(max(ages), 3) if ages else None,
                "violation_count": self.violation_count,
                "active_violation_count": len(self.active_violations),
                "p99_detection_delay_ms": self.percentile(delays, 0.99),
                "violation_duration_seconds": round(duration, 3),
                "replication_ready": self.replication_ready,
                "probe_ready": self.probe_ready,
            }

    def prometheus_metrics(self) -> str:
        metrics = self.metrics()
        mapping = {
            "sample_count": "cdc_staleness_samples_total",
            "p50_staleness_ms": "cdc_staleness_p50_milliseconds",
            "p95_staleness_ms": "cdc_staleness_p95_milliseconds",
            "p99_staleness_ms": "cdc_staleness_p99_milliseconds",
            "max_staleness_ms": "cdc_staleness_max_milliseconds",
            "in_flight_count": "cdc_staleness_in_flight",
            "oldest_in_flight_age_ms": "cdc_staleness_oldest_in_flight_milliseconds",
            "violation_count": "cdc_staleness_violations_total",
            "active_violation_count": "cdc_staleness_active_violations",
            "p99_detection_delay_ms": "cdc_staleness_detection_delay_p99_milliseconds",
            "violation_duration_seconds": "cdc_staleness_violation_duration_seconds",
        }
        return "".join(
            f"{metric_name} {metrics[key] if metrics[key] is not None else 'NaN'}\n"
            for key, metric_name in mapping.items()
        )

    def run(self) -> None:
        threading.Thread(target=self.probe_loop, daemon=True).start()
        threading.Thread(target=self.ack_loop, daemon=True).start()
        log("monitor_started", slot=self.slot, host=socket.gethostname())
        self.observe()

    def probe_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.probe()
            except Exception as exc:
                self.probe_ready = False
                self.last_error = str(exc)
                logging.exception(
                    json.dumps({"event": "monitor_probe_error"}, separators=(",", ":"))
                )
            self.stop_event.wait(self.poll_seconds)

    def ack_loop(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                while self.pending_commits and all(
                    key not in self.in_flight for key in self.pending_commits[0][1]
                ):
                    flush_lsn, _ = self.pending_commits.popleft()
                    cursor = self.replication_cursor
                    if cursor is not None:
                        with self.replication_lock:
                            cursor.send_feedback(flush_lsn=flush_lsn)
            self.stop_event.wait(self.poll_seconds)


def serve(monitor: Monitor) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            content_type = "application/json"
            if self.path == "/staleness":
                body = json.dumps(monitor.metrics(), separators=(",", ":")).encode()
                status = 200
            elif self.path == "/metrics":
                body = monitor.prometheus_metrics().encode()
                content_type = "text/plain; version=0.0.4"
                status = 200
            elif self.path in ("/health", "/ready"):
                metrics = monitor.metrics()
                ready = metrics["replication_ready"] and metrics["probe_ready"]
                healthy = ready and metrics["active_violation_count"] == 0
                body = json.dumps(
                    {
                        "status": "ok" if healthy else "STALE" if ready else "unavailable",
                        "error": monitor.last_error,
                        **metrics,
                    },
                    separators=(",", ":"),
                ).encode()
                status = 200 if healthy else 503
            else:
                body = b"{}"
                status = 404
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    server.timeout = 0.5
    while not monitor.stop_event.is_set():
        server.handle_request()
    server.server_close()


def main() -> int:
    configure_logging()
    monitor = Monitor()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: monitor.stop_event.set())
    threading.Thread(target=serve, args=(monitor,), daemon=True).start()
    monitor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
