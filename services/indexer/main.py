"""Consume CDC events from Redis Streams and apply them to Meilisearch."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import time
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import meilisearch
import redis
from redis.exceptions import ResponseError


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


class Indexer:
    def __init__(self) -> None:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        meili_url = os.environ.get("MEILISEARCH_URL", "http://localhost:7700")
        self.stream = os.environ.get("CDC_STREAM", "cdc_events")
        self.group = os.environ.get("REDIS_CONSUMER_GROUP", "indexers")
        self.consumer = os.environ.get("REDIS_CONSUMER_NAME", socket.gethostname())
        self.dead_letter_stream = os.environ.get("CDC_DLQ_STREAM", f"{self.stream}_dlq")
        self.retry_hash = os.environ.get("CDC_RETRY_HASH", f"{self.stream}:retries")
        self.max_attempts = int(os.environ.get("CDC_MAX_ATTEMPTS", "5"))
        self.claim_idle_ms = int(os.environ.get("CDC_CLAIM_IDLE_MS", "30000"))
        self.key_lock_seconds = int(os.environ.get("CDC_KEY_LOCK_SECONDS", "120"))
        self.processing_delay_ms = int(os.environ.get("INDEXER_PROCESSING_DELAY_MS", "0"))
        self.marker_retention_seconds = int(
            os.environ.get("VISIBILITY_MARKER_RETENTION_SECONDS", "604800")
        )
        self.marker_cleanup_interval_seconds = int(
            os.environ.get("VISIBILITY_MARKER_CLEANUP_SECONDS", "3600")
        )
        self.last_marker_cleanup = 0.0
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self.client = meilisearch.Client(meili_url, os.environ.get("MEILI_MASTER_KEY"))
        self.index_name = os.environ.get("MEILI_INDEX", "products")
        self.visibility_index_name = os.environ.get(
            "MEILI_VISIBILITY_INDEX", "cdc_visibility"
        )
        self.index: Any = None
        self.visibility_index: Any = None
        self.stop_event = threading.Event()
        self.ready = False
        self.last_error: str | None = None

    def ensure_index(self) -> None:
        try:
            self.client.get_index(self.index_name)
        except meilisearch.errors.MeilisearchApiError as exc:
            if exc.code != "index_not_found":
                raise
            task = self.client.create_index(self.index_name, {"primaryKey": "id"})
            self.client.wait_for_task(task.task_uid)
            log("indexer_index_created", index=self.index_name, primary_key="id")

        self.index = self.client.index(self.index_name)
        task = self.index.update_filterable_attributes(["_lsn", "_deleted"])
        self.client.wait_for_task(task.task_uid)
        log(
            "indexer_index_configured",
            index=self.index_name,
            filterable_attributes=["_lsn", "_deleted"],
        )
        try:
            self.client.get_index(self.visibility_index_name)
        except meilisearch.errors.MeilisearchApiError as exc:
            if exc.code != "index_not_found":
                raise
            task = self.client.create_index(
                self.visibility_index_name, {"primaryKey": "event_id"}
            )
            self.client.wait_for_task(task.task_uid)
            log(
                "indexer_index_created",
                index=self.visibility_index_name,
                primary_key="event_id",
            )
        self.visibility_index = self.client.index(self.visibility_index_name)
        task = self.visibility_index.update_filterable_attributes(["commit_ts_us"])
        self.client.wait_for_task(task.task_uid)

    def ensure_group(self) -> None:
        try:
            self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def stored_lsn(self, document_id: Any) -> str | None:
        try:
            document = self.index.get_document(str(document_id))
        except meilisearch.errors.MeilisearchApiError as exc:
            if exc.code == "document_not_found":
                return None
            raise
        if isinstance(document, dict):
            return document.get("_lsn")
        return getattr(document, "_lsn", None)

    def apply(self, envelope: dict[str, Any]) -> str:
        document_id = envelope["pk"]["id"]
        lock = (
            self.redis.lock(
                f"{self.stream}:document-lock:{document_id}",
                timeout=self.key_lock_seconds,
                blocking_timeout=self.key_lock_seconds,
            )
            if hasattr(self, "redis")
            else nullcontext()
        )
        with lock:
            return self._apply_locked(envelope)

    def _apply_locked(self, envelope: dict[str, Any]) -> str:
        document_id = envelope["pk"]["id"]
        incoming_lsn = envelope["commit_lsn"]
        current_lsn = self.stored_lsn(document_id)
        if current_lsn is not None and lsn_value(incoming_lsn) <= lsn_value(current_lsn):
            result = "superseded"
        else:
            if envelope["op"] == "d":
                document = {
                    "id": document_id,
                    "_lsn": incoming_lsn,
                    "_deleted": True,
                }
            else:
                document = dict(envelope["after"])
                document["_lsn"] = incoming_lsn
                document["_deleted"] = False

        started_ns = time.perf_counter_ns()
        delay_ms = getattr(self, "processing_delay_ms", 0)
        if delay_ms:
            time.sleep(delay_ms / 1000)
        if current_lsn is None or lsn_value(incoming_lsn) > lsn_value(current_lsn):
            if envelope["op"] == "u":
                task = self.index.update_documents([document], primary_key="id")
            else:
                task = self.index.add_documents([document], primary_key="id")
            self.client.wait_for_task(task.task_uid)
            result = "applied"

        marker = {
            "event_id": envelope["event_id"],
            "document_id": str(document_id),
            "op": envelope["op"],
            "commit_lsn": incoming_lsn,
            "commit_ts_us": envelope["commit_ts_us"],
            "result": result,
        }
        marker_task = self.visibility_index.add_documents(
            [marker], primary_key="event_id"
        )
        self.client.wait_for_task(marker_task.task_uid)
        latency_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
        log(
            "indexer_write",
            document_id=document_id,
            op=envelope["op"],
            commit_lsn=incoming_lsn,
            indexer_write_latency_ms=round(latency_ms, 3),
        )
        return result

    def cleanup_visibility_markers(self) -> None:
        now = time.monotonic()
        if now - self.last_marker_cleanup < self.marker_cleanup_interval_seconds:
            return
        cutoff_us = (time.time_ns() // 1_000) - self.marker_retention_seconds * 1_000_000
        deleted = 0
        for _ in range(100):
            result = self.visibility_index.search(
                "", {"filter": f"commit_ts_us < {cutoff_us}", "limit": 1000}
            )
            hits = result.get("hits", []) if isinstance(result, dict) else []
            event_ids = [hit["event_id"] for hit in hits]
            if not event_ids:
                break
            task = self.visibility_index.delete_documents(event_ids)
            self.client.wait_for_task(task.task_uid)
            deleted += len(event_ids)
        if deleted:
            log("visibility_markers_deleted", count=deleted, cutoff_ts_us=cutoff_us)
        self.last_marker_cleanup = now

    def process_message(self, message_id: str, fields: dict[str, str]) -> None:
        try:
            envelope = json.loads(fields["event"])
            result = self.apply(envelope)
            try:
                self.cleanup_visibility_markers()
            except Exception:
                logging.exception(
                    json.dumps(
                        {"event": "visibility_marker_cleanup_error"},
                        separators=(",", ":"),
                    )
                )
            self.redis.xack(self.stream, self.group, message_id)
            self.redis.hdel(self.retry_hash, message_id)
            log(
                "indexer_event",
                message_id=message_id,
                result=result,
                document_id=envelope["pk"]["id"],
                commit_lsn=envelope["commit_lsn"],
            )
        except Exception as exc:
            attempts = self.redis.hincrby(self.retry_hash, message_id, 1)
            self.last_error = str(exc)
            logging.exception(
                json.dumps(
                    {
                        "event": "indexer_error",
                        "message_id": message_id,
                        "attempt": attempts,
                    },
                    separators=(",", ":"),
                )
            )
            if attempts >= self.max_attempts:
                self.redis.xadd(
                    self.dead_letter_stream,
                    {
                        "source_stream": self.stream,
                        "source_message_id": message_id,
                        "event": fields.get("event", ""),
                        "error": str(exc),
                        "attempts": str(attempts),
                        "failed_ts_us": str(time.time_ns() // 1_000),
                    },
                )
                self.redis.xack(self.stream, self.group, message_id)
                self.redis.hdel(self.retry_hash, message_id)
                log("indexer_dead_lettered", message_id=message_id, attempts=attempts)

    def reclaim_pending(self) -> int:
        claimed = self.redis.xautoclaim(
            self.stream,
            self.group,
            self.consumer,
            min_idle_time=self.claim_idle_ms,
            start_id="0-0",
            count=10,
        )
        messages = claimed[1] if len(claimed) > 1 else []
        for message_id, fields in messages:
            self.process_message(message_id, fields)
        return len(messages)

    def connect(self) -> None:
        self.redis.ping()
        self.ensure_group()
        self.ensure_index()
        self.ready = True
        self.last_error = None

    def run(self) -> None:
        retry_seconds = 1.0
        while not self.stop_event.is_set():
            try:
                self.connect()
                log(
                    "indexer_started",
                    stream=self.stream,
                    group=self.group,
                    consumer=self.consumer,
                )
                retry_seconds = 1.0
                while not self.stop_event.is_set():
                    if self.reclaim_pending():
                        continue
                    batches = self.redis.xreadgroup(
                        self.group,
                        self.consumer,
                        {self.stream: ">"},
                        count=10,
                        block=1_000,
                    )
                    for _, messages in batches:
                        for message_id, fields in messages:
                            self.process_message(message_id, fields)
            except Exception as exc:
                self.ready = False
                self.last_error = str(exc)
                logging.exception(
                    json.dumps({"event": "indexer_connection_error"}, separators=(",", ":"))
                )
                self.stop_event.wait(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 30.0)


def serve(indexer: Indexer) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in ("/health", "/ready"):
                self.send_response(404)
                body = b"{}"
            else:
                status = 200 if indexer.ready else 503
                body = json.dumps(
                    {"status": "ok" if indexer.ready else "unavailable", "error": indexer.last_error},
                    separators=(",", ":"),
                ).encode()
                self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8081"))), Handler)
    server.timeout = 0.5
    while not indexer.stop_event.is_set():
        server.handle_request()
    server.server_close()


def main() -> int:
    configure_logging()
    indexer = Indexer()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: indexer.stop_event.set())
    threading.Thread(target=serve, args=(indexer,), daemon=True).start()
    indexer.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
