"""Decode the subset of PostgreSQL pgoutput used by the pipeline."""

from __future__ import annotations

import struct
import time
import hashlib
from dataclasses import dataclass
from typing import Any

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

    def u8(self) -> int: return self.take(1)[0]
    def i16(self) -> int: return struct.unpack("!h", self.take(2))[0]
    def i32(self) -> int: return struct.unpack("!i", self.take(4))[0]
    def i64(self) -> int: return struct.unpack("!q", self.take(8))[0]

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
        tag, buf = chr(payload[0]), Buffer(payload[1:])
        if tag == "B":
            buf.i64(); buf.i64(); self.xid = buf.i32(); self.pending = []; return []
        if tag == "C":
            buf.u8(); commit_lsn = buf.i64(); buf.i64(); commit_ts_us = PG_EPOCH_US + buf.i64()
            events = [
                self._committed_event(change, commit_lsn, commit_ts_us, sequence)
                for sequence, change in enumerate(self.pending)
            ]
            self.pending = []; self.xid = None; return events
        if tag == "R": self._relation(buf); return []
        if tag == "I":
            relation = self._known_relation(buf.i32())
            if chr(buf.u8()) != "N": raise DecodeError("insert message has no new tuple")
            self.pending.append(RowChange("c", relation, self._tuple(buf, relation), None, time.time_ns() // 1_000)); return []
        if tag == "U":
            relation = self._known_relation(buf.i32()); marker = chr(buf.u8())
            if marker in ("K", "O"): self._tuple(buf, relation); marker = chr(buf.u8())
            if marker != "N": raise DecodeError("update message has no new tuple")
            self.pending.append(RowChange("u", relation, self._tuple(buf, relation), None, time.time_ns() // 1_000)); return []
        if tag == "D":
            relation = self._known_relation(buf.i32()); marker = chr(buf.u8())
            if marker not in ("K", "O"): raise DecodeError("delete message has no old/key tuple")
            self.pending.append(RowChange("d", relation, None, self._tuple(buf, relation), time.time_ns() // 1_000)); return []
        if tag in ("O", "Y", "T"): return []
        raise DecodeError(f"unsupported pgoutput message type {tag!r}")

    def _relation(self, buf: Buffer) -> None:
        relation_id, schema, table = buf.i32(), buf.cstring(), buf.cstring(); buf.u8()
        columns = []
        for _ in range(buf.i16()):
            flags, name, type_oid = buf.u8(), buf.cstring(), buf.i32(); buf.i32()
            columns.append(Column(name, type_oid, bool(flags & 1)))
        self.relations[relation_id] = Relation(relation_id, schema, table, tuple(columns))

    def _known_relation(self, relation_id: int) -> Relation:
        try: return self.relations[relation_id]
        except KeyError as exc: raise DecodeError(f"unknown relation id {relation_id}") from exc

    def _tuple(self, buf: Buffer, relation: Relation) -> dict[str, Any]:
        count = buf.i16()
        if count != len(relation.columns): raise DecodeError(f"tuple has {count} columns; relation has {len(relation.columns)}")
        row = {}
        for column in relation.columns:
            kind = chr(buf.u8())
            if kind == "n": row[column.name] = None
            elif kind == "u": continue
            elif kind in ("t", "b"):
                raw = buf.take(buf.i32()); row[column.name] = decode_text(column.type_oid, raw) if kind == "t" else raw.hex()
            else: raise DecodeError(f"unknown tuple data kind {kind!r}")
        return row

    def _committed_event(
        self,
        change: RowChange,
        commit_lsn: int,
        commit_ts_us: int,
        sequence: int,
    ) -> dict[str, Any]:
        row = change.after if change.after is not None else change.before or {}
        commit_lsn_string = lsn_string(commit_lsn)
        identity = (
            f"{self.database}|{change.relation.schema}|{change.relation.table}|"
            f"{commit_lsn_string}|{self.xid}|{sequence}"
        )
        event_id = hashlib.sha256(identity.encode()).hexdigest()
        return {"event_id": event_id, "op": change.op, "source": {"db": self.database, "schema": change.relation.schema, "table": change.relation.table}, "pk": {c.name: row[c.name] for c in change.relation.columns if c.is_key and c.name in row}, "after": change.after, "before": change.before, "commit_lsn": commit_lsn_string, "commit_ts_us": commit_ts_us, "xid": self.xid, "captured_ts_us": change.captured_ts_us}
