"""Consume CDCEvent envelopes from Redis Streams and apply them to Meilisearch."""

from __future__ import annotations

import json
import logging
import os
import socket
import time
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
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self.client = meilisearch.Client(meili_url, os.environ.get("MEILI_MASTER_KEY"))
        self.index = self.client.index(os.environ.get("MEILI_INDEX", "products"))

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
        incoming_lsn = envelope["commit_lsn"]
        current_lsn = self.stored_lsn(document_id)
        if current_lsn is not None and lsn_value(incoming_lsn) <= lsn_value(current_lsn):
            return "ignored"

        started_ns = time.perf_counter_ns()
        if envelope["op"] == "d":
            task = self.index.delete_document(str(document_id))
        else:
            document = dict(envelope["after"])
            document["_lsn"] = incoming_lsn
            task = self.index.add_documents([document], primary_key="id")
        self.client.wait_for_task(task.task_uid)
        latency_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
        log(
            "indexer_write",
            document_id=document_id,
            op=envelope["op"],
            commit_lsn=incoming_lsn,
            indexer_write_latency_ms=round(latency_ms, 3),
        )
        return "applied"

    def run(self) -> None:
        self.ensure_group()
        log("indexer_started", stream=self.stream, group=self.group, consumer=self.consumer)
        while True:
            batches = self.redis.xreadgroup(
                self.group,
                self.consumer,
                {self.stream: ">"},
                count=10,
                block=5_000,
            )
            for _, messages in batches:
                for message_id, fields in messages:
                    try:
                        envelope = json.loads(fields["event"])
                        result = self.apply(envelope)
                        self.redis.xack(self.stream, self.group, message_id)
                        log(
                            "indexer_event",
                            message_id=message_id,
                            result=result,
                            document_id=envelope["pk"]["id"],
                            commit_lsn=envelope["commit_lsn"],
                        )
                    except Exception:
                        logging.exception(
                            json.dumps(
                                {"event": "indexer_error", "message_id": message_id},
                                separators=(",", ":"),
                            )
                        )


def main() -> int:
    configure_logging()
    Indexer().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
