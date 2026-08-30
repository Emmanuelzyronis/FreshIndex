"""Consume CDC events from Redis Streams and apply them to Meilisearch."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
        self.batch_size = int(os.environ.get("INDEXER_BATCH_SIZE", "50"))
        self.worker_count = int(os.environ.get("INDEXER_WORKERS", "4"))
        self.metrics_lock = threading.Lock()
        self.queue_depth = 0
        self.pending_claimed = 0
        self.processed_events = 0
        self.batch_count = 0
        self.product_task_count = 0
        self.marker_task_count = 0
        self.total_lock_wait_ms = 0.0
        self.active_workers = 0
        self.total_processing_ms = 0.0

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

    def current_document(self, document_id: Any) -> dict[str, Any] | None:
        try:
            document = self.index.get_document(str(document_id))
        except meilisearch.errors.MeilisearchApiError as exc:
            if exc.code == "document_not_found":
                return None
            raise
        return document if isinstance(document, dict) else dict(document.__dict__)

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

    def _lock_for(self, document_id: Any):
        return self.redis.lock(
            f"{self.stream}:document-lock:{document_id}",
            timeout=self.key_lock_seconds,
            blocking_timeout=self.key_lock_seconds,
        )

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
        cleanup_lock = self.redis.lock(
            f"{self.stream}:visibility-cleanup-lock",
            timeout=max(60, self.marker_cleanup_interval_seconds),
            blocking_timeout=0,
        )
        if not cleanup_lock.acquire(blocking=False):
            self.last_marker_cleanup = now
            return
        try:
            self._cleanup_visibility_markers(now)
        finally:
            cleanup_lock.release()

    def _cleanup_visibility_markers(self, now: float) -> None:
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
        dequeue_ts_us = time.time_ns() // 1_000
        try:
            fields["_dequeue_ts_us"] = str(dequeue_ts_us)
        except Exception:
            pass
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
                stream_queue_latency_ms=round(
                    max(0, dequeue_ts_us - int(envelope.get("published_ts_us", dequeue_ts_us))) / 1_000,
                    3,
                ),
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

    def process_batch(self, messages: list[tuple[str, dict[str, str]]]) -> None:
        """Apply independent events with one product and one marker task per batch.

        Events sharing a document are handled sequentially under the existing
        Redis lock. A batch with duplicate document IDs falls back to the
        proven single-event path so LSN ordering remains explicit.
        """
        if not messages:
            return
        batch_started = time.perf_counter_ns()
        envelopes = [(message_id, fields, json.loads(fields["event"])) for message_id, fields in messages]
        document_ids = [str(envelope["pk"]["id"]) for _, _, envelope in envelopes]
        if len(set(document_ids)) != len(document_ids):
            for message_id, fields, _ in envelopes:
                self.process_message(message_id, fields)
            return

        locks: list[Any] = []
        acquired: list[Any] = []
        try:
            ordered = sorted(
                envelopes, key=lambda item: str(item[2]["pk"]["id"])
            )
            for _, _, envelope in ordered:
                lock = self._lock_for(envelope["pk"]["id"])
                wait_started = time.perf_counter_ns()
                lock.acquire()
                with self.metrics_lock:
                    self.total_lock_wait_ms += (time.perf_counter_ns() - wait_started) / 1_000_000
                locks.append(lock)
                acquired.append(lock)

            products: list[dict[str, Any]] = []
            decisions: list[tuple[str, dict[str, str], dict[str, Any], str]] = []
            for message_id, fields, envelope in envelopes:
                document_id = envelope["pk"]["id"]
                current = self.current_document(document_id)
                current_lsn = current.get("_lsn") if current else None
                incoming_lsn = envelope["commit_lsn"]
                if current_lsn is not None and lsn_value(incoming_lsn) <= lsn_value(str(current_lsn)):
                    result = "superseded"
                else:
                    if envelope["op"] == "d":
                        document = {"id": document_id, "_lsn": incoming_lsn, "_deleted": True}
                    else:
                        document = dict(current or {})
                        document.update(envelope["after"] or {})
                        document.update({"_lsn": incoming_lsn, "_deleted": False})
                    products.append(document)
                    result = "applied"
                decisions.append((message_id, fields, envelope, result))

            if products:
                started = time.perf_counter_ns()
                task = self.index.add_documents(products, primary_key="id")
                self.client.wait_for_task(task.task_uid)
                with self.metrics_lock:
                    self.product_task_count += 1
                product_wait_ms = (time.perf_counter_ns() - started) / 1_000_000
            else:
                product_wait_ms = 0.0

            markers = [
                {
                    "event_id": envelope["event_id"],
                    "document_id": str(envelope["pk"]["id"]),
                    "op": envelope["op"],
                    "commit_lsn": envelope["commit_lsn"],
                    "commit_ts_us": envelope["commit_ts_us"],
                    "result": result,
                }
                for _, _, envelope, result in decisions
            ]
            started = time.perf_counter_ns()
            marker_task = self.visibility_index.add_documents(markers, primary_key="event_id")
            self.client.wait_for_task(marker_task.task_uid)
            marker_wait_ms = (time.perf_counter_ns() - started) / 1_000_000
            with self.metrics_lock:
                self.marker_task_count += 1
                self.processed_events += len(decisions)
                self.batch_count += 1

            for message_id, fields, envelope, result in decisions:
                self.redis.xack(self.stream, self.group, message_id)
                self.redis.hdel(self.retry_hash, message_id)
                dequeue_ts_us = int(fields.get("_dequeue_ts_us", time.time_ns() // 1_000))
                log(
                    "indexer_event",
                    message_id=message_id,
                    result=result,
                    document_id=envelope["pk"]["id"],
                    commit_lsn=envelope["commit_lsn"],
                    stream_queue_latency_ms=round(
                        max(0, dequeue_ts_us - int(envelope.get("published_ts_us", dequeue_ts_us))) / 1_000,
                        3,
                    ),
                    product_task_wait_ms=round(product_wait_ms, 3),
                    marker_task_wait_ms=round(marker_wait_ms, 3),
                    batch_size=len(decisions),
                    dequeue_to_processing_ms=round(
                        max(0, time.time_ns() // 1_000 - int(fields.get("_dequeue_ts_us", time.time_ns() // 1_000))) / 1_000,
                        3,
                    ),
                    batch_processing_ms=round(
                        (time.perf_counter_ns() - batch_started) / 1_000_000,
                        3,
                    ),
                )
            with self.metrics_lock:
                self.total_processing_ms += (time.perf_counter_ns() - batch_started) / 1_000_000
        except Exception:
            logging.exception(
                json.dumps(
                    {"event": "indexer_batch_error", "batch_size": len(envelopes)},
                    separators=(",", ":"),
                )
            )
        finally:
            for lock in reversed(acquired):
                try:
                    lock.release()
                except Exception:
                    pass

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
            fields["_dequeue_ts_us"] = str(time.time_ns() // 1_000)
            self.process_message(message_id, fields)
        with self.metrics_lock:
            self.pending_claimed += len(messages)
        return len(messages)

    def connect(self) -> None:
        self.redis.ping()
        self.ensure_group()
        self.ensure_index()
        self.ready = True
        self.last_error = None

    def metrics(self) -> dict[str, Any]:
        with self.metrics_lock:
            return {
                "batch_size": self.batch_size,
                "worker_count": self.worker_count,
                "queue_depth": self.queue_depth,
                "pending_claimed_total": self.pending_claimed,
                "processed_events_total": self.processed_events,
                "batches_total": self.batch_count,
                "product_tasks_total": self.product_task_count,
                "marker_tasks_total": self.marker_task_count,
                "lock_wait_ms_total": round(self.total_lock_wait_ms, 3),
                "processing_ms_total": round(self.total_processing_ms, 3),
                "active_workers": self.active_workers,
            }

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
                executor = ThreadPoolExecutor(max_workers=self.worker_count)
                while not self.stop_event.is_set():
                    if self.reclaim_pending():
                        continue
                    batches = self.redis.xreadgroup(
                        self.group,
                        self.consumer,
                        {self.stream: ">"},
                        count=self.batch_size,
                        block=1_000,
                    )
                    try:
                        pending = self.redis.xpending(self.stream, self.group)
                        with self.metrics_lock:
                            self.queue_depth = int(pending.get("pending", 0))
                    except Exception:
                        pass
                    for _, messages in batches:
                        dequeue_ts_us = time.time_ns() // 1_000
                        for _, fields in messages:
                            fields["_dequeue_ts_us"] = str(dequeue_ts_us)
                        with self.metrics_lock:
                            self.queue_depth = max(0, self.queue_depth - len(messages))
                        def run_batch(batch=messages):
                            with self.metrics_lock:
                                self.active_workers += 1
                            try:
                                self.process_batch(batch)
                            finally:
                                with self.metrics_lock:
                                    self.active_workers -= 1
                        executor.submit(run_batch)
                executor.shutdown(wait=True)
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
                    {"status": "ok" if indexer.ready else "unavailable", "error": indexer.last_error, **indexer.metrics()},
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
