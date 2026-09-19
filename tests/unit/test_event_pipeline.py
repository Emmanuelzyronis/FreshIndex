from __future__ import annotations

import importlib.util
import json
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

    def xadd(self, stream, fields, **kwargs):
        self.dead_letters.append((stream, fields))

    def xack(self, stream, group, message_id):
        self.acked.append((stream, group, message_id))

    def hdel(self, key, field):
        self.deleted.append((key, field))

    def lock(self, *args, **kwargs):
        return LocalLock()


class SdkDocumentLike:
    """Mimic meilisearch-python 0.31's Document model surface.

    The SDK stores the raw payload under a mangled private slot
    (_Document__doc) and copies each document field onto the instance, so its
    __dict__ mixes real fields with internal state.
    """

    def __init__(self, fields):
        self.__dict__["_Document__doc"] = dict(fields)
        self.__dict__.update(fields)


class GetDocumentIndex(RecordingIndex):
    def __init__(self, current):
        super().__init__()
        self.current = current

    def get_document(self, document_id):
        if str(document_id) == "7":
            return self.current
        raise RuntimeError("unexpected document id in test")


class LocalLock:
    def acquire(self):
        return True

    def release(self):
        return None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


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
        indexer.redis = RecordingRedis()
        indexer.stream = "cdc_events"
        indexer.group = "indexers"
        indexer.retry_hash = "cdc_events:retries"
        indexer.dead_letter_stream = "cdc_events_dlq"
        indexer.key_lock_seconds = 120
        import threading
        indexer.metrics_lock = threading.Lock()
        indexer.total_lock_wait_ms = 0.0
        indexer.product_task_count = 0
        indexer.marker_task_count = 0
        indexer.processed_events = 0
        indexer.batch_count = 0
        indexer.total_processing_ms = 0.0
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

    def test_batch_uses_one_product_and_one_marker_task(self):
        indexer = self.indexer()
        indexer.redis = RecordingRedis()
        indexer.current_document = lambda document_id: None
        messages = [
            ("1-0", {"event": json.dumps(envelope(op="c", lsn="0/20"))}),
            ("2-0", {"event": json.dumps({**envelope(op="c", lsn="0/21"), "event_id": "event-2", "pk": {"id": 8}, "after": {"id": 8, "name": "other"}})}),
        ]

        indexer.process_batch(messages)

        self.assertEqual(len(indexer.index.batches), 1)
        self.assertEqual(len(indexer.visibility_index.batches), 1)
        self.assertEqual(len(indexer.visibility_index.batches[0]), 2)

    def test_batch_update_merge_never_leaks_sdk_internal_state(self):
        indexer = self.indexer()
        indexer.redis = RecordingRedis()
        current = SdkDocumentLike(
            {
                "id": 7,
                "sku": "sku-7",
                "name": "old name",
                "description": "kept by the merge",
                "category": "books",
                "price_cents": 100,
                "in_stock": True,
                "updated_at": "2026-09-09 10:00:00+00",
                "_lsn": "0/10",
                "_deleted": False,
            }
        )
        indexer.index = GetDocumentIndex(current)
        update = {
            **envelope(op="u", lsn="0/20"),
            "after": {"id": 7, "name": "new name"},
        }

        indexer.process_batch([("1-0", {"event": json.dumps(update)})])

        self.assertEqual(len(indexer.index.batches), 1)
        merged = indexer.index.batches[0][0]
        self.assertEqual(
            merged,
            {
                "id": 7,
                "sku": "sku-7",
                "name": "new name",
                "description": "kept by the merge",
                "category": "books",
                "price_cents": 100,
                "in_stock": True,
                "updated_at": "2026-09-09 10:00:00+00",
                "_lsn": "0/20",
                "_deleted": False,
            },
        )
        self.assertFalse(
            any(key.startswith("_Document__") for key in merged),
            f"SDK internal state leaked into merged document: {merged}",
        )

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
        indexer.dlq_maxlen = 50000
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

    # ------------------------------------------------------------------
    # Pending-reclaim behaviour (EMM-68)
    # ------------------------------------------------------------------

    def test_reclaim_calls_process_message_for_each_pending_entry(self):
        indexer = self.indexer()
        processed = []
        indexer.process_message = lambda msg_id, fields: processed.append(msg_id)

        class ClaimRedis(RecordingRedis):
            def xautoclaim(self, stream, group, consumer, **kwargs):
                # Return (next_start_id, [(msg_id, fields)], [deleted_ids])
                return ("0-0", [("5-0", {"event": "{}"}), ("6-0", {"event": "{}"})], [])

        indexer.redis = ClaimRedis()
        indexer.stream = "cdc_events"
        indexer.group = "indexers"
        indexer.consumer = "test-consumer"
        indexer.claim_idle_ms = 30000
        indexer.metrics_lock = __import__("threading").Lock()
        indexer.pending_claimed = 0

        count = indexer.reclaim_pending()

        self.assertEqual(count, 2)
        self.assertEqual(sorted(processed), ["5-0", "6-0"])
        self.assertEqual(indexer.pending_claimed, 2)

    def test_reclaim_returns_zero_when_no_pending_entries(self):
        indexer = self.indexer()

        class EmptyClaimRedis(RecordingRedis):
            def xautoclaim(self, stream, group, consumer, **kwargs):
                return ("0-0", [], [])

        indexer.redis = EmptyClaimRedis()
        indexer.stream = "cdc_events"
        indexer.group = "indexers"
        indexer.consumer = "test-consumer"
        indexer.claim_idle_ms = 30000
        indexer.metrics_lock = __import__("threading").Lock()
        indexer.pending_claimed = 0

        count = indexer.reclaim_pending()

        self.assertEqual(count, 0)
        self.assertEqual(indexer.pending_claimed, 0)

    def test_duplicate_events_are_idempotent_via_lsn_ordering(self):
        # Replaying an event whose LSN is already stored must not overwrite.
        indexer = self.indexer(stored_lsn="0/30")

        result = indexer.apply(envelope(op="u", lsn="0/20"))

        self.assertEqual(result, "superseded")
        self.assertEqual(indexer.index.batches, [])
        self.assertEqual(indexer.index.updated_batches, [])


# ------------------------------------------------------------------
# pgoutput decoder coverage (EMM-75)
# ------------------------------------------------------------------

import struct  # noqa: E402 — import inside module; acceptable in test file

from services.shared.pgoutput_decoder import DecodeError  # noqa: E402


def _make_buf(*parts: bytes) -> bytes:
    return b"".join(parts)


def _i32(n: int) -> bytes:
    return struct.pack("!i", n)


def _i64(n: int) -> bytes:
    return struct.pack("!q", n)


def _u8(n: int) -> bytes:
    return struct.pack("!B", n)


def _cstring(s: str) -> bytes:
    return s.encode() + b"\x00"


def _relation_payload(rel_id: int = 1, schema: str = "public", table: str = "t") -> bytes:
    # R <rel_id i32> <schema cstring> <table cstring> <replica_identity u8>
    # <num_columns i16> <flags u8> <col_name cstring> <type_oid i32> <type_mod i32>
    return (
        b"R"
        + _i32(rel_id)
        + _cstring(schema)
        + _cstring(table)
        + _u8(0)  # replica identity = DEFAULT
        + struct.pack("!h", 1)  # 1 column
        + _u8(1)  # is_key flag
        + _cstring("id")
        + _i32(23)  # int4 OID
        + _i32(-1)  # type modifier
    )


def _begin_payload(xid: int = 1) -> bytes:
    # B <final_lsn i64> <commit_ts i64> <xid i32>
    return b"B" + _i64(0) + _i64(0) + _i32(xid)


def _commit_payload() -> bytes:
    # C <flags u8> <commit_lsn i64> <end_lsn i64> <commit_ts i64>
    return b"C" + _u8(0) + _i64(32) + _i64(40) + _i64(0)


def _insert_payload(rel_id: int = 1, pk_value: int = 7) -> bytes:
    # I <rel_id i32> N <tuple>
    value = str(pk_value).encode()
    return (
        b"I"
        + _i32(rel_id)
        + b"N"
        + struct.pack("!h", 1)  # 1 column in tuple
        + b"t"  # text kind
        + _i32(len(value))
        + value
    )


class PgoutputDecoderTest(unittest.TestCase):
    def _decoder_with_relation(self) -> PgoutputDecoder:
        decoder = PgoutputDecoder("catalog")
        decoder.feed(_relation_payload())
        return decoder

    def _full_transaction(self, decoder: PgoutputDecoder, inner: bytes) -> list[dict]:
        decoder.feed(_begin_payload())
        decoder.feed(inner)
        return decoder.feed(_commit_payload())

    def test_begin_returns_empty_and_sets_xid(self):
        decoder = PgoutputDecoder("catalog")
        result = decoder.feed(_begin_payload(xid=99))
        self.assertEqual(result, [])
        self.assertEqual(decoder.xid, 99)

    def test_relation_is_registered_and_returns_empty(self):
        decoder = PgoutputDecoder("catalog")
        result = decoder.feed(_relation_payload(rel_id=5, schema="s", table="t"))
        self.assertEqual(result, [])
        self.assertIn(5, decoder.relations)
        self.assertEqual(decoder.relations[5].table, "t")

    def test_insert_produces_committed_event(self):
        decoder = self._decoder_with_relation()
        events = self._full_transaction(decoder, _insert_payload(pk_value=42))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["op"], "c")
        self.assertEqual(events[0]["pk"], {"id": 42})

    def test_truncate_passthrough_returns_empty(self):
        # T (truncate) is silently skipped — it carries no row data we care about.
        decoder = PgoutputDecoder("catalog")
        decoder.feed(_begin_payload())
        result = decoder.feed(b"T" + b"\x00" * 8)  # arbitrary trailing bytes
        self.assertEqual(result, [])

    def test_type_passthrough_returns_empty(self):
        decoder = PgoutputDecoder("catalog")
        decoder.feed(_begin_payload())
        result = decoder.feed(b"Y" + b"\x00" * 8)
        self.assertEqual(result, [])

    def test_origin_passthrough_returns_empty(self):
        decoder = PgoutputDecoder("catalog")
        decoder.feed(_begin_payload())
        result = decoder.feed(b"O" + b"\x00" * 8)
        self.assertEqual(result, [])

    def test_unknown_message_type_raises_decode_error(self):
        decoder = PgoutputDecoder("catalog")
        decoder.feed(_begin_payload())
        with self.assertRaises(DecodeError):
            decoder.feed(b"Z" + b"\x00" * 4)

    def test_empty_payload_raises_decode_error(self):
        decoder = PgoutputDecoder("catalog")
        with self.assertRaises(DecodeError):
            decoder.feed(b"")

    def test_unknown_relation_id_raises_decode_error(self):
        decoder = self._decoder_with_relation()
        decoder.feed(_begin_payload())
        with self.assertRaises(DecodeError):
            # Insert referencing rel_id=99 which was never registered
            decoder.feed(_insert_payload(rel_id=99))

    def test_null_column_value_produces_none(self):
        decoder = PgoutputDecoder("catalog")
        # Relation with two columns, second nullable
        rel_payload = (
            b"R"
            + _i32(2)
            + _cstring("public")
            + _cstring("products")
            + _u8(0)
            + struct.pack("!h", 2)
            + _u8(1) + _cstring("id") + _i32(23) + _i32(-1)
            + _u8(0) + _cstring("name") + _i32(25) + _i32(-1)
        )
        decoder.feed(rel_payload)
        insert_payload = (
            b"I"
            + _i32(2)
            + b"N"
            + struct.pack("!h", 2)
            + b"t" + _i32(1) + b"5"  # id = 5
            + b"n"                    # name = NULL
        )
        events = self._full_transaction(decoder, insert_payload)
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["after"]["name"])
        self.assertEqual(events[0]["after"]["id"], 5)

    def test_binary_column_value_produces_hex_string(self):
        decoder = PgoutputDecoder("catalog")
        rel_payload = (
            b"R"
            + _i32(3)
            + _cstring("public")
            + _cstring("blobs")
            + _u8(0)
            + struct.pack("!h", 1)
            + _u8(1) + _cstring("data") + _i32(17) + _i32(-1)
        )
        decoder.feed(rel_payload)
        raw_bytes = b"\xde\xad\xbe\xef"
        insert_payload = (
            b"I"
            + _i32(3)
            + b"N"
            + struct.pack("!h", 1)
            + b"b" + _i32(4) + raw_bytes
        )
        events = self._full_transaction(decoder, insert_payload)
        self.assertEqual(events[0]["after"]["data"], raw_bytes.hex())

    def test_integer_oids_are_decoded_as_int(self):
        decoder = PgoutputDecoder("catalog")
        for oid, value, expected in [(20, b"9876543210", 9876543210), (21, b"32767", 32767), (23, b"42", 42)]:
            decoder.relations.clear()
            rel_payload = (
                b"R"
                + _i32(10)
                + _cstring("public")
                + _cstring("nums")
                + _u8(0)
                + struct.pack("!h", 1)
                + _u8(1) + _cstring("n") + _i32(oid) + _i32(-1)
            )
            decoder.feed(rel_payload)
            ins = (
                b"I" + _i32(10) + b"N"
                + struct.pack("!h", 1)
                + b"t" + _i32(len(value)) + value
            )
            events = self._full_transaction(decoder, ins)
            self.assertIsInstance(events[0]["after"]["n"], int)
            self.assertEqual(events[0]["after"]["n"], expected)
            decoder.pending = []

    def test_boolean_oid_decoded_correctly(self):
        decoder = PgoutputDecoder("catalog")
        rel_payload = (
            b"R"
            + _i32(11)
            + _cstring("public")
            + _cstring("flags")
            + _u8(0)
            + struct.pack("!h", 1)
            + _u8(1) + _cstring("active") + _i32(16) + _i32(-1)
        )
        decoder.feed(rel_payload)
        for raw, expected in [(b"t", True), (b"f", False)]:
            decoder.pending = []
            ins = (
                b"I" + _i32(11) + b"N"
                + struct.pack("!h", 1)
                + b"t" + _i32(len(raw)) + raw
            )
            events = self._full_transaction(decoder, ins)
            self.assertEqual(events[0]["after"]["active"], expected)

    def test_uncommitted_pending_is_cleared_on_begin(self):
        decoder = self._decoder_with_relation()
        decoder.feed(_begin_payload())
        decoder.feed(_insert_payload())
        self.assertEqual(len(decoder.pending), 1)
        # New BEGIN clears pending from abandoned transaction
        decoder.feed(_begin_payload(xid=2))
        self.assertEqual(len(decoder.pending), 0)


if __name__ == "__main__":
    unittest.main()
