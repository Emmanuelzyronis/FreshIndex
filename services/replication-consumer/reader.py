"""Minimal pgoutput reader.

This stage decodes committed row changes and writes diagnostic JSON to stdout.
It deliberately does not publish CDCEvent envelopes: published_ts_us must be
the timestamp of a real Redis XADD, which belongs to the next stage.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any

import psycopg2
from psycopg2.extras import LogicalReplicationConnection, StopReplication


PG_EPOCH_US = 946_684_800_000_000


class DecodeError(RuntimeError):
    pass


class Buffer:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.payload):
            raise DecodeError("truncated pgoutput message")
        value = self.payload[self.offset:end]
        self.offset = end
        return value

    def u8(self) -> int:
        return self.take(1)[0]

    def i16(self) -> int:
        return struct.unpack("!h", self.take(2))[0]

    def i32(self) -> int:
        return struct.unpack("!i", self.take(4))[0]

    def i64(self) -> int:
        return struct.unpack("!q", self.take(8))[0]

    def cstring(self) -> str:
        end = self.payload.find(b"\x00", self.offset)
        if end < 0:
            raise DecodeError("unterminated pgoutput string")
        value = self.payload[self.offset:end].decode("utf-8")
        self.offset = end + 1
        return value


@dataclass(frozen=True)
class Column:
    name: str
    type_oid: int
    is_key: bool


@dataclass(frozen=True)
class Relation:
    relation_id: int
    schema: str
    table: str
    columns: tuple[Column, ...]


@dataclass
class RowChange:
    op: str
    relation: Relation
    after: dict[str, Any] | None
    before: dict[str, Any] | None
    captured_ts_us: int


def lsn_string(lsn: int) -> str:
    return f"{lsn >> 32:X}/{lsn & 0xFFFFFFFF:X}"


def epoch_us(pg_timestamp_us: int) -> int:
    return PG_EPOCH_US + pg_timestamp_us


def decode_text(type_oid: int, raw: bytes) -> Any:
    text = raw.decode("utf-8")
    if type_oid in (20, 21, 23):
        return int(text)
    if type_oid == 16:
        return text == "t"
    return text


class PgoutputDecoder:
    def __init__(self, database: str):
        self.database = database
        self.relations: dict[int, Relation] = {}
        self.xid: int | None = None
        self.pending: list[RowChange] = []

    def feed(self, payload: bytes) -> list[dict[str, Any]]:
        if not payload:
            raise DecodeError("empty pgoutput message")
        tag = chr(payload[0])
        buf = Buffer(payload[1:])

        if tag == "B":
            buf.i64()  # final LSN, informative only
            buf.i64()  # transaction commit time is authoritative at Commit
            self.xid = buf.i32()
            self.pending = []
            return []
        if tag == "C":
            buf.u8()  # flags
            commit_lsn = buf.i64()
            buf.i64()  # transaction end LSN
            commit_ts_us = epoch_us(buf.i64())
            events = [
                self._committed_event(change, commit_lsn, commit_ts_us)
                for change in self.pending
            ]
            self.pending = []
            self.xid = None
            return events
        if tag == "R":
            self._relation(buf)
            return []
        if tag == "I":
            relation = self._known_relation(buf.i32())
            if chr(buf.u8()) != "N":
                raise DecodeError("insert message has no new tuple")
            self.pending.append(
                RowChange("c", relation, self._tuple(buf, relation), None, time.time_ns() // 1_000)
            )
            return []
        if tag == "U":
            relation = self._known_relation(buf.i32())
            marker = chr(buf.u8())
            if marker in ("K", "O"):
                self._tuple(buf, relation)
                marker = chr(buf.u8())
            if marker != "N":
                raise DecodeError("update message has no new tuple")
            self.pending.append(
                RowChange("u", relation, self._tuple(buf, relation), None, time.time_ns() // 1_000)
            )
            return []
        if tag == "D":
            relation = self._known_relation(buf.i32())
            marker = chr(buf.u8())
            if marker not in ("K", "O"):
                raise DecodeError("delete message has no old/key tuple")
            self.pending.append(
                RowChange("d", relation, None, self._tuple(buf, relation), time.time_ns() // 1_000)
            )
            return []
        if tag in ("O", "Y", "T"):
            return []
        raise DecodeError(f"unsupported pgoutput message type {tag!r}")

    def _relation(self, buf: Buffer) -> None:
        relation_id = buf.i32()
        schema = buf.cstring()
        table = buf.cstring()
        buf.u8()  # replica identity
        columns = []
        for _ in range(buf.i16()):
            flags = buf.u8()
            name = buf.cstring()
            type_oid = buf.i32()
            buf.i32()  # type modifier
            columns.append(Column(name, type_oid, bool(flags & 1)))
        self.relations[relation_id] = Relation(
            relation_id, schema, table, tuple(columns)
        )

    def _known_relation(self, relation_id: int) -> Relation:
        try:
            return self.relations[relation_id]
        except KeyError as exc:
            raise DecodeError(f"unknown relation id {relation_id}") from exc

    def _tuple(self, buf: Buffer, relation: Relation) -> dict[str, Any]:
        count = buf.i16()
        if count != len(relation.columns):
            raise DecodeError(
                f"tuple has {count} columns; relation has {len(relation.columns)}"
            )
        row: dict[str, Any] = {}
        for column in relation.columns:
            kind = chr(buf.u8())
            if kind == "n":
                row[column.name] = None
            elif kind == "u":
                continue
            elif kind in ("t", "b"):
                raw = buf.take(buf.i32())
                row[column.name] = (
                    decode_text(column.type_oid, raw) if kind == "t" else raw.hex()
                )
            else:
                raise DecodeError(f"unknown tuple data kind {kind!r}")
        return row

    def _committed_event(
        self, change: RowChange, commit_lsn: int, commit_ts_us: int
    ) -> dict[str, Any]:
        row = change.after if change.after is not None else change.before or {}
        return {
            "op": change.op,
            "source": {
                "db": self.database,
                "schema": change.relation.schema,
                "table": change.relation.table,
            },
            "pk": {
                column.name: row[column.name]
                for column in change.relation.columns
                if column.is_key and column.name in row
            },
            "after": change.after,
            "before": change.before,
            "commit_lsn": lsn_string(commit_lsn),
            "commit_ts_us": commit_ts_us,
            "xid": self.xid,
            "captured_ts_us": change.captured_ts_us,
        }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Decode a pgoutput replication slot")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--slot", default=os.environ.get("CDC_SLOT", "cdc_products_slot"))
    parser.add_argument("--publication", default=os.environ.get("CDC_PUBLICATION", "cdc_pub"))
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
    emitted = 0

    connection = psycopg2.connect(
        args.dsn, connection_factory=LogicalReplicationConnection
    )
    cursor = connection.cursor()

    def consume(message: Any) -> None:
        nonlocal emitted
        for event in decoder.feed(bytes(message.payload)):
            print(json.dumps(event, separators=(",", ":")), flush=True)
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

