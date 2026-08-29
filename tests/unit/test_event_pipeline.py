from __future__ import annotations

import json
import importlib.util
import sys
import types
import unittest
from pathlib import Path


if importlib.util.find_spec("meilisearch") is None:
    meilisearch = types.ModuleType("meilisearch")
    meilisearch.errors = types.SimpleNamespace(MeilisearchApiError=RuntimeError)
    meilisearch.Client = object
    sys.modules["meilisearch"] = meilisearch

if importlib.util.find_spec("redis") is None:
    redis = types.ModuleType("redis")
    redis.Redis = object
    redis_exceptions = types.ModuleType("redis.exceptions")
    redis_exceptions.ResponseError = RuntimeError
    sys.modules["redis"] = redis
    sys.modules["redis.exceptions"] = redis_exceptions

if importlib.util.find_spec("psycopg2") is None:
    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.extras = types.ModuleType("psycopg2.extras")
    psycopg2.extras.LogicalReplicationConnection = object
    psycopg2.extras.StopReplication = RuntimeError
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = psycopg2.extras

from services.indexer.main import Indexer
from services.shared.pgoutput_decoder import Column, PgoutputDecoder, Relation, RowChange


monitor_path = Path(__file__).parents[2] / "services" / "staleness-monitor" / "main.py"
monitor_spec = importlib.util.spec_from_file_location("staleness_monitor_main", monitor_path)
assert monitor_spec and monitor_spec.loader
monitor_module = importlib.util.module_from_spec(monitor_spec)
sys.modules[monitor_spec.name] = monitor_module
monitor_spec.loader.exec_module(monitor_module)
Monitor = monitor_module.Monitor
Observation = monitor_module.Observation


class Task:
    task_uid = 1


class RecordingIndex:
    def __init__(self) -> None:
        self.batches: list[list[dict]] = []
        self.updated_batches: list[list[dict]] = []

    def add_documents(self, documents, primary_key=None):
        self.batches.append(documents)
        return Task()

    def update_documents(self, documents, primary_key=None):
        self.updated_batches.append(documents)
        return Task()


class RecordingClient:
    def wait_for_task(self, task_uid):
        return None


class RecordingRedis:
    def __init__(self) -> None:
        self.dead_letters = []
        self.acked = []
        self.deleted = []

    def hincrby(self, key, field, amount):
        return 5

    def xadd(self, stream, fields):
        self.dead_letters.append((stream, fields))

    def xack(self, stream, group, message_id):
        self.acked.append((stream, group, message_id))

    def hdel(self, key, field):
        self.deleted.append((key, field))


def envelope(op="d", lsn="0/20"):
    return {
        "event_id": "event-1",
        "op": op,
        "pk": {"id": 7},
        "after": None if op == "d" else {"id": 7, "name": "product"},
        "commit_lsn": lsn,
        "commit_ts_us": 123,
    }


class EventPipelineTest(unittest.TestCase):
    def indexer(self, stored_lsn=None):
        indexer = Indexer.__new__(Indexer)
        indexer.index = RecordingIndex()
        indexer.visibility_index = RecordingIndex()
        indexer.client = RecordingClient()
        indexer.stored_lsn = lambda document_id: stored_lsn
        return indexer

    def test_delete_is_versioned_and_gets_visibility_marker(self):
        indexer = self.indexer()

        result = indexer.apply(envelope())

        self.assertEqual(result, "applied")
        self.assertEqual(
            indexer.index.batches[0][0],
            {"id": 7, "_lsn": "0/20", "_deleted": True},
        )
        self.assertEqual(indexer.visibility_index.batches[0][0]["event_id"], "event-1")

    def test_older_event_cannot_overwrite_newer_tombstone(self):
        indexer = self.indexer(stored_lsn="0/30")

        result = indexer.apply(envelope(op="u", lsn="0/20"))

        self.assertEqual(result, "superseded")
        self.assertEqual(indexer.index.batches, [])
        self.assertEqual(indexer.visibility_index.batches[0][0]["result"], "superseded")

    def test_update_preserves_fields_omitted_by_pgoutput(self):
        indexer = self.indexer(stored_lsn="0/10")

        result = indexer.apply(envelope(op="u", lsn="0/20"))

        self.assertEqual(result, "applied")
        self.assertEqual(indexer.index.batches, [])
        self.assertEqual(indexer.index.updated_batches[0][0]["_deleted"], False)

    def test_monitor_matches_an_immutable_marker(self):
        observation = Observation("event-1", "products", "7", "d", "0/20", 123, 124)

        self.assertTrue(
            Monitor.marker_visible(
                observation, {"event_id": "event-1", "commit_lsn": "0/20"}
            )
        )
        self.assertFalse(
            Monitor.marker_visible(
                observation, {"event_id": "event-2", "commit_lsn": "0/20"}
            )
        )

    def test_percentile_does_not_underreport_small_samples(self):
        self.assertEqual(Monitor.percentile([1.0, 100.0], 0.99), 100.0)

    def test_poison_event_is_dead_lettered_after_max_attempts(self):
        indexer = Indexer.__new__(Indexer)
        indexer.redis = RecordingRedis()
        indexer.stream = "cdc_events"
        indexer.group = "indexers"
        indexer.dead_letter_stream = "cdc_events_dlq"
        indexer.retry_hash = "cdc_events:retries"
        indexer.max_attempts = 5
        indexer.last_error = None
        indexer.apply = lambda event: (_ for _ in ()).throw(ValueError("invalid event"))

        indexer.process_message("1-0", {"event": json.dumps(envelope())})

        self.assertEqual(indexer.redis.acked, [("cdc_events", "indexers", "1-0")])
        self.assertEqual(indexer.redis.dead_letters[0][0], "cdc_events_dlq")

    def test_event_id_is_deterministic_for_wal_position(self):
        decoder = PgoutputDecoder("catalog")
        decoder.xid = 42
        relation = Relation(1, "public", "products", (Column("id", 20, True),))
        change = RowChange("c", relation, {"id": 7}, None, 100)

        first = decoder._committed_event(change, 32, 200, 0)
        second = decoder._committed_event(change, 32, 200, 0)

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(first["event_id"]), 64)


if __name__ == "__main__":
    unittest.main()
